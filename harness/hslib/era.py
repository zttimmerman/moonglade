"""era-profile (openspec/changes/m3-vanilla-mode) harness helpers.

Expected values are read from the source data at check time, never typed in (CLAUDE.md §7):
  * VMaNGOS values: the pinned VMaNGOS SQLite snapshot, through tools/era-diff (final 1.12 row per entry);
  * stock AzerothCore values: the pinned core's own base SQL (data/sql/base/db_world/*.sql) for the XP table, and
    QuestXP.dbc (+ questxp_dbc overrides) with AC's Quest::XPValue rounding for quest XP (era_diff helpers).
Observed values come from the running server through the protocol probe: our character's own update fields
(PLAYER_NEXT_LEVEL_XP, as the client's XP bar reads it) and SMSG_LOG_XPGAIN (what the client prints on a reward).
"""
import importlib.util
import math
import re
import struct
import sys
import time

from . import config, db, fixtures as F, paths as P, soap
from .probe import client as probe, proto as W

ERA_DIFF = P.ROOT / "tools/era-diff/era_diff.py"
MODULE = P.MODULES / "mod-era-profile"
SQL_DIR = MODULE / "data/sql/db-world"
CONF_DIST = MODULE / "conf" / (config.ERA_CONF + ".dist")
AC_XP_SQL = P.CORE_SRC / "data/sql/base/db_world/player_xp_for_level.sql"
LOG_TAG = "[mod-era-profile]"
ACTIVE_RE = re.compile(r"\[mod-era-profile\] Vanilla mode ACTIVE \(EraProfile\.Enable = 1\)")
OFF_RE = re.compile(r"\[mod-era-profile\] Vanilla mode OFF \(EraProfile\.Enable = 0\)")
FAILED_RE = re.compile(r"\[mod-era-profile\] (Vanilla mode FAILED|.*query failed|.*apply did not verify)")
TABLE_LINE_RE = re.compile(r"\[mod-era-profile\] (\w+): mode (\w+), (\d+) side-table rows, (\d+) captured into \S+ "
                           r"\((\d+) new\), (\d+) rows at vanilla values, (\d+) rows differing from the mode")
UPDATE_S = 15.0          # a GM change shows up in our update fields within one or two map updates; generous bound
XP_GAIN_S = 15.0


class EraError(Exception):
    def __init__(self, msg: str, **evidence):
        super().__init__(msg)
        self.evidence = evidence


_ED = None


def era_diff():
    """tools/era-diff/era_diff.py as a module (its directory name has a hyphen, so no plain import)."""
    global _ED
    if _ED is None:
        spec = importlib.util.spec_from_file_location("era_diff", ERA_DIFF)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["era_diff"] = mod
        spec.loader.exec_module(mod)
        _ED = mod
    return _ED


# ----------------------------------------------------------------------------------------- source values

def vm_xp_for_level() -> dict[int, int]:
    """VMaNGOS player_xp_for_level (lvl -> xp_for_next_level) from the pinned snapshot."""
    vm = era_diff().VM()
    return {r["lvl"]: r["xp_for_next_level"] for r in vm.q("SELECT lvl, xp_for_next_level FROM player_xp_for_level")}


def ac_stock_xp_for_level() -> dict[int, int]:
    """Stock AzerothCore player_xp_for_level from the pinned core's base SQL (no core DB update touches the table)."""
    text = AC_XP_SQL.read_text()
    m = re.search(r"INSERT INTO `player_xp_for_level` VALUES\s*(.*?);", text, re.S)
    if not m:
        raise EraError(f"no INSERT for player_xp_for_level in {AC_XP_SQL}")
    return {int(a): int(b) for a, b in re.findall(r"\((\d+),(\d+)\)", m.group(1))}


def rendered_option(conf, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}\s*=\s*(.*?)\s*$", conf.read_text(), re.M) if conf.exists() else None
    return m.group(1).strip('"') if m else None


def quest_rate() -> float:
    """Rate.XP.Quest of the running config (Player::GetQuestRate; quests here are not Dungeon Finder quests)."""
    v = rendered_option(P.SERVER_ETC / "worldserver.conf", "Rate.XP.Quest")
    if v is None:
        raise EraError("Rate.XP.Quest not found in the rendered worldserver.conf")
    return float(v)


def quest_sources() -> dict:
    """{quest id: {...}} for every quest in both DBs: VMaNGOS RewXP/QuestLevel and AC QuestLevel/difficulty, plus
    the era-diff verdict (status/reason) for the quest-XP category."""
    ed = era_diff()
    c = ed.Ctx()
    rows = {r["entry"]: r for r in ed.cat_quest_xp(c)}
    vm, ac = c.vm_quests(), c.ac_quests()
    qxp, _ = c.quest_xp_table()
    return {"ctx": c, "rows": rows, "vm": vm, "ac": ac, "questxp": qxp, "ed": ed}


