"""Send a message to the MP50's vendor channel and print what comes back in the next few seconds.

    python mp50_listen.py time +10        send a time message (minutes ahead) and show the replies
    python mp50_listen.py listen 10       just listen for 10 seconds
"""
import ctypes
import datetime
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
from mp50_replay import send, vendor_path, k32  # noqa: E402
from mp50_time import message  # noqa: E402


def main():
    fh = k32.CreateFileW(vendor_path(), 0xC0000000, 3, None, 3, 0, None)
    if fh in (None, ctypes.c_void_p(-1).value):
        sys.exit("cannot open the vendor collection")
    t0 = time.time()

    def reader():
        buf = ctypes.create_string_buffer(1024)
        n = ctypes.c_uint32(0)
        while True:
            if not k32.ReadFile(ctypes.c_void_p(fh), buf, 1024, ctypes.byref(n), None):
                return
            d = buf.raw[:n.value].rstrip(bytes(1))
            print(f"{time.time() - t0:6.2f}s <- {len(d)}B {d[:70].hex(' ')}", flush=True)

    threading.Thread(target=reader, daemon=True).start()
    a = sys.argv[1:]
    if a and a[0] == "time":
        t = datetime.datetime.now() + datetime.timedelta(minutes=int(a[1]))
        send(fh, message(t))
        print(f"{time.time() - t0:6.2f}s -> sent time {t:%H:%M:%S}", flush=True)
        wait = 4
    else:
        wait = float(a[1]) if len(a) > 1 else 10
    time.sleep(wait)
    sys.stdout.flush()
    os._exit(0)


main()
