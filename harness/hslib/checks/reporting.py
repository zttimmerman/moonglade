"""harness-reporting meta-checks. These run after all other checks, over the results document."""
import fnmatch
import json
import re
import subprocess
import time

from .. import paths as P
from ..registry import REGISTRY, TIERS, check, require, tiers_for

SCENARIO_RE = re.compile(r"^#### Scenario:\s*(.+)$", re.M)
CHECK_RE = re.compile(r"Check:\s*`([^`]+)`")
HUMAN_RE = re.compile(r"\[human-verify\]\s*(\S.{10,})")
POST_IDS = ("report.coverage", "report.schema", "report.full_run", "report.tiers")
QUICK_BUDGET_S = 180          # spec: the quick tier finishes in under 3 minutes on the target Mac
NOT_QUICK = ("build.clean", "client.launch_smoke", "lifecycle.persist")   # named by the spec
QUICK_DIR = P.OUT / "quick-tier"   # where report.tiers' nested quick run writes its results


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


def scenarios(include_planned=False):
    """Yield (file, scenario name, check ids, human reason)."""
    for f in spec_files(include_planned):
        text = f.read_text()
        heads = list(SCENARIO_RE.finditer(text))
        for i, m in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            nxt = re.search(r"^#{2,3} ", text[m.end():end], re.M)
            body = text[m.end(): m.end() + nxt.start()] if nxt else text[m.end():end]
            hm = HUMAN_RE.search(body)
            yield str(f.relative_to(P.ROOT)), m.group(1).strip(), CHECK_RE.findall(body), (hm.group(1).strip() if hm else None)


def coverage() -> tuple[bool, dict]:
    known = set(REGISTRY) | set(POST_IDS)
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
                        "problem": "unregistered check id" if unknown else "no Check: id and no [human-verify] reason"})
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
                     "active_changes": sorted(active), "planned_changes": planned,
                     "planned_checks_not_yet_registered": planned_unimpl}


def _validate(inst, schema, path="$"):
    errs = []
    t = schema.get("type")
    tmap = {"object": dict, "array": list, "string": str, "boolean": bool, "number": (int, float)}
    if t and not isinstance(inst, tmap[t]) or (t == "number" and isinstance(inst, bool)):
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


# ----------------------------------------------------------------------------- tiers

def tier_problems(listing: list[dict]) -> list[str]:
    """Problems with a `hs list --json` listing: each check needs exactly one declared tier from TIERS, and the
    spec-named expensive checks must not be quick."""
    probs = []
    seen = {}
    for c in listing:
        seen[c["id"]] = seen.get(c["id"], 0) + 1
        t = c.get("tier")
        if not isinstance(t, str) or t not in TIERS:
            probs.append(f"{c['id']}: tier {t!r} not one of {TIERS}")
        if not c.get("tier_declared"):
            probs.append(f"{c['id']}: no tier declared (relies on the default)")
    probs += [f"{i}: listed {n} times" for i, n in seen.items() if n != 1]
    for i in NOT_QUICK:
        if i not in seen:
            probs.append(f"{i}: not registered")
        elif any(c["id"] == i and c.get("tier") == "quick" for c in listing):
            probs.append(f"{i}: must not be quick")
    return probs


@check("report.tiers_declared", order=1, capability="harness-reporting", stack="any", tier="quick")
def tiers_declared(ctx):
    """`hs list` shows every registered check with exactly one declared tier from {quick, full, slow};
    build.clean, client.launch_smoke and lifecycle.persist are not quick."""
    r = subprocess.run([str(P.HARNESS / "hs"), "list", "--json"], capture_output=True, text=True, timeout=120)
    require(r.returncode == 0, "`hs list --json` failed", exit=r.returncode, stderr=r.stderr[-1500:])
    listing = json.loads(r.stdout)
    by_tier = {t: sorted(c["id"] for c in listing if c.get("tier") == t) for t in TIERS}
    probs = tier_problems(listing)
    ev = {"cmd": "harness/hs list --json", "checks_listed": len(listing), "by_tier": by_tier,
          "registered_in_process": len(REGISTRY), "problems": probs,
          "not_quick_required": {i: next((c["tier"] for c in listing if c["id"] == i), None) for i in NOT_QUICK}}
    require(len(listing) == len(REGISTRY), "`hs list` count differs from the registry", **ev)
    require(not probs, f"{len(probs)} tier problem(s)", **ev)
    return ev


