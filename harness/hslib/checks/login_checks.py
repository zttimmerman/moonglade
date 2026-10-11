"""login-screen: the front menu in the client's own login screen (openspec/changes/in-client-login).

Runtime checks drive the real ./play with the real Wow.exe against a scratch play client (playclient_util.PlayRun),
logged in only as the harness test account HSTEST; never the player's account. Evidence is server-side (auth.account
last_login / online, read-only), the client's own logs (Logs/connection.log, GlueXML.log, FrameXML.log, WTF/Config.wtf)
and the client's own screenshots, whose marker squares encode the menu state (HSFrontMenu_UpdateMarker)."""
from __future__ import annotations

import json
import re
import shutil
import struct
import tempfile
import time
from pathlib import Path

from .. import frontmenu, paths as P, play
from ..registry import check, require
from .play_checks import is_clear, remaining, require_given_clear, _delete_scratch
from .playclient_util import (PlayRun, account_state, build_scratch, clear_logs, connection_log, db_ready, drop_scratch,
                              fresh_scratch, glue_logs, marker, screenshots)

CAP = "login-screen"
LOGIN_LIMIT = 90               # spec: within 90 s
WAIT_MENU = 60                 # spec: no login for at least 60 s after the menu appears


def _wait(pred, timeout, step=1.0):
    t = time.time()
    while time.time() - t < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def _finish(run: PlayRun, ev: dict) -> None:
    ev["stop"] = run.stop()
    rec = run.record()
    ev["play_record"] = {k: rec.get(k) for k in ("outcome", "exit_code", "client")}
    ev["profiles_removed"] = ((rec.get("cleanup") or {}).get("profiles_removed"))
    ev["remaining_after"] = remaining()


def _no_secret(ev: dict) -> None:
    """The test account's password never shows up in ./play's record or output."""
    text = play.LAST.read_text() if play.LAST.exists() else ""
    ev["password_in_play_last_json"] = P.TEST_PASS in json.dumps(json.loads(text).get("client", {})) if text else False


# ---------------------------------------------------------------- Continue

def _hook_refused() -> dict:
    """A normal ./play (no test flag) with a test switch set ($HS_GLUE_TEST or $HS_PLAY_SKIP_PATCHES), in patched and
    in stock mode, exits nonzero naming the switch, before anything happens: a scratch player file stays absent (so
    the player's own login is never read), the lock file isn't taken, nothing is running before or after."""
    import os
    import subprocess
    from .play_checks import PLAY
    tmp = Path(tempfile.mkdtemp(prefix="hs-hook-"))
    pf = tmp / "player.json"
    lock_before = play.LOCK.stat().st_mtime_ns if play.LOCK.exists() else None
    out: dict = {"runs": {}}
    try:
        switches = {"HS_GLUE_TEST": '{"script": [{"t": 1, "act": "continue"}]}', "HS_PLAY_SKIP_PATCHES": "0001"}
        for mode in ("patched", "stock"):
            settings = tmp / f"settings-{mode}.json"
            settings.write_text(json.dumps({"client": mode}))
            for var, val in switches.items():
                # safety nets in case a broken ./play accepted the switch: a scratch account name and a world data
                # dir that makes the server fail at once, so nothing real is ever logged into
                for args in ([], ["--account", "test"]):
                    t = time.time()
                    r = subprocess.run([PLAY, *args, "--world-datadir", str(tmp / "no-such-data")], cwd=P.ROOT,
                                       capture_output=True, text=True, timeout=600,
                                       env={**{k: v for k, v in os.environ.items() if k not in switches},
                                            var: val, "HS_SETTINGS": str(settings), "HS_PLAYER_FILE": str(pf),
                                            "HS_PLAYER_DEFAULT_NAME": "HSHOOKTEST", "HS_PROFILE": "m0"})
                    key = f"{mode} {var} {' '.join(args) or '(default account)'}"
                    out["runs"][key] = {"exit": r.returncode, "seconds": round(time.time() - t, 1),
                                        "refused_naming_switch": f"{var} is set" in r.stdout,
                                        "output": (r.stdout + r.stderr)[-300:]}
        out["player_file_created"] = pf.exists()
        out["lock_touched"] = (play.LOCK.stat().st_mtime_ns if play.LOCK.exists() else None) != lock_before
        out["remaining_after"] = remaining()
        out["ok"] = (len(out["runs"]) == 8
                     and all(v["exit"] != 0 and v["refused_naming_switch"] for v in out["runs"].values())
                     and not out["player_file_created"] and not out["lock_touched"]
                     and is_clear(out["remaining_after"]))
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Shared runs (harness-speed): the two Continue scenarios share one ./play run, and the menu-waits scenario shares the
# backdrop check's restart run, the way the launcher-polish checks share their app session. Each check asserts its
# own clauses, unchanged, on that run's evidence; a session runs once per harness process (again for each `--only`
# invocation), so evidence is always from this run, and each run starts from "nothing running" (verified).
_SESSIONS: dict = {}
PRE_CONTINUE_SHOT_S = 17
CONTINUE_AT_S = 20
# Continue run: the menu waits (screenshot at 4 s), one Continue at 20 s, a screenshot at character select at 40 s.
# login.no_glue_errors' script as it was; login.auto_login's single Continue moves from 3 s to 20 s after the menu,
# so its 90 s limit (measured from the client's start, as before) leaves the client less time, not more.
# launcher-fixes (F25): plus a screenshot at 17 s, before the Continue, so login.fresh_menu_waits can show that no login
# happened between the client's start and the Continue. The screenshot's file time bounds it: the file lands up to
# ~1 s after the act (measured: first login 0.11 s after a 19 s shot's file time), so 3 s ahead leaves a margin.
CONTINUE_GLUE = {"marker": True, "script": [{"t": 4, "act": "shot"}, {"t": PRE_CONTINUE_SHOT_S, "act": "shot"},
                                            {"t": CONTINUE_AT_S, "act": "continue"}, {"t": 40, "act": "shot"}]}
