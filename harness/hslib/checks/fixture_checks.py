"""harness-control fixtures, client-connectivity protocol probe, and lifecycle.persist."""
import random
import re
import subprocess
import time

from .. import db, fixtures as F, paths as P, soap, stack
from ..probe import client as probe
from ..registry import check, require

HS = str(P.HARNESS / "hs")


def _fx(fn, *a):
    """Run a fixture step; turn FixtureError into a CheckFail with its evidence."""
    try:
        return fn(*a)
    except F.FixtureError as e:
        require(False, str(e), **e.evidence)


# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "harness/hslib/fixtures.py",
    "harness/hslib/fixture.py",
    "harness/hslib/sharedfix.py",
    "harness/hslib/checks/fixture_checks.py",
)


@check("fixture.account_character", order=100, capability="harness-control", stack="up", layer="integration")
def account_character(ctx):
    """Fixture setup run twice leaves exactly one test account and one test character, both server-created.
    The account is first deleted through the server (SOAP `account delete`) so this run's evidence shows the
    creation itself: `account create` over SOAP and CMSG_CHAR_CREATE over the protocol."""
    reset = _fx(F.reset)
    first = _fx(F.ensure)
    second = _fx(F.ensure)
    acct = F.account_rows()
    chars = F.char_rows()
    on_account = db.scalar(f"SELECT COUNT(*) FROM characters WHERE account={F.account_id() or -1}", "characters")
    ev = {"reset": reset, "first_run": first, "second_run": second, "account_rows": acct, "character_rows": chars,
          "characters_on_test_account": on_account}
    require(len(acct) == 1, "expected exactly one test account", **ev)
    require(len(chars) == 1, "expected exactly one test character", **ev)
    require(on_account == "1", "test account should own exactly one character", **ev)
    require(int(chars[0][1]) == int(acct[0][0]), "test character not on the test account", **ev)
    require(first["account"]["created"] and "gm" in first["account"] and first["account"]["gm"]["ok"],
            "first run did not create the account via SOAP `account create`", **ev)
    require(first["character"]["created"] and first["character"]["char_create_result"].startswith("0x2F"),
            "first run did not create the character via CMSG_CHAR_CREATE (CHAR_CREATE_SUCCESS)", **ev)
    require(not second["account"]["created"] and not second["character"]["created"],
            "second run created something again (not idempotent)", **ev)
    ctx["fixture_guid"] = int(chars[0][0])
    return ev


@check("fixture.level", order=110, capability="harness-control", stack="up", layer="integration")
def level(ctx):
    """Harness sets the test character to level 10 → characters.level is 10."""
    if F.char_state(F.char_guid() or 0).get("level") == F.LEVEL:
        _fx(F.set_level, F.LEVEL - 1)    # make the change observable when already at 10
    ev = _fx(F.set_level, F.LEVEL)
    lv = db.scalar(f"SELECT level FROM characters WHERE guid={ev['guid']}", "characters")
    ev["db_level"] = lv
    require(ev["gm"]["ok"], "level GM command failed", **ev)
    require(lv == str(F.LEVEL), f"characters.level != {F.LEVEL}", **ev)
    return ev


@check("fixture.teleport", order=111, capability="harness-control", stack="up", layer="integration")
def teleport(ctx):
    """Harness teleports the test character to Goldshire → saved map/coords within 50 yd of its game_tele row."""
    ev = _fx(F.teleport, F.TELE_NAME)
    st, tele = F.char_state(ev["guid"]), ev["tele"]
    d = F.distance(st, tele)
    ev |= {"db_state": st, "db_distance_yd": round(d, 2)}
    require(ev["gm"]["ok"], "teleport GM command failed", **ev)
    require(st["map"] == tele["map"], "saved map != game_tele map", **ev)
    require(d <= 50.0, "saved position not within 50 yards of game_tele", **ev)
    return ev


@check("fixture.add_item", order=112, capability="harness-control", stack="up", layer="integration")
def add_item(ctx):
    """Harness gives the test character an item by entry → the entry is owned by it (inventory or mail) in the DB."""
    ev = _fx(F.add_item, F.ITEM_ENTRY)
    require(ev["gm"]["ok"], "send items GM command failed", **ev)
    require(ev["after"]["total"] > ev["before"]["total"], "item entry count owned by the character did not increase", **ev)
    require(ev["mail_item_rows"] and all(r[2] == r[3] == str(ev["guid"]) for r in ev["mail_item_rows"]),
            "no mail (mail + mail_items) addressed to the test character carries the item entry", **ev)
    return ev