def stock_quest_xp(src: dict, qid: int) -> int:
    """AC full XP at quest level: Quest::XPValue with diffFactor 10 = step-rounded QuestXP[level][difficulty]
    (era_diff.ac_round_quest_xp, copied from AC QuestDef.cpp), times the quest rate (PlayerQuest.cpp:1481)."""
    a = src["ac"][qid]
    return int(src["ed"].ac_round_quest_xp(src["questxp"][a["QuestLevel"]][a["RewardXPDifficulty"]]) * quest_rate())


def vanilla_quest_xp(src: dict, qid: int) -> int:
    """VMaNGOS XP for a character at the quest's level (d <= 5 -> RewXP exactly, memo "RewXP semantics"), times the
    quest rate with AC's truncation (the module keeps AC's rate semantics)."""
    return int(src["vm"][qid]["RewXP"] * quest_rate())


def side_table_quests() -> set[int]:
    return {int(r[0]) for r in db.query("SELECT QuestId FROM era_quest_xp", "world")}


def pick_review_quest(src: dict) -> dict:
    """The needs-review quest for era.quest_xp: the lowest quest id that era-diff marks needs-review because its
    QuestLevel differs between the cores, whose VMaNGOS XP differs from AC's by more than 5% (so a vanilla value
    applied by mistake would show), that the human warrior test character can take with `.quest add` (races/classes
    allow it, not started from an item), and that is absent from the module's era_quest_xp table."""
    in_side = side_table_quests()
    item_started = {int(r[0]) for r in db.query("SELECT DISTINCT startquest FROM item_template WHERE startquest > 0",
                                                "world")}
    for qid in sorted(src["rows"]):
        r = src["rows"][qid]
        if r["status"] != "needs-review" or not r["reason"].startswith("QuestLevel differs"):
            continue
        a = src["ac"][qid]
        vxp, acxp = r["vmangos"], r["ac"]
        if not acxp or not vxp or 100 * abs(vxp - acxp) <= 5 * acxp:
            continue
        races, classes = a["AllowableRaces"], a["AllowableClasses"]
        if (races and not races & (1 << (F.RACE_HUMAN - 1))) or (classes and not classes & (1 << (F.CLASS_WARRIOR - 1))):
            continue
        if qid in item_started or qid in in_side or a["QuestLevel"] <= 0:
            continue
        return {"quest": qid, "title": a["LogTitle"], "reason": r["reason"], "ac_level": a["QuestLevel"],
                "vm_level": src["vm"][qid]["QuestLevel"], "vm_rewxp": vxp, "ac_full_xp": acxp,
                "rule": pick_review_quest.__doc__.split("\n")[0]}
    raise EraError("no needs-review quest matches the selection rule")


# ----------------------------------------------------------------------------------------- server log

def worldserver_log() -> str:
    from . import stack
    return stack.LOG["worldserver"].read_text(errors="replace") if stack.LOG["worldserver"].exists() else ""


def module_log_lines(text: str | None = None) -> list[str]:
    text = worldserver_log() if text is None else text
    return [l for l in text.splitlines() if LOG_TAG in l]


def table_lines(lines: list[str]) -> dict[str, dict]:
    out = {}
    for l in lines:
        m = TABLE_LINE_RE.search(l)
        if m:
            out[m.group(1)] = {"mode": m.group(2), "side_rows": int(m.group(3)), "captured": int(m.group(4)),
                               "new": int(m.group(5)), "at_vanilla": int(m.group(6)), "differing": int(m.group(7))}
    return out


# ----------------------------------------------------------------------------------------- in-world driver

def gm(cmd: str, ok_required=True) -> soap.GMResult:
    r = soap.gm(cmd)
    if ok_required and not r.ok:
        raise EraError(f"GM command failed: {cmd}", gm=r.evidence())
    return r