CHARSELECT_WAIT = 120          # no_glue_errors' wait for the character list (as before)
CHARSELECT_ALIVE_S = 20        # spec: the client doesn't quit by itself through 20 s at character select
# Menu-waits run = the backdrop check's restart: no Continue, screenshots ~5 s and ~65 s after the menu showed
# (login.cancel_shows_list's script as it was; the backdrop restart only needs the first one).
MENU_GLUE = {"marker": True, "script": [{"t": 5, "act": "shot"}, {"t": 5 + WAIT_MENU, "act": "shot"}]}


def _session_ok(key: str) -> dict:
    ev = _SESSIONS[key]
    require(not ev.get("session_error"), f"the shared {key} run failed: {ev.get('session_error')}", **ev)
    return ev


def continue_session() -> dict:
    """One Continue run (memoised): stale login file + partial write planted in a fresh scratch play client, real
    Wow.exe via ./play, one Continue; evidence for login.auto_login and login.no_glue_errors."""
    if "continue" in _SESSIONS:
        return _SESSIONS["continue"]
    ev: dict = {"account": P.TEST_USER, "glue_test": CONTINUE_GLUE,
                "input": "no keystrokes or input helpers; one Continue click by the GlueXML test hook",
                "shared_run": "continue (login.auto_login + login.no_glue_errors)"}
    _SESSIONS["continue"] = ev
    scratch = run = None
    try:
        require_given_clear(ev)
        scratch = fresh_scratch("login")
        ev["fresh_scratch"] = {"dir": str(scratch), "existed_before_build": scratch.exists()}
        # a login file left behind by a killed ./play: the next run must remove it before anything else
        ev["fresh_scratch"]["seeded"] = build_scratch(scratch)["seeded"]
        ev["fresh_scratch"] |= {"connection_log_before": (scratch / "Logs" / "connection.log").exists(),
                                "screenshots_before": len(screenshots(scratch)),
                                "account_name_in_config_before": _config_value(scratch, "accountName"),
                                "glue_test": CONTINUE_GLUE}
        stale = frontmenu.write(scratch, [{"name": "HSSTALE", "account": "HSSTALE", "password": "stale123"}], "HSSTALE")
        partial = frontmenu.archive_path(scratch).with_name(frontmenu.archive_path(scratch).name + ".hs-new")
        partial.write_bytes(b"partial write of a killed ./play")          # what a SIGKILL inside write() leaves
        partial.chmod(0o600)
        ev["stale_planted"] = [stale["archive"], str(partial.relative_to(scratch))]
        run = PlayRun("login", glue_test=CONTINUE_GLUE, scratch=scratch)
        run.start()
        up = run.wait_client()
        ev["client_up"] = {k: v for k, v in up.items() if k not in ("t",)}
        require(up.get("up"), "the real Wow.exe never came up", output=run.output()[-2500:], **ev)
        ev["db_ready_while_client_runs"] = db_ready()
        # baseline once the client is up: the play loop's own fixture step (before the client starts) may log the
        # test account in through the protocol probe, so only a newer last_login counts
        base = account_state(P.TEST_USER)
        ev["account_at_client_start"] = base
        ev["t_client_up"] = round(up["t"], 3)
        samples, alive = [], []
        hit = charsel = None
        t_wait = time.time()
        while time.time() - t_wait < max(LOGIN_LIMIT, CHARSELECT_WAIT):
            st = account_state(P.TEST_USER)
            conn = connection_log(scratch)
            alive.append(bool(run.wow_alive()))
            st |= {"t": round(time.time() - up["t"], 1),
                   "chars_received": bool(re.search(r"COP_GET_CHARACTERS code=\d+ result=TRUE", conn))}
            samples.append(st)
            ev.setdefault("account_samples_abs", []).append(
                [round(time.time(), 2), st.get("last_login"), st.get("online"), bool(conn.strip())])
            if run.proc.poll() is not None:          # the client quit and ./play shut down: stop waiting
                hit = hit or {"play_exited": run.proc.returncode, **st}
                break
            if hit is None and (st.get("last_login") or 0) > (base.get("last_login") or 0) \
                    and st.get("online") == 1 and st["chars_received"]:
                hit = st
            if st["chars_received"]:
                charsel = charsel or st
            if hit is not None and charsel is not None:
                break
            time.sleep(1)
        ev["samples"] = samples[-5:]
        ev["login_seen"] = hit
        ev["login_seen_within_limit"] = bool(hit and "play_exited" not in hit and hit["t"] <= LOGIN_LIMIT)
        ev["reached_character_list"] = charsel is not None
        ev["connection_log"] = connection_log(scratch)[-1500:]
        t = time.time()
        while time.time() - t < CHARSELECT_ALIVE_S:
            alive.append(bool(run.wow_alive()))
            time.sleep(1)
        ev["alive_samples"] = {"n": len(alive), "dead": alive.count(False)}
        _wait(lambda: len(screenshots(scratch)) >= 2, 30)
        shots = screenshots(scratch)
        ev["screen"] = marker(shots[0]) if shots else None
        ev["screens"] = [marker(p) for p in shots]
        ev["screenshot_mtimes"] = [round(p.stat().st_mtime, 3) for p in shots]
        ev["connection_log_full"] = connection_log(scratch)
        ev["logs"] = glue_logs(scratch)
        _finish(run, ev)
        ev["stale_removed"] = ((ev["play_record"].get("client") or {}).get("stale_profiles_removed"))
        ev["profiles_used"] = ((ev["play_record"].get("client") or {}).get("profiles") or {}).get("profiles")
        ev["partial_left_after"] = partial.exists()
        _no_secret(ev)
        return ev
    except Exception as e:  # noqa: BLE001  (recorded for both checks; the first caller re-raises)
        ev["session_error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        if run is not None:
            run.cleanup()
        drop_scratch(scratch)


# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "client-mods/glue/**",
    "patches/client/**",
    "harness/hslib/checks/login_checks.py",
    "harness/hslib/checks/pristine_checks.py",
    "harness/hslib/clientcfg.py",
    "harness/hslib/frontmenu.py",
)