@check("fixture.start_quest", order=113, capability="harness-control", stack="up", layer="integration")
def start_quest(ctx):
    """Harness starts a quest by ID → character_queststatus has it for the character with an incomplete status (3)."""
    q = _fx(F.describe_quest, F.QUEST_ID)
    require(q["starts_from_item"] == 0, "fixture quest starts from an item; `.quest add` refuses those", quest=q)
    require(int(q["req_npc_or_go_count1"] or 0) > 0 or int(q["req_item_count1"] or 0) > 0,
            "fixture quest has no objective, it would complete immediately", quest=q)
    ev = _fx(F.start_quest, F.QUEST_ID)
    require(not ev["before"] or ev.get("removed_saved"), "pre-existing quest row was not cleared before the add", **ev)
    require(ev["gm_add"]["ok"], "quest add GM command failed", **ev)
    rows = F.quest_status(ev["guid"], F.QUEST_ID)
    ev["db_rows"] = rows
    require(len(rows) == 1, "no character_queststatus row for the quest", **ev)
    require(rows[0][1] == "3", "quest status is not QUEST_STATUS_INCOMPLETE (3)", **ev)
    return ev


@check("client.protocol_probe", order=120, capability="client-connectivity", stack="up", layer="integration")
def protocol_probe(ctx):
    """Probe: SRP6 logon proof OK, realm list has localhost, world auth OK, enum lists the test character,
    player login yields SMSG_LOGIN_VERIFY_WORLD with the character's map and position."""
    _fx(F.ensure)
    guid = F.char_guid()
    _fx(F._require_offline, guid)
    db_before = F.char_state(guid)
    lr = probe.logon(P.TEST_USER, P.TEST_PASS)
    ev = {"logon": lr.evidence()}
    require(lr.ok, "logon proof failed", **ev)
    realm = probe.localhost_realm(lr.realms)
    ev["localhost_realm"] = realm
    require(realm is not None, f"realm list has no {P.HOST}:{P.WORLD_PORT} realm", **ev)
    with probe.WorldSession(P.TEST_USER, lr.session_key, realm) as ws:
        try:
            ev["world"] = ws.connect()
        except probe.ProbeError as e:
            require(False, f"world auth failed: {e}", **ev, **e.info)
        chars = ws.char_enum()
        ev["char_enum"] = chars
        mine = [c for c in chars if c["name"] == P.TEST_CHAR]
        require(mine and mine[0]["guid"] == guid, "char enum does not list the test character", **ev)
        lvw = ws.player_login(guid)
        ev["login_verify_world"] = lvw
        ev["db_saved_position"] = db_before
        ev["logout"] = _fx(F.logout, ws, guid)
        ev["trail"] = ws.log[:40]
    d = ((lvw["x"] - db_before["x"]) ** 2 + (lvw["y"] - db_before["y"]) ** 2 + (lvw["z"] - db_before["z"]) ** 2) ** 0.5
    ev["verify_world_vs_db_distance"] = round(d, 3)
    require(lvw["map"] == db_before["map"], "SMSG_LOGIN_VERIFY_WORLD map != character's saved map", **ev)
    require(d < 1.0, "SMSG_LOGIN_VERIFY_WORLD position != character's saved position", **ev)
    return ev


