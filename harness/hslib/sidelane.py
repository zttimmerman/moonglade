"""test-loop-speedups "Full run under 25 minutes" (B36): the side lane.

A run's checks go in two lanes:
- the main lane: every check not listed below, in (order, id) sequence, exactly as before (the stack is brought to
  each check's state, nothing else runs alongside it except the side lane while that is still going);
- the side lane: the checks in SIDE_LANE, in (order, id) sequence, in ONE worker process started when the run's
  checks start (`python3 -m hslib.sidelane <spec.json>`).

Safety model (the spec: "only where they share no exclusive resource: the stack, the client, the screen, or a fixed
port"):
1. A side check touches none of: the stack (mysqld/authserver/worldserver, their ports, the live acore_* schemas),
   Wine / the client processes / the Wine prefix, the screen, any fixed port, the build tree (build/, vendor/,
   server/, baseline/), the player's env/client-play, env/player.json, env/settings.json, env/backups. Each entry
   below says what it does touch.
2. The barrier: the main lane runs only the checks in PRE_BARRIER while the side lane is running; before its first
   check outside that list it waits for the side lane to finish. So no side check ever overlaps a check that starts,
   stops or uses the stack, Wine or the screen. PRE_BARRIER lists what each of those early checks touches.
3. The worker is a separate process (its own os.environ and module globals, so an in-process env tweak in a main
   lane check can't reach it), in its own process group, killed as a group on timeout. It never starts `hs run`:
   `hs run` refuses inside it ($HS_SIDELANE_WORKER), and the worker refuses to start inside another worker.
4. Records are merged and sorted by (order, id), the serial order, so results.json and summary.md are ordered
   exactly as in a serial run. `hs run --serial` runs everything in the main lane (the reference schedule).
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import paths as P

WORKER_ENV = "HS_SIDELANE_WORKER"
WORKER_TIMEOUT_S = 900          # the side lane takes ~45 s; a hung worker is killed (whole group) after this

# id -> what it touches (reviewed; nothing here may use a runtime resource, see the module doc)
SIDE_LANE = {
    "report.tiers_declared": "in-process registry scan; writes nothing",
    "report.slow_tier_rule": "in-process simulation of the runner over fake registries; writes nothing",
    "report.gui_tier_rule": "simulations + scratch git repos in a temp dir; reads the repo's sources",
    "report.backups_tier_rule": "simulations + scratch git repos in a temp dir",
    "repo.naming": "reads tracked files (git ls-files); temp dir for its self-test",
    "repo.rename_script": "sandboxed script on a temp tree; reads the unified log for its own sandbox tag; "
                          "lists ~/.claude/projects names",
    "client.config_gen": "`hs client-config` on a mock client in a temp dir",
    "client.inspect": "reads client/ and its manifest (client/ is OS-locked read-only)",
    "re.ghidra_installed": "`hs ghidra-install` on the installed tree (no download): reads env/ghidra",
    "re.ghidra_readonly": "Ghidra headless on a copy of Wow.exe in env/re (only the re.* checks use env/re)",
    "re.mpq_readonly": "reads client/Data/common.MPQ, extracts into env/re",
    "re.ghidra_refuses_mismatch": "temp dir + a fake download; no network",
    "re.mpq_write_roundtrip": "writes MPQs in a temp dir; a refused write into a scratch stand-in",
    "client.patch0001_recorded": "reads patches/client, client/Wow.exe and docs/adr",
    "client.play_build": "`hs client-build --dest env/check-play-build` (+ check-play-base/-queue/-refuse-*), "
                         "reads client/; no other check uses those dirs",
    "client.play_revert": "`hs client-build --dest env/check-play-revert`, reads client/",
}

# Main-lane checks that may run while the side lane is still going (all early in the order). What each touches:
PRE_BARRIER = {
    "report.gui_preflight": "in-process cases + nested `hs run`s that are decision-only (exit before run())",
    "report.keep_awake": "pmset assertions of its own nested runs (the side worker holds none); nested `hs run "
                         "--only` of an in-process check",
    "report.last_full_run": "stub documents in a temp dir; `hs timing-baseline join` into a scratch baseline",
    "repo.agent_guard_hook": "runs the hook script on fake tool inputs",
    "report.nightly": "stubbed runs, scratch git repos, a scratch LaunchAgents dir",
    "build.pin_enforced": "build tree / vendor checkouts (no side check reads them)",
    "build.clean": "build tree / server install (no side check reads them)",
    "build.clean_smart": "build tree",
    "build.moved_repo": "scratch repo copy under harness/out/setup-scratch",
    "core.patch_applied": "vendor/azerothcore-wotlk with a scratch git index",
    "core.playerbots_patches_applied": "vendor checkouts with a scratch git index",
    "setup.preflight": "./bootstrap in a scratch repo copy (harness/out/setup-scratch) with injected facts",
    "build.downloads_pinned": "fork.lock + a scratch repo copy",
    "repo.portable": "reads tracked files",
}


def split(checks: list, serial: bool) -> tuple[list, list]:
    """(main lane, side lane) for `checks`; serial -> everything in the main lane."""
    if serial or os.environ.get(WORKER_ENV):
        return list(checks), []
    side = [c for c in checks if c.id in SIDE_LANE and c.stack == "any" and not c.gui and not c.backups]
    if len(side) < 2:                      # not worth a worker
        return list(checks), []
    ids = {c.id for c in side}
    return [c for c in checks if c.id not in ids], side


class SideLane:
    """The worker process running the side checks. start() at the beginning; wait() returns {id: record}."""

    def __init__(self, checks: list, say=None):
        self.ids = [c.id for c in checks]
        self.say = say or (lambda _m: None)
        self.dir = Path(tempfile.mkdtemp(prefix="hs-sidelane-"))
        self.out = self.dir / "records.jsonl"
        self.proc = None
        self.t0 = None
        self.result = None
        self.info = {"ids": self.ids}

    def start(self):
        spec = self.dir / "spec.json"
        spec.write_text(json.dumps({"ids": self.ids, "out": str(self.out)}))
        env = dict(os.environ)
        env["PYTHONPATH"] = str(P.HARNESS) + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self.t0 = time.time()
        self.proc = subprocess.Popen([sys.executable, "-m", "hslib.sidelane", str(spec)], cwd=str(P.HARNESS),
                                     env=env, stdin=subprocess.DEVNULL, start_new_session=True)
        self.info |= {"pid": self.proc.pid, "started_at": round(self.t0, 3)}
        return self

    def done(self) -> bool:
        return self.proc is None or self.proc.poll() is not None

    def wait(self, timeout: float = WORKER_TIMEOUT_S) -> dict:
        if self.result is not None:
            return self.result
        t_wait = time.time()
        timed_out = False
        try:
            self.proc.wait(timeout=max(1.0, timeout - (time.time() - self.t0)))
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait()
        recs = {}
        if self.out.exists():
            for line in self.out.read_text().splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                recs[r["id"]] = r
        self.info |= {"exit": self.proc.returncode, "timed_out": timed_out, "wall_s": round(time.time() - self.t0, 2),
                      "main_lane_waited_s": round(time.time() - t_wait, 2)}
        for i in self.ids:                         # a worker that died mid-way: its unfinished checks are errors
            if i not in recs:
                recs[i] = {"id": i, "status": "error", "duration_s": 0.0,
                           "message": f"side lane worker {'timed out' if timed_out else 'exited'} "
                                      f"(exit {self.proc.returncode}) before this check finished",
                           "evidence": {"side_lane": self.info}}
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)
        self.result = recs
        return recs

    def kill(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait()


def worker(spec_path: str) -> int:
    """Run the listed checks in order and append one JSON record per check to the spec's out file."""
    if os.environ.get(WORKER_ENV):
        print("[hs] side lane worker inside another worker: refusing", file=sys.stderr)
        return 2
    if os.environ.get("HS_HARNESS_RUN") != "1":
        print("[hs] the side lane worker runs only under `hs run`", file=sys.stderr)
        return 2
    os.environ[WORKER_ENV] = "1"
    spec = json.loads(Path(spec_path).read_text())
    from .registry import load_all
    from .runner import run_one
    reg = load_all()
    out = Path(spec["out"])
    ctx = {"side_lane": True}
    for i in spec["ids"]:
        c = reg.get(i)
        if c is None or i not in SIDE_LANE:
            rec = {"id": i, "status": "error", "duration_s": 0.0, "message": "not a side-lane check",
                   "evidence": {"side_lane": True}}
        else:
            rec = run_one(c, ctx, ensure=None, lane="side")
        with out.open("a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(worker(sys.argv[1]))
