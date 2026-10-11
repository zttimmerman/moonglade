"""Clustered integration sessions (openspec/changes/test-pyramid design 13 rule 3): one check runs several scenarios in
one session; each scenario keeps its own id (registry `children`) and gets its own status and evidence
(runner._child_records). A scenario's function raises CheckFail like a check; its failure fails the session check
too, never the next scenario (each runs whatever happened before it, with its own GIVEN asserted as before)."""
from __future__ import annotations

import time
import traceback

from .registry import CheckFail, CheckSkip


def run_children(ctx: dict, steps, stop_on=()) -> dict:
    """steps: [(scenario id, fn(ctx) -> evidence)]. Returns {"children": {id: record}, "order": [...]}.
    A scenario id in `stop_on` that doesn't pass ends the session (later scenarios depend on it): they are recorded
    as errors naming it."""
    out: dict = {}
    order = []
    stopped = None
    for sid, fn in steps:
        order.append(sid)
        if stopped:
            out[sid] = {"status": "error", "message": f"not run: {stopped} didn't pass and this scenario needs it",
                        "evidence": {"blocked_by": stopped}, "duration_s": 0.0}
            continue
        t0 = time.time()
        try:
            ev = fn(ctx) or {}
            rec = {"status": "pass", "evidence": ev}
        except CheckFail as e:
            rec = {"status": "fail", "message": str(e)[:500], "evidence": e.evidence or {"failed": True}}
        except CheckSkip as e:
            rec = {"status": "skip", "message": str(e), "evidence": {"skip_reason": str(e), **e.evidence}}
        except Exception as e:  # noqa: BLE001
            rec = {"status": "error", "message": f"{type(e).__name__}: {e}"[:500],
                   "evidence": {"traceback": traceback.format_exc()[-3000:]}}
        rec["duration_s"] = round(time.time() - t0, 2)
        out[sid] = rec
        if sid in stop_on and rec["status"] != "pass":
            stopped = sid
    return {"children": out, "order": order}
