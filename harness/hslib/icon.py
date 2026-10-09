"""App icon generator (openspec/changes/launcher-app, task 1.1; navy tile: openspec/changes/release-signing, task 3.1):
Zach's "zW+" gold-ring logo on a dark navy macOS rounded-square tile, as `branding/AppIcon.png` (1024×1024 RGBA) and
`branding/AppIcon.icns` (16-1024 px incl. @2x), with the measurements and parameters in `branding/AppIcon.json`.

Source: `env/branding/logo-source.jpeg` (gitignored; 2048×2048 JPEG, white surround, no alpha). Stdlib Python plus
macOS `sips` (JPEG decode and resampling) and `iconutil` (icns packing). Run with `harness/hs icon`.

How the ring is found (nothing about its position or size is hardcoded):
1. `sips` decodes the JPEG to an uncompressed 24-bit BMP, which is parsed here.
2. Each pixel gets a "deficit" = 255 - min(R, G, B): ~0 for the white surround (and its JPEG noise), ~180-200 for
   the gold ring (its blue channel is low). EDGE_DEFICIT (96, about half-way) separates the two.
3. RAYS rays are cast from a provisional centre (first the image centre) *inwards*, starting just inside the image
   border, sampling the deficit with bilinear interpolation every RAY_STEP px. The ring's outer edge on a ray is the
   first point where the deficit crosses EDGE_DEFICIT and stays above it for the next RUN samples (so a stray speck
   in the surround can't stop the ray); the crossing is placed with linear interpolation (sub-pixel). Rays that start
   on the logo (it touches the border there) are dropped. Walking inwards from the outside means the artwork inside
   the ring (globe, letters) can never be mistaken for the edge.
4. A least-squares (Kåsa) circle fit through the edge points gives centre and radius; points more than
   OUTLIER_PX from the fitted circle are dropped and the fit repeated (twice), then the rays are recast from the
   fitted centre and fitted once more. The fit's RMS residual and point count are recorded in AppIcon.json.
5. The cut circle is the fitted edge moved INSET_SRC_PX inwards: the JPEG's anti-aliased edge (white blending into
   gold over ~4 source px) falls outside it, so no white halo survives. Alpha is the exact coverage of each output
   pixel by that circle (a 1 px analytic ramp), which gives a clean anti-aliased edge; fully transparent pixels
   are RGB 0.
6. `sips` resamples the whole source once, by the factor that maps the cut circle to the ring's size on the tile
   (`logo_diameter()`), and the ring is centred on the 1024 canvas.

The tile (render_tile; the "Approved navy tile" in release-signing design.md, recovered from the mockup Zach picked
on 2026-10-08, with the outline Zach picked on 2026-10-09, openspec change icon-corner-c, option C): macOS 26's
app-icon shape, a TILE_BODY = 832 px square centred on the 1024 canvas with continuous-curvature corners of radius
TILE_RADIUS = 216 px (continuous_inset; rasterised by corner_mask), a vertical navy gradient with a soft centred glow,
a drop shadow, and the ring scaled to int(LOGO_REF_BODY × LOGO_SCALE) / CANVAS of its borderless size (the mockup
scaled the whole 824-on-1024 borderless icon to that many px; the logo kept that size when the outline changed).
Blurs follow Pillow's GaussianBlur (three extended box passes) in floats.

Deterministic: same source + same macOS tools → identical pixels (the PNG is written with filter 0 and zlib level 9,
no timestamps or metadata). `app.icon` re-runs this into a temp dir and compares the decoded pixels.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import struct
import subprocess
import tempfile
import zlib
from bisect import bisect_left
from itertools import accumulate
from pathlib import Path

from . import paths as P

SOURCE = P.ENV / "branding" / "logo-source.jpeg"
BRANDING = P.ROOT / "branding"
PNG_OUT = BRANDING / "AppIcon.png"
ICNS_OUT = BRANDING / "AppIcon.icns"
META_OUT = BRANDING / "AppIcon.json"

CANVAS = 1024
ICON_DIAMETER = 824            # Apple macOS icon grid: 824×824 body on a 1024 canvas (the borderless ring's size)
# approved navy tile (release-signing design.md "Approved navy tile"); per CANVAS unless noted
# outline: macOS 26's app-icon shape, option C (icon-corner-c): matplotlib issue 31895 (comment by iccir) measured the
# system mask at 208 px with corner radius 54 on a 256 px canvas = 832 / 216 on 1024 (release-signing design.md 6)
TILE_SHAPE = "continuous"      # "continuous" (continuous_inset) or "circular" (circular arcs; counterexamples only)
TILE_BODY = 832                # tile square, centred
TILE_RADIUS = 216.0            # corner radius (the cornerRadius Core Animation would be given)
LOGO_REF_BODY = 824            # the logo's size reference: the approved mockup's tile body (logo unchanged by option C)
TILE_TOP_RGB = (28, 38, 62)    # gradient at t = y/canvas = 0 ...
TILE_BOTTOM_RGB = (18, 24, 38)  # ... and at t = 1
GLOW_BOX = (0.2, 0.2, 0.8, 0.8)  # glow disc's bounding box, fractions of the canvas
GLOW_RGBA = (90, 120, 170, 110)
GLOW_BLUR_FRAC = 0.08          # Gaussian sigma, fraction of the canvas
SHADOW_BLUR_512 = 10.0         # shadow: tile mask blurred this many px per 512 px of canvas ...
SHADOW_ALPHA = 0.35            # ... times this alpha ...
SHADOW_DY_512 = 6              # ... moved this many px down per 512 px of canvas
LOGO_SCALE = 0.86              # the borderless icon was scaled to int(body × this) px on the tile
EDGE_DEFICIT = 96.0            # 255-min(RGB): white surround ≈ 0-10, gold ring ≈ 180-200
RAYS = 720
RAY_STEP = 0.5
RUN = 6                        # samples (3 px) that must stay above the threshold
OUTLIER_PX = 4.0             # the drawn ring is out of round by about ±2.3 px; a pale highlight on the rim (top, ~-130°..-95°) reads ~20 px inside and is dropped
INSET_SRC_PX = 8.0             # source px: the ~±2 px anti-aliased JPEG edge plus the ring's ≤3.9 px out-of-roundness
SIPS = "/usr/bin/sips"
ICONUTIL = "/usr/bin/iconutil"

# iconset name → pixel size (Apple's standard set, 16-512 pt at 1x and 2x)
ICONSET = {f"icon_{pt}x{pt}{suf}.png": pt * k for pt in (16, 32, 128, 256, 512)
           for suf, k in (("", 1), ("@2x", 2))}


class IconError(Exception):
    pass


# ---------------------------------------------------------------- image I/O (stdlib)

def read_bmp(path: Path) -> tuple[int, int, bytes]:
    """(w, h, RGB bytes top-down) from an uncompressed 24/32-bit BMP (what `sips -s format bmp` writes)."""
    b = Path(path).read_bytes()
    if b[:2] != b"BM":
        raise IconError(f"{path}: not a BMP")
    off = struct.unpack_from("<I", b, 10)[0]
    _, w, h, _, bpp, comp = struct.unpack_from("<IiiHHI", b, 14)
    if bpp not in (24, 32) or comp not in (0, 3):
        raise IconError(f"{path}: unsupported BMP ({bpp} bpp, compression {comp})")
    bypp, stride = bpp // 8, (w * (bpp // 8) + 3) & ~3
    out = bytearray(w * abs(h) * 3)
    for y in range(abs(h)):
        row = (abs(h) - 1 - y) if h > 0 else y
        src = b[off + row * stride: off + row * stride + w * bypp]
        dst = y * w * 3
        out[dst + 0: dst + w * 3: 3] = src[2::bypp]
        out[dst + 1: dst + w * 3: 3] = src[1::bypp]
        out[dst + 2: dst + w * 3: 3] = src[0::bypp]
    return w, abs(h), bytes(out)


def write_png(path: Path, w: int, h: int, rgba: bytes) -> None:
    """8-bit RGBA PNG, filter 0 on every row, zlib level 9, no ancillary chunks (byte-stable)."""
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def png_info(data: bytes) -> dict:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise IconError("not a PNG")
    w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", data[16:29])
    return {"w": w, "h": h, "depth": depth, "color_type": ctype, "interlace": interlace}


def read_png(data: bytes) -> tuple[int, int, bytes]:
    """(w, h, RGBA8 bytes) from a non-interlaced 8-bit RGB/RGBA PNG (all five filter types)."""
    info = png_info(data)
    w, h = info["w"], info["h"]
    if info["depth"] != 8 or info["color_type"] not in (2, 6) or info["interlace"]:
        raise IconError(f"unsupported PNG {info}")
    pos, idat = 8, bytearray()
    while pos < len(data):
        n = struct.unpack(">I", data[pos:pos + 4])[0]
        t = data[pos + 4:pos + 8]
        if t == b"IDAT":
            idat += data[pos + 8:pos + 8 + n]
        pos += 12 + n
    raw = zlib.decompress(bytes(idat))
    bpp = 4 if info["color_type"] == 6 else 3
    stride = w * bpp
    out = bytearray(h * stride)
    prev = bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                line[i] = (line[i] + ((line[i - bpp] if i >= bpp else 0) + prev[i]) // 2) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        elif f != 0:
            raise IconError(f"bad PNG filter {f}")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    if bpp == 3:
        rgba = bytearray(w * h * 4)
        rgba[0::4], rgba[1::4], rgba[2::4] = out[0::3], out[1::3], out[2::3]
        rgba[3::4] = b"\xff" * (w * h)
        out = rgba
    return w, h, bytes(out)


def icns_entries(data: bytes) -> dict[str, bytes]:
    """{OSType: payload} of an .icns file (top-level elements)."""
    if data[:4] != b"icns" or struct.unpack(">I", data[4:8])[0] != len(data):
        raise IconError("not an icns file (bad magic or length)")
    out, pos = {}, 8
    while pos + 8 <= len(data):
        t, n = data[pos:pos + 4].decode("latin-1"), struct.unpack(">I", data[pos + 4:pos + 8])[0]
        if n < 8 or pos + n > len(data):
            raise IconError(f"icns element {t!r} has a bad length")
        out[t] = data[pos + 8:pos + n]
        pos += n
    return out


def _sips(*args) -> None:
    r = subprocess.run([SIPS, *map(str, args)], capture_output=True, text=True)
    if r.returncode != 0:
        raise IconError(f"sips {' '.join(map(str, args))} failed: {r.stderr.strip() or r.stdout.strip()}")


# ---------------------------------------------------------------- ring detection

def _deficit_sampler(w: int, h: int, rgb: bytes):
    d = bytes(map(min, rgb[0::3], rgb[1::3], rgb[2::3])).translate(bytes(range(255, -1, -1)))  # 255 - min(R, G, B)

    def at(x: float, y: float) -> float:
        x0, y0 = int(math.floor(x)), int(math.floor(y))
        if x0 < 0 or y0 < 0 or x0 + 1 >= w or y0 + 1 >= h:
            return 0.0
        fx, fy = x - x0, y - y0
        i = y0 * w + x0
        return ((d[i] * (1 - fx) + d[i + 1] * fx) * (1 - fy) + (d[i + w] * (1 - fx) + d[i + w + 1] * fx) * fy)
    return at


def _cast(at, w: int, h: int, cx: float, cy: float) -> list[tuple[float, float]]:
    """Outer-edge points: one per ray, walking inwards from just inside the image border."""
    pts = []
    for k in range(RAYS):
        a = 2 * math.pi * k / RAYS
        dx, dy = math.cos(a), math.sin(a)
        # start radius: just inside the border along this direction
        lim = []
        if dx > 1e-9: lim.append((w - 2 - cx) / dx)
        if dx < -1e-9: lim.append((1 - cx) / dx)
        if dy > 1e-9: lim.append((h - 2 - cy) / dy)
        if dy < -1e-9: lim.append((1 - cy) / dy)
        r = min(lim)
        if at(cx + dx * r, cy + dy * r) >= EDGE_DEFICIT:
            continue                                   # the logo touches the border on this ray
        prev_r, prev_v = r, at(cx + dx * r, cy + dy * r)
        while r > 0:
            r -= RAY_STEP
            v = at(cx + dx * r, cy + dy * r)
            if v >= EDGE_DEFICIT and all(at(cx + dx * (r - j * RAY_STEP), cy + dy * (r - j * RAY_STEP)) >= EDGE_DEFICIT
                                         for j in range(1, RUN + 1)):
                t = (EDGE_DEFICIT - prev_v) / (v - prev_v) if v != prev_v else 0.0
                re = prev_r - t * RAY_STEP
                pts.append((cx + dx * re, cy + dy * re))
                break
            prev_r, prev_v = r, v
    return pts


def _fit(pts: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Kåsa algebraic circle fit: least squares for x²+y² + D x + E y + F = 0."""
    sxx = sxy = syy = sx = sy = sxz = syz = sz = 0.0
    n = len(pts)
    for x, y in pts:
        z = x * x + y * y
        sxx += x * x; sxy += x * y; syy += y * y; sx += x; sy += y
        sxz += x * z; syz += y * z; sz += z
    m = [[sxx, sxy, sx, -sxz], [sxy, syy, sy, -syz], [sx, sy, n, -sz]]
    for c in range(3):                                   # Gauss-Jordan with partial pivoting
        p = max(range(c, 3), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        for r in range(3):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [m[r][i] - f * m[c][i] for i in range(4)]
    D, E, F = (m[i][3] / m[i][i] for i in range(3))
    cx, cy = -D / 2, -E / 2
    return cx, cy, math.sqrt(cx * cx + cy * cy - F)


def detect_ring(w: int, h: int, rgb: bytes) -> dict:
    """Centre and outer radius of the ring (pixel-centre coordinates of the source), with fit statistics."""
    at = _deficit_sampler(w, h, rgb)
    cx, cy = (w - 1) / 2, (h - 1) / 2
    rounds = []
    for _ in range(2):                                   # cast from the image centre, then from the fitted centre
        pts = _cast(at, w, h, cx, cy)
        if len(pts) < RAYS // 4:
            raise IconError(f"only {len(pts)} of {RAYS} rays found the ring's edge; is this the logo?")
        keep = pts
        for _ in range(3):
            cx, cy, r = _fit(keep)
            keep = [p for p in pts if abs(math.hypot(p[0] - cx, p[1] - cy) - r) <= OUTLIER_PX]
        cx, cy, r = _fit(keep)
        res = [math.hypot(p[0] - cx, p[1] - cy) - r for p in keep]
        rounds.append({"rays_hit": len(pts), "points_kept": len(keep), "cx": cx, "cy": cy, "r": r,
                       "rms_px": math.sqrt(sum(e * e for e in res) / len(res)),
                       "max_abs_px": max(abs(e) for e in res)})
    return {"cx": cx, "cy": cy, "r": r, "rounds": rounds}



# ---------------------------------------------------------------- tile stage (release-signing, design 6)

def _box_params(sigma: float, passes: int) -> tuple[int, float]:
    """Pillow's _gaussian_blur_radius: integer box radius l plus the fractional edge weight a."""
    s2 = sigma * sigma / passes
    L = math.sqrt(12.0 * s2 + 1.0)
    l = int(math.floor((L - 1.0) / 2.0))
    a = (2 * l + 1) * (l * (l + 1) - 3 * s2) / (6 * (s2 - (l + 1) * (l + 1)))
    return l, a


def blur_reach(sigma: float, passes: int = 3) -> int:
    """How far (px) `box_blur` can spread a value: passes × (l + 1)."""
    l, _ = _box_params(sigma, passes)
    return passes * (l + 1)


def box_blur(plane: list[float], w: int, h: int, sigma: float, passes: int = 3) -> list[float]:
    """Gaussian blur approximated the way Pillow's GaussianBlur(radius=sigma) does it: `passes` extended box blurs
    (box radius l plus a fractional edge weight a) along rows, then the same along columns; edges extend the border
    pixel. Floats in, floats out (no per-pass rounding, so not bit-identical to Pillow)."""
    l, a = _box_params(sigma, passes)
    norm = 1.0 / (2 * (l + a) + 1)
    pad = l + 2

    def line(x: list[float]) -> list[float]:
        n = len(x)
        for _ in range(passes):
            ext = [x[0]] * pad + x + [x[-1]] * pad
            ps = [0.0, *accumulate(ext)]                     # ps[k] = sum(ext[:k])
            x = [(ps[i + pad + l + 1] - ps[i + pad - l] + a * (ext[i + pad - l - 1] + ext[i + pad + l + 1])) * norm
                 for i in range(n)]
        return x

    rows = [line(plane[y * w:(y + 1) * w]) for y in range(h)]
    cols = [line([rows[y][x] for y in range(h)]) for x in range(w)]
    return [cols[x][y] for y in range(h) for x in range(w)]


# Continuous-curvature corner ("squircle" corner) of a rounded square, in units of the corner radius r, for the
# top-right corner: (u, v) = (distance in from the vertical side, distance down from the top side); the corner runs
# from (EXT, 0) on the top side to (0, EXT) on the vertical side as three cubic Béziers, and r is first limited to
# body / 2 / EXT. Source: the published reverse engineering of Apple's continuous rounded rectangle, "Drawing Code for
# iOS 7 Rounded Rectangles" (PaintCode/PixelCut, 2013-11-18, https://www.paintcodeapp.com/news/code-for-ios-7-rounded-
# rectangles): EXT = 1.52866483, the limit, every control point below and the middle curve. One difference, read from
# Core Animation itself: on macOS 26 a CALayer with cornerCurve = continuous, rendered into a PDF context
# (renderInContext), emits exactly these curves except that the first and last curves end at the middle curve's ends,
# (0.63149399, 0.07491100) and its mirror, where the 2013 code ends them at (0.66993427, 0.06549600) and adds a short
# line (icon-corner-c design "As built"). The constants below are Core Animation's; app.icon compares the raster with
# a Core Animation rendering.
CONTINUOUS_EXT = 1.52866483
CONTINUOUS_CORNER = (
    ((1.52866483, 0.0), (1.08849323, 0.0), (0.86840689, 0.0), (0.63149399, 0.07491100)),
    ((0.63149399, 0.07491100), (0.37282392, 0.16905899), (0.16906013, 0.37282401), (0.07491100, 0.63149399)),
    ((0.07491100, 0.63149399), (0.0, 0.86840689), (0.0, 1.08849323), (0.0, 1.52866483)),
)
CORNER_STEPS = 1024            # points per cubic when the corner is flattened (chord error < 1e-5 r)
MASK_SUBROWS = 16              # vertical samples per pixel row; coverage along each sample row is exact


def _continuous_profile() -> tuple[list[float], list[float]]:
    """The corner flattened to a polyline: (v, u) lists in units of r, v increasing (every curve is monotone)."""
    vs, us = [], []
    for p0, p1, p2, p3 in CONTINUOUS_CORNER:
        for i in range(0 if not vs else 1, CORNER_STEPS + 1):
            t = i / CORNER_STEPS
            s = 1 - t
            a, b, c, d = s * s * s, 3 * s * s * t, 3 * s * t * t, t * t * t
            vs.append(a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])
            us.append(a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0])
    return vs, us