class Session:
    """A character in the world through the probe (pumped), driven by SOAP GM commands by name. Default: the HSTEST
    fixture character (security 0). `gm=True`: the harness's companion account HSCOMP (security 2, hslib/companion.py)
    with GM mode on, for checks that must teleport and inspect creatures (`.go xyz`, `.npc info`); never the PLAYER
    account."""

    def __init__(self, gm: bool = False):
        self.gm = gm
        self.char = P.COMP_CHAR if gm else P.TEST_CHAR
        self.ws = None
        self.guid = None
        self.level = None
        self.ev = {}

    def __enter__(self):
        if self.gm:
            from . import companion as C
            self.ev["fixture"] = {"account": C.ensure_account(), "character": C.ensure_character()}
            self.guid = C.char_row()["guid"]
            _, ok, _ = F.poll(lambda: C.char_row()["online"], lambda v: v == 0, timeout=30)
            if not ok:
                raise EraError("companion character is online; the GM session needs it logged out")
            user, pw = P.COMP_USER, P.COMP_PASS
        else:
            self.ev["fixture"] = F.ensure()
            self.guid = F.char_guid()
            F._require_offline(self.guid)
            user, pw = P.TEST_USER, P.TEST_PASS
        lr, ws, wev = probe.open_world(user, pw)
        self.ws = ws
        try:
            chars = ws.char_enum()
            c = next((c for c in chars if c["guid"] == self.guid), None)
            if not c:
                raise EraError("test character not in char enum", enum=chars)
            self.level = c["level"]
            self.ev["login"] = ws.player_login(self.guid)
            self.ev["settle"] = F.settle_login(ws, self.guid)
            ws.start_pump()
            # GiveXP does nothing for a dead character (Player.cpp:2409)
            self.ev["revive"] = gm(f"revive {self.char}", ok_required=False).evidence()
            if self.gm:
                # GM mode (cs_gm.cpp HandleGMOnCommand): creatures don't attack a game master while we inspect them
                self.ev["gm_on"] = ws.command(".gm on", settle=0.4, timeout=5)
        except Exception:
            ws.close()
            raise
        return self

    def __exit__(self, *exc):
        if not self.ws:
            return
        try:
            if self.gm and not self.ws.pump_error:
                self.ev["gm_off"] = self.ws.command(".gm off", settle=0.4, timeout=5)
            self.ev["logout"] = self.ws.logout_pumped()
        finally:
            self.ev["ignored_opcodes"] = dict(self.ws.ignored)
            self.ws.close()

    def _wait_fields(self, since: int, pred, timeout: float):
        e = self.ws.wait_event(lambda e: e["kind"] == "self_values" and pred(e["fields"]), since, timeout)
        return e["fields"] if e else None

    def set_level(self, level: int) -> dict:
        """`.character level <name> <level>` (online: Player::GiveLevel sets PLAYER_NEXT_LEVEL_XP from the server's
        XP table, cs_character.cpp HandleCharacterLevel) -> our next update with that level. Returns the level and
        the XP needed for the next level as the server sent them."""
        out = {}
        if self.level == level:
            # same level: no field changes, no update. Step away first.
            out["step_away"] = self._set(level + 1 if level < 80 else level - 1)
        out |= self._set(level)
        return out

    def _set(self, level: int) -> dict:
        since = self.ws.mark()
        t0 = time.time()
        r = gm(f"character level {self.char} {level}")
        f = self._wait_fields(since, lambda f: f.get(W.UNIT_FIELD_LEVEL) == level and W.PLAYER_NEXT_LEVEL_XP in f,
                              UPDATE_S)
        if f is None:
            raise EraError(f"no update with level {level} and PLAYER_NEXT_LEVEL_XP within {UPDATE_S} s",
                           gm=r.evidence(), self_updates=[e["fields"] for e in self.ws.events_since(since, "self_values")][-5:],
                           pump_error=self.ws.pump_error, ignored=dict(self.ws.ignored))
        self.level = level
        return {"level": level, "next_level_xp": f[W.PLAYER_NEXT_LEVEL_XP], "xp": f.get(W.PLAYER_XP),
                "gm": r.output.strip()[-200:], "seen_after_s": round(time.time() - t0, 2)}

    def reward_quest(self, qid: int, level: int) -> dict:
        """At `level`: remove any earlier state of the quest (`.quest remove` also clears "rewarded"), add, complete
        and reward it with GM commands (cs_quest.cpp; reward runs Player::RewardQuest), and return the XP the server
        logged (SMSG_LOG_XPGAIN, non-kill) and our PLAYER_XP afterwards (XP was 0 after the level set)."""
        ev = {"quest": qid, "level": self.set_level(level)}
        ev["remove_before"] = gm(f"quest remove {qid} {self.char}", ok_required=False).output.strip()[-200:]
        ev["add"] = gm(f"quest add {qid} {self.char}").output.strip()[-200:]
        ev["complete"] = gm(f"quest complete {qid} {self.char}").output.strip()[-200:]
        since = self.ws.mark()
        ev["reward"] = gm(f"quest reward {qid} {self.char}").output.strip()[-200:]
        g = self.ws.wait_event(lambda e: e["kind"] == "xp_gain", since, XP_GAIN_S)
        ev["xp_gain_event"] = g
        if g is None:
            raise EraError(f"no SMSG_LOG_XPGAIN within {XP_GAIN_S} s of `.quest reward {qid}`", **ev)
        f = self._wait_fields(since, lambda f: W.PLAYER_XP in f, UPDATE_S)
        ev["player_xp_after"] = f.get(W.PLAYER_XP) if f else None
        ev["xp_gained"] = g["xp"]
        ev["remove_after"] = gm(f"quest remove {qid} {self.char}", ok_required=False).output.strip()[-200:]
        return ev


