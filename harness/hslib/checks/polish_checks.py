"""launcher-polish: one Dock icon (ADR-0010) and the staged loading window (openspec/changes/launcher-polish).

GUI stand-in: Wine's built-in notepad, started by a .bat outside client/ (`--client-cmd`) under the same runtime,
prefix and shim as the game. Never Wow.exe in the Dock checks (app.shim_perf is the one check that runs the real
client, by design). Dock state comes from LaunchServices itself (`lsappinfo`: Foreground = a Dock icon, UIElement =
none); the frontmost app from `lsappinfo front`; windows from the window server (launch.all_onscreen_windows). None
of these needs a macOS permission. Focus checks need an unlocked GUI session: a locked screen (loginwindow in front)
is recorded and fails the GIVEN.

Shared sessions: the app checks share one app run (population profile, stack down, notepad), the play checks share
one terminal ./play run (m0, stack down, notepad). Each check asserts its own clause on that run's evidence; the
session runs once per harness process (again for each `--only` invocation), so evidence is always from this run.
"""
from __future__ import annotations

import ast
import json
import os
import re
import statistics
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from .. import app as A, config, dockshim, launch, paths as P, play, procs, stack, stages as S
from ..registry import CheckFail, check, require
from .app_checks import (Hook, OPEN, _launchers, _log_since, _log_size, _play_pid, app_remaining, build_app, open_app,
                         require_clear, stop_launchers, tcc_prompts, wait_app_exit)
from .play_checks import PINNED, StandIn, is_clear, last_record, remaining

CAP_APP = "mac-app"
CAP_PLAY = "play-launcher"
RUNTIME = P.WINE_BIN.parent                     # env/wowsilicon-r17/.wine-runtime
RUNTIME_DIR = P.ENV / "wowsilicon-r17"
SAMPLE_HZ_MIN = 10
AFTER_WINDOW_S = 5.0
FOCUS_LIMIT_S = 2.0
NOTHING_ON_SCREEN_S = 0.3     # hand-off: longest moment with neither the loading window nor the game window on screen
# [play] lines as they were before launcher-polish (main at 1ba1d3b): every line a run prints must match one of
# these say() templates, so the terminal output is unchanged
PLAY_LINES_BASE = "1ba1d3b"
PLAY_LINES_FILES = ("harness/hslib/play.py", "harness/hslib/launch.py")


# ---------------------------------------------------------------- environment probes

def screen_locked() -> bool:
    out = subprocess.run(["/usr/sbin/ioreg", "-n", "Root", "-d1", "-a"], capture_output=True, text=True).stdout
    m = re.search(r"<key>CGSSessionScreenIsLocked</key>\s*<(true|false)/>", out)
    c = re.search(r"<key>IOConsoleLocked</key>\s*<(true|false)/>", out)
    return bool((m and m.group(1) == "true") or (c and c.group(1) == "true"))


def display_asleep() -> bool | None:
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e",
                        'ObjC.import("CoreGraphics"); $.CGDisplayIsAsleep($.CGMainDisplayID())'],
                       capture_output=True, text=True, timeout=20)
    v = r.stdout.strip()
    return None if v not in ("0", "1", "true", "false") else v in ("1", "true")


def display_state() -> dict:
    """The GIVEN of GUI measurements: screen unlocked and display awake (a locked screen changes how much the game
    renders: Wow.exe's login-screen CPU was ~92 % locked vs ~77 % unlocked in the 2026-10-05 verifier run)."""
    st = {"t": round(time.time(), 3), "locked": screen_locked(), "display_asleep": display_asleep()}
    st["ok"] = st["locked"] is False and st["display_asleep"] is False
    return st


def lsappinfo_list() -> list[dict]:
    """Every app LaunchServices knows: name, pid, type (Foreground/UIElement/BackgroundOnly), executable, bundle id."""
    out = subprocess.run(["/usr/bin/lsappinfo", "list"], capture_output=True, text=True, timeout=10).stdout
    apps, cur = [], None
    for line in out.splitlines():
        m = re.match(r'\s*\d+\) "(.*)" ASN:', line)
        if m:
            cur = {"name": m.group(1)}
            apps.append(cur)
            continue
        if cur is None:
            continue
        m = re.match(r'\s*bundleID=(?:"(.*)"|\[ NULL \])', line)
        if m:
            cur["bundle"] = m.group(1)
        m = re.match(r'\s*executable path="(.*)"', line)
        if m:
            cur["exe"] = m.group(1)
        m = re.match(r'\s*pid = (\d+) .*?type="(\w+)"', line)
        if m:
            cur["pid"], cur["type"] = int(m.group(1)), m.group(2)
    return [a for a in apps if "pid" in a]


def frontmost_pid() -> int | None:
    asn = subprocess.run(["/usr/bin/lsappinfo", "front"], capture_output=True, text=True, timeout=10).stdout.strip()
    if not asn:
        return None
    out = subprocess.run(["/usr/bin/lsappinfo", "info", "-only", "pid", asn], capture_output=True, text=True,
                         timeout=10).stdout
    m = re.search(r'"pid"=(\d+)', out)
    return int(m.group(1)) if m else None


def is_ours(a: dict) -> bool:
    return str(a.get("exe", "")).startswith(str(RUNTIME) + "/") or a.get("bundle") == A.BUNDLE_ID


class LsSampler:
    """lsappinfo list as fast as it goes (>= 10 Hz needed), keeping this runtime's Wine processes and our launcher.
    Two staggered worker threads, so one slow lsappinfo call (Wine start-up load) doesn't leave a gap."""

    WORKERS = 2
    PERIOD = 0.06

    def __init__(self):
        self.samples: list[dict] = []
        self.stop_ev = threading.Event()
        self.lock = threading.Lock()
        self.threads = [threading.Thread(target=self._run, args=(i * self.PERIOD / self.WORKERS,), daemon=True,
                                         name=f"ls-sampler-{i}") for i in range(self.WORKERS)]

    def _run(self, offset: float):
        if self.stop_ev.wait(offset):
            return
        while not self.stop_ev.is_set():
            t = time.time()
            try:
                apps = [a for a in lsappinfo_list() if is_ours(a)]
            except Exception as e:  # noqa: BLE001
                apps = [{"error": str(e)}]
            with self.lock:
                self.samples.append({"t": round(t, 3), "apps": apps})
            time.sleep(max(0.0, self.PERIOD - (time.time() - t)))

    def start(self):
        for th in self.threads:
            th.start()

    def stop(self):
        self.stop_ev.set()
        for th in self.threads:
            if th.is_alive():
                th.join(timeout=5)
        with self.lock:
            self.samples.sort(key=lambda s: s["t"])


class Sampler(threading.Thread):
    """Calls fn() every `every` seconds, keeping (t, value) pairs."""

    def __init__(self, fn, every: float):
        super().__init__(daemon=True)
        self.fn, self.every = fn, every
        self.samples: list[tuple[float, object]] = []
        self.stop_ev = threading.Event()

    def run(self):
        while not self.stop_ev.is_set():
            t = time.time()
            try:
                v = self.fn()
            except Exception:  # noqa: BLE001
                v = None
            self.samples.append((round(t, 3), v))
            self.stop_ev.wait(self.every)

    def stop(self):
        self.stop_ev.set()
        self.join(timeout=5)


