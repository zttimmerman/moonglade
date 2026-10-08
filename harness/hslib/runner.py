"""`hs run`: execute checks, write results.json + summary.md."""
import fnmatch
import json
from pathlib import Path
import platform
import shutil
import subprocess
import time
import traceback
from datetime import datetime, timezone

from . import config, paths as P, stack
from .registry import REGISTRY, CheckFail, CheckSkip, load_all, tiers_for
from .util import keep_awake, log

# harness-reporting "Slow-tier scenario checks": what a full-tier run records for a scenario-referenced slow check
SLOW_SKIP_MSG = "slow tier, not run in a full run"
# harness-reporting "GUI tier": what a full run records for a gui-marked check when no GUI path changed
GUI_SKIP_MSG = "gui tier, not run in a full run (no GUI path changed)"
# The GUI path rule: a change touching any GUI input runs the gui checks in full runs. launcher-fixes (F26, verifier
# 2026-10-05): dependency-based. The GUI inputs are the non-Python files below plus the transitive in-repo import
# closure of GUI_ROOTS (the gui checks and their shared app session, and the code that session runs: the launcher
# build, the ./play loop, the shim build), computed from the sources at evaluation time, so a new helper they
# import is covered without editing this list.
GUI_ROOTS = (
    "harness/hslib/checks/polish_checks.py",  # the gui checks and their shared app session
    "harness/hslib/checks/app_checks.py",     # open_app / Hook / build_app used by that session
    "harness/hslib/play.py",                  # the play loop the app runs (stages, ready -> hand-off, shutdown)
    "harness/hslib/app.py",                   # builds dist/Moonglade.app
    "harness/hslib/dockshim.py",              # builds/loads the Dock shim
    "harness/hslib/__main__.py",              # `harness/hs app` (build_app) and the CLI
    "harness/hslib/runner.py",                # this rule and the GUI tier itself
)
GUI_FILES = (
    "harness/app/*",                          # launcher.m, dockshim.m
    "play",                                   # the ./play entry point
    "harness/hs",                             # the harness entry point (`hs app`, `hs run`)
    "harness/hslib/checks/lssampler.m",       # the session's LaunchServices sampler (app.single_dock_icon)
)
GUI_PATHS = GUI_FILES + GUI_ROOTS             # the fixed part (the closure is added by gui_patterns)

# harness-reporting "Backups tier" (save-backups, Zach 2026-10-06: "run them only when needed"): what a full run records
# for a backups-marked check when no backup path changed. The backup path list is FIXED (no import closure: the
# backups checks drive ./play, so a closure would contain most of the harness and the tier would always run). It is
# the spec's list plus the backup checks' own file and the bits only they and the Backups window use.
BACKUPS_SKIP_MSG = "backups tier, not run in a full run (no backup path changed)"
BACKUP_PATHS = (
    "harness/hslib/backups.py",               # backups: take / list / verify / rotate / restore / CLI
    "harness/hslib/play.py",                  # the backup on quit, the gate, --backups dispatch, the notice
    "harness/hslib/stack.py",                 # stack.down's before_database hook
    "harness/hslib/stages.py",                # the shutdown_backup stage
    "harness/app/launcher.m",                 # the Backups window, the notice line
    "play",                                   # the ./play entry point (./play --backups)
    "harness/hslib/checks/backup_checks.py",  # the backups-tier checks themselves
    "harness/hslib/db.py",                    # the mysql client wrapper the dumps and loads go through
)


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


def import_closure(roots=GUI_ROOTS, root=None) -> list[str]:
    """Repo-relative paths of every hslib module reachable from `roots` by static imports (absolute `hslib.x` and
    relative `from . / .. import x`, including imports inside functions). Raises on a source that doesn't parse
    (callers fail closed)."""
    import ast
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
        for node in ast.walk(ast.parse(f.read_text(), str(f))):
            targets = []
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
            for parts, name in targets:
                m = _module_file(root, parts, name)
                if m is not None:
                    todo.append(m)
    return sorted(str(x.relative_to(root)) for x in seen)


def gui_patterns(root=None) -> tuple:
    """Every GUI input pattern: the fixed files plus the import closure of GUI_ROOTS (evaluated now)."""
    return tuple(dict.fromkeys(GUI_PATHS + tuple(import_closure(root=root))))


def backups_paths_matching(paths: list[str] | None) -> list[str] | None:
    """The changed paths on the backup path list (exact paths, no closure); None (unknown) stays None."""
    if paths is None:
        return None
    return sorted(p for p in paths if p in BACKUP_PATHS)


