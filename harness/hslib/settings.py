"""The user's local settings (m2-living-world task 2.2, world-population "World size is one easy setting").

File: env/settings.json (env/ is gitignored), or $HS_SETTINGS (checks point this at a scratch file; they never read
the user's). Today it holds one key:

    {"world_size": "small" | "normal" | "large" | <number of random bots>}

Missing file or key -> "normal". Change it with `harness/hs world-size small` (or edit the file); `./play` reads it
on every start. Intended as a front-menu option later.

    {"client": "patched" | "stock"}   (in-client-login, ADR-0009) which game client ./play and the app run:
        patched = the play client env/client-play/ (built from client/ + patches/client/; the default),
        stock   = the untouched client/, exactly as before the play client existed.
    Change it with `harness/hs client-mode stock|patched`.

    {"dock_policy": "accessory" | "regular" | "off"}   (launcher-polish, ADR-0010) how the game shows in the Dock
        when it runs from Moonglade.app:
        accessory = no Dock icon of its own; the Moonglade icon brings it forward (the default),
        regular   = the game keeps its own Dock icon, named Moonglade with our logo (option B: Wine's menus and
                    native full screen as before),
        off       = no shim at all (Wine's "wine" icon, as before launcher-polish).
    The environment variable HSDOCK_POLICY overrides it. Change it with `harness/hs dock-policy <value>`."""
import json
import os
from pathlib import Path

from . import config, paths as P

FILE = P.ENV / "settings.json"
ENV = "HS_SETTINGS"

# Moonglade's defaults, in one place (easy-setup "The same setup as Zach's"): bootstrap writes the settings-file ones
# into a new env/settings.json, the getters below fall back to them, and docs/setup.md's defaults table is generated
# from this (defaults_table_md; setup.fresh_clone compares the two). Vanilla mode isn't a settings-file key: its
# default is EraProfile.Enable in the module's .conf.dist (era-profile "Default is vanilla mode"); vanilla_mode()
# reads it from the installed server, and this entry only states what it must be.
DEFAULTS = {
    "world_size": {"value": "normal", "setting": "World size", "means": "about 500 AI players",
                   "show": "harness/hs world-size", "change": "harness/hs world-size <small, normal, large or a number>"},
    "vanilla_mode": {"value": "on", "setting": "Vanilla mode", "means": "levels 1-60 at the pre-2.3 pace and difficulty",
                     "show": "harness/hs vanilla-mode", "change": "no supported switch yet (see the README)"},
    "client": {"value": "patched", "setting": "Game client", "means": "the play client with the Moonglade front menu",
               "show": "harness/hs client-mode", "change": "harness/hs client-mode <stock or patched>"},
}
SETTINGS_FILE_KEYS = ("world_size", "client")       # the defaults bootstrap writes into env/settings.json
DEFAULT_SIZE = DEFAULTS["world_size"]["value"]
MIN_BOTS, MAX_BOTS = 10, 2000     # sanity bounds for a numeric size (accounts get created for every bot)


class SettingsError(ValueError):
    pass


def path(write: bool = False) -> Path:
    """env/settings.json, or $HS_SETTINGS. Lane B (second-stack) may read the player's file but never write it."""
    if os.environ.get(ENV):
        return Path(os.environ[ENV])
    if write and P.LANE != "a":
        raise SettingsError(f"lane {P.LANE.upper()} never writes the player's {FILE}; set {ENV}")
    return FILE


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
    path(write=True).write_text(json.dumps(data, indent=2) + "\n")
    return world_size()


def play_world() -> dict:
    """How `./play` starts the world: the population profile at the configured size. An explicit $HS_PROFILE
    (harness checks pin `m0`) wins and the settings file isn't read."""
    if os.environ.get("HS_PROFILE"):
        return {"profile": config.profile_name(), "population_n": None, "source": "$HS_PROFILE"}
    ws = world_size()
    return {"profile": "population", "population_n": ws["bots"], "source": ws}


CLIENT_MODES = ("patched", "stock")
DEFAULT_CLIENT = DEFAULTS["client"]["value"]


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
    path(write=True).write_text(json.dumps(data, indent=2) + "\n")
    return client_mode()


DOCK_POLICIES = ("accessory", "regular", "off")
DEFAULT_DOCK_POLICY = "accessory"
DOCK_ENV = "HSDOCK_POLICY"


def dock_policy() -> dict:
    """{"policy": accessory|regular|off, "source": env|settings|default, "file": path}."""
    if os.environ.get(DOCK_ENV):
        raw, source = os.environ[DOCK_ENV], "env"
    else:
        raw = load().get("dock_policy")
        source = "settings" if raw is not None else "default"
    policy = DEFAULT_DOCK_POLICY if raw is None else str(raw).strip().lower()
    if policy not in DOCK_POLICIES:
        raise SettingsError(f"dock_policy {raw!r} ({source}): use one of {list(DOCK_POLICIES)}")
    return {"policy": policy, "source": source, "file": str(path())}


def set_dock_policy(value: str) -> dict:
    if value not in DOCK_POLICIES:
        raise SettingsError(f"dock_policy {value!r}: use one of {list(DOCK_POLICIES)}")
    data = load()
    data["dock_policy"] = value
    path().parent.mkdir(parents=True, exist_ok=True)
    path(write=True).write_text(json.dumps(data, indent=2) + "\n")
    return dock_policy()


# ---------------------------------------------------------------- vanilla mode (read-only) and the defaults table

ERA_CONF_DIST = P.SERVER_ETC / "modules" / "mod_era_profile.conf.dist"     # installed by `hs build`


def vanilla_mode() -> dict:
    """Vanilla mode for ./play's world: EraProfile.Enable in the installed module .conf.dist (configs are rendered from
    it at every start), unless the population profile ./play uses overrides it (none does)."""
    import re
    try:
        text = ERA_CONF_DIST.read_text()
    except OSError:
        raise SettingsError(f"{ERA_CONF_DIST} not found; build the server first (`harness/hs build`)") from None
    m = re.search(r"^EraProfile\.Enable\s*=\s*\"?(\d+)\"?\s*$", text, re.M)
    if not m:
        raise SettingsError(f"{ERA_CONF_DIST} has no EraProfile.Enable line")
    override = (config.PROFILES.get("population") or {}).get("era", {}).get("EraProfile.Enable")
    val = override if override is not None else m.group(1)
    return {"mode": "on" if str(val) == "1" else "off", "EraProfile.Enable": str(val), "file": str(ERA_CONF_DIST),
            "play_profile_override": override}


def defaults_table_md() -> str:
    """docs/setup.md's defaults table (between its <!-- defaults:begin/end --> markers), generated from DEFAULTS."""
    rows = ["| Setting | Default | What it means | Show it | Change it |", "|---|---|---|---|---|"]
    for d in DEFAULTS.values():
        rows.append(f"| {d['setting']} | **{d['value']}** | {d['means']} | `{d['show']}` | "
                    + (f"`{d['change']}`" if d["change"].startswith("harness/") else d["change"]) + " |")
    return "\n".join(rows) + "\n"


def write_missing_defaults() -> dict:
    """Bootstrap's last step: put the settings-file defaults into env/settings.json where a key is missing, through the
    setters above. A value the player already chose is never changed."""
    data = load()
    written = {}
    for k in SETTINGS_FILE_KEYS:
        if k not in data:
            v = DEFAULTS[k]["value"]
            (set_world_size if k == "world_size" else set_client_mode)(v)
            written[k] = v
    return {"file": str(path()), "written": written, "kept": {k: data[k] for k in SETTINGS_FILE_KEYS if k in data}}
