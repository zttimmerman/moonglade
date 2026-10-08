"""power-guard checks (openspec/changes/power-guard, F34): the build job cap, the battery pause/resume/stop, the guarded
harness run, bootstrap resuming after a power stop, and the test switches' refusal.

Everything here is file-only: the core probe and the battery are stubbed (power.TEST_ENV), builds and bootstraps run
plan-only ($HS_TEST_POWER_PLAN_ONLY, a scratch dir: nothing is synced, configured, built or installed), and nested
harness runs execute three file-only checks with --leave-up. Every refusal case also sets a safety net, so a broken
refusal can only reach a plan or another refusal: plan-only for `hs build`/`./bootstrap`, and $HS_CLIENT_DIR (refused
right after the power switches, before anything starts) for ./play."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .. import paths as P, power
from ..registry import check, require

HS = str(P.HARNESS / "hs")
BOOTSTRAP = str(P.ROOT / "bootstrap")
PLAY = str(P.ROOT / "play")
CAP = "power-guard"
CORES_6_10 = json.dumps({"perf": 6, "total": 10})
# three file-only checks for the nested guarded run (orders 1, 225, 380): the pause must come before the second
RUN_CHECKS = ("report.tiers_declared", "client.patch0001_recorded", "app.icon")


def _scratch(tag: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"hs-power-{tag}-")).resolve()


def _env(harness_run: bool = True, **extra) -> dict:
    """This run's environment without any power or GUI-preflight test switch, plus `extra`."""
    from ..guienv import PREFLIGHT_TEST_ENV
    env = {k: v for k, v in os.environ.items() if k not in power.TEST_ENV and k not in PREFLIGHT_TEST_ENV}
    if harness_run:
        env["HS_HARNESS_RUN"] = "1"
    else:
        env.pop("HS_HARNESS_RUN", None)
    return env | {k: str(v) for k, v in extra.items()}


def _run(cmd: list[str], env: dict, timeout: float = 120) -> dict:
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    return {"cmd": cmd, "rc": r.returncode, "seconds": round(time.time() - t0, 2), "stdout": r.stdout,
            "stderr": r.stderr}


def _short(r: dict, n: int = 1500) -> dict:
    return r | {"stdout": r["stdout"][-n:], "stderr": r["stderr"][-n:]}


def _read(p: Path) -> str:
    try:
        return p.read_text()
    except OSError:
        return ""


def _plan_build(args: list[str], seq: str | None = None, cores: str | None = CORES_6_10) -> dict:
    """`hs build <args>` plan-only in a scratch dir: {"rc", "log" (build.log text), "json" (stdout), ...}."""
    d = _scratch("build")
    try:
        extra = {power.PLAN_ENV: d}
        if seq is not None:
            extra[power.BATTERY_ENV] = seq
        if cores is not None:
            extra[power.CORES_ENV] = cores
        r = _run([HS, "build", *args], _env(**extra))
        r["log"] = _read(d / "build.log")
        try:
            r["json"] = json.loads(r["stdout"])
        except ValueError:
            r["json"] = None
        return r
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _build_j(log: str) -> list[str]:
    """The -j value of every `cmake --build` line in a build log."""
    out = []
    for line in log.splitlines():
        if line.startswith("$ cmake --build"):
            parts = line.split()
            out.append(parts[parts.index("-j") + 1] if "-j" in parts else "(none)")
    return out


def _events(stdout: str) -> list[dict]:
    out = []
    for line in stdout.splitlines():
        if line.startswith("[bootstrap] "):
            try:
                out.append(json.loads(line[len("[bootstrap] "):]))
            except ValueError:
                pass
    return out


def _plan_bootstrap(d: Path, args: list[str], seq: str | None, cores: str | None = CORES_6_10) -> dict:
    """`./bootstrap --allow-test-inject <args>` plan-only with its state in `d` (kept between calls)."""
    extra = {power.PLAN_ENV: d}
    if seq is not None:
        extra[power.BATTERY_ENV] = seq
    if cores is not None:
        extra[power.CORES_ENV] = cores
    r = _run([BOOTSTRAP, "--allow-test-inject", *args], _env(**extra))
    r["events"] = _events(r["stdout"])
    r["state"] = _read(d / "bootstrap-state.json")
    r["build_log"] = _read(d / "build.log")
    return r