def backups_rule(root=None) -> dict:
    """The backup path rule for this checkout: {"changed_backup_paths": [...] or None (git failed: counts as
    changed), "base_ref", "base", "error", "paths"}."""
    c = changed_paths(root)
    return {"base_ref": c["base_ref"], "base": c["base"], "error": c["error"],
            "changed_backup_paths": backups_paths_matching(c["paths"]), "paths": list(BACKUP_PATHS)}


def backups_changed(rule: dict | None) -> bool:
    """A backup path changed, or it can't be told (fail closed)."""
    return rule is None or rule.get("changed_backup_paths") is None or bool(rule["changed_backup_paths"])


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


def gui_paths_matching(paths: list[str] | None, patterns=None) -> list[str] | None:
    """The changed paths that fall under the GUI path rule (default patterns: gui_patterns(), i.e. with the import
    closure); None (unknown) stays None."""
    if paths is None:
        return None
    patterns = gui_patterns() if patterns is None else patterns
    return sorted(p for p in paths if any(fnmatch.fnmatch(p, g) for g in patterns))


def gui_rule(root=None) -> dict:
    """The GUI path rule for this checkout: {"changed_gui_paths": [...] or None (git failed, or the import closure
    couldn't be computed: counts as changed), "base_ref", "base", "error", "changed_paths_count", "patterns"}."""
    c = changed_paths(root)
    try:
        pats = gui_patterns()
    except (OSError, SyntaxError, ValueError) as e:      # fail closed
        return {"base_ref": c["base_ref"], "base": c["base"], "error": f"GUI import closure: {type(e).__name__}: {e}",
                "changed_paths_count": None if c["paths"] is None else len(c["paths"]), "changed_gui_paths": None,
                "patterns": list(GUI_PATHS)}
    return {"base_ref": c["base_ref"], "base": c["base"], "error": c["error"],
            "changed_paths_count": None if c["paths"] is None else len(c["paths"]),
            "changed_gui_paths": gui_paths_matching(c["paths"], pats), "patterns": list(pats)}


def gui_changed(rule: dict) -> bool:
    """A GUI path changed, or it can't be told (fail closed)."""
    return rule.get("changed_gui_paths") is None or bool(rule["changed_gui_paths"])


def is_full_run(tier: str, only: list[str] | None) -> bool:
    """A run is *full* only if it is unfiltered and its tier is full or slow (harness-reporting)."""
    return not only and tier in ("full", "slow")


def tolerated_skip(rec: dict, meta: dict, reg: dict | None = None) -> bool:
    """The only non-pass statuses that don't fail a run (any other skip fails it):
    - report.full_run = skip on a run that isn't full (harness-reporting "Single-command full run");
    - a scenario-referenced slow check that a full-tier run recorded as skip "slow tier" (harness-reporting
      "Slow-tier scenario checks"): the runner listed it in meta.slow_tier_skipped and the registry says slow."""
    if rec["id"] == "report.full_run" and rec["status"] == "skip" and meta.get("full_run") is False:
        return True
    reg = REGISTRY if reg is None else reg
    c = reg.get(rec["id"])
    if (rec["status"] == "skip" and meta.get("tier") == "full" and c is not None and c.tier == "slow"
            and rec["id"] in meta.get("slow_tier_skipped", []) and rec.get("message") == SLOW_SKIP_MSG):
        return True
    return is_gui_skip(rec, meta, reg) or is_backups_skip(rec, meta, reg)


def is_backups_skip(rec: dict, meta: dict, reg: dict | None = None) -> bool:
    """A by-design backups-tier skip (harness-reporting "Backups tier"): a backups-marked check that a full-tier run
    recorded as skip with BACKUPS_SKIP_MSG, listed by the runner in meta.backups_tier_skipped, on a run whose backup
    path rule found no changed backup path. A check raising CheckSkip with the same message isn't listed there."""
    reg = REGISTRY if reg is None else reg
    c = reg.get(rec["id"])
    rule = meta.get("backups_rule") or {}
    return (rec["status"] == "skip" and meta.get("tier") == "full" and c is not None and c.backups
            and rec["id"] in meta.get("backups_tier_skipped", []) and rec.get("message") == BACKUPS_SKIP_MSG
            and rule.get("changed_backup_paths") == [])


