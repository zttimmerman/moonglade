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
#   pause-off   m0 with true pause turned off (Moonglade.Pause.Enable = 0, mod-moonglade), for pause.disabled_noop
#               (openspec/changes/true-pause).
#   scaling-off m0 with dungeon scaling off (AutoBalance.Enable.Global = 0, mod-autobalance): the unscaled reference
#               reads of scale.* and their counterexamples (openspec/changes/m4-dungeon-scaling design.md §2).
#   lockouts-stock  m0 with the stock lockouts: AccountInstancesPerHour 5 (worldserver.conf.dist), fresh-after-clear
#               off (Moonglade.Dungeons.FreshAfterClear = 0) and stranded companions left alone
#               (Moonglade.Dungeons.FreeStrandedCompanions = 0), for the lock.* counterexamples.
# A profile may carry an "era" section: overrides for mod_era_profile.conf, and a "moonglade" section for
# mod_moonglade.conf. No profile but `wotlk` sets an era section, so the
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
# true-pause (pause.disabled_noop): m0 with pausing turned off (mod-moonglade Moonglade.Pause.Enable = 0)
PROFILES["pause-off"] = {**PROFILES["m0"], "moonglade": {"Moonglade.Pause.Enable": "0"}}
# test-pyramid (pause.respawn_frozen, harness-control "Server-side waits are controllable in tests"): m0 with a 5 s corpse
# decay for normal creatures, so a scratch spawn's respawn remainder R (respawn time + corpse delay) is <= 15 s
PROFILES["pause-respawn"] = {**PROFILES["m0"], "world": {"Corpse.Decay.NORMAL": "5"}}
# front-menu-saves (saves.load_ordering): m0 with two character-database workers; the module must refuse saves then
PROFILES["saves-workers2"] = {**PROFILES["m0"], "world": {"CharacterDatabase.WorkerThreads": "2"}}
# m4-dungeon-scaling (design.md §2): the module off, every other Moonglade AutoBalance key unchanged
PROFILES["scaling-off"] = {**PROFILES["m0"], "autobalance": {"AutoBalance.Enable.Global": "0"}}
# test-pyramid (dng.session1/2): m0 (scaling on: AutoBalance.conf.dist's AutoBalance.Enable.Global=1, which m0 keeps)
# as a profile of its own so its group runs after scaling-off's (the sessions compare with the unscaled reference
# scale.disabled_stock reads under scaling-off in the same run). Its overrides are m0's exactly (part 3), so the same
# digest: the journeys after its group need no worldserver restart back to m0 (stack.profile_matches).
PROFILES["scaling-on"] = {**PROFILES["m0"]}
# m4-dungeon-scaling (design.md §2): stock lockouts. 5 = worldserver.conf.dist's AccountInstancesPerHour default
PROFILES["lockouts-stock"] = {**PROFILES["m0"], "world": {"AccountInstancesPerHour": "5"},
                              "moonglade": {"Moonglade.Dungeons.FreshAfterClear": "0",
                                            "Moonglade.Dungeons.FreeStrandedCompanions": "0"}}

