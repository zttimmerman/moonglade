"""The play client's default resolution (openspec/changes/launcher-fixes, client-connectivity; design.md "Resolution").

Why: with no `gxResolution` the 3.3.5a client opens a 1024x768 (4:3) window. In macOS full screen Wine resizes that
window to the screen, the client keeps its 4:3 back buffer and mtld3d scales the frame to the window: everything is
stretched sideways ("night elf wide"). The client sizes its window (and so its back buffer) with the aspect ratio of
`gxResolution`, so a value with the full-screen aspect ratio removes the stretch.

What the launcher does before each launch of a play client (never client/), in `launch.prepare`:
- reads the display the game opens on (the primary display, with the menu bar): its size in points, its Retina scale, and the strip a full-screen window doesn't
  get (the camera housing: `safeAreaInsets.top`). The full-screen area is what the game fills in macOS full screen.
- picks `<w>x<h>` with that area's aspect ratio (DEFAULT, measured in design.md: the area in Retina pixels; the
  client still renders at the size of the window it fits on screen, see design.md).
- writes it into the play client's WTF/Config.wtf only when the player hasn't chosen one: when `gxResolution` is
  absent, or still the value the launcher wrote itself last time (the marker WTF/hs-resolution.json, which then
  re-picks for a changed display). A different value is the player's own choice (e.g. in-game Video options) and is
  never touched.

Test-only: $HS_TEST_DISPLAY="<w>x<h>@<scale>[/<top>]" stands in for the real display; honoured only for a scratch
play client ($HS_PLAY_CLIENT_DIR), and ./play refuses it without --allow-test-client-dir.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import clientcfg

MARKER = "hs-resolution.json"              # in the play client's WTF/ (survives play-client rebuilds with WTF/)
KEY = "gxResolution"
TEST_ENV = "HS_TEST_DISPLAY"
DEFAULT = "native"                         # "native" (Retina pixels) or "half" (points); decided in design.md
_SPEC = re.compile(r"^(\d{3,5})x(\d{3,5})@(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?$")
# The screen the game opens on: Wine's primary monitor is the macOS primary display, the one with the menu bar
# (NSScreen.screens[0], i.e. CGMainDisplayID), not NSScreen.mainScreen (whichever screen has keyboard focus).
_JXA = ("ObjC.import('AppKit'); var all = $.NSScreen.screens; var s = all.objectAtIndex(0); var f = s.frame; "
        "var i = s.safeAreaInsets; JSON.stringify({w: f.size.width, h: f.size.height, scale: s.backingScaleFactor, "
        "top: i.top, screens: all.count, name: ObjC.unwrap(s.localizedName)})")


class ResolutionError(RuntimeError):
    pass


def parse_display(spec: str) -> dict:
    m = _SPEC.match(spec.strip())
    if not m:
        raise ResolutionError(f"display spec {spec!r} isn't <w>x<h>@<scale>[/<top>]")
    w, h, sc, top = m.groups()
    return {"points": [int(w), int(h)], "scale": float(sc), "top_inset": float(top or 0), "source": f"test {spec}"}


def display() -> dict:
    """The main display: {"points": [w, h], "scale", "top_inset", "source"}. $HS_TEST_DISPLAY (scratch play client
    only) replaces it."""
    spec = os.environ.get(TEST_ENV)
    if spec:
        if not os.environ.get("HS_PLAY_CLIENT_DIR"):
            raise ResolutionError(f"${TEST_ENV} is test-only (needs a scratch play client)")
        return parse_display(spec)
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", _JXA], capture_output=True, text=True, timeout=20)
        d = json.loads(r.stdout)
        return {"points": [int(d["w"]), int(d["h"])], "scale": float(d["scale"]), "top_inset": float(d.get("top") or 0),
                "source": "NSScreen.screens[0] (primary display)", "name": d.get("name"), "screens": int(d.get("screens") or 0)}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as e:
        raise ResolutionError(f"couldn't read the display: {e}") from None


def fullscreen_area(d: dict) -> tuple[float, float]:
    """Points a full-screen window gets: the screen minus the camera-housing strip at the top."""
    w, h = d["points"]
    return float(w), float(h) - float(d.get("top_inset") or 0)


def choose(d: dict, default: str = DEFAULT) -> str:
    """`<w>x<h>` with the full-screen area's aspect ratio: the area in points (half) or Retina pixels (native)."""
    w, h = fullscreen_area(d)
    k = float(d["scale"]) if default == "native" else 1.0
    return f"{int(round(w * k))}x{int(round(h * k))}"


def aspect(value: str) -> float | None:
    m = re.match(r"^(\d+)x(\d+)$", value or "")
    return int(m.group(1)) / int(m.group(2)) if m and int(m.group(2)) else None


def _config(client_dir: Path) -> Path:
    return Path(client_dir) / "WTF" / "Config.wtf"


def current(client_dir: Path) -> str | None:
    p = _config(client_dir)
    if not p.exists():
        return None
    m = None
    for m in re.finditer(rb'^\s*SET\s+gxResolution\s+"([^"]*)"', p.read_bytes(), re.I | re.M):
        pass                                # the client reads the last one
    return m.group(1).decode("latin-1") if m else None


def marker(client_dir: Path) -> dict | None:
    try:
        d = json.loads((Path(client_dir) / "WTF" / MARKER).read_text())
        return d if isinstance(d, dict) and isinstance(d.get("value") or d.get("pending"), str) else None
    except (OSError, ValueError):
        return None


def apply(client_dir: Path, d: dict | None = None, default: str = DEFAULT) -> dict:
    """Set the default resolution in a play client's Config.wtf unless the player chose one. Returns evidence:
    {"action": "set" | "repicked" | "kept_ours" | "kept_player", "value", "before", "display", ...}."""
    client_dir = Path(client_dir)
    if not (client_dir / "hs-build.json").is_file():
        raise ResolutionError(f"{client_dir} isn't a play client; the resolution default is only for play clients")
    d = d or display()
    want = choose(d, default)
    have, mk = current(client_dir), marker(client_dir)
    ev = {"display": d, "chosen": want, "before": have, "marker_before": mk, "default": default}
    ours = {mk.get("value"), mk.get("pending")} - {None} if mk else set()
    if have is not None and have not in ours:
        return ev | {"action": "kept_player", "value": have}
    mp = client_dir / "WTF" / MARKER
    mp.parent.mkdir(parents=True, exist_ok=True)

    def mark(**kw):                     # written whole (tmp + rename): a kill never leaves half a marker
        tmp = mp.with_name(mp.name + ".hs-tmp")
        tmp.write_text(json.dumps({"display": d, "default": default, "written": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                   **kw}, indent=2) + "\n")
        os.replace(tmp, mp)
    if have == want:
        action = "kept_ours"
    else:
        action = "set" if have is None else "repicked"
        # pending first: killed between the two writes, the next launch still knows both values are ours
        mark(value=have, pending=want)
        cfg = _config(client_dir)
        old = cfg.read_bytes() if cfg.exists() else b""
        new, _detail = clientcfg._update_config(old, {KEY: want})
        ev["write"] = clientcfg._write_if_changed(cfg, new)
    mark(value=want)
    return ev | {"action": action, "value": want}