class _Job:
    """A guarded three-step job in-process: the guard before each step, the steps that ran, the lines it printed."""

    def __init__(self, seq: str, ignore: bool = False):
        self.lines: list[str] = []
        self.ran: list[str] = []
        self.guard = power.Guard("the test job", "the test job", ignore, say=self.lines.append,
                                 battery=power.Battery(seq))
        self.stop = None
        self.events: list[tuple] = []
        try:
            for step in ("step 1", "step 2", "step 3"):
                p = self.guard.check(step)
                if p:
                    self.events.append(("resumed", step))
                self.ran.append(step)
        except power.PowerStop as e:
            self.stop = {"message": str(e), "record": e.record}

    def ev(self) -> dict:
        return {"lines": self.lines, "ran": self.ran, "record": self.guard.record(), "stop": self.stop}


# ============================================================================ power.jobs_default

@check("power.jobs_default", order=34, capability=CAP, stack="any", tier="quick")
def jobs_default(ctx):
    """power-guard "Build job cap": with no --jobs the build's job count is the performance-core count (stubbed 6 of
    10: every `cmake --build` uses -j 6 and the log line names "6 performance cores"); with no performance-core value
    it is half the cores, at least 2 (10 -> 5, 2 -> 2), never more than all cores. In-process (power.default_jobs) and
    through `hs build` plan-only."""
    probs = []
    cases = {
        "perf6_total10": ({"perf": 6, "total": 10}, 6),
        "noperf_total10": ({"perf": None, "total": 10}, 5),
        "noperf_total2": ({"perf": None, "total": 2}, 2),
        "perf12_total10": ({"perf": 12, "total": 10}, 10),
    }
    inproc = {}
    for name, (probe, want) in cases.items():
        n, why = power.default_jobs(probe)
        inproc[name] = {"jobs": n, "why": why, "want": want}
        if n != want:
            probs.append(f"in-process {name}: {n} jobs, want {want} ({why})")
    if "6 performance cores" not in inproc["perf6_total10"]["why"]:
        probs.append(f"in-process perf6_total10: reason {inproc['perf6_total10']['why']!r} doesn't name "
                     "'6 performance cores'")
    nested = {}
    for name, cores, want in (("perf6_total10", CORES_6_10, "6"),
                              ("noperf_total10", json.dumps({"perf": None, "total": 10}), "5"),
                              ("noperf_total2", json.dumps({"perf": None, "total": 2}), "2")):
        r = _plan_build([], seq="100p", cores=cores)
        js = _build_j(r["log"])
        nested[name] = _short(r) | {"build_j": js, "log": r["log"][-800:], "first_line": r["log"].split("\n", 1)[0]}
        if r["rc"] != 0 or js != [want]:
            probs.append(f"hs build plan ({name}): rc {r['rc']}, cmake --build -j {js}, want [{want}]")
    first = nested["perf6_total10"]["first_line"]
    if "build jobs: 6" not in first or "6 performance cores" not in first:
        probs.append(f"hs build plan (perf6_total10): the log's job line is {first!r}; want it to name "
                     "'6 performance cores'")
    ev = {"in_process": inproc, "nested_hs_build": nested, "problems": probs}
    require(not probs, f"{len(probs)} job-default problem(s)", **ev)
    return ev


# ============================================================================ power.jobs_override

