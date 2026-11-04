"""Draw the Yea-Line logo: a blue rounded square with a white Y and signal arcs.
Every icon size is rendered separately from a big master (bolder shapes at small sizes) so none of them is blurry.
Writes yealine_logo.png (512), yealine.ico (16-256 px, hand-tuned small sizes), yealine_logo.svg and header PNGs."""
import math
from PIL import Image, ImageDraw

TOP, BOT, WHITE = (59, 130, 246), (29, 78, 216), (255, 255, 255)
RADIUS = 230
Y = [((250, 300), (420, 560)), ((590, 300), (420, 560)), ((420, 560), (420, 790))]
ARC_C = (640, 300)

# (y stroke, arc radii, arc width, arc span) per size class; small icons get thicker, simpler shapes
FULL = dict(ys=120, radii=(110, 190, 270), aw=50, span=50)
MID = dict(ys=140, radii=(120, 230), aw=66, span=48)
TINY = dict(ys=170, radii=(150,), aw=90, span=46)


def variant(size):
    return TINY if size <= 24 else MID if size <= 40 else FULL


def render(size, master=None):
    v = variant(size)
    S = max(size * 8, 1024)   # supersample, then shrink
    K = S / 1024
    bg = Image.new("RGB", (S, S))
    bd = ImageDraw.Draw(bg)
    for row in range(S):
        t = row / (S - 1)
        bd.line([(0, row), (S, row)], fill=tuple(int(TOP[i] + (BOT[i] - TOP[i]) * t) for i in range(3)))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(RADIUS * K), fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(bg, (0, 0), mask)
    d = ImageDraw.Draw(img)

    def line(p, q, w):
        d.line([(p[0] * K, p[1] * K), (q[0] * K, q[1] * K)], fill=WHITE, width=int(w * K))
        for x, y in (p, q):
            r = w * K / 2
            d.ellipse([x * K - r, y * K - r, x * K + r, y * K + r], fill=WHITE)

    for p, q in Y:
        line(p, q, v["ys"])
    cx, cy = ARC_C
    for r in v["radii"]:
        ro = r + v["aw"] / 2
        d.arc([(cx - ro) * K, (cy - ro) * K, (cx + ro) * K, (cy + ro) * K], -v["span"], v["span"], fill=WHITE, width=int(v["aw"] * K))
        for sign in (-1, 1):
            a = math.radians(sign * v["span"])
            x, y = (cx + r * math.cos(a)) * K, (cy + r * math.sin(a)) * K
            e = v["aw"] * K / 2
            d.ellipse([x - e, y - e, x + e, y + e], fill=WHITE)
    return img.resize((size, size), Image.LANCZOS)


sizes = [16, 20, 24, 32, 40, 48, 64, 96, 128, 256]
render(512).save("yealine_logo.png")
imgs = [render(s) for s in sizes]
imgs[-1].save("yealine.ico", format="ICO", sizes=[(s, s) for s in sizes], append_images=imgs[:-1])
for n in (28, 35, 42, 49, 56, 70, 84):   # header logo at 100%..300% display scaling
    render(n).save(f"yealine_hdr_{n}.png")
for n in (32, 48, 64, 128, 256):          # window / taskbar icons
    render(n).save(f"yealine_icon_{n}.png")

# SVG with the full-size geometry
v = FULL
arcs = ""
for r in v["radii"]:
    cx, cy = ARC_C
    a = math.radians(v["span"])
    arcs += f'  <path d="M{cx + r * math.cos(a):.1f} {cy - r * math.sin(a):.1f} A{r} {r} 0 0 1 {cx + r * math.cos(a):.1f} {cy + r * math.sin(a):.1f}"/>\n'
ylines = "".join(f'  <line x1="{p[0]}" y1="{p[1]}" x2="{q[0]}" y2="{q[1]}"/>\n' for p, q in Y)
open("yealine_logo.svg", "w", encoding="utf-8").write(f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="512" height="512">
  <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#3b82f6"/><stop offset="1" stop-color="#1d4ed8"/></linearGradient></defs>
  <rect width="1024" height="1024" rx="{RADIUS}" fill="url(#g)"/>
  <g fill="none" stroke="#fff" stroke-linecap="round">
    <g stroke-width="{v['ys']}">
{ylines}    </g>
    <g stroke-width="{v['aw']}">
{arcs}    </g>
  </g>
</svg>
''')
print("ok", [im.size for im in imgs])
