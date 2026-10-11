"""harness-reporting meta-checks. The post-run records (POST_IDS) run after all other checks, over the results
document; the rule checks of the test pyramid live in pyramid_checks.py."""
import fnmatch
import json
import re
import subprocess
import time
from pathlib import Path

from .. import paths as P
from ..registry import REGISTRY, check, require

SCENARIO_RE = re.compile(r"^#### Scenario:\s*(.+)$", re.M)
CHECK_RE = re.compile(r"Check:\s*`([^`]+)`")
HUMAN_RE = re.compile(r"\[human-verify\]\s*(\S.{10,})")
REQ_RE = re.compile(r"^### Requirement:\s*(.+)$", re.M)
POST_IDS = ("report.coverage", "report.schema", "report.full_run", "report.client_pristine", "report.timing",
            "report.lanes", "report.unit_layer", "report.layer_budgets", "report.pr_average", "report.journeys",
            "report.fixture_cleanup")


ACTIVE_FILE = P.HARNESS / "active-changes"

def active_changes() -> list[str]:
    """Changes being built or verified (one id per line in harness/active-changes). Drafted-but-unstarted
    changes are planning only: reported, but they don't fail coverage/full_run until listed here."""
    if not ACTIVE_FILE.exists():
        return []
    return [l.strip() for l in ACTIVE_FILE.read_text().splitlines() if l.strip() and not l.startswith("#")]


def _change_dirs():
    return [d for d in (P.ROOT / "openspec" / "changes").iterdir() if d.is_dir() and d.name != "archive"]


def spec_files(include_planned=False):
    """Archived specs + active changes' spec deltas (+ planned changes when include_planned)."""
    active = set(active_changes())
    roots = [P.ROOT / "openspec" / "specs"] + [d / "specs" for d in _change_dirs()
                                                if include_planned or d.name in active]
    return sorted(f for r in roots if r.exists() for f in r.rglob("spec.md"))




def _delta_names(text: str) -> dict:
    """Requirement names per section (ADDED / MODIFIED / REMOVED / RENAMED) of a spec delta."""
    out = {"ADDED": set(), "MODIFIED": set(), "REMOVED": set(), "RENAMED": set()}
    sec = None
    for line in text.splitlines():
        m = re.match(r"^## (ADDED|MODIFIED|REMOVED|RENAMED) Requirements", line)
        if m:
            sec = m.group(1)
            continue
        r = re.match(r"^### Requirement:\s*(.+)$", line)
        if r and sec:
            out[sec].add(r.group(1).strip())
    return out


def superseded() -> dict:
    """{capability: requirement names an active change's delta modifies or removes}: their archived versions no longer
    gate a run (the delta's own version does, or nothing does for a removed one)."""
    out: dict = {}
    active = set(active_changes())
    for d in _change_dirs():
        if d.name not in active:
            continue
        for f in (d / "specs").rglob("spec.md") if (d / "specs").exists() else ():
            n = _delta_names(f.read_text())
            out.setdefault(f.parent.name, set()).update(n["MODIFIED"] | n["REMOVED"])
    return out


def scenarios(include_planned=False):
    """Yield (file, scenario name, check ids, human reason). An archived requirement that an active change modifies or
    removes is skipped (its delta's version is yielded from the delta file instead)."""
    sup = superseded()
    archived = P.ROOT / "openspec" / "specs"
    for f in spec_files(include_planned):
        text = f.read_text()
        skip_spans = []
        if f.is_relative_to(archived) and sup.get(f.parent.name):
            reqs = list(REQ_RE.finditer(text))
            for i, r in enumerate(reqs):
                if r.group(1).strip() in sup[f.parent.name]:
                    end = reqs[i + 1].start() if i + 1 < len(reqs) else len(text)
                    nxt2 = re.search(r"^## ", text[r.end():end], re.M)
                    skip_spans.append((r.start(), r.end() + nxt2.start() if nxt2 else end))
        heads = list(SCENARIO_RE.finditer(text))
        for i, m in enumerate(heads):
            if any(a <= m.start() < b for a, b in skip_spans):
                continue
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            nxt = re.search(r"^#{2,3} ", text[m.end():end], re.M)
            body = text[m.end(): m.end() + nxt.start()] if nxt else text[m.end():end]
            hm = HUMAN_RE.search(body)
            yield str(f.relative_to(P.ROOT)), m.group(1).strip(), CHECK_RE.findall(body), (hm.group(1).strip() if hm else None)


