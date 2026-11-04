"""Keypad menu for the test call.

Main menu:  1 = echo test (records a few seconds, plays it back)   2 = settings   0 = repeat menu
Settings:   2 = previous microphone   8 = next microphone   5 = select   0 = back
Any keypress interrupts whatever is being spoken or played.
"""
import threading
import time

import mp50_audio as audio

MENU = "Test call. Press 1 for echo test. Press 2 for settings. Hang up to end the call."
MIC_HELP = "Microphone settings. Press 2 for up, 8 for down, 5 to select, or 0 to go back."
ECHO_SECONDS = 5


class IVR:
    def __init__(self, log, emit):
        self.log, self.emit = log, emit
        self.mode = "menu"
        self.mics = []
        self.sel = 0
        self.mic = None  # chosen input device name (None = Yealink if present, else first)
        self._ev = None
        self._thread = None

    # ---- control ------------------------------------------------------
    def start(self):
        self.mode = "menu"
        self._run(self._say_menu)

    def stop(self):
        self._cancel()

    def key(self, k):
        if self.mode == "menu":
            if k == "1":
                self._run(self._echo)
            elif k == "2":
                self.mode = "mic"
                self._run(self._mic_enter)
            elif k == "0":
                self._run(self._say_menu)
        else:
            if k == "2":
                self._run(self._mic_move, -1)
            elif k == "8":
                self._run(self._mic_move, 1)
            elif k == "5":
                self._run(self._mic_select)
            elif k == "0":
                self.mode = "menu"
                self._run(self._say_menu)

    # ---- plumbing -----------------------------------------------------
    def _cancel(self):
        if self._ev:
            self._ev.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.5)

    def _run(self, fn, *a):
        self._cancel()
        ev = self._ev = threading.Event()

        def go():
            try:
                fn(ev, *a)
            except Exception as e:
                self.log(f"  !! menu error: {e}")
        self._thread = threading.Thread(target=go, daemon=True)
        self._thread.start()

    def _say(self, ev, text):
        if ev.is_set():
            return
        self.log(f"  IVR: {text}")
        audio.say(text, ev)

    def _device_index(self):
        names = audio.list_devices("in")
        if self.mic in names:
            return names.index(self.mic), self.mic
        i = audio.find_device("in")
        i = 0 if i is None else i
        return i, (names[i] if names else None)

    # ---- actions ------------------------------------------------------
    def _say_menu(self, ev):
        self._say(ev, MENU)

    def _echo(self, ev):
        idx, name = self._device_index()
        if name is None:
            self._say(ev, "No microphone found.")
            return
        self._say(ev, "Echo test. Speak after the beep.")
        if ev.is_set():
            return
        audio.beep(ev)
        self.log(f"  IVR: recording {ECHO_SECONDS}s from {name}")
        pcm, rate = audio.record(ECHO_SECONDS, idx, ev, on_level=lambda v: self.emit("level", value=v))
        if ev.is_set():
            return
        self._say(ev, "Playing back.")
        if not ev.is_set():
            audio.play_pcm(pcm, rate, stop=ev)
        self._say(ev, "Press 1 to try again, or 2 for settings.")

    def _mic_enter(self, ev):
        self.mics = audio.list_devices("in")
        if not self.mics:
            self.mode = "menu"
            self._say(ev, "No microphones found.")
            return
        _, cur = self._device_index()
        self.sel = self.mics.index(cur) if cur in self.mics else 0
        self._say(ev, MIC_HELP)
        self._say(ev, audio.speakable(self.mics[self.sel]))

    def _mic_move(self, ev, delta):
        if not self.mics:
            return
        self.sel = (self.sel + delta) % len(self.mics)
        self._say(ev, audio.speakable(self.mics[self.sel]))

    def _mic_select(self, ev):
        if not self.mics:
            return
        self.mic = self.mics[self.sel]
        self.emit("mic", name=self.mic)
        self.mode = "menu"
        self._say(ev, "Selected " + audio.speakable(self.mic))
        self._say(ev, MENU)
