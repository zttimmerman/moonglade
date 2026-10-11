"""`hs run`: execute checks, write results.json + summary.md.

test-pyramid (harness-reporting "Test layers and run tiers"): run tiers are
- unit: the unit layer only (unit tests + unit-layer checks); never touches the stack (`harness/hs test` is the same);
- pr: the unit layer first (a failure stops the run there), then impact selection (hslib/selection.py), then the
  selected integration, journey and gate checks in run order (segment());
- full: every check of every layer.
Run order (segment): unit -> cold integration (stack down) -> `hs up` (lifecycle.up) -> shared integration, default
profile -> profile groups -> warm journeys -> `hs down` (lifecycle.down) -> journey.first_play -> the release
bracket and the other gate checks -> run guards (order >= GUARD_ORDER) -> post-run reports.
"""
import fnmatch
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
import traceback
from datetime import datetime, timezone

from . import config, paths as P, stack
from .registry import CHILDREN, REGISTRY, RUN_TIERS, CheckFail, CheckSkip, load_all
from .util import keep_awake, log

# release-signing: a ci_only check outside the release workflow (GITHUB_ACTIONS != true) reports this skip, never a pass
CI_ONLY_SKIP_MSG = "not applicable: CI-only (judges a release-workflow run; local builds are never Developer ID signed)"
UNIT_FAILED_MSG = "not run: the unit layer failed (a pr run stops there)"
GUARD_ORDER = 990            # run guards (player protection, the release bracket check): after everything else
FIRST_PLAY = "journey.first_play"
BRACKET_PREFIXES = ("release.", "firstlaunch.")


def segment(c) -> float:
    """The check's place in the run order (see the module doc)."""
    if c.order >= GUARD_ORDER:
        return 9
    if c.layer == "unit":
        return 0
    if c.id == "lifecycle.up":
        return 1.5                       # the shared fixture's start: the end of the cold segment
    if c.id == "lifecycle.down":
        return 6
    if c.id.startswith(BRACKET_PREFIXES):
        return 8                         # release.player_data_untouched's bracket: contiguous, stack down
    if c.layer == "gate":
        return 8.5
    if c.layer == "journey":
        return 7 if c.id == FIRST_PLAY else 5
    if c.stack == "up":
        return 2 if (c.profile or config.DEFAULT_PROFILE) == config.DEFAULT_PROFILE else 3
    return 1


def run_key(c) -> tuple:
    return (segment(c), c.profile or "", c.order, c.id)


def layer_of(cid: str, reg: dict | None = None) -> str | None:
    reg = REGISTRY if reg is None else reg
    if cid in reg:
        return reg[cid].layer
    if cid in CHILDREN and CHILDREN[cid] in reg:
        return reg[CHILDREN[cid]].layer
    return "unit" if cid.startswith("report.") else None


def _module_file(root, pkg_parts: list[str], name: str | None):
    """The repo file for module `pkg_parts[.name]` (a module or a package's __init__), or None."""
    from pathlib import Path
    base = Path(root, "harness", *pkg_parts)
    cands = ([base / name, ] if name else [base])
    for c in cands:
        if c.with_suffix(".py").is_file():
            return c.with_suffix(".py")
        if (c / "__init__.py").is_file():
            return c / "__init__.py"
    return None


_IMPORT_TARGETS: dict = {}   # (path, mtime_ns, size) -> that file's hslib import targets: a pure function of its source


def _import_targets(f, pkg: list[str]) -> list[tuple]:
    """The (package parts, name) pairs a source imports from hslib, parsed once per file version in a process
    (report.gui_tier_rule evaluates the closure 36 times; the parse was ~13 s of the quick tier)."""
    import ast
    st = f.stat()
    key = (str(f), st.st_mtime_ns, st.st_size, tuple(pkg))
    hit = _IMPORT_TARGETS.get(key)
    if hit is not None:
        return hit
    targets = []
    for node in ast.walk(ast.parse(f.read_text(), str(f))):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if parts[0] == "hslib":
                    targets.append((parts[:-1], parts[-1]) if len(parts) > 1 else ([], "hslib"))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else list(pkg)
            elif (node.module or "").split(".")[0] == "hslib":
                base = []
            else:
                continue
            mod = (node.module or "").split(".") if node.module else []
            full = base + mod
            if full:
                targets.append((full[:-1], full[-1]))           # the module itself (or package __init__)
            for a in node.names:                               # `from pkg import module`
                targets.append((full, a.name))
    _IMPORT_TARGETS[key] = targets
    return targets


