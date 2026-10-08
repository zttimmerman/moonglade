"""Harness run files and orphan healing (test-loop-speedups, harness-reporting "A killed run's processes don't block
the next run").

Every `hs run` writes a run file `env/run/harness-runs/<run id>.json` at its start and removes it when it exits (also
on error). The run id is `<pid>-<process start time>`, so a reused pid never looks alive. The run also exports
`$HS_HARNESS_RUN_ID=<run id>` to every subprocess it starts (./play, Wine, the servers inherit it), next to
`$HS_HARNESS_RUN=1`.

A run file whose run is no longer alive means that run was killed (SIGKILL, power loss). The next `hs run` treats the
processes still carrying that run id in their environment as its orphans and stops them the existing way: ./play
gets SIGTERM (its own cleanup stops the client and the stack gracefully), then Wine's prefix is stopped and `hs down`
runs, each only when every process of that kind is an orphan. A process without the tag, such as the player's own
./play or the app's, is never touched.
"""
from __future__ import annotations

import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from . import paths as P, procs

RUN_DIR = P.RUN_REGISTRY          # global (env/run/harness-runs): every lane sees every run (second-stack)
ENV_ID = "HS_HARNESS_RUN_ID"
PLAY_TERM_WAIT_S = 150        # ./play's cleanup (client stop + graceful stack down) normally takes 10-60 s
OTHER_TERM_WAIT_S = 10


def run_id(pid: int | None = None) -> str | None:
    pid = pid or os.getpid()
    st = procs.start_time(pid)
    return None if st is None else f"{pid}-{st}"


def alive(rec: dict) -> bool:
    """The recorded run's process still exists (same pid and start time)."""
    pid = rec.get("pid")
    return isinstance(pid, int) and run_id(pid) == rec.get("id")


def start(run_dir: Path = RUN_DIR) -> dict:
    """Write this process's run file and tag its future subprocesses. Returns the record."""
    rid = run_id()
    rec = {"id": rid, "pid": os.getpid(), "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{rid}.json").write_text(json.dumps(rec) + "\n")
    os.environ[ENV_ID] = rid
    return rec


def finish(rec: dict, run_dir: Path = RUN_DIR) -> None:
    try:
        (run_dir / f"{rec['id']}.json").unlink()
    except FileNotFoundError:
        pass


def records(run_dir: Path = RUN_DIR) -> list[dict]:
    out = []
    for f in sorted(run_dir.glob("*.json")) if run_dir.is_dir() else []:
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            d = {"id": f.stem, "pid": None, "unreadable": True}
        out.append(d | {"file": str(f)})
    return out


def active_runs(run_dir: Path = RUN_DIR, exclude_self: bool = True) -> list[dict]:
    me = run_id() if exclude_self else None
    return [r for r in records(run_dir) if alive(r) and r.get("id") != me]


def dead_runs(run_dir: Path = RUN_DIR) -> list[dict]:
    return [r for r in records(run_dir) if not alive(r)]


def _ancestors() -> set[int]:
    out, p = set(), os.getpid()
    while p and p not in out and p > 1:
        out.add(p)
        p = procs.ppid(p) or 0
    return out


def tagged(ids: set[str]) -> list[dict]:
    """This user's processes whose (initial) environment carries HS_HARNESS_RUN=1 and HS_HARNESS_RUN_ID in `ids`,
    except this process and its ancestors."""
    if not ids:
        return []
    me, skip = os.getuid(), _ancestors()
    out = []
    for pid in procs.pids():
        if pid in skip or procs.uid(pid) != me:
            continue
        ae = procs.args_env(pid)
        if not ae:
            continue
        env = dict(e.split("=", 1) for e in ae[1] if "=" in e)
        if env.get("HS_HARNESS_RUN") == "1" and env.get(ENV_ID) in ids:
            out.append({"pid": pid, "run": env.get(ENV_ID), "cmd": " ".join(ae[0])[:1000],
                        "lane": (env.get(P.LANE_ENV) or "").strip().lower() or "a"})
    return out


def _wait_gone(pids: list[int], timeout: float) -> list[int]:
    t0 = time.time()
    left = list(pids)
    while left and time.time() - t0 < timeout:
        time.sleep(0.5)
        left = [p for p in left if _alive_pid(p)]
    return left


