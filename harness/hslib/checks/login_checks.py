"""login-screen: the front menu in the client's own login screen (openspec/changes/in-client-login).

Runtime checks drive the real ./play with the real Wow.exe against a scratch play client (playclient_util.PlayRun),
logged in only as the harness test account HSTEST; never the player's account. Evidence is server-side (auth.account
last_login / online, read-only), the client's own logs (Logs/connection.log, GlueXML.log, FrameXML.log, WTF/Config.wtf)
and the client's own screenshots, whose marker squares encode the menu state (HSFrontMenu_UpdateMarker)."""
from __future__ import annotations

import json
import re
import shutil
import struct
import tempfile
import time
from pathlib import Path

from .. import frontmenu, paths as P, play
from ..registry import check, require
from .play_checks import is_clear, remaining, require_given_clear, _delete_scratch
from .playclient_util import (PlayRun, account_state, clear_logs, connection_log, db_ready, fresh_scratch,
                              glue_logs, marker, screenshots)

CAP = "login-screen"
LOGIN_LIMIT = 90               # spec: within 90 s
WAIT_MENU = 60                 # spec: no login for at least 60 s after the menu appears


def _wait(pred, timeout, step=1.0):
    t = time.time()
    while time.time() - t < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def _wait_shots(client_dir: Path, before: set, n: int, timeout: float) -> list[Path]:
    got = _wait(lambda: (lambda new: new if len(new) >= n else None)(
        [p for p in screenshots(client_dir) if p.name not in before]), timeout)
    return got or [p for p in screenshots(client_dir) if p.name not in before]


def _finish(run: PlayRun, ev: dict) -> None:
    ev["stop"] = run.stop()
    rec = run.record()
    ev["play_record"] = {k: rec.get(k) for k in ("outcome", "exit_code", "client")}
    ev["profiles_removed"] = ((rec.get("cleanup") or {}).get("profiles_removed"))
    ev["remaining_after"] = remaining()


def _no_secret(ev: dict) -> None:
    """The test account's password never shows up in ./play's record or output."""
    text = play.LAST.read_text() if play.LAST.exists() else ""
    ev["password_in_play_last_json"] = P.TEST_PASS in json.dumps(json.loads(text).get("client", {})) if text else False


# ---------------------------------------------------------------- Continue

@check("login.auto_login", order=387, capability=CAP, stack="down", tier="full")
def auto_login(ctx):
    """One synthetic Continue (the generated file's test flag; no keystrokes): within 90 s the authserver records a
    successful login for the profile's account (last_login), the worldserver opens its session (online = 1) and the
    client receives the character list (its own connection.log: COP_GET_CHARACTERS result=TRUE)."""
    ev: dict = {"account": P.TEST_USER, "glue_test": {"script": [{"t": 3, "act": "continue"}]},
                "input": "no keystrokes or input helpers; one Continue click by the GlueXML test hook"}
    require_given_clear(ev)
    scratch = fresh_scratch("login")
    run = PlayRun("login", glue_test=ev["glue_test"], scratch=scratch)
    try:
        run.start()
        up = run.wait_client()
        ev["client_up"] = {k: v for k, v in up.items() if k not in ("t",)}
        require(up.get("up"), "the real Wow.exe never came up", output=run.output()[-2500:], **ev)
        require(db_ready(), "database not reachable while the client runs", **ev)
        # baseline once the client is up: the play loop's own fixture step (before the client starts) may log the
        # test account in through the protocol probe, so only a newer last_login counts
        base = account_state(P.TEST_USER)
        ev["account_at_client_start"] = base
        samples = []

        def logged_in():
            st = account_state(P.TEST_USER)
            conn = connection_log(scratch)
            st |= {"t": round(time.time() - up["t"], 1),
                   "chars_received": bool(re.search(r"COP_GET_CHARACTERS code=\d+ result=TRUE", conn))}
            samples.append(st)
            ok = (st.get("last_login") or 0) > (base.get("last_login") or 0) and st.get("online") == 1 \
                and st["chars_received"]
            return st if ok else None
        hit = _wait(logged_in, LOGIN_LIMIT)
        ev["samples"] = samples[-5:]
        ev["login_seen"] = hit
        ev["connection_log"] = connection_log(scratch)[-1500:]
        require(hit is not None, f"no login + character list within {LOGIN_LIMIT} s of the client starting", **ev)
        _finish(run, ev)
        _no_secret(ev)
        require(not ev["password_in_play_last_json"], "the password appears in play-last.json", **ev)
        require((ev["profiles_removed"] or {}).get("removed") is True, "the generated profile file wasn't removed", **ev)
        require(is_clear(ev["remaining_after"]), "something is still running", **ev)
        return ev
    finally:
        run.cleanup()


