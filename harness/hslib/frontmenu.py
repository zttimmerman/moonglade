"""The front menu's generated profile file (openspec/changes/in-client-login, login-screen; design.md "Credentials").

At each start of the play client the launcher writes `Interface\\GlueXML\\HSProfiles.lua` with the profile list into a
small archive, `Data/<locale>/patch-<locale>-5.MPQ`, inside the play client only. It ranks above the front-menu
archive (patch-<locale>-4.MPQ, patch 0002), whose HSProfiles.lua is an empty default, so the generated list wins.

The file holds logins: the same secret as env/player.json (gitignored). So it is
- written only into a play client (never client/; refused there), mode 0600, and removed again when the play loop
  ends (`remove`);
- never printed: the evidence this module returns names the archive and the profile names, never a password.

Test hook (harness checks only): `test` becomes the Lua table HS_TEST (see client-mods/glue/HSFrontMenu.lua):
cancel, countdown, quit_after_charlist, script = [{"t": seconds, "act": "shot"|"cancel"|"quit"|"backdrop:TBC"}].
`./play` takes it from $HS_GLUE_TEST (JSON).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import mpq, paths as P
from .clientbuild import locale

INSIDE = "Interface\\GlueXML\\HSProfiles.lua"
ARCHIVE = "Data/{locale}/patch-{locale}-5.MPQ"
TEST_ENV = "HS_GLUE_TEST"


class FrontMenuError(RuntimeError):
    pass


def _lua(v) -> str:
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):   # UTF-8 (what the client's fonts expect); anything outside a safe ASCII set as \ddd bytes
        safe = set(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-.:,;/!?()[]=+*#@%^&|<>~'")
        return '"' + "".join(chr(b) if b in safe else f"\\{b:03d}" for b in v.encode("utf-8")) + '"'
    if isinstance(v, list):
        return "{ " + ", ".join(_lua(x) for x in v) + " }"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_lua(x)}" for k, x in v.items() if k.isidentifier()) + " }"
    raise FrontMenuError(f"can't put {type(v).__name__} into the profile file")


ABOUT_KEYS = ("version", "commit", "core", "playerbots", "vanilla", "world")


def about_info() -> dict:
    """What the front menu's About page shows besides the client's own version: Moonglade's version and commit,
    the core/playerbots pins (fork.lock) and the world the server is running now (vanilla mode from the rendered
    mod_era_profile.conf, world size from the running profile). Never anything secret."""
    import re
    import subprocess
    from . import app, config, stack
    lock = P.fork_lock()
    r = subprocess.run(["git", "-C", str(P.ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    commit = r.stdout.strip() or "unknown"
    dirty = subprocess.run(["git", "-C", str(P.ROOT), "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True).stdout.strip()
    era = P.SERVER_ETC / "modules" / config.ERA_CONF
    m = re.search(r"^\s*EraProfile\.Enable\s*=\s*(\d)", era.read_text(), re.M) if era.exists() else None
    rp = stack.running_profile()
    if not rp:
        world = "server not running"
    elif rp["profile"] == "population":
        names = {v: k for k, v in config.WORLD_SIZES.items()}
        n = rp.get("n")
        world = f"{names[n]} ({n} AI players)" if n in names else f"{n} AI players"
    else:
        world = f"test world ({rp['profile']})"
    return {"version": app.info_plist()["CFBundleShortVersionString"],
            "commit": commit + ("+changes" if dirty else ""),
            "core": f"{lock.get('core_branch', '?')} @ {lock.get('core_sha', '?')[:8]}",
            "playerbots": f"{lock.get('playerbots_branch', '?')} @ {lock.get('playerbots_sha', '?')[:8]}",
            "vanilla": ("on" if m.group(1) == "1" else "off") if m else "unknown",
            "world": world}


DOT = "\u00b7"           # middle dot, "Arthas · Level 23 Paladin"
NO_CHARACTERS = "No characters yet"


def last_character(account: str) -> dict | None:
    """The account's most recently played character (latest logout_time), read-only: {name, level, class}."""
    from . import db
    name = account.upper()
    if not re.fullmatch(r"[A-Z0-9]{1,16}", name):
        raise FrontMenuError(f"bad account name {account!r}")
    aid = db.scalar(f"SELECT id FROM account WHERE username='{name}'", "auth")
    if not aid:
        return None
    rows = db.query(f"SELECT name, level, class FROM characters WHERE account={int(aid)} AND deleteDate IS NULL "
                    "ORDER BY logout_time DESC, guid DESC LIMIT 1", "characters")
    if not rows:
        return None
    n, lvl, cls = rows[0]
    return {"name": n, "level": int(lvl), "class": int(cls)}