def _alive_pid(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not procs.is_zombie(pid)


def _lane_pids(lane: str) -> set[int]:
    """Stack + Wine-prefix processes of `lane`."""
    from . import launch, stack
    return ({p for v in stack.stack_processes(lane).values() for p in v}
            | {p["pid"] for p in launch.prefix_processes(prefix=P.layout(lane)["wineprefix"])})


def lane_down(lane: str, timeout: float = 600) -> dict:
    """Stop `lane`'s Wine prefix and stack from a process of another lane: `hs lane down` with HS_LANE=<lane>
    (lane down is the one lane-B command allowed outside a harness run; it only stops things)."""
    from .lanestack import run_in_lane
    r = run_in_lane(lane, ["lane", "down"], timeout=timeout)
    out = {"lane": lane, "rc": r.returncode, "tail": (r.stdout + r.stderr)[-1500:]}
    try:
        out["result"] = json.loads(r.stdout)
    except ValueError:
        pass
    return out


def heal(run_dir: Path = RUN_DIR, stop_prefix=None, stack_down=None, prefix_pids=None, stack_pids=None,
         other_lanes=None, other_lane_pids=None, other_lane_down=None) -> dict:
    """Stop the orphans of every dead run recorded in `run_dir`. Returns evidence:
    {"dead_runs", "orphans" (found at the start), "actions", "remaining"}. The stop functions and process listers
    default to launch.stop_prefix / stack.down / launch.prefix_processes / stack.stack_processes (this lane), and for
    the other lanes (second-stack: per-lane healing) to _lane_pids / lane_down over every lane but this one."""
    from . import launch, stack
    stop_prefix = stop_prefix or launch.stop_prefix
    stack_down = stack_down or stack.down
    prefix_pids = prefix_pids or (lambda: {p["pid"] for p in launch.prefix_processes()})
    stack_pids = stack_pids or (lambda: {p for v in stack.stack_processes().values() for p in v})
    other_lanes = [l for l in P.LANES if l != P.LANE] if other_lanes is None else other_lanes
    other_lane_pids = other_lane_pids or _lane_pids
    other_lane_down = other_lane_down or lane_down
    dead = dead_runs(run_dir)
    ids = {r["id"] for r in dead if r.get("id")}
    ev = {"dead_runs": [{k: r.get(k) for k in ("id", "pid", "started")} for r in dead], "orphans": [],
          "actions": [], "remaining": []}
    if not ids:
        return ev
    found = tagged(ids)
    ev["orphans"] = found
    # 0. lane workers (second-stack: `python -m hslib.lane`): stopped first, so they start nothing while we heal
    workers = [o["pid"] for o in found if "hslib.lane" in o["cmd"].split(" ")]
    for pid in workers:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for pid in _wait_gone(workers, OTHER_TERM_WAIT_S):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if workers:
        ev["actions"].append({"stop_lane_workers": workers})
    # 1. ./play: SIGTERM, its own handler stops the client and the stack gracefully
    plays = [o["pid"] for o in found if "hslib.play" in o["cmd"]]
    for pid in plays:
        try:
            os.kill(pid, signal.SIGTERM)
            ev["actions"].append({"sigterm_play": pid})
        except ProcessLookupError:
            pass
    left = _wait_gone(plays, PLAY_TERM_WAIT_S)
    if left:
        ev["actions"].append({"play_still_running_after_s": PLAY_TERM_WAIT_S, "pids": left})
    # 2. Wine of this prefix, only if every prefix process is an orphan
    orph = {o["pid"] for o in tagged(ids)}
    pre = prefix_pids()
    if pre and pre <= orph:
        ev["actions"].append({"stop_prefix": stop_prefix()})
    elif pre & orph:
        ev["actions"].append({"prefix_left": "prefix processes not started by the dead run are running",
                              "untagged": sorted(pre - orph)})
    # 3. the stack (`hs down`), only if every stack process is an orphan
    orph = {o["pid"] for o in tagged(ids)}
    sp = stack_pids()
    if sp and sp <= orph:
        ev["actions"].append({"stack_down": stack_down()})
    elif sp & orph:
        ev["actions"].append({"stack_left": "stack processes not started by the dead run are running",
                              "untagged": sorted(sp - orph)})
    # 3b. every other lane's prefix + stack (lane_down), only if every one of its processes is an orphan
    others: set[int] = set()
    for lane in other_lanes:
        orph = {o["pid"] for o in tagged(ids)}
        lp = other_lane_pids(lane)
        if lp and lp <= orph:
            ev["actions"].append({"lane_down": other_lane_down(lane)})
        elif lp & orph:
            ev["actions"].append({"lane_left": lane, "why": "processes of that lane not started by the dead run are "
                                  "running", "untagged": sorted(lp - orph)})
        others |= other_lane_pids(lane)
    # 4. anything else the dead run started (a nested hs run, a helper): SIGTERM, then SIGKILL
    rest = [o["pid"] for o in tagged(ids) if o["pid"] not in prefix_pids() and o["pid"] not in stack_pids()
            and o["pid"] not in others]
    for pid in rest:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    stubborn = _wait_gone(rest, OTHER_TERM_WAIT_S)
    for pid in stubborn:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if rest:
        ev["actions"].append({"sigterm_other": rest, "sigkilled": stubborn})
    ev["remaining"] = tagged(ids)
    if not ev["remaining"]:
        for r in dead:
            try:
                Path(r["file"]).unlink()
            except (FileNotFoundError, KeyError):
                pass
    return ev
