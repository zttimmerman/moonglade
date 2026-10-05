"""`hs run`: execute checks, write results.json + summary.md."""
import fnmatch
import json
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
# The GUI path rule: a change touching any of these (fnmatch against repo-relative paths) runs the gui checks in
# full runs. The launcher app and its sources, the Wine host Dock shim, the play loop and launch code that drive
# the hand-off (client window detection, dock_env, stage events), the ./play entry point, and the GUI checks with
# the app-check helpers their shared session uses.
GUI_PATHS = (
    "harness/app/*",                          # launcher.m, dockshim.m
    "harness/hslib/app.py",                   # builds dist/Hearthstone.app
    "harness/hslib/dockshim.py",              # builds/loads the Dock shim
    "harness/hslib/launch.py",                # dock_env, client_window, start_client, prefix discovery
    "harness/hslib/play.py",                  # the play loop the app runs (stages, ready -> hand-off)
    "harness/hslib/stages.py",                # loading-window stage protocol
    "play",                                   # the ./play entry point
    "harness/hslib/checks/polish_checks.py",  # the gui checks and their shared app session
    "harness/hslib/checks/app_checks.py",     # open_app / Hook / build_app used by that session
    "harness/hslib/checks/lssampler.m",       # the session's LaunchServices sampler (app.single_dock_icon)
    # launcher-fixes (F26): the shared helpers that session runs on
    "harness/hslib/checks/play_checks.py",    # StandIn (the notepad stand-in's base), remaining/is_clear
    "harness/hslib/procs.py",                 # process table, launcher / prefix discovery
    "harness/hslib/paths.py",                 # every path the session uses (runtime, prefix, logs)
    "harness/hslib/util.py",                  # log, keep_awake
    "harness/hslib/config.py",                # profiles / world size of the session
    "harness/hslib/runner.py",                # this rule and the GUI tier itself
    "harness/hslib/frontmenu.py",             # the play loop's login file step (select_client)
    "harness/hslib/clientbuild.py",           # the play loop's client selection
    "harness/hslib/clientcfg.py",             # game_setup: the client config written before the client starts
    "harness/hslib/resolution.py",            # game_setup: the resolution default
)


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


def gui_paths_matching(paths: list[str] | None, patterns=GUI_PATHS) -> list[str] | None:
    """The changed paths that fall under the GUI path rule; None (unknown) stays None."""
    if paths is None:
        return None
    return sorted(p for p in paths if any(fnmatch.fnmatch(p, g) for g in patterns))


def gui_rule(root=None) -> dict:
    """The GUI path rule for this checkout: {"changed_gui_paths": [...] or None (git failed: counts as changed),
    "base_ref", "base", "error", "changed_paths_count"}."""
    c = changed_paths(root)
    return {"base_ref": c["base_ref"], "base": c["base"], "error": c["error"],
            "changed_paths_count": None if c["paths"] is None else len(c["paths"]),
            "changed_gui_paths": gui_paths_matching(c["paths"]), "patterns": list(GUI_PATHS)}


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
    return is_gui_skip(rec, meta, reg)


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


def execute(checks: list, ctx: dict, slow_skips: list = (), ensure=None, quiet: bool = False,
            gui_skips: list = ()) -> list[dict]:
    """Run `checks` in (order, id) sequence, recording `slow_skips` as skip ("slow tier") and `gui_skips` as skip
    ("gui tier") in their place. `ensure(stack_want, ctx, profile)` brings the stack to each check's required state
    (default _ensure_stack). `quiet` suppresses the per-check log lines (synthetic runs of the report.* rules)."""
    ensure = ensure or _ensure_stack
    say = (lambda _m: None) if quiet else log
    skip_ids = {c.id for c in slow_skips}
    gui_ids = {c.id for c in gui_skips}
    results = []
    for c in sorted([*checks, *slow_skips, *gui_skips], key=lambda c: (c.order, c.id)):
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


def timing_summary(records: list[dict], wall_s: float | None) -> dict:
    """harness-reporting "Full-run timing": per-check wall time, their sum, the run's wall time, and the 10 slowest."""
    per = {r["id"]: float(r.get("duration_s") or 0.0) for r in records}
    top = sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    return {"per_check_s": per, "checks_sum_s": round(sum(per.values()), 1),
            "total_wall_s": None if wall_s is None else round(wall_s, 1),
            "top10": [{"id": i, "s": v} for i, v in top]}