UNIT_ID_RE = re.compile(r"^unit:(py|cpp|lua):\S+$")


def coverage() -> tuple[bool, dict]:
    from ..registry import known_ids
    from .. import unitrun
    unit_ids = unitrun.list_ids()
    known = known_ids() | set(POST_IDS) | unit_ids
    bad, mapped, human, referenced = [], 0, [], set()
    for f, name, ids, reason in scenarios():
        referenced.update(ids)
        unknown = [i for i in ids if i not in known]
        if ids and not unknown:
            mapped += 1
        elif not ids and reason:
            human.append({"file": f, "scenario": name, "reason": reason})
        else:
            bad.append({"file": f, "scenario": name, "check_ids": ids, "unregistered": unknown,
                        "problem": ("unknown unit test id" if any(UNIT_ID_RE.match(i) for i in unknown)
                                    else "unregistered check id") if unknown else "no Check: id and no [human-verify] reason"})
    active = set(active_changes())
    planned = sorted(d.name for d in _change_dirs() if d.name not in active)
    # A change with any ticked task has started; it must be listed as active so its scenarios gate the run.
    started_unlisted = []
    for d in _change_dirs():
        t = d / "tasks.md"
        if d.name not in active and t.exists() and re.search(r"^\s*- \[[xX]\]", t.read_text(), re.M):
            started_unlisted.append(d.name)
    for name in started_unlisted:
        bad.append({"file": f"openspec/changes/{name}/tasks.md", "scenario": "(change)", "check_ids": [],
                    "unregistered": [], "problem": "change has completed tasks but isn't listed in harness/active-changes"})
    planned_unimpl = sorted({i for _, _, ids, _ in scenarios(include_planned=True) for i in ids} - known - referenced)
    return not bad, {"started_but_unlisted": started_unlisted, "scenarios_mapped": mapped, "human_verify": human, "incomplete": bad,
                     "referenced_ids": sorted(referenced), "spec_files": [str(f.relative_to(P.ROOT)) for f in spec_files()],
                     "superseded_requirements": {k: sorted(v) for k, v in superseded().items()},
                     "unit_test_ids": len(unit_ids),
                     "active_changes": sorted(active), "planned_changes": planned,
                     "planned_checks_not_yet_registered": planned_unimpl}


def coverage_record(doc: dict) -> dict:
    """report.coverage: every scenario maps; on a full run every referenced unit test passed in this run's unit layer."""
    t0 = time.time()
    ok, ev = coverage()
    m = doc["meta"]
    if m.get("full_run"):
        tests = {t["id"]: t["status"] for s in ((m.get("unit") or {}).get("suites") or []) for t in s["tests"]}
        ref_units = sorted(i for i in ev["referenced_ids"] if UNIT_ID_RE.match(i))
        not_passed = [i for i in ref_units if tests.get(i) != "pass"]
        ev["referenced_unit_tests"] = ref_units
        ev["referenced_unit_tests_not_passed"] = not_passed
        if not_passed:
            ok = False
    msg = None
    if not ok:
        msg = (f"{len(ev['incomplete'])} scenario(s) without a valid check mapping"
               + (f"; referenced unit tests not passing: {ev.get('referenced_unit_tests_not_passed')}"
                  if ev.get("referenced_unit_tests_not_passed") else ""))
    return {"id": "report.coverage", "capability": "harness-reporting", "status": "pass" if ok else "fail",
            "evidence": ev, "duration_s": round(time.time() - t0, 2), **({"message": msg} if msg else {})}