# m4-dungeon-scaling (design.md §2, ADR-0014): Moonglade's mod-autobalance settings, in every profile (a profile's
# "autobalance" section layers on top). Keys not listed keep AutoBalance.conf.dist's default. These are module tuning
# knobs, not WoW data (CLAUDE.md §7); each records why.
AUTOBALANCE = {
    # solo scales down to one player (the module default, stated explicitly), normal and heroic 5-player dungeons
    "AutoBalance.MinPlayers": "1",
    "AutoBalance.MinPlayers.Heroic": "1",
    # the difficulty curve (multiplier by player count; ABUtils.cpp getDefaultMultiplier). CurveCeiling 1.0: a party of
    # five meets the unchanged dungeon and nothing is ever harder than stock (Zach, proposal Q2, 2026-10-10). The
    # inflection point, floor and boss modifier are tuned by the scale.solo_final_boss runs, recorded in
    # docs/research/m4-scaling-tuning.md (proposal Q1: a solo fight should feel like a normal group's fight).
    # Tuned: CurveFloor 0.05 (solo 0.167 instead of the default 0.123). Floor 0.2 (solo 0.299) made the classic bosses
    # real fights but Ingvar (Utgarde Keep, level 72) killed the solo warrior twice; at 0.05 every listed final boss
    # died first try (Ingvar: 50-73 s, down to 17-64 % health). Inflection point and boss modifier stay at the defaults.
    "AutoBalance.InflectionPoint": "0.5",
    "AutoBalance.InflectionPoint.CurveFloor": "0.05",
    "AutoBalance.InflectionPoint.CurveCeiling": "1.0",
    "AutoBalance.InflectionPoint.BossModifier": "1.0",
    "AutoBalance.InflectionPointHeroic": "0.5",
    "AutoBalance.InflectionPointHeroic.CurveFloor": "0.05",
    "AutoBalance.InflectionPointHeroic.CurveCeiling": "1.0",
    "AutoBalance.InflectionPointHeroic.BossModifier": "1.0",
    # companions count as one person each (proposal Q2 default; the module has no per-bot weight)
    "AutoBalance.playerCountDifficultyOffset": "0",
    # dungeons keep their own levels (proposal Q3, Zach: no level scaling); also avoids upstream #198
    "AutoBalance.LevelScaling": "0",
    # less XP and money when the monsters are weaker (proposal Q4, Zach: yes); the module defaults, stated explicitly
    "AutoBalance.RewardScaling.Method": '"dynamic"',
    "AutoBalance.RewardScaling.XP": "1",
    "AutoBalance.RewardScaling.Money": "1",
    # no "difficulty changed" chat lines (seamless-experience: no server chatter); `.ab mapstat` still answers
    "AutoBalance.PlayerChangeNotify": "0",
    # no "This server is running the AutoBalance module." at login (ABPlayerScript.cpp:14-15; .dist default 1;
    # client.login_text_clean, verifier cycle 1)
    "AutoBalanceAnnounce.enable": "0",
    # no dungeon-token rewards mailed or given at encounter credit (ABGlobalScript.cpp OnAfterUpdateEncounterState;
    # the .dist says 0 but the code default is 1 when the key is missing): stated explicitly
    "AutoBalance.reward.enable": "0",
}
# AutoBalance's chat output, from the module source (every ChatHandler send outside ABCommandScript):
#   AutoBalanceAnnounce.enable   login announcement                       -> 0 above
#   AutoBalance.PlayerChangeNotify  welcome / "X entered" / "X left" lines -> 0 above
#   (no key)  ABPlayerScript.cpp:224 "leaving_instance_combat_change", sent when the party size changed during a fight
#             and the fight ends; no setting turns it off (design.md "As built", reported to the orchestrator)
AUTOBALANCE_CHAT_OFF = ("AutoBalanceAnnounce.enable", "AutoBalance.PlayerChangeNotify")
AUTOBALANCE_CONF = "AutoBalance.conf"   # vendor/mod-autobalance/conf/AutoBalance.conf.dist, installed to etc/modules

ERA_CONF = "mod_era_profile.conf"     # modules/mod-era-profile/conf/mod_era_profile.conf.dist, installed to etc/modules
MOONGLADE_CONF = "mod_moonglade.conf"  # modules/mod-moonglade/conf/mod_moonglade.conf.dist, installed to etc/modules


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
    """Write `out` from `dist` with `overrides`, atomically (a temp file renamed over it): a worldserver starting in
    another lane while this lane renders never reads a half-written file (second-stack)."""
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
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, out)


# ------------------------------------------------------------------------------- lane B: module keys as AC_* env
# worldserver reads module confs only from the compiled-in _CONF_DIR/modules (server/etc/modules, lane A's), so lane B
# can't render its own. AzerothCore lets every key be overridden by an AC_<UPPER_SNAKE> environment variable
# (vendor/azerothcore-wotlk/src/common/Configuration/Config.cpp:370-447; GetValueDefault checks the env first), so
# lane B passes every module key any profile sets (union over PROFILES, so lane A's rendering can't leak in), its DB
# info and the command-server port as AC_* variables (stack._spawn). ADR-0011 decision 4.

def env_var_name(key: str) -> str:
    """Config.cpp IniKeyToEnvVarKey + GetEnvVarName, ported exactly."""
    out, n = [], len(key)
    for i, c in enumerate(key):
        if c in " .-":
            out.append("_")
            continue
        if i != n - 1:
            nxt = key[i + 1]
            if not c.isupper() and nxt.isupper():
                out.append(c.upper() + "_")
                continue
            if not c.isdigit() and nxt.isdigit():
                out.append(c.upper() + "_")
                continue
            if c.isdigit() and not nxt.isdigit():
                out.append(c.upper() + "_")
                continue
        out.append(c.upper())
    return "AC_" + "".join(out)


def _dist_value(dist: Path, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}\s*=\s*(.*?)\s*$", dist.read_text(), re.M) if dist.exists() else None
    return m.group(1) if m else None


