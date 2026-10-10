"""Repo-relative paths, ports, and fork.lock parsing.

Lanes (ADR-0011, openspec/changes/second-stack): `HS_LANE` (a|b, default a) is read once, here. Lane A is the
original layout, unchanged. Lane B is the harness's second, isolated test stack: every runtime path (run, logs, tmp,
server confs, my.cnf, client.cnf, mysqld datadir, Wine prefix) lives under env/lanes/b/ and every port is lane B's.
Player paths (env/player.json, env/settings.json, env/backups, env/client-play), the run registry
(GLOBAL_RUN/harness-runs) and the read-only shared inputs (server/bin, client data, client/, the Wine runtime) are
the same in both lanes. Lane B refuses to run outside a harness run (lane_refusal).

Modes (release-first-launch, openspec/changes/release-first-launch design 1): MODE is decided once, here. A git
checkout (ROOT has .git) is always "dev" with the layout above, byte for byte (release.mode_resolution compares the
whole table with one recorded from the parent commit). A release payload (ROOT has release-build's VERSION file and no
.git) is "release": APP (= ROOT, the payload folder inside Moonglade.app) is read-only, and every path Moonglade writes
is under HOME (~/Library/Application Support/Moonglade). In dev mode APP = HOME = ROOT. Release mode runs lane A only.
Release test switches (RELEASE_TEST_SWITCHES) are honoured only with --allow-test-release on the command line inside a
live harness run; test_release_refusal() is what the entry points refuse with.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _detect_mode(root: Path) -> str:
    """'release' iff `root` holds release-build's VERSION (a JSON object) and no .git; otherwise 'dev'."""
    if (root / ".git").exists():
        return "dev"
    try:
        v = json.loads((root / "VERSION").read_text())
    except (OSError, ValueError):
        return "dev"
    return "release" if isinstance(v, dict) and v.get("version") and v.get("commit") else "dev"


MODE = _detect_mode(ROOT)
APP = ROOT                         # read-only inputs (dev: the checkout; release: Moonglade.app/Contents/Resources/Moonglade)

# ---------------------------------------------------------------------------------- release test switches (design 1)
# Test-only. Every one is refused (test_release_refusal) unless the command line carries RELEASE_TEST_FLAG and the
# process is inside a live harness run; the launcher strips them from the play loop's environment.
RELEASE_TEST_FLAG = "--allow-test-release"
RELEASE_HOME_ENV = "HS_RELEASE_HOME"                  # a scratch HOME (only under .../harness/out/release-scratch/)
RELEASE_PORTS_ENV = "HS_RELEASE_PORTS"                # scratch ports, JSON {"mysql","auth","world","soap"}
RELEASE_DOWNLOAD_BASE_ENV = "HS_RELEASE_DOWNLOAD_BASE"   # loopback URL base for the pinned downloads (hashes unchanged)
FIRSTLAUNCH_TEST_ENV = "HS_FIRSTLAUNCH_TEST"          # injected Mac facts / counterexamples (JSON)
APP_TEST_CLIENT_PICK_ENV = "HS_APP_TEST_CLIENT_PICK"  # the launcher's scripted folder pick (with its test hook only)
RELEASE_TEST_SWITCHES = (RELEASE_HOME_ENV, RELEASE_PORTS_ENV, RELEASE_DOWNLOAD_BASE_ENV, FIRSTLAUNCH_TEST_ENV,
                         APP_TEST_CLIENT_PICK_ENV)
HARNESS_RUN_REGISTRY_ENV = "HS_HARNESS_RUN_REGISTRY"  # release mode only: the dev checkout's run registry (a check)
SCRATCH_PARTS = ("harness", "out", "release-scratch")   # a scratch HOME lives below a folder ending in these


def _user_home() -> Path:
    """The current user's home folder from the user database (not $HOME, which a sandbox or a test may change)."""
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


RELEASE_HOME_DEFAULT = _user_home() / "Library" / "Application Support" / "Moonglade"


def _registry_dir(environ) -> Path:
    if MODE == "release":
        reg = environ.get(HARNESS_RUN_REGISTRY_ENV) or ""
        # only a dev checkout's registry: <checkout>/env/run/harness-runs
        return Path(reg) if reg and Path(reg).parts[-3:] == ("env", "run", "harness-runs") else Path("/nonexistent")
    return ROOT / "env" / "run" / "harness-runs"


