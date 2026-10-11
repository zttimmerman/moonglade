"""test-pyramid rule checks (openspec/changes/test-pyramid, capability harness-reporting): layers declared, the unit
layer, impact selection, ownership, gate rule, layer budgets, the per-PR average, the fixed journey set, and the
no-fixed-sleeps lint. The registered ones are unit-layer checks; the post-run records (unit_layer_record,
layer_budgets_record, pr_average_record, journeys_record) are produced by reporting.post_run over the run's own
results document. Every rule is also run against synthetic inputs that must fail (its counterexamples)."""
from __future__ import annotations

import ast
import json
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from .. import paths as P
from ..registry import LAYERS, REGISTRY, Check, check, require

CAP = "harness-reporting"
JOURNEY_SET = {"journey.first_play": 150, "journey.pause": 90, "journey.saves": 150, "journey.login_recovery": 120,
               "journey.stock_client": 60, "journey.app_desktop": 240}
JOURNEY_MAX_N, JOURNEY_MAX_S = 8, 840
GATE_REQUIRED = ("build.clean", "core.login_logout_race_baseline")
BUDGETS = {"unit": 120.0, "selection": 10.0, "integration": 900.0, "acceptance": 1800.0, "new_checks": 120.0}
PR_MEAN_MAX_MIN, PR_MIN_RUNS, PR_WINDOW = 15.0, 5, 10

COVERS = (
    "harness/hslib/checks/pyramid_checks.py",
    "harness/hslib/checks/reporting.py",
    "harness/hslib/selection.py",
    "harness/hslib/unitrun.py",
    "harness/hslib/unittools.py",
    "harness/hslib/luaprelude.py",
    "harness/hslib/registry.py",
    "harness/hslib/runner.py",
    "harness/tests/unit/**",
)


def _scratch(tag: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"hs-{tag}-")).resolve()


def _syn(cid, layer, covers=(), stack="any", budget=None, gui=False, fn=None, order=100):
    return Check(cid, fn or (lambda ctx: {"ok": 1}), order, "synthetic", stack, "", layer, None, gui, False,
                 tuple(covers), "exclusive", budget)


# ============================================================================ report.layers_declared

def layer_problems(reg: dict) -> list[str]:
    probs = []
    for i, c in sorted(reg.items()):
        if c.layer not in LAYERS:
            probs.append(f"{i}: layer {c.layer!r} isn't one of {LAYERS}")
        if c.layer == "journey" and i not in JOURNEY_SET:
            probs.append(f"{i}: journey layer outside the named journey set")
    for i in GATE_REQUIRED:
        if i in reg and reg[i].layer != "gate":
            probs.append(f"{i}: must be gate (is {reg[i].layer})")
        elif i not in reg:
            probs.append(f"{i}: not registered")
    return probs


@check("report.layers_declared", order=1, capability=CAP, stack="any", layer="unit", parallel_safe=True)
def layers_declared(ctx):
    """`hs list --json` shows every registered check with exactly one layer from {unit, integration, journey, gate};
    build.clean and core.login_logout_race_baseline are gate; no check outside the named journey set is a journey.
    Counterexamples: a scratch registry with an undeclared check and one with an unnamed journey both fail."""
    r = subprocess.run([str(P.HARNESS / "hs"), "list", "--json"], capture_output=True, text=True, timeout=120)
    require(r.returncode == 0, "`hs list --json` failed", exit=r.returncode, stderr=r.stderr[-1500:])
    listing = json.loads(r.stdout)
    ids = [c["id"] for c in listing]
    by_layer = {L: sorted(c["id"] for c in listing if c.get("layer") == L) for L in LAYERS}
    probs = layer_problems(REGISTRY)
    probs += [f"{c['id']}: listed with layer {c.get('layer')!r}" for c in listing if c.get("layer") not in LAYERS]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        probs.append(f"listed more than once: {dup}")
    ce = {"undeclared": layer_problems({"t.x": _syn("t.x", None), **{g: _syn(g, "gate") for g in GATE_REQUIRED}}),
          "unnamed_journey": layer_problems({"journey.extra": _syn("journey.extra", "journey"),
                                             **{g: _syn(g, "gate") for g in GATE_REQUIRED}}),
          "build_clean_not_gate": layer_problems({"build.clean": _syn("build.clean", "integration"),
                                                  "core.login_logout_race_baseline": _syn("x", "gate")})}
    for k, v in ce.items():
        if not v:
            probs.append(f"counterexample {k} wasn't caught")
    ev = {"cmd": "harness/hs list --json", "checks_listed": len(listing), "registered": len(REGISTRY),
          "by_layer": {k: len(v) for k, v in by_layer.items()}, "journeys": by_layer["journey"],
          "gate_required": {i: getattr(REGISTRY.get(i), "layer", None) for i in GATE_REQUIRED},
          "counterexamples": ce, "problems": probs}
    require(len(listing) == len(REGISTRY), "`hs list` count differs from the registry", **ev)
    require(not probs, f"{len(probs)} layer problem(s)", **ev)
    return ev


# ============================================================================ report.unit_layer (post-run)

