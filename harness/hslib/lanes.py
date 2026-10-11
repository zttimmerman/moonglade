"""Two-lane scheduling for `hs run` (ADR-0011 Phase 1, openspec/changes/second-stack, task group 2).

`hs run` runs the checks of the reviewed LANE_B table in a lane-B worker (`python -m hslib.lane <spec.json>`, with
HS_LANE=b and this run's id) while lane A runs everything else:

- Start barrier: the worker starts once every selected check with order < START_ORDER (34: the build/setup checks
  and the lane.* checks, which drive lane B themselves) has run.
- Join barrier: lane A waits for the worker before the first executed check that has order >= JOIN_ORDER (380, the
  GUI/app phase), is gui-marked or `app.*`, or has order >= PROTECT_ORDER (998, the player-protection checks); with no
  such check, before the post-run checks.
- Records are merged in (order, id) order, so results.json reads as if the run were serial. A record executed in lane
  B carries "lane": "b". meta.lanes holds the assignment, lane B's wall time and per-check timeline, the preflight's
  inputs and decision, and the fallback reason.
- `--lanes 1` runs everything serially in lane A (the old code path). Default: 2 for unfiltered full/slow runs, 1 for
  every other run (quick, gui, backups, and every filtered run: those are short, and a second stack's start-up would
  cost more than it saves; pass `--lanes 2` to a filtered run to use lane B).
- Lane B runs only if the preflight (memory per ADR-0011 "Memory preflight", plus battery) allows it; otherwise the
  run is serial and meta.lanes.fallback says why. Mid-run, before each lane-B check, the worker probes again: pressure
  level >= 2, swap growth > 1.5 GB since the run started, or the battery below the power guard's pause level stop B
  from starting further checks; lane A runs the rest at the join. A worker that dies or times out (its process group
  is killed, then `hs lane down`) also leaves its unfinished checks to lane A, and report.lanes fails such a run.

Test switches (TEST_ENV; accepted only by a nested `hs run` inside a harness run, refused by every other entry point;
read once by the run and removed from its environment, so no check inherits them):
  HS_TEST_LANE_MEMORY     JSON: a memory reading {pressure_level, avail_gb, swap_used_gb, swap_free_gb} used for
                          every probe, or {"preflight": reading, "midrun": [reading, ...]} (the worker's i-th probe,
                          before its i-th check, uses midrun[i], the last one repeating)
  HS_TEST_LANE_B          comma list of check ids: the run's lane-B set instead of the LANE_B table (forced lane;
                          each still has to pass the table rules)
  HS_TEST_LANE_TIMEOUT_S  the worker's timeout in seconds (default WORKER_TIMEOUT_S)
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import paths as P

# ---------------------------------------------------------------- the reviewed table
# Phase 1 (ADR-0011): server and stand-in checks. Each entry says why it's safe in lane B. Left out on purpose (see
# design.md "As built: scheduler"): report.tiers (a post-run check; its nested quick tier has a 180 s budget that
# contention would break, and runs app.bundle_valid, which rebuilds dist/Moonglade.app and kills stale test
# launchers), play.player_account_untouched (a player-protection check), and comp.* (they need the random bots that
# lane A's long-lived DB has; lane B's per-run template clone has none online: in the measured two-lane run
# 2026-10-08, comp.add_remove/follows/quest_shared failed "no online priest joined the party within 30 s").
LANE_B = {
    "era.sql_deterministic": "era SQL against lane B's world DB",
    "era.default_on": "reads lane B's rendered confs (P.LANE_ETC) and its module log",
    "era.xp_curve": "lane B's running server",
    "era.quest_xp": "lane B's running server and rendered worldserver.conf",
    "era.elites": "lane B's running server and rendered worldserver.conf",
    "era.mounts": "lane B's running server",
    "era.session_toggle": "era.toggle_roundtrip + era.scope_guard on one off/on restart pair of lane B's stack; the "
                          "confs and DBs are lane B's",
    "lifecycle.persist": "`hs down`/`hs up` inherit HS_LANE=b; lane B's characters DB",
    # test-pyramid part 3: the former play.* stand-in checks are scenarios of play.cold_sessions, which needs the GUI
    # (its terminal session's notepad window and Dock state), so it stays in lane A
}

START_ORDER = 34
JOIN_ORDER = 380
PROTECT_ORDER = 998
WORKER_TIMEOUT_S = 3600.0     # lane B's checks take ~10 min serially; each has its own internal timeouts
JOIN_POLL_S = 1.0

MEMORY_ENV = "HS_TEST_LANE_MEMORY"
FORCE_ENV = "HS_TEST_LANE_B"
TIMEOUT_ENV = "HS_TEST_LANE_TIMEOUT_S"
TEST_ENV = (MEMORY_ENV, FORCE_ENV, TIMEOUT_ENV)

# ADR-0011 "Memory preflight" (docs/research/second-stack-spike.md)
PRESSURE_OK = 1               # kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical
AVAIL_MIN_GB = 5.0            # free + inactive + speculative + purgeable, with A's stack up (B measured 4.6 GB)
AVAIL_MIN_CLIENT_GB = 6.0     # Phase 2 (a real client in B); not used in Phase 1
SWAP_FREE_MIN_GB = 1.0
SWAP_GROWTH_START_MAX_GB = 1.0
MIDRUN_PRESSURE_STOP = 2
MIDRUN_SWAP_GROWTH_MAX_GB = 1.5
BATTERY_MIN_PCT = 50          # power.RESUME_PCT: below it (or on battery power) B doesn't start


# ---------------------------------------------------------------- test switches

def set_switches(environ=None) -> list[str]:
    env = os.environ if environ is None else environ
    return [v for v in TEST_ENV if env.get(v)]


def refusal(cmd: str, environ=None) -> str | None:
    """Why an entry point must refuse the lane test switches, or None: only `hs run` inside a harness run
    (HS_HARNESS_RUN=1, a nested run) accepts them; every other command, ./play, ./bootstrap and the app refuse."""
    env = os.environ if environ is None else environ
    on = set_switches(env)
    if not on:
        return None
    if cmd == "run" and env.get("HS_HARNESS_RUN") == "1":
        return None
    return (f"${on[0]} is set: that's a test switch of the harness's two-lane scheduler, not for normal use. "
            f"Unset it (`unset {' '.join(on)}`) and run it again.")


def take_switches() -> dict:
    """Read the lane test switches once and remove them from this process's environment."""
    out = {v: os.environ.get(v) for v in TEST_ENV if os.environ.get(v)}
    for v in TEST_ENV:
        os.environ.pop(v, None)
    return out


