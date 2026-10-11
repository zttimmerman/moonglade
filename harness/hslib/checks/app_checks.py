"""mac-app: Moonglade.app (openspec/changes/launcher-app).

app.icon and app.bundle_valid inspect the committed icon and a fresh `harness/hs app` build. The runtime checks open
the built app the way Finder does, through LaunchServices (`/usr/bin/open`, never by exec'ing the binary), with the
play.* checks' stand-in client (`--account test --client-cmd <bat>`, passed via `open --args`) and the app's test
hook (`open --env HS_APP_TEST_HOOK=<file>`): the app then shows no window or dialog and records each status, dialog
and its exit code in that file. LaunchServices starts the app with launchd's environment, so the pinned profile
(HS_PROFILE=m0) goes through `open --env` too.

macOS privacy (TCC): the repo lives in ~/Documents, so the play loop the app starts needs the "Documents folder"
permission for Moonglade.app. macOS asks once ("Moonglade would like to access files in your Documents folder");
until someone clicks Allow, the loop is blocked and the runtime checks fail fast (within APP_OUTPUT_TIMEOUT) with that
diagnosis and the tccd log lines as evidence, instead of waiting out the startup timeout.
"""
from __future__ import annotations

import json
import math
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .. import app as A, icon as I, launch, paths as P, play, procs, stack
from ..registry import check, require
from .naming_checks import NEW_NAME, OLD_NAME
from .play_checks import (PINNED, START_TIMEOUT, StandIn, _denials, _wait_denials, graceful_halt, is_clear, last_record,
                          play_processes, remaining, SANDBOX_EXEC)

CAP = "mac-app"
# rename-moonglade: the spec's literal expectations, not app.py's constants, so a wrong constant there fails
EXPECT_APP_DIR = "Moonglade.app"
EXPECT_NAME = NEW_NAME
EXPECT_ID = "local.moonglade.app"
EXPECT_DR = 'designated => identifier "local.moonglade.app"'
NAMES_TIMEOUT = 60            # the shim names the game process at its start; 60 s after the client is up = it didn't
HS = str(P.HARNESS / "hs")
OPEN = "/usr/bin/open"
APP_OUTPUT_TIMEOUT = 90       # the play loop prints its first line within ~1-2 s; 90 s = blocked (e.g. TCC prompt)
DENIAL_LOG_WAIT_S = 60       # how long to wait for a sandbox denial to reach the unified log (normally 1-3 s)
EXIT_TIMEOUT = 300            # play's own limit after the client exits is 120 s (play.loop_clean_exit)
TCC_HINT = ("the play loop never started: macOS is probably asking for Moonglade's Documents-folder permission "
            "(the repo is in ~/Documents). Open the app once and click Allow; see tcc_prompts")


# ---------------------------------------------------------------- icon helpers

# icns OSType → pixel size (Apple's standard 16-512 pt set at 1x and 2x)
ICNS_TYPES = {"ic04": 16, "ic11": 32, "ic05": 32, "ic12": 64, "ic07": 128, "ic13": 256, "ic08": 256,
              "ic14": 512, "ic09": 512, "ic10": 1024}


def _argb_size(payload: bytes) -> int | None:
    """Pixel count of an 'ARGB' icns payload (four RLE-packed channels), None if malformed."""
    if payload[:4] != b"ARGB":
        return None
    out, i, d = 0, 4, payload
    while i < len(d):
        n = d[i]
        if n < 0x80:
            out, i = out + n + 1, i + n + 2
        else:
            out, i = out + n - 0x80 + 3, i + 2
    return out // 4 if out % 4 == 0 else None


def icns_sizes(data: bytes) -> dict:
    """{OSType: {"expected": px, "actual": (w, h) or None, "format": ...}} for every standard type."""
    entries = I.icns_entries(data)
    out = {}
    for t, px in ICNS_TYPES.items():
        p = entries.get(t)
        if p is None:
            out[t] = {"expected": px, "actual": None, "format": "missing"}
        elif p[:8] == b"\x89PNG\r\n\x1a\n":
            info = I.png_info(p)
            out[t] = {"expected": px, "actual": [info["w"], info["h"]], "format": "png"}
        else:
            n = _argb_size(p)
            side = math.isqrt(n) if n else 0
            out[t] = {"expected": px, "actual": [side, side] if n and side * side == n else None, "format": "argb"}
    return out


def _near_white(r: int, g: int, b: int) -> bool:
    return min(r, g, b) >= 215 and max(r, g, b) - min(r, g, b) <= 30


# release-signing: the navy tile. Every geometry number comes from AppIcon.json's `tile` record (body, position,
# corner radius, shape, gradient, shadow reach, logo diameter), never from this file, so switching the corner shape
# is a generator parameter change plus a regeneration. The check knows two corner kinds by the record's `shape`:
# "circular" (circular arcs, the approved mockup) is exact; "continuous" (Apple's continuous-curvature corners) is
# judged by an envelope: the shape lies inside the circular rounded square of the same radius (to within 1 px at
# the diagonal) and contains the circular one of 1.3 × the radius (its corners start curving ~1.21 × the radius
# from the body corner, design.md 6 "As built"). Samples sit TILE_INSET px inside the inner bound; "outside" means
# beyond the outer bound plus the recorded shadow band.
TILE_INSET = 6.0              # px inside the tile's (inner-bound) edge where the edge ring is sampled
NAVY_TOLERANCE = 18.0         # Euclidean RGB distance from the recorded gradient at that height (the glow adds ~8)
EDGE_SAMPLES_PER_SIDE = 3
RING_ANGLES = 16
RING_PROBE_PX = 6.0           # the gold ring is sampled this far inside its recorded outer edge, navy as far outside


def tile_record(meta: dict | None = None) -> dict:
    meta = meta if meta is not None else json.loads(I.META_OUT.read_text())
    t = dict(meta["tile"])
    t["canvas"] = meta["canvas"]
    shape = str(t.get("shape", "")).lower()
    t["kind"] = "continuous" if "continuous" in shape else "circular" if "circular" in shape else None
    return t


def _rr_sdf(x: float, y: float, x0: float, y0: float, size: float, r: float) -> float:
    """Signed distance to a circular-arc rounded square (negative inside)."""
    c, inner = size / 2, size / 2 - r
    qx, qy = abs(x - x0 - c) - inner, abs(y - y0 - c) - inner
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def tile_bounds(t: dict, n: int) -> dict:
    """Outer and inner bound (x0, y0, size, radius) of the recorded tile, scaled to an n-px image."""
    k = n / t["canvas"]
    x0, y0, size, r = t["x0_px"] * k, t["y0_px"] * k, t["body_px"] * k, t["corner_radius_px"] * k
    if t["kind"] == "circular":
        return {"outer": (x0, y0, size, r), "outer_slack": 0.0, "inner": (x0, y0, size, r)}
    if t["kind"] == "continuous":
        return {"outer": (x0, y0, size, r), "outer_slack": 1.0 * k, "inner": (x0, y0, size, min(size / 2, 1.3 * r))}
    raise ValueError(f"unknown tile shape {t.get('shape')!r}")


def tile_edge_points(t: dict, n: int) -> list[tuple[str, float, float]]:
    """Points TILE_INSET px inside the tile's inner bound: EDGE_SAMPLES_PER_SIDE on each straight side and one on
    each rounded corner's diagonal. Continuous image coordinates (pixel i spans [i, i+1))."""
    k = n / t["canvas"]
    x0, y0, size, r = tile_bounds(t, n)["inner"]
    m = TILE_INSET * k
    pts = []
    straight = size - 2 * r
    for i in range(EDGE_SAMPLES_PER_SIDE):
        f = r + straight * (i + 1) / (EDGE_SAMPLES_PER_SIDE + 1)
        pts += [(f"top{i}", x0 + f, y0 + m), (f"bottom{i}", x0 + f, y0 + size - m),
                (f"left{i}", x0 + m, y0 + f), (f"right{i}", x0 + size - m, y0 + f)]
    d = (r - m) / math.sqrt(2)
    for name, cx, cy, sx, sy in (("corner_tl", x0 + r, y0 + r, -1, -1), ("corner_tr", x0 + size - r, y0 + r, 1, -1),
                                 ("corner_bl", x0 + r, y0 + size - r, -1, 1),
                                 ("corner_br", x0 + size - r, y0 + size - r, 1, 1)):
        pts.append((name, cx + sx * d, cy + sy * d))
    return pts


def ring_points(t: dict, n: int) -> list[tuple[str, float, float, bool]]:
    """(name, x, y, gold?) RING_PROBE_PX inside (gold) and outside (navy) the logo's recorded outer edge."""
    k = n / t["canvas"]
    R, c = t["logo_diameter_px"] / 2 * k, n / 2
    out = []
    for i in range(RING_ANGLES):
        a = 2 * math.pi * i / RING_ANGLES
        for gold, rr in ((True, R - RING_PROBE_PX * k), (False, R + RING_PROBE_PX * k)):
            out.append((f"{'in' if gold else 'out'}{i}", c + rr * math.cos(a), c + rr * math.sin(a), gold))
    return out


def expected_navy(t: dict, y: float, n: int) -> tuple[float, float, float]:
    tt = y / n
    top, bot = t["fill"]["top_rgb"], t["fill"]["bottom_rgb"]
    return tuple(top[c] + (bot[c] - top[c]) * tt for c in range(3))


def is_navy(t: dict, px: list, y: float, n: int) -> bool:
    e = expected_navy(t, y, n)
    return px[3] == 255 and math.dist(px[:3], e) <= NAVY_TOLERANCE and px[2] > px[0]


def is_gold(px: list) -> bool:
    """Gold or its dark-brown rim (warm: red well above blue), never navy or white."""
    r, g, b, a = px
    return a == 255 and r - b >= 60 and r > g > b


def _px(img: bytes, w: int, x: float, y: float) -> list:
    i = (int(y) * w + int(x)) * 4
    return list(img[i:i + 4])


