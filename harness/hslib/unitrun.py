"""`hs test`: the unit layer (openspec/changes/test-pyramid design 3-5).

Runs, in this order, with no stack, Wine or client process:
1. the Python unit tests (`unittest` discovery under harness/tests/unit, in-process, timed per test);
2. the C++ logic tests (harness/tests/unit/cpp via unittools.ensure_cpp, a host-built binary);
3. the Lua tests (harness/tests/unit/lua/run.lua under the pinned Lua 5.1.5);
4. optionally the unit-layer static checks (`--static`: registered @checks listed in STATIC_CHECKS), serially or in a
   process pool.

Every test over UNIT_TEST_CAP_S fails ("unit test over budget"); the C++ and Lua builds are timed separately (the
reviewed fixture allowlist). The record goes to harness/out/unit/results.json; harness/results.json is not touched.
Test ids: unit:py:<module>.<Class>.<method>, unit:cpp:<suite>.<name>, unit:lua:<file>.<name>.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

from . import paths as P

UNIT_TEST_CAP_S = 2.0
UNIT_DIR = P.HARNESS / "tests" / "unit"
LUA_DIR = UNIT_DIR / "lua"
RESULTS = P.HARNESS / "out" / "unit" / "results.json"


# ---------------------------------------------------------------- Python

class _TimedResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.records: list[dict] = []
        self._t0 = 0.0

    def startTest(self, test):
        super().startTest(test)
        self._t0 = time.perf_counter()

    def _rec(self, test, status, message=""):
        tid = test.id()
        if tid.startswith("unittest.loader."):           # an import error in a test module
            tid = f"<load>.{tid}"
        self.records.append({"id": f"unit:py:{tid}", "status": status, "duration_s": round(time.perf_counter() - self._t0, 4),
                             **({"message": message[-1500:]} if message else {})})

    def addSuccess(self, test):
        super().addSuccess(test)
        self._rec(test, "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._rec(test, "fail", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._rec(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._rec(test, "fail", f"skipped ({reason}): a unit test never skips")


def run_python(pattern: str | None = None) -> dict:
    t0 = time.perf_counter()
    if str(P.HARNESS) not in sys.path:
        sys.path.insert(0, str(P.HARNESS))
    suite = unittest.TestLoader().discover(str(UNIT_DIR), pattern="test_*.py", top_level_dir=str(P.HARNESS))
    res = _TimedResult()
    if pattern:
        def flat(s):
            for x in s:
                yield from (flat(x) if isinstance(x, unittest.TestSuite) else [x])
        suite = unittest.TestSuite([t for t in flat(suite) if pattern in t.id()])
    suite.run(res)
    return {"suite": "python", "wall_s": round(time.perf_counter() - t0, 3), "tests": res.records}


# ---------------------------------------------------------------- TAP-like (C++ and Lua)

def parse_tap(out: str, prefix: str) -> list[dict]:
    """`ok <id> <ms>` / `not ok <id> <ms>` lines, with the `# ...` lines before a failure as its message."""
    recs, notes = [], []
    for line in out.splitlines():
        if line.startswith("# "):
            notes.append(line[2:])
            continue
        parts = line.split()
        if line.startswith("ok ") and len(parts) == 3:
            recs.append({"id": f"{prefix}{parts[1]}", "status": "pass", "duration_s": round(float(parts[2]) / 1000, 4)})
            notes = []
        elif line.startswith("not ok ") and len(parts) == 4:
            recs.append({"id": f"{prefix}{parts[2]}", "status": "fail", "duration_s": round(float(parts[3]) / 1000, 4),
                         "message": "\n".join(notes)[-1500:]})
            notes = []
    return recs


def _tap_suite(name: str, cmd: list[str], prefix: str, build: dict) -> dict:
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    recs = parse_tap(r.stdout, prefix)
    out = {"suite": name, "wall_s": round(time.perf_counter() - t0, 3), "build": build, "tests": recs,
           "exit": r.returncode}
    declared = [ln for ln in r.stdout.splitlines() if ln.startswith("# tests ")]
    if r.returncode != 0 and all(x["status"] == "pass" for x in recs):
        recs.append({"id": f"{prefix}<runner>", "status": "error", "duration_s": 0.0,
                     "message": f"exit {r.returncode}: {(r.stderr or r.stdout)[-1500:]}"})
    elif not declared:
        recs.append({"id": f"{prefix}<runner>", "status": "error", "duration_s": 0.0,
                     "message": f"no summary line (crashed?): {(r.stderr or r.stdout)[-1500:]}"})
    return out


def run_cpp(pattern: str | None = None) -> dict:
    from . import unittools as U
    try:
        b = U.ensure_cpp()
    except U.ToolError as e:
        return {"suite": "cpp", "wall_s": 0.0, "build": {"error": str(e)},
                "tests": [{"id": "unit:cpp:<build>", "status": "error", "duration_s": 0.0, "message": str(e)}]}
    return _tap_suite("cpp", [b["bin"], *([pattern] if pattern else [])], "unit:cpp:", b)


def run_lua(pattern: str | None = None) -> dict:
    from . import unittools as U
    try:
        b = U.ensure_lua()
    except Exception as e:  # noqa: BLE001  a missing or mismatched build fails the suite, never skips it
        return {"suite": "lua", "wall_s": 0.0, "build": {"error": str(e)},
                "tests": [{"id": "unit:lua:<build>", "status": "error", "duration_s": 0.0, "message": str(e)}]}
    t0 = time.perf_counter()
    from . import luaprelude
    prelude = luaprelude.write_prelude()
    b = b | {"prelude_s": round(time.perf_counter() - t0, 3)}
    files = sorted(str(f) for f in LUA_DIR.glob("test_*.lua"))
    cmd = [b["lua"], str(LUA_DIR / "run.lua"), str(LUA_DIR), str(P.ROOT), str(prelude), pattern or "", *files]
    return _tap_suite("lua", cmd, "unit:lua:", b)


# ---------------------------------------------------------------- static unit-layer checks (measurement, task 1.1)

# design.md appendix A "Goes to: unit", minus the ten rewritten as true unit tests (lane.merge,
# client.resolution_default, backups.rotation, saves.inworld_list, lane.preflight, saves.quick_slot, savesctl.listener,
# savesctl.load_verb, savesctl.build_refused, saves.list_wording) and login.backdrop_choice (a Lua unit test; today
# it is a live real-client check).
STATIC_CHECKS = (
    "app.icon app.bundle_valid app.icon_everywhere build.downloads_pinned client.config_gen client.inspect "
    "client.pristine_locked client.build_refuses_pristine client.patch0001_recorded client.play_build "
    "client.play_revert client.resolution_gate core.patch_applied core.playerbots_patches_applied "
    "firstlaunch.preflight lane.paths lane.refusal lane.table power.jobs_default power.jobs_override "
    "power.pause_resume power.run_pause power.stop_low power.bootstrap_resume power.switch_refusal public.license "
    "public.tree_scrub public.no_game_files re.ghidra_installed re.mpq_readonly re.ghidra_refuses_mismatch "
    "re.mpq_write_roundtrip release.mode_resolution release.readline_stub release.test_switches_refused "
    "release.player_data_untouched repo.naming repo.agent_guard_hook repo.portable report.tiers_declared "
    "report.slow_tier_rule report.gui_tier_rule report.backups_tier_rule report.gui_preflight report.keep_awake "
    "report.last_full_run report.nightly saves.addon_static setup.preflight signing.mode_setting "
    "signing.open_instructions").split()


def _run_one_check(cid: str) -> dict:
    import os
    os.environ["HS_HARNESS_RUN"] = "1"           # as inside `hs run` (the unit tier runs there in task 2.1)
    from .registry import load_all
    from . import runner
    reg = load_all()
    ctx: dict = {"unit_layer": True}
    rec = runner.execute([reg[cid]], ctx, ensure=lambda *_a: None, quiet=True)[0]
    return {"id": rec["id"], "status": rec["status"], "duration_s": rec["duration_s"],
            **({"message": rec.get("message", "")[-800:]} if rec["status"] != "pass" else {})}


def run_static(workers: int = 1, ids=STATIC_CHECKS) -> dict:
    t0 = time.perf_counter()
    if workers <= 1:
        recs = [_run_one_check(i) for i in ids]
    else:
        import multiprocessing as mp
        from concurrent.futures import ProcessPoolExecutor
        # longest first, so the pool's tail is short
        order = sorted(ids, key=lambda i: -_STATIC_HINT_S.get(i, 1.0))
        with ProcessPoolExecutor(workers, mp_context=mp.get_context("spawn")) as ex:
            recs = list(ex.map(_run_one_check, order))
    return {"suite": "static", "workers": workers, "wall_s": round(time.perf_counter() - t0, 3), "tests": recs}


# baseline durations (design.md appendix A) used only to order the pool
_STATIC_HINT_S = {"report.keep_awake": 9.3, "power.run_pause": 7.5, "app.bundle_valid": 6.7, "client.play_revert": 6.2,
                  "app.icon": 6.0, "release.test_switches_refused": 6.0, "setup.preflight": 5.1,
                  "firstlaunch.preflight": 4.9, "release.player_data_untouched": 4.7, "re.mpq_readonly": 4.5,
                  "release.readline_stub": 4.4}


# ---------------------------------------------------------------- the run

def run(pattern: str | None = None, static_workers: int = 0, out: Path | None = None) -> dict:
    t0 = time.perf_counter()
    suites = [run_python(pattern), run_cpp(pattern), run_lua(pattern)]
    if static_workers:
        suites.append(run_static(static_workers))
    over = []
    for s in suites:
        if s["suite"] == "static":
            continue                                       # static checks have their own (check) budgets
        for t in s["tests"]:
            if t["status"] == "pass" and t["duration_s"] > UNIT_TEST_CAP_S:
                t["status"] = "fail"
                t["message"] = f"unit test over budget: {t['duration_s']} s > {UNIT_TEST_CAP_S} s"
                over.append(t["id"])
    tests = [t for s in suites for t in s["tests"]]
    bad = [t["id"] for t in tests if t["status"] != "pass"]
    doc = {"tier": "unit", "wall_s": round(time.perf_counter() - t0, 3), "ok": not bad, "failed": bad,
           "over_budget": over, "counts": {s["suite"]: len(s["tests"]) for s in suites}, "suites": suites}
    out = out or RESULTS
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1))
    return doc


def main(pattern: str | None, static_workers: int) -> int:
    doc = run(pattern, static_workers)
    for s in doc["suites"]:
        slow = sorted(s["tests"], key=lambda t: -t["duration_s"])[:3]
        print(f"[hs] unit {s['suite']:6} {len(s['tests']):3} tests  {s['wall_s']:7.2f} s"
              f"{'  build ' + json.dumps(s.get('build')) if s.get('build') else ''}"
              f"  slowest {[(t['id'], t['duration_s']) for t in slow]}")
    for t in (t for s in doc["suites"] for t in s["tests"] if t["status"] != "pass"):
        print(f"[hs] {t['status'].upper()} {t['id']}: {t.get('message', '')[:600]}")
    print(f"[hs] unit layer {'PASS' if doc['ok'] else 'FAIL'} in {doc['wall_s']} s ({RESULTS.relative_to(P.ROOT)})")
    return 0 if doc["ok"] else 1