# ---------------------------------------------------------------- the table rules (lane.table)

PROTECTION_IDS = ("play.player_account_untouched", "client.player_config_untouched",
                  "client.player_client_not_rebuilt", "backups.player_untouched")


def forbidden(c) -> str | None:
    """Why check `c` (a registry Check) may never run in lane B, or None."""
    if c.gui:
        return "gui-marked (needs the screen)"
    if c.id.startswith("app."):
        return "an app.* check"
    if c.id in ("client.stock_mode", "client.launch_smoke"):
        return "a real-client check (Phase 1 has no client in lane B)"
    if c.id.startswith("build.") or "baseline" in c.id or c.id.startswith("setup."):
        return "a baseline/build check"
    if c.id.startswith(("pop.", "perf.")):
        return "a population/perf check"
    if c.order >= PROTECT_ORDER or c.id in PROTECTION_IDS or c.id.startswith("backups."):
        return "a player-protection check"
    if c.id.startswith(("lane.", "report.")):
        return "drives lane B itself or reports on the whole run"
    if not (START_ORDER <= c.order < JOIN_ORDER):
        return f"order {c.order} is outside lane B's window [{START_ORDER}, {JOIN_ORDER})"
    return None


def table_problems(table, reg: dict) -> list[str]:
    probs = []
    for i in sorted(table):
        c = reg.get(i)
        if c is None:
            probs.append(f"{i}: not a registered check")
            continue
        why = forbidden(c)
        if why:
            probs.append(f"{i}: {why}")
    return probs


def is_join_point(c) -> bool:
    """Lane A waits for lane B before running `c` (test-pyramid: by run segment; runner.segment)."""
    from .runner import segment
    return segment(c) >= 5 or c.gui or c.id.startswith("app.") or c.order >= PROTECT_ORDER


def lanes_default(tier: str, only) -> int:
    return 2 if (not only and tier == "full") else 1


def b_ids(selected: list, forced: str | None = None) -> list[str]:
    """The selected checks lane B runs (the LANE_B table, or the forced list), in (order, id) order."""
    table = set(LANE_B) if not forced else {x.strip() for x in forced.split(",") if x.strip()}
    return [c.id for c in sorted(selected, key=lambda c: (c.order, c.id)) if c.id in table]


