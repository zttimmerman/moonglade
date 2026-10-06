"""`hs launch [--smoke]`: start the installed 3.3.5a client under the Wine layer (ADR-0004).

Wine layer: selected by HS_WINE (paths.WINE_LAYERS). Default `mtld3d` = WoWSilicon runtime r17 (Wine 11.13 +
mtld3d D3D9->Metal) in env/wowsilicon-r17; `whisky` = Whisky's Wine 7.7, read-only. Each layer has its own
repo-local prefix; Whisky's own bottles and settings are never touched.
No client binary is modified: the manifest's .exe/.dll hashes are compared before and after.
Test-only: HS_CLIENT_DIR points the launch at a scratch copy of the client (e.g. an APFS clone in env/, login-screen
spike B.1). It affects only what this module starts and configures; client.inspect and the manifest always describe
the real client/ (paths.CLIENT), and the scratch copy's binaries must still match that manifest.

Normal mode: stack up (if needed) → client-config (+ Wine defaults) → fixtures → start Wow.exe
detached, logging to env/logs/wine.log, and print the login.
--smoke: the same, then require Wow.exe alive after 30 s, stop the prefix (`wineserver -k`),
and require that no process of this prefix remains.
"""
from __future__ import annotations

import os
import signal
import re
import subprocess
import time
from pathlib import Path

from . import paths as P
from .util import log, tail

WINE = P.WINE_BIN / P.WINE_EXE
WINESERVER = P.WINE_BIN / "wineserver"
SMOKE_SECONDS = 30


_selected: Path | None = None       # set by ./play for the play client (select_client); None = client/


def client_dir() -> Path:
    """The client directory to launch: HS_CLIENT_DIR (test-only scratch copy), else the client ./play selected
    (select_client: the play client env/client-play/ in `client = patched` mode), else the real client/."""
    v = os.environ.get("HS_CLIENT_DIR")
    if v:
        return Path(v).resolve()
    return _selected or P.CLIENT


def select_client(d: Path | None) -> None:
    """./play: launch `d` (a play client built by `hs client-build`) instead of client/; None = client/ again."""
    global _selected
    _selected = Path(d).resolve() if d else None


def is_play_client(d: Path | None = None) -> bool:
    from .clientbuild import STAMP
    return ((d or client_dir()) / STAMP).is_file()


def expected_binaries(man: dict) -> dict:
    """What the launched client's .exe/.dll must hash to: the manifest's, or for a play client the manifest's with
    the build's patched outputs (its build stamp) on top."""
    if is_play_client():
        from .clientbuild import expected_binaries as play_expected
        return play_expected(client_dir())
    return man["binaries_sha256"]


class LaunchError(RuntimeError):
    def __init__(self, msg, evidence: dict | None = None):
        super().__init__(msg)
        self.evidence = evidence or {}


FROM_APP_ENV = "HS_FROM_APP"          # set by Hearthstone.app (launcher.m) for its play loop
APP_BUNDLE_ENV = "HS_APP_BUNDLE"      # the launcher's bundle path (for its icon)
DOCK_TOKEN = str(P.ROOT)              # object of the launcher <-> shim distributed notifications
_dock_test = False                    # ./play --client-cmd (a test stand-in): the shim's test-only hooks may run


def allow_dock_test(on: bool) -> None:
    """./play with a --client-cmd stand-in: pass HSDOCK_TEST=1 (+ HSDOCK_TEST_* knobs) to the shim."""
    global _dock_test
    _dock_test = bool(on)


