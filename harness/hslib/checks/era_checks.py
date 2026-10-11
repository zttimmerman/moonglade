"""era-profile (openspec/changes/m3-vanilla-mode): vanilla mode for levels 1-60.

Expected values come from the source data at check time (hslib/era.py), observed values from the running server
through the probe (our own update fields, SMSG_LOG_XPGAIN, creature/item queries, the trainer window, `.npc info` as
the companion GM character). The checks use the default profile (m0), which sets no EraProfile override, so they test
the shipped default (vanilla on); era.toggle_roundtrip and era.scope_guard also restart with the `wotlk` profile
(EraProfile.Enable = 0) and back. Counterexample: the `wotlk` profile must make the vanilla spot checks fail."""
import hashlib
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import config, db, era as E, paths as P, stack
from ..probe import client as probe
from ..registry import check, require

XP_LEVELS_VANILLA = (20, 40, 59)     # spec: these match the VMaNGOS snapshot
XP_LEVELS_STOCK = (10, 60)           # spec: these match AzerothCore
QUESTS_VANILLA = (10, 5944)          # spec: Scrimshank Redemption (L48), In Dreams (L60)


def _run(fn):
    try:
        return fn()
    except E.EraError as e:
        require(False, str(e), **e.evidence)
    except probe.ProbeError as e:
        require(False, f"probe: {e}", **e.info)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "modules/mod-era-profile/**",
    "tools/era-diff/**",
    "modules/mod-era-profile/conf/mod_era_profile.conf.dist#EraProfile",
    "harness/hslib/checks/era_checks.py",
    "harness/hslib/era.py",
)


@check("era.sql_deterministic", order=140, capability="era-profile", stack="up", layer="integration")
def sql_deterministic(ctx):
    """Two fresh `era_diff.py --emit-sql` runs write SQL byte-identical to each other and to the committed
    modules/mod-era-profile/data/sql/db-world/era_*.sql (deterministic, and the committed files are generated)."""
    committed = sorted(E.SQL_DIR.glob("era_*.sql"))
    ev = {"committed": {p.name: _sha(p) for p in committed}, "runs": []}
    require(committed, "no committed era_*.sql in the module", **ev)
    with tempfile.TemporaryDirectory(prefix="hs-era-sql-") as td:
        outs = []
        for i in (1, 2):
            d = Path(td) / f"run{i}"
            r = subprocess.run([sys.executable, str(E.ERA_DIFF), "--emit-sql", "--sql-dir", str(d)],
                               capture_output=True, text=True, timeout=300)
            ev["runs"].append({"cmd": f"python3 tools/era-diff/era_diff.py --emit-sql --sql-dir <tmp>/run{i}",
                               "exit": r.returncode, "stdout": r.stdout[-1500:], "stderr": r.stderr[-1500:]})
            require(r.returncode == 0, f"era_diff --emit-sql run {i} failed", **ev)
            outs.append({p.name: _sha(p) for p in sorted(d.glob("*.sql"))})
        ev["run1"], ev["run2"] = outs
        diff12 = sorted(n for n in set(outs[0]) | set(outs[1]) if outs[0].get(n) != outs[1].get(n))
        diffc = sorted(n for n in set(outs[0]) | set(ev["committed"]) if outs[0].get(n) != ev["committed"].get(n))
        ev |= {"differs_run1_run2": diff12, "differs_run_vs_committed": diffc}
        require(not diff12, f"two runs differ: {diff12}", **ev)
        require(not diffc, f"generated SQL differs from the committed files: {diffc}", **ev)
    return ev


@check("era.default_on", order=141, capability="era-profile", stack="up", layer="integration")
def default_on(ctx):
    """With the default profile (no EraProfile override; the .conf.dist says EraProfile.Enable = 1) the worldserver
    log says vanilla mode is ACTIVE, every applied table verified, and the XP needed at level 20 on the running
    server is the VMaNGOS value."""
    dist_val = E.rendered_option(E.CONF_DIST, "EraProfile.Enable")
    conf = P.LANE_ETC / "modules" / config.ERA_CONF
    prof = config.overrides(config.DEFAULT_PROFILE)
    lines = E.module_log_lines()
    tables = E.table_lines(lines)
    ev = {"profile": config.DEFAULT_PROFILE, "profile_era_overrides": prof.get("era", {}),
          "conf_dist_value": dist_val, "rendered_conf": str(conf), "rendered_value": E.rendered_option(conf, "EraProfile.Enable"),
          "module_log_lines": lines, "tables": tables}
    require(not prof.get("era"), "the default profile overrides EraProfile settings", **ev)
    require(dist_val == "1", "the module's .conf.dist does not default EraProfile.Enable to 1", **ev)
    require(ev["rendered_value"] == "1", "rendered mod_era_profile.conf does not have EraProfile.Enable = 1", **ev)
    ev["expected_tables"] = sorted(E.era_diff().BASELINE_COLS)
    ev["failed_lines"] = [l for l in lines if E.FAILED_RE.search(l)]
    require(not ev["failed_lines"], "the module logged a FAILED line (an apply or verify query failed)", **ev)
    require(any(E.ACTIVE_RE.search(l) for l in lines), "worldserver log has no 'Vanilla mode ACTIVE' line", **ev)
    require(sorted(tables) == ev["expected_tables"], "the module did not report a verified count line for every "
                                                     "table it applies", **ev)
    require(tables and all(t["mode"] == "vanilla" and t["differing"] == 0 and t["at_vanilla"] == t["side_rows"]
                           for t in tables.values()),
            "an applied table is not fully at its vanilla values", **ev)
    vm, ac = E.vm_xp_for_level(), E.ac_stock_xp_for_level()
    ev["xp_l20_expected_vmangos"], ev["xp_l20_stock_ac"] = vm[20], ac[20]
    with E.Session() as s:
        ev["xp_l20_observed"] = _run(lambda: s.set_level(20))
        s.set_level(E.F.LEVEL)
    require(ev["xp_l20_observed"]["next_level_xp"] == vm[20],
            f"XP needed at level 20 is {ev['xp_l20_observed']['next_level_xp']}, VMaNGOS says {vm[20]}", **ev)
    return ev