def _unquote(v: str) -> str:
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v


MODULE_SECTIONS = {"playerbots": "playerbots.conf.dist", "era": ERA_CONF + ".dist", "moonglade": MOONGLADE_CONF + ".dist",
                   "autobalance": AUTOBALANCE_CONF + ".dist"}


def module_overrides(prof: dict) -> dict[str, dict[str, str]]:
    """{module .conf.dist name: overrides} for a profile's overrides (what write_all renders into module confs)."""
    return {"playerbots.conf.dist": {"PlayerbotsDatabaseInfo": _dbinfo("playerbots"), **prof.get("playerbots", {}),
                                     # command server binds 0.0.0.0 with no address option
                                     # (PlayerbotCommandServer.cpp:62); off in every profile.
                                     "AiPlayerbot.CommandServerPort": "0"},
            ERA_CONF + ".dist": dict(prof.get("era", {})),
            # front-menu-saves: the lane's save store (lane A: env/saves, the player's; lane B: its own root)
            MOONGLADE_CONF + ".dist": {"Moonglade.Saves.Dir": f'"{P.SAVES}"', **prof.get("moonglade", {})},
            # m4-dungeon-scaling: Moonglade's AutoBalance settings, then the profile's
            AUTOBALANCE_CONF + ".dist": {**AUTOBALANCE, **prof.get("autobalance", {})}}


def module_env(prof: dict, mods: Path) -> dict[str, str]:
    """AC_* variables that make a worldserver's module settings exactly `prof`'s, whatever server/etc/modules holds:
    every key any profile sets gets the profile's value or else its .conf.dist default. Raises ConfigError (fail
    closed) when a key has no value to give."""
    ov = module_overrides(prof)
    union: dict[str, set[str]] = {d: set(v) for d, v in ov.items()}
    for name in PROFILES:
        for d, v in module_overrides(overrides(name)).items():
            union[d] |= set(v)
    env = {}
    for d, keys in union.items():
        for k in sorted(keys):
            val = ov[d].get(k)
            if val is None:
                val = _dist_value(mods / d, k)
            if val is None:
                raise ConfigError(f"lane B: {k} has no value (not in the profile, not in {mods / d})")
            env[env_var_name(k)] = _unquote(val)
    return env


class ConfigError(RuntimeError):
    pass


def common(logs: Path = P.LOGS, tmp: Path = P.TMP, source: Path = P.SQL_SOURCE) -> dict:
    """Keys both servers get. SourceDirectory and MySQLExecutable come from the resolver: dev the vendor/ tree and
    Homebrew's mysql, release the payload's sql/ and mysql/bin/mysql (release-build "As built": the compiled-in
    defaults name the build machine, so a release must always set them)."""
    return {
        "LogsDir": f'"{logs}"',
        "TempDir": f'"{tmp}"',
        "SourceDirectory": f'"{source}"',
        "MySQLExecutable": f'"{P.MYSQL}"',
        "BindIP": '"127.0.0.1"',
        "Updates.AutoSetup": "1",
    } | ({"Updates.ArchivedRedundancy": "0"} if P.MODE == "release" else {})   # release: archive/ holds names only


LANE_ENV_FILE = "ac-env.json"      # lane B: the AC_* variables stack._spawn gives the worldserver (in P.LANE_ETC)


