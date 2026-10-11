"""The play loop's own start and stop, in as few cold ./play sessions as the scenarios' endings allow (test-pyramid part
3, design 15 lever 1: no cold ./play session per scenario in the integration layer).

`play.cold_sessions` is one integration check; each scenario it covers keeps its own id as a child (session.
run_children) with its own status and evidence, asserting the same THEN as its former check did. A cold ./play
session ends one way, so the sessions are the distinct endings the scenarios need, each shared by every scenario
whose GIVEN it meets (all with the stand-in client, never the real Wow.exe):

- RA (mysqld only, nothing else running): backups.restore_refusals' damaged-backup and live-target cases, on a backup
  of the live databases (read only) seeded into scratch hsbk_rf_* schemas; and backups.kill_safe's earlier backup in a
  scratch folder R1.
- N, a normal exit, from nothing running, under the offline sandbox profile, Wine's notepad as the stand-in, the
  personal-account path with a scratch player file (its first run), no backup (a backup's [play] line is newer than
  play.stage_lines' base): play.offline, play.loop_clean_exit, play.dock_identity, play.stage_lines,
  play.world_progress, play.player_account (run 1).
- K, the play loop SIGKILLed in the middle of its backup in R1 (test knob: paused after the first database).
- N2, the next run after K (it meets K's leftovers: the temp folder, the stale lock, mysqld still running): a normal
  exit, the personal account's second run: backups.kill_safe (the heal and a complete backup), play.single_instance
  (a second ./play refused; K's stale lock reclaimed), play.player_account (run 2), backups.restore_refusals (a
  restore while it runs is refused, and it continues unaffected). The scratch personal account is deleted over SOAP
  while N2's server runs.
- backups.kill_safe's killed restore (mysqld only, then a test-shaped ./play that heals it).
- I, SIGINT with the client up, the backup's dump failing (test knob) in a scratch folder R2:
  play.interrupt_clean_exit, backups.failure_nonblocking (first failure).
- C, the client SIGKILLed, the backup failing for low disk (test knob) in R2: play.client_crash_cleanup,
  backups.failure_nonblocking (second failure; its start carries the notice of the first).
- F, a launch whose worldserver can't start (bad DataDir), in R2: play.startup_failure, backups.failure_nonblocking
  (its output carries the notice of the second failure).

SIGTERM and the repeated-signal handling are unit tests of the handler (unit:py:test_play_signals.*), the live
interrupt is SIGINT (design 9.3). The sessions run in this order (drivers memoised in `_RUN`); a scenario whose
session didn't happen fails naming it.
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
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .. import backups as B, launch, paths as P, play, stack, stages as S
from ..registry import CheckFail, check, require
from ..session import run_children
from . import backup_checks as BC
from . import play_checks as PC
from .play_checks import PINNED, PLAY, StandIn, graceful_halt, human_lines, is_clear, last_record, remaining

CAP = "play-launcher"
KILL_PAUSE_S = "240"             # the backup's mid-dump pause in K (the kill comes as soon as the pause is reached)
BLOCKING_MIN_S = 5.0             # play.loop_clean_exit: ./play still blocking this long after the client came up
SANDBOX_REPORTS_S = 5            # play.offline: the kernel's sandbox reports reach the unified log after the loop

_RUN: dict = {}                  # driver name -> its evidence (this process; a new run starts empty)


def _tmp(tag: str) -> Path:
    return BC.scratch_root(tag)


def _need(name: str) -> dict:
    """A session's evidence, running it (and the sessions before it) first; a failed session raises CheckFail."""
    if name not in _RUN:
        _RUN[name] = {"running": True}
        t0 = time.time()
        try:
            _RUN[name] = DRIVERS[name]() | {"ok": True}
        except CheckFail as e:
            _RUN[name] = {"ok": False, "error": str(e), "evidence": e.evidence}
        except Exception as e:  # noqa: BLE001
            import traceback
            _RUN[name] = {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()[-2500:]}
        _RUN[name]["wall_s"] = round(time.time() - t0, 1)   # includes the sessions it needed first
    ev = _RUN[name]
    if not ev.get("ok"):
        raise CheckFail(f"session {name} didn't complete: {ev.get('error')}", {"session": name, **ev})
    return ev


def _plain(ev: dict) -> dict:
    return {k: v for k, v in ev.items() if not k.startswith("_")}


# ---------------------------------------------------------------- RA: restore refusals before anything runs

def drive_ra() -> dict:
    ev: dict = {}
    BC.given_clear(ev)
    root = _tmp("rf")
    prefix = "hsbk_rf"
    ev["root"] = str(root)
    stack.mysql_up()
    try:
        ev["leftover_scratch_player"] = _heal_scratch_player()
        good = B.take("auto", P.TEST_USER, None, root)
        r1 = _tmp("r1")                    # backups.kill_safe's folder: its earlier backup (K is killed in R1)
        ev["_r1"] = r1
        ev["r1_earlier"] = B.take("auto", P.TEST_USER, None, r1)["id"]
        t = BC.seed_target(good, prefix)
        live_before, t_before = BC.live_stats(), BC.target_stats(t)
        bad = BC.clone_backup(good, root, datetime.now() - timedelta(hours=1), "auto")
        f = Path(bad["path"]) / "auth.sql.gz"
        data = bytearray(f.read_bytes())
        i = len(data) // 2
        data[i] ^= 0x01
        f.unlink()                                          # a clone shares blocks: write a new file, not in place
        f.write_bytes(bytes(data))
        ev["damaged"] = {"id": bad["id"], "file": "auth.sql.gz", "byte": i}
        stack.mysql_down()
        ids_before = BC.ids(root)
        runs = {}
        for name, args in (("damaged", ("restore", bad["id"], "--target", prefix)),
                           ("live target (--target live)", ("restore", good["id"], "--target", "live")),
                           ("live target (--target acore)", ("restore", good["id"], "--target", "acore")),
                           ("no target", ("restore", good["id"]))):
            samples: list = []
            stop = threading.Event()
            th = BC.watch_stack(samples, stop)
            r = BC.backups_cli(*args, root=root)
            stop.set()
            th.join(timeout=5)
            runs[name] = r | {"stack_seen": [s for s in samples if s][:3], "ids_after": BC.ids(root)}
        ev["refused_runs"] = runs
        ev["ids_before"] = ids_before
        stack.mysql_up()
        ev["damaged_scratch_differences"] = BC.diff_stats(t_before, BC.target_stats(t))[:10]
        ev["live_differences"] = BC.diff_stats(live_before, BC.live_stats())[:10]
        ev["_good"] = good
        ev["_prefix"] = prefix
        ev["_root"] = root
        return ev
    finally:
        stack.mysql_up()
        BC.drop_target(prefix, ev)
        stack.down()