def _live_run_id(environ) -> str | None:
    rid = environ.get("HS_HARNESS_RUN_ID") or ""
    if environ.get("HS_HARNESS_RUN") != "1" or not rid or "/" in rid or not (_registry_dir(environ) / f"{rid}.json").is_file():
        return None
    pid, _, st = rid.partition("-")
    if not pid.isdigit():
        return None
    from . import procs                     # procs imports nothing from here
    got = procs.start_time(int(pid))
    return rid if got is not None and f"{pid}-{got}" == rid else None


def release_switches_set(environ=None) -> list[str]:
    env = os.environ if environ is None else environ
    return [k for k in RELEASE_TEST_SWITCHES if env.get(k)]


def test_release_refusal(environ=None, argv=None) -> str | None:
    """Why the release test switches set in `environ` mustn't be honoured, or None (none set, or the explicit test
    flag is in `argv` and this is a live harness run). Entry points refuse with this message before starting
    anything; it names every switch that is set."""
    env = os.environ if environ is None else environ
    args = sys.argv[1:] if argv is None else argv
    names = release_switches_set(env)
    if not names:
        return None
    if RELEASE_TEST_FLAG in args and _live_run_id(env) is not None:
        return None
    why = (f"without {RELEASE_TEST_FLAG}" if RELEASE_TEST_FLAG not in args else "outside a live harness run")
    return (f"{', '.join('$' + n for n in names)} {'is a test switch' if len(names) == 1 else 'are test switches'} of "
            f"the harness's release checks and {'is' if len(names) == 1 else 'are'} refused {why}. Unset "
            f"{'it' if len(names) == 1 else 'them'} (unset {' '.join(names)}) and try again.")


def release_switch(name: str, environ=None, argv=None) -> str | None:
    """The value of release test switch `name` when it may be honoured, else None (never raises)."""
    env = os.environ if environ is None else environ
    if not env.get(name) or test_release_refusal(env, argv) is not None:
        return None
    return env[name]


def firstlaunch_test(environ=None, argv=None) -> dict:
    """$HS_FIRSTLAUNCH_TEST as a dict when it may be honoured (test flag + live harness run), else {}."""
    raw = release_switch(FIRSTLAUNCH_TEST_ENV, environ, argv)
    if raw is None:
        return {}
    try:
        got = json.loads(raw)
    except ValueError:
        got = None
    if not isinstance(got, dict):
        raise SystemExit(f"${FIRSTLAUNCH_TEST_ENV} must be a JSON object; unset it")
    return got


def scratch_home_problem(home: Path) -> str | None:
    """Why `home` can't be a scratch HOME: it must be absolute, below a folder .../harness/out/release-scratch/, not
    the real HOME and not inside APP."""
    if not home.is_absolute():
        return f"{home} isn't an absolute path"
    h = Path(os.path.abspath(home))
    real = Path(os.path.abspath(RELEASE_HOME_DEFAULT))
    if h == real or real in h.parents or h in real.parents:
        return f"{h} is (or contains) the real Moonglade folder {real}"
    if h == APP or APP in h.parents:
        return f"{h} is inside the app ({APP})"
    if not any(h.parts[i:i + 3] == SCRATCH_PARTS for i in range(len(h.parts) - 3)):
        return f"{h} isn't below a .../harness/out/release-scratch/ folder"
    return None


def _release_home() -> Path:
    raw = release_switch(RELEASE_HOME_ENV)
    if raw is None:
        return RELEASE_HOME_DEFAULT
    why = scratch_home_problem(Path(raw))
    if why:
        raise SystemExit(f"${RELEASE_HOME_ENV}: {why}; unset it")
    return Path(os.path.abspath(raw))


BREW = Path("/opt/homebrew") if MODE == "dev" else None   # dev only: Homebrew (a release never reads it)
HOME = _release_home() if MODE == "release" else ROOT   # every path Moonglade writes (dev: the checkout, as today)
HOME_FILE = HOME / "home.json" if MODE == "release" else None     # release: layout version + first-launch record
HOME_LAYOUT_VERSION = 1