def dock_env() -> dict:
    """The Wine host shim's environment for the game client's Wine process (ADR-0010), or {} when it's off.
    App runs (HS_FROM_APP=1): the dock policy setting (default accessory: no Dock icon of its own). Terminal
    ./play: name + icon only (regular), so the game keeps a normal Dock icon. Never set anywhere else."""
    from . import dockshim, settings
    pol = settings.dock_policy()
    if pol["policy"] == "off":
        return {}
    app = os.environ.get(FROM_APP_ENV) == "1"
    bundle_icon = Path(os.environ.get(APP_BUNDLE_ENV, "")) / "Contents/Resources/AppIcon.icns"
    icon = bundle_icon if app and os.environ.get(APP_BUNDLE_ENV) and bundle_icon.is_file() \
        else P.ROOT / "branding" / "AppIcon.icns"
    env = {"DYLD_INSERT_LIBRARIES": dockshim.ensure_built(),
           "HSDOCK_POLICY": pol["policy"] if app else "regular",
           "HSDOCK_NAME": "Hearthstone", "HSDOCK_ICON": str(icon), "HSDOCK_LOG": str(dockshim.LOG),
           "HSDOCK_TOKEN": DOCK_TOKEN}
    if _dock_test:
        env["HSDOCK_TEST"] = "1"
        env |= {k: v for k, v in os.environ.items() if k.startswith("HSDOCK_TEST_")}
    return env


