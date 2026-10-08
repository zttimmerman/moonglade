"""Power guard (openspec/changes/power-guard, F34): cap build parallelism and pause or stop heavy work on low battery.

F34 (2026-10-07): the stock 67 W charger lost to sustained full-core load on the M2 Pro; the battery drained from
100 % to 0 while plugged in and the Mac shut down. So:

- `default_jobs()`: a build's default job count is the performance-core count (`sysctl hw.perflevel0.physicalcpu`), or
  half of `hw.ncpu` (at least 2) when that is missing, never more than the total core count. `--jobs N` overrides.
- `Guard.check(step)`: called before each build step, each bootstrap step and each check of a full/slow/gui run.
  Below PAUSE_PCT (plugged in or not) it pauses with one plain line, polls every POLL_S, resumes at RESUME_PCT, and
  raises PowerStop at STOP_PCT or lower. An unknown reading (desktop Macs) never pauses. `ignore` (--ignore-power)
  skips it and says so. Every pause is recorded (start, end, percents).

Test switches (TEST_ENV; refused outside a nested harness run by `refusal()`, ./play and the CLI):
  HS_TEST_POWER_CORES      JSON {"perf": int|null, "total": int}: replaces the core probe
  HS_TEST_BATTERY_SEQ      comma list like `40p,34,50` (`p` = plugged in, `none` = no battery): the battery readings
                           in order, the last one repeating; the poll interval is 0 under it
  HS_TEST_POWER_PLAN_ONLY  a scratch directory: `hs build` and `hs bootstrap` compute and log their plan there
                           (guard included) without building anything
"""
from __future__ import annotations

import json
import os
import subprocess
import time

PAUSE_PCT = 35
RESUME_PCT = 50
STOP_PCT = 15
POLL_S = 60.0
CORES_ENV = "HS_TEST_POWER_CORES"
BATTERY_ENV = "HS_TEST_BATTERY_SEQ"
PLAN_ENV = "HS_TEST_POWER_PLAN_ONLY"
TEST_ENV = (CORES_ENV, BATTERY_ENV, PLAN_ENV)
STOP_EXIT = 3                     # exit code of work the guard stopped


class PowerStop(RuntimeError):
    def __init__(self, msg: str, record: dict | None = None):
        super().__init__(msg)
        self.record = record or {}


class JobsError(ValueError):
    pass


# ---------------------------------------------------------------- test switches

def set_switches(environ=None) -> list[str]:
    env = os.environ if environ is None else environ
    return [v for v in TEST_ENV if env.get(v)]


def refusal(cmd: str, allow_flag: bool = True, environ=None) -> str | None:
    """Why this entry point must refuse the power-guard test switches, or None. They are allowed only inside a
    harness run (HS_HARNESS_RUN=1) for `hs run`, `hs build` and `hs bootstrap` (the last with --allow-test-inject,
    `allow_flag`). Every other command, ./play and the app refuse them always."""
    env = os.environ if environ is None else environ
    on = set_switches(env)
    if not on:
        return None
    if cmd in ("run", "build", "bootstrap") and env.get("HS_HARNESS_RUN") == "1" and allow_flag:
        return None
    return (f"${on[0]} is set: that's a test switch of the harness's power guard, not for normal use. "
            f"Unset it (`unset {' '.join(on)}`) and run it again.")


def plan_dir() -> str | None:
    """The plan-only scratch directory, or None when plan-only is off. Raises JobsError on a value that isn't one."""
    v = os.environ.get(PLAN_ENV)
    if not v:
        return None
    if not os.path.isdir(v):
        raise JobsError(f"${PLAN_ENV} must name an existing scratch directory (got {v!r})")
    return v


# ---------------------------------------------------------------- job count

def core_probe() -> dict:
    """{"perf": performance cores or None, "total": all cores or None}; stubbed by $HS_TEST_POWER_CORES."""
    raw = os.environ.get(CORES_ENV)
    if raw:
        d = json.loads(raw)
        return {"perf": d.get("perf"), "total": d.get("total"), "stub": True}

    def sysctl(name):
        try:
            r = subprocess.run(["/usr/sbin/sysctl", "-n", name], capture_output=True, text=True, timeout=10)
            return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
    return {"perf": sysctl("hw.perflevel0.physicalcpu"), "total": sysctl("hw.ncpu")}


