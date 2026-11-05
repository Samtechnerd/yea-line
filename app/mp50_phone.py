"""Phone screen settings for the Yealink MP50: light/dark mode, the custom screensaver picture and the screensaver delay.

Yealink USB Connect does the same thing through a vendor-defined HID channel (usage page 0xFF00, report id 0xC8, 1024 byte
reports). The message layout was worked out from USB recordings; see tools/mp50_vendor.py for the notes and the offline
self-test that rebuilds the recorded messages byte for byte.

Two kinds of message:
  * short commands, one report:      c8 13 LL LL | op(2) id(2) | fields ...     LL LL = 16 + payload length
      op 01 open a file, 03 seek, 02 close, 16 run a shell command (`rm <file>`; the Yealink app also sets the clock
      with `date -s`). That one runs on the phone, so only the fixed command below is ever built.
  * long messages, up to 3 reports:  c8 11 / c8 10 / c8 12 | u16 | up to 1020 bytes of the message
      a message is a 16 byte header (op(2) id(2) u32 length, u32 a, u32 b) plus a 2048 byte body
      op 07 set a setting (key padded to 32 bytes, then the value as text), op 05 write file data
The phone echoes every message back, and a file open returns the file handle.
"""
import ctypes
import io
import queue
import random
import struct
import threading
import time

import mp50_led as led

k32 = led.k32
REPORT = 1024
SCREEN = (288, 480)  # the MP50's screen, portrait
SAVER_DIR = "/data/userdata/screensaver/"
# The phone only rereads the screensaver folder when the screensaver mode is switched on, so after sending pictures the mode
# is toggled. Pictures are named so they sort before the Yealink app's own timestamp names. At most MAX_PICTURES.
MAX_PICTURES = 9
LEGACY_FILES = ["00000000-yealine.jpg"]  # the single fixed name an earlier version used


def picture_name(i):
    return f"0000-yealine-{i}.jpg"

# screensaver delay choices shown in the UI: label -> seconds
WAIT_CHOICES = {"30 seconds": 30, "1 minute": 60, "2 minutes": 120, "5 minutes": 300, "10 minutes": 600, "15 minutes": 900,
                "30 minutes": 1800, "1 hour": 3600, "2 hours": 7200}


class PhoneError(Exception):
    pass


def _pad(b, n=REPORT):
    return b + bytes(n - len(b))


def _long_message(M):
    reps, pos, sent = [], 0, 0
    while True:
        chunk = M[pos:pos + 1020]
        pos += len(chunk)
        sent += len(chunk)
        last = pos >= len(M)
        kind = 0x12 if last else (0x11 if not reps else 0x10)
        reps.append(_pad(bytes([0xC8, kind]) + struct.pack("<H", sent + 4 * len(reps)) + chunk))
        if last:
            return reps


def _short(op, rid, fields, payload=b""):
    return _pad(b"\xc8\x13" + struct.pack("<H", 16 + len(payload)) + struct.pack("<2sH", op, rid) + fields + payload)


def _rid():
    return random.randrange(0x10000)


def msg_setting(key, value):
    body = _pad(key.encode(), 32) + value.encode()
    return _long_message(struct.pack("<2sHIII", b"\x07\x02", _rid(), 2048, 0x11, 0) + _pad(body, 2048))


def msg_write(handle, data):
    return _long_message(struct.pack("<2sHIII", b"\x05\x02", _rid(), len(data), handle, len(data)) + data)


def msg_open(path):
    return _short(b"\x01\x02", _rid(), struct.pack("<III", 256, 0x32, 0x1E4), _pad(path.encode(), 256))


def msg_seek_start(handle):
    return _short(b"\x03\x02", _rid(), struct.pack("<III", 8, handle, 0), bytes(8))


def msg_close(handle):
    return _short(b"\x02\x02", _rid(), struct.pack("<III", 0, handle, 0))


def msg_remove_picture(name):
    """Delete one picture from the screensaver folder. The name must be a plain .jpg file name: nothing else is sent."""
    import re
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}\.jpg", name) or ".." in name:
        raise PhoneError("Refusing to delete that file name.")
    text = ("rm " + SAVER_DIR + name).encode() + b"\0"
    return _short(b"\x16\x02", _rid(), struct.pack("<I", len(text)) + bytes(8), text)


def vendor_path():
    for p in led._paths():
        if "mi_03" in p.lower() and "col05" in p.lower():
            return p
    return None


def available():
    """True when the phone's vendor channel is there (the MP50 is plugged in over USB)."""
    try:
        return vendor_path() is not None
    except Exception:
        return False


