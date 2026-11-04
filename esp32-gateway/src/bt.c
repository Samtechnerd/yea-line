/* Bluetooth hands-free gateway: the MP50 pairs with this ESP32 as if it were a mobile phone.
 * Dial / answer / hang-up from the phone arrive as events; the call manager (main.c) decides what to do. */
#include <string.h>
#include <stdio.h>
#include "esp_log.h"
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_bt_device.h"
#include "esp_gap_bt_api.h"
#include "esp_hf_ag_api.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "gw.h"

static esp_bd_addr_t s_peer;
static bool s_have_peer, s_slc, s_audio, s_stack_ready;
static volatile int s_pair_secs;
static const char *s_state = "starting";

bool bt_slc_up(void) { return s_slc; }
bool bt_audio_up(void) { return s_audio; }
const char *bt_state_str(void) { return s_state; }

static void set_scan(bool discoverable)
{
    esp_bt_gap_set_scan_mode(ESP_BT_CONNECTABLE, discoverable ? ESP_BT_GENERAL_DISCOVERABLE : ESP_BT_NON_DISCOVERABLE);
}

void bt_pair_mode(int seconds)
{
    s_pair_secs = seconds;
    if (s_stack_ready) {
        set_scan(true);
        gw_log("Bluetooth: pairing mode on for %d s", seconds);
        s_state = s_slc ? "connected" : "pairing";
    }
}

void bt_forget(void)
{
    if (s_have_peer) {
        esp_bt_gap_remove_bond_device(s_peer);
    }
    s_have_peer = false;
    cfg_clear_peer();
    gw_log("Bluetooth: forgot the paired phone");
    bt_pair_mode(120);
}

static void gap_cb(esp_bt_gap_cb_event_t event, esp_bt_gap_cb_param_t *p)
{
    switch (event) {
    case ESP_BT_GAP_AUTH_CMPL_EVT:
        if (p->auth_cmpl.stat == ESP_BT_STATUS_SUCCESS) {
            gw_log("Bluetooth: paired with %s", p->auth_cmpl.device_name);
            memcpy(s_peer, p->auth_cmpl.bda, 6);
            s_have_peer = true;
            cfg_set_peer(s_peer);
        } else {
            gw_log("Bluetooth: pairing failed (%d)", p->auth_cmpl.stat);
        }
        break;
    case ESP_BT_GAP_PIN_REQ_EVT: {
        esp_bt_pin_code_t pin = {'0', '0', '0', '0'};
        esp_bt_gap_pin_reply(p->pin_req.bda, true, 4, pin);
        break;
    }
    case ESP_BT_GAP_CFM_REQ_EVT:
        esp_bt_gap_ssp_confirm_reply(p->cfm_req.bda, true);
        break;
    default:
        break;
    }
}

