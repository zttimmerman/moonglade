"""`hs run`: execute checks, write results.json + summary.md."""
import fnmatch
import json
import platform
import shutil
import subprocess
import time
import traceback
from datetime import datetime, timezone

from . import paths as P, stack
from .registry import CheckFail, CheckSkip, load_all, tiers_for
from .util import log


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git_sha():
    r = subprocess.run(["git", "-C", P.ROOT, "rev-parse", "HEAD"], capture_output=True, text=True)
    dirty = subprocess.run(["git", "-C", P.ROOT, "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
    return (r.stdout.strip() or "unknown") + ("-dirty" if dirty else "")


def _ensure_stack(want: str, ctx: dict):
    st = stack.stack_processes()
    is_up = all(st[k] for k in st)
    if want == "up" and not is_up:
        ctx.setdefault("stack_transitions", []).append({"up": stack.up()})
    elif want == "down" and any(st.values()):
        ctx.setdefault("stack_transitions", []).append({"down": stack.down()})


def is_full_run(tier: str, only: list[str] | None) -> bool:
    """A run is *full* only if it is unfiltered and its tier is full or slow (harness-reporting)."""
    return not only and tier in ("full", "slow")


def tolerated_skip(rec: dict, meta: dict) -> bool:
    """The only non-pass status that doesn't fail a run: report.full_run = skip on a run that isn't full
    (harness-reporting "Single-command full run"). Any other skip, and any skip on a full run, fails."""
    return rec["id"] == "report.full_run" and rec["status"] == "skip" and meta.get("full_run") is False


def select(reg: dict, tier: str, only: list[str] | None) -> list:
    tiers = tiers_for(tier)
    return sorted(
        (c for c in reg.values()
         if c.tier in tiers and (not only or any(fnmatch.fnmatch(c.id, g) for g in only))),
        key=lambda c: (c.order, c.id),
    )


def run(only: list[str] | None = None, leave_up: bool = False, tier: str = "full",
        force_clean: bool = False) -> int:
    t_start = time.time()
    reg = load_all()
    selected = select(reg, tier, only)
    full = is_full_run(tier, only)
    P.OUT.mkdir(parents=True, exist_ok=True)
    du = shutil.disk_usage(P.ROOT)
    meta = {
        "started": _now(), "repo_sha": _git_sha(), "fork_lock": P.fork_lock(), "full_run": full,
        "tier": tier, "force_clean": force_clean,
        "filter": only or [], "host": {"platform": platform.platform(), "machine": platform.machine(),
                                      "python": platform.python_version(), "disk_free_gb": round(du.free / 2**30, 1)},
    }
    results, ctx = [], {"force_clean": force_clean, "tier": tier}
    for c in selected:
        t0 = time.time()
        rec = {"id": c.id, "capability": c.capability, "description": c.doc}
        try:
            _ensure_stack(c.stack, ctx)
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
        log(f"{rec['status'].upper():5} {c.id} ({rec['duration_s']}s){'  ' + rec.get('message', '') if rec['status'] != 'pass' else ''}")
        results.append(rec)

    if not leave_up and any(stack.stack_processes().values()):
        ctx.setdefault("stack_transitions", []).append({"down(final)": stack.down()})

    doc = {"meta": meta | {"finished": _now()}, "checks": results}
    # post-run meta-checks (report.*) — computed over the document itself
    from .checks import reporting
    for rec in reporting.post_run(doc, selected_ids=[c.id for c in selected], t_start=t_start,
                                  leave_up=leave_up):
        doc["checks"].append(rec)
    doc["meta"]["finished"] = _now()      # after the post-run checks (report.tiers may run a nested quick tier)
    doc["meta"]["wall_s"] = round(time.time() - t_start, 1)
    doc["meta"]["counts"] = {s: sum(1 for r in doc["checks"] if r["status"] == s) for s in ("pass", "fail", "error", "skip")}
    doc["meta"]["ok"] = all(r["status"] == "pass" or tolerated_skip(r, doc["meta"]) for r in doc["checks"])
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
        "",
        "| Check | Status | Time | Note |",
        "|---|---|---|---|",
    ]
    for r in doc["checks"]:
        note = (r.get("message") or "").replace("|", "\\|").replace("\n", " ")[:140]
        lines.append(f"| `{r['id']}` | {r['status'].upper()} | {r.get('duration_s', 0)}s | {note} |")
    return "\n".join(lines) + "\n"
