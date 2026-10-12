"""Companion-bot driver for the comp.* checks (m2-living-world, spec companion-bots).

A headless probe logs in a character on the harness's own companion account and types what a player would type.
Never the PLAYER account (env/player.json) and never the HSTEST fixture account:

  account    P.COMP_USER, created through the server (SOAP `account create`) and given security level 2
             (`account set gmlevel`): RBAC role 193 links RBAC_PERM_COMMAND_GM (371, so Player::CanBeGameMaster() and
             playerbots grants PLAYERBOT_SECURITY_ALLOW_ALL, PlayerbotSecurity.cpp), .go/.summon/.npc info/.gps, and
             instant logout (1).
  character  P.COMP_CHAR, human warrior, created with the probe's CMSG_CHAR_CREATE.
  companions `.playerbots bot addclass <class>` / `.playerbots bot remove <name>`, typed as chat
             (vendor/mod-playerbots/src/Bot/PlayerbotMgr.cpp HandlePlayerbotCommand: "addclass" picks an offline
             character of that class and the master's team from the addclass account pool and logs it in with the
             master's account as owner; OnBotLogin queues a GroupInviteOperation that puts it in the master's party and
             re-rolls it to the master's level when they differ by more than 3. "remove" -> LogoutPlayerBot, which
             queues BotLogoutGroupCleanupOperation).

Every world-data choice (quest, creature, teleport points) is made by SQL/DBC rules written here, never from memory
(CLAUDE.md §7)."""
import json
import math
import re
import time

from . import botlog, db, dbc, fixtures as F, paths as P, soap
from .probe import client as probe
from .probe import proto as W

GM_LEVEL = 2            # SEC_GAMEMASTER (src/server/shared/Common.h AccountTypes); see module doc
LEVEL = 8               # master level: >= the shared quest's MinLevel (picked by rule below, 7 at this pin)
START_TELE = "ElwynnForest"   # where each session starts; a game_tele row in the human start zone (validated)

# ChrClasses ids: SharedDefines.h enum Classes; addclass class words: PlayerbotMgr.cpp HandlePlayerbotCommand
CLASS_IDS = {"warrior": 1, "paladin": 2, "hunter": 3, "rogue": 4, "priest": 5, "dk": 6, "shaman": 7, "mage": 8,
             "warlock": 9, "druid": 11}
ADDCLASS_ACCOUNT_TYPE = 2   # playerbots_account_type.account_type of the addclass pool (RandomPlayerbotMgr IsAccountType)
FACTION_MASK_PLAYER = 1     # DBCEnums.h; FactionTemplateEntry::IsHostileToPlayers = hostileMask & FACTION_MASK_PLAYER
FT_HOSTILE_MASK = 5         # FactionTemplate.dbc field 5 = hostileMask (DBCStructure.h FactionTemplateEntry)
QUEST_FLAGS_SHARABLE = 0x8  # QuestDef.h; Player::CanShareQuest requires it
ISOLATION_YD = 30           # creature rule: no other hostile spawn this close (adds made comp.assists racy)
MOVE_WAYPOINT = 2           # creature.MovementType: WAYPOINT_MOTION_TYPE (MotionMaster.h MovementGeneratorType; 0 idle, 1 random)
UNIT_FLAG_IN_COMBAT = 0x80000   # UnitDefines.h; set while the unit is in combat with anything (CombatManager)


class CompanionError(Exception):
    def __init__(self, msg: str, **evidence):
        super().__init__(msg)
        self.evidence = evidence


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "''")


def gm(cmd: str, ok_required=True) -> soap.GMResult:
    r = soap.gm(cmd)
    if ok_required and not r.ok:
        raise CompanionError(f"GM command failed: {cmd}", gm=r.evidence())
    return r