static void hf_cb(esp_hf_cb_event_t event, esp_hf_cb_param_t *p)
{
    switch (event) {
    case ESP_HF_CONNECTION_STATE_EVT:
        if (p->conn_stat.state == ESP_HF_CONNECTION_STATE_SLC_CONNECTED) {
            memcpy(s_peer, p->conn_stat.remote_bda, 6);
            s_have_peer = true;
            s_slc = true;
            s_state = "connected";
            gw_log("Bluetooth: phone connected");
            app_post(EV_BT_SLC, 1, "");
        } else if (p->conn_stat.state == ESP_HF_CONNECTION_STATE_DISCONNECTED) {
            s_slc = false;
            s_audio = false;
            s_state = s_have_peer ? "waiting for phone" : "not paired";
            gw_log("Bluetooth: phone disconnected");
            audio_bt_down();
            app_post(EV_BT_SLC, 0, "");
        }
        break;
    case ESP_HF_AUDIO_STATE_EVT:
        if (p->audio_stat.state == ESP_HF_AUDIO_STATE_CONNECTED) {
            s_audio = true;
            audio_bt_up();
            gw_log("Bluetooth: call audio connected");
            app_post(EV_BT_AUDIO, 1, "");
        } else if (p->audio_stat.state == ESP_HF_AUDIO_STATE_DISCONNECTED) {
            s_audio = false;
            audio_bt_down();
            gw_log("Bluetooth: call audio closed");
            app_post(EV_BT_AUDIO, 0, "");
        }
        break;
    case ESP_HF_VOLUME_CONTROL_EVT:
        break;
    case ESP_HF_UNAT_RESPONSE_EVT:
        gw_log("Phone sent: %s", p->unat_rep.unat);
        esp_hf_ag_unknown_at_send(p->unat_rep.remote_addr, NULL);
        break;
    case ESP_HF_IND_UPDATE_EVT:
        esp_hf_ag_ciev_report(p->ind_upd.remote_addr, ESP_HF_IND_TYPE_SERVICE, 1);
        esp_hf_ag_ciev_report(p->ind_upd.remote_addr, ESP_HF_IND_TYPE_SIGNAL, 5);
        esp_hf_ag_ciev_report(p->ind_upd.remote_addr, ESP_HF_IND_TYPE_BATTCHG, 5);
        break;
    case ESP_HF_CIND_RESPONSE_EVT:
        esp_hf_ag_cind_response(p->cind_rep.remote_addr, 0, 0, 1, 5, 0, 5, 0);
        break;
    case ESP_HF_COPS_RESPONSE_EVT:
        esp_hf_ag_cops_response(p->cops_rep.remote_addr, "Yea-Line");
        break;
    case ESP_HF_CLCC_RESPONSE_EVT:
        esp_hf_ag_clcc_response(p->clcc_rep.remote_addr, 0, 0, 0, 0, 0, NULL, 0);
        break;
    case ESP_HF_CNUM_RESPONSE_EVT:
        esp_hf_ag_cnum_response(p->cnum_rep.remote_addr, "0", 129, ESP_HF_SUBSCRIBER_SERVICE_TYPE_VOICE);
        break;
    case ESP_HF_ATA_RESPONSE_EVT:
        app_post(EV_BT_ANSWER, 0, "");
        break;
    case ESP_HF_CHUP_RESPONSE_EVT:
        app_post(EV_BT_HANGUP, 0, "");
        break;
    case ESP_HF_DIAL_EVT:
        if (p->out_call.num_or_loc) {
            gw_log("Phone dialled: %s", p->out_call.num_or_loc);
            esp_hf_ag_cmee_send(p->out_call.remote_addr, ESP_HF_AT_RESPONSE_CODE_OK, ESP_HF_CME_AG_FAILURE);
            app_post(EV_BT_DIAL, 0, p->out_call.num_or_loc);
        } else {
            gw_log("Phone pressed redial (not supported)");
            esp_hf_ag_cmee_send(p->out_call.remote_addr, ESP_HF_AT_RESPONSE_CODE_CME, ESP_HF_CME_AG_FAILURE);
        }
        break;
    case ESP_HF_VTS_RESPONSE_EVT:
        gw_log("Phone sent tone: %s", p->vts_rep.code);
        break;
    default:
        break;
    }
}

/* ---- call state shown on the phone ---- */
void bt_call_dialing(const char *num)
{
    if (s_slc) esp_hf_ag_out_call(s_peer, 0, 0, ESP_HF_CALL_STATUS_NO_CALLS, ESP_HF_CALL_SETUP_STATUS_OUTGOING_DIALING, (char *)num, 0);
}

void bt_call_alerting(const char *num)
{
    if (s_slc) esp_hf_ag_out_call(s_peer, 0, 0, ESP_HF_CALL_STATUS_NO_CALLS, ESP_HF_CALL_SETUP_STATUS_OUTGOING_ALERTING, (char *)num, 0);
}

void bt_call_active(const char *num)
{
    if (s_slc) esp_hf_ag_out_call(s_peer, 1, 0, ESP_HF_CALL_STATUS_CALL_IN_PROGRESS, ESP_HF_CALL_SETUP_STATUS_IDLE, (char *)num, 0);
}

