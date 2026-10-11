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
        # a warm journey (WARM): the harness's keep-stack switch, which ./play takes only with its test flag
        warm = ["--allow-test-client-dir"] if warm_env() and "--allow-test-client-dir" not in extra else []
        self.proc = subprocess.Popen([*(wrap or []), PLAY, "--account", account, "--client-cmd", str(self.bat), *extra,
                                      *warm],
                                     cwd=P.ROOT, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                     start_new_session=True, env={**os.environ, **PINNED, **warm_env(), **(env or {})})
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
        if any(stack.stack_processes().values()) and WARM is None:
            stack.down()

    def cleanup(self):
        self.kill_play()
        shutil.rmtree(self.dir, ignore_errors=True)


def process_lane(pid: int) -> str:
    """The lane a process runs in: its environment's HS_LANE (unset or unreadable = lane A, the player's)."""
    ae = procs.args_env(pid)
    env = dict(e.split("=", 1) for e in (ae[1] if ae else []) if "=" in e)
    return (env.get(P.LANE_ENV) or "").strip().lower() or "a"


def play_processes(exclude=(), lane: str | None = None) -> list[dict]:
    """./play processes of `lane` (default: this process's lane; "*" = every lane)."""
    me = os.getpid()
    lane = lane or P.LANE
    return [{"pid": pid, "cmd": cmd[:200]} for pid, cmd in procs.table()
            if "hslib.play" in cmd and pid != me and pid not in exclude
            and (lane == "*" or process_lane(pid) == lane)]


# test-pyramid part 2, warm journeys (journey_checks.warm): while a warm journey runs, WARM holds the pids of the shared
# stack it found running. Its sessions then run ./play with the harness's keep-stack switch, and "nothing left running"
# means nothing besides that same stack (same pids; a different or partly stopped stack still counts as left running).
WARM: dict | None = None


def stack_pids() -> dict:
    return {k: sorted(p if isinstance(p, int) else p.get("pid", p) for p in v) for k, v in stack.stack_processes().items()}


def warm_env() -> dict:
    """The environment a warm journey's ./play runs with ({} outside one)."""
    return {play.KEEP_STACK_ENV: "1", "HS_HARNESS_RUN": "1"} if WARM is not None else {}


def warm_refresh(why: str) -> None:
    """A warm session restarted a stack component on purpose (e.g. the login server for the friendly error): the
    stack the journey keeps is the one running now."""
    if WARM is not None:
        WARM["pids"] = stack_pids()
        WARM.setdefault("refreshed", []).append(why)


def remaining(stand_in: StandIn | None = None) -> dict:
    """Everything from this repo that could still be running: stack processes, Wine/Wow.exe processes of this
    prefix, stack ports, a lingering ./play, and the stand-in's own process. In a warm journey (WARM) the shared stack
    it found running, unchanged, doesn't count."""
    st = stack.stack_processes()
    pref = launch.prefix_processes()
    r = {"stack": {k: v for k, v in st.items() if v},
         "prefix_processes": pref,
         "wow_exe": launch.wow_processes(pref),
         "ports_open": [p for p in P.STACK_PORTS if stack.port_open(p)],
         "play_processes": play_processes()}
    if stand_in:
        r["stand_in_processes"] = [p for p in pref if stand_in.bat.name.lower() in p["cmd"].lower()]
    if WARM is not None and all(st.values()) and stack_pids() == WARM["pids"]:
        r["stack"], r["ports_open"] = {}, []
    return r


def human_lines(text: str) -> str:
    """./play's output without its machine-readable `[stage] {json}` (launcher-polish) and `[shutdown] {json}`
    (launcher-fixes) lines, so a tail kept as evidence still holds the human lines a check asserts on."""
    return "\n".join(l for l in text.splitlines() if not l.startswith(("[stage] ", "[shutdown] ")))


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

# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "play",
    "harness/hslib/play.py",
    "harness/hslib/checks/fixture_checks.py",
    "harness/hslib/checks/play_checks.py",
    "harness/hslib/checks/resolution_checks.py",
)


# ---------------------------------------------------------------- offline proof (task 2.1)

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
WINE_ECHOES = 60     # self-test B: an upper bound; the ping is stopped as soon as the denial is logged (_selftest_wine)
WINE_SELFTEST_MAX_S = 75
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



def _selftest_wine(prof: Path, tag: str, since: str, work: Path) -> dict:
    """Self-test B: Wine's ping to 1.1.1.1 under the profile keeps sending echoes until the kernel's denial for a
    Wine process appears in the log, or WINE_SELFTEST_MAX_S passes; then the prefix is stopped.
    Why it keeps pinging (easy-setup 1.9, B30/B32): since the 2026-10-07 reboot this Mac's kernel usually emits no
    first violation report for a non-Apple (ad-hoc signed) process, only its "N duplicate reports for Sandbox:
    <proc>(<pid>) deny(1) ..." summary, and only while the process keeps denying and stays alive. A fixed number of
    echoes raced the prefix's shutdown (winedevice.exe exited before the summary: 1 FAIL in 7, 2026-10-07 14:02).
    Pinging until the line is observed removes the race; the line is tied to this run by the profile's unique tag
    (only this run's processes use this profile) and must name a .exe. Fail-closed: no line within the bound, or a
    reply from 1.1.1.1, is a failure."""
    out_path = work / "wine-ping.out"
    t0 = time.time()
    with open(out_path, "w") as out:
        ping = subprocess.Popen([SANDBOX_EXEC, "-f", str(prof), str(launch.WINE), "cmd", "/c",
                                 f"ping -n {WINE_ECHOES} 1.1.1.1"],
                                env=launch.wine_env(), cwd=work, stdout=out, stderr=subprocess.STDOUT, text=True)
        polls = 0
        wine_rows: list = []
        while time.time() - t0 < WINE_SELFTEST_MAX_S:
            time.sleep(2)
            polls += 1
            wine_rows = [r for r in _denials(tag, since)
                         if r["proc"].lower().endswith(".exe") and r["verdict"] == "deny" and not r["dns"]]
            if wine_rows or ping.poll() is not None:
                break
        observed_s = round(time.time() - t0, 1)
        ping_running = ping.poll() is None
        prefix_pids = sorted({p["pid"] for p in launch.prefix_processes()} | {ping.pid})
        stop = launch.stop_prefix()                     # also ends the ping (cmd/ping are prefix processes)
        try:
            ping.wait(timeout=30)
        except subprocess.TimeoutExpired:
            ping.kill()
            ping.wait(timeout=10)
    text = out_path.read_text(errors="replace")
    if not wine_rows:                                   # one last read after the stop (late lines still count)
        wine_rows = [r for r in _denials(tag, since)
                     if r["proc"].lower().endswith(".exe") and r["verdict"] == "deny" and not r["dns"]]
    return {"exit": ping.returncode, "max_echoes": WINE_ECHOES, "bound_s": WINE_SELFTEST_MAX_S,
            "observed_after_s": observed_s, "log_polls": polls, "ping_still_running_when_observed": ping_running,
            "stdout_tail": text[-600:], "replies": len(re.findall(r"Reply from [0-9.]+", text)),
            "stop_prefix": stop, "prefix_pids": prefix_pids, "denials": wine_rows,
            "denial_kinds": sorted({"duplicate-summary" if "duplicate report" in r["message"] else "first-report"
                                    for r in wine_rows})}


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


@check("play.player_account_untouched", order=151, capability=CAP, stack="up", layer="integration")
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