def import_closure(roots=(), root=None) -> list[str]:
    """Repo-relative paths of every hslib module reachable from `roots` by static imports (absolute `hslib.x` and
    relative `from . / .. import x`, including imports inside functions). Raises on a source that doesn't parse
    (callers fail closed)."""
    from pathlib import Path
    root = Path(root or P.ROOT)
    seen: set = set()
    todo = [root / r for r in roots if (root / r).suffix == ".py"]
    while todo:
        f = todo.pop()
        if f in seen or not f.is_file():
            continue
        seen.add(f)
        rel = f.relative_to(root / "harness").with_suffix("").parts      # e.g. ("hslib", "checks", "polish_checks")
        pkg = list(rel[:-1])                                              # the module's package
        for parts, name in _import_targets(f, pkg):
            m = _module_file(root, parts, name)
            if m is not None:
                todo.append(m)
    return sorted(str(x.relative_to(root)) for x in seen)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git_sha():
    r = subprocess.run(["git", "-C", P.ROOT, "rev-parse", "HEAD"], capture_output=True, text=True)
    dirty = subprocess.run(["git", "-C", P.ROOT, "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
    return (r.stdout.strip() or "unknown") + ("-dirty" if dirty else "")


def _ensure_stack(want: str, ctx: dict, profile: str | None = None):
    st = stack.stack_processes()
    is_up = all(st[k] for k in st)
    prof = profile or config.DEFAULT_PROFILE
    if want == "up" and not is_up:
        ctx.setdefault("stack_transitions", []).append({"up": stack.up(profile=prof)})
    elif want == "up" and not stack.profile_matches(prof):
        # up, but started with another config profile (or before profiles were recorded): restart the worldserver
        ctx.setdefault("stack_transitions", []).append({"profile": stack.ensure_profile(prof)})
    elif want == "down" and any(st.values()):
        ctx.setdefault("stack_transitions", []).append({"down": stack.down()})


def changed_paths(root=None) -> dict:
    """Repo-relative paths this change touches: `git diff --name-only --no-renames <merge-base>` (committed since the
    merge-base with origin/main, falling back to main, plus staged and unstaged edits) and untracked files.
    {"base_ref", "base", "paths", "error"}; paths is None when git can't answer (callers fail closed)."""
    root = str(root or P.ROOT)
    out = {"base_ref": None, "base": None, "paths": None, "error": None}

    def git(*a):
        return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, timeout=60)
    try:
        for ref in ("origin/main", "main"):
            r = git("merge-base", "HEAD", ref)
            if r.returncode == 0 and r.stdout.strip():
                out["base_ref"], out["base"] = ref, r.stdout.strip()
                break
        if not out["base"]:
            out["error"] = "no merge-base with origin/main or main"
            return out
        d = git("diff", "--name-only", "--no-renames", out["base"])
        u = git("ls-files", "--others", "--exclude-standard")
        if d.returncode != 0 or u.returncode != 0:
            out["error"] = f"git diff/ls-files failed: {(d.stderr + u.stderr).strip()[-300:]}"
            return out
        out["paths"] = sorted({l for l in (d.stdout + u.stdout).splitlines() if l.strip()})
    except (OSError, subprocess.SubprocessError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out




def is_full_run(tier: str, only: list[str] | None) -> bool:
    """A run is *full* only if it is unfiltered and its tier is full (harness-reporting)."""
    return not only and tier == "full"


def tolerated_skip(rec: dict, meta: dict, reg: dict | None = None) -> bool:
    """The only non-pass statuses that don't fail a run (any other skip fails it): report.full_run = skip on a run that
    isn't full (harness-reporting "Single-command full run"), and a ci_only check's "not applicable" skip outside CI."""
    if rec["id"] == "report.full_run" and rec["status"] == "skip" and meta.get("full_run") is False:
        return True
    return is_ci_only_skip(rec, meta, reg)


def is_ci_only_skip(rec: dict, meta: dict, reg: dict | None = None) -> bool:
    """release-signing: a ci_only check's "not applicable" skip on a run outside the release workflow (meta.ci is
    False). Only checks the registry marks ci_only, only with CI_ONLY_SKIP_MSG; in CI the check must pass."""
    reg = REGISTRY if reg is None else reg
    c = reg.get(rec["id"])
    return (rec["status"] == "skip" and c is not None and c.ci_only and meta.get("ci") is False
            and rec.get("message") == CI_ONLY_SKIP_MSG)


def run_ok(doc: dict, reg: dict | None = None) -> bool:
    """A run passes when every record passes or is a tolerated skip, and every layer budget held (report.layer_budgets
    is a record)."""
    return all(r["status"] == "pass" or tolerated_skip(r, doc["meta"], reg) for r in doc["checks"])


def referenced_ids() -> set[str]:
    """Check ids referenced by scenarios of archived specs and active changes (the ones that gate a run)."""
    from .checks import reporting
    return {i for _, _, ids, _ in reporting.scenarios() for i in ids}


def _child_records(c, rec: dict) -> list[dict]:
    """A clustered session's scenario results (evidence["children"]: {id: {status, message?, evidence, duration_s}})
    as their own records; a declared child without a result is an error record."""
    if not c.children:
        return []
    got = (rec.get("evidence") or {}).get("children") or {}
    out = []
    for cid in c.children:
        r = got.get(cid)
        if r is None:
            why = rec.get("message") or "the session didn't record this scenario"
            r = {"status": "error" if rec["status"] in ("pass", "fail", "error") else rec["status"],
                 "message": f"no result from {c.id}: {why}"[:300], "evidence": {"session": c.id}}
        out.append({"id": cid, "capability": c.capability, "description": f"(scenario of {c.id})", "layer": c.layer,
                    "parent": c.id, "status": r["status"], "duration_s": round(float(r.get("duration_s") or 0.0), 2),
                    "evidence": r.get("evidence") or {"session": c.id},
                    **({"message": r["message"]} if r.get("message") else {})})
    return out


def run_one(c, ctx: dict, ensure) -> dict:
    """One check: ensure its stack state, run it, and return its record (children not included)."""
    rec = {"id": c.id, "capability": c.capability, "description": c.doc, "layer": c.layer}
    t0 = time.time()
    try:
        ensure(c.stack, ctx, c.profile)
        ev = c.fn(ctx) or {}
        rec |= {"status": "pass", "evidence": ev}
        if c.children:
            bad = [k for k, v in ((ev.get("children") or {}).items()) if v.get("status") != "pass"]
            missing = [k for k in c.children if k not in (ev.get("children") or {})]
            if bad or missing:
                rec |= {"status": "fail", "message": f"scenario(s) failing: {bad}" + (f"; missing: {missing}" if missing else "")}
    except CheckFail as e:
        rec |= {"status": "fail", "message": str(e), "evidence": e.evidence}
    except CheckSkip as e:
        rec |= {"status": "skip", "message": str(e), "evidence": {"skip_reason": str(e), **e.evidence}}
    except Exception as e:  # noqa: BLE001
        excerpt = getattr(e, "log_excerpt", "")
        rec |= {"status": "error", "message": f"{type(e).__name__}: {e}",
                "evidence": {"traceback": traceback.format_exc()[-3000:], **({"log_excerpt": excerpt} if excerpt else {})}}
    rec["duration_s"] = round(time.time() - t0, 2)
    rec["_t"] = (t0, time.time())
    return rec


def execute(checks: list, ctx: dict, ensure=None, quiet: bool = False, guard=None, skipped: dict | None = None
            ) -> list[dict]:
    """Run `checks` in run order (run_key). `ensure(stack_want, ctx, profile)` brings the stack to each check's required
    state (default _ensure_stack). `quiet` suppresses the per-check log lines (synthetic runs of the report.* rules).
    `guard` (power.Guard; power-guard) is checked before each executed check, outside its timed duration; a power stop
    ends the loop with ctx["power_stop"] set and the checks so far returned. `skipped` {id: message}: checks recorded
    as skip without running. Character-scoped shared-server checks run two at a time (fixture.run_pooled) when
    ctx["pool"] is set; the shared-server fixture resets and checks shared state between them (ctx["fixture"])."""
    ensure = ensure or _ensure_stack
    say = (lambda _m: None) if quiet else log
    skipped = skipped or {}
    results = []
    plan = ctx.get("release_bracket_plan")      # run-scoped (_run plans it from the whole run's selection)
    br_start, br_end = (plan.get("start_check"), plan.get("end_check")) if plan else (None, None)
    xlog = ctx.setdefault("exec_log", [])        # the run's actual lane-A execution sequence, across every execute
    order = sorted(checks, key=run_key)
    i = 0
    while i < len(order):
        c = order[i]
        if c.id in skipped:
            rec = {"id": c.id, "capability": c.capability, "description": c.doc, "layer": c.layer, "status": "skip",
                   "message": skipped[c.id], "duration_s": 0.0, "evidence": {"skip_reason": skipped[c.id]}}
            say(f"SKIP  {c.id}  {skipped[c.id]}")
            results += [rec] + _child_records(c, rec)
            i += 1
            continue
        if guard is not None:
            from .power import PowerStop
            try:
                guard.check(f"check {c.id}")
            except PowerStop as e:
                ctx["power_stop"] = {"message": str(e), "before_check": c.id, "pause": e.record}
                break
        if c.id == br_start and "start" not in (ctx.get("release_bracket") or {}):
            _bracket_fingerprint(ctx, "start", c.id, ensure)
        fx = ctx.get("fixture")
        batch = [c]
        if fx is not None and ctx.get("pool") and c.scope == "character" and c.layer == "integration":
            j = i + 1
            while j < len(order) and order[j].scope == "character" and order[j].layer == "integration" \
                    and order[j].id not in skipped and segment(order[j]) == segment(c) \
                    and (order[j].profile or "") == (c.profile or ""):
                batch.append(order[j])
                j += 1
        if fx is not None and c.layer == "integration" and c.stack == "up":
            recs = fx.run(batch, ctx, ensure, say)
        else:
            recs = [run_one(c, ctx, ensure)]
        for rec in recs:
            t0, t1 = rec.pop("_t", (time.time(), time.time()))
            if ctx.get("lane_timeline") is not None:          # second-stack: two-lane runs record when each check ran
                ctx["lane_timeline"][rec["id"]] = {"start": t0, "end": t1, "lane": P.LANE}
            xlog.append({"id": rec["id"], "start": t0, "end": t1, "lane": P.LANE, "layer": rec.get("layer")})
            say(f"{rec['status'].upper():5} {rec['id']} ({rec['duration_s']}s){'  ' + rec.get('message', '') if rec['status'] != 'pass' else ''}")
            cc = REGISTRY.get(rec["id"]) or next((b for b in batch if b.id == rec["id"]), None)
            results.append(rec)
            if cc is not None:
                results += _child_records(cc, rec)
            br = ctx.get("release_bracket")
            if br is not None and "start" in br and "end" not in br:
                br["ran"].append(rec["id"])
                if rec["id"] == br_end:
                    _bracket_fingerprint(ctx, "end", rec["id"], ensure)
                else:
                    _bracket_save(ctx)
        i += len(batch)
    return results


PDU_ID = "release.player_data_untouched"


BRACKET_DIR = P.OUT / "release-bracket"         # one state file per run id (release.player_data_untouched)
BRACKET_KEEP_S = 7 * 86400


def _player_saves_fingerprint():
    from . import backups, saves
    acct = saves.player_account()
    return backups.fingerprint(P.PLAYER_SAVES / acct) if acct else None


def release_bracket_plan(checks, run_id: str | None, state_dir: Path | None = None) -> dict | None:
    """release.player_data_untouched brackets every release.*/firstlaunch.* check of the WHOLE run (not of one
    execute call: a two-lane run calls execute once per segment, and the check sits in a later segment than the
    checks it brackets). `checks` is the run's whole selection. The start fingerprint is taken right before the first
    of them in run order (run_key), the end right after the last; with none of them, the start is taken right before
    the check itself and there is no end. The state (fingerprints and what ran between them) is kept in ctx and
    persisted, keyed by the run id, under the run's output dir. None when the check isn't in this run."""
    ids = [c.id for c in sorted(checks, key=run_key)]
    if PDU_ID not in ids:
        return None
    inner = [i for i in ids if i.startswith(BRACKET_PREFIXES) and i != PDU_ID]
    rid = run_id or f"pid{os.getpid()}-{int(time.time())}"
    return {"run_id": rid, "inner": inner, "start_check": inner[0] if inner else PDU_ID,
            "end_check": inner[-1] if inner else None, "state_file": str(Path(state_dir or BRACKET_DIR) / f"{rid}.json")}


def bracket_intruders(checks) -> list[str]:
    """Checks that aren't release.*/firstlaunch.* (or are release.player_data_untouched) but sort, by (order, id),
    between the first and the last release.*/firstlaunch.* check of `checks` (pass the whole registry): in a run that
    selects them with the bracketed checks they would run inside the bracket and fail it (release-signing verifier
    cycle 1: signing.* at order 13). Keep the bracket strict: register such checks outside its order range."""
    ids = [c.id for c in sorted(checks, key=lambda c: (c.order, c.id))]
    inner = [n for n, i in enumerate(ids) if i.startswith(BRACKET_PREFIXES) and i != PDU_ID]
    if not inner:
        return []
    return [i for i in ids[inner[0]:inner[-1] + 1] if not i.startswith(BRACKET_PREFIXES) or i == PDU_ID]


def _prune_bracket_states(d: Path, keep: str) -> None:
    try:
        for f in d.glob("*.json"):
            if f.name != keep and time.time() - f.stat().st_mtime > BRACKET_KEEP_S:
                f.unlink()
    except OSError:
        pass


def _bracket_save(ctx: dict) -> None:
    """Persist ctx["release_bracket"] to the run's state file (atomic). An IO error is recorded in ctx; the check
    then fails (it reads the file)."""
    plan, br = ctx.get("release_bracket_plan"), ctx.get("release_bracket")
    if not plan or br is None:
        return
    f = Path(plan["state_file"])
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"run_id": plan["run_id"], "plan": plan,
                                   **br}, default=str))
        tmp.replace(f)
    except OSError as e:
        br["save_error"] = f"{type(e).__name__}: {e}"


