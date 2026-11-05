"""Talk to the MP50's vendor data channel the way Yealink USB Connect does (worked out from USB recordings).

Report id 0xC8, 1024 bytes each way. Two kinds of message:
  * short commands, one report:   c8 13 LL LL | op(2) id(2) | fields ...       LL LL = 16 + payload length
      op 01 open file, 03 seek, 02 close, 16 set the clock ("date -s MMDDhhmmYYYY.ss")
  * long messages, up to 3 reports:  c8 11 / c8 10 / c8 12 | u16 | up to 1020 bytes of the message
      the u16 is the payload bytes sent so far + 4 per earlier report; a message is a 16 byte header
      (op(2) id(2) u32 length, u32 a, u32 b) plus 2048 bytes of body, shorter for the last file chunk
      op 07 set a setting (key padded to 32 bytes, then the value), op 05 write file data
The phone answers every message by echoing it back, and a file open returns the file handle.
"""
import ctypes
import os
import queue
import random
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
from mp50_replay import vendor_path, k32  # noqa: E402

REPORT = 1024


def pad(b, n=REPORT):
    return b + bytes(n - len(b))


def long_message(M):
    """Split a long message into its 1-3 reports."""
    reps, pos, sent = [], 0, 0
    while True:
        chunk = M[pos:pos + 1020]
        pos += len(chunk)
        sent += len(chunk)
        last = pos >= len(M)
        kind = 0x12 if last else (0x11 if not reps else 0x10)
        reps.append(pad(bytes([0xC8, kind]) + struct.pack("<H", sent + 4 * len(reps)) + chunk))
        if last:
            return reps


def set_setting(key, value, rid):
    body = pad(key.encode(), 32) + value.encode()
    M = struct.pack("<2sHIII", b"\x07\x02", rid, 2048, 0x11, 0) + pad(body, 2048)
    return long_message(M)


def write_chunk(handle, data, rid):
    M = struct.pack("<2sHIII", b"\x05\x02", rid, len(data), handle, len(data)) + data
    return long_message(M)


def short_command(op, rid, fields, payload=b""):
    return pad(b"\xc8\x13" + struct.pack("<H", 16 + len(payload)) + struct.pack("<2sH", op, rid) + fields + payload)


def open_file(path, rid):
    return short_command(b"\x01\x02", rid, struct.pack("<III", 256, 0x32, 0x1E4), pad(path.encode(), 256))


def seek_start(handle, rid):
    return short_command(b"\x03\x02", rid, struct.pack("<III", 8, handle, 0), bytes(8))


def close_file(handle, rid):
    return short_command(b"\x02\x02", rid, struct.pack("<III", 0, handle, 0))


def time_command(when, rid):
    text = b"date -s " + when.strftime("%m%d%H%M%Y.%S").encode() + b"\0"
    return short_command(b"\x16\x02", rid, struct.pack("<I", len(text)) + bytes(8), text)


class Vendor:
    def __init__(self):
        # separate handles for reading and writing: a blocked read would otherwise hold up every write on a shared one
        path = vendor_path()
        self.rh = k32.CreateFileW(path, 0x80000000, 3, None, 3, 0, None)
        self.fh = k32.CreateFileW(path, 0x40000000, 3, None, 3, 0, None)
        if ctypes.c_void_p(-1).value in (self.rh, self.fh) or None in (self.rh, self.fh):
            raise OSError("cannot open the MP50 vendor channel (is it plugged in over USB?)")
        self.replies = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = ctypes.create_string_buffer(REPORT)
        n = ctypes.c_uint32(0)
        while k32.ReadFile(ctypes.c_void_p(self.rh), buf, REPORT, ctypes.byref(n), None):
            self.replies.put(buf.raw[:n.value])

    def send(self, reports):
        for r in ([reports] if isinstance(reports, bytes) else reports):
            b = ctypes.create_string_buffer(r, len(r))
            n = ctypes.c_uint32(0)
            if not k32.WriteFile(ctypes.c_void_p(self.fh), b, len(r), ctypes.byref(n), None):
                raise OSError(f"WriteFile failed ({ctypes.get_last_error()})")

    def wait(self, op, timeout=2.0):
        """Wait for a short-command reply with this op code; returns the report or None."""
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
        k32.CloseHandle(ctypes.c_void_p(self.fh))


