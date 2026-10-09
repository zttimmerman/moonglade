"""saves-channel: the launcher's control listener and the front menu's requests to it (openspec/changes/saves-channel).

Offline (no client): savesctl.listener drives harness/hslib/savesctl.py's Listener over loopback with recorded-layout
3.3.5a logon challenges (savesctl.challenge, the packet probe.client.logon sends). savesctl.test_actions_refused runs
a normal ./play with the control test switches set. savesctl.hygiene runs ./play with the stand-in client
(play_checks.StandIn) against a scratch play client: the real listener inside ./play, lsof, SIGKILL, the next run.

Live (real Wow.exe, scratch play client, test account HSTEST; never the player's): one shared ./play run whose
GlueXML test hook sends `ctl:S` requests from the title menu. Evidence: the listener's own log (env/logs/
savesctl.log), the glue's compact test record (HSSeamless.lua; "K" tokens, handed out through WTF/Config.wtf when the
character enters the world), the client's screenshots (marker squares, second row: control state and stock dialog),
its Logs/connection.log, the authserver's Auth.log and the server's Players.log.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from .. import frontmenu, mpq, paths as P, play, savesctl
from ..registry import check, require
from .play_checks import PINNED, PLAY, StandIn, is_clear, remaining, require_given_clear

CAP = "saves-channel"


# ---------------------------------------------------------------- offline: the listener

def _exchange(port: int, data: bytes, half_close: bool = False, client_closes: bool = True,
              timeout: float = 4.0) -> dict:
    """Send `data` and read until the listener closes: {"reply": hex, "closed", "after_reply_s", "error"}.
    half_close: end our side right after sending (a truncated packet). client_closes: once 3 reply bytes are in, end our
    side, as the real client does after a failed challenge (the listener then closes at once instead of lingering)."""
    out: dict = {"sent": len(data)}
    buf = b""
    t_reply = None
    with socket.create_connection((savesctl.HOST, port), timeout=timeout) as c:
        try:
            c.sendall(data)
            if half_close:
                c.shutdown(socket.SHUT_WR)
            while True:
                chunk = c.recv(4096)
                if not chunk:
                    out["closed"] = True
                    break
                buf += chunk
                if len(buf) >= 3 and t_reply is None:
                    t_reply = time.time()
                    if client_closes and not half_close:
                        c.shutdown(socket.SHUT_WR)
        except (ConnectionResetError, BrokenPipeError) as e:     # closed by the listener while we were sending
            out["closed"] = True
            out["error"] = type(e).__name__
        except socket.timeout:
            out["closed"] = False
    out["reply"] = buf.hex()
    if t_reply is not None:
        out["closed_after_reply_s"] = round(time.time() - t_reply, 3)
    return out


def listener_run() -> dict:
    """One Listener with a known nonce; the scenario's requests in order. Evidence never holds the nonce."""
    nonce = savesctl.new_nonce()
    tmp = Path(tempfile.mkdtemp(prefix="hs-ctl-"))
    acted: list[str] = []
    log = tmp / "savesctl.log"
    L = savesctl.Listener(nonce=nonce, log_path=log, actions={"S": lambda arg: acted.append("S")}).start()
    ev: dict = {"addr": f"{savesctl.HOST}:{L.port}", "accepted_code": savesctl.ACCEPTED,
                "refused_code": savesctl.REFUSED}
    try:
        wrong = "".join("0123456789ABCDEF"[(int(c, 16) + 1) % 16] for c in nonce)
        good = savesctl.challenge(f"HS{nonce}S")
        bad_game = bytearray(good)
        bad_game[4:8] = b"WoX\x00"
        reqs = {
            "status": (good, False),
            "wrong_nonce": (savesctl.challenge(f"HS{wrong}S"), False),
            "unknown_verb": (savesctl.challenge(f"HS{nonce}Q"), False),
            "status_with_argument": (savesctl.challenge(f"HS{nonce}S01"), False),
            "malformed": (bytes(bad_game), False),
            "player_name": (savesctl.challenge("PLAYER"), False),
            "truncated": (good[:20], True),
            "oversized": (good[:2] + b"\xff\xff" + bytes(savesctl.MAX_READ + 1024), False),
        }
        t0 = time.time()
        ev["requests"] = {k: _exchange(L.port, data, half_close=hc) for k, (data, hc) in reqs.items()}
        ev["seconds"] = round(time.time() - t0, 3)
        # a client that never closes: the listener closes by itself after its linger (a short one here, for speed;
        # production uses savesctl.LINGER_S), still with nothing after the 3 bytes
        L2 = savesctl.Listener(nonce=nonce, log_path=None, linger_s=0.3).start()
        try:
            ev["requests"]["status_client_stays"] = _exchange(L2.port, good, client_closes=False)
        finally:
            L2.stop()
        time.sleep(0.05)
        ev["events"] = [{k: v for k, v in e.items() if k != "t"} for e in L.events]
        ev["actions_run"] = list(acted)
    finally:
        ev["stop"] = {k: v for k, v in L.stop().items() if k != "events"}
        text = log.read_text() if log.exists() else ""
        ev["log_lines"] = len(text.splitlines())
        ev["nonce_in_log"] = nonce in text
        ev["nonce_in_evidence"] = nonce in json.dumps(ev)
        ev["log_tail"] = text.splitlines()[-3:]
        shutil.rmtree(tmp, ignore_errors=True)
    return ev


