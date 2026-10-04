"""Render server *.conf from installed *.conf.dist with key overrides. Regenerated on every `up`."""
import hashlib
import json
import os
import re
from pathlib import Path

from . import paths as P

# ----------------------------------------------------------------------------------------------- profiles
# A profile is a named set of config overrides layered on top of the always-on harness settings below.
#   m0          10 random bots: the default for `hs up`, `./play` and every check that doesn't ask otherwise.
#   population  the M2 living-world population (openspec/changes/m2-living-world/design.md "Population profile":
#               every key below is explained there, with the playerbots.conf.dist line it comes from).
#   wotlk       m0 with vanilla mode OFF (EraProfile.Enable = 0, mod-era-profile): stock WotLK values, for the
#               era-profile toggle checks and counterexamples (openspec/changes/m3-vanilla-mode).
# A profile may carry an "era" section: overrides for mod_era_profile.conf. No profile but `wotlk` sets one, so the
# default (vanilla mode on) comes from the module's .conf.dist (era-profile "Default is vanilla mode").
# Select one with `hs up --profile NAME`, `HS_PROFILE=NAME` (also read by `./play` and `hs launch`), or in code
# `stack.up(profile=NAME)`. Checks declare `@check(..., profile=NAME)`; the runner restarts a running stack that
# was started with a different profile (env/run/stack-profile.json records the running one).
DEFAULT_PROFILE = "m0"

# Population size N, chosen by measurement: docs/research/m2-population-tuning.md (perf.population_tuned reads it).
# World-size presets (random bots), the user's one setting (hslib/settings.py, env/settings.json). Sizes chosen from
# measurements: docs/research/m2-population-tuning.md.
WORLD_SIZES = {"small": 150, "normal": 500, "large": 1000}
POPULATION_N = WORLD_SIZES["normal"]      # what the checks' population profile uses (never the user's setting)
POPULATION_N_ENV = "HS_POPULATION_N"     # tuning runs only: measure the population profile at another N


def _population(n: int) -> dict[str, dict[str, str]]:
    """The population profile at N random bots. Every key is explained (with its playerbots.conf.dist line) in
    openspec/changes/m2-living-world/design.md "Population profile knobs"."""
    return {
        "playerbots": {
            "AiPlayerbot.MinRandomBots": str(n), "AiPlayerbot.MaxRandomBots": str(n),
            "AiPlayerbot.AddClassAccountPoolSize": "5",
            "AiPlayerbot.RandomBotMinLevelChance": "0.4",
            "AiPlayerbot.RandomBotMaxLevelChance": "0.05",
            "AiPlayerbot.ProbTeleToBankers": "1.0",
            # capital-banker weights per faction, from measured capital counts (design.md "Population profile knobs");
            # Darnassus and Exodar keep the default 1 (they held 12-15 bots at weight 1 with N=500). Undercity 4,
            # Thunder Bluff 3 (B14): at UC 2 / TB 4, Undercity was the weakest capital in 12 of 12 N=500 sessions
            # (3-9 bots at the warm-up snapshot) while Thunder Bluff held 9-27
            "AiPlayerbot.TeleToStormwindWeight": "2",
            "AiPlayerbot.TeleToOrgrimmarWeight": "2",
            "AiPlayerbot.TeleToIronforgeWeight": "2",
            "AiPlayerbot.TeleToUndercityWeight": "4",
            "AiPlayerbot.TeleToThunderBluffWeight": "3",
            "AiPlayerbot.TeleToSilvermoonCityWeight": "2",
            "AiPlayerbot.MinRandomBotTeleportInterval": "300",
            "AiPlayerbot.MaxRandomBotTeleportInterval": "1200",
            "AiPlayerbot.BotActiveAlone": "100",
            "AiPlayerbot.botActiveAloneSmartScale": "0",
            "AiPlayerbot.RandomBotJoinLfg": "0",
            "AiPlayerbot.RandomBotJoinBG": "0",
        },
        "world": {
            "MapUpdate.Threads": "4",
            "DontCacheRandomMovementPaths": "1",
            # timestamp + level prefix on Playerbots.log lines (LogCommon.h:53-55) so checks can see FATAL/ERROR
            "Appender.Playerbots": "2,5,3,Playerbots.log,w",
            "Logger.playerbots": "4,Console Playerbots",
        },
    }


PROFILES: dict[str, dict[str, dict[str, str]]] = {
    "m0": {
        "playerbots": {
            # small population. Defaults (500 random bots + 50 addclass accounts) are too heavy for a 16 GB Mac
            # and slow first boot (docs/research/playerbots-scripting.md §5).
            "AiPlayerbot.MinRandomBots": "10", "AiPlayerbot.MaxRandomBots": "10",
            "AiPlayerbot.AddClassAccountPoolSize": "5",
        },
        "world": {},
    },
    "population": _population(POPULATION_N),
}
PROFILES["wotlk"] = {**PROFILES["m0"], "era": {"EraProfile.Enable": "0"}}

ERA_CONF = "mod_era_profile.conf"     # modules/mod-era-profile/conf/mod_era_profile.conf.dist, installed to etc/modules