class Channel:
    """Open the vendor channel. Reads and writes use separate handles: a read waiting for the phone would otherwise
    block every write made on the same one."""

    def __init__(self):
        path = vendor_path()
        if not path:
            raise PhoneError("Phone not found. Plug the MP50 in with its USB cable (not Bluetooth mode).")
        bad = (None, ctypes.c_void_p(-1).value)
        self.rh = k32.CreateFileW(path, 0x80000000, 3, None, 3, 0, None)
        self.wh = k32.CreateFileW(path, 0x40000000, 3, None, 3, 0, None)
        if self.rh in bad or self.wh in bad:
            raise PhoneError("Could not open the phone. Is another program using it?")
        self.replies = queue.Queue()
        self.alive = True
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = ctypes.create_string_buffer(REPORT)
        n = ctypes.c_uint32(0)
        while self.alive and k32.ReadFile(ctypes.c_void_p(self.rh), buf, REPORT, ctypes.byref(n), None):
            self.replies.put(buf.raw[:n.value])

    def send(self, reports):
        for r in ([reports] if isinstance(reports, bytes) else reports):
            b = ctypes.create_string_buffer(r, len(r))
            n = ctypes.c_uint32(0)
            if not k32.WriteFile(ctypes.c_void_p(self.wh), b, len(r), ctypes.byref(n), None):
                raise PhoneError(f"Could not send to the phone (error {ctypes.get_last_error()})")

    def wait(self, op, timeout=2.0):
        end = time.time() + timeout
        while time.time() < end:
            try:
                r = self.replies.get(timeout=0.1)
            except queue.Empty:
                continue
            if r[1] == 0x13 and r[4] == op:
                return r
        return None

    def close(self):
        self.alive = False
        # closing the write handle only; the reader thread ends with the process (a blocked read cannot be cancelled here)
        k32.CloseHandle(ctypes.c_void_p(self.wh))


_lock = threading.Lock()


def _setting(key, value):
    with _lock:
        ch = Channel()
        try:
            ch.send(msg_setting(key, value))
            time.sleep(0.5)
        finally:
            ch.close()


def set_dark(dark):
    _setting("theme.mode", "1" if dark else "0")


def set_screensaver_wait(seconds):
    _setting("screensaver.wait_time", str(int(seconds)))


def use_custom_screensaver(on):
    _setting("screensaver.mode", "1" if on else "0")


def prepare_picture(path):
    """Crop and scale any picture to the phone's screen and return JPEG bytes padded to whole 2048 byte chunks."""
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    im = ImageOps.fit(im, SCREEN, Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=88)
    jpeg = out.getvalue()
    # only whole 2048 byte chunks have been seen on the wire; JPEG readers ignore bytes after the end of the picture
    return jpeg + bytes(-len(jpeg) % 2048)


def _send_file(ch, name, jpeg, progress, done, total):
    ch.send(msg_open(SAVER_DIR + name))
    r = ch.wait(0x01)
    if not r:
        raise PhoneError("The phone did not answer. Try unplugging and replugging it.")
    handle = struct.unpack_from("<I", r, 12)[0]
    ch.send(msg_seek_start(handle))
    ch.wait(0x03)
    for i in range(0, len(jpeg), 2048):
        ch.send(msg_write(handle, jpeg[i:i + 2048]))
        if not ch.wait(0x05, 1.5):
            raise PhoneError("The phone stopped answering while receiving a picture.")
        done += 1
        if progress:
            progress(done, total)
    ch.send(msg_close(handle))
    ch.wait(0x02)
    return done


def upload_pictures(jpegs, progress=None):
    """Send prepared pictures (a list of JPEG bytes, at most MAX_PICTURES) into the phone's screensaver folder, remove
    any older ones of ours, and switch the screensaver to custom pictures."""
    if not jpegs:
        raise PhoneError("Add at least one picture first.")
    if len(jpegs) > MAX_PICTURES:
        raise PhoneError(f"At most {MAX_PICTURES} pictures.")
    total = sum(len(j) // 2048 for j in jpegs)
    with _lock:
        ch = Channel()
        try:
            done = 0
            for i, jpeg in enumerate(jpegs, start=1):
                done = _send_file(ch, picture_name(i), jpeg, progress, done, total)
            stale = [picture_name(i) for i in range(len(jpegs) + 1, MAX_PICTURES + 1)] + LEGACY_FILES
            for n in stale:
                ch.send(msg_remove_picture(n))
                time.sleep(0.15)
            ch.send(msg_setting("screensaver.mode", "0"))  # off and on again makes the phone reread the folder
            time.sleep(1.0)
            ch.send(msg_setting("screensaver.mode", "1"))
            time.sleep(0.5)
        finally:
            ch.close()
    return len(jpegs)


def remove_pictures(names, switch_to_system=False):
    """Delete pictures (plain file names) from the phone's screensaver folder; optionally go back to the system ones."""
    with _lock:
        ch = Channel()
        try:
            for n in names:
                ch.send(msg_remove_picture(n))
                time.sleep(0.15)
            if switch_to_system:
                ch.send(msg_setting("screensaver.mode", "0"))
                time.sleep(0.5)
        finally:
            ch.close()


def remove_my_pictures():
    """Every picture Yea-Line sent: delete them and fall back to the system screensavers."""
    remove_pictures([picture_name(i) for i in range(1, MAX_PICTURES + 1)] + LEGACY_FILES, switch_to_system=True)