def tile_report(w: int, h: int, px: bytes, t: dict) -> dict:
    """The THEN of "Icon is the logo on a navy tile" on one decoded RGBA image, geometry from the `tile` record:
    canvas corners alpha 0; every pixel outside the tile and its recorded shadow band alpha 0; the edge ring (straight
    sides and the four rounded corners, just inside the edge) opaque and navy; the centre opaque; the gold ring at the
    recorded scale (gold just inside its recorded edge, navy just outside)."""
    rep: dict = {"size": [w, h], "tile_kind": t.get("kind"), "problems": []}
    probs = rep["problems"]
    if w != h:
        probs.append("not square")
        rep["ok"] = False
        return rep
    try:
        bd = tile_bounds(t, w)
    except ValueError as e:
        probs.append(str(e))
        rep["ok"] = False
        return rep
    k = w / t["canvas"]
    band = (t["shadow"]["reach_px"] + t["shadow"]["dy_px"]) * k + bd["outer_slack"] + 1.0
    x0, y0, size, r = bd["outer"]
    outside_nonzero, samples = 0, []
    alpha = px[3::4]
    # per row, the pixels whose centre is within `band` of the outer rounded square (_rr_sdf <= band) form one run
    # [lo, hi]: _rr_sdf <= band  <=>  qx <= sqrt((r + band)^2 - max(qy, 0)^2), qx = |x + 0.5 - cx| - inner
    cx, cy, inner, rb = x0 + size / 2, y0 + size / 2, size / 2 - r, r + band
    for y in range(h):
        row = alpha[y * w:(y + 1) * w]
        qy = max(abs(y + 0.5 - cy) - inner, 0.0)
        if qy > rb:
            lo, hi = w, w - 1                                  # the whole row is outside
        else:
            half = inner + math.sqrt(rb * rb - qy * qy)
            lo, hi = max(0, math.ceil(cx - half - 0.5)), min(w - 1, math.floor(cx + half - 0.5))
            while lo <= hi and _rr_sdf(lo + 0.5, y + 0.5, x0, y0, size, r) > band:
                lo += 1                                        # float edge cases: the exact test decides
            while hi >= lo and _rr_sdf(hi + 0.5, y + 0.5, x0, y0, size, r) > band:
                hi -= 1
            while lo > 0 and _rr_sdf(lo - 0.5, y + 0.5, x0, y0, size, r) <= band:
                lo -= 1
            while hi < w - 1 and _rr_sdf(hi + 1.5, y + 0.5, x0, y0, size, r) <= band:
                hi += 1
        outside = (row[:lo], row[hi + 1:]) if lo <= hi else (row,)
        offs = (0, hi + 1) if lo <= hi else (0,)
        for seg, off in zip(outside, offs):
            nz = len(seg) - seg.count(0)
            if nz:
                outside_nonzero += nz
                for x, a in enumerate(seg):
                    if a and len(samples) < 5:
                        samples.append([off + x, y, a])
    rep["outside_band_px"] = round(band, 1)
    rep["outside_nonzero_alpha"] = outside_nonzero
    rep["outside_samples"] = samples
    if outside_nonzero:
        probs.append(f"{outside_nonzero} pixel(s) outside the tile and its shadow band aren't transparent")
    corners = {f"{x},{y}": px[(y * w + x) * 4 + 3] for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))}
    rep["corner_alpha"] = corners
    if any(corners.values()):
        probs.append("a canvas corner isn't transparent")
    edge, bad = {}, []
    for name, x, y in tile_edge_points(t, w):
        v = _px(px, w, x, y)
        edge[name] = v + [round(math.dist(v[:3], expected_navy(t, y, w)), 1)]
        if not is_navy(t, v, y, w):
            bad.append(name)
    rep["edge_samples"] = edge
    if bad:
        probs.append(f"edge samples not opaque navy: {bad}")
    centre = _px(px, w, w / 2, h / 2)
    rep["centre"] = centre
    if centre[3] != 255:
        probs.append("the centre isn't opaque")
    ring = {name: (_px(px, w, x, y), gold) for name, x, y, gold in ring_points(t, w)}
    rep["ring_samples"] = {k_: v[0] for k_, v in ring.items()}
    not_gold = [k_ for k_, (v, gold) in ring.items() if gold and not is_gold(v)]
    not_navy = [k_ for k_, (v, gold) in ring.items() if not gold and (is_gold(v) or v[3] != 255)]
    if not_gold:
        probs.append(f"no gold just inside the recorded logo edge at {not_gold}")
    if not_navy:
        probs.append(f"gold or transparent just outside the recorded logo edge at {not_navy}")
    rep["ok"] = not probs
    return rep


def verify_icon(png_path: Path, icns_path: Path, regenerate: bool = True, meta: dict | None = None) -> dict:
    """Evidence for app.icon; raises CheckFail on the first unmet clause."""
    ev: dict = {"png": str(png_path), "icns": str(icns_path)}
    require(png_path.is_file() and icns_path.is_file(), "icon files missing", **ev)
    t = tile_record(meta)
    ev["tile_record"] = t
    require(t["kind"] is not None, f"AppIcon.json's tile shape {t.get('shape')!r} is unknown to the check", **ev)
    data = icns_path.read_bytes()
    sizes = icns_sizes(data)
    ev["icns_sizes"] = sizes
    bad = {t_: s for t_, s in sizes.items() if s["actual"] != [s["expected"], s["expected"]]}
    require(not bad, f"icns lacks standard sizes or has wrong dimensions: {sorted(bad)}", **ev)
    w, h, px = I.read_png(I.icns_entries(data)["ic10"])
    ev["icns_1024"] = tile_report(w, h, px, t)
    require(ev["icns_1024"]["ok"], f"the .icns 1024 px image isn't the logo on the navy tile: "
            f"{ev['icns_1024']['problems']}", **ev)
    w2, h2, px2 = I.read_png(png_path.read_bytes())
    ev["png"] = tile_report(w2, h2, px2, t)
    require(ev["png"]["ok"] and (w2, h2) == (1024, 1024),
            f"the PNG isn't a 1024 px logo on the navy tile: {ev['png']['problems']}", **ev)
    if regenerate:
        tmp = Path(tempfile.mkdtemp(prefix="hs-icon-regen-"))
        try:
            require(I.SOURCE.is_file(), f"logo source {I.SOURCE} is missing, so reproducibility can't be shown", **ev)
            meta2 = I.generate(out_dir=tmp)
            committed_meta = json.loads(I.META_OUT.read_text()) if I.META_OUT.exists() else {}
            rw, rh, rpx = I.read_png((tmp / I.PNG_OUT.name).read_bytes())
            diff = sum(1 for i in range(0, len(rpx), 4) if rpx[i:i + 4] != px2[i:i + 4]) if len(rpx) == len(px2) else -1
            ev["regenerated"] = {"dir": str(tmp), "ring_source_px": meta2["ring_source_px"],
                                 "source_sha256": meta2["source_sha256"],
                                 "committed_source_sha256": committed_meta.get("source_sha256"),
                                 "tile_record_same": meta2.get("tile") == committed_meta.get("tile"),
                                 "pixels_differing": diff,
                                 "png_bytes_identical": (tmp / I.PNG_OUT.name).read_bytes() == png_path.read_bytes()}
            require(meta2["source_sha256"] == committed_meta.get("source_sha256"),
                    "the local logo source differs from the one the committed icon was made from", **ev)
            require(ev["regenerated"]["tile_record_same"], "the generator's tile record differs from the committed one",
                    **ev)
            require((rw, rh) == (w2, h2) and diff == 0, "re-running the generator doesn't reproduce the committed PNG", **ev)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return ev


# icon-corner-c: "The tile has macOS 26's continuous-corner shape". The spec's literal values (not icon.py's constants,
# so a wrong constant there fails); the sampling geometry still comes from the record.
EXPECT_TILE = {"kind": "continuous", "body_px": 832.0, "x0_px": 96.0, "y0_px": 96.0, "corner_radius_px": 216.0,
               "logo_diameter_px": 569.719}        # logo: Zach's preview, ICON_DIAMETER × int(824 × 0.86) / 1024
CA_MEAN_MAX = 1.0             # generator mask vs Core Animation: mean |difference| over the canvas, in 1/255 ...
CA_PIXEL_MAX = 64.0           # ... and at any pixel (anti-aliasing only)
CAMASK = Path(I.__file__).with_name("camask.py")
SHAPE_RAY_ANGLES = (0.0, 8.0, 22.5, 45.0, 67.5, 82.0, 90.0)   # degrees from the corner centre, top side → vertical side
GAP_ALPHA_MAX = 128           # inside the circular outline but outside the continuous one: shadow only, never tile
GAP_MIN_PX = 16               # the gap between the two outlines must hold at least this many whole pixels


def record_masks(t: dict, n: int) -> dict:
    """The generator's tile mask for the record's geometry and shape, and the circular-arc mask of the same body and
    radius (the counterexample), both rendered by icon.corner_mask."""
    k = n / t["canvas"]
    x0, size, r = t["x0_px"] * k, t["body_px"] * k, t["corner_radius_px"] * k
    return {"generator": I.corner_mask(n, x0, t["y0_px"] * k, size, r, t["kind"]),
            "circular": I.corner_mask(n, x0, t["y0_px"] * k, size, r, "circular")}


