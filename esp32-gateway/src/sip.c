/* Minimal SIP user agent: REGISTER with digest auth, answers the PBX's OPTIONS pings, outgoing calls (INVITE/CANCEL/BYE)
 * with G.711 u-law RTP. UDP only, no incoming calls yet (busy). */
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <strings.h>
#include <errno.h>
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "gw.h"

#define SIP_PORT 5060
#define RTP_PORT 40000
#define BUF 1536

static int s_sip = -1, s_rtp = -1;
static struct sockaddr_in s_srv;
static sip_reg_t s_reg = SIP_UNREG;
static volatile sip_call_t s_cs = SIPC_NONE;
static volatile bool s_reconf, s_hangup_req, s_answer_req;
static char s_ip[16];

/* registration */
static char s_reg_callid[40], s_reg_tag[12];
static int s_reg_cseq;
static int64_t s_reg_next_ms, s_reg_sent_ms;
static int s_reg_fail;
static bool s_reg_authed;

/* call */
static struct {
    char callid[48], ftag[12], ttag[48], branch[28], num[32], target[96];
    char realm[64], nonce[96], opaque[64], qop[16];
    int cseq;
    bool authed, answered, incoming;
    char i_via[500], i_from[200], i_to[200], i_cseq[40];  /* the INVITE we are answering */
    int64_t t_invite;
    int invite_tx;
    struct sockaddr_in rtp_to;
    volatile bool rtp_on;
} c;
static uint32_t s_ssrc, s_ts;
static uint16_t s_seq;

sip_reg_t sip_reg_state(void) { return s_reg; }
sip_call_t sip_call_state(void) { return s_cs; }
void sip_reconfigure(void) { s_reconf = true; }

const char *sip_reg_str(void)
{
    static const char *n[] = {"not registered", "registering...", "registered", "registration failed"};
    return n[s_reg];
}

/* ---------- helpers ---------- */
static int64_t now_ms(void) { return esp_timer_get_time() / 1000; }

static void rnd_hex(char *out, int n)
{
    static const char h[] = "0123456789abcdef";
    for (int i = 0; i < n; i++) out[i] = h[esp_random() & 15];
    out[n] = 0;
}


/* ---------- MD5 (RFC 1321), for SIP digest authentication ---------- */
static void md5_raw(const unsigned char *msg, size_t len, unsigned char out[16])
{
    static const uint32_t K[64] = {
        0xd76aa478, 0xe8c7b756, 0x242070db, 0xc1bdceee, 0xf57c0faf, 0x4787c62a, 0xa8304613, 0xfd469501, 0x698098d8, 0x8b44f7af,
        0xffff5bb1, 0x895cd7be, 0x6b901122, 0xfd987193, 0xa679438e, 0x49b40821, 0xf61e2562, 0xc040b340, 0x265e5a51, 0xe9b6c7aa,
        0xd62f105d, 0x02441453, 0xd8a1e681, 0xe7d3fbc8, 0x21e1cde6, 0xc33707d6, 0xf4d50d87, 0x455a14ed, 0xa9e3e905, 0xfcefa3f8,
        0x676f02d9, 0x8d2a4c8a, 0xfffa3942, 0x8771f681, 0x6d9d6122, 0xfde5380c, 0xa4beea44, 0x4bdecfa9, 0xf6bb4b60, 0xbebfbc70,
        0x289b7ec6, 0xeaa127fa, 0xd4ef3085, 0x04881d05, 0xd9d4d039, 0xe6db99e5, 0x1fa27cf8, 0xc4ac5665, 0xf4292244, 0x432aff97,
        0xab9423a7, 0xfc93a039, 0x655b59c3, 0x8f0ccc92, 0xffeff47d, 0x85845dd1, 0x6fa87e4f, 0xfe2ce6e0, 0xa3014314, 0x4e0811a1,
        0xf7537e82, 0xbd3af235, 0x2ad7d2bb, 0xeb86d391};
    static const int R[64] = {7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 5, 9,  14, 20, 5, 9,
                              14, 20, 5, 9,  14, 20, 5, 9,  14, 20, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
                              4, 11, 16, 23, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21};
    uint32_t a0 = 0x67452301, b0 = 0xefcdab89, c0 = 0x98badcfe, d0 = 0x10325476;
    size_t total = ((len + 8) / 64 + 1) * 64;
    unsigned char *buf = calloc(total, 1);
    memcpy(buf, msg, len);
    buf[len] = 0x80;
    uint64_t bits = (uint64_t)len * 8;
    for (int i = 0; i < 8; i++) buf[total - 8 + i] = (unsigned char)(bits >> (8 * i));
    for (size_t off = 0; off < total; off += 64) {
        uint32_t M[16];
        for (int i = 0; i < 16; i++)
            M[i] = buf[off + i * 4] | (buf[off + i * 4 + 1] << 8) | (buf[off + i * 4 + 2] << 16) | ((uint32_t)buf[off + i * 4 + 3] << 24);
        uint32_t A = a0, B = b0, C = c0, D = d0;
        for (int i = 0; i < 64; i++) {
            uint32_t F;
            int g;
            if (i < 16) { F = (B & C) | (~B & D); g = i; }
            else if (i < 32) { F = (D & B) | (~D & C); g = (5 * i + 1) % 16; }
            else if (i < 48) { F = B ^ C ^ D; g = (3 * i + 5) % 16; }
            else { F = C ^ (B | ~D); g = (7 * i) % 16; }
            F = F + A + K[i] + M[g];
            A = D; D = C; C = B;
            B = B + ((F << R[i]) | (F >> (32 - R[i])));
        }
        a0 += A; b0 += B; c0 += C; d0 += D;
    }
    free(buf);
    uint32_t v[4] = {a0, b0, c0, d0};
    for (int i = 0; i < 4; i++)
        for (int j = 0; j < 4; j++) out[i * 4 + j] = (unsigned char)(v[i] >> (8 * j));
}

