"""Home Assistant Assist for Yea-Line: talk to an Assist pipeline through the phone.

Uses HA's WebSocket API (assist_pipeline/run): the phone microphone is streamed as 16 kHz 16-bit mono
to HA's speech-to-text, HA runs the conversation agent, and the spoken reply is played on the phone.
Each reply keeps the conversation_id so follow-up questions work. Hang up to end.

Events sent through emit(kind, **data):
  assist_start   url
  assist_state   state = listening | thinking | speaking
  assist_heard   text          (what STT understood)
  assist_reply   text          (what Assist answered)
  assist_end     reason
  assist_error   message
"""
import io
import json
import threading
import time
import urllib.error
import urllib.request
import wave

import mp50_audio as audio

MAX_LISTEN = 12.0        # seconds to wait for the end of an utterance before giving up
MAX_SILENT_TURNS = 2     # end the session after this many turns with nothing recognised


def ws_url(base):
    base = base.strip().rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + "/api/websocket"
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):] + "/api/websocket"
    return "ws://" + base + "/api/websocket"


class AssistError(Exception):
    """An Assist failure. `spoken` is what the phone says aloud; the full text goes to the log and UI."""

    def __init__(self, message, spoken="Sorry, Assist ran into a problem."):
        super().__init__(message)
        self.spoken = spoken


class _BadTtsOption(Exception):
    """This Home Assistant version rejected the tts_audio_output value we sent."""