def core_animation_mask(t: dict, n: int) -> tuple[bytes | None, str]:
    """Core Animation's cornerCurve = continuous rendering of the record's body and radius (hslib/camask.py, its own
    process). (None, why) when Core Animation can't render it: the check then fails."""
    k = n / t["canvas"]
    try:
        r = subprocess.run([sys.executable, "-I", str(CAMASK), str(n), repr(t["x0_px"] * k), repr(t["body_px"] * k),
                            repr(t["corner_radius_px"] * k)], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"{type(e).__name__}: {e}"
    if r.returncode != 0 or len(r.stdout) != n * n:
        return None, f"rc {r.returncode}, {len(r.stdout)} bytes, stderr {r.stderr.decode(errors='replace')[-300:]!r}"
    return r.stdout, "ok"


def mask_vs_reference(mask: list[float], ref: bytes) -> dict:
    """Mean and max |mask × 255 - ref| over the canvas (in 1/255), with where the max is."""
    total, worst, at = 0.0, -1.0, 0
    for i, (m, c) in enumerate(zip(mask, ref)):
        d = abs(m * 255.0 - c)
        total += d
        if d > worst:
            worst, at = d, i
    n = math.isqrt(len(ref))
    mean = total / len(ref)
    return {"mean": round(mean, 4), "max": round(worst, 2), "max_at": [at % n, at // n],
            "ok": mean <= CA_MEAN_MAX and worst <= CA_PIXEL_MAX}


def shape_probes(t: dict, n: int, masks: dict) -> dict:
    """Pixels that tell the continuous outline from the circular one, from the two masks:
    - `inside`: per corner, along rays from the corner's arc centre at SHAPE_RAY_ANGLES (45° = the diagonal; 0° and 90°
      are the straight sides where the corner meets them), the outermost pixel the generator's mask covers fully: the
      tile's own edge pixels, which must be opaque navy;
    - `gap`: every pixel the circular-arc mask covers fully and the continuous one not at all (each at least ~0.5 px
      from both outlines), which must not be tile."""
    k = n / t["canvas"]
    x0, y0, size, r = t["x0_px"] * k, t["y0_px"] * k, t["body_px"] * k, t["corner_radius_px"] * k
    gen, circ = masks["generator"], masks["circular"]
    inside = []
    for cname, cx, cy, sx, sy in (("tl", x0 + r, y0 + r, -1, -1), ("tr", x0 + size - r, y0 + r, 1, -1),
                                  ("bl", x0 + r, y0 + size - r, -1, 1), ("br", x0 + size - r, y0 + size - r, 1, 1)):
        for ang in SHAPE_RAY_ANGLES:
            a = math.radians(ang)
            dx, dy = sx * math.sin(a), sy * math.cos(a)
            last, s_ = None, 0.0
            while s_ < 2 * r:
                x, y = int(math.floor(cx + dx * s_)), int(math.floor(cy + dy * s_))
                if not (0 <= x < n and 0 <= y < n) or gen[y * n + x] < 1.0:
                    break
                last, s_ = (x, y), s_ + 0.25
            inside.append((f"{cname}{ang:g}", *(last or (-1, -1))))
    gap = [(i % n, i // n) for i, (g, c) in enumerate(zip(gen, circ)) if c >= 1.0 and g <= 0.0]
    return {"inside": inside, "gap": gap}


def shape_report(w: int, h: int, px: bytes, t: dict, probes: dict) -> dict:
    """The PNG's pixels at the shape probes: `inside` pixels opaque navy, `gap` pixels not tile (alpha <= GAP_ALPHA_MAX)."""
    rep: dict = {"problems": []}
    inside, bad_in = {}, []
    for name, x, y in probes["inside"]:
        v = _px(px, w, x, y) if x >= 0 else [0, 0, 0, 0]
        inside[name] = [x, y] + v
        if x < 0 or not is_navy(t, v, y + 0.5, w):
            bad_in.append(name)
    rep["inside_samples"] = inside
    gap = probes["gap"]
    gap_alpha = [px[(y * w + x) * 4 + 3] for x, y in gap]
    tile_in_gap = [[x, y, a] for (x, y), a in zip(gap, gap_alpha) if a > GAP_ALPHA_MAX]
    rep["gap_pixels"] = len(gap)
    rep["gap_alpha_max"] = max(gap_alpha) if gap_alpha else None
    rep["gap_tile_pixels"] = len(tile_in_gap)
    rep["gap_tile_samples"] = tile_in_gap[:5]
    if bad_in:
        rep["problems"].append(f"edge pixels just inside the continuous outline aren't opaque navy: {bad_in}")
    if len(gap) < GAP_MIN_PX:
        rep["problems"].append(f"only {len(gap)} pixel(s) between the circular and the continuous outline")
    if tile_in_gap:
        rep["problems"].append(f"{len(tile_in_gap)} pixel(s) inside the circular outline but outside the continuous "
                               f"one are tile (alpha > {GAP_ALPHA_MAX})")
    rep["ok"] = not rep["problems"]
    return rep


def flat_tile(t: dict, n: int, mask: list[float], probes: dict) -> bytes:
    """A counterexample image as shape_report sees it: at every probe pixel, the recorded navy gradient with `mask` as
    its alpha (no logo, glow or shadow); the pixels shape_report never reads stay transparent."""
    out = bytearray(n * n * 4)
    for x, y in [(x, y) for _, x, y in probes["inside"] if x >= 0] + probes["gap"]:
        a = round(mask[y * n + x] * 255)
        if a:
            out[(y * n + x) * 4:(y * n + x) * 4 + 4] = bytes((*(round(c) for c in expected_navy(t, y + 0.5, n)), a))
    return bytes(out)


def verify_shape(png_path: Path, ev: dict, meta: dict | None = None) -> dict:
    """Evidence for "The tile has macOS 26's continuous-corner shape"; raises CheckFail on the first unmet clause."""
    t = tile_record(meta)
    n = t["canvas"]
    sh: dict = {"expected": EXPECT_TILE}
    ev["shape"] = sh
    got = {"kind": t["kind"], **{k: t.get(k) for k in EXPECT_TILE if k != "kind"}}
    sh["record"] = got
    wrong = {k: got[k] for k, v in EXPECT_TILE.items()
             if not (got[k] == v if isinstance(v, str) else isinstance(got[k], (int, float)) and abs(got[k] - v) < 1e-6)}
    centred = abs(t["x0_px"] - (n - t["body_px"]) / 2) < 1e-6 and abs(t["y0_px"] - (n - t["body_px"]) / 2) < 1e-6
    require(not wrong and centred and n == 1024,
            f"AppIcon.json's tile record isn't the continuous 832 / 216 tile centred on 1024 with the preview's logo: "
            f"{wrong or 'not centred'}", **ev)
    masks = record_masks(t, n)
    ref, why = core_animation_mask(t, n)
    sh["core_animation"] = why
    require(ref is not None, f"Core Animation couldn't render the reference outline: {why}", **ev)
    sh["generator_vs_ca"] = mask_vs_reference(masks["generator"], ref)
    sh["circular_vs_ca"] = mask_vs_reference(masks["circular"], ref)
    require(sh["generator_vs_ca"]["ok"], f"the generator's mask differs from Core Animation's continuous corner by more "
            f"than {CA_MEAN_MAX}/255 mean or {CA_PIXEL_MAX}/255 at a pixel", **ev)
    require(not sh["circular_vs_ca"]["ok"], "counterexample: a circular-arc mask of the same radius matched Core "
            "Animation's continuous corner", **ev)
    probes = shape_probes(t, n, masks)
    w, h, px = I.read_png(png_path.read_bytes())
    require((w, h) == (n, n), f"the PNG is {w}x{h}, the record's canvas is {n}", **ev)
    sh["png"] = shape_report(w, h, px, t, probes)
    require(sh["png"]["ok"], f"the PNG's tile edge isn't the continuous outline: {sh['png']['problems']}", **ev)
    # counterexamples: a tile with circular-arc corners of the same radius, and the previous tile (824, circular 185.4)
    old_k = n / 1024
    old = I.corner_mask(n, 100 * old_k, 100 * old_k, 824 * old_k, 185.4 * old_k, "circular")
    sh["counterexamples"] = {}
    for name, m in (("circular_216", masks["circular"]), ("old_824_185.4", old)):
        rep = shape_report(n, n, flat_tile(t, n, m, probes), t, probes)
        sh["counterexamples"][name] = {"ok": rep["ok"], "problems": rep["problems"][:3]}
    require(not any(v["ok"] for v in sh["counterexamples"].values()),
            "a counterexample tile passed the continuous-outline test", **ev)
    return ev


def old_layout_logo(ring: dict | None) -> bytes:
    """The logo on the old transparent layout (borderless ring, ICON_DIAMETER px on the canvas). With the ring
    app.icon's regeneration just measured, only the cut is redone (no second ring detection)."""
    if not ring:
        return I.load_logo(diameter=I.ICON_DIAMETER)
    tmp = Path(tempfile.mkdtemp(prefix="hs-icon-old-"))
    try:
        sw = json.loads(I.META_OUT.read_text())["source_size"][0]
        return I.cut_logo(I.SOURCE, tmp, ring, sw, I.ICON_DIAMETER)[0]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def icon_counterexamples(png_path: Path, ring: dict | None = None) -> dict:
    """app.icon's counterexamples, each judged by tile_report with the committed tile record: the logo on the old
    transparent layout (the borderless ring, 824 px on 1024) and the committed icon with its tile painted white."""
    t = tile_record()
    w, h, px = I.read_png(png_path.read_bytes())
    old = old_layout_logo(ring)
    white = bytearray(px)
    for i in range(0, len(white), 4):
        if white[i + 3] and not is_gold(list(white[i:i + 4])):
            white[i:i + 3] = b"\xff\xff\xff"
    out = {}
    for name, img in (("old_transparent_layout", old), ("white_tile", bytes(white))):
        rep = tile_report(I.CANVAS, I.CANVAS, img, t)
        out[name] = {"ok": rep["ok"], "problems": rep["problems"][:4]}
    return out


# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "harness/app/*",
    "branding/*",
    "play",
    "harness/hslib/camask.py",
    "harness/hslib/app.py",
    "harness/hslib/checks/app_checks.py",
    "harness/hslib/checks/naming_checks.py",
    "harness/hslib/checks/polish_checks.py",
    "harness/hslib/checks/release_runtime_checks.py",
    "harness/hslib/dockshim.py",
    "harness/hslib/icon.py",
)


@check("app.icon", order=380, capability=CAP, stack="any", layer="unit", parallel_safe=True)
def app_icon(ctx):
    """branding/AppIcon.icns has every standard size 16-1024 px (incl. @2x); its 1024 px image and the PNG are the
    logo on the navy tile, judged with the geometry in AppIcon.json's `tile` record: canvas corners and everything
    outside the tile and its recorded shadow band alpha 0, a ring of points just inside the tile's edge (sides and
    rounded corners) opaque navy, the centre opaque, the gold ring at the recorded scale; re-running the generator
    reproduces the committed PNG pixel for pixel. Counterexamples: the old transparent layout and a white tile fail.
    icon-corner-c (verify_shape): the record names the continuous 832 / 216 tile centred on 1024 with the preview's logo;
    the generator's mask matches Core Animation's cornerCurve = continuous rendering (≤ 1/255 mean, ≤ 64/255 at any
    pixel) and a circular-arc mask of the same radius doesn't; the PNG's edge pixels just inside the continuous outline
    (incl. the corners at 45°) are opaque navy and the pixels between the circular and the continuous outline aren't
    tile. Counterexamples: tiles with circular 216 corners and the old 824 / 185.4 tile fail."""
    ev = verify_icon(I.PNG_OUT, I.ICNS_OUT)
    verify_shape(I.PNG_OUT, ev)
    ev["counterexamples"] = ce = icon_counterexamples(I.PNG_OUT, (ev.get("regenerated") or {}).get("ring_source_px"))
    require(all(not v["ok"] for v in ce.values()), "a counterexample icon passed the tile test", **ev)
    return ev


# ---------------------------------------------------------------- one icon everywhere (release-signing)

def pl_of(app: Path) -> dict:
    return plistlib.loads((app / "Contents" / "Info.plist").read_bytes())


def bundle_icon(app: Path) -> Path:
    name = str(pl_of(app).get("CFBundleIconFile", ""))
    return app / "Contents" / "Resources" / (name if name.endswith(".icns") else name + ".icns")


def icns_1024(path: Path) -> tuple[int, int, bytes]:
    return I.read_png(I.icns_entries(Path(path).read_bytes())["ic10"])


def dock_env_for(app_bundle: Path | None) -> dict:
    """launch.dock_env() as an app run of `app_bundle` (HS_FROM_APP=1, HS_APP_BUNDLE) or, with None, as a terminal
    run; the process environment is restored afterwards."""
    keys = (launch.FROM_APP_ENV, launch.APP_BUNDLE_ENV)
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        if app_bundle is not None:
            os.environ[launch.FROM_APP_ENV] = "1"
            os.environ[launch.APP_BUNDLE_ENV] = str(app_bundle)
        return launch.dock_env()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def release_wrapped_app(d: Path) -> Path:
    """release.py's app wrapper (wrap_app) run on a scratch payload (bin/Moonglade stand-in + branding/ as the payload
    carries it): the release app's icon as packaging puts it there, without packaging."""
    pl = d / "payload"
    (pl / "bin").mkdir(parents=True)
    (pl / "bin" / "Moonglade").write_bytes(b"stand-in launcher (never run)\n")
    shutil.copytree(P.ROOT / "branding", pl / "branding")
    app = d / "Moonglade.app"
    code = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import release; "
            "release.wrap_app(Path(sys.argv[2]), Path(sys.argv[3]))")
    r = subprocess.run([sys.executable, "-I", "-c", code, str(P.ROOT / "tools" / "release"), str(pl), str(app)],
                       capture_output=True, text=True, cwd=d, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"release.py wrap_app failed: {r.stderr[-600:]}")
    return app


def same_icon(path: Path, ref: bytes, t: dict) -> dict:
    """Decode `path` at 1024 px: identical to `ref` (branding/AppIcon.png's pixels)? Plus its tile-edge samples."""
    try:
        w, h, px = icns_1024(path)
    except (OSError, KeyError, I.IconError) as e:
        return {"path": str(path), "ok": False, "error": f"{type(e).__name__}: {e}"}
    edge = {n_: _px(px, w, x, y) for n_, x, y in tile_edge_points(t, w)}
    navy = all(is_navy(t, v, next(y for m, _, y in tile_edge_points(t, w) if m == n_), w) for n_, v in edge.items())
    diff = sum(1 for i in range(0, len(px), 4) if px[i:i + 4] != ref[i:i + 4]) if len(px) == len(ref) else -1
    return {"path": str(path), "size": [w, h], "pixels_differing": diff, "edge_navy": navy,
            "ok": (w, h) == (1024, 1024) and diff == 0 and navy}


@check("app.icon_everywhere", order=382, capability=CAP, stack="any", layer="unit", parallel_safe=True)
def icon_everywhere(ctx):
    """The dev app's and the release app's icon files (CFBundleIconFile; the release app's from this run's packaging
    or, when this run packaged none, from release.py's app wrapper on a scratch app) and the HSDOCK_ICON that
    launch.dock_env() gives a dev app run, a dev terminal run and a release app run all decode at 1024 px to the
    pixels of branding/AppIcon.png, with navy tile-edge samples. Counterexample: an app run whose bundle icon is the
    logo on a transparent surround fails."""
    from .release_runtime_checks import NEW_APP
    t = tile_record()
    _, _, ref = I.read_png(I.PNG_OUT.read_bytes())
    ev: dict = {"reference": str(I.PNG_OUT)}
    if not (A.APP / "Contents" / "Info.plist").is_file():
        build_app(ev)
    d = Path(tempfile.mkdtemp(prefix="hs-icon-everywhere-", dir=P.RUN if P.RUN.is_dir() else None))
    try:
        packaged = ctx.get("release_package") and (NEW_APP / "Contents" / "Info.plist").is_file() \
            and ctx["release_package"].get("rc") == 0
        rel = NEW_APP if packaged else release_wrapped_app(d / "wrapped")
        ev["release_app"] = {"path": str(rel), "from": "this run's packaging" if packaged else "release.py wrap_app"}
        envs = {"dev_app_run": dock_env_for(A.APP), "dev_terminal_run": dock_env_for(None),
                "release_app_run": dock_env_for(rel)}
        ev["dock_env_icons"] = {k: v.get("HSDOCK_ICON") for k, v in envs.items()}
        files = {"dev_app_bundle_icon": bundle_icon(A.APP), "release_app_bundle_icon": bundle_icon(rel),
                 **{f"{k}_HSDOCK_ICON": Path(v) for k, v in ev["dock_env_icons"].items() if v}}
        require(all(ev["dock_env_icons"].values()), "a dock environment has no HSDOCK_ICON", **ev)
        require(ev["dock_env_icons"]["dev_app_run"] == str(bundle_icon(A.APP))
                and ev["dock_env_icons"]["release_app_run"] == str(bundle_icon(rel)),
                "an app run's HSDOCK_ICON isn't its own bundle's icon", **ev)
        decoded: dict = {}                                # each distinct file decoded once
        ev["decoded"] = {k: decoded.setdefault(str(p), same_icon(p, ref, t)) if str(p) not in decoded
                         else decoded[str(p)] for k, p in files.items()}
        bad = [k for k, v in ev["decoded"].items() if not v["ok"]]
        require(not bad, f"icon files that aren't the navy-tile icon: {bad}", **ev)
        # counterexample: an app run whose bundle icon is the logo on a transparent surround
        ce = d / "ce" / "Moonglade.app" / "Contents" / "Resources"
        ce.mkdir(parents=True)
        (ce.parent / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIconFile": "AppIcon"}))
        iconset = d / "ce" / "AppIcon.iconset"
        iconset.mkdir()
        clear = bytearray(ref)                            # the logo kept, everything else transparent
        for i in range(0, len(clear), 4):
            if not is_gold(list(clear[i:i + 4])):
                clear[i:i + 4] = b"\0\0\0\0"
        I.write_png(iconset / next(k for k, v in I.ICONSET.items() if v == I.CANVAS), I.CANVAS, I.CANVAS, bytes(clear))
        subprocess.run(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(ce / "AppIcon.icns")], check=True,
                       capture_output=True)
        ce_env = dock_env_for(ce.parents[1])
        ev["counterexample"] = {"HSDOCK_ICON": ce_env.get("HSDOCK_ICON"),
                                **same_icon(Path(ce_env.get("HSDOCK_ICON", "")), ref, t)}
        require(ce_env.get("HSDOCK_ICON") == str(ce / "AppIcon.icns") and not ev["counterexample"]["ok"]
                and not ev["counterexample"].get("edge_navy"),
                "counterexample: a transparent-surround HSDOCK_ICON passed", **ev)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ev


# ---------------------------------------------------------------- bundle

def _write_guard_profile(path: Path, tag: str) -> str:
    """Allow everything except file writes outside the repo (plus /dev's tty/null/fd nodes); denials are logged."""
    repo = str(P.ROOT.resolve())
    prof = f"""(version 1)
(allow default)
(deny file-write*
  (require-all
    (require-not (subpath "{repo}"))
    (require-not (literal "/dev/null"))
    (require-not (literal "/dev/zero"))
    (require-not (literal "/dev/dtracehelper"))
    (require-not (regex #"^/dev/(tty|fd/|ttys)")))
  (with message "{tag}"))
"""
    path.write_text(prof)
    return prof


def verify_bundle(app: Path, ev: dict) -> None:
    contents = app / "Contents"
    plist_path = contents / "Info.plist"
    require(app.is_dir() and plist_path.is_file(), f"{app} wasn't built (no Info.plist)", **ev)
    lint = subprocess.run(["/usr/bin/plutil", "-lint", str(plist_path)], capture_output=True, text=True)
    ev["plutil_lint"] = {"rc": lint.returncode, "output": (lint.stdout + lint.stderr).strip()}
    require(lint.returncode == 0, "plutil -lint fails on Info.plist", **ev)
    pl = plistlib.loads(plist_path.read_bytes())
    ev["plist"] = pl
    exe = contents / "MacOS" / str(pl.get("CFBundleExecutable", ""))
    icon_name = str(pl.get("CFBundleIconFile", ""))
    icon_file = contents / "Resources" / (icon_name if icon_name.endswith(".icns") else icon_name + ".icns")
    ev |= {"executable": str(exe), "executable_is_file": exe.is_file(), "executable_x": os.access(exe, os.X_OK),
           "icon_file": str(icon_file), "icon_matches_branding": icon_file.is_file()
           and icon_file.read_bytes() == I.ICNS_OUT.read_bytes()}
    require(pl.get("CFBundleExecutable") and ev["executable_is_file"] and ev["executable_x"],
            "the plist doesn't name an executable that exists and is executable", **ev)
    require(pl.get("CFBundleIdentifier") == A.BUNDLE_ID, "the plist doesn't carry the bundle identifier", **ev)
    require(pl.get("CFBundlePackageType") == "APPL", "the plist isn't an application bundle (CFBundlePackageType)", **ev)
    ev["redistribution_wording"] = redistribution_wording(pl)
    require(not ev["redistribution_wording"], "the plist carries \"redistribution\" wording (release-signing)", **ev)
    require(icon_name and ev["icon_matches_branding"], "the plist's icon isn't the committed AppIcon.icns", **ev)
    repo_rec = (contents / "Resources" / "repo-path").read_text().strip() if (contents / "Resources" / "repo-path").exists() else None
    ev["repo_path_record"] = repo_rec
    require(repo_rec == str(P.ROOT.resolve()), "the bundle's repo-path record isn't this repo", **ev)
    v = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)],
                       capture_output=True, text=True)
    d = subprocess.run(["/usr/bin/codesign", "-dvv", str(app)], capture_output=True, text=True)
    ev["codesign_verify"] = {"rc": v.returncode, "output": (v.stdout + v.stderr).strip()}
    ev["codesign_display"] = [l for l in d.stderr.splitlines() if l.split("=")[0] in
                              ("Identifier", "Format", "CodeDirectory v", "Signature", "Sealed Resources version")]
    require(v.returncode == 0, "codesign --verify --deep --strict fails", **ev)
    require("Signature=adhoc" in d.stderr, "the bundle isn't ad-hoc signed", **ev)
    verify_names(app, pl, ev)