@check("power.jobs_override", order=34, capability=CAP, stack="any", tier="quick")
def jobs_override(ctx):
    """power-guard "Override is honoured on every entry point": `hs build --jobs 3` and `./bootstrap --jobs 3`
    (plan-only, stubbed 6 performance cores) both build with -j 3, and --jobs 0 is refused by both with a plain
    message before any plan or log is written."""
    probs = []
    hb = _plan_build(["--jobs", "3"], seq="100p")
    if hb["rc"] != 0 or _build_j(hb["log"]) != ["3"] or "--jobs 3" not in hb["log"]:
        probs.append(f"hs build --jobs 3: rc {hb['rc']}, -j {_build_j(hb['log'])}; want rc 0, [3], log naming --jobs 3")
    d, d0 = _scratch("bs"), _scratch("bs0")
    try:
        bs = _plan_bootstrap(d, ["--jobs", "3"], "100p")
        bs0 = _plan_bootstrap(d0, ["--jobs", "0"], "100p")
        bs0["written"] = sorted(p.name for p in d0.iterdir())
    finally:
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(d0, ignore_errors=True)
    if bs["rc"] != 0 or _build_j(bs["build_log"]) != ["3"]:
        probs.append(f"./bootstrap --jobs 3: rc {bs['rc']}, -j {_build_j(bs['build_log'])}; want rc 0, [3]")
    h0 = _plan_build(["--jobs", "0"], seq="100p")
    for name, r, out in (("hs build --jobs 0", h0, h0["stderr"]), ("./bootstrap --jobs 0", bs0, bs0["stdout"])):
        wrote = r.get("log") or r.get("written")
        if r["rc"] != 2 or "--jobs must be 1 or more" not in out or wrote:
            probs.append(f"{name}: rc {r['rc']}, output {out[-200:]!r}, wrote {bool(wrote)}; want rc 2, the plain "
                         "message, nothing written")
    ev = {"hs_build_jobs3": _short(hb) | {"log": hb["log"][-600:]},
          "bootstrap_jobs3": _short(bs) | {"build_log": bs["build_log"][-600:], "events": bs["events"][-3:]},
          "hs_build_jobs0": _short(h0), "bootstrap_jobs0": _short(bs0) | {"events": bs0["events"]}, "problems": probs}
    require(not probs, f"{len(probs)} --jobs override problem(s)", **ev)
    return ev


# ============================================================================ power.pause_resume

def pause_resume_cases() -> dict:
    """In-process guarded three-step jobs over stubbed batteries."""
    return {"low_plugged": _Job("40p,34p,38p,49p,50p").ev(), "desktop": _Job("none").ev(),
            "ignored": _Job("20p", ignore=True).ev()}


@check("power.pause_resume", order=35, capability=CAP, stack="any", tier="quick")
def pause_resume(ctx):
    """power-guard "Battery pause": a battery of 40 % (plugged in), 34, 38, 49, 50 pauses before the step that read 34,
    prints the pause line once, resumes when 50 is read, runs every step and records one pause (start, end,
    percents); no battery (desktop) never pauses; --ignore-power at 20 % plugged in runs without pausing and logs that
    the guard was skipped. In-process and through `hs build` plan-only (its build.log)."""
    probs = []
    c = pause_resume_cases()
    lo = c["low_plugged"]
    pauses = lo["record"]["pauses"]
    nlines = sum(1 for l in lo["lines"] if l.startswith("Paused: battery at 34 %."))
    if lo["ran"] != ["step 1", "step 2", "step 3"] or len(pauses) != 1:
        probs.append(f"low_plugged: ran {lo['ran']}, {len(pauses)} pause(s); want all 3 steps and one pause")
    else:
        p = pauses[0]
        if (p["step"] != "step 2" or p["percents"] != [34, 38, 49, 50] or p["end_pct"] != 50 or not p["start"]
                or not p["end"] or p["stopped"] or not p["plugged_in"]):
            probs.append(f"low_plugged: pause record {p}; want before step 2, percents 34,38,49,50, end 50, plugged")
    if nlines != 1 or power.pause_line(34) not in lo["lines"]:
        probs.append(f"low_plugged: the pause line printed {nlines} time(s); want once, exactly {power.pause_line(34)!r}")
    if c["desktop"]["record"]["pauses"] or c["desktop"]["ran"] != ["step 1", "step 2", "step 3"] or c["desktop"]["lines"]:
        probs.append(f"desktop: {c['desktop']}; want no pause and no line")
    ig = c["ignored"]
    if ig["record"]["pauses"] or ig["ran"] != ["step 1", "step 2", "step 3"] \
            or not any("skipped" in l and "--ignore-power" in l for l in ig["lines"]):
        probs.append(f"ignored: {ig}; want no pause and a 'skipped (--ignore-power)' line")
    nested = {
        "low_plugged": _plan_build([], seq="40p,34p,38p,49p,50p"),
        "desktop": _plan_build([], seq="none"),
        "ignored": _plan_build(["--ignore-power"], seq="20p"),
    }
    lg = nested["low_plugged"]["log"].splitlines()
    i_pause = [i for i, l in enumerate(lg) if l.startswith("Paused:")]
    i_cfg = [i for i, l in enumerate(lg) if l.startswith("$ cmake -S")]
    i_build = [i for i, l in enumerate(lg) if l.startswith("$ cmake --build")]
    jp = ((nested["low_plugged"]["json"] or {}).get("power") or {}).get("pauses") or []
    if (nested["low_plugged"]["rc"] != 0 or len(i_pause) != 1 or not i_cfg or not i_build
            or not (i_cfg[0] < i_pause[0] < i_build[0]) or len(jp) != 1 or jp[0].get("percents") != [34, 38, 49, 50]
            or not any(l.startswith("Battery at 50 %: continuing") for l in lg)):
        probs.append(f"hs build plan low_plugged: rc {nested['low_plugged']['rc']}, pause lines at {i_pause} "
                     f"(configure {i_cfg}, compile {i_build}), recorded {jp}; want one pause between configure and "
                     "compile, resumed at 50, recorded")
    if nested["desktop"]["rc"] != 0 or "Paused" in nested["desktop"]["log"] or len(_build_j(nested["desktop"]["log"])) != 1:
        probs.append("hs build plan desktop: paused or didn't complete")
    il = nested["ignored"]["log"]
    if nested["ignored"]["rc"] != 0 or "Paused" in il or "Battery guard skipped (--ignore-power)" not in il:
        probs.append("hs build plan --ignore-power: paused, or its log doesn't say the guard was skipped")
    ev = {"in_process": c, "nested_hs_build": {k: _short(v) | {"log": v["log"][-1200:], "json": None}
                                               for k, v in nested.items()},
          "nested_pauses": jp, "problems": probs}
    require(not probs, f"{len(probs)} pause/resume problem(s)", **ev)
    return ev