@check("login.cancel_shows_list", order=388, capability=CAP, stack="down", tier="full")
def menu_waits(ctx):
    """With no Continue action, no login is attempted for at least 60 s after the menu appears (HSTEST last_login
    unchanged, never online) and the main menu with Continue stays up (marker squares in the client's own
    screenshots at ~5 s and ~65 s after the menu showed)."""
    glue = {"marker": True, "script": [{"t": 5, "act": "shot"}, {"t": 5 + WAIT_MENU, "act": "shot"}]}
    ev: dict = {"account": P.TEST_USER, "glue_test": glue}
    require_given_clear(ev)
    scratch = fresh_scratch("wait")
    run = PlayRun("wait", glue_test=glue, scratch=scratch)
    try:
        run.start()
        up = run.wait_client()
        ev["client_up"] = {k: v for k, v in up.items() if k != "t"}
        require(up.get("up"), "the real Wow.exe never came up", output=run.output()[-2500:], **ev)
        base = account_state(P.TEST_USER)
        ev["account_before"] = base
        samples, before = [], set()

        def done():
            st = account_state(P.TEST_USER)
            samples.append(st | {"t": round(time.time() - up["t"], 1)})
            return len([p for p in screenshots(scratch) if p.name not in before]) >= 2
        _wait(done, WAIT_MENU + 90, step=2)
        shots = [p for p in screenshots(scratch) if p.name not in before]
        ev["screens"] = [marker(p) for p in shots]
        ev["samples_first_last"] = [samples[0], samples[-1]] if samples else []
        ev["login_attempts"] = [s for s in samples if s.get("last_login") != base.get("last_login") or s.get("online")]
        ev["wow_alive_at_end"] = bool(run.wow_alive())
        ev["connection_log"] = connection_log(scratch)[-800:]
        require(len(shots) >= 2, "the client didn't take both screenshots", **ev)
        require(all(m["main_menu_up"] is True for m in ev["screens"]),
                "the main menu wasn't up in both screenshots", **ev)
        require(not ev["login_attempts"], "a login happened without Continue", **ev)
        require("COP_AUTHENTICATE" not in ev["connection_log"], "the client attempted a login (connection.log)", **ev)
        require(ev["wow_alive_at_end"], "the client quit by itself", **ev)
        _finish(run, ev)
        require(is_clear(ev["remaining_after"]), "something is still running", **ev)
        return ev
    finally:
        run.cleanup()


@check("login.no_glue_errors", order=389, capability=CAP, stack="down", tier="full")
def no_glue_errors(ctx):
    """Patched client with our GlueXML, menu-waits then Continue: our code runs (marker squares on screen), the play
    client's Logs/GlueXML.log and FrameXML.log hold no errors (absent or empty) and the client doesn't quit by itself
    through 20 s at character select."""
    glue = {"marker": True, "script": [{"t": 4, "act": "shot"}, {"t": 20, "act": "continue"}]}
    ev: dict = {"glue_test": glue}
    require_given_clear(ev)
    scratch = fresh_scratch("glue")
    run = PlayRun("glue", glue_test=glue, scratch=scratch)
    try:
        run.start()
        up = run.wait_client()
        ev["client_up"] = {k: v for k, v in up.items() if k != "t"}
        require(up.get("up"), "the real Wow.exe never came up", output=run.output()[-2500:], **ev)
        alive = []

        def at_charselect():
            alive.append(bool(run.wow_alive()))
            return re.search(r"COP_GET_CHARACTERS code=\d+ result=TRUE", connection_log(scratch))
        ev["reached_character_list"] = bool(_wait(at_charselect, 120))
        t = time.time()
        while time.time() - t < 20:
            alive.append(bool(run.wow_alive()))
            time.sleep(1)
        ev["alive_samples"] = {"n": len(alive), "dead": alive.count(False)}
        shots = screenshots(scratch)
        ev["screen"] = marker(shots[0]) if shots else None
        ev["logs"] = glue_logs(scratch)
        _finish(run, ev)
        require(ev["screen"] and ev["screen"]["main_menu_up"] is True, "our GlueXML didn't run (no marker)", **ev)
        require(not any((v or "").strip() for v in ev["logs"].values()), "GlueXML.log / FrameXML.log hold errors",
                **ev)
        require(ev["reached_character_list"], "never reached the character list", **ev)
        require(ev["alive_samples"]["dead"] == 0, "the client quit by itself", **ev)
        require(is_clear(ev["remaining_after"]), "something is still running", **ev)
        return ev
    finally:
        run.cleanup()


# ---------------------------------------------------------------- backdrop

