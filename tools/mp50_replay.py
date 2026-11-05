"""Replay messages that Yealink USB Connect was recorded sending to the MP50's vendor data channel (report id 0xC8, 1024 bytes).

Only ever sends bytes taken from a recording, never invented ones.

    python mp50_replay.py capture.pcap START END [START END ...]     e.g. 11.8 11.85 29.69 29.71 (seconds in the recording)
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import mp50_led as led  # noqa: E402  (HID enumeration helpers)
from usb_decode import packets  # noqa: E402

k32 = led.k32


def vendor_path():
    for p in led._paths():
        if "mi_03" in p.lower() and "col05" in p.lower():
            return p
    raise OSError("MP50 vendor collection (MI_03 COL05) not found: is it plugged in over USB?")


def send(fh, data):
    buf = ctypes.create_string_buffer(data, len(data))
    n = ctypes.c_uint32(0)
    if not k32.WriteFile(ctypes.c_void_p(fh), buf, len(data), ctypes.byref(n), None):
        raise OSError(f"WriteFile failed ({ctypes.get_last_error()})")
    return n.value


if __name__ == "__main__":
    pcap, spans = sys.argv[1], [float(x) for x in sys.argv[2:]]
    windows = list(zip(spans[::2], spans[1::2]))
    path = vendor_path()
    fh = k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0, None)
    if fh in (None, ctypes.c_void_p(-1).value):
        sys.exit("cannot open the vendor collection for writing")
    out = [p for p in packets(pcap) if p["ep"] == 0x04 and p["tr"] == 1 and len(p["data"]) == 1024]
    for a, b in windows:
        batch = [p for p in out if a <= p["t"] <= b and p["data"][1] != 0x13]  # skip the Bluetooth status message
        print(f"window {a}-{b}: {len(batch)} messages")
        for p in batch:
            d = p["data"]
            print(f"  send {d.rstrip(bytes(1))[:60].hex(' ')}")
            send(fh, d)
            time.sleep(0.05)
        print("  waiting 20 s", flush=True)
        time.sleep(20)
    k32.CloseHandle(ctypes.c_void_p(fh))