def _scratch_suites(d: Path) -> dict:
    """One passing and one deliberately failing test per runner, in scratch copies of the test folders; plus the
    Lua runner with an altered pinned archive hash (a scratch cache holding a copy of the archive)."""
    from .. import downloads as D, unitrun, unittools as U
    out = {}
    py = d / "pytop" / "scratchunit"
    py.mkdir(parents=True)
    (py / "__init__.py").write_text("")
    (py / "test_scratch.py").write_text(
        "import unittest\n\nclass S(unittest.TestCase):\n    def test_passes(self):\n        self.assertEqual(1, 1)\n\n"
        "    def test_fails(self):\n        self.assertEqual(1, 2, 'deliberate')\n")
    out["python"] = unitrun.run_python(start=py, top=d / "pytop")
    cpp = d / "cpp"
    shutil.copytree(U.CPP_TESTS, cpp)
    for f in cpp.glob("test_*.cpp"):
        f.unlink()
    (cpp / "test_scratch.cpp").write_text('#include "mgtest.h"\n\nMG_TEST(scratch, passes) { MG_EQ(1, 1); }\n\n'
                                          'MG_TEST(scratch, fails) { MG_EQ(1, 2); }\n')
    out["cpp"] = unitrun.run_cpp(tests=cpp, out=d / "cpp-out")
    lua = d / "lua"
    lua.mkdir()
    (lua / "test_scratch.lua").write_text("return {\n\tpasses = function(T) T.eq(1, 1, 'one') end,\n"
                                          "\tfails = function(T) T.eq(1, 2, 'deliberate') end,\n}\n")
    out["lua"] = unitrun.run_lua(tests=lua)
    # the pinned archive's hash altered: a scratch Lua dir, a scratch cache with a copy of the real archive
    cache = d / "cache"
    cache.mkdir()
    pin = D.pin("lua51")
    real = pin["keep_path"]
    if real.is_file():
        shutil.copy2(real, cache / pin["file"])
    lock = dict(P.fork_lock())
    lock["lua51_sha256"] = "0" * 64
    out["lua_altered_pin"] = unitrun.run_lua(tests=lua, lua_dir=d / "lua-build", lock=lock, cache=cache)
    out["lua_altered_pin"]["archive_copied"] = real.is_file()
    return out


def _scratch_verdict(s: dict) -> list[str]:
    probs = []
    for suite in ("python", "cpp", "lua"):
        st = {t["id"].rsplit(".", 1)[-1].replace("test_", ""): t for t in s[suite]["tests"]}
        p, f = st.get("passes"), st.get("fails")
        if not p or p["status"] != "pass" or not isinstance(p.get("duration_s"), (int, float)):
            probs.append(f"{suite}: the passing scratch test isn't recorded as pass with a duration: {p}")
        if not f or f["status"] == "pass" or not f.get("message"):
            probs.append(f"{suite}: the failing scratch test isn't recorded as failing with its message: {f}")
    alt = s["lua_altered_pin"]["tests"]
    if not alt or alt[0]["status"] == "pass" or "pinned download mismatch" not in (alt[0].get("message") or ""):
        probs.append(f"lua with an altered pin: not failed as 'pinned download mismatch': {alt[:1]}")
    return probs


AC_HEADER_RE = re.compile(r'#include\s*[<"](?!(?:[a-z_]+(?:\.h)?|zlib\.h|SavesLogic\.h|PauseLogic\.h|DungeonsLogic\.h)[>"])'
                          r'([^>"]+)[>"]')
STD_HEADERS = {"algorithm", "array", "cctype", "chrono", "cmath", "cstdint", "cstdio", "cstdlib", "cstring", "ctime",
               "functional", "iomanip", "limits", "map", "memory", "optional", "set", "sstream", "string",
               "string_view", "tuple", "unordered_map", "unordered_set", "utility", "vector", "zlib.h"}


def logic_scan(logic: Path | None = None, tests: Path | None = None, module: Path | None = None) -> dict:
    """Every function the C++ unit tests call is defined once under src/logic and called from the module sources;
    no file under src/logic includes an AzerothCore header (only the C++ standard library, zlib and logic/ itself)."""
    from .. import unittools as U
    logic = Path(logic or U.LOGIC_DIR)
    tests = Path(tests or U.CPP_TESTS)
    module = Path(module or logic.parent)
    probs = []
    includes = {}
    for f in sorted(logic.glob("*.[hc]pp")) + sorted(logic.glob("*.h")):
        for inc in re.findall(r'#include\s*[<"]([^>"]+)[>"]', f.read_text()):
            includes.setdefault(f.name, []).append(inc)
            if inc not in STD_HEADERS and not (logic / inc).exists():
                probs.append(f"{f.name} includes {inc}, which isn't the standard library, zlib or logic/")
    called = set()
    for f in tests.glob("test_*.cpp"):
        t = f.read_text()
        for ns_use in re.findall(r"using\s+MoongladeLogic::(\w+)\s*;", t):
            called.add(ns_use)
        called |= set(re.findall(r"MoongladeLogic::(\w+)\s*\(", t))
    defs = {}
    for f in sorted(logic.glob("*.cpp")):
        for name in re.findall(r"^\s{4}[\w:<>,\s\*&]+?\b(\w+)\s*\([^;{]*\)\s*\n?\s*\{", f.read_text(), re.M):
            defs.setdefault(name, []).append(f.name)
    module_src = "\n".join(x.read_text() for x in sorted(module.glob("*.cpp")))
    for name in sorted(called):
        if len(defs.get(name, [])) != 1:
            probs.append(f"{name}: defined {len(defs.get(name, []))} time(s) under src/logic (want once)")
        if not re.search(rf"MoongladeLogic::{name}\s*\(", module_src):
            probs.append(f"{name}: not called from the module sources")
    return {"logic_dir": str(logic.relative_to(P.ROOT)) if logic.is_relative_to(P.ROOT) else str(logic),
            "called_by_tests": sorted(called), "defined": defs, "includes": includes, "problems": probs}