# ============================================================================ power.run_pause

def _nested_run(seq: str, tag: str) -> dict:
    rd = _scratch(tag)
    try:
        cmd = [HS, "run", "--tier", "full", "--results-dir", str(rd), "--leave-up"]
        for i in RUN_CHECKS:
            cmd += ["--only", i]
        t_start = time.time()
        r = _run(cmd, _env(**{power.BATTERY_ENV: seq}), timeout=900)
        try:
            doc = json.loads((rd / "results.json").read_text())
        except (OSError, ValueError) as e:
            doc = {"error": f"{type(e).__name__}: {e}"}
        r["doc"] = doc
        r["t_start"] = t_start
        return r
    finally:
        shutil.rmtree(rd, ignore_errors=True)


@check("power.run_pause", order=36, capability=CAP, stack="any", tier="quick")
def run_pause(ctx):
    """power-guard "Harness run pauses between checks": a nested full-tier run of three file-only checks with a
    stubbed battery dropping to 30 % after the first pauses before the second, resumes at 50 %, all three pass, and
    its results.json (written by this run) lists the pause and the paused seconds."""
    probs = []
    r = _nested_run("80p,30p,50p", "runpause")
    doc = r["doc"]
    m = doc.get("meta") or {}
    pw = m.get("power") or {}
    recs = {x["id"]: x for x in doc.get("checks") or []}
    status = {i: (recs.get(i) or {}).get("status") for i in RUN_CHECKS}
    pauses = pw.get("pauses") or []
    started = m.get("started") or ""
    if "error" in doc:
        probs.append(f"no results.json from the nested run: {doc['error']}")
    if any(s != "pass" for s in status.values()):
        probs.append(f"the three checks didn't all pass: {status}")
    if not pw.get("guarded") or len(pauses) != 1:
        probs.append(f"results.json meta.power: guarded {pw.get('guarded')}, {len(pauses)} pause(s); want one")
    else:
        p = pauses[0]
        if p.get("step") != f"check {RUN_CHECKS[1]}" or p.get("start_pct") != 30 or p.get("end_pct") != 50 \
                or p.get("stopped"):
            probs.append(f"pause record {p}; want before check {RUN_CHECKS[1]}, 30 -> 50, not stopped")
    if (m.get("timing") or {}).get("paused_s") is None:
        probs.append("meta.timing has no paused_s")
    lines = r["stdout"] + r["stderr"]
    if lines.count("Paused: battery at 30 %.") != 1:
        probs.append(f"the pause line appeared {lines.count('Paused: battery at 30 %.')} time(s); want once")
    ev = {"rc": r["rc"], "run_started": started, "check_started_epoch": r["t_start"], "statuses": status,
          "meta_power": pw, "timing_paused_s": (m.get("timing") or {}).get("paused_s"),
          "stderr_tail": r["stderr"][-1500:], "problems": probs}
    require(not probs, f"{len(probs)} guarded-run problem(s)", **ev)
    return ev


