"""Store secrets in Windows Credential Manager (encrypted by Windows, tied to this user account)."""
import ctypes
from ctypes import wintypes as w

advapi = ctypes.WinDLL("advapi32", use_last_error=True)
CRED_TYPE_GENERIC, CRED_PERSIST_LOCAL_MACHINE = 1, 2


class CREDENTIAL(ctypes.Structure):
    _fields_ = [("Flags", w.DWORD), ("Type", w.DWORD), ("TargetName", w.LPWSTR), ("Comment", w.LPWSTR),
                ("LastWritten", w.FILETIME), ("CredentialBlobSize", w.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                ("Persist", w.DWORD), ("AttributeCount", w.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", w.LPWSTR), ("UserName", w.LPWSTR)]


advapi.CredReadW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
advapi.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), w.DWORD]
advapi.CredDeleteW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD]
advapi.CredFree.argtypes = [ctypes.c_void_p]


def set_secret(target, username, secret):
    blob = secret.encode("utf-16-le")
    buf = (ctypes.c_byte * len(blob)).from_buffer_copy(blob)
    cred = CREDENTIAL(0, CRED_TYPE_GENERIC, target, None, w.FILETIME(), len(blob), buf,
                      CRED_PERSIST_LOCAL_MACHINE, 0, None, None, username)
    if not advapi.CredWriteW(ctypes.byref(cred), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def get_secret(target):
    """Return the stored secret, or None."""
    p = ctypes.POINTER(CREDENTIAL)()
    if not advapi.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
        return None
    try:
        c = p.contents
        return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize).decode("utf-16-le")
    finally:
        advapi.CredFree(p)


def delete_secret(target):
    advapi.CredDeleteW(target, CRED_TYPE_GENERIC, 0)
