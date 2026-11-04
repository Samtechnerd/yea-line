"""Send the standard telephony LED/indicator output report (report id 2) to a Yealink MP50.

Usages on the LED page (0x08): 0x17 Off-Hook, 0x18 Ring, 0x09 Mute, 0x20 Hold, 0x21 Microphone.
set_leds() with no arguments clears all of them. HidP builds the report so bit order is never guessed.
"""
import ctypes
from ctypes import wintypes as w

hid = ctypes.WinDLL("hid")
setupapi = ctypes.WinDLL("setupapi")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p

OFF_HOOK, RING, MUTE, HOLD, MIC = 0x17, 0x18, 0x09, 0x20, 0x21
LED_PAGE = 0x0008
HIDP_OUTPUT = 1


class GUID(ctypes.Structure):
    _fields_ = [("a", w.DWORD), ("b", w.WORD), ("c", w.WORD), ("d", ctypes.c_ubyte * 8)]


class SPDID(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("g", GUID), ("f", w.DWORD), ("r", ctypes.c_void_p)]


class CAPS(ctypes.Structure):
    _fields_ = [("Usage", w.USHORT), ("UsagePage", w.USHORT), ("InLen", w.USHORT), ("OutLen", w.USHORT),
                ("FeatLen", w.USHORT), ("res", w.USHORT * 17), ("rest", w.USHORT * 10)]


def _paths(vid="6993"):
    g = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(g))
    h = setupapi.SetupDiGetClassDevsW(ctypes.byref(g), None, None, 0x12)
    i = 0
    while True:
        d = SPDID()
        d.cb = ctypes.sizeof(d)
        if not setupapi.SetupDiEnumDeviceInterfaces(ctypes.c_void_p(h), None, ctypes.byref(g), i, ctypes.byref(d)):
            return
        i += 1
        n = w.DWORD()
        setupapi.SetupDiGetDeviceInterfaceDetailW(ctypes.c_void_p(h), ctypes.byref(d), None, 0, ctypes.byref(n), None)
        buf = ctypes.create_string_buffer(n.value)
        ctypes.c_uint32.from_buffer(buf).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
        if setupapi.SetupDiGetDeviceInterfaceDetailW(ctypes.c_void_p(h), ctypes.byref(d), buf, n, None, None):
            p = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            if f"vid_{vid}" in p.lower():
                yield p


def build_report(usages):
    """Return (report_bytes, path) for the telephony collection (usage page 0x0B, usage 0x05)."""
    for path in _paths():
        if "mi_03" not in path.lower():
            continue
        fh = k32.CreateFileW(path, 0, 3, None, 3, 0, None)
        pp = ctypes.c_void_p()
        if not hid.HidD_GetPreparsedData(ctypes.c_void_p(fh), ctypes.byref(pp)):
            continue
        try:
            c = CAPS()
            hid.HidP_GetCaps(pp, ctypes.byref(c))
            if (c.UsagePage, c.Usage) != (0x0B, 0x05) or not c.OutLen:
                continue
            rep = ctypes.create_string_buffer(c.OutLen)
            hid.HidP_InitializeReportForID(HIDP_OUTPUT, 2, pp, rep, c.OutLen)
            if usages:
                arr = (w.USHORT * len(usages))(*usages)
                cnt = w.ULONG(len(usages))
                st = hid.HidP_SetUsages(HIDP_OUTPUT, LED_PAGE, 0, arr, ctypes.byref(cnt), pp, rep, c.OutLen)
                if st & 0xFFFFFFFF != 0x00110000:
                    raise OSError(f"HidP_SetUsages failed 0x{st & 0xFFFFFFFF:08X}")
            return bytes(rep), path
        finally:
            hid.HidD_FreePreparsedData(pp)
            k32.CloseHandle(ctypes.c_void_p(fh))
    raise OSError("MP50 telephony collection not found")


def set_leds(*usages):
    data, path = build_report(list(usages))
    fh = k32.CreateFileW(path, 0xC0000000, 3, None, 3, 0, None)  # read+write, shared
    if fh in (None, ctypes.c_void_p(-1).value):
        raise OSError("cannot open the phone for writing")
    try:
        buf = ctypes.create_string_buffer(data, len(data))
        if not hid.HidD_SetOutputReport(ctypes.c_void_p(fh), buf, len(data)):
            raise OSError(f"HidD_SetOutputReport failed ({ctypes.get_last_error()})")
    finally:
        k32.CloseHandle(ctypes.c_void_p(fh))
    return data


if __name__ == "__main__":
    for name, u in [("clear", []), ("off-hook", [OFF_HOOK]), ("ring", [RING]), ("mute", [MUTE]), ("hold", [HOLD]), ("mic", [MIC])]:
        print(f"{name:9}", build_report(u)[0].hex(" "))