@check("era.xp_curve", order=142, capability="era-profile", stack="up", layer="integration")
def xp_curve(ctx):
    """XP needed for the next level, read from the running server (our PLAYER_NEXT_LEVEL_XP after `.character
    level`), equals VMaNGOS at levels 20, 40, 59 and AzerothCore's base data at levels 10 and 60."""
    vm, ac = E.vm_xp_for_level(), E.ac_stock_xp_for_level()
    expected = {L: ("vmangos", vm[L]) for L in XP_LEVELS_VANILLA} | {L: ("azerothcore", ac[L]) for L in XP_LEVELS_STOCK}
    ev = {"sources": {"vmangos": "VMaNGOS snapshot player_xp_for_level (era_diff.VM)",
                      "azerothcore": str(E.AC_XP_SQL.relative_to(P.ROOT))},
          "expected": {L: {"source": s, "xp": x, "vmangos": vm[L], "azerothcore": ac[L]} for L, (s, x) in expected.items()}}
    # the spot levels must tell the modes apart, or the check could not fail with vanilla mode off
    require(all(vm[L] != ac[L] for L in XP_LEVELS_VANILLA), "VMaNGOS and AC agree at a vanilla spot level", **ev)
    obs = {}
    with E.Session() as s:
        for L in sorted(expected):
            obs[L] = _run(lambda: s.set_level(L))
        s.set_level(E.F.LEVEL)
        ev["session"] = {k: s.ev.get(k) for k in ("login", "revive")}
    ev["observed"] = obs
    wrong = {L: {"expected": expected[L][1], "observed": obs[L]["next_level_xp"], "source": expected[L][0]}
             for L in expected if obs[L]["next_level_xp"] != expected[L][1]}
    ev["wrong"] = wrong
    require(not wrong, f"XP needed differs from the source at levels {sorted(wrong)}", **ev)
    return ev


@check("era.quest_xp", order=143, capability="era-profile", stack="up", layer="integration")
def quest_xp(ctx):
    """At each quest's level, completing and rewarding quest 10 and 5944 gives the VMaNGOS-derived XP and one
    needs-review quest (era-diff: quest level differs) gives AC's stock XP (SMSG_LOG_XPGAIN and PLAYER_XP)."""
    src = E.quest_sources()
    review = _run(lambda: E.pick_review_quest(src))
    side = E.side_table_quests()
    cases = []
    for q in QUESTS_VANILLA:
        a = src["ac"][q]
        cases.append({"quest": q, "title": a["LogTitle"], "level": a["QuestLevel"], "want": "vanilla",
                      "expected": E.vanilla_quest_xp(src, q), "stock": E.stock_quest_xp(src, q),
                      "vm_rewxp": src["vm"][q]["RewXP"], "vm_level": src["vm"][q]["QuestLevel"], "in_side_table": q in side})
    rq = review["quest"]
    cases.append({"quest": rq, "title": review["title"], "level": review["ac_level"], "want": "stock",
                  "expected": E.stock_quest_xp(src, rq), "stock": E.stock_quest_xp(src, rq),
                  "vm_rewxp": review["vm_rewxp"], "vm_level": review["vm_level"], "in_side_table": rq in side,
                  "selection": review})
    ev = {"quest_rate": E.quest_rate(), "cases": cases}
    for c in cases[:2]:
        require(c["vm_level"] == c["level"], f"quest {c['quest']}: VMaNGOS and AC quest levels differ", **ev)
        require(c["expected"] != c["stock"], f"quest {c['quest']}: vanilla and stock XP are equal (check could not "
                                             "tell the modes apart)", **ev)
    with E.Session() as s:
        for c in cases:
            c["run"] = _run(lambda: s.reward_quest(c["quest"], c["level"]))
        s.set_level(E.F.LEVEL)
    bad = [c["quest"] for c in cases
           if c["run"]["xp_gained"] != c["expected"] or c["run"]["player_xp_after"] != c["expected"]]
    ev["mismatch"] = [{"quest": c["quest"], "want": c["want"], "expected": c["expected"], "stock": c["stock"],
                       "xp_gained": c["run"]["xp_gained"], "player_xp_after": c["run"]["player_xp_after"]}
                      for c in cases if c["quest"] in bad]
    require(not bad, f"quest XP differs from the expected source value for quests {bad}", **ev)
    return ev




# ------------------------------------------------------------------------------------------------ elites (task 2.3)

ELITE_CREATURES = (678, 436, 623)    # spec: Mosh'Ogg Mauler, Blackrock Shadowcaster, Skeletal Miner
ELITE_RANKS = (1, 2, 3)              # elite, rare-elite, world boss (SharedDefines.h CreatureEliteType)


def _base_stock(table: str, key_cols: list[str], cols: list[str], key: tuple) -> dict:
    """One row's stock values from the pinned core's base SQL (E.stock_from_base_sql), independent of the module's
    baseline table and revert. Fails when the update SQL may have changed the row (no trustworthy stock value)."""
    st = E.stock_from_base_sql(table, key_cols, cols, {key})
    if key not in st["values"]:
        raise E.EraError(f"{table} {key}: no trustworthy base-SQL stock value",
                         guarded={"/".join(map(str, k)): f for k, f in st["guarded"].items()},
                         missing=st["missing"])
    return st["values"][key] | {"source": str((E.BASE_SQL_DIR / f"{table}.sql").relative_to(P.ROOT))}


def _stock_creature(entry: int) -> dict:
    """Stock rank/HealthModifier of a creature, from the pinned core's base SQL (not the module's baseline)."""
    r = _run(lambda: _base_stock("creature_template", ["entry"], ["rank", "HealthModifier"], (entry,)))
    return {"rank": int(r["rank"]), "HealthModifier": float(r["HealthModifier"]), "source": r["source"]}


def _template_shape(entry: int) -> dict:
    """Columns the module never writes that the HP formula needs (exp, unit_class), from the live template."""
    exp, cls = db.query(f"SELECT exp, unit_class FROM creature_template WHERE entry = {entry}", "world")[0]
    return {"exp": int(exp), "unit_class": int(cls)}