def continue_context(account: str, profile_name: str, several: bool, classes: dict[int, str] | None = None,
                     dbc_dir: Path | None = None) -> str:
    """The line under Continue: "<character> · Level <n> <class>" (class name from ChrClasses.dbc), or
    "No characters yet"; " · <profile>" is appended when there are several profiles. Never credentials."""
    from . import dbc
    ch = last_character(account)
    if ch:
        classes = classes if classes is not None else dbc.class_names(*([dbc_dir] if dbc_dir else []))
        line = f"{ch['name']} {DOT} Level {ch['level']} {classes.get(ch['class'], 'class ' + str(ch['class']))}"
    else:
        line = NO_CHARACTERS
    return f"{line} {DOT} {profile_name}" if several else line


def with_context(profiles: list[dict], dbc_dir: Path | None = None) -> list[dict]:
    """profiles + a "context" line each (needs the stack's database). dbc_dir: test-only (a scratch ChrClasses.dbc)."""
    several = len(profiles) > 1
    return [p | {"context": continue_context(p["account"], p.get("name") or p["account"], several, dbc_dir=dbc_dir)}
            for p in profiles]


def render(profiles: list[dict], default: str | None, test: dict | None = None, about: dict | None = None) -> bytes:
    """Lua source for HSProfiles.lua. profiles = [{"name", "account", "password"}]; about = about_info()."""
    lines = ["-- Generated by the Moonglade launcher at each start (harness/hslib/frontmenu.py). Holds logins:",
             "-- never commit or copy it. It's removed again when the game closes.",
             "HS_PROFILES = {"]
    for p in profiles:
        lines.append("\t" + _lua({"name": p.get("name") or p["account"], "account": p["account"],
                                  "password": p["password"], "context": p.get("context")}) + ",")
    about = {k: str(v) for k, v in (about or {}).items() if k in ABOUT_KEYS}
    lines += ["};", f"HS_PROFILE_DEFAULT = {_lua(default)};", f"HS_ABOUT = {_lua(about)};",
              f"HS_TEST = {_lua(test) if test else 'nil'};", ""]
    return "\r\n".join(lines).encode("ascii")


def archive_path(client_dir: Path) -> Path:
    loc = locale(client_dir)
    return Path(client_dir) / ARCHIVE.replace("{locale}", loc)


def test_hook_from_env() -> dict | None:
    raw = os.environ.get(TEST_ENV)
    if not raw:
        return None
    try:
        t = json.loads(raw)
    except ValueError as e:
        raise FrontMenuError(f"${TEST_ENV} is not JSON: {e}") from e
    if not isinstance(t, dict):
        raise FrontMenuError(f"${TEST_ENV} must be a JSON object")
    return t


def write(client_dir: Path, profiles: list[dict], default: str | None = None, test: dict | None = None,
          about: dict | None = None) -> dict:
    """Write the profile archive into a play client. Returns evidence without any password."""
    client_dir = Path(client_dir)
    if mpq._under_client(client_dir):
        raise FrontMenuError("refusing to write the profile file into client/ (only a play client gets it)")
    if not (client_dir / "hs-build.json").is_file():
        raise FrontMenuError(f"{client_dir} is not a play client (no hs-build.json); run `harness/hs client-build`")
    dest = archive_path(client_dir)
    tmp = dest.with_name(dest.name + ".hs-new")
    tmp.unlink(missing_ok=True)
    os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))   # 0600 before any secret is written
    files = {INSIDE: render(profiles, default, test, about)}
    try:
        mpq.write_archive(tmp, files)
        mpq.verify_written(tmp, files)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return {"archive": str(dest.relative_to(client_dir)), "mode": oct(dest.stat().st_mode & 0o777),
            "profiles": [p.get("name") or p["account"] for p in profiles],
            "contexts": {p.get("name") or p["account"]: p.get("context") for p in profiles}, "default": default,
            "test_hook": test, "about": about}


def remove(client_dir: Path) -> dict:
    try:
        p = archive_path(Path(client_dir))
    except Exception as e:  # noqa: BLE001
        return {"removed": False, "why": str(e)}
    tmp = p.with_name(p.name + ".hs-new")             # a write killed mid-way leaves this (0600, holds logins)
    existed, tmp_existed = p.exists(), tmp.exists()
    p.unlink(missing_ok=True)
    tmp.unlink(missing_ok=True)
    gone = (existed and not p.exists(), tmp_existed and not tmp.exists())   # what was really deleted, checked after
    return {"archive": str(p), "removed": any(gone), "archive_removed": gone[0], "partial_write_removed": gone[1],
            "left_behind": [str(x) for x in (p, tmp) if x.exists()]}