def wine_env(client: bool = False) -> dict:
    """Environment for every Wine call (ADR-0004): WINEPREFIX of the selected layer, WINEDEBUG=fixme-all
    (HS_WINEDEBUG overrides), msync, no Mono/Gecko/menu builder, plus the layer's own variables. For the
    default `mtld3d` layer that's d3d9=b (builtin mtld3d, D3D9->Metal), large-address-aware, and the
    runtime's external dylib path. client=True (start_client only): plus the Wine host shim (dock_env)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("WINE", "DXVK_", "MVK_", "MTL_", "D3DM_", "HSDOCK_"))
           and k != "DYLD_INSERT_LIBRARIES"}
    env.update({
        "WINEPREFIX": str(P.WINEPREFIX),
        # HS_WINEDEBUG overrides the channel spec for diagnosis (e.g. "fixme-all,fixme+d3d_shader,warn+d3d_shader")
        "WINEDEBUG": os.environ.get("HS_WINEDEBUG", "fixme-all"),
        "WINEMSYNC": "1",
    })
    # No Mono/Gecko install prompts (they block wineboot on a new prefix) and no macOS menu entries.
    overrides = ["mscoree=d", "mshtml=d", "winemenubuilder.exe=d"]
    for k, v in P.WINE_LAYERS[P.WINE_LAYER].get("env", {}).items():
        if k == "WINEDLLOVERRIDES":
            overrides += v.split(";")
        else:
            env[k] = v
    env["WINEDLLOVERRIDES"] = ";".join(overrides)
    if client:
        env |= dock_env()
    return env


# ---------------------------------------------------------------- process discovery

def _ps_env() -> list[tuple[int, str]]:
    """(pid, command+environment) for this user's processes, like macOS `ps -E -x -ww`: the environment is
    appended, which lets us match WINEPREFIX exactly. Read via procs (no /bin/ps: it can't run sandboxed)."""
    from . import procs
    return procs.table(with_env=True, own_only=True)


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


def _pids_with_open_files_under(*roots: Path) -> set[int]:
    """Processes holding any open file whose path starts with one of roots. Wine 11 (msync over Mach ports)
    processes hold no file in the wineserver dir, but always hold files under their prefix (drive_c/...) or the
    server dir's tmpmap-* files; this also catches orphans of an earlier prefix at the same path."""
    # trailing "/" = path boundary: env/wineprefix must not match env/wineprefix-mtld3d
    prefixes = tuple({str(r).rstrip("/") + "/" for r in roots}
                     | {"/private" + str(r).rstrip("/") + "/" for r in roots if str(r).startswith("/tmp/")})
    out = subprocess.run(["lsof", "-nP", "-Fpn"], capture_output=True, text=True).stdout
    pids, pid = set(), None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None and line[1:].startswith(prefixes):
            pids.add(pid)
    return pids


def _runtime_roots() -> tuple[str, ...]:
    """The pinned Wine runtime's root (paths.WINE_BIN's parent: bin/, lib/wine/x86_64-unix/wine, the preloader),
    spelled as configured and resolved, each with a trailing "/" as path boundary."""
    root = P.WINE_BIN.parent
    return tuple(sorted({str(root).rstrip("/") + "/", str(root.resolve()).rstrip("/") + "/"}))


def process_scope(pid: int, roots: tuple[str, ...] | None = None, max_depth: int = 64) -> str | None:
    """Why pid counts as one of this runtime's Wine/Wow processes (F24), or None if it doesn't:
    "runtime_exe"        its executable lives under the pinned runtime (wine, wineserver, the preloader; Windows
                         programs such as Wow.exe run as the runtime's wine binary, whatever their argv says);
    "runtime_descendant" a child (at any depth) of such a process, e.g. a helper Wine started;
    "exe_unreadable"     its executable path can't be read (e.g. exiting): counted, so leftover detection fails
                         closed rather than missing a real Wine process.
    A process whose executable is readable and lies outside the runtime, with no runtime ancestor, is not ours even
    if it holds a file open under the prefix (macOS's StorageManagementService did, and failed client.launch_smoke)."""
    from . import procs
    roots = roots or _runtime_roots()
    exe = procs.path(pid)
    if exe is None:
        return "exe_unreadable"
    if exe.startswith(roots):
        return "runtime_exe"
    cur, seen = procs.ppid(pid), {pid}
    for _ in range(max_depth):
        if not cur or cur <= 1 or cur in seen:
            return None
        seen.add(cur)
        pe = procs.path(cur)
        if pe and pe.startswith(roots):
            return "runtime_descendant"
        cur = procs.ppid(cur)
    return None


def prefix_processes(excluded: list | None = None) -> list[dict]:
    """This user's Wine/Wow processes of this repo's prefix. A process is a candidate when any of these holds:
    1. it holds a file in this prefix's wineserver dir or under the prefix (lsof; covers all Windows processes),
    2. its environment contains exactly WINEPREFIX=<env/wineprefix> (covers wine64/wineserver),
    3. it is a process of the runtime whose command line runs Wow.exe (conservative: counted as ours);
    and a candidate counts only if process_scope() places it in the pinned runtime (F24: an unrelated process that
    merely has a file open under the prefix is not ours). Candidates left out are appended to `excluded` if given.
    Processes of other prefixes ([redacted]) have a different server dir and WINEPREFIX."""
    tag = f"WINEPREFIX={P.WINEPREFIX}"
    sd = server_dir()
    by_lsof = (_lsof_pids(sd) if sd else set()) | _pids_with_open_files_under(P.WINEPREFIX, *([sd] if sd else []))
    roots = _runtime_roots()
    found = []
    for pid, rest in _ps_env():
        if pid == os.getpid():
            continue
        cmd = rest.split(" WINEPREFIX=")[0]
        is_wine = str(P.WINE_BIN.parent) in rest.split(" ")[0] or rest.startswith(str(P.WINE_BIN))
        idx = rest.find(tag)
        env_match = idx >= 0 and rest[idx + len(tag): idx + len(tag) + 1] in ("", " ")
        wow_match = is_wine and "wow.exe" in rest.lower()
        if pid in by_lsof or (env_match and is_wine) or wow_match:
            # Trim the environment block ps -E appends, keep the command.
            for marker in (" NVM_", " PATH=", " HOME=", " TMPDIR=", " SHELL=", " USER=", " __CF"):
                cut = cmd.find(marker)
                if cut > 0:
                    cmd = cmd[:cut]
            via = [k for k, v in (("server_dir", pid in by_lsof), ("env", env_match), ("wow_cmd", wow_match)) if v]
            scope = process_scope(pid, roots)
            if scope is None:
                if excluded is not None:
                    from . import procs
                    excluded.append({"pid": pid, "cmd": cmd[:200], "via": via, "exe": procs.path(pid)})
                continue
            found.append({"pid": pid, "cmd": cmd[:200], "via": via, "scope": scope})
    return found


# The real client process only, never the launcher (observed with a 0.2 s trace, see the M1 change):
#   mtld3d (Wine 11.13): launcher pid `.../bin/wine Wow.exe` -> `.../x86_64-unix/wine Wow.exe` -> `start.exe /exec`
#                        (it waits); the client is a separate pid whose argv is the Windows path `Z:\...\Wow.exe`.
#   whisky (Wine 7.7):   one pid: `.../wine64 Wow.exe` -> `.../wine64-preloader /.../wine64 Wow.exe` (still
#                        loading) -> `.../wine64-preloader Wow.exe` (the client itself).
_WOW_WINDOWS = re.compile(r"^(?:[a-z]:\\\S*\\)?wow\.exe(?:\s|$)", re.I)
_WOW_PRELOADER = re.compile(r"/wine(?:64)?-preloader (?:[a-z]:\\\S*\\)?wow\.exe(?:\s|$)", re.I)


def is_wow_client(cmd: str) -> bool:
    return bool(_WOW_WINDOWS.match(cmd) or _WOW_PRELOADER.search(cmd))


def wow_processes(procs=None) -> list[dict]:
    procs = prefix_processes() if procs is None else procs
    return [p for p in procs if is_wow_client(p["cmd"])]


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
            raise LaunchError(f"Wine layer missing: {b} (ADR-0004: run `harness/hs wine-install` to fetch the pinned "
                              f"Wine runtime; HS_WINE=whisky needs Whisky.app's Wine)")


def _installed_client() -> dict:
    from .clientinst import load_manifest
    man = load_manifest()
    if not man or not (client_dir() / "Wow.exe").is_file():
        raise LaunchError("no installed client: run `harness/hs client-install <zip-or-dir>` first",
                          {"manifest": str(P.CLIENT_MANIFEST), "client": str(client_dir())})
    return man


def _binary_hashes(man: dict) -> dict:
    from .clientinst import sha256_file
    return {rel: sha256_file(client_dir() / rel) for rel in man["binaries_sha256"]}


def _logf():
    P.LOGS.mkdir(parents=True, exist_ok=True)
    return P.WINE_LOG.open("ab")


def _stamp(msg: str):
    with _logf() as f:
        f.write(f"\n===== [hs launch] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg} =====\n".encode())


def _prefix_complete() -> bool:
    """system.reg appears early in wineboot; Wow.exe (32-bit) needs the WoW64 side, so require syswow64's kernel32.
    A half-built prefix otherwise fails later with "could not load kernel32.dll, status c0000135"."""
    return (P.WINEPREFIX / "system.reg").exists() and (P.WINEPREFIX / "drive_c/windows/syswow64/kernel32.dll").exists()


def ensure_prefix() -> dict:
    """Create env/wineprefix on first use (`wineboot --init`). Never touches any other prefix."""
    if _prefix_complete():
        return {"prefix": str(P.WINEPREFIX), "created": False}
    P.WINEPREFIX.mkdir(parents=True, exist_ok=True)
    _stamp("wineboot --init (new prefix)")
    t0 = time.time()
    with _logf() as lf:
        r = subprocess.run([str(WINE), "wineboot", "--init"], env=wine_env(), stdout=lf,
                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=600)
        subprocess.run([str(WINESERVER), "-w"], env=wine_env(), stdout=lf, stderr=subprocess.STDOUT,
                       timeout=300)
    ok = _prefix_complete()
    info = {"prefix": str(P.WINEPREFIX), "created": True, "wineboot_exit": r.returncode,
            "seconds": round(time.time() - t0, 1)}
    if not ok:
        raise LaunchError("wineboot did not create the prefix", info | {"wine_log_tail": tail(P.WINE_LOG, 40)})
    return info


def start_client(args: list[str] | None = None, cwd: Path | None = None) -> subprocess.Popen:
    """Start Wow.exe (default) or `args` (a Windows program + arguments, e.g. a `./play --client-cmd` test
    stand-in) under the Wine layer, detached, logging to env/logs/wine.log."""
    args = args or ["Wow.exe"]
    cwd = cwd or client_dir()
    _stamp(f"start {' '.join(args)} (cwd {cwd})")
    lf = _logf()
    try:
        return subprocess.Popen([str(WINE), *args], cwd=cwd, env=wine_env(client=True), stdout=lf,
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


def all_onscreen_windows() -> list[dict]:
    """Every on-screen window (owner pid, size, layer), via the window server (no Screen Recording permission).
    [] if the window server can't be asked (e.g. under a sandbox): callers treat that as "no window yet"."""
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", _JXA_WINDOWS], capture_output=True, text=True,
                           timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    wins = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0].isdigit():
            wins.append({"pid": int(parts[0]), "width": int(float(parts[1])), "height": int(float(parts[2])),
                         "layer": int(parts[3])})
    return wins


def is_wine_host(pid: int) -> bool:
    """pid runs this layer's Wine (its executable lives under the runtime). Windows processes rewrite their argv and
    environment, so the executable path is what identifies them."""
    from . import procs
    exe = procs.path(pid) or ""
    return exe.startswith(str(P.WINE_BIN.parent.resolve()) + "/") or exe.startswith(str(P.WINE_BIN.parent) + "/")


CLIENT_WINDOW_MIN = (300, 200)     # a real game/stand-in window (Wow.exe opens >= 640x480; notepad ~924x697)


def client_window() -> dict | None:
    """The first on-screen layer-0 window of at least CLIENT_WINDOW_MIN owned by a process of this prefix, as
    {"pid", "width", "height"}; None if there's none yet. Used by ./play for launching -> ready."""
    cands = [w for w in all_onscreen_windows()
             if w["layer"] == 0 and w["width"] >= CLIENT_WINDOW_MIN[0] and w["height"] >= CLIENT_WINDOW_MIN[1]
             and is_wine_host(w["pid"])]
    if not cands:
        return None
    mine = {p["pid"] for p in prefix_processes()}          # this prefix only (not another Wine of the same runtime)
    for w in cands:
        if w["pid"] in mine:
            return {"pid": w["pid"], "width": w["width"], "height": w["height"]}
    return None


def onscreen_windows(pid: int) -> list[dict]:
    """On-screen windows owned by pid, via the window server (no Screen Recording permission needed)."""
    r = subprocess.run(["osascript", "-l", "JavaScript", "-e", _JXA_WINDOWS], capture_output=True, text=True, timeout=20)
    wins = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0] == str(pid):
            wins.append({"width": int(float(parts[1])), "height": int(float(parts[2])), "layer": int(parts[3])})
    return wins

