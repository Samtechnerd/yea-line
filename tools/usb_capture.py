"""Record USB traffic from one USBPcap root hub (open the result with Wireshark, or read it with a script).

Needs the USBPcap driver (https://desowin.org/usbpcap) installed and a restart afterwards, and an administrator prompt.
The official USBPcapCMD.exe does the same job; this is a small stand-in that only listens, it never sends anything to a device.

Recording a whole hub also records every other USB device on it, keyboards included. So there are two steps:

    python usb_capture.py scan 15 [hub]                 lists the devices that talk, with counts only (no data is kept)
    python usb_capture.py record out.pcap 60 ADDR [hub]  records only the device with that USB address
"""
import ctypes
import os
import struct
import sys
import threading
import time
from collections import defaultdict
from ctypes import wintypes

k32 = ctypes.windll.kernel32
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                            wintypes.DWORD, wintypes.HANDLE]
k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]


def ctl(func, access):
    return (0x22 << 16) | (access << 14) | (func << 2)  # FILE_DEVICE_UNKNOWN, METHOD_BUFFERED


SETUP_BUFFER = ctl(0x800, 1)
START_FILTERING = ctl(0x801, 3)
STOP_FILTERING = ctl(0x802, 3)
SET_SNAPLEN = ctl(0x804, 1)
TRANSFERS = {0: "isoc", 1: "interrupt", 2: "control", 3: "bulk", 0xFE: "irp-info", 0xFF: "unknown"}


def ioctl(h, code, data):
    buf = ctypes.create_string_buffer(data, len(data))
    out = wintypes.DWORD(0)
    if not k32.DeviceIoControl(h, code, buf, len(data), None, 0, ctypes.byref(out), None):
        raise OSError(f"DeviceIoControl 0x{code:X} failed (error {ctypes.GetLastError()})")


def open_hub(hub):
    h = k32.CreateFileW("\\\\.\\USBPcap%d" % hub, 0xC0000000, 0, None, 3, 0, None)
    if h in (None, wintypes.HANDLE(-1).value, 0xFFFFFFFFFFFFFFFF, -1):
        sys.exit(f"cannot open USBPcap{hub} (error {ctypes.GetLastError()}): run as administrator, after the driver is "
                 "installed and the PC restarted")
    return h


def start(h, addr=None):
    ioctl(h, SET_SNAPLEN, struct.pack("<I", 65535))
    ioctl(h, SETUP_BUFFER, struct.pack("<I", 1024 * 1024))
    words, every = [0, 0, 0, 0], 1
    if addr is not None:
        words[addr // 32] |= 1 << (addr % 32)  # only this device address
        every = 0
    for pad in (b"", bytes(3)):  # the driver's struct may or may not be padded
        try:
            ioctl(h, START_FILTERING, struct.pack("<4IB", *words, every) + pad)
            return
        except OSError as e:
            err = e
    raise err


def read_loop(h, sink):
    buf = ctypes.create_string_buffer(1024 * 1024)
    got = wintypes.DWORD(0)
    while True:
        if not k32.ReadFile(h, buf, len(buf), ctypes.byref(got), None):
            return
        sink(buf.raw[:got.value])


def scan(hub, secs):
    """Count packets per device and endpoint. Payloads are read only to get their length and are never stored."""
    h = open_hub(hub)
    start(h)
    stats = defaultdict(lambda: [0, 0])  # (device, endpoint, transfer) -> [packets, largest payload]
    pend = bytearray()
    state = {"hdr": False}

    def sink(chunk):
        pend.extend(chunk)
        if not state["hdr"]:
            if len(pend) < 24:
                return
            del pend[:24]
            state["hdr"] = True
        while len(pend) >= 16:
            incl = struct.unpack_from("<I", pend, 8)[0]
            if len(pend) < 16 + incl:
                return
            p = bytes(pend[16:16 + incl])
            del pend[:16 + incl]
            if len(p) >= 27:
                dev, ep, tr, dlen = struct.unpack_from("<HBBI", p, 19)
                s = stats[(dev, ep, tr)]
                s[0] += 1
                s[1] = max(s[1], dlen)

    threading.Thread(target=read_loop, args=(h, sink), daemon=True).start()
    print(f"listening on USBPcap{hub} for {secs:g} s (counts only) ...", flush=True)
    time.sleep(secs)
    try:
        ioctl(h, STOP_FILTERING, b"")
    except OSError:
        pass
    time.sleep(0.3)
    print("device  endpoint  transfer   packets  largest payload")
    for (dev, ep, tr), (n, big) in sorted(stats.items()):
        print(f"{dev:6d}  0x{ep:02X}      {TRANSFERS.get(tr, tr):9s} {n:8d}  {big:6d}")
    sys.stdout.flush()
    os._exit(0)


def record(path, secs, addr, hub):
    h = open_hub(hub)
    start(h, addr)
    f = open(path, "wb")
    total = [0]

    def sink(chunk):
        f.write(chunk)
        total[0] += len(chunk)

    threading.Thread(target=read_loop, args=(h, sink), daemon=True).start()
    print(f"recording device {addr} on USBPcap{hub} to {path} for {secs:g} s ...", flush=True)
    time.sleep(secs)
    try:
        ioctl(h, STOP_FILTERING, b"")
    except OSError:
        pass
    time.sleep(0.5)
    f.flush()
    print(f"done: {total[0]} bytes", flush=True)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "scan":
        scan(int(a[2]) if len(a) > 2 else 1, float(a[1]))
    elif len(a) >= 4 and a[0] == "record":
        record(a[1], float(a[2]), int(a[3]), int(a[4]) if len(a) > 4 else 1)
    else:
        sys.exit(__doc__)