def dist(a: dict, b: dict) -> float:
    return math.sqrt((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2 + (a["z"] - b["z"]) ** 2)


# ----------------------------------------------------------------------------- account + character

# test-pyramid: a shared-server test may run the companion helpers on its own fixture character (as_identity);
# the default is the companion harness account and character.
_IDENT = __import__("threading").local()


def _id() -> tuple:
    return getattr(_IDENT, "v", None) or (P.COMP_USER, P.COMP_PASS, P.COMP_CHAR)


class as_identity:
    """with as_identity(user, password, character): the companion helpers use that account and character."""
    def __init__(self, user: str, password: str, char: str):
        self.v = (user, password, char)

    def __enter__(self):
        self.prev = getattr(_IDENT, "v", None)
        _IDENT.v = self.v
        return self

    def __exit__(self, *exc):
        _IDENT.v = self.prev


def account_id() -> int | None:
    v = db.scalar(f"SELECT id FROM account WHERE username='{_id()[0]}'", "auth")
    return int(v) if v else None


def char_row() -> dict | None:
    rows = db.query(f"SELECT guid, account, level, online, map, zone FROM characters WHERE name='{_id()[2]}'",
                    "characters")
    if not rows:
        return None
    g, a, lv, on, m, z = rows[0]
    return {"guid": int(g), "account": int(a), "level": int(lv), "online": int(on), "map": int(m), "zone": int(z)}


def ensure_account() -> dict:
    ev = {}
    if account_id() is None:
        ev["create"] = gm(f"account create {_id()[0]} {_id()[1]}").evidence()
        _, ok, _ = F.poll(account_id, bool, timeout=15)
        if not ok:
            raise CompanionError("companion account not in auth.account after `account create`", **ev)
    acct = account_id()
    access = F.access_rows(acct)
    # a fixture identity (as_identity) keeps a higher level it was created with (the saves identities are level 3)
    keep_higher = getattr(_IDENT, "v", None) is not None and len(access) == 1 and access[0][1] == "-1" \
        and int(access[0][0]) > GM_LEVEL
    if access != [[str(GM_LEVEL), "-1"]] and not keep_higher:
        ev["gmlevel"] = gm(f"account set gmlevel {_id()[0]} {GM_LEVEL} -1").evidence()
        access, ok, _ = F.poll(lambda: F.access_rows(acct), lambda v: v == [[str(GM_LEVEL), "-1"]], timeout=15)
        if not ok:
            raise CompanionError("companion account security level not set", access=access, **ev)
    return ev | {"account": _id()[0], "id": acct, "account_access": access}


def ensure_character() -> dict:
    if char_row():
        return {"character": _id()[2], "created": False}
    lr, ws, _ = probe.open_world(_id()[0], _id()[1])
    with ws:
        code = ws.char_create(_id()[2], F.RACE_HUMAN, F.CLASS_WARRIOR)
        if code != W.CHAR_CREATE_SUCCESS:
            raise CompanionError("CMSG_CHAR_CREATE rejected", result=f"0x{code:02X} {W.RESPONSE_NAMES.get(code, '?')}")
    _, ok, _ = F.poll(char_row, bool, timeout=15)
    if not ok:
        raise CompanionError("companion character not in characters DB after CHAR_CREATE_SUCCESS")
    return {"character": _id()[2], "created": True}


def prepare_offline(level: int = LEVEL, tele: str = START_TELE) -> dict:
    """Level and place the (offline) master through the server: `.character level`, `.tele name`."""
    row = char_row()
    _, ok, _ = F.poll(lambda: char_row()["online"], lambda v: v == 0, timeout=30)
    if not ok:
        raise CompanionError("companion character still online; offline preparation needs it logged out", row=row)
    ev = {}
    if row["level"] != level:
        ev["level"] = gm(f"character level {_id()[2]} {level}").evidence()
        _, ok, _ = F.poll(lambda: char_row()["level"], lambda v: v == level, timeout=15)
        if not ok:
            raise CompanionError("companion level not saved", **ev)
    t = F.tele_row(tele)
    ev["tele"] = gm(f"tele name {_id()[2]} {t['name']}").evidence()
    return ev | {"tele_row": t}


# ----------------------------------------------------------------------------- data rules (SQL / DBC)

def bot_info(guid: int) -> dict:
    """characters row of a (bot) player GUID; player GUIDs are HighGuid::Player (0) | counter = characters.guid."""
    rows = db.query(f"SELECT c.guid, c.name, c.class, c.race, c.level, c.online, c.account, "
                    f"(SELECT account_type FROM {P.DB_NAMES['playerbots']}.playerbots_account_type t "
                    f"WHERE t.account_id=c.account) FROM characters c WHERE c.guid={int(guid)}", "characters")
    if not rows:
        return {}
    g, n, c, r, lv, on, a, t = rows[0]
    return {"guid": int(g), "name": n, "class": int(c), "race": int(r), "level": int(lv), "online": int(on),
            "account": int(a), "account_type": None if t in (None, "NULL") else int(t)}


def addclass_pool(cls_id: int) -> list[dict]:
    """The addclass-pool characters (playerbots_account_type.account_type = ADDCLASS_ACCOUNT_TYPE) of one class, with
    their online flag and saved party (characters.group_member)."""
    rows = db.query(f"SELECT c.guid, c.name, c.level, c.online, c.account, "
                    f"(SELECT gm.guid FROM group_member gm WHERE gm.memberGuid=c.guid) FROM characters c "
                    f"JOIN {P.DB_NAMES['playerbots']}.playerbots_account_type t ON t.account_id=c.account "
                    f"WHERE t.account_type={ADDCLASS_ACCOUNT_TYPE} AND c.class={int(cls_id)} ORDER BY c.guid",
                    "characters")
    return [{"guid": int(g), "name": n, "level": int(lv), "online": int(on), "account": int(a),
             "group": None if grp in (None, "NULL") else int(grp)} for g, n, lv, on, a, grp in rows]


def addclass_verdict(pool: list[dict], before: set, chat: list[dict], master_group: int | None) -> str:
    """Which step of the addclass path (PlayerbotMgr.cpp HandlePlayerbotCommand -> AddPlayerBot -> async login ->
    OnBotLogin -> GroupInviteOperation) a missing companion stopped at, from what the master and the DB can see."""
    refused = [c["msg"] for c in chat if c.get("type") == W.CHAT_MSG_SYSTEM and "Failure" in (c.get("msg") or "")]
    if refused:
        return f"refused by AddPlayerBot: {refused[0]}"
    online = [c for c in pool if c["online"] and c["guid"] not in before]
    greeted = {c.get("sender") for c in chat if c.get("sender")} & {c["guid"] for c in online}
    if not online:
        return "no pool character of the class came online (the bot login never completed)"
    seen_in_db = [c["name"] for c in online if master_group is not None and c["group"] == master_group]
    if seen_in_db:
        return (f"pool character(s) {seen_in_db} in the master's saved party (DB) but no SMSG_GROUP_LIST with it reached "
                "the probe in time (late invite or a probe-side miss)")
    stale = [c["name"] for c in online if c["group"] is not None]
    if stale:
        return (f"pool character(s) {stale} online but in another saved party: the invite was lost "
                "(GroupInviteOperation refuses a target already in a group)")
    who = [c["name"] for c in online]
    return (f"pool character(s) {who} online{' and greeted the master' if greeted else ''} but not in the party "
            "(OnBotLogin / GroupInviteOperation never added it)")


def human_start_zone() -> int:
    """Start zone of the master's race (human): the zone most classes of race 1 start in (world.playercreateinfo),
    as in the population checks' starting-zone rule."""
    rows = db.query(f"SELECT zone, COUNT(*) n FROM playercreateinfo WHERE race={F.RACE_HUMAN} GROUP BY zone "
                    f"ORDER BY n DESC LIMIT 1", "world")
    return int(rows[0][0])


QUEST_RULE = ("starter-zone kill quest: quest_template.QuestSortID is the human start zone or an AreaTable.dbc area "
              "inside it; RequiredNpcOrGo1 > 0 (a creature) with RequiredNpcOrGoCount1 > 0; Flags has "
              "QUEST_FLAGS_SHARABLE; quest_template_addon PrevQuestID = 0 and AllowableClasses = 0 (anyone can take "
              "it without a chain); AllowableRaces 0 or includes human; no item starts it; a creature_queststarter "
              "with a spawn; lowest ID")


def pick_quest() -> dict:
    zone = human_start_zone()
    areas = dbc.area_table()
    sorts = sorted(a for a in areas if dbc.top_zone(areas, a) == zone)
    rows = db.query(
        f"SELECT q.ID, q.LogTitle, q.MinLevel, q.QuestLevel, q.Flags, q.QuestSortID, q.RequiredNpcOrGo1, "
        f"q.RequiredNpcOrGoCount1 FROM quest_template q JOIN quest_template_addon a ON a.ID = q.ID "
        f"WHERE q.QuestSortID IN ({','.join(map(str, sorts))}) AND q.RequiredNpcOrGo1 > 0 "
        f"AND q.RequiredNpcOrGoCount1 > 0 AND (q.Flags & {QUEST_FLAGS_SHARABLE}) AND a.PrevQuestID = 0 "
        f"AND a.AllowableClasses = 0 AND (q.AllowableRaces = 0 OR (q.AllowableRaces & 1)) "
        f"AND NOT EXISTS (SELECT 1 FROM item_template i WHERE i.startquest = q.ID) "
        f"AND EXISTS (SELECT 1 FROM creature_queststarter s JOIN creature c ON c.id = s.id WHERE s.quest = q.ID) "
        f"ORDER BY q.ID LIMIT 1", "world")
    if not rows:
        raise CompanionError("no quest matches the rule", rule=QUEST_RULE, zone=zone)
    k = ["id", "title", "min_level", "quest_level", "flags", "sort_id", "kill_creature", "kill_count"]
    q = {kk: (int(v) if kk not in ("title",) else v) for kk, v in zip(k, rows[0])}
    return q | {"rule": QUEST_RULE, "start_zone": zone}


CREATURE_RULE = ("hostile creature near the master: creature_template.faction's FactionTemplate.dbc hostileMask has "
                 "FACTION_MASK_PLAYER; rank 0 (normal); npcflag 0; maxlevel in [master level - 3, master level - 1]; "
                 "no unit_flags 0x2|0x100|0x2000000 (non-attackable, immune to PC, not selectable) and no "
                 "flags_extra 0x2 (civilian); creature.MovementType not 2 (waypoint walkers never stand still, see READY_RULE); "
                 "no other hostile spawn within 30 yd (2D), so one creature fights the master; "
                 "the spawn on the master's map nearest the master's start point that is alive, "
                 "at full health and not in combat right now (`.npc info <spawn>`: health, Unit Flags without "
                 "UNIT_FLAG_IN_COMBAT), so an earlier kill or another unit's fight doesn't block the check")


_CANDIDATES: dict = {}
_CANDIDATES_LOCK = __import__("threading").Lock()


def creature_candidates(level: int = LEVEL, tele: str = START_TELE, n: int = 10) -> list[dict]:
    """CREATURE_RULE's candidates. The query takes ~14 s (a correlated isolation test over every spawn on the map, no
    spatial index) and reads only the world database's spawns, which no check changes, so it runs once per
    (level, tele) per process (the largest n asked for; ORDER BY d, guid makes a smaller n its prefix) (test-pyramid 1.6: two pooled sessions ran it at once and both passed their 25 s
    combat timeout). The rows are copied out, so a caller can't change the cache."""
    key = (level, tele)
    with _CANDIDATES_LOCK:
        have = _CANDIDATES.get(key)
        if have is None or (have[0] < n and len(have[1]) >= have[0]):     # a shorter list can serve a smaller n
            _CANDIDATES[key] = (n, _creature_candidates(level, tele, n))
        return [dict(c, spawn=dict(c["spawn"])) for c in _CANDIDATES[key][1][:n]]


# Across runs (test-pyramid design 14): the candidates are cached on disk keyed by a hash of the world database content
# they come from (CHECKSUM TABLE of creature, creature_template and game_tele, plus FactionTemplate.dbc), so the run-start
# fixture phase reads them instead of running the ~14 s query; any change to those tables changes the key.
WORLD_FACTS_KEYS = ((LEVEL, START_TELE, 20),)
WORLD_FACTS_DIR = P.OUT / "world-facts"


def world_hash() -> str:
    import hashlib
    rows = db.query("CHECKSUM TABLE creature, creature_template, game_tele", "world")
    h = hashlib.sha256(json.dumps(sorted([list(map(str, r)) for r in rows])).encode())
    h.update((dbc.DBC_DIR / "FactionTemplate.dbc").read_bytes())
    return h.hexdigest()


def world_facts_warm() -> dict:
    """Fill the in-process candidate cache for WORLD_FACTS_KEYS from the on-disk cache of this world database's
    content, or compute and store it. Returns {"from": "cache" | "computed", "s", "hash"}."""
    t0 = time.time()
    h = world_hash()
    f = WORLD_FACTS_DIR / f"{h}.json"
    try:
        data = json.loads(f.read_text())
        src = "cache"
    except (OSError, ValueError):
        data = {}
        src = "computed"
    out = {}
    for level, tele, n in WORLD_FACTS_KEYS:
        k = f"{level}|{tele}|{n}"
        rows = data.get(k)
        if rows is None:
            rows = _creature_candidates(level, tele, n)
        with _CANDIDATES_LOCK:
            _CANDIDATES[(level, tele)] = (n, rows)
        out[k] = rows
    if src == "computed":
        WORLD_FACTS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(out))
        tmp.replace(f)
        for old in WORLD_FACTS_DIR.glob("*.json"):           # keep only this world's facts
            if old != f:
                old.unlink(missing_ok=True)
    return {"from": src, "s": round(time.time() - t0, 2), "hash": h[:12]}


