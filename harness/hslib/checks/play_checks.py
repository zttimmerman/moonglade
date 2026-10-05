"""play-launcher: `./play` driven headlessly with a scripted stand-in client (openspec/changes/m1-offline-play-loop).

The stand-in is a Windows batch file in a temp dir (never under client/): it writes a `started` marker, then
waits until a `stop` marker appears and exits 0. `./play --account test --client-cmd <bat>` runs it with
`wine cmd /c` under the same Wine layer and prefix as Wow.exe. The checks use the harness test account (HSTEST);
they never touch the player's own account (env/player.json).
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .. import launch, paths as P, play, procs, stack
from ..registry import check, require

PLAY = str(P.ROOT / "play")
# Checks keep fixed profiles and never read the user's world-size setting (world-population "World size is one easy
# setting"): ./play under test runs the 10-bot m0 profile.
PINNED = {"HS_PROFILE": "m0"}
START_TIMEOUT = 900          # cold up is normally ~20-90 s; first_run would be far longer, never in a check run
SERVER_LOG = P.LOGS / "Server.log"
CAP = "play-launcher"


# ---------------------------------------------------------------- helpers

class StandIn:
    """A temp dir with the stand-in .bat, its markers, and the captured ./play output."""

    def __init__(self, tag: str):
        self.dir = Path(tempfile.mkdtemp(prefix=f"hs-play-{tag}-")).resolve()
        self.bat = self.dir / "hs-standin.bat"
        self.started = self.dir / "started"
        self.stop_file = self.dir / "stop"
        self.out = self.dir / "play.out"
        win = lambda p: "Z:" + str(p).replace("/", "\\")
        self.bat.write_text("\r\n".join([
            "@echo off",
            f"echo started> {win(self.started)}",
            ":loop",
            f"if exist {win(self.stop_file)} goto done",
            "ping -n 2 127.0.0.1 >nul",
            "goto loop",
            ":done",
            "exit 0", ""]))
        self.proc: subprocess.Popen | None = None

    def start_play(self, *extra: str, wrap: list[str] | None = None, account: str = "test",
                   env: dict | None = None, out: Path | None = None) -> subprocess.Popen:
        fh = open(out or self.out, "w")
        # Own session/process group, like a terminal's foreground job, so the check can deliver a Ctrl-C to
        # the whole group (os.killpg) the way a terminal does.
        self.proc = subprocess.Popen([*(wrap or []), PLAY, "--account", account, "--client-cmd", str(self.bat), *extra],
                                     cwd=P.ROOT, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                     start_new_session=True, env={**os.environ, **PINNED, **(env or {})})
        fh.close()
        return self.proc

    def wait_client_up(self, timeout=START_TIMEOUT) -> dict:
        """Wait until the stand-in wrote its marker and its Wine process is visible. Returns evidence."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                return {"up": False, "why": f"./play exited early with {self.proc.returncode}", "after_s": round(time.time() - t0, 1)}
            if self.started.exists():
                cp = play.client_processes(self.bat.name.lower())
                if cp:
                    return {"up": True, "after_s": round(time.time() - t0, 1), "client_processes": cp}
            time.sleep(1)
        return {"up": False, "why": "timeout", "after_s": round(time.time() - t0, 1)}

    def wait_exit(self, timeout) -> tuple[int | None, float]:
        t0 = time.time()
        try:
            rc = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            rc = None
        return rc, round(time.time() - t0, 1)

    def output(self) -> str:
        try:
            return self.out.read_text(errors="replace")
        except FileNotFoundError:
            return ""

    def kill_play(self):
        """Last resort if a check fails with ./play still running: stop it and clean up."""
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait()
        if launch.prefix_processes():
            launch.stop_prefix()
        if any(stack.stack_processes().values()):
            stack.down()

    def cleanup(self):
        self.kill_play()
        shutil.rmtree(self.dir, ignore_errors=True)


def play_processes(exclude=()) -> list[dict]:
    me = os.getpid()
    return [{"pid": pid, "cmd": cmd[:200]} for pid, cmd in procs.table()
            if "hslib.play" in cmd and pid != me and pid not in exclude]


def remaining(stand_in: StandIn | None = None) -> dict:
    """Everything from this repo that could still be running: stack processes, Wine/Wow.exe processes of this
    prefix, stack ports, a lingering ./play, and the stand-in's own process."""
    st = stack.stack_processes()
    pref = launch.prefix_processes()
    r = {"stack": {k: v for k, v in st.items() if v},
         "prefix_processes": pref,
         "wow_exe": launch.wow_processes(pref),
         "ports_open": [p for p in P.STACK_PORTS if stack.port_open(p)],
         "play_processes": play_processes()}
    if stand_in:
        r["stand_in_processes"] = [p for p in pref if stand_in.bat.name.lower() in p["cmd"].lower()]
    return r