def ensure_world(say=log, world_datadir: Path | None = None, profile: str | None = None,
                 population_n: int | None = None, stages=None) -> dict:
    """The stack step of `prepare` (./play, `hs launch`): start the stack with `profile` / `population_n` if it
    isn't running. When a profile is asked for (./play passes its world-size setting) and a worldserver left
    running by `hs up`, a harness run or an earlier session holds another profile or size, restart the world server
    rather than silently keeping the wrong world (m2-living-world design.md, "./play and an already-running
    stack"). profile=None (`hs launch`) keeps whatever runs. Returns evidence."""
    from . import config, stack
    ev: dict = {}
    st = stack.stack_processes()
    if profile is not None and st["worldserver"] and not stack.world_matches(profile, population_n):
        was = stack.running_profile()
        want = {"profile": profile, "n": config.population_n(population_n) if profile == "population" else None}
        say(f"The game server is running {stack.describe_world(was)}, not {stack.describe_world(want)}; "
            "restarting the world server...")
        ev["restarted_world"] = {"was": was, "want": want, "stop": stack.stop_worldserver(was["pid"])}
        st = stack.stack_processes()
    if all(st.values()):
        ev["stack"] = "already up"
        if stages:
            for sid in ("database", "login", "world"):
                stages.skip(sid)
        say("The game server is already running.")
    else:
        say("Starting the game server (database, login server, world server). This takes a minute or two...")
        ev["stack"] = {"up": stack.up(world_datadir=world_datadir, profile=profile, population_n=population_n,
                                      on_stage=stages.on_stack if stages else None)}
        say("The game server is ready.")
    return ev