_PROFILE = _continuous_profile()


def corner_inset(shape: str, size: float, radius: float):
    """f(v) -> how far the outline is in from the vertical side at depth v (px) below the horizontal side, for a
    rounded square of side `size` whose corners are `shape` ("continuous" or "circular") with radius `radius`; 0 on
    the straight parts. Depths beyond size/2 mirror (the caller folds them)."""
    if shape == "circular":
        r = min(radius, size / 2)

        def circ(v: float) -> float:
            if v >= r:
                return 0.0
            w = r - v
            return r - math.sqrt(max(0.0, r * r - w * w))
        return circ
    if shape != "continuous":
        raise IconError(f"unknown corner shape {shape!r}")
    r = min(radius, size / 2 / CONTINUOUS_EXT)
    vs, us = _PROFILE
    reach = vs[-1] * r

    def cont(v: float) -> float:
        if v >= reach:
            return 0.0
        q = v / r
        i = bisect_left(vs, q)
        if i == 0:
            return us[0] * r
        v0, v1 = vs[i - 1], vs[i]
        f = (q - v0) / (v1 - v0) if v1 > v0 else 0.0
        return (us[i - 1] + (us[i] - us[i - 1]) * f) * r
    return cont


def corner_mask(n: int, x0: float, y0: float, size: float, radius: float, shape: str = TILE_SHAPE) -> list[float]:
    """Coverage (0..1) of each pixel of an n×n canvas by a rounded square (x0, y0, size) with `shape` corners.
    Pixel i spans [i, i+1). Each row is sampled at MASK_SUBROWS evenly spaced heights; on each sample row the shape is
    one interval, whose exact overlap with each pixel is added. Pure Python floats, so the same on every macOS."""
    inset = corner_inset(shape, size, radius)
    x1, y1 = x0 + size, y0 + size
    out = [0.0] * (n * n)
    sub = MASK_SUBROWS
    for y in range(max(0, int(math.floor(y0))), min(n, int(math.ceil(y1)))):
        spans = []
        for k in range(sub):
            yy = y + (k + 0.5) / sub
            if y0 <= yy <= y1:
                d = inset(min(yy - y0, y1 - yy))
                a, b = max(0.0, x0 + d), min(float(n), x1 - d)
                if b > a:
                    spans.append((a, b))
        if not spans:
            continue
        lo = int(math.floor(min(a for a, _ in spans)))
        hi = min(n, int(math.ceil(max(b for _, b in spans))))
        full_lo = int(math.ceil(max(a for a, _ in spans))) if len(spans) == sub else hi
        full_hi = int(math.floor(min(b for _, b in spans))) if len(spans) == sub else hi
        row = [0.0] * (hi - lo)
        for x in range(lo, hi):
            if full_lo <= x < full_hi:
                row[x - lo] = 1.0
                continue
            c = 0.0
            for a, b in spans:
                o = min(b, x + 1.0) - max(a, float(x))
                if o > 0:
                    c += o
            row[x - lo] = c / sub
        out[y * n + lo:y * n + hi] = row
    return out


