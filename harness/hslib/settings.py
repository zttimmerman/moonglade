"""The user's local settings (m2-living-world task 2.2, world-population "World size is one easy setting").

File: env/settings.json (env/ is gitignored), or $HS_SETTINGS (checks point this at a scratch file; they never read
the user's). Today it holds one key:

    {"world_size": "small" | "normal" | "large" | <number of random bots>}

Missing file or key -> "normal". Change it with `harness/hs world-size small` (or edit the file); `./play` reads it
on every start. Intended as a front-menu option later.

    {"client": "patched" | "stock"}   (in-client-login, ADR-0009) which game client ./play and the app run:
        patched = the play client env/client-play/ (built from client/ + patches/client/; the default),
        stock   = the untouched client/, exactly as before the play client existed.
    Change it with `harness/hs client-mode stock|patched`."""
import json
import os
from pathlib import Path

from . import config, paths as P

FILE = P.ENV / "settings.json"
ENV = "HS_SETTINGS"
DEFAULT_SIZE = "normal"
MIN_BOTS, MAX_BOTS = 10, 2000     # sanity bounds for a numeric size (accounts get created for every bot)


class SettingsError(ValueError):
    pass


def path() -> Path:
    return Path(os.environ[ENV]) if os.environ.get(ENV) else FILE


def load() -> dict:
    f = path()
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text())
    except ValueError as e:
        raise SettingsError(f"{f}: not valid JSON ({e})") from e
    if not isinstance(data, dict):
        raise SettingsError(f"{f}: expected a JSON object")
    return data


def resolve_size(value) -> int:
    """A preset name (config.WORLD_SIZES) or a number of random bots -> number of random bots."""
    if isinstance(value, str) and value.strip().lower() in config.WORLD_SIZES:
        return config.WORLD_SIZES[value.strip().lower()]
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise SettingsError(f"world_size {value!r}: use one of {sorted(config.WORLD_SIZES)} or a number") from None
    if not MIN_BOTS <= n <= MAX_BOTS:
        raise SettingsError(f"world_size {n}: must be between {MIN_BOTS} and {MAX_BOTS}")
    return n


def world_size() -> dict:
    """{"setting": as written (or the default), "bots": N, "file": path, "file_exists": bool}."""
    data = load()
    setting = data.get("world_size", DEFAULT_SIZE)
    return {"setting": setting, "bots": resolve_size(setting), "file": str(path()), "file_exists": path().exists()}


def set_world_size(value) -> dict:
    resolve_size(value)                                   # validate before writing
    data = load()
    data["world_size"] = value if isinstance(value, str) and not value.isdigit() else int(value)
    path().parent.mkdir(parents=True, exist_ok=True)
    path().write_text(json.dumps(data, indent=2) + "\n")
    return world_size()


def play_world() -> dict:
    """How `./play` starts the world: the population profile at the configured size. An explicit $HS_PROFILE
    (harness checks pin `m0`) wins and the settings file isn't read."""
    if os.environ.get("HS_PROFILE"):
        return {"profile": config.profile_name(), "population_n": None, "source": "$HS_PROFILE"}
    ws = world_size()
    return {"profile": "population", "population_n": ws["bots"], "source": ws}


CLIENT_MODES = ("patched", "stock")
DEFAULT_CLIENT = "patched"


def client_mode() -> dict:
    """{"mode": "patched"|"stock", "setting": as written or None, "file": path}."""
    data = load()
    raw = data.get("client")
    mode = DEFAULT_CLIENT if raw is None else str(raw).strip().lower()
    if mode not in CLIENT_MODES:
        raise SettingsError(f"{path()}: client {raw!r}: use one of {list(CLIENT_MODES)}")
    return {"mode": mode, "setting": raw, "file": str(path()), "file_exists": path().exists()}


def set_client_mode(value: str) -> dict:
    if value not in CLIENT_MODES:
        raise SettingsError(f"client {value!r}: use one of {list(CLIENT_MODES)}")
    data = load()
    data["client"] = value
    path().parent.mkdir(parents=True, exist_ok=True)
    path().write_text(json.dumps(data, indent=2) + "\n")
    return client_mode()