@check("client.protocol_badpass", order=121, capability="client-connectivity", stack="up", layer="integration")
def protocol_badpass(ctx):
    """Wrong password: challenge accepted (account exists) but the logon proof is rejected with AC's
    proof rejection (WOW_FAIL_UNKNOWN_ACCOUNT 0x04 after a successful challenge), no session key is issued, and the world server refuses a session."""
    _fx(F.ensure_account)
    key_sql = f"SELECT HEX(session_key) FROM account WHERE username='{P.TEST_USER.upper()}'"
    key_before = db.scalar(key_sql, "auth")
    auth_log = P.LOGS / "Auth.log"
    log_off = auth_log.stat().st_size if auth_log.exists() else 0
    lr = probe.logon(P.TEST_USER, P.TEST_PASS + "-wrong", want_realms=False)
    key_after = db.scalar(key_sql, "auth")
    new_log = None
    if auth_log.exists():   # the server logs after sending the reply, so give it a moment
        new_log, _, _ = F.poll(lambda: auth_log.read_text(errors="replace")[log_off:],
                               lambda t: "tried to login with invalid password" in t, timeout=5, interval=0.2)
    ev = {"logon": lr.evidence(), "session_key_unchanged": key_before == key_after,
          "authserver_log_new": None if new_log is None else new_log[-1500:],
          "note": "AuthSession::HandleLogonProof answers a failed SRP6 verification (wrong password) with "
                  "AUTH_LOGON_PROOF error WOW_FAIL_UNKNOWN_ACCOUNT (0x04) and logs 'tried to login with invalid password'"}
    require(lr.challenge_result == probe.W.WOW_SUCCESS, "challenge was not accepted (account missing?)", **ev)
    require(lr.proof_result == probe.W.WOW_FAIL_UNKNOWN_ACCOUNT,
            "logon proof not rejected with WOW_FAIL_UNKNOWN_ACCOUNT (0x04) after a successful challenge", **ev)
    require(lr.session_key is None and not lr.server_proof_ok, "probe obtained a session key", **ev)
    require(ev["session_key_unchanged"], "server stored a new session key for a failed logon", **ev)
    # Informational only: authserver's file appender doesn't flush promptly, so the line can lag the reply.
    ev["authserver_logged_invalid_password"] = bool(new_log and "tried to login with invalid password" in new_log)
    # No world session: without K the only thing a client can do is guess; the server must refuse it.
    realm = {"address": f"{P.HOST}:{P.WORLD_PORT}", "id": 1}
    ws = probe.WorldSession(P.TEST_USER, random.randbytes(40), realm, timeout=10)
    try:
        ws.connect()
        world = "AUTH_OK"
    except probe.ProbeError as e:
        world = f"rejected: {e} {e.info}"
    except (ConnectionError, OSError, ValueError) as e:
        world = f"rejected: server closed/garbled ({type(e).__name__}: {e})"
    finally:
        ws.close()
    ev["world_session_attempt_with_guessed_key"] = world
    ev["world_trail"] = ws.log
    require(world != "AUTH_OK", "world server accepted a session without a valid session key", **ev)
    return ev


INSTANT_LOGOUT_MAX_S = 5.0   # request → SMSG_LOGOUT_COMPLETE; the non-instant path waits the server's 20 s timer


def _others_snapshot(test_id: int) -> dict:
    """rbac + account_access rows of every account except the test account (must not change)."""
    return {"rbac": db.query(f"SELECT accountId, permissionId, granted, realmId FROM rbac_account_permissions "
                             f"WHERE accountId<>{int(test_id)} ORDER BY 1, 2, 4", "auth"),
            "account_access": db.query(f"SELECT id, gmlevel, RealmID FROM account_access WHERE id<>{int(test_id)} "
                                       f"ORDER BY 1, 3", "auth")}


def _rbac_list(output: str) -> dict:
    """Parse `.rbac account list` (acore_string 65/66/67/71). The "granted" section lists the account's own grants
    plus those inherited from its security level, so own grants = granted - inherited."""
    sec = re.search(r"inherited permissions by sec level (\d+) \((\w+)\)", output)
    granted_part, _, rest = output.partition("denied permissions:")
    denied_part, _, inherited_part = rest.partition("inherited permissions")
    ids = lambda t: [int(x) for x in re.findall(r"^- (\d+) \(", t, re.M)]
    g, inh = ids(granted_part), ids(inherited_part)
    return {"granted": g, "denied": ids(denied_part), "inherited": inh, "own_grants": sorted(set(g) - set(inh)),
            "sec_level": int(sec.group(1)) if sec else None, "sec_name": sec.group(2) if sec else None}


