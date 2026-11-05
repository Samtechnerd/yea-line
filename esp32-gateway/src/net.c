/* Wi-Fi (station, with a setup access point when it cannot connect) and the configuration web page. */
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_http_server.h"
#include "esp_system.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "gw.h"

static volatile bool s_connected, s_ap_on;
static char s_ip[16] = "";
static int s_retry;
static int64_t s_t_start;

bool net_connected(void) { return s_connected; }
const char *net_ip(void) { return s_ip; }

static void wifi_evt(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        if (g_cfg.wifi_ssid[0]) esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_connected = false;
        s_ip[0] = 0;
        s_retry++;
        if (g_cfg.wifi_ssid[0]) esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&e->ip_info.ip));
        s_connected = true;
        s_retry = 0;
        gw_log("Wi-Fi connected, address %s", s_ip);
    }
}

static void ap_start(void)
{
    wifi_config_t ap = {0};
    strlcpy((char *)ap.ap.ssid, "Yea-Line-Setup", sizeof(ap.ap.ssid));
    ap.ap.ssid_len = strlen("Yea-Line-Setup");
    ap.ap.channel = 1;
    ap.ap.max_connection = 3;
    ap.ap.authmode = WIFI_AUTH_OPEN;
    esp_wifi_set_mode(WIFI_MODE_APSTA);
    esp_wifi_set_config(WIFI_IF_AP, &ap);
    s_ap_on = true;
    gw_log("Setup network 'Yea-Line-Setup' is on (open http://192.168.4.1)");
}

static void ap_stop(void)
{
    esp_wifi_set_mode(WIFI_MODE_STA);
    s_ap_on = false;
    gw_log("Setup network off");
}

static void wifi_watch(void *arg)
{
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(3000));
        int64_t up = esp_timer_get_time() / 1000000 - s_t_start;
        if (!s_connected && !s_ap_on && (!g_cfg.wifi_ssid[0] || up > 25)) ap_start();
        else if (s_connected && s_ap_on && up > 60) ap_stop();
    }
}

