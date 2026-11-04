"""Read-only: list the OUTPUT (host->phone) button usages of the telephony collections."""
import ctypes
from ctypes import wintypes as w
hid=ctypes.WinDLL("hid"); setupapi=ctypes.WinDLL("setupapi"); k32=ctypes.WinDLL("kernel32",use_last_error=True)
class GUID(ctypes.Structure): _fields_=[("a",w.DWORD),("b",w.WORD),("c",w.WORD),("d",ctypes.c_ubyte*8)]
class SPDID(ctypes.Structure): _fields_=[("cb",w.DWORD),("g",GUID),("f",w.DWORD),("r",ctypes.c_void_p)]
class CAPS(ctypes.Structure): _fields_=[("Usage",w.USHORT),("UsagePage",w.USHORT),("InLen",w.USHORT),("OutLen",w.USHORT),("FeatLen",w.USHORT),("res",w.USHORT*17),("NumLinkColl",w.USHORT),("NumInBtn",w.USHORT),("NumInVal",w.USHORT),("NumInData",w.USHORT),("NumOutBtn",w.USHORT),("NumOutVal",w.USHORT),("NumOutData",w.USHORT),("NumFeatBtn",w.USHORT),("NumFeatVal",w.USHORT),("NumFeatData",w.USHORT)]
class RNG(ctypes.Structure): _fields_=[("UMin",w.USHORT),("UMax",w.USHORT),("SMin",w.USHORT),("SMax",w.USHORT),("DMin",w.USHORT),("DMax",w.USHORT),("IMin",w.USHORT),("IMax",w.USHORT)]
class BCAP(ctypes.Structure): _fields_=[("UsagePage",w.USHORT),("ReportID",ctypes.c_ubyte),("IsAlias",ctypes.c_ubyte),("BitField",w.USHORT),("LinkColl",w.USHORT),("LinkUsage",w.USHORT),("LinkPage",w.USHORT),("IsRange",ctypes.c_ubyte),("IsStr",ctypes.c_ubyte),("IsDes",ctypes.c_ubyte),("IsAbs",ctypes.c_ubyte),("res",w.ULONG*10),("r",RNG)]
g=GUID(); hid.HidD_GetHidGuid(ctypes.byref(g))
setupapi.SetupDiGetClassDevsW.restype=ctypes.c_void_p
h=setupapi.SetupDiGetClassDevsW(ctypes.byref(g),None,None,0x12)
k32.CreateFileW.restype=ctypes.c_void_p
i=0
while True:
    d=SPDID(); d.cb=ctypes.sizeof(d)
    if not setupapi.SetupDiEnumDeviceInterfaces(ctypes.c_void_p(h),None,ctypes.byref(g),i,ctypes.byref(d)): break
    i+=1
    n=w.DWORD(); setupapi.SetupDiGetDeviceInterfaceDetailW(ctypes.c_void_p(h),ctypes.byref(d),None,0,ctypes.byref(n),None)
    buf=ctypes.create_string_buffer(n.value); ctypes.c_uint32.from_buffer(buf).value=8
    if not setupapi.SetupDiGetDeviceInterfaceDetailW(ctypes.c_void_p(h),ctypes.byref(d),buf,n,None,None): continue
    path=ctypes.wstring_at(ctypes.addressof(buf)+4)
    if "vid_6993" not in path.lower() or "mi_03" not in path.lower(): continue
    fh=k32.CreateFileW(path,0,3,None,3,0,None); pp=ctypes.c_void_p()
    if not hid.HidD_GetPreparsedData(ctypes.c_void_p(fh),ctypes.byref(pp)): continue
    c=CAPS(); hid.HidP_GetCaps(pp,ctypes.byref(c))
    if c.NumOutBtn:
        cnt=w.USHORT(c.NumOutBtn); arr=(BCAP*c.NumOutBtn)()
        hid.HidP_GetButtonCaps(1,arr,ctypes.byref(cnt),pp)   # 1 = output
        print(path.split('#')[1].upper(), f"page=0x{c.UsagePage:04X} usage=0x{c.Usage:04X} outLen={c.OutLen}")
        for b in arr[:cnt.value]:
            u=(f"0x{b.r.UMin:02X}-0x{b.r.UMax:02X}" if b.IsRange else f"0x{b.r.UMin:02X}")
            print(f"   report id {b.ReportID}  usage page 0x{b.UsagePage:04X}  usages {u}")
    hid.HidD_FreePreparsedData(pp); k32.CloseHandle(ctypes.c_void_p(fh))
