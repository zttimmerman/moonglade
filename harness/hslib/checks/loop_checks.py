"""test-loop-speedups checks (openspec/changes/test-loop-speedups, capability harness-reporting): the GUI preflight
follows the GUI rule, runs keep the display awake, the last full run is kept, a killed run's orphans are healed, and
the nightly flake hunt. Every check here stubs what it can (probes, runs, launchctl) and never locks the screen,
installs the LaunchAgent, or touches the player's ./play, app, account or files."""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .. import paths as P, procs, runreg, runner
from ..registry import check, require

HS = str(P.HARNESS / "hs")
CAP = "harness-reporting"


def _scratch(tag: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"hs-{tag}-")).resolve()


def _env(**extra) -> dict:
    """A nested run's environment: this run's, with every report.gui_preflight switch removed, plus `extra`."""
    from ..guienv import PREFLIGHT_TEST_ENV
    return {k: v for k, v in os.environ.items() if k not in PREFLIGHT_TEST_ENV} | {k: str(v) for k, v in extra.items()}


# ============================================================================ report.gui_preflight

LOCKED = {"locked": True, "display_asleep": False, "battery": {"on_battery": False, "percent": 100}}
ASLEEP = {"locked": False, "display_asleep": True, "battery": {"on_battery": False, "percent": 100}}
FINE = {"locked": False, "display_asleep": False, "battery": {"on_battery": False, "percent": 100}}
LOW_BATT = {"locked": False, "display_asleep": False, "battery": {"on_battery": True, "percent": 20}}
LOW_BATT_AC = {"locked": True, "display_asleep": False, "battery": {"on_battery": False, "percent": 20}}
RULES = {"unchanged": [], "changed": ["harness/app/launcher.m"], "unknown": None}

# (name, tier, only, ignore, probe state, GUI rule, want refused)
PREFLIGHT_CASES = (
    ("locked_full_no_gui_change", "full", None, False, LOCKED, "unchanged", False),
    ("asleep_full_no_gui_change", "full", None, False, ASLEEP, "unchanged", False),
    ("locked_slow_no_gui_change", "slow", None, False, LOCKED, "unchanged", False),
    ("locked_full_gui_changed", "full", None, False, LOCKED, "changed", True),
    ("asleep_full_gui_changed", "full", None, False, ASLEEP, "changed", True),
    ("locked_full_gui_rule_unknown", "full", None, False, LOCKED, "unknown", True),       # fail closed
    ("locked_tier_gui", "gui", None, False, LOCKED, "unchanged", True),
    ("asleep_tier_gui", "gui", None, False, ASLEEP, "unchanged", True),
    ("low_battery_no_gui_change", "full", None, False, LOW_BATT, "unchanged", True),
    ("low_battery_gui_changed", "full", None, False, LOW_BATT, "changed", True),
    ("low_battery_on_ac_no_gui_change", "full", None, False, LOW_BATT_AC, "unchanged", False),
    ("fine_full_gui_changed", "full", None, False, FINE, "changed", False),
    ("locked_ignored", "full", None, True, LOCKED, "changed", False),
    ("low_battery_ignored", "full", None, True, LOW_BATT, "unchanged", False),
    ("locked_filtered", "full", ["report.*"], False, LOCKED, "changed", False),
    ("locked_quick", "quick", None, False, LOCKED, "changed", False),
)


def preflight_cases() -> dict:
    """runner.gui_preflight_decision over stubbed probes and stubbed GUI rules (never the real screen)."""
    out = {}
    for name, tier, only, ignore, state, rule, want in PREFLIGHT_CASES:
        probed = []

        def probe(s=state):
            probed.append(1)
            return dict(s)
        d = runner.gui_preflight_decision(tier, only, ignore, probe,
                                          lambda r=rule: {"changed_gui_paths": RULES[r], "error": None})
        out[name] = {"refused": bool(d["refuse"]), "want_refused": want, "refuse": d["refuse"], "state": d["state"],
                     "gui_due": d["gui_due"], "probed": bool(probed)}
    return out


def _nested_preflight(tier: str, state: dict, rule: str, harness_run: bool = True) -> dict:
    """`harness/hs run --tier <tier>` with the stubbed probe and rule. $HS_TEST_PREFLIGHT_ONLY makes a run that isn't
    refused print its decision and exit 0 instead of starting (so a broken preflight never starts a real full run
    from a check); a refusal happens before that, exits 2, and must leave the results dir empty."""
    rd = _scratch("preflight")
    env = _env(HS_TEST_GUI_PROBE=json.dumps(state), HS_TEST_GUI_RULE=rule, HS_TEST_PREFLIGHT_ONLY="1")
    if not harness_run:
        env.pop("HS_HARNESS_RUN", None)
    t0 = time.time()
    try:
        r = subprocess.run([HS, "run", "--tier", tier, "--results-dir", str(rd)], capture_output=True, text=True,
                           timeout=120, env=env)
        dt = round(time.time() - t0, 2)
        out = {"tier": tier, "state": state, "rule": rule, "rc": r.returncode, "seconds": dt,
               "stdout": r.stdout[-2000:], "stderr": r.stderr[-1000:],
               "results_written": sorted(p.name for p in rd.iterdir())}
    finally:
        shutil.rmtree(rd, ignore_errors=True)
    if r.returncode == 0:
        try:
            out["decision"] = json.loads(r.stdout)["preflight"]
        except (ValueError, KeyError) as e:
            out["decision_error"] = str(e)
    return out


