#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"
#include "gw.h"

gw_cfg_t g_cfg;

static void get_str(nvs_handle_t h, const char *key, char *dst, size_t cap, const char *def)
{
    size_t n = cap;
    if (nvs_get_str(h, key, dst, &n) != ESP_OK) {
        strlcpy(dst, def, cap);
    }
}

void cfg_load(void)
{
    nvs_handle_t h;
    memset(&g_cfg, 0, sizeof(g_cfg));
    if (nvs_open("gw", NVS_READONLY, &h) != ESP_OK) {
        g_cfg.sip_port = 5060;
        strlcpy(g_cfg.echo_num, "#878", sizeof(g_cfg.echo_num));
        return;
    }
    get_str(h, "ssid", g_cfg.wifi_ssid, sizeof(g_cfg.wifi_ssid), "");
    get_str(h, "wpass", g_cfg.wifi_pass, sizeof(g_cfg.wifi_pass), "");
    get_str(h, "sipsrv", g_cfg.sip_server, sizeof(g_cfg.sip_server), "");
    get_str(h, "sipuser", g_cfg.sip_user, sizeof(g_cfg.sip_user), "");
    get_str(h, "sippass", g_cfg.sip_pass, sizeof(g_cfg.sip_pass), "");
    get_str(h, "echo", g_cfg.echo_num, sizeof(g_cfg.echo_num), "#878");
    uint16_t port = 5060;
    nvs_get_u16(h, "sipport", &port);
    g_cfg.sip_port = port ? port : 5060;
    nvs_close(h);
}

void cfg_save(void)
{
    nvs_handle_t h;
    if (nvs_open("gw", NVS_READWRITE, &h) != ESP_OK) {
        return;
    }
    nvs_set_str(h, "ssid", g_cfg.wifi_ssid);
    nvs_set_str(h, "wpass", g_cfg.wifi_pass);
    nvs_set_str(h, "sipsrv", g_cfg.sip_server);
    nvs_set_str(h, "sipuser", g_cfg.sip_user);
    nvs_set_str(h, "sippass", g_cfg.sip_pass);
    nvs_set_str(h, "echo", g_cfg.echo_num);
    nvs_set_u16(h, "sipport", g_cfg.sip_port);
    nvs_commit(h);
    nvs_close(h);
}

bool cfg_get_peer(uint8_t bda[6])
{
    nvs_handle_t h;
    size_t n = 6;
    bool ok = false;
    if (nvs_open("gw", NVS_READONLY, &h) == ESP_OK) {
        ok = nvs_get_blob(h, "peer", bda, &n) == ESP_OK && n == 6;
        nvs_close(h);
    }
    return ok;
}

void cfg_set_peer(const uint8_t bda[6])
{
    nvs_handle_t h;
    if (nvs_open("gw", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_blob(h, "peer", bda, 6);
        nvs_commit(h);
        nvs_close(h);
    }
}

void cfg_clear_peer(void)
{
    nvs_handle_t h;
    if (nvs_open("gw", NVS_READWRITE, &h) == ESP_OK) {
        nvs_erase_key(h, "peer");
        nvs_commit(h);
        nvs_close(h);
    }
}

/* ---- tiny log ring ---- */
#define LOG_LINES 24
#define LOG_LEN 110
static char s_log[LOG_LINES][LOG_LEN];
static int s_log_next, s_log_count;
static SemaphoreHandle_t s_log_mu;

void gw_log(const char *fmt, ...)
{
    char line[LOG_LEN];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    ESP_LOGI("gw", "%s", line);
    if (!s_log_mu) {
        s_log_mu = xSemaphoreCreateMutex();
    }
    xSemaphoreTake(s_log_mu, portMAX_DELAY);
    strlcpy(s_log[s_log_next], line, LOG_LEN);
    s_log_next = (s_log_next + 1) % LOG_LINES;
    if (s_log_count < LOG_LINES) {
        s_log_count++;
    }
    xSemaphoreGive(s_log_mu);
}

size_t gw_log_dump(char *out, size_t cap)
{
    size_t w = 0;
    if (!s_log_mu) {
        out[0] = 0;
        return 0;
    }
    xSemaphoreTake(s_log_mu, portMAX_DELAY);
    int start = (s_log_next - s_log_count + LOG_LINES) % LOG_LINES;
    for (int i = 0; i < s_log_count && w + 2 < cap; i++) {
        const char *l = s_log[(start + i) % LOG_LINES];
        for (const char *p = l; *p && w + 2 < cap; p++) {
            char c = *p;
            if (c == '<' || c == '>' || c == '&' || c == '"' || c == '\\') c = ' ';  /* keep it JSON/HTML safe */
            out[w++] = c;
        }
        out[w++] = '\n';
    }
    out[w] = 0;
    xSemaphoreGive(s_log_mu);
    return w;
}