def _bracket_fingerprint(ctx: dict, which: str, cid: str, ensure) -> None:
    """The bracket's start (lane A brought down first, outside any check's timing: its world/auth servers change the
    databases by themselves) or end fingerprint into ctx["release_bracket"], marked in the run's execution log and
    persisted. Errors are recorded; the check fails."""
    from .checks.release_runtime_checks import player_data_fingerprint
    br = ctx.setdefault("release_bracket", {"ran": []})
    br[f"{which}_check"] = cid
    try:
        if which == "start":
            ensure("down", ctx, None)
        br[which] = player_data_fingerprint()
    except Exception as e:  # noqa: BLE001  (the check fails on an erroneous record)
        br[which] = {"error": f"{type(e).__name__}: {e}"}
    xlog = ctx.setdefault("exec_log", [])
    br[f"{which}_log_index"] = len(xlog)          # the fingerprint sits between exec_log[i-1] and exec_log[i]
    xlog.append({"bracket": which, "at": time.time(), "check": cid})
    if which == "start":
        _prune_bracket_states(Path(ctx["release_bracket_plan"]["state_file"]).parent,
                              Path(ctx["release_bracket_plan"]["state_file"]).name)
    _bracket_save(ctx)




# harness-reporting "Full-run timing is recorded and guarded" (seamless-experience, Zach 2026-10-07: "catch real
# slowdowns only"): a per-check baseline, build time and new checks reported apart.
TIMING_BASELINE = P.HARNESS / "timing-baseline.json"
BUILD_CHECKS = ("build.clean",)      # their whole time is building server binaries
# harness-reporting "Only allowlisted checks may claim build time" (easy-setup, F33): `evidence.build_s` is
# self-reported, so only these checks' claims are subtracted; any other check's claim counts as check time and is a
# warning. Reviewed list; each entry says what the claimed time is.
BUILD_CLAIM_ALLOWLIST = {
    "core.login_logout_race_baseline": "its rebuild of the unpatched baseline server (baseline.build_install wall "
                                       "time, core_checks.py)",
}
BUILD_TIME_ALLOWLIST = BUILD_CHECKS + tuple(BUILD_CLAIM_ALLOWLIST)   # the checks that may have build time at all
NEW_CHECK_WARN_S = 180.0             # spec: a check not in the baseline taking longer is a warning in summary.md
TIMING_SELF = "report.timing"        # measured after everything else; never in the baseline