# ---------------------------------------------------------------- memory

def _gb(n_bytes: float) -> float:
    return round(n_bytes / 2**30, 2)


def _swap(text: str) -> dict:
    out = {}
    for k in ("total", "used", "free"):
        m = re.search(rf"{k} = ([\d.]+)([MG])", text)
        if m:
            out[k] = float(m.group(1)) / (1024 if m.group(2) == "M" else 1)
    return out


def real_probe() -> dict:
    """One memory reading: pressure level, available (free + inactive + speculative + purgeable) and swap. A field it
    couldn't read is None (the preflight then refuses: fail closed)."""
    out = {"pressure_level": None, "avail_gb": None, "swap_used_gb": None, "swap_free_gb": None, "at": time.time()}
    try:
        r = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"], capture_output=True,
                           text=True, timeout=10)
        out["pressure_level"] = int(r.stdout.strip()) if r.stdout.strip().isdigit() else None
        r = subprocess.run(["/usr/sbin/sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=10)
        sw = _swap(r.stdout)
        if "used" in sw and "free" in sw:
            out["swap_used_gb"], out["swap_free_gb"] = round(sw["used"], 2), round(sw["free"], 2)
        r = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=10)
        page = re.search(r"page size of (\d+) bytes", r.stdout)
        pages = {k: re.search(rf"^Pages {k}:\s+(\d+)\.", r.stdout, re.M)
                 for k in ("free", "inactive", "speculative", "purgeable")}
        if page and all(pages.values()):
            out["avail_gb"] = _gb(sum(int(m.group(1)) for m in pages.values()) * int(page.group(1)))
            out["pages"] = {k: int(m.group(1)) for k, m in pages.items()} | {"page_size": int(page.group(1))}
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


class Probe:
    """Memory readings: real, or the HS_TEST_LANE_MEMORY stub (`raw`)."""

    def __init__(self, raw: str | None = None):
        self.raw = raw
        self.stub = json.loads(raw) if raw else None
        self.i = 0

    def _stub_reading(self, r: dict) -> dict:
        return {"pressure_level": r.get("pressure_level"), "avail_gb": r.get("avail_gb"),
                "swap_used_gb": r.get("swap_used_gb"), "swap_free_gb": r.get("swap_free_gb"), "at": time.time(),
                "stub": True}

    def preflight(self) -> dict:
        if self.stub is None:
            return real_probe()
        return self._stub_reading(self.stub.get("preflight", self.stub) if "preflight" in self.stub else self.stub)

    def midrun(self) -> dict:
        if self.stub is None:
            return real_probe()
        seq = self.stub.get("midrun") if "midrun" in self.stub else None
        if seq is None:
            r = self.stub.get("preflight", self.stub) if "preflight" in self.stub else self.stub
        else:
            r = seq[min(self.i, len(seq) - 1)] if seq else {}
        self.i += 1
        return self._stub_reading(r)


def preflight_decision(now: dict, start: dict, a_stack_up: bool, battery: dict | None, ignore_power: bool) -> dict:
    """ADR-0011 "Memory preflight" plus power: may lane B start? {"allowed", "reasons", "inputs", "thresholds"}.
    The reading must be taken with lane A's stack up (the scheduler brings it up first); a real reading with A's
    stack down, or one that couldn't be taken, refuses (fail closed)."""
    need = AVAIL_MIN_GB
    th = {"pressure_level": PRESSURE_OK, "avail_min_gb": need, "swap_free_min_gb": SWAP_FREE_MIN_GB,
          "swap_growth_max_gb": SWAP_GROWTH_START_MAX_GB, "battery_min_pct": BATTERY_MIN_PCT,
          "on_battery_power": "refused"}
    reasons = []
    pl, av = now.get("pressure_level"), now.get("avail_gb")
    su, sf, s0 = now.get("swap_used_gb"), now.get("swap_free_gb"), (start or {}).get("swap_used_gb")
    if pl is None or av is None or su is None or sf is None:
        reasons.append(f"memory: the probe couldn't read {[k for k in ('pressure_level', 'avail_gb', 'swap_used_gb', 'swap_free_gb') if now.get(k) is None]} (fail closed)")
    else:
        if pl != PRESSURE_OK:
            reasons.append(f"memory: pressure level {pl} (needs {PRESSURE_OK}, normal)")
        if not a_stack_up and not now.get("stub"):
            reasons.append("memory: measured with lane A's stack down (the ADR-0011 preflight needs it up)")
        if av < need:
            reasons.append(f"memory: {av} GB available (free+inactive+speculative+purgeable) < {need} GB")
        growth = None if s0 is None else round(su - s0, 2)
        if sf < SWAP_FREE_MIN_GB and (growth is None or growth > SWAP_GROWTH_START_MAX_GB):
            reasons.append(f"memory: swap has {sf} GB free (< {SWAP_FREE_MIN_GB} GB) and grew "
                           f"{growth if growth is not None else '?'} GB since the run started "
                           f"(> {SWAP_GROWTH_START_MAX_GB} GB)")
    b = battery or {}
    if ignore_power:
        pass
    elif b.get("on_battery"):
        reasons.append(f"power: on battery power ({b.get('percent')} %); two stacks need the charger")
    elif b.get("percent") is not None and b["percent"] < BATTERY_MIN_PCT:
        reasons.append(f"power: battery at {b['percent']} % (below {BATTERY_MIN_PCT} %) although plugged in")
    return {"allowed": not reasons, "reasons": reasons, "thresholds": th,
            "inputs": {"memory": now, "memory_at_run_start": start, "a_stack_up": a_stack_up, "battery": battery,
                       "ignore_power": ignore_power}}


def midrun_stop(now: dict, start: dict) -> str | None:
    """Why lane B must start no further checks (memory), or None. An unreadable probe stops it (fail closed)."""
    pl, su, s0 = now.get("pressure_level"), now.get("swap_used_gb"), (start or {}).get("swap_used_gb")
    if pl is None or su is None:
        return "memory: the probe couldn't read the pressure level or swap (fail closed)"
    if pl >= MIDRUN_PRESSURE_STOP:
        return f"memory: pressure level {pl} (>= {MIDRUN_PRESSURE_STOP}, warn)"
    if s0 is not None and su - s0 > MIDRUN_SWAP_GROWTH_MAX_GB:
        return f"memory: swap grew {round(su - s0, 2)} GB since the run started (> {MIDRUN_SWAP_GROWTH_MAX_GB} GB)"
    return None


# ---------------------------------------------------------------- serial reference (report.lanes)

def walls_file() -> Path:
    return P.OUT / "full-run-walls.json"


def record_full_run_wall(meta: dict, path: Path | None = None) -> None:
    """Keep the last unfiltered full run's wall time per lane mode (report.lanes compares with the serial one)."""
    path = path or walls_file()
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        d = {}
    mode = str((meta.get("lanes") or {}).get("mode") or 1)
    d[mode] = {"wall_s": meta.get("wall_s"), "started": meta.get("started"), "repo_sha": meta.get("repo_sha")}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1) + "\n")
    tmp.replace(path)