static void md5hex(const char *in, char out[33])
{
    unsigned char d[16];
    md5_raw((const unsigned char *)in, strlen(in), d);
    for (int i = 0; i < 16; i++) sprintf(out + i * 2, "%02x", d[i]);
}

static bool hget(const char *msg, const char *name, char *out, size_t cap)
{
    size_t nl = strlen(name);
    const char *p = strstr(msg, "\r\n");
    while (p) {
        p += 2;
        if (*p == '\r' || *p == 0) break;
        if (strncasecmp(p, name, nl) == 0 && p[nl] == ':') {
            p += nl + 1;
            while (*p == ' ') p++;
            const char *e = strstr(p, "\r\n");
            size_t n = e ? (size_t)(e - p) : strlen(p);
            if (n >= cap) n = cap - 1;
            memcpy(out, p, n);
            out[n] = 0;
            return true;
        }
        p = strstr(p, "\r\n");
    }
    return false;
}

/* all lines of one header (Via) as "Via: ...\r\n" */
static void hcopy_all(const char *msg, const char *name, char *out, size_t cap)
{
    size_t nl = strlen(name), w = 0;
    const char *p = strstr(msg, "\r\n");
    out[0] = 0;
    while (p) {
        p += 2;
        if (*p == '\r' || *p == 0) break;
        const char *e = strstr(p, "\r\n");
        if (!e) break;
        if (strncasecmp(p, name, nl) == 0 && p[nl] == ':' && w + (e - p) + 3 < cap) {
            memcpy(out + w, p, e - p);
            w += e - p;
            out[w++] = '\r';
            out[w++] = '\n';
            out[w] = 0;
        }
        p = e;
    }
}

static bool param(const char *s, const char *name, char *out, size_t cap)
{
    char key[24];
    snprintf(key, sizeof(key), "%s=", name);
    const char *p = s;
    while ((p = strcasestr(p, key))) {
        if (p == s || p[-1] == ' ' || p[-1] == ',') {
            p += strlen(key);
            bool q = *p == '"';
            if (q) p++;
            size_t n = 0;
            while (p[n] && p[n] != (q ? '"' : ',') && p[n] != ' ' && n + 1 < cap) n++;
            memcpy(out, p, n);
            out[n] = 0;
            return true;
        }
        p++;
    }
    return false;
}

static int status_of(const char *m) { return strncmp(m, "SIP/2.0 ", 8) == 0 ? atoi(m + 8) : 0; }

static void send_to(const struct sockaddr_in *to, const char *data, int len)
{
    sendto(s_sip, data, len, 0, (const struct sockaddr *)to, sizeof(*to));
}

static bool resolve_server(void)
{
    struct addrinfo hints = {.ai_family = AF_INET, .ai_socktype = SOCK_DGRAM}, *res = NULL;
    char port[8];
    snprintf(port, sizeof(port), "%u", g_cfg.sip_port ? g_cfg.sip_port : 5060);
    if (!g_cfg.sip_server[0] || getaddrinfo(g_cfg.sip_server, port, &hints, &res) != 0 || !res) {
        return false;
    }
    memcpy(&s_srv, res->ai_addr, sizeof(s_srv));
    freeaddrinfo(res);
    return true;
}