HARNESS = ROOT / "harness"
VENDOR = ROOT / "vendor"
CORE_SRC = VENDOR / "azerothcore-wotlk"
BOTS_SRC = VENDOR / "mod-playerbots"
BUILD = ROOT / "build"
SERVER = ROOT / "server"          # cmake install prefix (release: APP/server, the payload's)
SERVER_BIN = SERVER / "bin"
SERVER_ETC = SERVER / "etc"       # the installed *.conf.dist (lane A also renders its confs here; release: read-only)
ENV = ROOT / "env" if MODE == "dev" else HOME     # runtime: config, logs, pidfiles, sockets
GLOBAL_RUN = ENV / "run"          # lane A's run dir; also home of the run registry (harness-runs/), shared by lanes
DATA = ROOT / "data" if MODE == "dev" else HOME
CLIENT_DATA = DATA / "client-data"
DOWNLOADS = DATA / "downloads"
PATCHES = ROOT / "patches" / "core"
BOTS_PATCHES = ROOT / "patches" / "playerbots"   # mod-playerbots patch queue (ADR-0008), applied like PATCHES
MODULES = ROOT / "modules"
OUT = HARNESS / "out" if MODE == "dev" else HOME / "tmp" / "harness-out"
RESULTS = HARNESS / "results.json" if MODE == "dev" else OUT / "results.json"
SUMMARY = HARNESS / "summary.md" if MODE == "dev" else OUT / "summary.md"

# Client install + Wine layer (ADR-0004)
CLIENT = ROOT / "client" if MODE == "dev" else HOME / "client"   # installed 3.3.5a client (gitignored)
CLIENT_MANIFEST = ENV / "client-manifest.json"        # written by `hs client-install`
WHISKY_LIBS = Path.home() / "Library/Application Support/com.isaacmarovitz.Whisky/Libraries"
WINE_RUNTIME = ENV / "wowsilicon-r17" if MODE == "dev" else HOME / "wine" / "runtime"   # the pinned Wine runtime
WINE_RUNTIME_ARCHIVE = (ENV / "wowsilicon-runtime.tar.xz" if MODE == "dev"
                        else DATA / "downloads" / "wowsilicon-runtime.tar.xz")   # kept while unpacking
# Wine layers (ADR-0004). Each has its own repo-local prefix, never a Whisky bottle. Select with HS_WINE.
WINE_LAYERS = {
    "whisky": {"bin": WHISKY_LIBS / "Wine/bin", "exe": "wine64", "prefix": ENV / "wineprefix"},          # Wine 7.7, read-only
    "wine11": {"bin": ENV / "wine-11/Wine Stable.app/Contents/Resources/wine/bin", "exe": "wine",
               "prefix": ENV / "wineprefix-wine11"},                                                   # Gcenx wine-stable 11.0_1
    # WoWSilicon Wine runtime r17 (Wine 11.13, WineAndAqua macOS branch) with mtld3d v0.7.0 as builtin d3d9
    # (D3D9 -> Metal). Runtime only; none of WoWSilicon's client patching (ADR-0004). Env per WoWSilicon's
    # LaunchService/BundledWineRuntime (mtld3d backend).
    "mtld3d": {"bin": WINE_RUNTIME / ".wine-runtime/bin", "exe": "wine",
               "prefix": ENV / "wineprefix-mtld3d" if MODE == "dev" else HOME / "wine" / "prefix",
               "env": {"WINEDLLOVERRIDES": "d3d9=b", "WINE_LARGE_ADDRESS_AWARE": "1",
                       "DYLD_LIBRARY_PATH": str(WINE_RUNTIME / ".wine-runtime/lib/external")}},
}
if MODE == "release":                  # a release ships only the pinned runtime's layer
    WINE_LAYERS = {"mtld3d": WINE_LAYERS["mtld3d"]}
WINE_LAYER = os.environ.get("HS_WINE", "mtld3d")   # default since F3 (ADR-0004); whisky kept for A/B
if MODE == "release" and WINE_LAYER not in WINE_LAYERS:
    raise SystemExit(f"HS_WINE={WINE_LAYER!r} isn't a Wine layer here (expected one of {', '.join(WINE_LAYERS)}); "
                     "unset HS_WINE")
WINE_BIN = WINE_LAYERS[WINE_LAYER]["bin"]
WINE_EXE = WINE_LAYERS[WINE_LAYER]["exe"]

HOST = "127.0.0.1"