# login.auto_login: a scenario of a journey (journey_checks.py), not a check of its own (test-pyramid part 2)
def auto_login(ctx):
    """A normal ./play refuses the test switches $HS_GLUE_TEST and $HS_PLAY_SKIP_PATCHES in patched and stock mode
    (exits nonzero, starts nothing); a stale login file and a partial write are removed first. Then one synthetic
    Continue (the generated file's test flag, test account, scratch play client; no keystrokes): within 90 s the authserver records a
    successful login for the profile's account (last_login), the worldserver opens its session (online = 1) and the
    client receives the character list (its own connection.log: COP_GET_CHARACTERS result=TRUE)."""
    pre: dict = {}
    require_given_clear(pre)
    pre["hook_refused"] = _hook_refused()
    require(pre["hook_refused"]["ok"], "a normal ./play accepted a test switch ($HS_GLUE_TEST / $HS_PLAY_SKIP_PATCHES)",
            **pre)
    if "continue" not in _SESSIONS:
        continue_session()
    ev = dict(_session_ok("continue")) | pre
    require(ev["db_ready_while_client_runs"], "database not reachable while the client runs", **ev)
    require(ev["login_seen_within_limit"],
            f"no login + character list within {LOGIN_LIMIT} s of the client starting", **ev)
    require(ev["stale_removed"] and any(x.get("partial_write_removed") for x in ev["stale_removed"])
            and ev["profiles_used"] == [P.TEST_USER] and not ev["partial_left_after"],
            "the stale login file / partial write wasn't removed at the start of the run", **ev)
    require(not ev["password_in_play_last_json"], "the password appears in play-last.json", **ev)
    require((ev["profiles_removed"] or {}).get("removed") is True, "the generated profile file wasn't removed", **ev)
    require(is_clear(ev["remaining_after"]), "something is still running", **ev)
    return ev


# login.no_glue_errors: a scenario of a journey (journey_checks.py), not a check of its own (test-pyramid part 2)
def no_glue_errors(ctx):
    """Patched client with our GlueXML, menu-waits then Continue: our code runs (marker squares on screen), no Lua
    error is raised at runtime (the test-mode error handler's count, on the login screen and at character select),
    the play client's Logs/GlueXML.log and FrameXML.log hold no errors (absent or empty) and the client doesn't quit
    by itself through 20 s at character select."""
    if "continue" not in _SESSIONS:
        continue_session()
    ev = dict(_session_ok("continue"))
    require(ev["screen"] and ev["screen"]["main_menu_up"] is True, "our GlueXML didn't run (no marker)", **ev)
    require(len(ev["screens"]) >= 2 and all(m["lua_errors"] == 0 for m in ev["screens"]),
            "Lua errors at runtime (the test error handler's marker isn't clean in every screenshot)", **ev)
    require(not any((v or "").strip() for v in ev["logs"].values()), "GlueXML.log / FrameXML.log hold errors",
            **ev)
    require(ev["reached_character_list"], "never reached the character list", **ev)
    require(ev["alive_samples"]["dead"] == 0, "the client quit by itself", **ev)
    require(is_clear(ev["remaining_after"]), "something is still running", **ev)
    return ev


