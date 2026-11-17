"""The DRAAI app icon, drawn as a PNG with the stdlib only.

Leaf module. Mirrors the sidebar's brand mark (`.mark` in
ui/css/20-chrome.css): a teal rounded square with a dark centre, glowing,
on a dark macOS-style tile. Served at /icon-<size>.png for the web app
manifest, so Chrome and Safari can install DRAAI with its own icon.
"""
import math
import struct
import zlib

SIZES = (192, 512)

_TILE = (13, 13, 18)
_INNER = (8, 8, 11)
_AC = (94, 234, 212)     # --ac  #5EEAD4
_AC2 = (45, 212, 191)    # --ac2 #2DD4BF

_cache = {}


def _box(px, py, cx, cy, half, r):
    """Signed distance from (px, py) to a rounded square."""
    qx = abs(px - cx) - (half - r)
    qy = abs(py - cy) - (half - r)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - r


def _cover(d):
    return 0.0 if d >= 0.5 else 1.0 if d <= -0.5 else 0.5 - d


def _over(dst, rgb, a):
    """Composite rgb at alpha a over a premultiplied-free RGBA pixel."""
    if a <= 0.0:
        return dst
    r, g, b, da = dst
    oa = a + da * (1.0 - a)
    if oa <= 0.0:
        return (0.0, 0.0, 0.0, 0.0)
    k = da * (1.0 - a)
    return ((rgb[0] * a + r * k) / oa, (rgb[1] * a + g * k) / oa,
            (rgb[2] * a + b * k) / oa, oa)


def _render(size):
    s = float(size)
    c = s / 2.0
    margin = s * 100.0 / 1024.0               # macOS icon grid
    tile_half = (s - 2.0 * margin) / 2.0
    tile_r = tile_half * 2.0 * 0.225
    mark = s * 0.46
    mark_half = mark / 2.0
    mark_r = mark * 11.0 / 36.0               # same proportions as .mark
    inner_half = mark_half - mark * 10.0 / 36.0
    inner_r = mark * 5.0 / 36.0
    glow = s * 0.07

    rows = []
    for y in range(size):
        row = bytearray([0])                  # PNG filter: none
        py = y + 0.5
        for x in range(size):
            px = x + 0.5
            pix = (0.0, 0.0, 0.0, 0.0)
            pix = _over(pix, _TILE, _cover(_box(px, py, c, c, tile_half, tile_r)))
            dm = _box(px, py, c, c, mark_half, mark_r)
            if dm > 0.0 and pix[3] > 0.0:
                pix = _over(pix, _AC, 0.45 * math.exp(-(dm / glow) ** 2))
            cov = _cover(dm)
            if cov > 0.0:
                t = min(max(((px - c) + (py - c)) / (4.0 * mark_half) + 0.5, 0.0), 1.0)
                ac = tuple(_AC[i] + (_AC2[i] - _AC[i]) * t for i in range(3))
                pix = _over(pix, ac, cov)
                pix = _over(pix, _INNER, _cover(_box(px, py, c, c, inner_half, inner_r)))
            row += bytes((int(pix[0] + 0.5), int(pix[1] + 0.5),
                          int(pix[2] + 0.5), int(pix[3] * 255.0 + 0.5)))
        rows.append(bytes(row))
    return _png(size, size, b"".join(rows))


def _png(w, h, raw):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)   # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def app_icon_png(size):
    """PNG bytes of the app icon at one of SIZES (rendered once, then cached)."""
    if size not in SIZES:
        raise ValueError("no icon at %r px" % (size,))
    if size not in _cache:
        _cache[size] = _render(size)
    return _cache[size]


def manifest():
    """The web app manifest: lets Chrome offer 'Install DRAAI'."""
    return {
        "name": "DRAAI",
        "short_name": "DRAAI",
        "description": "Your music on your speakers.",
        "id": "/",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#08080b",
        "theme_color": "#08080b",
        "icons": [{"src": "/icon-%d.png" % n, "sizes": "%dx%d" % (n, n),
                   "type": "image/png", "purpose": "any"} for n in SIZES],
    }