@check("report.gui_preflight", order=5, capability=CAP, stack="any", tier="quick")
def gui_preflight(ctx):
    """test-loop-speedups: the GUI preflight refuses a locked screen or a sleeping display only when GUI checks will
    run (GUI rule says changed or unknown, or --tier gui), refuses low battery on battery power always, and
    --ignore-gui-preflight overrides; over stubbed probes and rules, in-process and through nested `hs run`s (a
    refusal exits 2 within 5 s with one line and writes no results; a go-ahead records the screen state). The test
    switches are refused outside a harness run."""
    probs = []
    cases = preflight_cases()
    for n, c in cases.items():
        if c["refused"] != c["want_refused"]:
            probs.append(f"{n}: refused={c['refused']}, want {c['want_refused']} ({c['refuse']})")
    for n in ("locked_full_no_gui_change", "asleep_full_no_gui_change"):
        if not (cases[n]["probed"] and cases[n]["state"] and (cases[n]["state"].get("locked")
                                                              or cases[n]["state"].get("display_asleep"))):
            probs.append(f"{n}: the screen state isn't recorded in the decision: {cases[n]['state']}")
    nested = {
        "locked_gui_changed": _nested_preflight("full", LOCKED, "changed"),
        "asleep_tier_gui": _nested_preflight("gui", ASLEEP, "unchanged"),
        "low_battery_no_gui_change": _nested_preflight("full", LOW_BATT, "unchanged"),
        "locked_no_gui_change_decide": _nested_preflight("full", LOCKED, "unchanged"),
        "switch_outside_harness_run": _nested_preflight("full", LOCKED, "unchanged", harness_run=False),
    }
    for n in ("locked_gui_changed", "asleep_tier_gui", "low_battery_no_gui_change"):
        c = nested[n]
        lines = [l for l in c["stdout"].splitlines() if l.strip()]
        if c["rc"] != 2 or c["seconds"] > 5 or len(lines) != 1 or "refusing" not in lines[0] or c["results_written"]:
            probs.append(f"nested {n}: rc={c['rc']} in {c['seconds']} s, stdout lines {len(lines)}, results "
                         f"{c['results_written']}; want rc 2 within 5 s, one 'refusing' line, no results")
    d = nested["locked_no_gui_change_decide"]
    dec = d.get("decision") or {}
    if d["rc"] != 0 or dec.get("refuse") is not None or dec.get("gui_due") is not False \
            or (dec.get("state") or {}).get("locked") is not True:
        probs.append(f"nested locked_no_gui_change: rc={d['rc']} decision={dec}; want go-ahead with the locked state "
                     "recorded")
    o = nested["switch_outside_harness_run"]
    if o["rc"] != 2 or "test switch" not in o["stderr"] or o["results_written"]:
        probs.append(f"switches outside a harness run: rc={o['rc']} stderr={o['stderr'][-200:]!r}; want refused (2)")
    ev = {"cases": cases, "nested": nested, "this_run_preflight": ctx.get("gui_preflight"), "problems": probs,
          "code": "runner.gui_preflight_decision / preflight_from_env + guienv.preflight_problem + cli run"}
    require(not probs, f"{len(probs)} GUI preflight problem(s)", **ev)
    return ev


# ============================================================================ report.keep_awake

def _caffeinates_for(pid: int) -> list[dict]:
    """caffeinate processes waiting on `pid` (-w <pid>)."""
    out = []
    for p, cmd in procs.table(own_only=True):
        parts = cmd.split()
        if parts and parts[0].endswith("caffeinate") and "-w" in parts and parts[parts.index("-w") + 1:][:1] == [str(pid)]:
            out.append({"pid": p, "cmd": cmd})
    return out


def _display_assertions(pid: int) -> list[str]:
    from ..util import awake_assertions
    return [l for l in awake_assertions(pid) if "PreventUserIdleDisplaySleep" in l]


def _gone_within(pids: list[int], secs: float) -> dict:
    """Wait until no assertion of `pids` is listed and the processes are gone; returns the time it took."""
    t0 = time.time()
    while True:
        held = {p: _display_assertions(p) for p in pids}
        alive = [p for p in pids if runreg._alive_pid(p)]
        if not any(held.values()) and not alive:
            return {"released": True, "after_s": round(time.time() - t0, 2)}
        if time.time() - t0 > secs:
            return {"released": False, "after_s": round(time.time() - t0, 2), "held": held, "alive": alive}
        time.sleep(0.25)


