"""Helpers for the play-client and login-screen checks (openspec/changes/in-client-login).

PlayRun drives the real `./play` with the real Wow.exe (no stand-in) against a scratch play client
(env/check-play-<tag>/, an APFS clone, rebuilt fresh per run) and the harness test account HSTEST, never the player's
account. The GlueXML test hook ($HS_GLUE_TEST) scripts actions inside the client (one Continue click, screenshots,
entering the world); nothing injects keystrokes.

Marker squares: with {"marker": true} the client draws four solid squares top right (HSFrontMenu.lua,
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
from ..registry import require
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


# Settings every real-client run on a scratch play client needs, seeded by the harness itself (easy-setup 1.9).
# Without them the client plays the intro movie and then shows the EULA and Terms of Use screens before the login
# screen (AccountLogin.lua AccountLogin_ShowUserAgreements: `not EULAAccepted()` / `not TOSAccepted()`, enUS
# patch-enUS-3.MPQ; the other notices are off in enUS, GlueLocalization.lua), so the front menu never logs in. Until
# 2026-10-07 the checks got them silently from client/WTF, which a play-client build carries over; measured by B30:
# with client/WTF gone the run blocked, with these three seeded it reached the world. hwDetect "0": a client with no
# saved settings runs its hardware detection at the first start and replaces the video settings (measured B31:
# client.resolution_default's back buffer stayed 1024x768 despite the gxResolution the launch wrote; with hwDetect "0" it
# took it). Plus realmName (the realm's name, realm_name()): without it the client shows the realm list after the
# login instead of character select (measured B31: login.friendly_errors' Try again sat in COP_GET_REALMS polling).
# Since B32 the play-client build itself adds them where absent (clientcfg.FIRST_START_SETTINGS, the same values);
# seed_settings(fresh=True) still drops the carried-over WTF and seeds them, so a check never depends on client/WTF.
from ..clientcfg import FIRST_START_SETTINGS as SEED_SETTINGS  # noqa: E402


def _is_scratch(d: Path) -> bool:
    d = Path(d)
    return d.parent.resolve() == P.ENV.resolve() and d.name.startswith("check-play-")


def seed_settings(client_dir: Path, fresh: bool = False) -> dict:
    """Write SEED_SETTINGS into a scratch play client's WTF/Config.wtf (scratch clients only: env/check-play-*;
    never client/ or env/client-play/). fresh=True first drops the WTF folder a build carried over from client/, so
    the run doesn't depend on whatever client/WTF holds."""
    from .. import clientcfg
    client_dir = Path(client_dir)
    require(_is_scratch(client_dir), "refusing to seed settings outside a scratch play client", dir=str(client_dir))
    wtf = client_dir / "WTF"
    out: dict = {}
    if fresh and wtf.exists():
        out["dropped_carried_over"] = sorted(str(p.relative_to(wtf)) for p in wtf.rglob("*"))[:20]
        shutil.rmtree(wtf)
    cfg = wtf / "Config.wtf"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    realm = realm_name()
    new, detail = clientcfg._update_config(cfg.read_bytes() if cfg.exists() else b"",
                                           {**SEED_SETTINGS, "realmName": realm["name"]})
    cfg.write_bytes(new)
    # the test account has played before: its account- and character-wide SavedVariables files exist (empty when
    # new). On a first login the client logs "Error loading WTF\Account\...\SavedVariables.lua" for each into
    # FrameXML.log, which the checks require to be empty; until 2026-10-07 these came from client/WTF too.
    sv = []
    for f in (wtf / "Account" / P.TEST_USER / "SavedVariables.lua",
              wtf / "Account" / P.TEST_USER / realm["name"] / P.TEST_CHAR / "SavedVariables.lua"):
        if not f.exists():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"")
            sv.append(str(f.relative_to(client_dir)))
    return out | {"config": str(cfg.relative_to(client_dir)), "settings": dict(SEED_SETTINGS), **detail,
                  "saved_variables_created": sv, "realm": realm}


def realm_name() -> dict:
    """Realm 1's name (clientcfg.realm_name: the auth database when it's up, else AzerothCore's base SQL)."""
    from ..clientcfg import realm_name as _rn
    r = _rn()
    require(r.get("name"), "can't find realm 1's name", realm=r)
    return r


def build_scratch(dest: Path, **kw) -> dict:
    """clientbuild.build into a scratch play client, then its settings seeded from nothing (seed_settings fresh)."""
    from .. import clientbuild
    b = clientbuild.build(dest=dest, **kw)
    b["seeded"] = seed_settings(dest, fresh=True)
    return b


def seeded_given(client_dir: Path) -> dict:
    """The seeded settings as the scratch's Config.wtf holds them now: {"values", "missing"}."""
    import re
    cfg = Path(client_dir) / "WTF" / "Config.wtf"
    text = cfg.read_text(errors="replace") if cfg.exists() else ""
    values = {}
    for k in (*SEED_SETTINGS, "realmName"):
        m = re.findall(rf'^\s*SET\s+{k}\s+"([^"]*)"', text, re.M | re.I)
        values[k] = m[-1] if m else None
    missing = sorted(k for k, v in SEED_SETTINGS.items() if values[k] != v) + ([] if values["realmName"] else ["realmName"])
    return {"config_exists": cfg.exists(), "values": values, "missing": missing}


