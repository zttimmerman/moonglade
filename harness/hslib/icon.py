"""App icon generator (openspec/changes/launcher-app, task 1.1): Zach's "zW+" gold-ring logo → a round icon on a
transparent background, as `branding/AppIcon.png` (1024×1024 RGBA) and `branding/AppIcon.icns` (16-1024 px incl. @2x).

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
6. Layout follows Apple's macOS icon grid: the round icon is ICON_DIAMETER (824) px across on the 1024 canvas,
   centred. `sips` resamples the whole source by the factor that maps the cut circle to that diameter.

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
from pathlib import Path

from . import paths as P

SOURCE = P.ENV / "branding" / "logo-source.jpeg"
BRANDING = P.ROOT / "branding"
PNG_OUT = BRANDING / "AppIcon.png"
ICNS_OUT = BRANDING / "AppIcon.icns"
META_OUT = BRANDING / "AppIcon.json"

CANVAS = 1024
ICON_DIAMETER = 824            # Apple macOS icon grid: 824×824 body on a 1024 canvas
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
    d = bytes(255 - min(rgb[i], rgb[i + 1], rgb[i + 2]) for i in range(0, len(rgb), 3))

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


# ---------------------------------------------------------------- generator

def _sha256(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


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
        r_cut = ring["r"] - INSET_SRC_PX
        n = round(sw * (ICON_DIAMETER / 2) / r_cut)          # resampled size: cut circle → ICON_DIAMETER
        s = n / sw
        _sips("-s", "format", "bmp", "-z", n, n, source, "--out", tmp / "scaled.bmp")
        nw, nh, srgb = read_bmp(tmp / "scaled.bmp")
        if (nw, nh) != (n, n):
            raise IconError(f"sips resampled to {nw}x{nh}, expected {n}x{n}")
        # continuous coordinates (pixel i spans [i, i+1)); source edge points are pixel-centre coordinates
        ccx, ccy = (ring["cx"] + 0.5) * s, (ring["cy"] + 0.5) * s
        ox, oy = round(CANVAS / 2 - ccx), round(CANVAS / 2 - ccy)
        cx, cy, R = ccx + ox, ccy + oy, r_cut * s
        out = bytearray(CANVAS * CANVAS * 4)
        for y in range(CANVAS):
            dy = y + 0.5 - cy
            if abs(dy) >= R + 0.5:
                continue
            half = math.sqrt(max(0.0, (R + 0.5) ** 2 - dy * dy))
            x0, x1 = max(0, int(math.floor(cx - half))), min(CANVAS - 1, int(math.ceil(cx + half)))
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
                i, j = (y * CANVAS + x) * 4, (sy * nw + sx) * 3
                out[i:i + 3] = srgb[j:j + 3]
                out[i + 3] = 255 if a >= 1 else round(a * 255)
        png = out_dir / PNG_OUT.name
        write_png(png, CANVAS, CANVAS, bytes(out))

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
        meta = {"source": str(source.relative_to(P.ROOT)) if source.is_relative_to(P.ROOT) else str(source),
                "source_sha256": _sha256(source), "source_size": [sw, sh],
                "ring_source_px": {"cx": round(ring["cx"], 3), "cy": round(ring["cy"], 3), "r": round(ring["r"], 3),
                                   "fit_rounds": [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in rd.items()}
                                                  for rd in ring["rounds"]]},
                "cut_radius_source_px": round(r_cut, 3), "resampled_size": n,
                "canvas": CANVAS, "icon_circle_canvas_px": {"cx": round(cx, 3), "cy": round(cy, 3), "r": round(R, 3)},
                "params": {"EDGE_DEFICIT": EDGE_DEFICIT, "RAYS": RAYS, "RAY_STEP": RAY_STEP, "RUN": RUN,
                           "OUTLIER_PX": OUTLIER_PX, "INSET_SRC_PX": INSET_SRC_PX, "ICON_DIAMETER": ICON_DIAMETER},
                "png_sha256": _sha256(png), "icns_sha256": _sha256(icns)}
        (out_dir / META_OUT.name).write_text(json.dumps(meta, indent=2) + "\n")
        return meta
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