def _claimed_build_s(rec: dict) -> float:
    b = (rec.get("evidence") or {}).get("build_s")
    return float(b) if isinstance(b, (int, float)) and not isinstance(b, bool) and b > 0 else 0.0


def check_build_s(rec: dict) -> float:
    """Seconds of a check record spent building server binaries: all of a BUILD_CHECKS check, else, for a check on
    BUILD_CLAIM_ALLOWLIST, the evidence's `build_s` (capped at the check's own time). Any other check: 0."""
    d = float(rec.get("duration_s") or 0.0)
    if rec["id"] in BUILD_CHECKS:
        return d
    if rec["id"] not in BUILD_CLAIM_ALLOWLIST:
        return 0.0
    b = _claimed_build_s(rec)
    return round(min(d, b), 2) if b else 0.0


def build_claim_warnings(records: list[dict]) -> list[str]:
    """A warning for each executed check that claims build time (evidence build_s) without being on the allowlist."""
    return [f"`{r['id']}` claims {_claimed_build_s(r):.0f} s of build time (evidence build_s) but isn't on the "
            "build-time allowlist (runner.BUILD_CLAIM_ALLOWLIST); counted as check time"
            for r in sorted(records, key=lambda r: r["id"])
            if r.get("status") != "skip" and r["id"] not in BUILD_TIME_ALLOWLIST and _claimed_build_s(r)]


def load_timing_baseline(path: Path | None = None) -> dict:
    """The per-check baseline ({"checks": {id: {"s", "run", ...}}, "runs": {...}, "host": ...}); raises on a missing or
    malformed file."""
    b = json.loads(Path(path or TIMING_BASELINE).read_text())
    if not isinstance(b.get("checks"), dict) or not b["checks"]:
        raise ValueError("baseline has no per-check times")
    for key in ("checks", "checks_lanes2"):          # checks_lanes2: the two-lane baseline (second-stack), optional
        if key == "checks_lanes2" and key not in b:
            continue
        if not isinstance(b[key], dict):
            raise ValueError(f"baseline {key} isn't a mapping")
        for i, e in b[key].items():
            if not isinstance(e.get("s"), (int, float)) or e.get("run") not in (b.get("runs") or {}):
                raise ValueError(f"baseline entry {key}.{i} lacks a time or a known source run")
    return b


def timing_summary(records: list[dict], wall_s: float | None, reg: dict | None = None,
                   baseline: dict | None | str = "file", paused_s: float = 0.0) -> dict:
    """harness-reporting "Full-run timing": per-check wall time, their sum, the run's wall time, the 10 slowest, the time spent building server binaries (per check and total), and the executed checks that
    aren't in the per-check baseline with their times, plus a warning for each one over NEW_CHECK_WARN_S."""
    if baseline == "file":
        try:
            baseline = load_timing_baseline()
        except (OSError, ValueError):
            baseline = None
    per = {r["id"]: float(r.get("duration_s") or 0.0) for r in records}
    top = sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    build = {r["id"]: check_build_s(r) for r in records if r.get("status") != "skip" and check_build_s(r) > 0}
    out = {"per_check_s": per, "checks_sum_s": round(sum(per.values()), 1),
           "total_wall_s": None if wall_s is None else round(wall_s, 1),
           "top10": [{"id": i, "s": v} for i, v in top],
           "build_s": round(sum(build.values()), 1), "build_by_check_s": build,
           # power-guard: seconds paused for low battery (between checks, so outside every check's duration_s, which
           # is what the guard compares); the run's wall time minus them is its active time
           "paused_s": round(paused_s, 1),
           "active_wall_s": None if wall_s is None else round(wall_s - paused_s, 1)}
    claims = build_claim_warnings(records)
    if baseline is None:
        out |= {"new_checks_s": None, "warnings": ["no readable per-check timing baseline"] + claims}
        return out
    new = {r["id"]: float(r.get("duration_s") or 0.0) for r in records
           if r.get("status") != "skip" and r["id"] not in baseline["checks"] and r["id"] != TIMING_SELF}
    out |= {"new_checks_s": new, "new_checks_sum_s": round(sum(new.values()), 1),
            "warnings": [f"new check `{i}` took {s:.0f} s (over {NEW_CHECK_WARN_S:.0f} s)"
                         for i, s in sorted(new.items()) if s > NEW_CHECK_WARN_S] + claims}
    return out




# ---------------------------------------------------------------- selection of what a run executes

