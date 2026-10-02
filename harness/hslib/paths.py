"""Repo-relative paths, ports, and fork.lock parsing."""
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
RUN = ENV / "run"
LOGS = ENV / "logs"
DATA = ROOT / "data"
MYSQL_DATA = DATA / "mysql"
CLIENT_DATA = DATA / "client-data"
DOWNLOADS = DATA / "downloads"
PATCHES = ROOT / "patches" / "core"
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
WINEPREFIX = WINE_LAYERS[WINE_LAYER]["prefix"]
WINE_LOG = LOGS / "wine.log"

HOST = "127.0.0.1"
MYSQL_PORT = 3307
AUTH_PORT = 3724
WORLD_PORT = 8085
SOAP_PORT = 7878
STACK_PORTS = (MYSQL_PORT, AUTH_PORT, WORLD_PORT, SOAP_PORT)

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

BREW = Path("/opt/homebrew")


def fork_lock() -> dict:
    out = {}
    for line in (ROOT / "fork.lock").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out
