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


def run_python(pattern: str | None = None, start: Path | None = None, top: Path | None = None) -> dict:
    """`start`/`top`: a scratch test package and its top-level dir (report.unit_layer's scratch cases)."""
    t0 = time.perf_counter()
    top = Path(top or P.HARNESS)
    if str(top) not in sys.path:
        sys.path.insert(0, str(top))
    suite = unittest.TestLoader().discover(str(start or UNIT_DIR), pattern="test_*.py", top_level_dir=str(top))
    res = _TimedResult()
    if pattern:
        def flat(s):
            for x in s:
                yield from (flat(x) if isinstance(x, unittest.TestSuite) else [x])
        suite = unittest.TestSuite([t for t in flat(suite) if pattern in f"unit:py:{t.id()}"])
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


def _pat(pattern: str | None, runner: str) -> tuple[bool, str | None]:
    """(run this runner?, the pattern its own filter takes): a full id names one runner (unit:cpp:x -> x)."""
    if not pattern:
        return True, None
    if pattern.startswith("unit:"):
        pre = f"unit:{runner}:"
        if not (pre.startswith(pattern) or pattern.startswith(pre)):
            return False, None
        return True, pattern[len(pre):] or None
    return True, pattern


def run_cpp(pattern: str | None = None, tests: Path | None = None, out: Path | None = None) -> dict:
    from . import unittools as U
    go, pattern = _pat(pattern, "cpp")
    if not go:
        return {"suite": "cpp", "wall_s": 0.0, "tests": [], "build": None}
    try:
        b = U.ensure_cpp(tests=tests, out=out)
    except U.ToolError as e:
        return {"suite": "cpp", "wall_s": 0.0, "build": {"error": str(e)},
                "tests": [{"id": "unit:cpp:<build>", "status": "error", "duration_s": 0.0, "message": str(e)}]}
    return _tap_suite("cpp", [b["bin"], *([pattern] if pattern else [])], "unit:cpp:", b)


def run_lua(pattern: str | None = None, tests: Path | None = None, lua_dir: Path | None = None,
            lock: dict | None = None, cache: Path | None = None) -> dict:
    from . import unittools as U
    go, pattern = _pat(pattern, "lua")
    if not go:
        return {"suite": "lua", "wall_s": 0.0, "tests": [], "build": None}
    try:
        b = U.ensure_lua(lua_dir, lock, cache)
    except Exception as e:  # noqa: BLE001  a missing or mismatched build fails the suite, never skips it
        return {"suite": "lua", "wall_s": 0.0, "build": {"error": str(e)},
                "tests": [{"id": "unit:lua:<build>", "status": "error", "duration_s": 0.0, "message": str(e)}]}
    t0 = time.perf_counter()
    from . import luaprelude
    prelude = luaprelude.write_prelude()
    b = b | {"prelude_s": round(time.perf_counter() - t0, 3)}
    tdir = Path(tests or LUA_DIR)
    files = sorted(str(f) for f in tdir.glob("test_*.lua"))
    cmd = [b["lua"], str(LUA_DIR / "run.lua"), str(LUA_DIR), str(P.ROOT), str(prelude), pattern or "", *files]
    return _tap_suite("lua", cmd, "unit:lua:", b)


# ---------------------------------------------------------------- unit-layer checks (the run's unit phase)

def _ctx_subset(ctx: dict) -> dict:
    out = {}
    for k, v in ctx.items():
        try:
            json.dumps(v)
        except (TypeError, ValueError):
            continue
        out[k] = v
    return out


def _pool_one(cid: str, ctx: dict) -> dict:
    import os
    os.environ["HS_HARNESS_RUN"] = "1"
    from .registry import load_all
    from . import runner
    reg = load_all()
    rec = runner.run_one(reg[cid], ctx, lambda *_a: None)
    rec.pop("_t", None)
    return json.loads(json.dumps(rec, default=str))


