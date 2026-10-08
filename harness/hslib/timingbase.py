"""The per-check timing baseline, harness/timing-baseline.json (harness-reporting "Full-run timing is recorded and
guarded"; seamless-experience, Zach 2026-10-07: "catch real slowdowns only").

Each executed check's time from a verifier's unfiltered full run, minus its time spent building server binaries
(runner.check_build_s), with the run it came from:

    {"schema": 2, "host": {...}, "runs": {"<label>": {"source", "repo_sha", "started", ...}},
     "checks": {"<id>": {"s": <seconds without build time>, "duration_s": ..., "build_s": ..., "run": "<label>"}}}

report.timing (reporting.timing_problems) compares a full run's baseline checks with these times.

When a change merges, its new checks join with their times from its verifier's full run (documented step, run by the
orchestrator at merge time):

    harness/hs timing-baseline join [<verifier results-full.json>] --label <change id>

Without a path it reads harness/out/last-full-run/results.json, the copy every unfiltered full run keeps (filtered and
quick runs never overwrite it; test-loop-speedups).

`join` only adds checks the baseline doesn't have; existing times are never raised (or changed) that way.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import paths as P
from .runner import LAST_FULL_RUN, TIMING_BASELINE, TIMING_SELF, check_build_s

SCHEMA = 2


class BaselineError(RuntimeError):
    pass


def _run_doc(path: Path) -> dict:
    d = json.loads(Path(path).read_text())
    m = d["meta"]
    if not (m.get("full_run") is True and m.get("tier") == "full" and not m.get("filter")):
        raise BaselineError(f"{path} isn't an unfiltered full-tier run")
    return d


def run_info(path: Path, d: dict) -> dict:
    m = d["meta"]
    p = Path(path).resolve()
    return {"source": str(p.relative_to(P.ROOT)) if P.ROOT in p.parents else str(p), "repo_sha": m.get("repo_sha"),
            "started": m.get("started"), "finished": m.get("finished"), "wall_s": m.get("wall_s"),
            "machine": (m.get("host") or {}).get("machine")}


def entries(d: dict, label: str, ids: set | None = None) -> dict:
    out = {}
    for r in d["checks"]:
        if r["status"] == "skip" or r["id"] == TIMING_SELF or (ids is not None and r["id"] not in ids):
            continue
        b = check_build_s(r)
        bb = (r.get("evidence") or {}).get("baseline_build")
        if not b and isinstance(bb, dict) and isinstance(bb.get("wall_s"), (int, float)):
            # a run from before core.login_logout_race_baseline recorded `build_s`: its baseline_build step's time
            b = min(float(r.get("duration_s") or 0.0), float(bb["wall_s"]))
        out[r["id"]] = {"s": round(float(r.get("duration_s") or 0.0) - b, 2), "duration_s": r.get("duration_s"),
                        "build_s": b, "status_in_run": r["status"], "run": label}
    return out


def build(primary: Path, primary_label: str, overrides: list[tuple[Path, str, list[str], str]]) -> dict:
    """A new baseline from `primary`'s executed checks; each override (path, label, ids, why) takes those ids from
    another run instead (recorded with the reason)."""
    d = _run_doc(primary)
    base = {"what": "Per-check timing baseline for report.timing (harness-reporting 'Full-run timing is recorded and "
                    "guarded'); see harness/hslib/timingbase.py.",
            "schema": SCHEMA, "host": d["meta"].get("host"), "runs": {primary_label: run_info(primary, d)},
            "checks": entries(d, primary_label)}
    for path, label, ids, why in overrides:
        o = _run_doc(path)
        got = entries(o, label, set(ids))
        if set(got) != set(ids):
            raise BaselineError(f"{path}: not executed there: {sorted(set(ids) - set(got))}")
        base["runs"][label] = run_info(path, o) | {"used_for": sorted(ids), "why": why}
        base["checks"].update(got)
    return base


def join(path: Path, label: str, baseline_path: Path = TIMING_BASELINE) -> dict:
    """Add the checks `path` executed that the baseline lacks; never touches existing entries."""
    base = json.loads(Path(baseline_path).read_text())
    if base.get("schema") != SCHEMA:
        raise BaselineError("not a per-check baseline")
    d = _run_doc(path)
    if label in base["runs"]:
        raise BaselineError(f"run label {label!r} is already in the baseline")
    new = {i: e for i, e in entries(d, label).items() if i not in base["checks"]}
    if new:
        base["runs"][label] = run_info(path, d) | {"used_for": sorted(new), "why": "checks new with this change"}
        base["checks"].update(new)
        Path(baseline_path).write_text(json.dumps(base, indent=1, sort_keys=True) + "\n")
    return {"added": sorted(new), "unchanged": len(base["checks"]) - len(new)}


def default_results() -> Path:
    return LAST_FULL_RUN / "results.json"


def cli(args) -> int:
    if args.action == "join":
        path = Path(args.results) if args.results else default_results()
        if not path.is_file():
            from .util import log
            log(f"no results at {path}" + ("" if args.results else " (no unfiltered full run kept yet; pass a "
                                                                     "results.json)"))
            return 1
        try:
            res = join(path, args.label, Path(getattr(args, "baseline", None) or TIMING_BASELINE))
        except BaselineError as e:
            from .util import log
            log(f"timing-baseline join: {e}")
            return 1
        print(json.dumps(res | {"results": str(path)}, indent=1))
        return 0
    raise SystemExit(f"unknown action {args.action}")