def select(reg: dict, tier: str, only: list[str] | None, chosen: set | None = None) -> list:
    """Checks a run executes, in run order: tier unit -> the unit layer; pr -> the unit layer plus `chosen` (impact
    selection) plus the run guards; full -> everything. `--only` globs filter any tier (a filtered pr run runs exactly
    the filter's checks, without impact selection)."""
    if tier not in RUN_TIERS:
        raise ValueError(f"unknown tier {tier!r}; expected one of {RUN_TIERS}")

    def wanted(c):
        if only:
            return any(fnmatch.fnmatch(c.id, g) for g in only) and (tier != "unit" or c.layer == "unit")
        if tier == "unit":
            return c.layer == "unit"
        if tier == "full":
            return True
        return c.layer == "unit" or segment(c) == 9 or (chosen is not None and c.id in chosen)
    return sorted((c for c in reg.values() if wanted(c)), key=run_key)


def gui_preflight_decision(tier: str, only: list[str] | None, ignore: bool, probe, gui_due_fn=None) -> dict:
    """`hs run`'s start-up preflight (harness-reporting "The GUI preflight only guards runs that need the screen").
    Only unfiltered pr/full runs are guarded; unit runs never. A locked screen or a sleeping display refuses only when
    the run's selection includes a GUI-marked check (`gui_due_fn()`: always for a full run with any GUI check; for a
    pr run, the impact selection); low battery on battery power always refuses. `ignore` (--ignore-gui-preflight)
    overrides both. Returns {"guarded", "state", "gui_due", "gui_checks_due", "refuse"} (meta.gui_preflight)."""
    out = {"tier": tier, "guarded": not (only or tier not in ("pr", "full")), "ignored": bool(ignore),
           "state": None, "gui_due": None, "gui_checks_due": None, "refuse": None}
    if not out["guarded"]:
        return out
    from .guienv import preflight_problem
    state = probe()
    out["state"] = state
    try:
        due = (gui_due_fn or (lambda: gui_checks_due(tier)))()
    except Exception as e:  # noqa: BLE001  fail closed: unknown counts as due
        due = None
        out["gui_due_error"] = f"{type(e).__name__}: {e}"
    out["gui_checks_due"] = due
    out["gui_due"] = due is None or bool(due)
    why = preflight_problem(state, gui_due=out["gui_due"])
    if why and not ignore:
        out["refuse"] = (f"[hs] refusing to start an unfiltered {tier} run: {why}; "
                         + ("its GUI checks would fail" if out["gui_due"] and "battery" not in why
                            else "a long run would drain the battery")
                         + ". Fix that and rerun, or pass --ignore-gui-preflight to run anyway.")
    return out


def gui_checks_due(tier: str) -> list[str] | None:
    """The GUI-marked checks an unfiltered run of `tier` would execute (None: can't tell)."""
    reg = load_all()
    gui = sorted(c.id for c in reg.values() if c.gui)
    if tier == "full":
        return gui
    from . import selection
    s = selection.select_for_run(reg)
    if s["full"]:
        return [i for i in gui if reg[i].layer != "gate" or i in s["selected"]]
    return [i for i in gui if i in s["selected"]]


def preflight_from_env(tier: str, only: list[str] | None, ignore: bool) -> dict:
    """gui_preflight_decision with the real probe and selection, or report.gui_preflight's stubs from the environment
    ($HS_TEST_GUI_PROBE: the screen state; $HS_TEST_GUI_DUE: "yes" | "no" | "unknown", the selection's GUI checks)."""
    from . import guienv
    probe, due_fn = guienv.probe, None
    if os.environ.get("HS_TEST_GUI_PROBE"):
        stub = json.loads(os.environ["HS_TEST_GUI_PROBE"])
        probe = lambda: stub
    if os.environ.get("HS_TEST_GUI_DUE"):
        v = os.environ["HS_TEST_GUI_DUE"]
        val = {"yes": ["journey.app_desktop"], "no": [], "unknown": None}[v]
        due_fn = lambda: val
    return gui_preflight_decision(tier, only, ignore, probe, due_fn)


def gui_preflight(tier: str, only: list[str] | None, ignore: bool, probe, gui_due_fn=None) -> str | None:
    """The one-line refusal of gui_preflight_decision, or None to go ahead."""
    return gui_preflight_decision(tier, only, ignore, probe, gui_due_fn)["refuse"]


def run(only: list[str] | None = None, leave_up: bool = False, tier: str = "pr",
        force_clean: bool = False, preflight: dict | None = None, ignore_power: bool = False,
        lanes: int | None = None) -> int:
    # test-loop-speedups: a run file (env/run/harness-runs/) tags this run's subprocesses, so a later run can heal
    # them if this one is killed; removed on every exit except a kill
    from . import runreg
    rec = runreg.start()
    try:
        try:
            orphans = runreg.heal()
        except Exception as e:  # noqa: BLE001  (a healing bug mustn't stop every run; recorded, and the checks'
            import traceback as _tb   # GIVENs still refuse whatever is left running)
            orphans = {"dead_runs": [], "orphans": [], "actions": [], "remaining": [],
                       "error": f"{type(e).__name__}: {e}", "traceback": _tb.format_exc()[-2000:]}
            log(f"orphan healing failed: {orphans['error']}")
        if orphans["dead_runs"]:
            log(f"healed {len(orphans['orphans'])} orphan process(es) of {len(orphans['dead_runs'])} killed harness "
                f"run(s); remaining: {len(orphans['remaining'])}")
        # F12: a mysqld stall ("Semaphore wait > 600 s") happened while the display slept; hold a no-sleep assertion
        # (test-loop-speedups "Runs keep the display awake": caffeinate -d ... -w <this pid>)
        with keep_awake() as awake:
            return _run(only, leave_up, tier, force_clean, awake, preflight, orphans | {"run": rec}, ignore_power,
                        lanes)
    finally:
        runreg.finish(rec)


def filter_refusal(reg: dict, tier: str, only: list[str] | None, selected: list) -> str | None:
    """A filtered run that would check nothing is refused, never reported as a pass (release-signing verifier cycle
    1: `--only 'a,b'` matched nothing and exited 0). Refused: a glob matching no registered check and no post-run
    report (a comma list is one glob: pass --only once per glob), or a filter under which this tier executes no
    check and no post-run report. None to go ahead."""
    if not only:
        return None
    from .checks.reporting import POST_IDS
    known = list(reg) + list(POST_IDS)
    dead = [g for g in only if not any(fnmatch.fnmatch(i, g) for i in known)]
    if dead:
        hint = " (a comma list is one glob; pass --only once per glob)" if any("," in g for g in dead) else ""
        return f"refusing the run: --only {', '.join(repr(g) for g in dead)} matches no check{hint}"
    if not selected and not any(fnmatch.fnmatch(i, g) for i in POST_IDS for g in only):
        other = sorted({reg[i].layer or "?" for i in reg if any(fnmatch.fnmatch(i, g) for g in only)})
        return (f"refusing the run: the filter {only} selects no check to execute in tier {tier} "
                f"(its checks are layer {', '.join(other)}; tier unit runs only the unit layer)")
    return None


