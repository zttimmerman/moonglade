"""era-profile (openspec/changes/m3-vanilla-mode): vanilla mode for levels 1-60.

Expected values come from the source data at check time (hslib/era.py), observed values from the running server
through the probe (our own update fields, SMSG_LOG_XPGAIN). The checks use the default profile (m0), which sets no
EraProfile override, so they test the shipped default (vanilla on). Counterexample: the `wotlk` profile
(EraProfile.Enable = 0) must make era.default_on, era.xp_curve and era.quest_xp fail."""
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import config, era as E, paths as P
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


@check("era.sql_deterministic", order=140, capability="era-profile", stack="up", tier="quick")
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


@check("era.default_on", order=141, capability="era-profile", stack="up", tier="quick")
def default_on(ctx):
    """With the default profile (no EraProfile override; the .conf.dist says EraProfile.Enable = 1) the worldserver
    log says vanilla mode is ACTIVE, every applied table verified, and the XP needed at level 20 on the running
    server is the VMaNGOS value."""
    dist_val = E.rendered_option(E.CONF_DIST, "EraProfile.Enable")
    conf = P.SERVER_ETC / "modules" / config.ERA_CONF
    prof = config.overrides(config.DEFAULT_PROFILE)
    lines = E.module_log_lines()
    tables = E.table_lines(lines)
    ev = {"profile": config.DEFAULT_PROFILE, "profile_era_overrides": prof.get("era", {}),
          "conf_dist_value": dist_val, "rendered_conf": str(conf), "rendered_value": E.rendered_option(conf, "EraProfile.Enable"),
          "module_log_lines": lines, "tables": tables}
    require(not prof.get("era"), "the default profile overrides EraProfile settings", **ev)
    require(dist_val == "1", "the module's .conf.dist does not default EraProfile.Enable to 1", **ev)
    require(ev["rendered_value"] == "1", "rendered mod_era_profile.conf does not have EraProfile.Enable = 1", **ev)
    require(any(E.ACTIVE_RE.search(l) for l in lines), "worldserver log has no 'Vanilla mode ACTIVE' line", **ev)
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


@check("era.xp_curve", order=142, capability="era-profile", stack="up", tier="quick")
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


@check("era.quest_xp", order=143, capability="era-profile", stack="up", tier="quick")
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


# ------------------------------------------------------------------------------------------------ not built yet
# Spec scenarios of later m3 tasks. Registered so report.coverage sees their ids; they FAIL until their task
# replaces them (a full run stays red until then). Not quick: a quick run doesn't cover scenarios.

def _not_yet(task: str):
    def fn(ctx):
        require(False, f"not implemented yet (openspec/changes/m3-vanilla-mode task {task})", task=task)
    return fn


for _id, _order, _task, _doc in (
        ("era.elites", 144, "2.3", "Creatures 678, 436, 623 have vanilla rank and HP; a map-571 creature is unchanged."),
        ("era.mounts", 145, "2.4", "Mount items 2411/18766 and riding 33388/33391 have vanilla levels and prices."),
        ("era.toggle_roundtrip", 146, "2.5", "Each mode shows its own values; the character is unchanged by switching."),
        ("era.scope_guard", 147, "2.5", "Out-of-scope rows are byte-identical to stock; changed rows = report count.")):
    _fn = _not_yet(_task)
    _fn.__doc__ = f"NOT IMPLEMENTED (task {_task}): {_doc}"
    check(_id, order=_order, capability="era-profile", stack="any", tier="full")(_fn)