def bot_target() -> int | None:
    """How many random AI players the running world aims for (its profile's AiPlayerbot.MaxRandomBots)."""
    from . import config, stack
    rp = stack.running_profile()
    if not rp:
        return None
    try:
        if rp["profile"] == "population":
            return config.population_n(rp.get("n"))
        return int(config.overrides(rp["profile"])["playerbots"]["AiPlayerbot.MaxRandomBots"])
    except (KeyError, ValueError, TypeError):
        return None


def bot_count() -> dict:
    """{"online": random bots online in the DB, "target": the world's target}; online None if the DB can't say."""
    from . import population
    try:
        online = population.online_bots()
    except Exception:  # noqa: BLE001  (informational only: never fail the start over the bot count)
        online = None
    return {"online": online, "target": bot_target()}


def bots_label(b: dict) -> str:
    return f"AI players: {b['online'] if b.get('online') is not None else '?'} / {b.get('target') or '?'} online"


def prepare(say=log, world_datadir: Path | None = None, accounts=None, profile: str | None = None,
            population_n: int | None = None, stages=None, set_resolution: bool = True) -> tuple[dict, dict]:
    """Everything before starting the client, shared by `hs launch` and `./play`: stack up (if needed) →
    client-config (+ Wine defaults) → accounts → Wine prefix → binaries match the manifest. `accounts` makes
    sure the login exists (default: the harness fixtures, test account + character; `./play` passes its
    player-account step). `say` reports each step. Raises stack.StackError / LaunchError / FixtureError.
    Returns (manifest, ev)."""
    from . import fixtures
    from .clientcfg import WINE_DEFAULTS, configure

    _require_wine()
    if os.environ.get("HS_CLIENT_DIR"):
        bar = "!" * 78
        log(f"{bar}\nWARNING: HS_CLIENT_DIR is set: launching the TEST client copy at {client_dir()}\n"
            f"         (not the real client at {P.CLIENT}). Unset HS_CLIENT_DIR to play normally.\n{bar}")
    man = _installed_client()
    ev: dict = {"client": str(client_dir()), "client_override": bool(os.environ.get("HS_CLIENT_DIR")),
                "wine": str(WINE), "wineprefix": str(P.WINEPREFIX), "wine_log": str(P.WINE_LOG),
                "env": {k: v for k, v in wine_env().items() if k.startswith("WINE")}}

    already = wow_processes()
    if already:
        raise LaunchError("Wow.exe is already running in this prefix; quit it first "
                          "(or `WINEPREFIX=env/wineprefix wineserver -k`)", ev | {"running": already})

    ev |= ensure_world(say, world_datadir, profile, population_n, stages=stages)
    if stages:
        # informational only (Zach, 2026-10-04): the game launches when the world is ready, bots keep arriving
        b = bot_count()
        stages.extra["bots"] = b
        frac = round(min(b["online"] / b["target"], 1.0), 4) if b["online"] is not None and b["target"] else None
        stages.start("bots", label=bots_label(b), fraction=frac)
        stages.done("bots", label=bots_label(b))
        stages.start("account")
    if accounts is None:
        say(f"Making sure the test account ({P.TEST_USER}) and character ({P.TEST_CHAR}) exist...")
        ev["fixtures"] = fixtures.ensure()
    else:
        ev["accounts"] = accounts()
    if stages:
        stages.start("game_setup")
    say("Pointing the game client at this computer (127.0.0.1)...")
    cfg = configure(client_dir(), defaults=WINE_DEFAULTS)
    ev["client_config"] = {"changed_files": cfg["changed_files"],
                           "config_wtf": next(c for c in cfg["changes"] if c["kind"] == "Config.wtf")}
    if is_play_client() and not set_resolution:
        ev["resolution"] = {"skipped": "not a genuine player launch, and this is the player's own play client"}
    elif is_play_client():          # launcher-fixes: a resolution matching the display, unless the player chose one
        from . import resolution
        try:
            ev["resolution"] = resolution.apply(client_dir())
        except resolution.ResolutionError as e:     # never block a launch over the default resolution
            ev["resolution"] = {"error": str(e)}       # (no new [play] line: the play loop's wording stays as it was)
    ev["prefix"] = ensure_prefix()

    hashes_before = _binary_hashes(man)
    ev["binaries_match_manifest_before"] = hashes_before == expected_binaries(man)
    ev["play_client"] = is_play_client()
    if not ev["binaries_match_manifest_before"]:
        raise LaunchError("client binaries no longer match env/client-manifest.json "
                          + ("plus the play client's build stamp; re-run `harness/hs client-build`" if ev["play_client"]
                             else "; re-run `harness/hs client-install`"), ev)
    return man, ev