# power-guard: the run tiers whose checks are guarded (the unit tier is short and runs on builders' laps)
POWER_GUARDED_TIERS = ("pr", "full")
UNIT_POOL = 4


def ensure_server_current(ctx: dict) -> dict:
    """Before the acceptance segments: when the installed server wasn't built from the current build inputs (module
    sources, patches, pins), an incremental `hs build` brings it up to date (the accepted fixed cost of a C++ change).
    Its wall time is the run's build layer."""
    from . import build
    t0 = time.time()
    try:
        want, have = build.input_hash(), build.read_stamp(build.install_stamp_path())
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "build_s": 0.0}
    if want == have:
        return {"current": True, "build_s": 0.0}
    if any(stack.stack_processes().values()):
        stack.down()
    out = {"current": False, "input_hash": want[:12], "installed": (have or "")[:12]}
    try:
        out["build"] = {k: v for k, v in build.build().items() if k in ("mode", "jobs", "seconds")}
    except Exception as e:  # noqa: BLE001  the integration tests then run against the old binaries and say so
        out["error"] = f"{type(e).__name__}: {e}"
    out["build_s"] = round(time.time() - t0, 1)
    return out


def _union_s(spans: list[tuple]) -> float:
    total, end = 0.0, None
    for a, b in sorted(spans):
        if end is None or a > end:
            total += b - a
            end = b
        elif b > end:
            total += b - end
            end = b
    return round(total, 1)


def layer_times(ctx: dict, records: list[dict], unit_s: float | None, selection_s: float | None,
                build_s: float) -> dict:
    """meta.layers: wall time per layer (integration = the union of its checks' wall spans, so pooled tests count
    once), each journey's time against its budget, gate time and build time."""
    xl = [e for e in ctx.get("exec_log", []) if "id" in e]
    spans = lambda layer: [(e["start"], e["end"]) for e in xl if e.get("layer") == layer]
    journeys = {r["id"]: {"s": r.get("duration_s"), "budget_s": (REGISTRY[r["id"]].budget_s if r["id"] in REGISTRY else None)}
                for r in records if r.get("layer") == "journey" and r["id"] in REGISTRY and r.get("status") != "skip"}
    integ = _union_s(spans("integration"))
    jsum = round(sum(v["s"] or 0.0 for v in journeys.values()), 1)
    return {"unit": {"wall_s": unit_s, "budget_s": 120.0},
            "selection": {"wall_s": selection_s, "budget_s": 10.0},
            "integration": {"wall_s": integ, "budget_s": 900.0},
            "journeys": {"wall_s": jsum, "each": journeys},
            "acceptance": {"wall_s": round(integ + jsum, 1), "budget_s": 1800.0},
            "gate": {"wall_s": _union_s(spans("gate"))},
            "build": {"wall_s": build_s}}


