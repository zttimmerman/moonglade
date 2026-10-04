"""mac-app: Hearthstone.app (openspec/changes/launcher-app).

app.icon and app.bundle_valid inspect the committed icon and a fresh `harness/hs app` build. The runtime checks open
the built app the way Finder does, through LaunchServices (`/usr/bin/open`, never by exec'ing the binary), with the
play.* checks' stand-in client (`--account test --client-cmd <bat>`, passed via `open --args`) and the app's test
hook (`open --env HS_APP_TEST_HOOK=<file>`): the app then shows no window or dialog and records each status, dialog
and its exit code in that file. LaunchServices starts the app with launchd's environment, so the pinned profile
(HS_PROFILE=m0) goes through `open --env` too.

macOS privacy (TCC): the repo lives in ~/Documents, so the play loop the app starts needs the "Documents folder"
permission for Hearthstone.app. macOS asks once ("Hearthstone would like to access files in your Documents folder");
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
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .. import app as A, icon as I, launch, paths as P, play, procs, stack
from ..registry import check, require
from .play_checks import (PINNED, START_TIMEOUT, StandIn, _denials, graceful_halt, is_clear, last_record,
                          play_processes, remaining, SANDBOX_EXEC)

CAP = "mac-app"
HS = str(P.HARNESS / "hs")
OPEN = "/usr/bin/open"
APP_OUTPUT_TIMEOUT = 90       # the play loop prints its first line within ~1-2 s; 90 s = blocked (e.g. TCC prompt)
EXIT_TIMEOUT = 300            # play's own limit after the client exits is 120 s (play.loop_clean_exit)
TCC_HINT = ("the play loop never started: macOS is probably asking for Hearthstone's Documents-folder permission "
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


def transparency_report(w: int, h: int, px: bytes) -> dict:
    """The THEN of "Icon is the logo on a transparent background", measured on one decoded RGBA image.
    The ring's outer radius R is found from colour, independently of the alpha channel: the farthest pixel from the
    image centre that is opaque (alpha >= 128) and not near-white. Then: corners alpha 0; centre alpha 255; every
    pixel farther than R + 1.5 px has alpha 0; no near-white pixel with alpha >= 32 at or beyond R - 3 (a white
    surround or halo); and the disc inside R - 2 is fully opaque."""
    cx, cy = w / 2, h / 2
    R = 0.0
    for y in range(h):
        row = y * w * 4
        for x in range(w):
            i = row + x * 4
            if px[i + 3] >= 128 and not _near_white(px[i], px[i + 1], px[i + 2]):
                d = math.hypot(x + 0.5 - cx, y + 0.5 - cy)
                if d > R:
                    R = d
    outside_nonzero = halo = inside_holes = transparent = 0
    samples: dict = {"outside_nonzero": [], "halo": [], "inside_holes": []}
    for y in range(h):
        row = y * w * 4
        for x in range(w):
            i = row + x * 4
            a = px[i + 3]
            d = math.hypot(x + 0.5 - cx, y + 0.5 - cy)
            transparent += a == 0
            if d > R + 1.5 and a != 0:
                outside_nonzero += 1
                if len(samples["outside_nonzero"]) < 5:
                    samples["outside_nonzero"].append([x, y, list(px[i:i + 4])])
            if d >= R - 3 and a >= 32 and _near_white(px[i], px[i + 1], px[i + 2]):
                halo += 1
                if len(samples["halo"]) < 5:
                    samples["halo"].append([x, y, list(px[i:i + 4])])
            if d < R - 2 and a != 255:
                inside_holes += 1
                if len(samples["inside_holes"]) < 5:
                    samples["inside_holes"].append([x, y, list(px[i:i + 4])])
    corners = {f"{x},{y}": px[(y * w + x) * 4 + 3] for x, y in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))}
    centre_alpha = px[(h // 2 * w + w // 2) * 4 + 3]
    rep = {"size": [w, h], "ring_radius_from_colour_px": round(R, 2), "corner_alpha": corners,
           "centre_alpha": centre_alpha, "outside_ring_nonzero_alpha": outside_nonzero,
           "near_white_at_or_outside_ring": halo, "inside_disc_not_opaque": inside_holes,
           "transparent_fraction": round(transparent / (w * h), 4), "samples": samples}
    rep["ok"] = (all(a == 0 for a in corners.values()) and centre_alpha == 255 and outside_nonzero == 0
                 and halo == 0 and inside_holes == 0 and 0.25 * w <= R < 0.5 * w)
    return rep


def verify_icon(png_path: Path, icns_path: Path, regenerate: bool = True) -> dict:
    """Evidence for app.icon; raises CheckFail on the first unmet clause."""
    ev: dict = {"png": str(png_path), "icns": str(icns_path)}
    require(png_path.is_file() and icns_path.is_file(), "icon files missing", **ev)
    data = icns_path.read_bytes()
    sizes = icns_sizes(data)
    ev["icns_sizes"] = sizes
    bad = {t: s for t, s in sizes.items() if s["actual"] != [s["expected"], s["expected"]]}
    require(not bad, f"icns lacks standard sizes or has wrong dimensions: {sorted(bad)}", **ev)
    w, h, px = I.read_png(I.icns_entries(data)["ic10"])
    ev["icns_1024"] = transparency_report(w, h, px)
    require(ev["icns_1024"]["ok"], "the .icns 1024 px image isn't the logo on a transparent background", **ev)
    w2, h2, px2 = I.read_png(png_path.read_bytes())
    ev["png"] = transparency_report(w2, h2, px2)
    require(ev["png"]["ok"] and (w2, h2) == (1024, 1024),
            "the cleaned PNG isn't a 1024 px logo on a transparent background", **ev)
    if regenerate:
        tmp = Path(tempfile.mkdtemp(prefix="hs-icon-regen-"))
        try:
            require(I.SOURCE.is_file(), f"logo source {I.SOURCE} is missing, so reproducibility can't be shown", **ev)
            meta = I.generate(out_dir=tmp)
            committed_meta = json.loads(I.META_OUT.read_text()) if I.META_OUT.exists() else {}
            rw, rh, rpx = I.read_png((tmp / I.PNG_OUT.name).read_bytes())
            diff = sum(1 for i in range(0, len(rpx), 4) if rpx[i:i + 4] != px2[i:i + 4]) if len(rpx) == len(px2) else -1
            ev["regenerated"] = {"dir": str(tmp), "ring_source_px": meta["ring_source_px"],
                                 "source_sha256": meta["source_sha256"],
                                 "committed_source_sha256": committed_meta.get("source_sha256"),
                                 "pixels_differing": diff,
                                 "png_bytes_identical": (tmp / I.PNG_OUT.name).read_bytes() == png_path.read_bytes()}
            require(meta["source_sha256"] == committed_meta.get("source_sha256"),
                    "the local logo source differs from the one the committed icon was made from", **ev)
            require((rw, rh) == (w2, h2) and diff == 0, "re-running the generator doesn't reproduce the committed PNG", **ev)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return ev


@check("app.icon", order=380, capability=CAP, stack="any", tier="quick")
def app_icon(ctx):
    """branding/AppIcon.icns has every standard size 16-1024 px (incl. @2x); its 1024 px image and the cleaned PNG have
    transparent corners and surround (alpha 0 beyond the ring), an opaque centre and no near-white surround or halo;
    re-running the generator on the logo source reproduces the committed PNG pixel for pixel."""
    return verify_icon(I.PNG_OUT, I.ICNS_OUT)


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


@check("app.bundle_valid", order=381, capability=CAP, stack="any", tier="quick")
def bundle_valid(ctx):
    """From a clean checkout with the committed branding assets, `harness/hs app` (run under a sandbox profile that
    denies and logs every file write outside the repo) builds dist/Hearthstone.app: plutil -lint passes; the plist
    names the executable (present, executable), the bundle identifier and the icon; codesign --verify --deep --strict
    passes on an ad-hoc signature; and no write outside the repo was attempted."""
    ev: dict = {"cmd": f"{SANDBOX_EXEC} -f <guard.sb> {HS} app"}
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
        time.sleep(3)                                     # let the kernel's sandbox reports reach the log
        rows = _denials(tag, since)
        ev["denials"] = {"selftest": [r for r in rows if r["time"] and r["time"][:19].replace("T", " ") < build_since],
                         "build": [r for r in rows if r["time"] and r["time"][:19].replace("T", " ") >= build_since]}
        require(any("outside-write-probe" in r["target"] for r in ev["denials"]["selftest"]),
                "self-test: the blocked write isn't in the denial log, so the log can't vouch for the build", **ev)
        require(not ev["denials"]["build"], "the build tried to write outside the repo", **ev)
        require(b.returncode == 0, "`harness/hs app` failed", **ev)
        require(A.APP.is_dir(), "dist/Hearthstone.app doesn't exist after the build", **ev)
        verify_bundle(A.APP, ev)
        ev["build_window_started"] = t0.isoformat()
        return ev
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- runtime helpers

def _launchers(bundle: Path | None = None) -> list[dict]:
    """Running Hearthstone.app launchers (any copy, or the one at `bundle`)."""
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


def open_app(bundle: Path, hook: Hook, *args: str) -> dict:
    cmd = [OPEN, "--env", f"{A.HOOK_ENV}={hook.path}", *[x for k, v in PINNED.items() for x in ("--env", f"{k}={v}")],
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
        idx = text.find(f"==== Hearthstone.app pid {launcher_pid} ")
        text = text[idx:] if idx >= 0 else ""
    m = pat.search(text)
    return int(m.group(1)) if m else None


def tcc_prompts(since: str) -> list[str]:
    r = subprocess.run(["/usr/bin/log", "show", "--start", since, "--predicate",
                        'process == "tccd" AND eventMessage CONTAINS "AUTHREQ_PROMPTING" '
                        f'AND eventMessage CONTAINS "{A.BUNDLE_ID}"'], capture_output=True, text=True, timeout=120)
    return [l[:400] for l in r.stdout.splitlines() if "AUTHREQ_PROMPTING" in l]


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
    while _launchers(bundle) and time.time() - t0 < 180:
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
        ev["open"] = open_app(bundle, hook, "--account", "test", "--client-cmd", str(s.bat))
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
        return ev
    finally:
        stop_launchers(bundle)
        s.cleanup()


# ---------------------------------------------------------------- runtime checks

@check("app.launch_lifecycle", order=382, capability=CAP, stack="down", tier="full")
def launch_lifecycle(ctx):
    """`open dist/Hearthstone.app` (stand-in client): a "starting" status is recorded, the stack reaches readiness, the
    client is launched by the app's play loop (the app's child, the app launched by LaunchServices, no Terminal),
    and when the client exits the stack shuts down gracefully, the app exits 0 with no dialog, and nothing from this
    repo (stack, Wine, Wow.exe, launcher) remains, ports 3307/3724/8085/7878 free."""
    ev: dict = {"bundle": str(A.APP)}
    build_app(ev)
    return lifecycle(A.APP, ev, "app")


@check("app.startup_failure", order=383, capability=CAP, stack="down", tier="full")
def startup_failure(ctx):
    """With an unusable worldserver DataDir, the opened app never starts the client, records an error dialog naming
    the problem (the play loop's error and the server's own cause) and the app.log path, exits nonzero, and leaves
    nothing running."""
    bad = "/nonexistent/hs-app-failfast"
    ev: dict = {"bundle": str(A.APP), "world_datadir": bad}
    build_app(ev)
    require_clear(ev)
    s = StandIn("appfail")
    hook = Hook(s.dir, "hook")
    off, since = _log_size(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    wine_off = P.WINE_LOG.stat().st_size if P.WINE_LOG.exists() else 0
    try:
        ev["open"] = open_app(A.APP, hook, "--account", "test", "--client-cmd", str(s.bat), "--world-datadir", bad)
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
        ev |= {"app_exit_code": rc, "elapsed_s": round(time.time() - t0, 1), "events": hook.events(),
               "play_record": {k: rec.get(k) for k in ("outcome", "error", "client_started", "exit_code")},
               "client_start_stamps_in_wine_log": launches, "stand_in_started_marker": s.started.exists(),
               "remaining_after": app_remaining(s), "app_log": _log_since(off)[-3000:]}
        require(rc is not None and rc != 0, f"the app's exit code is {rc}, expected nonzero", **ev)
        require(errors, "no error dialog was shown", **ev)
        require(rec.get("outcome") == "startup_failed" and rec.get("client_started") is False and rec.get("error"),
                "the play loop didn't record a startup failure before the client", **ev)
        require(rec["error"] in text, "the error dialog doesn't name the play loop's error", dialog=text, **ev)
        require("Failed to find map files" in text, "the error dialog doesn't give the server's cause", dialog=text, **ev)
        require(str(A.LOG) in text, "the error dialog doesn't give the log path", dialog=text, **ev)
        require(not s.started.exists() and not launches, "the client was started despite the failed startup", **ev)
        require(is_clear(ev["remaining_after"]), "something from this repo is still running", **ev)
        return ev
    finally:
        stop_launchers(A.APP)
        s.cleanup()


@check("app.single_instance", order=384, capability=CAP, stack="down", tier="full")
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
        A_ev["open_second"] = open_app(A.APP, h2, "--account", "test", "--client-cmd", str(a2.bat))
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


@check("app.relocatable", order=385, capability=CAP, stack="down", tier="full")
def relocatable(ctx):
    """A copy of the built app in a temp folder outside dist/ (signature still valid) finds the repo and plays exactly
    as in app.launch_lifecycle: the copy's own launcher runs, starts the repo's play loop, and quits cleanly."""
    ev: dict = {}
    build_app(ev)
    work = Path(tempfile.mkdtemp(prefix="hs-app-moved-")).resolve()
    copy = work / "Elsewhere" / "Hearthstone.app"
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
