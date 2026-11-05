"""Print the packets of a USBPcap recording made with usb_capture.py (or Wireshark).

    python usb_decode.py capture.pcap [max bytes per packet, default 64] [intr]

With "intr" only the interrupt transfers are shown (the MP50's vendor data channels), and trailing zero bytes are cut.
"""
import struct
import sys

TRANSFERS = {0: "isoc", 1: "intr", 2: "ctrl", 3: "bulk", 0xFE: "irp", 0xFF: "?"}


def packets(path):
    b = open(path, "rb").read()
    pos = 24  # pcap global header
    t0 = None
    while pos + 16 <= len(b):
        sec, usec, incl, _orig = struct.unpack_from("<IIII", b, pos)
        pos += 16
        p = b[pos:pos + incl]
        pos += incl
        if len(p) < 27:
            continue
        hlen, irp, status, func, info, bus, dev, ep, tr, dlen = struct.unpack_from("<HQIHBHHBBI", p, 0)
        t = sec + usec / 1e6
        t0 = t if t0 is None else t0
        yield {"t": t - t0, "dev": dev, "ep": ep, "tr": tr, "info": info, "status": status, "irp": irp,
               "data": p[hlen:], "len": dlen}


if __name__ == "__main__":
    maxb = int(sys.argv[2]) if len(sys.argv) > 2 else 64
    only_intr = "intr" in sys.argv[3:]
    for k in packets(sys.argv[1]):
        d = k["data"]
        if not d or (only_intr and k["tr"] != 1):
            continue
        if only_intr:
            d = d.rstrip(b"\0")
        arrow = "->" if k["ep"] < 0x80 else "<-"
        print(f"{k['t']:8.3f} {arrow} ep 0x{k['ep']:02X} {TRANSFERS.get(k['tr'], k['tr']):4s} {len(d):5d}B  "
              f"{d[:maxb].hex(' ')}{' ...' if len(d) > maxb else ''}")