def _run(only, leave_up, tier, force_clean, awake, preflight=None, orphans=None, ignore_power=False,
         lanes_req=None) -> int:
    t_start = time.time()
    from . import power
    guard = (power.Guard(f"the harness run (tier {tier})", "the same `harness/hs run` command", ignore_power, say=log,
                         again="it runs the checks again from the start.")
             if tier in POWER_GUARDED_TIERS else None)
    import os as _os
    from . import lanes
    battery_seq = _os.environ.get(power.BATTERY_ENV)           # second-stack: the lane preflight's own battery reader
    lane_battery = power.Battery(battery_seq)
    lane_switches = lanes.take_switches()                     # read once; never inherited by the checks
    mem_start = lanes.Probe(lane_switches.get(lanes.MEMORY_ENV)).preflight()   # swap growth is measured from here
    for _v in power.TEST_ENV:            # read by the guard above; never inherited by the checks' own subprocesses
        _os.environ.pop(_v, None)
    reg = load_all()
    full = is_full_run(tier, only)
    sel_meta = None
    if tier == "pr" and not only:
        from . import selection
        sel_meta = selection.select_for_run(reg)
        chosen = set(reg) if sel_meta["full"] else set(sel_meta["selected"])
        if sel_meta["full"]:                    # FULL: the complete acceptance suite, and only the gate checks selected
            chosen = {i for i, c in reg.items() if c.layer != "gate" or i in sel_meta["selected"]}
        selected = select(reg, tier, only, chosen)
    else:
        selected = select(reg, tier, only)
    refuse = filter_refusal(reg, tier, only, selected)
    if refuse:
        log(refuse)
        return 2
    P.OUT.mkdir(parents=True, exist_ok=True)
    du = shutil.disk_usage(P.ROOT)
    meta = {
        "started": _now(), "repo_sha": _git_sha(), "fork_lock": P.fork_lock(), "full_run": full,
        "tier": tier, "force_clean": force_clean, "selection": sel_meta,
        "keep_awake": awake, "gui_preflight": preflight, "orphans": orphans,
        "ci": _os.environ.get("GITHUB_ACTIONS") == "true",
        "filter": only or [], "host": {"platform": platform.platform(), "machine": platform.machine(),
                                      "python": platform.python_version(), "disk_free_gb": round(du.free / 2**30, 1)},
    }
    # launcher-fixes: every check's subprocesses know they're a harness run (./play then never writes the player's own
    # play client's resolution), and client.player_config_untouched compares the player's config with this
    os.environ["HS_HARNESS_RUN"] = "1"
    from .checks.resolution_checks import player_config_sha, player_marker_sha
    from . import backups
    ctx = {"force_clean": force_clean, "tier": tier, "player_config_sha256_start": player_config_sha(),
           "keep_awake": awake, "gui_preflight": preflight, "orphans": orphans,
           "player_marker_sha256_start": player_marker_sha(),
           # save-backups: the player's env/backups/ must be byte-identical at the end (backups.player_untouched)
           "player_backups_fingerprint_start": backups.fingerprint(backups.PLAYER_ROOT),
           # front-menu-saves: the player's env/saves/<PLAYER>/ must be identical at the end (saves.player_untouched)
           "player_saves_fingerprint_start": _player_saves_fingerprint()}
    # release-first-launch: release.player_data_untouched's bracket, planned over the whole run (every lane-A segment)
    ctx["run_id"] = ((orphans or {}).get("run") or {}).get("id")
    ctx["release_bracket_plan"] = release_bracket_plan(selected, ctx["run_id"])
    # easy-setup (F28): the player's play client build must be untouched at the end (client.player_client_not_rebuilt)
    from . import clientbuild
    try:
        ctx["player_play_client_fingerprint_start"] = clientbuild.fingerprint(clientbuild.PLAY)
        ctx["player_play_client_current_start"] = clientbuild.up_to_date(clientbuild.PLAY)
    except Exception as e:  # noqa: BLE001  (the check fails on a missing/erroneous start record)
        ctx["player_play_client_fingerprint_start"] = {"error": f"{type(e).__name__}: {e}"}
    # easy-setup (pristine-client protection): the pristine client (or the test stand-in) at the start;
    # report.client_pristine compares at the end
    from .checks.pristine_checks import start_record
    pristine_start = start_record()
    if sel_meta is not None:
        log(f"selection: {'FULL (' + '; '.join(sel_meta['full_reasons'][:3]) + ')' if sel_meta['full'] else str(len(sel_meta['selected'])) + ' acceptance test(s)'}"
            f" in {sel_meta['duration_s']} s from {len(sel_meta.get('paths') or [])} changed path(s)")

    # 1. the unit layer (unit tests + unit-layer checks; never the stack)
    from . import unitrun
    unit_checks = [c for c in selected if c.layer == "unit" and segment(c) < 9]
    results: list[dict] = []
    t_unit = time.time()
    run_tests = not only or tier == "unit"
    unit_doc = unitrun.unit_layer(unit_checks, ctx, run_tests=run_tests, workers=UNIT_POOL, say=log, guard=guard)
    results += unit_doc.pop("records")
    meta["unit"] = unit_doc
    unit_s = round(time.time() - t_unit, 1)
    unit_ok = unit_doc["ok"]
    rest = [c for c in selected if c not in unit_checks]
    skipped = {}
    if ctx.get("power_stop"):
        skipped = {}
        rest = []
    elif tier == "pr" and not unit_ok:
        log("the unit layer failed: a pr run stops there")
        skipped = {c.id: UNIT_FAILED_MSG for c in rest if segment(c) < 9}
    # 2. the server build (accepted fixed cost) before any stack is used
    build_info = {"build_s": 0.0, "skipped": "no acceptance check needs the server"}
    if not skipped and any(c.stack == "up" or c.layer in ("integration", "journey") for c in rest) and tier != "unit":
        build_info = ensure_server_current(ctx)
    meta["server_build"] = build_info
    # 3. the acceptance segments (one or two lanes) and the run guards
    if tier != "unit" and rest:
        from . import fixture
        ctx["fixture"] = fixture.SharedServer(rest)
        ctx["pool"] = True
        try:
            recs, meta["lanes"] = lanes.schedule(rest, ctx, guard, requested=lanes_req, tier=tier, only=only,
                                                 switches=lane_switches, battery=lane_battery,
                                                 battery_seq=battery_seq, ignore_power=ignore_power,
                                                 mem_start=mem_start, say=log, skipped=skipped)
        finally:
            meta["fixture"] = ctx["fixture"].finish(ctx)
        results += recs
    else:
        recs = execute([c for c in rest], ctx, ensure=lambda *_a: None, guard=guard) if rest else []
        results += recs
        meta["lanes"] = {"mode": 1, "lanes": 1, "fallback": "unit tier: one lane", "preflight": None,
                         "assignment": {"b_planned": [], "b_ran": [], "b_to_a": []}}
    meta["lanes"]["memory_at_run_start"] = mem_start
    if meta["lanes"].get("mode") == 2 and lanes.lane_b_running():   # the worker stops lane B; this is the safety net
        from . import runreg
        meta["lanes"]["final_lane_down"] = runreg.lane_down("b")
    meta["power"] = {"guarded": guard is not None, **(guard.record() if guard is not None else {})}
    paused = guard.paused_s() if guard is not None else 0.0
    meta["layers"] = layer_times(ctx, results, unit_s, (sel_meta or {}).get("duration_s"), build_info.get("build_s", 0.0))
    meta["timeline"] = [{k: e.get(k) for k in ("id", "start", "end", "lane", "layer")}
                        for e in ctx.get("exec_log", []) if "id" in e]
    if ctx.get("power_stop"):
        # power-guard "Stop cleanly": no later check runs; the results so far are written and the run fails
        meta["power"]["stop"] = ctx["power_stop"]
        log(ctx["power_stop"]["message"])
        if not leave_up and any(stack.stack_processes().values()):
            ctx.setdefault("stack_transitions", []).append({"down(final)": stack.down()})
        doc = {"meta": meta | {"finished": _now()}, "checks": results}
        doc["meta"]["wall_s"] = round(time.time() - t_start, 1)
        doc["meta"]["timing"] = timing_summary(results, doc["meta"]["wall_s"], paused_s=paused)
        doc["meta"]["counts"] = {s: sum(1 for r in results if r["status"] == s) for s in ("pass", "fail", "error", "skip")}
        doc["meta"]["ok"] = False
        from .util import redact
        write_outputs(redact(doc), P.RESULTS, P.SUMMARY)
        print(P.SUMMARY.read_text())
        return power.STOP_EXIT

    if not leave_up and any(stack.stack_processes().values()):
        ctx.setdefault("stack_transitions", []).append({"down(final)": stack.down()})

    doc = {"meta": meta | {"finished": _now()}, "checks": results}
    doc["meta"]["timing"] = timing_summary(results, time.time() - t_start, paused_s=paused)   # report.timing reads it
    # post-run meta-checks (report.*) — computed over the document itself
    from .checks import reporting
    for rec in reporting.post_run(doc, selected_ids=[c.id for c in selected], t_start=t_start,
                                  leave_up=leave_up, pristine_start=pristine_start):
        rec.setdefault("layer", "unit")
        doc["checks"].append(rec)
    for rec in doc["checks"]:
        rec.setdefault("layer", layer_of(rec["id"]) or "unit")
    doc["meta"]["finished"] = _now()
    doc["meta"]["wall_s"] = round(time.time() - t_start, 1)
    doc["meta"]["timing"] = timing_summary(doc["checks"], doc["meta"]["wall_s"], paused_s=paused)
    doc["meta"]["counts"] = {s: sum(1 for r in doc["checks"] if r["status"] == s) for s in ("pass", "fail", "error", "skip")}
    doc["meta"]["ok"] = run_ok(doc)
    from .util import redact
    doc = redact(doc)                     # no passwords in results.json / summary.md (check output tails etc.)
    write_outputs(doc, P.RESULTS, P.SUMMARY)
    if tier == "pr" and not only:
        from . import selection
        selection.record_pr_run(doc)
    print(P.SUMMARY.read_text())
    return 0 if doc["meta"]["ok"] else 1