class ProcSampler:
    """Samples this lane's stack processes and the Wine/client processes while the unit phase runs (harness-reporting
    "Unit tier runs fast, starts nothing"): any process present now that wasn't at the start is a violation."""
    def __init__(self, period: float = 0.5):
        import threading
        self.period, self.samples, self.new = period, 0, {}
        self._stop = threading.Event()
        self.start = self._snap()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _snap(self) -> set:
        from . import stack
        found = set()
        try:
            for name, pids in stack.stack_processes().items():
                found |= {f"{name}:{p}" for p in pids}
        except Exception:  # noqa: BLE001
            pass
        try:
            r = subprocess.run(["/bin/ps", "-axo", "pid=,comm="], capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                pid, _, comm = line.strip().partition(" ")
                base = comm.rsplit("/", 1)[-1].lower()
                if base.startswith(("wine", "wow.exe", "wowsilicon")) or base in ("wow.exe",):
                    found.add(f"{base}:{pid}")
        except (OSError, subprocess.SubprocessError):
            pass
        return found

    def _loop(self):
        while not self._stop.wait(self.period):
            now = self._snap()
            self.samples += 1
            for x in now - self.start:
                self.new.setdefault(x, time.time())

    def stop(self) -> dict:
        self._stop.set()
        self._t.join(timeout=5)
        return {"samples": self.samples, "period_s": self.period, "at_start": sorted(self.start),
                "started_during": sorted(self.new)}


def unit_layer(checks: list, ctx: dict, run_tests: bool = True, workers: int = 4, say=print, guard=None) -> dict:
    """The run's unit phase: the unit tests (Python, C++, Lua) and the unit-layer checks (parallel_safe ones in a
    `workers`-process pool, the rest one by one), never touching the stack. Returns meta.unit plus "records" (the
    checks' records, for results.json)."""
    from . import runner
    t0 = time.perf_counter()
    sampler = ProcSampler()
    suites = []
    if run_tests:
        suites = [run_python(), run_cpp(), run_lua()]
        for s_ in suites:
            for t in s_["tests"]:
                if t["status"] == "pass" and t["duration_s"] > UNIT_TEST_CAP_S:
                    t["status"] = "fail"
                    t["message"] = f"unit test over budget: {t['duration_s']} s > {UNIT_TEST_CAP_S} s"
    t_tests = round(time.perf_counter() - t0, 3)
    pooled = [c for c in checks if c.parallel_safe]
    serial = [c for c in checks if not c.parallel_safe]
    records = []
    t_c = time.perf_counter()
    if pooled and workers > 1:
        import multiprocessing as mp
        from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
        sub = _ctx_subset(ctx)
        by = {}
        todo = list(pooled)
        running = {}
        with ProcessPoolExecutor(min(workers, len(pooled)), mp_context=mp.get_context("spawn")) as ex:
            while todo or running:
                while todo and len(running) < workers and not ctx.get("power_stop"):
                    c = todo[0]
                    if guard is not None:            # power-guard: checked before each check starts, as in execute
                        from .power import PowerStop
                        try:
                            guard.check(f"check {c.id}")
                        except PowerStop as e:
                            ctx["power_stop"] = {"message": str(e), "before_check": c.id, "pause": e.record}
                            break
                    todo.pop(0)
                    running[ex.submit(_pool_one, c.id, sub)] = c
                if not running:
                    break
                done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                for fut in done:
                    c = running.pop(fut)
                    try:
                        rec = fut.result()
                    except Exception as e:  # noqa: BLE001
                        rec = {"id": c.id, "capability": c.capability, "description": c.doc, "layer": c.layer,
                               "status": "error", "message": f"pool worker: {type(e).__name__}: {e}", "duration_s": 0.0,
                               "evidence": {"pool": True}}
                    rec["pool"] = True
                    say(f"{rec['status'].upper():5} {rec['id']} ({rec['duration_s']}s, pool)"
                        f"{'  ' + rec.get('message', '') if rec['status'] != 'pass' else ''}")
                    by[c.id] = rec
        records += [by[c.id] for c in pooled if c.id in by]
    else:
        serial = pooled + serial
    if serial and not ctx.get("power_stop"):
        records += runner.execute(serial, ctx, ensure=lambda *_a: None, guard=guard)
    from .runner import run_key
    from .registry import REGISTRY
    records.sort(key=lambda r: run_key(REGISTRY[r["id"]]) if r["id"] in REGISTRY else ((99,), r["id"]))
    t_checks = round(time.perf_counter() - t_c, 3)
    procs = sampler.stop()
    tests = [t for s_ in suites for t in s_["tests"]]
    bad_tests = [t["id"] for t in tests if t["status"] != "pass"]
    bad_checks = [r["id"] for r in records if r["status"] != "pass"]
    doc = {"run_tests": run_tests, "wall_s": round(time.perf_counter() - t0, 3), "tests_wall_s": t_tests,
           "checks_wall_s": t_checks, "workers": workers, "pooled": [c.id for c in pooled],
           "suites": [{k: v for k, v in s_.items()} for s_ in suites],
           "counts": {s_["suite"]: len(s_["tests"]) for s_ in suites} | {"checks": len(records)},
           "failed_tests": bad_tests, "failed_checks": bad_checks, "processes": procs,
           "ok": not bad_tests and not bad_checks and not procs["started_during"], "records": records}
    for s_ in suites:
        say(f"unit {s_['suite']:6} {len(s_['tests']):3} tests {s_['wall_s']:6.2f} s"
            + (f" ({len([t for t in s_['tests'] if t['status'] != 'pass'])} failing)" if any(t['status'] != 'pass' for t in s_['tests']) else ""))
    for t in tests:
        if t["status"] != "pass":
            say(f"{t['status'].upper()} {t['id']}: {t.get('message', '')[:400]}")
    if procs["started_during"]:
        say(f"FAIL  unit layer started processes: {procs['started_during']}")
    return doc


# ---------------------------------------------------------------- the inner loop (`hs test -k`)

def run(pattern: str | None = None, out: Path | None = None) -> dict:
    """Unit tests only (no checks), optionally only ids containing `pattern`: the builder's inner loop."""
    t0 = time.perf_counter()
    suites = [run_python(pattern), run_cpp(pattern), run_lua(pattern)]
    over = []
    for s in suites:
        for t in s["tests"]:
            if t["status"] == "pass" and t["duration_s"] > UNIT_TEST_CAP_S:
                t["status"] = "fail"
                t["message"] = f"unit test over budget: {t['duration_s']} s > {UNIT_TEST_CAP_S} s"
                over.append(t["id"])
    tests = [t for s in suites for t in s["tests"]]
    bad = [t["id"] for t in tests if t["status"] != "pass"]
    doc = {"tier": "unit-tests", "pattern": pattern, "wall_s": round(time.perf_counter() - t0, 3), "ok": not bad,
           "failed": bad, "over_budget": over, "counts": {s["suite"]: len(s["tests"]) for s in suites}, "suites": suites}
    out = out or RESULTS
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1))
    return doc