def rid():
    return random.randrange(0x10000)


def set_screensaver_custom(v, on):
    v.send(set_setting("screensaver.mode", "1" if on else "0", rid()))


def upload_screensaver(v, jpeg, name, log=print):
    """Upload a 288x480 JPEG into the phone's screensaver folder. Returns the file name used."""
    path = "/data/userdata/screensaver/" + name
    # only full 2048 byte chunks have been seen on the wire, so pad to a whole number (viewers ignore bytes after the JPEG end)
    jpeg = jpeg + bytes(-len(jpeg) % 2048)
    v.send(open_file(path, rid()))
    r = v.wait(0x01)
    if not r:
        raise OSError("the phone did not answer the file open")
    handle = struct.unpack_from("<I", r, 12)[0]
    log(f"opened {path}, handle 0x{handle:08X}")
    v.send(seek_start(handle, rid()))
    v.wait(0x03)
    for i in range(0, len(jpeg), 2048):
        v.send(write_chunk(handle, jpeg[i:i + 2048], rid()))
        if not v.wait(0x05, 1.0):
            raise OSError(f"no answer to chunk {i // 2048}")
    v.send(close_file(handle, rid()))
    v.wait(0x02)
    log(f"wrote {len(jpeg)} bytes")
    return name


if __name__ == "__main__":
    # offline self-test against a recording: rebuild the recorded messages and compare bytes
    from usb_decode import packets
    pk = [p for p in packets(sys.argv[1]) if p["ep"] == 0x04 and p["tr"] == 1 and len(p["data"]) == REPORT]
    ok = bad = 0

    def check(name, built, rec):
        global ok, bad
        if built == rec:
            ok += 1
        else:
            bad += 1
            print("MISMATCH", name)
    for i, p in enumerate(pk):
        d = p["data"]
        rid_ = struct.unpack_from("<H", d, 6)[0]
        if d[1] == 0x11 and d[4] == 7:  # set a setting
            key = d[20:52].rstrip(bytes(1)).decode()
            val = d[52:].split(b"\0")[0].decode()
            built = set_setting(key, val, rid_)
            check(f"set {key}={val}", b"".join(built), d + pk[i + 1]["data"] + pk[i + 2]["data"])
        elif d[1] == 0x11 and d[4] == 5:  # file data
            n = struct.unpack_from("<I", d, 8)[0]
            h = struct.unpack_from("<I", d, 12)[0]
            reps = [d, pk[i + 1]["data"]] + ([pk[i + 2]["data"]] if pk[i + 1]["data"][1] == 0x10 else [])
            M = b"".join(r[4:] for r in reps)
            body = M[16:16 + n]
            built = write_chunk(h, body, rid_)
            used = 16 + n  # the app leaves old bytes after the end of the message; only the used part matters
            check(f"write {n}", b"".join(r[:4 + min(1020, max(0, used - 1020 * k))] for k, r in enumerate(built)),
                  b"".join(r[:4 + min(1020, max(0, used - 1020 * k))] for k, r in enumerate(reps)))
        elif d[1] == 0x13 and d[4] == 1 and d[3] == 1:
            check("open", open_file(d[20:276].split(b"\0")[0].decode(), rid_), d)
        elif d[1] == 0x13 and d[4] == 3:
            check("seek", seek_start(struct.unpack_from("<I", d, 12)[0], rid_), d[:28] + bytes(REPORT - 28))
        elif d[1] == 0x13 and d[4] == 2:
            check("close", close_file(struct.unpack_from("<I", d, 12)[0], rid_), d[:20] + bytes(REPORT - 20))
        elif d[1] == 0x13 and d[4] == 0x16:
            import datetime
            t = d[20:].split(b"\0")[0].decode().split()[-1]
            when = datetime.datetime.strptime(t, "%m%d%H%M%Y.%S")
            check("time", time_command(when, rid_), d)
    print(f"{ok} messages rebuilt identically, {bad} different")
