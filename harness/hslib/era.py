"""era-profile (openspec/changes/m3-vanilla-mode) harness helpers.

Expected values are read from the source data at check time, never typed in (CLAUDE.md §7):
  * VMaNGOS values: the pinned VMaNGOS SQLite snapshot, through tools/era-diff (final 1.12 row per entry);
  * stock AzerothCore values: the pinned core's own base SQL (data/sql/base/db_world/*.sql) for the XP table, and
    QuestXP.dbc (+ questxp_dbc overrides) with AC's Quest::XPValue rounding for quest XP (era_diff helpers).
Observed values come from the running server through the protocol probe: our character's own update fields
(PLAYER_NEXT_LEVEL_XP, as the client's XP bar reads it) and SMSG_LOG_XPGAIN (what the client prints on a reward).
"""
import importlib.util
import re
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
    """The HSTEST character in the world through the probe (pumped), driven by SOAP GM commands by name."""

    def __init__(self):
        self.ws = None
        self.guid = None
        self.level = None
        self.ev = {}

    def __enter__(self):
        self.ev["fixture"] = F.ensure()
        self.guid = F.char_guid()
        F._require_offline(self.guid)
        lr, ws, wev = probe.open_world(P.TEST_USER, P.TEST_PASS)
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
            self.ev["revive"] = gm(f"revive {P.TEST_CHAR}", ok_required=False).evidence()
        except Exception:
            ws.close()
            raise
        return self

    def __exit__(self, *exc):
        if not self.ws:
            return
        try:
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
        r = gm(f"character level {P.TEST_CHAR} {level}")
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
        ev["remove_before"] = gm(f"quest remove {qid} {P.TEST_CHAR}", ok_required=False).output.strip()[-200:]
        ev["add"] = gm(f"quest add {qid} {P.TEST_CHAR}").output.strip()[-200:]
        ev["complete"] = gm(f"quest complete {qid} {P.TEST_CHAR}").output.strip()[-200:]
        since = self.ws.mark()
        ev["reward"] = gm(f"quest reward {qid} {P.TEST_CHAR}").output.strip()[-200:]
        g = self.ws.wait_event(lambda e: e["kind"] == "xp_gain", since, XP_GAIN_S)
        ev["xp_gain_event"] = g
        if g is None:
            raise EraError(f"no SMSG_LOG_XPGAIN within {XP_GAIN_S} s of `.quest reward {qid}`", **ev)
        f = self._wait_fields(since, lambda f: W.PLAYER_XP in f, UPDATE_S)
        ev["player_xp_after"] = f.get(W.PLAYER_XP) if f else None
        ev["xp_gained"] = g["xp"]
        ev["remove_after"] = gm(f"quest remove {qid} {P.TEST_CHAR}", ok_required=False).output.strip()[-200:]
        return ev