def _creature_candidates(level: int, tele: str, n: int) -> list[dict]:
    recs, _ = dbc.read_dbc("FactionTemplate.dbc")
    hostile = sorted(r[0] for r in recs if r[FT_HOSTILE_MASK] & FACTION_MASK_PLAYER)
    t = F.tele_row(tele)
    fac = ",".join(map(str, hostile))
    rows = db.query(
        f"SELECT c.guid, c.id, ct.name, ct.minlevel, ct.maxlevel, ct.faction, c.position_x, c.position_y, "
        f"c.position_z, SQRT(POW(c.position_x-({t['x']}),2)+POW(c.position_y-({t['y']}),2)+POW(c.position_z-({t['z']}),2)) d "
        f"FROM creature c JOIN creature_template ct ON ct.entry = c.id WHERE c.map = {t['map']} AND ct.rank = 0 "
        f"AND ct.npcflag = 0 AND ct.maxlevel BETWEEN {level - 3} AND {level - 1} AND c.MovementType <> {MOVE_WAYPOINT} "
        f"AND (ct.unit_flags & {0x2 | 0x100 | 0x2000000}) = 0 AND (ct.flags_extra & 2) = 0 "
        f"AND ct.faction IN ({fac}) AND NOT EXISTS (SELECT 1 FROM creature o JOIN creature_template ot ON ot.entry = o.id "
        f"WHERE o.map = c.map AND o.guid <> c.guid AND ot.faction IN ({fac}) "
        f"AND SQRT(POW(o.position_x-c.position_x,2)+POW(o.position_y-c.position_y,2)) < {ISOLATION_YD}) "
        f"ORDER BY d, c.guid LIMIT {int(n)}", "world")
    if not rows:
        raise CompanionError("no creature matches the rule", rule=CREATURE_RULE)
    return [{"spawn_id": int(g), "entry": int(e), "name": nm, "minlevel": int(mn), "maxlevel": int(mx),
             "faction": int(fac), "spawn": {"x": float(x), "y": float(y), "z": float(z)},
             "distance_from_start_yd": round(float(d), 1)} for g, e, nm, mn, mx, fac, x, y, z, d in rows]


