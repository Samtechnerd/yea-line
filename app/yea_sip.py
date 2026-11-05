"""SIP client for Yea-Line: registers to a PBX with pyVoIP and bridges calls to the Yealink's audio.

Events sent through emit(kind, **data):
  sip_status   state = registering | registered | failed | stopped | error, detail
  sip_call     state = dialing | ringing | answered | ended, number, direction ('out' / 'in'), reason
  sip_incoming caller
pyVoIP limits worth knowing: UDP only, G.711 audio, and it cannot CANCEL an outgoing call that is
still ringing (the far end keeps ringing until it answers or times out; we hang up if it answers).
"""
import re
import socket
import threading
import time

import mp50_audio as audio

RING_TIMEOUT = 30
_patched = False


def _answer_options(client, raw):
    """Reply 200 OK to a SIP OPTIONS ping, built straight from the raw request."""
    import os
    text = raw.decode("utf-8", "ignore")
    head = text.split("\r\n\r\n", 1)[0].split("\r\n")[1:]
    hdr = {}
    for line in head:
        if ":" in line:
            k, v = line.split(":", 1)
            hdr.setdefault(k.strip().lower(), []).append(v.strip())
    if not all(k in hdr for k in ("via", "from", "to", "call-id", "cseq")):
        return
    to = hdr["to"][0]
    if "tag=" not in to:
        to += ";tag=" + os.urandom(4).hex()
    reply = ["SIP/2.0 200 OK"] + [f"Via: {v}" for v in hdr["via"]] + [
        f"From: {hdr['from'][0]}", f"To: {to}", f"Call-ID: {hdr['call-id'][0]}", f"CSeq: {hdr['cseq'][0]}",
        "Allow: INVITE, ACK, BYE, CANCEL, OPTIONS", "User-Agent: Yea-Line", "Content-Length: 0", "", ""]
    # Reply to the PBX's own address, not the Via sent-by: with "rport" the answer belongs at the packet's
    # source, and Asterisk puts its public IP in Via when its NAT settings treat the LAN as external.
    dest = (client.server, client.port)
    client.out.sendto("\r\n".join(reply).encode("utf8"), dest)


def _options_as_trying(raw):
    """Re-label an OPTIONS packet as a '100 Trying' response (same Via/From/To/Call-ID, CSeq method INVITE)
    so pyVoIP's own wait loops simply ignore it. Its Call-ID never matches a real call."""
    text = raw.decode("utf-8", "ignore")
    head = text.split("\r\n\r\n", 1)[0].split("\r\n")[1:]
    keep = [ln for ln in head if ln.split(":", 1)[0].strip().lower() in ("via", "from", "to", "call-id")]
    return ("SIP/2.0 100 Trying\r\n" + "\r\n".join(keep) + "\r\nCSeq: 1 INVITE\r\nContent-Length: 0\r\n\r\n").encode()


def _patch_pyvoip():
    """pyVoIP cannot parse OPTIONS (the PBX's keep-alive "qualify"), so those pings go unanswered: Asterisk
    then marks the extension Unavailable and drops its contact, and incoming calls can no longer reach us.
    Wrap the packet reader so OPTIONS is answered before pyVoIP's parser sees it."""
    global _patched
    if _patched:
        return
    import pyVoIP
    from pyVoIP.VoIP import VoIPPhone  # noqa: F401  (import the package fully first: SIP alone is circular)
    from pyVoIP import SIP
    if "OPTIONS" not in pyVoIP.SIPCompatibleMethods:
        pyVoIP.SIPCompatibleMethods.append("OPTIONS")
    original_parse = SIP.SIPMessage.__init__

    def parse(self, data, *a, **k):
        if data[:8] == b"OPTIONS ":
            # pyVoIP's own packet reader swallows parse errors, so answer here and then bail out of parsing
            client = _active["sip"]
            if client is not None:
                try:
                    _answer_options(client, data)
                    _active["pings"] += 1
                    if _active["pings"] <= 3 or _active["pings"] % 20 == 0:
                        _active["log"](f"  (answered PBX keep-alive ping #{_active['pings']})")
                except Exception as e:
                    _active["log"](f"  !! could not answer a PBX keep-alive ping: {e}")
            data = _options_as_trying(data)  # never raise: pyVoIP's call set-up loops read raw packets
        return original_parse(self, data, *a, **k)

    SIP.SIPMessage.__init__ = parse

    original_parse_message = SIP.SIPClient.parse_message

    def parse_message(self, message):
        if message.type == SIP.SIPMessageType.MESSAGE and message.method == "BYE":
            try:  # pyVoIP replies to the Via address; also reply where the PBX really is
                self.out.sendto(self.gen_ok(message).encode("utf8"), (self.server, self.port))
            except Exception:
                pass
        return original_parse_message(self, message)

    SIP.SIPClient.parse_message = parse_message
    _patched = True


