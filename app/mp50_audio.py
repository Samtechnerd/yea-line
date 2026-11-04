"""Audio helpers for the MP50 test call: text-to-speech, playback and recording on chosen devices.

Uses only Windows built-ins (winmm + System.Speech), so nothing to install. Playback always goes to the
Yealink output regardless of the Windows default; recording uses whichever input device you pass.
"""
import ctypes
import hashlib
import math
import os
import re
import struct
import subprocess
import tempfile
import time
import wave
from ctypes import wintypes as w

winmm = ctypes.WinDLL("winmm")


class _Caps(ctypes.Structure):
    _fields_ = [("m", w.WORD), ("p", w.WORD), ("v", w.UINT), ("name", ctypes.c_wchar * 32),
                ("f", w.DWORD), ("ch", w.WORD), ("r", w.WORD), ("s", w.DWORD)]


class _Fmt(ctypes.Structure):
    _fields_ = [("tag", w.WORD), ("ch", w.WORD), ("rate", w.DWORD), ("avg", w.DWORD),
                ("align", w.WORD), ("bits", w.WORD), ("cb", w.WORD)]


class _Hdr(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("len", w.DWORD), ("rec", w.DWORD), ("user", ctypes.c_size_t),
                ("flags", w.DWORD), ("loops", w.DWORD), ("next", ctypes.c_void_p), ("res", ctypes.c_size_t)]


WHDR_DONE = 1


def list_devices(kind):
    """Names of Windows audio devices; kind is 'in' or 'out' (names are truncated by Windows to 31 chars)."""
    count, getcaps = ((winmm.waveInGetNumDevs, winmm.waveInGetDevCapsW) if kind == "in"
                      else (winmm.waveOutGetNumDevs, winmm.waveOutGetDevCapsW))
    names = []
    for i in range(count()):
        c = _Caps()
        getcaps(i, ctypes.byref(c), ctypes.sizeof(c))
        names.append(c.name)
    return names


def find_device(kind, part="yealink"):
    for i, n in enumerate(list_devices(kind)):
        if part in n.lower():
            return i
    return None


def speakable(name):
    """Turn a device name like 'Microphone (4- Yealink MP50)' into something nice to say."""
    if "yealink" in name.lower():
        return "Yealink phone microphone"
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z ]", " ", name)).strip() or "unknown device"