STAGE_RULE = ("staging point for comp.assists (where the companion joins the master before the fight): a point "
              "STAGE_YD (35) yd from the creature's live position, out of its aggro reach, trying bearings in 45-degree "
              "steps starting with the one toward the master's start point; the first with no hostile-to-players spawn "
              "(FactionTemplate.dbc rule above), the picked creature included, within "
              "STAGE_CLEAR_YD (25) yd (2D); ground height by `.go xyz x y` (cs_go.cpp HandleGoXYZCommand: "
              "max(GetHeight, GetWaterLevel) when z is omitted)")
STAGE_YD = 35          # > the creature's aggro radius (Creature.cpp GetAggroRange: DetectionRange, 20 by default, minus the
                       # level difference: ~20-23 yd for these creatures vs. the level-8 master)
STAGE_CLEAR_YD = 25


def staging_point(creature: dict, toward: dict, map_id: int) -> dict:
    """STAGE_RULE: {"x", "y", "bearing_deg", "distance_to_creature_yd", "tried"}; raises CompanionError if none."""
    recs, _ = dbc.read_dbc("FactionTemplate.dbc")
    fac = ",".join(str(r[0]) for r in recs if r[FT_HOSTILE_MASK] & FACTION_MASK_PLAYER)
    cx, cy = creature["x"], creature["y"]
    r = STAGE_YD + STAGE_CLEAR_YD
    spawns = [(float(x), float(y)) for x, y in db.query(
        f"SELECT c.position_x, c.position_y FROM creature c JOIN creature_template ct ON ct.entry = c.id "
        f"WHERE c.map = {int(map_id)} AND ct.faction IN ({fac}) AND c.position_x BETWEEN {cx - r} AND {cx + r} "
        f"AND c.position_y BETWEEN {cy - r} AND {cy + r}", "world")]
    base = math.atan2(toward["y"] - cy, toward["x"] - cx)
    tried = []
    for k in range(8):
        a = base + k * math.pi / 4
        x, y = cx + STAGE_YD * math.cos(a), cy + STAGE_YD * math.sin(a)
        near = min((math.hypot(sx - x, sy - y) for sx, sy in spawns), default=None)
        tried.append({"bearing_deg": round(math.degrees(a) % 360), "nearest_hostile_spawn_yd":
                      round(near, 1) if near is not None else None})
        if near is None or near >= STAGE_CLEAR_YD:
            return {"x": round(x, 2), "y": round(y, 2), "bearing_deg": round(math.degrees(a) % 360),
                    "distance_to_creature_yd": STAGE_YD, "tried": tried, "rule": STAGE_RULE}
    raise CompanionError("no staging point matches the rule", rule=STAGE_RULE, tried=tried)