@check("era.elites", order=144, capability="era-profile", stack="up", layer="integration")
def elites(ctx):
    """On the running server, creatures 678, 436 and 623 have VMaNGOS's elite rank (creature query) and max HP =
    base HP x VMaNGOS HealthModifier at their spawned level (`.npc info`), and a map-571 creature is unchanged from
    the pinned core's stock data."""
    cases = []
    for e in ELITE_CREATURES:
        v = E.vm_creature(e)
        cases.append({"entry": e, "name": v["name"], "vm": {k: v[k] for k in ("rank", "health_multiplier", "unit_class",
                      "level_min", "level_max")}, "stock": _stock_creature(e), "shape": _template_shape(e)})
    ev = {"source": "VMaNGOS snapshot creature_template/creature_classlevelstats (era_diff.VM); AC "
                    "creature_classlevelstats; Rate.Creature.*.HP from worldserver.conf", "cases": cases}
    for c in cases:
        require(c["vm"]["rank"] in ELITE_RANKS, f"creature {c['entry']}: VMaNGOS rank {c['vm']['rank']} is not elite",
                **ev)
        require(c["vm"]["rank"] != c["stock"]["rank"], f"creature {c['entry']}: stock rank equals VMaNGOS's (check "
                                                       "could not tell the modes apart)", **ev)
    nr_cands = _run(lambda: E.northrend_candidates())
    ev["northrend_rule"] = E.NORTHREND_RULE
    with E.Session(gm=True) as s:
        for c in cases:
            c["creature_query"] = _run(lambda: s.ws.query_creature(c["entry"]))
            c["live"] = _run(lambda: E.inspect_spawn(s, c["entry"], E.spawn_rows(c["entry"])))
            L = c["live"]["npc_info"]["level"]
            ac_base = E.ac_base_hp(L, c["shape"]["unit_class"], c["shape"]["exp"])
            vm_base = E.vm_base_hp(L, c["vm"]["unit_class"])
            c["hp"] = {"level": L, "ac_basehp": ac_base, "vm_base_health": vm_base,
                       "expected_max": E.creature_max_hp(ac_base, c["vm"]["health_multiplier"], c["vm"]["rank"]),
                       "vmangos_hp": math.ceil(vm_base * c["vm"]["health_multiplier"]),
                       "stock_max": E.creature_max_hp(ac_base, c["stock"]["HealthModifier"], c["stock"]["rank"]),
                       "observed_max": c["live"]["npc_info"]["max_health"]}
        nr = None
        tried = []
        for cand in nr_cands:
            try:
                live = E.inspect_spawn(s, cand["entry"], E.spawn_rows(cand["entry"], {E.NORTHREND_MAP}), max_tries=2)
            except E.EraError as e:
                tried.append({"entry": cand["entry"], "error": str(e)})
                continue
            nr = cand | {"live": live, "creature_query": _run(lambda: s.ws.query_creature(cand["entry"]))}
            break
        ev["northrend_tried_before"] = tried
    require(nr is not None, "no map-571 candidate answered `.npc info`", **ev)
    b = nr["base_sql"]
    nr["live_db"] = dict(zip(("rank", "HealthModifier"), db.query(
        f"SELECT `rank`, HealthModifier FROM creature_template WHERE entry = {nr['entry']}", "world")[0]))
    nr["in_module_tables"] = {t: bool(db.scalar(f"SELECT COUNT(*) FROM {t} WHERE entry = {nr['entry']}", "world") != "0")
                              for t in ("era_creature_template", "era_baseline_creature_template")}
    L = nr["live"]["npc_info"]["level"]
    nr["hp"] = {"level": L, "ac_basehp": E.ac_base_hp(L, int(b["unit_class"]), int(b["exp"])),
                "observed_max": nr["live"]["npc_info"]["max_health"]}
    nr["hp"]["expected_max"] = E.creature_max_hp(nr["hp"]["ac_basehp"], float(b["HealthModifier"]), int(b["rank"]))
    ev["northrend"] = nr
    bad = []
    for c in cases:
        q, hp = c["creature_query"], c["hp"]
        if hp["ac_basehp"] != hp["vm_base_health"]:
            bad.append(f"{c['entry']}: base HP tables disagree at level {hp['level']}")
        if q["rank"] != c["vm"]["rank"]:
            bad.append(f"{c['entry']}: rank {q['rank']}, VMaNGOS {c['vm']['rank']}")
        if abs(q["health_modifier"] - E.f32(c["vm"]["health_multiplier"])) > 1e-4:
            bad.append(f"{c['entry']}: HealthModifier {q['health_modifier']}, VMaNGOS {c['vm']['health_multiplier']}")
        if hp["observed_max"] != hp["expected_max"]:
            bad.append(f"{c['entry']}: max HP {hp['observed_max']} at level {hp['level']}, expected "
                       f"{hp['expected_max']} (stock would be {hp['stock_max']})")
        if hp["expected_max"] == hp["stock_max"]:
            bad.append(f"{c['entry']}: vanilla and stock max HP are equal at level {hp['level']}")
    if any(nr["in_module_tables"].values()):
        bad.append(f"map-571 creature {nr['entry']} has rows in the module's tables: {nr['in_module_tables']}")
    if int(nr["live_db"]["rank"]) != int(b["rank"]) or \
            abs(float(nr["live_db"]["HealthModifier"]) - float(b["HealthModifier"])) > 1e-6:
        bad.append(f"map-571 creature {nr['entry']}: world DB row {nr['live_db']} differs from stock {b}")
    if nr["creature_query"]["rank"] != int(b["rank"]):
        bad.append(f"map-571 creature {nr['entry']}: rank {nr['creature_query']['rank']}, stock {b['rank']}")
    if abs(nr["creature_query"]["health_modifier"] - E.f32(float(b["HealthModifier"]))) > 1e-4:
        bad.append(f"map-571 creature {nr['entry']}: HealthModifier {nr['creature_query']['health_modifier']}, "
                   f"stock {b['HealthModifier']}")
    if nr["hp"]["observed_max"] != nr["hp"]["expected_max"]:
        bad.append(f"map-571 creature {nr['entry']}: max HP {nr['hp']['observed_max']}, stock formula "
                   f"{nr['hp']['expected_max']}")
    ev["mismatch"] = bad
    require(not bad, "; ".join(bad), **ev)
    return ev


# ------------------------------------------------------------------------------------------------ mounts (task 2.4)

MOUNT_ITEMS = (2411, 18766)          # spec: Black Stallion Bridle, Swift Frostsaber
RIDING_SPELLS = (33388, 33391)       # spec: Apprentice / Journeyman riding (AC trainer spell ids)
UNCHANGED_RIDING = ("Expert Riding", "Artisan Riding", "Cold Weather Flying")   # spec: unchanged from stock


