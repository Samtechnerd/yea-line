"""Print raw HID input reports from a Yealink MP50 (VID 6993) using Windows Raw Input.

No dependencies. Run:  python mp50_buttons.py
Press buttons on the phone; each report prints with its interface and a diff of changed bytes.
Ctrl+C to quit. Use --vid 6993 to change the vendor filter (hex).
"""
import argparse
import ctypes
import re
import sys
import time
from ctypes import wintypes as w

ap = argparse.ArgumentParser()
ap.add_argument("--vid", default="6993", help="USB vendor id in hex (default 6993 = Yealink)")
ap.add_argument("--log", help="also append every line to this file")
ap.add_argument("--dial-cmd", help='shell command run on pick-up with a number, e.g. start "" "sip:{number}"')
ap.add_argument("--hangup-cmd", help="shell command run when the phone goes on-hook")
ap.add_argument("--test-number", default="#878", help="number that starts the built-in test call bot (default #878)")
ap.add_argument("--assist-number", default="#999", help="number that opens Home Assistant Assist (default #999)")
ap.add_argument("--quiet", action="store_true", help="hide raw reports, show only decoded events")
args = ap.parse_args()
_logf = open(args.log, "a", encoding="utf-8", buffering=1) if args.log else None


def out(line):
    print(line, flush=True)
    if _logf:
        _logf.write(line + "\n")


listeners = []  # callbacks fn(kind, data) for UIs: pickup, hangup, bot_start, bot_stop, bot_done
speed_dial = {"enabled": False, "prefix": "*", "entries": {}, "names": {}}  # filled in by the UI: <prefix>1..9 -> a saved number


def resolve_speed(number):
    """With speed dial on, <prefix>1 .. <prefix>9 (prefix * or #, or none = a single digit) become the saved
    number; anything else is left alone."""
    pre = speed_dial.get("prefix", "*")
    if speed_dial["enabled"] and len(number) == len(pre) + 1 and number.startswith(pre) and number[-1] in "123456789":
        digit = number[-1]
        target = speed_dial["entries"].get(digit, "").strip()
        if target:
            who = speed_dial["names"].get(digit, "") or target
            out(f"  (speed dial {number} -> {who})")
            return target
    return number



def emit(kind, **data):
    for fn in listeners:
        try:
            fn(kind, data)
        except Exception as e:
            print("listener error:", e, file=sys.stderr)


VID = args.vid.upper()

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LRESULT = ctypes.c_ssize_t

WM_INPUT = 0x00FF
RIM_TYPEHID = 2
RID_INPUT = 0x10000003
RIDI_DEVICENAME = 0x20000007
RIDI_DEVICEINFO = 0x2000000B
RIDEV_INPUTSINK = 0x100
RIDEV_PAGEONLY = 0x20
HWND_MESSAGE = ctypes.c_void_p(-3)


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", w.USHORT), ("usUsage", w.USHORT),
                ("dwFlags", w.DWORD), ("hwndTarget", w.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", w.DWORD), ("dwSize", w.DWORD),
                ("hDevice", w.HANDLE), ("wParam", w.WPARAM)]


class RID_DEVICE_INFO_HID(ctypes.Structure):
    _fields_ = [("dwVendorId", w.DWORD), ("dwProductId", w.DWORD),
                ("dwVersionNumber", w.DWORD), ("usUsagePage", w.USHORT), ("usUsage", w.USHORT)]


class RID_DEVICE_INFO(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("hid", RID_DEVICE_INFO_HID), ("_pad", ctypes.c_byte * 24)]
    _anonymous_ = ("u",)
    _fields_ = [("cbSize", w.DWORD), ("dwType", w.DWORD), ("u", _U)]


WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HANDLE),
                ("hCursor", w.HANDLE), ("hbrBackground", w.HANDLE),
                ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]


