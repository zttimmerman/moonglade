"""Repo-relative paths, ports, and fork.lock parsing."""
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
WINEPREFIX = ENV / "wineprefix"                       # repo-local Wine prefix, never a Whisky bottle
WINE_LOG = LOGS / "wine.log"
WHISKY_LIBS = Path.home() / "Library/Application Support/com.isaacmarovitz.Whisky/Libraries"
WINE_BIN = WHISKY_LIBS / "Wine/bin"                   # Whisky's bundled Wine (used read-only)

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
