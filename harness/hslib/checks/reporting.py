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
    if full_run_rec is None or full_run_rec.get("status") != "fail" or "not a full run" not in (full_run_rec.get("message") or ""):
        probs.append("report.full_run did not report fail ('not a full run')")
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
    if r.returncode == 0:
        probs.append("nested quick run exited 0 although report.full_run must fail in it")
    return _tiers_record(probs, ev | tev, t0)


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

    referenced = set(ev["referenced_ids"]) - set(POST_IDS)
    executed = {r["id"] for r in doc["checks"]}
    not_run = sorted(referenced - executed)
    failing = sorted(r["id"] for r in doc["checks"] + out if r["status"] != "pass")
    full = doc["meta"]["full_run"]
    ok = full and not not_run and not failing
    why = [] if full else [f"not a full run (tier {m.get('tier')}{', filtered' if m.get('filter') else ''})"]
    out.append({"id": "report.full_run", "capability": "harness-reporting", "status": "pass" if ok else "fail",
                "evidence": {"full_run": full, "tier": m.get("tier"), "filter": m.get("filter"),
                             "spec_ids_not_executed": not_run,
                             "non_passing": failing, "checks_executed": len(executed),
                             "outputs": [str(P.RESULTS.relative_to(P.ROOT)), str(P.SUMMARY.relative_to(P.ROOT))]},
                "duration_s": 0.0,
                **({} if ok else {"message": ("not a clean full run: " + ", ".join(why + not_run + failing))[:300]})})
    if self_mode:
        t0 = time.time()
        wall = time.time() - (t_start or time.time())
        probs, tev = _tier_problems_for_run(doc, wall, out[-1])
        out.append(_tiers_record(probs, {"mode": "this run (unfiltered quick tier)"} | tev, t0))
    return out
