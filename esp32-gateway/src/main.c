/* Yea-Line ESP32 gateway: a Yealink MP50 pairs over Bluetooth as if the ESP32 were its mobile phone;
 * numbers dialled on the phone become SIP calls on the PBX, or the built-in echo test. */
#include <string.h>
#include <math.h>
#include "esp_log.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_timer.h"
#include "gw.h"

typedef enum { CS_IDLE, CS_DIAL, CS_SIP, CS_ECHO, CS_RING } cstate_t;
static int64_t s_ring_until_ms;
static cstate_t s_state = CS_IDLE;
static char s_num[32];
static QueueHandle_t s_q;
static volatile bool s_echo_run;

const char *call_state_str(void)
{
    switch (s_state) {
    case CS_DIAL: return "calling";
    case CS_SIP: return "in a call";
    case CS_ECHO: return "echo test";
    case CS_RING: return "ringing the phone";
    default: return "idle";
    }
}

void app_post(app_evt_type_t type, int value, const char *text)
{
    app_evt_t e = {.type = type, .value = value};
    strlcpy(e.text, text ? text : "", sizeof(e.text));
    if (s_q) xQueueSend(s_q, &e, 0);
}

static bool is_echo(const char *n)
{
    const char *a = n, *b = g_cfg.echo_num;
    if (!b[0]) return false;
    if (*a == '#' || *a == '*') a++;
    if (*b == '#' || *b == '*') b++;
    return strcmp(n, g_cfg.echo_num) == 0 || strcmp(a, b) == 0;
}

/* ---- echo test: a beep, then whatever you say comes back about a second later ---- */
static void echo_task(void *arg)
{
    enum { N = 12000, DELAY = 8000 };
    static int16_t dl[N];
    int16_t in[160], out[160];
    int pos = 0;
    memset(dl, 0, sizeof(dl));
    for (int i = 0; i < 30 && s_echo_run && !bt_audio_up(); i++) vTaskDelay(pdMS_TO_TICKS(100));
    if (bt_audio_up()) {
        int16_t beep[160];
        for (int blk = 0; blk < 12 && s_echo_run; blk++) {
            for (int i = 0; i < 160; i++) beep[i] = (int16_t)(9000 * sinf(6.2831853f * 1000 * (blk * 160 + i) / 8000.0f));
            audio_to_bt(beep, 160);
            vTaskDelay(pdMS_TO_TICKS(20));
        }
    }
    while (s_echo_run) {
        int got = audio_from_bt(in, 160, 100);
        for (int i = 0; i < got; i++) {
            out[i] = dl[(pos + N - DELAY) % N];
            dl[pos] = in[i];
            pos = (pos + 1) % N;
        }
        if (got) audio_to_bt(out, got);
    }
    vTaskDelete(NULL);
}

static void echo_stop(void) { s_echo_run = false; }

static void start_echo(void)
{
    s_state = CS_ECHO;
    s_echo_run = true;
    xTaskCreate(echo_task, "echo", 4096, NULL, 4, NULL);
}

static void end_all(bool tell_phone)
{
    echo_stop();
    if (s_state == CS_RING) {
        bt_ring_stop(s_num);
        s_state = CS_IDLE;
        return;
    }
    if (s_state == CS_DIAL || s_state == CS_SIP) sip_hangup();
    if (tell_phone && s_state != CS_IDLE) bt_call_end(s_num);
    s_state = CS_IDLE;
}

static void on_event(const app_evt_t *e)
{
    switch (e->type) {
    case EV_BT_DIAL:
        if (s_state != CS_IDLE) end_all(true);
        for (int i = 0; i < 30 && sip_call_state() != SIPC_NONE; i++) vTaskDelay(pdMS_TO_TICKS(50));  /* let the old call finish */
        strlcpy(s_num, e->text, sizeof(s_num));
        if (is_echo(s_num)) {
            gw_log("Echo test started");
            bt_call_active(s_num);
            start_echo();
        } else if (sip_call(s_num)) {
            s_state = CS_DIAL;
            bt_call_dialing(s_num);
        } else {
            gw_log(sip_reg_state() == SIP_REGISTERED ? "Cannot call %s: still finishing the last call" : "Cannot call %s: PBX not registered", s_num);
            bt_call_dialing(s_num);
            bt_call_end(s_num);
        }
        break;
    case EV_SIP_CALL:
        if (e->value == 1 && s_state == CS_DIAL) {
            bt_call_alerting(s_num);
        } else if (e->value == 2 && s_state == CS_DIAL) {
            s_state = CS_SIP;
            bt_call_active(s_num);
        } else if (e->value == 0 && (s_state == CS_DIAL || s_state == CS_SIP)) {
            bt_call_end(s_num);
            s_state = CS_IDLE;
        }
        break;
    case EV_BT_HANGUP:
        if (s_state != CS_IDLE) {
            gw_log("Phone hung up");
            end_all(true);
        }
        break;
    case EV_RING:
        if (s_state != CS_IDLE) {
            gw_log("Cannot ring: a call is in progress");
        } else if (!bt_slc_up()) {
            gw_log("Cannot ring: the phone is not connected");
        } else {
            strlcpy(s_num, "Yea-Line", sizeof(s_num));
            s_state = CS_RING;
            s_ring_until_ms = esp_timer_get_time() / 1000 + 30000;
            gw_log("Ringing the phone - pick up to answer");
            bt_ring(s_num);
        }
        break;
    case EV_BT_ANSWER:
        if (s_state == CS_RING) {
            gw_log("Phone answered - echo test");
            bt_ring_answered(s_num);
            start_echo();
        }
        break;
    case EV_BT_AUDIO:
        if (e->value == 0 && s_state != CS_IDLE) end_all(false);
        break;
    case EV_BT_SLC:
        if (e->value == 0 && s_state != CS_IDLE) end_all(false);
        break;
    default:
        break;
    }
}

static void app_task(void *arg)
{
    app_evt_t e;
    for (;;) {
        if (xQueueReceive(s_q, &e, pdMS_TO_TICKS(500))) on_event(&e);
        if (s_state == CS_RING && esp_timer_get_time() / 1000 > s_ring_until_ms) {
            gw_log("Ring timed out (no answer)");
            end_all(false);
        }
    }
}

void app_main(void)
{
    esp_err_t r = nvs_flash_init();
    if (r == ESP_ERR_NVS_NO_FREE_PAGES || r == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        r = nvs_flash_init();
    }
    ESP_ERROR_CHECK(r);
    cfg_load();
    s_q = xQueueCreate(16, sizeof(app_evt_t));
    xTaskCreate(app_task, "app", 4096, NULL, 5, NULL);
    audio_init();
    net_start();
    bt_start();
    sip_start();
    gw_log("Yea-Line gateway started");
}