def _stock_item(entry: int) -> dict:
    """Stock level/riding rank/price of an item, from the pinned core's base SQL (not the module's baseline)."""
    cols = ["RequiredLevel", "RequiredSkillRank", "BuyPrice"]
    r = _run(lambda: _base_stock("item_template", ["entry"], cols, (entry,)))
    return {c: int(r[c]) for c in cols} | {"source": r["source"]}


def vm_riding() -> dict:
    """{AC spell: {"ReqLevel", "MoneyCost", "vm_trainer"}} for the riding spells, from era-diff's riding category
    (VMaNGOS npc_trainer(_template) at build 5875 matched to AC trainer spells, era_diff.cat_riding_training)."""
    ed = E.era_diff()
    out = {}
    for r in ed.cat_riding_training(ed.Ctx()):
        if r["status"] != "change":
            continue
        for sp in r["ac_spell_ids"]:
            out.setdefault(sp, {})[r["field"]] = r["vmangos"]
            out[sp]["vm_trainer"] = r["vm_trainer"]
            out[sp]["vm_spell"] = r["entry"]
    return out


@check("era.mounts", order=145, capability="era-profile", stack="up", layer="integration")
def mounts(ctx):
    """On the running server, items 2411 and 18766 show VMaNGOS's required level and price (item query), riding
    33388/33391 show VMaNGOS's level and cost in a riding trainer's window (and in every trainer_spell row), and
    Expert/Artisan/Cold Weather riding in the same window are unchanged from the pinned core's stock data."""
    items = []
    for e in MOUNT_ITEMS:
        v = E.vm_item(e)
        items.append({"entry": e, "name": v["name"], "vm": {k: v[k] for k in ("required_level", "required_skill_rank",
                      "buy_price")}, "stock": _stock_item(e)})
    vmr = vm_riding()
    ev = {"source": "VMaNGOS snapshot item_template / npc_trainer(_template) via era_diff; stock riding from "
                    "vendor/azerothcore-wotlk/data/sql/base/db_world/trainer_spell.sql", "items": items,
          "vm_riding": vmr}
    for it in items:
        require(it["vm"]["required_level"] != it["stock"]["RequiredLevel"] and it["vm"]["buy_price"] != it["stock"]["BuyPrice"],
                f"item {it['entry']}: VMaNGOS level/price equal stock (check could not tell the modes apart)", **ev)
    for sp in RIDING_SPELLS:
        require(sp in vmr and "ReqLevel" in vmr[sp] and "MoneyCost" in vmr[sp],
                f"era-diff has no VMaNGOS level and cost for riding spell {sp}", **ev)
    tr = _run(lambda: E.riding_trainer(RIDING_SPELLS, UNCHANGED_RIDING))
    ev["trainer"] = {k: v for k, v in tr.items() if k != "spawns"}
    unchanged = sorted(tr["unchanged_spells"])
    keys = {(tr["trainer"], sp) for sp in unchanged}
    stock = E.ac_base_rows("trainer_spell", ["TrainerId", "SpellId"], keys)
    ev["unchanged_stock"] = {f"{t}/{sp}": {"ReqLevel": int(r["ReqLevel"]), "MoneyCost": int(r["MoneyCost"])}
                             for (t, sp), r in stock.items()}
    ev["unchanged_update_mentions"] = E.update_sql_mentions("trainer_spell", unchanged)
    side = E.side_keys("era_trainer_spell", "TrainerId, SpellId")
    require(len(stock) == len(keys), "unchanged riding spells missing from the base SQL", **ev)
    require(not any(ev["unchanged_update_mentions"].values()), "a world-DB update touches an unchanged riding spell; "
                                                                "its base-SQL value may not be stock", **ev)
    require(not (keys & side), "an Expert/Artisan/Cold Weather row is in era_trainer_spell", **ev)
    # every trainer row of the two riding spells holds the VMaNGOS values (all trainers, not just the one we open)
    rows = db.query(f"SELECT TrainerId, SpellId, ReqLevel, MoneyCost FROM trainer_spell WHERE SpellId IN "
                    f"({','.join(map(str, RIDING_SPELLS))}) ORDER BY TrainerId, SpellId", "world")
    ev["riding_rows"] = rows
    wrong_rows = [r for r in rows if int(r[2]) != vmr[int(r[1])]["ReqLevel"] or int(r[3]) != vmr[int(r[1])]["MoneyCost"]]
    with E.Session(gm=True) as s:
        for it in items:
            it["item_query"] = _run(lambda: s.ws.query_item(it["entry"]))
        live = _run(lambda: E.inspect_spawn(s, tr["creature"], tr["spawns"]))
        ev["trainer_live"] = live
        ev["discount"] = _run(lambda: E.price_discount(s.char, tr["faction_template"]))
        tl = _run(lambda: s.ws.trainer_list(live["npc_info"]["guid"]))
    ev["trainer_list"] = tl
    shown = {x["spell"]: x for x in tl["spells"]}
    disc = ev["discount"]["discount"]
    bad = []
    for it in items:
        q = it["item_query"]
        if q["required_level"] != it["vm"]["required_level"] or q["buy_price"] != it["vm"]["buy_price"]:
            bad.append(f"item {it['entry']}: level {q['required_level']} price {q['buy_price']}, VMaNGOS "
                       f"{it['vm']['required_level']} / {it['vm']['buy_price']}")
        if q["required_skill_rank"] != it["vm"]["required_skill_rank"]:
            bad.append(f"item {it['entry']}: riding rank {q['required_skill_rank']}, VMaNGOS {it['vm']['required_skill_rank']}")
    for sp in RIDING_SPELLS:
        x = shown.get(sp)
        want = {"req_level": vmr[sp]["ReqLevel"], "money_cost": E.discounted(vmr[sp]["MoneyCost"], disc)}
        if not x or x["req_level"] != want["req_level"] or x["money_cost"] != want["money_cost"]:
            bad.append(f"spell {sp}: shown {x and {k: x[k] for k in want}}, VMaNGOS {want}")
    for (t, sp), r in stock.items():
        x = shown.get(sp)
        want = {"req_level": int(r["ReqLevel"]), "money_cost": E.discounted(int(r["MoneyCost"]), disc)}
        if not x or x["req_level"] != want["req_level"] or x["money_cost"] != want["money_cost"]:
            bad.append(f"spell {sp} ({tr['unchanged_spells'][sp]}): shown {x and {k: x[k] for k in want}}, stock {want}")
    if wrong_rows:
        bad.append(f"trainer_spell rows without the VMaNGOS values: {wrong_rows}")
    ev["mismatch"] = bad
    require(not bad, "; ".join(bad), **ev)
    return ev