def _validate(inst, schema, path="$"):
    errs = []
    t = schema.get("type")
    tmap = {"object": dict, "array": list, "string": str, "boolean": bool, "number": (int, float),
            "null": type(None)}
    ts = t if isinstance(t, list) else [t] if t else []
    if ts and not any(isinstance(inst, tmap[x]) and not (x == "number" and isinstance(inst, bool)) for x in ts):
        return [f"{path}: expected {t}"]
    if "enum" in schema and inst not in schema["enum"]:
        errs.append(f"{path}: {inst!r} not in {schema['enum']}")
    if isinstance(inst, dict):
        errs += [f"{path}: missing {k}" for k in schema.get("required", []) if k not in inst]
        if len(inst) < schema.get("minProperties", 0):
            errs.append(f"{path}: fewer than {schema['minProperties']} properties")
        for k, sub in schema.get("properties", {}).items():
            if k in inst:
                errs += _validate(inst[k], sub, f"{path}.{k}")
    if isinstance(inst, list) and "items" in schema:
        for i, x in enumerate(inst):
            errs += _validate(x, schema["items"], f"{path}[{i}]")
    return errs




# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "harness/active-changes",
    "harness/results.schema.json",
    "harness/timing-baseline.json",
    "harness/hslib/runner.py",
    "harness/hslib/selection.py",
    "harness/hslib/unitrun.py",
    "harness/hslib/unittools.py",
    "harness/hslib/fixture.py",
    "harness/hslib/sharedfix.py",
    "harness/hslib/checks/lane_checks.py",
    "harness/hslib/checks/pristine_checks.py",
    "harness/hslib/checks/reporting.py",
    "harness/hslib/pristine.py",
)


def _repo_rel(p: Path) -> str:
    """p relative to the repo, or absolute when it lies outside (`hs run --results-dir` elsewhere)."""
    try:
        return str(Path(p).resolve().relative_to(P.ROOT.resolve()))
    except ValueError:
        return str(Path(p).resolve())




def full_run_record(doc: dict, referenced: set, extra: list, reg: dict | None = None) -> dict:
    """report.full_run over a results document (+ the post-run records in `extra`): a full run passes only if every
    scenario-referenced check has a record and every record passes, except tolerated skips (runner.tolerated_skip:
    a ci_only check's "not applicable" outside CI)."""
    from ..runner import tolerated_skip
    m = doc["meta"]
    referenced = {i for i in referenced if not i.startswith("unit:")} - set(POST_IDS)
    executed = {r["id"] for r in doc["checks"]}
    not_run = sorted(referenced - executed)
    tolerated = [r for r in doc["checks"] if r["status"] == "skip" and tolerated_skip(r, m, reg)]
    ok_skips = {r["id"] for r in tolerated}
    failing = sorted(r["id"] for r in doc["checks"] + extra if r["status"] != "pass" and r["id"] not in ok_skips)
    full = m["full_run"]
    ok = full and not not_run and not failing
    # Not a full run (tier unit or pr, or --only): skip, which only for this record and only then doesn't fail the run
    # (runner.tolerated_skip). A full run must pass; it can never be skipped.
    status = "pass" if ok else ("fail" if full else "skip")
    why = [] if full else [f"not a full run (tier {m.get('tier')}{', filtered' if m.get('filter') else ''})"]
    return {"id": "report.full_run", "capability": "harness-reporting", "status": status,
            "evidence": {"full_run": full, "tier": m.get("tier"), "filter": m.get("filter"),
                         "spec_ids_not_executed": not_run, "tolerated_skips": sorted(ok_skips),
                         "non_passing": failing, "checks_executed": len(executed) - len(ok_skips),
                         "outputs": [_repo_rel(P.RESULTS), _repo_rel(P.SUMMARY)]},
            "duration_s": 0.0,
            **({} if ok else {"message": (why[0] if not full else
                                          "not a clean full run: " + ", ".join(not_run + failing))[:300]})}