def unit_layer_record(doc: dict) -> dict:
    t0 = time.time()
    m = doc["meta"]
    u = m.get("unit") or {}
    probs = []
    tier = m.get("tier")
    unit_tier = tier == "unit" and not m.get("filter")
    from .reporting import POST_IDS
    executed = [r for r in doc["checks"] if r["id"] not in POST_IDS]
    if u:
        if u.get("processes", {}).get("started_during"):
            probs.append(f"the unit phase started processes: {u['processes']['started_during']}")
        if (u.get("wall_s") or 0) > BUDGETS["unit"]:
            probs.append(f"the unit layer took {u['wall_s']} s (> {BUDGETS['unit']:.0f} s)")
        if u.get("run_tests") and not u.get("suites"):
            probs.append("no unit-test suites recorded")
        for s in u.get("suites") or []:
            for t in s["tests"]:
                if not isinstance(t.get("duration_s"), (int, float)) or "id" not in t:
                    probs.append(f"unit test record without id or duration: {t}")
                    break
        if u.get("failed_tests"):
            probs.append(f"unit tests failing: {u['failed_tests'][:10]}")
        if u.get("failed_checks"):
            probs.append(f"unit-layer checks failing: {u['failed_checks'][:10]}")
    else:
        probs.append("no unit-layer record (meta.unit)")
    if unit_tier:
        from ..runner import segment
        want = sorted(i for i, c in REGISTRY.items() if c.layer == "unit" or (segment(c) == 9 and c.layer == "unit"))
        got = sorted(r["id"] for r in executed if r.get("parent") is None)
        others = sorted(i for i in got if i in REGISTRY and REGISTRY[i].layer != "unit")
        missing = sorted(set(want) - set(got))
        if others:
            probs.append(f"non-unit checks executed in the unit tier: {others}")
        if missing:
            probs.append(f"unit-layer checks not executed: {missing}")
        if m.get("full_run") is not False:
            probs.append("a unit run marked as a full run")
    d = _scratch("unitlayer")
    try:
        syn = _scratch_suites(d)
        sprobs = _scratch_verdict(syn)
    except Exception as e:  # noqa: BLE001
        syn, sprobs = {"error": f"{type(e).__name__}: {e}"}, [f"scratch suites: {type(e).__name__}: {e}"]
    finally:
        shutil.rmtree(d, ignore_errors=True)
    scan = logic_scan()
    probs += sprobs + scan["problems"]
    ev = {"tier": tier, "unit_tier_unfiltered": unit_tier, "unit": {k: v for k, v in u.items() if k not in ("suites",)},
          "unit_tests": {s["suite"]: len(s["tests"]) for s in u.get("suites") or []},
          "scratch_suites": {k: {"tests": [{x: t.get(x) for x in ("id", "status", "duration_s", "message")}
                                           for t in v.get("tests", [])]} for k, v in syn.items() if isinstance(v, dict)},
          "logic_scan": scan, "problems": probs}
    ok = not probs
    return {"id": "report.unit_layer", "capability": CAP, "layer": "unit", "status": "pass" if ok else "fail",
            "evidence": ev, "duration_s": round(time.time() - t0, 2), **({} if ok else {"message": "; ".join(probs)[:300]})}


# ============================================================================ report.selection

def scratch_tree(d: Path, extra: dict | None = None) -> dict:
    """A small repo for the selection and ownership rules: the paths the spec's cases name."""
    files = {
        "docs/x.md": "doc\n",
        "play": "#!/bin/sh\n",
        "modules/mod-moonglade/src/MoongladePause.cpp": "// pause\n",
        "modules/mod-moonglade/src/MoongladeSaves.cpp": "// saves\n",
        "modules/mod-moonglade/src/mod_moonglade_loader.cpp": "// loader\n",
        "modules/mod-moonglade/conf/mod_moonglade.conf.dist": "Moonglade.Saves.KeepAuto = 10\nMoonglade.Pause.Enable = 1\n",
        "harness/hslib/__init__.py": "",
        "harness/hslib/stack.py": "def up():\n    return 1\n",
        "harness/hslib/newthing.py": "X = 1\n",
        "harness/hslib/helper.py": "def h():\n    return 1\n",
        "harness/hslib/checks/__init__.py": "",
        "harness/hslib/checks/playclient_util.py": "from .. import helper\n",
        "patches/core/0003-world-pause.patch": "diff\n",
        "fork.lock": "core_sha=aaaa\nplayerbots_sha=bbbb\nautobalance_sha=cccc\n",
        "harness/active-changes": "some-change\n",
    }
    files.update(extra or {})
    for rel, text in files.items():
        f = d / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    return files


def scratch_registry() -> dict:
    MP = "modules/mod-moonglade/"
    conf = MP + "conf/mod_moonglade.conf.dist"
    return {
        "pause.freeze_matrix": _syn("pause.freeze_matrix", "integration", (MP + "src/MoongladePause.cpp", conf + "#Moonglade.Pause"), "up"),
        "pause.refused_on_taxi": _syn("pause.refused_on_taxi", "integration", (MP + "src/MoongladePause.cpp",), "up"),
        "journey.pause": _syn("journey.pause", "journey", (MP + "src/MoongladePause.cpp",), budget=90),
        "saves.cluster": _syn("saves.cluster", "integration", (MP + "src/MoongladeSaves.cpp", conf + "#Moonglade.Saves"), "up"),
        "login.continue_context": _syn("login.continue_context", "integration", ("harness/hslib/checks/playclient_util.py",), "up"),
        "dng.session1": _syn("dng.session1", "integration", ("fork.lock#autobalance_*",), "up"),
        "journey.first_play": _syn("journey.first_play", "journey", ("play",), budget=150),
    }