@check("savesctl.listener", order=48, capability=CAP, stack="any", tier="quick")
def listener(ctx):
    """Offline, no client: a status request with the right nonce gets exactly `00 00 <accepted>` and the connection
    closes (when the client closes, as the real one does, or by itself after its linger); wrong nonce / unknown verb / an argument where none belongs / a malformed challenge / a non-control name
    get `00 00 <refused>`; a truncated and an oversized packet are closed with no reply. Every connection is logged
    with its outcome and source address (accepted ones with the verb); only the valid request runs its action; no
    reply anywhere is a success; the nonce is in no log line."""
    ev = listener_run()
    r, evs = ev["requests"], ev["events"]
    acc, ref = f"0000{savesctl.ACCEPTED:02x}", f"0000{savesctl.REFUSED:02x}"
    require(savesctl.ACCEPTED != 0 and savesctl.REFUSED != 0 and savesctl.ACCEPTED != savesctl.REFUSED,
            "the accepted / refused codes must be distinct failure codes", **ev)
    for k in ("status", "status_client_stays"):
        require(r[k]["reply"] == acc and r[k].get("closed"),
                f"{k}: didn't get exactly the accepted code followed by a close", **ev)
    require(0.2 <= r["status_client_stays"].get("closed_after_reply_s", -1) < 2,
            "the listener didn't close by itself after its linger", **ev)
    for k in ("wrong_nonce", "unknown_verb", "status_with_argument", "malformed", "player_name"):
        require(r[k]["reply"] == ref and r[k].get("closed"), f"{k}: not exactly the refused code followed by a close",
                **ev)
    for k in ("truncated", "oversized"):
        require(r[k]["reply"] == "" and r[k].get("closed"), f"{k}: expected a close with no reply", **ev)
    replies = [v["reply"] for v in r.values() if v["reply"]]
    require(all(len(x) == 6 and x[:4] == "0000" and x[4:] != "00" for x in replies),
            "a reply that isn't a 3-byte failure (a success or more bytes)", **ev)
    require(len(evs) == len(r) - 1, "not one log event per connection", **ev)
    outcomes = [(e.get("outcome"), e.get("verb"), e.get("src")) for e in evs]
    require(outcomes[0] == ("accepted", "S", "127.0.0.1"), "the accepted request isn't logged with verb and source",
            **ev)
    require(all(e.get("outcome") == "refused" and e.get("src") == "127.0.0.1" and e.get("why") for e in evs[1:]),
            "a refused request isn't logged as refused with its source and reason", **ev)
    require(ev["actions_run"] == ["S"], "an action ran for a refused request (or not for the valid one)", **ev)
    require(not ev["nonce_in_log"] and not ev["nonce_in_evidence"], "the nonce appears in the log", **ev)
    require(ev["log_lines"] >= len(r), "the log file lacks lines", **ev)
    return ev


# ---------------------------------------------------------------- offline: test switches refused