def tone(freq=1000, ms=300, rate=16000, volume=0.4):
    n = int(rate * ms / 1000)
    return b"".join(struct.pack("<h", int(volume * 32767 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n)), rate


def tts_wav(text):
    """Render text to a cached WAV with Windows speech synthesis (no audio device involved)."""
    clean = re.sub(r"[^A-Za-z0-9 .,:!?-]", " ", text)
    path = os.path.join(tempfile.gettempdir(), "mp50_tts_" + hashlib.sha1(clean.encode()).hexdigest()[:12] + ".wav")
    if not os.path.exists(path):
        ps = ("Add-Type -AssemblyName System.Speech;"
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
              f"$s.SetOutputToWaveFile('{path}');$s.Speak('{clean}');$s.Dispose()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    return path


def play_pcm(pcm, rate, channels=1, bits=16, stop=None):
    """Play raw PCM to the Yealink output. Returns False if the device could not be opened."""
    dev = find_device("out")
    if dev is None:
        raise OSError("no Yealink audio output found")
    block = channels * bits // 8
    fmt = _Fmt(1, channels, rate, rate * block, block, bits, 0)
    hwo = ctypes.c_void_p()
    if winmm.waveOutOpen(ctypes.byref(hwo), dev, ctypes.byref(fmt), 0, 0, 0):
        raise OSError("could not open the Yealink output (is another app using it?)")
    buf = ctypes.create_string_buffer(pcm, len(pcm))
    hdr = _Hdr(ctypes.cast(buf, ctypes.c_void_p), len(pcm), 0, 0, 0, 0, None, 0)
    winmm.waveOutPrepareHeader(hwo, ctypes.byref(hdr), ctypes.sizeof(hdr))
    winmm.waveOutWrite(hwo, ctypes.byref(hdr), ctypes.sizeof(hdr))
    while not (hdr.flags & WHDR_DONE) and not (stop and stop.is_set()):
        time.sleep(0.03)
    winmm.waveOutReset(hwo)
    winmm.waveOutUnprepareHeader(hwo, ctypes.byref(hdr), ctypes.sizeof(hdr))
    winmm.waveOutClose(hwo)


def say(text, stop=None):
    with wave.open(tts_wav(text), "rb") as wf:
        play_pcm(wf.readframes(wf.getnframes()), wf.getframerate(), wf.getnchannels(), wf.getsampwidth() * 8, stop)


def beep(stop=None):
    pcm, rate = tone()
    play_pcm(pcm, rate, stop=stop)


def record(seconds, device, stop=None, rates=(16000, 22050, 44100, 48000, 8000), on_level=None):
    """Record mono 16-bit PCM from input device index `device`. Returns (pcm, rate).

    on_level(x) is called about 10 times a second with the loudness of each 0.1 s block (0.0 to 1.0).
    """
    hwi = ctypes.c_void_p()
    rate = None
    for r in rates:
        fmt = _Fmt(1, 1, r, r * 2, 2, 16, 0)
        if not winmm.waveInOpen(ctypes.byref(hwi), device, ctypes.byref(fmt), 0, 0, 0):
            rate = r
            break
    if rate is None:
        raise OSError("could not open the microphone (is another app using it?)")
    chunk = rate // 5  # bytes in 0.1 s of 16-bit mono
    n = max(1, int(seconds * 10))
    bufs = [ctypes.create_string_buffer(chunk) for _ in range(n)]
    hdrs = [_Hdr(ctypes.cast(b, ctypes.c_void_p), chunk, 0, 0, 0, 0, None, 0) for b in bufs]
    for h in hdrs:
        winmm.waveInPrepareHeader(hwi, ctypes.byref(h), ctypes.sizeof(h))
        winmm.waveInAddBuffer(hwi, ctypes.byref(h), ctypes.sizeof(h))
    winmm.waveInStart(hwi)
    seen = 0
    while not all(h.flags & WHDR_DONE for h in hdrs) and not (stop and stop.is_set()):
        time.sleep(0.03)
        while on_level and seen < n and hdrs[seen].flags & WHDR_DONE:
            samples = struct.unpack("<%dh" % (chunk // 2), bytes(bufs[seen]))
            on_level(math.sqrt(sum(x * x for x in samples) / len(samples)) / 32768)
            seen += 1
    winmm.waveInStop(hwi)
    winmm.waveInReset(hwi)
    pcm = b"".join(bytes(b)[:h.rec or chunk] if h.flags & WHDR_DONE else b"" for b, h in zip(bufs, hdrs))
    for h in hdrs:
        winmm.waveInUnprepareHeader(hwi, ctypes.byref(h), ctypes.sizeof(h))
    winmm.waveInClose(hwi)
    return pcm, rate


# ---- continuous streams (used for live SIP calls) ---------------------------------------------
import queue as _queue
import threading as _threading


class MicStream:
    """Continuous capture from a waveIn device. read() returns 16-bit mono blocks at self.rate."""

    def __init__(self, device, rates=(8000, 16000, 48000, 44100, 22050), block_ms=20, nbuf=10):
        self.hwi = ctypes.c_void_p()
        self.rate = None
        for r in rates:
            fmt = _Fmt(1, 1, r, r * 2, 2, 16, 0)
            if not winmm.waveInOpen(ctypes.byref(self.hwi), device, ctypes.byref(fmt), 0, 0, 0):
                self.rate = r
                break
        if self.rate is None:
            raise OSError("could not open the microphone (is another app using it?)")
        self.block = self.rate * 2 * block_ms // 1000
        self.bufs = [ctypes.create_string_buffer(self.block) for _ in range(nbuf)]
        self.hdrs = [_Hdr(ctypes.cast(b, ctypes.c_void_p), self.block, 0, 0, 0, 0, None, 0) for b in self.bufs]
        for h in self.hdrs:
            winmm.waveInPrepareHeader(self.hwi, ctypes.byref(h), ctypes.sizeof(h))
            winmm.waveInAddBuffer(self.hwi, ctypes.byref(h), ctypes.sizeof(h))
        self.q = _queue.Queue(maxsize=100)
        self._stop = _threading.Event()
        winmm.waveInStart(self.hwi)
        self._t = _threading.Thread(target=self._pump, daemon=True)
        self._t.start()

    def _pump(self):
        i = 0
        while not self._stop.is_set():
            h = self.hdrs[i]
            if h.flags & WHDR_DONE:
                try:
                    self.q.put_nowait(bytes(self.bufs[i])[:h.rec or self.block])
                except _queue.Full:
                    pass  # nobody is reading: drop rather than grow
                h.rec = 0
                winmm.waveInAddBuffer(self.hwi, ctypes.byref(h), ctypes.sizeof(h))
                i = (i + 1) % len(self.hdrs)
            else:
                time.sleep(0.004)

    def read(self, timeout=0.2):
        try:
            return self.q.get(timeout=timeout)
        except _queue.Empty:
            return None

    def close(self):
        self._stop.set()
        self._t.join(timeout=1)
        winmm.waveInStop(self.hwi)
        winmm.waveInReset(self.hwi)
        for h in self.hdrs:
            winmm.waveInUnprepareHeader(self.hwi, ctypes.byref(h), ctypes.sizeof(h))
        winmm.waveInClose(self.hwi)


class SpeakerStream:
    """Continuous playback to the Yealink output. write() takes 16-bit mono PCM at self.rate."""
    MAX_QUEUED = 15  # about 300 ms of 20 ms blocks; beyond that drop to keep latency low

    def __init__(self, rates=(8000, 16000, 48000, 44100, 22050)):
        dev = find_device("out")
        if dev is None:
            raise OSError("no Yealink audio output found")
        self.hwo = ctypes.c_void_p()
        self.rate = None
        for r in rates:
            fmt = _Fmt(1, 1, r, r * 2, 2, 16, 0)
            if not winmm.waveOutOpen(ctypes.byref(self.hwo), dev, ctypes.byref(fmt), 0, 0, 0):
                self.rate = r
                break
        if self.rate is None:
            raise OSError("could not open the Yealink output (is another app using it?)")
        self.pending = []
        self.lock = _threading.Lock()

    def _prune(self):
        keep = []
        for buf, h in self.pending:
            if h.flags & WHDR_DONE:
                winmm.waveOutUnprepareHeader(self.hwo, ctypes.byref(h), ctypes.sizeof(h))
            else:
                keep.append((buf, h))
        self.pending = keep

    def write(self, pcm):
        with self.lock:
            self._prune()
            if len(self.pending) >= self.MAX_QUEUED:
                return
            buf = ctypes.create_string_buffer(pcm, len(pcm))
            h = _Hdr(ctypes.cast(buf, ctypes.c_void_p), len(pcm), 0, 0, 0, 0, None, 0)
            winmm.waveOutPrepareHeader(self.hwo, ctypes.byref(h), ctypes.sizeof(h))
            winmm.waveOutWrite(self.hwo, ctypes.byref(h), ctypes.sizeof(h))
            self.pending.append((buf, h))

    def close(self):
        with self.lock:
            winmm.waveOutReset(self.hwo)
            self._prune()
            winmm.waveOutClose(self.hwo)