# ------------------------------------------------------------------------------------------------ lanes (ADR-0011)
LANE_ENV = "HS_LANE"
LANES = ("a", "b")
LANE_PORTS = {
    "a": {"mysql": 3307, "auth": 3724, "world": 8085, "soap": 7878},
    "b": {"mysql": 3317, "auth": 3734, "world": 8095, "soap": 7888},
}
LANES_DIR = ENV / "lanes"


def _lane_name(raw: str | None) -> str:
    return (raw or "").strip().lower() or "a"


def layout(lane: str, wine_layer: str | None = None) -> dict:
    """Every per-lane runtime path and port of `lane`. Lane A is the original layout."""
    if lane not in LANES:
        raise ValueError(f"unknown lane {lane!r}; expected one of {LANES}")
    a_prefix = WINE_LAYERS[wine_layer or WINE_LAYER]["prefix"]
    root = ENV if lane == "a" else LANES_DIR / lane
    return {
        "lane": lane,
        "root": root,                                                   # my.cnf, client*.cnf live here
        "run": root / "run",
        "logs": root / "logs",
        "tmp": root / "tmp",
        "etc": (SERVER_ETC if MODE == "dev" else root / "etc") if lane == "a" else root / "etc",   # rendered confs
        "mysql_data": DATA / "mysql" if lane == "a" else root / "mysql",
        "mysql_template": None if lane == "a" else root / "template",   # lane B: built by `hs lane init`
        "my_cnf": root / "my.cnf",
        "wineprefix": a_prefix if lane == "a" else root / a_prefix.name,
        "ports": dict(LANE_PORTS[lane]) if MODE == "dev" or lane != "a" else _release_ports(),
    }


def _release_ports() -> dict:
    """Release mode: lane A's ports, or the scratch ports of $HS_RELEASE_PORTS (a test switch, honoured only with
    the test flag inside a live harness run): a JSON object with every key of lane A's ports, none of them a lane's."""
    ports = dict(LANE_PORTS["a"])
    raw = release_switch(RELEASE_PORTS_ENV)
    if raw is None:
        return ports
    try:
        got = json.loads(raw)
        ok = (isinstance(got, dict) and set(got) == set(ports)
              and all(isinstance(v, int) and 1024 < v < 65536 for v in got.values())
              and not set(got.values()) & {v for lp in LANE_PORTS.values() for v in lp.values()})
    except ValueError:
        ok = False
    if not ok:
        raise SystemExit(f"${RELEASE_PORTS_ENV} must be a JSON object with mysql/auth/world/soap ports above 1024 "
                         f"that aren't a lane's; unset it")
    return got


LANE = _lane_name(os.environ.get(LANE_ENV))
if LANE not in LANES:
    raise SystemExit(f"{LANE_ENV}={os.environ.get(LANE_ENV)!r} is not a lane (expected one of {', '.join(LANES)}); "
                     f"unset {LANE_ENV}")
_L = layout(LANE)
LANE_ROOT = _L["root"]
RUN = _L["run"]
LOGS = _L["logs"]
TMP = _L["tmp"]
LANE_ETC = _L["etc"]
SAVES = LANE_ROOT / "saves"             # this lane's save store (mod-moonglade Moonglade.Saves.Dir); lane A: PLAYER_SAVES
MYSQL_DATA = _L["mysql_data"]
MYSQL_TEMPLATE = _L["mysql_template"]
MY_CNF = _L["my_cnf"]
WINEPREFIX = _L["wineprefix"]
WINE_LOG = LOGS / "wine.log"
MYSQL_PORT = _L["ports"]["mysql"]
AUTH_PORT = _L["ports"]["auth"]
WORLD_PORT = _L["ports"]["world"]
SOAP_PORT = _L["ports"]["soap"]
STACK_PORTS = (MYSQL_PORT, AUTH_PORT, WORLD_PORT, SOAP_PORT)
RUN_REGISTRY = GLOBAL_RUN / "harness-runs"     # runreg.RUN_DIR; global so every lane sees every run

# Player-owned paths: the same in every lane, and never written by lane B.
PLAYER_FILE = ENV / "player.json"
SETTINGS_FILE = ENV / "settings.json"
PLAYER_BACKUPS = ENV / "backups"
PLAYER_SAVES = ENV / "saves"           # front-menu-saves: the player's character saves (<ACCOUNT>/<character id>/<save>)
PLAYER_PLAY_CLIENT = ENV / "client-play"