# ------------------------------------------------------------------------------------------------ toggle (task 2.5)

WOTLK_PROFILE = "wotlk"              # config.PROFILES: m0 + EraProfile.Enable = 0
TOGGLE_LEVEL = 48                    # quest 10's level, so rewarding it gives the character XP
TOGGLE_REWARDED = QUESTS_VANILLA[0]  # quest 10 (spec spot quest; no reward items, so inventory stays bounded)
TOGGLE_IN_LOG = E.F.QUEST_ID         # the fixture quest, left incomplete in the quest log
SPOT_ITEM, SPOT_CREATURE, SPOT_XP_LEVEL = MOUNT_ITEMS[0], ELITE_CREATURES[0], 20


def char_snapshot(guid: int) -> dict:
    """The character's saved level, XP, inventory (bag, slot, item entry, count) and quest status (log rows and
    rewarded quests) from the characters DB; money is recorded as extra evidence."""
    lvl, xp, money, online = db.query(f"SELECT level, xp, money, online FROM characters WHERE guid = {guid}",
                                      "characters")[0]
    return {
        "level": int(lvl), "xp": int(xp),
        "inventory": db.query("SELECT ci.bag, ci.slot, ii.itemEntry, ii.count FROM character_inventory ci JOIN "
                              f"item_instance ii ON ii.guid = ci.item WHERE ci.guid = {guid} ORDER BY ci.bag, ci.slot",
                              "characters"),
        "quest_status": db.query("SELECT quest, status, explored, mobcount1, mobcount2, mobcount3, mobcount4, itemcount1, "
                                 "itemcount2, itemcount3, itemcount4, itemcount5, itemcount6, playercount FROM "
                                 f"character_queststatus WHERE guid = {guid} ORDER BY quest", "characters"),
        "quest_rewarded": db.query(f"SELECT quest, active FROM character_queststatus_rewarded WHERE guid = {guid} "
                                   "ORDER BY quest", "characters"),
        "money": int(money), "online": int(online)}


SNAP_FIELDS = ("level", "xp", "inventory", "quest_status", "quest_rewarded")   # spec: level, XP, inventory, quests


def _set_known_state(src: dict) -> dict:
    """HSTEST with vanilla mode on: level TOGGLE_LEVEL, quest 10 rewarded (its XP is the character's XP), the fixture
    quest in the log, incomplete. Deterministic on every run (`.character level` zeroes XP; both quests are
    removed first)."""
    ev = {}
    with E.Session() as s:
        ev["level"] = s.set_level(TOGGLE_LEVEL)
        for q in (TOGGLE_REWARDED, TOGGLE_IN_LOG):
            E.gm(f"quest remove {q} {s.char}", ok_required=False)
        E.gm(f"quest add {TOGGLE_REWARDED} {s.char}")
        E.gm(f"quest complete {TOGGLE_REWARDED} {s.char}")
        since = s.ws.mark()
        E.gm(f"quest reward {TOGGLE_REWARDED} {s.char}")
        g = s.ws.wait_event(lambda e: e["kind"] == "xp_gain", since, E.XP_GAIN_S)
        ev["xp_gain"] = g
        E.gm(f"quest add {TOGGLE_IN_LOG} {s.char}")
        guid = s.guid
    ev["session"] = s.ev
    want_xp = E.vanilla_quest_xp(src, TOGGLE_REWARDED)
    snap, ok, dt = E.F.poll(lambda: char_snapshot(guid), lambda v: v["online"] == 0 and v["level"] == TOGGLE_LEVEL
                            and v["xp"] == want_xp and any(r[:2] == [str(TOGGLE_IN_LOG), "3"] for r in v["quest_status"])
                            and [str(TOGGLE_REWARDED), "1"] in v["quest_rewarded"], timeout=30)
    ev |= {"expected": {"level": TOGGLE_LEVEL, "xp": want_xp, "rewarded": TOGGLE_REWARDED, "in_log_incomplete":
                        TOGGLE_IN_LOG}, "snapshot": snap, "saved": ok, "wait_s": dt}
    if not ok:
        raise E.EraError("the known character state was not saved", **ev)
    return ev


def _reset_toggle_state() -> dict:
    """After the comparison: back to the fixture level with both quests removed, as the other era checks leave it."""
    with E.Session() as s:
        for q in (TOGGLE_REWARDED, TOGGLE_IN_LOG):
            E.gm(f"quest remove {q} {s.char}", ok_required=False)
        return {"level": s.set_level(E.F.LEVEL)}


def mode_expectations(src: dict) -> dict:
    """Spot values each mode must show, from the sources: XP needed at level 20, quest 10's XP, item 2411's level and
    price, creature 678's rank. vanilla = VMaNGOS; stock = the pinned core's base SQL / QuestXP.dbc (never the
    module's own baseline table, so a wrong revert shows)."""
    vm_xp, ac_xp = E.vm_xp_for_level(), E.ac_stock_xp_for_level()
    vi, si = E.vm_item(SPOT_ITEM), _stock_item(SPOT_ITEM)
    vc, sc = E.vm_creature(SPOT_CREATURE), _stock_creature(SPOT_CREATURE)
    exp = {"vanilla": {"xp_l20": vm_xp[SPOT_XP_LEVEL], "quest10_xp": E.vanilla_quest_xp(src, TOGGLE_REWARDED),
                       "item_level": vi["required_level"], "item_price": vi["buy_price"], "creature_rank": vc["rank"]},
           "stock": {"xp_l20": ac_xp[SPOT_XP_LEVEL], "quest10_xp": E.stock_quest_xp(src, TOGGLE_REWARDED),
                     "item_level": si["RequiredLevel"], "item_price": si["BuyPrice"], "creature_rank": sc["rank"]}}
    same = [k for k in exp["vanilla"] if exp["vanilla"][k] == exp["stock"][k]]
    if same:
        raise E.EraError(f"spot values identical in both modes: {same}", expectations=exp)
    return exp