def _heal_scratch_player() -> dict:
    """A scratch personal account left by an earlier run that died between N and N2 (mysqld is up): removed through
    the server (a full stack start, only in that case), so N's first-run GIVEN holds."""
    if not PC._acct_rows(PC.SCRATCH_PLAYER):
        return {"present": False}
    stack.up()
    try:
        return {"present": True, "removed": PC._delete_scratch(PC.SCRATCH_PLAYER)}
    finally:
        stack.down()
        stack.mysql_up()


# ---------------------------------------------------------------- N: the normal exit (offline, terminal, account run 1)

def drive_n() -> dict:
    from .polish_checks import GuiStandIn, Sampler, is_ours, lsappinfo_list, shim_log_lines, wait_window
    from .resolution_checks import player_config_sha, player_marker_exists
    _need("RA")
    ev: dict = {}
    require(os.access(PC.SANDBOX_EXEC, os.X_OK), "sandbox-exec not available on this macOS", **ev)
    before = remaining()
    ev["given_nothing_running"] = before
    require(is_clear(before), "GIVEN not met: something from this repo is already running", **ev)
    ev["real_player_account"] = PC._real_player_name()
    ev["real_player_file_before"] = play.PLAYER_FILE.read_bytes().hex() if play.PLAYER_FILE.exists() else None
    ev["player_config_before"] = {"sha256": player_config_sha(), "marker": player_marker_exists()}
    # play.offline's self-tests first (nothing of the loop running): the profile denies and logs, directly and via Wine
    tag = f"hs-offline-{os.getpid()}-{int(time.time())}"
    work = Path(tempfile.mkdtemp(prefix="hs-offline-")).resolve()
    prof = work / "offline.sb"
    prof.write_text(PC._profile(tag))
    ev |= {"profile_path": str(prof), "profile": prof.read_text(), "log_tag": tag}
    since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ev["_work"] = work
    probe = subprocess.Popen([PC.SANDBOX_EXEC, "-f", str(prof), "python3", "-c", PC._PROBE],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    probe_out = probe.communicate(timeout=60)[0]
    try:
        res = ast.literal_eval(probe_out.strip().splitlines()[-1])
    except (ValueError, SyntaxError, IndexError):
        res = {}
    ev["selftest_direct"] = {"pid": probe.pid, "exit": probe.returncode, "result": res or probe_out[-1000:]}
    rows = PC._wait_denials(tag, since, lambda rs: any(r["pid"] == probe.pid and r["verdict"] == "deny" for r in rs))
    ev["selftest_direct"]["denials"] = [r for r in rows if r["pid"] == probe.pid]
    ev["selftest_wine"] = PC._selftest_wine(prof, tag, since, work)
    # the loop window starts strictly after the self-tests' log lines (whole seconds in the log's timestamps)
    loop_since_t = time.time() + 1.0
    while time.time() < loop_since_t:
        time.sleep(0.1)
    loop_since = datetime.fromtimestamp(loop_since_t).strftime("%Y-%m-%d %H:%M:%S")
    time.sleep(1.0)

    s = GuiStandIn("term")
    ev["_s"] = s
    pfile = s.dir / "player.json"
    ev["_pfile"] = pfile
    # no backup folder: a backup prints a [play] line ("Backing up your save...") newer than play.stage_lines' base
    env = {"HS_PLAYER_FILE": str(pfile), "HS_PLAYER_DEFAULT_NAME": PC.SCRATCH_PLAYER}
    wlog = stack.LOG["worldserver"]
    lines = Sampler(lambda: S.count_lines(wlog), 0.1)
    t_start = time.time()
    try:
        lines.start()
        s.start_play("--allow-test-client-dir", wrap=[PC.SANDBOX_EXEC, "-f", str(prof)], account="player", env=env)
        ev["play_pid"] = s.proc.pid
        up = s.wait_client_up()
        ev["client_up"] = up
        require(up["up"], "the stand-in never came up under sandboxed ./play", output=s.output()[-3000:], **_plain(ev))
        t_up = time.time()
        win, t_win = wait_window(600, stop_if=lambda: s.proc.poll() is not None)
        ev |= {"window": win, "t_window": t_win}
        require(win, "the stand-in's window never appeared under ./play", output=s.output()[-3000:], **_plain(ev))
        # the Dock state settles (was a fixed 1.5 s): poll until the stand-in is listed as a Foreground app (or 5 s)
        t_ls = time.time()
        while True:
            ev["apps_at_window"] = [a for a in lsappinfo_list() if is_ours(a)]
            if any(a["pid"] == win["pid"] and a.get("type") == "Foreground" for a in ev["apps_at_window"]) \
                    or time.time() - t_ls > 5:
                break
            time.sleep(0.2)
        ev["shim_log"] = shim_log_lines(t_start, win["pid"])
        # play.offline: every long-running process of the loop runs under the profile (inherited from sandbox-exec)
        st = stack.stack_processes()
        members = ([{"pid": s.proc.pid, "what": "./play"}]
                   + [{"pid": pid, "what": k} for k, v in st.items() for pid in v]
                   + [{"pid": p["pid"], "what": p["cmd"][:80]} for p in launch.prefix_processes()])
        for m in members:
            m["sandboxed"] = PC._sandboxed(m["pid"]) == 1
            if not m["sandboxed"] and not stack.alive(m["pid"]):
                m["exited_before_check"] = True
        ev["loop_processes_sandboxed"] = members
        ev["stack_while_client_runs"] = {k: bool(v) for k, v in st.items()}
        ev["account_run1"] = _observe_account(pfile, s)
        while time.time() - t_up < BLOCKING_MIN_S:
            time.sleep(0.2)
        ev["play_blocking_while_client_runs"] = s.proc.poll() is None
        ev["blocking_observed_after_s"] = round(time.time() - t_up, 1)
        s.stop_file.write_text("quit")
        t_quit = time.time()
        rc, dt = s.wait_exit(timeout=300)
        lines.stop()
        rec = last_record(s.proc.pid)
        out = s.output()
        ev |= {"exit_code": rc, "exit_after_client_quit_s": dt, "output": out, "line_samples": lines.samples,
               "play_record": {k: rec.get(k) for k in ("outcome", "client_exit", "steps", "exit_code", "cleanup",
                                                       "account", "backup")},
               "graceful_halt": graceful_halt(t_start, rec), "remaining_after": remaining(s),
               "client_exit_to_play_exit_s": round(time.time() - t_quit, 1), "t_start": round(t_start, 3)}
        ev["account_run1"] |= {"exit_code": rc, "outcome": rec.get("outcome"),
                               "record_account": rec.get("account"),
                               "record_prepare_accounts": (rec.get("prepare") or {}).get("accounts"),
                               "output": human_lines(out)[-2500:],
                               "_last_text": play.LAST.read_text() if play.LAST.exists() else ""}
        time.sleep(SANDBOX_REPORTS_S)       # let the kernel's sandbox reports reach the log (absence is the claim)
        rows = PC._denials(tag, since)
        selftest_pids = ({probe.pid} | set(ev["selftest_wine"]["prefix_pids"])
                         | {r["pid"] for r in ev["selftest_wine"]["denials"]})
        in_loop = [r for r in rows if r["time"] and r["time"][:19].replace("T", " ") >= loop_since
                   and r["pid"] not in selftest_pids]
        ev["denial_log"] = {"query_since": since, "loop_window_since": loop_since, "all_rows": rows,
                            "loop_ip_denials": [r for r in in_loop if not r["dns"]],
                            "loop_dns_lookups_denied": [r for r in in_loop if r["dns"]]}
        return ev
    finally:
        lines.stop()
        s.kill_play()


def _observe_account(pfile: Path, s: StandIn) -> dict:
    """play.player_account's observations while the stack is up (it's down again after ./play exits)."""
    from .. import db
    run: dict = {}
    run["account_rows"] = PC._acct_rows(PC.SCRATCH_PLAYER)
    aid = run["account_rows"][0][0] if run["account_rows"] else "-1"
    run["account_access_rows"] = db.query(f"SELECT id, gmlevel, RealmID FROM account_access WHERE id={aid}", "auth")
    run["characters"] = db.query(f"SELECT guid, name FROM characters WHERE account={aid}", "characters")
    run["total_accounts"] = db.scalar("SELECT COUNT(*) FROM account", "auth")
    run["file_mode"] = oct(pfile.stat().st_mode & 0o777) if pfile.exists() else None
    run["file_bytes"] = pfile.read_text() if pfile.exists() else None
    run["file_mtime_ns"] = pfile.stat().st_mtime_ns if pfile.exists() else None
    return run


# ---------------------------------------------------------------- K: SIGKILL in the middle of the backup

def drive_k() -> dict:
    _need("N")
    r1 = _need("RA")["_r1"]
    ev: dict = {"root": str(r1)}
    listed = B.list_backups(r1)
    require(len(listed) == 1, "GIVEN not met: the scratch folder doesn't hold exactly one earlier backup",
            listed=[m["id"] for m in listed])
    early = listed[0]
    ev["earlier_backup"] = early["id"]
    ev["_early"] = early
    ev["_early_files"] = {rel: B.sha256_file(Path(early["path"]) / rel) for rel in early["files"]}
    s = StandIn("bkkill")
    try:
        s.start_play("--allow-test-client-dir",
                     env={"HS_BACKUP_DIR": str(r1), B.TEST_PAUSE_ENV: KILL_PAUSE_S, "HS_SETTINGS": _stock(s)})
        up = s.wait_client_up()
        ev["client_up"] = up
        require(up["up"], f"K: the stand-in never came up: {up.get('why')}", output=s.output()[-2000:], **_plain(ev))
        s.stop_file.write_text("quit")
        t0, tmp = time.time(), None
        while time.time() - t0 < 300 and s.proc.poll() is None:
            tmp = next((d for d in r1.iterdir() if d.name.startswith(".tmp-") and (d / "paused").exists()), None)
            if tmp:
                break
            time.sleep(0.2)
        require(tmp, "GIVEN not met: the backup never reached its mid-dump pause", output=s.output()[-2000:],
                **_plain(ev))
        ev["in_progress"] = {"tmp": tmp.name, "files": sorted(p.name for p in tmp.iterdir()), "play_pid": s.proc.pid}
        os.kill(s.proc.pid, signal.SIGKILL)               # the play loop only (as a crash or force quit would)
        s.proc.wait(timeout=30)
        ev["killed_pid"] = s.proc.pid
        ev["after_kill"] = {"listed": BC.ids(r1), "leftovers": BC.leftovers_in(r1), "stack": BC.stack_clear(),
                            "earlier_verify": B.verify(Path(early["path"])),
                            "lock_file": play.LOCK.read_text().strip() if play.LOCK.exists() else None}
        ev["after_kill"]["earlier_files_now"] = {rel: B.sha256_file(Path(early["path"]) / rel) for rel in early["files"]}
        ev["_tmp"] = tmp.name
        return ev
    finally:
        shutil.rmtree(s.dir, ignore_errors=True)          # never stop anything here: N2 is the next run


def _stock(s: StandIn) -> str:
    f = s.dir / "settings.json"
    f.write_text(json.dumps({"client": "stock"}))
    return str(f)


# ---------------------------------------------------------------- N2: the next run (K's leftovers), account run 2

def drive_n2() -> dict:
    k = _need("K")
    n = _need("N")
    ra = _need("RA")
    r1 = ra["_r1"]
    ev: dict = {"k_left_running": k["after_kill"]["stack"], "lock_file_before": k["after_kill"]["lock_file"]}
    first, second = StandIn("first"), StandIn("second")
    ev["_s"] = first
    pfile = n["_pfile"]                      # the same scratch player file as N's first run (N's stand-in folder)
    env = {"HS_PLAYER_FILE": str(pfile), "HS_PLAYER_DEFAULT_NAME": PC.SCRATCH_PLAYER, "HS_BACKUP_DIR": str(r1)}
    try:
        first.start_play("--allow-test-client-dir", account="player", env=env)
        ev["first_pid"] = first.proc.pid
        up = first.wait_client_up()
        ev["first_client_up"] = up
        require(up["up"], "N2 never got its client up (K's leftovers not healed?)", output=first.output()[-3000:],
                **_plain(ev))
        ev["leftovers_while_next_runs"] = BC.leftovers_in(r1)
        stack_before = stack.stack_processes()
        lock_before = play.LOCK.read_text().strip()
        ev["account_run2"] = _observe_account(pfile, first)
        # play.single_instance: a second ./play while N2 runs
        t0 = time.time()
        second.start_play()
        rc2, _ = second.wait_exit(timeout=30)
        dt2 = round(time.time() - t0, 1)
        ev |= {"lock_holder_before": lock_before, "second_exit_code": rc2, "second_elapsed_s": dt2,
               "second_output": second.output()[-2000:], "second_client_started": second.started.exists()}
        # backups.restore_refusals (b): a restore while N2 holds the play lock
        r2 = BC.backups_cli("restore", ra["_good"]["id"], "--target", ra["_prefix"], root=ra["_root"])
        ev["running_restore"] = r2
        ev["running_restore_safety_backups"] = [m["id"] for m in B.list_backups(ra["_root"])
                                                if m["kind"] == "before-restore"]
        ev |= {"first_running_after": first.proc.poll() is None,
               "first_client_after": play.client_processes(first.bat.name.lower()),
               "lock_holder_after": play.LOCK.read_text().strip(),
               "stack_before": stack_before, "stack_after": stack.stack_processes()}
        # the scratch personal account goes while N2's server runs (play.player_account's cleanup)
        ev["scratch_cleanup"] = PC._delete_scratch(PC.SCRATCH_PLAYER)
        first.stop_file.write_text("quit")
        rc1, dt1 = first.wait_exit(timeout=300)
        rec = last_record(first.proc.pid)
        out = first.output()
        ev |= {"first_exit_code": rc1, "first_exit_after_quit_s": dt1, "first_output": out[-2500:],
               "first_record": {k: rec.get(k) for k in ("lock", "outcome", "exit_code", "backup", "backup_plan")},
               "remaining_after": remaining(first)}
        ev["account_run2"] |= {"exit_code": rc1, "outcome": rec.get("outcome"), "record_account": rec.get("account"),
                               "record_prepare_accounts": (rec.get("prepare") or {}).get("accounts"),
                               "output": human_lines(out)[-2500:],
                               "_last_text": play.LAST.read_text() if play.LAST.exists() else ""}
        listed = B.list_backups(r1)
        ev["next"] = {"listed": [m["id"] for m in listed], "leftovers": BC.leftovers_in(r1),
                      "verify": {m["id"]: B.verify(Path(m["path"]), m)["ok"] for m in listed},
                      "record_backup": rec.get("backup"), "record_backup_plan": rec.get("backup_plan")}
        return ev
    finally:
        second.cleanup()
        first.kill_play()


# ---------------------------------------------------------------- I, C, F: interrupt, crash, startup failure (R2)

def _ending_session(tag: str, root: Path, knobs: dict) -> tuple[StandIn, dict]:
    s = StandIn(tag)
    env = {"HS_BACKUP_DIR": str(root), "HS_SETTINGS": _stock(s), **knobs}
    ev: dict = {"t_start": time.time()}
    s.start_play("--allow-test-client-dir", env=env)
    ev["play_pid"] = s.proc.pid
    up = s.wait_client_up()
    ev["client_up"] = up
    return s, ev


def _ending_result(s: StandIn, ev: dict) -> dict:
    out = s.output()
    rec = last_record(s.proc.pid)
    return ev | {"output": out[-3000:], "human_output": human_lines(out)[-2500:],
                 "shutdown_events": BC.shutdown_events(out),
                 "notices": [l for l in out.splitlines() if l.startswith("[notice] ")],
                 "record": rec, "record_backup": rec.get("backup"),
                 "graceful_halt": graceful_halt(ev["t_start"], rec), "remaining_after": remaining(s),
                 "stand_in_was_told_to_stop": s.stop_file.exists()}


def drive_i() -> dict:
    _need("KR")
    given = remaining()
    require(is_clear(given), "GIVEN not met: something from this repo is already running", given=given)
    r2 = _tmp("r2")
    s, ev = _ending_session("sigint", r2, {B.TEST_FAIL_ENV: "dump"})
    ev["_r2"] = r2
    try:
        require(ev["client_up"]["up"], "I: the stand-in never came up under ./play", output=s.output()[-3000:])
        t_sig = time.time()
        os.killpg(s.proc.pid, signal.SIGINT)              # Ctrl-C: to the whole process group, as a terminal does
        time.sleep(1)
        second_sent = s.proc.poll() is None
        if second_sent:
            os.killpg(s.proc.pid, signal.SIGINT)          # idempotence: must not abort or double-kill the cleanup
        ev["second_signal_sent_during_shutdown"] = second_sent
        rc, _ = s.wait_exit(timeout=300)
        ev |= {"exit_code": rc, "exit_after_signal_s": round(time.time() - t_sig, 1)}
        return _ending_result(s, ev) | {"listed": BC.ids(r2), "leftovers": BC.leftovers_in(r2)}
    finally:
        s.cleanup()


def drive_c() -> dict:
    i = _need("I")
    r2 = i["_r2"]
    given = remaining()
    require(is_clear(given), "GIVEN not met: something from this repo is already running", given=given)
    s, ev = _ending_session("crash", r2, {B.TEST_FREE_ENV: str(1 << 20)})
    try:
        require(ev["client_up"]["up"], "C: the stand-in never came up under ./play", output=s.output()[-3000:])
        victims = play.client_processes(s.bat.name.lower())
        ev["killed"] = victims
        for v in victims:
            os.kill(v["pid"], signal.SIGKILL)
        t_kill = time.time()
        rc, _ = s.wait_exit(timeout=300)
        ev |= {"exit_code": rc, "exit_after_kill_s": round(time.time() - t_kill, 1)}
        return _ending_result(s, ev) | {"listed": BC.ids(r2), "leftovers": BC.leftovers_in(r2)}
    finally:
        s.cleanup()


def drive_f() -> dict:
    c = _need("C")
    r2 = _need("I")["_r2"]
    bad = "/nonexistent/hs-play-failfast"
    given = remaining()
    require(is_clear(given), "GIVEN not met: something from this repo is already running", given=given)
    s = StandIn("fail")
    wine_log_offset = P.WINE_LOG.stat().st_size if P.WINE_LOG.exists() else 0
    ev: dict = {"cmd": f"{PLAY} --account test --client-cmd <stand-in .bat> --world-datadir {bad}", "t_start": time.time()}
    try:
        s.start_play("--allow-test-client-dir", "--world-datadir", bad,
                     env={"HS_BACKUP_DIR": str(r2), "HS_SETTINGS": _stock(s)})
        ev["play_pid"] = s.proc.pid
        rc, dt = s.wait_exit(timeout=900)
        try:
            with open(P.WINE_LOG, errors="replace") as fh:
                fh.seek(wine_log_offset)
                launches = [l.strip() for l in fh if "[hs launch]" in l and " start " in l]
        except FileNotFoundError:
            launches = []
        out = s.output()
        ev |= {"exit_code": rc, "elapsed_s": dt, "client_start_stamps_in_wine_log": launches,
               "stand_in_started_marker": s.started.exists(),
               "excerpt": out.split("server log excerpt", 1)[-1] if "server log excerpt" in out else ""}
        res = _ending_result(s, ev)
        return res | {"listed": BC.ids(r2), "leftovers": BC.leftovers_in(r2), "c_error": (c["record_backup"] or {}).get("error")}
    finally:
        s.cleanup()


# ---------------------------------------------------------------- KR: the killed restore (backups.kill_safe, 2nd half)

def drive_kr() -> dict:
    _need("N2")
    if any(stack.stack_processes().values()):
        stack.down()
    holder: dict = {}
    try:
        out = BC.killed_restore(holder)
    except CheckFail as e:
        raise CheckFail(str(e), {"killed_restore": holder.get("killed_restore") or e.evidence})
    return {"killed_restore": out}


DRIVERS = {"RA": drive_ra, "N": drive_n, "K": drive_k, "N2": drive_n2, "KR": drive_kr, "I": drive_i, "C": drive_c,
           "F": drive_f}


# ---------------------------------------------------------------- the scenarios (each the THEN of its former check)

def offline(ctx):
    """The whole play loop runs under a sandbox-exec profile that denies (and logs) every non-loopback outbound
    connection for ./play and all its children; startup, client launch and shutdown succeed, and the denial log
    shows no attempt from the loop. The deny is proven live first: a non-loopback connect under the same profile,
    directly and through Wine, is refused and logged."""
    n = _need("N")
    ev = {k: n.get(k) for k in ("profile", "log_tag", "selftest_direct", "selftest_wine", "loop_processes_sandboxed",
                                "client_up", "exit_code", "exit_after_client_quit_s", "play_record", "graceful_halt",
                                "remaining_after", "denial_log", "stack_while_client_runs")}
    res = (n["selftest_direct"]["result"] if isinstance(n["selftest_direct"]["result"], dict) else {})
    require(res and all(res.get(h, "").startswith("PermissionError errno=1") for h in ("1.1.1.1", "2606:4700:4700::1111")),
            "self-test: non-loopback connects (IPv4 + IPv6) not refused with EPERM", **ev)
    require(res.get("127.0.0.1:9", "").startswith("ConnectionRefusedError"),
            "self-test: loopback isn't reachable under the profile (expected 'connection refused' from a closed port)", **ev)
    require(any(r["op"] == "network-outbound" and not r["dns"] for r in n["selftest_direct"]["denials"]),
            "self-test: the refused connection isn't in the denial log", **ev)
    require(n["selftest_wine"]["denials"], "self-test: a Wine process's non-loopback attempt wasn't denied+logged", **ev)
    require(not n["selftest_wine"]["replies"], "self-test: Wine's ping got replies from 1.1.1.1 (the network isn't blocked)", **ev)
    members = n["loop_processes_sandboxed"]
    require(all(n["stack_while_client_runs"].values()), "stack not fully up while the client runs", **ev)
    require(all(m["sandboxed"] or m.get("exited_before_check") for m in members),
            "a loop process isn't running under the deny profile", **ev)
    require(sum(m["sandboxed"] for m in members) >= 4 + len(n["client_up"]["client_processes"]),
            "too few sandboxed loop processes found (./play + 3 stack + client expected)", **ev)
    rec = n["play_record"]
    require(n["exit_code"] == 0, f"sandboxed ./play exit code {n['exit_code']}, expected 0", **ev)
    require(rec.get("outcome") == "client_exit", "sandboxed loop didn't complete normally", **ev)
    require(n["graceful_halt"]["ok"], "sandboxed loop didn't shut the stack down gracefully", **ev)
    require(is_clear(n["remaining_after"]), "something from this repo is still running", **ev)
    require(not n["denial_log"]["loop_ip_denials"], "a process of the loop attempted a non-loopback connection", **ev)
    return ev


def loop_clean_exit(ctx):
    """`./play` blocks while the client runs; when the client exits normally ./play exits 0 within 120 s, worldserver
    logged a graceful halt, and no stack/Wine/Wow.exe process remains, ports 3307/3724/8085/7878 free."""
    n = _need("N")
    ev = {k: n.get(k) for k in ("given_nothing_running", "play_pid", "client_up", "play_blocking_while_client_runs",
                                "blocking_observed_after_s", "exit_code", "exit_after_client_quit_s", "play_record",
                                "graceful_halt", "remaining_after", "client_exit_to_play_exit_s")}
    ev["output"] = n["output"][-3000:]
    rec = n["play_record"]
    require(is_clear(n["given_nothing_running"]), "GIVEN not met: something from this repo was already running", **ev)
    require(n["play_blocking_while_client_runs"] and n["blocking_observed_after_s"] >= BLOCKING_MIN_S,
            "./play exited while the client was still running", **ev)
    require(n["exit_code"] == 0, f"./play exit code {n['exit_code']}, expected 0", **ev)
    require(n["exit_after_client_quit_s"] <= 120, f"./play took {n['exit_after_client_quit_s']}s after the client "
            "exited (limit 120 s)", **ev)
    require(rec.get("outcome") == "client_exit" and (rec.get("client_exit") or {}).get("code") == 0,
            "./play didn't record a normal client exit", **ev)
    require(n["graceful_halt"]["ok"], "worldserver didn't log a graceful halt for this run", **ev)
    require(is_clear(n["remaining_after"]), "something from this repo is still running", **ev)
    return ev


def _term_ev(n: dict) -> dict:
    return {"window": n["window"], "apps_at_window": n["apps_at_window"], "shim_log": n["shim_log"],
            "output": n["output"], "exit_code": n["exit_code"], "remaining_after": n["remaining_after"],
            "line_samples": n["line_samples"]}


def dock_identity(ctx):
    """Terminal ./play with the notepad stand-in (no app): the stand-in is a normal Foreground app named Moonglade,
    and the shim log records the icon replacement for its pid in this run."""
    from .polish_checks import verify_dock_identity
    return verify_dock_identity(_term_ev(_need("N")))


def stage_lines(ctx):
    """./play in a terminal (stand-in): every [play] line matches a [play] line of the play loop before
    launcher-polish (unchanged), and every [stage] line parses as JSON with a documented id and state. (The existing
    play.* checks run in the same harness run.)"""
    from .polish_checks import base_play_templates, verify_stage_lines
    n = _need("N")
    out = verify_stage_lines(n["output"], base_play_templates())
    require(n["exit_code"] == 0 and is_clear(n["remaining_after"]), "./play didn't end cleanly",
            exit_code=n["exit_code"], **out)
    return out


def world_progress(ctx):
    """Cold worldserver start under ./play: at least three distinct world fractions strictly between 0 and 1 come
    before the world is done, each = worldserver.out's line count / the calibration, and that count matches the log's
    own line count (sampled independently at 10 Hz) within one poll (0.5 s)."""
    from .polish_checks import verify_world_progress
    n = _need("N")
    return verify_world_progress(n["output"], n["line_samples"])


def kill_safe(ctx):
    """An earlier backup in the scratch folder (taken in RA); a session whose backup is paused mid-dump (after the first
    database, test knob) is SIGKILLed: no partial backup is listed, the earlier backup's files still match their
    checksums (and are unchanged), and the next session (N2) removes the leftover temp folder, exits 0, adds a complete
    backup and leaves nothing running. Then "Killed in the middle of a restore" (killed_restore): a test restore into
    scratch schemas killed while loading is finished by the next test-shaped ./play; live tables never change."""
    k = _need("K")
    ev = {k_: v for k_, v in k.items() if not k_.startswith("_") and k_ != "ok"}
    early = k["_early"]
    require(ev["after_kill"]["listed"] == [early["id"]], "a partial backup is listed after the kill", **ev)
    require(ev["after_kill"]["earlier_verify"]["ok"] and ev["after_kill"]["earlier_files_now"] == k["_early_files"],
            "the earlier backup's files changed or don't match their checksums", **ev)
    require(k["_tmp"] in ev["after_kill"]["leftovers"], "GIVEN not met: the kill left no temp folder to heal", **ev)
    n2 = _need("N2")
    ev["next"] = {x: n2.get(x) for x in ("first_exit_code", "remaining_after", "leftovers_while_next_runs")} | n2["next"]
    stale = ((n2["next"]["record_backup_plan"] or {}).get("stale_removed") or [])
    require(any(x.get("dir") == k["_tmp"] and x.get("removed") for x in stale),
            "the next run didn't remove the leftover temp folder at its start", **ev)
    require(n2["first_exit_code"] == 0 and is_clear(n2["remaining_after"]), "the next run didn't end normally", **ev)
    listed = n2["next"]["listed"]
    require(len(listed) == 2 and early["id"] in listed and all(n2["next"]["verify"].values())
            and not n2["next"]["leftovers"], "the next run didn't add one complete backup next to the earlier one", **ev)
    ev["killed_restore"] = _need("KR")["killed_restore"]
    return ev


def single_instance(ctx):
    """While ./play runs, a second ./play exits nonzero within 5 s naming the first one's pid; the first is unaffected
    (keeps running, then exits 0 normally). The first also reclaims a stale lock (a dead pid: the play loop K
    SIGKILLed)."""
    k = _need("K")
    n2 = _need("N2")
    ev = {x: n2.get(x) for x in ("lock_file_before", "first_pid", "first_client_up", "lock_holder_before",
                                  "second_exit_code", "second_elapsed_s", "second_output", "second_client_started",
                                  "first_running_after", "first_client_after", "lock_holder_after", "stack_before",
                                  "stack_after", "first_exit_code", "first_exit_after_quit_s", "first_output",
                                  "first_record", "remaining_after")}
    ev["stale_lock_pid_planted"] = k["killed_pid"]
    pid = n2["first_pid"]
    require(n2["lock_file_before"] == str(k["killed_pid"]), "GIVEN not met: the lock file doesn't hold the killed "
            "loop's pid", **ev)
    require(n2["lock_holder_before"] == str(pid), "lock file doesn't hold the first ./play's pid", **ev)
    rc2 = n2["second_exit_code"]
    require(rc2 is not None and rc2 != 0, f"second ./play exit code {rc2}, expected nonzero", **ev)
    require(n2["second_elapsed_s"] < 5, f"second ./play took {n2['second_elapsed_s']}s to refuse (limit 5 s)", **ev)
    require(re.search(rf"\b{pid}\b", n2["second_output"]), "second ./play's message doesn't name the running pid", **ev)
    require(not n2["second_client_started"], "second ./play started a client", **ev)
    require(n2["first_running_after"] and n2["first_client_after"], "first ./play or its client was affected", **ev)
    require(n2["stack_after"] == n2["stack_before"] and n2["lock_holder_after"] == str(pid),
            "the stack or the lock changed under the first ./play", **ev)
    rec = n2["first_record"]
    require((rec.get("lock") or {}).get("reclaimed_stale_pid") == str(k["killed_pid"]),
            "first ./play didn't record reclaiming the stale lock", **ev)
    require(n2["first_exit_code"] == 0 and rec.get("outcome") == "client_exit", "first ./play didn't finish normally",
            **ev)
    require(is_clear(n2["remaining_after"]), "something from this repo is still running", **ev)
    return ev


def player_account(ctx):
    """Default (personal-account) path with a scratch player file: the first ./play (N) writes the file (0600) and
    creates exactly one non-GM account with no characters and prints the login; a second ./play (N2) creates nothing
    and keeps the account id; the password is never in play-last.json; HSTEST / HS_GM in the file are refused; the
    real env/player.json and the player's play client config are unchanged."""
    from .resolution_checks import player_config_sha, player_marker_exists
    n = _need("N")
    n2 = _need("N2")
    ev: dict = {"scratch_account": PC.SCRATCH_PLAYER, "real_player_file": str(play.PLAYER_FILE),
                "real_player_account": n["real_player_account"], "player_config_before": n["player_config_before"]}
    require(PC.SCRATCH_PLAYER != ev["real_player_account"], "scratch account name collides with the real player account",
            **ev)
    r1, r2 = dict(n["account_run1"]), dict(n2["account_run2"])
    runs = [r1, r2]
    for r, ok_up in ((r1, n["client_up"]["up"]), (r2, n2["first_client_up"]["up"])):
        require(ok_up and r["exit_code"] == 0, "a run of ./play didn't complete normally", runs=runs, **ev)
    ev["player_config_after"] = {"sha256": player_config_sha(), "marker": player_marker_exists()}
    require(ev["player_config_after"] == ev["player_config_before"],
            "the player-account run changed the player's own play client config (resolution default)", **ev)
    pw = json.loads(n["_pfile"].read_text())["password"]
    for r in runs:
        r["password_in_play_last_json"] = pw in r.pop("_last_text")
    ev["runs"] = runs
    require(r1["file_mode"] == "0o600", "player file not mode 0600", **ev)
    require(len(r1["account_rows"]) == 1, "first run: expected exactly one scratch account", **ev)
    require(not any(int(row[1]) > 0 for row in r1["account_access_rows"]), "account has a GM security level", **ev)
    require(r1["characters"] == [], "new personal account has characters", **ev)
    require((r1["record_prepare_accounts"] or {}).get("created") is True
            and (r1["record_account"] or {}).get("file_created") is True, "first run didn't record creating file + account", **ev)
    require(PC.SCRATCH_PLAYER in r1["output"] and pw in r1["output"], "first run didn't print the login", **ev)
    require(r2["account_rows"] == r1["account_rows"], "second run: account id/rows changed", **ev)
    require(r2["total_accounts"] == r1["total_accounts"], "second run created another account", **ev)
    require(r2["file_bytes"] == r1["file_bytes"] and r2["file_mtime_ns"] == r1["file_mtime_ns"],
            "second run rewrote the player file", **ev)
    require((r2["record_prepare_accounts"] or {}).get("created") is False
            and (r2["record_account"] or {}).get("file_created") is False, "second run recorded creating something", **ev)
    require(not any(r["password_in_play_last_json"] for r in runs), "the password appears in play-last.json", **ev)
    # reserved names are refused (before anything starts)
    ev["reserved"] = {}
    st = StandIn("reserved")
    tmp = st.dir
    try:
        for name in (P.TEST_USER, P.GM_USER):
            bad = tmp / f"reserved-{name}.json"
            bad.write_text(json.dumps({"account": name, "password": "whatever1"}))
            r = subprocess.run([PLAY, "--account", "player", "--client-cmd", str(st.bat)], cwd=P.ROOT,
                               env={**os.environ, **PINNED, "HS_PLAYER_FILE": str(bad)}, capture_output=True, text=True,
                               timeout=300)
            ev["reserved"][name] = {"exit": r.returncode, "output": (r.stdout + r.stderr)[-800:]}
            require(r.returncode != 0 and f"{name} is reserved for the test harness" in r.stdout,
                    f"{name} in the player file wasn't refused", **ev)
    finally:
        st.cleanup()
    require(is_clear(remaining()), "something from this repo is still running", **ev)
    real_after = play.PLAYER_FILE.read_bytes().hex() if play.PLAYER_FILE.exists() else None
    require(real_after == n["real_player_file_before"], "the real env/player.json changed", **ev)
    ev["real_player_file_unchanged"] = True
    ev["scratch_cleanup"] = n2["scratch_cleanup"]
    require(n2["scratch_cleanup"].get("deleted") is True, "the scratch personal account wasn't removed", **ev)
    return ev


def restore_refusals(ctx):
    """(a) A backup with one byte of one dump changed: the restore (into scratch schemas) exits nonzero naming the
    damaged file, takes no safety backup, starts nothing, and no scratch or live table changes. (a2) A test-shaped
    restore naming the live schemas (`--target live`, `--target acore`) and one naming no target are refused with a
    nonzero exit before anything starts, with no safety backup and every live table unchanged. (b) With a play session
    running (N2, the play lock held): the restore exits nonzero with "already running", and the session (client,
    stack, lock) continues unaffected and later exits 0."""
    ra = _need("RA")
    ev = _plain({k: v for k, v in ra.items() if k != "ok"})
    runs = ra["refused_runs"]
    for name, r in runs.items():
        js = r["json"] or {}
        require(r["rc"] != 0 and not js.get("ok"), f"{name}: the restore wasn't refused", **ev)
        require(not r["stack_seen"], f"{name}: something was started for the refused restore", **ev)
        require(r["ids_after"] == ra["ids_before"], f"{name}: a backup (safety or other) appeared", **ev)
    require("auth.sql.gz" in str((runs["damaged"]["json"] or {}).get("error")),
            "the damaged-backup refusal doesn't name the damaged file", **ev)
    require(not ra["damaged_scratch_differences"], "a scratch table changed although the restore was refused", **ev)
    require(not ra["live_differences"], "a live table changed", **ev)
    n2 = _need("N2")
    r2 = n2["running_restore"]
    js2 = r2["json"] or {}
    ev["running_restore"] = r2 | {x: n2.get(x) for x in ("stack_before", "stack_after", "lock_holder_before",
                                                         "lock_holder_after", "first_client_after", "first_pid",
                                                         "first_running_after")}
    require(r2["rc"] != 0 and "already running" in str(js2.get("error")), "the restore wasn't refused with "
            "\"already running\"", **ev)
    require(n2["stack_after"] == n2["stack_before"] and n2["lock_holder_after"] == n2["lock_holder_before"]
            == str(n2["first_pid"]) and n2["first_client_after"] and n2["first_running_after"],
            "the running session was affected", **ev)
    require(not n2["running_restore_safety_backups"], "a safety backup was taken", **ev)
    ev["session_end"] = {"exit_code": n2["first_exit_code"], "remaining_after": n2["remaining_after"]}
    require(n2["first_exit_code"] == 0 and is_clear(n2["remaining_after"]), "the session didn't end normally", **ev)
    return ev


def interrupt_clean_exit(ctx):
    """SIGINT (Ctrl-C to the process group) to ./play with the client up: the client is closed, the stack shuts down
    gracefully, ./play exits within 120 s, nothing from this repo remains. A repeated signal during shutdown is
    ignored. (SIGTERM takes the same handler: unit:py:test_play_signals.)"""
    i = _need("I")
    rec = i["record"]
    clean = rec.get("cleanup") or {}
    ev = {"signal": "SIGINT", **{k: i.get(k) for k in ("play_pid", "client_up", "second_signal_sent_during_shutdown",
                                                       "exit_code", "exit_after_signal_s", "output", "graceful_halt",
                                                       "remaining_after", "stand_in_was_told_to_stop")}}
    ev["play_record"] = {k: rec.get(k) for k in ("outcome", "signal", "steps", "exit_code", "cleanup")}
    require(i["exit_code"] is not None, "./play didn't exit within 300 s of the signal", **ev)
    require(i["exit_after_signal_s"] <= 120, f"./play took {i['exit_after_signal_s']}s to exit after SIGINT "
            "(limit 120 s)", **ev)
    require(rec.get("outcome") == "interrupted" and (rec.get("signal") or {}).get("name") == "SIGINT",
            "./play didn't record the SIGINT interrupt", **ev)
    require("stop_prefix" in clean and not clean["stop_prefix"].get("sigkilled")
            and not clean["stop_prefix"].get("remaining_after_k"),
            "./play didn't close the client cleanly (wineserver -k)", **ev)
    require(not i["stand_in_was_told_to_stop"] and not i["remaining_after"].get("stand_in_processes"),
            "client not closed by ./play", **ev)
    require(i["graceful_halt"]["ok"], "stack didn't shut down gracefully", **ev)
    require(is_clear(i["remaining_after"]), "something from this repo is still running", **ev)
    return ev


def client_crash_cleanup(ctx):
    """SIGKILL the client's Wine process: ./play detects it, shuts the stack down gracefully, exits nonzero with a
    message naming the client crash, and nothing from this repo remains."""
    c = _need("C")
    rec = c["record"]
    out = c["output"]
    crash_lines = [l for l in out.splitlines() if "crash" in l.lower()]
    ev = {k: c.get(k) for k in ("play_pid", "client_up", "killed", "exit_code", "exit_after_kill_s", "output",
                                "graceful_halt", "remaining_after")}
    ev |= {"crash_message_lines": crash_lines,
           "play_record": {k: rec.get(k) for k in ("outcome", "client_exit", "crash_message", "steps", "exit_code",
                                                   "cleanup")}}
    rc = c["exit_code"]
    require(c["killed"], "no client process found to kill", **ev)
    require(rc is not None and rc != 0, f"./play exit code {rc}, expected nonzero", **ev)
    require(rec.get("outcome") == "client_crash", "./play didn't detect the client crash", **ev)
    require(any("client crashed" in l.lower() or "client crash" in l.lower() for l in crash_lines),
            "./play's output doesn't name the client crash", **ev)
    require(c["graceful_halt"]["ok"], "stack didn't shut down gracefully", **ev)
    require(is_clear(c["remaining_after"]), "something from this repo is still running", **ev)
    return ev


def startup_failure(ctx):
    """With an unusable worldserver DataDir ./play exits nonzero with the server's log excerpt, never starts the
    client, and leaves nothing running."""
    f = _need("F")
    rec = f["record"]
    ev = {k: f.get(k) for k in ("cmd", "play_pid", "exit_code", "elapsed_s", "output", "client_start_stamps_in_wine_log",
                                "stand_in_started_marker", "remaining_after")}
    ev["play_record"] = {k: rec.get(k) for k in ("outcome", "error", "client_started", "steps", "exit_code", "cleanup")}
    rc = f["exit_code"]
    require(rc is not None and rc != 0, f"./play exit code {rc}, expected nonzero", **ev)
    # the cause worldserver logs for a missing DataDir (World.cpp, LOG_ERROR "server.loading")
    require("Failed to find map files" in f["excerpt"], "./play's output lacks the server log excerpt showing the cause",
            **ev)
    require(rec.get("outcome") == "startup_failed" and rec.get("client_started") is False,
            "./play didn't record a startup failure before the client", **ev)
    require(not f["stand_in_started_marker"] and not f["client_start_stamps_in_wine_log"],
            "the client was started despite the failed startup", **ev)
    require(is_clear(f["remaining_after"]), "something from this repo is still running", **ev)
    return ev


# the exit code each ending gives without any backup involved (play.py: EXIT_OK; 128 + SIGINT; EXIT_CRASH)
ENDING_EXIT = {"I": 128 + signal.SIGINT, "C": play.EXIT_CRASH}


def verify_failed_session(res: dict, root_ids: list, leftovers: list, expect: str, ending: str) -> dict:
    """backups.failure_nonblocking for one session: the backup failure changed nothing about how the session ended
    (its exit code is the one its ending gives: 130 for SIGINT, the crash code for a client crash), the stack is fully
    down, no backup is listed, no temporary files remain, play-last.json records the failure and why, and the
    shutdown_backup stage is 'failed'."""
    out = {"exit_code": res["exit_code"], "expected_exit_code": ENDING_EXIT[ending], "record_backup": res["record_backup"],
           "shutdown_backup": [e for e in res["shutdown_events"] if e["id"] == "shutdown_backup"],
           "remaining_after": res["remaining_after"], "listed": root_ids, "leftovers": leftovers}
    require(res["exit_code"] == ENDING_EXIT[ending], f"./play exited {res['exit_code']}, not its ending's "
            f"{ENDING_EXIT[ending]} (a failed backup must not change how shutdown ends)", **out)
    require(is_clear(res["remaining_after"]), "the stack isn't fully down after a failed backup", **out)
    require(not root_ids, "a backup is listed although it failed", **out)
    require(not leftovers, "temporary backup files remain", **out)
    rb = res["record_backup"] or {}
    require(rb.get("ok") is False and expect in str(rb.get("error")), f"play-last.json doesn't record the failure "
            f"({expect!r})", **out)
    require(any(e["state"] == "failed" for e in out["shutdown_backup"]), "the shutdown_backup stage isn't 'failed'", **out)
    return out


def failure_nonblocking(ctx):
    """An injected dump failure (session I, SIGINT), then an injected low-disk reading of 1 MB free (session C, client
    crash): each session still ends the way its ending dictates with the stack fully down, no backup listed, no
    temporary files, the failure (and why) in play-last.json and the shutdown_backup stage 'failed'; the next launch
    prints a `[notice] Last backup failed ...` line naming that reason (C's start for the first failure, the launch F
    for the second)."""
    i, c, f = _need("I"), _need("C"), _need("F")
    ev: dict = {}
    ev["dump_failure"] = {k: i.get(k) for k in ("exit_code", "record_backup", "notices", "remaining_after")}
    # I's listing is taken right after I (before C and F wrote to the same folder)
    ev["dump_failure"]["verify"] = verify_failed_session(i, i["listed"], i["leftovers"], "mysqldump", "I")
    ev["low_disk"] = {k: c.get(k) for k in ("exit_code", "record_backup", "notices", "remaining_after")}
    ev["low_disk"]["verify"] = verify_failed_session(c, c["listed"], c["leftovers"], "not enough free disk space", "C")
    err1 = (i["record_backup"] or {}).get("error")
    ev["notice_for_failure_1"] = c["notices"]
    require(err1 and any("Last backup failed" in n and err1 in n for n in c["notices"]),
            "the next launch didn't print a \"last backup failed\" notice naming the dump failure", **ev)
    err2 = (c["record_backup"] or {}).get("error")
    ev["next_launch"] = {k: f.get(k) for k in ("exit_code", "notices", "remaining_after")}
    ev["next_launch"]["record_outcome"] = (f.get("record") or {}).get("outcome")
    require(err2 and any("Last backup failed" in n and err2 in n for n in f["notices"]),
            "the next launch didn't print a \"last backup failed\" notice naming the low-disk reason", **ev)
    require(is_clear(f["remaining_after"]) and not f["listed"], "the further launch left something behind", **ev)
    return ev


STEPS = (("play.offline", offline), ("play.loop_clean_exit", loop_clean_exit), ("play.dock_identity", dock_identity),
         ("play.stage_lines", stage_lines), ("play.world_progress", world_progress),
         ("backups.kill_safe", kill_safe), ("play.single_instance", single_instance),
         ("play.player_account", player_account), ("backups.restore_refusals", restore_refusals),
         ("play.interrupt_clean_exit", interrupt_clean_exit), ("play.client_crash_cleanup", client_crash_cleanup),
         ("play.startup_failure", startup_failure), ("backups.failure_nonblocking", failure_nonblocking))
CHILDREN = tuple(i for i, _ in STEPS)

COVERS = (
    "play",
    "harness/hslib/play.py",
    "harness/hslib/backups.py",
    "harness/hslib/stages.py",
    "harness/hslib/checks/cold_play_checks.py",
    "harness/hslib/checks/play_checks.py",
    "harness/hslib/checks/backup_checks.py",
    "harness/hslib/checks/polish_checks.py",
    "harness/hslib/checks/resolution_checks.py",
)


@check("play.cold_sessions", order=300, capability=CAP, stack="down", layer="integration", targeted=True, covers=COVERS,
       children=CHILDREN)
def cold_sessions(ctx):
    """The play loop's own start and stop in its distinct endings (normal exit from nothing running, SIGKILL during
    the backup, the next run after it, SIGINT, a client crash, a failed server start), each scenario asserting its
    THEN on the session that meets its GIVEN (the scenarios in CHILDREN, each its own record); nothing of this repo
    left running at the end."""
    _RUN.clear()
    try:
        out = run_children(ctx, list(STEPS))
    finally:
        sessions = {k: {"ok": v.get("ok"), "wall_s": v.get("wall_s"), **({"error": v.get("error")} if not v.get("ok") else {})}
                    for k, v in _RUN.items()}
        n = _RUN.get("N") or {}
        if n.get("_work"):
            shutil.rmtree(n["_work"], ignore_errors=True)
        for key in ("N", "N2"):
            s = (_RUN.get(key) or {}).get("_s")
            if s is not None:
                s.cleanup()
        ok_all = all(v.get("ok") for v in _RUN.values())
        if any(stack.stack_processes().values()):
            stack.down()
        if launch.prefix_processes():
            launch.stop_prefix()
        for key, root_key in (("RA", "_r1"), ("I", "_r2"), ("RA", "_root")):
            r = (_RUN.get(key) or {}).get(root_key)
            if r is not None and ok_all:
                BC.drop(r)
        _RUN.clear()
    out["sessions"] = sessions
    out["remaining_after"] = remaining()
    require(is_clear(out["remaining_after"]), "something from this repo is still running after the sessions", **out)
    return out