def is_gui_skip(rec: dict, meta: dict, reg: dict | None = None) -> bool:
    """A by-design GUI skip (harness-reporting "GUI tier"): a gui-marked check that a full/slow-tier run recorded as
    skip with GUI_SKIP_MSG, listed by the runner in meta.gui_tier_skipped, on a run whose GUI rule found no changed
    GUI path. A check raising CheckSkip with the same message is not listed there, so it can't forge one."""
    reg = REGISTRY if reg is None else reg
    c = reg.get(rec["id"])
    rule = meta.get("gui_rule") or {}
    return (rec["status"] == "skip" and meta.get("tier") in ("full", "slow") and c is not None and c.gui
            and rec["id"] in meta.get("gui_tier_skipped", []) and rec.get("message") == GUI_SKIP_MSG
            and rule.get("changed_gui_paths") == [])


def run_ok(doc: dict, reg: dict | None = None) -> bool:
    """A run passes when every record passes or is a tolerated skip."""
    return all(r["status"] == "pass" or tolerated_skip(r, doc["meta"], reg) for r in doc["checks"])


def referenced_ids() -> set[str]:
    """Check ids referenced by scenarios of archived specs and active changes (the ones that gate a run)."""
    from .checks import reporting
    return {i for _, _, ids, _ in reporting.scenarios() for i in ids}


def slow_tier_skips(reg: dict, tier: str, only: list[str] | None, referenced: set[str]) -> list:
    """Slow checks a full-tier run records as skip: tier slow, referenced by a gating scenario, and matching the
    filter. Quick and slow runs record none (quick runs don't cover scenarios; slow runs execute them)."""
    if tier != "full":
        return []
    return sorted((c for c in reg.values()
                   if c.tier == "slow" and c.id in referenced
                   and (not only or any(fnmatch.fnmatch(c.id, g) for g in only))),
                  key=lambda c: (c.order, c.id))


def gui_tier_skips(reg: dict, tier: str, only: list[str] | None, changed: bool) -> list:
    """GUI checks a full/slow-tier run records as skip: gui-marked, in the run's tiers, matching the filter, and only
    when no GUI path changed (`changed` False). `--tier gui` and quick runs record none."""
    if tier not in ("full", "slow") or changed:
        return []
    tiers = tiers_for(tier)
    return sorted((c for c in reg.values()
                   if c.gui and c.tier in tiers and (not only or any(fnmatch.fnmatch(c.id, g) for g in only))),
                  key=lambda c: (c.order, c.id))


def backups_tier_skips(reg: dict, tier: str, only: list[str] | None, changed: bool) -> list:
    """Backups-marked checks a full-tier run records as skip: in the run's tiers, matching the filter, and only when
    no backup path changed. Slow runs (the periodic sweep), `--tier backups` and quick runs record none."""
    if tier != "full" or changed:
        return []
    tiers = tiers_for(tier)
    return sorted((c for c in reg.values()
                   if c.backups and c.tier in tiers and (not only or any(fnmatch.fnmatch(c.id, g) for g in only))),
                  key=lambda c: (c.order, c.id))