FOLLOW_RULE = ("two game_tele rows on the start point's map, 100-200 yd apart (2D), height difference < 10 yd, with no "
               "hostile-to-players spawn (FactionTemplate.dbc rule above) within 40 yd of the straight path between them "
               "(a companion that gets pulled into a fight on the way stops following: seen in a slow run on the "
               "ElwynnForest-CrystalLake pair, 7 hostile spawns near the path); the pair whose points are closest to "
               "the start point, the start point first if it is one of them")
FOLLOW_CLEAR_YD = 40


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    vx, vy = bx - ax, by - ay
    u = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / (vx * vx + vy * vy)))
    return math.hypot(ax + u * vx - px, ay + u * vy - py)


def pick_follow_points(tele: str = START_TELE) -> tuple[dict, dict]:
    t = F.tele_row(tele)
    recs, _ = dbc.read_dbc("FactionTemplate.dbc")
    fac = ",".join(str(r[0]) for r in recs if r[FT_HOSTILE_MASK] & FACTION_MASK_PLAYER)
    near = (f"SQRT(POW({{a}}.position_x-({t['x']}),2)+POW({{a}}.position_y-({t['y']}),2))")
    rows = db.query(
        f"SELECT a.name, b.name, a.position_x, a.position_y, b.position_x, b.position_y, "
        f"{near.format(a='a')} + {near.format(a='b')} s FROM game_tele a JOIN game_tele b ON a.id <> b.id "
        f"AND a.map = b.map WHERE a.map = {t['map']} "
        f"AND SQRT(POW(a.position_x-b.position_x,2)+POW(a.position_y-b.position_y,2)) BETWEEN 100 AND 200 "
        f"AND ABS(a.position_z-b.position_z) < 10 "
        f"ORDER BY s, (a.name <> '{_q(t['name'])}'), a.id", "world")
    c = FOLLOW_CLEAR_YD
    for an, bn, ax, ay, bx, by, _s in rows:
        ax, ay, bx, by = map(float, (ax, ay, bx, by))
        spawns = db.query(
            f"SELECT c.position_x, c.position_y FROM creature c JOIN creature_template ct ON ct.entry = c.id "
            f"WHERE c.map = {t['map']} AND ct.faction IN ({fac}) "
            f"AND c.position_x BETWEEN {min(ax, bx) - c} AND {max(ax, bx) + c} "
            f"AND c.position_y BETWEEN {min(ay, by) - c} AND {max(ay, by) + c}", "world")
        if not any(_seg_dist(float(x), float(y), ax, ay, bx, by) < c for x, y in spawns):
            return F.tele_row(an), F.tele_row(bn)
    raise CompanionError("no game_tele pair matches the rule", rule=FOLLOW_RULE)


READY_RULE = ("the picked creature right before the master engages it (comp.assists, F23): alive at full health, "
              "Unit Flags without UNIT_FLAG_IN_COMBAT (`.npc info`), no SMSG_ATTACKSTART / combat-log event with it "
              "as attacker or victim since the master reached the staging point, and standing still (two consecutive "
              "`.npc info` positions, >= READY_POLL_S apart, within READY_STILL_YD 2D); the master is then teleported onto that live "
              "position. Why: the creatures wander (creature.MovementType 1); teleporting onto a stale position left "
              "the master up to ~11 yd from the creature, which then sometimes didn't attack the master until the "
              "companion arrived and attacked it instead (design.md, F23)")
READY_S = 30            # a wander pause comes within a few seconds; a fight with another unit ends (death or reset)
READY_POLL_S = 0.3      # >= the server's visibility notify delay (DynamicVisibility.h, 300 ms below 500 sessions)
READY_STILL_YD = 0.05


def creature_fights(events: list[dict], guid: int) -> list[dict]:
    """Probe combat events (SMSG_ATTACKSTART, combat-log hits) with the creature as attacker or victim."""
    return [e for e in events if e["kind"] in ("attack_start", "melee_damage", "spell_damage")
            and guid in (e["attacker"], e["victim"])]


def creature_ready(info: dict, prev: dict | None, fights: list[dict]) -> bool:
    """READY_RULE for one `.npc info` sample (prev = the sample before it, fights = creature_fights since staging)."""
    if not (info.get("health") and info["health"] == info.get("max_health") and info.get("in_combat") is False
            and not fights and prev and info.get("pos") and prev.get("pos")):
        return False
    return math.hypot(info["pos"]["x"] - prev["pos"]["x"], info["pos"]["y"] - prev["pos"]["y"]) <= READY_STILL_YD