def post_run(doc, selected_ids, t_start=None, leave_up=False, pristine_start=None):
    from . import pyramid_checks as PY
    out = [coverage_record(doc)]
    m = doc["meta"]
    out.append(PY.unit_layer_record(doc))
    out.append(PY.journeys_record(doc))
    out.append(fixture_cleanup_record(doc))
    for r in out:
        r.setdefault("layer", "unit")          # the post-run reports start nothing: unit layer
    t0 = time.time()
    schema = json.loads((P.HARNESS / "results.schema.json").read_text())
    # validate the document as it will be written (including the records so far)
    errs = _validate(json.loads(json.dumps({"meta": doc["meta"], "checks": doc["checks"] + out}, default=str)), schema)
    empty = [r["id"] for r in doc["checks"] + out if not r.get("evidence")]
    ok = not errs and not empty
    out.append({"id": "report.schema", "capability": "harness-reporting", "status": "pass" if ok else "fail",
                "evidence": {"schema": "harness/results.schema.json", "errors": errs[:50], "empty_evidence": empty,
                             "records_validated": len(doc["checks"]) + len(out)},
                "duration_s": round(time.time() - t0, 2), **({} if ok else {"message": "results.json fails schema"})})

    # report.client_pristine (easy-setup, play-client): the pristine client unchanged since the run started; its
    # self-test (nested runs on a scratch stand-in) runs on unfiltered runs and when the filter names it, never nested
    from . import pristine_checks
    from .. import pristine as _pristine
    import os as _os
    selftest = (not _os.environ.get(_pristine.TEST_ENV)
                and (not m.get("filter") or any(fnmatch.fnmatch("report.client_pristine", g) for g in m.get("filter") or [])))
    out.append(pristine_checks.post_run_record(pristine_start, selftest))

    # report.lanes (second-stack): meta.lanes agrees with the records; a two-lane full run ran every LANE_B check in B
    from .lane_checks import lanes_record
    out.append(lanes_record(doc, time.time() - (t_start or time.time())))
    out.append(PY.pr_average_record(doc))
    out.append(PY.layer_budgets_record(doc))
    out.append(full_run_record(doc, set(out[0]["evidence"]["referenced_ids"]), out))
    # last, so its wall time covers everything before it
    out.append(timing_record(doc, out, time.time() - (t_start or time.time())))
    for r in out:
        r.setdefault("layer", "unit")
    return out


def fixture_cleanup_problems(fx: dict | None) -> list[str]:
    """The shared-server fixture's end-of-run cleanup (meta.fixture): an identity it couldn't set up, a cleanup that
    errored or left errors, or identities created but never cleaned up."""
    if not fx:
        return []
    probs = []
    c = fx.get("cleanup")
    made = bool(fx.get("identities")) or bool(fx.get("identity_setup_failures"))
    if made and not c:
        probs.append("the fixture's identities were never cleaned up")
    if c and c.get("error"):
        probs.append(f"fixture cleanup failed: {c['error']}"[:200])
    if c and c.get("errors"):
        probs.append(f"fixture cleanup errors: {c['errors'][:3]}"[:200])
    if fx.get("identity_setup_failures"):
        probs.append(f"fixture identity setup failed for {sorted(fx['identity_setup_failures'])}")
    if fx.get("start_error"):
        probs.append(f"fixture start failed: {fx['start_error']}"[:200])
    return probs


def fixture_cleanup_record(doc: dict) -> dict:
    """report.fixture_cleanup: the shared-server fixture set up and removed its identities without an error."""
    fx = doc["meta"].get("fixture")
    probs = fixture_cleanup_problems(fx)
    ok = not probs
    return {"id": "report.fixture_cleanup", "capability": "harness-control", "status": "pass" if ok else "fail",
            "evidence": {"fixture_ran": bool(fx), "cleanup": (fx or {}).get("cleanup"), "problems": probs},
            "duration_s": 0.0, **({} if ok else {"message": "; ".join(probs)[:300]})}


# ----------------------------------------------------------------------------- full-run timing