def execute(checks: list, ctx: dict, slow_skips: list = (), ensure=None, quiet: bool = False,
            gui_skips: list = (), backups_skips: list = (), guard=None) -> list[dict]:
    """Run `checks` in (order, id) sequence, recording `slow_skips` as skip ("slow tier") and `gui_skips` as skip
    ("gui tier") in their place. `ensure(stack_want, ctx, profile)` brings the stack to each check's required state
    (default _ensure_stack). `quiet` suppresses the per-check log lines (synthetic runs of the report.* rules).
    `guard` (power.Guard; power-guard) is checked before each executed check, outside its timed duration; a power stop
    ends the loop with ctx["power_stop"] set and the checks so far returned."""
    ensure = ensure or _ensure_stack
    say = (lambda _m: None) if quiet else log
    skip_ids = {c.id for c in slow_skips}
    gui_ids = {c.id for c in gui_skips}
    bk_ids = {c.id for c in backups_skips}
    results = []
    for c in sorted([*checks, *slow_skips, *gui_skips, *backups_skips], key=lambda c: (c.order, c.id)):
        rec = {"id": c.id, "capability": c.capability, "description": c.doc}
        if c.id in skip_ids:
            rec |= {"status": "skip", "message": SLOW_SKIP_MSG, "duration_s": 0.0,
                    "evidence": {"skip_reason": SLOW_SKIP_MSG, "tier": c.tier, "slow_tier": True}}
            say(f"SKIP  {c.id}  {SLOW_SKIP_MSG}")
            results.append(rec)
            continue
        if c.id in gui_ids:
            rec |= {"status": "skip", "message": GUI_SKIP_MSG, "duration_s": 0.0,
                    "evidence": {"skip_reason": GUI_SKIP_MSG, "tier": c.tier, "gui_tier": True}}
            say(f"SKIP  {c.id}  {GUI_SKIP_MSG}")
            results.append(rec)
            continue
        if c.id in bk_ids:
            rec |= {"status": "skip", "message": BACKUPS_SKIP_MSG, "duration_s": 0.0,
                    "evidence": {"skip_reason": BACKUPS_SKIP_MSG, "tier": c.tier, "backups_tier": True}}
            say(f"SKIP  {c.id}  {BACKUPS_SKIP_MSG}")
            results.append(rec)
            continue
        if guard is not None:
            from .power import PowerStop
            try:
                guard.check(f"check {c.id}")
            except PowerStop as e:
                ctx["power_stop"] = {"message": str(e), "before_check": c.id, "pause": e.record}
                break
        t0 = time.time()
        try:
            ensure(c.stack, ctx, c.profile)
            ev = c.fn(ctx) or {}
            rec |= {"status": "pass", "evidence": ev}
        except CheckFail as e:
            rec |= {"status": "fail", "message": str(e), "evidence": e.evidence}
        except CheckSkip as e:
            rec |= {"status": "skip", "message": str(e), "evidence": {"skip_reason": str(e)}}
        except Exception as e:  # noqa: BLE001
            excerpt = getattr(e, "log_excerpt", "")
            rec |= {"status": "error", "message": f"{type(e).__name__}: {e}",
                    "evidence": {"traceback": traceback.format_exc()[-3000:], **({"log_excerpt": excerpt} if excerpt else {})}}
        rec["duration_s"] = round(time.time() - t0, 2)
        say(f"{rec['status'].upper():5} {c.id} ({rec['duration_s']}s){'  ' + rec.get('message', '') if rec['status'] != 'pass' else ''}")
        results.append(rec)
    return results


def backups_tier_s(records: list[dict], reg: dict | None = None) -> float:
    """Seconds spent in executed backups-marked checks (harness-reporting "Backups tier": reported separately and
    left out of the timing guard's wall-time comparison)."""
    reg = REGISTRY if reg is None else reg
    return round(sum(float(r.get("duration_s") or 0.0) for r in records
                     if r.get("status") != "skip" and r["id"] in reg and reg[r["id"]].backups), 1)


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
    for i, e in b["checks"].items():
        if not isinstance(e.get("s"), (int, float)) or e.get("run") not in (b.get("runs") or {}):
            raise ValueError(f"baseline entry {i} lacks a time or a known source run")
    return b