def _config_value(client_dir: Path, key: str) -> str | None:
    p = client_dir / "WTF" / "Config.wtf"
    m = re.search(rf'^SET {key} "([^"]*)"', p.read_text(errors="replace"), re.M) if p.exists() else None
    return m.group(1) if m else None


@check("login.backdrop_choice", order=390, capability=CAP, stack="down", tier="full")
def backdrop_choice(ctx):
    """Picking TBC in the menu is applied (marker: TBC model and music requested by GlueXML), stored in the client's
    own settings when it saves them (WTF/Config.wtf accountList HSBDTBC, written on entering the world), and on the
    next start, with nothing picked, the TBC model and music are requested again."""
    ev: dict = {}
    require_given_clear(ev)
    scratch = fresh_scratch("backdrop")
    g1 = {"marker": True, "script": [{"t": 3, "act": "shot"}, {"t": 4, "act": "backdrop:TBC"}, {"t": 7, "act": "shot"},
                                     {"t": 9, "act": "continue"}], "enter_world_after_charlist": 3}
    run = PlayRun("backdrop1", glue_test=g1, scratch=scratch)
    try:
        run.start()
        up = run.wait_client()
        require(up.get("up"), "run 1: the real Wow.exe never came up", output=run.output()[-2500:], **ev)
        stored = _wait(lambda: _config_value(scratch, "accountList"), 180, step=2)
        shots = screenshots(scratch)
        ev["run1"] = {"glue_test": g1, "screens": [marker(p) for p in shots[:2]], "stored_accountList": stored,
                      "stored_accountName": _config_value(scratch, "accountName")}
        _finish(run, ev)
        ev["run1"]["remaining_after"] = ev.pop("remaining_after")
    finally:
        run.cleanup()
    s = ev["run1"]["screens"]
    require(len(s) == 2 and s[0]["backdrop"] == "WOTLK" and s[1]["backdrop"] == "TBC" and s[1]["music"] == "TBC",
            "run 1: default WotLK then picked TBC not both seen", **ev)
    require(ev["run1"]["stored_accountList"] and "HSBDTBC" in ev["run1"]["stored_accountList"],
            "run 1: the choice wasn't stored in WTF/Config.wtf", **ev)

    g2 = {"marker": True, "script": [{"t": 5, "act": "shot"}]}
    clear_logs(scratch)
    before = {p.name for p in screenshots(scratch)}
    run = PlayRun("backdrop2", glue_test=g2, scratch=scratch)
    try:
        run.start()
        up = run.wait_client()
        require(up.get("up"), "run 2: the real Wow.exe never came up", output=run.output()[-2500:], **ev)
        shots = _wait_shots(scratch, before, 1, 60)
        ev["run2"] = {"glue_test": g2, "screens": [marker(p) for p in shots],
                      "stored_accountList": _config_value(scratch, "accountList")}
        _finish(run, ev)
    finally:
        run.cleanup()
    s2 = ev["run2"]["screens"]
    require(s2 and s2[0]["backdrop"] == "TBC" and s2[0]["music"] == "TBC",
            "run 2 (restart): TBC model and music weren't requested", **ev)
    require(is_clear(ev["remaining_after"]), "something is still running", **ev)
    return ev


# ---------------------------------------------------------------- Continue context line

CTX_A, CTX_B = "HSCTXA", "HSCTXB"               # scratch accounts (never the player's)
CTX_PASS = "ctx12345"
CTX_CHARS = (("Hsctxwar", 1, 1), ("Hsctxpal", 1, 2))     # name, race (Human), class (Warrior, Paladin)


def _dbc_class_names_raw(path: Path) -> dict[int, str]:
    """Independent ChrClasses.dbc read (field 0 id, field 4 enUS name; DBCfmt.h ChrClassesEntryfmt)."""
    b = path.read_bytes()
    _, n, nf, rs, ss = struct.unpack("<4s4I", b[:20])
    strings = b[20 + n * rs:]
    out = {}
    for i in range(n):
        f = struct.unpack_from(f"<{nf}I", b, 20 + i * rs)
        out[f[0]] = strings[f[4]:strings.index(b"\0", f[4])].decode()
    return out