def main(pattern: str | None) -> int:
    doc = run(pattern)
    for s in doc["suites"]:
        print(f"[hs] unit {s['suite']:6} {len(s['tests']):3} tests  {s['wall_s']:7.2f} s")
    for t in (t for s in doc["suites"] for t in s["tests"] if t["status"] != "pass"):
        print(f"[hs] {t['status'].upper()} {t['id']}: {t.get('message', '')[:600]}")
    print(f"[hs] unit tests {'PASS' if doc['ok'] else 'FAIL'} in {doc['wall_s']} s ({RESULTS.relative_to(P.ROOT)})")
    return 0 if doc["ok"] else 1


# ---------------------------------------------------------------- unit test ids (report.coverage)

def list_ids() -> set[str]:
    """Every unit test id, without running anything: Python by discovery (imports the test modules), C++ by the
    MG_TEST macros, Lua by the test files' keys."""
    import re
    ids = set()
    if str(P.HARNESS) not in sys.path:
        sys.path.insert(0, str(P.HARNESS))
    suite = unittest.TestLoader().discover(str(UNIT_DIR), pattern="test_*.py", top_level_dir=str(P.HARNESS))

    def flat(s):
        for x in s:
            yield from (flat(x) if isinstance(x, unittest.TestSuite) else [x])
    ids |= {f"unit:py:{t.id()}" for t in flat(suite)}
    for f in sorted((UNIT_DIR / "cpp").glob("*.cpp")):
        ids |= {f"unit:cpp:{a}.{b}" for a, b in re.findall(r"^MG_TEST\((\w+),\s*(\w+)\)", f.read_text(), re.M)}
    for f in sorted(LUA_DIR.glob("test_*.lua")):
        ids |= {f"unit:lua:{f.stem}.{n}" for n in re.findall(r"^\t(\w+) = function\(T\)", f.read_text(), re.M)}
    return ids