def observe_spots() -> dict:
    """The spot values on the running server, read by the GM character (not the toggled character): our
    PLAYER_NEXT_LEVEL_XP at level 20, SMSG_LOG_XPGAIN for quest 10, the item and creature query responses."""
    with E.Session(gm=True) as s:
        start_level = s.level
        xp = s.set_level(SPOT_XP_LEVEL)
        q = s.reward_quest(TOGGLE_REWARDED, TOGGLE_LEVEL)
        it = s.ws.query_item(SPOT_ITEM)
        cq = s.ws.query_creature(SPOT_CREATURE)
        s.set_level(start_level)
    return {"xp_l20": xp["next_level_xp"], "quest10_xp": q["xp_gained"], "item_level": it["required_level"],
            "item_price": it["buy_price"], "creature_rank": cq["rank"]}


def _phase(name: str, want: str, exp: dict, guid: int) -> dict:
    """One phase: the module's startup lines, the spot values against the mode's expectations, the character."""
    lines = E.module_log_lines()
    tables = E.table_lines(lines)
    obs = _run(observe_spots)
    return {"phase": name, "mode": want, "running_profile": stack.running_profile(), "module_log_lines": lines,
            "tables": tables, "observed": obs, "expected": exp[want],
            "spot_mismatch": {k: {"observed": obs[k], "expected": exp[want][k]} for k in obs if obs[k] != exp[want][k]},
            "log_ok": (any(E.ACTIVE_RE.search(l) for l in lines) if want == "vanilla" else
                       any(E.OFF_RE.search(l) for l in lines))
                      and bool(tables) and all(t["mode"] == ("vanilla" if want == "vanilla" else "stock")
                                               and t["differing"] == 0 for t in tables.values()),
            "character": char_snapshot(guid)}


# era.toggle_roundtrip: a scenario of era.session_toggle (test-pyramid part 2: one off/on restart pair for both)
def toggle_roundtrip(ctx):
    """With the test character at a known level, XP and quest state (vanilla on), restarting with EraProfile.Enable = 0
    shows the stock spot values and restarting with 1 shows the vanilla ones again, while the character's level, XP,
    inventory and quest status stay identical through both switches."""
    src = E.quest_sources()
    exp = _run(lambda: mode_expectations(src))
    ev = {"expectations": exp, "known_state": _run(lambda: _set_known_state(src))}
    guid = E.F.char_guid()
    phases = [_phase("before (vanilla on)", "vanilla", exp, guid)]
    ev["to_wotlk"] = yield WOTLK_PROFILE
    phases.append(_phase(f"after restart with profile {WOTLK_PROFILE} (EraProfile.Enable = 0)", "stock", exp, guid))
    ev["to_vanilla"] = yield config.DEFAULT_PROFILE
    phases.append(_phase(f"after restart with profile {config.DEFAULT_PROFILE} (EraProfile.Enable = 1)", "vanilla",
                         exp, guid))
    ev["phases"] = phases
    base = ev["known_state"]["snapshot"]
    drift = [{"phase": p["phase"], "field": f, "before": base[f], "now": p["character"][f]}
             for p in phases for f in SNAP_FIELDS if p["character"][f] != base[f]]
    ev["character_drift"] = drift
    ev["cleanup"] = _run(_reset_toggle_state)
    bad_log = [p["phase"] for p in phases if not p["log_ok"]]
    bad_spot = {p["phase"]: p["spot_mismatch"] for p in phases if p["spot_mismatch"]}
    require(not bad_log, f"module startup log does not show the expected mode in: {bad_log}", **ev)
    require(not bad_spot, f"spot values differ from the mode's expectations: {bad_spot}", **ev)
    require(not drift, f"character changed across the switches: {[(d['phase'], d['field']) for d in drift]}", **ev)
    return ev


# ------------------------------------------------------------------------------------------------ scope guard (2.5)

def report_scope() -> dict:
    """In-scope rows per touched table and the values vanilla mode must give them, from era-diff's report rows (the
    same category functions that write tools/era-diff/out/report.json): rank from outdoor_elite, HealthModifier from
    elite_hp (VMaNGOS multiplier verbatim for VMaNGOS elite ranks, design.md "Elite HP scope"), so the expected
    counts follow era-diff. quest_template has no in-scope rows: quest XP is a runtime hook."""
    ed = E.era_diff()
    c = ed.Ctx()
    out = {t: {} for t in ("player_xp_for_level", "creature_template", "item_template", "trainer_spell",
                           "quest_template")}
    used = {}
    for r in ed.cat_xp_curve(c):
        if r["status"] == "change":
            out["player_xp_for_level"].setdefault((int(r["entry"]),), {})["Experience"] = r["proposed"]
            used["xp_curve"] = used.get("xp_curve", 0) + 1
    for r in ed.cat_outdoor_elite(c):
        if r["status"] == "change":
            out["creature_template"].setdefault((r["entry"],), {})["rank"] = r["proposed"]
            used["outdoor_elite rank"] = used.get("outdoor_elite rank", 0) + 1
    elite_hp_review = []
    for r in ed.cat_elite_hp(c):
        if r["status"] == "change":
            out["creature_template"].setdefault((r["entry"],), {})["HealthModifier"] = float(r["proposed"])
            used["elite_hp HealthModifier"] = used.get("elite_hp HealthModifier", 0) + 1
        else:
            elite_hp_review.append({"entry": r["entry"], "name": r["name"], "reason": r["reason"]})
    for r in ed.cat_mount_items(c):
        if r["status"] == "change":
            out["item_template"].setdefault((r["entry"],), {})[r["field"]] = r["proposed"]
            used["mount_items"] = used.get("mount_items", 0) + 1
    live = {(a["TrainerId"], a["SpellId"]) for a in c.ac_trainer_spells()}
    for r in ed.cat_riding_training(c):
        if r["status"] == "change":
            used["riding_training"] = used.get("riding_training", 0) + 1
            for t in r["ac_trainers"]:
                for sp in r["ac_spell_ids"]:
                    if (t, sp) in live:
                        out["trainer_spell"].setdefault((t, sp), {})[r["field"]] = r["proposed"]
    return {"rows": out, "report_rows_used": used, "elite_hp_needs_review": elite_hp_review, "ctx": c}


