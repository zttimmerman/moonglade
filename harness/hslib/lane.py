"""The lane-B worker of a two-lane `hs run` (second-stack; started by lanes.Worker, never by hand):

    HS_LANE=b python -m hslib.lane <spec.json>

It runs inside the parent run (same HS_HARNESS_RUN_ID, so lane B's guard allows it and orphan healing finds it by
the argv token `hslib.lane`), gives lane B a fresh datadir clone (lanestack.reset), then runs the spec's checks one
at a time through runner.execute with lane B's stack. Before each check it probes memory (lanes.midrun_stop) and the
battery (below the power guard's pause level) and checks that its parent run is still alive; any of them stops it
from starting further checks. After each check it rewrites its state file (records, per-check timeline, the
remaining ids), so the parent knows what finished even if the worker is killed. At the end lane B is stopped.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from . import paths as P


def _write(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, default=str))
    tmp.replace(path)


def main(argv: list[str]) -> int:
    spec = json.loads(Path(argv[0]).read_text())
    out = Path(spec["state"])
    state: dict = {"started": time.time(), "pid": os.getpid(), "records": [], "timeline": {}, "remaining": list(spec["ids"]),
                   "stop": None, "prepare": None, "down": None, "finished": None, "error": None}
    _write(out, state)
    if P.LANE != "b" or P.lane_refusal():
        state |= {"error": f"refused: lane {P.LANE}, {P.lane_refusal()}", "finished": time.time()}
        _write(out, state)
        return 2
    from . import lanes, lanestack, power, runner
    from .registry import load_all
    reg = load_all()
    try:
        state["prepare"] = lanestack.reset()           # lane B down + a fresh datadir clone of the template
        _write(out, state)
        probe = lanes.Probe(spec.get("memory_stub"))
        battery = power.Battery(spec.get("battery_seq"))     # None: the real pmset reading
        start = spec.get("memory_at_run_start") or {}
        ctx = {"tier": spec.get("tier"), "force_clean": spec.get("force_clean", False), "lane": "b",
               "lane_timeline": state["timeline"]}
        ids = list(spec["ids"])
        for i, cid in enumerate(ids):
            reading = probe.midrun()
            why = lanes.midrun_stop(reading, start)
            if why is None and not spec.get("ignore_power"):
                b = battery.read()
                if b.get("percent") is not None and b["percent"] < power.PAUSE_PCT:
                    why = f"power: battery at {b['percent']} % (the power guard pauses below {power.PAUSE_PCT} %)"
            if why is None and os.getppid() != spec.get("parent_pid"):
                why = "the parent harness run is gone"
            if why is not None:
                state["stop"] = {"reason": why, "before_check": cid, "memory": reading, "at": time.time()}
                state["remaining"] = ids[i:]
                _write(out, state)
                print(f"[hs b] lane B stops before {cid}: {why}", file=sys.stderr, flush=True)
                break
            state["current"] = {"id": cid, "start": time.time(), "memory": reading}
            _write(out, state)
            rec = runner.execute([reg[cid]], ctx)[0]
            state["timeline"].setdefault(cid, {})["memory_before"] = reading
            state["records"].append(rec)
            state["remaining"] = ids[i + 1:]
            state.pop("current", None)
            _write(out, state)
    except Exception as e:  # noqa: BLE001  (recorded; the parent runs what's left in lane A)
        import traceback
        state["error"] = f"{type(e).__name__}: {e}"
        state["traceback"] = traceback.format_exc()[-3000:]
    finally:
        try:
            state["down"] = lanestack.down()
        except Exception as e:  # noqa: BLE001
            state["down"] = {"error": f"{type(e).__name__}: {e}"}
        state["finished"] = time.time()
        _write(out, state)
    return 1 if state["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
