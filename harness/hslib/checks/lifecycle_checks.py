"""server-lifecycle checks (lifecycle.persist lives with the fixtures)."""
import re
import subprocess
import time

from .. import db, paths as P, soap, stack
from ..registry import check, require

HS = str(P.HARNESS / "hs")


# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "harness/hs",
    "play",
    "harness/hslib/cli.py",
    "harness/hslib/__main__.py",
    "harness/hslib/stack.py",
    "harness/hslib/checks/lifecycle_checks.py",
)


@check("lifecycle.failfast", order=30, capability="server-lifecycle", stack="down", layer="integration")
def failfast(ctx):
    """With an unusable worldserver DataDir, `hs up` fails fast with a log excerpt and leaves no worldserver."""
    t0 = time.time()
    r = subprocess.run([HS, "up", "--world-datadir", "/nonexistent/hs-failfast", "--timeout", "600"],
                       capture_output=True, text=True, timeout=900)
    dt = time.time() - t0
    procs = stack.stack_processes()
    ev = {"cmd": f"{HS} up --world-datadir /nonexistent/hs-failfast --timeout 600", "exit": r.returncode,
          "elapsed_s": round(dt, 1), "stderr_tail": r.stderr[-2500:], "worldserver_pids_after": procs["worldserver"]}
    subprocess.run([HS, "down"], capture_output=True, text=True)
    require(r.returncode != 0, "hs up succeeded with a broken DataDir", **ev)
    require(dt < 300, "did not fail fast (>=300s with a 600s timeout)", **ev)
    excerpt = r.stderr.split("log excerpt", 1)[-1] if "log excerpt" in r.stderr else ""
    # the cause worldserver reports for a missing DataDir (World.cpp, LOG_ERROR "server.loading")
    require("Failed to find map files" in excerpt, "log excerpt does not show the cause", **ev)
    require("rc=" in r.stderr, "failure message lacks the worldserver exit code", **ev)
    require(not procs["worldserver"], "worldserver still running after failed start", **ev)
    return ev


@check("lifecycle.up", order=40, capability="server-lifecycle", stack="down", layer="integration")
def up(ctx):
    """Cold `hs up` exits 0, then SOAP `.server info` succeeds and 3724 accepts."""
    t0 = time.time()
    r = subprocess.run([HS, "up"], capture_output=True, text=True, timeout=2400)
    dt = time.time() - t0
    ev = {"cmd": f"{HS} up", "exit": r.returncode, "elapsed_s": round(dt, 1), "stdout": r.stdout[-1500:], "stderr_tail": r.stderr[-1500:]}
    require(r.returncode == 0, "hs up failed", **ev)
    info = soap.gm("server info")
    ev |= {"server_info": info.evidence(), "auth_port_open": stack.port_open(P.AUTH_PORT)}
    require(info.ok, "SOAP server info failed right after up", **ev)
    require(ev["auth_port_open"], f"auth port {P.AUTH_PORT} not accepting", **ev)
    return ev


@check("lifecycle.idempotent", order=45, capability="server-lifecycle", stack="up", layer="integration")
def idempotent(ctx):
    """Second `hs up` exits 0 and leaves exactly one of each process."""
    before = stack.stack_processes()
    r = subprocess.run([HS, "up"], capture_output=True, text=True, timeout=600)
    after = stack.stack_processes()
    ev = {"exit": r.returncode, "before": before, "after": after, "stdout": r.stdout[-800:]}
    require(r.returncode == 0, "second hs up failed", **ev)
    require(all(len(v) == 1 for v in after.values()), "not exactly one process per component", **ev)
    require(after == before, "process set changed", **ev)
    return ev


@check("lifecycle.isolated", order=50, capability="server-lifecycle", stack="up", layer="integration")
def isolated(ctx):
    """mysqld datadir is in-repo on port 3307; every stack listener is bound to 127.0.0.1."""
    datadir = db.scalar("SELECT @@datadir", root=True)
    port = db.scalar("SELECT @@port", root=True)
    procs = stack.stack_processes()
    socks = stack.listening_sockets([p for v in procs.values() for p in v])
    bad = [s for s in socks if not s.split()[-1].startswith("127.0.0.1:")]
    ev = {"datadir": datadir, "port": port, "listeners": socks, "non_localhost": bad, "repo": str(P.ROOT)}
    require(datadir and datadir.rstrip("/").startswith(str(P.ROOT)), "datadir outside repo", **ev)
    require(port == str(P.MYSQL_PORT), f"mysql port != {P.MYSQL_PORT}", **ev)
    require(socks, "no listening sockets found (lsof failed?)", **ev)
    require(not bad, "listener not bound to 127.0.0.1", **ev)
    return ev


@check("lifecycle.down", order=190, capability="server-lifecycle", stack="up", layer="integration")
def down(ctx):
    """`hs down` exits 0, nothing remains, ports are free, worldserver logged a graceful halt."""
    r = subprocess.run([HS, "down"], capture_output=True, text=True, timeout=600)
    procs = stack.stack_processes()
    ports = {p: stack.port_open(p) for p in P.STACK_PORTS}
    srv_log = (P.LOGS / "Server.log").read_text(errors="replace") if (P.LOGS / "Server.log").exists() else ""
    ev = {"exit": r.returncode, "stdout": r.stdout[-1500:], "remaining": procs, "ports_open": ports,
          "halting_logged": "Halting process" in srv_log, "pidfiles_left": [str(f) for f in stack.PIDFILE.values() if f.exists()]}
    require(r.returncode == 0, "hs down failed", **ev)
    require(not any(procs.values()), "orphaned processes", **ev)
    require(not any(ports.values()), "stack ports still open", **ev)
    require(ev["halting_logged"], "worldserver did not log a graceful halt", **ev)
    require('"graceful": false' not in r.stdout, "a component needed a hard kill", **ev)
    require(not ev["pidfiles_left"], "stale pidfiles", **ev)
    return ev