SCOPE_KEYS = {"player_xp_for_level": ["Level"], "creature_template": ["entry"], "item_template": ["entry"],
              "trainer_spell": ["TrainerId", "SpellId"], "quest_template": ["ID"]}


def table_hashes(table: str, keys: list[str], era_cols: list[str]) -> dict[tuple, tuple]:
    """{key: (md5 of the whole row, md5 of the row without the module's columns, module column values)} for every row
    of a world table, hashed by MySQL (every column, NULL kept distinct)."""
    cols = [r[0] for r in db.query("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
                                   f"AND TABLE_NAME = '{table}' ORDER BY ORDINAL_POSITION", "world")]
    h = lambda cs: "MD5(CONCAT_WS(0x1f, " + ", ".join(f"IFNULL(CAST(`{x}` AS CHAR), 0x00)" for x in cs) + "))"  # noqa
    rest = [x for x in cols if x not in era_cols]
    extra = "".join(f", `{x}`" for x in era_cols)
    rows = db.query(f"SELECT {', '.join(f'`{k}`' for k in keys)}, {h(cols)}, {h(rest)}{extra} FROM `{table}`", "world",
                    timeout=300)
    n = len(keys)
    return {tuple(int(x) for x in r[:n]): (r[n], r[n + 1], tuple(r[n + 2:])) for r in rows}


def _digest(rows: dict, keys) -> str:
    m = hashlib.sha256()
    for k in sorted(keys):
        m.update(f"{k}:{rows[k][0] if k in rows else '-'}\n".encode())
    return m.hexdigest()


def _same(a: str, b) -> bool:
    try:
        return abs(float(a) - float(b)) <= 1e-4
    except (TypeError, ValueError):
        return str(a) == str(b)


def off_vs_base_sql(off: dict, scope_rows: dict, era_cols: dict) -> dict:
    """Independent stock reference for the vanilla-off phase: every in-scope row's module columns, as the live world
    DB holds them with EraProfile.Enable = 0 (the module's revert), must equal the pinned core's base SQL
    (E.stock_from_base_sql), not the module's own baseline table. Rows the update SQL may have changed are guarded
    (listed with their files, not compared)."""
    out = {}
    for t, rows in scope_rows.items():
        if not rows:
            continue
        keys = set(rows)
        st = E.stock_from_base_sql(t, SCOPE_KEYS[t], era_cols[t], keys)
        wrong = []
        for k, base in sorted(st["values"].items()):
            if k not in off[t]:
                wrong.append({"key": k, "off": None, "base_sql": base})
                continue
            live = dict(zip(era_cols[t], off[t][k][2]))
            if any(not _same(live[c], base[c]) for c in era_cols[t]):
                wrong.append({"key": k, "off": live, "base_sql": base})
        out[t] = {"in_scope": len(keys), "compared": len(st["values"]),
                  "guarded": {"/".join(map(str, k)): f for k, f in sorted(st["guarded"].items())},
                  "missing_from_base_sql": st["missing"], "wrong": wrong}
    return out


def elite_stock_hp(c, off: dict, on: dict) -> dict:
    """Elite-HP rule, checked on the live rows: every era-diff in-scope creature (creature_scope: outdoor, 1-60) that
    has an elite rank (1-3) with vanilla mode on and an elite VMaNGOS rank must have VMaNGOS's HealthModifier
    (verbatim), so no creature is elite in vanilla mode with its stock HP where VMaNGOS's differs. Exempt, and listed:
    creatures whose live `exp` is not 0 (AC HP base basehp1/2, which VMaNGOS's multiplier does not describe; era-diff
    cat_elite_hp marks them needs-review)."""
    vm = c.vm_creatures()
    exp = {int(e): int(x) for e, x in db.query("SELECT entry, exp FROM creature_template", "world")}
    viol, exempt, checked = [], [], 0
    for e, (ok, _why) in sorted(c.creature_scope().items()):
        k = (e,)
        if ok is not True or k not in on["creature_template"] or vm[e]["rank"] not in ELITE_RANKS:
            continue
        rank_on, hm_on = on["creature_template"][k][2]
        if int(rank_on) not in ELITE_RANKS:
            continue
        checked += 1
        hm_off = off["creature_template"][k][2][1] if k in off["creature_template"] else None
        if _same(hm_on, vm[e]["health_multiplier"]):
            continue
        row = {"entry": e, "rank_on": int(rank_on), "HealthModifier_on": hm_on, "HealthModifier_off": hm_off,
               "vmangos": vm[e]["health_multiplier"], "exp": exp.get(e), "stock_hp": _same(hm_on, hm_off)}
        (exempt if exp.get(e) != 0 else viol).append(row)
    return {"rule": elite_stock_hp.__doc__.split("\n")[0], "elite_in_scope_checked": checked,
            "violations": viol, "exempt_exp_not_0": exempt}