def default_jobs(probe: dict | None = None) -> tuple[int, str]:
    """(job count, where it came from) for a build with no --jobs."""
    p = probe if probe is not None else core_probe()
    perf, total = p.get("perf"), p.get("total")
    total = total if isinstance(total, int) and total > 0 else None
    if isinstance(perf, int) and perf > 0:
        n, why = perf, f"{perf} performance cores" + (f" of {total} cores" if total else "")
    elif total:
        n, why = max(2, total // 2), f"half of {total} cores (no performance-core count), at least 2"
    else:
        n, why = 2, "no core count available"
    if total and n > total:
        n, why = total, why + f", capped at the {total} cores this Mac has"
    return n, why


def resolve_jobs(jobs: int | None, probe: dict | None = None) -> tuple[int, str]:
    """--jobs N (refused below 1) or the default."""
    if jobs is not None:
        if jobs < 1:
            raise JobsError(f"--jobs must be 1 or more (got {jobs}); leave it out to use this Mac's performance cores")
        return jobs, f"--jobs {jobs}"
    return default_jobs(probe)


def jobs_line(n: int, why: str) -> str:
    return f"build jobs: {n} ({why})"


# ---------------------------------------------------------------- battery

def _parse_seq(raw: str) -> list[dict]:
    out = []
    for tok in raw.split(","):
        tok = tok.strip().lower()
        if not tok:
            continue
        if tok in ("none", "?", "unknown"):
            out.append({"on_battery": None, "percent": None})
            continue
        plugged = tok.endswith("p")
        out.append({"on_battery": not plugged, "percent": int(tok.rstrip("p"))})
    return out


class Battery:
    """Battery readings: the real `pmset -g batt` (guienv.battery), or the stubbed $HS_TEST_BATTERY_SEQ."""

    def __init__(self, seq: str | None = None):
        raw = seq if seq is not None else os.environ.get(BATTERY_ENV)
        self.seq = _parse_seq(raw) if raw else None
        self.i = 0

    @property
    def stub(self) -> bool:
        return self.seq is not None

    def read(self) -> dict:
        if self.seq is not None:
            if not self.seq:
                return {"on_battery": None, "percent": None}
            r = self.seq[min(self.i, len(self.seq) - 1)]
            self.i += 1
            return dict(r)
        from .guienv import battery
        try:
            return battery()
        except Exception:  # noqa: BLE001  (no reading counts as unknown: desktop Macs never pause)
            return {"on_battery": None, "percent": None}


def pause_line(pct: int) -> str:
    return (f"Paused: battery at {pct} %. Moonglade will continue at {RESUME_PCT} %; plug in a stronger charger if "
            "this keeps happening.")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class Guard:
    """Battery guard for one piece of heavy work. `what` names it and `resume` says how to start it again (both go
    into the stop message). `say` prints a line for the person; `sink` (optional) also writes it to a log file."""

    def __init__(self, what: str, resume: str, ignore: bool = False, say=None, sink=None,
                 battery: Battery | None = None, poll_s: float | None = None):
        self.what, self.resume, self.ignore = what, resume, ignore
        self.say = say or (lambda m: print(m, flush=True))
        self.sink = sink
        self.battery = battery or Battery()
        self.poll_s = poll_s if poll_s is not None else (0.0 if self.battery.stub else POLL_S)
        self.pauses: list[dict] = []
        self.readings: list[dict] = []
        self.stopped: dict | None = None
        self.skip_logged = False

    def _out(self, msg: str) -> None:
        self.say(msg)
        if self.sink:
            self.sink(msg)

    def _read(self, step: str) -> dict:
        r = self.battery.read()
        self.readings.append({"step": step, "at": _now(), **r})
        return r

    def check(self, step: str) -> dict | None:
        """Before `step`: return at once when the battery is fine (or unknown, or the guard is ignored); else pause
        until RESUME_PCT and return the pause record; raise PowerStop at STOP_PCT or lower."""
        if self.ignore:
            if not self.skip_logged:
                self._out("Battery guard skipped (--ignore-power): heavy work won't pause on low battery.")
                self.skip_logged = True
            return None
        r = self._read(step)
        pct = r.get("percent")
        if pct is None or pct >= PAUSE_PCT:
            return None
        t0 = time.time()
        rec = {"step": step, "start": _now(), "start_pct": pct, "plugged_in": r.get("on_battery") is False,
               "percents": [pct], "end": None, "end_pct": None, "paused_s": None, "stopped": False}
        self.pauses.append(rec)
        self._out(pause_line(pct))
        while True:
            if pct is not None and pct <= STOP_PCT:
                rec |= {"end": _now(), "end_pct": pct, "paused_s": round(time.time() - t0, 1), "stopped": True}
                self.stopped = {"step": step, "percent": pct, "at": rec["end"]}
                msg = (f"Stopped: {self.what} stopped before {step} because the battery fell to {pct} % while paused "
                       f"(Moonglade stops at {STOP_PCT} % so the Mac doesn't shut down). Plug in a stronger charger, "
                       f"let the battery charge above {RESUME_PCT} %, then run {self.resume} again; it continues from "
                       "where it stopped.")
                self._out(msg)
                raise PowerStop(msg, rec)
            if pct is not None and pct >= RESUME_PCT:
                rec |= {"end": _now(), "end_pct": pct, "paused_s": round(time.time() - t0, 1)}
                self._out(f"Battery at {pct} %: continuing ({step}).")
                return rec
            if self.battery.stub and self.battery.i > len(self.battery.seq) + 2:
                # a stubbed sequence that ended mid-pause would otherwise loop forever at poll interval 0
                raise RuntimeError(f"${BATTERY_ENV} ended while paused at {pct} % (test sequence too short)")
            if self.poll_s:
                time.sleep(self.poll_s)
            r = self._read(step)
            pct = r.get("percent")
            if pct is None:          # the reading went away (e.g. pmset failed): unknown never keeps us paused
                rec |= {"end": _now(), "end_pct": None, "paused_s": round(time.time() - t0, 1)}
                self._out(f"Battery reading unavailable: continuing ({step}).")
                return rec
            rec["percents"].append(pct)

    def paused_s(self) -> float:
        return round(sum(p.get("paused_s") or 0.0 for p in self.pauses), 1)

    def record(self) -> dict:
        return {"ignored": self.ignore, "stub": self.battery.stub, "pause_below_pct": PAUSE_PCT,
                "resume_at_pct": RESUME_PCT, "stop_at_pct": STOP_PCT, "pauses": self.pauses,
                "paused_s": self.paused_s(), "stopped": self.stopped, "readings": len(self.readings)}