@check("fixture.instant_logout", order=122, capability="harness-control", stack="up", layer="integration")
def instant_logout(ctx):
    """Probe logout of the test character (not resting) is instant (SMSG_LOGOUT_RESPONSE instant=1, completes in
    < 5 s, no 20 s timer); the test account holds only the instant-logout permission and is still security level 0."""
    _fx(F.ensure)
    acct = F.account_id()
    others_before = _others_snapshot(acct)
    # Re-run the grant inside this check: revoke through the server, then the fixture grants it again.
    ev = {"revoke": soap.gm(f"rbac account revoke {P.TEST_USER} {F.INSTANT_LOGOUT_PERM}").evidence()}
    F.poll(lambda: F.rbac_rows(acct), lambda v: not v, timeout=15)
    ev["rows_after_revoke"] = F.rbac_rows(acct)
    ev["fixture"] = _fx(F.ensure)
    ev["others_unchanged"] = _others_snapshot(acct) == others_before
    guid = F.char_guid()
    _fx(F._require_offline, guid)
    lr, ws, _ = probe.open_world(P.TEST_USER, P.TEST_PASS)
    with ws:
        ws.char_enum()
        ev["login_verify_world"] = ws.player_login(guid)
        ev["settle"] = _fx(F.settle_login, ws, guid)   # see fixtures.settle_login (core race on instant logout)
        t0 = time.time()
        ev["logout"] = ws.logout(timeout=30)
        ev["logout_request_to_complete_s"] = round(time.time() - t0, 2)
    st, offline, _ = F.poll(lambda: F.char_state(guid), lambda s: s.get("online") == 0, timeout=15)
    rest = db.scalar(f"SELECT is_logout_resting FROM characters WHERE guid={guid}", "characters")
    rbac_list = soap.gm(f"rbac account list {P.TEST_USER}")
    ev |= {"saved_offline": offline, "is_logout_resting": rest, "rbac_rows": F.rbac_rows(acct),
           "account_access_rows": F.access_rows(acct), "rbac_list": rbac_list.evidence(),
           "rbac_list_parsed": _rbac_list(rbac_list.output),
           "perm_1_linked_children": db.query(f"SELECT linkedId FROM rbac_linked_permissions WHERE id={F.INSTANT_LOGOUT_PERM}", "auth"),
           "note": "is_logout_resting is PLAYER_FLAGS_RESTING when the server saved the character on logout "
                   "(Player::SaveToDB): 0 means the instant logout came from the permission, not a rest area"}
    require(ev["fixture"]["account"]["instant_logout"].get("granted_now") is True,
            "fixture did not grant instant logout after the revoke", **ev)
    require(ev["others_unchanged"], "rbac/account_access rows of another account changed", **ev)
    require(ev["logout"]["logout_result"] == 0, "logout refused", **ev)
    require(rest == "0", "character was resting at logout (rest area); the test is not valid there", **ev)
    require(ev["logout"]["instant"] is True, "SMSG_LOGOUT_RESPONSE did not flag an instant logout", **ev)
    require(ev["logout_request_to_complete_s"] < INSTANT_LOGOUT_MAX_S,
            f"logout took {ev['logout_request_to_complete_s']} s (>= {INSTANT_LOGOUT_MAX_S} s)", **ev)
    require(offline, "character not saved offline after logout", **ev)
    require(ev["rbac_rows"] == F.INSTANT_ONLY, "test account rbac rows are not exactly [instant logout]", **ev)
    require(ev["perm_1_linked_children"] == [], "permission 1 links other permissions", **ev)
    require(all(r[0] == "0" for r in ev["account_access_rows"]), "test account has a security level above 0", **ev)
    p = ev["rbac_list_parsed"]
    require(rbac_list.ok and p["own_grants"] == [F.INSTANT_LOGOUT_PERM] and p["denied"] == [] and p["sec_level"] == 0,
            "server's `.rbac account list` does not show exactly [1] granted beyond sec level 0's defaults", **ev)
    return ev


@check("lifecycle.persist", order=150, capability="server-lifecycle", stack="up", layer="integration")
def persist(ctx):
    """Level changed via the harness survives `hs down` + `hs up`."""
    guid_ev = _fx(F.ensure)
    guid = F.char_guid()
    cur = F.char_state(guid).get("level")
    target = 12 if cur != 12 else 13
    set_ev = _fx(F.set_level, target)
    before = db.scalar(f"SELECT level FROM characters WHERE guid={guid}", "characters")
    ev = {"guid": guid, "set_level": set_ev, "db_level_before_restart": before, "fixture": guid_ev}
    require(before == str(target), "level change not saved before restart", **ev)
    d = subprocess.run([HS, "down"], capture_output=True, text=True, timeout=600)
    ev["down"] = {"exit": d.returncode, "stdout": d.stdout[-800:], "stderr": d.stderr[-800:],
                  "remaining": stack.stack_processes()}
    require(d.returncode == 0, "hs down failed", **ev)
    require(not any(ev["down"]["remaining"].values()), "stack still running after hs down", **ev)
    u = subprocess.run([HS, "up"], capture_output=True, text=True, timeout=2400)
    ev["up"] = {"exit": u.returncode, "stdout": u.stdout[-800:], "stderr": u.stderr[-800:]}
    require(u.returncode == 0, "hs up failed", **ev)
    after = db.scalar(f"SELECT level FROM characters WHERE guid={guid}", "characters")
    ev["db_level_after_restart"] = after
    lr, ws, _ = probe.open_world(P.TEST_USER, P.TEST_PASS)
    with ws:
        c = next((c for c in ws.char_enum() if c["guid"] == guid), None)
    ev["char_enum_level_after_restart"] = c and c["level"]
    require(after == str(target), "characters.level changed across restart", **ev)
    require(c and c["level"] == target, "server reports a different level after restart", **ev)
    return ev
