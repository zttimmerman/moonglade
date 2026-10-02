"""Test fixtures: test account (SOAP `account create`), test character (probe CMSG_CHAR_CREATE), and
server-driven state drivers (level / teleport / item / quest). Nothing here inserts rows directly;
the DB is only read, to observe what the server saved.

Drivers (see design.md "Fixture state drivers"):
  level     `.character level <name> <n>`              offline: server runs CHAR_UPD_LEVEL
  teleport  `.teleport name <name> <game_tele name>`    offline: Player::SavePositionInDB
  item      `.send items <name> "subj" "text" <entry>`   mail: item_instance + mail_items
  quest     `.quest add <id> <name>` while the probe holds the character in the world, then a protocol
            logout (the server saves the quest log). The offline form of `.quest add` is NOT used: it
            writes character_queststatus.status = 1 (QUEST_STATUS_COMPLETE), see cs_quest.cpp.
"""
import re
import time

from . import db, paths as P, soap
from .probe import client as probe

# Human (race 1) warrior (class 1): starts in Northshire. ChrRaces/ChrClasses ids per SharedDefines.h.
RACE_HUMAN, CLASS_WARRIOR = 1, 1

# Fixture targets. Each is validated against the world DB at use (see describe_*), never assumed.
TELE_NAME = "Goldshire"
ITEM_ENTRY = 2589          # validated in item_template at runtime
QUEST_ID = 7               # validated in quest_template at runtime (must have a kill/collect objective)
LEVEL = 10

# Instant logout for the test account only (harness-control "Test account logs out instantly"). Permission id 1 is
# rbac::RBAC_PERM_INSTANT_LOGOUT (vendor core src/server/game/Accounts/RBAC.h; auth rbac_permissions row
# (1,'Instant logout'), no rbac_linked_permissions children), checked in WorldSession::HandleLogoutRequestOpcode.
# Granted with the server's own console command `.rbac account grant` (cs_rbac.cpp), which writes
# auth.rbac_account_permissions (accountId, 1, granted=1, realmId=-1). The security level stays 0.
INSTANT_LOGOUT_PERM = 1


class FixtureError(Exception):
    def __init__(self, msg: str, **evidence):
        super().__init__(msg)
        self.evidence = evidence


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "''")


def gm(cmd: str, ok_required=True) -> soap.GMResult:
    r = soap.gm(cmd)
    if ok_required and not r.ok:
        raise FixtureError(f"GM command failed: {cmd}", gm=r.evidence())
    return r


def poll(fn, pred, timeout=30.0, interval=0.5):
    """Call fn() until pred(value) is true; return (value, ok, elapsed)."""
    t0 = time.time()
    while True:
        v = fn()
        if pred(v):
            return v, True, round(time.time() - t0, 2)
        if time.time() - t0 > timeout:
            return v, False, round(time.time() - t0, 2)
        time.sleep(interval)


# ----------------------------------------------------------------------------- DB observers (read-only)

def account_rows() -> list[list[str]]:
    return db.query(f"SELECT id, username FROM account WHERE username='{_q(P.TEST_USER.upper())}'", "auth")


def account_id() -> int | None:
    rows = account_rows()
    return int(rows[0][0]) if rows else None


def char_rows() -> list[list[str]]:
    return db.query(f"SELECT guid, account, name, race, class, level, online FROM characters "
                    f"WHERE name='{_q(P.TEST_CHAR)}'", "characters")


def char_guid() -> int | None:
    rows = char_rows()
    return int(rows[0][0]) if rows else None


def char_state(guid: int) -> dict:
    rows = db.query(f"SELECT level, map, position_x, position_y, position_z, zone, online FROM characters WHERE guid={guid}",
                    "characters")
    if not rows:
        return {}
    lv, m, x, y, z, zone, online = rows[0]
    return {"level": int(lv), "map": int(m), "x": float(x), "y": float(y), "z": float(z), "zone": int(zone),
            "online": int(online)}


# ----------------------------------------------------------------------------- account + character

def rbac_rows(acct: int) -> list[list[str]]:
    """auth.rbac_account_permissions rows (permissionId, granted, realmId) for an account id."""
    return db.query(f"SELECT permissionId, granted, realmId FROM rbac_account_permissions WHERE accountId={int(acct)} "
                    f"ORDER BY permissionId, realmId", "auth")


def access_rows(acct: int) -> list[list[str]]:
    """auth.account_access rows (gmlevel, RealmID) for an account id; none = security level 0."""
    return db.query(f"SELECT gmlevel, RealmID FROM account_access WHERE id={int(acct)} ORDER BY RealmID", "auth")