def tile_geometry(n: int = CANVAS) -> dict:
    """The tile on an n×n canvas: its shape, body (TILE_BODY on CANVAS, centred) and corner radius."""
    k = n / CANVAS
    body = TILE_BODY * k
    return {"canvas": n, "shape": TILE_SHAPE, "body": body, "x0": (n - body) / 2, "y0": (n - body) / 2,
            "corner_radius": TILE_RADIUS * k}


def tile_mask(n: int = CANVAS) -> list[float]:
    """The tile's coverage on an n×n canvas (corner_mask with tile_geometry)."""
    g = tile_geometry(n)
    return corner_mask(n, g["x0"], g["y0"], g["body"], g["corner_radius"], g["shape"])


def logo_diameter(n: int = CANVAS) -> float:
    """The ring's diameter on the tile. The mockup scaled the whole borderless icon (ring ICON_DIAMETER px across a
    CANVAS-px canvas) to int(LOGO_REF_BODY × LOGO_SCALE) px; the ring is cut straight to that size here (one
    resample). Option C kept the logo at that size (Zach's preview), so it doesn't follow TILE_BODY."""
    return ICON_DIAMETER * int(n * LOGO_REF_BODY / CANVAS * LOGO_SCALE) / CANVAS


def render_tile(logo: bytes, n: int = CANVAS, mask: list[float] | None = None) -> bytes:
    """The approved navy tile (release-signing design.md "Approved navy tile") on an n×n canvas with `logo` (n×n
    RGBA, already at its final size and place) on top. `mask` overrides the tile's coverage (to preview other corner
    shapes); the shadow is cast by whichever mask is used. Returns n×n RGBA, straight alpha; alpha-0 pixels are RGB 0.

    Layers, bottom to top: shadow (black, SHADOW_ALPHA × the mask moved SHADOW_DY down and blurred SHADOW_BLUR),
    tile (vertical gradient TILE_TOP_RGB → TILE_BOTTOM_RGB, t = y/n, with a glow: GLOW_RGBA's colour at up to its
    alpha over a centred disc of GLOW_BOX blurred GLOW_BLUR_FRAC × n), logo. The mockup's shadow numbers are per
    512 px and scale with n."""
    k = n / 512
    if mask is None:
        mask = tile_mask(n)
    dy = round(SHADOW_DY_512 * k)
    shadow = box_blur([0.0] * (dy * n) + mask[:n * n - dy * n], n, n, SHADOW_BLUR_512 * k)
    gr, gc = (GLOW_BOX[2] - GLOW_BOX[0]) * n / 2, n / 2
    glow = box_blur([1.0 if math.hypot(i % n + 0.5 - gc, i // n + 0.5 - gc) <= gr else 0.0 for i in range(n * n)],
                    n, n, GLOW_BLUR_FRAC * n)
    ga_max = GLOW_RGBA[3] / 255
    out = bytearray(n * n * 4)
    for y in range(n):
        t = y / n
        base = [TILE_TOP_RGB[c] + (TILE_BOTTOM_RGB[c] - TILE_TOP_RGB[c]) * t for c in range(3)]
        for x in range(n):
            i = y * n + x
            tm, sa = mask[i], SHADOW_ALPHA * shadow[i]
            p = i * 4
            la = logo[p + 3] / 255
            A = tm + sa * (1 - tm)                           # tile over shadow
            A2 = la + A * (1 - la)                           # logo over that
            a8 = min(255, round(A2 * 255))
            if a8 == 0:
                continue
            ga = ga_max * glow[i]
            for c in range(3):
                col = (GLOW_RGBA[c] * ga + base[c] * (1 - ga)) * tm / A if A > 0 else 0.0
                out[p + c] = min(255, max(0, round((logo[p + c] * la + col * A * (1 - la)) / A2)))
            out[p + 3] = a8
    return bytes(out)


# ---------------------------------------------------------------- generator

def _sha256(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def cut_logo(source: Path, tmp: Path, ring: dict, src_w: int, diameter: float, n: int = CANVAS) -> tuple[bytes, dict]:
    """The gold ring cut out of the source (cut circle = fitted edge - INSET_SRC_PX), resampled by `sips` so the cut
    circle is `diameter` px across, centred on an n×n transparent canvas. Returns (RGBA, geometry)."""
    r_cut = ring["r"] - INSET_SRC_PX
    m = round(src_w * (diameter / 2) / r_cut)                # resampled size: cut circle → diameter
    s = m / src_w
    scaled = tmp / f"scaled-{m}.bmp"
    _sips("-s", "format", "bmp", "-z", m, m, source, "--out", scaled)
    nw, nh, srgb = read_bmp(scaled)
    if (nw, nh) != (m, m):
        raise IconError(f"sips resampled to {nw}x{nh}, expected {m}x{m}")
    # continuous coordinates (pixel i spans [i, i+1)); source edge points are pixel-centre coordinates
    ccx, ccy = (ring["cx"] + 0.5) * s, (ring["cy"] + 0.5) * s
    ox, oy = round(n / 2 - ccx), round(n / 2 - ccy)
    cx, cy, R = ccx + ox, ccy + oy, r_cut * s
    out = bytearray(n * n * 4)
    for y in range(n):
        dy = y + 0.5 - cy
        if abs(dy) >= R + 0.5:
            continue
        half = math.sqrt(max(0.0, (R + 0.5) ** 2 - dy * dy))
        x0, x1 = max(0, int(math.floor(cx - half))), min(n - 1, int(math.ceil(cx + half)))
        sy = y - oy
        if not 0 <= sy < nh:
            continue
        for x in range(x0, x1 + 1):
            sx = x - ox
            if not 0 <= sx < nw:
                continue
            a = R - math.hypot(x + 0.5 - cx, dy) + 0.5
            if a <= 0:
                continue
            i, j = (y * n + x) * 4, (sy * nw + sx) * 3
            out[i:i + 3] = srgb[j:j + 3]
            out[i + 3] = 255 if a >= 1 else round(a * 255)
    return bytes(out), {"cut_radius_source_px": r_cut, "resampled_size": m, "cx": cx, "cy": cy, "r": R}


def load_logo(source: Path = SOURCE, diameter: float | None = None) -> bytes:
    """The cut ring at `diameter` (default: its size on the tile) on a CANVAS×CANVAS transparent canvas."""
    tmp = Path(tempfile.mkdtemp(prefix="hs-icon-"))
    try:
        _sips("-s", "format", "bmp", source, "--out", tmp / "src.bmp")
        sw, sh, rgb = read_bmp(tmp / "src.bmp")
        return cut_logo(source, tmp, detect_ring(sw, sh, rgb), sw, diameter or logo_diameter())[0]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def generate(source: Path = SOURCE, out_dir: Path | None = None) -> dict:
    """Write AppIcon.png, AppIcon.icns and AppIcon.json into out_dir (default: branding/). Returns the metadata."""
    source = Path(source)
    if not source.is_file():
        raise IconError(f"logo source not found: {source} (gitignored; copy Zach's logo there)")
    out_dir = Path(out_dir) if out_dir else BRANDING
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="hs-icon-"))
    try:
        _sips("-s", "format", "bmp", source, "--out", tmp / "src.bmp")
        sw, sh, rgb = read_bmp(tmp / "src.bmp")
        ring = detect_ring(sw, sh, rgb)
        logo, lg = cut_logo(source, tmp, ring, sw, logo_diameter())
        png = out_dir / PNG_OUT.name
        write_png(png, CANVAS, CANVAS, render_tile(logo))

        iconset = tmp / "AppIcon.iconset"
        iconset.mkdir()
        for name, px in ICONSET.items():
            if px == CANVAS:
                shutil.copyfile(png, iconset / name)
            else:
                _sips("-z", px, px, png, "--out", iconset / name)
        icns = out_dir / ICNS_OUT.name
        r = subprocess.run([ICONUTIL, "-c", "icns", str(iconset), "-o", str(icns)], capture_output=True, text=True)
        if r.returncode != 0:
            raise IconError(f"iconutil failed: {r.stderr.strip()}")
        src_sha = _sha256(source)
        geo = tile_geometry()
        sh_sigma = SHADOW_BLUR_512 * CANVAS / 512
        meta = {"source": str(source.relative_to(P.ROOT)) if source.is_relative_to(P.ROOT) else str(source),
                "source_sha256": src_sha, "source_size": [sw, sh],
                "ring_source_px": {"cx": round(ring["cx"], 3), "cy": round(ring["cy"], 3), "r": round(ring["r"], 3),
                                   "fit_rounds": [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in rd.items()}
                                                  for rd in ring["rounds"]]},
                "cut_radius_source_px": round(lg["cut_radius_source_px"], 3), "resampled_size": lg["resampled_size"],
                "canvas": CANVAS,
                "icon_circle_canvas_px": {"cx": round(lg["cx"], 3), "cy": round(lg["cy"], 3), "r": round(lg["r"], 3)},
                "tile": {"source_sha256": src_sha,
                         "shape": ("rounded square, continuous-curvature corners (Core Animation cornerCurve = "
                                   "continuous; macOS 26 app-icon shape, option C)" if geo["shape"] == "continuous"
                                   else f"rounded square, {geo['shape']} corners"),
                         "shape_kind": geo["shape"], "corner_curve_ext": CONTINUOUS_EXT,
                         "body_px": geo["body"], "x0_px": geo["x0"], "y0_px": geo["y0"],
                         "corner_radius_px": round(geo["corner_radius"], 3),
                         "corner_radius_frac": round(geo["corner_radius"] / geo["body"], 6),
                         "fill": {"kind": "vertical gradient, t = y / canvas", "top_rgb": list(TILE_TOP_RGB),
                                  "bottom_rgb": list(TILE_BOTTOM_RGB)},
                         "glow": {"disc_box_frac": list(GLOW_BOX), "rgba": list(GLOW_RGBA),
                                  "blur_sigma_px": round(GLOW_BLUR_FRAC * CANVAS, 3)},
                         "shadow": {"blur_sigma_px": sh_sigma, "alpha": SHADOW_ALPHA,
                                    "dy_px": round(SHADOW_DY_512 * CANVAS / 512),
                                    "reach_px": blur_reach(sh_sigma)},
                         "logo_scale": LOGO_SCALE, "logo_ref_body_px": LOGO_REF_BODY,
                         "logo_canvas_px": int(LOGO_REF_BODY * LOGO_SCALE),
                         "logo_diameter_px": round(logo_diameter(), 3)},
                "params": {"EDGE_DEFICIT": EDGE_DEFICIT, "RAYS": RAYS, "RAY_STEP": RAY_STEP, "RUN": RUN,
                           "OUTLIER_PX": OUTLIER_PX, "INSET_SRC_PX": INSET_SRC_PX, "ICON_DIAMETER": ICON_DIAMETER},
                "png_sha256": _sha256(png), "icns_sha256": _sha256(icns)}
        (out_dir / META_OUT.name).write_text(json.dumps(meta, indent=2) + "\n")
        return meta
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
