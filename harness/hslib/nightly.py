"""`hs nightly`: the flake hunt (test-loop-speedups, harness-reporting "Nightly flake hunt").

Runs the unfiltered full run RUNS (3) times on a clean `main` in the main checkout and writes
harness/out/nightly/<date>/: run<n>.json (each run's results.json), nightly.json (what happened) and flakes.md (every
check whose status differed between the runs, and a summary line naming them). It starts no run, and records why in
nightly.json and flakes.md, when the checkout isn't a clean `main`, when the stack, ./play, the game or another harness
run is in use, or when the Mac is on battery below 35 %.

`--install` / `--uninstall` manage a user LaunchAgent (~/Library/LaunchAgents/<LABEL>.plist) that runs `hs nightly` at
03:00. Installing it is Zach's call; agents never install it (the checks use a scratch directory).
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import paths as P
from .util import log

RUNS = 3
OUT = P.OUT / "nightly"
LABEL = "local.moonglade.nightly"
AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
HOUR, MINUTE = 3, 0
RUN_TIMEOUT_S = 3 * 3600


def git_state(root: Path = P.ROOT) -> dict:
    """{"branch", "dirty": [porcelain lines], "error"} of a checkout."""
    def git(*a):
        return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True, timeout=60)
    try:
        b = git("rev-parse", "--abbrev-ref", "HEAD")
        s = git("status", "--porcelain")
        if b.returncode or s.returncode:
            return {"branch": None, "dirty": None, "error": (b.stderr + s.stderr).strip()[-300:]}
        return {"branch": b.stdout.strip(), "dirty": [l for l in s.stdout.splitlines() if l.strip()], "error": None}
    except (OSError, subprocess.SubprocessError) as e:
        return {"branch": None, "dirty": None, "error": f"{type(e).__name__}: {e}"}


def busy() -> dict:
    """What of this repo is in use: stack processes, ./play, Wine of the prefix, other live harness runs."""
    from . import launch, runreg, stack
    from .checks.play_checks import play_processes
    return {"stack": {k: v for k, v in stack.stack_processes().items() if v}, "play": play_processes(),
            "prefix": launch.prefix_processes(), "harness_runs": runreg.active_runs()}


def skip_reason(git: dict, in_use: dict, battery: dict) -> str | None:
    """Why the nightly mustn't start now, or None. Fails closed on a git error."""
    from .guienv import MIN_BATTERY_PCT
    if git.get("error") or git.get("branch") is None or git.get("dirty") is None:
        return f"can't read the checkout's git state ({git.get('error')})"
    if git["branch"] != "main":
        return f"the main checkout is on {git['branch']!r}, not main"
    if git["dirty"]:
        return f"the main checkout has uncommitted changes ({len(git['dirty'])} path(s))"
    used = [k for k, v in in_use.items() if v]
    if used:
        return "in use: " + ", ".join(used)
    if battery.get("on_battery") and battery.get("percent") is not None and battery["percent"] < MIN_BATTERY_PCT:
        return f"on battery at {battery['percent']} % (below {MIN_BATTERY_PCT} %)"
    return None


def flakes(docs: list[dict]) -> dict:
    """{check id: [status per run]} for every check whose status differs between the runs (a check missing from a
    run counts as "absent")."""
    ids = sorted({r["id"] for d in docs for r in d.get("checks", [])})
    per = {i: [next((r["status"] for r in d.get("checks", []) if r["id"] == i), "absent") for d in docs] for i in ids}
    return {i: s for i, s in per.items() if len(set(s)) > 1}


def flakes_md(day: str, docs: list[dict], fl: dict, skipped: str | None = None) -> str:
    if skipped:
        return f"# Nightly flake hunt {day}: skipped\n\n- Skipped: {skipped}\n- No run was started.\n"
    lines = [f"# Nightly flake hunt {day}", ""]
    lines.append("- Runs: " + ", ".join(
        f"run{n + 1} {'PASS' if d.get('meta', {}).get('ok') else 'FAIL'} ({d.get('meta', {}).get('wall_s', '?')} s, "
        f"`{d.get('meta', {}).get('repo_sha', '?')}`)" for n, d in enumerate(docs)))
    lines.append("- Summary: " + (f"{len(fl)} flaky check(s): " + ", ".join(f"`{i}`" for i in fl) if fl
                                  else "no check changed status between the runs"))
    lines += ["", "| Check | " + " | ".join(f"run{n + 1}" for n in range(len(docs))) + " |",
              "|---|" + "---|" * len(docs)]
    for i, s in fl.items():
        lines.append(f"| `{i}` | " + " | ".join(x.upper() for x in s) + " |")
    return "\n".join(lines) + "\n"