INSTANT_ONLY = [[str(INSTANT_LOGOUT_PERM), "1", "-1"]]


def ensure_instant_logout() -> dict:
    """Grant the test account (and only it) the instant-logout permission via `.rbac account grant`, if missing.
    Never applied to any other account: the account name is the harness's own TEST_USER constant."""
    acct = account_id()
    if acct is None:
        raise FixtureError("no test account to grant instant logout to")
    before = rbac_rows(acct)
    if before == INSTANT_ONLY:
        return {"granted_now": False, "rows": before}
    r = gm(f"rbac account grant {P.TEST_USER} {INSTANT_LOGOUT_PERM}")
    rows, ok, dt = poll(lambda: rbac_rows(acct), lambda v: [str(INSTANT_LOGOUT_PERM), "1", "-1"] in v, timeout=15)
    if not ok:
        raise FixtureError("instant-logout permission not in rbac_account_permissions after `rbac account grant`",
                           gm=r.evidence(), rows=rows)
    return {"granted_now": True, "rows_before": before, "rows": rows, "gm": r.evidence(), "wait_s": dt}


def ensure_account() -> dict:
    """Create the test account via SOAP `account create` if it doesn't exist; grant it instant logout."""
    if account_id():
        return {"account": P.TEST_USER, "created": False, "id": account_id(), "instant_logout": ensure_instant_logout()}
    r = gm(f"account create {P.TEST_USER} {P.TEST_PASS}")
    acct, ok, dt = poll(account_id, bool, timeout=15)
    if not ok:
        raise FixtureError("account not visible in auth.account after `account create`", gm=r.evidence())
    return {"account": P.TEST_USER, "created": True, "id": acct, "gm": r.evidence(),
            "instant_logout": ensure_instant_logout()}


def ensure_character() -> dict:
    """Create the test character via the probe's CMSG_CHAR_CREATE if the account has no such character."""
    lr, ws, wev = probe.open_world(P.TEST_USER, P.TEST_PASS)
    with ws:
        chars = ws.char_enum()
        mine = [c for c in chars if c["name"].lower() == P.TEST_CHAR.lower()]
        if mine:
            return {"character": P.TEST_CHAR, "created": False, "guid": mine[0]["guid"], "enum": chars}
        code = ws.char_create(P.TEST_CHAR, RACE_HUMAN, CLASS_WARRIOR)
        name = probe.W.RESPONSE_NAMES.get(code, "?")
        if code != probe.W.CHAR_CREATE_SUCCESS:
            raise FixtureError("CMSG_CHAR_CREATE rejected", result=f"0x{code:02X} {name}", enum_before=chars)
        chars = ws.char_enum()
    guid, ok, _ = poll(char_guid, bool, timeout=15)
    if not ok:
        raise FixtureError("character not in characters DB after SMSG_CHAR_CREATE success")
    return {"character": P.TEST_CHAR, "created": True, "char_create_result": f"0x{code:02X} {name}",
            "guid": guid, "enum_after": chars}


def ensure() -> dict:
    """Idempotent: test account + test character, both created through the server."""
    a = ensure_account()
    c = ensure_character()
    return {"account": a, "character": c}


def reset() -> dict:
    """Delete the test account (and its characters) through the server: SOAP `account delete`."""
    if not account_id():
        return {"deleted": False}
    r = gm(f"account delete {P.TEST_USER}")
    _, ok1, _ = poll(account_id, lambda v: v is None, timeout=20)
    _, ok2, _ = poll(char_rows, lambda v: not v, timeout=20)
    if not (ok1 and ok2):
        raise FixtureError("test account/character still present after `account delete`", gm=r.evidence(),
                           account=account_rows(), chars=char_rows())
    return {"deleted": True, "gm": r.evidence()}


LOGIN_SETTLE_MARGIN_S = 0.25   # after the barrier: lets a session update run the ready login callbacks