static void make_auth(const char *method, const char *uri, const char *realm, const char *nonce, const char *qop,
                      const char *opaque, bool proxy, char *out, size_t cap)
{
    char a[200], ha1[33], ha2[33], resp[33], cn[17] = "", line[260];
    snprintf(a, sizeof(a), "%s:%s:%s", g_cfg.sip_user, realm, g_cfg.sip_pass);
    md5hex(a, ha1);
    snprintf(a, sizeof(a), "%s:%s", method, uri);
    md5hex(a, ha2);
    bool useq = qop[0] && strstr(qop, "auth");
    if (useq) {
        rnd_hex(cn, 16);
        snprintf(line, sizeof(line), "%s:%s:00000001:%s:auth:%s", ha1, nonce, cn, ha2);
    } else {
        snprintf(line, sizeof(line), "%s:%s:%s", ha1, nonce, ha2);
    }
    md5hex(line, resp);
    int n = snprintf(out, cap, "%s: Digest username=\"%s\", realm=\"%s\", nonce=\"%s\", uri=\"%s\", response=\"%s\", algorithm=MD5",
                     proxy ? "Proxy-Authorization" : "Authorization", g_cfg.sip_user, realm, nonce, uri, resp);
    if (useq) n += snprintf(out + n, cap - n, ", cnonce=\"%s\", qop=auth, nc=00000001", cn);
    if (opaque && opaque[0]) n += snprintf(out + n, cap - n, ", opaque=\"%s\"", opaque);
    snprintf(out + n, cap - n, "\r\n");
}

/* ---------- registration ---------- */
static void send_register(const char *authline)
{
    char m[BUF], uri[80];
    snprintf(uri, sizeof(uri), "sip:%s", g_cfg.sip_server);
    int n = snprintf(m, sizeof(m),
                     "REGISTER %s SIP/2.0\r\nVia: SIP/2.0/UDP %s:%d;branch=z9hG4bK%08x;rport\r\nMax-Forwards: 70\r\n"
                     "From: <sip:%s@%s>;tag=%s\r\nTo: <sip:%s@%s>\r\nCall-ID: %s\r\nCSeq: %d REGISTER\r\n"
                     "Contact: <sip:%s@%s:%d>\r\nExpires: 300\r\nUser-Agent: Yea-Line-ESP32\r\n%sContent-Length: 0\r\n\r\n",
                     uri, s_ip, SIP_PORT, (unsigned)esp_random(), g_cfg.sip_user, g_cfg.sip_server, s_reg_tag, g_cfg.sip_user,
                     g_cfg.sip_server, s_reg_callid, ++s_reg_cseq, g_cfg.sip_user, s_ip, SIP_PORT, authline ? authline : "");
    send_to(&s_srv, m, n);
    s_reg_sent_ms = now_ms();
}

static void handle_register_reply(const char *m)
{
    int code = status_of(m);
    if (code == 401 || code == 407) {
        if (s_reg_authed) {  /* second challenge: wrong password */
            gw_log("SIP: PBX rejected the login (check the password)");
            s_reg = SIP_FAILED;
            s_reg_next_ms = now_ms() + 30000;
            app_post(EV_SIP_REG, s_reg, "");
            s_reg_authed = false;
            return;
        }
        char h[400], realm[64] = "", nonce[96] = "", qop[16] = "", opq[64] = "", uri[80], auth[500];
        if (!hget(m, code == 401 ? "WWW-Authenticate" : "Proxy-Authenticate", h, sizeof(h))) return;
        param(h, "realm", realm, sizeof(realm));
        param(h, "nonce", nonce, sizeof(nonce));
        param(h, "qop", qop, sizeof(qop));
        param(h, "opaque", opq, sizeof(opq));
        snprintf(uri, sizeof(uri), "sip:%s", g_cfg.sip_server);
        make_auth("REGISTER", uri, realm, nonce, qop, opq, code == 407, auth, sizeof(auth));
        s_reg_authed = true;
        send_register(auth);
    } else if (code == 200) {
        s_reg_authed = false;
        s_reg_fail = 0;
        if (s_reg != SIP_REGISTERED) gw_log("SIP: registered as %s", g_cfg.sip_user);
        s_reg = SIP_REGISTERED;
        s_reg_next_ms = now_ms() + 120000;
        app_post(EV_SIP_REG, s_reg, "");
    } else if (code >= 300) {
        gw_log("SIP: registration answered %d", code);
        s_reg = SIP_FAILED;
        s_reg_authed = false;
        s_reg_next_ms = now_ms() + 30000;
        app_post(EV_SIP_REG, s_reg, "");
    }
}

/* ---------- RTP ---------- */
static void rtp_rx_task(void *arg)
{
    uint8_t pkt[400];
    int16_t pcm[320];
    while (c.rtp_on) {
        int n = recv(s_rtp, pkt, sizeof(pkt), 0);
        if (n <= 12 || !c.rtp_on) continue;
        int hdr = 12 + (pkt[0] & 0x0F) * 4;
        if (pkt[0] & 0x10) {
            if (n < hdr + 4) continue;
            hdr += 4 + 4 * ((pkt[hdr + 2] << 8) | pkt[hdr + 3]);
        }
        if ((pkt[1] & 0x7F) != 0 || n <= hdr) continue;  /* PCMU only; skips telephone-event */
        int cnt = n - hdr > 320 ? 320 : n - hdr;
        for (int i = 0; i < cnt; i++) pcm[i] = ulaw2lin(pkt[hdr + i]);
        audio_to_bt(pcm, cnt);
    }
    vTaskDelete(NULL);
}