def redistribution_wording(pl) -> list[str]:
    """Every plist key or value (nested) containing "redistribut", any case (release-signing: none allowed)."""
    hits = []

    def walk(v, path):
        if isinstance(v, dict):
            for k, x in v.items():
                if re.search(r"(?i)redistribut", str(k)):
                    hits.append(f"{path}{k}")
                walk(x, f"{path}{k}.")
        elif isinstance(v, (list, tuple)):
            for i, x in enumerate(v):
                walk(x, f"{path}{i}.")
        elif re.search(r"(?i)redistribut", str(v)):
            hits.append(f"{path.rstrip('.')}={v}")
    walk(pl, "")
    return hits


def repo_roots() -> list[str]:
    """This repo's folder path (as given and resolved), longest first. A checkout may still sit in a folder with the
    old name; that's the folder, not wording the app shows, so name scans mask it first (e.g. the shim's install
    name embeds its build path)."""
    return sorted({str(P.ROOT), str(P.ROOT.resolve())}, key=len, reverse=True)


def old_name_hits(path: Path) -> list[str]:
    """Case-insensitive hits of the old project name in a file's bytes (the strings a binary can show), with the
    repo folder's own path masked."""
    try:
        data = path.read_bytes()
    except OSError as e:
        return [f"unreadable: {e}"]          # fail-closed
    for r in repo_roots():
        data = data.replace(r.encode(), b"<repo>")
    return [data[max(0, m.start() - 40):m.end() + 20].decode(errors="replace")
            for m in re.finditer(re.escape(OLD_NAME).encode(), data, re.I)]


