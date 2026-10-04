#!/usr/bin/env python3
"""era-diff: compare VMaNGOS (1.12) world data against AzerothCore (3.3.5a) for the
Hearthstone era profile (CLAUDE.md §7, KICKOFF.md §3 Gate 1).

Scope rules (KICKOFF.md correction note):
  * only entities present in BOTH databases, matched by entry ID;
  * only 1-60 scope: no map 530 (Outland/TBC starting zones), 571 (Northrend),
    609 (DK starting zone), nothing above level 60;
  * override-only: the tool never emits deletions;
  * anything that can't be mapped cleanly is emitted with status "needs-review"
    and a reason.

VMaNGOS is a read-only DATA reference. Nothing here builds or targets it.

Stdlib only.  Usage:
    python3 tools/era-diff/era_diff.py               # all categories
    python3 tools/era-diff/era_diff.py --list
    python3 tools/era-diff/era_diff.py -c quest_xp -c outdoor_elite
    python3 tools/era-diff/era_diff.py --emit-sql     # mod-era-profile side tables (m3 task 1.2)

--emit-sql writes modules/mod-era-profile/data/sql/db-world/era_*.sql: one side table per touched AC table with the
vanilla value of every in-scope change row, each row citing its source (VMaNGOS snapshot id from fork.lock, VMaNGOS
table, VMaNGOS key), plus era_baseline.sql (CREATE TABLE IF NOT EXISTS only) for the stock values the module captures
before its first apply. Output is deterministic: no timestamps, rows sorted by key, fixed number formats, so a re-run
against the same sources is byte-identical (check era.sql_deterministic). The AC side of the diff reads captured
baseline values (era_baseline_*) in place of live rows when they exist, so a world DB with vanilla mode applied
diffs (and emits) exactly like a stock one.
"""
import argparse
import csv
import datetime as _dt
import hashlib
import json
import math
import sqlite3
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))
from hslib import dbc as _hs_dbc  # noqa: E402  (shared WDBC reader)

VM_ZIP = ROOT / "data/downloads/vmangos-db-sqlite.zip"
VM_DIR = ROOT / "data/vmangos"
VM_DB = VM_DIR / "sqlite-dump/mangos.sqlite"
DBC_DIR = ROOT / "data/client-data/dbc"
OUT_DIR = Path(__file__).resolve().parent / "out"

VM_PATCH = 10        # VMaNGOS patch index for 1.12 (max patch row = final 1.12 state)
VM_BUILD = 5875      # 1.12.1 client build, used by VMaNGOS spell_template/npc_trainer
MAX_LEVEL = 60
EXCLUDED_MAPS = {530, 571, 609}   # Outland(+TBC starter zones), Northrend, DK start (Ebon Hold)
RIDING_SKILL = 762
AURA_MOUNTED = 78
SPELL_EFFECT_LEARN_SPELL = 36
CLASS_DK_MASK = 32

RANK_NAMES = {0: "normal", 1: "elite", 2: "rare-elite", 3: "boss", 4: "rare"}

# Columns of each AC world table that mod-era-profile may overwrite in place (startup apply/revert, design.md) and
# therefore captures into era_baseline_<table> before its first apply. Keep in sync with EMIT_TABLES below and with
# the module's apply specs (modules/mod-era-profile/src/EraProfile.cpp).
BASELINE_COLS = {
    "player_xp_for_level": [("Experience", int)],
    "creature_template": [("rank", int), ("HealthModifier", float)],
    "item_template": [("RequiredLevel", int), ("RequiredSkillRank", int), ("BuyPrice", int)],
    "trainer_spell": [("ReqLevel", int), ("MoneyCost", int)],
}