# ----------------------------------------------------------------------------- the session

NUM = r"(-?\d+(?:\.\d+)?)"


def parse_gps(lines: list[str]) -> dict:
    """LANG_MAP_POSITION (acore_string 101, cs_misc.cpp HandleGPSCommand): 'Map: m (..) Zone: z (..) Area: a ..' and
    'X: x Y: y Z: z Orientation: o'."""
    text = "\n".join(lines)
    m = re.search(r"Map: (\d+) .*?Zone: (\d+) .*?Area: (\d+)", text)
    p = re.search(rf"X: {NUM} Y: {NUM} Z: {NUM}", text)
    if not (m and p):
        return {"raw": lines}
    return {"map": int(m.group(1)), "zone": int(m.group(2)), "area": int(m.group(3)),
            "x": float(p.group(1)), "y": float(p.group(2)), "z": float(p.group(3))}


def parse_npc_info(lines: list[str]) -> dict:
    """`.npc info` (cs_npc.cpp HandleNpcInfoCommandShowCreature): LANG_NPCINFO_CHAR 'current GUID: GUID Full: 0x..'
    (ObjectGuid::ToString), LANG_NPCINFO_HEALTH 'Health (base): b. (max): m. (current): c.' and
    LANG_NPCINFO_POSITION 'Position: x y z.'."""
    text = "\n".join(lines)
    g = re.search(r"current GUID: GUID Full: 0x([0-9a-fA-F]+)", text)
    h = re.search(r"\(max\): (\d+)\. \(current\): (\d+)\.", text)
    p = re.search(rf"Position: {NUM} {NUM} {NUM}\.", text)      # LANG_NPCINFO_POSITION (acore_string 544)
    f = re.search(r"Unit Flags: (\d+)\.", text)                 # LANG_NPCINFO_FLAGS (acore_string 542), GetUnitFlags()
    return {"guid": int(g.group(1), 16) if g else None, "max_health": int(h.group(1)) if h else None,
            "health": int(h.group(2)) if h else None, "unit_flags": int(f.group(1)) if f else None,
            "in_combat": bool(int(f.group(1)) & UNIT_FLAG_IN_COMBAT) if f else None,
            "pos": {"x": float(p.group(1)), "y": float(p.group(2)), "z": float(p.group(3))} if p else None,
            "raw": lines[:6]}