user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.GetRawInputData.argtypes = [w.HANDLE, w.UINT, ctypes.c_void_p, ctypes.POINTER(w.UINT), w.UINT]
user32.GetRawInputData.restype = w.UINT
user32.GetRawInputDeviceInfoW.argtypes = [w.HANDLE, w.UINT, ctypes.c_void_p, ctypes.POINTER(w.UINT)]
user32.CreateWindowExW.restype = w.HWND
user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, w.HWND, w.HANDLE, w.HINSTANCE, w.LPVOID]

dev_cache = {}   # hDevice -> (label, usage_page, usage) or None if not ours
last_report = {}  # label -> bytes


def describe(hdev):
    if hdev in dev_cache:
        return dev_cache[hdev]
    n = w.UINT(0)
    user32.GetRawInputDeviceInfoW(hdev, RIDI_DEVICENAME, None, ctypes.byref(n))
    buf = ctypes.create_unicode_buffer(n.value)
    user32.GetRawInputDeviceInfoW(hdev, RIDI_DEVICENAME, buf, ctypes.byref(n))
    name = buf.value
    result = None
    if f"VID_{VID}" in name.upper():
        info = RID_DEVICE_INFO()
        info.cbSize = ctypes.sizeof(info)
        sz = w.UINT(info.cbSize)
        user32.GetRawInputDeviceInfoW(hdev, RIDI_DEVICEINFO, ctypes.byref(info), ctypes.byref(sz))
        mi = re.search(r"MI_(\w\w)", name, re.I)
        col = re.search(r"COL(\w\w)", name, re.I)
        label = "MI_" + (mi.group(1) if mi else "--") + (f"/COL{col.group(1)}" if col else "")
        result = (label, info.hid.usUsagePage, info.hid.usUsage)
    dev_cache[hdev] = result
    return result


KEYS = "0123456789*#"