def timing_summary(records: list[dict], wall_s: float | None, reg: dict | None = None,
                   baseline: dict | None | str = "file", paused_s: float = 0.0) -> dict:
    """harness-reporting "Full-run timing": per-check wall time, their sum, the run's wall time, the 10 slowest, the
    backups tier's total, the time spent building server binaries (per check and total), and the executed checks that
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
           "backups_tier_s": backups_tier_s(records, reg),
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


def select(reg: dict, tier: str, only: list[str] | None, gui_skip_ids: set | frozenset = frozenset()) -> list:
    """Checks a run executes: those of the tier's check tiers (gui: only gui-marked ones; backups: only backups-marked
    ones) matching the filter, minus the ones recorded as GUI / backups-tier skips (`gui_skip_ids` holds both)."""
    tiers = tiers_for(tier)
    return sorted(
        (c for c in reg.values()
         if c.tier in tiers and (tier != "gui" or c.gui) and (tier != "backups" or c.backups)
         and c.id not in gui_skip_ids
         and (not only or any(fnmatch.fnmatch(c.id, g) for g in only))),
        key=lambda c: (c.order, c.id),
    )


def gui_preflight_decision(tier: str, only: list[str] | None, ignore: bool, probe, rule_fn=None) -> dict:
    """`hs run`'s start-up preflight (B32; test-loop-speedups "The GUI preflight only guards runs that need the
    screen"). Only unfiltered full/slow/gui runs are guarded. A locked screen or a sleeping display refuses only when
    GUI-marked checks will execute: always for `--tier gui`, and for full/slow when the GUI path rule (`rule_fn`,
    default gui_rule) says a GUI path changed or can't tell (fail closed). Low battery on battery power always
    refuses. `ignore` (--ignore-gui-preflight) overrides both.
    Returns {"guarded", "state" (the probe), "gui_due", "gui_rule_changed", "refuse" (one line or None)}; the run
    records it in meta.gui_preflight."""
    out = {"tier": tier, "guarded": not (only or tier not in ("full", "slow", "gui")), "ignored": bool(ignore),
           "state": None, "gui_due": None, "gui_rule_changed": None, "refuse": None}
    if not out["guarded"]:
        return out
    from .guienv import preflight_problem
    state = probe()
    out["state"] = state
    if tier == "gui":
        out["gui_due"] = True
    else:
        rule = (rule_fn or gui_rule)()
        out["gui_rule_changed"] = rule.get("changed_gui_paths")
        out["gui_due"] = gui_changed(rule)
    why = preflight_problem(state, gui_due=out["gui_due"])
    if why and not ignore:
        out["refuse"] = (f"[hs] refusing to start an unfiltered {tier} run: {why}; "
                         + ("its GUI checks would fail" if out["gui_due"] and "battery" not in why
                            else "a long run would drain the battery")
                         + ". Fix that and rerun, or pass --ignore-gui-preflight to run anyway.")
    return out


def preflight_from_env(tier: str, only: list[str] | None, ignore: bool) -> dict:
    """gui_preflight_decision with the real probe and rule, or report.gui_preflight's stubs from the environment."""
    import os
    from . import guienv
    probe, rule_fn = guienv.probe, None
    if os.environ.get("HS_TEST_GUI_PROBE"):
        stub = json.loads(os.environ["HS_TEST_GUI_PROBE"])
        probe = lambda: stub
    if os.environ.get("HS_TEST_GUI_RULE"):
        v = os.environ["HS_TEST_GUI_RULE"]
        paths = {"changed": ["harness/app/launcher.m"], "unchanged": [], "unknown": None}[v]
        rule_fn = lambda: {"changed_gui_paths": paths, "error": None, "stub": v}
    return gui_preflight_decision(tier, only, ignore, probe, rule_fn)


def gui_preflight(tier: str, only: list[str] | None, ignore: bool, probe, rule_fn=None) -> str | None:
    """The one-line refusal of gui_preflight_decision, or None to go ahead."""
    return gui_preflight_decision(tier, only, ignore, probe, rule_fn)["refuse"]


def run(only: list[str] | None = None, leave_up: bool = False, tier: str = "full",
        force_clean: bool = False, preflight: dict | None = None, ignore_power: bool = False) -> int:
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
            return _run(only, leave_up, tier, force_clean, awake, preflight, orphans | {"run": rec}, ignore_power)
    finally:
        runreg.finish(rec)


# power-guard: the run tiers whose checks are guarded (quick is short and runs on builders' laps)
POWER_GUARDED_TIERS = ("full", "slow", "gui")