def settle_login(ws, guid: int) -> dict:
    """Wait until the server has finished the character-DB work it queued at login, before a probe logout.

    Why: with instant logout the probe can log out within milliseconds of SMSG_LOGIN_VERIFY_WORLD. The core's
    ServerMailReward::OnPlayerLogin (src/server/scripts/World/server_mail.cpp) queues async SELECTs whose callbacks
    call SendServerMail(session->GetPlayer(), ...) without a null check; if they run after the logout, the
    worldserver segfaults (seen 2026-10-02: ServerMailCondition::CheckCondition ← SendServerMail). A human client
    can't log out that fast, so this is a probe-only window; we don't patch the core for it.

    Barrier: CharacterDatabase has one async worker (WorkerThreads = 1, asserted), so its queue is FIFO. The probe
    sends CMSG_UPDATE_ACCOUNT_DATA for a per-character type (data unchanged, a new timestamp); the server queues a
    REPLACE INTO character_account_data after the login SELECTs. When that row shows the new timestamp, the login
    queries have completed; a short margin lets the session's next update run their callbacks."""
    conf = (P.SERVER_ETC / "worldserver.conf").read_text()
    m = re.search(r"^CharacterDatabase\.WorkerThreads\s*=\s*(\d+)", conf, re.M)
    workers = int(m.group(1)) if m else None
    if workers != 1:
        raise FixtureError("login barrier needs CharacterDatabase.WorkerThreads = 1 (FIFO async queue)", workers=workers)
    t = probe.W.PER_CHARACTER_LAYOUT_CACHE
    rows = db.query(f"SELECT time, HEX(data) FROM character_account_data WHERE guid={int(guid)} AND type={t}", "characters")
    old_time = int(rows[0][0]) if rows else 0
    text = bytes.fromhex(rows[0][1]).decode("utf-8", "replace") if rows else ""
    stamp = max(int(time.time()), old_time + 1)
    t0 = time.time()
    ws.update_account_data(t, stamp, text)
    v, ok, dt = poll(lambda: db.scalar(f"SELECT time FROM character_account_data WHERE guid={int(guid)} AND type={t}",
                                       "characters"), lambda x: x == str(stamp), timeout=30, interval=0.05)
    if not ok:
        raise FixtureError("login barrier: character_account_data timestamp never appeared", expected=stamp, got=v)
    time.sleep(LOGIN_SETTLE_MARGIN_S)
    return {"barrier": f"character_account_data type {t} time {old_time} -> {stamp}", "db_seen_after_s": dt,
            "settle_s": round(time.time() - t0, 2)}


def logout(ws, guid: int, timeout=45) -> dict:
    """settle_login, then the probe's CMSG_LOGOUT_REQUEST; returns both."""
    settle = settle_login(ws, guid)
    return ws.logout(timeout=timeout) | {"settle": settle}


def _require_offline(guid: int):
    st, ok, _ = poll(lambda: char_state(guid), lambda s: s.get("online") == 0, timeout=30)
    if not ok:
        raise FixtureError("test character is online; offline driver needs it logged out", state=st)


# ----------------------------------------------------------------------------- state drivers

def set_level(n: int = LEVEL) -> dict:
    ensure()
    guid = char_guid()
    _require_offline(guid)
    before = char_state(guid)
    r = gm(f"character level {P.TEST_CHAR} {n}")
    st, ok, dt = poll(lambda: char_state(guid), lambda s: s.get("level") == n)
    return {"guid": guid, "before": before, "gm": r.evidence(), "after": st, "saved": ok, "wait_s": dt}


def tele_row(name: str = TELE_NAME) -> dict:
    rows = db.query(f"SELECT id, name, map, position_x, position_y, position_z FROM game_tele WHERE name='{_q(name)}'",
                    "world")
    if not rows:
        raise FixtureError(f"no game_tele row named {name}")
    i, nm, m, x, y, z = rows[0]
    return {"id": int(i), "name": nm, "map": int(m), "x": float(x), "y": float(y), "z": float(z)}


def distance(st: dict, tele: dict) -> float:
    return ((st["x"] - tele["x"]) ** 2 + (st["y"] - tele["y"]) ** 2 + (st["z"] - tele["z"]) ** 2) ** 0.5


def teleport(name: str = TELE_NAME) -> dict:
    ensure()
    guid = char_guid()
    _require_offline(guid)
    tele = tele_row(name)
    before = char_state(guid)
    r = gm(f"teleport name {P.TEST_CHAR} {tele['name']}")
    st, ok, dt = poll(lambda: char_state(guid), lambda s: s.get("map") == tele["map"] and distance(s, tele) <= 50)
    return {"guid": guid, "tele": tele, "before": before, "gm": r.evidence(), "after": st,
            "distance_yd": round(distance(st, tele), 2), "saved": ok, "wait_s": dt}


def describe_item(entry: int) -> dict:
    rows = db.query(f"SELECT entry, name FROM item_template WHERE entry={int(entry)}", "world")
    if not rows:
        raise FixtureError(f"item entry {entry} not in item_template")
    return {"entry": int(rows[0][0]), "name": rows[0][1]}