def _conn_times(text: str, year: int) -> list[tuple[float, str]]:
    """connection.log lines as (epoch, line); the client stamps local time "M/D HH:MM:SS.mmm" without a year."""
    out = []
    for l in text.splitlines():
        m = re.match(r"^(\d{1,2})/(\d{1,2}) (\d{2}):(\d{2}):(\d{2})\.(\d{3})\s+(.*)$", l)
        if m:
            mo, d, hh, mi, ss, ms = (int(x) for x in m.groups()[:6])
            t = time.mktime((year, mo, d, hh, mi, ss, 0, 0, -1)) + ms / 1000
            out.append((round(t, 3), m.group(7).strip()))
    return out


def verify_fresh_menu_waits(ev: dict) -> dict:
    """F25 on the Continue run's evidence: a freshly built scratch play client; between the client's start and the
    Continue (bounded by the screenshot taken just before it) the client made no authserver connection (its own
    connection.log) and the account shows no new login; the login comes only after that screenshot."""
    fs = ev.get("fresh_scratch") or {}
    out = {"fresh_scratch": fs, "t_client_up": ev.get("t_client_up"), "screens": ev.get("screens"),
           "screenshot_mtimes": ev.get("screenshot_mtimes"), "account_at_client_start": ev.get("account_at_client_start")}
    require(fs.get("existed_before_build") is False and fs.get("connection_log_before") is False
            and fs.get("screenshots_before") == 0 and not fs.get("account_name_in_config_before"),
            "GIVEN not met: the scratch play client wasn't freshly built and never used", **out)
    glue = fs.get("glue_test") or {}
    acts = sorted(glue.get("script", []), key=lambda a: a["t"])
    shots = [a["t"] for a in acts if a["act"] == "shot"]
    out["script"] = acts
    # the run's own Continue (CONTINUE_AT_S) and the screenshot taken before it; anything else in the script is
    # what the check is looking for (e.g. a Continue injected earlier)
    require({"t": CONTINUE_AT_S, "act": "continue"} in acts and PRE_CONTINUE_SHOT_S in shots,
            "the run's script lacks its Continue or the screenshot before it", **out)
    idx = shots.index(PRE_CONTINUE_SHOT_S)         # the screenshot before the Continue
    mt = ev.get("screenshot_mtimes") or []
    require(len(mt) > idx and len(ev.get("screens") or []) > idx, "the screenshot before the Continue is missing",
            **out)
    t_pre = mt[idx]
    pre = ev["screens"][idx]
    t0 = ev["t_client_up"]
    year = time.localtime(t0).tm_year
    conn = _conn_times(ev.get("connection_log_full") or "", year)
    first = conn[0] if conn else None
    out |= {"pre_continue_screenshot": {"t": t_pre, "after_client_start_s": round(t_pre - t0, 1), "marker": pre},
            "first_connection_log_line": first,
            "first_connection_after_pre_shot_s": round(first[0] - t_pre, 2) if first else None,
            "connection_lines_before_pre_shot": [c for c in conn if c[0] <= t_pre][:5]}
    base = (ev.get("account_at_client_start") or {}).get("last_login")
    early = [s for s in ev.get("account_samples_abs") or [] if s[0] <= t_pre and (s[1] != base or s[2])]
    out["account_samples_before_pre_shot"] = [s for s in ev.get("account_samples_abs") or [] if s[0] <= t_pre][-5:]
    out["account_changes_before_pre_shot"] = early
    require(not out["connection_lines_before_pre_shot"],
            "the client connected to the authserver before the Continue (connection.log)", **out)
    require(not early, "the account logged in before the Continue (last_login / online)", **out)
    require(pre.get("main_menu_up") is True, "the menu wasn't up before the Continue (screenshot marker)", **out)
    require(first and first[0] > t_pre and ev.get("login_seen"),
            "no login came after the Continue (nothing to tell a waiting menu from a dead one)", **out)
    return out


# login.fresh_menu_waits: a scenario of a journey (journey_checks.py), not a check of its own (test-pyramid part 2)
def fresh_menu_waits(ctx):
    """F25, on the shared Continue run: a freshly built, never-used scratch play client shows no authserver login
    between the client's start and the Continue (connection.log and the account's last_login/online up to the
    screenshot taken just before the Continue); the login follows the Continue."""
    if "continue" not in _SESSIONS:
        continue_session()
    return verify_fresh_menu_waits(_session_ok("continue"))


# ---------------------------------------------------------------- backdrop + menu waits

def _config_value(client_dir: Path, key: str) -> str | None:
    p = client_dir / "WTF" / "Config.wtf"
    m = re.search(rf'^SET {key} "([^"]*)"', p.read_text(errors="replace"), re.M) if p.exists() else None
    return m.group(1) if m else None