@check("login.continue_context", order=128, capability=CAP, stack="up", tier="full")
def continue_context(ctx):
    """The launcher's line under Continue names the account's most recently played character (latest logout_time) as
    "<name> · Level <n> <class>" with the class from ChrClasses.dbc (a renamed class in a scratch DBC shows up), adds
    " · <profile>" only with several profiles, shows "No characters yet" for an account without characters, and
    carries no credentials."""
    from .. import db, fixtures as F, soap
    from ..probe import client as probe
    ev: dict = {"accounts": [CTX_A, CTX_B]}
    real = None
    try:
        real = json.loads(play.PLAYER_FILE.read_text())["account"].upper()
    except (FileNotFoundError, ValueError, KeyError):
        pass
    require(real not in (CTX_A, CTX_B), "scratch account collides with the player's", **ev)
    tmp = Path(tempfile.mkdtemp(prefix="hs-ctx-"))
    try:
        for acct in (CTX_A, CTX_B):
            ev.setdefault("create", {})[acct] = play.ensure_player_account({"account": acct, "password": CTX_PASS})
        aid = int(db.scalar(f"SELECT id FROM account WHERE username='{CTX_A}'", "auth"))
        _, ws, _ = probe.open_world(CTX_A, CTX_PASS)
        with ws:
            have = {c["name"].lower() for c in ws.char_enum()}
            for name, race, cls in CTX_CHARS:
                if name.lower() not in have:
                    ev.setdefault("char_create", {})[name] = ws.char_create(name, race, cls)
        F.poll(lambda: len(db.query(f"SELECT guid FROM characters WHERE account={aid}", "characters")) >= 2,
               bool, timeout=20)
        dbc_names = _dbc_class_names_raw(P.CLIENT_DATA / "dbc" / "ChrClasses.dbc")
        ev["dbc_names"] = dbc_names
        profiles = [{"name": CTX_A, "account": CTX_A, "password": CTX_PASS},
                    {"name": CTX_B, "account": CTX_B, "password": CTX_PASS}]
        D = frontmenu.DOT
        cases = {}
        for latest in ("Hsctxpal", "Hsctxwar"):           # flip which one was played last
            other = "Hsctxwar" if latest == "Hsctxpal" else "Hsctxpal"
            db.query(f"UPDATE characters SET logout_time=1000 WHERE name='{other}'", "characters")
            db.query(f"UPDATE characters SET logout_time=2000 WHERE name='{latest}'", "characters")
            row = db.query(f"SELECT name, level, class FROM characters WHERE name='{latest}'", "characters")[0]
            want_line = f"{row[0]} {D} Level {row[1]} {dbc_names[int(row[2])]}"
            got_two = frontmenu.with_context(profiles)
            got_one = frontmenu.with_context(profiles[:1])
            cases[latest] = {"expected_line": want_line, "two_profiles": [p["context"] for p in got_two],
                             "one_profile": got_one[0]["context"]}
            require(got_two[0]["context"] == f"{want_line} {D} {CTX_A}", f"latest={latest}: wrong line (2 profiles)",
                    cases=cases, **ev)
            require(got_two[1]["context"] == f"{frontmenu.NO_CHARACTERS} {D} {CTX_B}",
                    "account without characters doesn't read 'No characters yet'", cases=cases, **ev)
            require(got_one[0]["context"] == want_line, f"latest={latest}: wrong line (1 profile)", cases=cases, **ev)
        ev["cases"] = cases
        # the class name comes from ChrClasses.dbc: rename Warrior in a scratch copy and it must show up
        raw = (P.CLIENT_DATA / "dbc" / "ChrClasses.dbc").read_bytes()
        (tmp / "ChrClasses.dbc").write_bytes(raw.replace(b"\0Warrior\0", b"\0Warxior\0", 1))
        ev["scratch_dbc_line"] = frontmenu.with_context(profiles[:1], dbc_dir=tmp)[0]["context"]
        require(ev["scratch_dbc_line"].endswith("Warxior"), "class name doesn't follow ChrClasses.dbc", **ev)
        # no hardcoded class table in our sources, no credentials in the line or its data
        srcs = [P.HARNESS / "hslib" / "frontmenu.py", P.ROOT / "client-mods" / "glue" / "HSFrontMenu.lua"]
        ev["hardcoded_class_names"] = {str(f.relative_to(P.ROOT)): [n for n in dbc_names.values() if n in f.read_text()]
                                       for f in srcs}
        # a table would name several classes; one example in a comment ("Level 23 Paladin") is not a source
        require(not any(len(v) >= 2 for v in ev["hardcoded_class_names"].values()),
                "our sources carry a class-name table", **ev)
        lua = frontmenu.render(frontmenu.with_context(profiles), CTX_A, about=frontmenu.about_info()).decode()
        ctx_lines = re.findall(r'context = "([^"]*)"', lua)
        about = re.search(r"HS_ABOUT = (\{.*?\});", lua).group(1)
        ev["rendered_contexts"] = ctx_lines
        ev["about_has_password"] = CTX_PASS in about
        require(len(ctx_lines) == 2 and not any(CTX_PASS in c for c in ctx_lines) and not ev["about_has_password"],
                "credentials in the context line or About data", **ev)
        return ev
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        ev["cleanup"] = [_delete_scratch(CTX_A), _delete_scratch(CTX_B)]