def _run(only, leave_up, tier, force_clean, awake, preflight=None, orphans=None, ignore_power=False) -> int:
    t_start = time.time()
    from . import power
    guard = (power.Guard(f"the harness run (tier {tier})", "the same `harness/hs run` command", ignore_power, say=log)
             if tier in POWER_GUARDED_TIERS else None)
    import os as _os
    for _v in power.TEST_ENV:            # read by the guard above; never inherited by the checks' own subprocesses
        _os.environ.pop(_v, None)
    reg = load_all()
    rule = gui_rule() if tier in ("full", "slow") else None
    gui_skips = gui_tier_skips(reg, tier, only, gui_changed(rule)) if rule is not None else []
    bk_rule = backups_rule() if tier == "full" else None
    bk_skips = backups_tier_skips(reg, tier, only, backups_changed(bk_rule)) if bk_rule is not None else []
    selected = select(reg, tier, only, {c.id for c in gui_skips} | {c.id for c in bk_skips})
    slow_skips = slow_tier_skips(reg, tier, only, referenced_ids())
    full = is_full_run(tier, only)
    P.OUT.mkdir(parents=True, exist_ok=True)
    du = shutil.disk_usage(P.ROOT)
    meta = {
        "started": _now(), "repo_sha": _git_sha(), "fork_lock": P.fork_lock(), "full_run": full,
        "tier": tier, "force_clean": force_clean, "slow_tier_skipped": [c.id for c in slow_skips],
        "gui_tier_skipped": [c.id for c in gui_skips], "gui_rule": rule,
        "backups_tier_skipped": [c.id for c in bk_skips], "backups_rule": bk_rule,
        "keep_awake": awake, "gui_preflight": preflight, "orphans": orphans,
        "filter": only or [], "host": {"platform": platform.platform(), "machine": platform.machine(),
                                      "python": platform.python_version(), "disk_free_gb": round(du.free / 2**30, 1)},
    }
    # launcher-fixes: every check's subprocesses know they're a harness run (./play then never writes the player's own
    # play client's resolution), and client.player_config_untouched compares the player's config with this
    import os
    os.environ["HS_HARNESS_RUN"] = "1"
    from .checks.resolution_checks import player_config_sha, player_marker_sha
    from . import backups
    ctx = {"force_clean": force_clean, "tier": tier, "player_config_sha256_start": player_config_sha(),
           "keep_awake": awake, "gui_preflight": preflight, "orphans": orphans,
           "player_marker_sha256_start": player_marker_sha(),
           # save-backups: the player's env/backups/ must be byte-identical at the end (backups.player_untouched)
           "player_backups_fingerprint_start": backups.fingerprint(backups.PLAYER_ROOT)}
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
    if rule is not None:
        log(f"GUI rule: {'GUI checks run' if gui_changed(rule) else 'GUI checks skipped by design'} "
            f"(changed GUI paths: {rule['changed_gui_paths']}{'; ' + rule['error'] if rule['error'] else ''})")
    if bk_rule is not None:
        log(f"Backups rule: {'backups checks run' if backups_changed(bk_rule) else 'backups checks skipped by design'} "
            f"(changed backup paths: {bk_rule['changed_backup_paths']}{'; ' + bk_rule['error'] if bk_rule['error'] else ''})")
    results = execute(selected, ctx, slow_skips, gui_skips=gui_skips, backups_skips=bk_skips, guard=guard)
    meta["power"] = {"guarded": guard is not None, **(guard.record() if guard is not None else {})}
    paused = guard.paused_s() if guard is not None else 0.0
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
        doc["checks"].append(rec)
    doc["meta"]["finished"] = _now()      # after the post-run checks (report.tiers may run a nested quick tier)
    doc["meta"]["wall_s"] = round(time.time() - t_start, 1)
    doc["meta"]["timing"] = timing_summary(doc["checks"], doc["meta"]["wall_s"], paused_s=paused)
    doc["meta"]["counts"] = {s: sum(1 for r in doc["checks"] if r["status"] == s) for s in ("pass", "fail", "error", "skip")}
    doc["meta"]["ok"] = run_ok(doc)
    from .util import redact
    doc = redact(doc)                     # no passwords in results.json / summary.md (check output tails etc.)
    write_outputs(doc, P.RESULTS, P.SUMMARY)
    print(P.SUMMARY.read_text())
    return 0 if doc["meta"]["ok"] else 1


# test-loop-speedups "The last full run's results are kept": every unfiltered full-tier run also writes here; no other
# run does. `hs timing-baseline join` reads it by default.
LAST_FULL_RUN = P.OUT / "last-full-run"


def keeps_last_full_run(meta: dict) -> bool:
    """An unfiltered full-tier run (not quick, slow, gui, backups or filtered)."""
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
    return {"results": str(results), "summary": str(summary), "last_full_run": None if last is None else str(last)}


def timing_lines(doc) -> list[str]:
    """summary.md: the timing guard's numbers (report.timing's evidence) and every timing warning."""
    t = doc["meta"].get("timing") or {}
    rt = next((r for r in doc["checks"] if r["id"] == TIMING_SELF), None)
    ev = (rt or {}).get("evidence") or {}
    out = [f"- Timing: build {t.get('build_s', '?')} s, backups tier {t.get('backups_tier_s', '?')} s, "
           f"new checks {len(t.get('new_checks_s') or {})} ({t.get('new_checks_sum_s', '?')} s)"
           + (f"; baseline checks {ev['now_sum_s']} s vs {ev['baseline_sum_s']} s "
              f"(limit {ev['limit_s']} s, ratio {ev['ratio']})" if "now_sum_s" in ev else "")]
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
        "",
        "| Check | Status | Time | Note |",
        "|---|---|---|---|",
    ]
    for r in doc["checks"]:
        note = (r.get("message") or "").replace("|", "\\|").replace("\n", " ")[:140]
        lines.append(f"| `{r['id']}` | {r['status'].upper()} | {r.get('duration_s', 0)}s | {note} |")
    return "\n".join(lines) + "\n"
