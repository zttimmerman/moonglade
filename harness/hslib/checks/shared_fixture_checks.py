"""test-pyramid shared-server fixture (harness-control "Integration tests share one server start", "Reset between
shared-server tests is checked, cheap and fail-closed", "Warm journeys reuse a running stack only in the harness").

fixture.shared_server and fixture.reset_leak judge nested `hs run`s of self-test checks that exist only when the
test switch $HS_TEST_FIXTURE_SELFTEST is set inside a harness run (cli refuses it anywhere else):
- fixture.selftest.c1..c5: character-scoped, default profile; each proves its fixture identity exists;
- fixture.selftest.p1: character-scoped, the scaling-off profile (one worldserver restart);
- fixture.selftest.leak: pauses the world and changes the save interval without registering an undo.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .. import db, paths as P, play, saves as S, soap, stack
from ..registry import check, require

CAP = "harness-control"
HS = str(P.HARNESS / "hs")
PLAY = str(P.ROOT / "play")
SELFTEST_ENV = "HS_TEST_FIXTURE_SELFTEST"
RESET_MEDIAN_MAX_S, RESET_MAX_S = 1.0, 3.0

COVERS = (
    "harness/hslib/fixture.py",
    "harness/hslib/sharedfix.py",
    "harness/hslib/play.py",
    "harness/hslib/checks/shared_fixture_checks.py",
    "play",
)


def _selftest_on() -> bool:
    return bool(os.environ.get(SELFTEST_ENV)) and os.environ.get("HS_HARNESS_RUN") == "1"


def _identity_rows(ctx: dict) -> dict:
    i = ctx.get("identity")
    require(i is not None, "no fixture identity in ctx")
    rows = db.query(f"SELECT name, account FROM characters WHERE name IN ('{i.char}', '{i.alt}')", "characters")
    acct = db.scalar(f"SELECT id FROM account WHERE username='{i.user}'", "auth")
    ok = len(rows) == 2 and acct and all(int(r[1]) == int(acct) for r in rows)
    require(ok, "the fixture identity's account and characters don't exist", identity=i.user, rows=rows, account=acct)
    return {"identity": {"account": i.user, "characters": [i.char, i.alt]}, "account_id": int(acct),
            "running_profile": (stack.running_profile() or {}).get("profile")}


if _selftest_on():
    for _n in range(1, 6):
        check(f"fixture.selftest.c{_n}", order=400 + _n, capability=CAP, stack="up", layer="integration",
              scope="character", covers=COVERS)(lambda ctx: _identity_rows(ctx))
    check("fixture.selftest.p1", order=410, capability=CAP, stack="up", layer="integration", scope="character",
          profile="scaling-off", covers=COVERS)(lambda ctx: _identity_rows(ctx))

    @check("fixture.selftest.leak", order=60, capability=CAP, stack="up", layer="integration", covers=COVERS)
    def _leak(ctx):
        """Pauses the world and changes the save interval without registering an undo (fixture.reset_leak)."""
        return {"pause": soap.gm("moonglade pause on", timeout=10).evidence(),
                "interval": soap.gm("moonglade save interval 7", timeout=10).evidence()}


def _nested(only: list[str], tag: str, timeout: int = 900) -> dict:
    d = Path(tempfile.mkdtemp(prefix=f"hs-fx-{tag}-")).resolve()
    cmd = [HS, "run", "--tier", "full", "--lanes", "1", "--leave-up", "--results-dir", str(d)]
    for o in only:
        cmd += ["--only", o]
    env = {**os.environ, SELFTEST_ENV: "1"}
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        doc = json.loads((d / "results.json").read_text())
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        return {"error": f"{type(e).__name__}: {e}", "wall_s": round(time.time() - t0, 1)}
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return {"rc": r.returncode, "doc": doc, "wall_s": round(time.time() - t0, 1), "tail": r.stdout[-1500:]}


def _player_rows_digest() -> dict | None:
    try:
        return S.player_rows()
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


@check("fixture.shared_server", order=29, capability=CAP, stack="down", layer="gate")
def shared_server(ctx):
    """A nested run of five character-scoped shared-server tests on the default profile and one on the scaling-off
    profile: its stack timeline shows exactly one stack start for the default-profile tests and one worldserver
    restart for the other profile; each test's evidence names characters created for it and used by no other test;
    after the run none of those characters remain; PLAYER's characters are byte-identical before and after."""
    # PLAYER's rows before: the database alone (the nested run must start from a stopped stack)
    stack.mysql_up()
    before = _player_rows_digest()
    stack.mysql_down()
    n = _nested(["fixture.selftest.*"], "shared")
    require("doc" in n, "the nested run didn't produce results", **n)
    doc = n["doc"]
    recs = {r["id"]: r for r in doc["checks"]}
    tests = [f"fixture.selftest.c{i}" for i in range(1, 6)] + ["fixture.selftest.p1"]
    ev = {"nested_rc": n["rc"], "nested_wall_s": n["wall_s"],
          "statuses": {t: (recs.get(t) or {}).get("status") for t in tests},
          "stack_transitions": doc["meta"].get("stack_transitions"), "fixture": doc["meta"].get("fixture")}
    require(all(ev["statuses"][t] == "pass" for t in tests), "a self-test check didn't pass", **ev)
    trans = ev["stack_transitions"] or []
    ups = [t for t in trans if "up" in t]
    restarts = [t for t in trans if "profile" in t]
    ev["starts"], ev["profile_restarts"] = len(ups), len(restarts)
    require(len(ups) == 1 and len(restarts) == 1 and trans.index(ups[0]) < trans.index(restarts[0]),
            "the stack timeline isn't one start and then one restart for the other profile", **ev)
    names = {}
    for t in tests:
        e = (recs[t].get("evidence") or {}).get("identity") or {}
        names[t] = (e.get("account"), tuple(e.get("characters") or ()))
    ev["identities"] = names
    chars = [c for _, cs in names.values() for c in cs]
    accts = [a for a, _ in names.values()]
    require(all(a and len(cs) == 2 for a, cs in names.values()) and len(set(chars)) == len(chars)
            and len(set(accts)) == len(accts), "the tests' characters aren't each their own", **ev)
    # the nested run left its stack up (--leave-up) so the rows can be read; the runner brings it down after
    left = db.query("SELECT name FROM characters WHERE name IN (" + ",".join(f"'{c}'" for c in chars) + ")",
                    "characters")
    accts_left = db.query("SELECT username FROM account WHERE username IN (" + ",".join(f"'{a}'" for a in accts) + ")",
                          "auth")
    after = _player_rows_digest()
    ev |= {"characters_left": left, "accounts_left": accts_left, "player_rows_equal": before == after,
           "player_rows_error": (before or {}).get("error") if isinstance(before, dict) else None}
    require(not left and not accts_left, "fixture characters or accounts remain after the run", **ev)
    require(before == after and not (isinstance(before, dict) and before.get("error")),
            "PLAYER's characters changed (or couldn't be read)", **ev)
    return ev