class Decoder:
    """Decode the MP50 headset report (id 02, 2 data bytes).

    byte1 bit0 = off-hook. A dialled number arrives as a ~5 ms burst just before the
    off-hook edge, one token per key: key k (0-9, 10='*', 11='#') is sent as
    byte2 = ceil(k/2) with byte1 bit7 set when k is even.
    """
    DEBOUNCE = 0.4  # the phone sends 01,00,01 when picking up; ignore dips shorter than this
    PRE_WINDOW = 0.8   # a number typed before pick-up: wait this long for stragglers, then dial
    LIVE_WINDOW = 1.5  # digits typed after pick-up: dial this long after the last digit

    def __init__(self):
        import threading
        self._threading = threading
        self.digits = []
        self.off_hook = False
        self.reported = False  # True once a pick-up has been reported as a call
        self.timer = None
        self.dial_open = False  # off hook, still collecting digits before dialling
        self.dial_timer = None
        self.btn_prev = 0       # function-key bits last seen in byte1
        self.key_answered = False  # the call was answered with the screen's Accept button (hands-free), not the handset

    def feed(self, rep):
        if len(rep) != 3 or rep[0] != 2:
            return
        hook, flag, val = rep[1] & 1, bool(rep[1] & 0x80), rep[2]
        # function keys share byte1 with the hook bit: 0x02 key 0x97, 0x04 mute, 0x08 flash (the Hold key), 0x10 redial
        btn = rep[1] & 0x1E
        rising, self.btn_prev = btn & ~self.btn_prev, btn
        if btn and not (flag or val):
            if rising & 0x08:
                on_hold_key()
            if rising & 0x02:
                self.on_accept_key()
            if rising & 0x14:
                out(f"{time.strftime('%H:%M:%S')}  >> key pressed (bits 0x{rising & 0x14:02X}: not used)")
            return
        if flag or val:  # a key token
            k = 2 * val if flag else 2 * val - 1
            if 0 <= k < len(KEYS):
                if self.off_hook and self.dial_open:  # digits typed after pick-up join the number
                    self.digits.append(KEYS[k])
                    self._arm_dial(self.LIVE_WINDOW)
                elif self.off_hook:  # keypress during a call
                    self.on_key(KEYS[k])
                else:              # pre-dial digits, sent as a burst at pick-up
                    self.digits.append(KEYS[k])
            return
        if hook:
            if self.timer:  # hook came back up quickly: it was the pick-up glitch
                self.timer.cancel()
                self.timer = None
            elif not self.off_hook:
                self.off_hook = True
                number = "".join(self.digits)
                if not number and (_ringing or sip.pending):
                    _leds()      # ring light off and "answered" on straight away: the phone only takes it in the
                    _leds(0x17)  # first moments after pick-up, otherwise it stays on its incoming-call screen
                    self.on_pickup("")  # answering an incoming call: nothing to dial
                elif not (_in_call or assist.active or sip.call):
                    self.dial_open = True  # keep collecting digits; dial when they stop
                    if number:
                        _leds(0x17)  # answer at once: the phone only takes this in the first moments after pick-up
                        self._arm_dial(self.PRE_WINDOW)
                else:
                    self.digits = []
        elif self.off_hook and not self.timer:
            self.timer = self._threading.Timer(self.DEBOUNCE, self._confirm_hangup)
            self.timer.start()

    def _arm_dial(self, delay):
        if self.dial_timer:
            self.dial_timer.cancel()
        self.dial_timer = self._threading.Timer(delay, self._commit_dial)
        self.dial_timer.daemon = True
        self.dial_timer.start()

    def _commit_dial(self):
        self.dial_timer = None
        if not self.dial_open:
            return
        self.dial_open = False
        number = "".join(self.digits)
        self.digits = []
        if number:
            self.on_pickup(number)

    def on_accept_key(self):
        """The Accept (answer / end) key on the phone's screen, bit 0x02 of byte1."""
        stamp = time.strftime('%H:%M:%S')
        if not self.off_hook and (_ringing or sip.pending):
            out(f"{stamp}  >> ACCEPT key (answering hands-free)")
            self.off_hook = True
            self.key_answered = True
            _leds()
            _leds(0x17)
            self.on_pickup("")
        elif self.off_hook and self.key_answered:
            out(f"{stamp}  >> END key")
            self._confirm_hangup()
        else:
            out(f"{stamp}  >> key pressed (bits 0x02: not used)")

    def _confirm_hangup(self):
        self.timer = None
        self.key_answered = False
        self.off_hook = False
        self.digits = []
        self.dial_open = False
        if self.dial_timer:
            self.dial_timer.cancel()
            self.dial_timer = None
        self.on_hangup()

    def on_pickup(self, number):
        number = resolve_speed(number)
        if not number and not _ringing and not sip.pending:
            # picked up without dialling or an incoming ring: nothing happens, so don't report a call
            out(f"{time.strftime('%H:%M:%S')}  >> pick up with no number (ignored)")
            return
        self.reported = True
        out(f"{time.strftime('%H:%M:%S')}  >> PICK UP  number={number or '(answering)'}")
        emit("pickup", number=number)
        route(number)

    def on_key(self, key):
        out(f"{time.strftime('%H:%M:%S')}  >> KEY {key}")
        emit("key", key=key)
        if _in_call:
            ivr.key(key)

    def on_hangup(self):
        if not self.reported:  # the ignored no-number pick-up: nothing to end
            return
        self.reported = False
        out(f"{time.strftime('%H:%M:%S')}  >> HANG UP")
        emit("hangup")
        stop_bot()
        sip.hangup()
        assist.stop()
        if args.hangup_cmd:
            import subprocess
            subprocess.Popen(args.hangup_cmd, shell=True)


def on_hold_key():
    """The Hold key: hold or resume a connected SIP call; the far end hears 'You have been placed on hold'."""
    stamp = time.strftime('%H:%M:%S')
    on = sip.toggle_hold()
    if on is None:
        out(f"{stamp}  >> HOLD key (only works during a connected SIP call)")
        return
    out(f"{stamp}  >> {'ON HOLD' if on else 'RESUMED'}")
    _leds(0x17, 0x20) if on else _leds(0x17, 0x21)  # Hold light on / back to the normal in-call lights


