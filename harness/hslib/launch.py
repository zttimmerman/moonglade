"""`hs launch [--smoke]`: start the installed 3.3.5a client under the Wine layer (ADR-0004).

Wine layer: Whisky's bundled Wine binaries, used read-only from Whisky's Libraries folder,
with a repo-local prefix `env/wineprefix`. Whisky's own bottles and settings are never touched.
No client binary is modified: the manifest's .exe/.dll hashes are compared before and after.

Normal mode: stack up (if needed) → client-config (+ Wine defaults) → fixtures → start Wow.exe
detached, logging to env/logs/wine.log, and print the login.
--smoke: the same, then require Wow.exe alive after 30 s, stop the prefix (`wineserver -k`),
and require that no process of this prefix remains.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from . import paths as P
from .util import log, tail

WINE = P.WINE_BIN / "wine64"
WINESERVER = P.WINE_BIN / "wineserver"
SMOKE_SECONDS = 30


class LaunchError(RuntimeError):
    def __init__(self, msg, evidence: dict | None = None):
        super().__init__(msg)
        self.evidence = evidence or {}


def wine_env() -> dict:
    """Environment for every Wine call. Mirrors only what Whisky sets for a default bottle
    (see ADR-0004 for sources): WINEPREFIX, WINEDEBUG=fixme-all, and msync (Whisky's default
    `enhancedSync`). DXVK is off in Whisky's defaults and WoW 3.3.5a is D3D9, so no
    WINEDLLOVERRIDES are set: d3d9 is Wine's builtin wined3d."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("WINE", "DXVK_", "MVK_", "MTL_", "D3DM_"))}
    env.update({
        "WINEPREFIX": str(P.WINEPREFIX),
        "WINEDEBUG": "fixme-all",
        "WINEMSYNC": "1",
    })
    return env


# ---------------------------------------------------------------- process discovery