class Session:
    """The master in the world: logged in through the probe with a running pump. Use as a context manager; on exit
    every companion still in the party is removed with the playerbots command and the master logs out."""

    def __init__(self, level: int = LEVEL, tele: str = START_TELE):
        self.level, self.tele = level, tele
        self.ws = None
        self.guid = None
        self.ev = {}
        self.companions = {}     # name -> bot_info
        self.log_marks = None    # botlog.marks() at session start

    def __enter__(self):
        self.log_marks = botlog.marks()     # server logs from here on are a failed addclass's evidence
        self.ev["account"] = ensure_account()
        self.ev["character"] = ensure_character()
        self.ev["prepare"] = prepare_offline(self.level, self.tele)
        lr, ws, wev = probe.open_world(_id()[0], _id()[1])
        self.ws = ws
        try:
            chars = ws.char_enum()
            c = next((c for c in chars if c["name"].lower() == _id()[2].lower()), None)
            if not c:
                raise CompanionError("companion character not in char enum", enum=chars)
            self.guid = c["guid"]
            self.ev["login"] = ws.player_login(self.guid)
            self.ev["settle"] = F.settle_login(ws, self.guid)
            ws.start_pump()
            self.ev["gm_off"] = ws.command(".gm off")
            self.ev["revive"] = self.revive_if_dead()
            self.ev["stale_group"] = self.leave_stale_group()
        except Exception:
            ws.close()
            raise
        return self

    def __exit__(self, *exc):
        if not self.ws:
            return
        try:
            for name in list(self.companions):
                if not bot_info(self.companions[name]["guid"]).get("online"):
                    continue           # already logged out (e.g. removed, still an offline party member)
                try:
                    self.remove(name)
                except Exception as e:  # cleanup is best-effort; logout below logs remaining bots out anyway
                    self.ev.setdefault("cleanup_errors", []).append(f"{name}: {e}")
            self.ev["left_group_at_exit"] = self.leave_stale_group()
            # logout is refused in combat (a creature may still be on us after comp.assists): stop it first
            self.ev["combatstop"] = self.ws.command(".combatstop", settle=0.3, timeout=3)
            self.ev["logout"] = self.ws.logout_pumped()
        finally:
            self.ev["ignored_opcodes"] = dict(self.ws.ignored)
            self.ws.close()

    # -- helpers
    def revive_if_dead(self) -> dict:
        """An earlier session may have left the master dead (a ghost is never attacked, and can't share quests).
        `.pinfo` (cs_misc.cpp HandlePInfoCommand, LANG_PINFO_CHR_ALIVE 'Alive ?: Yes/No'); if dead, `.revive` on self
        (HandleReviveCommand: ResurrectPlayer at 100% health for a non-player security level)."""
        pinfo = soap.gm(f"pinfo {_id()[2]}").output
        alive = re.search(r"Alive \?: (\w+)", pinfo)
        ev = {"alive_before": alive.group(1) if alive else None}
        if alive and alive.group(1) != "Yes":
            ev["revive"] = self.ws.command(".revive", settle=0.5, timeout=5)
            pinfo = soap.gm(f"pinfo {_id()[2]}").output
            alive = re.search(r"Alive \?: (\w+)", pinfo)
            ev["alive_after"] = alive.group(1) if alive else None
            if ev["alive_after"] != "Yes":
                raise CompanionError("master is dead and `.revive` didn't bring it back", **ev)
        return ev

    def group_rows(self) -> list[list[str]]:
        """characters.group_member rows (groupId, memberGuid, online-less) of the master's saved party."""
        return db.query(f"SELECT gm2.guid, gm2.memberGuid FROM group_member gm1 JOIN group_member gm2 "
                        f"ON gm2.guid = gm1.guid WHERE gm1.memberGuid = {int(self.guid)}", "characters")

    def leave_stale_group(self) -> dict | None:
        """Leave a party left over from an earlier session (a removed companion stays in it as an offline member, see
        comp.add_remove), so every check starts and ends without one. CMSG_GROUP_DISBAND = the client's 'Leave party'."""
        rows = self.group_rows()
        if not rows:
            return None
        self.ws.leave_group()
        _, ok, dt = F.poll(self.group_rows, lambda v: not v, timeout=15)
        return {"rows": rows, "left": ok, "wait_s": dt}

    def members(self) -> list[dict]:
        g = self.ws.group
        return list(g["members"]) if g else []

    def gps(self, guid: int | None = None) -> dict:
        """Live position from `.gps` (the selected unit, else the master: ChatHandler::getSelectedUnit)."""
        self.ws.set_selection(guid or self.guid)
        return parse_gps(self.ws.command(".gps", settle=0.4, timeout=5))

    def addclass(self, cls: str, timeout: float = 30.0) -> dict:
        """`.playerbots bot addclass <cls>`; wait until the server's SMSG_GROUP_LIST shows a new online member whose
        characters.class is that class. Returns the evidence (raises CompanionError on timeout)."""
        want = CLASS_IDS[cls]
        before = {m["guid"] for m in self.members()}
        since = cmd_mark = self.ws.mark()
        t0 = time.time()
        reply = self.ws.command(f".playerbots bot addclass {cls}", settle=0.5, timeout=5)
        found = None
        while time.time() - t0 < timeout and not found:
            e = self.ws.wait_event(lambda e: e["kind"] == "group_list", since, timeout=max(0.1, timeout - (time.time() - t0)))
            if not e:
                break
            since = e["i"] + 1
            for m in e["members"]:
                if m["guid"] not in before and m["online"]:
                    info = bot_info(m["guid"])
                    if info.get("class") == want:
                        found = (m, info, e)
        ev = {"command": f".playerbots bot addclass {cls}", "reply": reply, "elapsed_s": round(time.time() - t0, 2)}
        if not found:
            raise CompanionError(f"no online {cls} joined the party within {timeout:.0f} s", **ev,
                                 group=self.ws.group, members=self.members(),
                                 diagnosis=self.addclass_diagnosis(want, before, cmd_mark))
        m, info, e = found
        self.companions[m["name"]] = info
        ev["reset_ai"] = self.reset_ai(m["name"], m["guid"])
        return ev | {"member": m, "bot": info, "group_list": e}

    def addclass_diagnosis(self, want: int, before: set, since: int) -> dict:
        """Evidence for an addclass that never showed a new member (comp.quest_shared flake, 2026-10-12): what the
        server told the master since the command (system messages such as AddPlayerBot's "Failure: ..." and the bot's
        TellMaster "Hello!" whisper, PlayerbotMgr.cpp), the addclass-pool characters of that class right now, the
        master's saved party, and the server log lines since the session started. Best-effort: a failing query is
        recorded, never raised (the check already fails)."""
        out = {"chat_since_command": [{k: e.get(k) for k in ("type", "sender", "msg")}
                                      for e in self.ws.events_since(since) if e["kind"] == "chat"][-30:]}
        try:
            out["pool"] = addclass_pool(want)
        except Exception as e:      # noqa: BLE001
            out["pool"] = f"query failed: {e}"
        try:
            out["master_group_rows"] = self.group_rows()
        except Exception as e:      # noqa: BLE001
            out["master_group_rows"] = f"query failed: {e}"
        pool = out["pool"] if isinstance(out["pool"], list) else []
        rows = out["master_group_rows"] if isinstance(out["master_group_rows"], list) else []
        out["verdict"] = addclass_verdict(pool, before, out["chat_since_command"], int(rows[0][0]) if rows else None)
        if self.log_marks:
            names = [re.escape(r["name"]) for r in pool] + [re.escape(_id()[2])]
            keys = r"GroupInvite|OnBotLogin|addclass|Add class|queue|operation|error|fatal|" + "|".join(names)
            out["server_log"] = botlog.excerpt(self.log_marks, max_lines=80, pattern=keys)
            out["server_log_tail"] = botlog.excerpt(self.log_marks, max_lines=25)
        return out

    def reset_ai(self, name: str, guid: int, timeout: float = 10.0) -> dict:
        """Whisper `reset botAI` (ResetAiAction: PlayerbotRepository::Reset + ResetStrategies, reply 'AI was reset to
        defaults'). Strategies a master sets persist per bot in playerbots_db_store (PlayerbotRepository::Save) and are
        reloaded at login, so a pool bot can carry an earlier session's (or a counterexample's) changes; the checks
        test companions with their default strategies."""
        since = self.ws.mark()
        self.ws.chat("reset botAI", W.CHAT_MSG_WHISPER, to=name)
        e = self.ws.wait_event(lambda e: e["kind"] == "chat" and e["type"] == W.CHAT_MSG_WHISPER
                               and e["sender"] == guid and "defaults" in e["msg"],
                               since, timeout=timeout)
        if not e:
            raise CompanionError(f"{name} didn't confirm `reset botAI` within {timeout:.0f} s")
        return {"reply": e["msg"]}

    def remove(self, name: str, wait: bool = True, timeout: float = 30.0) -> dict:
        since = self.ws.mark()
        t0 = time.time()
        reply = self.ws.command(f".playerbots bot remove {name}", settle=0.5, timeout=5)
        ev = {"command": f".playerbots bot remove {name}", "reply": reply}
        if not wait:
            return ev
        gone = None
        while time.time() - t0 < timeout:
            if not any(m["name"] == name for m in self.members()):
                gone = round(time.time() - t0, 2)
                break
            time.sleep(0.2)
        self.companions.pop(name, None) if gone is not None else None
        group_events = [e for e in self.ws.events_since(since) if e["kind"] in ("group_list", "group_destroyed")]
        return ev | {"left_party_after_s": gone, "group_after": self.ws.group, "group_events": group_events[-3:]}

    def npc_info(self, spawn_id: int | None = None) -> dict:
        """Live state of a creature via `.npc info [spawnId]` (cs_npc.cpp HandleNpcInfoCommand): the master's selected
        creature if any, else the spawn (GetCreatureFromPlayerMapByDbGuid)."""
        cmd = ".npc info" + (f" {int(spawn_id)}" if spawn_id else "")
        info = parse_npc_info(self.ws.command(cmd, settle=0.4, timeout=5))
        m = re.search(r"DB GUID: (\d+)", "\n".join(info["raw"]))
        return info | {"spawn_id": int(m.group(1)) if m else None}

    def pick_live_creature(self, exclude: set | None = None) -> dict:
        tried = []
        for c in creature_candidates(self.level, self.tele, n=20):
            if c["spawn_id"] in (exclude or ()):
                continue
            info = self.npc_info(c["spawn_id"])
            tried.append({"spawn_id": c["spawn_id"], "health": info["health"], "max": info["max_health"]})
            tried[-1]["in_combat"] = info["in_combat"]
            if info["guid"] and info["health"] and info["health"] == info["max_health"] and info["in_combat"] is False:
                return c | {"guid": info["guid"], "npc_info": info, "rule": CREATURE_RULE, "tried": tried}
        raise CompanionError("no live, unhurt creature out of combat among the rule's nearest candidates", rule=CREATURE_RULE,
                             tried=tried)

    def wait_creature_ready(self, spawn_id: int, guid: int, since: int, timeout: float = READY_S) -> dict:
        """READY_RULE: poll `.npc info <spawn>` every READY_POLL_S until creature_ready() holds for the latest sample;
        returns {"ok", "snapshot" (the ready sample), "samples", "elapsed_s", "dead"}. `since` is the probe event mark
        from which combat events involving the creature count as "another unit's fight"."""
        t0, samples, prev = time.time(), [], None
        while True:
            info = self.npc_info(spawn_id)
            fights = creature_fights(self.ws.events_since(since), guid)
            moved = (round(math.hypot(info["pos"]["x"] - prev["pos"]["x"], info["pos"]["y"] - prev["pos"]["y"]), 2)
                     if prev and info.get("pos") and prev.get("pos") else None)
            samples.append({"t": round(time.time() - t0, 2), "health": info["health"], "max": info["max_health"],
                            "unit_flags": info["unit_flags"], "in_combat": info["in_combat"], "moved_yd": moved,
                            "pos": info["pos"], "combat_events": len(fights)})
            if info["health"] == 0 or info["guid"] is None:
                return {"ok": False, "dead": True, "elapsed_s": round(time.time() - t0, 1), "samples": samples[-10:],
                        "combat_events": fights[:10]}
            if creature_ready(info, prev, fights):
                return {"ok": True, "dead": False, "elapsed_s": round(time.time() - t0, 1), "snapshot": info,
                        "samples": samples[-10:], "combat_events": fights[:10]}
            if time.time() - t0 > timeout:
                return {"ok": False, "dead": False, "elapsed_s": round(time.time() - t0, 1), "samples": samples[-10:],
                        "combat_events": fights[:10]}
            prev = info
            time.sleep(READY_POLL_S)

    def summon(self, name: str) -> list[str]:
        return self.ws.command(f".summon {name}", settle=0.5, timeout=5)

    def wait_near(self, bot_guid: int, yards: float, timeout: float, interval: float = 1.0) -> dict:
        """Poll live positions of master and bot until within `yards` (3D) or timeout."""
        t0, samples = time.time(), []
        while True:
            me, bot = self.gps(), self.gps(bot_guid)
            d = round(dist(me, bot), 2) if "x" in me and "x" in bot else None
            samples.append({"t": round(time.time() - t0, 1), "master": me, "bot": bot, "distance_yd": d})
            if d is not None and d <= yards and me.get("map") == bot.get("map"):
                return {"ok": True, "elapsed_s": round(time.time() - t0, 1), "samples": samples}
            if time.time() - t0 > timeout:
                return {"ok": False, "elapsed_s": round(time.time() - t0, 1), "samples": samples}
            time.sleep(interval)
