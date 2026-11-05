#pragma once
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

typedef struct {
    char wifi_ssid[33];
    char wifi_pass[65];
    char sip_server[64];
    uint16_t sip_port;
    char sip_user[32];
    char sip_pass[64];
    char echo_num[16];
} gw_cfg_t;

extern gw_cfg_t g_cfg;
void cfg_load(void);
void cfg_save(void);
bool cfg_get_peer(uint8_t bda[6]);
void cfg_set_peer(const uint8_t bda[6]);
void cfg_clear_peer(void);

/* log ring shown on the web page */
void gw_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
size_t gw_log_dump(char *out, size_t cap);

/* events into the call manager (main.c) */
typedef enum {
    EV_BT_SLC,      /* value: 1 connected, 0 disconnected */
    EV_BT_AUDIO,    /* value: 1 connected, 0 disconnected */
    EV_BT_DIAL,     /* text: number */
    EV_BT_ANSWER,   /* phone pressed answer (ATA) */
    EV_BT_HANGUP,   /* phone pressed hang up (CHUP) */
    EV_SIP_REG,     /* value: sip_reg_t */
    EV_SIP_CALL,    /* value: 1 ringing, 2 answered, 0 ended; text: reason */
    EV_RING,        /* web page: ring the phone */
    EV_SIP_INCOMING, /* text: caller number */
} app_evt_type_t;

typedef struct {
    app_evt_type_t type;
    int value;
    char text[48];
} app_evt_t;

void app_post(app_evt_type_t type, int value, const char *text);

/* ---- network + web page ---- */
void net_start(void);
bool net_connected(void);
const char *net_ip(void);

/* ---- bluetooth hands-free gateway ---- */
void bt_start(void);
void bt_pair_mode(int seconds);
bool bt_slc_up(void);
bool bt_audio_up(void);
const char *bt_state_str(void);
void bt_call_dialing(const char *num);
void bt_call_alerting(const char *num);
void bt_call_active(const char *num);
void bt_call_end(const char *num);
void bt_forget(void);
void bt_ring(const char *caller);
void bt_ring_answered(const char *caller);
void bt_ring_stop(const char *caller);
void bt_audio_connect(void);

/* ---- audio plumbing between Bluetooth SCO and the network ---- */
void audio_init(void);
void audio_bt_up(void);
void audio_bt_down(void);
uint32_t audio_out_cb(uint8_t *buf, uint32_t len);   /* controller wants PCM for the phone speaker */
void audio_in_cb(const uint8_t *buf, uint32_t len);  /* PCM arriving from the phone microphone */
void audio_to_bt(const int16_t *pcm, int n);
int audio_from_bt(int16_t *pcm, int n, int timeout_ms);
void audio_flush(void);
uint8_t lin2ulaw(int16_t s);
int16_t ulaw2lin(uint8_t u);

/* ---- SIP ---- */
typedef enum { SIP_UNREG, SIP_REGISTERING, SIP_REGISTERED, SIP_FAILED } sip_reg_t;
typedef enum { SIPC_NONE, SIPC_CALLING, SIPC_RINGING, SIPC_ACTIVE, SIPC_INRING } sip_call_t;
void sip_start(void);
sip_reg_t sip_reg_state(void);
sip_call_t sip_call_state(void);
const char *sip_reg_str(void);
bool sip_call(const char *number);
void sip_hangup(void);
bool sip_answer(void);
void sip_reconfigure(void);

/* ---- call manager ---- */
const char *call_state_str(void);