def _refusal_runs() -> dict:
    """A normal ./play (no test flag) with the control test switches: refused before anything happens."""
    tmp = Path(tempfile.mkdtemp(prefix="hs-ctlref-"))
    pf = tmp / "player.json"
    lock_before = play.LOCK.stat().st_mtime_ns if play.LOCK.exists() else None
    out: dict = {"runs": {}}
    glue = json.dumps({"script": [{"t": 1, "act": "ctl:S"}, {"t": 2, "act": "realm:reset"}]})
    knobs = json.dumps({"codes": [8]})
    cases = [("HS_GLUE_TEST", glue, [], "HS_GLUE_TEST is set"),
             ("HS_GLUE_TEST", glue, ["--account", "test"], "HS_GLUE_TEST is set"),
             ("HS_SAVESCTL_TEST", knobs, [], "HS_SAVESCTL_TEST is set"),
             ("HS_SAVESCTL_TEST", knobs, ["--account", "test"], "HS_SAVESCTL_TEST is set"),
             # with the test flag but the player's account: still refused (the knobs are for the test account only)
             ("HS_SAVESCTL_TEST", knobs, ["--allow-test-client-dir"], "HS_SAVESCTL_TEST is set")]
    try:
        for var, val, args, says in cases:
            t = time.time()
            env = {k: v for k, v in os.environ.items() if k not in ("HS_GLUE_TEST", "HS_SAVESCTL_TEST")}
            # safety nets in case a broken ./play accepted the switch: a scratch player file and account name, and a
            # world data dir that makes the server fail at once, so nothing real is ever logged into
            r = subprocess.run([PLAY, *args, "--world-datadir", str(tmp / "no-such-data")], cwd=P.ROOT,
                               capture_output=True, text=True, timeout=600,
                               env={**env, var: val, "HS_PLAYER_FILE": str(pf), "HS_PLAYER_DEFAULT_NAME": "HSCTLTEST",
                                    "HS_PROFILE": "m0"})
            out["runs"][f"{var} {' '.join(args) or '(default account)'}"] = {
                "exit": r.returncode, "seconds": round(time.time() - t, 1), "refused_naming_switch": says in r.stdout,
                "output": (r.stdout + r.stderr)[-300:]}
        out["player_file_created"] = pf.exists()
        out["lock_touched"] = (play.LOCK.stat().st_mtime_ns if play.LOCK.exists() else None) != lock_before
        out["remaining_after"] = remaining()
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _player_never_gets_hook() -> dict:
    """The play loop hands the GlueXML test hook (and with it every ctl: action) only to the test account, and the
    listener's test knobs likewise (static read of play.py / savesctl.from_env, plus the knobs dropped for a player
    in-process)."""
    src = (P.HARNESS / "hslib" / "play.py").read_text()
    hook_line = re.search(r"hook = frontmenu\.test_hook_from_env\(\) if self\.args\.account == \"test\" else None",
                          src)
    os.environ.pop(savesctl.TEST_ENV, None)
    knob_player = savesctl.from_env("player", {savesctl.TEST_ENV: json.dumps({"codes": [8]})}).test
    knob_test = savesctl.from_env("test", {savesctl.TEST_ENV: json.dumps({"codes": [8]})}).test
    return {"hook_only_for_test_account": bool(hook_line), "knobs_for_player": knob_player,
            "knobs_for_test": knob_test}


@check("savesctl.test_actions_refused", order=49, capability=CAP, stack="down", tier="full")
def test_actions_refused(ctx):
    """A normal ./play (the app runs the same ./play) with $HS_GLUE_TEST carrying ctl: / realm: actions, or the
    listener's $HS_SAVESCTL_TEST, without the harness's test flag (default and test account), exits nonzero naming
    the switch before anything happens (no player file read or created, lock untouched, nothing running); the knobs
    are refused for the player's account even with the flag; the test hook reaches only the test account."""
    pre: dict = {}
    require_given_clear(pre)
    ev = _refusal_runs() | {"player": _player_never_gets_hook()}
    require(len(ev["runs"]) == 5 and all(v["exit"] != 0 and v["refused_naming_switch"] for v in ev["runs"].values()),
            "a normal ./play accepted a control test switch", **ev)
    require(not ev["player_file_created"] and not ev["lock_touched"], "the refused run touched the player file or lock",
            **ev)
    require(is_clear(ev["remaining_after"]), "something is running after the refused runs", **ev)
    p = ev["player"]
    require(p["hook_only_for_test_account"] and p["knobs_for_player"] is None and p["knobs_for_test"],
            "the test hook / listener knobs can reach the player's account", **ev)
    return ev