@check("fixture.reset_leak", order=29, capability=CAP, stack="down", layer="gate")
def reset_leak(ctx):
    """A nested run of a scratch test that pauses the world and changes the save interval without an undo,
    followed by gm.server_info: the scratch test fails naming "world left paused" and "save interval changed", the
    worldserver restarts before gm.server_info, which passes; the run records its reset times, median <= 1 s and
    max <= 3 s."""
    n = _nested(["fixture.selftest.leak", "gm.server_info"], "leak")
    require("doc" in n, "the nested run didn't produce results", **n)
    doc = n["doc"]
    recs = {r["id"]: r for r in doc["checks"]}
    fx = doc["meta"].get("fixture") or {}
    leak, gm = recs.get("fixture.selftest.leak") or {}, recs.get("gm.server_info") or {}
    restarts = fx.get("restarts") or []
    tl = {e["id"]: e for e in doc["meta"].get("timeline") or [] if e.get("id")}
    ev = {"leak": {k: leak.get(k) for k in ("status", "message")}, "gm_server_info": gm.get("status"),
          "restarts": restarts, "resets": fx.get("resets"), "reset_median_s": fx.get("reset_median_s"),
          "reset_max_s": fx.get("reset_max_s"), "order": [r["id"] for r in doc["checks"]][:6]}
    msg = leak.get("message") or ""
    require(leak.get("status") == "fail" and "world left paused" in msg and "save interval changed" in msg,
            "the leaking test didn't fail naming both leaks", **ev)
    require(len(restarts) == 1 and restarts[0]["after"] == ["fixture.selftest.leak"],
            "the worldserver wasn't restarted once, after the leaking test", **ev)
    require(gm.get("status") == "pass" and tl.get("gm.server_info", {}).get("start", 0) >=
            tl.get("fixture.selftest.leak", {}).get("end", 1e18), "gm.server_info didn't pass after the restart", **ev)
    require(fx.get("reset_median_s") is not None and fx["reset_median_s"] <= RESET_MEDIAN_MAX_S
            and fx["reset_max_s"] <= RESET_MAX_S, "reset times missing or over budget", **ev)
    return ev