def _leds(*usages):
    """Tell the phone the call is connected (off-hook indicator) or clear it; never fatal."""
    try:
        import mp50_led
        mp50_led.set_leds(*usages)
    except Exception as e:
        out(f"  !! could not set phone indicator: {e}")


import mp50_ivr  # noqa: E402
import yea_assist  # noqa: E402
import yea_sip  # noqa: E402

ivr = mp50_ivr.IVR(lambda line: out(line), emit)
sip = yea_sip.SipClient(emit, lambda line: out(line))
assist = yea_assist.AssistClient(emit, lambda line: out(line))


def start_assist():
    """Open an Assist conversation through the phone (mic in, spoken reply out)."""
    if assist.active:
        return
    if not assist.configured():
        out(f"{time.strftime('%H:%M:%S')}  !! Assist is not set up (Settings > Home Assistant)")
        import threading
        import mp50_audio
        threading.Thread(target=lambda: mp50_audio.say("Home Assistant is not set up."), daemon=True).start()
        return
    out(f"{time.strftime('%H:%M:%S')}  >> ASSIST started")
    _leds(0x17)
    assist.start(ivr._device_index()[0])


def _sip_events(kind, data):
    """Mirror SIP call state on the phone's indicators: ring on incoming, off-hook once connected."""
    if kind == "assist_end":
        _leds()
        out(f"{time.strftime('%H:%M:%S')}  >> ASSIST ended ({data.get('reason', '')})")
    elif kind == "sip_incoming":
        _leds(0x18)
    elif kind == "sip_call":
        out(f"  (sip {data.get('state')} {data.get('direction')} {data.get('reason', '')} -> phone indicator)")
        if data.get("state") == "dialing":
            _leds()      # exactly what a test call does: clear, then off-hook
            _leds(0x17)
        elif data.get("state") == "answered":
            _leds(0x17, 0x21)  # off-hook + microphone: this is what flips the phone to its in-call screen
        elif data.get("state") == "ended":
            _leds()


listeners.append(_sip_events)
_in_call = False


def start_bot():
    """Test call: mark the phone off-hook ("connected") and run the keypad menu through the phone."""
    global _in_call
    stop_bot()
    _in_call = True
    out(f"{time.strftime('%H:%M:%S')}  >> TEST CALL started")
    emit("bot_start")
    _leds(0x17)  # Off-Hook indicator on: the host has "answered"
    ivr.start()


def stop_bot():
    global _in_call
    if _in_call:
        _in_call = False
        ivr.stop()
        out(f"{time.strftime('%H:%M:%S')}  >> TEST CALL ended")
        emit("bot_stop")
    _leds()


_ringing = False
_ring_timer = None
RING_TIMEOUT = 30


def ring_phone():
    """Incoming test call: switch on the phone's ring indicator. Picking up answers it."""
    import threading
    global _ringing, _ring_timer
    if decoder.off_hook:
        out("  !! phone is already off hook - hang up first")
        return
    stop_bot()
    _ringing = True
    _leds(0x18)  # Ring indicator
    out(f"{time.strftime('%H:%M:%S')}  >> RINGING (pick up to answer)")
    emit("ring_start")
    _ring_timer = threading.Timer(RING_TIMEOUT, lambda: stop_ring(missed=True))
    _ring_timer.daemon = True
    _ring_timer.start()


def stop_ring(missed=False, answered=False):
    global _ringing, _ring_timer
    if _ring_timer:
        _ring_timer.cancel()
        _ring_timer = None
    if not _ringing:
        return
    _ringing = False
    if not answered:
        _leds()
        out(f"{time.strftime('%H:%M:%S')}  >> RINGING stopped" + (" (no answer)" if missed else ""))
    emit("ring_stop", missed=missed, answered=answered)