def selection_cases(d: Path) -> dict:
    from .. import selection as S
    scratch_tree(d)
    reg = scratch_registry()
    m = S.Map(reg, d)
    price = lambda tid: {"pause.refused_on_taxi": 5.0, "saves.cluster": 10.0}.get(tid, 20.0)
    conf = "modules/mod-moonglade/conf/mod_moonglade.conf.dist"
    old = {conf: "Moonglade.Saves.KeepAuto = 10\nMoonglade.Pause.Enable = 1\n",
           "fork.lock": "core_sha=aaaa\nplayerbots_sha=bbbb\nautobalance_sha=cccc\n",
           "harness/hslib/stack.py": "def up():\n    return 1\n"}

    def run(paths, new=None):
        nw = dict(old, **(new or {}))
        return S.select(paths, m, price, old_source=lambda p: old.get(p, ""), new_source=lambda p: nw.get(p, ""))
    cases = {
        "a_docs": run(["docs/x.md"]),
        "b_pause_cpp": run(["modules/mod-moonglade/src/MoongladePause.cpp"]),
        "c_new_hslib_file": run(["harness/hslib/newthing.py"]),
        "d_core_patch": run(["patches/core/0003-world-pause.patch"]),
        "e_git_failure": run(None),
        "f_helper_of_playclient_util": run(["harness/hslib/helper.py"]),
        "g_saves_key": run([conf], {conf: "Moonglade.Saves.KeepAuto = 12\nMoonglade.Pause.Enable = 1\n"}),
        "h_stack_body": run(["harness/hslib/stack.py"], {"harness/hslib/stack.py": "def up():\n    return 2\n"}),
        "i_autobalance_pin": run(["fork.lock"], {"fork.lock": "core_sha=aaaa\nplayerbots_sha=bbbb\nautobalance_sha=dddd\n"}),
        "j_core_pin": run(["fork.lock"], {"fork.lock": "core_sha=eeee\nplayerbots_sha=bbbb\nautobalance_sha=cccc\n"}),
        "k_active_changes": run(["harness/active-changes"]),
        "k_active_changes_with_docs": run(["harness/active-changes", "docs/x.md"]),
    }
    smoke = set(m.smoke_set(price))
    sel = {k: set(v["selected"]) for k, v in cases.items()}
    full = {k: v["full"] for k, v in cases.items()}
    verdict = {
        "a_only_unit": not full["a_docs"] and not sel["a_docs"],
        "b_pause_tests_and_journey_no_saves": not full["b_pause_cpp"] and
        sel["b_pause_cpp"] == {"pause.freeze_matrix", "pause.refused_on_taxi", "journey.pause"},
        "c_smoke_with_reason": not full["c_new_hslib_file"] and sel["c_new_hslib_file"] == smoke
        and bool(cases["c_new_hslib_file"]["smoke_paths"]),
        "d_full": full["d_core_patch"] and bool(cases["d_core_patch"]["full_reasons"]),
        "e_full": full["e_git_failure"] and bool(cases["e_git_failure"]["full_reasons"]),
        "f_only_declarers": not full["f_helper_of_playclient_util"] and
        sel["f_helper_of_playclient_util"] == {"login.continue_context"},
        "g_saves_no_pause": not full["g_saves_key"] and "saves.cluster" in sel["g_saves_key"]
        and not any(i.startswith(("pause.", "journey.pause")) for i in sel["g_saves_key"]),
        "h_smoke_with_reason": not full["h_stack_body"] and sel["h_stack_body"] == smoke
        and bool(cases["h_stack_body"]["smoke_paths"]),
        "i_autobalance_declarers_not_full": not full["i_autobalance_pin"] and sel["i_autobalance_pin"] == {"dng.session1"},
        "j_full": full["j_core_pin"] and bool(cases["j_core_pin"]["full_reasons"]),
        "k_active_changes_unit_only": all(not full[k] and not sel[k] and not cases[k]["smoke_paths"]
                                          and "harness/active-changes" in cases[k]["unit_only"]
                                          for k in ("k_active_changes", "k_active_changes_with_docs")),
        "smoke_is_first_play_plus_cheapest_per_area": smoke == {"journey.first_play", "pause.refused_on_taxi",
                                                               "saves.cluster", "login.continue_context",
                                                               "dng.session1"},
        "every_case_timed": all(isinstance(v["duration_s"], float) and v["duration_s"] <= 10 for v in cases.values()),
    }
    return {"cases": {k: {"full": v["full"], "full_reasons": v["full_reasons"], "selected": sorted(v["selected"]),
                          "smoke_paths": v["smoke_paths"], "duration_s": v["duration_s"]} for k, v in cases.items()},
            "smoke_set": sorted(smoke), "verdict": verdict}


@check("report.selection", order=2, capability=CAP, stack="any", layer="unit", parallel_safe=True)
def selection_rule(ctx):
    """The selector on a scratch git repo for the spec's cases (a)-(j), plus harness/active-changes (no-impact: only the
    unit layer, alone or with docs), gives the required selections, each with its
    reasons and duration; a stale covers entry selects FULL; and this checkout's own selection takes at most 10 s,
    recorded."""
    from .. import selection as S
    from ..registry import load_all
    d = _scratch("selection")
    try:
        res = selection_cases(d)
        # a stale covers entry (a path matching no file) selects FULL
        reg = scratch_registry()
        reg["t.stale"] = _syn("t.stale", "integration", ("harness/hslib/gone.py",), "up")
        st = S.select(["docs/x.md"], S.Map(reg, d), lambda t: 1.0)
        res["stale_covers"] = {"full": st["full"], "reasons": st["full_reasons"][:3]}
        res["verdict"]["stale_covers_full"] = st["full"] and any("stale" in r for r in st["full_reasons"])
    finally:
        shutil.rmtree(d, ignore_errors=True)
    t0 = time.time()
    live = S.select_for_run(load_all())
    live_s = round(time.time() - t0, 2)
    res["this_checkout"] = {"duration_s": live["duration_s"], "wall_s": live_s, "full": live["full"],
                            "full_reasons": live["full_reasons"][:5], "selected": len(live["selected"]),
                            "smoke_paths": live["smoke_paths"][:10], "paths": len(live["paths"] or [])}
    res["verdict"]["this_checkout_within_10s"] = live_s <= 10.0
    bad = [k for k, v in res["verdict"].items() if not v]
    require(not bad, f"selection rule wrong for: {bad}", **res)
    return res


# ============================================================================ report.ownership

@check("report.ownership", order=2, capability=CAP, stack="any", layer="unit", parallel_safe=True)
def ownership(ctx):
    """Every tracked product path (module sources and configs, client-mods/**, harness/app/**, play, bootstrap,
    tools/release/**) is named by a test's covers or by the FULL / no-impact / smoke lists; a synthetic tree that adds
    modules/mod-moonglade/src/MoongladeNew.cpp, named by no covers, fails and lists it."""
    from .. import selection as S
    from ..registry import load_all
    m = S.Map(load_all())
    live = S.unowned(m)
    d = _scratch("ownership")
    try:
        scratch_tree(d, {"modules/mod-moonglade/src/MoongladeNew.cpp": "// new\n"})
        reg = scratch_registry()
        syn = S.unowned(S.Map(reg, d))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    ev = {"unowned": live, "product_globs": list(S.PRODUCT), "synthetic_unowned": syn,
          "tests_with_covers": sum(1 for c in m.tests.values() if c.covers), "acceptance_tests": len(m.tests)}
    require("modules/mod-moonglade/src/MoongladeNew.cpp" in syn,
            "counterexample: the synthetic unowned file wasn't listed", **ev)
    require(not live, f"{len(live)} product path(s) without an owner: {live[:10]}", **ev)
    return ev


# ============================================================================ report.gate_rule