def _real_run(n: int) -> dict:
    """One unfiltered full run (`harness/hs run`), its results.json as a dict."""
    log(f"nightly: full run {n}")
    r = subprocess.run([str(P.HARNESS / "hs"), "run", "--tier", "full"], cwd=P.ROOT, timeout=RUN_TIMEOUT_S,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return json.loads(P.RESULTS.read_text()) | {"_exit": r.returncode}
    except (OSError, ValueError) as e:
        return {"meta": {"ok": False, "error": f"no results.json: {e}"}, "checks": [], "_exit": r.returncode}


def nightly(out_root: Path = OUT, runs: int = RUNS, run_fn=_real_run, git_fn=git_state, busy_fn=busy,
            battery_fn=None, day: str | None = None) -> dict:
    """The flake hunt. Every collaborator is injectable (report.nightly runs it with stubs)."""
    from . import guienv
    day = day or datetime.now().strftime("%Y-%m-%d")
    d = out_root / day
    d.mkdir(parents=True, exist_ok=True)
    git, in_use, batt = git_fn(), busy_fn(), (battery_fn or guienv.battery)()
    why = skip_reason(git, in_use, batt)
    rec = {"day": day, "started": datetime.now().isoformat(timespec="seconds"), "git": git, "in_use": in_use,
           "battery": batt, "skipped": why, "runs": [], "flakes": {}}
    docs = []
    if not why:
        for n in range(1, runs + 1):
            doc = run_fn(n)
            doc.pop("_exit", None)
            (d / f"run{n}.json").write_text(json.dumps(doc, indent=2, default=str))
            docs.append(doc)
            rec["runs"].append({"n": n, "file": f"run{n}.json", "ok": doc.get("meta", {}).get("ok"),
                                "repo_sha": doc.get("meta", {}).get("repo_sha")})
        rec["flakes"] = flakes(docs)
    rec["finished"] = datetime.now().isoformat(timespec="seconds")
    (d / "nightly.json").write_text(json.dumps(rec, indent=2, default=str))
    (d / "flakes.md").write_text(flakes_md(day, docs, rec["flakes"], why))
    rec["dir"] = str(d)
    return rec


# ---------------------------------------------------------------- LaunchAgent

def plist(root: Path = P.ROOT) -> dict:
    return {"Label": LABEL, "ProgramArguments": [str(root / "harness" / "hs"), "nightly"],
            "WorkingDirectory": str(root), "StartCalendarInterval": {"Hour": HOUR, "Minute": MINUTE},
            "StandardOutPath": str(P.LOGS / "nightly.log"), "StandardErrorPath": str(P.LOGS / "nightly.log"),
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
            "LowPriorityIO": False, "ProcessType": "Standard"}


def _launchctl(*a) -> dict:
    r = subprocess.run(["/bin/launchctl", *a], capture_output=True, text=True, timeout=60)
    return {"cmd": ["launchctl", *a], "rc": r.returncode, "err": r.stderr.strip()[-300:]}


def install(agents_dir: Path = AGENTS_DIR, launchctl=_launchctl) -> dict:
    agents_dir.mkdir(parents=True, exist_ok=True)
    f = agents_dir / f"{LABEL}.plist"
    f.write_bytes(plistlib.dumps(plist()))
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", f"{domain}/{LABEL}")            # replace an older copy; fails harmlessly if none
    return {"plist": str(f), "load": launchctl("bootstrap", domain, str(f))}


def uninstall(agents_dir: Path = AGENTS_DIR, launchctl=_launchctl) -> dict:
    f = agents_dir / f"{LABEL}.plist"
    out = {"plist": str(f), "unload": launchctl("bootout", f"gui/{os.getuid()}/{LABEL}"), "removed": f.exists()}
    if f.exists():
        f.unlink()
    return out


def cli(do_install: bool = False, do_uninstall: bool = False, runs: int = RUNS) -> int:
    if do_install:
        print(json.dumps(install(), indent=1))
        return 0
    if do_uninstall:
        print(json.dumps(uninstall(), indent=1))
        return 0
    if runs < 1:
        log("nightly: --runs must be at least 1")
        return 2
    if shutil.which("git") is None:
        log("nightly: git not found")
        return 1
    rec = nightly(runs=runs)
    print((Path(rec["dir"]) / "flakes.md").read_text())
    return 0 if not rec["skipped"] else 3