def _nested_awake(how: str) -> dict:
    """A nested `hs run` (how = ok | error | sigkill): freeze it (SIGSTOP) once its caffeinate exists, read the
    assertions, let it go on, and time the release after it exits. `error`: its results.json path is a directory, so
    writing the results raises; `sigkill`: it's SIGKILLed while frozen."""
    rd = _scratch(f"awake-{how}")
    if how == "error":
        (rd / "results.json").mkdir()
    out: dict = {"how": how}
    proc = subprocess.Popen([HS, "run", "--only", "report.tiers_declared", "--results-dir", str(rd), "--leave-up"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=_env())
    try:
        out["run_pid"] = proc.pid
        out["given_no_assertion_of_run"] = _display_assertions(proc.pid) == [] and not _caffeinates_for(proc.pid)
        cafs, t0 = [], time.time()
        while not cafs and proc.poll() is None and time.time() - t0 < 60:
            cafs = _caffeinates_for(proc.pid)
            if not cafs:
                time.sleep(0.02)
        if cafs:
            os.kill(proc.pid, signal.SIGSTOP)
            try:
                t1 = time.time()
                held = []
                while time.time() - t1 < 5 and not held:      # pmset lists a new assertion within a moment
                    held = [l for c in cafs for l in _display_assertions(c["pid"])]
                    if not held:
                        time.sleep(0.2)
                out["during"] = {"caffeinate": cafs, "display_assertions": held}
            finally:
                os.kill(proc.pid, signal.SIGKILL if how == "sigkill" else signal.SIGCONT)
        else:
            out["during"] = {"caffeinate": [], "display_assertions": [], "why": "no caffeinate seen for the run"}
        text, _ = proc.communicate(timeout=300)
        out["rc"] = proc.returncode
        out["output_tail"] = (text or "")[-800:]
        out["after"] = _gone_within([c["pid"] for c in cafs] or [0], 5) if cafs else {"released": None}
        if how == "ok":
            try:
                doc = json.loads((rd / "results.json").read_text())
                out["meta_keep_awake"] = doc["meta"].get("keep_awake")
                out["check_status"] = next((c["status"] for c in doc["checks"] if c["id"] == "report.tiers_declared"),
                                           None)
            except (OSError, ValueError, KeyError) as e:
                out["meta_keep_awake"] = {"error": str(e)}
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for x in runreg.dead_runs():          # the killed run's file (it started nothing to heal)
            if x.get("pid") == proc.pid:
                Path(x["file"]).unlink(missing_ok=True)
        shutil.rmtree(rd, ignore_errors=True)
    return out


def _keep_awake_cmd() -> dict:
    """`hs keep-awake --hours 0.002` (~7 s): it holds the assertion, and it's gone when that time is up; a second one
    stopped with SIGTERM releases it at once."""
    out = {}
    for name, stop in (("timed", None), ("sigterm", 1.5)):
        p = subprocess.Popen([HS, "keep-awake", "--hours", "0.002"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=_env())
        try:
            held, t0 = [], time.time()
            while not held and time.time() - t0 < 5:
                held = _display_assertions(p.pid)
                if not held:
                    time.sleep(0.2)
            r = {"pid": p.pid, "held": held}
            if stop:
                time.sleep(stop)
                p.terminate()
            p.wait(timeout=30)
            r |= {"rc": p.returncode, "ran_s": round(time.time() - t0, 1), "after": _gone_within([p.pid], 5),
                  "stderr": p.stderr.read()[-300:]}
            out[name] = r
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
    return out


@check("report.keep_awake", order=6, capability=CAP, stack="any", tier="quick")
def keep_awake(ctx):
    """test-loop-speedups "Runs keep the display awake": during this check `pmset -g assertions` lists a
    PreventUserIdleDisplaySleep assertion of this run's caffeinate (-w <this run's pid>); a nested `hs run` holds its
    own while it runs and none remains within 5 s after it exits, also after a run that errors and a run that's
    SIGKILLed; `hs keep-awake --hours N` holds it and releases it when the time is up or it's stopped."""
    probs = []
    me = os.getpid()
    mine = _caffeinates_for(me)
    own = {"run_pid": me, "ctx_keep_awake": ctx.get("keep_awake"), "caffeinate": mine,
           "display_assertions": [l for c in mine for l in _display_assertions(c["pid"])]}
    if not own["display_assertions"]:
        probs.append(f"this run (pid {me}) holds no PreventUserIdleDisplaySleep assertion during the check: {mine}")
    nested = {h: _nested_awake(h) for h in ("ok", "error", "sigkill")}
    for h, n in nested.items():
        if not n.get("given_no_assertion_of_run"):
            probs.append(f"{h}: GIVEN not met: an assertion of the run existed before it started")
        if not (n.get("during") or {}).get("display_assertions"):
            probs.append(f"{h}: no PreventUserIdleDisplaySleep assertion of the run while it ran: {n.get('during')}")
        if not (n.get("after") or {}).get("released"):
            probs.append(f"{h}: the run's assertion wasn't released within 5 s of its exit: {n.get('after')}")
    if nested["ok"].get("check_status") != "pass" or not (nested["ok"].get("meta_keep_awake") or {}).get("pid"):
        probs.append(f"ok: its check {nested['ok'].get('check_status')} meta.keep_awake="
                     f"{nested['ok'].get('meta_keep_awake')}; want a completed run that recorded its caffeinate")
    if nested["error"].get("rc") in (0, None) or "IsADirectoryError" not in nested["error"].get("output_tail", ""):
        probs.append(f"error: the run didn't error as set up (rc={nested['error'].get('rc')})")
    if nested["sigkill"].get("rc") != -signal.SIGKILL:
        probs.append(f"sigkill: rc={nested['sigkill'].get('rc')}, want -9")
    cmd = _keep_awake_cmd()
    for n, r in cmd.items():
        if not r.get("held") or not (r.get("after") or {}).get("released"):
            probs.append(f"hs keep-awake ({n}): held={r.get('held')} after={r.get('after')}")
    if cmd["timed"].get("rc") != 0 or not (5 <= (cmd["timed"].get("ran_s") or 0) <= 15):
        probs.append(f"hs keep-awake (timed): rc={cmd['timed'].get('rc')} ran {cmd['timed'].get('ran_s')} s, want 0 "
                     "after ~7 s")
    ev = {"this_run": own, "nested_runs": nested, "keep_awake_cmd": cmd, "problems": probs,
          "code": "util.keep_awake (runner.run) + util.keep_awake_cli"}
    require(not probs, f"{len(probs)} keep-awake problem(s)", **ev)
    return ev


# ============================================================================ report.last_full_run

def _doc(tier: str, filt: list, sha: str, started: str) -> dict:
    meta = {"started": started, "finished": started, "repo_sha": sha, "fork_lock": {}, "tier": tier, "filter": filt,
            "full_run": runner.is_full_run(tier, filt or None), "force_clean": False, "wall_s": 1.0,
            "host": {"disk_free_gb": 1.0}, "counts": {"pass": 1, "fail": 0, "error": 0, "skip": 0}, "ok": True}
    return {"meta": meta, "checks": [{"id": "t.a", "capability": "x", "status": "pass", "duration_s": 1.0,
                                      "evidence": {"ok": 1}}]}


@check("report.last_full_run", order=7, capability=CAP, stack="any", tier="quick")
def last_full_run(ctx):
    """test-loop-speedups "The last full run's results are kept": through runner.write_outputs (what `hs run` calls),
    an unfiltered full run writes results.json + summary.md to the last-full-run dir; later filtered, quick, slow, gui
    and backups runs leave it holding that full run (full_run true, its sha and start); `hs timing-baseline join
    --label x` without a path reads harness/out/last-full-run/results.json (against a scratch baseline)."""
    probs = []
    d = _scratch("lastfull")
    ev: dict = {}
    try:
        res, summ, last = d / "results.json", d / "summary.md", d / "last-full-run"
        seq = [("full", [], "sha-full-1", "2026-10-07T01:00:00+00:00"),
               ("full", [], "sha-full-2", "2026-10-07T02:00:00+00:00"),
               ("full", ["report.*"], "sha-filtered", "2026-10-07T03:00:00+00:00"),
               ("quick", [], "sha-quick", "2026-10-07T04:00:00+00:00"),
               ("backups", [], "sha-backups", "2026-10-07T05:00:00+00:00"),
               ("gui", [], "sha-gui", "2026-10-07T06:00:00+00:00"),
               ("slow", [], "sha-slow", "2026-10-07T07:00:00+00:00")]
        steps = []
        for tier, filt, sha, started in seq:
            w = runner.write_outputs(_doc(tier, filt, sha, started), res, summ, last_dir=last)
            kept = json.loads((last / "results.json").read_text())["meta"] if (last / "results.json").exists() else {}
            steps.append({"run": f"{tier}{' filtered' if filt else ''} {sha}", "copied": w["last_full_run"] is not None,
                          "last_holds": {k: kept.get(k) for k in ("repo_sha", "started", "full_run", "tier")}})
        ev["steps"] = steps
        want = ["sha-full-1", "sha-full-2", "sha-full-2", "sha-full-2", "sha-full-2", "sha-full-2", "sha-full-2"]
        got = [s["last_holds"]["repo_sha"] for s in steps]
        if got != want:
            probs.append(f"last-full-run held {got}, want {want}")
        fin = steps[-1]["last_holds"]
        if fin != {"repo_sha": "sha-full-2", "started": "2026-10-07T02:00:00+00:00", "full_run": True, "tier": "full"}:
            probs.append(f"after the later runs last-full-run holds {fin}")
        if not (last / "summary.md").read_text().count("sha-full-2"):
            probs.append("last-full-run/summary.md isn't the full run's")
        # the default path, and `hs timing-baseline join` without a path, against a scratch copy of the baseline
        from .. import timingbase
        ev["default_results"] = str(timingbase.default_results())
        if timingbase.default_results() != P.OUT / "last-full-run" / "results.json" or \
                runner.LAST_FULL_RUN != P.OUT / "last-full-run":
            probs.append(f"default join path {timingbase.default_results()} isn't harness/out/last-full-run/results.json")
        base = d / "baseline.json"
        shutil.copy2(runner.TIMING_BASELINE, base)
        real_sha = _sha(runner.TIMING_BASELINE)
        r = subprocess.run([HS, "timing-baseline", "join", "--label", "x-last-full-run-check", "--baseline", str(base)],
                           capture_output=True, text=True, timeout=120, env=_env())
        ev["join"] = {"rc": r.returncode, "stdout": r.stdout[-1500:], "stderr": r.stderr[-800:]}
        default = str(timingbase.default_results())
        if r.returncode == 0:
            used = json.loads(r.stdout).get("results")
            if used != default:
                probs.append(f"join without a path read {used}, not {default}")
        elif default not in r.stderr:
            probs.append(f"join without a path failed without naming {default}: {r.stderr[-300:]}")
        # the kept copy is what join takes (in-process, on the scratch copy and the scratch baseline)
        try:
            ev["join_scratch_copy"] = timingbase.join(last / "results.json", "x-scratch-copy", base)
            if ev["join_scratch_copy"].get("added") != ["t.a"]:
                probs.append(f"join on the kept copy added {ev['join_scratch_copy']}, want ['t.a']")
        except (timingbase.BaselineError, OSError, ValueError, KeyError) as e:
            probs.append(f"join on the kept copy failed: {e}")
        if _sha(runner.TIMING_BASELINE) != real_sha:
            probs.append("the real timing baseline changed")
        # the live copy, if any: an unfiltered full-tier run
        live = runner.LAST_FULL_RUN / "results.json"
        if live.exists():
            m = json.loads(live.read_text())["meta"]
            ev["live_last_full_run"] = {k: m.get(k) for k in ("repo_sha", "started", "full_run", "tier", "filter")}
            if not runner.keeps_last_full_run(m):
                probs.append(f"the live last-full-run isn't an unfiltered full run: {ev['live_last_full_run']}")
        else:
            ev["live_last_full_run"] = None
    finally:
        shutil.rmtree(d, ignore_errors=True)
    ev |= {"problems": probs, "code": "runner.write_outputs/keeps_last_full_run + timingbase.cli"}
    require(not probs, f"{len(probs)} last-full-run problem(s)", **ev)
    return ev


def _sha(p: Path) -> str:
    import hashlib
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


# ============================================================================ report.nightly

def _stub_doc(flaky: str) -> dict:
    return {"meta": {"ok": flaky == "pass", "wall_s": 1.0, "repo_sha": "stub", "full_run": True, "tier": "full"},
            "checks": [{"id": "t.steady", "status": "pass"}, {"id": "t.flaky", "status": flaky},
                       {"id": "t.steady_fail", "status": "fail"}]}


def _scratch_git(state: str) -> Path:
    d = _scratch(f"nightly-git-{state}")

    def git(*a):
        subprocess.run(["git", "-C", str(d), "-c", "user.name=hs", "-c", "user.email=hs@localhost", *a],
                       capture_output=True, text=True, timeout=60, check=True)
    git("init", "-q", "-b", "main")
    (d / "f").write_text("1\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    if state == "branch":
        git("checkout", "-q", "-b", "change/x")
    elif state == "dirty":
        (d / "f").write_text("2\n")
    return d


@check("report.nightly", order=8, capability=CAP, stack="any", tier="quick")
def nightly_check(ctx):
    """test-loop-speedups "Nightly flake hunt", with stubbed full runs (one check passes, fails, passes): the flake
    table lists exactly that check with its three statuses and the summary names it, with run1-3.json kept; on a
    branch, dirty (scratch git repos), with another live harness run (a real run file of a live process), or on low
    battery it records the reason and starts no run. --install/--uninstall write and remove the LaunchAgent plist in
    a scratch directory with launchctl stubbed; the real ~/Library/LaunchAgents is never written."""
    from .. import nightly
    probs = []
    d = _scratch("nightly")
    ev: dict = {}
    real_plist = nightly.AGENTS_DIR / f"{nightly.LABEL}.plist"
    real_before = real_plist.exists()
    clean_main = _scratch_git("main")
    sleeper = None
    try:
        statuses = ["pass", "fail", "pass"]
        calls = []
        run_fn = lambda n: (calls.append(n), _stub_doc(statuses[n - 1]))[1]
        no_busy = lambda: {"stack": {}, "play": [], "prefix": [], "harness_runs": []}
        ac = lambda: {"on_battery": False, "percent": 100}
        rec = nightly.nightly(out_root=d, run_fn=run_fn, git_fn=lambda: nightly.git_state(clean_main),
                              busy_fn=no_busy, battery_fn=ac, day="2026-10-07")
        out = d / "2026-10-07"
        md = (out / "flakes.md").read_text()
        ev["three_runs"] = {"calls": list(calls), "flakes": rec["flakes"], "skipped": rec["skipped"],
                            "files": sorted(p.name for p in out.iterdir()), "flakes_md": md}
        if calls != [1, 2, 3] or rec["skipped"]:
            probs.append(f"stubbed nightly: runs {calls}, skipped {rec['skipped']}; want 3 runs")
        if rec["flakes"] != {"t.flaky": ["pass", "fail", "pass"]}:
            probs.append(f"flake table {rec['flakes']}, want exactly t.flaky pass/fail/pass")
        rows = [l for l in md.splitlines() if l.startswith("| `")]
        summ = [l for l in md.splitlines() if l.startswith("- Summary:")]
        if rows != ["| `t.flaky` | PASS | FAIL | PASS |"] or not summ or "`t.flaky`" not in summ[0] \
                or "t.steady" in md:
            probs.append(f"flakes.md rows {rows} summary {summ}")
        for n in (1, 2, 3):
            f = out / f"run{n}.json"
            if not f.exists() or json.loads(f.read_text())["checks"][1]["status"] != statuses[n - 1]:
                probs.append(f"run{n}.json missing or wrong")
        # skips: start no run, record the reason
        sleeper = subprocess.Popen(["/bin/sleep", "120"])
        time.sleep(0.2)
        live_dir = d / "runs"
        live_dir.mkdir()
        rid = runreg.run_id(sleeper.pid)
        (live_dir / f"{rid}.json").write_text(json.dumps({"id": rid, "pid": sleeper.pid}))
        live_runs = runreg.active_runs(live_dir)
        skips = {}
        for name, git_dir, busy, batt in (
                ("branch", _scratch_git("branch"), no_busy, ac),
                ("dirty", _scratch_git("dirty"), no_busy, ac),
                ("harness_run_active", clean_main, lambda: no_busy() | {"harness_runs": live_runs}, ac),
                ("stack_up", clean_main, lambda: no_busy() | {"stack": {"worldserver": [1]}}, ac),
                ("low_battery", clean_main, no_busy, lambda: {"on_battery": True, "percent": 20})):
            calls.clear()
            r = nightly.nightly(out_root=d / "skips", run_fn=run_fn, git_fn=lambda g=git_dir: nightly.git_state(g),
                                busy_fn=busy, battery_fn=batt, day=name)
            sd = d / "skips" / name
            skips[name] = {"skipped": r["skipped"], "runs_started": list(calls),
                           "files": sorted(p.name for p in sd.iterdir()),
                           "recorded": json.loads((sd / "nightly.json").read_text()).get("skipped")}
            if git_dir != clean_main:
                shutil.rmtree(git_dir, ignore_errors=True)
            if not r["skipped"] or calls or skips[name]["recorded"] != r["skipped"] or \
                    any(f.startswith("run") for f in skips[name]["files"]):
                probs.append(f"skip {name}: {skips[name]}; want a recorded reason and no run")
        ev["skips"] = skips
        ev["live_run_file_seen_active"] = live_runs
        if not live_runs:
            probs.append("a run file of a live process wasn't seen as an active harness run")
        # the live checks the CLI uses (evidence only; this run itself is excluded)
        ev["live_busy"] = nightly.busy()
        ev["live_git"] = nightly.git_state()
        # LaunchAgent, scratch dir + stubbed launchctl
        lc = []
        stub = lambda *a: (lc.append(list(a)), {"cmd": list(a), "rc": 0})[1]
        agents = d / "LaunchAgents"
        ins = nightly.install(agents_dir=agents, launchctl=stub)
        pl = plistlib.loads((agents / f"{nightly.LABEL}.plist").read_bytes())
        uni = nightly.uninstall(agents_dir=agents, launchctl=stub)
        ev["launch_agent"] = {"install": ins, "plist": pl, "uninstall": uni, "launchctl_calls": lc,
                              "left_after_uninstall": sorted(p.name for p in agents.iterdir())}
        if pl.get("Label") != nightly.LABEL or pl.get("ProgramArguments") != [HS, "nightly"] \
                or pl.get("WorkingDirectory") != str(P.ROOT) or not pl.get("StartCalendarInterval"):
            probs.append(f"plist content: {pl}")
        if not any(c[0] == "bootstrap" and c[-1] == str(agents / f"{nightly.LABEL}.plist") for c in lc) or \
                not any(c[0] == "bootout" for c in lc) or ev["launch_agent"]["left_after_uninstall"]:
            probs.append(f"install/uninstall: launchctl calls {lc}, left {ev['launch_agent']['left_after_uninstall']}")
        if nightly.AGENTS_DIR != Path.home() / "Library" / "LaunchAgents":
            probs.append(f"default LaunchAgents dir is {nightly.AGENTS_DIR}")
        h = subprocess.run([HS, "nightly", "--help"], capture_output=True, text=True, timeout=60, env=_env())
        if "--install" not in h.stdout or "--uninstall" not in h.stdout:
            probs.append("`hs nightly --help` doesn't offer --install/--uninstall")
    finally:
        if sleeper:
            sleeper.kill()
            sleeper.wait()
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(clean_main, ignore_errors=True)
    ev["real_launch_agent"] = {"path": str(real_plist), "before": real_before, "after": real_plist.exists()}
    if real_plist.exists() != real_before:
        probs.append("the real LaunchAgent plist changed during the check")
    ev |= {"problems": probs, "code": "nightly.nightly/flakes/flakes_md/skip_reason/install/uninstall"}
    require(not probs, f"{len(probs)} nightly problem(s)", **ev)
    return ev


# ============================================================================ report.orphans_healed

def _victim_ready(rid: str) -> dict:
    from .. import stack
    tg = runreg.tagged({rid})
    st = stack.stack_processes()
    return {"play": [o for o in tg if "hslib.play" in o["cmd"]],
            "stand_in": [o for o in tg if "hs-standin.bat" in o["cmd"].lower()],
            "worldserver": st["worldserver"], "tagged_n": len(tg)}


def _results(rd: Path) -> dict:
    """A nested run's results.json, or an empty document when it wrote none (the evaluation then fails)."""
    try:
        return json.loads((rd / "results.json").read_text())
    except (OSError, ValueError):
        return {"meta": {"no_results": str(rd)}, "checks": []}


def _force_clean(pids_play: list[int]) -> dict:
    """Last resort after a failed check: stop what's left (our own processes only)."""
    from .. import launch, stack
    out = {}
    for p in pids_play:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
    runreg._wait_gone(pids_play, 120)
    if launch.prefix_processes():
        out["stop_prefix"] = launch.stop_prefix()
    if any(stack.stack_processes().values()):
        out["down"] = stack.down()
    return out


@check("report.orphans_healed", order=345, capability=CAP, stack="down", tier="full")
def orphans_healed(ctx):
    """test-loop-speedups "A killed run's processes don't block the next run": a nested `hs run --only
    play.loop_clean_exit` (test account, stand-in client) is SIGKILLed while its ./play child and the stack run; the
    next nested `hs run` records those orphans, stops them (./play's SIGTERM path), its check passes, and nothing from
    this repo remains. Then a ./play not started by a harness run (no run id; test account, stand-in) survives a run
    that heals another killed run, and the play checks' GIVEN still refuses while it runs."""
    from .play_checks import StandIn, is_clear, remaining, require_given_clear
    from ..registry import CheckFail
    ev: dict = {}
    require_given_clear(ev)
    d = _scratch("orphans")
    victim = None
    play_pids: list[int] = []
    victim_play_cmds: list[str] = []
    s = None
    try:
        # ---- A: a harness run killed mid-run
        victim = subprocess.Popen([HS, "run", "--only", "play.loop_clean_exit", "--results-dir", str(d / "victim")],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  env=_env())
        rid, t0 = None, time.time()
        while rid is None and time.time() - t0 < 60 and victim.poll() is None:
            rid = next((r["id"] for r in runreg.records() if r.get("pid") == victim.pid), None)
            time.sleep(0.1)
        require(rid, "the victim run wrote no run file", **ev)
        ev["victim"] = {"pid": victim.pid, "run_id": rid}
        ready, t0 = {}, time.time()
        while time.time() - t0 < 600 and victim.poll() is None:
            ready = _victim_ready(rid)
            if ready["play"]:
                os.kill(victim.pid, signal.SIGSTOP)      # it must not stop its stand-in before the kill
                play_pids = [o["pid"] for o in ready["play"]]
                victim_play_cmds = [o["cmd"] for o in ready["play"]]
            if ready["play"] and ready["stand_in"] and ready["worldserver"]:
                break
            time.sleep(0.5)
        ev["victim"]["ready"] = ready
        require(ready.get("play") and ready.get("stand_in") and ready.get("worldserver"),
                "GIVEN not met: the victim run's ./play, stand-in client and stack never all ran", **ev)
        victim.send_signal(signal.SIGKILL)
        victim.wait()
        ev["victim"]["killed_rc"] = victim.returncode
        ev["victim"]["orphans_before"] = runreg.tagged({rid})
        ev["victim"]["run_file_left"] = any(r.get("id") == rid for r in runreg.records())
        require(ev["victim"]["run_file_left"], "the killed run's run file is gone", **ev)
        # the next run
        t1 = time.time()
        r = subprocess.run([HS, "run", "--only", "report.tiers_declared", "--results-dir", str(d / "healer")],
                           capture_output=True, text=True, timeout=900, env=_env())
        doc = _results(d / "healer")
        heal = doc["meta"].get("orphans") or {}
        ev["healer"] = {"rc": r.returncode, "seconds": round(time.time() - t1, 1), "orphans": heal,
                        "checks": {c["id"]: c["status"] for c in doc["checks"]}}
        ev["remaining_after_heal"] = remaining()
        ev["victim_tagged_after"] = runreg.tagged({rid})
        found_play = [o for o in heal.get("orphans", []) if o["pid"] in play_pids]
        require(rid in [x["id"] for x in heal.get("dead_runs", [])] and found_play,
                "the next run didn't record the killed run's ./play as an orphan", **ev)
        require(ev["healer"]["checks"].get("report.tiers_declared") == "pass",
                "the next run's checks didn't run normally", **ev)
        require(not heal.get("remaining") and not ev["victim_tagged_after"] and is_clear(ev["remaining_after_heal"]),
                "the killed run's processes weren't all stopped", **ev)
        require(not any(x.get("id") == rid for x in runreg.records()), "the killed run's run file wasn't removed", **ev)

        # ---- B: a ./play no harness run started, while another killed run is healed
        s = StandIn("outside")
        env_untagged = {k: v for k, v in os.environ.items() if k != runreg.ENV_ID}
        fh = open(s.out, "w")
        from .play_checks import PINNED, PLAY
        s.proc = subprocess.Popen([PLAY, "--account", "test", "--client-cmd", str(s.bat)], cwd=P.ROOT,
                                  stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                  start_new_session=True, env={**env_untagged, **PINNED})
        fh.close()
        up = s.wait_client_up()
        ev["outside_play"] = {"pid": s.proc.pid, "client_up": up}
        require(up["up"], "GIVEN not met: the outside ./play's stand-in never came up", **ev)
        # another killed run (it never gets as far as a check)
        v2 = subprocess.Popen([HS, "run", "--only", "report.tiers_declared", "--results-dir", str(d / "victim2"),
                               "--leave-up"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=_env())
        rid2, t0 = None, time.time()
        while rid2 is None and time.time() - t0 < 60 and v2.poll() is None:
            rid2 = next((x["id"] for x in runreg.records() if x.get("pid") == v2.pid), None)
            if rid2 is None:
                time.sleep(0.01)
        v2.kill()
        v2.wait()
        ev["victim2"] = {"pid": v2.pid, "run_id": rid2, "run_file_left": any(x.get("id") == rid2 for x in runreg.records())}
        require(rid2 and ev["victim2"]["run_file_left"], "GIVEN not met: the second killed run left no run file", **ev)
        before = remaining(s)
        r2 = subprocess.run([HS, "run", "--only", "report.tiers_declared", "--results-dir", str(d / "healer2"),
                             "--leave-up"], capture_output=True, text=True, timeout=600, env=_env())
        doc2 = _results(d / "healer2")
        h2 = doc2["meta"].get("orphans") or {}
        after = remaining(s)
        ev["healer2"] = {"rc": r2.returncode, "orphans": h2, "output_tail": (r2.stdout + r2.stderr)[-1500:]}
        ev["outside_before"], ev["outside_after"] = before, after
        require(s.proc.poll() is None and after["stand_in_processes"] and after["stack"].get("worldserver")
                and not any(o["pid"] == s.proc.pid for o in h2.get("orphans", [])),
                "the outside ./play, its client or its stack was touched", **ev)
        require(rid2 in [x["id"] for x in h2.get("dead_runs", [])], "the healing run didn't see the killed run", **ev)
        refused = None
        try:
            require_given_clear({})
        except CheckFail as e:
            refused = str(e)
        ev["given_refuses_while_outside_play_runs"] = refused
        require(refused and "already running" in refused, "the play checks' GIVEN didn't refuse", **ev)
        # stop the outside ./play the normal way
        s.stop_file.write_text("quit")
        rc, dt = s.wait_exit(timeout=300)
        ev["outside_play"] |= {"exit_code": rc, "exit_s": dt}
        ev["remaining_end"] = remaining()
        require(rc == 0 and is_clear(ev["remaining_end"]), "the outside ./play didn't end cleanly", **ev)
        ev["code"] = "runreg.start/heal/tagged (runner.run) + play's SIGTERM cleanup"
        return ev
    finally:
        for p in (victim,):
            if p is not None and p.poll() is None:
                try:
                    os.kill(p.pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                p.kill()
                p.wait()
        if s is not None:
            s.cleanup()
        if not is_clear(remaining()):
            ev["force_clean"] = _force_clean(play_pids)
        for x in runreg.dead_runs():                  # never leave a run file of ours behind
            if x.get("pid") in ((victim.pid if victim else None),):
                Path(x["file"]).unlink(missing_ok=True)
        # the victim's stand-in temp dir (its ./play's --client-cmd; its check never got to clean it up)
        for cmd in victim_play_cmds:
            parts = cmd.split()
            if "--client-cmd" in parts:
                sd = Path(parts[parts.index("--client-cmd") + 1]).parent
                if sd.name.startswith("hs-play-") and sd.parent == Path(tempfile.gettempdir()).resolve():
                    shutil.rmtree(sd, ignore_errors=True)
        shutil.rmtree(d, ignore_errors=True)