/* ---------- web ---------- */
static const char PAGE[] =
"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
"<title>Yea-Line Gateway</title><style>"
"body{font:15px system-ui,sans-serif;margin:0;background:#f4f5f7;color:#1c1f26}"
"main{max-width:560px;margin:0 auto;padding:16px}h1{font-size:20px;margin:8px 0 14px}"
".card{background:#fff;border-radius:10px;padding:14px 16px;margin-bottom:14px;box-shadow:0 1px 3px #0002}"
".card h2{font-size:14px;margin:0 0 10px;color:#555;text-transform:uppercase;letter-spacing:.04em}"
"label{display:block;margin:8px 0 3px;font-size:13px;color:#555}"
"input,select{width:100%;box-sizing:border-box;padding:8px;border:1px solid #c9ced6;border-radius:6px;font-size:15px}"
"button{margin-top:12px;margin-right:8px;padding:9px 16px;border:0;border-radius:6px;background:#2563eb;color:#fff;font-size:15px}"
"button.g{background:#6b7280}button.r{background:#b91c1c}"
".row{display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #eee}.row:last-child{border:0}"
"pre{background:#111827;color:#d1d5db;padding:10px;border-radius:6px;font-size:12px;white-space:pre-wrap;max-height:220px;overflow:auto}"
".pw{display:flex;gap:6px;align-items:center}.pw input,.pw select{flex:1;width:auto;min-width:0}"
".ic{margin:0;padding:8px 10px;background:#e5e7eb;color:#1c1f26;display:flex;align-items:center}"
"#msg{color:#047857;margin-top:8px}</style></head><body><main><h1>Yea-Line Gateway</h1>"
"<div class=card><h2>Status</h2>"
"<div class=row><span>Wi-Fi</span><b id=w>-</b></div><div class=row><span>SIP</span><b id=s>-</b></div>"
"<div class=row><span>Phone (Bluetooth)</span><b id=b>-</b></div><div class=row><span>Call</span><b id=c>-</b></div></div>"
"<form id=f class=card><h2>Settings</h2>"
"<label>Wi-Fi name</label><div class=pw><select id=ssidsel></select>"
"<button type=button class=ic onclick=scan() title=Rescan>&#8635;</button></div>"
"<input name=ssid id=ssid placeholder='Network name' style='display:none;margin-top:6px' autocomplete=off>"
"<label>Wi-Fi password</label><div class=pw><input name=wpass id=wpass type=password placeholder='(leave empty to keep)' autocomplete=off>"
"<button type=button class=ic onclick=\"eye('wpass')\" title=Show><svg width=18 height=18 viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=2><path d='M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z'/><circle cx=12 cy=12 r=3 /></svg></button></div>"
"<label>PBX address</label><input name=sipsrv id=sipsrv placeholder='192.168.2.85'>"
"<label>PBX port</label><input name=sipport id=sipport>"
"<label>Extension (user name)</label><input name=sipuser id=sipuser autocomplete=off>"
"<label>Extension secret</label><div class=pw><input name=sippass id=sippass type=password placeholder='(leave empty to keep)' autocomplete=off>"
"<button type=button class=ic onclick=\"eye('sippass')\" title=Show><svg width=18 height=18 viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=2><path d='M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z'/><circle cx=12 cy=12 r=3 /></svg></button></div>"
"<label>Echo test number (dial this on the phone)</label><input name=echo id=echo>"
"<button type=submit>Save</button><span id=msg></span></form>"
"<div class=card><h2>Phone pairing</h2>"
"<p style='margin:0 0 4px'>Put the MP50 in Bluetooth pairing mode, then press Pair and pick <b>Yea-Line Gateway</b> on the phone.</p>"
"<button class=g onclick=\"act('/ring')\">Ring phone</button><button class=g onclick=\"act('/pair')\">Pair</button><button class=g onclick=\"act('/forget')\">Forget phone</button>"
"<button class=r onclick=\"act('/reboot')\">Restart</button></div>"
"<div class=card><h2>Log</h2><pre id=l></pre></div></main><script>"
"const $=id=>document.getElementById(id);"
"async function st(){try{const j=await (await fetch('/status')).json();$('w').textContent=j.wifi;$('s').textContent=j.sip;"
"$('b').textContent=j.bt;$('c').textContent=j.call;$('l').textContent=j.log;}catch(e){}}"
"async function load(){const j=await (await fetch('/config')).json();for(const k in j){if($(k))$(k).value=j[k];}}"
"function pick(){const s=$('ssidsel'),i=$('ssid');if(s.value==='__other'){i.style.display='block';}else{i.style.display='none';i.value=s.value}}"
"async function scan(){const s=$('ssidsel');s.innerHTML='<option>Scanning...</option>';let j=[];"
"try{j=await (await fetch('/scan')).json()}catch(e){}const cur=$('ssid').value;s.innerHTML='';let hit=false;"
"for(const n of j){const o=document.createElement('option');o.value=o.textContent=n;if(n===cur){o.selected=true;hit=true}s.appendChild(o)}"
"if(cur&&!hit){const o=document.createElement('option');o.value=o.textContent=cur;o.selected=true;s.insertBefore(o,s.firstChild)}"
"const o=document.createElement('option');o.value='__other';o.textContent='Other network...';s.appendChild(o);pick()}"
"function eye(id){const i=$(id);i.type=i.type==='password'?'text':'password'}"
"$('ssidsel').onchange=pick;"
"async function act(u){await fetch(u,{method:'POST'});setTimeout(st,500)}"
"$('f').onsubmit=async e=>{e.preventDefault();const b=new URLSearchParams(new FormData($('f'))).toString();"
"const r=await fetch('/save',{method:'POST',body:b});$('msg').textContent=await r.text();setTimeout(()=>$('msg').textContent='',6000);setTimeout(st,800)};"
"load().then(scan);st();setInterval(st,2000);</script></body></html>";

static void json_str(char *dst, size_t cap, const char *src)
{
    size_t w = 0;
    for (; *src && w + 3 < cap; src++) {
        if (*src == '"' || *src == '\\') { dst[w++] = '\\'; dst[w++] = *src; }
        else if (*src == '\n') { dst[w++] = '\\'; dst[w++] = 'n'; }
        else if ((unsigned char)*src >= 32) dst[w++] = *src;
    }
    dst[w] = 0;
}

