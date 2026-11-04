/* Audio plumbing. The phone talks 8 kHz 16-bit mono PCM (CVSD) over Bluetooth SCO; the network side is G.711 u-law RTP.
 *  - phone mic  -> from_bt ring -> (echo task | RTP sender)
 *  - network    -> to_bt ring   -> controller callback -> phone speaker */
#include <string.h>
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_hf_ag_api.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/ringbuf.h"
#include "freertos/semphr.h"
#include "gw.h"

#define RB_SIZE 6400  /* 0.4 s of 8 kHz 16-bit */
#define TICK_US 7500  /* one SCO block: 7.5 ms = 60 samples = 120 bytes */

static RingbufHandle_t s_to_bt, s_from_bt;
static esp_timer_handle_t s_timer;
static SemaphoreHandle_t s_tick;
static TaskHandle_t s_pump;
static volatile bool s_up;

void audio_init(void)
{
    s_to_bt = xRingbufferCreate(RB_SIZE, RINGBUF_TYPE_BYTEBUF);
    s_from_bt = xRingbufferCreate(RB_SIZE, RINGBUF_TYPE_BYTEBUF);
    s_tick = xSemaphoreCreateBinary();
}

static void tick_cb(void *arg)
{
    xSemaphoreGive(s_tick);
}

static void pump_task(void *arg)
{
    while (s_up) {
        if (xSemaphoreTake(s_tick, pdMS_TO_TICKS(100))) {
            esp_hf_ag_outgoing_data_ready();
        }
    }
    s_pump = NULL;
    vTaskDelete(NULL);
}

void audio_bt_up(void)
{
    if (s_up) {
        return;
    }
    audio_flush();
    esp_hf_ag_register_data_callback(audio_in_cb, audio_out_cb);
    s_up = true;
    const esp_timer_create_args_t a = {.callback = tick_cb, .name = "sco"};
    esp_timer_create(&a, &s_timer);
    esp_timer_start_periodic(s_timer, TICK_US);
    xTaskCreate(pump_task, "scopump", 3072, NULL, configMAX_PRIORITIES - 3, &s_pump);
}

void audio_bt_down(void)
{
    if (!s_up) {
        return;
    }
    s_up = false;
    if (s_timer) {
        esp_timer_stop(s_timer);
        esp_timer_delete(s_timer);
        s_timer = NULL;
    }
    audio_flush();
}

uint32_t audio_out_cb(uint8_t *buf, uint32_t len)
{
    size_t got = 0;
    if (s_to_bt) {
        uint8_t *d = xRingbufferReceiveUpTo(s_to_bt, &got, 0, len);
        if (d) {
            memcpy(buf, d, got);
            vRingbufferReturnItem(s_to_bt, d);
        }
    }
    if (got < len) {
        memset(buf + got, 0, len - got);  /* underrun: silence */
    }
    return len;
}

void audio_in_cb(const uint8_t *buf, uint32_t len)
{
    if (s_from_bt) {
        xRingbufferSend(s_from_bt, buf, len, 0);  /* full: drop */
    }
}

void audio_to_bt(const int16_t *pcm, int n)
{
    if (s_to_bt && s_up) {
        xRingbufferSend(s_to_bt, pcm, n * 2, 0);
    }
}

int audio_from_bt(int16_t *pcm, int n, int timeout_ms)
{
    size_t got = 0;
    if (!s_from_bt) {
        return 0;
    }
    uint8_t *d = xRingbufferReceiveUpTo(s_from_bt, &got, pdMS_TO_TICKS(timeout_ms), n * 2);
    if (!d) {
        return 0;
    }
    memcpy(pcm, d, got);
    vRingbufferReturnItem(s_from_bt, d);
    return got / 2;
}

void audio_flush(void)
{
    size_t got;
    void *d;
    while (s_to_bt && (d = xRingbufferReceiveUpTo(s_to_bt, &got, 0, RB_SIZE))) {
        vRingbufferReturnItem(s_to_bt, d);
    }
    while (s_from_bt && (d = xRingbufferReceiveUpTo(s_from_bt, &got, 0, RB_SIZE))) {
        vRingbufferReturnItem(s_from_bt, d);
    }
}

/* ---- G.711 u-law ---- */
uint8_t lin2ulaw(int16_t s)
{
    const int BIAS = 0x84, CLIP = 32635;
    int sign = (s >> 8) & 0x80;
    int v = s;
    if (sign) v = -v;
    if (v > CLIP) v = CLIP;
    v += BIAS;
    int exp = 7;
    for (int mask = 0x4000; (v & mask) == 0 && exp > 0; exp--, mask >>= 1) {
    }
    int mant = (v >> (exp + 3)) & 0x0F;
    return (uint8_t)~(sign | (exp << 4) | mant);
}

int16_t ulaw2lin(uint8_t u)
{
    u = ~u;
    int t = ((u & 0x0F) << 3) + 0x84;
    t <<= (u & 0x70) >> 4;
    return (int16_t)((u & 0x80) ? (0x84 - t) : (t - 0x84));
}