def verify_names(app: Path, pl: dict, ev: dict) -> None:
    """rename-moonglade: bundle file name, plist names and id, designated requirement; and no launcher, plist or
    shim text still carries the old name (every user-visible launcher string is compiled into the executable)."""
    from .. import dockshim
    r = subprocess.run(["/usr/bin/codesign", "-d", "-r-", str(app)], capture_output=True, text=True)
    dr = [l.strip() for l in (r.stdout + r.stderr).splitlines() if l.strip().startswith("designated =>")]
    exe = app / "Contents" / "MacOS" / str(pl.get("CFBundleExecutable", ""))
    texts = {"executable": exe, "Info.plist": app / "Contents" / "Info.plist", "shim": dockshim.DYLIB}
    n = {"bundle_dir_name": app.name if app.resolve().parent == A.DIST.resolve() else None,
         "CFBundleName": pl.get("CFBundleName"), "CFBundleDisplayName": pl.get("CFBundleDisplayName"),
         "CFBundleIdentifier": pl.get("CFBundleIdentifier"), "designated_requirement": dr,
         "old_name_hits": {k: old_name_hits(p) for k, p in texts.items()}}
    ev["names"] = n
    if n["bundle_dir_name"] is not None:      # app.relocatable checks a copy under its own folder name
        require(n["bundle_dir_name"] == EXPECT_APP_DIR, f"the built bundle isn't dist/{EXPECT_APP_DIR}", **ev)
    require(n["CFBundleName"] == EXPECT_NAME and n["CFBundleDisplayName"] == EXPECT_NAME,
            f"CFBundleName / CFBundleDisplayName aren't {EXPECT_NAME}", **ev)
    require(n["CFBundleIdentifier"] == EXPECT_ID, f"the bundle identifier isn't {EXPECT_ID}", **ev)
    require(dr == [EXPECT_DR], f"the designated requirement isn't {EXPECT_DR}", **ev)
    require(not any(n["old_name_hits"].values()), "the launcher, plist or shim still carries the old name", **ev)