# ----------------------------------------------------------------------------------------- GM inspection (gm=True)

NPC_HEALTH_RE = re.compile(r"Health \(base\): (\d+)\. \(max\): (\d+)\. \(current\): (\d+)\.")   # acore_string 541
NPC_LEVEL_RE = re.compile(r"^Level: (\d+)\.$", re.M)                                            # acore_string 540
NPC_CHAR_RE = re.compile(r"DB GUID: (\d+), current GUID: GUID Full: 0x([0-9a-fA-F]+).*?Current Entry: (\d+)", re.S)
GO_S = 20.0              # teleport ack (MSG_MOVE_TELEPORT_ACK / SMSG_NEW_WORLD) after `.go xyz`
ARRIVED_YD = 3.0         # `.gps` within this of the `.go xyz` target (2D; the server may settle z) = arrived
GRID_S = 20.0            # after a teleport the target grid loads on the next map updates; `.npc info` retried


def parse_npc_info(lines: list[str]) -> dict:
    """`.npc info [spawnId]` (cs_npc.cpp HandleNpcInfoCommandShowCreature): LANG_NPCINFO_CHAR (539: DB GUID, current
    GUID via ObjectGuid::ToString 'GUID Full: 0x..', Current Entry), LANG_NPCINFO_LEVEL (540 'Level: {}.') and
    LANG_NPCINFO_HEALTH (541 'Health (base): {}. (max): {}. (current): {}.'). The base value is CreateHealth, set
    with max health in Creature::SelectLevel."""
    text = "\n".join(l or "" for l in lines)
    c, h, lv = NPC_CHAR_RE.search(text), NPC_HEALTH_RE.search(text), NPC_LEVEL_RE.search(text)
    return {"spawn_id": int(c.group(1)) if c else None, "guid": int(c.group(2), 16) if c else None,
            "entry": int(c.group(3)) if c else None, "level": int(lv.group(1)) if lv else None,
            "base_health": int(h.group(1)) if h else None, "max_health": int(h.group(2)) if h else None,
            "health": int(h.group(3)) if h else None, "raw": [l for l in lines if l][:8]}


def _go(self, x: float, y: float, z: float, mapid: int) -> dict:
    """`.go xyz x y z map` typed by the GM character (cs_go.cpp); waits for the teleport the probe acknowledges."""
    since = self.ws.mark()
    reply = self.ws.command(f".go xyz {x:.3f} {y:.3f} {z:.3f} {int(mapid)}", settle=0.5, timeout=5)
    e = self.ws.wait_event(lambda e: e["kind"] in ("teleport_near", "new_world"), since, GO_S)
    if e is None:
        raise EraError(f"no teleport within {GO_S} s of `.go xyz` to map {mapid}", reply=reply)
    # The server moves the player only when it handles our teleport ack, which can come after later packets (a
    # trainer window asked for too early fails Player::GetNPCIfCanInteractWith's distance check). Wait until `.gps`
    # (cs_misc.cpp HandleGPSCommand: the server's own position) shows the destination.
    from .companion import parse_gps
    t0, gps = time.time(), {}
    while time.time() - t0 < GO_S:
        gps = parse_gps(self.ws.command(".gps", settle=0.3, timeout=5))
        if gps.get("map") == mapid and math.hypot(gps["x"] - x, gps["y"] - y) <= ARRIVED_YD:
            break
        time.sleep(0.5)
    else:
        raise EraError(f"server position never reached the `.go xyz` destination within {GO_S} s", reply=reply,
                       event=e, gps=gps)
    return {"to": {"x": x, "y": y, "z": z, "map": mapid}, "reply": reply, "event": e, "gps": gps,
            "arrived_s": round(time.time() - t0, 2)}


def _npc_info(self, spawn_id: int, timeout: float = GRID_S) -> dict:
    """`.npc info <spawnId>` until the creature answers (the grid may still be loading after a teleport)."""
    t0, tries = time.time(), []
    while True:
        info = parse_npc_info(self.ws.command(f".npc info {int(spawn_id)}", settle=0.4, timeout=5))
        tries.append(info["raw"][:2])
        if info["guid"] and info["max_health"] is not None and info["spawn_id"] == spawn_id:
            return info | {"after_s": round(time.time() - t0, 1), "tries": len(tries)}
        if time.time() - t0 > timeout:
            raise EraError(f"`.npc info {spawn_id}` did not show the creature within {timeout} s", tries=tries[-3:])
        time.sleep(1.0)


Session.go = _go
Session.npc_info = _npc_info