# --------------------------------------------------------------------------- sources
def fork_lock():
    out = {}
    for line in (ROOT / "fork.lock").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_vmangos():
    if not VM_DB.exists():
        if not VM_ZIP.exists():
            sys.exit(f"missing {VM_ZIP}; fetch it first (fork.lock vmangos_db_url)")
        VM_DIR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(VM_ZIP) as z:
            z.extractall(VM_DIR)
    con = sqlite3.connect(f"file:{VM_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


class VM:
    """VMaNGOS reader. Patch-versioned tables resolve to the final 1.12 row per entry."""

    def __init__(self):
        self.con = ensure_vmangos()

    def q(self, sql, args=()):
        return [dict(r) for r in self.con.execute(sql, args)]

    def latest(self, table, key="entry", ver="patch", cap=VM_PATCH, cols="*"):
        sql = (f"SELECT {cols} FROM {table} t WHERE {ver} = (SELECT MAX({ver}) FROM {table} u "
               f"WHERE u.{key} = t.{key} AND u.{ver} <= ?)")
        return {r[key]: r for r in self.q(sql, (cap,))}


class AC:
    """AzerothCore live world DB via harness/hslib/db.py (mysql CLI)."""

    def __init__(self):
        from hslib import db  # noqa: E402  (harness on sys.path)
        self.db = db

    def q(self, sql, cols):
        rows = self.db.query(sql, "world", timeout=600)
        out = []
        for r in rows:
            d = {}
            for (name, typ), v in zip(cols, r):
                d[name] = None if v == "NULL" else (typ(v) if typ is not str else v)
            out.append(d)
        return out

    def scalar(self, sql):
        return self.db.scalar(sql, "world")


def read_dbc(name):
    """WDBC reader (moved to harness/hslib/dbc.py, m2-living-world task 1.1) -> (records, string block)."""
    return _hs_dbc.read_dbc(name, DBC_DIR)


dbc_string = _hs_dbc.dbc_string


# --------------------------------------------------------------------------- helpers
def ac_round_quest_xp(xp):
    """Rounding rule copied from AC Quest::XPValue (src/server/game/Quests/QuestDef.cpp)."""
    xp = int(xp)
    if xp <= 100:
        return 5 * ((xp + 2) // 5)
    if xp <= 500:
        return 10 * ((xp + 5) // 10)
    if xp <= 1000:
        return 25 * ((xp + 12) // 25)
    return 50 * ((xp + 25) // 50)


def row(category, entity, entry, name, field, vm, ac, proposed, mechanism, status="change",
        reason="", **extra):
    r = {"category": category, "entity": entity, "entry": entry, "name": name, "field": field,
         "vmangos": vm, "ac": ac, "proposed": proposed, "mechanism": mechanism,
         "status": status, "reason": reason}
    r.update(extra)
    return r


def review(category, entity, entry, name, field, vm, ac, reason, **extra):
    return row(category, entity, entry, name, field, vm, ac, None, "none", "needs-review",
               reason, **extra)


# --------------------------------------------------------------------------- context
class Ctx:
    def __init__(self):
        self.vm = VM()
        self.ac = AC()
        self._cache = {}

    def memo(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    # --- captured stock baseline (mod-era-profile): stock values of rows the module may have overwritten
    def baseline(self, table, keys, cols):
        """{key tuple: {col: value}} from era_baseline_<table> (written by mod-era-profile before its first apply), or
        {} when that table doesn't exist. Used so the diff compares VMaNGOS against *stock* AC even when vanilla mode
        has been applied to the live world rows."""
        def f():
            name = f"era_baseline_{table}"
            n = self.ac.scalar("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() "
                               f"AND table_name = '{name}'")
            if n in (None, "0"):
                return {}
            spec = [(k, int) for k in keys] + list(cols)
            sel = ", ".join(f"`{c}`" for c, _ in spec)
            out = {}
            for r in self.ac.q(f"SELECT {sel} FROM `{name}`", spec):
                out[tuple(r[k] for k in keys)] = {c: r[c] for c, _ in cols}
            return out
        return self.memo(("baseline", table), f)

    def overlay(self, rows, table, keys, cols):
        """Replace live values in `rows` (dicts keyed by AC column names) with captured baseline values."""
        base = self.baseline(table, keys, cols)
        if base:
            for r in rows:
                b = base.get(tuple(r[k] for k in keys))
                if b:
                    r.update(b)
        return rows

    # --- maps / zones
    def map_types(self):
        def f():
            recs, _ = read_dbc("Map.dbc")
            types = {r[0]: r[2] for r in recs}
            for r in self.ac.q("SELECT ID, InstanceType FROM map_dbc", [("ID", int), ("t", int)]):
                types[r["ID"]] = r["t"]
            return types
        return self.memo("map_types", f)

    def outdoor_maps(self):
        """Open-world maps in scope: Map.dbc map_type 0 (common, i.e. not instance/raid/BG/
        arena) that also exist in VMaNGOS map_template, minus excluded maps."""
        def f():
            vm_maps = {r["entry"] for r in self.vm.q("SELECT DISTINCT entry FROM map_template")}
            return {m for m, t in self.map_types().items()
                    if t == 0 and m in vm_maps and m not in EXCLUDED_MAPS}
        return self.memo("outdoor", f)

    def zone_map(self):
        def f():
            recs, _ = read_dbc("AreaTable.dbc")
            zm = {r[0]: r[1] for r in recs}
            for r in self.ac.q("SELECT ID, ContinentID FROM areatable_dbc",
                               [("ID", int), ("m", int)]):
                zm[r["ID"]] = r["m"]
            return zm
        return self.memo("zone_map", f)

    def quest_xp_table(self):
        """QuestXP.dbc (level -> Exp[10]) with AC's questxp_dbc DB overrides applied
        (AC DBCDatabaseLoader: DB rows replace/add DBC rows by ID)."""
        def f():
            recs, _ = read_dbc("QuestXP.dbc")
            t = {r[0]: list(r[1:11]) for r in recs}
            cols = [("ID", int)] + [(f"d{i}", int) for i in range(1, 11)]
            over = self.ac.q("SELECT ID," + ",".join(f"Difficulty_{i}" for i in range(1, 11)) +
                             " FROM questxp_dbc", cols)
            for r in over:
                t[r["ID"]] = [r[f"d{i}"] for i in range(1, 11)]
            return t, len(over)
        return self.memo("questxp", f)

    # --- creatures
    def vm_creatures(self):
        return self.memo("vm_ct", lambda: self.vm.latest(
            "creature_template",
            cols="entry, patch, name, level_min, level_max, rank, unit_class, health_multiplier, "
                 "damage_multiplier, armor_multiplier, xp_multiplier, mana_multiplier, "
                 "base_attack_time, trainer_id"))

    def ac_creatures(self):
        cols = [("entry", int), ("name", str), ("minlevel", int), ("maxlevel", int), ("exp", int),
                ("rank", int), ("unit_class", int), ("HealthModifier", float),
                ("DamageModifier", float), ("ArmorModifier", float),
                ("ExperienceModifier", float), ("ManaModifier", float), ("BaseAttackTime", int)]
        return self.memo("ac_ct", lambda: {r["entry"]: r for r in self.overlay(self.ac.q(
            "SELECT entry, REPLACE(name,'\\t',' '), minlevel, maxlevel, exp, `rank`, unit_class, "
            "HealthModifier, DamageModifier, ArmorModifier, ExperienceModifier, ManaModifier, "
            "BaseAttackTime FROM creature_template", cols), "creature_template", ["entry"],
            BASELINE_COLS["creature_template"])})

    def vm_spawn_maps(self):
        def f():
            m = defaultdict(set)
            for r in self.vm.q("SELECT id, id2, id3, id4, id5, map FROM creature "
                               "WHERE patch_min <= ? AND patch_max >= ?", (VM_PATCH, VM_PATCH)):
                for k in ("id", "id2", "id3", "id4", "id5"):
                    if r[k]:
                        m[r[k]].add(r["map"])
            return m
        return self.memo("vm_spawn", f)

    def ac_spawn_maps(self):
        def f():
            m = defaultdict(set)
            idc = self.creature_id_cols()
            cols = [(f"i{n}", int) for n in range(len(idc))] + [("map", int)]
            for r in self.ac.q(f"SELECT {', '.join(idc)}, map FROM creature", cols):
                for n in range(len(idc)):
                    if r[f"i{n}"]:
                        m[r[f"i{n}"]].add(r["map"])
            return m
        return self.memo("ac_spawn", f)

    def ac_trainer_spells(self):
        cols = [("TrainerId", int), ("SpellId", int), ("MoneyCost", int), ("ReqSkillLine", int),
                ("ReqSkillRank", int), ("ReqLevel", int)]
        return self.memo("ac_ts", lambda: self.overlay(self.ac.q(
            "SELECT TrainerId, SpellId, MoneyCost, ReqSkillLine, ReqSkillRank, ReqLevel FROM trainer_spell "
            "ORDER BY TrainerId, SpellId", cols), "trainer_spell", ["TrainerId", "SpellId"],
            BASELINE_COLS["trainer_spell"]))

    def creature_id_cols(self):
        """Live AC `creature` spawn-id columns: `id` (this fork's live schema) or id1..id3."""
        def f():
            cols = {r[0] for r in self.ac.db.query("SHOW COLUMNS FROM creature", "world")}
            return [c for c in ("id", "id1", "id2", "id3") if c in cols]
        return self.memo("cidcols", f)

    def creature_scope(self):
        """Classify every creature in both DBs -> (in_scope: bool, reason)."""
        def f():
            vm, ac = self.vm_creatures(), self.ac_creatures()
            vsp, asp = self.vm_spawn_maps(), self.ac_spawn_maps()
            outdoor = self.outdoor_maps()
            res = {}
            for e in sorted(set(vm) & set(ac)):
                v, a = vm[e], ac[e]
                if max(v["level_max"], a["maxlevel"]) > MAX_LEVEL:
                    res[e] = (False, "level>60")
                    continue
                am, vmaps = asp.get(e, set()), vsp.get(e, set())
                if not am or not vmaps:
                    res[e] = (None, "no static spawn in "
                              + ("AC" if not am else "VMaNGOS") + "; outdoor/instance unknown")
                    continue
                if am & EXCLUDED_MAPS:
                    res[e] = (False, f"AC spawns on excluded map(s) {sorted(am & EXCLUDED_MAPS)}")
                    continue
                if am <= outdoor and vmaps <= outdoor:
                    res[e] = (True, "outdoor")
                elif not (am & outdoor) and not (vmaps & outdoor):
                    res[e] = (False, "instance-only")
                else:
                    res[e] = (None, f"mixed outdoor/instance spawns AC={sorted(am)} "
                                    f"VM={sorted(vmaps)}")
            return res
        return self.memo("cscope", f)

    def classlevel(self):
        def f():
            vm = {(r["class"], r["level"]): r for r in self.vm.q(
                "SELECT class, level, health, base_health, melee_damage, armor FROM "
                "creature_classlevelstats")}
            cols = [("level", int), ("class", int), ("hp0", int), ("hp1", int), ("hp2", int),
                    ("armor", int), ("dmg0", float)]
            ac = {(r["class"], r["level"]): r for r in self.ac.q(
                "SELECT level, class, basehp0, basehp1, basehp2, basearmor, damage_base "
                "FROM creature_classlevelstats", cols)}
            return vm, ac
        return self.memo("cls", f)

    # --- quests
    def vm_quests(self):
        return self.memo("vm_qt", lambda: self.vm.latest(
            "quest_template",
            cols="entry, patch, Title, QuestLevel, MinLevel, ZoneOrSort, RequiredClasses, "
                 "RequiredRaces, RewXP, RewOrReqMoney, RewMoneyMaxLevel, RewSpell, RewSpellCast, "
                 "PrevQuestId, NextQuestId, NextQuestInChain, QuestFlags, SpecialFlags"))

    def ac_quests(self):
        cols = [("ID", int), ("LogTitle", str), ("QuestLevel", int), ("MinLevel", int),
                ("QuestSortID", int), ("RewardXPDifficulty", int), ("RewardMoney", int),
                ("RewardSpell", int), ("RewardDisplaySpell", int), ("Flags", int),
                ("AllowableRaces", int), ("AllowableClasses", int), ("MaxLevel", int),
                ("PrevQuestID", int), ("NextQuestID", int), ("SpecialFlags", int)]
        return self.memo("ac_qt", lambda: {r["ID"]: r for r in self.ac.q(
            "SELECT q.ID, REPLACE(REPLACE(q.LogTitle,'\\t',' '),'\\n',' '), q.QuestLevel, "
            "q.MinLevel, q.QuestSortID, q.RewardXPDifficulty, q.RewardMoney, q.RewardSpell, "
            "q.RewardDisplaySpell, q.Flags, q.AllowableRaces, IFNULL(a.AllowableClasses,0), "
            "IFNULL(a.MaxLevel,0), IFNULL(a.PrevQuestID,0), IFNULL(a.NextQuestID,0), "
            "IFNULL(a.SpecialFlags,0) "
            "FROM quest_template q LEFT JOIN quest_template_addon a ON a.ID = q.ID", cols)})

    def ac_quest_starter_maps(self):
        def f():
            m = defaultdict(set)
            idc = self.creature_id_cols()
            on = " OR ".join(f"c.{x} = s.id" for x in idc)
            for r in self.ac.q("SELECT s.quest, c.map FROM creature_queststarter s "
                               f"JOIN creature c ON ({on}) GROUP BY s.quest, c.map",
                               [("q", int), ("map", int)]):
                m[r["q"]].add(r["map"])
            for r in self.ac.q("SELECT s.quest, g.map FROM gameobject_queststarter s "
                               "JOIN gameobject g ON g.id = s.id GROUP BY s.quest, g.map",
                               [("q", int), ("map", int)]):
                m[r["q"]].add(r["map"])
            return m
        return self.memo("ac_qsm", f)

    def ac_disabled_quests(self):
        return self.memo("ac_dis", lambda: {r["e"]: r["c"] for r in self.ac.q(
            "SELECT entry, REPLACE(comment,'\\t',' ') FROM disables WHERE sourceType = 1",
            [("e", int), ("c", str)])})

    def quest_scope(self):
        """Quests in both DBs, classified -> (in_scope, reason)."""
        def f():
            vm, ac = self.vm_quests(), self.ac_quests()
            zm, sm = self.zone_map(), self.ac_quest_starter_maps()
            res = {}
            for e in sorted(set(vm) & set(ac)):
                v, a = vm[e], ac[e]
                if (a["AllowableClasses"] or 0) & CLASS_DK_MASK and a["AllowableClasses"] == CLASS_DK_MASK:
                    res[e] = (False, "Death Knight quest")
                    continue
                if v["QuestLevel"] > MAX_LEVEL or a["QuestLevel"] > MAX_LEVEL \
                        or v["MinLevel"] > MAX_LEVEL or a["MinLevel"] > MAX_LEVEL:
                    res[e] = (False, "level>60")
                    continue
                if a["QuestSortID"] > 0 and zm.get(a["QuestSortID"]) in EXCLUDED_MAPS:
                    res[e] = (False, f"AC zone {a['QuestSortID']} on map {zm[a['QuestSortID']]}")
                    continue
                starters = sm.get(e, set())
                if starters and starters <= EXCLUDED_MAPS:
                    res[e] = (False, f"AC quest starters only on maps {sorted(starters)}")
                    continue
                res[e] = (True, "")
            return res
        return self.memo("qscope", f)


# --------------------------------------------------------------------------- categories
def cat_xp_curve(c):
    """player_xp_for_level: XP needed at level L to reach L+1. World-DB table in both cores."""
    vm = {r["lvl"]: r["xp_for_next_level"] for r in c.vm.q("SELECT * FROM player_xp_for_level")}
    ac = {r["Level"]: r["Experience"] for r in c.overlay(
        c.ac.q("SELECT Level, Experience FROM player_xp_for_level", [("Level", int), ("Experience", int)]),
        "player_xp_for_level", ["Level"], BASELINE_COLS["player_xp_for_level"])}
    out = []
    for lvl in sorted(set(vm) & set(ac)):
        if lvl > MAX_LEVEL:
            continue
        if vm[lvl] == ac[lvl]:
            continue
        if lvl == MAX_LEVEL:
            out.append(review("xp_curve", "player_xp_for_level", lvl, f"level {lvl}->{lvl + 1}",
                              "Experience", vm[lvl], ac[lvl],
                              "row gates 60->61 (Outland pacing); 60 was the 1.12 level cap, "
                              "outside the 1-60 scope", delta=vm[lvl] - ac[lvl]))
            continue
        out.append(row("xp_curve", "player_xp_for_level", lvl, f"level {lvl}->{lvl + 1}",
                       "Experience", vm[lvl], ac[lvl], vm[lvl],
                       "sql:UPDATE player_xp_for_level", delta=vm[lvl] - ac[lvl],
                       pct=round(100.0 * (vm[lvl] - ac[lvl]) / ac[lvl], 1)))
    return out


def cat_exploration_xp(c):
    vm = {r["level"]: r["basexp"] for r in c.vm.q("SELECT * FROM exploration_basexp")}
    ac = {r["L"]: r["X"] for r in c.ac.q("SELECT level, basexp FROM exploration_basexp",
                                         [("L", int), ("X", int)])}
    out = []
    for lvl in sorted(set(vm) & set(ac)):
        if lvl > MAX_LEVEL or vm[lvl] == ac[lvl]:
            continue
        out.append(row("exploration_xp", "exploration_basexp", lvl, f"area level {lvl}",
                       "basexp", vm[lvl], ac[lvl], vm[lvl], "sql:UPDATE exploration_basexp",
                       delta=vm[lvl] - ac[lvl]))
    return out


def cat_quest_xp(c):
    """VMaNGOS stores absolute RewXP; AC stores RewardXPDifficulty (0-9), an index into
    QuestXP.dbc[QuestLevel].Exp[]. AC full (no level-penalty) XP at quest level =
    ac_round_quest_xp(Exp[QuestLevel][RewardXPDifficulty]) (diffFactor clamps to 10)."""
    vm, ac = c.vm_quests(), c.ac_quests()
    qxp, nover = c.quest_xp_table()
    out = []
    for e, (ok, why) in c.quest_scope().items():
        if not ok:
            continue
        v, a = vm[e], ac[e]
        title = a["LogTitle"]
        vxp = v["RewXP"]
        if a["QuestLevel"] in (-1, 0) or v["QuestLevel"] in (-1, 0):
            out.append(review("quest_xp", "quest_template", e, title, "RewardXPDifficulty",
                              vxp, None, f"level-scaling quest (QuestLevel AC={a['QuestLevel']} "
                              f"VM={v['QuestLevel']}); no fixed XP to compare"))
            continue
        exp = qxp.get(a["QuestLevel"])
        if not exp:
            out.append(review("quest_xp", "quest_template", e, title, "RewardXPDifficulty", vxp,
                              None, f"no QuestXP row for level {a['QuestLevel']}"))
            continue
        d = a["RewardXPDifficulty"]
        acxp = ac_round_quest_xp(exp[d]) if 0 <= d < 10 else None
        if acxp == vxp:
            continue
        ladder = [ac_round_quest_xp(x) for x in exp]
        common = dict(quest_level_ac=a["QuestLevel"], quest_level_vm=v["QuestLevel"],
                      ac_difficulty=d, delta=None if acxp is None else vxp - acxp,
                      pct=round(100.0 * (vxp - acxp) / acxp, 1) if acxp else None)
        if a["QuestLevel"] != v["QuestLevel"]:
            out.append(review("quest_xp", "quest_template", e, title, "RewXP", vxp, acxp,
                              f"QuestLevel differs (AC {a['QuestLevel']}, VM {v['QuestLevel']}); "
                              "XP not comparable without also changing quest level", **common))
            continue
        if vxp == 0:
            out.append(review("quest_xp", "quest_template", e, title, "RewXP", vxp, acxp,
                              "VMaNGOS RewXP=0 (no XP, or unset in VMaNGOS); "
                              "would remove XP from a WotLK quest", **common))
            continue
        if acxp is None or acxp == 0:
            out.append(review("quest_xp", "quest_template", e, title, "RewXP", vxp, acxp,
                              f"AC RewardXPDifficulty={d} gives no XP", **common))
            continue
        if vxp in ladder:
            nd = ladder.index(vxp)
            out.append(row("quest_xp", "quest_template", e, title, "RewardXPDifficulty", vxp, acxp,
                           nd, "sql:UPDATE quest_template.RewardXPDifficulty (exact)", **common))
        else:
            nd = min(range(10), key=lambda i: abs(ladder[i] - vxp))
            out.append(row("quest_xp", "quest_template", e, title, "RewXP(absolute)", vxp, acxp,
                           vxp, "module:OnPlayerQuestComputeXP absolute override",
                           nearest_difficulty=nd, nearest_difficulty_xp=ladder[nd], **common))
    return out


def cat_quest_levels(c):
    vm, ac = c.vm_quests(), c.ac_quests()
    out = []
    for e, (ok, _) in c.quest_scope().items():
        if not ok:
            continue
        v, a = vm[e], ac[e]
        for vf, af in (("QuestLevel", "QuestLevel"), ("MinLevel", "MinLevel")):
            if v[vf] != a[af] and (a[af] == -1 or v[vf] == -1):
                out.append(review("quest_levels", "quest_template", e, a["LogTitle"], af, v[vf],
                                  a[af], "one side is level-scaling (-1); fixing it changes "
                                         "semantics, not just a value"))
            elif v[vf] != a[af]:
                out.append(row("quest_levels", "quest_template", e, a["LogTitle"], af, v[vf], a[af],
                               v[vf], "sql:UPDATE quest_template", delta=v[vf] - a[af]))
    return out


def cat_quest_money(c):
    vm, ac = c.vm_quests(), c.ac_quests()
    out = []
    for e, (ok, _) in c.quest_scope().items():
        if not ok:
            continue
        v, a = vm[e], ac[e]
        if v["RewOrReqMoney"] != a["RewardMoney"]:
            if (v["RewOrReqMoney"] < 0) != (a["RewardMoney"] < 0):
                out.append(review("quest_money", "quest_template", e, a["LogTitle"], "RewardMoney",
                                  v["RewOrReqMoney"], a["RewardMoney"],
                                  "sign differs (reward vs required money)"))
                continue
            out.append(row("quest_money", "quest_template", e, a["LogTitle"], "RewardMoney",
                           v["RewOrReqMoney"], a["RewardMoney"], v["RewOrReqMoney"],
                           "sql:UPDATE quest_template.RewardMoney",
                           delta=v["RewOrReqMoney"] - a["RewardMoney"]))
    return out


def cat_outdoor_elite(c):
    vm, ac = c.vm_creatures(), c.ac_creatures()
    out = []
    for e, (ok, why) in c.creature_scope().items():
        v, a = vm[e], ac[e]
        if v["rank"] == a["rank"] or ok is False:
            continue
        direction = f"{RANK_NAMES.get(a['rank'], a['rank'])}->{RANK_NAMES.get(v['rank'], v['rank'])}"
        lv = f"{a['minlevel']}-{a['maxlevel']}"
        if ok is None:
            out.append(review("outdoor_elite", "creature_template", e, a["name"], "rank",
                              v["rank"], a["rank"], why, direction=direction, ac_levels=lv))
            continue
        out.append(row("outdoor_elite", "creature_template", e, a["name"], "rank", v["rank"],
                       a["rank"], v["rank"], "sql:UPDATE creature_template.rank",
                       direction=direction, ac_levels=lv,
                       vm_health_multiplier=v["health_multiplier"],
                       ac_HealthModifier=a["HealthModifier"],
                       note="rank alone does not change HP/damage in AC (Rate.Creature.Elite.* "
                            "default 1); pair with creature_stats rows"))
    return out


def cat_creature_stats(c):
    """HP: AC = ceil(basehp{exp}[level,class] * HealthModifier) (CreatureData.h GenerateHealth).
    VMaNGOS multiplier applies to creature_classlevelstats.health; that column is compared to
    AC basehp0 per (class, level) in category creature_base_stats. Proposed HealthModifier
    reproduces VMaNGOS HP at both min and max level when the base tables agree."""
    vm, ac = c.vm_creatures(), c.ac_creatures()
    vcls, acls = c.classlevel()
    out = []
    for e, (ok, why) in c.creature_scope().items():
        if ok is not True:
            continue
        v, a = vm[e], ac[e]
        name = a["name"]
        # health
        if abs(v["health_multiplier"] - a["HealthModifier"]) > 1e-4 or \
                (v["level_min"], v["level_max"]) != (a["minlevel"], a["maxlevel"]):
            ratios = []
            bad = None
            for lvl in sorted({a["minlevel"], a["maxlevel"]}):
                vb = vcls.get((v["unit_class"], lvl))
                ab = acls.get((a["unit_class"], lvl))
                if not vb or not ab:
                    bad = f"no classlevelstats for class/level {a['unit_class']}/{lvl}"
                    break
                abase = ab[f"hp{a['exp']}"] if a["exp"] in (0, 1, 2) else None
                if not abase or abase <= 1:
                    bad = f"AC basehp{a['exp']} unusable at level {lvl}"
                    break
                ratios.append(vb["health"] * v["health_multiplier"] / abase)
            if (v["level_min"], v["level_max"]) != (a["minlevel"], a["maxlevel"]):
                out.append(review("creature_stats", "creature_template", e, name, "level",
                                  f"{v['level_min']}-{v['level_max']}",
                                  f"{a['minlevel']}-{a['maxlevel']}",
                                  "level range differs; HP multiplier not comparable"))
            elif bad:
                out.append(review("creature_stats", "creature_template", e, name, "HealthModifier",
                                  v["health_multiplier"], a["HealthModifier"], bad))
            elif a["exp"] != 0:
                out.append(review("creature_stats", "creature_template", e, name, "HealthModifier",
                                  v["health_multiplier"], a["HealthModifier"],
                                  f"AC exp={a['exp']} uses basehp{a['exp']} (non-vanilla base)",
                                  proposed_if_applied=round(sum(ratios) / len(ratios), 4)))
            elif max(ratios) - min(ratios) > 0.01 * max(ratios):
                out.append(review("creature_stats", "creature_template", e, name, "HealthModifier",
                                  v["health_multiplier"], a["HealthModifier"],
                                  "base HP tables disagree across level range", ratios=ratios))
            elif abs(ratios[0] - a["HealthModifier"]) > 1e-3:
                out.append(row("creature_stats", "creature_template", e, name, "HealthModifier",
                               v["health_multiplier"], a["HealthModifier"],
                               round(ratios[0], 4), "sql:UPDATE creature_template.HealthModifier",
                               rank_vm=v["rank"], rank_ac=a["rank"],
                               ac_hp_min=math.ceil(acls[(a["unit_class"], a["minlevel"])]["hp0"]
                                                   * a["HealthModifier"]),
                               vm_hp_min=round(vcls[(v["unit_class"], a["minlevel"])]["health"]
                                               * v["health_multiplier"])))
        # damage: the two cores use different base damage models -> always review
        if abs(v["damage_multiplier"] - a["DamageModifier"]) > 1e-4:
            out.append(review("creature_stats", "creature_template", e, name, "DamageModifier",
                              v["damage_multiplier"], a["DamageModifier"],
                              "damage models differ (VM melee_damage vs AC damage_base/AP); "
                              "multiplier not directly transferable"))
        if abs(v["xp_multiplier"] - a["ExperienceModifier"]) > 1e-4:
            out.append(row("creature_stats", "creature_template", e, name, "ExperienceModifier",
                           v["xp_multiplier"], a["ExperienceModifier"], v["xp_multiplier"],
                           "sql:UPDATE creature_template.ExperienceModifier"))
    return out


def cat_creature_base_stats(c):
    """creature_classlevelstats (class, level<=60): VMaNGOS health vs AC basehp0, armor."""
    vcls, acls = c.classlevel()
    out = []
    for k in sorted(set(vcls) & set(acls)):
        cl, lvl = k
        if lvl > MAX_LEVEL:
            continue
        v, a = vcls[k], acls[k]
        if v["health"] != a["hp0"]:
            out.append(review("creature_base_stats", "creature_classlevelstats", f"{cl}/{lvl}",
                              f"class {cl} level {lvl}", "basehp0", v["health"], a["hp0"],
                              "shared base table: changing it moves every exp=0 creature of this "
                              "class/level, including WotLK-era spawns"))
        if v["armor"] != a["armor"]:
            out.append(review("creature_base_stats", "creature_classlevelstats", f"{cl}/{lvl}",
                              f"class {cl} level {lvl}", "basearmor", v["armor"], a["armor"],
                              "shared base table; affects all creatures of this class/level"))
    return out


def cat_mount_items(c):
    vm = c.vm.latest("item_template", cols="entry, patch, name, required_level, required_skill, "
                     "required_skill_rank, buy_price, quality, allowable_class")
    cols = [("entry", int), ("name", str), ("RequiredLevel", int), ("RequiredSkill", int),
            ("RequiredSkillRank", int), ("BuyPrice", int)]
    ac = {r["entry"]: r for r in c.overlay(c.ac.q(
        "SELECT entry, REPLACE(name,'\\t',' '), RequiredLevel, RequiredSkill, RequiredSkillRank, "
        "BuyPrice FROM item_template", cols), "item_template", ["entry"], BASELINE_COLS["item_template"])}
    sold = {r["i"] for r in c.ac.q("SELECT DISTINCT item FROM npc_vendor", [("i", int)])}
    vsold = {r["item"] for r in c.vm.q("SELECT item FROM npc_vendor UNION SELECT item FROM npc_vendor_template")}
    out = []
    for e, v in sorted(vm.items()):
        if v["required_skill"] != RIDING_SKILL or e not in ac:
            continue
        a = ac[e]
        if e not in sold or e not in vsold or a["RequiredSkill"] != RIDING_SKILL:
            why = ("not sold by any AC vendor" if e not in sold else
                   "not sold by any VMaNGOS vendor" if e not in vsold else
                   f"AC RequiredSkill={a['RequiredSkill']} (not riding)")
            diffs = {af: (v[vf], a[af]) for vf, af in (
                ("required_level", "RequiredLevel"), ("required_skill_rank", "RequiredSkillRank"),
                ("buy_price", "BuyPrice")) if v[vf] != a[af]}
            if diffs:
                out.append(review("mount_items", "item_template", e, a["name"], ",".join(diffs),
                                  {k: x[0] for k, x in diffs.items()},
                                  {k: x[1] for k, x in diffs.items()},
                                  why + "; likely a deprecated/unobtainable item, not a mount "
                                        "purchase path"))
            continue
        for vf, af in (("required_level", "RequiredLevel"), ("required_skill_rank",
                                                              "RequiredSkillRank"),
                       ("buy_price", "BuyPrice")):
            if v[vf] != a[af]:
                if af == "RequiredLevel" and v[vf] > MAX_LEVEL:
                    continue
                out.append(row("mount_items", "item_template", e, a["name"], af, v[vf], a[af],
                               v[vf], f"sql:UPDATE item_template.{af}"))
    return out


def cat_riding_training(c):
    """Riding skill spells on trainers. VMaNGOS npc_trainer(_template) active at build 5875
    vs AC trainer_spell, matched by spell ID or by the spell it teaches."""
    st = c.vm.latest("spell_template", ver="build", cap=VM_BUILD,
                     cols="entry, build, name, effect1, effectTriggerSpell1")
    vrows = c.vm.q("SELECT 'npc_trainer' src, entry, spell, spellcost, reqlevel, reqskill, "
                   "reqskillvalue FROM npc_trainer WHERE build_min <= ? AND build_max >= ? "
                   "UNION ALL SELECT 'npc_trainer_template', entry, spell, spellcost, reqlevel, "
                   "reqskill, reqskillvalue FROM npc_trainer_template "
                   "WHERE build_min <= ? AND build_max >= ?", (VM_BUILD,) * 4)
    # only trainer lists actually used by 1.12 creatures (the final patch<=10 creature row)
    vct = c.vm_creatures()
    spawned = c.vm_spawn_maps()
    used_tpl = {v["trainer_id"] for e, v in vct.items() if v["trainer_id"] and e in spawned}
    vriding = {}
    for r in vrows:
        s = st.get(r["spell"])
        live = (r["entry"] in used_tpl) if r["src"] == "npc_trainer_template" else (r["entry"] in spawned)
        if s and "Riding" in s["name"] and live:
            vriding.setdefault(r["spell"], (r, s))
    cols = [("TrainerId", int), ("SpellId", int), ("MoneyCost", int), ("ReqSkillLine", int),
            ("ReqSkillRank", int), ("ReqLevel", int)]
    arows = c.ac_trainer_spells()
    out = []
    for sid, (r, s) in sorted(vriding.items()):
        taught = s["effectTriggerSpell1"] if s["effect1"] == SPELL_EFFECT_LEARN_SPELL else None
        matches = [a for a in arows if a["SpellId"] in (sid, taught)]
        if not matches:
            out.append(review("riding_training", "trainer_spell", sid, s["name"], "presence",
                              f"reqlevel {r['reqlevel']} cost {r['spellcost']}", None,
                              f"spell {sid} (teaches {taught}) not on any AC trainer",
                              vm_trainer=f"{r['src']}:{r['entry']}"))
            continue
        for fld, vv, af in (("ReqLevel", r["reqlevel"], "ReqLevel"),
                            ("MoneyCost", r["spellcost"], "MoneyCost")):
            vals = Counter(a[af] for a in matches)
            if set(vals) == {vv}:
                continue
            out.append(row("riding_training", "trainer_spell", sid, s["name"], fld, vv,
                           dict(vals), vv, f"sql:UPDATE trainer_spell.{fld} WHERE SpellId IN "
                           f"({sid},{taught})", ac_trainers=sorted({a['TrainerId'] for a in matches}),
                           ac_spell_ids=sorted({a['SpellId'] for a in matches}),
                           vm_trainer=f"{r['src']}:{r['entry']}"))
    return out


def cat_class_mount_quests(c):
    """VMaNGOS quests (and their chains) whose reward teaches a mount spell
    (aura 78 SPELL_AURA_MOUNTED). Report AC state; never auto-change (would need re-enabling
    quests and/or removing trainer spells, which is not override-only)."""
    st = c.vm.latest("spell_template", ver="build", cap=VM_BUILD,
                     cols="entry, build, name, effect1, effect2, effectTriggerSpell1, "
                          "effectTriggerSpell2, effectApplyAuraName1, effectApplyAuraName2, "
                          "effectApplyAuraName3")
    mounts = {e for e, s in st.items()
              if AURA_MOUNTED in (s["effectApplyAuraName1"], s["effectApplyAuraName2"],
                                  s["effectApplyAuraName3"])}
    vq, aq = c.vm_quests(), c.ac_quests()
    dis = c.ac_disabled_quests()
    starters = c.ac_quest_starter_maps()

    def mount_of(spell):
        if spell in mounts:
            return spell
        s = st.get(spell)
        if s:
            for k in ("effectTriggerSpell1", "effectTriggerSpell2"):
                if s[k] in mounts:
                    return s[k]
        return None

    finals = {}
    for e, v in vq.items():
        m = mount_of(v["RewSpellCast"]) or mount_of(v["RewSpell"])
        if m and v["RequiredClasses"]:
            finals[e] = m
    # walk chains backwards via PrevQuestId (positive or negative) and NextQuestInChain
    prev_of = defaultdict(set)
    for e, v in vq.items():
        if v["NextQuestInChain"]:
            prev_of[v["NextQuestInChain"]].add(e)
        if v["PrevQuestId"]:
            prev_of[e].add(abs(v["PrevQuestId"]))
    chain = {}
    for f, m in finals.items():
        todo, seen = [f], set()
        while todo:
            q = todo.pop()
            if q in seen or q not in vq:
                continue
            seen.add(q)
            chain.setdefault(q, (f, m))
            todo.extend(prev_of.get(q, ()))
    acq_spell_cols = [("SpellId", int), ("TrainerId", int), ("ReqLevel", int)]
    tspells = defaultdict(list)
    for r in c.ac_trainer_spells():
        tspells[r["SpellId"]].append({k: r[k] for k, _ in acq_spell_cols})
    out = []
    for q, (f, m) in sorted(chain.items()):
        v = vq[q]
        a = aq.get(q)
        info = dict(final_quest=f, mount_spell=m, mount_name=st[m]["name"],
                    vm_level=v["QuestLevel"], vm_minlevel=v["MinLevel"],
                    vm_required_classes=v["RequiredClasses"])
        if a is None:
            out.append(review("class_mount_quests", "quest_template", q, v["Title"], "presence",
                              "present", "absent", "quest not in AC (cannot override-only)",
                              **info))
            continue
        taught = [x for x in (v["RewSpell"], v["RewSpellCast"]) if x]
        ac_trainer = {s: [(t["TrainerId"], t["ReqLevel"]) for t in tspells.get(s, [])]
                      for s in set(taught + [m])}
        info.update(ac_level=a["QuestLevel"], ac_minlevel=a["MinLevel"],
                    ac_reward_spell=a["RewardSpell"], ac_display_spell=a["RewardDisplaySpell"],
                    ac_disabled=dis.get(q), ac_starter_maps=sorted(starters.get(q, ())),
                    ac_trainer_spells=ac_trainer)
        reasons = []
        if q in dis:
            reasons.append(f"disabled in AC `disables` ({dis[q]!r})")
        if not starters.get(q):
            reasons.append("no spawned quest giver in AC")
        if any(ac_trainer.values()):
            reasons.append("AC teaches the mount spell from class trainers; restoring the quest "
                           "as the only source would require removing trainer rows (not override-only)")
        if (a["QuestLevel"], a["MinLevel"]) != (v["QuestLevel"], v["MinLevel"]):
            reasons.append("quest level differs")
        out.append(review("class_mount_quests", "quest_template", q, v["Title"], "state",
                          f"L{v['QuestLevel']} min{v['MinLevel']}",
                          f"L{a['QuestLevel']} min{a['MinLevel']}",
                          "; ".join(reasons) or "present and enabled in AC", **info))
    return out


CATEGORIES = {
    "xp_curve": cat_xp_curve,
    "exploration_xp": cat_exploration_xp,
    "quest_xp": cat_quest_xp,
    "quest_levels": cat_quest_levels,
    "quest_money": cat_quest_money,
    "outdoor_elite": cat_outdoor_elite,
    "creature_stats": cat_creature_stats,
    "creature_base_stats": cat_creature_base_stats,
    "mount_items": cat_mount_items,
    "riding_training": cat_riding_training,
    "class_mount_quests": cat_class_mount_quests,
}

NOT_IN_DB = {
    "kill_xp_formula": "Creature kill XP is computed in core code (AC Acore::XP::BaseGain); not "
                       "world-DB data. VMaNGOS core source not consulted; unsourced.",
    "rest_xp": "Rest XP rates are worldserver.conf settings (Rate.Rest.*) in AC; VMaNGOS keeps "
               "them in core config, not the world DB. No DB value to diff; unsourced.",
}



# --------------------------------------------------------------------------- --emit-sql (m3 task 1.2)
SQL_DIR = ROOT / "modules/mod-era-profile/data/sql/db-world"
QUEST_XP_KEEP_PCT = 5       # Gate 1 Decision 2: quests whose VMaNGOS XP is within +-5% of AC's keep AC's XP
# Gate 1 Decision 3 "Outdoor elites: restored together with their elite HP" (orchestrator scope decision, m3 task 2.3,
# design.md "Elite HP scope"): a creature_stats HealthModifier change is emitted only for creatures whose VMaNGOS rank
# is an elite rank. Rank values: AC SharedDefines.h CreatureEliteType (0 normal, 1 elite, 2 rare-elite, 3 world boss,
# 4 rare); VMaNGOS uses the same values (RANK_NAMES).
ELITE_RANKS = (1, 2, 3)


def elite_hp_in_scope(vm_rank):
    """Elite-HP scope rule: HealthModifier is applied only where the VMaNGOS rank is elite, rare-elite or boss."""
    return vm_rank in ELITE_RANKS


def sql_lit(v):
    """Deterministic SQL literal: NULL, integers as-is, floats with 4 decimals, strings single-quoted."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return f"{v:.4f}"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


# Side tables: name -> (AC target table, key columns, value columns (name, SQL type, comment), description).
# Value columns are NULL when that field keeps its stock value for the row.
EMIT_TABLES = {
    "era_player_xp_for_level": (
        "player_xp_for_level", [("Level", "TINYINT UNSIGNED")],
        [("Experience", "INT UNSIGNED", "VMaNGOS player_xp_for_level.xp_for_next_level")],
        "Gate 1 #1: XP to reach the next level, levels 11-59 (the L60 row is needs-review and stays AC)."),
    "era_quest_xp": (
        "quest_template", [("QuestId", "INT UNSIGNED")],
        [("QuestLevel", "SMALLINT", "AC quest_template.QuestLevel (equal to VMaNGOS QuestLevel for every row)"),
         ("RewXP", "INT UNSIGNED", "VMaNGOS quest_template.RewXP: full XP at quest level (docs/gate1-era-profile-memo.md)")],
        "Gate 1 #2: vanilla quest XP for the OnPlayerQuestComputeXP hook; rows within +-5% of AC and "
        "needs-review rows are left out (they keep AC XP)."),
    "era_creature_template": (
        "creature_template", [("entry", "INT UNSIGNED")],
        [("rank", "TINYINT UNSIGNED", "VMaNGOS creature_template.rank; NULL = keep AC"),
         ("HealthModifier", "FLOAT", "derived from VMaNGOS health_multiplier (era_diff cat_creature_stats); only VMaNGOS elite ranks (ELITE_RANKS); NULL = keep AC")],
        "Gate 1 #3/#4: outdoor 1-60 creatures: vanilla rank, and vanilla HealthModifier only where the VMaNGOS rank "
        "is elite/rare-elite/boss (elite HP); damage/armor deferred."),
    "era_item_template": (
        "item_template", [("entry", "INT UNSIGNED")],
        [("RequiredLevel", "TINYINT UNSIGNED", "VMaNGOS item_template.required_level; NULL = keep AC"),
         ("RequiredSkillRank", "SMALLINT UNSIGNED", "VMaNGOS item_template.required_skill_rank; NULL = keep AC"),
         ("BuyPrice", "BIGINT", "VMaNGOS item_template.buy_price; NULL = keep AC")],
        "Gate 1 #5: vendor-sold riding mounts."),
    "era_trainer_spell": (
        "trainer_spell", [("TrainerId", "INT UNSIGNED"), ("SpellId", "INT UNSIGNED")],
        [("ReqLevel", "TINYINT UNSIGNED", "VMaNGOS npc_trainer(_template).reqlevel; NULL = keep AC"),
         ("MoneyCost", "INT UNSIGNED", "VMaNGOS npc_trainer(_template).spellcost; NULL = keep AC")],
        "Gate 1 #5: Apprentice/Journeyman riding on AC trainers."),
}

BASELINE_SQL_TYPES = {
    "player_xp_for_level": ([("Level", "TINYINT UNSIGNED")], [("Experience", "INT UNSIGNED")]),
    "creature_template": ([("entry", "INT UNSIGNED")], [("rank", "TINYINT UNSIGNED"), ("HealthModifier", "FLOAT")]),
    "item_template": ([("entry", "INT UNSIGNED")],
                      [("RequiredLevel", "TINYINT UNSIGNED"), ("RequiredSkillRank", "SMALLINT UNSIGNED"),
                       ("BuyPrice", "BIGINT")]),
    "trainer_spell": ([("TrainerId", "INT UNSIGNED"), ("SpellId", "INT UNSIGNED")],
                      [("ReqLevel", "TINYINT UNSIGNED"), ("MoneyCost", "INT UNSIGNED")]),
}


def keep_stock_quest(vxp, acxp):
    """Decision 2: |VM - AC| <= 5% of AC keeps AC's XP. Exact integer arithmetic (no rounded pct)."""
    return 100 * abs(vxp - acxp) <= QUEST_XP_KEEP_PCT * acxp


def emit_rows(c, snap):
    """{side table: sorted list of row tuples (keys..., values..., src_snapshot, src_table, src_entry)}."""
    rows = {}
    rows["era_player_xp_for_level"] = sorted(
        (r["entry"], r["proposed"], snap, "player_xp_for_level", f"lvl={r['entry']}")
        for r in cat_xp_curve(c) if r["status"] == "change")

    q = []
    for r in cat_quest_xp(c):
        if r["status"] != "change" or keep_stock_quest(r["vmangos"], r["ac"]):
            continue
        # both mechanisms ("exact" difficulty index and absolute) go through the module so over-level XP follows
        # VMaNGOS rounding too (memo, RewXP semantics, Caveat A)
        assert r["quest_level_ac"] == r["quest_level_vm"], r
        q.append((r["entry"], r["quest_level_ac"], r["vmangos"], snap, "quest_template", f"entry={r['entry']}"))
    rows["era_quest_xp"] = sorted(q)

    cre = {}
    for r in cat_outdoor_elite(c):
        if r["status"] == "change":
            cre.setdefault(r["entry"], {})["rank"] = r["proposed"]
    vmc = c.vm_creatures()
    for r in cat_creature_stats(c):
        if r["status"] == "change" and r["field"] == "HealthModifier" and elite_hp_in_scope(vmc[r["entry"]]["rank"]):
            cre.setdefault(r["entry"], {})["HealthModifier"] = float(r["proposed"])
    rows["era_creature_template"] = sorted(
        (e, v.get("rank"), v.get("HealthModifier"), snap, "creature_template", f"entry={e}")
        for e, v in cre.items())

    it = {}
    for r in cat_mount_items(c):
        if r["status"] == "change":
            it.setdefault(r["entry"], {})[r["field"]] = r["proposed"]
    rows["era_item_template"] = sorted(
        (e, v.get("RequiredLevel"), v.get("RequiredSkillRank"), v.get("BuyPrice"), snap, "item_template",
         f"entry={e}") for e, v in it.items())

    ts = {}
    live = {(a["TrainerId"], a["SpellId"]) for a in c.ac_trainer_spells()}
    for r in cat_riding_training(c):
        if r["status"] != "change":
            continue
        vt_table, vt_entry = r["vm_trainer"].split(":")
        for t in r["ac_trainers"]:
            for sp in r["ac_spell_ids"]:
                if (t, sp) not in live:
                    continue
                d = ts.setdefault((t, sp), {"src": (vt_table, f"entry={vt_entry},spell={r['entry']}")})
                d[r["field"]] = r["proposed"]
    rows["era_trainer_spell"] = sorted(
        (t, sp, v.get("ReqLevel"), v.get("MoneyCost"), snap, v["src"][0], v["src"][1]) for (t, sp), v in ts.items())
    return rows


def emit_header(c, lock, snap, title, lines):
    try:
        last_core = c.ac.scalar("SELECT name FROM updates WHERE state <> 'MODULE' ORDER BY name DESC LIMIT 1")
    except Exception:  # pragma: no cover
        last_core = None
    migs = c.vm.q("SELECT id FROM migrations ORDER BY id DESC LIMIT 1")
    h = [f"-- {title}",
         "-- GENERATED by `python3 tools/era-diff/era_diff.py --emit-sql`. DO NOT EDIT: regenerate instead (CLAUDE.md §7).",
         f"-- Source: VMaNGOS {snap} ({lock.get('vmangos_db_url', '')}; zip sha256 "
         f"{sha256(VM_ZIP) if VM_ZIP.exists() else 'n/a'}; latest migration {migs[0]['id'] if migs else 'n/a'}),",
         f"--   final 1.12 row per entry (patch <= {VM_PATCH}; spells/trainers at build {VM_BUILD}).",
         f"-- Compared against: AzerothCore world DB, core {lock.get('core_sha')}, last core DB update {last_core}.",
         f"-- Scope: entities in both DBs by entry, level <= {MAX_LEVEL}, no maps {sorted(EXCLUDED_MAPS)}; "
         "override-only; needs-review rows omitted."]
    return "\n".join(h + [f"-- {l}" for l in lines]) + "\n"


def emit_sql(c, out_dir):
    lock = fork_lock()
    url = lock.get("vmangos_db_url", "")
    snap = url.rsplit("/", 1)[-1].removesuffix(".zip")
    if not snap:
        sys.exit("fork.lock has no vmangos_db_url; cannot cite a snapshot")
    rows = emit_rows(c, snap)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for name, (target, keys, vals, desc) in EMIT_TABLES.items():
        rs = rows[name]
        cols = [k for k, _ in keys] + [v for v, _, _ in vals] + ["src_snapshot", "src_table", "src_entry"]
        body = [emit_header(c, lock, snap, f"mod-era-profile side table `{name}` (vanilla values for AC `{target}`)",
                            [desc, f"Rows: {len(rs)}."]),
                f"DROP TABLE IF EXISTS `{name}`;",
                f"CREATE TABLE `{name}` ("]
        body += [f"  `{k}` {t} NOT NULL," for k, t in keys]
        body += [f"  `{v}` {t} {'NOT NULL' if name in ('era_player_xp_for_level', 'era_quest_xp') else 'NULL'} "
                 f"COMMENT {sql_lit(cm)}," for v, t, cm in vals]
        body += ["  `src_snapshot` VARCHAR(64) NOT NULL COMMENT 'VMaNGOS DB snapshot id (fork.lock vmangos_db_url)',",
                 "  `src_table` VARCHAR(64) NOT NULL COMMENT 'VMaNGOS table the value comes from',",
                 "  `src_entry` VARCHAR(64) NOT NULL COMMENT 'VMaNGOS key of the source row',",
                 f"  PRIMARY KEY ({', '.join(f'`{k}`' for k, _ in keys)})",
                 f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT={sql_lit(desc[:2048])};"]
        if rs:
            body.append(f"INSERT INTO `{name}` ({', '.join(f'`{x}`' for x in cols)}) VALUES")
            body.append(",\n".join("(" + ",".join(sql_lit(v) for v in r) + ")" for r in rs) + ";")
        f = out_dir / f"{name}.sql"
        f.write_text("\n".join(body) + "\n")
        written[f.name] = len(rs)
    # captured baseline: CREATE IF NOT EXISTS only, never dropped by a regeneration (it holds the stock values)
    body = [emit_header(c, lock, snap, "mod-era-profile captured stock baseline tables",
                        ["mod-era-profile copies the stock AzerothCore values of every row it may overwrite into these",
                         "tables before its first apply (INSERT IGNORE: the first capture wins), and restores them when",
                         "EraProfile.Enable = 0. They are never dropped or regenerated: they hold the only copy of the",
                         "stock values while vanilla mode is applied. era_diff reads them in place of live rows."])]
    for target, (keys, vals) in BASELINE_SQL_TYPES.items():
        name = f"era_baseline_{target}"
        assert [v for v, _ in vals] == [v for v, _ in BASELINE_COLS[target]], target
        body.append(f"CREATE TABLE IF NOT EXISTS `{name}` (")
        body += [f"  `{k}` {t} NOT NULL," for k, t in keys]
        body += [f"  `{v}` {t} NULL," for v, t in vals]
        body += ["  `captured_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,",
                 f"  PRIMARY KEY ({', '.join(f'`{k}`' for k, _ in keys)})",
                 f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT="
                 f"{sql_lit(f'stock AC {target} values captured by mod-era-profile before its first apply')};"]
    f = out_dir / "era_baseline.sql"
    f.write_text("\n".join(body) + "\n")
    written[f.name] = 0
    return written

# --------------------------------------------------------------------------- main
def header(c):
    lock = fork_lock()
    try:
        last_upd = c.ac.scalar("SELECT name FROM updates ORDER BY timestamp DESC, name DESC LIMIT 1")
    except Exception as ex:  # pragma: no cover
        last_upd = f"unavailable: {ex}"
    try:
        ver = c.ac.db.query("SELECT core_version, core_revision, db_version FROM version", "world")
    except Exception:
        ver = None
    migs = c.vm.q("SELECT id FROM migrations ORDER BY id DESC LIMIT 1")
    url = lock.get("vmangos_db_url", "")
    return {
        "tool": "tools/era-diff/era_diff.py",
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "vmangos": {
            "snapshot_url": url,
            "snapshot_id": url.rsplit("/", 1)[-1].removesuffix(".zip") if url else None,
            "zip": str(VM_ZIP.relative_to(ROOT)),
            "zip_sha256": sha256(VM_ZIP) if VM_ZIP.exists() else None,
            "latest_migration": migs[0]["id"] if migs else None,
            "row_selection": f"patch-versioned tables: max patch <= {VM_PATCH} (1.12); "
                             f"spell/trainer: build {VM_BUILD}; spawns active at patch {VM_PATCH}",
        },
        "azerothcore": {
            "core_repo": lock.get("core_repo"), "core_sha": lock.get("core_sha"),
            "world_db_last_update": last_upd, "version_table": ver,
            "questxp_dbc_overrides": c.quest_xp_table()[1],
            "dbc_dir": str(DBC_DIR.relative_to(ROOT)),
        },
        "scope": {"max_level": MAX_LEVEL, "excluded_maps": sorted(EXCLUDED_MAPS),
                  "outdoor_maps": sorted(c.outdoor_maps()),
                  "rules": ["entities present in both DBs (by entry ID)", "1-60 only",
                            "override-only; no deletions", "unmappable rows -> needs-review"]},
        "not_in_world_db": NOT_IN_DB,
    }


def write(cat, rows, out):
    out.mkdir(parents=True, exist_ok=True)
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(out / f"{cat}.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys or ["category"])
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                        for k, v in r.items()})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("-c", "--category", action="append", choices=list(CATEGORIES))
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--emit-sql", action="store_true",
                    help="write the mod-era-profile side tables (era_*.sql) instead of the report")
    ap.add_argument("--sql-dir", type=Path, default=SQL_DIR, help="where --emit-sql writes (default: the module)")
    args = ap.parse_args()
    if args.list:
        for k, fn in CATEGORIES.items():
            print(f"{k:22s} {(fn.__doc__ or '').strip().splitlines()[0] if fn.__doc__ else ''}")
        return
    c = Ctx()
    if args.emit_sql:
        for name, n in emit_sql(c, args.sql_dir).items():
            print(f"{name:32s} rows={n:5d}")
        print(f"wrote {args.sql_dir}")
        return
    cats = args.category or list(CATEGORIES)
    report = {"header": header(c), "summary": {}, "categories": {}}
    qs, cs = c.quest_scope(), c.creature_scope()
    report["header"]["scope"]["quests_in_both"] = len(qs)
    report["header"]["scope"]["quests_in_scope"] = sum(1 for v in qs.values() if v[0])
    report["header"]["scope"]["quest_exclusions"] = dict(Counter(
        v[1].split(" (")[0].split(" on map")[0] if not v[0] else "in-scope" for v in qs.values()))
    report["header"]["scope"]["creatures_in_both"] = len(cs)
    report["header"]["scope"]["creature_scope"] = dict(Counter(
        "outdoor" if v[0] else ("excluded:" + v[1].split(" ")[0] if v[0] is False
                                else "unknown:" + v[1].split(";")[0]) for v in cs.values()))
    for cat in cats:
        rows = CATEGORIES[cat](c)
        report["categories"][cat] = rows
        st = Counter(r["status"] for r in rows)
        mech = Counter(r["mechanism"] for r in rows if r["status"] == "change")
        report["summary"][cat] = {"rows": len(rows), "change": st.get("change", 0),
                                  "needs_review": st.get("needs-review", 0),
                                  "by_mechanism": dict(mech)}
        write(cat, rows, args.out)
        print(f"{cat:22s} change={st.get('change', 0):5d} needs-review={st.get('needs-review', 0):5d}")
    (args.out / "report.json").write_text(json.dumps(report, indent=1, default=str))
    (args.out / "summary.json").write_text(json.dumps(
        {"header": report["header"], "summary": report["summary"]}, indent=1, default=str))
    print(f"wrote {args.out}/report.json, summary.json, <category>.csv")


if __name__ == "__main__":
    main()