def heal_test_launchers() -> list[dict]:
    """Kill-safe: a harness killed mid-run leaves its own app session running (launcher, play loop, stack, stand-in).
    Only launchers that are provably a check's own are stopped: started with --client-cmd pointing at a stand-in .bat
    in a harness temp dir (hs-play-*). A player's session never has --client-cmd, so it's never touched (the GIVEN
    then fails instead). The launcher's SIGTERM stops its play loop, which shuts the stack down."""
    tmp = str(Path(tempfile.gettempdir()).resolve())
    own = []
    for l in _launchers(A.APP):
        m = re.search(r"--client-cmd (\S+)", l["cmd"])
        bat = Path(m.group(1)) if m else None
        if bat and str(bat.resolve()).startswith(tmp + "/hs-play-") and bat.name == "hs-standin.bat":
            own.append(l | {"stand_in_dir": str(bat.parent)})
    if not own:
        return []
    for l in own:
        Path(l["stand_in_dir"], "stop").write_text("quit")      # the stand-in exits; the play loop then cleans up
    t0 = time.time()
    while any(x["pid"] == l["pid"] for l in own for x in _launchers(A.APP)) and time.time() - t0 < 150:
        time.sleep(1)
    left = [l for l in own if any(x["pid"] == l["pid"] for x in _launchers(A.APP))]
    for l in left:
        try:
            os.kill(l["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    t0 = time.time()
    while any(x["pid"] == l["pid"] for l in left for x in _launchers(A.APP)) and time.time() - t0 < 150:
        time.sleep(1)
    for l in own:
        shutil.rmtree(l["stand_in_dir"], ignore_errors=True)
    return [{"pid": l["pid"], "stand_in_dir": l["stand_in_dir"], "needed_sigterm": l in left} for l in own]


def running_names(ev: dict) -> dict:
    """Open the built app through LaunchServices with the notepad stand-in (m0, stock client setting, test hook):
    the launcher's and the game process's LaunchServices display names, and every status/dialog/stage text the
    launcher recorded. Then quit the stand-in: the app exits 0 and nothing is left running."""
    from .polish_checks import GuiStandIn, lsappinfo_list      # polish_checks imports this module
    run: dict = {}
    ev["names_run"] = run
    require_clear(run)
    s = GuiStandIn("names")
    hook = Hook(s.dir, "hook")
    sfile = s.dir / "settings.json"
    sfile.write_text(json.dumps({"client": "stock"}))
    off, since = _log_size(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        t_open = time.time()
        run["open"] = open_app(A.APP, hook, "--account", "test", "--client-cmd", str(s.bat),
                               env={"HS_SETTINGS": str(sfile)})
        require(run["open"]["rc"] == 0, "`open` failed to launch the app", **run)
        up = wait_app_client_up(s, hook, off, since)
        run["client_up"] = up
        require(up["up"], f"stand-in client never came up under the app: {up.get('why')}", **run)
        launchers = _launchers(A.APP)
        lp = launchers[0]["pid"] if len(launchers) == 1 else None
        t0, apps, nps = time.time(), [], []
        while time.time() - t0 < NAMES_TIMEOUT:
            nps = GuiStandIn.notepad_pids()
            apps = [a for a in lsappinfo_list() if a["pid"] in nps or a["pid"] == lp or a.get("bundle") == EXPECT_ID]
            game = [a for a in apps if a["pid"] in nps]
            if lp and game and all(a.get("name") == EXPECT_NAME for a in game) \
                    and any(a["pid"] == lp and a.get("name") == EXPECT_NAME for a in apps):
                break
            time.sleep(0.5)
        run |= {"t_open": round(t_open, 3), "launchers": launchers, "launcher_pid": lp, "notepad_pids": nps,
                "apps": apps, "names_after_s": round(time.time() - t0, 1)}
        ls_launcher = [a for a in apps if a["pid"] == lp]
        ls_game = [a for a in apps if a["pid"] in nps]
        require(lp and ls_launcher and ls_launcher[0].get("name") == EXPECT_NAME
                and ls_launcher[0].get("bundle") == EXPECT_ID,
                f"the launcher's LaunchServices display name isn't {EXPECT_NAME} (bundle {EXPECT_ID})", **run)
        require(ls_game and all(a.get("name") == EXPECT_NAME for a in ls_game),
                f"the game process's LaunchServices display name isn't {EXPECT_NAME}", **run)
        s.stop_file.write_text("quit")
        rc, dt = wait_app_exit(hook, A.APP)
        shown = [{"kind": e.get("kind"), "id": e.get("id"), **{k: e[k] for k in ("title", "text", "label") if k in e}}
                 for e in hook.events() if e.get("kind") in ("status", "dialog", "detail", "stage")]
        # a text may include the repo's folder path (e.g. a log path): masked, see repo_roots()
        roots = repo_roots()

        def unrooted(v) -> str:
            v = str(v)
            for r in roots:
                v = v.replace(r, "<repo>")
            return v.lower()
        old = [x for x in shown if any(OLD_NAME.lower() in unrooted(x.get(k, "")) for k in ("title", "text", "label"))]
        run |= {"app_exit_code": rc, "exit_after_s": dt, "shown_texts": shown[:60], "shown_with_old_name": old,
                "remaining_after": app_remaining(s)}
        require(shown, "the launcher recorded no status/stage text (nothing to judge)", **run)
        require(not old, "a launcher status/dialog/stage text still says the old name", **run)
        require(rc == 0, f"the app's exit code is {rc}, expected 0", **run)
        require(is_clear(run["remaining_after"]), "something from this repo is still running", **run)
        return run
    finally:
        stop_launchers(A.APP)
        s.cleanup()


@check("app.bundle_valid", order=381, capability=CAP, stack="down", layer="integration")
def bundle_valid(ctx):
    """From a clean checkout with the committed branding assets, `harness/hs app` (run under a sandbox profile that
    denies and logs every file write outside the repo) builds dist/Moonglade.app: plutil -lint passes; the plist
    names the executable (present, executable), the bundle identifier and the icon; codesign --verify --deep --strict
    passes on an ad-hoc signature; no plist key or value says "redistribut" (release-signing; counterexample: the
    old "not for redistribution" line is caught); and no write outside the repo was attempted. rename-moonglade: CFBundleName and
    CFBundleDisplayName are Moonglade, the id is local.moonglade.app, the designated requirement is
    `identifier "local.moonglade.app"`, and the launcher/plist/shim carry no old-name text. (The opened-app half of
    the scenario is app.names_running: it puts windows on screen, so it's a gui check, not quick.)"""
    ev: dict = {"cmd": f"{SANDBOX_EXEC} -f <guard.sb> {HS} app"}
    ev["healed_stale_test_launchers"] = heal_test_launchers()     # kill-safe: a killed earlier run's own session
    status = subprocess.run(["git", "-C", str(P.ROOT), "status", "--porcelain", "--", "branding/"],
                            capture_output=True, text=True).stdout
    tracked = subprocess.run(["git", "-C", str(P.ROOT), "ls-files", "--", "branding/AppIcon.icns", "branding/AppIcon.png"],
                             capture_output=True, text=True).stdout.split()
    ev["given_branding"] = {"tracked": tracked, "uncommitted_changes": status.strip()}
    require(len(tracked) == 2 and not status.strip(), "GIVEN not met: branding assets not committed and clean", **ev)
    require(not _launchers(A.APP), "the built app is running; can't rebuild it under a check", running=_launchers(A.APP), **ev)
    tag = f"hs-appguard-{os.getpid()}-{int(time.time())}"
    work = Path(tempfile.mkdtemp(prefix="hs-appguard-")).resolve()
    prof = work / "guard.sb"
    ev["profile"] = _write_guard_profile(prof, tag)
    since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        # self-test: the profile really blocks (and logs) a write outside the repo, and allows one inside it
        outside = work / "outside-write-probe"
        inside = P.RUN / f".app-guard-probe-{os.getpid()}"
        P.RUN.mkdir(parents=True, exist_ok=True)
        t_out = subprocess.run([SANDBOX_EXEC, "-f", str(prof), "/usr/bin/touch", str(outside)], capture_output=True, text=True)
        t_in = subprocess.run([SANDBOX_EXEC, "-f", str(prof), "/usr/bin/touch", str(inside)], capture_output=True, text=True)
        ev["selftest"] = {"outside": {"rc": t_out.returncode, "stderr": t_out.stderr.strip(), "exists": outside.exists()},
                          "inside": {"rc": t_in.returncode, "exists": inside.exists()}}
        inside.unlink(missing_ok=True)
        require(t_out.returncode != 0 and not outside.exists(), "self-test: the guard didn't block a write outside the repo", **ev)
        require(t_in.returncode == 0, "self-test: the guard blocked a write inside the repo", **ev)

        shutil.rmtree(A.APP, ignore_errors=True)
        t0 = datetime.now()
        time.sleep(1.1)                                   # keep self-test denials strictly before the build window
        build_since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        b = subprocess.run([SANDBOX_EXEC, "-f", str(prof), HS, "app"], cwd=P.ROOT, capture_output=True, text=True,
                           timeout=600)
        ev["build"] = {"rc": b.returncode, "stdout_tail": b.stdout[-1500:], "stderr_tail": b.stderr[-1500:]}
        # sentinel (cycle 3): a second blocked write after the build. Once its denial is in the log, every earlier
        # report of this tag (the build's) is too; a fixed 3 s wait sometimes read the log before delivery (the Mac
        # idle/locked: 1 of 5 quick runs saw no rows at all)
        time.sleep(1.1)
        after = work / "after-build-probe"
        subprocess.run([SANDBOX_EXEC, "-f", str(prof), "/usr/bin/touch", str(after)], capture_output=True)
        rows = _wait_denials(tag, since, lambda rs: any("after-build-probe" in r["target"] for r in rs),
                             timeout=DENIAL_LOG_WAIT_S)
        ev["denials"] = {"selftest": [r for r in rows if r["time"] and r["time"][:19].replace("T", " ") < build_since],
                         "build": [r for r in rows if r["time"] and r["time"][:19].replace("T", " ") >= build_since
                                   and "after-build-probe" not in r["target"]],
                         "sentinel": [r for r in rows if "after-build-probe" in r["target"]]}
        require(any("outside-write-probe" in r["target"] for r in ev["denials"]["selftest"]),
                "self-test: the blocked write isn't in the denial log, so the log can't vouch for the build", **ev)
        require(ev["denials"]["sentinel"] and not after.exists(),
                "the post-build sentinel denial never reached the log, so the log can't vouch for the build", **ev)
        require(not ev["denials"]["build"], "the build tried to write outside the repo", **ev)
        require(b.returncode == 0, "`harness/hs app` failed", **ev)
        require(A.APP.is_dir(), "dist/Moonglade.app doesn't exist after the build", **ev)
        verify_bundle(A.APP, ev)
        ev["build_window_started"] = t0.isoformat()
        # release-signing counterexample: the plist line the dev app used to carry is caught by the same test
        ev["counterexample_old_copyright_line"] = redistribution_wording(
            {**pl_of(A.APP), "NSHumanReadableCopyright": "Personal project; not for redistribution."})
        require(ev["counterexample_old_copyright_line"], "counterexample: the old copyright line wasn't caught", **ev)
        return ev
    finally:
        shutil.rmtree(work, ignore_errors=True)


# app.names_running: a scenario of a journey (journey_checks.py), not a check of its own (test-pyramid part 2)
def names_running(ctx):
    """rename-moonglade, the running half of "Built bundle and running app carry the new name": the freshly built app,
    opened through LaunchServices with the notepad stand-in: the launcher's LaunchServices display name is Moonglade
    (bundle id local.moonglade.app), the game process's is Moonglade, no status/dialog/stage text the launcher shows
    says the old name, the app exits 0 and nothing is left running. gui: it puts the launcher and a Wine window on
    screen, so quick runs never run it."""
    ev: dict = {"healed_stale_test_launchers": heal_test_launchers()}   # kill-safe: a killed earlier run's session
    build_app(ev)
    pl = plistlib.loads((A.APP / "Contents" / "Info.plist").read_bytes())
    verify_names(A.APP, pl, ev)                       # the bundle being opened is the renamed one
    running_names(ev)
    return ev


# ---------------------------------------------------------------- runtime helpers

def _launchers(bundle: Path | None = None) -> list[dict]:
    """Running Moonglade.app launchers (any copy, or the one at `bundle`)."""
    suffix = ".app/Contents/MacOS/" + A.EXE_NAME
    out = []
    for pid, cmd in procs.table():
        exe = cmd.split(" --", 1)[0]
        if exe.endswith(suffix) and (bundle is None or os.path.realpath(exe) ==
                                     os.path.realpath(str(bundle) + "/Contents/MacOS/" + A.EXE_NAME)):
            out.append({"pid": pid, "cmd": cmd[:300], "ppid": procs.ppid(pid)})
    return out


def _terminal_pids() -> set[int]:
    return {pid for pid, cmd in procs.table() if "Terminal.app/Contents/MacOS/Terminal" in cmd}


class Hook:
    """The app's test-hook file: one JSON event per line (status / dialog / exit)."""

    def __init__(self, d: Path, name: str):
        self.path = d / f"{name}.jsonl"

    def events(self) -> list[dict]:
        try:
            return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]
        except (FileNotFoundError, ValueError):
            return []

    def find(self, kind: str, ident: str | None = None) -> list[dict]:
        return [e for e in self.events() if e.get("kind") == kind and (ident is None or e.get("id") == ident)]

    def exit_code(self):
        ex = self.find("exit")
        return ex[-1]["code"] if ex else None


def open_app(bundle: Path, hook: Hook, *args: str, env: dict | None = None, new_instance: bool = False) -> dict:
    """`open` the app through LaunchServices. new_instance: `open -n`, a second copy of the app process (a second
    *launch*), instead of a reopen event to the running one."""
    cmd = [OPEN, *(["-n"] if new_instance else []), "--env", f"{A.HOOK_ENV}={hook.path}",
           *[x for k, v in {**PINNED, **(env or {})}.items() for x in ("--env", f"{k}={v}")],
           str(bundle), "--args", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return {"cmd": " ".join(cmd), "rc": r.returncode, "output": (r.stdout + r.stderr).strip()}


def _log_size() -> int:
    return A.LOG.stat().st_size if A.LOG.exists() else 0


def _log_since(off: int) -> str:
    try:
        with open(A.LOG, errors="replace") as f:
            f.seek(off)
            return f.read()
    except FileNotFoundError:
        return ""


def _play_pid(off: int, launcher_pid: int | None = None) -> int | None:
    pat = re.compile(r"\[app [^\]]+\] play loop pid (\d+):")
    text = _log_since(off)
    if launcher_pid is not None:          # the launcher's header line names its own pid; take the play pid after it
        idx = text.find(f"==== Moonglade.app pid {launcher_pid} ")
        text = text[idx:] if idx >= 0 else ""
    m = pat.search(text)
    return int(m.group(1)) if m else None


def tcc_prompts(since: str) -> list[str]:
    """tccd's Documents-folder prompts and answers for the app over the last 2 h (a prompt raised by an earlier launch
    may still be waiting on screen; later requests queue behind it without a new prompt line). `since` is kept in
    the evidence by callers; the window is wider on purpose."""
    r = subprocess.run(["/usr/bin/log", "show", "--last", "2h", "--predicate",
                        'process == "tccd" AND eventMessage CONTAINS "SystemPolicyDocumentsFolder"'],
                       capture_output=True, text=True, timeout=300)
    return [l[:400] for l in r.stdout.splitlines()
            if ("AUTHREQ_PROMPTING" in l and A.BUNDLE_ID in l) or "AUTHREQ_RESULT" in l]


def wait_app_client_up(s: StandIn, hook: Hook, off: int, since: str, timeout=START_TIMEOUT) -> dict:
    """Until the stand-in (started by the app's play loop) is up. Fails fast if the app exits or its play loop never
    prints anything (blocked, e.g. by a macOS permission prompt)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if hook.exit_code() is not None:
            return {"up": False, "why": f"the app exited early with {hook.exit_code()}", "events": hook.events(),
                    "after_s": round(time.time() - t0, 1)}
        if s.started.exists():
            cp = play.client_processes(s.bat.name.lower())
            if cp:
                return {"up": True, "after_s": round(time.time() - t0, 1), "client_processes": cp}
        if time.time() - t0 > APP_OUTPUT_TIMEOUT and "[play] " not in _log_since(off):
            return {"up": False, "why": TCC_HINT, "tcc_prompts": tcc_prompts(since), "events": hook.events(),
                    "app_log": _log_since(off)[-2000:], "after_s": round(time.time() - t0, 1)}
        time.sleep(1)
    return {"up": False, "why": "timeout", "after_s": round(time.time() - t0, 1)}


def wait_app_exit(hook: Hook, bundle: Path, timeout=EXIT_TIMEOUT) -> tuple[int | None, float]:
    """Until the app recorded its exit and its process is gone. Returns (exit code or None, seconds)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if hook.exit_code() is not None and not _launchers(bundle):
            return hook.exit_code(), round(time.time() - t0, 1)
        time.sleep(1)
    return hook.exit_code(), round(time.time() - t0, 1)


def stop_launchers(bundle: Path | None = None) -> list:
    """Last resort after a failure: SIGTERM the launcher(s) (they stop their play loop), then SIGKILL."""
    hit = _launchers(bundle)
    for l in hit:
        try:
            os.kill(l["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
    t0 = time.time()
    while _launchers(bundle) and time.time() - t0 < 150:    # > play's own cleanup (stack down ~60 s)
        time.sleep(1)
    for l in _launchers(bundle):
        os.kill(l["pid"], signal.SIGKILL)
    return hit


def app_remaining(s: StandIn | None = None) -> dict:
    r = remaining(s)
    r["app_launchers"] = _launchers()
    return r


def require_clear(ev: dict):
    before = app_remaining()
    ev["given_nothing_running"] = before
    require(is_clear(before), "GIVEN not met: something from this repo is already running", **ev)


def build_app(ev: dict) -> None:
    require(not _launchers(A.APP), "the built app is already running", running=_launchers(A.APP), **ev)
    b = subprocess.run([HS, "app"], cwd=P.ROOT, capture_output=True, text=True, timeout=600)
    ev["build"] = {"rc": b.returncode, "stderr_tail": b.stderr[-800:]}
    require(b.returncode == 0, "`harness/hs app` failed", **ev)


def lifecycle(bundle: Path, ev: dict, tag: str) -> dict:
    """Open, play, quit, for the app at `bundle`. Fills ev; returns it."""
    require_clear(ev)
    s = StandIn(tag)
    hook = Hook(s.dir, "hook")
    terminals_before = _terminal_pids()
    off, since = _log_size(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        t_start = time.time()
        # stock client setting (scratch settings file): the play loop then prints a login line, the case where a
        # password could reach app.log (asserted at the end)
        settings = s.dir / "settings.json"
        settings.write_text(json.dumps({"client": "stock"}))
        ev["open"] = open_app(bundle, hook, "--account", "test", "--client-cmd", str(s.bat),
                              env={"HS_SETTINGS": str(settings)})
        require(ev["open"]["rc"] == 0, "`open` failed to launch the app", **ev)
        up = wait_app_client_up(s, hook, off, since)
        ev["client_up"] = up
        require(up["up"], f"stand-in client never came up under the app: {up.get('why')}", **ev)
        launchers = _launchers(bundle)
        pid_launcher = launchers[0]["pid"] if launchers else None
        pid_play = _play_pid(off, pid_launcher)
        st = stack.status()
        ev |= {"launchers_while_running": launchers, "play_pid": pid_play,
               "play_parent": procs.ppid(pid_play) if pid_play else None,
               "stack_while_client_up": {"processes": st["processes"], "mysql_ready": st["mysql_ready"],
                                         "soap_ready": st["soap_ready"]},
               "events_while_running": hook.events()}
        starting = hook.find("status", "starting")
        require(starting, "no \"starting\" status was shown", **ev)
        require(len(launchers) == 1 and launchers[0]["ppid"] == 1,
                "the app isn't running as one LaunchServices-launched process (parent launchd)", **ev)
        require(pid_play and ev["play_parent"] == pid_launcher, "the play loop isn't the app's child", **ev)
        require(st["mysql_ready"] and st["soap_ready"] and all(st["processes"].values()),
                "the stack isn't at readiness while the client runs", **ev)
        time.sleep(3)
        require(hook.exit_code() is None and _launchers(bundle), "the app exited while the client was running", **ev)

        s.stop_file.write_text("quit")               # the stand-in exits 0 within ~1 s
        rc, dt = wait_app_exit(hook, bundle)
        rec = last_record(pid_play) if pid_play else {}
        ev |= {"app_exit_code": rc, "app_exit_after_client_quit_s": dt, "events": hook.events(),
               "play_record": {k: rec.get(k) for k in ("outcome", "client_exit", "exit_code", "cleanup")},
               "graceful_halt": graceful_halt(t_start, rec), "remaining_after": app_remaining(s),
               "new_terminal_processes": sorted(_terminal_pids() - terminals_before),
               "app_log": _log_since(off)[-3000:]}
        require(rc == 0, f"the app's exit code is {rc}, expected 0", **ev)
        require(not hook.find("dialog"), "the app showed a dialog on a normal run", **ev)
        require(rec.get("outcome") == "client_exit" and rec.get("exit_code") == 0,
                "the play loop under the app didn't finish with a normal client exit", **ev)
        require(ev["graceful_halt"]["ok"], "the stack didn't shut down gracefully", **ev)
        require(is_clear(ev["remaining_after"]), "something from this repo is still running", **ev)
        require(not ev["new_terminal_processes"], "a Terminal window/process was started", **ev)
        # in-client-login: an app run never writes a password into app.log (play.shown_password)
        text = _log_since(off)
        ev["login_line_in_app_log"] = [l[-160:] for l in text.splitlines() if "Log in with account" in l]
        ev["password_in_app_log"] = f"password {P.TEST_PASS}" in text.lower()
        require(ev["login_line_in_app_log"], "the stock-mode login line didn't reach app.log (nothing to test)", **ev)
        require(not ev["password_in_app_log"], "the play loop wrote the password into app.log", **ev)
        return ev
    finally:
        stop_launchers(bundle)
        s.cleanup()


# ---------------------------------------------------------------- runtime checks

# app.launch_lifecycle: a scenario of a journey (journey_checks.py), not a check of its own (test-pyramid part 2)
def launch_lifecycle(ctx):
    """`open dist/Moonglade.app` (stand-in client): a "starting" status is recorded, the stack reaches readiness, the
    client is launched by the app's play loop (the app's child, the app launched by LaunchServices, no Terminal),
    and when the client exits the stack shuts down gracefully, the app exits 0 with no dialog, and nothing from this
    repo (stack, Wine, Wow.exe, launcher) remains, ports 3307/3724/8085/7878 free."""
    ev: dict = {"bundle": str(A.APP)}
    build_app(ev)
    return lifecycle(A.APP, ev, "app")


# seamless-experience (mac-app "Friendly launcher errors"): words the error dialog's main text must not use
TECH_WORDS = re.compile(r"\b(servers?|databases?|mysql|ports?|realms?|soap)\b", re.I)


@check("app.startup_failure", order=383, capability=CAP, stack="down", layer="integration")
def startup_failure(ctx):
    """With an unusable worldserver DataDir, the opened app never starts the client, records an error dialog whose
    main text is plain (none of server, database, MySQL, port, realm, SOAP) and names the stage it stopped at, and
    whose "Show details" text names the problem (the play loop's error and the server's own cause) and the app.log
    path; the app exits nonzero and leaves nothing running. launcher-polish: its stage events stop at world and the
    dialog names "Loading the world"."""
    bad = "/nonexistent/hs-app-failfast"
    ev: dict = {"bundle": str(A.APP), "world_datadir": bad}
    build_app(ev)
    require_clear(ev)
    s = StandIn("appfail")
    hook = Hook(s.dir, "hook")
    off, since = _log_size(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    wine_off = P.WINE_LOG.stat().st_size if P.WINE_LOG.exists() else 0
    try:
        # scratch settings, stock client: without them the play loop's "prepare" stage would (re)build the player's
        # own env/client-play/ whenever the client patch inputs changed (F28); the failure under test is the world's
        settings = s.dir / "settings.json"
        settings.write_text(json.dumps({"client": "stock"}))
        ev["open"] = open_app(A.APP, hook, "--account", "test", "--client-cmd", str(s.bat), "--world-datadir", bad,
                              env={"HS_SETTINGS": str(settings)})
        require(ev["open"]["rc"] == 0, "`open` failed to launch the app", **ev)
        t0 = time.time()
        while hook.exit_code() is None and time.time() - t0 < 900:
            if time.time() - t0 > APP_OUTPUT_TIMEOUT and "[play] " not in _log_since(off):
                ev |= {"tcc_prompts": tcc_prompts(since), "app_log": _log_since(off)[-2000:]}
                require(False, TCC_HINT, **ev)
            time.sleep(1)
        rc, dt = wait_app_exit(hook, A.APP, timeout=60)
        launcher_pid = (hook.events() or [{}])[0].get("pid")
        pid_play = _play_pid(off, launcher_pid)
        rec = last_record(pid_play) if pid_play else {}
        try:
            with open(P.WINE_LOG, errors="replace") as fh:
                fh.seek(wine_off)
                launches = [l.strip() for l in fh if "[hs launch]" in l and " start " in l]
        except FileNotFoundError:
            launches = []
        errors = hook.find("dialog", "error")
        text = errors[-1]["text"] if errors else ""
        details = errors[-1].get("details") or "" if errors else ""
        ev |= {"app_exit_code": rc, "elapsed_s": round(time.time() - t0, 1), "events": hook.events(),
               "play_record": {k: rec.get(k) for k in ("outcome", "error", "client_started", "exit_code")},
               "client_start_stamps_in_wine_log": launches, "stand_in_started_marker": s.started.exists(),
               "remaining_after": app_remaining(s), "app_log": _log_since(off)[-3000:]}
        require(rc is not None and rc != 0, f"the app's exit code is {rc}, expected nonzero", **ev)
        require(errors, "no error dialog was shown", **ev)
        require(rec.get("outcome") == "startup_failed" and rec.get("client_started") is False and rec.get("error"),
                "the play loop didn't record a startup failure before the client", **ev)
        # seamless-experience: the technical text moved from the main text into "Show details"; each clause the
        # check asserted on the dialog before is now asserted on the details, and the main text must be plain
        ev["error_dialog"] = {"title": errors[-1].get("title") if errors else None, "text": text, "details": details,
                        "details_control": errors[-1].get("details_control") if errors else None}
        ev["tech_words_in_main_text"] = sorted({m.group(0).lower() for m in TECH_WORDS.finditer(
            f"{ev['error_dialog']['title']} {text}")})
        require(ev["error_dialog"]["details_control"] == "Show details" and details,
                "the error dialog has no Show details text", **ev)
        require(not ev["tech_words_in_main_text"], "the error dialog's main text uses technical wording", **ev)
        require(rec["error"] in details, "the dialog's details don't name the play loop's error", **ev)
        require("Failed to find map files" in details, "the dialog's details don't give the server's cause", **ev)
        require(str(A.LOG) in details, "the dialog's details don't give the log path", **ev)
        require(not s.started.exists() and not launches, "the client was started despite the failed startup", **ev)
        require(is_clear(ev["remaining_after"]), "something from this repo is still running", **ev)
        verify_failure_stage(ev, text, details)   # launcher-polish: the stage it stopped at
        return ev
    finally:
        stop_launchers(A.APP)
        s.cleanup()


def verify_failure_stage(ev: dict, text: str, details: str) -> None:
    """launcher-polish "Failures name the stage": the stage events stop at world and the dialog names it (in its main
    text; seamless-experience moved the log path into the dialog's details)."""
    st = [e for e in ev["events"] if e.get("kind") == "stage"]
    ev["stage_events"] = [{k: e.get(k) for k in ("id", "state")} for e in st]
    require(st and st[-1]["id"] == "world" and not [e for e in st if e["id"] in
                                                     ("bots", "account", "game_setup", "launching", "ready")],
            "the stage events don't stop at world", dialog=text, **ev)
    require("Loading the world" in text, "the error dialog doesn't name the stage \"Loading the world\"", dialog=text, **ev)
    require(str(A.LOG) in details, "the error dialog's details don't give the log path", dialog=details, **ev)


# app.single_instance: a scenario of a journey (journey_checks.py), not a check of its own (test-pyramid part 2)
def single_instance(ctx):
    """(a) With the app running its game, opening the app again records an "already running" message, starts no second
    launcher, play loop, stack or client, and the first session continues and later exits 0. (b) With ./play
    running, opening the app records "already running", the app exits 0 without starting anything, and ./play
    continues and later exits 0."""
    ev: dict = {"bundle": str(A.APP)}
    build_app(ev)
    require_clear(ev)
    # (a) the app is running
    a1, a2 = StandIn("app1"), StandIn("app2")
    h1, h2 = Hook(a1.dir, "hook"), Hook(a2.dir, "hook")
    off, since = _log_size(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    A_ev: dict = {}
    ev["app_running"] = A_ev
    try:
        A_ev["open_first"] = open_app(A.APP, h1, "--account", "test", "--client-cmd", str(a1.bat))
        up = wait_app_client_up(a1, h1, off, since)
        A_ev["first_client_up"] = up
        require(up["up"], f"first app's client never came up: {up.get('why')}", **ev)
        first = _launchers(A.APP)
        play_before = [p["pid"] for p in play_processes()]
        stack_before, lock_before = stack.stack_processes(), play.LOCK.read_text().strip()
        # launcher-fixes: a reopen of the running app while it loads brings its loading window forward (no message,
        # app.reopen_during_loading), so the "already running" case is a second launch: a second app process
        A_ev["open_second"] = open_app(A.APP, h2, "--account", "test", "--client-cmd", str(a2.bat), new_instance=True)
        t0 = time.time()
        while time.time() - t0 < 30 and not (h1.find("dialog", "already_running") or h2.find("dialog", "already_running")):
            time.sleep(0.5)
        A_ev["message_after_s"] = round(time.time() - t0, 1)
        time.sleep(5)
        A_ev |= {"first_launchers": first, "launchers_after": _launchers(), "events_first": h1.events(),
                 "events_second": h2.events(), "play_before": play_before,
                 "play_after": [p["pid"] for p in play_processes()], "stack_before": stack_before,
                 "stack_after": stack.stack_processes(), "lock_before": lock_before,
                 "lock_after": play.LOCK.read_text().strip(), "second_client_started": a2.started.exists(),
                 "first_client_after": play.client_processes(a1.bat.name.lower())}
        msgs = h1.find("dialog", "already_running") + h2.find("dialog", "already_running")
        require(msgs, "no \"already running\" message after opening the app again", **ev)
        require(not A_ev["second_client_started"], "a second client was started", **ev)
        require([l["pid"] for l in A_ev["launchers_after"]] == [l["pid"] for l in first],
                "a second launcher is still running (or the first one is gone)", **ev)
        require(A_ev["play_after"] == play_before and len(play_before) == 1, "a second play loop ran, or the first died", **ev)
        require(A_ev["stack_after"] == stack_before and A_ev["lock_after"] == lock_before,
                "the stack or the lock changed under the first session", **ev)
        require(A_ev["first_client_after"] and h1.exit_code() is None, "the first session was affected", **ev)
        a1.stop_file.write_text("quit")
        rc, dt = wait_app_exit(h1, A.APP)
        A_ev |= {"first_exit_code": rc, "first_exit_after_quit_s": dt, "remaining_after": app_remaining(a1)}
        require(rc == 0, f"the first app's exit code is {rc}, expected 0", **ev)
        require(is_clear(A_ev["remaining_after"]), "something from this repo is still running", **ev)
    finally:
        stop_launchers(A.APP)
        a2.cleanup()
        a1.cleanup()

    # (b) ./play is running
    B_ev: dict = {}
    ev["play_running"] = B_ev
    first, other = StandIn("play1"), StandIn("app3")
    h3 = Hook(other.dir, "hook")
    off, since = _log_size(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        first.start_play()
        up = first.wait_client_up()
        B_ev["play_client_up"] = up
        require(up["up"], "./play's client never came up", output=first.output()[-2000:], **ev)
        stack_before, lock_before = stack.stack_processes(), play.LOCK.read_text().strip()
        B_ev["open"] = open_app(A.APP, h3, "--account", "test", "--client-cmd", str(other.bat))
        rc, dt = wait_app_exit(h3, A.APP, timeout=60)
        if rc is None and "[play] " not in _log_since(off):
            B_ev["tcc_prompts"] = tcc_prompts(since)
        time.sleep(2)
        B_ev |= {"app_exit_code": rc, "app_exit_after_s": dt, "events": h3.events(),
                 "app_client_started": other.started.exists(), "play_running_after": first.proc.poll() is None,
                 "play_client_after": play.client_processes(first.bat.name.lower()),
                 "stack_before": stack_before, "stack_after": stack.stack_processes(),
                 "lock_before": lock_before, "lock_after": play.LOCK.read_text().strip(),
                 "app_log": _log_since(off)[-2000:]}
        require(h3.find("dialog", "already_running"), "no \"already running\" message from the app", **ev)
        require(rc == 0, f"the app's exit code is {rc}, expected 0", **ev)
        require(not B_ev["app_client_started"], "the app started a client", **ev)
        require(B_ev["play_running_after"] and B_ev["play_client_after"], "./play or its client was affected", **ev)
        require(B_ev["stack_after"] == stack_before and B_ev["lock_after"] == lock_before == str(first.proc.pid),
                "the stack or the lock changed under ./play", **ev)
        first.stop_file.write_text("quit")
        rc1, _ = first.wait_exit(timeout=300)
        B_ev |= {"play_exit_code": rc1, "remaining_after": app_remaining(first)}
        require(rc1 == 0, f"./play exit code {rc1}, expected 0", **ev)
        require(is_clear(B_ev["remaining_after"]), "something from this repo is still running", **ev)
        return ev
    finally:
        stop_launchers(A.APP)
        other.cleanup()
        first.cleanup()


# app.relocatable: a scenario of a journey (journey_checks.py), not a check of its own (test-pyramid part 2)
def relocatable(ctx):
    """A copy of the built app in a temp folder outside dist/ (signature still valid) finds the repo and plays exactly
    as in app.launch_lifecycle: the copy's own launcher runs, starts the repo's play loop, and quits cleanly."""
    ev: dict = {}
    build_app(ev)
    work = Path(tempfile.mkdtemp(prefix="hs-app-moved-")).resolve()
    copy = work / "Elsewhere" / "Moonglade.app"
    try:
        copy.parent.mkdir()
        d = subprocess.run(["/usr/bin/ditto", str(A.APP), str(copy)], capture_output=True, text=True)
        v = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(copy)], capture_output=True, text=True)
        ev |= {"copy": str(copy), "ditto_rc": d.returncode,
               "copy_codesign_verify": {"rc": v.returncode, "output": (v.stdout + v.stderr).strip()},
               "copy_outside_dist": not copy.is_relative_to(A.DIST) and not copy.is_relative_to(P.ROOT)}
        require(d.returncode == 0 and v.returncode == 0 and ev["copy_outside_dist"],
                "couldn't make a valid copy outside the repo", **ev)
        lifecycle(copy, ev, "moved")
        ran_from = {os.path.realpath(e.get("bundle", "")) for e in ev.get("events", [])}
        ev["events_bundle_paths"] = sorted(ran_from)
        require(ran_from == {os.path.realpath(copy)}, "the events weren't recorded by the copy's launcher", **ev)
        require(ev["launchers_while_running"] and all(
            os.path.realpath(l["cmd"].split(" --", 1)[0]).startswith(os.path.realpath(copy) + "/")
            for l in ev["launchers_while_running"]), "the running launcher wasn't the copy", **ev)
        return ev
    finally:
        stop_launchers(copy)
        shutil.rmtree(work, ignore_errors=True)