def spawn_rows(entry: int, maps: set[int] | None = None) -> list[dict]:
    """AC `creature` spawns of an entry (any spawn-id column of this fork's schema), lowest spawn id first."""
    idc = [r[0] for r in db.query("SHOW COLUMNS FROM creature", "world") if r[0] in ("id", "id1", "id2", "id3")]
    where = " OR ".join(f"{c} = {int(entry)}" for c in idc)
    rows = db.query(f"SELECT guid, map, position_x, position_y, position_z FROM creature WHERE {where} ORDER BY guid",
                    "world")
    out = [{"spawn_id": int(g), "map": int(m), "x": float(x), "y": float(y), "z": float(z)} for g, m, x, y, z in rows]
    return [r for r in out if maps is None or r["map"] in maps]


def inspect_spawn(s: Session, entry: int, spawns: list[dict], max_tries: int = 5) -> dict:
    """Teleport to the entry's spawns in order until one answers `.npc info`; returns its live state."""
    tried = []
    for sp in spawns[:max_tries]:
        try:
            go = s.go(sp["x"], sp["y"], sp["z"], sp["map"])
            info = s.npc_info(sp["spawn_id"])
        except EraError as e:
            tried.append({"spawn": sp, "error": str(e), **e.evidence})
            continue
        if info["entry"] != entry:
            tried.append({"spawn": sp, "error": f"spawn shows entry {info['entry']}", "info": info})
            continue
        return {"spawn": sp, "go": go, "npc_info": info, "tried_before": tried}
    raise EraError(f"none of the first {max_tries} spawns of creature {entry} answered `.npc info`", tried=tried)


# ----------------------------------------------------------------------------------------- stock AC (pinned base SQL)

BASE_SQL_DIR = P.CORE_SRC / "data/sql/base/db_world"


def _sql_tuple(line: str) -> list:
    """Values of one mysqldump tuple line `(v1,'str',NULL,...),` as strings (None for NULL)."""
    vals, i, n = [], 1, len(line)
    while i < n:
        if line[i] == "'":
            j, buf = i + 1, []
            while True:
                ch = line[j]
                if ch == "\\":
                    buf.append({"n": "\n", "t": "\t", "r": "\r", "0": "\0"}.get(line[j + 1], line[j + 1]))
                    j += 2
                elif ch == "'" and j + 1 < n and line[j + 1] == "'":
                    buf.append("'")
                    j += 2
                elif ch == "'":
                    break
                else:
                    buf.append(ch)
                    j += 1
            vals.append("".join(buf))
            i = j + 1
        else:
            j = i
            while line[j] not in ",)":
                j += 1
            tok = line[i:j]
            vals.append(None if tok == "NULL" else tok)
            i = j
        if line[i] == ")":
            break
        i += 1
    return vals