void bt_call_end(const char *num)
{
    if (s_slc) esp_hf_ag_end_call(s_peer, 0, 0, 0, 0, (char *)num, 0);
}

void bt_ring(const char *caller)
{
    if (s_slc) esp_hf_ag_answer_call(s_peer, 0, 0, ESP_HF_CALL_STATUS_NO_CALLS, ESP_HF_CALL_SETUP_STATUS_INCOMING, (char *)caller, 0);
}

void bt_ring_answered(const char *caller)
{
    if (s_slc) esp_hf_ag_answer_call(s_peer, 1, 0, ESP_HF_CALL_STATUS_CALL_IN_PROGRESS, ESP_HF_CALL_SETUP_STATUS_IDLE, (char *)caller, 0);
}

void bt_ring_stop(const char *caller)
{
    if (s_slc) esp_hf_ag_reject_call(s_peer, 0, 0, 0, 0, (char *)caller, 0);
}

static void link_task(void *arg)
{
    /* keeps pairing mode timed, and reconnects to the paired phone when it is out of reach */
    int tries = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        if (!s_stack_ready) {
            continue;
        }
        if (s_pair_secs > 0 && --s_pair_secs == 0) {
            set_scan(false);
            s_state = s_slc ? "connected" : (s_have_peer ? "waiting for phone" : "not paired");
            gw_log("Bluetooth: pairing mode off");
        }
        if (!s_slc && s_have_peer && ++tries >= 15) {
            tries = 0;
            esp_hf_ag_slc_connect(s_peer);
        }
        if (s_slc) {
            tries = 0;
        }
    }
}

void bt_start(void)
{
    esp_err_t r;
    ESP_ERROR_CHECK(esp_bt_controller_mem_release(ESP_BT_MODE_BLE));
    esp_bt_controller_config_t bt_cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    if ((r = esp_bt_controller_init(&bt_cfg)) != ESP_OK ||
        (r = esp_bt_controller_enable(ESP_BT_MODE_CLASSIC_BT)) != ESP_OK) {
        gw_log("Bluetooth controller failed: %s", esp_err_to_name(r));
        s_state = "error";
        return;
    }
    esp_bluedroid_config_t bd_cfg = BT_BLUEDROID_INIT_CONFIG_DEFAULT();
    if ((r = esp_bluedroid_init_with_cfg(&bd_cfg)) != ESP_OK || (r = esp_bluedroid_enable()) != ESP_OK) {
        gw_log("Bluetooth stack failed: %s", esp_err_to_name(r));
        s_state = "error";
        return;
    }
    esp_bt_gap_register_callback(gap_cb);
    esp_bt_gap_set_device_name("Yea-Line Gateway");
    esp_bt_cod_t cod = {0};
    cod.major = ESP_BT_COD_MAJOR_DEV_PHONE;
    cod.minor = 1; /* cellular */
    cod.service = ESP_BT_COD_SRVC_AUDIO | ESP_BT_COD_SRVC_TELEPHONY;
    esp_bt_gap_set_cod(cod, ESP_BT_INIT_COD);
    uint8_t iocap = ESP_BT_IO_CAP_NONE;
    esp_bt_gap_set_security_param(ESP_BT_SP_IOCAP_MODE, &iocap, sizeof(iocap));
    esp_bt_pin_code_t pin = {'0', '0', '0', '0'};
    esp_bt_gap_set_pin(ESP_BT_PIN_TYPE_VARIABLE, 4, pin);

    esp_hf_ag_register_callback(hf_cb);
    esp_hf_ag_init();
    s_stack_ready = true;

    s_have_peer = cfg_get_peer(s_peer);
    s_state = s_have_peer ? "waiting for phone" : "not paired";
    if (!s_have_peer) {
        bt_pair_mode(180);  /* nothing paired yet: be discoverable for a few minutes */
    } else {
        set_scan(false);
    }
    xTaskCreate(link_task, "btlink", 3072, NULL, 3, NULL);
    gw_log("Bluetooth ready (%s)", s_state);
}