def write_all(world_datadir: Path | None = None, extra_world: dict | None = None, *, etc: Path = P.LANE_ETC,
              logs: Path = P.LOGS, run: Path = P.RUN, tmp: Path = P.TMP, source: Path = P.SQL_SOURCE,
              profile: str | None = None, population_n: int | None = None, dist_dir: Path | None = None) -> dict:
    """Render authserver/worldserver/module confs into `etc` with the given profile (see PROFILES). The keyword
    paths default to this lane's stack; the unpatched baseline worldserver (core.login_logout_race_baseline) renders
    its own install's confs with them. The .conf.dist files come from `dist_dir` (default: `etc` in lane A, the
    install's server/etc in lane B).
    Lane B (second-stack): auth/world confs go to the lane's etc; module confs are rendered there too (a mirror for
    readers such as era.default_on) while the worldserver gets their values as AC_* env (etc/ac-env.json), because it
    only reads module confs from server/etc/modules, which stays lane A's."""
    prof = overrides(profile_name(profile), population_n)
    # lane B and release mode (release-first-launch design 3): module keys go to the server as AC_* env
    lane_b = (P.LANE != "a" or P.MODE == "release") and etc == P.LANE_ETC
    dist_dir = dist_dir or (P.SERVER_ETC if lane_b else etc)
    for d in (logs, tmp, run, etc):
        d.mkdir(parents=True, exist_ok=True)
    auth = common(logs, tmp, source) | {
        "LoginDatabaseInfo": _dbinfo("auth"),
        "PidFile": f'"{run / "authserver.pid"}"',
        "Updates.EnableDatabases": "1",
        "RealmServerPort": str(P.AUTH_PORT),
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
        "WorldServerPort": str(P.WORLD_PORT),
        "Ra.Enable": "0",
        "Updates.EnableDatabases": "7",
        "RealmID": "1",
        # Warden = anti-cheat that sends native code modules to the client. Pointless offline, and the prime
        # suspect for the client's ILLEGAL_INSTRUCTION crash at a non-module EIP under Wine+Rosetta (2026-10-01).
        "Warden.Enabled": "0",
        # seamless-experience (client-connectivity "No server-identity text on login"): no core version line at login
        # (CharacterHandler.cpp:846; the .dist default is 0 too, pinned here so a .dist change can't bring it back).
        # The motd and mod-playerbots' login lines are held back by mod-moonglade (Moonglade.QuietServerText).
        "Server.LoginInfo": "0",
        # character logins ("Login Character:[name]", CharacterHandler.cpp:1067, logger entities.player) in their own
        # timestamped server log, Players.log (flags 1 = time prefix, Appender.cpp:62), so checks can see when a client
        # entered the world; the auction house's info lines (playerbots trade there) stay out
        "Appender.Players": "2,4,1,Players.log,w",
        "Logger.entities.player": "4,Players",
        "Logger.entities.player.auctionhouse": "2,Players",
        # m4-dungeon-scaling (design.md §3): no "too many instances" limit. The core refuses a dungeon entry once the
        # account entered AccountInstancesPerHour instances in an hour (MapMgr.cpp:232-244; .dist default 5; 0 would
        # block everything). 1000 = one entry every 3.6 s for an hour: beyond any solo pace, still finite against a
        # runaway script. Profile lockouts-stock puts 5 back.
        "AccountInstancesPerHour": "1000",
    } | prof["world"] | (extra_world or {})
    render(dist_dir / "authserver.conf.dist", etc / "authserver.conf", auth)
    render(dist_dir / "worldserver.conf.dist", etc / "worldserver.conf", world)
    mods = dist_dir / "modules"
    rendered = []
    mod_ov = module_overrides(prof)
    if lane_b:
        (etc / "modules").mkdir(parents=True, exist_ok=True)
    for dist in sorted(mods.glob("*.conf.dist")) if mods.exists() else []:
        out = (etc / "modules" / dist.name) if lane_b else dist
        render(dist, out.with_suffix(""), mod_ov.get(dist.name, {}))
        rendered.append(dist.with_suffix("").name)
    out = {"authserver.conf": auth, "worldserver.conf": world, "modules": rendered, "profile": profile_name(profile)}
    if lane_b:
        env = module_env(prof, mods)
        f = etc / LANE_ENV_FILE
        tmp_f = f.with_name(f".{f.name}.{os.getpid()}.tmp")
        tmp_f.write_text(json.dumps(env, indent=1, sort_keys=True))
        os.replace(tmp_f, f)
        out["ac_env"] = sorted(env)
    if P.MODE == "release" and etc == P.LANE_ETC:
        # an empty OpenSSL config for the servers' OPENSSL_CONF (stack.spawn_env), so OpenSSL never opens the build
        # machine's compiled-in OPENSSLDIR
        P.OPENSSL_CONF.write_text("# Moonglade: intentionally empty (release mode sets OPENSSL_CONF to this file)\n")
        out["openssl_conf"] = str(P.OPENSSL_CONF)
    return out


def lane_env(etc: Path = P.LANE_ETC) -> dict[str, str]:
    """Lane B's AC_* variables as write_all wrote them (stack._spawn). Lane A: none. Fails closed (ConfigError)
    when lane B's file is missing or unreadable."""
    if P.LANE == "a" and P.MODE == "dev":
        return {}
    try:
        env = json.loads((etc / LANE_ENV_FILE).read_text())
    except (OSError, ValueError) as e:
        raise ConfigError(f"lane B: no readable {etc / LANE_ENV_FILE} ({e}); render the confs first") from e
    if not isinstance(env, dict) or not env or not all(k.startswith("AC_") for k in env):
        raise ConfigError(f"lane B: {etc / LANE_ENV_FILE} is not a map of AC_* variables")
    return {str(k): str(v) for k, v in env.items()}