def overrides(name: str, population_n: int | None = None) -> dict[str, dict[str, str]]:
    """A profile's overrides. For the population profile, N is `population_n` if given (./play's world-size
    setting), else $HS_POPULATION_N (tuning runs), else POPULATION_N."""
    if name == "population":
        n = population_n or (int(os.environ[POPULATION_N_ENV]) if os.environ.get(POPULATION_N_ENV) else None)
        if n:
            return _population(int(n))
    return PROFILES[name]


def population_n(population_n: int | None = None) -> int:
    """N of the population profile as it would be rendered now."""
    return int(overrides("population", population_n)["playerbots"]["AiPlayerbot.MaxRandomBots"])


def profile_name(profile: str | None = None) -> str:
    """Resolve a profile name: explicit argument, else $HS_PROFILE, else DEFAULT_PROFILE. Unknown names raise."""
    name = profile or os.environ.get("HS_PROFILE") or DEFAULT_PROFILE
    if name not in PROFILES:
        raise ValueError(f"unknown config profile {name!r}; known: {sorted(PROFILES)}")
    return name


def profile_digest(name: str, population_n: int | None = None) -> str:
    """Short hash of a profile's overrides: a running stack whose recorded digest differs is stale."""
    return hashlib.sha256(json.dumps(overrides(name, population_n), sort_keys=True).encode()).hexdigest()[:12]


def _dbinfo(db: str) -> str:
    return f'"{P.HOST};{P.MYSQL_PORT};{P.DB_USER};{P.DB_PASS};{P.DB_NAMES[db]}"'


def render(dist: Path, out: Path, overrides: dict[str, str]) -> None:
    text = dist.read_text()
    seen = set()
    for key, val in overrides.items():
        pat = re.compile(rf"^{re.escape(key)}\s*=.*$", re.M)
        if pat.search(text):
            text = pat.sub(lambda _m: f"{key} = {val}", text)
            seen.add(key)
    missing = [k for k in overrides if k not in seen]
    if missing:
        text += "\n# --- harness additions ---\n" + "".join(f"{k} = {overrides[k]}\n" for k in missing)
    out.write_text(text)


def common(logs: Path = P.LOGS, tmp: Path = P.ENV / "tmp", source: Path = P.CORE_SRC) -> dict:
    return {
        "LogsDir": f'"{logs}"',
        "TempDir": f'"{tmp}"',
        "SourceDirectory": f'"{source}"',
        "MySQLExecutable": f'"{P.BREW / "opt/mysql/bin/mysql"}"',
        "BindIP": '"127.0.0.1"',
        "Updates.AutoSetup": "1",
    }


def write_all(world_datadir: Path | None = None, extra_world: dict | None = None, *, etc: Path = P.SERVER_ETC,
              logs: Path = P.LOGS, run: Path = P.RUN, tmp: Path = P.ENV / "tmp", source: Path = P.CORE_SRC,
              profile: str | None = None, population_n: int | None = None) -> dict:
    """Render authserver/worldserver/module confs into `etc` with the given profile (see PROFILES). The keyword
    paths default to the real stack; the unpatched baseline worldserver (core.login_logout_race_baseline) renders
    its own install's confs with them."""
    prof = overrides(profile_name(profile), population_n)
    for d in (logs, tmp, run):
        d.mkdir(parents=True, exist_ok=True)
    auth = common(logs, tmp, source) | {
        "LoginDatabaseInfo": _dbinfo("auth"),
        "PidFile": f'"{run / "authserver.pid"}"',
        "Updates.EnableDatabases": "1",
    }
    world = common(logs, tmp, source) | {
        "LoginDatabaseInfo": _dbinfo("auth"),
        "WorldDatabaseInfo": _dbinfo("world"),
        "CharacterDatabaseInfo": _dbinfo("characters"),
        "DataDir": f'"{world_datadir or P.CLIENT_DATA}"',
        "PidFile": f'"{run / "worldserver.pid"}"',
        "Console.Enable": "0",
        "SOAP.Enabled": "1",
        "SOAP.IP": '"127.0.0.1"',
        "SOAP.Port": str(P.SOAP_PORT),
        "Ra.Enable": "0",
        "Updates.EnableDatabases": "7",
        "RealmID": "1",
        # Warden = anti-cheat that sends native code modules to the client. Pointless offline, and the prime
        # suspect for the client's ILLEGAL_INSTRUCTION crash at a non-module EIP under Wine+Rosetta (2026-10-01).
        "Warden.Enabled": "0",
    } | prof["world"] | (extra_world or {})
    render(etc / "authserver.conf.dist", etc / "authserver.conf", auth)
    render(etc / "worldserver.conf.dist", etc / "worldserver.conf", world)
    mods = etc / "modules"
    rendered = []
    for dist in sorted(mods.glob("*.conf.dist")) if mods.exists() else []:
        ov = {}
        if dist.name == "playerbots.conf.dist":
            ov["PlayerbotsDatabaseInfo"] = _dbinfo("playerbots")
            ov |= prof["playerbots"]
            # command server binds 0.0.0.0 with no address option (PlayerbotCommandServer.cpp:62); off in every profile.
            ov["AiPlayerbot.CommandServerPort"] = "0"
        elif dist.name == ERA_CONF + ".dist":
            ov |= prof.get("era", {})
        render(dist, dist.with_suffix(""), ov)
        rendered.append(dist.with_suffix("").name)
    return {"authserver.conf": auth, "worldserver.conf": world, "modules": rendered, "profile": profile_name(profile)}