@check("report.gate_rule", order=3, capability=CAP, stack="any", layer="unit", parallel_safe=True)
def gate_rule(ctx):
    """A gate check whose covers names harness/hslib/lanes.py: a pr run with only docs/x.md changed doesn't run it
    and records nothing for it (not even as owed); a pr run with lanes.py changed doesn't run it either and lists it
    under "gate checks owed" naming lanes.py (meta.selection and summary.md); a full run runs it and requires it to
    pass (the run's own selection and execution, with the stack handling stubbed out; a failing gate check fails the
    full run)."""
    from .. import runner, selection as S
    d = _scratch("gaterule")
    try:
        scratch_tree(d, {"harness/hslib/lanes.py": "X = 1\n"})
        calls = []
        reg = {"g.lanes": _syn("g.lanes", "gate", ("harness/hslib/lanes.py",)),
               "i.other": _syn("i.other", "integration", ("modules/mod-moonglade/src/MoongladeSaves.cpp",))}
        m = S.Map(reg, d)
        out = {}

        def failing(ctx):
            require(False, "gate failed (counterexample)")
        for name, tier, paths, fn_ok in (("pr_docs", "pr", ["docs/x.md"], True),
                                         ("pr_lanes", "pr", ["harness/hslib/lanes.py"], True),
                                         ("pr_lanes_failing", "pr", ["harness/hslib/lanes.py"], False),
                                         ("full", "full", None, True), ("full_failing", "full", None, False)):
            calls.clear()
            reg["g.lanes"].fn = (lambda ctx: calls.append(1) or {"ran": 1}) if fn_ok else failing
            chosen, sel_meta = None, None
            if tier == "pr":
                sel_meta = S.select(paths, m, lambda t: 1.0)
                chosen = S.pr_chosen(sel_meta, reg)
            sel = runner.select(reg, tier, None, chosen)
            recs = runner.execute(sel, {}, ensure=lambda *_a: None, quiet=True)
            owed = (sel_meta or {}).get("gate_owed") or {}
            out[name] = {"selected": [c.id for c in sel], "records": {r["id"]: r["status"] for r in recs},
                         "ran": len(calls), "owed": owed,
                         "summary_owed": runner.owed_lines(sel_meta) if sel_meta else []}
    finally:
        shutil.rmtree(d, ignore_errors=True)
    lanes_path = "harness/hslib/lanes.py"
    v = {"pr_docs_doesnt_run_or_record": "g.lanes" not in out["pr_docs"]["records"] and not out["pr_docs"]["ran"]
         and "g.lanes" not in out["pr_docs"]["owed"],
         "pr_lanes_doesnt_run": "g.lanes" not in out["pr_lanes"]["records"] and not out["pr_lanes"]["ran"]
         and "g.lanes" not in out["pr_lanes"]["selected"],
         "pr_lanes_lists_owed_with_path": any(lanes_path in r for r in out["pr_lanes"]["owed"].get("g.lanes", []))
         and any("g.lanes" in ln and lanes_path in ln for ln in out["pr_lanes"]["summary_owed"]),
         "pr_owed_failing_gate_is_not_a_failure": "g.lanes" not in out["pr_lanes_failing"]["records"],
         "full_runs_and_passes": out["full"]["records"].get("g.lanes") == "pass" and out["full"]["ran"] == 1,
         "full_failing_gate_fails": out["full_failing"]["records"].get("g.lanes") == "fail"}
    ev = {"runs": out, "verdict": v}
    require(all(v.values()), f"gate rule wrong: {[k for k, x in v.items() if not x]}", **ev)
    return ev


# ============================================================================ report.layer_budgets (post-run)

def budget_problems(meta: dict, new_selected_s: float | None = None) -> tuple[list[str], dict]:
    L = meta.get("layers") or {}
    full_like = (meta.get("full_run") is True) or bool((meta.get("selection") or {}).get("full"))
    probs, rows = [], {}

    def chk(name, val, budget, applies=True):
        rows[name] = {"s": val, "budget_s": budget, "applies": applies}
        if applies and val is not None and val > budget:
            probs.append(f"{name}: {val:.0f} s > budget {budget:.0f} s")
    chk("unit", (L.get("unit") or {}).get("wall_s"), BUDGETS["unit"])
    chk("selection", (L.get("selection") or {}).get("wall_s"), BUDGETS["selection"])
    for j, v in sorted(((L.get("journeys") or {}).get("each") or {}).items()):
        if v.get("budget_s"):
            chk(j, v.get("s"), float(v["budget_s"]))
    for k, v in sorted(((L.get("integration") or {}).get("sessions") or {}).items()):
        if v.get("budget_s"):
            chk(k, v.get("s"), float(v["budget_s"]))
    chk("integration", (L.get("integration") or {}).get("wall_s"), BUDGETS["integration"], full_like)
    chk("acceptance (integration + journeys)", (L.get("acceptance") or {}).get("wall_s"), BUDGETS["acceptance"], full_like)
    if meta.get("tier") == "pr" and not full_like:
        chk("new selected checks", new_selected_s, BUDGETS["new_checks"])
    rows["gate_s"] = (L.get("gate") or {}).get("wall_s")
    rows["build_s"] = (L.get("build") or {}).get("wall_s")
    return probs, rows


