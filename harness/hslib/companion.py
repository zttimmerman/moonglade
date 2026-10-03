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
import math
import re
import time

from . import db, dbc, fixtures as F, paths as P, soap
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

def account_id() -> int | None:
    v = db.scalar(f"SELECT id FROM account WHERE username='{P.COMP_USER}'", "auth")
    return int(v) if v else None


def char_row() -> dict | None:
    rows = db.query(f"SELECT guid, account, level, online, map, zone FROM characters WHERE name='{P.COMP_CHAR}'",
                    "characters")
    if not rows:
        return None
    g, a, lv, on, m, z = rows[0]
    return {"guid": int(g), "account": int(a), "level": int(lv), "online": int(on), "map": int(m), "zone": int(z)}


def ensure_account() -> dict:
    ev = {}
    if account_id() is None:
        ev["create"] = gm(f"account create {P.COMP_USER} {P.COMP_PASS}").evidence()
        _, ok, _ = F.poll(account_id, bool, timeout=15)
        if not ok:
            raise CompanionError("companion account not in auth.account after `account create`", **ev)
    acct = account_id()
    access = F.access_rows(acct)
    if access != [[str(GM_LEVEL), "-1"]]:
        ev["gmlevel"] = gm(f"account set gmlevel {P.COMP_USER} {GM_LEVEL} -1").evidence()
        access, ok, _ = F.poll(lambda: F.access_rows(acct), lambda v: v == [[str(GM_LEVEL), "-1"]], timeout=15)
        if not ok:
            raise CompanionError("companion account security level not set", access=access, **ev)
    return ev | {"account": P.COMP_USER, "id": acct, "account_access": access}


def ensure_character() -> dict:
    if char_row():
        return {"character": P.COMP_CHAR, "created": False}
    lr, ws, _ = probe.open_world(P.COMP_USER, P.COMP_PASS)
    with ws:
        code = ws.char_create(P.COMP_CHAR, F.RACE_HUMAN, F.CLASS_WARRIOR)
        if code != W.CHAR_CREATE_SUCCESS:
            raise CompanionError("CMSG_CHAR_CREATE rejected", result=f"0x{code:02X} {W.RESPONSE_NAMES.get(code, '?')}")
    _, ok, _ = F.poll(char_row, bool, timeout=15)
    if not ok:
        raise CompanionError("companion character not in characters DB after CHAR_CREATE_SUCCESS")
    return {"character": P.COMP_CHAR, "created": True}


def prepare_offline(level: int = LEVEL, tele: str = START_TELE) -> dict:
    """Level and place the (offline) master through the server: `.character level`, `.tele name`."""
    row = char_row()
    _, ok, _ = F.poll(lambda: char_row()["online"], lambda v: v == 0, timeout=30)
    if not ok:
        raise CompanionError("companion character still online; offline preparation needs it logged out", row=row)
    ev = {}
    if row["level"] != level:
        ev["level"] = gm(f"character level {P.COMP_CHAR} {level}").evidence()
        _, ok, _ = F.poll(lambda: char_row()["level"], lambda v: v == level, timeout=15)
        if not ok:
            raise CompanionError("companion level not saved", **ev)
    t = F.tele_row(tele)
    ev["tele"] = gm(f"tele name {P.COMP_CHAR} {t['name']}").evidence()
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
                 "flags_extra 0x2 (civilian); no other hostile spawn within 30 yd (2D), so one creature fights the master; "
                 "the spawn on the master's map nearest the master's start point that is alive "
                 "and at full health right now (`.npc info <spawn>`), so an earlier kill doesn't block the check")


def creature_candidates(level: int = LEVEL, tele: str = START_TELE, n: int = 10) -> list[dict]:
    recs, _ = dbc.read_dbc("FactionTemplate.dbc")
    hostile = sorted(r[0] for r in recs if r[FT_HOSTILE_MASK] & FACTION_MASK_PLAYER)
    t = F.tele_row(tele)
    fac = ",".join(map(str, hostile))
    rows = db.query(
        f"SELECT c.guid, c.id, ct.name, ct.minlevel, ct.maxlevel, ct.faction, c.position_x, c.position_y, "
        f"c.position_z, SQRT(POW(c.position_x-({t['x']}),2)+POW(c.position_y-({t['y']}),2)+POW(c.position_z-({t['z']}),2)) d "
        f"FROM creature c JOIN creature_template ct ON ct.entry = c.id WHERE c.map = {t['map']} AND ct.rank = 0 "
        f"AND ct.npcflag = 0 AND ct.maxlevel BETWEEN {level - 3} AND {level - 1} "
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
    return {"guid": int(g.group(1), 16) if g else None, "max_health": int(h.group(1)) if h else None,
            "health": int(h.group(2)) if h else None,
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

    def __enter__(self):
        self.ev["account"] = ensure_account()
        self.ev["character"] = ensure_character()
        self.ev["prepare"] = prepare_offline(self.level, self.tele)
        lr, ws, wev = probe.open_world(P.COMP_USER, P.COMP_PASS)
        self.ws = ws
        try:
            chars = ws.char_enum()
            c = next((c for c in chars if c["name"].lower() == P.COMP_CHAR.lower()), None)
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
        pinfo = soap.gm(f"pinfo {P.COMP_CHAR}").output
        alive = re.search(r"Alive \?: (\w+)", pinfo)
        ev = {"alive_before": alive.group(1) if alive else None}
        if alive and alive.group(1) != "Yes":
            ev["revive"] = self.ws.command(".revive", settle=0.5, timeout=5)
            pinfo = soap.gm(f"pinfo {P.COMP_CHAR}").output
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
        since = self.ws.mark()
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
                                 group=self.ws.group, members=self.members())
        m, info, e = found
        self.companions[m["name"]] = info
        ev["reset_ai"] = self.reset_ai(m["name"], m["guid"])
        return ev | {"member": m, "bot": info, "group_list": e}

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

    def pick_live_creature(self) -> dict:
        tried = []
        for c in creature_candidates(self.level, self.tele):
            info = self.npc_info(c["spawn_id"])
            tried.append({"spawn_id": c["spawn_id"], "health": info["health"], "max": info["max_health"]})
            if info["guid"] and info["health"] and info["health"] == info["max_health"]:
                return c | {"guid": info["guid"], "npc_info": info, "rule": CREATURE_RULE, "tried": tried}
        raise CompanionError("no live, unhurt creature among the rule's nearest candidates", rule=CREATURE_RULE,
                             tried=tried)

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