def route(number):
    """Pick-up: answers a ringing test call; the test number goes to the bot; anything else to --dial-cmd."""
    if _ringing:
        stop_ring(answered=True)
        out(f"{time.strftime('%H:%M:%S')}  >> ANSWERED incoming test call")
        start_bot()
    elif number == args.test_number:
        start_bot()
    elif number == args.assist_number:
        start_assist()
    elif sip.pending:
        out(f"{time.strftime('%H:%M:%S')}  >> ANSWERED incoming SIP call")
        sip.answer_pending()
    elif number and sip.ready():
        out(f"{time.strftime('%H:%M:%S')}  >> SIP call to {number}")
        sip.call_number(number)
    elif args.dial_cmd and number:
        import subprocess
        subprocess.Popen(args.dial_cmd.replace("{number}", number), shell=True)


decoder = Decoder()


def on_input(hraw):
    sz = w.UINT(0)
    user32.GetRawInputData(hraw, RID_INPUT, None, ctypes.byref(sz), ctypes.sizeof(RAWINPUTHEADER))
    buf = ctypes.create_string_buffer(sz.value)
    if user32.GetRawInputData(hraw, RID_INPUT, buf, ctypes.byref(sz), ctypes.sizeof(RAWINPUTHEADER)) == 0xFFFFFFFF:
        return
    hdr = RAWINPUTHEADER.from_buffer_copy(buf)
    if hdr.dwType != RIM_TYPEHID:
        return
    d = describe(hdr.hDevice)
    if not d:
        return
    label, page, usage = d
    off = ctypes.sizeof(RAWINPUTHEADER)
    size_hid, count = ctypes.c_uint32.from_buffer_copy(buf, off).value, ctypes.c_uint32.from_buffer_copy(buf, off + 4).value
    data = bytes(buf.raw[off + 8: off + 8 + size_hid * count])
    for i in range(count):
        rep = data[i * size_hid:(i + 1) * size_hid]
        prev = last_report.get(label)
        last_report[label] = rep
        diff = ""
        if prev is not None and len(prev) == len(rep):
            ch = [str(j) for j in range(len(rep)) if rep[j] != prev[j]]
            diff = f"  changed bytes: {','.join(ch)}" if ch else "  (no change)"
        if not args.quiet:
            out(f"{time.strftime('%H:%M:%S')}.{int(time.time()*1000)%1000:03d}  {label:<12} page=0x{page:04X} usage=0x{usage:04X}  {rep.hex(' ')}{diff}")
        if (page, usage) == (0x0B, 0x05):
            decoder.feed(rep)


@WNDPROC
def wndproc(hwnd, msg, wparam, lparam):
    if msg == WM_INPUT:
        try:
            on_input(lparam)
        except Exception as e:  # keep the message loop alive
            print("error:", e, file=sys.stderr)
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def phone_audio_present():
    import mp50_audio
    return mp50_audio.find_device("out") is not None


def main():
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASS()
    wc.lpfnWndProc = wndproc
    wc.hInstance = hinst
    wc.lpszClassName = "MP50Listener"
    user32.RegisterClassW(ctypes.byref(wc))
    hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "mp50", 0, 0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())

    # Telephony, Consumer, Generic Desktop (keyboard-ish), and common vendor pages.
    pages = [0x0B, 0x0C, 0x01, 0xFF00, 0xFF01, 0xFF99, 0xFFA0]
    devs = (RAWINPUTDEVICE * len(pages))()
    for i, p in enumerate(pages):
        devs[i] = RAWINPUTDEVICE(p, 0, RIDEV_INPUTSINK | RIDEV_PAGEONLY, hwnd)
    if not user32.RegisterRawInputDevices(devs, len(pages), ctypes.sizeof(RAWINPUTDEVICE)):
        raise ctypes.WinError(ctypes.get_last_error())

    print(f"Listening for HID reports from VID_{VID}. Press buttons on the phone; Ctrl+C to quit.\n")
    msg = w.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