def synthetic_budget_cases() -> dict:
    def meta(**kw):
        L = {"unit": {"wall_s": 50.0}, "selection": {"wall_s": 1.0}, "integration": {"wall_s": 600.0},
             "journeys": {"wall_s": 500.0, "each": {"journey.pause": {"s": 50.0, "budget_s": 90}}},
             "acceptance": {"wall_s": 1100.0}, "gate": {"wall_s": 0.0}, "build": {"wall_s": 0.0}}
        for k, v in kw.pop("layers", {}).items():
            L[k] = (L.get(k) or {}) | v
        return {"tier": kw.get("tier", "full"), "full_run": kw.get("full_run", True), "layers": L,
                "selection": kw.get("selection")}
    cases = {
        "a_unit_121": budget_problems(meta(layers={"unit": {"wall_s": 121.0}}))[0],
        "b_selection_11": budget_problems(meta(layers={"selection": {"wall_s": 11.0}}))[0],
        "c_journey_pause_95": budget_problems(meta(layers={"journeys": {"wall_s": 95.0, "each": {"journey.pause": {"s": 95.0, "budget_s": 90}}}}))[0],
        "d_full_selection_pr_31min": budget_problems(meta(tier="pr", full_run=False, selection={"full": True},
                                                          layers={"acceptance": {"wall_s": 31 * 60.0}}))[0],
        "e_full_31min_plus_build": budget_problems(meta(layers={"acceptance": {"wall_s": 31 * 60.0}, "build": {"wall_s": 600.0}}))[0],
        "f_pr_130s_new": budget_problems(meta(tier="pr", full_run=False, selection={"full": False}), 130.0)[0],
        "h_full_16min_integration": budget_problems(meta(layers={"integration": {"wall_s": 16 * 60.0}}))[0],
        "i_session_over_its_budget": budget_problems(meta(layers={"integration": {"wall_s": 600.0, "sessions": {
            "dng.session1": {"s": 95.0, "budget_s": 90}}}}))[0],
        "g_full_29min_plus_25min_build_gate": budget_problems(meta(layers={"acceptance": {"wall_s": 29 * 60.0},
                                                                          "build": {"wall_s": 900.0}, "gate": {"wall_s": 600.0}}))[0],
    }
    names = {"a_unit_121": "unit", "b_selection_11": "selection", "c_journey_pause_95": "journey.pause",
             "d_full_selection_pr_31min": "acceptance", "e_full_31min_plus_build": "acceptance",
             "f_pr_130s_new": "new selected checks", "h_full_16min_integration": "integration",
             "i_session_over_its_budget": "dng.session1"}
    verdict = {k: bool(cases[k]) and any(n in p for p in cases[k]) for k, n in names.items()}
    verdict["g_passes"] = not cases["g_full_29min_plus_25min_build_gate"]
    return {"cases": cases, "verdict": verdict}


def layer_budgets_record(doc: dict) -> dict:
    t0 = time.time()
    m = doc["meta"]
    new_s = None
    if m.get("tier") == "pr":
        new = (m.get("timing") or {}).get("new_checks_s") or {}
        # design 9.1: the new acceptance checks the selection picked (integration and journeys; gate checks are
        # timed apart, like the build)
        sel = set(((m.get("selection") or {}).get("selected") or {}))
        lay = {r["id"]: r.get("layer") for r in doc.get("checks") or []}
        new_s = round(sum(v for k, v in new.items() if k in sel and lay.get(k) in ("integration", "journey")), 1)
    probs, rows = budget_problems(m, new_s)
    syn = synthetic_budget_cases()
    bad = [k for k, v in syn["verdict"].items() if not v]
    probs += [f"synthetic case {k} has the wrong verdict" for k in bad]
    ok = not probs
    return {"id": "report.layer_budgets", "capability": CAP, "layer": "unit", "status": "pass" if ok else "fail",
            "evidence": {"layers": rows, "new_selected_checks_s": new_s, "synthetic": syn, "problems": probs},
            "duration_s": round(time.time() - t0, 2), **({} if ok else {"message": "; ".join(probs)[:300]})}


# ============================================================================ report.pr_average (post-run)

def pr_average(history: list[dict], replay_fn=None) -> dict:
    """The rolling mean of the last PR_WINDOW pr runs (minutes of integration + journeys); from the PR_MIN_RUNS-th
    run on it fails above PR_MEAN_MAX_MIN; with fewer runs it reports the replay's mean, never failing."""
    if len(history) < PR_MIN_RUNS:
        rep = replay_fn() if replay_fn else None
        return {"ok": True, "note": f"fewer than {PR_MIN_RUNS} recorded runs", "recorded": len(history),
                "replay_mean_min": (rep or {}).get("mean_min"), "replay": rep}
    last = history[-PR_WINDOW:]
    mins = [float(r.get("acceptance_s") or 0.0) / 60 for r in last]
    mean = round(statistics.mean(mins), 1)
    slow = sorted(({"sha": r.get("sha"), "min": round(float(r.get("acceptance_s") or 0) / 60, 1)} for r in last),
                  key=lambda x: -x["min"])[:3]
    return {"ok": mean <= PR_MEAN_MAX_MIN, "mean_min": mean, "runs": len(last),
            "full_share": round(sum(bool(r.get("full")) for r in last) / len(last), 2), "slowest": slow}


def synthetic_pr_cases() -> dict:
    base = [{"sha": f"s{i}", "acceptance_s": 14 * 60.0, "full": False} for i in range(9)]
    a = pr_average(base + [{"sha": "big", "acceptance_s": 25 * 60.0, "full": True}])
    b = pr_average(base + [{"sha": "ok", "acceptance_s": 15 * 60.0, "full": False}])
    c = pr_average(base[:3], replay_fn=lambda: {"mean_min": 12.5, "prs": [{"pr": 1}]})
    return {"cases": {"a": a, "b": b, "c": c},
            "verdict": {"a_fails_15_1": not a["ok"] and a["mean_min"] == 15.1 and bool(a["slowest"]) and "full_share" in a,
                        "b_passes_14_1": b["ok"] and b["mean_min"] == 14.1,
                        "c_replay_note": c["ok"] and c["note"] == f"fewer than {PR_MIN_RUNS} recorded runs"
                        and c["replay_mean_min"] == 12.5}}