def ac_base_rows(table: str, key_cols: list[str], keys: set[tuple]) -> dict[tuple, dict]:
    """Rows of the pinned core's base SQL (data/sql/base/db_world/<table>.sql, one tuple per line) for the given key
    tuples, as {key: {column: string value}}; columns come from the file's own CREATE TABLE."""
    cols, out, in_create = [], {}, False
    first = {str(k[0]) for k in keys}
    with open(BASE_SQL_DIR / f"{table}.sql", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(f"CREATE TABLE `{table}`"):
                in_create = True
                continue
            if in_create:
                m = re.match(r"\s+`(\w+)`", line)
                if m:
                    cols.append(m.group(1))
                elif line.startswith(")"):
                    in_create = False
                continue
            if not (line.startswith("(") and cols) or line[1:line.find(",")] not in first:
                continue
            row = dict(zip(cols, _sql_tuple(line.rstrip("\n"))))
            k = tuple(int(row[c]) for c in key_cols)
            if k in keys:
                out[k] = row
    return out


def update_sql_mentions(table: str, ids: list[int]) -> dict[int, list[str]]:
    """{id: [files]} for SQL statements, in the core's world-DB updates and the other modules' world SQL (not mod-era-profile, the
    module under test), that name `table` and contain the id as a whole number. A mention means the base SQL may not
    be the stock value any more (a coarse test: a false positive only rules a candidate out)."""
    roots = [P.CORE_SRC / "data/sql/updates/db_world", P.CORE_SRC / "data/sql/updates/pending_db_world",
             P.BOTS_SRC / "data/sql/world"] + \
            [d / "data/sql/db-world" for d in sorted(P.MODULES.iterdir()) if d.name != "mod-era-profile"]
    out = {i: [] for i in ids}
    want = set(ids)
    pat = re.compile(rf"\b{re.escape(table)}\b")
    num = re.compile(r"(?<![\d.])\d+(?![\d.])")
    for root in roots:
        for f in sorted(root.rglob("*.sql")) if root.exists() else []:
            text = f.read_text(errors="replace")
            if not pat.search(text):
                continue
            rel = str(f.relative_to(P.ROOT))
            for stmt in text.split(";"):
                if pat.search(stmt):
                    for i in want & {int(x) for x in num.findall(stmt)}:
                        if rel not in out[i]:
                            out[i].append(rel)
    return out


_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_STMT_FULL_ROW = re.compile(r"\s*(INSERT|REPLACE|DELETE)\b", re.I)


def update_sql_touches(table: str, key_cols: list[str], cols: list[str], keys: set[tuple]) -> dict[tuple, list[str]]:
    """{key: [files]} for the rows of `table` whose stock value of `cols` the world-DB update SQL may have changed
    after the pinned base SQL (same roots as update_sql_mentions: core updates and other modules' world SQL, not
    mod-era-profile). A statement touches a key when it names `table` and either (a) contains every number of the key
    and names one of `cols` or is a full-row INSERT/REPLACE/DELETE, or (b) names one of `cols` and none of `key_cols`
    (a broad UPDATE: it touches every key). Coarse on purpose: a false positive only drops a row from a base-SQL
    comparison, and the comparing check reports every dropped row with its files."""
    roots = [P.CORE_SRC / "data/sql/updates/db_world", P.CORE_SRC / "data/sql/updates/pending_db_world",
             P.BOTS_SRC / "data/sql/world"] + \
            [d / "data/sql/db-world" for d in sorted(P.MODULES.iterdir()) if d.name != "mod-era-profile"]
    out = {k: [] for k in keys}
    pat = re.compile(rf"\b{re.escape(table)}\b")
    colp = re.compile(r"\b(" + "|".join(map(re.escape, cols)) + r")\b", re.I)
    keyp = re.compile(r"\b(" + "|".join(map(re.escape, key_cols)) + r")\b", re.I)
    num = re.compile(r"(?<![\d.])\d+(?![\d.])")
    for root in roots:
        for f in sorted(root.rglob("*.sql")) if root.exists() else []:
            text = f.read_text(errors="replace")
            if not pat.search(text):
                continue
            rel = str(f.relative_to(P.ROOT))
            for stmt in text.split(";"):
                if not pat.search(stmt):
                    continue
                names_col = bool(colp.search(stmt))
                if names_col and not keyp.search(stmt):
                    hit = keys
                elif names_col or _STMT_FULL_ROW.match(_SQL_COMMENT.sub(" ", stmt)):
                    nums = {int(x) for x in num.findall(stmt)}
                    hit = [k for k in keys if all(int(x) in nums for x in k)]
                else:
                    continue
                for k in hit:
                    if rel not in out[k]:
                        out[k].append(rel)
    return out


def stock_from_base_sql(table: str, key_cols: list[str], cols: list[str], keys: set[tuple]) -> dict:
    """Stock AzerothCore values of `cols` for `keys`, read from the pinned core's base SQL, independent of the module
    (its baseline table and its revert). {"values": {key: {col: str}}, "guarded": {key: [files]} (rows the update SQL
    may have changed, update_sql_touches: no trustworthy base value), "missing": [keys absent from the base SQL]}.
    player_xp_for_level comes from ac_stock_xp_for_level (its own two-column dump)."""
    if table == "player_xp_for_level":
        xp = ac_stock_xp_for_level()
        base = {(lvl,): {"Experience": str(v)} for lvl, v in xp.items() if (lvl,) in keys}
    else:
        base = {k: {c: r[c] for c in cols} for k, r in ac_base_rows(table, key_cols, keys).items()}
    touched = update_sql_touches(table, key_cols, cols, keys)
    guarded = {k: v for k, v in touched.items() if v}
    return {"values": {k: v for k, v in base.items() if k not in guarded}, "guarded": guarded,
            "missing": sorted(k for k in keys if k not in base)}


# ----------------------------------------------------------------------------------------- creature HP (AC formula)

def f32(x: float) -> float:
    """Round to IEEE-754 single precision (C++ float arithmetic)."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def hp_rate(rank: int) -> float:
    """Creature::_GetHealthMod(rank) (Creature.cpp:1575-1592): the Rate.Creature.*.HP setting of the running config
    for the rank (WorldConfig.cpp defaults 1.0)."""
    key = {0: "Rate.Creature.Normal.HP", 1: "Rate.Creature.Elite.Elite.HP", 2: "Rate.Creature.Elite.RAREELITE.HP",
           3: "Rate.Creature.Elite.WORLDBOSS.HP", 4: "Rate.Creature.Elite.RARE.HP"}.get(rank, "Rate.Creature.Elite.Elite.HP")
    v = rendered_option(P.SERVER_ETC / "worldserver.conf", key)
    return f32(float(v) if v is not None else 1.0)


def creature_max_hp(base_hp: int, mod: float, rank: int) -> int:
    """AC max HP: CreatureBaseStats::GenerateHealth = uint32(ceil(BaseHealth[exp] * ModHealth)) (CreatureData.h:313),
    then uint32(max(1, that) * _GetHealthMod(rank)) (Creature::SelectLevel, Creature.cpp:1530-1533); float math."""
    hp = max(1, int(math.ceil(f32(base_hp * f32(mod)))))
    return int(f32(hp * hp_rate(rank)))


def ac_base_hp(level: int, unit_class: int, exp: int) -> int:
    """creature_classlevelstats basehp{exp} for (level, class) from the world DB (not touched by the module)."""
    v = db.scalar(f"SELECT basehp{int(exp)} FROM creature_classlevelstats WHERE level={int(level)} AND "
                  f"class={int(unit_class)}", "world")
    if v is None:
        raise EraError(f"no creature_classlevelstats row for level {level} class {unit_class}")
    return int(v)


def vm_base_hp(level: int, unit_class: int) -> int:
    """VMaNGOS creature_classlevelstats.health for (class, level) from the pinned snapshot."""
    r = era_diff().VM().q("SELECT health FROM creature_classlevelstats WHERE class=? AND level=?", (unit_class, level))
    if not r:
        raise EraError(f"no VMaNGOS creature_classlevelstats row for class {unit_class} level {level}")
    return int(r[0]["health"])


def vm_creature(entry: int) -> dict:
    """Final 1.12 VMaNGOS creature_template row (era_diff.VM.latest: patch <= 10)."""
    rows = era_diff().VM().latest("creature_template", cols="entry, patch, name, level_min, level_max, rank, "
                                  "unit_class, health_multiplier")
    if entry not in rows:
        raise EraError(f"creature {entry} not in the VMaNGOS snapshot")
    return dict(rows[entry])


def vm_item(entry: int) -> dict:
    rows = era_diff().VM().latest("item_template", cols="entry, patch, name, required_level, required_skill, "
                                  "required_skill_rank, buy_price")
    if entry not in rows:
        raise EraError(f"item {entry} not in the VMaNGOS snapshot")
    return dict(rows[entry])


def side_keys(table: str, keys: str) -> set[tuple]:
    return {tuple(int(x) for x in r) for r in db.query(f"SELECT {keys} FROM `{table}`", "world")}


# ----------------------------------------------------------------------------------------- reputation discount

def rep_rank_names() -> list[str]:
    """Reputation rank names in ReputationRank order (Hated .. Exalted): acore_string LANG_REP_HATED..LANG_REP_EXALTED
    (ids parsed from the core's Language.h), the strings `.character reputation` prints."""
    text = (P.CORE_SRC / "src/server/game/Miscellaneous/Language.h").read_text()
    lo = int(re.search(r"LANG_REP_HATED\s*=\s*(\d+)", text).group(1))
    hi = int(re.search(r"LANG_REP_EXALTED\s*=\s*(\d+)", text).group(1))
    rows = {int(i): t for i, t in db.query(f"SELECT entry, content_default FROM acore_string WHERE entry BETWEEN {lo} "
                                           f"AND {hi}", "world")}
    return [rows[i] for i in range(lo, hi + 1)]


REP_NEUTRAL = 3          # SharedDefines.h enum ReputationRank (REP_HATED 0 .. REP_NEUTRAL 3 .. REP_EXALTED 7)


def price_discount(char: str, faction_template: int) -> dict:
    """Player::GetReputationPriceDiscount (Player.cpp:12596-12618) for an online character and a creature's faction
    template: 1.0 at Neutral or below (and for a faction the character has no standing with: ReputationMgr::GetReputation
    returns 0), else 1 - 0.05 * (rank - Neutral) in float. The rank comes from `.character reputation <name>`
    (cs_character.cpp) and the faction from FactionTemplate.dbc field 1 (DBCStructure.h FactionTemplateEntry)."""
    from . import dbc
    recs, _ = dbc.read_dbc("FactionTemplate.dbc")
    faction = next((r[1] for r in recs if r[0] == faction_template), None)
    out = {"faction_template": faction_template, "faction": faction}
    r = gm(f"character reputation {char}")
    names = rep_rank_names()
    line = next((l for l in r.output.splitlines() if faction is not None and l.startswith(f"{faction} - ")), None)
    out["reputation_line"] = line
    rank = REP_NEUTRAL
    if line:
        m = re.search(r" (\S+) \((-?\d+)\)", line)
        if not m or m.group(1) not in names:
            raise EraError("cannot read the reputation rank", line=line, rank_names=names)
        rank = names.index(m.group(1))
    out["rank"] = rank
    out["discount"] = 1.0 if rank <= REP_NEUTRAL else f32(1.0 - f32(f32(0.05) * (rank - REP_NEUTRAL)))
    return out


def discounted(cost: int, discount: float) -> int:
    """int32(MoneyCost * reputationDiscount) as Trainer::SendSpells computes it (float)."""
    return int(f32(cost * f32(discount)))


def spell_names(ids: set[int] | None = None) -> dict[int, str]:
    """Spell.dbc enUS names (SpellEntry field 136, DBCStructure.h) by spell id."""
    from . import dbc
    recs, strings = dbc.read_dbc("Spell.dbc")
    return {r[0]: dbc.dbc_string(strings, r[136]) for r in recs if ids is None or r[0] in ids}


# ----------------------------------------------------------------------------------------- selection rules

NORTHREND_MAP = 571
NORTHREND_RULE = ("lowest creature_template entries whose AC spawns are all on map 571 (at least one), AIName and "
                  "ScriptName empty, exp 0-2, no addon auras, with a row in the pinned base SQL and no world-DB update "
                  "SQL statement mentioning it together with creature_template (so the base SQL is its stock value). "
                  "Not filtered on the module's tables: the check itself requires it to be absent from them")


def _spawn_id_cols() -> list[str]:
    return [r[0] for r in db.query("SHOW COLUMNS FROM creature", "world") if r[0] in ("id", "id1", "id2", "id3")]


def northrend_candidates(n: int = 5) -> list[dict]:
    """Out-of-scope creatures for era.elites, by NORTHREND_RULE (first n)."""
    on = lambda a: "(" + " OR ".join(f"{a}.{c} = ct.entry" for c in _spawn_id_cols()) + ")"   # noqa: E731
    rows = db.query(
        "SELECT ct.entry, ct.`rank`, ct.HealthModifier, ct.exp, ct.unit_class FROM creature_template ct "
        "WHERE ct.AIName = '' AND ct.ScriptName = '' AND ct.exp IN (0, 1, 2) "
        f"AND EXISTS (SELECT 1 FROM creature c WHERE {on('c')} AND c.map = {NORTHREND_MAP}) "
        f"AND NOT EXISTS (SELECT 1 FROM creature c WHERE {on('c')} AND c.map <> {NORTHREND_MAP}) "
        "AND NOT EXISTS (SELECT 1 FROM creature_template_addon a WHERE a.entry = ct.entry AND IFNULL(a.auras, '') <> '') "
        f"AND NOT EXISTS (SELECT 1 FROM creature c JOIN creature_addon a ON a.guid = c.guid WHERE {on('c')} "
        "AND IFNULL(a.auras, '') <> '') "
        "ORDER BY ct.entry LIMIT 100", "world")
    live = {int(e): {"rank": int(r), "HealthModifier": float(h), "exp": int(x), "unit_class": int(u)}
            for e, r, h, x, u in rows}
    base = ac_base_rows("creature_template", ["entry"], {(e,) for e in live})
    mentions = update_sql_mentions("creature_template", list(live))
    out = []
    for e, lv in live.items():
        b = base.get((e,))
        if not b or mentions[e]:
            continue
        out.append({"entry": e, "name": b["name"], "base_sql": {k: b[k] for k in ("rank", "HealthModifier", "exp",
                    "unit_class", "minlevel", "maxlevel")}, "live": lv})
        if len(out) >= n:
            break
    if not out:
        raise EraError("no map-571 creature matches the rule", rule=NORTHREND_RULE)
    return out


RIDING_TRAINER_RULE = ("lowest creature entry with a spawn whose trainer (creature_default_trainer) is valid for the "
                       "human GM character (trainer.Requirement 0 or race 1) and lists the two riding spells and "
                       "every spell named Expert Riding, Artisan Riding and Cold Weather Flying (Spell.dbc)")


def riding_trainer(spells: tuple, names: tuple) -> dict:
    """The trainer for era.mounts' trainer window, by RIDING_TRAINER_RULE."""
    rows = db.query("SELECT d.CreatureId, d.TrainerId, ts.SpellId FROM creature_default_trainer d "
                    "JOIN trainer t ON t.Id = d.TrainerId JOIN trainer_spell ts ON ts.TrainerId = d.TrainerId "
                    "WHERE t.Requirement IN (0, 1) ORDER BY d.CreatureId", "world")
    by = {}
    for c, t, sp in rows:
        by.setdefault((int(c), int(t)), set()).add(int(sp))
    sn = spell_names({sp for v in by.values() for sp in v})
    for (c, t), sps in sorted(by.items()):
        named = {sp: sn.get(sp) for sp in sps if sn.get(sp) in names}
        if not set(spells) <= sps or {sn.get(sp) for sp in named} != set(names):
            continue
        spawns = spawn_rows(c)
        if not spawns:
            continue
        ft = int(db.scalar(f"SELECT faction FROM creature_template WHERE entry = {c}", "world"))
        return {"creature": c, "trainer": t, "spawns": spawns, "faction_template": ft, "unchanged_spells": named,
                "rule": RIDING_TRAINER_RULE}
    raise EraError("no riding trainer matches the rule", rule=RIDING_TRAINER_RULE)
