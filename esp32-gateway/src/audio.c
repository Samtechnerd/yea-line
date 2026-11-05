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
static volatile uint32_t s_drops, s_gaps, s_blocks;
static volatile bool s_seen, s_playing;
static volatile uint32_t s_cb_len, s_in_bytes, s_in_drop, s_out_calls, s_ticks;
static int64_t s_t_up;

#define PREBUF 640    /* start playing once 40 ms is queued */
#define HIGH_WATER 3200  /* more than 200 ms queued: skip ahead */

void audio_init(void)
{
    s_to_bt = xRingbufferCreate(RB_SIZE, RINGBUF_TYPE_BYTEBUF);
    s_from_bt = xRingbufferCreate(RB_SIZE, RINGBUF_TYPE_BYTEBUF);
    s_tick = xSemaphoreCreateBinary();
}

static void tick_cb(void *arg)
{
    s_ticks++;
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
    s_drops = s_gaps = s_blocks = 0;
    s_seen = s_playing = false;
    s_in_bytes = s_in_drop = s_out_calls = s_ticks = 0;
    s_t_up = esp_timer_get_time();
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
    {
        float secs = (esp_timer_get_time() - s_t_up) / 1e6f;
        if (secs > 1) {
            gw_log("Audio link %.0f s: phone mic %.0f bytes/s (%u lost), speaker asked %.0f times/s, timer ticks %.0f/s",
                   secs, s_in_bytes / secs, (unsigned)s_in_drop, s_out_calls / secs, s_ticks / secs);
        }
    }
    if (s_blocks) {
        gw_log("Audio: %u blocks from the network, %u skipped (too much queued), %u gaps (ran dry), %u bytes per Bluetooth block",
               (unsigned)s_blocks, (unsigned)s_drops, (unsigned)s_gaps, (unsigned)s_cb_len);
    }
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
    s_cb_len = len;
    s_out_calls++;
    if (s_to_bt) {
        size_t queued = RB_SIZE - xRingbufferGetCurFreeSize(s_to_bt);
        if (!s_playing && queued >= PREBUF) s_playing = true;
        while (s_playing && got < len) {  /* the ring can hand back a short piece at its end: ask again */
            size_t n = 0;
            uint8_t *d = xRingbufferReceiveUpTo(s_to_bt, &n, 0, len - got);
            if (!d) break;
            memcpy(buf + got, d, n);
            vRingbufferReturnItem(s_to_bt, d);
            got += n;
        }
        if (s_playing && got < len) {
            s_playing = false;  /* ran dry: wait until a little has built up again instead of stuttering */
            if (s_seen) s_gaps++;
        }
        if (got) s_seen = true;
    }
    if (got < len) {
        memset(buf + got, 0, len - got);
    }
    return len;
}

void audio_in_cb(const uint8_t *buf, uint32_t len)
{
    if (s_from_bt) {
        s_in_bytes += len;
        if (xRingbufferSend(s_from_bt, buf, len, 0) != pdTRUE) s_in_drop++;  /* full: drop */
    }
}

void audio_to_bt(const int16_t *pcm, int n)
{
    if (s_to_bt && s_up) {
        s_blocks++;
        if (RB_SIZE - xRingbufferGetCurFreeSize(s_to_bt) > HIGH_WATER) {
            s_drops++;  /* far too much queued: throw away the oldest audio to catch up */
            size_t got = 0;
            for (int i = 0; i < 4 && got < 1280; i++) {
                size_t m = 0;
                void *d = xRingbufferReceiveUpTo(s_to_bt, &m, 0, 1280 - got);
                if (!d) break;
                vRingbufferReturnItem(s_to_bt, d);
                got += m;
            }
        }
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