def serial_reference(path: Path | None = None, last_full: Path | None = None) -> dict | None:
    """The last serial (one-lane) unfiltered full run: from full-run-walls.json, else the kept last full run when that
    one was serial (no meta.lanes, or mode 1)."""
    try:
        d = json.loads((path or walls_file()).read_text())
        if isinstance(d.get("1"), dict) and d["1"].get("wall_s"):
            return d["1"] | {"source": str((path or walls_file()).relative_to(P.ROOT))}
    except (OSError, ValueError):
        pass
    from .runner import LAST_FULL_RUN
    f = last_full or (LAST_FULL_RUN / "results.json")
    try:
        m = json.loads(f.read_text())["meta"]
    except (OSError, ValueError, KeyError):
        return None
    if (m.get("lanes") or {}).get("mode", 1) == 1 and m.get("wall_s"):
        return {"wall_s": m["wall_s"], "started": m.get("started"), "repo_sha": m.get("repo_sha"),
                "source": str(f.relative_to(P.ROOT)) if P.ROOT in f.resolve().parents else str(f)}
    return None


# ---------------------------------------------------------------- the scheduler (lane A side)

def _remove_stale_worker_files(run_dir: Path) -> None:
    """Spec/state files left by workers of runs that no longer exist (a killed run's)."""
    from . import procs
    for f in run_dir.glob("lane-worker-*.json"):
        rid = f.name[len("lane-worker-"):].split(".")[0]
        pid, _, st = rid.partition("-")
        if pid.isdigit() and procs.start_time(int(pid)) is not None and f"{pid}-{procs.start_time(int(pid))}" == rid:
            continue
        f.unlink(missing_ok=True)