# ============================================================================ fixture.keep_stack_refused

def _sample_while(proc: subprocess.Popen, timeout: float = 30) -> dict:
    """Sample this lane's stack and the Wine prefix at 10 Hz until `proc` exits."""
    from .. import launch
    seen, n = set(), 0
    t0 = time.time()
    while proc.poll() is None and time.time() - t0 < timeout:
        st = stack.stack_processes()
        for k, v in st.items():
            seen |= {f"{k}:{p}" for p in v}
        seen |= {f"wine:{p.get('pid')}" for p in launch.prefix_processes()}
        n += 1
        time.sleep(0.1)
    return {"samples": n, "processes_seen": sorted(seen), "rc": proc.wait(timeout=5)}


@check("fixture.keep_stack_refused", order=29, capability=CAP, stack="down", layer="gate")
def keep_stack_refused(ctx):
    """With HS_PLAY_KEEP_STACK=1: a normal ./play, ./play --account player inside a harness run, and the app's
    play loop (./play as Moonglade.app starts it) each exit nonzero naming the switch before any server or Wine
    process starts (sampled at 10 Hz), and the player's files are untouched; inside a harness run with the test
    account and a running stack, ./play with a stand-in exits 0 and the stack's pids are unchanged afterwards."""
    from ..checks.play_checks import StandIn, PINNED
    from .. import backups
    fp0 = {"player_file": _fp(P.PLAYER_FILE), "settings": _fp(P.ROOT / "env" / "settings.json"),
           "backups": backups.fingerprint(backups.PLAYER_ROOT)}
    base = {k: v for k, v in os.environ.items() if k not in ("HS_HARNESS_RUN",)}
    cases = {
        "normal_play": ([PLAY], base),
        "player_account_in_harness": ([PLAY, "--account", "player", "--allow-test-client-dir"], {**base, "HS_HARNESS_RUN": "1"}),
        "app_play_loop": ([PLAY], {**base, play.FROM_APP_ENV: "1"}),
        "test_account_without_flag": ([PLAY, "--account", "test"], {**base, "HS_HARNESS_RUN": "1"}),
    }
    out = {}
    for name, (cmd, env) in cases.items():
        p = subprocess.Popen(cmd, cwd=P.ROOT, env={**env, **PINNED, play.KEEP_STACK_ENV: "1"},
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        smp = _sample_while(p)
        text = p.stdout.read()
        out[name] = smp | {"output": text[-400:], "names_switch": play.KEEP_STACK_ENV in text}
    ev = {"refusals": out}
    bad = [k for k, v in out.items() if v["rc"] == 0 or not v["names_switch"] or v["processes_seen"]]
    require(not bad, f"not refused before anything started: {bad}", **ev)
    # inside the harness, test account, running stack: kept up, same pids
    ctx.setdefault("stack_transitions", []).append({"up": stack.up()})
    pids0 = stack.stack_processes()
    s = StandIn("keepstack")
    try:
        p = s.start_play("--allow-test-client-dir", env={play.KEEP_STACK_ENV: "1", "HS_HARNESS_RUN": "1"})
        up = s.wait_client_up(timeout=300)
        s.stop_file.write_text("stop")
        rc, waited = s.wait_exit(300)
        pids1 = stack.stack_processes()
        ev["warm"] = {"client_up": up, "rc": rc, "waited_s": waited, "pids_before": pids0, "pids_after": pids1,
                      "output_tail": s.output()[-600:]}
    finally:
        s.cleanup() if (s.proc and s.proc.poll() is None) else shutil.rmtree(s.dir, ignore_errors=True)
    fp1 = {"player_file": _fp(P.PLAYER_FILE), "settings": _fp(P.ROOT / "env" / "settings.json"),
           "backups": backups.fingerprint(backups.PLAYER_ROOT)}
    ev["player_files_unchanged"] = fp0 == fp1
    require(ev["warm"]["rc"] == 0 and ev["warm"]["client_up"].get("up") and pids0 == pids1 and all(pids1.values()),
            "the warm ./play didn't exit 0 with the stack's pids unchanged", **ev)
    require(fp0 == fp1, "the player's files changed", **ev)
    return ev


def _fp(p: Path):
    try:
        st = p.stat()
        return [st.st_size, st.st_mtime_ns]
    except OSError:
        return None
