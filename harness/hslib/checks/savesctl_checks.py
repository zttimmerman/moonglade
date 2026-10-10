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
        L2 = savesctl.Listener(nonce=nonce, log_path=None, linger_s=0.2).start()
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
    require(0.15 <= r["status_client_stays"].get("closed_after_reply_s", -1) < 2,
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
    require(p["hook_only_for_test_account"] and not p["knobs_for_player"] and p["knobs_for_test"],
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
        # the killed run's stand-in "game" outlives ./play (Wine isn't in its process group); it's closed the way a
        # player would close the game, so the next run's play loop doesn't see it as its own client
        s1.stop_file.write_text("stop")
        t_k = time.time()
        while play.client_processes(s1.bat.name.lower()) and time.time() - t_k < 60:
            time.sleep(1)
        ev["run1"]["after_sigkill"]["stand_in_closed_s"] = round(time.time() - t_k, 1)
        s2 = StandIn("ctlhyg2")
        s2.start_play("--allow-test-client-dir", env=env)
        ev["run2"] = observe(s2, "run 2")
        s2.stop_file.write_text("stop")
        rc, secs = s2.wait_exit(300)
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


# ---------------------------------------------------------------- live: the menu talks to the launcher

# One shared ./play run (real Wow.exe, scratch play client, test account) for the four live scenarios. The listener's
# test knobs: the six candidate codes in turn (the accepted one first: that request is the round trip), each reply
# held LIVE_DELAY_S so a screenshot lands while a request is pending, and the listener closed after the sixth (the
# "listener down" request). Glue clock (seconds after the glue screens load):
#   4 shot (menu, no request yet) | 6 ctl:S + 6.8 shot (pending) + 10 shot (accepted) | 12..28 ctl:S x5 (the other
#   codes) | 32 ctl:S (listener closed) | 40.5 shot (couldn't reach) | 44 Continue -> character list -> enter world
LIVE_CODES = [savesctl.ACCEPTED] + [c for c in savesctl.CANDIDATES if c != savesctl.ACCEPTED]
LIVE_DELAY_S = 1.5
LIVE_CTL = {"codes": LIVE_CODES, "delay_s": LIVE_DELAY_S, "stop_after": len(LIVE_CODES)}
LIVE_SENDS = [6, 12, 16, 20, 24, 28]
DOWN_AT = 32
LIVE_GLUE = {"marker": True, "enter_world_after_charlist": 3,
             "script": [{"t": 4, "act": "shot"}, {"t": 6, "act": "ctl:S"}, {"t": 6.8, "act": "shot"},
                        {"t": 10, "act": "shot"}]
                       + [{"t": t, "act": "ctl:S"} for t in LIVE_SENDS[1:]]
                       + [{"t": DOWN_AT, "act": "ctl:S"}, {"t": 40.5, "act": "shot"}, {"t": 44, "act": "continue"}]}
LIVE_TIMEOUT = 300
SHOTS = ("menu", "pending", "accepted", "down")
_SESSIONS: dict = {}
K_RE = re.compile(r"^K([ARU])([01])([01]):([0-9a-f]{8}):(\d+)$")


def square(path: Path, col: int, row: int) -> str:
    """A marker square's colour (HSTestMarker: 40x40 UI-unit squares, 53 units apart, anchored TOPRIGHT -10,-10)."""
    from .playclient_util import _bmp_pixels, _classify
    w, h, px = _bmp_pixels(path)
    u = h / 768
    cx, cy = round(w - (10 + 200) * u + (col * 53 + 20) * u), round((10 + row * 53 + 20) * u)
    r = max(3, int(6 * u))
    acc = [0, 0, 0]
    for x in range(cx - r, cx + r + 1):
        for y in range(cy - r, cy + r + 1):
            p = px(x, y)
            for i in range(3):
                acc[i] += p[i]
    n = (2 * r + 1) ** 2
    return _classify(tuple(round(v / n) for v in acc))


def k_tokens(rec: list[str] | None) -> list[dict]:
    """The glue's control results (HSFrontMenu.lua finish(): K<class><realm restored><stock dialog seen>:<text adler>:
    <tenths of a second from send to result>)."""
    out = []
    for t in rec or []:
        m = K_RE.match(t)
        if m:
            out.append({"cls": m.group(1), "realm_restored": m.group(2) == "1", "stock_dialog_seen": m.group(3) == "1",
                        "adler": m.group(4), "secs": int(m.group(5)) / 10})
    return out


def live_session() -> dict:
    if "live" in _SESSIONS:
        return _SESSIONS["live"]
    from .playclient_util import PlayRun, build_scratch, connection_log, drop_scratch, fresh_scratch, screenshots
    from .seamless_checks import config_value, glue_record, server_logins
    ev: dict = {"account": P.TEST_USER, "glue_test": LIVE_GLUE, "listener_test_knobs": LIVE_CTL,
                "shared_run": "saves-channel live (live_roundtrip + live_codes + live_down + realm_restored)"}
    _SESSIONS["live"] = ev
    scratch = run = None
    try:
        require_given_clear(ev)
        scratch = fresh_scratch("ctllive")
        build_scratch(scratch)
        run = PlayRun("ctllive", glue_test=LIVE_GLUE, scratch=scratch,
                      extra_env={savesctl.TEST_ENV: json.dumps(LIVE_CTL)})
        run.start()
        up = run.wait_client()
        ev["client_up"] = {k: v for k, v in up.items() if k not in ("t", "wow")}
        require(up.get("up"), "the real Wow.exe never came up", output=run.output()[-2500:], **ev)
        t_up = up["t"]
        ev["t_client_up"] = round(t_up, 3)
        ctl = control_of(scratch)                     # in memory only: the nonce never goes into the evidence
        require(ctl, "no HS_CONTROL in the generated login file", **ev)
        ev["listener_port"] = ctl["port"]
        t = time.time()
        rec = None
        while time.time() - t < LIVE_TIMEOUT and run.proc.poll() is None:
            rec = glue_record(scratch)
            if rec and [x for x in server_logins(P.TEST_CHAR) if x["t"] >= int(t_up)]:
                break
            time.sleep(2)
        ev["glue_record"] = rec
        ev["k"] = k_tokens(rec)
        ev["realmlist_in_config"] = config_value(scratch, "realmList") or config_value(scratch, "realmlist")
        shots = [p for p in screenshots(scratch) if p.stat().st_mtime >= t_up - 5]
        ev["screens"] = [{"file": p.name, "control": square(p, 1, 1), "stock_dialog": square(p, 2, 1),
                          "lua_errors": square(p, 3, 0)} for p in shots]
        ev["connection_log"] = connection_log(scratch)
        log = P.LOGS / "savesctl.log"
        ev["listener_log"] = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
        auth = P.LOGS / "Auth.log"
        auth_text = auth.read_text(errors="replace") if auth.exists() else ""
        ev["auth_log"] = {"exists": auth.exists(), "lines": len(auth_text.splitlines()),
                          "hs_token_lines": [l[:160] for l in auth_text.splitlines()
                                             if re.search(r"HS[0-9A-F]{%d}[A-Z]" % savesctl.NONCE_HEX, l.upper())],
                          "nonce_seen": ctl["nonce"] in auth_text.upper()}
        ev["stop"] = run.stop()
        rec_play = run.record()
        ev["play_record"] = {"outcome": rec_play.get("outcome"), "savesctl": rec_play.get("savesctl")}
        logins = server_logins(P.TEST_CHAR)
        ev["server_logins_after_client_up"] = [x for x in logins if x["t"] >= int(t_up)]
        ev["nonce_in_evidence"] = ctl["nonce"] in json.dumps(ev, default=str)
        ev["remaining_after"] = remaining()
        return ev
    except Exception as e:  # noqa: BLE001  (recorded for all four checks; the first caller re-raises)
        ev["session_error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        if run is not None:
            run.cleanup()
        drop_scratch(scratch)


def _live() -> dict:
    ev = live_session()
    require(not ev.get("session_error"), f"the shared live run failed: {ev.get('session_error')}", **ev)
    require(ev["glue_record"] and not ev["glue_record"][-1].endswith("~"),
            "no glue record (or it was cut at the cvar limit)", **ev)
    require(not ev["nonce_in_evidence"], "the nonce is in the evidence", **ev)
    return ev


def _requests(ev: dict) -> list[dict]:
    return [e for e in ev["listener_log"] if e.get("event") == "request"]


def _glue_strings() -> dict[str, str]:
    from .seamless_checks import stock_strings
    return stock_strings("Interface\\GlueXML\\GlueStrings.lua")


def _names(adler_hex: str, gs: dict[str, str]) -> list[str]:
    from .seamless_checks import adler
    return sorted(k for k, v in gs.items() if k != "__source__" and adler(v) == adler_hex)


def _connects(conn: str) -> list[str]:
    return re.findall(r"RESPONSE_CONNECTED result: LOGIN_OK (\S+)", conn)


@check("savesctl.live_roundtrip", order=395, capability=CAP, stack="down", tier="full")
def live_roundtrip(ctx):
    """Real client on the title menu (test run, listener up), the test script sends ctl:S: the listener logs exactly
    one challenge for it, account HS<nonce>S (accepted: the nonce matched), build 12340, from 127.0.0.1, within 5 s
    (before the glue had its reply); the glue records the accepted reply; a screenshot while the request is pending
    shows our overlay (control square magenta) and no stock dialog, and no stock dialog was on screen in any frame of
    any request; the authserver's log has no HS name, and every login-server connection before the Continue went to
    the listener's port (the client's connection.log)."""
    ev = _live()
    reqs, k = _requests(ev), ev["k"]
    require(len(k) == len(LIVE_SENDS) + 1, "the glue didn't record one result per request", **ev)
    first = reqs[0] if reqs else {}
    require(first.get("outcome") == "accepted" and first.get("verb") == "S" and first.get("build") == 12340
            and first.get("src") == "127.0.0.1" and first.get("reply") == savesctl.ACCEPTED,
            "the first request isn't one accepted HS<nonce>S challenge, build 12340, from 127.0.0.1", **ev)
    require(len(reqs) == len(LIVE_SENDS), "not exactly one challenge per request while the listener was up", **ev)
    require(k[0]["cls"] == "A" and k[0]["secs"] <= 5, "the glue didn't record the accepted reply within 5 s", **ev)
    sc = dict(zip(SHOTS, ev["screens"]))
    require(len(ev["screens"]) >= 4 and sc["menu"]["control"] == "white" and sc["pending"]["control"] == "magenta"
            and sc["pending"]["stock_dialog"] == "white" and sc["accepted"]["control"] == "green",
            "the screenshots don't show: no request / our overlay while pending / accepted", **ev)
    require(all(s["stock_dialog"] == "white" for s in ev["screens"]) and not any(x["stock_dialog_seen"] for x in k),
            "a stock dialog was on screen during a request", **ev)
    require(ev["auth_log"]["exists"] and not ev["auth_log"]["hs_token_lines"] and not ev["auth_log"]["nonce_seen"],
            "the authserver's log names a control token", **ev)
    conn = _connects(ev["connection_log"])
    before = conn[:len(LIVE_SENDS)]
    ev["connects"] = conn
    require(before == [f"127.0.0.1:{ev['listener_port']}"] * len(LIVE_SENDS),
            "a control request's connection didn't go to the listener's port", **ev)
    return ev


@check("savesctl.live_codes", order=396, capability=CAP, stack="down", tier="full")
def live_codes(ctx):
    """The listener answers successive ctl:S requests with each candidate code (0x03 0x04 0x07 0x08 0x0C 0x10); the
    glue records each one's status text (Adler-32, matched against the installed client's GlueStrings.lua). The
    table goes into the evidence; the accepted and refused codes the design uses each map to a text no other tested
    code produces, and to the GlueStrings name savesctl.CODE_TEXT and the glue's HS_CONTROL_REPLY name."""
    ev = _live()
    reqs, k = _requests(ev), ev["k"]
    require(len(reqs) == len(LIVE_CODES) and [r.get("reply") for r in reqs] == LIVE_CODES,
            "the listener didn't answer the candidate codes in order", **ev)
    gs = _glue_strings()
    table = []
    for code, tok in zip(LIVE_CODES, k):
        table.append({"code": f"0x{code:02X}", "glue_class": tok["cls"], "text_adler": tok["adler"],
                      "glue_strings": _names(tok["adler"], gs), "secs": tok["secs"]})
    ev["table"] = table
    src = (P.ROOT / "client-mods" / "glue" / "HSFrontMenu.lua").read_text()
    m = re.search(r'HS_CONTROL_REPLY = \{ accepted = "(\w+)", refused = "(\w+)" \};', src)
    ev["glue_reply_names"] = m.groups() if m else None
    adlers = [t["text_adler"] for t in table]
    for role, code in (("accepted", savesctl.ACCEPTED), ("refused", savesctl.REFUSED)):
        row = next(t for t in table if t["code"] == f"0x{code:02X}")
        require(adlers.count(row["text_adler"]) == 1, f"the {role} code's text isn't unique among the tested codes",
                **ev)
        require(savesctl.CODE_TEXT[code] in row["glue_strings"], f"the {role} code's text isn't "
                f"{savesctl.CODE_TEXT[code]} (savesctl.CODE_TEXT)", **ev)
        require(m and m.group(1 if role == "accepted" else 2) == savesctl.CODE_TEXT[code],
                f"the glue's HS_CONTROL_REPLY.{role} isn't {savesctl.CODE_TEXT[code]}", **ev)
        require(row["glue_class"] == ("A" if role == "accepted" else "R"), f"the glue didn't classify {role}", **ev)
    require(all(t["glue_strings"] for t in table), "a reply text isn't one of the client's GlueStrings", **ev)
    require(all(t["glue_class"] == "U" for t in table if t["code"] not in
                (f"0x{savesctl.ACCEPTED:02X}", f"0x{savesctl.REFUSED:02X}")),
            "another code was taken for accepted / refused", **ev)
    return ev


@check("savesctl.live_down", order=397, capability=CAP, stack="down", tier="full")
def live_down(ctx):
    """With the listener stopped (closed after its sixth connection), the script's ctl:S: within 10 s the glue records
    "couldn't reach the launcher" (class U) with realmList restored; the listener saw no further challenge; no stock
    dialog showed; the screenshot after it shows the couldn't-reach state; the Continue afterwards still works
    (savesctl.realm_restored)."""
    ev = _live()
    reqs, k = _requests(ev), ev["k"]
    closed = [e for e in ev["listener_log"] if e.get("event") == "closed"]
    require(closed and len(reqs) == len(LIVE_SENDS) and closed[0]["t"] >= reqs[-1]["t"],
            "GIVEN not met: the listener wasn't closed after its last answered request", **ev)
    d = k[len(LIVE_SENDS)] if len(k) > len(LIVE_SENDS) else {}
    ev["down_result"] = d | {"glue_strings": _names(d.get("adler", ""), _glue_strings())} if d else None
    require(d and d["cls"] == "U" and d["secs"] <= 10 and d["realm_restored"] and not d["stock_dialog_seen"],
            "no couldn't-reach result within 10 s with realmList restored and no stock dialog", **ev)
    sc = dict(zip(SHOTS, ev["screens"]))
    require(sc.get("down", {}).get("control") == "blue", "the screenshot after it doesn't show couldn't-reach", **ev)
    require(len(_connects(ev["connection_log"])) >= len(LIVE_SENDS) + 1
            and _connects(ev["connection_log"])[len(LIVE_SENDS)] == "127.0.0.1:3724",
            "the next connection after the down request isn't the Continue's real login", **ev)
    return ev


@check("savesctl.realm_restored", order=398, capability=CAP, stack="down", tier="full")
def realm_restored(ctx):
    """After the control requests the script continues: the login reaches the real login server (127.0.0.1:3724,
    character list received), the test character enters the world (the server's Players.log), and WTF/Config.wtf,
    which the client writes on entering the world, has realmList without a port; every request restored realmList."""
    ev = _live()
    k = ev["k"]
    require(k and all(x["realm_restored"] for x in k), "a request didn't restore realmList", **ev)
    conn = ev["connection_log"]
    real = _connects(conn)[len(LIVE_SENDS):]
    require(real and real[0] == "127.0.0.1:3724" and re.search(r"COP_GET_CHARACTERS code=\d+ result=TRUE", conn),
            "the Continue didn't reach the real login server and the character list", **ev)
    require("W" in ev["glue_record"] and ev["server_logins_after_client_up"],
            "the test character didn't enter the world", **ev)
    rl = ev["realmlist_in_config"]
    require(rl and ":" not in rl, "WTF/Config.wtf has realmList with a port (or none)", **ev)
    require(is_clear(ev["remaining_after"]), "something is still running", **ev)
    return ev


# ---------------------------------------------------------------- front-menu-saves: the load verb, the build check

def load_verb_run(listener_cls=None) -> dict:
    """A listener with a stub load action (records its arguments; told to succeed, refuse or stall) and the launcher's
    validator for stale/unknown entries (a stub list: generation 1, indexes 01-02): challenges for a valid L, a stale
    generation, an unknown index, a refused load, a load that never finishes within the poll limit, and two
    overlapping loads."""
    L = listener_cls or savesctl.Listener
    calls, mode = [], {"r": (True, "done")}
    valid = {"101", "102"}

    def act(arg):
        calls.append(arg)
        r = mode["r"]
        if r == "stall":
            time.sleep(3.0)
            return False, "the load didn't finish in time"
        return r

    log = Path(tempfile.mkdtemp(prefix="hs-ctl-load-")) / "savesctl.log"
    lis = L(log_path=log, linger_s=0.2)
    lis.actions["L"] = act
    lis.validators["L"] = lambda a: None if a in valid else "stale or unknown list entry"
    lis.start()
    n = lis.nonce

    def send(arg):
        return _exchange(lis.port, savesctl.challenge(f"HS{n}L{arg}"))["reply"]

    out = {}
    try:
        out["valid"] = send("101")
        out["stale_generation"] = send("001")
        out["unknown_index"] = send("199")
        mode["r"] = (False, "That save doesn't exist.")
        out["refused"] = send("102")
        mode["r"] = "stall"
        import threading
        box = {}
        th = threading.Thread(target=lambda: box.setdefault("first", send("101")))
        th.start()
        time.sleep(0.5)
        out["overlap_second"] = send("102")
        th.join()
        out["stall_first"] = box.get("first")
    finally:
        lis.stop()
        events = [json.loads(l) for l in log.read_text().splitlines()]
        shutil.rmtree(log.parent, ignore_errors=True)
    return {"replies": out, "calls": calls, "log": [e for e in events if e.get("event") == "request"],
            "log_text": json.dumps(events)}


@check("savesctl.load_verb", order=50, capability=CAP, stack="any", tier="full")
def load_verb(ctx):
    """The listener with a stub load action: only the valid L that succeeds gets ACCEPTED; a stale generation, an
    unknown index, a refused load, a load that doesn't finish in the poll limit and the second of two overlapping
    loads get REFUSED; the stub saw exactly one call per non-stale request; the log names the verb and outcome but no
    save id."""
    r = load_verb_run()
    acc, ref = f"0000{savesctl.ACCEPTED:02x}", f"0000{savesctl.REFUSED:02x}"
    ev = r | {"accepted_hex": acc, "refused_hex": ref}
    rep = r["replies"]
    require(rep["valid"] == acc, "the valid load wasn't ACCEPTED", **ev)
    require(all(rep[k] == ref for k in ("stale_generation", "unknown_index", "refused", "overlap_second", "stall_first")),
            "a stale, unknown, refused, stalled or overlapping load wasn't REFUSED", **ev)
    require(r["calls"] == ["101", "102", "101"], "the stub didn't see exactly one call per non-stale, non-overlapping "
            "request", **ev)
    require(all(e.get("verb") == "L" and e.get("outcome") in ("accepted", "refused") for e in r["log"] if e.get("verb")),
            "the log doesn't name the verb and outcome", **ev)
    require(not re.search(r"\d{8}-\d{6}-(manual|quick|auto|before-load)", r["log_text"]), "a save id is in the log", **ev)
    return ev


def build_run() -> dict:
    log = Path(tempfile.mkdtemp(prefix="hs-ctl-build-")) / "savesctl.log"
    lis = savesctl.Listener(log_path=log, linger_s=0.2).start()
    out = {}
    try:
        for label, build, ver in (("12340 3.3.5", 12340, (3, 3, 5)), ("12339 3.3.5", 12339, (3, 3, 5)),
                                  ("8606 3.3.5", 8606, (3, 3, 5)), ("12340 2.4.3", 12340, (2, 4, 3))):
            out[label] = _exchange(lis.port, savesctl.challenge(f"HS{lis.nonce}S", build=build, version=ver))["reply"]
    finally:
        lis.stop()
        events = [json.loads(l) for l in log.read_text().splitlines()]
        shutil.rmtree(log.parent, ignore_errors=True)
    return {"replies": out, "log": [e for e in events if e.get("event") == "request"]}


@check("savesctl.build_refused", order=51, capability=CAP, stack="any", tier="full")
def build_refused(ctx):
    """With the right nonce, valid S challenges with builds 12340, 12339 and 8606 and with version 2.4.3: only build
    12340 at 3.3.5 gets ACCEPTED; the others get REFUSED and are logged with the reason "wrong build"."""
    r = build_run()
    acc, ref = f"0000{savesctl.ACCEPTED:02x}", f"0000{savesctl.REFUSED:02x}"
    ev = r
    require(r["replies"]["12340 3.3.5"] == acc, "build 12340 at 3.3.5 wasn't ACCEPTED", **ev)
    others = [k for k in r["replies"] if k != "12340 3.3.5"]
    require(all(r["replies"][k] == ref for k in others), "another build or version wasn't REFUSED", **ev)
    wrong = [e for e in r["log"] if e.get("why") == "wrong build"]
    require(len(wrong) == len(others), "the refusals aren't logged as \"wrong build\"", **ev)
    return ev


def heal_run() -> dict:
    """Scenario 2: a scratch play client whose Config.wtf has accountName set to a control token; ./play (test mode,
    stand-in client) starts and writes its client config: accountName is the default profile's account by the time
    the client starts."""
    from .playclient_util import build_scratch, drop_scratch, fresh_scratch
    scratch = fresh_scratch("ctlheal")
    ev = {}
    s = None
    try:
        build_scratch(scratch)
        cfg = scratch / "WTF" / "Config.wtf"
        token = "HS1A2B3CS"
        text = cfg.read_text() if cfg.exists() else ""
        text = re.sub(r'(?m)^SET accountName "[^"]*"\r?\n?', "", text) + f'SET accountName "{token}"\r\n'
        cfg.write_text(text)
        ev["planted"] = token
        settings = Path(tempfile.mkdtemp(prefix="hs-ctlheal-")) / "settings.json"
        settings.write_text(json.dumps({"client": "patched"}))
        s = StandIn("ctlheal")
        s.start_play("--allow-test-client-dir", env={"HS_SETTINGS": str(settings), "HS_PLAY_CLIENT_DIR": str(scratch)})
        up = s.wait_client_up()
        ev["stand_in_up"] = {k: v for k, v in up.items() if k != "processes"}
        m = re.search(r'^SET accountName "([^"]*)"', cfg.read_text(), re.M)
        ev["account_name_at_client_start"] = m.group(1) if m else None
        s.stop_file.write_text("x")
        s.proc.wait(timeout=120)
    finally:
        if s and s.proc and s.proc.poll() is None:
            os.killpg(s.proc.pid, signal.SIGKILL)
        drop_scratch(scratch)
    return ev


@check("savesctl.account_name_kept", order=404, capability=CAP, stack="down", tier="full", gui=True)
def account_name_kept(ctx):
    """(1) A test run on the title menu, accountName = the test profile's account: two `ctl:S` requests, then Continue,
    the world, quit: WTF/Config.wtf afterwards has accountName = the test account and no HS... token anywhere in WTF/
    (the shared saves GUI run). (2) A scratch play client whose Config.wtf has accountName = an HS<6 hex>S token:
    ./play (test mode) starts and writes its client config, and accountName is the default profile's account before
    the client starts."""
    from .saves_gui_checks import session
    s = session()
    ev = {"run": {k: s.get(k) for k in ("account_name_cfg", "wtf_token_files", "session_error")}}
    first = s.get("records", [{}])[0].get("rec") if s.get("records") else None
    ev["k_tokens"] = [t for t in (first or []) if t.startswith("K")]
    require(not s.get("session_error"), "the shared GUI run failed", **ev)
    require(len(ev["k_tokens"]) >= 2 and all(t[1] == "A" for t in ev["k_tokens"][:2]),
            "GIVEN not met: the two ctl:S requests weren't both answered", **ev)
    require(s.get("account_name_cfg") == P.TEST_USER and not s.get("wtf_token_files"),
            "accountName isn't the test account, or a token is in WTF/", **ev)
    ev["heal"] = heal_run()
    require(ev["heal"]["account_name_at_client_start"] == P.TEST_USER.lower() or
            ev["heal"]["account_name_at_client_start"] == P.TEST_USER, "the token in accountName wasn't healed at start", **ev)
    return ev