def pr_average_record(doc: dict) -> dict:
    from .. import selection as S
    t0 = time.time()
    m = doc["meta"]
    hist = []
    try:
        hist = [json.loads(l) for l in S.PR_RUNS.read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        pass
    probs = []
    live = None
    if m.get("tier") == "pr" and not m.get("filter"):
        L = m.get("layers") or {}
        this = {"sha": m.get("repo_sha"), "acceptance_s": (L.get("acceptance") or {}).get("wall_s"),
                "full": bool((m.get("selection") or {}).get("full"))}
        live = pr_average(hist + [this], replay_fn=lambda: S.replay(15))
        if not live["ok"]:
            probs.append(f"the mean of the last {live['runs']} pr runs is {live['mean_min']} min (> {PR_MEAN_MAX_MIN} min); "
                         f"FULL share {live['full_share']}; slowest {live['slowest']}")
    syn = synthetic_pr_cases()
    probs += [f"synthetic case {k} has the wrong verdict" for k, v in syn["verdict"].items() if not v]
    ok = not probs
    return {"id": "report.pr_average", "capability": CAP, "layer": "unit", "status": "pass" if ok else "fail",
            "evidence": {"this_run": live, "recorded_runs": len(hist), "history_file": str(S.PR_RUNS.relative_to(P.ROOT)),
                         "synthetic": syn, "problems": probs},
            "duration_s": round(time.time() - t0, 2), **({} if ok else {"message": "; ".join(probs)[:300]})}


# ============================================================================ report.journeys (post-run)

def journey_problems(reg: dict, timeline: list[dict], named: dict | None = None,
                     cap_s: float = JOURNEY_MAX_S) -> list[str]:
    probs = []
    named = JOURNEY_SET if named is None else named
    js = {i: c for i, c in reg.items() if c.layer == "journey"}
    if set(js) != set(named):
        probs.append(f"journey-layer checks {sorted(js)} != the named set {sorted(named)}")
    for i, b in named.items():
        if i in js and js[i].budget_s != b:
            probs.append(f"{i}: budget {js[i].budget_s} != {b} s")
    if len(js) > JOURNEY_MAX_N:
        probs.append(f"{len(js)} journeys (> {JOURNEY_MAX_N})")
    tot = sum(float(c.budget_s or 0) for c in js.values())
    if tot > cap_s:
        probs.append(f"journey budgets sum to {tot:.0f} s (> {cap_s:.0f} s)")
    spans = [e for e in timeline if e.get("id") and e.get("lane", "a") in ("a", None)]
    for j in (e for e in spans if e["id"] in js):
        for o in spans:
            if o is j or o.get("parent"):
                continue
            if o["start"] < j["end"] and j["start"] < o["end"]:
                probs.append(f"{j['id']} overlaps {o['id']}")
    return probs


def journeys_record(doc: dict) -> dict:
    t0 = time.time()
    tl = (doc["meta"].get("timeline") or [])
    probs = journey_problems(REGISTRY, tl)
    syn_reg = {i: _syn(i, "journey", budget=b) for i, b in JOURNEY_SET.items()}
    ce = journey_problems({**syn_reg, "journey.seventh": _syn("journey.seventh", "journey", budget=30)}, [])
    ok_syn = journey_problems(syn_reg, [])
    # the 840 s cap on its own: the same six ids with budgets summing to 841 s (one more second on app_desktop)
    over = dict(JOURNEY_SET, **{"journey.app_desktop": JOURNEY_SET["journey.app_desktop"]
                                + (JOURNEY_MAX_S + 1 - sum(JOURNEY_SET.values()))})
    ce_cap = journey_problems({i: _syn(i, "journey", budget=b) for i, b in over.items()}, [], named=over)
    if not ce:
        probs.append("counterexample: a seventh unnamed journey wasn't caught")
    if not any("sum to" in x for x in ce_cap):
        probs.append(f"counterexample: budgets summing to {sum(over.values())} s weren't caught by the {JOURNEY_MAX_S} s cap")
    if ok_syn:
        probs.append(f"the named set itself fails the rule: {ok_syn}")
    ok = not probs
    return {"id": "report.journeys", "capability": CAP, "layer": "unit", "status": "pass" if ok else "fail",
            "evidence": {"registered": sorted(i for i, c in REGISTRY.items() if c.layer == "journey"),
                         "named_set": JOURNEY_SET, "budget_sum_s": sum(JOURNEY_SET.values()), "cap_s": JOURNEY_MAX_S,
                         "timeline_entries": len(tl), "counterexample": ce, "counterexample_cap": ce_cap,
                         "problems": probs},
            "duration_s": round(time.time() - t0, 2), **({} if ok else {"message": "; ".join(probs)[:300]})}


# ============================================================================ report.no_fixed_sleeps

# Reviewed allowlist: "<file>:<constant or literal>" -> the spec that requires the real time.
SLEEP_ALLOWLIST: dict = {
    'harness/hslib/checks/pause_checks.py:PAUSE_S': 'true-pause spec: the world is paused for 20 s in each freeze scenario',
    'harness/hslib/checks/dungeon_checks.py:PAUSE_S': 'dungeon-scaling spec "Pausing keeps the scaling": paused for 5 s (test-pyramid delta)',
    'harness/hslib/checks/pause_checks.py:RESPAWN_PAUSE_OVER_R_S': 'true-pause "Respawn and leash freeze" / harness-control "Server-side waits are controllable": the respawn pause lasts longer than R',
    'harness/hslib/checks/pause_checks.py:LEASH_PAUSE_OVER_R_S': 'true-pause "Respawn and leash freeze" / harness-control "Server-side waits are controllable": the leash pause lasts longer than R',
    'harness/hslib/checks/cold_play_checks.py:SANDBOX_REPORTS_S': 'play-launcher "Loop works with the network denied": the claim is that the denial log shows no attempt, so the kernel\'s sandbox reports get time to reach the unified log (moved from play.offline, test-pyramid part 3)',
    'harness/hslib/checks/pause_checks.py:LEASH_SAMPLE_S': 'true-pause "Respawn and leash freeze": the chaser has not evaded after the resume (observed LEASH_SAMPLE_S, the leash would fire at once on the wall clock)',
}
# Waits found when the lint landed (2026-10-11), each to be turned into a poll or cited by its area's migration slice
# (tasks 3.x). They are reported, not failed, until then; the list only ever shrinks (a new wait isn't on it).
PENDING_SLEEPS = frozenset({
    'harness/hslib/checks/app_checks.py:1.1',
    'harness/hslib/checks/app_checks.py:2',
    'harness/hslib/checks/app_checks.py:3',
    'harness/hslib/checks/app_checks.py:5',
    'harness/hslib/checks/backup_checks.py:2',
    'harness/hslib/checks/backup_checks.py:3',
    'harness/hslib/checks/backup_checks.py:3.0',
    'harness/hslib/checks/client_play_checks.py:5',
    'harness/hslib/checks/core_checks.py:3',
    'harness/hslib/checks/core_checks.py:IPS_WAIT_S',
    'harness/hslib/checks/dungeon_checks.py:1.5',
    'harness/hslib/checks/dungeon_checks.py:2',
    'harness/hslib/checks/dungeon_checks.py:3',
    'harness/hslib/checks/firstlaunch_checks.py:2',
    'harness/hslib/checks/firstlaunch_checks.py:3',
    'harness/hslib/checks/pause_checks.py:2',
    'harness/hslib/checks/pause_checks.py:4',
    'harness/hslib/checks/pause_checks.py:5',
    'harness/hslib/checks/pause_checks.py:LEASH_SAMPLE_S',
    'harness/hslib/checks/play_checks.py:1.5',
    'harness/hslib/checks/play_checks.py:2',
    'harness/hslib/checks/play_checks.py:5',
    'harness/hslib/checks/polish_checks.py:1.5',
    'harness/hslib/checks/polish_checks.py:PERF_CPU_EVERY_S',
    'harness/hslib/checks/polish_checks.py:PERF_SETTLE_S',
    'harness/hslib/checks/release_runtime_checks.py:2',
    'harness/hslib/checks/saves_checks.py:1.1',
    'harness/hslib/checks/saves_checks.py:1.5',
    'harness/hslib/checks/saves_checks.py:1.8',
    'harness/hslib/checks/saves_checks.py:2',
    'harness/hslib/checks/saves_checks.py:2.0',
    'harness/hslib/checks/saves_checks.py:3',
    'harness/hslib/checks/saves_checks.py:3.0',
    'harness/hslib/checks/saves_checks.py:4',
    'harness/hslib/checks/saves_gui_checks.py:1.1',
    'harness/hslib/checks/savesctl_checks.py:2',
    'harness/hslib/checks/savesctl_checks.py:3.0',
    'harness/hslib/checks/setup_checks.py:2',
    'harness/hslib/dungeon.py:15',
    'harness/hslib/dungeon.py:2',
    'harness/hslib/dungeon.py:4',
    'harness/hslib/launch.py:3',
    'harness/hslib/population.py:1.1',
    'harness/hslib/population.py:2',
    'harness/hslib/population.py:30',
    'harness/hslib/probe/client.py:2',
})
SLEEP_CALLS = ("sleep", "wait_until")


def _consts(tree) -> dict:
    out = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) \
                and isinstance(n.value, ast.Constant) and isinstance(n.value.value, (int, float)):
            out[n.targets[0].id] = n.value.value
    return out