def owned_items(guid: int, entry: int) -> dict:
    inv = db.scalar(f"SELECT COUNT(*) FROM character_inventory ci JOIN item_instance ii ON ii.guid = ci.item "
                    f"WHERE ci.guid={guid} AND ii.itemEntry={int(entry)}", "characters")
    mail = db.scalar(f"SELECT COUNT(*) FROM mail_items mi JOIN item_instance ii ON ii.guid = mi.item_guid "
                     f"WHERE mi.receiver={guid} AND ii.itemEntry={int(entry)}", "characters")
    return {"inventory": int(inv), "mail": int(mail), "total": int(inv) + int(mail)}


def add_item(entry: int = ITEM_ENTRY) -> dict:
    ensure()
    guid = char_guid()
    item = describe_item(entry)
    before = owned_items(guid, entry)
    r = gm(f'send items {P.TEST_CHAR} "hs fixture" "hs fixture item" {int(entry)}')
    after, ok, dt = poll(lambda: owned_items(guid, entry), lambda o: o["total"] > before["total"])
    # Mailed items are owned through mail_items.receiver / mail.receiver; `.send items` to an offline player
    # creates the item with owner_guid 0 (Item::CreateItem(..., nullptr) in cs_send.cpp) until it is taken.
    rows = db.query(f"SELECT ii.guid, ii.itemEntry, mi.receiver, m.receiver, m.id FROM mail_items mi "
                    f"JOIN item_instance ii ON ii.guid = mi.item_guid JOIN mail m ON m.id = mi.mail_id "
                    f"WHERE mi.receiver={guid} AND ii.itemEntry={int(entry)}", "characters")
    return {"guid": guid, "item": item, "before": before, "gm": r.evidence(), "after": after, "saved": ok,
            "wait_s": dt, "mail_item_rows": rows}


def describe_quest(qid: int) -> dict:
    rows = db.query(f"SELECT ID, LogTitle, MinLevel, QuestLevel, AllowableRaces, "
                    f"RequiredNpcOrGo1, RequiredNpcOrGoCount1, RequiredItemId1, RequiredItemCount1 "
                    f"FROM quest_template WHERE ID={int(qid)}", "world")
    if not rows:
        raise FixtureError(f"quest {qid} not in quest_template")
    k = ["id", "title", "min_level", "quest_level", "allowable_races", "req_npc_or_go1", "req_npc_or_go_count1",
         "req_item1", "req_item_count1"]
    d = dict(zip(k, rows[0]))
    starts_from_item = db.scalar(f"SELECT COUNT(*) FROM item_template WHERE startquest={int(qid)}", "world")
    d["starts_from_item"] = int(starts_from_item)
    return d


def quest_status(guid: int, qid: int) -> list[list[str]]:
    return db.query(f"SELECT quest, status, mobcount1, itemcount1 FROM character_queststatus "
                    f"WHERE guid={guid} AND quest={int(qid)}", "characters")


def start_quest(qid: int = QUEST_ID) -> dict:
    """Log the character in through the probe, `.quest add` it over SOAP, then log out (server saves)."""
    ensure()
    guid = char_guid()
    _require_offline(guid)
    quest = describe_quest(qid)
    ev = {"guid": guid, "quest": quest, "before": quest_status(guid, qid)}
    lr, ws, wev = probe.open_world(P.TEST_USER, P.TEST_PASS)
    with ws:
        chars = ws.char_enum()
        c = next((c for c in chars if c["guid"] == guid), None)
        if not c:
            raise FixtureError("test character not in char enum", enum=chars)
        ev["login_verify_world"] = ws.player_login(guid)
        st, online, _ = poll(lambda: char_state(guid), lambda s: s.get("online") == 1, timeout=15)
        ev["online_seen"] = online
        if ev["before"]:
            # make the add observable: remove it, have the server save, and wait for the row to go
            ev["gm_remove"] = gm(f"quest remove {int(qid)} {P.TEST_CHAR}", ok_required=False).evidence()
            ev["gm_saveall"] = gm("saveall").evidence()
            _, ev["removed_saved"], _ = poll(lambda: quest_status(guid, qid), lambda rows: not rows, timeout=15)
        r = gm(f"quest add {int(qid)} {P.TEST_CHAR}")
        ev["gm_add"] = r.evidence()
        ev["logout"] = logout(ws, guid)
    ev["after"], ok, ev["wait_s"] = poll(lambda: quest_status(guid, qid), lambda rows: bool(rows) and rows[0][1] == "3")
    ev["saved_incomplete"] = ok
    return ev


def cli(action: str, value: str | None) -> dict:
    if action == "setup":
        return ensure()
    if action == "level":
        return set_level(int(value or LEVEL))
    if action == "tele":
        return teleport(value or TELE_NAME)
    if action == "item":
        return add_item(int(value or ITEM_ENTRY))
    if action == "quest":
        return start_quest(int(value or QUEST_ID))
    raise FixtureError(f"unknown action {action}")