def run(smoke: bool = False, smoke_seconds: int = SMOKE_SECONDS) -> dict:
    man, ev = prepare()
    ev["mode"] = "smoke" if smoke else "normal"

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

    # --smoke: wait for the real Wow.exe process (not the launcher), then require that same pid to stay alive
    # for smoke_seconds measured from when it was first seen. Sampled once a second; a pid counts as gone only
    # when two consecutive checks (0.5 s apart) find it dead.
    t0 = time.time()
    samples, crashed = [], None
    wow_pid, t_seen = None, None
    errors_dir = client_dir() / "Errors"
    errors_before = {p.name for p in errors_dir.iterdir()} if errors_dir.is_dir() else set()
    from .stack import alive

    def gone(pid):
        if alive(pid):
            return False
        time.sleep(0.5)
        return not alive(pid)

    while True:
        time.sleep(1)
        procs = prefix_processes()
        if any("wowerror" in p["cmd"].lower() for p in procs):
            crashed = "WowError.exe (the client's crash reporter) started"
            break
        if wow_pid is None:
            wow = wow_processes(procs)
            if wow:
                wow_pid, t_seen = wow[0]["pid"], time.time()
                ev["wow_first_seen"] = {"pid": wow_pid, "cmd": wow[0]["cmd"], "after_launch_s": round(t_seen - t0, 1)}
            elif time.time() - t0 > 15:
                crashed = "no Wow.exe process appeared within 15 s"
                break
            continue
        if gone(wow_pid):
            crashed = f"Wow.exe process (pid {wow_pid}) exited"
            break
        if int(time.time() - t_seen) % 10 == 0:
            samples.append({"t": round(time.time() - t_seen), **_ps_stats(wow_pid)})
        if time.time() - t_seen >= smoke_seconds:
            break
    elapsed = round(time.time() - t_seen, 1) if t_seen else 0.0
    procs = prefix_processes()
    wow = [p for p in wow_processes(procs) if p["pid"] == wow_pid]
    ev["smoke"] = {"watched_seconds": elapsed, "watched_from": "first sighting of the real Wow.exe process",
                   "required_seconds": smoke_seconds, "wow_pid": wow_pid,
                   "samples": samples, "prefix_processes_at_end": procs,
                   "wow_at_end": [_ps_stats(p["pid"]) | {"cmd": p["cmd"]} for p in wow]}
    errors_after = {p.name for p in errors_dir.iterdir()} if errors_dir.is_dir() else set()
    ev["smoke"]["new_error_reports"] = sorted(errors_after - errors_before)
    alive_ok = crashed is None and bool(wow) and elapsed >= smoke_seconds
    # graphics-translation errors logged by Wine during this run only (log offset recorded at launch)
    try:
        with open(P.WINE_LOG, errors="replace") as fh:
            fh.seek(log_offset)
            # wined3d/opengl32 log Wine-style ("err:d3d"); mtld3d logs Rust-style ("ERROR mtld3d::...", may be ANSI-coloured)
            gfx_errors = [l.rstrip() for l in fh
                          if "err:d3d" in l or "err:opengl" in l or re.search(r"ERROR\W*(\x1b\[[0-9;]*m)*\s*mtld3d", l)]
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
    ev["binaries_match_manifest_after"] = hashes_after == expected_binaries(man)
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