# test-loop-speedups "The last full run's results are kept": every unfiltered full-tier run also writes here; no other
# run does. `hs timing-baseline join` reads it by default.
LAST_FULL_RUN = P.OUT / "last-full-run"


def keeps_last_full_run(meta: dict) -> bool:
    """An unfiltered full-tier run (not unit, pr or filtered)."""
    return meta.get("full_run") is True and meta.get("tier") == "full" and not meta.get("filter")


def write_outputs(doc: dict, results: Path, summary: Path, last_dir: Path | None = None) -> dict:
    """Write results.json + summary.md, and a copy of both to `last_dir` (default LAST_FULL_RUN) when the run
    keeps_last_full_run. Returns {"results", "summary", "last_full_run": dir or None}."""
    text, md = json.dumps(doc, indent=2, default=str), summary_md(doc)
    results.write_text(text)
    summary.write_text(md)
    log(f"wrote {results} and {summary}")
    last = None
    if keeps_last_full_run(doc["meta"]):
        last = Path(last_dir or LAST_FULL_RUN)
        last.mkdir(parents=True, exist_ok=True)
        for name, body in (("results.json", text), ("summary.md", md)):
            tmp = last / f".{name}.tmp"
            tmp.write_text(body)
            tmp.replace(last / name)
        log(f"kept a copy in {last}")
        if last_dir is None:                  # second-stack: the last full run's wall time per lane mode (report.lanes)
            from .lanes import record_full_run_wall
            try:
                record_full_run_wall(doc["meta"])
            except OSError as e:
                log(f"couldn't record the full run's wall time: {e}")
    return {"results": str(results), "summary": str(summary), "last_full_run": None if last is None else str(last)}


def timing_lines(doc) -> list[str]:
    """summary.md: the timing guard's numbers (report.timing's evidence) and every timing warning."""
    t = doc["meta"].get("timing") or {}
    rt = next((r for r in doc["checks"] if r["id"] == TIMING_SELF), None)
    ev = (rt or {}).get("evidence") or {}
    out = [f"- Timing: build {t.get('build_s', '?')} s, "
           f"new checks {len(t.get('new_checks_s') or {})} ({t.get('new_checks_sum_s', '?')} s)"
           + (f"; baseline checks {ev['now_sum_s']} s vs {ev['baseline_sum_s']} s "
              f"(limit {ev['limit_s']} s, ratio {ev['ratio']})" if "now_sum_s" in ev else "")]
    if ev.get("speed_target"):
        out.append(f"- Timing guard ({ev.get('lane_mode', 1)} lane(s)): {ev['speed_target']}")
    out += [f"- WARNING (timing): {w}" for w in t.get("warnings") or []]
    return out


def power_lines(doc) -> list[str]:
    """summary.md: the power guard's pauses and stop (power-guard), if any."""
    pw = doc["meta"].get("power") or {}
    out = [f"- Power: paused {len(pw.get('pauses') or [])} time(s), {pw.get('paused_s', 0)} s"
           + (" (guard skipped: --ignore-power)" if pw.get("ignored") else "")] if pw.get("guarded") else []
    if pw.get("stop"):
        out.append(f"- STOPPED (power): {pw['stop']['message']}")
    return out


def lane_lines(doc) -> list[str]:
    """summary.md: how many lanes ran and why (second-stack)."""
    from .lanes import summary_line
    line = summary_line(doc["meta"])
    return [line] if line else []


def summary_md(doc) -> str:
    m = doc["meta"]
    c = m["counts"]
    lines = [
        f"# Harness summary — {'PASS' if m['ok'] else 'FAIL'}",
        "",
        f"- Run: {m['started']} → {m['finished']}  (tier {m.get('tier', 'full')}, "
        f"{'full run' if m['full_run'] else 'not a full run' + (': filtered ' + ' '.join(m['filter']) if m['filter'] else '')}"
        f"{', --force-clean' if m.get('force_clean') else ''}; wall {m.get('wall_s', '?')} s)",
        f"- Repo: `{m['repo_sha']}`  core `{m['fork_lock'].get('core_sha', '')[:12]}`  playerbots `{m['fork_lock'].get('playerbots_sha', '')[:12]}`",
        f"- Counts: {c['pass']} pass, {c['fail']} fail, {c['error']} error, {c['skip']} skip. Disk free: {m['host']['disk_free_gb']} GB",
        "- Slowest: " + ", ".join(f"`{t['id']}` {t['s']}s" for t in (m.get("timing") or {}).get("top10", [])[:5]),
        *timing_lines(doc),
        *power_lines(doc),
        *lane_lines(doc),
        *layer_lines(doc),
        "",
        "| Check | Status | Time | Note |",
        "|---|---|---|---|",
    ]
    for r in doc["checks"]:
        note = (r.get("message") or "").replace("|", "\\|").replace("\n", " ")[:140]
        lines.append(f"| `{r['id']}` | {r['status'].upper()} | {r.get('duration_s', 0)}s | {note} |")
    return "\n".join(lines) + "\n"

def layer_lines(doc) -> list[str]:
    """summary.md: each layer's time against its budget (harness-reporting "Every layer is timed and budgeted")."""
    L = doc["meta"].get("layers") or {}
    if not L:
        return []
    def f(k):
        v = L.get(k) or {}
        b = v.get("budget_s")
        return f"{k} {v.get('wall_s')} s" + (f" (budget {b:.0f} s)" if b else "")
    out = ["- Layers: " + ", ".join(f(k) for k in ("unit", "selection", "integration", "journeys", "acceptance", "gate", "build") if k in L)]
    for j, v in sorted(((L.get("journeys") or {}).get("each") or {}).items()):
        out.append(f"  - {j}: {v.get('s')} s (budget {v.get('budget_s')} s)")
    s = doc["meta"].get("selection")
    if s:
        out.append(f"- Selection: {'FULL: ' + '; '.join(s.get('full_reasons') or [])[:200] if s.get('full') else str(len(s.get('selected') or {})) + ' acceptance test(s)'}"
                   f" ({s.get('duration_s')} s; smoke set: {bool(s.get('smoke_paths'))})")
    return out

