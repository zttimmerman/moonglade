"""Loading stages of the play loop (openspec/changes/launcher-polish, task 1.1; design.md "Stages").

`./play` prints one structured line per stage event next to its human `[play]` lines:

    [stage] {"id": "world", "label": "Loading the world", "state": "progress", "fraction": 0.42, "overall": 0.31, ...}

ids, in order (the contract; labels are what Zach reads):
    prepare     Getting ready                lock, which client, which world
    database    Starting the database        mysqld
    login       Starting the login server    authserver
    world       Loading the world            worldserver; fraction = worldserver.out lines / the last good boot's count
    bots        AI players: N / M online     informational: the DB's online random bots vs the target; never waits
    account     Checking your account        the login account exists
    game_setup  Preparing the game           client config, Wine prefix, binaries match the manifest
    launching   Launching the game           client started, until its first window is on screen
    ready       Ready                        the game window is up (the launcher hands focus to it)

state: start | progress | done | skipped | failed. database/login/world are `skipped` when the server is already
running. `overall` is the whole bar (0..1, never decreasing): the sum of finished stages' weights plus the current
stage's weight x its fraction. Weights are the last run's measured stage durations (env/run/boot-calibration.json),
defaults below until a run has finished. Each event also carries `t` (epoch seconds) and the stage's `slot`
([lo, hi] of overall) so the launcher can creep within an indeterminate stage without ever passing it.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from . import paths as P

STAGES = [
    ("prepare", "Getting ready"),
    ("database", "Starting the database"),
    ("login", "Starting the login server"),
    ("world", "Loading the world"),
    ("bots", "Waking the AI players"),
    ("account", "Checking your account"),
    ("game_setup", "Preparing the game"),
    ("launching", "Launching the game"),
    ("ready", "Ready"),
]
IDS = [s for s, _ in STAGES]
LABELS = dict(STAGES)
STATES = ("start", "progress", "done", "skipped", "failed")
# Expected seconds per stage before any run has been measured (spike table: world ~16 s warm, client ~4 s to its
# process plus window creation; the rest are small). Only the ratios matter.
DEFAULT_SECONDS = {"prepare": 1.0, "database": 4.0, "login": 2.0, "world": 18.0, "bots": 0.5, "account": 1.0,
                   "game_setup": 2.0, "launching": 6.0, "ready": 0.0}
# worldserver.out line count at readiness before any boot has been measured (spike: "World Initialized" at line 878
# of the 2026-10-04 log); replaced by the measured count of the last good boot.
DEFAULT_WORLD_LINES = 880
CALIBRATION = P.RUN / "boot-calibration.json"
WORLD_POLL_S = 0.5
PREFIX = "[stage] "

_out_lock = threading.Lock()


def write_line(line: str) -> None:
    """One whole line to stdout in a single write (the world watcher thread prints too)."""
    with _out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def parse(line: str) -> dict | None:
    """The event of a `[stage] {...}` line, else None."""
    if not line.startswith(PREFIX):
        return None
    try:
        ev = json.loads(line[len(PREFIX):])
    except ValueError:
        return None
    return ev if isinstance(ev, dict) else None


def load_calibration() -> dict:
    try:
        d = json.loads(CALIBRATION.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as f:
            return f.read().count(b"\n")
    except FileNotFoundError:
        return 0


class Stages:
    """Emits stage events and keeps the overall bar monotonic. `emit` receives each finished line."""

    def __init__(self, emit=write_line, profile: str | None = None):
        self.emit = emit
        self.profile = profile
        self.cal = load_calibration()
        secs = dict(DEFAULT_SECONDS)
        for k, v in (self.cal.get("durations") or {}).items():
            if k in secs and isinstance(v, (int, float)) and v >= 0:
                secs[k] = max(float(v), 0.05) if k != "ready" else 0.0
        total = sum(secs.values()) or 1.0
        self.weights = {k: secs[k] / total for k in IDS}
        self.expect_s = secs
        self.state: dict[str, str] = {}
        self.started_at: dict[str, float] = {}
        self.durations: dict[str, float] = {}
        self.current: str | None = None
        self.overall = 0.0
        self.extra: dict = {}            # fields added to every later event (e.g. the bots count)
        self._lock = threading.RLock()
        self._watch: threading.Thread | None = None
        self._watch_stop = threading.Event()
        self.world_lines_of = self._world_lines_of()
        self.events: list[dict] = []

    # ------------------------------------------------------------------ bookkeeping
    def _world_lines_of(self) -> tuple[int, bool]:
        wl = self.cal.get("world_lines") or {}
        n = wl.get(self.profile or "") if isinstance(wl, dict) else None
        return (int(n), True) if isinstance(n, int) and n > 0 else (DEFAULT_WORLD_LINES, False)

    def _slot(self, sid: str) -> list[float]:
        lo = sum(self.weights[k] for k in IDS[: IDS.index(sid)])
        return [round(lo, 4), round(min(lo + self.weights[sid], 1.0), 4)]

    def _send(self, sid: str, state: str, fraction: float | None = None, label: str | None = None, **kw) -> dict:
        with self._lock:
            lo, hi = self._slot(sid)
            if state in ("done", "skipped") or sid == "ready":
                o = hi if sid != "ready" else 1.0
            elif fraction is not None:
                o = lo + (hi - lo) * max(0.0, min(fraction, 1.0))
            else:
                o = lo
            if sid == "prepare" and state == "start":
                o = 0.0
            self.overall = max(self.overall, round(o, 4))
            ev = {"id": sid, "label": label or LABELS[sid], "state": state, "fraction": fraction,
                  "overall": self.overall, "slot": [lo, hi], "expect_s": round(self.expect_s[sid], 1),
                  "t": round(time.time(), 3), **self.extra, **kw}
            self.events.append(ev)
            self.emit(PREFIX + json.dumps(ev, separators=(",", ":")))
            return ev

    # ------------------------------------------------------------------ API
    def start(self, sid: str, label: str | None = None, fraction: float | None = None, **kw) -> dict:
        with self._lock:
            if self.current and self.current != sid and self.state.get(self.current) in ("start", "progress"):
                self.done(self.current)
            self.current = sid
            self.state[sid] = "start"
            self.started_at[sid] = time.time()
            return self._send(sid, "start", fraction if fraction is not None else (1.0 if sid == "ready" else None),
                              label, **kw)

    def progress(self, sid: str, fraction: float | None, label: str | None = None, **kw) -> dict | None:
        with self._lock:
            if self.state.get(sid) not in ("start", "progress"):
                return None
            self.state[sid] = "progress"
            return self._send(sid, "progress", fraction, label, **kw)

    def done(self, sid: str, label: str | None = None, **kw) -> dict | None:
        with self._lock:
            if self.state.get(sid) not in ("start", "progress"):
                return None
            if sid == "world":
                self.stop_world_watch()
            self.state[sid] = "done"
            self.durations[sid] = round(time.time() - self.started_at.get(sid, time.time()), 2)
            if self.current == sid:
                self.current = None
            return self._send(sid, "done", 1.0, label, **kw)

    def skip(self, sid: str, why: str = "already running") -> dict:
        with self._lock:
            self.state[sid] = "skipped"
            return self._send(sid, "skipped", None, None, why=why)

    def fail(self, error: str) -> dict | None:
        """The current stage failed (startup error); the last event names it."""
        with self._lock:
            self.stop_world_watch()
            sid = self.current
            if not sid:
                return None
            self.state[sid] = "failed"
            return self._send(sid, "failed", None, None, error=error[:300])

    def failed_stage(self) -> dict | None:
        sid = next((k for k, v in self.state.items() if v == "failed"), None) or self.current
        return {"id": sid, "label": LABELS[sid]} if sid else None

    # ------------------------------------------------------------------ the stack's callback (stack.up on_stage)
    def on_stack(self, sid: str, state: str, **kw) -> None:
        if state == "start":
            if sid == "world" and kw.get("first_run"):     # DB import: no calibrated progress, can take minutes
                self.first_run_world = True
                self.start(sid, label="Building the world (first run, this takes a while)", first_run=True)
            else:
                self.start(sid)
        elif state == "done":
            if sid == "world":
                self.record_world_lines()
            self.done(sid)
        elif state == "spawned" and sid == "world" and not getattr(self, "first_run_world", False):
            self.start_world_watch(Path(kw["log"]))

    # ------------------------------------------------------------------ world progress from the server log
    def start_world_watch(self, log: Path) -> None:
        self.stop_world_watch()
        self._watch_stop.clear()
        self._world_log = log
        of, calibrated = self.world_lines_of

        def run():
            last = -1
            while not self._watch_stop.wait(WORLD_POLL_S):
                n = count_lines(log)
                if n != last:
                    last = n
                    self.progress("world", round(min(n / of, 0.99), 4), lines=n, of=of, calibrated=calibrated)
        self._watch = threading.Thread(target=run, name="world-progress", daemon=True)
        self._watch.start()

    def stop_world_watch(self) -> None:
        if self._watch and self._watch is not threading.current_thread():
            self._watch_stop.set()
            self._watch.join(timeout=2)
        self._watch = None

    def record_world_lines(self) -> None:
        log = getattr(self, "_world_log", None)
        if log is not None:
            self.world_lines_ready = count_lines(log)

    # ------------------------------------------------------------------ calibration (after a good start)
    def save_calibration(self) -> dict | None:
        """Store this run's stage durations (only stages that really ran) and, after a cold world start, the
        worldserver.out line count at readiness for this profile."""
        cal = load_calibration()
        dur = dict(cal.get("durations") or {})
        dur.update({k: v for k, v in self.durations.items() if self.state.get(k) == "done"})
        cal["durations"] = dur
        n = getattr(self, "world_lines_ready", None)
        if n and self.profile:
            wl = dict(cal.get("world_lines") or {})
            wl[self.profile] = int(n)
            cal["world_lines"] = wl
        cal["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            CALIBRATION.parent.mkdir(parents=True, exist_ok=True)
            CALIBRATION.write_text(json.dumps(cal, indent=2))
        except OSError:
            return None
        return cal
