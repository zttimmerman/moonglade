"""Render server *.conf from installed *.conf.dist with key overrides. Regenerated on every `up`."""
import re
from pathlib import Path

from . import paths as P


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


def common() -> dict:
    return {
        "LogsDir": f'"{P.LOGS}"',
        "TempDir": f'"{P.ENV / "tmp"}"',
        "SourceDirectory": f'"{P.CORE_SRC}"',
        "MySQLExecutable": f'"{P.BREW / "opt/mysql/bin/mysql"}"',
        "BindIP": '"127.0.0.1"',
        "Updates.AutoSetup": "1",
    }


def write_all(world_datadir: Path | None = None, extra_world: dict | None = None) -> dict:
    for d in (P.LOGS, P.ENV / "tmp"):
        d.mkdir(parents=True, exist_ok=True)
    auth = common() | {
        "LoginDatabaseInfo": _dbinfo("auth"),
        "PidFile": f'"{P.RUN / "authserver.pid"}"',
        "Updates.EnableDatabases": "1",
    }
    world = common() | {
        "LoginDatabaseInfo": _dbinfo("auth"),
        "WorldDatabaseInfo": _dbinfo("world"),
        "CharacterDatabaseInfo": _dbinfo("characters"),
        "DataDir": f'"{world_datadir or P.CLIENT_DATA}"',
        "PidFile": f'"{P.RUN / "worldserver.pid"}"',
        "Console.Enable": "0",
        "SOAP.Enabled": "1",
        "SOAP.IP": '"127.0.0.1"',
        "SOAP.Port": str(P.SOAP_PORT),
        "Ra.Enable": "0",
        "Updates.EnableDatabases": "7",
        "RealmID": "1",
    } | (extra_world or {})
    render(P.SERVER_ETC / "authserver.conf.dist", P.SERVER_ETC / "authserver.conf", auth)
    render(P.SERVER_ETC / "worldserver.conf.dist", P.SERVER_ETC / "worldserver.conf", world)
    mods = P.SERVER_ETC / "modules"
    rendered = []
    for dist in sorted(mods.glob("*.conf.dist")) if mods.exists() else []:
        ov = {}
        if dist.name == "playerbots.conf.dist":
            ov["PlayerbotsDatabaseInfo"] = _dbinfo("playerbots")
            # M0 profile: small population. Defaults (500 random bots + 50 addclass accounts) are too heavy for
            # a 16 GB Mac and slow first boot (docs/research/playerbots-scripting.md §5). M2 sets the real budget.
            ov |= {"AiPlayerbot.MinRandomBots": "10", "AiPlayerbot.MaxRandomBots": "10",
                   "AiPlayerbot.AddClassAccountPoolSize": "5",
                   # command server binds 0.0.0.0 with no address option (PlayerbotCommandServer.cpp:62); off.
                   "AiPlayerbot.CommandServerPort": "0"}
        render(dist, dist.with_suffix(""), ov)
        rendered.append(dist.with_suffix("").name)
    return {"authserver.conf": auth, "worldserver.conf": world, "modules": rendered}