static esp_err_t h_root(httpd_req_t *r)
{
    httpd_resp_set_type(r, "text/html");
    return httpd_resp_send(r, PAGE, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t h_status(httpd_req_t *r)
{
    static char log[2200], logj[2600], out[3300];
    char wifi[80];
    gw_log_dump(log, sizeof(log));
    json_str(logj, sizeof(logj), log);
    if (s_connected) snprintf(wifi, sizeof(wifi), "%s (%s)", g_cfg.wifi_ssid, s_ip);
    else snprintf(wifi, sizeof(wifi), s_ap_on ? "setup network on" : "connecting...");
    snprintf(out, sizeof(out), "{\"wifi\":\"%s\",\"sip\":\"%s\",\"bt\":\"%s\",\"call\":\"%s\",\"heap\":%u,\"heapmin\":%u,\"log\":\"%s\"}", wifi,
             sip_reg_str(), bt_state_str(), call_state_str(), (unsigned)esp_get_free_heap_size(),
             (unsigned)esp_get_minimum_free_heap_size(), logj);
    httpd_resp_set_type(r, "application/json");
    return httpd_resp_send(r, out, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t h_scan(httpd_req_t *r)
{
    wifi_scan_config_t sc = {.show_hidden = false};
    wifi_ap_record_t recs[24];
    uint16_t n = 24;
    char out[1100];
    size_t w = 0;
    out[w++] = '[';
    if (esp_wifi_scan_start(&sc, true) == ESP_OK && esp_wifi_scan_get_ap_records(&n, recs) == ESP_OK) {
        int added = 0;
        for (int i = 0; i < n; i++) {
            const char *name = (const char *)recs[i].ssid;
            bool dup = false;
            for (int j = 0; j < i; j++) if (strcmp(name, (const char *)recs[j].ssid) == 0) dup = true;
            if (!name[0] || dup) continue;
            char esc[70];
            json_str(esc, sizeof(esc), name);
            if (w + strlen(esc) + 4 >= sizeof(out) - 2) break;
            w += snprintf(out + w, sizeof(out) - w, "%s\"%s\"", added++ ? "," : "", esc);
        }
    }
    out[w++] = ']';
    out[w] = 0;
    httpd_resp_set_type(r, "application/json");
    return httpd_resp_send(r, out, HTTPD_RESP_USE_STRLEN);
}

static esp_err_t h_config(httpd_req_t *r)
{
    char out[600], a[80], b[140], c[80], d[40];
    json_str(a, sizeof(a), g_cfg.wifi_ssid);
    json_str(b, sizeof(b), g_cfg.sip_server);
    json_str(c, sizeof(c), g_cfg.sip_user);
    json_str(d, sizeof(d), g_cfg.echo_num);
    snprintf(out, sizeof(out), "{\"ssid\":\"%s\",\"sipsrv\":\"%s\",\"sipport\":\"%u\",\"sipuser\":\"%s\",\"echo\":\"%s\"}", a, b,
             g_cfg.sip_port, c, d);
    httpd_resp_set_type(r, "application/json");
    return httpd_resp_send(r, out, HTTPD_RESP_USE_STRLEN);
}

static void url_decode(char *s)
{
    char *w = s;
    for (; *s; s++) {
        if (*s == '+') *w++ = ' ';
        else if (*s == '%' && s[1] && s[2]) {
            char h[3] = {s[1], s[2], 0};
            *w++ = (char)strtol(h, NULL, 16);
            s += 2;
        } else *w++ = *s;
    }
    *w = 0;
}

static bool form_get(const char *body, const char *key, char *out, size_t cap)
{
    size_t kl = strlen(key);
    const char *p = body;
    while (p && *p) {
        if (strncmp(p, key, kl) == 0 && p[kl] == '=') {
            p += kl + 1;
            size_t n = strcspn(p, "&");
            if (n >= cap) n = cap - 1;
            memcpy(out, p, n);
            out[n] = 0;
            url_decode(out);
            return true;
        }
        p = strchr(p, '&');
        if (p) p++;
    }
    return false;
}

static void reboot_later(void *arg)
{
    vTaskDelay(pdMS_TO_TICKS(1500));
    esp_restart();
}

static esp_err_t h_save(httpd_req_t *r)
{
    char body[700] = {0}, v[80];
    int n = httpd_req_recv(r, body, sizeof(body) - 1);
    if (n <= 0) return httpd_resp_send_err(r, HTTPD_400_BAD_REQUEST, "empty");
    body[n] = 0;
    bool wifi_changed = false;
    char old_ssid[33], old_pass[65];
    strlcpy(old_ssid, g_cfg.wifi_ssid, sizeof(old_ssid));
    strlcpy(old_pass, g_cfg.wifi_pass, sizeof(old_pass));
    if (form_get(body, "ssid", v, sizeof(v))) strlcpy(g_cfg.wifi_ssid, v, sizeof(g_cfg.wifi_ssid));
    if (form_get(body, "wpass", v, sizeof(v)) && v[0]) strlcpy(g_cfg.wifi_pass, v, sizeof(g_cfg.wifi_pass));
    if (form_get(body, "sipsrv", v, sizeof(v))) strlcpy(g_cfg.sip_server, v, sizeof(g_cfg.sip_server));
    if (form_get(body, "sipport", v, sizeof(v))) g_cfg.sip_port = atoi(v) > 0 ? atoi(v) : 5060;
    if (form_get(body, "sipuser", v, sizeof(v))) strlcpy(g_cfg.sip_user, v, sizeof(g_cfg.sip_user));
    if (form_get(body, "sippass", v, sizeof(v)) && v[0]) strlcpy(g_cfg.sip_pass, v, sizeof(g_cfg.sip_pass));
    if (form_get(body, "echo", v, sizeof(v))) strlcpy(g_cfg.echo_num, v, sizeof(g_cfg.echo_num));
    wifi_changed = strcmp(old_ssid, g_cfg.wifi_ssid) != 0 || strcmp(old_pass, g_cfg.wifi_pass) != 0;
    cfg_save();
    gw_log("Settings saved");
    if (wifi_changed) {
        httpd_resp_sendstr(r, "Saved. Restarting to join the new Wi-Fi...");
        xTaskCreate(reboot_later, "reboot", 2048, NULL, 3, NULL);
    } else {
        sip_reconfigure();
        httpd_resp_sendstr(r, "Saved.");
    }
    return ESP_OK;
}

static esp_err_t h_ring(httpd_req_t *r) { app_post(EV_RING, 0, ""); return httpd_resp_sendstr(r, "ok"); }
static esp_err_t h_pair(httpd_req_t *r) { bt_pair_mode(120); return httpd_resp_sendstr(r, "ok"); }
static esp_err_t h_forget(httpd_req_t *r) { bt_forget(); return httpd_resp_sendstr(r, "ok"); }
static esp_err_t h_reboot(httpd_req_t *r)
{
    httpd_resp_sendstr(r, "ok");
    xTaskCreate(reboot_later, "reboot", 2048, NULL, 3, NULL);
    return ESP_OK;
}

static void web_start(void)
{
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.stack_size = 8192;
    cfg.max_uri_handlers = 16;
    httpd_handle_t srv = NULL;
    if (httpd_start(&srv, &cfg) != ESP_OK) {
        gw_log("web server failed to start");
        return;
    }
    const httpd_uri_t u[] = {
        {"/", HTTP_GET, h_root, NULL},        {"/status", HTTP_GET, h_status, NULL}, {"/config", HTTP_GET, h_config, NULL},
        {"/scan", HTTP_GET, h_scan, NULL}, {"/ring", HTTP_POST, h_ring, NULL}, {"/save", HTTP_POST, h_save, NULL},   {"/pair", HTTP_POST, h_pair, NULL},    {"/forget", HTTP_POST, h_forget, NULL},
        {"/reboot", HTTP_POST, h_reboot, NULL},
    };
    for (size_t i = 0; i < sizeof(u) / sizeof(u[0]); i++) httpd_register_uri_handler(srv, &u[i]);
}

void net_start(void)
{
    s_t_start = esp_timer_get_time() / 1000000;
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    esp_netif_create_default_wifi_ap();
    wifi_init_config_t wi = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wi));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_evt, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_evt, NULL));
    wifi_config_t sta = {0};
    strlcpy((char *)sta.sta.ssid, g_cfg.wifi_ssid, sizeof(sta.sta.ssid));
    strlcpy((char *)sta.sta.password, g_cfg.wifi_pass, sizeof(sta.sta.password));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta));
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_ps(WIFI_PS_NONE);  /* steady latency matters for voice, and Bluetooth coexistence still works */
    web_start();
    xTaskCreate(wifi_watch, "wifiwatch", 3072, NULL, 3, NULL);
}