def select(reg: dict, tier: str, only: list[str] | None, gui_skip_ids: set | frozenset = frozenset()) -> list:
    """Checks a run executes: those of the tier's check tiers (gui: only gui-marked ones) matching the filter, minus
    the ones recorded as GUI skips."""
    tiers = tiers_for(tier)
    return sorted(
        (c for c in reg.values()
         if c.tier in tiers and (tier != "gui" or c.gui) and c.id not in gui_skip_ids
         and (not only or any(fnmatch.fnmatch(c.id, g) for g in only))),
        key=lambda c: (c.order, c.id),
    )


def run(only: list[str] | None = None, leave_up: bool = False, tier: str = "full",
        force_clean: bool = False) -> int:
    # F12: a mysqld stall ("Semaphore wait > 600 s") happened while the display slept; hold a no-sleep assertion
    with keep_awake() as awake:
        return _run(only, leave_up, tier, force_clean, awake)


def _run(only, leave_up, tier, force_clean, awake) -> int:
    t_start = time.time()
    reg = load_all()
    rule = gui_rule() if tier in ("full", "slow") else None
    gui_skips = gui_tier_skips(reg, tier, only, gui_changed(rule)) if rule is not None else []
    selected = select(reg, tier, only, {c.id for c in gui_skips})
    slow_skips = slow_tier_skips(reg, tier, only, referenced_ids())
    full = is_full_run(tier, only)
    P.OUT.mkdir(parents=True, exist_ok=True)
    du = shutil.disk_usage(P.ROOT)
    meta = {
        "started": _now(), "repo_sha": _git_sha(), "fork_lock": P.fork_lock(), "full_run": full,
        "tier": tier, "force_clean": force_clean, "slow_tier_skipped": [c.id for c in slow_skips],
        "gui_tier_skipped": [c.id for c in gui_skips], "gui_rule": rule,
        "keep_awake": awake,
        "filter": only or [], "host": {"platform": platform.platform(), "machine": platform.machine(),
                                      "python": platform.python_version(), "disk_free_gb": round(du.free / 2**30, 1)},
    }
    ctx = {"force_clean": force_clean, "tier": tier}
    if rule is not None:
        log(f"GUI rule: {'GUI checks run' if gui_changed(rule) else 'GUI checks skipped by design'} "
            f"(changed GUI paths: {rule['changed_gui_paths']}{'; ' + rule['error'] if rule['error'] else ''})")
    results = execute(selected, ctx, slow_skips, gui_skips=gui_skips)

    if not leave_up and any(stack.stack_processes().values()):
        ctx.setdefault("stack_transitions", []).append({"down(final)": stack.down()})

    doc = {"meta": meta | {"finished": _now()}, "checks": results}
    doc["meta"]["timing"] = timing_summary(results, time.time() - t_start)   # report.timing reads it; final below
    # post-run meta-checks (report.*) — computed over the document itself
    from .checks import reporting
    for rec in reporting.post_run(doc, selected_ids=[c.id for c in selected], t_start=t_start,
                                  leave_up=leave_up):
        doc["checks"].append(rec)
    doc["meta"]["finished"] = _now()      # after the post-run checks (report.tiers may run a nested quick tier)
    doc["meta"]["wall_s"] = round(time.time() - t_start, 1)
    doc["meta"]["timing"] = timing_summary(doc["checks"], doc["meta"]["wall_s"])
    doc["meta"]["counts"] = {s: sum(1 for r in doc["checks"] if r["status"] == s) for s in ("pass", "fail", "error", "skip")}
    doc["meta"]["ok"] = run_ok(doc)
    from .util import redact
    doc = redact(doc)                     # no passwords in results.json / summary.md (check output tails etc.)
    P.RESULTS.write_text(json.dumps(doc, indent=2, default=str))
    P.SUMMARY.write_text(summary_md(doc))
    log(f"wrote {P.RESULTS} and {P.SUMMARY}")
    print(P.SUMMARY.read_text())
    return 0 if doc["meta"]["ok"] else 1


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
        "",
        "| Check | Status | Time | Note |",
        "|---|---|---|---|",
    ]
    for r in doc["checks"]:
        note = (r.get("message") or "").replace("|", "\\|").replace("\n", " ")[:140]
        lines.append(f"| `{r['id']}` | {r['status'].upper()} | {r.get('duration_s', 0)}s | {note} |")
    return "\n".join(lines) + "\n"