def _tier_problems_for_run(doc: dict, wall_s: float, full_run_rec: dict | None) -> tuple[list, dict]:
    """Check a quick-tier results document against the quick-tier scenario's THEN."""
    expected = sorted(i for i, c in REGISTRY.items() if c.tier == "quick")
    executed = sorted(r["id"] for r in doc["checks"] if r["id"] not in POST_IDS)
    non_quick = sorted(i for i in executed if i not in REGISTRY or REGISTRY[i].tier != "quick")
    missing = sorted(set(expected) - set(executed))
    failing = sorted(r["id"] for r in doc["checks"] if r["id"] in executed and r["status"] != "pass")
    ev = {"tier_recorded": doc["meta"].get("tier"), "filter": doc["meta"].get("filter"),
          "full_run_flag": doc["meta"].get("full_run"), "wall_s": round(wall_s, 1), "budget_s": QUICK_BUDGET_S,
          "quick_checks_expected": len(expected), "executed": executed, "missing_quick": missing,
          "non_quick_executed": non_quick, "failing_quick_checks": failing,
          "report_full_run": None if full_run_rec is None else
          {"status": full_run_rec.get("status"), "message": full_run_rec.get("message")}}
    probs = []
    if doc["meta"].get("tier") != "quick":
        probs.append("results.json does not record tier: quick")
    if doc["meta"].get("filter"):
        probs.append("quick run was filtered")
    if missing:
        probs.append(f"quick checks not executed: {missing}")
    if non_quick:
        probs.append(f"full/slow checks executed: {non_quick}")
    if failing:
        probs.append(f"quick checks not passing: {failing}")
    if wall_s >= QUICK_BUDGET_S:
        probs.append(f"took {wall_s:.0f} s (budget < {QUICK_BUDGET_S} s)")
    if doc["meta"].get("full_run") is not False:
        probs.append("quick run marked as a full run")
    if full_run_rec is None or full_run_rec.get("status") != "skip" or "not a full run" not in (full_run_rec.get("message") or ""):
        probs.append("report.full_run did not report skip ('not a full run')")
    others = sorted(r["id"] for r in doc["checks"] if r["id"] != "report.full_run" and r["status"] != "pass")
    if others:
        probs.append(f"non-passing checks besides report.full_run: {others}")
    return probs, ev


def _tiers_record(probs, ev, t0):
    ok = not probs
    return {"id": "report.tiers", "capability": "harness-reporting", "status": "pass" if ok else "fail",
            "evidence": ev | {"problems": probs}, "duration_s": round(time.time() - t0, 2),
            **({} if ok else {"message": "; ".join(probs)[:300]})}


def tiers_nested(leave_up: bool) -> dict:
    """Run `harness/hs run --tier quick` as a subprocess (results under harness/out/quick-tier/) and check it."""
    t0 = time.time()
    cmd = [str(P.HARNESS / "hs"), "run", "--tier", "quick", "--results-dir", str(QUICK_DIR)] + (["--leave-up"] if leave_up else [])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    wall = time.time() - t0
    (QUICK_DIR / "stderr.log").write_text(r.stderr)
    ev = {"mode": "nested quick-tier run", "cmd": " ".join(cmd[1:]).replace(str(P.ROOT) + "/", ""),
          "exit": r.returncode, "results": str((QUICK_DIR / "results.json").relative_to(P.ROOT))}
    try:
        doc = json.loads((QUICK_DIR / "results.json").read_text())
    except (OSError, ValueError) as e:
        return _tiers_record([f"nested quick run wrote no readable results.json: {e}"], ev | {"stderr_tail": r.stderr[-1500:]}, t0)
    fr = next((x for x in doc["checks"] if x["id"] == "report.full_run"), None)
    probs, tev = _tier_problems_for_run(doc, wall, fr)
    ev["other_checks_all_pass"] = not any(x["status"] != "pass" for x in doc["checks"] if x["id"] != "report.full_run")
    if r.returncode != (0 if ev["other_checks_all_pass"] else 1):
        probs.append(f"nested quick run exit {r.returncode}: must be 0 iff every check but report.full_run passed")
    return _tiers_record(probs, ev | tev, t0)