# ============================================================================ power.stop_low

@check("power.stop_low", order=37, capability=CAP, stack="any", tier="quick")
def stop_low(ctx):
    """power-guard "Falling battery stops the job": a battery of 34, 20, 15 % stops a guarded job with a plain message
    (what stopped, why, how to resume), no later step runs, and the record shows the stop at 15 %. In-process, through
    `hs build` plan-only (exit nonzero, its log stops before the next step) and through a nested guarded run (exit
    nonzero, only the checks before the stop ran, results.json records the stop)."""
    probs = []
    j = _Job("34p,20p,15p").ev()
    st = j["stop"] or {}
    if j["ran"] or not st or (st.get("record") or {}).get("end_pct") != 15 or j["record"]["stopped"] is None \
            or j["record"]["stopped"].get("percent") != 15:
        probs.append(f"in-process: ran {j['ran']}, stop {st}; want no step run and a stop at 15 %")
    msg = st.get("message", "")
    for part in ("Stopped:", "the test job", "15 %", "run the test job again"):
        if part not in msg:
            probs.append(f"in-process stop message lacks {part!r}: {msg!r}")
    b = _plan_build([], seq="60p,34p,20p,15p")
    js = (b["json"] or {}).get("power") or {}
    if b["rc"] == 0 or "$ cmake -S" not in b["log"] or "$ cmake --build" in b["log"] or "$ cmake --install" in b["log"] \
            or (js.get("stopped") or {}).get("percent") != 15 or "Stopped:" not in b["stderr"] \
            or "harness/hs build again" not in b["stderr"]:
        probs.append(f"hs build plan: rc {b['rc']}, stopped {js.get('stopped')}; want nonzero, configure only, stop "
                     "at 15 % with the message")
    r = _nested_run("80p,34p,20p,15p", "runstop")
    m = (r["doc"].get("meta") or {})
    ran = [x["id"] for x in r["doc"].get("checks") or []]
    stop = ((m.get("power") or {}).get("stop") or {})
    if r["rc"] == 0 or ran != [RUN_CHECKS[0]] or stop.get("before_check") != RUN_CHECKS[1] \
            or (stop.get("pause") or {}).get("end_pct") != 15 or m.get("ok") is not False:
        probs.append(f"nested run: rc {r['rc']}, ran {ran}, stop {stop}; want nonzero, only {RUN_CHECKS[0]}, stopped "
                     f"before {RUN_CHECKS[1]} at 15 %")
    ev = {"in_process": j, "hs_build": _short(b) | {"log": b["log"][-1000:], "json": js},
          "nested_run": {"rc": r["rc"], "ran": ran, "stop": stop, "ok": m.get("ok"), "started": m.get("started"),
                         "stderr_tail": r["stderr"][-800:]}, "problems": probs}
    require(not probs, f"{len(probs)} stop problem(s)", **ev)
    return ev


# ============================================================================ power.bootstrap_resume