def _ps_env() -> list[tuple[int, str]]:
    """(pid, command+environment) for this user's processes. macOS `ps -E` appends each
    process's environment, which lets us match WINEPREFIX exactly."""
    out = subprocess.run(["ps", "-E", "-x", "-ww", "-o", "pid=,command="],
                         capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines():
        pid_s, _, rest = line.strip().partition(" ")
        if pid_s.isdigit():
            rows.append((int(pid_s), rest))
    return rows


def server_dir() -> Path | None:
    """Wine's per-prefix server directory: /tmp/.wine-<uid>/server-<st_dev hex>-<st_ino hex>
    of the prefix dir. Every process attached to this prefix's wineserver maps a file in it."""
    try:
        st = P.WINEPREFIX.stat()
    except FileNotFoundError:
        return None
    return Path(f"/tmp/.wine-{os.getuid()}/server-{st.st_dev:x}-{st.st_ino:x}")


def _lsof_pids(d: Path) -> set[int]:
    if not d.is_dir():
        return set()
    out = subprocess.run(["lsof", "-n", "-t", "+D", str(d)], capture_output=True, text=True).stdout
    return {int(x) for x in out.split() if x.isdigit()}


def prefix_processes() -> list[dict]:
    """Processes belonging to this repo's prefix, found three ways:
    1. holding a file in this prefix's wineserver dir (lsof; covers all Windows processes),
    2. environment contains exactly WINEPREFIX=<env/wineprefix> (ps -E; covers wine64/wineserver),
    3. any Whisky-Wine process whose command line runs Wow.exe (conservative: counted as ours).
    Processes of other prefixes ([redacted]) have a different server dir and WINEPREFIX."""
    tag = f"WINEPREFIX={P.WINEPREFIX}"
    sd = server_dir()
    by_lsof = _lsof_pids(sd) if sd else set()
    found = []
    for pid, rest in _ps_env():
        if pid == os.getpid():
            continue
        cmd = rest.split(" WINEPREFIX=")[0]
        is_wine = rest.startswith(str(P.WINE_BIN))
        idx = rest.find(tag)
        env_match = idx >= 0 and rest[idx + len(tag): idx + len(tag) + 1] in ("", " ")
        wow_match = is_wine and "wow.exe" in rest.lower()
        if pid in by_lsof or (env_match and is_wine) or wow_match:
            # Trim the environment block ps -E appends, keep the command.
            for marker in (" NVM_", " PATH=", " HOME=", " TMPDIR=", " SHELL=", " USER=", " __CF"):
                cut = cmd.find(marker)
                if cut > 0:
                    cmd = cmd[:cut]
            found.append({"pid": pid, "cmd": cmd[:200],
                          "via": [k for k, v in (("server_dir", pid in by_lsof), ("env", env_match),
                                                  ("wow_cmd", wow_match)) if v]})
    return found


def wow_processes(procs=None) -> list[dict]:
    procs = prefix_processes() if procs is None else procs
    # `wine64 Wow.exe` also spawns a `start.exe /exec Wow.exe` helper; that isn't the client.
    return [p for p in procs if "wow.exe" in p["cmd"].lower() and "wowerror" not in p["cmd"].lower()
            and "start.exe" not in p["cmd"].lower()]


def _ps_stats(pid: int) -> dict:
    r = subprocess.run(["ps", "-o", "pid=,rss=,time=,etime=,state=", "-p", str(pid)],
                       capture_output=True, text=True).stdout.split()
    if len(r) < 5:
        return {"pid": pid, "alive": False}
    return {"pid": pid, "alive": True, "rss_mb": round(int(r[1]) / 1024, 1), "cpu_time": r[2],
            "elapsed": r[3], "state": r[4]}


# ---------------------------------------------------------------- pieces

def _require_wine():
    for b in (WINE, WINESERVER):
        if not os.access(b, os.X_OK):
            raise LaunchError(f"Wine layer missing: {b} (ADR-0004 expects Whisky's bundled Wine; "
                              f"install Whisky.app and open it once so it downloads its Wine)")


def _installed_client() -> dict:
    from .clientinst import load_manifest
    man = load_manifest()
    if not man or not (P.CLIENT / "Wow.exe").is_file():
        raise LaunchError("no installed client: run `harness/hs client-install <zip-or-dir>` first",
                          {"manifest": str(P.CLIENT_MANIFEST), "client": str(P.CLIENT)})
    return man


def _binary_hashes(man: dict) -> dict:
    from .clientinst import sha256_file
    return {rel: sha256_file(P.CLIENT / rel) for rel in man["binaries_sha256"]}


def _logf():
    P.LOGS.mkdir(parents=True, exist_ok=True)
    return P.WINE_LOG.open("ab")


def _stamp(msg: str):
    with _logf() as f:
        f.write(f"\n===== [hs launch] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg} =====\n".encode())


def ensure_prefix() -> dict:
    """Create env/wineprefix on first use (`wineboot --init`). Never touches any other prefix."""
    if (P.WINEPREFIX / "system.reg").exists():
        return {"prefix": str(P.WINEPREFIX), "created": False}
    P.WINEPREFIX.mkdir(parents=True, exist_ok=True)
    _stamp("wineboot --init (new prefix)")
    t0 = time.time()
    with _logf() as lf:
        r = subprocess.run([str(WINE), "wineboot", "--init"], env=wine_env(), stdout=lf,
                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=600)
        subprocess.run([str(WINESERVER), "-w"], env=wine_env(), stdout=lf, stderr=subprocess.STDOUT,
                       timeout=300)
    ok = (P.WINEPREFIX / "system.reg").exists()
    info = {"prefix": str(P.WINEPREFIX), "created": True, "wineboot_exit": r.returncode,
            "seconds": round(time.time() - t0, 1)}
    if not ok:
        raise LaunchError("wineboot did not create the prefix", info | {"wine_log_tail": tail(P.WINE_LOG, 40)})
    return info


def start_client() -> subprocess.Popen:
    _stamp(f"start {P.CLIENT / 'Wow.exe'}")
    lf = _logf()
    try:
        return subprocess.Popen([str(WINE), "Wow.exe"], cwd=P.CLIENT, env=wine_env(), stdout=lf,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    finally:
        lf.close()


def stop_prefix(timeout: float = 30) -> dict:
    """`wineserver -k` for this prefix only, then wait for every prefix process to exit."""
    t0 = time.time()
    with _logf() as lf:
        r = subprocess.run([str(WINESERVER), "-k"], env=wine_env(), stdout=lf, stderr=subprocess.STDOUT,
                           timeout=60)
    remaining = prefix_processes()
    while remaining and time.time() - t0 < timeout:
        time.sleep(0.5)
        remaining = prefix_processes()
    info = {"wineserver_k_exit": r.returncode, "seconds": round(time.time() - t0, 1),
            "remaining_after_k": remaining}
    if remaining:                        # last resort; still reported as a failure by --smoke
        for p in remaining:
            try:
                os.kill(p["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(1)
        info["sigkilled"] = [p["pid"] for p in remaining]
        info["remaining_after_sigkill"] = prefix_processes()
    return info


# ---------------------------------------------------------------- entry point


_JXA_WINDOWS = """
ObjC.import("CoreGraphics");
var l = ObjC.castRefToObject($.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly, 0));
var out = [];
for (var i = 0; i < l.count; i++) { var w = l.objectAtIndex(i), b = w.objectForKey("kCGWindowBounds");
  out.push([ObjC.unwrap(w.objectForKey("kCGWindowOwnerPID")), ObjC.unwrap(b.objectForKey("Width")),
            ObjC.unwrap(b.objectForKey("Height")), ObjC.unwrap(w.objectForKey("kCGWindowLayer"))].join(" ")); }
out.join("\\n")
"""


def onscreen_windows(pid: int) -> list[dict]:
    """On-screen windows owned by pid, via the window server (no Screen Recording permission needed)."""
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", _JXA_WINDOWS], capture_output=True, text=True, timeout=20)
    wins = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0] == str(pid):
            wins.append({"width": int(float(parts[1])), "height": int(float(parts[2])), "layer": int(parts[3])})
    return wins

def run(smoke: bool = False, smoke_seconds: int = SMOKE_SECONDS) -> dict:
    from . import fixtures, stack
    from .clientcfg import WINE_DEFAULTS, configure

    _require_wine()
    man = _installed_client()
    ev: dict = {"mode": "smoke" if smoke else "normal", "client": str(P.CLIENT),
                "wine": str(WINE), "wineprefix": str(P.WINEPREFIX), "wine_log": str(P.WINE_LOG),
                "env": {k: v for k, v in wine_env().items() if k.startswith("WINE")}}

    already = wow_processes()
    if already:
        raise LaunchError("Wow.exe is already running in this prefix; quit it first "
                          "(or `WINEPREFIX=env/wineprefix wineserver -k`)", ev | {"running": already})

    st = stack.stack_processes()
    if all(st.values()):
        ev["stack"] = "already up"
    else:
        log("stack not fully up; starting it")
        ev["stack"] = {"up": stack.up()}
    cfg = configure(P.CLIENT, defaults=WINE_DEFAULTS)
    ev["client_config"] = {"changed_files": cfg["changed_files"],
                           "config_wtf": next(c for c in cfg["changes"] if c["kind"] == "Config.wtf")}
    ev["fixtures"] = fixtures.ensure()
    ev["prefix"] = ensure_prefix()

    hashes_before = _binary_hashes(man)
    ev["binaries_match_manifest_before"] = hashes_before == man["binaries_sha256"]
    if not ev["binaries_match_manifest_before"]:
        raise LaunchError("client binaries no longer match env/client-manifest.json; "
                          "re-run `harness/hs client-install`", ev)

    log_offset = P.WINE_LOG.stat().st_size if P.WINE_LOG.exists() else 0
    proc = start_client()
    ev["launcher_pid"] = proc.pid
    login = {"account": P.TEST_USER, "password": P.TEST_PASS, "character": P.TEST_CHAR,
             "realm": f"{P.HOST}:{P.WORLD_PORT}"}
    if not smoke:
        time.sleep(3)
        ev["wow_processes"] = wow_processes()
        if proc.poll() is not None and not ev["wow_processes"]:
            raise LaunchError(f"Wow.exe exited immediately (code {proc.returncode})",
                              ev | {"wine_log_tail": tail(P.WINE_LOG, 40)})
        log(f"Wow.exe started under Wine (log: {P.WINE_LOG}). Log in with "
            f"{P.TEST_USER} / {P.TEST_PASS}")
        print(f"\n  Account: {P.TEST_USER}\n  Password: {P.TEST_PASS}\n  Character: {P.TEST_CHAR}\n")
        return ev | {"login": login}

    # --smoke: watch for crashes for smoke_seconds, sampling once a second.
    t0 = time.time()
    samples, crashed = [], None
    seen_wow = False
    errors_dir = P.CLIENT / "Errors"
    errors_before = {p.name for p in errors_dir.iterdir()} if errors_dir.is_dir() else set()
    while time.time() - t0 < smoke_seconds:
        time.sleep(1)
        procs = prefix_processes()
        wow = wow_processes(procs)
        seen_wow = seen_wow or bool(wow)
        if any("wowerror" in p["cmd"].lower() for p in procs):
            crashed = "WowError.exe (the client's crash reporter) started"
            break
        if seen_wow and not wow:
            crashed = "Wow.exe process exited"
            break
        if not seen_wow and time.time() - t0 > 15:
            crashed = "no Wow.exe process appeared within 15 s"
            break
        if int(time.time() - t0) % 10 == 0 and wow:
            samples.append({"t": round(time.time() - t0), **_ps_stats(wow[0]["pid"])})
    elapsed = round(time.time() - t0, 1)
    procs = prefix_processes()
    wow = wow_processes(procs)
    ev["smoke"] = {"watched_seconds": elapsed, "required_seconds": smoke_seconds,
                   "samples": samples, "prefix_processes_at_end": procs,
                   "wow_at_end": [_ps_stats(p["pid"]) | {"cmd": p["cmd"]} for p in wow]}
    errors_after = {p.name for p in errors_dir.iterdir()} if errors_dir.is_dir() else set()
    ev["smoke"]["new_error_reports"] = sorted(errors_after - errors_before)
    alive_ok = crashed is None and bool(wow) and elapsed >= smoke_seconds
    # graphics-translation errors logged by Wine during this run only (log offset recorded at launch)
    try:
        with open(P.WINE_LOG, errors="replace") as fh:
            fh.seek(log_offset)
            gfx_errors = [l.rstrip() for l in fh if "err:d3d" in l or "err:opengl" in l]
    except FileNotFoundError:
        gfx_errors = []
    ev["smoke"]["graphics_errors"] = {"count": len(gfx_errors), "first": gfx_errors[:5]}
    # A live process alone could be hung before creating its window; require a real on-screen game window.
    ev["smoke"]["windows"] = onscreen_windows(wow[0]["pid"]) if wow else []
    window_ok = any(w["layer"] == 0 and w["width"] >= 640 and w["height"] >= 480 for w in ev["smoke"]["windows"])

    ev["stop"] = stop_prefix()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    ev["remaining_prefix_processes"] = prefix_processes()
    ev["wow_remaining"] = [p for p in ev["remaining_prefix_processes"] if "wow.exe" in p["cmd"].lower()]
    hashes_after = _binary_hashes(man)
    ev["binaries_match_manifest_after"] = hashes_after == man["binaries_sha256"]
    ev["wine_log_tail"] = tail(P.WINE_LOG, 25)

    if not alive_ok:
        raise LaunchError(f"smoke: {crashed or 'Wow.exe not running at the end of the window'} "
                          f"after {elapsed}s", ev)
    if gfx_errors:
        raise LaunchError(f"smoke: Wine logged {len(gfx_errors)} graphics errors (err:d3d/err:opengl) during the run", ev)
    if not window_ok:
        raise LaunchError("smoke: Wow.exe has no on-screen window of at least 640x480", ev)
    if ev["smoke"]["new_error_reports"]:
        raise LaunchError("smoke: the client wrote a crash report to client/Errors/", ev)
    if ev["remaining_prefix_processes"]:
        raise LaunchError("smoke: prefix processes remain after wineserver -k", ev)
    if "sigkilled" in ev["stop"]:
        raise LaunchError("smoke: wineserver -k didn't stop everything; SIGKILL was needed", ev)
    if not ev["binaries_match_manifest_after"]:
        raise LaunchError("smoke: a client binary changed during the run", ev)
    ev["smoke"]["ok"] = True
    return ev