def human_lines(text: str) -> str:
    """./play's output without its machine-readable `[stage] {json}` lines (launcher-polish), so a tail kept as
    evidence still holds the human lines a check asserts on."""
    return "\n".join(l for l in text.splitlines() if not l.startswith("[stage] "))


def is_clear(r: dict) -> bool:
    return not any(r.values())


def last_record(pid: int) -> dict:
    try:
        d = json.loads(play.LAST.read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return d if d.get("pid") == pid else {"stale_record_pid": d.get("pid")}


def graceful_halt(since: float, rec: dict) -> dict:
    """worldserver's log of this run shows a graceful halt, and play's stack.down() reports it graceful.
    Server.log is opened with mode 'w' at each worldserver start (Appender.Server ... ,w), so a file written
    after `since` that contains 'Halting process' belongs to this run's worldserver."""
    text = SERVER_LOG.read_text(errors="replace") if SERVER_LOG.exists() else ""
    mtime = SERVER_LOG.stat().st_mtime if SERVER_LOG.exists() else 0
    down = (rec.get("cleanup") or {}).get("down") or {}
    ws = down.get("worldserver") or {}
    return {"server_log": str(SERVER_LOG), "server_log_written_this_run": mtime >= since,
            "halting_logged": "Halting process" in text, "halting_tail": [l for l in text.splitlines() if "Halting" in l][-2:],
            "down_worldserver": ws, "worldserver_graceful": ws.get("graceful") is True,
            "ok": mtime >= since and "Halting process" in text and ws.get("graceful") is True}


def require_given_clear(ev: dict):
    before = remaining()
    ev["given_nothing_running"] = before
    require(is_clear(before), "GIVEN not met: something from this repo is already running", **ev)


# ---------------------------------------------------------------- checks

@check("play.loop_clean_exit", order=300, capability=CAP, stack="down", tier="full")
def loop_clean_exit(ctx):
    """`./play` blocks while the client runs; when the client exits normally ./play exits 0 within 120 s, worldserver
    logged a graceful halt, and no stack/Wine/Wow.exe process remains, ports 3307/3724/8085/7878 free."""
    ev: dict = {"cmd": f"{PLAY} --account test --client-cmd <stand-in .bat>"}
    require_given_clear(ev)
    s = StandIn("exit")
    try:
        t_start = time.time()
        s.start_play()
        ev["play_pid"] = s.proc.pid
        up = s.wait_client_up()
        ev["client_up"] = up
        require(up["up"], "stand-in client never came up under ./play", output=s.output()[-3000:], **ev)
        time.sleep(5)
        ev["play_blocking_while_client_runs"] = s.proc.poll() is None
        require(ev["play_blocking_while_client_runs"], "./play exited while the client was still running", **ev)

        s.stop_file.write_text("quit")         # the stand-in exits 0 within ~1 s of this (its poll interval)
        t_client_exit = time.time()
        rc, dt = s.wait_exit(timeout=300)
        ev |= {"exit_code": rc, "exit_after_client_quit_s": dt, "output": s.output()[-3000:]}
        rec = last_record(s.proc.pid)
        ev["play_record"] = {k: rec.get(k) for k in ("outcome", "client_exit", "steps", "exit_code", "cleanup")}
        ev["graceful_halt"] = graceful_halt(t_start, rec)
        ev["remaining_after"] = remaining(s)
        require(rc == 0, f"./play exit code {rc}, expected 0", **ev)
        require(dt <= 120, f"./play took {dt}s after the client exited (limit 120 s)", **ev)
        require(rec.get("outcome") == "client_exit" and (rec.get("client_exit") or {}).get("code") == 0,
                "./play didn't record a normal client exit", **ev)
        require(ev["graceful_halt"]["ok"], "worldserver didn't log a graceful halt for this run", **ev)
        require(is_clear(ev["remaining_after"]), "something from this repo is still running", **ev)
        ev["client_exit_to_play_exit_s"] = round(time.time() - t_client_exit, 1)
        return ev
    finally:
        s.cleanup()


def _interrupt_cycle(how: str) -> dict:
    """One ./play run interrupted while the client is up. how = 'SIGINT' (Ctrl-C: sent to the whole process group,
    as a terminal does) or 'SIGTERM' (sent to ./play). A second identical signal follows 1 s later."""
    ev: dict = {"signal": how}
    s = StandIn(how.lower())
    try:
        t_start = time.time()
        s.start_play()
        ev["play_pid"] = s.proc.pid
        up = s.wait_client_up()
        ev["client_up"] = up
        require(up["up"], "stand-in client never came up under ./play", output=s.output()[-3000:], **ev)
        sig = signal.SIGINT if how == "SIGINT" else signal.SIGTERM
        send = (lambda: os.killpg(s.proc.pid, sig)) if how == "SIGINT" else (lambda: os.kill(s.proc.pid, sig))
        t_sig = time.time()
        send()
        time.sleep(1)
        second_sent = s.proc.poll() is None
        if second_sent:
            send()                               # idempotence: must not abort or double-kill the cleanup
        ev["second_signal_sent_during_shutdown"] = second_sent
        rc, _ = s.wait_exit(timeout=300)
        dt = round(time.time() - t_sig, 1)
        rec = last_record(s.proc.pid)
        clean = rec.get("cleanup") or {}
        ev |= {"exit_code": rc, "exit_after_signal_s": dt, "output": s.output()[-3000:],
               "play_record": {k: rec.get(k) for k in ("outcome", "signal", "steps", "exit_code", "cleanup")},
               "stand_in_was_told_to_stop": s.stop_file.exists(),
               "graceful_halt": graceful_halt(t_start, rec), "remaining_after": remaining(s)}
        require(rc is not None, "./play didn't exit within 300 s of the signal", **ev)
        require(dt <= 120, f"./play took {dt}s to exit after {how} (limit 120 s)", **ev)
        require(rec.get("outcome") == "interrupted" and (rec.get("signal") or {}).get("name") == how,
                f"./play didn't record the {how} interrupt", **ev)
        # the client was closed by ./play (the stand-in never got its stop marker, so it didn't exit by itself)
        require("stop_prefix" in clean and not clean["stop_prefix"].get("sigkilled")
                and not clean["stop_prefix"].get("remaining_after_k"),
                "./play didn't close the client cleanly (wineserver -k)", **ev)
        require(not ev["stand_in_was_told_to_stop"] and not ev["remaining_after"]["stand_in_processes"],
                "client not closed by ./play", **ev)
        require(ev["graceful_halt"]["ok"], "stack didn't shut down gracefully", **ev)
        require(is_clear(ev["remaining_after"]), "something from this repo is still running", **ev)
        return ev
    finally:
        s.cleanup()


@check("play.interrupt_clean_exit", order=310, capability=CAP, stack="down", tier="full")
def interrupt_clean_exit(ctx):
    """SIGINT (Ctrl-C to the process group) and, in a second run, SIGTERM to ./play with the client up: the client is
    closed, the stack shuts down gracefully, ./play exits within 120 s, nothing from this repo remains. A repeated
    signal during shutdown is ignored."""
    ev: dict = {"cmd": f"{PLAY} --account test --client-cmd <stand-in .bat>"}
    require_given_clear(ev)
    ev["SIGINT"] = _interrupt_cycle("SIGINT")
    _ensure_down_between_cycles(ev)
    ev["SIGTERM"] = _interrupt_cycle("SIGTERM")
    return ev


def _ensure_down_between_cycles(ev):
    r = remaining()
    require(is_clear(r), "something still running between interrupt cycles", between=r, **ev)


@check("play.client_crash_cleanup", order=320, capability=CAP, stack="down", tier="full")
def client_crash_cleanup(ctx):
    """SIGKILL the client's Wine process: ./play detects it, shuts the stack down gracefully, exits nonzero with a
    message naming the client crash, and nothing from this repo remains."""
    ev: dict = {"cmd": f"{PLAY} --account test --client-cmd <stand-in .bat>"}
    require_given_clear(ev)
    s = StandIn("crash")
    try:
        t_start = time.time()
        s.start_play()
        ev["play_pid"] = s.proc.pid
        up = s.wait_client_up()
        ev["client_up"] = up
        require(up["up"], "stand-in client never came up under ./play", output=s.output()[-3000:], **ev)
        victims = play.client_processes(s.bat.name.lower())
        ev["killed"] = victims
        for v in victims:
            os.kill(v["pid"], signal.SIGKILL)
        t_kill = time.time()
        rc, _ = s.wait_exit(timeout=300)
        out = s.output()
        rec = last_record(s.proc.pid)
        crash_lines = [l for l in out.splitlines() if "crash" in l.lower()]
        ev |= {"exit_code": rc, "exit_after_kill_s": round(time.time() - t_kill, 1), "output": out[-3000:],
               "crash_message_lines": crash_lines,
               "play_record": {k: rec.get(k) for k in ("outcome", "client_exit", "crash_message", "steps", "exit_code", "cleanup")},
               "graceful_halt": graceful_halt(t_start, rec), "remaining_after": remaining(s)}
        require(victims, "no client process found to kill", **ev)
        require(rc is not None and rc != 0, f"./play exit code {rc}, expected nonzero", **ev)
        require(rec.get("outcome") == "client_crash", "./play didn't detect the client crash", **ev)
        require(any("client crashed" in l.lower() or "client crash" in l.lower() for l in crash_lines),
                "./play's output doesn't name the client crash", **ev)
        require(ev["graceful_halt"]["ok"], "stack didn't shut down gracefully", **ev)
        require(is_clear(ev["remaining_after"]), "something from this repo is still running", **ev)
        return ev
    finally:
        s.cleanup()


@check("play.startup_failure", order=330, capability=CAP, stack="down", tier="full")
def startup_failure(ctx):
    """With an unusable worldserver DataDir ./play exits nonzero with the server's log excerpt, never starts the
    client, and leaves nothing running."""
    bad = "/nonexistent/hs-play-failfast"
    ev: dict = {"cmd": f"{PLAY} --account test --client-cmd <stand-in .bat> --world-datadir {bad}"}
    require_given_clear(ev)
    s = StandIn("fail")
    wine_log_offset = P.WINE_LOG.stat().st_size if P.WINE_LOG.exists() else 0
    try:
        s.start_play("--world-datadir", bad)
        ev["play_pid"] = s.proc.pid
        rc, dt = s.wait_exit(timeout=900)
        out = s.output()
        rec = last_record(s.proc.pid)
        try:
            with open(P.WINE_LOG, errors="replace") as fh:
                fh.seek(wine_log_offset)
                launches = [l.strip() for l in fh if "[hs launch]" in l and " start " in l]
        except FileNotFoundError:
            launches = []
        excerpt = out.split("server log excerpt", 1)[-1] if "server log excerpt" in out else ""
        ev |= {"exit_code": rc, "elapsed_s": dt, "output": out[-4000:],
               "play_record": {k: rec.get(k) for k in ("outcome", "error", "client_started", "steps", "exit_code", "cleanup")},
               "client_start_stamps_in_wine_log": launches, "stand_in_started_marker": s.started.exists(),
               "remaining_after": remaining(s)}
        require(rc is not None and rc != 0, f"./play exit code {rc}, expected nonzero", **ev)
        # the cause worldserver logs for a missing DataDir (World.cpp, LOG_ERROR "server.loading")
        require("Failed to find map files" in excerpt, "./play's output lacks the server log excerpt showing the cause", **ev)
        require(rec.get("outcome") == "startup_failed" and rec.get("client_started") is False,
                "./play didn't record a startup failure before the client", **ev)
        require(not s.started.exists() and not launches, "the client was started despite the failed startup", **ev)
        require(is_clear(ev["remaining_after"]), "something from this repo is still running", **ev)
        return ev
    finally:
        s.cleanup()


@check("play.single_instance", order=340, capability=CAP, stack="down", tier="full")
def single_instance(ctx):
    """While ./play runs, a second ./play exits nonzero within 5 s naming the first one's pid; the first is unaffected
    (keeps running, then exits 0 normally). Setup also leaves a stale lock (dead pid) that the first must reclaim."""
    ev: dict = {"cmd": f"{PLAY} --account test --client-cmd <stand-in .bat>"}
    require_given_clear(ev)
    dead = subprocess.Popen(["/usr/bin/true"])
    dead.wait()
    P.RUN.mkdir(parents=True, exist_ok=True)
    play.LOCK.write_text(f"{dead.pid}\n")       # stale: that process is gone and holds no flock
    ev["stale_lock_pid_planted"] = dead.pid
    first, second = StandIn("first"), StandIn("second")
    try:
        first.start_play()
        ev["first_pid"] = first.proc.pid
        up = first.wait_client_up()
        ev["first_client_up"] = up
        require(up["up"], "first ./play never got its client up (stale lock not reclaimed?)",
                output=first.output()[-3000:], **ev)
        stack_before = stack.stack_processes()
        lock_before = play.LOCK.read_text().strip()

        t0 = time.time()
        second.start_play()
        rc2, _ = second.wait_exit(timeout=30)
        dt2 = round(time.time() - t0, 1)
        out2 = second.output()
        time.sleep(2)
        ev |= {"lock_holder_before": lock_before, "second_exit_code": rc2, "second_elapsed_s": dt2,
               "second_output": out2[-2000:], "second_client_started": second.started.exists(),
               "first_running_after": first.proc.poll() is None,
               "first_client_after": play.client_processes(first.bat.name.lower()),
               "lock_holder_after": play.LOCK.read_text().strip(),
               "stack_before": stack_before, "stack_after": stack.stack_processes()}
        require(lock_before == str(first.proc.pid), "lock file doesn't hold the first ./play's pid", **ev)
        require(rc2 is not None and rc2 != 0, f"second ./play exit code {rc2}, expected nonzero", **ev)
        require(dt2 < 5, f"second ./play took {dt2}s to refuse (limit 5 s)", **ev)
        require(re.search(rf"\b{first.proc.pid}\b", out2), "second ./play's message doesn't name the running pid", **ev)
        require(not ev["second_client_started"], "second ./play started a client", **ev)
        require(ev["first_running_after"] and ev["first_client_after"], "first ./play or its client was affected", **ev)
        require(ev["stack_after"] == stack_before and ev["lock_holder_after"] == str(first.proc.pid),
                "the stack or the lock changed under the first ./play", **ev)

        first.stop_file.write_text("quit")
        rc1, dt1 = first.wait_exit(timeout=300)
        rec = last_record(first.proc.pid)
        ev |= {"first_exit_code": rc1, "first_exit_after_quit_s": dt1, "first_output": first.output()[-2500:],
               "first_record": {k: rec.get(k) for k in ("lock", "outcome", "exit_code")},
               "remaining_after": remaining(first)}
        require((rec.get("lock") or {}).get("reclaimed_stale_pid") == str(dead.pid),
                "first ./play didn't record reclaiming the stale lock", **ev)
        require(rc1 == 0 and rec.get("outcome") == "client_exit", "first ./play didn't finish normally", **ev)
        require(is_clear(ev["remaining_after"]), "something from this repo is still running", **ev)
        return ev
    finally:
        second.cleanup()
        first.cleanup()


# ---------------------------------------------------------------- offline proof (task 2.1)

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
LOG = "/usr/bin/log"
_LINE_RE = re.compile(r"Sandbox: (?P<proc>.+?)\((?P<pid>\d+)\) (?P<verdict>deny|allow)(?:\(\d+\))? (?P<op>\S+) ?(?P<target>\S*)")


def _profile(tag: str) -> str:
    """Deny every outbound IP connection except loopback, and the system DNS resolver socket; each denial is
    logged by the kernel with `tag`. Everything else is allowed (the loop must work as normal)."""
    return f"""(version 1)
(allow default)
(deny network-outbound (remote ip "*:*") (with message "{tag}"))
(allow network-outbound (remote ip "localhost:*"))
(deny network-outbound (literal "/private/var/run/mDNSResponder") (with message "{tag}-dns"))
"""


def _denials(tag: str, since: str) -> list[dict]:
    r = subprocess.run([LOG, "show", "--start", since, "--style", "ndjson", "--predicate",
                        f'eventMessage CONTAINS "{tag}"'], capture_output=True, text=True, timeout=180)
    rows = []
    for line in r.stdout.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get("eventMessage", "")
        m = _LINE_RE.search(msg)
        if not m or e.get("senderImagePath", "").endswith("/log"):
            continue
        rows.append({"time": e.get("timestamp"), "proc": m["proc"], "pid": int(m["pid"]), "verdict": m["verdict"],
                     "op": m["op"], "target": m["target"], "dns": f"{tag}-dns" in msg, "message": msg[:300]})
    return rows


def _wait_denials(tag, since, pred, timeout=30):
    t0 = time.time()
    rows = _denials(tag, since)
    while not pred(rows) and time.time() - t0 < timeout:
        time.sleep(2)
        rows = _denials(tag, since)
    return rows


def _sandboxed(pid: int) -> int:
    import ctypes
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    lib.sandbox_check.restype = ctypes.c_int
    lib.sandbox_check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    return lib.sandbox_check(pid, None, 0)          # 1 = the process runs under a sandbox profile


_PROBE = r"""
import socket, sys
out = {}
for fam, host, port in ((socket.AF_INET, "1.1.1.1", 443), (socket.AF_INET6, "2606:4700:4700::1111", 443)):
    s = socket.socket(fam, socket.SOCK_STREAM); s.settimeout(3)
    try:
        s.connect((host, port)); out[host] = "CONNECTED"
    except OSError as e:
        out[host] = f"{type(e).__name__} errno={e.errno}"
    finally:
        s.close()
s = socket.socket(); s.settimeout(3)
try:
    s.connect(("127.0.0.1", 9)); out["127.0.0.1:9"] = "CONNECTED"
except OSError as e:
    out["127.0.0.1:9"] = f"{type(e).__name__} errno={e.errno}"
print(repr(out))
"""


@check("play.offline", order=350, capability=CAP, stack="down", tier="full")
def offline(ctx):
    """The whole play loop runs under a sandbox-exec profile that denies (and logs) every non-loopback outbound
    connection for ./play and all its children; startup, client launch and shutdown succeed, and the denial log
    shows no attempt from the loop. The deny is proven live first: a non-loopback connect under the same profile,
    directly and through Wine, is refused and logged."""
    ev: dict = {}
    require(os.access(SANDBOX_EXEC, os.X_OK), "sandbox-exec not available on this macOS", **ev)
    require_given_clear(ev)
    tag = f"hs-offline-{os.getpid()}-{int(time.time())}"
    work = Path(tempfile.mkdtemp(prefix="hs-offline-")).resolve()
    prof = work / "offline.sb"
    prof.write_text(_profile(tag))
    ev |= {"profile_path": str(prof), "profile": prof.read_text(), "log_tag": tag}
    since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    s = StandIn("offline")
    try:
        # 1. self-test A: a plain process under the profile can't reach a non-loopback address; loopback still works
        probe = subprocess.Popen([SANDBOX_EXEC, "-f", str(prof), "python3", "-c", _PROBE],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        probe_out = probe.communicate(timeout=60)[0]
        try:
            res = ast.literal_eval(probe_out.strip().splitlines()[-1])
        except (ValueError, SyntaxError, IndexError):
            res = {}
        ev["selftest_direct"] = {"pid": probe.pid, "exit": probe.returncode, "result": res or probe_out[-1000:]}
        require(res and all(res.get(h, "").startswith("PermissionError errno=1") for h in ("1.1.1.1", "2606:4700:4700::1111")),
                "self-test: non-loopback connects (IPv4 + IPv6) not refused with EPERM", **ev)
        require(res.get("127.0.0.1:9", "").startswith("ConnectionRefusedError"),
                "self-test: loopback isn't reachable under the profile (expected 'connection refused' from a closed port)", **ev)
        rows = _wait_denials(tag, since, lambda rs: any(r["pid"] == probe.pid and r["verdict"] == "deny" for r in rs))
        ev["selftest_direct"]["denials"] = [r for r in rows if r["pid"] == probe.pid]
        require(any(r["op"] == "network-outbound" and not r["dns"] for r in ev["selftest_direct"]["denials"]),
                "self-test: the refused connection isn't in the denial log", **ev)

        # 2. self-test B: the same through the Wine layer (Wine's ping to 1.1.1.1; Wine sends ICMP from winedevice.exe)
        t_wine = time.time()
        wine = subprocess.run([SANDBOX_EXEC, "-f", str(prof), str(launch.WINE), "cmd", "/c", "ping -n 1 1.1.1.1"],
                              env=launch.wine_env(), cwd=work, capture_output=True, text=True, timeout=180)
        ev["selftest_wine"] = {"exit": wine.returncode, "stdout_tail": wine.stdout[-600:],
                               "stop_prefix": launch.stop_prefix()}
        rows = _wait_denials(tag, since, lambda rs: any(r["proc"].lower().endswith(".exe") and r["verdict"] == "deny"
                                                        and not r["dns"] for r in rs))
        ev["selftest_wine"]["denials"] = [r for r in rows if r["proc"].lower().endswith(".exe") and not r["dns"]]
        require(ev["selftest_wine"]["denials"], "self-test: a Wine process's non-loopback attempt wasn't denied+logged", **ev)
        time.sleep(2)
        t_loop = datetime.now()
        loop_since = t_loop.strftime("%Y-%m-%d %H:%M:%S")
        time.sleep(1.5)                                  # keep self-test log lines strictly before the loop window

        # 3. the play loop under the profile
        t_start = time.time()
        s.start_play(wrap=[SANDBOX_EXEC, "-f", str(prof)])
        ev["play_pid"] = s.proc.pid
        up = s.wait_client_up()
        ev["client_up"] = up
        require(up["up"], "stand-in client never came up under sandboxed ./play", output=s.output()[-3000:], **ev)
        # every long-running process of the loop runs under the profile (inherited from sandbox-exec)
        st = stack.stack_processes()
        members = ([{"pid": s.proc.pid, "what": "./play"}]
                   + [{"pid": pid, "what": k} for k, v in st.items() for pid in v]
                   + [{"pid": p["pid"], "what": p["cmd"][:80]} for p in launch.prefix_processes()])
        for m in members:
            m["sandboxed"] = _sandboxed(m["pid"]) == 1
            if not m["sandboxed"] and not stack.alive(m["pid"]):
                m["exited_before_check"] = True       # transient (e.g. the stand-in's ping); nothing to inspect
        ev["loop_processes_sandboxed"] = members
        require(all(st.values()), "stack not fully up while the client runs", stack=st, **ev)
        require(all(m["sandboxed"] or m.get("exited_before_check") for m in members),
                "a loop process isn't running under the deny profile", **ev)
        require(sum(m["sandboxed"] for m in members) >= 4 + len(up["client_processes"]),
                "too few sandboxed loop processes found (./play + 3 stack + client expected)", **ev)

        s.stop_file.write_text("quit")
        rc, dt = s.wait_exit(timeout=300)
        rec = last_record(s.proc.pid)
        ev |= {"exit_code": rc, "exit_after_client_quit_s": dt, "output": s.output()[-3000:],
               "play_record": {k: rec.get(k) for k in ("outcome", "steps", "exit_code", "cleanup")},
               "graceful_halt": graceful_halt(t_start, rec), "remaining_after": remaining(s)}
        time.sleep(5)                                    # let the kernel's sandbox reports reach the log
        rows = _denials(tag, since)
        in_loop = [r for r in rows if r["time"] and r["time"][:19].replace("T", " ") >= loop_since]
        ev["denial_log"] = {"query_since": since, "loop_window_since": loop_since, "all_rows": rows,
                            "loop_ip_denials": [r for r in in_loop if not r["dns"]],
                            "loop_dns_lookups_denied": [r for r in in_loop if r["dns"]]}
        require(rc == 0, f"sandboxed ./play exit code {rc}, expected 0", **ev)
        require(rec.get("outcome") == "client_exit", "sandboxed loop didn't complete normally", **ev)
        require(ev["graceful_halt"]["ok"], "sandboxed loop didn't shut the stack down gracefully", **ev)
        require(is_clear(ev["remaining_after"]), "something from this repo is still running", **ev)
        require(not ev["denial_log"]["loop_ip_denials"],
                "a process of the loop attempted a non-loopback connection", **ev)
        return ev
    finally:
        s.cleanup()
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- personal player account

SCRATCH_PLAYER = "HSPLAYTEST"        # scratch personal accounts: never the real env/player.json account
SCRATCH_KEEP = "HSPCKEEP"
SCRATCH_KEEP_CHAR = "Pckeeper"


def _real_player_name() -> str | None:
    try:
        return json.loads(play.PLAYER_FILE.read_text())["account"].upper()
    except (FileNotFoundError, ValueError, KeyError):
        return None


def _acct_rows(name: str) -> list:
    from .. import db
    return db.query(f"SELECT id, username FROM account WHERE username='{name.upper()}'", "auth")


def _delete_scratch(name: str) -> dict:
    """SOAP `account delete` for a scratch account (also deletes its characters). Refuses the real player's account."""
    from .. import fixtures as F, soap
    require(name.upper() != _real_player_name(), f"refusing to delete {name}: it is the real player account")
    if not _acct_rows(name):
        return {"account": name, "deleted": False, "reason": "absent"}
    r = soap.gm(f"account delete {name}")
    _, gone, _ = F.poll(lambda: _acct_rows(name), lambda v: not v, timeout=20)
    return {"account": name, "deleted": gone, "gm": r.evidence()}


@check("play.player_account", order=360, capability=CAP, stack="down", tier="full")
def player_account(ctx):
    """Default (personal-account) path with a scratch player file: the first ./play writes the file (0600) and
    creates exactly one non-GM account with no characters and prints the login; a second ./play creates nothing and
    keeps the account id; the password is never in play-last.json; HSTEST / HS_GM in the file are refused."""
    from .. import db
    ev: dict = {"scratch_account": SCRATCH_PLAYER, "real_player_file": str(play.PLAYER_FILE),
                "real_player_account": _real_player_name()}
    require(SCRATCH_PLAYER != ev["real_player_account"], "scratch account name collides with the real player account", **ev)
    real_before = play.PLAYER_FILE.read_bytes() if play.PLAYER_FILE.exists() else None
    require_given_clear(ev)
    s = StandIn("player")
    pfile = s.dir / "player.json"
    env = {"HS_PLAYER_FILE": str(pfile), "HS_PLAYER_DEFAULT_NAME": SCRATCH_PLAYER}
    try:
        runs = []
        for i in (1, 2):
            for f in (s.started, s.stop_file):
                f.unlink(missing_ok=True)
            out = s.dir / f"play{i}.out"
            s.out = out
            s.start_play(account="player", env=env, out=out)
            up = s.wait_client_up()
            run = {"client_up": up}
            if up["up"]:
                # observe the DB while the stack is up (it's down again after ./play exits)
                run["account_rows"] = _acct_rows(SCRATCH_PLAYER)
                aid = run["account_rows"][0][0] if run["account_rows"] else "-1"
                run["account_access_rows"] = db.query(f"SELECT id, gmlevel, RealmID FROM account_access WHERE id={aid}", "auth")
                run["characters"] = db.query(f"SELECT guid, name FROM characters WHERE account={aid}", "characters")
                run["total_accounts"] = db.scalar("SELECT COUNT(*) FROM account", "auth")
                run["file_mode"] = oct(pfile.stat().st_mode & 0o777) if pfile.exists() else None
                run["file_bytes"] = pfile.read_text() if pfile.exists() else None
                run["file_mtime_ns"] = pfile.stat().st_mtime_ns if pfile.exists() else None
                s.stop_file.write_text("quit")
            rc, _ = s.wait_exit(timeout=300)
            rec = last_record(s.proc.pid)
            last_text = play.LAST.read_text() if play.LAST.exists() else ""
            run |= {"exit_code": rc, "output": human_lines(out.read_text(errors="replace"))[-2500:],
                    "record_account": rec.get("account"), "record_prepare_accounts": (rec.get("prepare") or {}).get("accounts"),
                    "outcome": rec.get("outcome"), "_last_text": last_text}
            runs.append(run)
            require(up["up"] and rc == 0, f"run {i}: ./play didn't complete normally", runs=runs, **ev)
        pw = json.loads(pfile.read_text())["password"]
        for r in runs:
            r["password_in_play_last_json"] = pw in r.pop("_last_text")
        r1, r2 = runs
        ev["runs"] = runs
        ev["remaining_after"] = remaining(s)
        require(r1["file_mode"] == "0o600", "player file not mode 0600", **ev)
        require(len(r1["account_rows"]) == 1, "first run: expected exactly one scratch account", **ev)
        require(not any(int(row[1]) > 0 for row in r1["account_access_rows"]), "account has a GM security level", **ev)
        require(r1["characters"] == [], "new personal account has characters", **ev)
        require((r1["record_prepare_accounts"] or {}).get("created") is True
                and (r1["record_account"] or {}).get("file_created") is True, "first run didn't record creating file + account", **ev)
        require(SCRATCH_PLAYER in r1["output"] and pw in r1["output"], "first run didn't print the login", **ev)
        require(r2["account_rows"] == r1["account_rows"], "second run: account id/rows changed", **ev)
        require(r2["total_accounts"] == r1["total_accounts"], "second run created another account", **ev)
        require(r2["file_bytes"] == r1["file_bytes"] and r2["file_mtime_ns"] == r1["file_mtime_ns"], "second run rewrote the player file", **ev)
        require((r2["record_prepare_accounts"] or {}).get("created") is False
                and (r2["record_account"] or {}).get("file_created") is False, "second run recorded creating something", **ev)
        require(not any(r["password_in_play_last_json"] for r in runs), "the password appears in play-last.json", **ev)

        # reserved names are refused (before anything starts)
        ev["reserved"] = {}
        for name in (P.TEST_USER, P.GM_USER):
            bad = s.dir / f"reserved-{name}.json"
            bad.write_text(json.dumps({"account": name, "password": "whatever1"}))
            r = subprocess.run([PLAY, "--account", "player", "--client-cmd", str(s.bat)], cwd=P.ROOT,
                               env={**os.environ, **PINNED, "HS_PLAYER_FILE": str(bad)}, capture_output=True, text=True,
                               timeout=300)
            ev["reserved"][name] = {"exit": r.returncode, "output": (r.stdout + r.stderr)[-800:]}
            require(r.returncode != 0 and f"{name} is reserved for the test harness" in r.stdout, f"{name} in the player file wasn't refused", **ev)
        require(is_clear(remaining(s)), "something from this repo is still running", **ev)
        real_after = play.PLAYER_FILE.read_bytes() if play.PLAYER_FILE.exists() else None
        require(real_after == real_before, "the real env/player.json changed", **ev)
        ev["real_player_file_unchanged"] = True
        return ev
    finally:
        s.kill_play()
        try:
            stack.up()
            ev["scratch_cleanup"] = _delete_scratch(SCRATCH_PLAYER)
        finally:
            stack.down()
            s.cleanup()


def _snapshot(name: str) -> dict:
    from .. import db
    acct = db.query(f"SELECT id, username, HEX(salt), HEX(verifier), expansion, locked, joindate, last_login "
                    f"FROM account WHERE username='{name.upper()}'", "auth")
    aid = acct[0][0] if acct else "-1"
    return {"account": acct,
            "account_access": db.query(f"SELECT id, gmlevel, RealmID, comment FROM account_access WHERE id={aid} ORDER BY RealmID", "auth"),
            # the fixture grants the TEST account instant logout (rbac); a personal account must get nothing
            "rbac_account_permissions": db.query(f"SELECT permissionId, granted, realmId FROM rbac_account_permissions "
                                                 f"WHERE accountId={aid} ORDER BY permissionId, realmId", "auth"),
            "characters": db.query(f"SELECT * FROM characters WHERE account={aid} ORDER BY guid", "characters")}


@check("play.player_account_untouched", order=151, capability=CAP, stack="up", tier="full")
def player_account_untouched(ctx):
    """A personal account (scratch, created by ./play's own account step, with one character) is byte-identical —
    auth row incl. salt/verifier, account_access, rbac permissions, characters — before and after the fixture reset that
    fixture.account_character runs (SOAP `account delete` + recreate of the test account)."""
    from . import fixture_checks
    from ..probe import client as probe
    ev: dict = {"scratch_account": SCRATCH_KEEP, "real_player_account": _real_player_name()}
    require(SCRATCH_KEEP != ev["real_player_account"], "scratch account name collides with the real player account", **ev)
    login = {"account": SCRATCH_KEEP, "password": "keep1234"}
    try:
        ev["create"] = play.ensure_player_account(login)
        lr, ws, _ = probe.open_world(login["account"], login["password"])
        with ws:
            if not any(c["name"].lower() == SCRATCH_KEEP_CHAR.lower() for c in ws.char_enum()):
                ev["char_create"] = ws.char_create(SCRATCH_KEEP_CHAR, 1, 1)
        from .. import fixtures as F
        F.poll(lambda: _snapshot(SCRATCH_KEEP)["characters"], bool, timeout=15)
        before = _snapshot(SCRATCH_KEEP)
        ev["before"] = before
        require(before["account"] and len(before["characters"]) == 1, "setup: scratch personal account/character missing", **ev)
        ev["test_fixture_present"] = F.ensure()          # so the reset below really deletes + recreates HSTEST
        test_id_before = F.account_id()
        ev["fixture_reset_run"] = fixture_checks.account_character({})
        ev["test_account_id_before_after"] = [test_id_before, F.account_id()]
        after = _snapshot(SCRATCH_KEEP)
        ev["after"] = after
        require(ev["fixture_reset_run"]["reset"].get("deleted") is True, "the fixture reset didn't actually delete+recreate HSTEST", **ev)
        require(after == before, "the personal account or its characters changed during the fixture reset", **ev)
        return ev
    finally:
        ev["scratch_cleanup"] = _delete_scratch(SCRATCH_KEEP)