def full_run_record(doc: dict, referenced: set, extra: list, reg: dict | None = None) -> dict:
    """report.full_run over a results document (+ the post-run records in `extra`): a full run passes only if every
    scenario-referenced check has a record and every record passes, except tolerated skips (runner.tolerated_skip:
    a full-tier run's "slow tier" skips of scenario-referenced slow checks)."""
    from ..runner import tolerated_skip
    m = doc["meta"]
    referenced = set(referenced) - set(POST_IDS)
    executed = {r["id"] for r in doc["checks"]}
    not_run = sorted(referenced - executed)
    slow_skipped = sorted(r["id"] for r in doc["checks"] if r["status"] == "skip" and tolerated_skip(r, m, reg))
    failing = sorted(r["id"] for r in doc["checks"] + extra if r["status"] != "pass" and r["id"] not in slow_skipped)
    full = m["full_run"]
    ok = full and not not_run and not failing
    # Not a full run (quick tier or --only): skip, which only for this record and only then doesn't fail the run
    # (runner.tolerated_skip). A full run must pass; it can never be skipped.
    status = "pass" if ok else ("fail" if full else "skip")
    why = [] if full else [f"not a full run (tier {m.get('tier')}{', filtered' if m.get('filter') else ''})"]
    return {"id": "report.full_run", "capability": "harness-reporting", "status": status,
            "evidence": {"full_run": full, "tier": m.get("tier"), "filter": m.get("filter"),
                         "spec_ids_not_executed": not_run, "slow_tier_skipped": slow_skipped,
                         "non_passing": failing, "checks_executed": len(executed) - len(slow_skipped),
                         "outputs": [str(P.RESULTS.relative_to(P.ROOT)), str(P.SUMMARY.relative_to(P.ROOT))]},
            "duration_s": 0.0,
            **({} if ok else {"message": (why[0] if not full else
                                          "not a clean full run: " + ", ".join(not_run + failing))[:300]})}


def post_run(doc, selected_ids, t_start=None, leave_up=False):
    out = []
    t0 = time.time()
    ok, ev = coverage()
    out.append({"id": "report.coverage", "capability": "harness-reporting",
                "status": "pass" if ok else "fail", "evidence": ev, "duration_s": round(time.time() - t0, 2),
                **({} if ok else {"message": f"{len(ev['incomplete'])} scenario(s) without a valid check mapping"})})

    # report.tiers: an unfiltered quick run checks itself (after report.full_run, whose verdict it reads); any
    # other run that selects it (every unfiltered full/slow run, or --only matching it) runs a nested quick tier.
    m = doc["meta"]
    self_mode = m.get("tier") == "quick" and not m.get("filter")
    nested = not self_mode and (not m.get("filter") or any(fnmatch.fnmatch("report.tiers", g) for g in m["filter"]))
    if nested:
        out.append(tiers_nested(leave_up))

    t0 = time.time()
    schema = json.loads((P.HARNESS / "results.schema.json").read_text())
    # validate the document as it will be written (including the coverage record)
    errs = _validate(json.loads(json.dumps({"meta": doc["meta"], "checks": doc["checks"] + out}, default=str)), schema)
    empty = [r["id"] for r in doc["checks"] + out if not r.get("evidence")]
    ok = not errs and not empty
    out.append({"id": "report.schema", "capability": "harness-reporting", "status": "pass" if ok else "fail",
                "evidence": {"schema": "harness/results.schema.json", "errors": errs[:50], "empty_evidence": empty,
                             "records_validated": len(doc["checks"]) + 1},
                "duration_s": round(time.time() - t0, 2), **({} if ok else {"message": "results.json fails schema"})})

    out.append(full_run_record(doc, set(ev["referenced_ids"]), out))
    if self_mode:
        t0 = time.time()
        wall = time.time() - (t_start or time.time())
        probs, tev = _tier_problems_for_run(doc, wall, out[-1])
        out.append(_tiers_record(probs, {"mode": "this run (unfiltered quick tier)"} | tev, t0))
    return out


# ----------------------------------------------------------------------------- slow-tier scenario rule

def _simulate(reg: dict, tier: str, referenced: set) -> dict:
    """One unfiltered run of a synthetic registry through the runner's own code paths (select, slow_tier_skips,
    execute, full_run_record, run_ok) with the stack handling stubbed out. Returns the outcome."""
    from .. import runner
    selected = runner.select(reg, tier, None)
    skips = runner.slow_tier_skips(reg, tier, None, referenced)
    results = runner.execute(selected, {}, skips, ensure=lambda *a: None, quiet=True)
    meta = {"full_run": runner.is_full_run(tier, None), "tier": tier, "filter": [],
            "slow_tier_skipped": [c.id for c in skips]}
    doc = {"meta": meta, "checks": results}
    fr = full_run_record(doc, referenced, [], reg)
    doc["checks"].append(fr)
    ok = runner.run_ok(doc, reg)
    return {"tier": tier, "records": {r["id"]: (r["status"], r.get("message", "")) for r in doc["checks"]},
            "report_full_run": fr["status"], "exit": 0 if ok else 1}