def sleep_hits(path: Path, rel: str) -> list[dict]:
    """Fixed waits over 1 s in one source: time.sleep(<literal or module constant > 1>), and wait_until(<expr with
    such a constant added>)."""
    tree = ast.parse(path.read_text())
    consts = _consts(tree)
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not n.args:
            continue
        fn = n.func
        name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
        if name not in SLEEP_CALLS:
            continue
        found = []
        for x in ast.walk(n.args[0]):
            if isinstance(x, ast.Constant) and isinstance(x.value, (int, float)) and not isinstance(x.value, bool) and x.value > 1:
                found.append((repr(x.value), x.value))
            elif isinstance(x, ast.Name) and isinstance(consts.get(x.id), (int, float)) and consts[x.id] > 1:
                found.append((x.id, consts[x.id]))
            elif isinstance(x, ast.Attribute) and x.attr.isupper():
                found.append((x.attr, None))      # a constant of another module: listed, judged by the allowlist
        if name == "wait_until":
            found = [f for f in found if f[1] is None or f[1] > 1]
        for label, val in found:
            if val is None and name == "sleep":
                continue
            out.append({"file": rel, "line": n.lineno, "call": name, "value": label, "seconds": val,
                        "key": f"{rel}:{label}"})
    return out


def acceptance_sources(reg: dict | None = None) -> list[str]:
    """The integration and journey check files and the hslib modules they import directly."""
    import inspect
    from .. import selection as S
    reg = reg if reg is not None else REGISTRY
    files = set()
    for c in reg.values():
        if c.layer in ("integration", "journey"):
            try:
                f = str(Path(inspect.getsourcefile(c.fn)).resolve().relative_to(P.ROOT))
            except (TypeError, ValueError):
                continue
            files.add(f)
    for f in list(files):
        files |= S._direct_imports(P.ROOT, f)
    return sorted(f for f in files if f.endswith(".py"))


def sleep_lint(files: list[str], root: Path | None = None) -> dict:
    root = Path(root or P.ROOT)
    hits = []
    for rel in files:
        hits += sleep_hits(root / rel, rel)
    pending = [h for h in hits if h["key"] in PENDING_SLEEPS and h["key"] not in SLEEP_ALLOWLIST]
    bad = [h for h in hits if h["key"] not in SLEEP_ALLOWLIST and h["key"] not in PENDING_SLEEPS]
    return {"files": len(files), "waits": len(hits), "allowlisted": len(hits) - len(bad) - len(pending),
            "pending_migration": pending, "violations": bad}


@check("report.no_fixed_sleeps", order=3, capability=CAP, stack="any", layer="unit", parallel_safe=True)
def no_fixed_sleeps(ctx):
    """Acceptance-layer code (integration and journey check files and the hslib modules they import) has no fixed
    wait over 1 s outside the reviewed allowlist, each entry naming the spec that requires the real time; a scratch
    copy of a check file with an added time.sleep(5) fails, naming the file and line."""
    files = acceptance_sources()
    res = sleep_lint(files)
    d = _scratch("sleeplint")
    try:
        f = d / "harness/hslib/checks/scratch_checks.py"
        f.parent.mkdir(parents=True)
        f.write_text("import time\n\ndef c(ctx):\n    time.sleep(5)\n")
        ce = sleep_lint(["harness/hslib/checks/scratch_checks.py"], d)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    ev = {"lint": res, "allowlist": SLEEP_ALLOWLIST, "counterexample": ce}
    require(any(v["line"] == 4 and v["file"].endswith("scratch_checks.py") for v in ce["violations"]),
            "counterexample: an added time.sleep(5) wasn't caught", **ev)
    require(not res["violations"], f"{len(res['violations'])} fixed wait(s) over 1 s outside the allowlist: "
            + ", ".join(f"{v['file']}:{v['line']} ({v['value']})" for v in res["violations"][:8]), **ev)
    return ev