# ---------------------------------------------------------------- ./play: loopback only, the secret handled safely

def control_of(client_dir: Path) -> dict | None:
    """HS_CONTROL {port, nonce} from the generated login file inside a play client (read back from its archive)."""
    arch = frontmenu.archive_path(client_dir)
    if not arch.exists():
        return None
    tmp = Path(tempfile.mkdtemp(prefix="hs-ctlprof-"))
    try:
        mpq.extract(arch, frontmenu.INSIDE, tmp / "p.lua")
        text = (tmp / "p.lua").read_text(errors="replace")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    m = re.search(r'HS_CONTROL = \{ port = (\d+), nonce = "([0-9A-F]+)" \};', text)
    return {"port": int(m.group(1)), "nonce": m.group(2), "archive": arch} if m else None


def listening(pid: int) -> list[str]:
    """TCP listening addresses of a process (lsof)."""
    r = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-Fn"],
                       capture_output=True, text=True, timeout=30)
    return [l[1:] for l in r.stdout.splitlines() if l.startswith("n")]


def port_answers(port: int) -> bool:
    try:
        with socket.create_connection((savesctl.HOST, port), timeout=2):
            return True
    except OSError:
        return False


def _texts_since(t0: float) -> dict[str, str]:
    """Every harness log written during the check (env/logs/*), play-last.json: where a leaked nonce would land."""
    out = {}
    for p in sorted(P.LOGS.glob("*")):
        if p.is_file() and p.stat().st_mtime >= t0 - 1 and p.stat().st_size < 50_000_000:
            out[str(p.relative_to(P.ROOT))] = p.read_text(errors="replace")
    if play.LAST.exists():
        out[str(play.LAST.relative_to(P.ROOT))] = play.LAST.read_text(errors="replace")
    return out