TIMING_BASELINE = P.HARNESS / "timing-baseline.json"
TIMING_TARGET = 1.10              # spec: the baseline checks together take <= 110 % of their baseline times


def timing_problems(doc: dict, extra: list, wall_s: float, baseline: dict | None, reg: dict | None = None) -> tuple:
    """report.timing over a results document (+ the post-run records before it), harness-reporting "Full-run timing is
    recorded and guarded" (seamless-experience: per-check baseline). The timing summary covers every record and its
    build time, backups-tier total, new checks and warnings agree with the records. On an unfiltered full-tier run:
    every baseline check ran (minus by-design GUI and backups-tier skips), and the executed baseline checks' times,
    each minus its time spent building server binaries (runner.check_build_s), sum to <= TIMING_TARGET x the same
    checks' baseline sum. Checks not in the baseline and build time are reported, never failed; a new check over
    runner.NEW_CHECK_WARN_S is a warning."""
    from ..runner import TIMING_SELF, check_build_s, timing_summary
    m = doc["meta"]
    recs = doc["checks"] + extra
    t = m.get("timing") or {}
    per = t.get("per_check_s") or {}
    probs = []
    main_ids = [r["id"] for r in doc["checks"]]
    missing = sorted(set(main_ids) - set(per))
    if not t or missing:
        probs.append(f"timing summary missing or incomplete (no time for {missing[:10]})")
    top = t.get("top10") or []
    want_top = sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    if [(x.get("id"), x.get("s")) for x in top] != [(i, v) for i, v in want_top]:
        probs.append("top10 isn't the 10 slowest checks of per_check_s")
    if not isinstance(t.get("checks_sum_s"), (int, float)):
        probs.append("no checks_sum_s in the timing summary")
    if doc["meta"].get("tier") and not isinstance(doc["meta"].get("layers"), dict) and "synthetic" not in doc["meta"]:
        probs.append("no per-layer timing (meta.layers)")
    want = timing_summary(doc["checks"], None, reg, baseline)
    for k in ("build_s", "new_checks_s", "warnings"):
        if t.get(k) != want.get(k):
            probs.append(f"the timing summary's {k} ({t.get(k)}) doesn't match the records ({want.get(k)})")
    ev = {"timing_summary_checks": len(per), "records": len(recs), "top10": top, "wall_s_now": round(wall_s, 1),
          # the run's total build time, under a name that isn't `build_s` (B32: report.timing's own record would
          # otherwise read as a check claiming build time, runner.build_claim_warnings)
          "layers": doc["meta"].get("layers"), "run_build_s": want["build_s"], "build_by_check_s": want["build_by_check_s"],
          "new_checks_s": want.get("new_checks_s"), "warnings": want.get("warnings"),
          "full_tier_unfiltered": m.get("full_run") is True and m.get("tier") == "full"}
    if not ev["full_tier_unfiltered"]:
        ev["speed_target"] = ("not evaluated: the guard compares an unfiltered full-tier run, this is "
                              f"tier {m.get('tier')}{' (filtered)' if m.get('filter') else ''}")
        return probs, ev
    if not baseline:
        probs.append(f"no readable baseline at {TIMING_BASELINE.relative_to(P.ROOT)}")
        return probs, ev
    # second-stack: like with like. A two-lane run's per-check times include the other lane's load, so they're
    # compared with a two-lane baseline (checks_lanes2), never with the serial one (checks), and the reverse.
    mode = lane_mode(m)
    ev["lane_mode"] = mode
    base = baseline["checks"] if mode == 1 else baseline.get(LANES2_KEY)
    if not base:
        ev["speed_target"] = (f"not evaluated: no {mode}-lane per-check baseline yet ({LANES2_KEY} in "
                              f"{TIMING_BASELINE.relative_to(P.ROOT)}); join this clean run with `harness/hs "
                              "timing-baseline join --label <change>`. Per-check times of a two-lane run aren't "
                              "compared with serial ones")
        return probs, ev
    by_id = {r["id"]: r for r in recs}
    executed = {r["id"] for r in recs if r["status"] != "skip"}
    need = set(base) - {TIMING_SELF}
    not_run = sorted(need - executed)
    compared = sorted(need & executed)
    now = {i: round(float(by_id[i].get("duration_s") or 0.0) - check_build_s(by_id[i]), 2) for i in compared}
    now_sum = round(sum(now.values()), 1)
    base_sum = round(sum(float(base[i]["s"]) for i in compared), 1)
    limit = round(TIMING_TARGET * base_sum, 1)
    slower = sorted(((i, round(now[i] - float(base[i]["s"]), 1)) for i in compared), key=lambda kv: -kv[1])[:10]
    ev |= {"baseline_runs": baseline.get("runs"), "baseline_checks": len(base), "compared_checks": len(compared),
           "baseline_ids_not_executed_now": not_run, "now_sum_s": now_sum, "baseline_sum_s": base_sum,
           "limit_s": limit, "ratio": round(now_sum / base_sum, 3) if base_sum else None,
           "most_slower_s": slower,
           "same_machine": (m.get("host") or {}).get("machine") == (baseline.get("host") or {}).get("machine")}
    if not ev["same_machine"]:
        probs.append(f"not the baseline's machine type ({(m.get('host') or {}).get('machine')} vs "
                     f"{(baseline.get('host') or {}).get('machine')})")
    if not_run:
        probs.append(f"baseline checks not executed in this run: {not_run}")
    if now_sum > limit:
        probs.append(f"the baseline checks took {now_sum:.0f} s (build time excluded) > {limit:.0f} s "
                     f"({int(TIMING_TARGET * 100)} % of their baseline {base_sum:.0f} s)")
    return probs, ev