@check("report.slow_tier_rule", order=2, capability="harness-reporting", stack="any", tier="quick")
def slow_tier_rule(ctx):
    """A full-tier run records a scenario-referenced slow check as skip "slow tier", report.full_run passes and the
    exit code is 0; a slow-tier run executes it and passes only if it passes; any other skip fails a full run."""
    from .. import runner
    from ..registry import Check, CheckFail, CheckSkip

    calls = []

    def ok_fn(cid):
        def f(_ctx):
            calls.append(cid)
            return {"ran": cid}
        return f

    def fail_fn(_ctx):
        calls.append("t.slow")
        raise CheckFail("synthetic slow failure", {"ran": "t.slow"})

    def skip_fn(msg):
        def f(_ctx):
            raise CheckSkip(msg)
        return f

    def reg(**fns):
        base = {"t.quick": ("quick", ok_fn("t.quick")), "t.full": ("full", ok_fn("t.full")),
                "t.slow": ("slow", ok_fn("t.slow")), "t.slow_unreferenced": ("slow", ok_fn("t.slow_unreferenced"))}
        base.update(fns)
        return {i: Check(i, fn, n, "synthetic", "any", f"synthetic {t}", t, True)
                for n, (i, (t, fn)) in enumerate(sorted(base.items()))}

    referenced = {"t.quick", "t.full", "t.slow"}
    cases = {}
    calls.clear()
    cases["A_full_tier"] = _simulate(reg(), "full", referenced) | {"executed": list(calls)}
    calls.clear()
    cases["B_slow_tier_pass"] = _simulate(reg(), "slow", referenced) | {"executed": list(calls)}
    calls.clear()
    cases["C_slow_tier_slow_check_fails"] = _simulate(reg(**{"t.slow": ("slow", fail_fn)}), "slow", referenced) \
        | {"executed": list(calls)}
    cases["D_full_tier_full_check_skips"] = _simulate(reg(**{"t.full": ("full", skip_fn("no reason"))}), "full",
                                                      referenced)
    # a non-slow check can't get the slow-tier exemption by imitating its message
    cases["E_full_tier_forged_slow_message"] = _simulate(
        reg(**{"t.full": ("full", skip_fn(runner.SLOW_SKIP_MSG))}), "full", referenced)

    A, B, C, D, E = (cases[k] for k in sorted(cases))
    probs = []
    if A["records"].get("t.slow", ("",))[0] != "skip" or "slow tier" not in A["records"]["t.slow"][1]:
        probs.append(f"A: full run recorded t.slow as {A['records'].get('t.slow')}, want skip 'slow tier'")
    if "t.slow" in A["executed"]:
        probs.append("A: full run executed the slow check")
    if "t.slow_unreferenced" in A["records"]:
        probs.append("A: an unreferenced slow check was recorded in a full run")
    if A["report_full_run"] != "pass" or A["exit"] != 0:
        probs.append(f"A: full run report.full_run={A['report_full_run']} exit={A['exit']}, want pass / 0")
    if "t.slow" not in B["executed"] or B["records"].get("t.slow", ("",))[0] != "pass":
        probs.append(f"B: slow run did not execute+pass t.slow ({B['records'].get('t.slow')})")
    if B["report_full_run"] != "pass" or B["exit"] != 0:
        probs.append(f"B: slow run report.full_run={B['report_full_run']} exit={B['exit']}, want pass / 0")
    if C["records"].get("t.slow", ("",))[0] != "fail" or C["report_full_run"] != "fail" or C["exit"] != 1:
        probs.append(f"C: failing slow check in a slow run: t.slow={C['records'].get('t.slow')} "
                     f"report.full_run={C['report_full_run']} exit={C['exit']}, want fail / fail / 1")
    if D["records"].get("t.full", ("",))[0] != "skip" or D["report_full_run"] != "fail" or D["exit"] != 1:
        probs.append(f"D: skipped full check in a full run: report.full_run={D['report_full_run']} exit={D['exit']}, "
                     "want fail / 1")
    if E["report_full_run"] != "fail" or E["exit"] != 1:
        probs.append(f"E: forged slow-tier message on a full check: report.full_run={E['report_full_run']} "
                     f"exit={E['exit']}, want fail / 1")

    # the live registry: which scenario-referenced slow checks a real full run will record as skip
    live_ref = runner.referenced_ids()
    live = [c.id for c in runner.slow_tier_skips(REGISTRY, "full", None, live_ref)]
    want = sorted(i for i in live_ref if i in REGISTRY and REGISTRY[i].tier == "slow")
    if sorted(live) != want:
        probs.append(f"live registry: full-run slow skips {sorted(live)} != referenced slow checks {want}")
    if runner.slow_tier_skips(REGISTRY, "slow", None, live_ref):
        probs.append("live registry: a slow run would skip slow checks")
    ev = {"cases": cases, "live_full_run_slow_skips": live, "live_referenced_slow_checks": want, "problems": probs,
          "code": "runner.select/slow_tier_skips/execute/run_ok + reporting.full_run_record (the paths `hs run` uses)"}
    require(not probs, f"{len(probs)} slow-tier rule violation(s)", **ev)
    return ev