@check("savesctl.hygiene", order=361, capability=CAP, stack="down", tier="full")
def hygiene(ctx):
    """./play in test mode (stand-in client, scratch play client, test account): while the game runs, ./play's
    control listener is bound to 127.0.0.1 only (lsof) and answers a status request; the port and nonce reach the
    glue in the generated file inside the scratch play client, never client/; after a SIGKILL of ./play the listener
    is gone, and the next run starts cleanly (the stale file removed first) with a fresh nonce; after a normal exit
    the listener is gone too; the nonce is in no harness log, play-last.json, ./play output or this evidence (which is
    what results.json and summary.md carry)."""
    ev: dict = {"cmd": f"{PLAY} --account test --client-cmd <stand-in .bat> --allow-test-client-dir"}
    require_given_clear(ev)
    from .playclient_util import build_scratch, drop_scratch, fresh_scratch
    t0 = time.time()
    scratch = fresh_scratch("ctlhyg")
    tmp = Path(tempfile.mkdtemp(prefix="hs-ctlhyg-"))
    s1 = s2 = None
    nonces: list[str] = []
    try:
        build_scratch(scratch)
        settings = tmp / "settings.json"
        settings.write_text(json.dumps({"client": "patched"}))
        env = {"HS_SETTINGS": str(settings), "HS_PLAY_CLIENT_DIR": str(scratch)}

        def observe(s: StandIn, tag: str) -> dict:
            o: dict = {}
            up = s.wait_client_up()
            o["client_up"] = {k: v for k, v in up.items() if k != "client_processes"}
            require(up.get("up"), f"{tag}: the stand-in never came up", output=s.output()[-2000:], **ev)
            c = control_of(scratch)
            require(c, f"{tag}: no HS_CONTROL in the generated login file", **ev)
            nonces.append(c["nonce"])
            o["file"] = {"archive": str(c["archive"].relative_to(P.ROOT)),
                         "in_client_dir": mpq._under_client(c["archive"]),
                         "in_scratch": c["archive"].resolve().is_relative_to(scratch.resolve()),
                         "mode": oct(c["archive"].stat().st_mode & 0o777)}
            o["port"] = c["port"]
            o["listening"] = listening(s.proc.pid)
            ex = _exchange(c["port"], savesctl.challenge(f"HS{c['nonce']}S"))
            o["status_request"] = {k: v for k, v in ex.items() if k != "sent"}
            return o

        s1 = StandIn("ctlhyg1")
        s1.start_play("--allow-test-client-dir", env=env)
        ev["run1"] = observe(s1, "run 1")
        os.killpg(s1.proc.pid, signal.SIGKILL)
        s1.proc.wait()
        time.sleep(1)
        ev["run1"]["after_sigkill"] = {"port_answers": port_answers(ev["run1"]["port"]),
                                       "stale_file_left": frontmenu.archive_path(scratch).exists()}
        s2 = StandIn("ctlhyg2")
        s2.start_play("--allow-test-client-dir", env=env)
        ev["run2"] = observe(s2, "run 2")
        s2.stop_file.write_text("stop")
        rc, secs = s2.wait_exit(600)
        ev["run2"]["exit"] = {"code": rc, "seconds": secs}
        rec = json.loads(play.LAST.read_text()) if play.LAST.exists() else {}
        ev["run2"]["play_record"] = {"pid_matches": rec.get("pid") == s2.proc.pid, "savesctl": rec.get("savesctl"),
                                     "stale_profiles_removed": (rec.get("client") or {}).get("stale_profiles_removed"),
                                     "profiles_removed": (rec.get("cleanup") or {}).get("profiles_removed")}
        ev["run2"]["after_exit"] = {"port_answers": port_answers(ev["run2"]["port"]),
                                    "listening": listening(s2.proc.pid) if s2.proc.poll() is None else []}
        ev["fresh_nonce"] = len(nonces) == 2 and nonces[0] != nonces[1]
        texts = _texts_since(t0) | {"run1 ./play output": s1.output(), "run2 ./play output": s2.output()}
        ev["searched_for_nonce"] = sorted(texts)
        ev["nonce_found_in"] = sorted(k for k, v in texts.items() if any(n in v for n in nonces))
        ev["nonce_in_evidence"] = any(n in json.dumps(ev, default=str) for n in nonces)
        ev["remaining_after"] = remaining()
    finally:
        for s in (s1, s2):
            if s is not None:
                s.cleanup()
        frontmenu.remove(scratch) if (scratch / "hs-build.json").is_file() else None
        drop_scratch(scratch)
        shutil.rmtree(tmp, ignore_errors=True)
    acc = f"0000{savesctl.ACCEPTED:02x}"
    for tag in ("run1", "run2"):
        r = ev[tag]
        require(r["listening"] == [f"{savesctl.HOST}:{r['port']}"],
                f"{tag}: ./play isn't listening on exactly 127.0.0.1:<port> (lsof)", **ev)
        require(r["status_request"]["reply"] == acc, f"{tag}: the listener didn't answer a status request", **ev)
        require(not r["file"]["in_client_dir"] and r["file"]["in_scratch"] and r["file"]["mode"] == "0o600",
                f"{tag}: the file with the port and nonce isn't a 0600 file inside the scratch play client", **ev)
    require(not ev["run1"]["after_sigkill"]["port_answers"], "the listener still answers after SIGKILL", **ev)
    rec2 = ev["run2"]["play_record"]
    require(rec2["pid_matches"] and rec2["stale_profiles_removed"], "run 2 didn't remove the killed run's file first",
            **ev)
    require(ev["run2"]["exit"]["code"] == 0 and (rec2["savesctl"] or {}).get("closed") is True
            and not ev["run2"]["after_exit"]["port_answers"], "the listener isn't gone after the game exits", **ev)
    require(ev["fresh_nonce"], "the next run didn't get a fresh nonce", **ev)
    require(not ev["nonce_found_in"] and not ev["nonce_in_evidence"], "the nonce leaked into a log or the evidence",
            **ev)
    require(is_clear(ev["remaining_after"]), "something is still running", **ev)
    return ev