class AssistClient:
    def __init__(self, emit, log):
        self.emit, self.log = emit, log
        self.url = self.token = self.pipeline = ""
        self.active = False
        self._stop = None
        self._thread = None
        # how to ask HA for WAV audio; versions differ, so try each until one is accepted (None = send nothing)
        self._tts_variants = ["wav", {"format": "wav"}, None]
        self._tts_i = 0

    def configure(self, url, token, pipeline=""):
        self.url, self.token, self.pipeline = url.strip().rstrip("/"), token, pipeline.strip()

    def configured(self):
        return bool(self.url and self.token)

    # ---- connection -----------------------------------------------------
    def _connect(self, timeout=8):
        import websocket
        ws = websocket.create_connection(ws_url(self.url), timeout=timeout)
        msg = json.loads(ws.recv())
        if msg.get("type") != "auth_required":
            ws.close()
            raise AssistError("unexpected reply from Home Assistant")
        ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        msg = json.loads(ws.recv())
        if msg.get("type") == "auth_invalid":
            ws.close()
            raise AssistError("Home Assistant rejected the access token", "Sorry, Home Assistant rejected the access token.")
        if msg.get("type") != "auth_ok":
            ws.close()
            raise AssistError("could not authenticate with Home Assistant")
        ws.ha_version = msg.get("ha_version", "")
        return ws

    def _call(self, ws, payload):
        payload = dict(payload, id=getattr(ws, "_next_id", 1))
        ws._next_id = payload["id"] + 1
        ws.send(json.dumps(payload))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == payload["id"] and msg.get("type") == "result":
                if not msg.get("success"):
                    raise AssistError((msg.get("error") or {}).get("message", "request failed"))
                return msg.get("result")

    def pipelines(self, ws):
        res = self._call(ws, {"type": "assist_pipeline/pipeline/list"})
        return res.get("pipelines", []), res.get("preferred_pipeline")

    def test(self):
        """Returns (ok, message). Never raises."""
        if not self.configured():
            return False, "enter the Home Assistant address and an access token"
        try:
            ws = self._connect()
        except AssistError as e:
            return False, str(e)
        except Exception as e:
            return False, f"cannot reach {self.url}: {e}"
        try:
            lst, preferred = self.pipelines(ws)
            names = [p["name"] + (" (preferred)" if p["id"] == preferred else "") for p in lst]
            note = ""
            if self.pipeline and self.pipeline.lower() not in [p["name"].lower() for p in lst]:
                note = f" - pipeline '{self.pipeline}' not found, will use the preferred one"
            return True, f"Connected to Home Assistant {ws.ha_version}. Pipelines: {', '.join(names) or 'none'}{note}"
        except Exception as e:
            return False, f"connected but could not list pipelines: {e}"
        finally:
            ws.close()

    def _pipeline_id(self, ws):
        if not self.pipeline:
            return None
        try:
            lst, _ = self.pipelines(ws)
        except Exception:
            return None
        for p in lst:
            if p["name"].lower() == self.pipeline.lower():
                return p["id"]
        return None

    # ---- session --------------------------------------------------------
    def start(self, mic_device=None):
        if self.active:
            return
        self._stop = threading.Event()
        self.active = True
        self._thread = threading.Thread(target=self._session, args=(self._stop, mic_device), daemon=True)
        self._thread.start()

    def stop(self):
        if self._stop:
            self._stop.set()

    def _say_local(self, text, stop):
        try:
            audio.say(text, stop)
        except Exception as e:
            self.log(f"  !! could not speak: {e}")

    def _session(self, stop, mic_device):
        ws = mic = None
        reason = "hung up"
        try:
            if not self.configured():
                raise AssistError("Home Assistant is not set up in Yea-Line", "Home Assistant is not set up.")
            try:
                ws = self._connect()
            except AssistError:
                raise
            except Exception as e:
                raise AssistError(f"cannot reach Home Assistant: {e}", "Sorry, I cannot reach Home Assistant.")
            self.emit("assist_start", url=self.url)
            pid = self._pipeline_id(ws)
            idx = mic_device if mic_device is not None else audio.find_device("in")
            if idx is None:
                raise AssistError("no microphone found")
            mic = audio.MicStream(idx, rates=(16000, 48000, 44100, 22050, 8000))
            cid, silent = None, 0
            while not stop.is_set():
                try:
                    got = self._turn(ws, mic, stop, pid, cid)
                except _BadTtsOption:
                    self._tts_i += 1
                    if self._tts_i >= len(self._tts_variants):
                        raise AssistError("Home Assistant rejected every audio format we tried")
                    self.log(f"  (Home Assistant rejected that audio option; retrying with {self._tts_variants[self._tts_i]!r})")
                    continue
                if got is None:
                    break
                cid = got.get("conversation_id") or cid
                silent = silent + 1 if not got.get("heard") else 0
                if silent >= MAX_SILENT_TURNS:
                    reason = "no speech"
                    break
        except AssistError as e:
            self.log(f"  !! Assist: {e}")
            self.emit("assist_error", message=str(e))
            self._say_local(e.spoken, stop)
            reason = "error"
        except Exception as e:  # keep the app alive whatever the network does
            self.log(f"  !! Assist failed: {e}")
            self.emit("assist_error", message=str(e))
            reason = "error"
        finally:
            for c in (mic, ws):
                try:
                    c and c.close()
                except Exception:
                    pass
            self.active = False
            self.emit("assist_end", reason=reason)

    def _turn(self, ws, mic, stop, pipeline_id, cid):
        """One listen -> think -> speak round. Returns dict(heard, conversation_id) or None to stop."""
        import audioop
        run_id = getattr(ws, "_next_id", 1)
        payload = {"type": "assist_pipeline/run", "start_stage": "stt", "end_stage": "tts",
                   "input": {"sample_rate": 16000}, "timeout": 30}
        fmt = self._tts_variants[self._tts_i]
        if fmt is not None:
            payload["tts_audio_output"] = fmt
        if pipeline_id:
            payload["pipeline"] = pipeline_id
        if cid:
            payload["conversation_id"] = cid
        ws._next_id = run_id + 1
        ws.send(json.dumps(dict(payload, id=run_id)))

        handler, heard, reply, new_cid, tts = None, "", "", cid, None
        speech_over = threading.Event()
        done = threading.Event()
        state = {"error": None}
        ws.settimeout(0.3)

        def sender():
            rate_state, t0 = None, time.time()
            audio.beep(stop)
            self.emit("assist_state", state="listening")
            while handler is None and not done.is_set() and not stop.is_set():
                time.sleep(0.01)
            while not (stop.is_set() or done.is_set() or speech_over.is_set() or time.time() - t0 > MAX_LISTEN):
                blk = mic.read()
                if blk is None:
                    continue
                if mic.rate != 16000:
                    blk, rate_state = audioop.ratecv(blk, 2, 1, mic.rate, 16000, rate_state)
                try:
                    ws.send_binary(bytes([handler]) + blk)
                except Exception:
                    return
            try:
                ws.send_binary(bytes([handler]))  # an empty chunk marks the end of the audio
            except Exception:
                pass

        mic_thread = threading.Thread(target=sender, daemon=True)
        mic_thread.start()
        import websocket
        while not stop.is_set():
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            if msg.get("id") != run_id:
                continue
            if msg.get("type") == "result":
                if not msg.get("success"):
                    done.set()
                    text = (msg.get("error") or {}).get("message", "Assist request failed")
                    if "tts_audio_output" in text:
                        raise _BadTtsOption(text)
                    raise AssistError(text)
                continue
            if msg.get("type") != "event":
                continue
            ev, data = msg["event"].get("type"), msg["event"].get("data") or {}
            if ev == "run-start":
                handler = (data.get("runner_data") or {}).get("stt_binary_handler_id")
            elif ev == "stt-vad-end":
                speech_over.set()
                self.emit("assist_state", state="thinking")
            elif ev == "stt-end":
                heard = ((data.get("stt_output") or {}).get("text") or "").strip()
                speech_over.set()
                if heard:
                    self.emit("assist_heard", text=heard)
                    self.emit("assist_state", state="thinking")
            elif ev == "intent-end":
                out = data.get("intent_output") or {}
                new_cid = out.get("conversation_id") or new_cid
                reply = (((out.get("response") or {}).get("speech") or {}).get("plain") or {}).get("speech", "")
                if reply:
                    self.emit("assist_reply", text=reply)
            elif ev == "tts-end":
                tts = data.get("tts_output") or {}
                self.log(f"  (Assist audio: {tts.get('mime_type', '?')})")
            elif ev == "error":
                state["error"] = data
                if data.get("code") in ("stt-no-text-recognized", "stt-stream-failed"):
                    pass  # nothing heard: fall through to run-end
                else:
                    done.set()
                    raise AssistError(data.get("message", "Assist reported an error"))
            elif ev == "run-end":
                break
        done.set()
        mic_thread.join(timeout=1)
        if stop.is_set():
            return None
        if tts or reply:
            self.emit("assist_state", state="speaking")
            self._play_reply(tts, reply, stop)
        return {"heard": heard, "conversation_id": new_cid}

    def _play_bytes(self, body, stop):
        """Decode MP3/WAV/FLAC/OGG to 16-bit mono PCM (miniaudio) and play it on the phone."""
        try:
            import miniaudio
        except ImportError:  # no decoder installed: WAV only
            with wave.open(io.BytesIO(body), "rb") as wf:
                audio.play_pcm(wf.readframes(wf.getnframes()), wf.getframerate(), wf.getnchannels(),
                               wf.getsampwidth() * 8, stop)
            return
        last = None
        for rate in (22050, 48000, 16000):  # use the first rate the phone's output accepts
            try:
                dec = miniaudio.decode(body, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=rate)
            except miniaudio.DecodeError as e:
                raise ValueError(f"cannot decode the audio: {e}")
            try:
                audio.play_pcm(dec.samples.tobytes(), rate, 1, 16, stop)
                return
            except OSError as e:
                last = e
        raise last

    def _play_reply(self, tts, text, stop):
        """Play HA's TTS audio on the phone; fall back to the local Windows voice for the reply text."""
        if tts and tts.get("url"):
            try:
                url = tts["url"] if tts["url"].startswith("http") else self.url + tts["url"]
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    body = r.read()
                self._play_bytes(body, stop)
                return
            except (wave.Error, urllib.error.URLError, OSError, EOFError, ValueError) as e:
                self.log(f"  (could not play Assist's audio: {e}; using the local voice)")
        if text:
            self._say_local(text, stop)