static void rtp_tx_task(void *arg)
{
    int16_t pcm[160];
    uint8_t pkt[12 + 160];
    int have = 0;
    while (c.rtp_on) {
        int got = audio_from_bt(pcm + have, 160 - have, 40);
        have += got;
        if (have < 160) continue;
        have = 0;
        pkt[0] = 0x80;
        pkt[1] = 0;
        pkt[2] = s_seq >> 8;
        pkt[3] = s_seq & 0xFF;
        s_seq++;
        pkt[4] = s_ts >> 24; pkt[5] = s_ts >> 16; pkt[6] = s_ts >> 8; pkt[7] = s_ts;
        s_ts += 160;
        pkt[8] = s_ssrc >> 24; pkt[9] = s_ssrc >> 16; pkt[10] = s_ssrc >> 8; pkt[11] = s_ssrc;
        for (int i = 0; i < 160; i++) pkt[12 + i] = lin2ulaw(pcm[i]);
        sendto(s_rtp, pkt, sizeof(pkt), 0, (struct sockaddr *)&c.rtp_to, sizeof(c.rtp_to));
    }
    vTaskDelete(NULL);
}

static void rtp_start(void)
{
    if (c.rtp_on) return;
    s_ssrc = esp_random();
    s_seq = esp_random();
    s_ts = esp_random();
    c.rtp_on = true;
    xTaskCreate(rtp_rx_task, "rtprx", 3072, NULL, 5, NULL);
    xTaskCreate(rtp_tx_task, "rtptx", 3072, NULL, 5, NULL);
}

