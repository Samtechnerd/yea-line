"""Read-only: list HID collections of a device (default VID 6993) with their report sizes.
Opens each interface with access 0 (query only) - nothing is read from or written to the device."""
import ctypes, sys
from ctypes import wintypes as w
VID = (sys.argv[1] if len(sys.argv) > 1 else "6993").lower()
hid = ctypes.WinDLL("hid"); setupapi = ctypes.WinDLL("setupapi"); k32 = ctypes.WinDLL("kernel32", use_last_error=True)
class GUID(ctypes.Structure): _fields_=[("a",w.DWORD),("b",w.WORD),("c",w.WORD),("d",ctypes.c_ubyte*8)]
class SPDID(ctypes.Structure): _fields_=[("cb",w.DWORD),("g",GUID),("f",w.DWORD),("r",ctypes.c_void_p)]
class CAPS(ctypes.Structure): _fields_=[("Usage",w.USHORT),("UsagePage",w.USHORT),("InLen",w.USHORT),("OutLen",w.USHORT),("FeatLen",w.USHORT),("res",w.USHORT*17),("NumLinkColl",w.USHORT),("NumInBtn",w.USHORT),("NumInVal",w.USHORT),("NumInData",w.USHORT),("NumOutBtn",w.USHORT),("NumOutVal",w.USHORT),("NumOutData",w.USHORT),("NumFeatBtn",w.USHORT),("NumFeatVal",w.USHORT),("NumFeatData",w.USHORT)]
g=GUID(); hid.HidD_GetHidGuid(ctypes.byref(g))
setupapi.SetupDiGetClassDevsW.restype=ctypes.c_void_p
h=setupapi.SetupDiGetClassDevsW(ctypes.byref(g),None,None,0x12)
i=0
k32.CreateFileW.restype=ctypes.c_void_p
while True:
    d=SPDID(); d.cb=ctypes.sizeof(d)
    if not setupapi.SetupDiEnumDeviceInterfaces(ctypes.c_void_p(h),None,ctypes.byref(g),i,ctypes.byref(d)): break
    i+=1
    n=w.DWORD(); setupapi.SetupDiGetDeviceInterfaceDetailW(ctypes.c_void_p(h),ctypes.byref(d),None,0,ctypes.byref(n),None)
    buf=ctypes.create_string_buffer(n.value); ctypes.c_uint32.from_buffer(buf).value=8 if ctypes.sizeof(ctypes.c_void_p)==8 else 6
    if not setupapi.SetupDiGetDeviceInterfaceDetailW(ctypes.c_void_p(h),ctypes.byref(d),buf,n,None,None): continue
    path=ctypes.wstring_at(ctypes.addressof(buf)+4)
    if f"vid_{VID}" not in path.lower(): continue
    fh=k32.CreateFileW(path,0,3,None,3,0,None)
    short=path.split("#")[1].upper()+" "+(path.split("#")[2] if path.count("#")>2 else "")
    if fh in (None,ctypes.c_void_p(-1).value): print(short,"-> cannot open (query)"); continue
    pp=ctypes.c_void_p()
    if hid.HidD_GetPreparsedData(ctypes.c_void_p(fh),ctypes.byref(pp)):
        c=CAPS(); hid.HidP_GetCaps(pp,ctypes.byref(c))
        print(f"{short}\n   page=0x{c.UsagePage:04X} usage=0x{c.Usage:04X}  in={c.InLen}B out={c.OutLen}B feature={c.FeatLen}B  outputs(btn/val)={c.NumOutBtn}/{c.NumOutVal} features(btn/val)={c.NumFeatBtn}/{c.NumFeatVal}")
        hid.HidD_FreePreparsedData(pp)
    k32.CloseHandle(ctypes.c_void_p(fh))