def menu_session() -> dict:
    """The backdrop runs (memoised): run 1 picks TBC, Continues and enters the world; run 2 restarts with nothing
    picked and no Continue, and waits ~65 s on the menu. Evidence for login.backdrop_choice (runs 1 and 2) and
    login.cancel_shows_list (run 2). Run 2 happens whatever run 1 showed, from "nothing running" (verified)."""
    if "menu" in _SESSIONS:
        return _SESSIONS["menu"]
    ev: dict = {"shared_run": "menu (login.backdrop_choice runs 1+2, login.cancel_shows_list run 2)"}
    _SESSIONS["menu"] = ev
    scratch = fresh_scratch("backdrop")
    try:
        r1: dict = {}
        ev["run1"] = r1
        require_given_clear(r1)
        g1 = {"marker": True, "script": [{"t": 3, "act": "shot"}, {"t": 4, "act": "backdrop:TBC"},
                                         {"t": 7, "act": "shot"}, {"t": 9, "act": "continue"}],
              "enter_world_after_charlist": 3}
        r1["glue_test"] = g1
        run = PlayRun("backdrop1", glue_test=g1, scratch=scratch)
        try:
            run.start()
            up = run.wait_client()
            r1["client_up"] = {k: v for k, v in up.items() if k != "t"}
            if up.get("up"):
                stored = _wait(lambda: _config_value(scratch, "accountList"), 180, step=2)
                shots = screenshots(scratch)
                r1 |= {"screens": [marker(p) for p in shots[:2]], "stored_accountList": stored,
                       "stored_accountName": _config_value(scratch, "accountName")}
            else:
                r1["output"] = run.output()[-2500:]
            _finish(run, r1)
        finally:
            run.cleanup()

        r2: dict = {"glue_test": MENU_GLUE, "account": P.TEST_USER}
        ev["run2"] = r2
        require_given_clear(r2)
        clear_logs(scratch)
        before = {p.name for p in screenshots(scratch)}
        r2["screenshots_before"] = sorted(before)
        run = PlayRun("backdrop2", glue_test=MENU_GLUE, scratch=scratch)
        try:
            run.start()
            up = run.wait_client()
            r2["client_up"] = {k: v for k, v in up.items() if k != "t"}
            require(up.get("up"), "run 2: the real Wow.exe never came up", output=run.output()[-2500:], **ev)
            base = account_state(P.TEST_USER)
            r2["account_before"] = base
            samples = []

            def done():
                st = account_state(P.TEST_USER)
                samples.append(st | {"t": round(time.time() - up["t"], 1)})
                return len([p for p in screenshots(scratch) if p.name not in before]) >= 2
            _wait(done, WAIT_MENU + 90, step=2)
            shots = [p for p in screenshots(scratch) if p.name not in before]
            r2["screens"] = [marker(p) for p in shots]
            r2["samples_first_last"] = [samples[0], samples[-1]] if samples else []
            r2["login_attempts"] = [s for s in samples
                                    if s.get("last_login") != base.get("last_login") or s.get("online")]
            r2["wow_alive_at_end"] = bool(run.wow_alive())
            r2["connection_log"] = connection_log(scratch)[-800:]
            r2["stored_accountList"] = _config_value(scratch, "accountList")
            _finish(run, r2)
        finally:
            run.cleanup()
        return ev
    except Exception as e:  # noqa: BLE001  (recorded for both checks; the first caller re-raises)
        ev["session_error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        drop_scratch(scratch)


# login.cancel_shows_list: a scenario of a journey (journey_checks.py), not a check of its own (test-pyramid part 2)
def menu_waits(ctx):
    """With no Continue action, no login is attempted for at least 60 s after the menu appears (HSTEST last_login
    unchanged, never online) and the main menu with Continue stays up (marker squares in the client's own
    screenshots at ~5 s and ~65 s after the menu showed)."""
    if "menu" not in _SESSIONS:
        menu_session()
    sess = _session_ok("menu")
    ev = dict(sess["run2"]) | {"shared_run": sess["shared_run"]}
    require(len(ev["screens"]) >= 2, "the client didn't take both screenshots", **ev)
    require(all(m["main_menu_up"] is True for m in ev["screens"]),
            "the main menu wasn't up in both screenshots", **ev)
    require(all(m["lua_errors"] == 0 for m in ev["screens"]), "Lua errors while the menu waited", **ev)
    require(not ev["login_attempts"], "a login happened without Continue", **ev)
    require("COP_AUTHENTICATE" not in ev["connection_log"], "the client attempted a login (connection.log)", **ev)
    require(ev["wow_alive_at_end"], "the client quit by itself", **ev)
    require(is_clear(ev["remaining_after"]), "something is still running", **ev)
    return ev


# login.backdrop_choice: a scenario of a journey (journey_checks.py), not a check of its own (test-pyramid part 2)
def backdrop_choice(ctx):
    """Picking TBC in the menu is applied (marker: TBC model and music requested by GlueXML), stored in the client's
    own settings when it saves them (WTF/Config.wtf accountList HSBDTBC, written on entering the world), and on the
    next start, with nothing picked, the TBC model and music are requested again."""
    if "menu" not in _SESSIONS:
        menu_session()
    ev = dict(_session_ok("menu"))
    r1 = ev["run1"]
    require((r1.get("client_up") or {}).get("up"), "run 1: the real Wow.exe never came up", **ev)
    s = r1.get("screens") or []
    require(len(s) == 2 and s[0]["backdrop"] == "WOTLK" and s[1]["backdrop"] == "TBC" and s[1]["music"] == "TBC",
            "run 1: default WotLK then picked TBC not both seen", **ev)
    require(r1["stored_accountList"] and "HSBDTBC" in r1["stored_accountList"],
            "run 1: the choice wasn't stored in WTF/Config.wtf", **ev)
    s2 = ev["run2"]["screens"]
    require(s2 and s2[0]["backdrop"] == "TBC" and s2[0]["music"] == "TBC",
            "run 2 (restart): TBC model and music weren't requested", **ev)
    require(is_clear(ev["run2"]["remaining_after"]), "something is still running", **ev)
    return ev


# ---------------------------------------------------------------- Continue context line

CTX_A, CTX_B = "HSCTXA", "HSCTXB"               # scratch accounts (never the player's)
CTX_PASS = "ctx12345"
CTX_CHARS = (("Hsctxwar", 1, 1), ("Hsctxpal", 1, 2))     # name, race (Human), class (Warrior, Paladin)


def _dbc_class_names_raw(path: Path) -> dict[int, str]:
    """Independent ChrClasses.dbc read (field 0 id, field 4 enUS name; DBCfmt.h ChrClassesEntryfmt)."""
    b = path.read_bytes()
    _, n, nf, rs, ss = struct.unpack("<4s4I", b[:20])
    strings = b[20 + n * rs:]
    out = {}
    for i in range(n):
        f = struct.unpack_from(f"<{nf}I", b, 20 + i * rs)
        out[f[0]] = strings[f[4]:strings.index(b"\0", f[4])].decode()
    return out


@check("login.continue_context", order=128, capability=CAP, stack="up", layer="integration")
def continue_context(ctx):
    """The launcher's line under Continue names the account's most recently played character (latest logout_time) as
    "<name> · Level <n> <class>" with the class from ChrClasses.dbc (a renamed class in a scratch DBC shows up), adds
    " · <profile>" only with several profiles, shows "No characters yet" for an account without characters, and
    carries no credentials."""
    from .. import db, fixtures as F, soap
    from ..probe import client as probe
    ev: dict = {"accounts": [CTX_A, CTX_B]}
    real = None
    try:
        real = json.loads(play.PLAYER_FILE.read_text())["account"].upper()
    except (FileNotFoundError, ValueError, KeyError):
        pass
    require(real not in (CTX_A, CTX_B), "scratch account collides with the player's", **ev)
    tmp = Path(tempfile.mkdtemp(prefix="hs-ctx-"))
    try:
        for acct in (CTX_A, CTX_B):
            ev.setdefault("create", {})[acct] = play.ensure_player_account({"account": acct, "password": CTX_PASS})
        aid = int(db.scalar(f"SELECT id FROM account WHERE username='{CTX_A}'", "auth"))
        _, ws, _ = probe.open_world(CTX_A, CTX_PASS)
        with ws:
            have = {c["name"].lower() for c in ws.char_enum()}
            for name, race, cls in CTX_CHARS:
                if name.lower() not in have:
                    ev.setdefault("char_create", {})[name] = ws.char_create(name, race, cls)
        F.poll(lambda: len(db.query(f"SELECT guid FROM characters WHERE account={aid}", "characters")) >= 2,
               bool, timeout=20)
        dbc_names = _dbc_class_names_raw(P.CLIENT_DATA / "dbc" / "ChrClasses.dbc")
        ev["dbc_names"] = dbc_names
        profiles = [{"name": CTX_A, "account": CTX_A, "password": CTX_PASS},
                    {"name": CTX_B, "account": CTX_B, "password": CTX_PASS}]
        D = frontmenu.DOT
        cases = {}
        for latest in ("Hsctxpal", "Hsctxwar"):           # flip which one was played last
            other = "Hsctxwar" if latest == "Hsctxpal" else "Hsctxpal"
            db.query(f"UPDATE characters SET logout_time=1000 WHERE name='{other}'", "characters")
            db.query(f"UPDATE characters SET logout_time=2000 WHERE name='{latest}'", "characters")
            row = db.query(f"SELECT name, level, class FROM characters WHERE name='{latest}'", "characters")[0]
            want_line = f"{row[0]} {D} Level {row[1]} {dbc_names[int(row[2])]}"
            got_two = frontmenu.with_context(profiles)
            got_one = frontmenu.with_context(profiles[:1])
            cases[latest] = {"expected_line": want_line, "two_profiles": [p["context"] for p in got_two],
                             "one_profile": got_one[0]["context"]}
            require(got_two[0]["context"] == f"{want_line} {D} {CTX_A}", f"latest={latest}: wrong line (2 profiles)",
                    cases=cases, **ev)
            require(got_two[1]["context"] == f"{frontmenu.NO_CHARACTERS} {D} {CTX_B}",
                    "account without characters doesn't read 'No characters yet'", cases=cases, **ev)
            require(got_one[0]["context"] == want_line, f"latest={latest}: wrong line (1 profile)", cases=cases, **ev)
        ev["cases"] = cases
        # the class name comes from ChrClasses.dbc: rename Warrior in a scratch copy and it must show up
        raw = (P.CLIENT_DATA / "dbc" / "ChrClasses.dbc").read_bytes()
        (tmp / "ChrClasses.dbc").write_bytes(raw.replace(b"\0Warrior\0", b"\0Warxior\0", 1))
        ev["scratch_dbc_line"] = frontmenu.with_context(profiles[:1], dbc_dir=tmp)[0]["context"]
        require(ev["scratch_dbc_line"].endswith("Warxior"), "class name doesn't follow ChrClasses.dbc", **ev)
        # no hardcoded class table in our sources, no credentials in the line or its data
        srcs = [P.HARNESS / "hslib" / "frontmenu.py", P.ROOT / "client-mods" / "glue" / "HSFrontMenu.lua"]
        ev["hardcoded_class_names"] = {str(f.relative_to(P.ROOT)): [n for n in dbc_names.values() if n in f.read_text()]
                                       for f in srcs}
        # a table would name several classes; one example in a comment ("Level 23 Paladin") is not a source
        require(not any(len(v) >= 2 for v in ev["hardcoded_class_names"].values()),
                "our sources carry a class-name table", **ev)
        lua = frontmenu.render(frontmenu.with_context(profiles), CTX_A, about=frontmenu.about_info()).decode()
        ctx_lines = re.findall(r'context = "([^"]*)"', lua)
        about = re.search(r"HS_ABOUT = (\{.*?\});", lua).group(1)
        ev["rendered_contexts"] = ctx_lines
        ev["about_has_password"] = CTX_PASS in about
        require(len(ctx_lines) == 2 and not any(CTX_PASS in c for c in ctx_lines) and not ev["about_has_password"],
                "credentials in the context line or About data", **ev)
        return ev
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        ev["cleanup"] = [_delete_scratch(CTX_A), _delete_scratch(CTX_B)]


# ---------------------------------------------------------------- first start of a freshly built play client (B32)

# One screenshot on the front menu, one Continue, one screenshot at character select.
FIRST_START_GLUE = {"marker": True, "script": [{"t": 5, "act": "shot"}, {"t": 8, "act": "continue"},
                                               {"t": 35, "act": "shot"}]}
FIRST_START_MENU_S = 120         # the front menu's screenshot must exist this long after the client came up
FIRST_START_CHARS_S = 90         # and the character list must have arrived this long after it


def _config_values(client_dir: Path, keys) -> dict:
    p = Path(client_dir) / "WTF" / "Config.wtf"
    text = p.read_text(errors="replace") if p.exists() else ""
    out = {}
    for k in keys:
        m = re.findall(rf'^\s*SET\s+{k}\s+"([^"]*)"', text, re.M | re.I)
        out[k] = m[-1] if m else None
    return out


# A base whose saved settings already set two of them, written the way a player's or the game's file may look: other
# case in key names, CRLF line endings, values opposite to the defaults, and a line that isn't one of them.
PRESET_CONFIG = b'SET gxWindow "1"\r\nSET movie "1"\r\nset READEULA "0"\r\n'
PRESET_VALUES = {"movie": "1", "readEULA": "0"}


def _preset_settings_kept(want: dict) -> dict:
    """client.first_start_settings' "already set are left unchanged" clause: build a play client from a scratch clone
    of client/ whose WTF/Config.wtf is PRESET_CONFIG; the built file must start with it byte-for-byte, keep its values,
    and add only the missing keys (each once, CRLF)."""
    from .. import clientbuild
    from .pristine_checks import clone_client
    base, dest = fresh_scratch("firststart-base"), fresh_scratch("firststart-preset")
    ev: dict = {"preset": PRESET_CONFIG.decode()}
    try:
        clone_client(base)
        shutil.rmtree(base / "WTF", ignore_errors=True)
        (base / "WTF").mkdir()
        (base / "WTF" / "Config.wtf").write_bytes(PRESET_CONFIG)
        b = clientbuild.build(dest=dest, base=base)
        got = (dest / "WTF" / "Config.wtf").read_bytes()
        ev["built"] = got.decode("latin-1")
        ev["state"] = (b.get("stamp") or {}).get("state", {}).get("first_start_settings")
        vals = _config_values(dest, list(want))
        ev["values"] = vals
        missing = [k for k in want if k not in PRESET_VALUES]
        tail = got[len(PRESET_CONFIG):]
        ev["appended"] = tail.decode("latin-1")
        expect_tail = b"".join(f'SET {k} "{want[k]}"\r\n'.encode() for k in missing)
        require(got.startswith(PRESET_CONFIG),
                "the build changed settings that were already set (the base's Config.wtf isn't a byte-for-byte "
                "prefix of the built one)", **ev)
        require(all(vals[k] == v for k, v in PRESET_VALUES.items()),
                f"already-set values changed: {dict((k, vals[k]) for k in PRESET_VALUES)}", **ev)
        require(tail == expect_tail, f"the build should append exactly the missing settings {missing} (CRLF), "
                "and nothing for the ones already set", **ev)
        return ev
    finally:
        drop_scratch(dest)
        drop_scratch(base)


def first_start_session(first_start: bool = True, real_run: bool = True) -> dict:
    """client.first_start_settings' run. first_start=False is the counterexample (the build leaves the settings out)."""
    from .. import clientbuild, clientcfg
    want, realm = clientcfg.first_start_settings()
    keys = list(want)
    ev: dict = {"first_start_settings_expected": want, "realm": realm}
    scratch = fresh_scratch("firststart")
    run = None
    try:
        require_given_clear(ev)
        # GIVEN: the base's saved settings (client/WTF, carried over by the build) set none of them, as on a friend's
        # freshly bootstrapped client (an empty client/WTF; at most the stock-mode lines)
        ev["given_base_config"] = _config_values(P.CLIENT, keys)
        require(not any(ev["given_base_config"].values()),
                "GIVEN not met: client/WTF/Config.wtf already sets a first-start setting, so the build can't be "
                "shown to add it", **ev)
        # WHEN: the play client is built the way bootstrap's playclient step / `hs client-build` builds it
        b = clientbuild.build(dest=scratch, first_start=first_start)
        ev["build"] = {"built": b["built"], "state": b["stamp"].get("state")}
        ev["built_config"] = _config_values(scratch, keys)
        wrong = {k: ev["built_config"][k] for k in keys if ev["built_config"][k] != want[k]}
        require(b["built"] and not wrong, f"the freshly built play client's WTF/Config.wtf lacks or differs in {wrong}", **ev)
        # AND: settings that were already set are left unchanged (structural; a scratch base, never client/)
        ev["already_set"] = _preset_settings_kept(want)
        if not real_run:
            return ev
        # THEN (real client): the front menu shows without the intro movie or the EULA/TOS screens, and one Continue
        # reaches the character list without the realm list
        clear_logs(scratch)
        run = PlayRun("firststart", glue_test=FIRST_START_GLUE, scratch=scratch, seed=False)
        run.start()
        up = run.wait_client()
        ev["client_up"] = {k: v for k, v in up.items() if k not in ("t",)}
        require(up.get("up"), "the real Wow.exe never came up", output=run.output()[-2500:], **ev)
        ev["config_at_launch"] = _config_values(scratch, keys + ["gxResolution"])
        shot = _wait(lambda: screenshots(scratch), FIRST_START_MENU_S)
        ev["menu_shot_after_s"] = round(time.time() - up["t"], 1) if shot else None
        chars = _wait(lambda: re.search(r"COP_GET_CHARACTERS code=\d+ result=TRUE", connection_log(scratch)),
                      FIRST_START_CHARS_S)
        _wait(lambda: len(screenshots(scratch)) >= 2, 30)
        shots = screenshots(scratch)
        ev["screens"] = [marker(p) for p in shots]
        conn = connection_log(scratch)
        ev["connection_log"] = conn[-2000:]
        ev["realm_list_polls"] = len(re.findall(r"COP_GET_REALMS", conn))
        ev["logs"] = glue_logs(scratch)
        _finish(run, ev)
        first = ev["screens"][0] if ev["screens"] else {}
        require(first.get("main_menu_up") is True,
                "no screenshot of the front menu within the limit (the intro movie or the EULA/Terms of Use screens "
                "came first)", **ev)
        require(chars, "Continue didn't reach the character list (the realm list came up, or the login failed)", **ev)
        gx = ev["config_at_launch"].get("gxResolution")
        if gx and re.fullmatch(r"\d+x\d+", gx):
            # the window (and so the screenshot) is scaled to fit the screen; its aspect ratio is the configured
            # one's. Hardware detection's replacement is 4:3 (1024x768, measured B31), far outside the tolerance.
            w, h = (int(v) for v in gx.split("x"))
            sw, sh = first.get("size") or (0, 1)
            ev["resolution"] = {"config": gx, "screenshot": [sw, sh], "config_aspect": round(w / h, 4),
                                "screenshot_aspect": round(sw / sh, 4)}
            require(abs((sw / sh) / (w / h) - 1) <= 0.02,
                    "the client didn't use the configured resolution's aspect ratio (first-start hardware detection "
                    "replaced it?)", **ev)
        require(is_clear(ev["remaining_after"]), "something from this repo is still running", **ev)
        return ev
    finally:
        if run is not None:
            run.cleanup()
        drop_scratch(scratch)


# client.first_start_settings: a scenario of a journey (journey_checks.py), not a check of its own (test-pyramid part 2)
def first_start_settings(ctx):
    """A play client built from a client with no saved settings (a friend's first bootstrap) gets the first-start
    settings where absent (clientcfg.FIRST_START_SETTINGS + realmName: no intro movie, EULA/TOS accepted, no hardware
    detection, the realm chosen) while a base whose Config.wtf already sets some of them keeps those lines byte-for-byte
    (_preset_settings_kept), and the real client then shows the front menu first and reaches character select
    with one Continue, at the configured resolution. Counterexample: the build without them fails the first clause."""
    return first_start_session()