_JXA_WINDOW_LOOP = """
ObjC.import('CoreGraphics'); ObjC.import('Foundation');
var out = $.NSFileHandle.fileHandleWithStandardOutput;
var end = Date.now() + %d;
while (Date.now() < end) {
  var l = ObjC.castRefToObject($.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly, 0));
  var row = [];
  for (var i = 0; i < l.count; i++) { var w = l.objectAtIndex(i), b = w.objectForKey('kCGWindowBounds');
    var lay = ObjC.unwrap(w.objectForKey('kCGWindowLayer')), wd = ObjC.unwrap(b.objectForKey('Width')),
        ht = ObjC.unwrap(b.objectForKey('Height'));
    if (lay == 0 && wd >= 200 && ht >= 150) row.push([ObjC.unwrap(w.objectForKey('kCGWindowOwnerPID')), wd, ht]); }
  out.writeData($.NSString.alloc.initWithUTF8String(JSON.stringify([Date.now() / 1000, row]) + '\\n')
                .dataUsingEncoding($.NSUTF8StringEncoding));
  delay(0.04);
}
"""


class WindowSampler(threading.Thread):
    """Every on-screen normal window (owner pid, size) ~20 times a second from one long-lived osascript, so a moment
    with no Hearthstone window on screen can be measured."""

    def __init__(self, max_s: int = 900):
        super().__init__(daemon=True, name="window-sampler")
        self.proc = subprocess.Popen(["osascript", "-l", "JavaScript", "-e", _JXA_WINDOW_LOOP % (max_s * 1000)],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.samples: list = []

    def run(self):
        for line in self.proc.stdout:
            try:
                self.samples.append(json.loads(line))
            except ValueError:
                pass

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.join(timeout=5)


def handoff_windows(samples: list, launcher_pid: int, game_pid: int, t_from: float, t_to: float) -> list:
    """[t, loading window on screen, game window on screen] per sample in [t_from, t_to]."""
    rows = []
    for t, wins in samples:
        if t_from <= t <= t_to:
            rows.append([round(t, 3), any(p == launcher_pid for p, _w, _h in wins),
                         any(p == game_pid and w >= 300 and h >= 200 for p, w, h in wins)])
    return rows


def post_note(name: str) -> dict:
    """A distributed notification with this repo's token (what the launcher/shim listen for)."""
    js = (f"ObjC.import('Foundation'); $.NSDistributedNotificationCenter.defaultCenter"
          f".postNotificationNameObjectUserInfoDeliverImmediately('{name}', '{launch.DOCK_TOKEN}', $(), true); 'ok'")
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", js], capture_output=True, text=True, timeout=20)
    return {"name": name, "rc": r.returncode, "out": (r.stdout + r.stderr).strip(), "t": round(time.time(), 3)}


def shim_log_lines(since: float, pid: int | None = None) -> list[str]:
    try:
        lines = dockshim.LOG.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for l in lines:
        try:
            t = datetime.fromisoformat(l.split(" ", 1)[0]).timestamp()
        except ValueError:
            continue
        if t >= since and (pid is None or f" pid={pid} " in l):
            out.append(l)
    return out


def runtime_listing() -> dict:
    """(path -> size, mtime) for every file of the pinned runtime: proves nothing was added or changed in it."""
    out = {}
    for root, _dirs, files in os.walk(RUNTIME_DIR):
        for f in files:
            p = Path(root) / f
            try:
                st = p.lstat()
            except FileNotFoundError:
                continue
            out[str(p.relative_to(RUNTIME_DIR))] = [st.st_size, int(st.st_mtime)]
    return out


class GuiStandIn(StandIn):
    """StandIn whose .bat starts Wine's built-in notepad (a real window), waits for the stop marker, then closes it."""

    def __init__(self, tag: str):
        super().__init__(tag)
        win = lambda p: "Z:" + str(p).replace("/", "\\")
        self.bat.write_text("\r\n".join([
            "@echo off",
            f"echo started> {win(self.started)}",
            "start notepad.exe",
            ":loop",
            f"if exist {win(self.stop_file)} goto done",
            "ping -n 2 127.0.0.1 >nul",
            "goto loop",
            ":done",
            "taskkill /f /im notepad.exe >nul 2>&1",
            "exit 0", ""]))

    @staticmethod
    def notepad_pids() -> list[int]:
        return [p["pid"] for p in launch.prefix_processes() if "notepad.exe" in p["cmd"].lower()]


def big_windows(pid: int) -> list[dict]:
    """pid's on-screen layer-0 windows of real size (not the Dock's minimise/restore animation)."""
    return [w for w in launch.onscreen_windows(pid) if w["layer"] == 0 and w["width"] >= launch.CLIENT_WINDOW_MIN[0]
            and w["height"] >= launch.CLIENT_WINDOW_MIN[1]]


def wait_window(timeout: float, stop_if=lambda: False) -> tuple[dict | None, float | None]:
    t0 = time.time()
    while time.time() - t0 < timeout and not stop_if():
        w = launch.client_window()
        if w:
            return w, time.time()
        time.sleep(0.2)
    return None, None


def wait_front(pid: int, timeout: float) -> tuple[float | None, list]:
    """Seconds until pid is the frontmost app (None if not within timeout), with the samples."""
    t0, seen = time.time(), []
    while time.time() - t0 < timeout:
        f = frontmost_pid()
        seen.append([round(time.time() - t0, 2), f])
        if f == pid:
            return round(time.time() - t0, 2), seen
        time.sleep(0.1)
    return None, seen


# ---------------------------------------------------------------- the app session (stack down, population, notepad)

_SESSIONS: dict = {}


def app_session(settings: dict | None = None, env: dict | None = None, profile: str = "population",
                key: str = "app", focus: bool = True) -> dict:
    """One app run with the notepad stand-in; returns its evidence (memoised per key). `focus` adds the steps only
    the gui checks judge (harness-reporting "GUI tier"): Dock/window sampling across the hand-off, waiting for the
    stand-in to be frontmost, Finder + reopen, minimise + reopen. Without them the run never activates another app,
    so a full run that skips the gui checks doesn't take the user's focus."""
    if key in _SESSIONS:
        return _SESSIONS[key]
    ev: dict = {"profile": profile, "settings": settings or {}, "env": env or {}, "focus_steps": focus}
    _SESSIONS[key] = ev
    build_app(ev)
    require_clear(ev)
    ev["screen_locked_at_start"] = screen_locked()
    s = GuiStandIn("dock")
    hook = Hook(s.dir, "hook")
    sfile = s.dir / "settings.json"
    sfile.write_text(json.dumps({"client": "stock", **(settings or {})}))
    off, since = _log_size(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rt_before = runtime_listing()
    from .. import population
    ls = LsSampler()
    dbs = Sampler(lambda: population.online_bots() if stack.mysql_ready() else None, 0.5)
    t_start = time.time()
    ev["t_start"] = round(t_start, 3)
    try:
        if focus:
            ls.start()
            wins = WindowSampler()
            wins.start()
        dbs.start()
        ev["open"] = open_app(A.APP, hook, "--account", "test", "--client-cmd", str(s.bat),
                              env={"HS_PROFILE": profile, "HS_SETTINGS": str(sfile), **(env or {})})
        require(ev["open"]["rc"] == 0, "`open` failed to launch the app", **ev)
        win, t_win = wait_window(600, stop_if=lambda: hook.exit_code() is not None)
        ev["window"], ev["t_window"] = win, t_win
        if not win:
            ev |= {"events": hook.events(), "app_log": _log_since(off)[-3000:], "tcc_prompts": tcc_prompts(since)}
            require(False, "the stand-in's window never appeared under the app", **ev)
        if focus:
            ev["front_after_window_s"], ev["front_samples"] = wait_front(win["pid"], FOCUS_LIMIT_S + 1.0)
        ev["handoff_events"] = [e for e in hook.events() if e.get("kind") in ("handoff", "activate", "window_note")
                                or e.get("id") == "hidden"]
        if focus:
            while time.time() < t_win + AFTER_WINDOW_S:
                time.sleep(0.1)
            ls.stop()
            wins.stop()
            ev["ls_samples"] = ls.samples
            ev["handoff_windows"] = handoff_windows(wins.samples, (_launchers(A.APP) or [{}])[0].get("pid"),
                                                    win["pid"], t_win - 15, t_win + AFTER_WINDOW_S)
        ev["apps_at_window"] = [a for a in lsappinfo_list() if is_ours(a)]
        ev["notepad_pid"] = win["pid"]
        launchers = _launchers(A.APP)
        ev["launcher_pid"] = launchers[0]["pid"] if launchers else None
        ev["play_pid"] = _play_pid(off, ev["launcher_pid"])
        ev["process_env"] = scan_process_env()
        ev["launchctl_dyld"] = subprocess.run(["/bin/launchctl", "getenv", "DYLD_INSERT_LIBRARIES"],
                                              capture_output=True, text=True).stdout.strip()

        if focus:
            _focus_steps(ev, win, hook, s, sfile, profile, env, t_start)
        else:
            ev["reopen"] = ev["unminimize"] = {"skipped": "gui checks not run (no focus steps)"}

        # quit: the stand-in exits, everything stops
        s.stop_file.write_text("quit")
        rc, dt = wait_app_exit(hook, A.APP)
        dbs.stop()
        rec = last_record(ev["play_pid"]) if ev["play_pid"] else {}
        ev |= {"app_exit_code": rc, "events": hook.events(), "db_online_samples": dbs.samples,
               "play_record": {k: rec.get(k) for k in ("outcome", "exit_code", "client_window", "stage")},
               "remaining_after": app_remaining(s), "runtime_changed": runtime_listing() != rt_before,
               "shim_log": shim_log_lines(t_start), "screen_locked_at_end": screen_locked(),
               "app_log": _log_since(off)[-4000:]}
        ev["ok"] = True
        return ev
    except CheckFail as e:
        ev["session_error"] = str(e)
        raise
    finally:
        ls.stop()
        dbs.stop()
        if "wins" in locals():
            wins.stop()
        stop_launchers(A.APP)
        s.cleanup()


def _focus_steps(ev: dict, win: dict, hook, s, sfile, profile: str, env: dict | None, t_start: float) -> None:
    """The gui checks' steps of the app session: Finder frontmost then reopen; minimise then reopen."""
    # reopen with another app frontmost
    ev["reopen"] = r = {}
    subprocess.run([OPEN, "-a", "Finder"], capture_output=True, text=True, timeout=30)
    t0 = time.time()
    while time.time() - t0 < 5 and frontmost_pid() == win["pid"]:
        time.sleep(0.1)
    r["front_before"] = frontmost_pid()
    r["finder_pids"] = [a["pid"] for a in lsappinfo_list() if a.get("bundle") == "com.apple.finder"]
    n_dialogs = len(hook.find("dialog"))
    r["open"] = open_app(A.APP, hook, "--account", "test", "--client-cmd", str(s.bat),
                         env={"HS_PROFILE": profile, "HS_SETTINGS": str(sfile), **(env or {})})
    r["front_after_s"], r["front_samples"] = wait_front(win["pid"], FOCUS_LIMIT_S + 1.0)
    time.sleep(0.5)
    r["dialogs_after"] = hook.find("dialog")[n_dialogs:]
    r["apps_after"] = [a for a in lsappinfo_list() if is_ours(a)]
    r["events"] = [e for e in hook.events() if e.get("kind") in ("reopen", "reopen_note", "activate", "became_active")]

    # minimise (test-only shim notification), then reopen
    ev["unminimize"] = u = {}
    u["minimize_note"] = post_note("local.hearthstone.test.minimize")
    t0 = time.time()
    while time.time() - t0 < 5 and big_windows(win["pid"]):
        time.sleep(0.2)
    time.sleep(1.0)                                  # let the minimise animation finish
    u["onscreen_after_minimize"] = big_windows(win["pid"])
    u["open"] = open_app(A.APP, hook, "--account", "test", "--client-cmd", str(s.bat),
                         env={"HS_PROFILE": profile, "HS_SETTINGS": str(sfile), **(env or {})})
    t0 = time.time()
    u["onscreen_after_reopen"], u["onscreen_after_s"] = [], None
    while time.time() - t0 < FOCUS_LIMIT_S + 3:
        w = big_windows(win["pid"])
        if w:
            u["onscreen_after_reopen"], u["onscreen_after_s"] = w, round(time.time() - t0, 2)
            break
        time.sleep(0.2)
    u["front_after_s"], u["front_samples"] = wait_front(win["pid"], FOCUS_LIMIT_S + 1.0)
    u["shim_log"] = shim_log_lines(t_start, win["pid"])


def scan_process_env() -> dict:
    """Which of this user's processes carry DYLD_INSERT_LIBRARIES / HSDOCK_POLICY (readable environments only)."""
    out = {"with_dyld_insert": [], "with_hsdock": []}
    for pid in procs.pids():
        if procs.uid(pid) != os.getuid():
            continue
        ae = procs.args_env(pid)
        if not ae:
            continue
        env = dict(e.split("=", 1) for e in ae[1] if "=" in e)
        row = {"pid": pid, "exe": procs.path(pid), "argv0": (ae[0] or [""])[0][:120]}
        if "DYLD_INSERT_LIBRARIES" in env:
            out["with_dyld_insert"].append(row | {"value": env["DYLD_INSERT_LIBRARIES"]})
        if "HSDOCK_POLICY" in env:
            out["with_hsdock"].append(row | {"value": env["HSDOCK_POLICY"]})
    return out


def _session(ctx_key: str = "app", focus: bool = False) -> dict:
    """The shared app session. gui checks ask for `focus` (the session with the focus steps); the other checks reuse
    whichever session this process ran, or run one without the focus steps. A session without them is never used
    for a gui check: a new one with them is run instead."""
    ev = _SESSIONS.get(ctx_key)
    if ev is not None and focus and not ev.get("focus_steps"):
        del _SESSIONS[ctx_key]
        ev = None
    if ev is None:
        ev = app_session(key=ctx_key, focus=focus)
    if not ev.get("ok"):
        raise CheckFail(f"the shared app session failed: {ev.get('session_error')}", ev)
    return ev


def app_name(pid) -> str | None:
    if not pid:
        return None
    try:
        return Path(procs.path(int(pid)) or "").name or None
    except (TypeError, ValueError):
        return None


def _no_thief(ev: dict, front_pids: list, allowed: set, out: dict, when: str):
    """GIVEN "without user input": no third app (the user's, or one activating itself) took activation during the
    step. macOS 14+ only lets the active app hand activation over, so such an app makes the THEN impossible."""
    thieves = sorted({p for p in front_pids if p and p not in allowed})
    out["third_party_frontmost"] = [{"pid": p, "app": app_name(p)} for p in thieves]
    require(not thieves, f"GIVEN not met: another app ({', '.join(str(app_name(p)) for p in thieves)}) took "
            f"activation {when} (user input or an app activating itself); macOS only lets the active app hand "
            "activation over, so no hand-off can be judged", **out)


def _focus_given(ev: dict, part: dict | None = None):
    require(not ev.get("screen_locked_at_start") and not ev.get("screen_locked_at_end"),
            "GIVEN not met: the Mac's screen is locked (loginwindow is frontmost), so no app can be activated; "
            "focus checks need an unlocked GUI session", screen_locked_at_start=ev.get("screen_locked_at_start"),
            screen_locked_at_end=ev.get("screen_locked_at_end"), **(part or {}))


# ---------------------------------------------------------------- verifiers (pure: evidence in, CheckFail out)

def verify_single_dock(ev: dict) -> dict:
    out: dict = {}
    np, lp = ev["notepad_pid"], ev["launcher_pid"]
    apps = ev["apps_at_window"]
    stand = [a for a in apps if a["pid"] == np]
    hs = [a for a in apps if a.get("bundle") == A.BUNDLE_ID]
    others_fg = [a for a in apps if a["pid"] != lp and a.get("type") == "Foreground"]
    out |= {"stand_in": stand, "launchers": hs, "other_foreground": others_fg}
    require(stand and stand[0]["type"] == "UIElement" and stand[0]["name"] == "Hearthstone",
            "the stand-in isn't a UIElement named Hearthstone", **out)
    require(len(hs) == 1 and hs[0]["pid"] == lp and hs[0]["type"] == "Foreground",
            "the launcher isn't the only Foreground process with bundle id local.hearthstone.app", **out)
    require(not others_fg, "another process of this prefix is Foreground", **out)
    # sampling from the Wine host's start to 5 s after the window
    samples = ev["ls_samples"]
    t_win = ev["t_window"]
    wine_first = next((s["t"] for s in samples if any(str(a.get("exe", "")).startswith(str(RUNTIME))
                                                      for a in s["apps"])), None)
    window = [s for s in samples if wine_first is not None and wine_first <= s["t"] <= t_win + AFTER_WINDOW_S]
    span = (window[-1]["t"] - window[0]["t"]) if len(window) > 1 else 0
    gaps = [b["t"] - a["t"] for a, b in zip(window, window[1:])]
    fg = [{"t": s["t"], "apps": [a for a in s["apps"] if a.get("bundle") != A.BUNDLE_ID and a.get("type") == "Foreground"]}
          for s in window]
    fg = [f for f in fg if f["apps"]]
    out |= {"wine_host_first_seen": wine_first, "window_t": t_win, "samples_in_window": len(window),
            "sampled_span_s": round(span, 2), "rate_hz": round(len(window) / span, 1) if span else 0,
            "max_gap_s": round(max(gaps), 3) if gaps else None, "foreground_samples": fg[:10],
            "foreground_sample_count": len(fg), "sample_errors": [s for s in samples if any("error" in a for a in s["apps"])][:3]}
    require(wine_first is not None and wine_first < t_win, "the sampler never saw the Wine host before its window", **out)
    require(span >= (t_win + AFTER_WINDOW_S - wine_first) - 0.5, "the samples don't cover host start to window + 5 s", **out)
    require(out["rate_hz"] >= SAMPLE_HZ_MIN and (out["max_gap_s"] or 0) <= 0.25,
            f"sampling slower than {SAMPLE_HZ_MIN} Hz (or a gap > 0.25 s)", **out)
    require(not fg, f"a process of this prefix was Foreground in {len(fg)} sample(s) (a Wine Dock icon flashed)", **out)
    return out


def verify_handoff(ev: dict) -> dict:
    out = {"window": ev["window"], "front_after_window_s": ev["front_after_window_s"],
           "front_samples": ev["front_samples"][:40], "handoff_events": ev["handoff_events"]}
    hidden = [e for e in ev["events"] if e.get("kind") == "status" and e.get("id") == "hidden"]
    out["hidden_events"] = hidden
    _focus_given(ev, out)
    # a third app holding activation when the window appeared (the launcher wasn't active at the hand-off) makes the
    # THEN impossible: report it as the GIVEN failing, not the hand-off
    ho = next((e for e in ev["handoff_events"] if e.get("kind") == "handoff"), None)
    act = next((e for e in ev["handoff_events"] if e.get("kind") == "activate"), None)
    if not (ev["front_after_window_s"] is not None and ev["front_after_window_s"] <= FOCUS_LIMIT_S) \
            and ho and ho.get("launcher_active") is False:
        _no_thief(ev, [act.get("frontmost_pid")] if act else [], {ev["notepad_pid"], ev["launcher_pid"]}, out,
                  "from the launcher before the game window appeared")
    t_hidden = datetime.fromisoformat(hidden[0]["t"].replace("Z", "+00:00")).timestamp() if hidden else None
    out["hidden_after_window_s"] = round(t_hidden - ev["t_window"], 2) if t_hidden else None
    require(hidden and out["hidden_after_window_s"] <= FOCUS_LIMIT_S, "the loading window wasn't hidden within 2 s", **out)
    # no blank moment: the loading window is still on screen when the game window first is (or the gap is <= 0.3 s)
    rows = ev["handoff_windows"]
    load_t = [t for t, l, _g in rows if l]
    game_first = next((t for t, _l, g in rows if g), None)
    before = [t for t in load_t if game_first is not None and t <= game_first]
    up_at_first = bool(game_first is not None and any(l for t, l, g in rows if t == game_first))
    gaps = [b[0] - a[0] for a, b in zip(rows, rows[1:])]
    out |= {"loading_window_last_seen_before_game_window": max(before) if before else None,
            "game_window_first_seen": game_first, "loading_window_up_when_game_window_appeared": up_at_first,
            "nothing_on_screen_s": (0.0 if up_at_first else round(game_first - max(before), 3)) if before else None,
            "window_samples": len(rows), "window_sample_max_gap_s": round(max(gaps), 3) if gaps else None}
    require(before and game_first, "the window samples don't show the loading window and then the game window", **out)
    require(out["window_sample_max_gap_s"] is not None and out["window_sample_max_gap_s"] <= 0.25,
            "window sampling had a gap > 0.25 s", **out)
    require(out["nothing_on_screen_s"] <= NOTHING_ON_SCREEN_S,
            f"nothing of Hearthstone's was on screen for {out['nothing_on_screen_s']} s between the loading window "
            f"and the game window (limit {NOTHING_ON_SCREEN_S} s)", **out)
    require(ev["front_after_window_s"] is not None and ev["front_after_window_s"] <= FOCUS_LIMIT_S,
            "the stand-in didn't become frontmost within 2 s of its window", **out)
    return out


def verify_reopen(ev: dict) -> dict:
    r = ev["reopen"]
    np = ev["notepad_pid"]
    out = {k: r[k] for k in ("front_before", "finder_pids", "front_after_s", "dialogs_after", "apps_after", "events")}
    out["front_samples"] = r["front_samples"][:40]
    _focus_given(ev, out)
    require(r["front_before"] != np and r["front_before"] in r["finder_pids"], "GIVEN not met: Finder wasn't frontmost", **out)

    require(r["front_after_s"] is not None and r["front_after_s"] <= FOCUS_LIMIT_S,
            "the stand-in wasn't frontmost within 2 s of the reopen", **out)
    require(not [d for d in r["dialogs_after"] if d.get("id") == "already_running"], "an \"already running\" dialog was shown", **out)
    hs = [a for a in r["apps_after"] if a.get("bundle") == A.BUNDLE_ID]
    fg = [a for a in r["apps_after"] if a.get("type") == "Foreground"]
    require(len(hs) == 1 and hs[0]["pid"] == ev["launcher_pid"] and [a["pid"] for a in fg] == [ev["launcher_pid"]],
            "the launcher isn't still the single Hearthstone Dock icon", **out)
    return out


def verify_unminimize(ev: dict) -> dict:
    u = ev["unminimize"]
    out = {k: u[k] for k in ("minimize_note", "onscreen_after_minimize", "onscreen_after_reopen", "onscreen_after_s",
                             "front_after_s", "shim_log")}
    require(not u["onscreen_after_minimize"] and any("minimise requested" in l for l in u["shim_log"]),
            "GIVEN not met: the stand-in window wasn't minimised by the test notification", **out)
    require(u["onscreen_after_reopen"], "the window didn't come back on screen after the reopen", **out)
    _focus_given(ev, out)
    require(u["front_after_s"] is not None, "the window isn't frontmost again after the reopen", **out)
    return out


def stage_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("kind") == "stage"]


def verify_stage_sequence(st: list[dict]) -> dict:
    out = {"stage_events": [{k: e.get(k) for k in ("id", "state", "fraction", "overall")} for e in st]}
    bad = [e for e in st if e.get("id") not in S.IDS or e.get("state") not in S.STATES]
    require(st and not bad, "stage events with unknown ids/states (or none)", bad=bad, **out)
    seq = [e["id"] for e in st]
    order = [S.IDS.index(i) for i in seq]
    back = [(seq[i], seq[i + 1]) for i in range(len(seq) - 1) if order[i + 1] < order[i]]
    first_seen = list(dict.fromkeys(seq))
    out |= {"first_seen_order": first_seen, "backwards": back}
    require(not back and first_seen == S.IDS, "stages aren't in the documented order (or one is missing)", **out)
    ov = [e["overall"] for e in st]
    dec = [(i, ov[i], ov[i + 1]) for i in range(len(ov) - 1) if ov[i + 1] < ov[i]]
    out |= {"overall_first": ov[0], "overall_last": ov[-1], "decreases": dec}
    require(not dec, "overall progress decreased", **out)
    require(ov[0] == 0 and ov[-1] == 1, "overall progress doesn't start at 0 and end at 1", **out)
    return out


def verify_bots(st: list[dict], db_samples: list, target: int) -> dict:
    bots = [e for e in st if e["id"] == "bots"]
    launching = next((e for e in st if e["id"] == "launching" and e["state"] == "start"), None)
    world_done = next((e for e in st if e["id"] == "world" and e["state"] in ("done", "skipped")), None)
    out: dict = {"bots_events": bots, "launching_start": launching, "target_expected": target}
    require(bots and launching and world_done, "no bots / launching / world-done events", **out)
    b = bots[0]["bots"]
    t = bots[0]["play_t"]
    near = [v for (ts, v) in db_samples if v is not None and abs(ts - t) <= 1.0]
    out |= {"bots_reported": b, "db_samples_within_1s": near}
    require(near and min(near) <= b["online"] <= max(near), "the bots stage's online count isn't the DB's at that time", **out)
    require(b["target"] == target, "the bots stage's target isn't the world's target", **out)
    reached = next((ts for (ts, v) in db_samples if v is not None and v >= target), None)
    out |= {"db_reached_target_at": reached, "launching_started_at": launching["play_t"],
            "launching_after_world_s": round(launching["play_t"] - world_done["play_t"], 2),
            "online_at_launching_start": (launching.get("bots") or {}).get("online")}
    require(reached is None or launching["play_t"] < reached,
            "the launching stage started only after the bots reached the target (it waited)", **out)
    require(out["launching_after_world_s"] <= 30, "launching started long after the world was ready", **out)
    return out


TERMINAL_ONLY = ("Ctrl-C",)          # play-loop wording that only makes sense in a terminal


def verify_details(events: list[dict]) -> dict:
    """The loading window's detail lines (the play loop's [play] lines) use app wording, not terminal wording."""
    det = [e["text"] for e in events if e.get("kind") == "detail"]
    bad = [d for d in det if any(w in d for w in TERMINAL_ONLY)]
    out = {"detail_lines": det, "terminal_wording": bad}
    require(det, "no detail lines were shown", **out)
    require(not bad, "the loading window showed terminal wording (Ctrl-C)", **out)
    return out


def verify_shim_scope(ev: dict, shim: str | None = None) -> dict:
    shim = shim or str(dockshim.DYLIB)
    sp = Path(shim).resolve()
    out = {"shim": str(sp), "launchctl_getenv_DYLD_INSERT_LIBRARIES": ev["launchctl_dyld"],
           "process_env": ev["process_env"], "runtime_changed_during_run": ev["runtime_changed"]}
    require(sp.is_file(), "the shim file doesn't exist", **out)
    require(not sp.is_relative_to(P.CLIENT.resolve()) and not sp.is_relative_to(RUNTIME_DIR.resolve()),
            "the shim lives inside client/ or the pinned runtime", **out)
    pin = P.fork_lock().get("wowsilicon_runtime_sha256")
    recorded = (RUNTIME_DIR / ".hs-runtime-sha256").read_text().strip() if (RUNTIME_DIR / ".hs-runtime-sha256").exists() else None
    out |= {"runtime_pin": pin, "runtime_recorded": recorded}
    require(pin and recorded == pin, "the runtime's recorded hash isn't the fork.lock pin", **out)
    require(not ev["runtime_changed"], "a file in the pinned runtime was added or changed during the run", **out)
    require(ev["launchctl_dyld"] == "", "DYLD_INSERT_LIBRARIES is set globally (launchctl)", **out)
    carriers = ev["process_env"]["with_dyld_insert"]
    non_wine = [c for c in carriers if not str(c.get("exe") or "").startswith(str(RUNTIME) + "/")]
    out["non_wine_carriers"] = non_wine
    require(not non_wine, "a process other than the Wine host carries DYLD_INSERT_LIBRARIES", **out)
    require(carriers and all(c["value"] == shim for c in carriers), "the Wine processes don't carry the shim", **out)
    acc = [c for c in ev["process_env"]["with_hsdock"] if c["value"] == "accessory"]
    require(acc, "no Wine process of this app run had HSDOCK_POLICY=accessory", **out)
    return out


def client_inspect() -> dict:
    from ..registry import REGISTRY
    return REGISTRY["client.inspect"].fn(None)


# ---------------------------------------------------------------- app checks

@check("app.single_dock_icon", order=392, capability=CAP_APP, stack="down", tier="full", gui=True)
def single_dock_icon(ctx):
    """App + notepad stand-in: lsappinfo has the stand-in as a UIElement named Hearthstone, the launcher as the only
    Foreground process with bundle id local.hearthstone.app, no other prefix process Foreground; sampled at >= 10 Hz
    from the Wine host's start to 5 s after the window, no prefix process is ever Foreground."""
    ev = _session(focus=True)
    return verify_single_dock(ev)


@check("app.handoff_focus", order=393, capability=CAP_APP, stack="down", tier="full", gui=True)
def handoff_focus(ctx):
    """When the stand-in's first window appears, the loading window is hidden and the stand-in is frontmost within
    2 s, without user input."""
    return verify_handoff(_session(focus=True))


@check("app.reopen_focuses_game", order=394, capability=CAP_APP, stack="down", tier="full", gui=True)
def reopen_focuses_game(ctx):
    """Stand-in up, Finder frontmost: reopening the app through LaunchServices makes the stand-in frontmost within
    2 s, shows no "already running" dialog, and the launcher stays the single Hearthstone Dock icon."""
    return verify_reopen(_session(focus=True))


@check("app.reopen_unminimizes", order=396, capability=CAP_APP, stack="down", tier="full", gui=True)
def reopen_unminimizes(ctx):
    """Stand-in window minimised by the shim's test notification: reopening the app puts it back on screen and
    frontmost."""
    return verify_unminimize(_session(focus=True))


@check("app.loading_stages", order=397, capability=CAP_APP, stack="down", tier="full")
def loading_stages(ctx):
    """Stack down, app + stand-in, population profile: stage events use only the documented ids, in order, overall
    progress never decreases from 0 to 1; the bots stage reports the DB's online count and the target, and launching
    starts without waiting for the bots to reach it."""
    ev = _session()
    st = stage_events(ev["events"])
    out = verify_stage_sequence(st)
    out["bots"] = verify_bots(st, ev["db_online_samples"], config.population_n())
    out["details"] = verify_details(ev["events"])
    require(ev["app_exit_code"] == 0 and is_clear(ev["remaining_after"]), "the session didn't end cleanly",
            app_exit_code=ev["app_exit_code"], remaining=ev["remaining_after"], **out)
    return out


@check("app.shim_scope", order=398, capability=CAP_APP, stack="down", tier="full")
def shim_scope(ctx):
    """During an app run: client.inspect passes; the shim lives outside client/ and env/wowsilicon-r17/ (pinned hash
    unchanged, nothing in the runtime changed); DYLD_INSERT_LIBRARIES is only in the Wine host processes' environment,
    never global (launchctl getenv is empty); Wow.exe (client/ and the play client) matches its recorded hashes."""
    ev = _session()
    out = verify_shim_scope(ev)
    out["client_inspect"] = client_inspect()
    return out


@check("app.loading_stages_warm", order=399, capability=CAP_APP, stack="down", tier="full")
def loading_stages_warm(ctx):
    """Stack already up: the opened app reports database, login and world as skipped within 2 s of its start."""
    ev: dict = {}
    build_app(ev)
    require_clear(ev)
    ev["up"] = {k: v for k, v in stack.up(profile="m0").items() if k in ("profile", "worldserver", "mysqld")}
    st0 = stack.status()
    ev["given_stack"] = {"mysql_ready": st0["mysql_ready"], "soap_ready": st0["soap_ready"], "processes": st0["processes"]}
    require(st0["mysql_ready"] and st0["soap_ready"], "GIVEN not met: the stack isn't up", **ev)
    return warm_run(ev)


def warm_run(ev: dict) -> dict:
    s = StandIn("warm")
    hook = Hook(s.dir, "hook")
    sfile = s.dir / "settings.json"
    sfile.write_text(json.dumps({"client": "stock"}))
    try:
        t0 = time.time()
        ev["open"] = open_app(A.APP, hook, "--account", "test", "--client-cmd", str(s.bat),
                              env={"HS_PROFILE": "m0", "HS_SETTINGS": str(sfile)})
        while time.time() - t0 < 120 and not hook.find("stage", "account") and hook.exit_code() is None:
            time.sleep(0.2)
        ev["t_open"] = round(t0, 3)
        s.stop_file.write_text("quit")
        rc, _ = wait_app_exit(hook, A.APP)
        ev |= {"app_exit_code": rc, "events": hook.events(), "remaining_after": app_remaining(s)}
        return verify_warm(ev)
    finally:
        stop_launchers(A.APP)
        s.cleanup()


def verify_warm(ev: dict) -> dict:
    st = stage_events(ev["events"])
    out = {"stages": [{k: e.get(k) for k in ("id", "state", "play_t")} for e in st]}
    sk = {e["id"]: e for e in st if e["state"] == "skipped"}
    out["skipped_after_open_s"] = {k: round(sk[k]["play_t"] - ev["t_open"], 2) for k in sk}
    for k in ("database", "login", "world"):
        require(k in sk, f"{k} wasn't reported as skipped", **out)
        require(out["skipped_after_open_s"][k] <= 2.0, f"{k} was reported skipped only after 2 s", **out)
    started = [e["id"] for e in st if e["id"] in ("database", "login", "world") and e["state"] == "start"]
    require(not started, "a server stage was started though the server was running", **out)
    require(ev["app_exit_code"] == 0 and is_clear(ev["remaining_after"]), "the run didn't end cleanly",
            app_exit_code=ev["app_exit_code"], **out)
    return out


# ---------------------------------------------------------------- terminal ./play session (m0, stack down, notepad)

def term_session(key: str = "term", settings: dict | None = None) -> dict:
    if key in _SESSIONS:
        return _SESSIONS[key]
    ev: dict = {}
    _SESSIONS[key] = ev
    before = remaining()
    ev["given_nothing_running"] = before
    require(is_clear(before), "GIVEN not met: something from this repo is already running", **ev)
    s = GuiStandIn("term")
    sfile = s.dir / "settings.json"
    sfile.write_text(json.dumps({"client": "stock", **(settings or {})}))
    wlog = stack.LOG["worldserver"]
    lines = Sampler(lambda: S.count_lines(wlog), 0.1)
    t_start = time.time()
    try:
        lines.start()
        s.start_play(env={"HS_SETTINGS": str(sfile)})
        win, t_win = wait_window(600, stop_if=lambda: s.proc.poll() is not None)
        ev |= {"window": win, "t_window": t_win}
        require(win, "the stand-in's window never appeared under ./play", output=s.output()[-3000:], **ev)
        time.sleep(1.5)
        ev["apps_at_window"] = [a for a in lsappinfo_list() if is_ours(a)]
        ev["shim_log"] = shim_log_lines(t_start, win["pid"])
        s.stop_file.write_text("quit")
        rc, _ = s.wait_exit(timeout=300)
        lines.stop()
        ev |= {"exit_code": rc, "output": s.output(), "line_samples": lines.samples,
               "remaining_after": remaining(s), "t_start": round(t_start, 3)}
        ev["ok"] = True
        return ev
    except CheckFail as e:
        ev["session_error"] = str(e)
        raise
    finally:
        lines.stop()
        s.cleanup()


def _term() -> dict:
    ev = term_session() if "term" not in _SESSIONS else _SESSIONS["term"]
    if not ev.get("ok"):
        raise CheckFail(f"the shared ./play session failed: {ev.get('session_error')}", ev)
    return ev


def _regex_of(node) -> str:
    """A say() argument as a regex: literal text escaped, every formatted value / unknown part `.*`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return re.escape(node.value)
    if isinstance(node, ast.JoinedStr):
        return "".join(_regex_of(v) for v in node.values)
    if isinstance(node, ast.FormattedValue):
        return ".*"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _regex_of(node.left) + _regex_of(node.right)
    if isinstance(node, ast.IfExp):
        return f"(?:{_regex_of(node.body)}|{_regex_of(node.orelse)})"
    return ".*"


def base_play_templates(rev: str = PLAY_LINES_BASE) -> list[str]:
    pats = []
    for f in PLAY_LINES_FILES:
        src = subprocess.run(["git", "-C", str(P.ROOT), "show", f"{rev}:{f}"], capture_output=True, text=True)
        if src.returncode != 0:
            raise CheckFail(f"can't read {f} at {rev}: {src.stderr.strip()}")
        for node in ast.walk(ast.parse(src.stdout)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "say" and node.args:
                pats.append(_regex_of(node.args[0]))
    return pats


def verify_stage_lines(output: str, templates: list[str]) -> dict:
    play_lines = [l[len("[play] "):] for l in output.splitlines() if l.startswith("[play] ")]
    stage_lines = [l for l in output.splitlines() if l.startswith(S.PREFIX)]
    unmatched = [l for l in play_lines if not any(re.fullmatch(p, l, re.S) for p in templates)]
    bad_json = [l for l in stage_lines if S.parse(l) is None]
    out = {"play_lines": play_lines, "stage_line_count": len(stage_lines), "templates": len(templates),
           "unmatched_play_lines": unmatched, "unparseable_stage_lines": bad_json[:5]}
    require(play_lines and templates, "no [play] lines or no templates (nothing to compare)", **out)
    require(not unmatched, "a [play] line differs from every pre-change say() line", **out)
    require(stage_lines and not bad_json, "[stage] lines missing or not JSON", **out)
    evs = [S.parse(l) for l in stage_lines]
    require(all(e.get("id") in S.IDS and e.get("state") in S.STATES for e in evs), "a [stage] line has an unknown id/state", **out)
    return out


@check("play.stage_lines", order=391, capability=CAP_PLAY, stack="down", tier="full")
def stage_lines(ctx):
    """./play in a terminal (stand-in): every [play] line matches a [play] line of the play loop before
    launcher-polish (unchanged), and every [stage] line parses as JSON with a documented id and state. (The existing
    play.* checks run in the same harness run.)"""
    ev = _term()
    out = verify_stage_lines(ev["output"], base_play_templates())
    require(ev["exit_code"] == 0 and is_clear(ev["remaining_after"]), "./play didn't end cleanly",
            exit_code=ev["exit_code"], **out)
    return out


def verify_world_progress(output: str, line_samples: list, poll: float = S.WORLD_POLL_S) -> dict:
    evs = [S.parse(l) for l in output.splitlines() if l.startswith(S.PREFIX)]
    world = [e for e in evs if e and e.get("id") == "world" and e.get("state") == "progress"]
    done = next((e for e in evs if e and e.get("id") == "world" and e.get("state") == "done"), None)
    rows, bad = [], []
    # this boot's log only: worldserver.out is truncated when the worldserver is spawned, so drop every sample before
    # the last drop in the count (the previous boot's log)
    vals = [(t, v) for (t, v) in line_samples if v is not None and (done is None or t <= done["t"] + 5)]
    cut = max([i for i in range(1, len(vals)) if vals[i][1] < vals[i - 1][1]] or [0])
    line_samples = vals[cut:]
    for e in world:
        before = [v for (t, v) in line_samples if t <= e["t"] - poll]
        lo = before[-1] if before else 0
        hi_c = [v for (t, v) in line_samples if t >= e["t"] + poll and v is not None]
        hi = min(hi_c) if hi_c else None
        expect = round(min(e["lines"] / e["of"], 0.99), 4)
        row = {"t": e["t"], "fraction": e["fraction"], "lines": e["lines"], "of": e["of"],
               "log_lines_one_poll_before": lo, "log_lines_one_poll_after": hi}
        rows.append(row)
        if hi is None or not lo <= e["lines"] <= hi or e["fraction"] != expect:
            bad.append(row)
    inside = sorted({r["fraction"] for r in rows if 0 < r["fraction"] < 1 and (done is None or r["t"] < done["t"])})
    out = {"world_progress": rows, "distinct_fractions_strictly_inside": inside, "mismatches": bad,
           "world_done": done and done["t"], "samples": len(line_samples)}
    require(done, "the world stage never finished", **out)
    require(len(inside) >= 3, "fewer than three distinct world fractions strictly between 0 and 1", **out)
    require(not bad, "a world fraction doesn't match the worldserver.out line count within one poll", **out)
    return out


@check("play.world_progress", order=391, capability=CAP_PLAY, stack="down", tier="full")
def world_progress(ctx):
    """Cold worldserver start under ./play: at least three distinct world fractions strictly between 0 and 1 come
    before the world is done, each = worldserver.out's line count / the calibration, and that count matches the log's
    own line count (sampled independently at 10 Hz) within one poll (0.5 s)."""
    ev = _term()
    return verify_world_progress(ev["output"], ev["line_samples"])


def verify_dock_identity(ev: dict) -> dict:
    np = ev["window"]["pid"]
    stand = [a for a in ev["apps_at_window"] if a["pid"] == np]
    out = {"stand_in": stand, "shim_log": ev["shim_log"]}
    require(stand and stand[0].get("type") == "Foreground" and stand[0].get("name") == "Hearthstone",
            "the stand-in isn't a normal Foreground app named Hearthstone", **out)
    require(any("setApplicationIconImage:" in l and "replaced=1" in l for l in ev["shim_log"]),
            "the shim log has no icon replacement for the stand-in (this run)", **out)
    require(not any("-> using 1" in l for l in ev["shim_log"]), "the terminal run made the stand-in an accessory app", **out)
    return out


@check("play.dock_identity", order=391, capability=CAP_PLAY, stack="down", tier="full")
def dock_identity(ctx):
    """Terminal ./play with the notepad stand-in (no app): the stand-in is a normal Foreground app named Hearthstone,
    and the shim log records the icon replacement for its pid in this run."""
    return verify_dock_identity(_term())


# ---------------------------------------------------------------- performance (real client; not a Dock check)

PERF_RUNS = 5
PERF_SETTLE_S = 10          # after the window: let the login screen finish loading before measuring
PERF_MEASURE_S = 60
PERF_CPU_EVERY_S = 2.0


def cpu_seconds(pid: int) -> float | None:
    r = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    if not r:
        return None
    parts = r.replace("-", ":").split(":")
    try:
        vals = [float(x) for x in parts]
    except ValueError:
        return None
    sec = 0.0
    for v in vals:
        sec = sec * 60 + v
    return sec


def perf_run(policy: str, ev_runs: list) -> dict:
    """One app run of the real client (test account, scratch settings): time from client start (launching stage) to
    its first window, and per-interval CPU % of Wow.exe over 60 s at the login screen."""
    run: dict = {"policy": policy}
    ev_runs.append(run)
    tmp = Path(tempfile.mkdtemp(prefix="hs-perf-")).resolve()
    hook = Hook(tmp, "hook")
    sfile = tmp / "settings.json"
    sfile.write_text(json.dumps({"dock_policy": policy}))
    try:
        run["display_states"] = states = [display_state()]
        run["up"] = {k: v for k, v in stack.up(profile="m0").items() if k in ("profile", "worldserver")}
        run["open"] = open_app(A.APP, hook, "--account", "test", env={"HS_PROFILE": "m0", "HS_SETTINGS": str(sfile)})
        t0 = time.time()
        launching, wow, t_win = None, None, None
        while time.time() - t0 < 300 and hook.exit_code() is None:
            if not launching:
                launching = next((e for e in hook.find("stage", "launching") if e["state"] == "start"), None)
            if launching and not wow:
                w = launch.wow_processes()
                wow = w[0]["pid"] if w else None
            if wow:
                wins = launch.onscreen_windows(wow)
                if any(x["layer"] == 0 and x["width"] >= 640 and x["height"] >= 480 for x in wins):
                    t_win = time.time()
                    break
            time.sleep(0.25)
        require(launching and t_win, f"the client's window never appeared ({policy})", run=run, events=hook.events())
        run |= {"wow_pid": wow, "time_to_first_window_s": round(t_win - launching["play_t"], 2)}
        time.sleep(PERF_SETTLE_S)
        samples, prev = [], (time.time(), cpu_seconds(wow))
        end = time.time() + PERF_MEASURE_S
        while time.time() < end:
            time.sleep(PERF_CPU_EVERY_S)
            cur = (time.time(), cpu_seconds(wow))
            if cur[1] is None or prev[1] is None:
                break
            samples.append(round(100 * (cur[1] - prev[1]) / (cur[0] - prev[0]), 1))
            prev = cur
            if len(samples) % 5 == 0:          # every ~10 s, kept out of the CPU intervals
                states.append(display_state())
                prev = (time.time(), cpu_seconds(wow))
        require(len(samples) >= PERF_MEASURE_S / PERF_CPU_EVERY_S - 2, f"Wow.exe died during the measurement ({policy})",
                run=run, samples=samples)
        states.append(display_state())
        run |= {"cpu_samples_pct": samples, "cpu_median_pct": statistics.median(samples),
                "display_ok": all(x["ok"] for x in states)}
        shim = shim_log_lines(launching["play_t"], wow)
        run["shim_loaded_in_wow"] = bool(shim)
        run["shim_log_head"] = shim[:6]
        return run
    finally:
        stop_launchers(A.APP)
        if launch.prefix_processes():
            launch.stop_prefix()
        if any(stack.stack_processes().values()):
            stack.down()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def verify_perf(runs: list[dict]) -> dict:
    by = {p: [r for r in runs if r["policy"] == p] for p in ("accessory", "off")}
    out: dict = {"runs": runs}
    bad = [{"run": i, "policy": r["policy"], "states": [x for x in r.get("display_states", []) if not x["ok"]][:3]}
           for i, r in enumerate(runs) if not r.get("display_ok") or not r.get("display_states")]
    out["runs_not_unlocked_and_awake"] = bad
    require(not bad, f"GIVEN not met: the screen was locked or the display asleep during {len(bad)} run(s) "
            f"({', '.join(str(b['run']) + ':' + b['policy'] for b in bad)}), so the runs aren't comparable "
            "(keep the screen unlocked and the display awake, e.g. caffeinate -d)", **out)
    for p in by:
        require(len(by[p]) == PERF_RUNS, f"expected {PERF_RUNS} runs with dock_policy={p}", **out)
    require(all(r["shim_loaded_in_wow"] for r in by["accessory"]) and not any(r["shim_loaded_in_wow"] for r in by["off"]),
            "the shim wasn't loaded in every 'with' run (or was in a 'without' run)", **out)
    st = {p: statistics.median(r["time_to_first_window_s"] for r in by[p]) for p in by}
    cpu = {p: statistics.median(r["cpu_median_pct"] for r in by[p]) for p in by}
    tol_st = max(2.0, 0.10 * st["off"])
    tol_cpu = max(5.0, 0.10 * cpu["off"])
    out |= {"startup_median_s": st, "cpu_median_pct": cpu, "startup_tolerance_s": round(tol_st, 2),
            "cpu_tolerance_pp": round(tol_cpu, 2), "startup_delta_s": round(st["accessory"] - st["off"], 2),
            "cpu_delta_pp": round(cpu["accessory"] - cpu["off"], 2)}
    # adjacent pairs (runs come in back-to-back with/without pairs): the median paired difference must be within
    # the same tolerances too, which cancels slow drift across the half hour
    pairs = []
    for a, b in zip(runs[0::2], runs[1::2]):
        w, o = (a, b) if a["policy"] == "accessory" else (b, a)
        pairs.append({"startup_delta_s": round(w["time_to_first_window_s"] - o["time_to_first_window_s"], 2),
                      "cpu_delta_pp": round(w["cpu_median_pct"] - o["cpu_median_pct"], 2)})
    out["pairs"] = pairs
    out["pair_median_startup_delta_s"] = statistics.median(p["startup_delta_s"] for p in pairs)
    out["pair_median_cpu_delta_pp"] = statistics.median(p["cpu_delta_pp"] for p in pairs)
    require(abs(out["startup_delta_s"]) <= tol_st, "time to first window with the shim is outside the tolerance", **out)
    require(abs(out["cpu_delta_pp"]) <= tol_cpu, "login-screen CPU with the shim is outside the tolerance", **out)
    require(abs(out["pair_median_startup_delta_s"]) <= tol_st, "paired startup difference outside the tolerance", **out)
    require(abs(out["pair_median_cpu_delta_pp"]) <= tol_cpu, "paired CPU difference outside the tolerance", **out)
    return out


@check("app.shim_perf", order=420, capability=CAP_APP, stack="down", tier="slow")
def shim_perf(ctx):
    """The real client from the app, 5 runs with the shim (dock_policy accessory) and 5 without (off), interleaved,
    stack already up each time: medians of time-to-first-window and of Wow.exe's median CPU over 60 s at the login
    screen agree within max(2 s, 10%) and max(5 pp, 10%). All samples recorded. (Slow tier: ~25 min.)"""
    ev: dict = {}
    build_app(ev)
    require_clear(ev)
    runs: list = []
    try:
        for i in range(PERF_RUNS):
            for policy in (("accessory", "off") if i % 2 == 0 else ("off", "accessory")):
                perf_run(policy, runs)
                if not runs[-1].get("display_ok"):        # stop early: the GIVEN already failed
                    return verify_perf(runs)
    except CheckFail as e:
        raise CheckFail(str(e), {"runs": runs, **e.evidence})
    return verify_perf(runs)
