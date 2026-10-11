"""harness-reporting "Leftover-process detection only counts our processes" (F24, openspec/changes/harness-speed).

launch.prefix_processes() used to count any process with a file open under the Wine prefix as ours; macOS's
StorageManagementService once held one and failed client.launch_smoke. It now counts only processes of the pinned
Wine runtime (or their descendants). This check plants a stand-in for such a system process (/bin/sleep holding
the prefix's system.reg open; not a child of Wine) next to a real Wine process of the runtime, and asks
prefix_processes() which of them are ours.
"""
from __future__ import annotations

import subprocess
import time

from .. import launch, paths as P, procs
from ..registry import check, require

WINE_WAIT_S = 90          # a cold Wine start under Rosetta is normally 3-10 s; generous so a busy Mac isn't a FAIL


def _is_windows_ping(p: dict) -> bool:
    """The Windows ping.exe itself (Wine rewrites its argv to the Windows path), not the `wine cmd /c ...` launcher."""
    c = p["cmd"].lower()
    return c.startswith("c:\\") and "ping" in c


def _alive(p: subprocess.Popen | None) -> bool:
    return bool(p) and p.poll() is None


# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "modules/mod-moonglade/src/MoongladeQuiet.cpp",
    "modules/mod-moonglade/conf/mod_moonglade.conf.dist#Moonglade.QuietServerText",
    "harness/hslib/checks/scope_checks.py",
)


@check("harness.prefix_scope", order=228, capability="harness-reporting", stack="any", layer="integration")
def prefix_scope(ctx):
    """An unrelated process (stand-in for StorageManagementService) holding a file open under env/wineprefix-mtld3d
    isn't reported by prefix_processes(), while a real Wine process from the pinned runtime is."""
    held = P.WINEPREFIX / "system.reg"
    ev: dict = {"prefix": str(P.WINEPREFIX), "runtime_roots": list(launch._runtime_roots()), "held_file": str(held)}
    before = launch.prefix_processes()
    ev["given_prefix_idle"] = before
    require(not before, "GIVEN not met: processes of this prefix are already running", **ev)
    require(held.is_file(), "GIVEN not met: the prefix has no system.reg to hold open", **ev)
    stand = wine = None
    t_start = time.time()
    try:
        # stand-in: a shell that opens the file read-only on fd 3, then execs /bin/sleep (exe outside the runtime,
        # parent = this harness process, so neither runtime_exe nor runtime_descendant)
        stand = subprocess.Popen(["/bin/sh", "-c", 'exec 3< "$1"; exec /bin/sleep 300', "sh", str(held)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        # a real Windows process under the runtime, in this prefix (wine_env: WINEPREFIX, msync, layer env)
        wine = subprocess.Popen([str(launch.WINE), "cmd", "/c", "ping -n 120 127.0.0.1 >nul"], env=launch.wine_env(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        ev["stand_in"] = {"pid": stand.pid}
        ev["wine_launcher_pid"] = wine.pid
        snap, excluded, t0 = [], [], time.time()
        while time.time() - t0 < WINE_WAIT_S:
            excluded = []
            snap = launch.prefix_processes(excluded=excluded)
            stand_seen = any(e["pid"] == stand.pid for e in excluded) or any(p["pid"] == stand.pid for p in snap)
            if stand_seen and any(_is_windows_ping(p) for p in snap):
                break
            time.sleep(1)
        ev["waited_s"] = round(time.time() - t0, 1)
        ev["stand_in"] |= {"alive": _alive(stand), "exe": procs.path(stand.pid), "ppid": procs.ppid(stand.pid),
                           "started_after_run_start": True}
        ev["reported"] = snap
        ev["excluded_candidates"] = excluded
        stand_rep = [p for p in snap if p["pid"] == stand.pid]
        stand_exc = [e for e in excluded if e["pid"] == stand.pid]
        wine_rep = [p for p in snap if p.get("scope") in ("runtime_exe", "runtime_descendant")]
        ping_rep = [p for p in wine_rep if _is_windows_ping(p)]
        # the unscoped rule (every candidate, F24's bug) would have counted the stand-in: the GIVEN really holds
        ev["unscoped_would_count"] = sorted({p["pid"] for p in snap} | {e["pid"] for e in excluded})
        ev["stand_in_reported"] = stand_rep
        ev["stand_in_excluded_as"] = stand_exc
        ev["wine_processes_reported"] = [{k: p[k] for k in ("pid", "cmd", "via", "scope")} for p in wine_rep]
        require(_alive(stand), "the stand-in exited early", **ev)
        cand = stand_exc + stand_rep
        require(cand and "server_dir" in cand[0]["via"],
                "GIVEN not met: the stand-in wasn't a candidate (its open file under the prefix wasn't seen)", **ev)
        require(not stand_rep, "prefix_processes() reported the unrelated stand-in process as ours", **ev)
        require(ping_rep, f"no real Windows process (PING.EXE under Wine) of the runtime was reported within "
                f"{WINE_WAIT_S} s", **ev)
        require(all((procs.path(p["pid"]) or "").startswith(tuple(ev["runtime_roots"])) for p in ping_rep),
                "the reported Wine process doesn't run from the pinned runtime", **ev)
    finally:
        if wine is not None:
            ev["stop_prefix"] = launch.stop_prefix()
            try:
                wine.wait(timeout=30)
            except subprocess.TimeoutExpired:
                wine.kill()
        if stand is not None and stand.poll() is None:
            stand.kill()
            stand.wait(timeout=10)
        ev["prefix_after"] = launch.prefix_processes()
        ev["run_s"] = round(time.time() - t_start, 1)
    require(not ev["prefix_after"], "processes of this prefix remain after wineserver -k", **ev)
    return ev
