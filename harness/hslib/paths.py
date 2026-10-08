"""Repo-relative paths, ports, and fork.lock parsing.

Lanes (ADR-0011, openspec/changes/second-stack): `HS_LANE` (a|b, default a) is read once, here. Lane A is the
original layout, unchanged. Lane B is the harness's second, isolated test stack: every runtime path (run, logs, tmp,
server confs, my.cnf, client.cnf, mysqld datadir, Wine prefix) lives under env/lanes/b/ and every port is lane B's.
Player paths (env/player.json, env/settings.json, env/backups, env/client-play), the run registry
(GLOBAL_RUN/harness-runs) and the read-only shared inputs (server/bin, client data, client/, the Wine runtime) are
the same in both lanes. Lane B refuses to run outside a harness run (lane_refusal).
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "harness"
VENDOR = ROOT / "vendor"
CORE_SRC = VENDOR / "azerothcore-wotlk"
BOTS_SRC = VENDOR / "mod-playerbots"
BUILD = ROOT / "build"
SERVER = ROOT / "server"          # cmake install prefix
SERVER_BIN = SERVER / "bin"
SERVER_ETC = SERVER / "etc"
ENV = ROOT / "env"                # runtime: config, logs, pidfiles, sockets
GLOBAL_RUN = ENV / "run"          # lane A's run dir; also home of the run registry (harness-runs/), shared by lanes
DATA = ROOT / "data"
CLIENT_DATA = DATA / "client-data"
DOWNLOADS = DATA / "downloads"
PATCHES = ROOT / "patches" / "core"
BOTS_PATCHES = ROOT / "patches" / "playerbots"   # mod-playerbots patch queue (ADR-0008), applied like PATCHES
MODULES = ROOT / "modules"
OUT = HARNESS / "out"
RESULTS = HARNESS / "results.json"
SUMMARY = HARNESS / "summary.md"

# Client install + Wine layer (ADR-0004)
CLIENT = ROOT / "client"                              # installed 3.3.5a client (gitignored)
CLIENT_MANIFEST = ENV / "client-manifest.json"        # written by `hs client-install`
WHISKY_LIBS = Path.home() / "Library/Application Support/com.isaacmarovitz.Whisky/Libraries"
# Wine layers (ADR-0004). Each has its own repo-local prefix, never a Whisky bottle. Select with HS_WINE.
WINE_LAYERS = {
    "whisky": {"bin": WHISKY_LIBS / "Wine/bin", "exe": "wine64", "prefix": ENV / "wineprefix"},          # Wine 7.7, read-only
    "wine11": {"bin": ENV / "wine-11/Wine Stable.app/Contents/Resources/wine/bin", "exe": "wine",
               "prefix": ENV / "wineprefix-wine11"},                                                   # Gcenx wine-stable 11.0_1
    # WoWSilicon Wine runtime r17 (Wine 11.13, WineAndAqua macOS branch) with mtld3d v0.7.0 as builtin d3d9
    # (D3D9 -> Metal). Runtime only; none of WoWSilicon's client patching (ADR-0004). Env per WoWSilicon's
    # LaunchService/BundledWineRuntime (mtld3d backend).
    "mtld3d": {"bin": ENV / "wowsilicon-r17/.wine-runtime/bin", "exe": "wine", "prefix": ENV / "wineprefix-mtld3d",
               "env": {"WINEDLLOVERRIDES": "d3d9=b", "WINE_LARGE_ADDRESS_AWARE": "1",
                       "DYLD_LIBRARY_PATH": str(ENV / "wowsilicon-r17/.wine-runtime/lib/external")}},
}
WINE_LAYER = os.environ.get("HS_WINE", "mtld3d")   # default since F3 (ADR-0004); whisky kept for A/B
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
        "etc": SERVER_ETC if lane == "a" else root / "etc",             # rendered authserver/worldserver confs
        "mysql_data": DATA / "mysql" if lane == "a" else root / "mysql",
        "mysql_template": None if lane == "a" else root / "template",   # lane B: built by `hs lane init`
        "my_cnf": root / "my.cnf",
        "wineprefix": a_prefix if lane == "a" else root / a_prefix.name,
        "ports": dict(LANE_PORTS[lane]),
    }


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
PLAYER_PLAY_CLIENT = ENV / "client-play"


def live_harness_run(environ=None) -> str | None:
    """The id of the live harness run this environment belongs to ($HS_HARNESS_RUN=1 and $HS_HARNESS_RUN_ID naming
    a run file in the run registry whose process still exists with the same start time), or None."""
    env = os.environ if environ is None else environ
    rid = env.get("HS_HARNESS_RUN_ID") or ""
    if env.get("HS_HARNESS_RUN") != "1" or not rid or "/" in rid or not (RUN_REGISTRY / f"{rid}.json").is_file():
        return None
    pid, _, st = rid.partition("-")
    if not pid.isdigit():
        return None
    from . import procs                     # procs imports nothing from here
    got = procs.start_time(int(pid))
    return rid if got is not None and f"{pid}-{got}" == rid else None


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

BREW = Path("/opt/homebrew")


def fork_lock() -> dict:
    out = {}
    for line in (ROOT / "fork.lock").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out
