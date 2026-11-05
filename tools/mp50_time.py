"""Set the MP50's clock the way Yealink USB Connect does: a vendor-channel message carrying `date -s MMDDhhmmYYYY.ss`.

    python mp50_time.py                 sets the phone to this PC's current local time
    python mp50_time.py +5              sets it 5 minutes ahead (for testing)

The 20 header bytes are copied from a recording of the Yealink app; only the date text is new.
"""
import ctypes
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from mp50_replay import send, vendor_path, k32  # noqa: E402

HEADER = bytes.fromhex("c8132800" "1602232f" "18000000" "00000000" "00000000")  # as recorded; bytes 2-3 = 16 + text length, byte 8 = text length


def message(when):
    text = b"date -s " + when.strftime("%m%d%H%M%Y.%S").encode() + b"\0"
    head = bytearray(HEADER)
    head[2] = 16 + len(text)
    head[8] = len(text)
    return bytes(head) + text + bytes(1024 - len(head) - len(text))


def set_time(when=None):
    when = when or datetime.datetime.now()
    fh = k32.CreateFileW(vendor_path(), 0xC0000000, 3, None, 3, 0, None)
    if fh in (None, ctypes.c_void_p(-1).value):
        raise OSError("cannot open the vendor collection for writing")
    try:
        send(fh, message(when))
    finally:
        k32.CloseHandle(ctypes.c_void_p(fh))
    return when


if __name__ == "__main__":
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    t = set_time(datetime.datetime.now() + datetime.timedelta(minutes=minutes))
    print("sent", t.strftime("%Y-%m-%d %H:%M:%S"))