_active = {"sip": None, "log": lambda m: None, "pings": 0}  # the pyVoIP SIPClient currently in use, set by SipClient.start


def local_ip_for(server, port):
    """The local address the OS would use to reach the server (needed for SIP/RTP)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((server, int(port)))
        return s.getsockname()[0]
    finally:
        s.close()


class SipClient:
    def __init__(self, emit, log):
        self.emit, self.log = emit, log
        self.cfg = None            # dict: server, port, user, password, sip_port
        self.phone = None
        self.call = None           # active VoIPCall (outgoing, or incoming once answered)
        self._ringback_stop = None  # set to stop the "calling" sound
        self._ringback_thread = None
        self.held = False          # we put the far end on hold (announcement plays to them, audio both ways paused)
        self._hold_stop = None
        self.pending = None        # incoming VoIPCall that is ringing
        self.status = "stopped"
        self._bridge_stop = None
        self._abandoned = set()  # ids of outgoing calls the user gave up on before they were answered
        self._lock = threading.Lock()

    # ---- registration ---------------------------------------------------
    def configure(self, server, port, user, password, sip_port=5060):
        self.cfg = dict(server=server.strip(), port=int(port), user=user.strip(), password=password, sip_port=int(sip_port))

    def configured(self):
        c = self.cfg
        return bool(c and c["server"] and c["user"] and c["password"])

    def ready(self):
        return self.status == "registered"

    def _set_status(self, state, detail=""):
        if state != self.status or detail:
            self.status = state
            self.emit("sip_status", state=state, detail=detail)

    def start(self):
        self.stop()
        if not self.configured():
            self._set_status("stopped", "not configured")
            return
        c = self.cfg
        try:
            _patch_pyvoip()
            from pyVoIP.VoIP import VoIPPhone
            ip = local_ip_for(c["server"], c["port"])
            self._set_status("registering", f"{c['user']}@{c['server']}")
            self.phone = VoIPPhone(c["server"], c["port"], c["user"], c["password"], myIP=ip,
                                   callCallback=self._incoming, sipPort=c["sip_port"])
            self.phone.start()
            _active["sip"], _active["log"] = self.phone.sip, self.log
        except Exception as e:
            self.phone = None
            self._set_status("error", str(e))
            return
        threading.Thread(target=self._watch_registration, args=(self.phone,), daemon=True).start()

    def _watch_registration(self, phone):
        from pyVoIP.VoIP import PhoneStatus
        t0 = time.time()
        while phone is self.phone:
            st = phone.get_status()
            if st == PhoneStatus.REGISTERED:
                self._set_status("registered", f"{self.cfg['user']}@{self.cfg['server']}")
            elif st == PhoneStatus.FAILED:
                self._set_status("failed", "registration failed - check server, extension and password")
                return
            elif st == PhoneStatus.REGISTERING and time.time() - t0 > 20 and self.status == "registering":
                self._set_status("failed", "no response from the server")
                return
            time.sleep(0.5)

    def stop(self):
        self.hangup()
        phone, self.phone = self.phone, None
        if phone:
            try:
                phone.stop()
            except Exception:
                pass
        if self.status != "stopped":
            self._set_status("stopped", "")

    # ---- outgoing -------------------------------------------------------
    def call_number(self, number):
        if not self.ready():
            self.log("  !! SIP is not registered")
            return False
        if self.call or self.pending:
            self.log("  !! already in a call")
            return False
        try:
            c = self.phone.call(number)
        except Exception as e:
            self.log(f"  !! could not place call: {e}")
            self.emit("sip_call", state="ended", number=number, direction="out", reason=str(e))
            return False
        self.call = c
        self.emit("sip_call", state="dialing", number=number, direction="out", reason="")
        self._start_ringback()
        threading.Thread(target=self._monitor, args=(c, "out", number), daemon=True).start()
        return True

    # ---- "calling" sound -------------------------------------------------
    def _start_ringback(self):
        """Play a ringing tone in the handset/speaker while an outgoing call is waiting to be answered, so you can
        tell it is still ringing. Stopped when the call is answered, ends, or is cancelled."""
        self._stop_ringback()
        stop = self._ringback_stop = threading.Event()
        self._ringback_thread = t = threading.Thread(target=self._ringback, args=(stop,), daemon=True)
        t.start()

    def _stop_ringback(self):
        if self._ringback_stop:
            self._ringback_stop.set()
            self._ringback_stop = None
        t, self._ringback_thread = self._ringback_thread, None
        if t and t is not threading.current_thread():
            t.join(timeout=1.5)  # the speaker must be closed before the call audio opens it

    def _ringback(self, stop):
        """UK-style double ring (400 + 450 Hz): 0.4 s on, 0.2 s off, 0.4 s on, 2 s off."""
        import math
        import struct
        try:
            spk = audio.SpeakerStream()
        except Exception as e:
            self.log(f"  (no calling sound: {e})")
            return
        try:
            rate = spk.rate
            per = rate // 50  # samples per 20 ms frame
            pattern = [(0.4, True), (0.2, False), (0.4, True), (2.0, False)]
            n = 0  # running sample index keeps the tone phase continuous
            nxt = time.monotonic()
            while not stop.is_set():
                for secs, on in pattern:
                    for _ in range(int(secs * 50)):
                        if stop.is_set():
                            return
                        if on:
                            frame = struct.pack(f"<{per}h", *(
                                int(5500 * (math.sin(2 * math.pi * 400 * (n + i) / rate) +
                                            math.sin(2 * math.pi * 450 * (n + i) / rate)))
                                for i in range(per)))
                        else:
                            frame = bytes(per * 2)
                        n += per
                        spk.write(frame)
                        nxt += 0.02
                        time.sleep(max(0.0, nxt - time.monotonic()))
        finally:
            try:
                spk.close()
            except Exception:
                pass

    # ---- incoming -------------------------------------------------------
    def _incoming(self, call):
        """Called by pyVoIP on a new INVITE (its own thread)."""
        with self._lock:
            if self.call or self.pending:
                call.deny()
                return
            self.pending = call
        caller = "unknown"
        try:
            caller = call.request.headers["From"]["caller"] or caller
        except Exception:
            pass
        self.emit("sip_incoming", caller=str(caller))
        threading.Thread(target=self._monitor, args=(call, "in", str(caller)), daemon=True).start()

    def answer_pending(self):
        c = self.pending
        if not c:
            return False
        try:
            c.answer()
        except Exception as e:
            self.log(f"  !! could not answer: {e}")
            return False
        with self._lock:
            self.call, self.pending = c, None
        return True

    # ---- common ---------------------------------------------------------
    def hangup(self):
        """End the active call, reject a ringing one, or mark an outgoing ring as abandoned."""
        c, p = self.call, self.pending
        self._stop_ringback()
        if p:
            try:
                p.deny()
            except Exception:
                pass
            self.pending = None
            self.emit("sip_call", state="ended", number="", direction="in", reason="rejected")
        if c:
            self._abandoned.add(id(c))
            try:
                c.hangup()  # only works once answered; _monitor then emits the ended event
            except Exception:
                # pyVoIP cannot cancel a call that is still ringing. Free our state right away so the next dial
                # works; _monitor hangs the call up if the far end answers later.
                if self.call is c:
                    self.call = None
                self.emit("sip_call", state="ended", number="", direction="out", reason="cancelled")

    def _monitor(self, call, direction, label):
        from pyVoIP.VoIP import CallState
        last, started = None, time.time()
        while True:
            st = call.state
            if st != last:
                last = st
                if st == CallState.RINGING and direction == "out":
                    self.emit("sip_call", state="ringing", number=label, direction=direction, reason="")
                elif st == CallState.ANSWERED:
                    if id(call) in self._abandoned:
                        try:
                            call.hangup()
                        except Exception:
                            pass
                    else:
                        self.emit("sip_call", state="answered", number=label, direction=direction, reason="")
                        self._start_bridge(call)
                elif st == CallState.ENDED:
                    break
            if direction == "in" and call is self.pending and time.time() - started > RING_TIMEOUT:
                try:
                    call.deny()
                except Exception:
                    pass
                self.pending = None
                self.emit("sip_call", state="ended", number=label, direction=direction, reason="no answer")
                return
            if direction == "in" and call is not self.pending and call is not self.call:
                return  # we rejected it
            if id(call) in self._abandoned and time.time() - started > 60:
                self._abandoned.discard(id(call))
                return  # abandoned and never answered: stop watching
            time.sleep(0.1)
        self._stop_bridge()
        was_pending = call is self.pending
        if call is self.call:
            self.call = None
        if was_pending:
            self.pending = None
        self.emit("sip_call", state="ended", number=label, direction=direction,
                  reason="missed" if was_pending else "")

    # ---- audio bridge ---------------------------------------------------
    def _start_bridge(self, call):
        self._stop_bridge()
        stop = self._bridge_stop = threading.Event()
        try:
            idx = audio.find_device("in")
            if idx is None:
                raise OSError("no Yealink microphone found")
            mic, spk = audio.MicStream(idx), audio.SpeakerStream()
        except Exception as e:
            self.log(f"  !! audio bridge failed: {e}")
            return

        def tx():  # phone mic -> far end (8-bit unsigned, 8 kHz, 160 bytes per 20 ms)
            import audioop
            import pyVoIP
            state, writes, t0 = None, 0, time.monotonic()
            self.log(f"  (audio bridge: {len(call.RTPClients)} RTP client(s), mic {mic.rate} Hz blocks of {mic.block} bytes, "
                     f"reduction={pyVoIP.TRANSMIT_DELAY_REDUCTION})")
            while not stop.is_set():
                blk = mic.read()
                if blk is None:
                    continue
                if self.held:
                    continue
                writes += 1
                if writes == 100:
                    self.log(f"  (audio bridge: {writes / (time.monotonic() - t0):.0f} mic blocks/s written to the call)")
                if mic.rate != 8000:
                    blk, state = audioop.ratecv(blk, 2, 1, mic.rate, 8000, state)
                try:
                    call.write_audio(audioop.bias(audioop.lin2lin(blk, 2, 1), 1, 128))
                except Exception:
                    pass

        def rx():  # far end -> phone speaker
            import audioop
            state, nxt = None, time.monotonic()
            while not stop.is_set():
                try:
                    data = call.read_audio(160, False)  # non-blocking so we can stop promptly
                except Exception:
                    break
                if data and not self.held:
                    pcm = audioop.lin2lin(audioop.bias(data, 1, 128), 1, 2)
                    if spk.rate != 8000:
                        pcm, state = audioop.ratecv(pcm, 2, 1, 8000, spk.rate, state)
                    spk.write(pcm)
                nxt += 0.02  # one 20 ms frame per tick
                time.sleep(max(0.0, nxt - time.monotonic()))

        def cleanup():
            tx_t.join(timeout=2)
            rx_t.join(timeout=2)
            for s in (mic, spk):
                try:
                    s.close()
                except Exception:
                    pass
        tx_t = threading.Thread(target=tx, daemon=True)
        rx_t = threading.Thread(target=rx, daemon=True)
        tx_t.start()
        rx_t.start()
        self._cleanup = cleanup

    # ---- hold ----------------------------------------------------------
    def active(self):
        """True while a call is connected (not just dialling or ringing)."""
        return bool(self.call and self._bridge_stop)

    def toggle_hold(self):
        """Hold or resume the connected call. Returns the new state, or None when there is no connected call."""
        if not self.active():
            return None
        if self.held:
            self._hold_off()
        else:
            self.held = True
            stop = self._hold_stop = threading.Event()
            threading.Thread(target=self._hold_announce, args=(self.call, stop), daemon=True).start()
            self.emit("sip_hold", on=True)
        return self.held

    def _hold_off(self):
        self.held = False
        if self._hold_stop:
            self._hold_stop.set()
            self._hold_stop = None
        self.emit("sip_hold", on=False)

    def _hold_announce(self, call, stop):
        """Tell the far end they have been put on hold now, and again every 30 s while they wait."""
        import audioop
        import wave
        try:
            with wave.open(audio.tts_wav("You have been placed on hold. Please wait."), "rb") as wf:
                pcm, rate, ch, width = wf.readframes(wf.getnframes()), wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
            if ch == 2:
                pcm = audioop.tomono(pcm, width, 0.5, 0.5)
            if width != 2:
                pcm = audioop.lin2lin(pcm, width, 2)
            pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 8000, None)
            u8 = audioop.bias(audioop.lin2lin(pcm, 2, 1), 1, 128)  # what pyVoIP wants: 8-bit unsigned, 8 kHz
        except Exception as e:
            self.log(f"  !! could not make the hold announcement: {e}")
            return
        silence = bytes([128]) * 160
        while not stop.is_set():
            nxt = time.monotonic()
            for i in range(0, len(u8), 160):
                if stop.is_set():
                    return
                try:
                    call.write_audio((u8[i:i + 160] + silence)[:160])
                except Exception:
                    return
                nxt += 0.02
                time.sleep(max(0.0, nxt - time.monotonic()))
            for _ in range(300):
                if stop.wait(0.1):
                    return

    def _stop_bridge(self):
        self._stop_ringback()
        self.held = False
        if self._hold_stop:
            self._hold_stop.set()
            self._hold_stop = None
        if self._bridge_stop:
            self._bridge_stop.set()
            self._bridge_stop = None
            cleanup = getattr(self, "_cleanup", None)
            if cleanup:
                threading.Thread(target=cleanup, daemon=True).start()