def require_seeded(client_dir: Path, ev: dict | None = None) -> dict:
    """GIVEN of every real-client scratch run: the settings that keep the intro movie and the EULA/TOS screens from
    blocking the login screen are in place. Fails naming the missing ones (instead of a timeout much later)."""
    g = seeded_given(client_dir)
    require(not g["missing"], "GIVEN not met: the scratch play client's WTF/Config.wtf lacks "
            + ", ".join(f'{k} "{SEED_SETTINGS.get(k, "<realm name>")}"' for k in g["missing"])
            + " (without them the intro movie and the EULA/Terms of Use screens block the login screen, hardware "
              "detection replaces the video settings, or the realm list comes up instead of character select)",
            seeded_given=g, **(ev or {}))
    return g


class PlayRun:
    def __init__(self, tag: str, mode: str = "patched", glue_test: dict | None = None, scratch: Path | None = None,
                 extra_env: dict | None = None, seed: bool = True):
        self.tag = tag
        self.seed = seed      # False: the scratch keeps the settings its build wrote (client.first_start_settings)
        self.tmp = Path(tempfile.mkdtemp(prefix=f"hs-pc-{tag}-")).resolve()
        self.settings = self.tmp / "settings.json"
        self.settings.write_text(json.dumps({"client": mode}))
        self.out = self.tmp / "play.out"
        self.scratch = scratch
        from .play_checks import warm_env
        extra_env = {**warm_env(), **(extra_env or {})}       # a warm journey: the shared stack stays up
        self.env = {**os.environ, **PINNED, "HS_SETTINGS": str(self.settings), **extra_env}
        self.env.pop("HS_PLAY_CLIENT_DIR", None)
        self.env.pop("HS_GLUE_TEST", None)
        if scratch:
            self.env["HS_PLAY_CLIENT_DIR"] = str(scratch)
        if glue_test is not None:
            self.env["HS_GLUE_TEST"] = json.dumps(glue_test)
        # test-pyramid warm journeys: ./play leaves the shared stack up (HS_PLAY_KEEP_STACK), and so does cleanup()
        self.keep_stack = bool((extra_env or {}).get("HS_PLAY_KEEP_STACK"))
        self.proc: subprocess.Popen | None = None
        self.t0 = 0.0
        self.seeded = self.given = None

    def start(self) -> None:
        if self.scratch:
            # GIVEN (easy-setup 1.9): the run establishes its own client settings; nothing comes from client/WTF.
            # An unbuilt scratch is built here (the play loop then finds it current); a built one gets the settings
            # (idempotent: the same lines are set again).
            from .. import clientbuild
            if not self.seed:
                require(clientbuild.read_stamp(self.scratch) is not None, "PlayRun(seed=False) needs a built scratch",
                        scratch=str(self.scratch))
                self.seeded = {"skipped": "seed=False: the settings come from the play-client build"}
            elif clientbuild.read_stamp(self.scratch) is None:
                self.seeded = build_scratch(self.scratch)["seeded"]
            else:
                self.seeded = seed_settings(self.scratch)
            self.given = require_seeded(self.scratch)
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
        """Stop everything; remove the generated login file even when ./play was killed before its own cleanup."""
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait()
        if launch.prefix_processes():
            launch.stop_prefix()
        if any(stack.stack_processes().values()) and not self.keep_stack:
            stack.down()
        from .. import clientbuild, frontmenu
        target = self.scratch or clientbuild.PLAY
        if (target / "hs-build.json").is_file():
            self.removed_profiles = frontmenu.remove(target)
        shutil.rmtree(self.tmp, ignore_errors=True)


def drop_scratch(d: Path | None) -> None:
    """Delete a scratch play client when a check is done (APFS clones, but the disk is tight)."""
    if d and d.name.startswith("check-play-") and d.parent == P.ENV:
        shutil.rmtree(d, ignore_errors=True)


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
    """Decode the four marker squares (HSTestMarker is anchored TOPRIGHT -10,-10, 200x40 UI units, 40x40 squares at
    x = 0 / 53 / 106 / 159 from its left edge). The UI is 768 units high (no UI scale), so a unit is h/768 pixels:
    1:1 in the client's old default 1024x768 window, larger since launcher-fixes gives the play client a resolution
    with the display's aspect ratio."""
    w, h, px = _bmp_pixels(path)
    u = h / 768
    cy = round((10 + 20) * u)
    left = w - (10 + 200) * u
    pts = {k: round(left + (i * 53 + 20) * u) for i, k in enumerate(("backdrop", "music", "menu", "errors"))}
    raw = {k: _avg(px, x, cy, r=max(3, int(6 * u))) for k, x in pts.items()}
    cls = {k: _classify(v) for k, v in raw.items()}
    out = {"file": path.name, "size": [w, h], "rgb": raw, "colors": cls}
    out["backdrop"] = BACKDROP_KEYS[COLOR_INDEX[cls["backdrop"]]] if cls["backdrop"] in COLOR_INDEX else None
    out["music"] = BACKDROP_KEYS[COLOR_INDEX[cls["music"]]] if cls["music"] in COLOR_INDEX else None
    out["main_menu_up"] = {"white": True, "magenta": False}.get(cls["menu"])
    out["lua_errors"] = {"white": 0, "red": ">=1"}.get(cls["errors"])        # None = square not readable
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