RACE_BASELINE = "core.login_logout_race_baseline"     # on runner.BUILD_CLAIM_ALLOWLIST
LANES2_KEY = "checks_lanes2"          # the two-lane per-check baseline (timingbase.join of a two-lane full run)


def lane_mode(meta: dict) -> int:
    """1 or 2: the lanes a run actually used (meta.lanes.mode; runs from before second-stack were serial)."""
    return 2 if (meta.get("lanes") or {}).get("mode") == 2 else 1


def synthetic_timing_cases() -> dict:
    """harness-reporting "Slowdowns fail, new checks and rebuilds don't" and (easy-setup, F33) "Only allowlisted checks
    may claim build time": timing_problems on synthetic full-run documents against a known baseline (t.a 600 s, the
    race baseline 400 s, build.clean 20 s of build). (a) the baseline checks 15 % slower; (b) unchanged + 300 s of new
    checks; (c) unchanged + a from-empty build adding 400 s (build.clean, and the allowlisted race baseline's own
    rebuild via evidence build_s); (d) one new check of 200 s; (e) the non-allowlisted t.a 600 s slower, claiming
    600 s of build_s; (f) the race baseline claiming its real rebuild time (its record in harness/timing-baseline.json:
    duration and build_s); (g) report.timing's own record over case (c) raises no claim warning (its run total is
    `run_build_s`, not a claim). Each case's verdict is computed by the guard's own code (runner.timing_summary +
    timing_problems)."""
    from .. import runner
    from ..registry import Check
    ids = ("t.a", RACE_BASELINE, "build.clean", "t.new1", "t.new2", "t.big")
    reg = {i: Check(i, lambda c: {}, n, "synthetic", "any", "", "integration") for n, i in enumerate(ids)}
    base = {"host": {"machine": "arm64"}, "runs": {"r": {"source": "synthetic"}},
            "checks": {"t.a": {"s": 600.0, "run": "r"}, RACE_BASELINE: {"s": 400.0, "run": "r"},
                       "build.clean": {"s": 0.0, "run": "r", "build_s": 20.0}}}
    try:
        real = runner.load_timing_baseline(TIMING_BASELINE)["checks"][RACE_BASELINE]
        real = {"s": float(real["s"]), "duration_s": float(real["duration_s"]), "build_s": float(real["build_s"])}
    except (OSError, ValueError, KeyError, TypeError) as e:
        real = {"error": f"{type(e).__name__}: {e}"}

    def r(i, s, build=None):
        return {"id": i, "status": "pass", "duration_s": s, "evidence": {"x": 1, **({"build_s": build} if build else {})}}

    def run(recs, b=base, lanes=1):
        meta = {"full_run": True, "tier": "full", "filter": [], "host": {"machine": "arm64"}, "synthetic": True,
                "lanes": {"mode": lanes}}
        wall = sum(x["duration_s"] for x in recs)
        meta["timing"] = runner.timing_summary(recs, wall, reg, b)
        probs, ev = timing_problems({"meta": meta, "checks": recs}, [], wall, b, reg)
        return {"problems": probs, "now_sum_s": ev.get("now_sum_s"), "limit_s": ev.get("limit_s"),
                "lane_mode": ev.get("lane_mode"), "speed_target": ev.get("speed_target"),
                "build_s": ev.get("run_build_s"), "build_by_check_s": ev.get("build_by_check_s"),
                "new_checks_s": ev.get("new_checks_s"), "warnings": ev.get("warnings"), "_ev": ev}
    cases = {
        "a_baseline_15pct_slower": run([r("t.a", 690.0), r(RACE_BASELINE, 460.0), r("build.clean", 20.0)]),
        "b_new_checks_300s": run([r("t.a", 600.0), r(RACE_BASELINE, 400.0), r("build.clean", 20.0),
                                  r("t.new1", 150.0), r("t.new2", 150.0)]),
        "c_from_empty_build_400s": run([r("t.a", 600.0), r(RACE_BASELINE, 550.0, build=150.0),
                                        r("build.clean", 270.0)]),
        "d_new_check_200s": run([r("t.a", 600.0), r(RACE_BASELINE, 400.0), r("build.clean", 20.0), r("t.big", 200.0)]),
        "e_unallowlisted_claim_600s": run([r("t.a", 1200.0, build=600.0), r(RACE_BASELINE, 400.0),
                                           r("build.clean", 20.0)]),
    }
    if "error" not in real:
        base_f = {**base, "checks": {**base["checks"], RACE_BASELINE: {"s": real["s"], "run": "r"}}}
        cases["f_race_baseline_real_rebuild"] = run([r("t.a", 600.0), r(RACE_BASELINE, real["duration_s"],
                                                                         build=real["build_s"]),
                                                     r("build.clean", 20.0)], base_f) | {"real_record": real}
    else:
        cases["f_race_baseline_real_rebuild"] = {"problems": [real["error"]], "real_record": real}
    # second-stack (h-j): like with like. (h) a two-lane run 15 % slower than the serial baseline, with no two-lane
    # baseline: not compared (not evaluated, no problem); (i) a two-lane run 15 % slower than a two-lane baseline:
    # fails; (j) a serial run equal to the serial baseline passes although the two-lane baseline is far faster
    b2 = {**base, "checks_lanes2": {"t.a": {"s": 500.0, "run": "r"}, RACE_BASELINE: {"s": 300.0, "run": "r"},
                                    "build.clean": {"s": 0.0, "run": "r", "build_s": 20.0}}}
    cases["h_two_lane_no_lanes2_baseline"] = run([r("t.a", 690.0), r(RACE_BASELINE, 460.0), r("build.clean", 20.0)],
                                                 base, lanes=2)
    cases["i_two_lane_15pct_slower_than_lanes2"] = run([r("t.a", 575.0), r(RACE_BASELINE, 345.0),
                                                        r("build.clean", 20.0)], b2, lanes=2)
    cases["j_serial_vs_serial_with_lanes2_present"] = run([r("t.a", 600.0), r(RACE_BASELINE, 400.0),
                                                           r("build.clean", 20.0)], b2, lanes=1)
    # (g) report.timing's own record (its evidence over case c, a run with 420 s of build time) next to the other
    # records: the guard's summary of the run's build time is not a claim, so no warning names report.timing
    g_recs = [r("t.a", 600.0), r(RACE_BASELINE, 550.0, build=150.0), r("build.clean", 270.0)]
    g_self = {"id": runner.TIMING_SELF, "status": "pass", "duration_s": 0.5, "evidence": cases["c_from_empty_build_400s"]["_ev"]}
    cases["g_timing_self_record"] = {"warnings": runner.build_claim_warnings(g_recs + [g_self]),
                                     "self_run_build_s": g_self["evidence"].get("run_build_s")}
    for v in cases.values():
        v.pop("_ev", None)
    a, b_, c, d, e, f = (cases[k] for k in ("a_baseline_15pct_slower", "b_new_checks_300s", "c_from_empty_build_400s",
                                            "d_new_check_200s", "e_unallowlisted_claim_600s",
                                            "f_race_baseline_real_rebuild"))
    claim = lambda case, cid: any(f"`{cid}` claims" in w for w in case.get("warnings") or [])
    h, i_, j = (cases[k] for k in ("h_two_lane_no_lanes2_baseline", "i_two_lane_15pct_slower_than_lanes2",
                                   "j_serial_vs_serial_with_lanes2_present"))
    verdict = {
        "a_fails": bool(a["problems"]),
        "h_two_lane_not_compared_with_serial": not h["problems"] and h["lane_mode"] == 2
                                               and "not evaluated" in (h["speed_target"] or "") and h["now_sum_s"] is None,
        "i_two_lane_slowdown_fails": bool(i_["problems"]) and i_["lane_mode"] == 2 and i_["limit_s"] == 880.0,
        "j_serial_compared_with_serial": not j["problems"] and j["lane_mode"] == 1 and j["limit_s"] == 1100.0,
        "b_passes_reporting_new_time": not b_["problems"] and b_["new_checks_s"] == {"t.new1": 150.0, "t.new2": 150.0}
                                       and not b_["warnings"],
        "c_passes_reporting_build_time": not c["problems"] and c["build_s"] == 420.0 and not c["new_checks_s"],
        "d_passes_with_warning": not d["problems"] and d["new_checks_s"] == {"t.big": 200.0}
                                 and any("t.big" in w for w in d["warnings"] or []),
        "e_fails_and_warns_about_the_claim": bool(e["problems"]) and claim(e, "t.a")
                                             and not (e["build_by_check_s"] or {}).get("t.a"),
        "g_own_record_is_not_a_claim": cases["g_timing_self_record"]["self_run_build_s"] == 420.0
                                        and not cases["g_timing_self_record"]["warnings"],
        "f_passes_reporting_the_build_time": not f["problems"] and "error" not in real
                                             and (f.get("build_by_check_s") or {}).get(RACE_BASELINE) == real.get("build_s")
                                             and not claim(f, RACE_BASELINE),
    }
    return {"cases": cases, "verdict": verdict, "allowlist": {"whole_check": list(runner.BUILD_CHECKS),
                                                              "evidence_build_s": runner.BUILD_CLAIM_ALLOWLIST}}


def timing_record(doc: dict, extra: list, wall_s: float) -> dict:
    from ..runner import load_timing_baseline
    t0 = time.time()
    try:
        baseline, err = load_timing_baseline(TIMING_BASELINE), None
    except (OSError, ValueError) as e:
        baseline, err = None, str(e)
    probs, ev = timing_problems(doc, extra, wall_s, baseline)
    if err:
        ev["baseline_error"] = err
    syn = synthetic_timing_cases()
    ev["synthetic"] = syn
    probs += [f"synthetic case {k} has the wrong verdict" for k, ok in syn["verdict"].items() if not ok]
    ok = not probs
    return {"id": "report.timing", "capability": "harness-reporting", "status": "pass" if ok else "fail",
            "evidence": ev | {"problems": probs, "baseline_file": str(TIMING_BASELINE.relative_to(P.ROOT))},
            "duration_s": round(time.time() - t0, 2), **({} if ok else {"message": "; ".join(probs)[:300]})}

