"""Helpers for the play-client and login-screen checks (openspec/changes/in-client-login).

PlayRun drives the real `./play` with the real Wow.exe (no stand-in) against a scratch play client
(env/check-play-<tag>/, an APFS clone, rebuilt fresh per run) and the harness test account HSTEST, never the player's
account. The GlueXML test hook ($HS_GLUE_TEST) scripts actions inside the client (one Continue click, screenshots,
entering the world); nothing injects keystrokes.

Marker squares: with {"marker": true} the client draws three solid squares top right (HSFrontMenu.lua,
HSFrontMenu_UpdateMarker); `marker()` reads them back from the client's own screenshot.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import struct
import subprocess
import tempfile
import time
from pathlib import Path

from .. import launch, paths as P, play, stack
from .play_checks import PLAY, remaining

PINNED = {"HS_PROFILE": "m0"}
START_TIMEOUT = 900
BACKDROP_KEYS = ("WOTLK", "TBC", "VANILLA")      # HS_BACKDROPS order in HSFrontMenu.lua (marker colour index)


def scratch_dir(tag: str) -> Path:
    return P.ENV / f"check-play-{tag}"


def fresh_scratch(tag: str) -> Path:
    d = scratch_dir(tag)
    shutil.rmtree(d, ignore_errors=True)
    return d


class PlayRun:
    def __init__(self, tag: str, mode: str = "patched", glue_test: dict | None = None, scratch: Path | None = None,
                 extra_env: dict | None = None):
        self.tag = tag
        self.tmp = Path(tempfile.mkdtemp(prefix=f"hs-pc-{tag}-")).resolve()
        self.settings = self.tmp / "settings.json"
        self.settings.write_text(json.dumps({"client": mode}))
        self.out = self.tmp / "play.out"
        self.scratch = scratch
        self.env = {**os.environ, **PINNED, "HS_SETTINGS": str(self.settings), **(extra_env or {})}
        self.env.pop("HS_PLAY_CLIENT_DIR", None)
        self.env.pop("HS_GLUE_TEST", None)
        if scratch:
            self.env["HS_PLAY_CLIENT_DIR"] = str(scratch)
        if glue_test is not None:
            self.env["HS_GLUE_TEST"] = json.dumps(glue_test)
        self.proc: subprocess.Popen | None = None
        self.t0 = 0.0

    def start(self) -> None:
        self.t0 = time.time()
        fh = open(self.out, "w")
        self.proc = subprocess.Popen([PLAY, "--account", "test", "--allow-test-client-dir"], cwd=P.ROOT,
                                     stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                     start_new_session=True, env=self.env)
        fh.close()

    def wait_client(self, timeout: float = START_TIMEOUT) -> dict:
        """Until the real Wow.exe process is visible (or ./play exits). Returns {"up", "after_s", "wow"}."""
        t = time.time()
        while time.time() - t < timeout:
            if self.proc.poll() is not None:
                return {"up": False, "why": f"./play exited with {self.proc.returncode}", "output": self.output()[-2500:]}
            w = launch.wow_processes()
            if w:
                return {"up": True, "after_s": round(time.time() - self.t0, 1), "wow": w, "t": time.time()}
            time.sleep(1)
        return {"up": False, "why": "timeout"}

    def wow_alive(self) -> list:
        return launch.wow_processes()

    def stop(self, timeout: float = 300) -> dict:
        """SIGTERM ./play (as the app's Quit does) and wait for its cleanup."""
        if not self.proc or self.proc.poll() is not None:
            return {"already_exited": self.proc.returncode if self.proc else None}
        os.killpg(self.proc.pid, signal.SIGTERM)
        try:
            rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            rc = None
        return {"exit_code": rc}

    def wait_exit(self, timeout: float = 300):
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def record(self) -> dict:
        try:
            d = json.loads(play.LAST.read_text())
        except (FileNotFoundError, ValueError):
            return {}
        return d if self.proc and d.get("pid") == self.proc.pid else {}

    def output(self) -> str:
        try:
            return self.out.read_text(errors="replace")
        except FileNotFoundError:
            return ""

    def cleanup(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait()
        if launch.prefix_processes():
            launch.stop_prefix()
        if any(stack.stack_processes().values()):
            stack.down()
        shutil.rmtree(self.tmp, ignore_errors=True)


def leftovers() -> dict:
    return remaining()


def db_ready() -> bool:
    from .. import db
    try:
        return db.scalar("SELECT 1", "auth") == "1"
    except Exception:  # noqa: BLE001
        return False


def account_state(name: str) -> dict:
    """auth.account last_login (epoch) and online flag for `name` (read-only)."""
    from .. import db
    try:
        rows = db.query(f"SELECT UNIX_TIMESTAMP(last_login), online FROM account WHERE username='{name.upper()}'",
                        "auth")
    except db.SQLError as e:          # e.g. the play loop already shut the stack down after the client quit
        return {"exists": None, "error": str(e)[:200]}
    if not rows:
        return {"exists": False}
    ll, online = rows[0]
    return {"exists": True, "last_login": None if ll in ("NULL", "") else int(float(ll)), "online": int(online)}


# ---------------------------------------------------------------- screenshots and the marker squares

def screenshots(client_dir: Path) -> list[Path]:
    d = client_dir / "Screenshots"
    return sorted(d.glob("*.jpg"), key=lambda p: p.stat().st_mtime) if d.is_dir() else []


def _bmp_pixels(path: Path) -> tuple[int, int, callable]:
    tmp = Path(tempfile.mkdtemp(prefix="hs-shot-")) / "s.bmp"
    r = subprocess.run(["/usr/bin/sips", "-s", "format", "bmp", str(path), "--out", str(tmp)], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"sips failed on {path}: {r.stderr[-300:]}")
    b = tmp.read_bytes()
    shutil.rmtree(tmp.parent, ignore_errors=True)
    off, w, h, bpp = struct.unpack_from("<I", b, 10)[0], *struct.unpack_from("<iiHH", b, 18)[:2], \
        struct.unpack_from("<H", b, 28)[0]
    bottom_up = h > 0
    h = abs(h)
    stride = ((w * bpp // 8) + 3) & ~3

    def px(x, y):                  # y from the top
        row = (h - 1 - y) if bottom_up else y
        o = off + row * stride + x * (bpp // 8)
        bl, g, rr = b[o], b[o + 1], b[o + 2]
        return rr, g, bl
    return w, h, px


def _avg(px, cx, cy, r=6):
    s = [0, 0, 0]
    n = 0
    for x in range(cx - r, cx + r + 1):
        for y in range(cy - r, cy + r + 1):
            p = px(x, y)
            for i in range(3):
                s[i] += p[i]
            n += 1
    return tuple(round(v / n) for v in s)


def _classify(rgb) -> str:
    r, g, b = rgb
    hi, lo = 170, 90
    if r > 200 and g > 200 and b > 200:
        return "white"
    if r > hi and b > hi and g < lo:
        return "magenta"
    if r > hi and g < lo and b < lo:
        return "red"
    if g > hi and r < lo and b < lo:
        return "green"
    if b > hi and r < lo and g < lo:
        return "blue"
    if r < 50 and g < 50 and b < 50:
        return "black"
    return "unclear"


COLOR_INDEX = {"red": 0, "green": 1, "blue": 2}


def marker(path: Path) -> dict:
    """Decode the three marker squares (UI scale 1:1 at the client's 1024x768 window; HSTestMarker is anchored
    TOPRIGHT -10,-10, 150x40, squares LEFT / CENTER / RIGHT)."""
    w, h, px = _bmp_pixels(path)
    cy = 10 + 20
    pts = {"backdrop": w - 10 - 150 + 20, "music": w - 10 - 75, "menu": w - 10 - 20}
    raw = {k: _avg(px, x, cy) for k, x in pts.items()}
    cls = {k: _classify(v) for k, v in raw.items()}
    out = {"file": path.name, "size": [w, h], "rgb": raw, "colors": cls}
    out["backdrop"] = BACKDROP_KEYS[COLOR_INDEX[cls["backdrop"]]] if cls["backdrop"] in COLOR_INDEX else None
    out["music"] = BACKDROP_KEYS[COLOR_INDEX[cls["music"]]] if cls["music"] in COLOR_INDEX else None
    out["main_menu_up"] = {"white": True, "magenta": False}.get(cls["menu"])
    return out


def glue_logs(client_dir: Path) -> dict:
    out = {}
    for f in ("GlueXML.log", "FrameXML.log"):
        p = client_dir / "Logs" / f
        out[f] = p.read_text(errors="replace") if p.exists() else None
    return out


def clear_logs(client_dir: Path) -> None:
    for f in ("GlueXML.log", "FrameXML.log", "connection.log"):
        (client_dir / "Logs" / f).unlink(missing_ok=True)


def connection_log(client_dir: Path) -> str:
    p = client_dir / "Logs" / "connection.log"
    return p.read_text(errors="replace") if p.exists() else ""