static void rtp_stop(void)
{
    c.rtp_on = false;
    /* nudge the receiver out of recv() */
    struct sockaddr_in me = {.sin_family = AF_INET, .sin_port = htons(RTP_PORT), .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    sendto(s_rtp, "x", 1, 0, (struct sockaddr *)&me, sizeof(me));
}

/* ---------- calls ---------- */
static void call_ended(const char *why)
{
    bool was = s_cs != SIPC_NONE;
    rtp_stop();
    s_cs = SIPC_NONE;
    c.answered = false;
    if (was) {
        gw_log("SIP: call ended (%s)", why);
        app_post(EV_SIP_CALL, 0, why);
    }
}

static void send_invite(const char *authline)
{
    char m[BUF], sdp[400], uri[128];
    snprintf(uri, sizeof(uri), "sip:%s@%s", c.num, g_cfg.sip_server);
    int sl = snprintf(sdp, sizeof(sdp),
                      "v=0\r\no=- %u %u IN IP4 %s\r\ns=Yea-Line\r\nc=IN IP4 %s\r\nt=0 0\r\nm=audio %d RTP/AVP 0 101\r\n"
                      "a=rtpmap:0 PCMU/8000\r\na=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-16\r\na=ptime:20\r\na=sendrecv\r\n",
                      (unsigned)esp_random(), (unsigned)esp_random(), s_ip, s_ip, RTP_PORT);
    rnd_hex(c.branch, 8);
    int n = snprintf(m, sizeof(m),
                     "INVITE %s SIP/2.0\r\nVia: SIP/2.0/UDP %s:%d;branch=z9hG4bK%s;rport\r\nMax-Forwards: 70\r\n"
                     "From: <sip:%s@%s>;tag=%s\r\nTo: <%s>\r\nCall-ID: %s\r\nCSeq: %d INVITE\r\n"
                     "Contact: <sip:%s@%s:%d>\r\nUser-Agent: Yea-Line-ESP32\r\n%sContent-Type: application/sdp\r\nContent-Length: %d\r\n\r\n%s",
                     uri, s_ip, SIP_PORT, c.branch, g_cfg.sip_user, g_cfg.sip_server, c.ftag, uri, c.callid, c.cseq,
                     g_cfg.sip_user, s_ip, SIP_PORT, authline ? authline : "", sl, sdp);
    send_to(&s_srv, m, n);
    c.t_invite = now_ms();
    c.invite_tx++;
}

bool sip_call(const char *number)
{
    if (s_reg != SIP_REGISTERED || s_cs != SIPC_NONE) return false;
    memset(&c, 0, sizeof(c));
    strlcpy(c.num, number, sizeof(c.num));
    char r[17];
    rnd_hex(r, 16);
    snprintf(c.callid, sizeof(c.callid), "%s@%s", r, s_ip);
    rnd_hex(c.ftag, 8);
    c.cseq = 1;
    s_cs = SIPC_CALLING;
    s_hangup_req = false;
    gw_log("SIP: calling %s", number);
    send_invite(NULL);
    return true;
}

void sip_hangup(void) { s_hangup_req = true; }
bool sip_answer(void) { s_answer_req = true; return true; }

/* ---------- incoming calls ---------- */
static void parse_sdp(const char *m);
static void reply(const struct sockaddr_in *to, const char *req, int code, const char *text, bool add_tag);

static int build_sdp(char *sdp, size_t cap)
{
    return snprintf(sdp, cap,
                    "v=0\r\no=- %u %u IN IP4 %s\r\ns=Yea-Line\r\nc=IN IP4 %s\r\nt=0 0\r\nm=audio %d RTP/AVP 0 101\r\n"
                    "a=rtpmap:0 PCMU/8000\r\na=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-16\r\na=ptime:20\r\na=sendrecv\r\n",
                    (unsigned)esp_random(), (unsigned)esp_random(), s_ip, s_ip, RTP_PORT);
}

/* a response inside the incoming dialog (180, 200, 486, 487, 603, ...) */
static void send_resp(int code, const char *text, bool with_sdp)
{
    char m[BUF], sdp[400] = "";
    int sl = with_sdp ? build_sdp(sdp, sizeof(sdp)) : 0;
    int n = snprintf(m, sizeof(m),
                     "SIP/2.0 %d %s\r\n%sFrom: %s\r\nTo: %s\r\nCall-ID: %s\r\nCSeq: %s\r\nContact: <sip:%s@%s:%d>\r\n"
                     "Allow: INVITE, ACK, CANCEL, BYE, OPTIONS\r\nUser-Agent: Yea-Line-ESP32\r\n%sContent-Length: %d\r\n\r\n%s",
                     code, text, c.i_via, c.i_from, c.i_to, c.callid, c.i_cseq, g_cfg.sip_user, s_ip, SIP_PORT,
                     with_sdp ? "Content-Type: application/sdp\r\n" : "", sl, sdp);
    send_to(&s_srv, m, n);
}

static void handle_invite(const struct sockaddr_in *from, const char *m)
{
    char cid[48];
    hget(m, "Call-ID", cid, sizeof(cid));
    if (s_cs != SIPC_NONE) {
        if (c.incoming && strcmp(cid, c.callid) == 0) {
            char cs[40] = "";
            hget(m, "CSeq", cs, sizeof(cs));
            if (s_cs == SIPC_INRING && strcmp(cs, c.i_cseq) == 0) {  /* the PBX repeating the INVITE */
                send_resp(180, "Ringing", false);
            } else if (s_cs == SIPC_ACTIVE) {  /* re-INVITE inside the call: accept it and keep going */
                strlcpy(c.i_cseq, cs, sizeof(c.i_cseq));
                hcopy_all(m, "Via", c.i_via, sizeof(c.i_via));
                parse_sdp(m);
                send_resp(200, "OK", true);
                gw_log("SIP: call updated by the PBX");
            }
            return;
        }
        reply(from, m, 486, "Busy Here", true);
        gw_log("SIP: incoming call refused (busy)");
        return;
    }
    memset(&c, 0, sizeof(c));
    c.incoming = true;
    c.cseq = 1;
    strlcpy(c.callid, cid, sizeof(c.callid));
    hcopy_all(m, "Via", c.i_via, sizeof(c.i_via));
    hget(m, "From", c.i_from, sizeof(c.i_from));
    hget(m, "To", c.i_to, sizeof(c.i_to));
    hget(m, "CSeq", c.i_cseq, sizeof(c.i_cseq));
    rnd_hex(c.ttag, 8);
    if (!strstr(c.i_to, "tag=")) {
        size_t l = strlen(c.i_to);
        snprintf(c.i_to + l, sizeof(c.i_to) - l, ";tag=%s", c.ttag);
    }
    const char *u = strstr(c.i_from, "sip:");
    if (u) sscanf(u + 4, "%31[^@;>]", c.num);
    if (!c.num[0]) strlcpy(c.num, "unknown", sizeof(c.num));
    char contact[120] = "";
    if (hget(m, "Contact", contact, sizeof(contact))) {
        char *a = strchr(contact, '<'), *b = a ? strchr(a, '>') : NULL;
        if (a && b) {
            *b = 0;
            strlcpy(c.target, a + 1, sizeof(c.target));
        }
    }
    parse_sdp(m);
    {
        const char *ml = strstr(m, "m=audio ");
        char line[80] = "";
        if (ml) sscanf(ml, "%79[^\r\n]", line);
        gw_log("SIP: offer %s", line);
    }
    c.t_invite = now_ms();
    s_cs = SIPC_INRING;
    s_hangup_req = s_answer_req = false;
    send_resp(100, "Trying", false);
    send_resp(180, "Ringing", false);
    gw_log("SIP: incoming call from %s", c.num);
    app_post(EV_SIP_INCOMING, 0, c.num);
}

static void do_answer(void)
{
    s_answer_req = false;
    if (s_cs != SIPC_INRING) return;
    send_resp(200, "OK", true);
    c.answered = true;
    s_cs = SIPC_ACTIVE;
    rtp_start();
    gw_log("SIP: incoming call answered");
    app_post(EV_SIP_CALL, 2, "");
}

static void do_hangup(void)
{
    char m[BUF], uri[128];
    s_hangup_req = false;
    if (s_cs == SIPC_NONE) return;
    if (c.incoming) {
        if (s_cs == SIPC_INRING) {
            send_resp(603, "Decline", false);
            call_ended("rejected");
            return;
        }
        snprintf(uri, sizeof(uri), "sip:%s@%s", c.num, g_cfg.sip_server);
        int n = snprintf(m, sizeof(m),
                         "BYE %s SIP/2.0\r\nVia: SIP/2.0/UDP %s:%d;branch=z9hG4bK%08x;rport\r\nMax-Forwards: 70\r\n"
                         "From: %s\r\nTo: %s\r\nCall-ID: %s\r\nCSeq: %d BYE\r\nContent-Length: 0\r\n\r\n",
                         c.target[0] ? c.target : uri, s_ip, SIP_PORT, (unsigned)esp_random(), c.i_to, c.i_from, c.callid,
                         ++c.cseq);
        send_to(&s_srv, m, n);
        call_ended("hung up");
        return;
    }
    snprintf(uri, sizeof(uri), "sip:%s@%s", c.num, g_cfg.sip_server);
    if (!c.answered) {  /* CANCEL the ringing call, then wait for its 487 */
        int n = snprintf(m, sizeof(m),
                         "CANCEL %s SIP/2.0\r\nVia: SIP/2.0/UDP %s:%d;branch=z9hG4bK%s;rport\r\nMax-Forwards: 70\r\n"
                         "From: <sip:%s@%s>;tag=%s\r\nTo: <%s>\r\nCall-ID: %s\r\nCSeq: %d CANCEL\r\nContent-Length: 0\r\n\r\n",
                         uri, s_ip, SIP_PORT, c.branch, g_cfg.sip_user, g_cfg.sip_server, c.ftag, uri, c.callid, c.cseq);
        send_to(&s_srv, m, n);
        gw_log("SIP: cancel sent");
        call_ended("cancelled");
        return;
    }
    char to[160];
    snprintf(to, sizeof(to), "<%s>;tag=%s", uri, c.ttag);
    int n = snprintf(m, sizeof(m),
                     "BYE %s SIP/2.0\r\nVia: SIP/2.0/UDP %s:%d;branch=z9hG4bK%08x;rport\r\nMax-Forwards: 70\r\n"
                     "From: <sip:%s@%s>;tag=%s\r\nTo: %s\r\nCall-ID: %s\r\nCSeq: %d BYE\r\nContent-Length: 0\r\n\r\n",
                     c.target[0] ? c.target : uri, s_ip, SIP_PORT, (unsigned)esp_random(), g_cfg.sip_user, g_cfg.sip_server,
                     c.ftag, to, c.callid, ++c.cseq);
    send_to(&s_srv, m, n);
    call_ended("hung up");
}

static void send_ack(const char *to_hdr, bool for_2xx)
{
    char m[BUF], uri[128];
    snprintf(uri, sizeof(uri), "sip:%s@%s", c.num, g_cfg.sip_server);
    int n = snprintf(m, sizeof(m),
                     "ACK %s SIP/2.0\r\nVia: SIP/2.0/UDP %s:%d;branch=z9hG4bK%s;rport\r\nMax-Forwards: 70\r\n"
                     "From: <sip:%s@%s>;tag=%s\r\nTo: %s\r\nCall-ID: %s\r\nCSeq: %d ACK\r\nContent-Length: 0\r\n\r\n",
                     for_2xx && c.target[0] ? c.target : uri, s_ip, SIP_PORT, for_2xx ? "ack00001" : c.branch, g_cfg.sip_user,
                     g_cfg.sip_server, c.ftag, to_hdr, c.callid, c.cseq);
    send_to(&s_srv, m, n);
}

static void parse_sdp(const char *m)
{
    const char *body = strstr(m, "\r\n\r\n");
    if (!body) return;
    char ip[20] = "";
    int port = 0;
    const char *cl = strstr(body, "c=IN IP4 ");
    if (cl) sscanf(cl + 9, "%19[0-9.]", ip);
    const char *ml = strstr(body, "m=audio ");
    if (ml) port = atoi(ml + 8);
    if (ip[0] && port) {
        memset(&c.rtp_to, 0, sizeof(c.rtp_to));
        c.rtp_to.sin_family = AF_INET;
        c.rtp_to.sin_port = htons(port);
        inet_aton(ip, &c.rtp_to.sin_addr);
    }
}

static void handle_call_reply(const char *m)
{
    char cseqh[40], to[200];
    if (!hget(m, "CSeq", cseqh, sizeof(cseqh)) || !strstr(cseqh, "INVITE")) return;
    int code = status_of(m);
    hget(m, "To", to, sizeof(to));
    if (code == 100) return;
    if (code == 180 || code == 183) {
        if (s_cs == SIPC_CALLING) {
            s_cs = SIPC_RINGING;
            app_post(EV_SIP_CALL, 1, "");
        }
        return;
    }
    if (code == 401 || code == 407) {
        send_ack(to, false);
        if (c.authed) {
            call_ended("login refused");
            return;
        }
        char h[400], uri[128], auth[500];
        if (!hget(m, code == 401 ? "WWW-Authenticate" : "Proxy-Authenticate", h, sizeof(h))) return;
        param(h, "realm", c.realm, sizeof(c.realm));
        param(h, "nonce", c.nonce, sizeof(c.nonce));
        param(h, "qop", c.qop, sizeof(c.qop));
        param(h, "opaque", c.opaque, sizeof(c.opaque));
        snprintf(uri, sizeof(uri), "sip:%s@%s", c.num, g_cfg.sip_server);
        make_auth("INVITE", uri, c.realm, c.nonce, c.qop, c.opaque, code == 407, auth, sizeof(auth));
        c.authed = true;
        c.cseq++;
        send_invite(auth);
        return;
    }
    if (code >= 200 && code < 300) {
        if (c.answered) {  /* retransmitted 200: re-ACK */
            send_ack(to, true);
            return;
        }
        char tag[48] = "", contact[120] = "";
        const char *t = strstr(to, "tag=");
        if (t) sscanf(t + 4, "%47[^;> \r]", tag);
        strlcpy(c.ttag, tag, sizeof(c.ttag));
        if (hget(m, "Contact", contact, sizeof(contact))) {
            char *a = strchr(contact, '<'), *b = a ? strchr(a, '>') : NULL;
            if (a && b) {
                *b = 0;
                strlcpy(c.target, a + 1, sizeof(c.target));
            }
        }
        parse_sdp(m);
        c.answered = true;
        s_cs = SIPC_ACTIVE;
        send_ack(to, true);
        rtp_start();
        gw_log("SIP: call answered");
        app_post(EV_SIP_CALL, 2, "");
        return;
    }
    if (code >= 300) {
        send_ack(to, false);
        char why[24];
        snprintf(why, sizeof(why), "%d", code);
        if (code == 487 && !s_cs) return;
        call_ended(code == 486 || code == 600 ? "busy" : (code == 404 ? "number not found" : why));
    }
}

/* ---------- incoming requests ---------- */
static void reply(const struct sockaddr_in *to, const char *req, int code, const char *text, bool add_tag)
{
    char via[500], from[200], tob[200], cid[80], cseq[60], m[BUF];
    hcopy_all(req, "Via", via, sizeof(via));
    hget(req, "From", from, sizeof(from));
    hget(req, "To", tob, sizeof(tob));
    hget(req, "Call-ID", cid, sizeof(cid));
    hget(req, "CSeq", cseq, sizeof(cseq));
    if (add_tag && !strstr(tob, "tag=")) {
        size_t l = strlen(tob);
        snprintf(tob + l, sizeof(tob) - l, ";tag=%08x", (unsigned)esp_random());
    }
    int n = snprintf(m, sizeof(m),
                     "SIP/2.0 %d %s\r\n%sFrom: %s\r\nTo: %s\r\nCall-ID: %s\r\nCSeq: %s\r\nAllow: INVITE, ACK, CANCEL, BYE, OPTIONS\r\n"
                     "User-Agent: Yea-Line-ESP32\r\nContent-Length: 0\r\n\r\n",
                     code, text, via, from, tob, cid, cseq);
    send_to(to, m, n);
}

static void handle_request(const struct sockaddr_in *from, const char *m)
{
    if (strncmp(m, "OPTIONS ", 8) != 0 && strncmp(m, "ACK ", 4) != 0) {
        char first[48] = "";
        sscanf(m, "%47[^\r\n]", first);
        gw_log("SIP rx %s (from %s)", first, inet_ntoa(from->sin_addr));
    }
    if (strncmp(m, "OPTIONS ", 8) == 0) {
        reply(&s_srv, m, 200, "OK", true);   /* answer at the PBX's real address, not a public Via */
        if (from->sin_addr.s_addr != s_srv.sin_addr.s_addr) reply(from, m, 200, "OK", true);
    } else if (strncmp(m, "INVITE ", 7) == 0) {
        handle_invite(from, m);
    } else if (strncmp(m, "CANCEL ", 7) == 0) {
        char cid[80];
        hget(m, "Call-ID", cid, sizeof(cid));
        reply(&s_srv, m, 200, "OK", false);
        if (s_cs == SIPC_INRING && strstr(cid, c.callid)) {
            send_resp(487, "Request Terminated", false);
            call_ended("missed");
        }
    } else if (strncmp(m, "BYE ", 4) == 0) {
        char cid[80];
        hget(m, "Call-ID", cid, sizeof(cid));
        reply(&s_srv, m, 200, "OK", false);
        if (from->sin_addr.s_addr != s_srv.sin_addr.s_addr) reply(from, m, 200, "OK", false);
        if (s_cs != SIPC_NONE && strstr(cid, c.callid)) {
            char rs[100] = "";
            hget(m, "Reason", rs, sizeof(rs));
            gw_log("SIP: BYE %lld ms after the call arrived %s", (long long)(now_ms() - c.t_invite), rs);
            call_ended("remote hung up");
        }
    } else if (strncmp(m, "NOTIFY ", 7) == 0 || strncmp(m, "MESSAGE ", 8) == 0 || strncmp(m, "UPDATE ", 7) == 0) {
        reply(from, m, 200, "OK", false);
    }
}

/* ---------- main loop ---------- */
static void sip_task(void *arg)
{
    static char buf[BUF];
    for (;;) {
        if (!net_connected() || !g_cfg.sip_server[0] || !g_cfg.sip_user[0]) {
            s_reg = SIP_UNREG;
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        strlcpy(s_ip, net_ip(), sizeof(s_ip));
        int64_t t = now_ms();
        if (s_reconf) {
            s_reconf = false;
            s_reg = SIP_UNREG;
            s_reg_next_ms = 0;
        }
        if (s_reg != SIP_REGISTERING && t >= s_reg_next_ms && s_cs == SIPC_NONE) {
            if (resolve_server()) {
                rnd_hex(s_reg_callid, 16);
                rnd_hex(s_reg_tag, 8);
                s_reg_authed = false;
                s_reg = SIP_REGISTERING;
                send_register(NULL);
            } else {
                gw_log("SIP: cannot find %s", g_cfg.sip_server);
                s_reg_next_ms = t + 15000;
            }
        } else if (s_reg == SIP_REGISTERING && t - s_reg_sent_ms > 4000) {
            if (++s_reg_fail > 3) {
                gw_log("SIP: no answer from the PBX");
                s_reg = SIP_FAILED;
                s_reg_fail = 0;
                s_reg_next_ms = t + 30000;
                app_post(EV_SIP_REG, s_reg, "");
            } else {
                send_register(NULL);
            }
        }
        if (s_answer_req) do_answer();
        if (s_hangup_req) do_hangup();
        if (s_cs == SIPC_INRING && now_ms() - c.t_invite > 45000) {
            send_resp(480, "Temporarily Unavailable", false);
            call_ended("no answer");
        }
        if (s_cs == SIPC_CALLING && !c.authed && c.invite_tx < 4 && now_ms() - c.t_invite > 700) send_invite(NULL);
        if (s_cs == SIPC_CALLING && now_ms() - c.t_invite > 30000) call_ended("no answer");

        fd_set rd;
        FD_ZERO(&rd);
        FD_SET(s_sip, &rd);
        struct timeval tv = {0, 100000};
        if (select(s_sip + 1, &rd, NULL, NULL, &tv) > 0) {
            struct sockaddr_in from;
            socklen_t fl = sizeof(from);
            int n = recvfrom(s_sip, buf, sizeof(buf) - 1, 0, (struct sockaddr *)&from, &fl);
            if (n > 0) {
                buf[n] = 0;
                if (strncmp(buf, "SIP/2.0", 7) == 0) {
                    char cseq[40];
                    if (hget(buf, "CSeq", cseq, sizeof(cseq))) {
                        if (strstr(cseq, "REGISTER")) handle_register_reply(buf);
                        else if (strstr(cseq, "INVITE")) handle_call_reply(buf);
                    }
                } else {
                    handle_request(&from, buf);
                }
            }
        }
    }
}

void sip_start(void)
{
    s_sip = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    s_rtp = socket(AF_INET, SOCK_DGRAM, IPPROTO_IP);
    struct sockaddr_in a = {.sin_family = AF_INET, .sin_addr.s_addr = htonl(INADDR_ANY)};
    a.sin_port = htons(SIP_PORT);
    if (bind(s_sip, (struct sockaddr *)&a, sizeof(a)) < 0) gw_log("SIP: cannot open port %d", SIP_PORT);
    a.sin_port = htons(RTP_PORT);
    if (bind(s_rtp, (struct sockaddr *)&a, sizeof(a)) < 0) gw_log("SIP: cannot open RTP port %d", RTP_PORT);
    xTaskCreate(sip_task, "sip", 8192, NULL, 4, NULL);
}