@check("power.bootstrap_resume", order=38, capability=CAP, stack="any", tier="quick")
def bootstrap_resume(ctx):
    """power-guard "Stopped bootstrap resumes": a plan-only ./bootstrap whose stubbed battery falls 80, 34, 20, 15 %
    stops at its server-build step (exit nonzero, brew done, build not), and the next ./bootstrap with a healthy
    battery starts at the server-build step, not the beginning, and completes."""
    probs = []
    d = _scratch("resume")
    try:
        first = _plan_bootstrap(d, [], "80p,34p,20p,15p")
        state1 = first["state"]
        second = _plan_bootstrap(d, [], "90p")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    f_fail = [e for e in first["events"] if e.get("state") == "failed"]
    if first["rc"] == 0 or not f_fail or f_fail[0].get("step") != "build" or not f_fail[0].get("power_stop"):
        probs.append(f"first run: rc {first['rc']}, failed events {f_fail}; want a power stop at step build")
    try:
        done1 = json.loads(state1).get("done") if state1 else []
    except ValueError:
        done1 = None
    if done1 != ["brew"]:
        probs.append(f"after the stop the plan state is {done1}; want only brew done")
    for part in ("Setup stopped at step 3/10", "run ./bootstrap again"):
        if part not in first["stdout"]:
            probs.append(f"first run's message lacks {part!r}")
    starts = [e.get("step") for e in second["events"] if e.get("event") == "step" and e.get("state") == "start"
              and e.get("step") != "preflight"]
    already = [e.get("step") for e in second["events"] if e.get("state") == "already_done"]
    if second["rc"] != 0 or not starts or starts[0] != "build" or "brew" not in already:
        probs.append(f"second run: rc {second['rc']}, first step started {starts[:1]}, already done {already}; want "
                     "rc 0 starting at build with brew already done")
    if _build_j(second["build_log"]) != ["6"]:
        probs.append(f"second run's build plan: -j {_build_j(second['build_log'])}; want [6] (stubbed cores)")
    ev = {"first": _short(first) | {"events": first["events"][-3:]}, "state_after_stop": done1,
          "second": _short(second) | {"steps_started": starts, "already_done": already}, "problems": probs}
    require(not probs, f"{len(probs)} bootstrap-resume problem(s)", **ev)
    return ev


# ============================================================================ power.switch_refusal

@check("power.switch_refusal", order=39, capability=CAP, stack="any", tier="quick")
def switch_refusal(ctx):
    """power-guard "Switches refused outside the harness": with each power-guard test switch set (outside a harness
    run), `hs build`, `./bootstrap`, `./play` and another `hs` command refuse with a message naming the switch, before
    any work (exit 2 for hs/bootstrap, 1 for ./play; no plan, log or state written). Safety nets (plan-only for
    hs/bootstrap, $HS_CLIENT_DIR for ./play) keep a broken refusal from doing real work."""
    probs = []
    cases = {}
    for var, val in ((power.CORES_ENV, CORES_6_10), (power.BATTERY_ENV, "40p"), (power.PLAN_ENV, None)):
        d = _scratch("refuse")
        try:
            extra = {power.PLAN_ENV: d}                        # the safety net: a broken refusal only plans
            if val is not None:
                extra[var] = val
            env = _env(harness_run=False, **extra)
            res = {
                "hs build": _run([HS, "build"], env),
                "./bootstrap": _run([BOOTSTRAP], env),
                "hs status": _run([HS, "status"], env),
                "./play": _run([PLAY], env | {"HS_CLIENT_DIR": str(d / "no-such-client")}),
            }
            written = sorted(p.name for p in d.iterdir())
        finally:
            shutil.rmtree(d, ignore_errors=True)
        for name, r in res.items():
            out = r["stdout"] + r["stderr"]
            want_rc = 1 if name == "./play" else 2
            named = f"{var} is set" in out
            if r["rc"] != want_rc or not named or "[1/" in r["stdout"] or "$ cmake" in out:
                probs.append(f"{var} / {name}: rc {r['rc']} (want {want_rc}), names the switch {named}; "
                             f"output {out.strip()[-200:]!r}")
        if written:
            probs.append(f"{var}: something was written to the plan dir: {written}")
        cases[var] = {k: {"rc": v["rc"], "out": (v["stdout"] + v["stderr"]).strip()[-300:], "s": v["seconds"]}
                      for k, v in res.items()} | {"written": written}
    ev = {"cases": cases, "problems": probs}
    require(not probs, f"{len(probs)} refusal problem(s)", **ev)
    return ev