# era.scope_guard: a scenario of era.session_toggle (test-pyramid part 2: one off/on restart pair for both)
def scope_guard(ctx):
    """Every out-of-scope row of creature_template, quest_template, item_template, trainer_spell and
    player_xp_for_level hashes identically with vanilla mode off and on; the rows that change are exactly the
    era-diff report's in-scope rows (count and keys, elite-HP scope applied), only in the module's columns, to the
    report's values. With vanilla off the in-scope rows equal the pinned core's base SQL (independent of the module's
    baseline), and with vanilla on no in-scope elite keeps a HealthModifier other than VMaNGOS's."""
    ed = E.era_diff()
    scope = report_scope()
    era_cols = {t: [x for x, _ in ed.BASELINE_COLS.get(t, [])] for t in SCOPE_KEYS}
    ev = {"report": {"rows_used": scope["report_rows_used"],
                     "elite_hp_needs_review (stay stock)": scope["elite_hp_needs_review"],
                     "expected_changed_rows": {t: len(v) for t, v in scope["rows"].items()}},
          "module_columns": era_cols}
    ev["to_off"] = yield WOTLK_PROFILE
    ev["off_log"] = E.module_log_lines()
    off = {t: table_hashes(t, k, era_cols[t]) for t, k in SCOPE_KEYS.items()}
    ev["to_on"] = yield config.DEFAULT_PROFILE
    ev["on_log"] = E.module_log_lines()
    on = {t: table_hashes(t, k, era_cols[t]) for t, k in SCOPE_KEYS.items()}
    tables, bad = {}, []
    for t in SCOPE_KEYS:
        exp = scope["rows"][t]
        keys = set(off[t]) | set(on[t])
        out_keys = keys - set(exp)
        out_diff = sorted(k for k in out_keys if off[t].get(k, (None,))[0] != on[t].get(k, (None,))[0])
        changed = sorted(k for k in keys if off[t].get(k, (None,))[0] != on[t].get(k, (None,))[0])
        in_other_cols = sorted(k for k in exp if k in off[t] and k in on[t] and off[t][k][1] != on[t][k][1])
        wrong_vals, missing = [], sorted(k for k in exp if k not in on[t])
        for k, want in exp.items():
            if k not in on[t] or k not in off[t]:
                continue
            for i, col in enumerate(era_cols[t]):
                target = want[col] if col in want else off[t][k][2][i]
                if not _same(on[t][k][2][i], target):
                    wrong_vals.append({"key": k, "column": col, "on": on[t][k][2][i], "want": target,
                                       "off": off[t][k][2][i]})
        tables[t] = {"rows_off": len(off[t]), "rows_on": len(on[t]), "out_of_scope_rows": len(out_keys),
                     "out_of_scope_digest_off": _digest(off[t], out_keys),
                     "out_of_scope_digest_on": _digest(on[t], out_keys),
                     "out_of_scope_changed": out_diff[:20], "out_of_scope_changed_count": len(out_diff),
                     "changed_rows": len(changed), "expected_changed_rows": len(exp),
                     "in_scope_not_changed": sorted(set(exp) - set(changed))[:20],
                     "in_scope_other_columns_changed": in_other_cols[:20], "in_scope_wrong_values": wrong_vals[:20],
                     "in_scope_missing": missing[:20]}
        if out_diff or len(off[t]) != len(on[t]):
            bad.append(f"{t}: {len(out_diff)} out-of-scope rows differ (rows off {len(off[t])}, on {len(on[t])})")
        if len(changed) != len(exp) or set(changed) != set(exp):
            bad.append(f"{t}: {len(changed)} rows changed, era-diff report says {len(exp)}")
        if in_other_cols or wrong_vals or missing:
            bad.append(f"{t}: in-scope rows changed outside the module's columns ({len(in_other_cols)}), hold wrong "
                       f"values ({len(wrong_vals)}) or are missing ({len(missing)})")
    ev["tables"] = tables
    ev["off_vs_base_sql"] = off_vs_base_sql(off, scope["rows"], era_cols)
    for t, r in ev["off_vs_base_sql"].items():
        if r["wrong"]:
            bad.append(f"{t}: {len(r['wrong'])} in-scope rows with vanilla off differ from the pinned base SQL "
                       f"(first {r['wrong'][0]})")
        if not r["compared"]:
            bad.append(f"{t}: no in-scope row could be compared with the base SQL")
    ev["elite_hp"] = elite_stock_hp(scope["ctx"], off, on)
    if ev["elite_hp"]["violations"]:
        bad.append(f"{len(ev['elite_hp']['violations'])} in-scope creatures are elite with vanilla on but lack "
                   f"VMaNGOS's HealthModifier: {[v['entry'] for v in ev['elite_hp']['violations']][:20]}")
    ev["mismatch"] = bad
    require(any(E.OFF_RE.search(l) for l in ev["off_log"]), "vanilla mode did not report OFF for the off snapshot", **ev)
    require(any(E.ACTIVE_RE.search(l) for l in ev["on_log"]), "vanilla mode did not report ACTIVE for the on snapshot",
            **ev)
    require(not bad, "; ".join(bad), **ev)
    return ev


# ------------------------------------------------------------------------------------------------ one restart pair
# test-pyramid part 2 (design 6, "era.toggle_roundtrip and era.scope_guard share their off/on restarts (4 -> 2)"):
# both scenarios are generators that yield the profile they need next; the session switches once per step and hands
# the restart's record to each, so each still observes the world after a real restart with that profile.

def drive_together(gens: dict) -> dict:
    """Run generator scenarios in lockstep: at each step every live one asks for the same profile; one
    stack.ensure_profile per step. Returns {id: {"status", "evidence"|"message"}}."""
    from ..registry import CheckFail
    import time as _t
    import traceback as _tb
    out, want, t0 = {}, {}, _t.time()

    def step(i, g, val, first=False):
        try:
            want[i] = next(g) if first else g.send(val)
        except StopIteration as e:
            want.pop(i, None)
            out[i] = {"status": "pass", "evidence": e.value or {}}
        except CheckFail as e:
            want.pop(i, None)
            out[i] = {"status": "fail", "message": str(e)[:500], "evidence": e.evidence or {}}
        except Exception as e:  # noqa: BLE001
            want.pop(i, None)
            out[i] = {"status": "error", "message": f"{type(e).__name__}: {e}"[:500],
                      "evidence": {"traceback": _tb.format_exc()[-3000:]}}
    for i, g in gens.items():
        step(i, g, None, first=True)
    switches = []
    while want:
        prof = sorted(set(want.values()))
        if len(prof) != 1:
            for i in list(want):
                out[i] = {"status": "error", "message": f"the scenarios asked for different profiles: {want}",
                          "evidence": {}}
                want.pop(i)
            break
        rec = stack.ensure_profile(prof[0])
        switches.append({"profile": prof[0], "at_s": round(_t.time() - t0, 1)})
        for i in list(want):
            step(i, gens[i], rec)
    for v in out.values():
        v["duration_s"] = round(_t.time() - t0, 1)
        v.setdefault("evidence", {})["shared_restarts"] = switches
    return out


@check("era.session_toggle", order=146, capability="era-profile", stack="up", layer="integration",
       children=("era.toggle_roundtrip", "era.scope_guard"))
def session_toggle(ctx):
    """era.toggle_roundtrip and era.scope_guard on one off/on restart pair (each scenario its own record)."""
    return {"children": drive_together({"era.toggle_roundtrip": toggle_roundtrip(ctx),
                                        "era.scope_guard": scope_guard(ctx)})}