# Tools and inputs whose place depends on the mode (release-first-launch design 1 and 3). Dev values are today's.
MYSQL_BASE = BREW / "opt/mysql" if MODE == "dev" else APP / "mysql"   # dev: Homebrew's MySQL; release: the payload's
MYSQLD = MYSQL_BASE / "bin/mysqld"
MYSQLADMIN = MYSQL_BASE / "bin/mysqladmin"
MYSQL = MYSQL_BASE / "bin/mysql"
MYSQLDUMP = MYSQL_BASE / "bin/mysqldump"
SQL_SOURCE = CORE_SRC if MODE == "dev" else APP / "sql"   # the DB updater's SourceDirectory
OPENSSL_CONF = None if MODE == "dev" else LANE_ETC / "openssl.cnf"   # release: an empty conf the servers get
SERVER_CWD = SERVER_BIN if MODE == "dev" else RUN         # release: HOME/run, so the relative etc/modules isn't there
DB_MARKER = DATA / ".hs-db-ready.json"
CLIENT_PATCH_QUEUE = ROOT / "patches" / "client"
APP_LOG = LOGS / "app.log"


def live_harness_run(environ=None) -> str | None:
    """The id of the live harness run this environment belongs to ($HS_HARNESS_RUN=1 and $HS_HARNESS_RUN_ID naming
    a run file in the run registry whose process still exists with the same start time), or None."""
    return _live_run_id(os.environ if environ is None else environ)


def lane_refusal(environ=None) -> str | None:
    """Why this process mustn't run in its lane, or None. Lane B (the harness's second test stack) runs only inside
    a live harness run; the entry points (`hs`, ./play, ./bootstrap via hs) refuse before starting anything."""
    env = os.environ if environ is None else environ
    raw = env.get(LANE_ENV)
    lane = _lane_name(raw)
    if lane == "a":
        return None
    if lane not in LANES:
        return f"{LANE_ENV}={raw!r} is not a lane (expected one of {', '.join(LANES)}); unset {LANE_ENV}"
    if MODE == "release":
        return (f"{LANE_ENV}={raw} selects the harness's second test stack, which a release doesn't have. Unset it "
                f"(`unset {LANE_ENV}`) and try again.")
    if live_harness_run(env) is None:
        return (f"{LANE_ENV}={raw} selects the harness's second test stack (lane {lane.upper()}), which only runs "
                f"inside a harness run (`harness/hs run`). Unset it (`unset {LANE_ENV}`) and try again.")
    return None


class LaneRefused(RuntimeError):
    pass


def require_lane_allowed(what: str = "this") -> None:
    """Raise LaneRefused if lane_refusal() objects (the library-level guard behind the entry points' refusal)."""
    why = lane_refusal()
    if why:
        raise LaneRefused(f"{what} refused: {why}")

DB_USER = "acore"
DB_PASS = "acore"
DB_NAMES = {
    "auth": "acore_auth",
    "world": "acore_world",
    "characters": "acore_characters",
    "playerbots": "acore_playerbots",
}

GM_USER = "HS_GM"
GM_PASS = "hsgm"
TEST_USER = "HSTEST"
TEST_PASS = "hstest"
TEST_CHAR = "Hstester"
# Companion checks (m2-living-world comp.*): a separate GM-level (security 2) harness account and character, so the
# HSTEST fixtures stay untouched. playerbots gives GM senders control of bots (PlayerbotSecurity.cpp).
COMP_USER = "HSCOMP"
COMP_PASS = "hscomp"
COMP_CHAR = "Hscomp"


def fork_lock() -> dict:
    out = {}
    for line in (ROOT / "fork.lock").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def path_table() -> dict:
    """Every upper-case name this module exports (paths, ports, names) plus layout() of both lanes, as JSON-able values
    (release.mode_resolution compares it with the parent commit's in dev mode and inspects it in release mode)."""
    def norm(v):
        if isinstance(v, Path):
            return str(v)
        if isinstance(v, dict):
            return {str(k): norm(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        if v is None or isinstance(v, (bool, int, float, str)):
            return v
        return repr(v)
    g = globals()
    out = {k: norm(v) for k, v in sorted(g.items()) if k[:1].isupper() and not callable(v) and not isinstance(v, type)}
    for lane in LANES:
        out[f"layout({lane})"] = norm(layout(lane))
    return out