class Worker:
    """The lane-B worker process and its state file."""

    def __init__(self, ids: list[str], spec: dict, timeout: float, say):
        self.ids, self.timeout, self.say = ids, timeout, say
        B = P.layout("b")
        B["run"].mkdir(parents=True, exist_ok=True)
        B["logs"].mkdir(parents=True, exist_ok=True)
        rid = os.environ.get("HS_HARNESS_RUN_ID", str(os.getpid()))
        self.spec_path = B["run"] / f"lane-worker-{rid}.json"
        self.state_path = B["run"] / f"lane-worker-{rid}.state.json"
        self.log_path = B["logs"] / "lane-worker.log"
        self.state_path.unlink(missing_ok=True)
        _remove_stale_worker_files(B["run"])
        self.spec = spec | {"ids": ids, "state": str(self.state_path), "parent_pid": os.getpid()}
        self.spec_path.write_text(json.dumps(self.spec, indent=1))
        env = {k: v for k, v in os.environ.items() if k not in TEST_ENV}
        env |= {P.LANE_ENV: "b", "PYTHONPATH": str(P.HARNESS) + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")}
        self.t0 = time.time()
        # own session = own process group: a timeout kills the whole group (its ./play, stand-ins, nested hs)
        self.proc = subprocess.Popen([sys.executable, "-m", "hslib.lane", str(self.spec_path)], cwd=P.ROOT, env=env,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
        self.timed_out = False
        self.killed = None

    def state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def stop(self, why: str) -> dict:
        """Kill the worker's process group, then stop lane B (`hs lane down`)."""
        out = {"why": why, "pid": self.proc.pid}
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                out["still_running"] = True
        from . import runreg
        out["lane_down"] = runreg.lane_down("b")
        self.killed = out
        return out

    def remove_files(self) -> None:
        for f in (self.spec_path, self.state_path):
            f.unlink(missing_ok=True)

    def join(self) -> dict:
        t_wait = time.time()
        deadline = self.t0 + self.timeout
        while self.proc.poll() is None and time.time() < deadline:
            time.sleep(JOIN_POLL_S)
        if self.proc.poll() is None:
            self.timed_out = True
            self.say(f"lane B: the worker exceeded its {self.timeout:.0f} s timeout; killing it and stopping lane B")
            self.stop(f"timeout after {self.timeout:.0f} s")
        st = self.state()
        return {"rc": self.proc.returncode, "waited_s": round(time.time() - t_wait, 1), "state": st,
                "timed_out": self.timed_out, "killed": self.killed}


def lane_b_running() -> bool:
    from . import launch, stack
    return any(stack.stack_processes("b").values()) or bool(launch.prefix_processes(prefix=P.layout("b")["wineprefix"]))


def schedule(selected: list, ctx: dict, guard, *, requested: int | None, tier: str, only, switches: dict, battery,
             battery_seq: str | None, ignore_power: bool, mem_start: dict, say, skipped: dict | None = None
             ) -> tuple[list[dict], dict]:
    """Run the acceptance checks in one or two lanes. Returns (records in run order, meta.lanes).
    test-pyramid: the barriers follow the run order (runner.run_key), not the order numbers: lane B starts when lane A
    reaches the shared-server segment (its stack is up) and joins before the first check that is_join_point (warm
    journeys, `hs down`, first_play, gate, gui/app checks, run guards)."""
    from . import stack
    from .runner import execute, run_key, segment
    skipped = skipped or {}
    n = requested or lanes_default(tier, only)
    meta = {"requested": requested, "default": lanes_default(tier, only),
            "default_rule": "2 for unfiltered full runs, else 1 (`--lanes` overrides)", "lanes": n, "mode": 1,
            "table": sorted(LANE_B), "forced": switches.get(FORCE_ENV), "barriers": {
                "start": "the first shared-server check (run order)",
                "join_before": "warm journeys, hs down, first_play, gate, gui, app.*, run guards"},
            "assignment": {"b_planned": [], "b_ran": [], "b_to_a": []}, "preflight": None, "fallback": None,
            "fallback_kind": None,     # preflight | midrun | timeout | worker_failed (the Phase 2 gate counts these)
            "b": None}
    seq = sorted(selected, key=run_key)

    def ex(checks):
        return execute(checks, ctx, guard=guard, skipped=skipped)
    if n == 1:
        meta["fallback"] = "--lanes 1" if requested == 1 else "one lane by default for this run"
        return _merge(ex(seq)), meta
    forced = switches.get(FORCE_ENV)
    planned = [i for i in b_ids(selected, forced) if i not in skipped]
    from .registry import REGISTRY
    bad = table_problems(planned, REGISTRY)
    if bad:
        meta["fallback"] = f"lane-B set refused: {bad}"
        planned = []
    meta["assignment"]["b_planned"] = planned
    if not planned:
        meta["fallback"] = meta["fallback"] or "no lane-B check selected"
        return _merge(ex(seq)), meta
    by_id = {c.id: c for c in selected}
    a_seq = [c for c in seq if c.id not in planned]
    meta["timeline_a"] = ctx.setdefault("lane_timeline", {})     # when each lane-A check ran (lane.merge)
    results: list[dict] = []
    start = next((k for k, c in enumerate(a_seq) if segment(c) >= 2), len(a_seq))
    # 1. before the start barrier
    results += ex(a_seq[:start])
    if ctx.get("power_stop"):
        meta["fallback"] = "the power guard stopped the run before lane B started"
        return _merge(results), meta
    # 2. preflight, measured with lane A's stack up (ADR-0011): a real probe brings it up first if it's down
    probe = Probe(switches.get(MEMORY_ENV))
    if probe.stub is None and not all(stack.stack_processes("a").values()):
        from .runner import _ensure_stack
        try:
            t_up = time.time()
            _ensure_stack("up", ctx)
            meta["a_stack_up_for_preflight_s"] = round(time.time() - t_up, 1)
        except Exception as e:  # noqa: BLE001  (the preflight then refuses: A's stack is down)
            meta["a_stack_up_error"] = f"{type(e).__name__}: {e}"[:500]
    a_up = all(stack.stack_processes("a").values())
    batt = battery.read() if battery is not None else None
    pf = preflight_decision(probe.preflight(), mem_start, a_up, batt, ignore_power)
    meta["preflight"] = pf
    worker = None
    if pf["allowed"]:
        try:
            from . import lanestack
            meta["ensure_ready"] = lanestack.ensure_ready()
        except Exception as e:  # noqa: BLE001  (no lane B: serial, with the reason)
            meta["fallback"] = f"lane B isn't ready: {type(e).__name__}: {e}"[:500]
            meta["fallback_kind"] = "not_ready"
        if not meta["fallback"]:
            timeout = float(switches.get(TIMEOUT_ENV) or WORKER_TIMEOUT_S)
            spec = {"tier": tier, "force_clean": bool(ctx.get("force_clean")), "memory_stub": switches.get(MEMORY_ENV),
                    "memory_at_run_start": mem_start, "battery_seq": battery_seq,
                    "ignore_power": ignore_power, "forced": forced}
            worker = Worker(planned, spec, timeout, say)
            meta["mode"] = 2
            meta["b"] = {"started": worker.t0, "pid": worker.proc.pid, "timeout_s": timeout,
                         "log": str(worker.log_path.relative_to(P.ROOT))}
            # release.player_data_untouched: when lane B ran (it must not overlap the release bracket)
            ctx["lane_b_window"] = {"started": worker.t0, "pid": worker.proc.pid}
            say(f"lane B: started a worker (pid {worker.proc.pid}) for {len(planned)} check(s): {', '.join(planned)}")
    else:
        meta["fallback"] = "preflight: " + "; ".join(pf["reasons"])
        meta["fallback_kind"] = "preflight"
        say(f"lane B not used ({meta['fallback']}); running every check in lane A")
    if worker is None:
        meta["assignment"]["b_to_a"] = list(planned)          # every planned lane-B check runs in lane A
        # serial from here on, B's checks included. The stack-down ones (the play.* stand-in sessions) run once the
        # shared segment's own `hs down` (lifecycle.down) has stopped the stack, not inside the shared segment (each
        # would stop the shared stack, and the next shared test start it again). Records are merged in run order.
        rest = [c for c in seq if c not in a_seq[:start]]
        cold_b = [c for c in rest if c.id in planned and c.stack == "down" and segment(c) < 2]
        others = [c for c in rest if c not in cold_b]
        k = next((i for i, c in enumerate(others) if segment(c) > 6), len(others))
        meta["cold_b_after_down"] = [c.id for c in cold_b]
        results += ex(others[:k])
        if cold_b and not ctx.get("power_stop"):
            results += ex(cold_b)
        if not ctx.get("power_stop"):
            results += ex(others[k:])
        return _merge(results), meta
    # 3. lane A up to the join barrier, with B running
    rest = a_seq[start:]
    jk = next((k for k, c in enumerate(rest) if is_join_point(c)), len(rest))
    join_c = rest[jk] if jk < len(rest) else None
    pre_join, post_join = rest[:jk], rest[jk:]
    results += ex(pre_join)
    if ctx.get("power_stop"):
        meta["b"] |= {"stopped_by_power_guard": worker.stop("the power guard stopped the run")}
    # 4. join
    say("lane B: joining (waiting for the worker)")
    j = worker.join()
    st = j["state"]
    worker.remove_files()
    b_recs = [r | {"lane": "b"} for r in st.get("records") or [] if r.get("id") in planned]
    ran = [r["id"] for r in b_recs]
    left = [i for i in planned if i not in ran]
    t_join = time.time()
    meta["b"] |= {"finished": st.get("finished"), "joined": t_join, "rc": j["rc"], "timed_out": j["timed_out"],
                  "killed": j["killed"], "join_waited_s": j["waited_s"],
                  "wall_s": round((st.get("finished") or t_join) - worker.t0, 1),
                  "prepare": st.get("prepare"), "down": st.get("down"), "stop": st.get("stop"),
                  "error": st.get("error"), "timeline": st.get("timeline") or {}}
    meta["assignment"] |= {"b_ran": ran, "b_to_a": left}
    ctx["lane_b_window"] |= {"finished": st.get("finished") or t_join, "ran": ran}
    if left and not meta["fallback"]:
        if st.get("stop"):
            meta["fallback"] = f"mid-run: {st['stop'].get('reason')}; {len(left)} lane-B check(s) ran in lane A"
            meta["fallback_kind"] = "midrun"
        elif j["timed_out"]:
            meta["fallback"] = f"worker timed out; {len(left)} lane-B check(s) ran in lane A"
            meta["fallback_kind"] = "timeout"
        else:
            meta["fallback_kind"] = "worker_failed"
            meta["fallback"] = (f"worker failed (rc {j['rc']}{', ' + str(st.get('error'))[:200] if st.get('error') else ''}); "
                                f"{len(left)} lane-B check(s) ran in lane A")
    if j["rc"] != 0 and not j["timed_out"] and lane_b_running():
        meta["b"]["lane_down_after_failure"] = worker.stop(f"worker exited {j['rc']} with lane B running")
    meta["barriers"] |= {"joined_before": None if join_c is None else join_c.id, "joined_at": t_join}
    results += b_recs
    if ctx.get("power_stop"):
        return _merge(results), meta
    # 5. lane B's leftovers in lane A, then the rest
    if left:
        say(f"lane B: running its {len(left)} unfinished check(s) in lane A: {', '.join(left)}")
        results += ex([by_id[i] for i in left])
        if ctx.get("power_stop"):
            return _merge(results), meta
    results += ex(post_join)
    return _merge(results), meta


def _merge(records: list[dict]) -> list[dict]:
    """Records in run order (runner.run_key), children right after their session."""
    from .registry import CHILDREN, REGISTRY
    from .runner import run_key

    def k(r):
        i = r["id"]
        c = REGISTRY.get(i) or REGISTRY.get(CHILDREN.get(i, ""))
        return (run_key(c), 0 if i in REGISTRY else 1, i) if c else ((99,), 0, i)
    # stable: a session's children keep the order the session recorded them in
    return sorted(records, key=k)


def summary_line(meta: dict) -> str:
    ml = meta.get("lanes") or {}
    if not ml:
        return ""
    if ml.get("mode") == 2:
        b = ml.get("b") or {}
        a = ml.get("assignment") or {}
        return (f"- Lanes: 2 (lane B ran {len(a.get('b_ran') or [])} check(s) in {b.get('wall_s')} s"
                + (f"; FALLBACK ({ml.get('fallback_kind')}): {len(a.get('b_to_a') or [])} moved to lane A: "
                   f"{ml.get('fallback')}" if a.get("b_to_a") else "")
                + ")")
    return (f"- Lanes: 1 ({'FALLBACK (' + ml['fallback_kind'] + '): ' if ml.get('fallback_kind') else ''}"
            f"{ml.get('fallback')})")
