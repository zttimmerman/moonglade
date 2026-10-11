"""`./bootstrap <client.zip|folder>` (`harness/hs bootstrap`): from a fresh clone to a ready dist/Moonglade.app
(openspec/changes/easy-setup, easy-setup "One-command bootstrap").

An ordered list of steps. Each has a plain-language label, a cheap honest `done()` (the output exists and is current;
never a self-written "done" marker alone), a `run()` that reuses the existing `harness/hs` subcommands, and a hint
for when it fails. A run does every step whose done() is false, in order, and stops at the first failure with the
step, the reason and what to do. So re-running skips what's done, and an interrupted run continues where it stopped.

    preflight    this Mac (Apple Silicon, macOS version, Rosetta, Command Line Tools, Homebrew), the client you gave
                 (a zip or folder holding a 3.3.5a build 12340 enUS client), nothing of Moonglade running, free disk
    brew         Homebrew packages from the Brewfile (`brew bundle check --no-upgrade`; installs only with your OK)
    build        pinned sources + the server build (`hs build --clean`)
    data         the pinned server data (downloads.install_client_data; `hs download client-data`)
    database     the repo's own MySQL and the game databases: first start (`hs up`, then `hs down`)
    wine         the pinned Wine runtime (`hs wine-install`)
    client       your client, installed read-only into client/ (`hs client-install`) and checked (client.inspect)
    playclient   the play client env/client-play/ (`hs client-build`)
    app          dist/Moonglade.app (`hs app`)
    defaults     Moonglade's default settings (settings.DEFAULTS) into env/settings.json where unset

Output: plain progress lines, plus one machine-readable line per step event, `[bootstrap] {json}` (the harness
reads those; the person can ignore them).

Test switch (setup.preflight only): $HS_BOOTSTRAP_TEST, a JSON object that replaces what preflight observes (arch,
macos, rosetta, clt, brew, brew_missing, free_bytes, answer). It's refused unless `--allow-test-inject` is given
AND $HS_HARNESS_RUN is set (a harness run), so it can't reach a normal ./bootstrap.

Power guard (openspec/changes/power-guard, F34): `--jobs N` sets the server build's job count (default: this Mac's
performance cores, power.default_jobs), and before each step that runs, the battery guard (power.Guard) pauses below
35 % (plugged in or not) until 50 %, and stops the setup at 15 % with what to do; the stopped step isn't done, so the
next ./bootstrap starts there. `--ignore-power` skips the guard. Its test switches (power.TEST_ENV) need the same
flag + harness run; with $HS_TEST_POWER_PLAN_ONLY (a scratch dir) the run is a plan: no preflight, each step's
done/run is a record in <dir>/bootstrap-state.json, except that the build step really runs `hs build --clean` (itself
plan-only, so nothing is built) to show the job count it passes on.
"""
from __future__ import annotations

import fcntl
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import paths as P

HS = P.HARNESS / "hs"
BREWFILE = P.ROOT / "Brewfile"
BREW_BIN = P.BREW / "bin" / "brew" if P.BREW else None    # dev only
DB_MARKER = P.DB_MARKER
DB_NAMES = ("acore_auth", "acore_world", "acore_characters", "acore_playerbots")
TEST_ENV = "HS_BOOTSTRAP_TEST"
TEST_FLAG = "--allow-test-inject"
# The minimum macOS: the pinned Wine runtime's binaries (wine, wineserver, ntdll.so, winemac.so) are built with
# LC_BUILD_VERSION minos 26.2 (`otool -l`, 2026-10-07; design.md "As built"), and dyld refuses a binary built for a
# newer macOS than the running one. The launcher and the Dock shim need only 13.0 (app.py, dockshim.py).
MIN_MACOS = (26, 2)
# Free disk each step needs while it runs (GB), measured on the dev Mac (docs/setup.md, design.md "Measurements"):
# preflight asks for the sum over the steps still to do.
NEED_GB = {"build": 6.0, "data": 5.0, "database": 2.0, "wine": 1.0, "client": 19.0, "playclient": 0.5, "app": 0.1}
HOMEBREW_INSTALL = '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'


class StepFailed(RuntimeError):
    def __init__(self, msg: str, hint: str = "", evidence: dict | None = None):
        super().__init__(msg)
        self.hint = hint
        self.evidence = evidence or {}


class PowerStopped(StepFailed):
    """The power guard stopped this step (power-guard): exit power.STOP_EXIT; the step stays not done."""


POWER_HINT = ("Plug in (a stronger charger if you have one), let the battery charge above 50 %, then run ./bootstrap "
              "again; it continues from this step.")


def emit(**kw) -> None:
    print("[bootstrap] " + json.dumps(kw, default=str), flush=True)


def say(msg: str) -> None:
    print(msg, flush=True)


def _hs(*args: str, timeout: float | None = None, out_dir: Path | None = None) -> subprocess.CompletedProcess:
    """Run one existing `harness/hs` subcommand: its progress (stderr) shows live, its JSON result (stdout) is kept
    (in env/logs/, or `out_dir` for a plan-only run)."""
    d = out_dir or P.LOGS
    d.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([str(HS), *args], stdout=subprocess.PIPE, stderr=None, text=True, timeout=timeout)
    (d / f"bootstrap-{args[0]}.out").write_text(r.stdout or "")
    return r


def _fmt_s(s: float) -> str:
    return f"{int(s // 60)} min {int(s % 60)} s" if s >= 60 else f"{s:.0f} s"


# ---------------------------------------------------------------- observations (preflight), injectable in tests

class Probe:
    """What preflight observes about this Mac. With a test injection (TEST_ENV + flag + harness run), the injected
    values replace the observed ones."""

    def __init__(self, inject: dict | None):
        self.inject = inject or {}

    def _get(self, key, real):
        return self.inject[key] if key in self.inject else real()

    def arch(self) -> str:
        def real():
            r = subprocess.run(["/usr/sbin/sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True)
            return "arm64" if r.stdout.strip() == "1" else platform.machine()
        return self._get("arch", real)

    def macos(self) -> str:
        return self._get("macos", lambda: subprocess.run(["/usr/bin/sw_vers", "-productVersion"], capture_output=True,
                                                         text=True).stdout.strip())

    def rosetta(self) -> bool:
        return self._get("rosetta", lambda: subprocess.run(["/usr/bin/arch", "-x86_64", "/usr/bin/true"],
                                                           capture_output=True).returncode == 0)

    def clt(self) -> bool:
        def real():
            return (subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True).returncode == 0
                    and subprocess.run(["/usr/bin/xcrun", "--find", "clang"], capture_output=True).returncode == 0)
        return self._get("clt", real)

    def brew(self) -> bool:
        return self._get("brew", lambda: os.access(BREW_BIN, os.X_OK))

    def brew_missing(self) -> list[str]:
        """Brewfile entries that aren't installed (`brew bundle check --no-upgrade --verbose`); fail-closed."""
        def real():
            env = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ENV_HINTS": "1"}
            r = subprocess.run([str(BREW_BIN), "bundle", "check", "--no-upgrade", "--verbose", f"--file={BREWFILE}"],
                               capture_output=True, text=True, env=env)
            if r.returncode == 0:
                return []
            missing = [l.split("Formula", 1)[1].split("needs", 1)[0].strip() for l in r.stdout.splitlines()
                       if "Formula" in l and "needs" in l]
            return missing or [f"(brew bundle check failed: {(r.stdout + r.stderr).strip()[-200:]})"]
        return self._get("brew_missing", real)

    def free_bytes(self) -> int:
        return self._get("free_bytes", lambda: shutil.disk_usage(P.HOME).free)

    def answer(self, prompt: str) -> str:
        if "answer" in self.inject:
            say(prompt + str(self.inject["answer"]))
            return str(self.inject["answer"])
        try:
            return input(prompt)
        except EOFError:
            return ""


# ---------------------------------------------------------------- the client you gave

def check_client_source(src: str | None) -> dict:
    """The client argument: a zip or a folder holding a Windows 3.3.5a build 12340 enUS client. Reads Wow.exe out of
    it (never writes). Raises StepFailed naming the problem."""
    from .clientinst import ClientInstallError, EXPECTED_BUILD, _open_source
    from .pe import PEError, version_info
    hint_get = ("Give ./bootstrap your own WoW 3.3.5a (build 12340) enUS Windows client: the .zip you downloaded, or "
                "the folder that holds Wow.exe, e.g. ./bootstrap ~/Downloads/YourClient_3.3.5a.zip")
    if not src:
        raise StepFailed("no client given", hint_get)
    p = Path(src).expanduser()
    if not p.exists():
        raise StepFailed(f"{p} doesn't exist", hint_get)
    if p.is_file() and not __import__("zipfile").is_zipfile(p):
        raise StepFailed(f"{p} is neither a zip file nor a folder", hint_get)
    if not (p.is_file() or p.is_dir()):
        raise StepFailed(f"{p} is neither a zip file nor a folder", hint_get)
    try:
        so = _open_source(p.resolve())
    except ClientInstallError as e:
        raise StepFailed(str(e), hint_get) from None
    try:
        names = {r.lower(): r for r in so.members}
        if "wow.exe" not in names:
            raise StepFailed(f"no Wow.exe at the top of {p}; that isn't a WoW client", hint_get)
        with so.open(names["wow.exe"]) as fh:
            data = fh.read()
        try:
            vi = version_info(data)
        except PEError as e:
            raise StepFailed(f"{p}: Wow.exe has no readable version ({e}); not a 3.3.5a client", hint_get) from None
        locales = sorted({r.split("/")[1] for r in so.members if r.lower().startswith("data/") and r.count("/") >= 2
                          and len(r.split("/")[1]) == 4 and r.split("/")[1][2:].isupper()})
    finally:
        so.close()
    info = {"source": str(p.resolve()), "kind": so.kind, "wow_exe_version": vi["file_version"], "locales": locales}
    if vi["file_version"] != EXPECTED_BUILD:
        raise StepFailed(f"{p}: Wow.exe is version {vi['file_version']}, not {EXPECTED_BUILD} (WoW 3.3.5a); Moonglade "
                         "needs exactly that client", hint_get, info)
    if "enUS" not in locales:
        raise StepFailed(f"{p}: the client's language is {', '.join(locales) or 'unknown'}, not enUS (English, US); "
                         "Moonglade needs an enUS client", hint_get, info)
    return info


# ---------------------------------------------------------------- steps

class Ctx:
    def __init__(self, client: str | None, yes: bool, probe: Probe, jobs: int | None = None,
                 ignore_power: bool = False, plan_dir: Path | None = None):
        self.client, self.yes, self.probe = client, yes, probe
        self.jobs, self.ignore_power, self.plan_dir = jobs, ignore_power, plan_dir
        self.client_info: dict | None = None


class Step:
    id = ""
    label = ""
    hint = ""

    def done(self, ctx: Ctx) -> tuple[bool, str]:
        return False, ""

    def run(self, ctx: Ctx) -> dict:
        raise NotImplementedError


class Brew(Step):
    id, label = "brew", "Homebrew packages (Brewfile)"
    hint = f"Install them yourself with: brew bundle install --no-upgrade --file={BREWFILE} ; then run ./bootstrap again."

    def done(self, ctx):
        miss = ctx.probe.brew_missing()
        ctx.brew_missing = miss
        return (not miss, "all installed" if not miss else f"missing: {', '.join(miss)}")

    def run(self, ctx):
        miss = ctx.brew_missing
        say(f"   Homebrew needs to install: {', '.join(miss)}")
        say(f"   (command: brew bundle install --no-upgrade --file={BREWFILE}; nothing you have is upgraded or removed)")
        if not ctx.yes:
            a = ctx.probe.answer("   Install them now? [y/N] ").strip().lower()
            if a not in ("y", "yes"):
                raise StepFailed("you declined the Homebrew install, so nothing was installed", self.hint,
                                 {"missing": miss})
        env = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1"}
        r = subprocess.run([str(BREW_BIN), "bundle", "install", "--no-upgrade", f"--file={BREWFILE}"], env=env)
        if r.returncode != 0:
            raise StepFailed(f"brew bundle install failed (exit {r.returncode})", self.hint)
        left = Probe(None).brew_missing()
        if left:
            raise StepFailed(f"still missing after the install: {', '.join(left)}", self.hint)
        return {"installed": miss}


class Build(Step):
    id, label = "build", "Building the game server (pinned sources; the first time takes 15-30 min)"
    hint = ("See harness/out/build.log for the compiler output. Network problems while cloning: run ./bootstrap again. "
            "To force a clean rebuild: harness/hs build --force-clean")

    def done(self, ctx):
        from . import build
        bins = all(os.access(P.SERVER_BIN / n, os.X_OK) for n in ("authserver", "worldserver"))
        if not bins:
            return False, "no server binaries yet"
        if build.moved_problem():
            return False, "built in another folder"
        rec = build.read_install_record()
        if not rec or rec.get("repo") != str(P.ROOT.resolve()):
            return False, "no install record for this folder"
        h = build.input_hash()
        if not (build.read_stamp(build.build_stamp_path()) == build.read_stamp(build.install_stamp_path()) == h):
            return False, "build inputs changed since the last clean build"
        lock = P.fork_lock()
        for repo, key in ((P.CORE_SRC, "core_sha"), (P.BOTS_SRC, "playerbots_sha"), (P.AB_SRC, "autobalance_sha")):
            r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
            if r.stdout.strip() != lock[key]:
                return False, f"{repo.name} isn't at its pinned commit"
        return True, f"built from the current inputs ({h[:12]})"

    @staticmethod
    def args(ctx) -> list[str]:
        return (["build", "--clean"] + (["--jobs", str(ctx.jobs)] if ctx.jobs is not None else [])
                + (["--ignore-power"] if ctx.ignore_power else []))

    def run_build(self, ctx) -> None:
        from . import power
        r = _hs(*self.args(ctx), out_dir=ctx.plan_dir)
        if r.returncode == power.STOP_EXIT:
            raise PowerStopped("the server build stopped because the battery got too low", POWER_HINT,
                               {"cmd": "harness/hs " + " ".join(self.args(ctx))})
        if r.returncode != 0:
            raise StepFailed(f"`harness/hs {' '.join(self.args(ctx))}` failed (exit {r.returncode})", self.hint)

    def run(self, ctx):
        self.run_build(ctx)
        ok, why = self.done(ctx)
        if not ok:
            raise StepFailed(f"the build finished but isn't complete: {why}", self.hint)
        return {"cmd": "harness/hs " + " ".join(self.args(ctx))}


class Data(Step):
    id, label = "data", "Server data (maps and game tables, about 1.2 GB download)"
    hint = "Check the network connection and run ./bootstrap again; a damaged download is removed and fetched again."

    def done(self, ctx):
        from .downloads import client_data_state
        s = client_data_state()
        return s["done"], (f"unpacked from the pinned zip ({s['files']} files)" if s["done"] else s["why"])

    def run(self, ctx):
        from .downloads import DownloadError, install_client_data
        try:
            return install_client_data()
        except DownloadError as e:
            raise StepFailed(str(e), self.hint, e.evidence) from None


def _db_dirs_ok() -> tuple[bool, str]:
    for n in DB_NAMES:
        d = P.MYSQL_DATA / n
        if not d.is_dir() or not any(d.glob("*.ibd")):
            return False, f"database {n} doesn't exist yet"
    return True, "the four game databases exist"


class Database(Step):
    id, label = "database", "Creating the game databases (first server start; can take 10-30 min)"
    hint = ("See env/logs/worldserver.out and env/logs/mysqld.err. Run ./bootstrap again to retry. If a first start "
            "was interrupted and keeps failing, ask for help before deleting anything: data/mysql holds your saves.")

    def done(self, ctx):
        ok, why = _db_dirs_ok()
        if not ok:
            return False, why
        try:
            m = json.loads(DB_MARKER.read_text())
        except (OSError, ValueError):
            return False, "no completed first start recorded"
        if m.get("repo") != str(P.ROOT.resolve()):
            return False, "first start recorded for another folder"
        return True, f"first start completed {m.get('at')}"

    def run(self, ctx):
        from . import stack
        if any(stack.stack_processes().values()):
            raise StepFailed("the server is already running", "Quit Moonglade (or run harness/hs down), then run "
                                                             "./bootstrap again.")
        t0 = time.time()
        r = _hs("up")
        up_ok = r.returncode == 0
        d = _hs("down")
        if not up_ok:
            raise StepFailed(f"the first server start failed (exit {r.returncode})", self.hint)
        if d.returncode != 0:
            raise StepFailed(f"the server didn't stop cleanly after its first start (exit {d.returncode})", self.hint)
        ok, why = _db_dirs_ok()
        if not ok:
            raise StepFailed(f"the server started but {why}", self.hint)
        DB_MARKER.write_text(json.dumps({"repo": str(P.ROOT.resolve()), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                         "first_start_s": round(time.time() - t0, 1)}, indent=2) + "\n")
        return {"first_start_s": round(time.time() - t0, 1)}


class Wine(Step):
    id, label = "wine", "Wine runtime (runs the Windows game on this Mac)"
    hint = "Check the network connection and run ./bootstrap again."

    def done(self, ctx):
        from . import wineinst
        want = P.fork_lock()["wowsilicon_runtime_sha256"]
        ok = (wineinst.RUNTIME_DIR / ".wine-runtime/bin/wine").exists() and wineinst.MARKER.exists() \
            and wineinst.MARKER.read_text().strip() == want
        return ok, ("the pinned runtime is installed" if ok else "not installed")

    def run(self, ctx):
        r = _hs("wine-install")
        if r.returncode != 0:
            raise StepFailed(f"`harness/hs wine-install` failed (exit {r.returncode})", self.hint)
        return {"cmd": "harness/hs wine-install"}


def _client_inspect() -> dict:
    """The registered client.inspect check, run in-process on this repo's client/ (no harness run, no results file)."""
    from .registry import CheckFail, load_all
    try:
        ev = load_all()["client.inspect"].fn(None)
        return {"ok": True, "evidence": ev}
    except CheckFail as e:
        return {"ok": False, "message": str(e), "evidence": e.evidence}


class Client(Step):
    id, label = "client", "Installing your game client (a copy; the original is only read)"
    hint = ("The client you gave must be a complete WoW 3.3.5a (12340) enUS client. If the zip is damaged, get a good "
            "copy and run ./bootstrap again; files that are already in place are kept.")

    def done(self, ctx):
        from .clientinst import load_manifest, mismatches
        man = load_manifest()
        if not man or not P.CLIENT.is_dir():
            return False, "not installed"
        bad = mismatches(P.CLIENT, man)
        if bad:
            return False, f"{len(bad)} file(s) differ from the install record"
        from .pristine import status
        lk = status(P.CLIENT)
        if not lk["locked"]:          # e.g. an install killed mid-way; client-install restores the protection
            return False, (f"installed, but not write-protected ({lk['writable_count']} path(s) writable, e.g. "
                           f"{lk['writable'][:2]})")
        return True, f"installed, intact and write-protected (Wow.exe {(man.get('wow_exe_version') or {}).get('file_version')})"

    def run(self, ctx):
        r = _hs("client-install", str(Path(ctx.client).expanduser().resolve()))
        if r.returncode != 0:
            raise StepFailed(f"`harness/hs client-install` failed (exit {r.returncode})", self.hint)
        ins = _client_inspect()
        if not ins["ok"]:
            raise StepFailed(f"the installed client didn't pass client.inspect: {ins['message']}", self.hint)
        return {"client_inspect": "pass"}


class PlayClient(Step):
    id, label = "playclient", "Building the play client (the game with the Moonglade front menu)"
    hint = "Run harness/hs client-build to see the details; harness/hs client-mode stock plays the untouched client."

    def done(self, ctx):
        from .clientbuild import PLAY, up_to_date
        u = up_to_date(PLAY)
        return u["current"], u["why"]

    def run(self, ctx):
        r = _hs("client-build")
        if r.returncode != 0:
            raise StepFailed(f"`harness/hs client-build` failed (exit {r.returncode})", self.hint)
        return {"cmd": "harness/hs client-build"}


class App(Step):
    id, label = "app", "Building Moonglade.app"
    hint = "Run harness/hs app to see the compiler output (it needs the Xcode Command Line Tools)."

    def done(self, ctx):
        from .app import up_to_date
        u = up_to_date()
        return u["current"], u["why"]

    def run(self, ctx):
        r = _hs("app")
        if r.returncode != 0:
            raise StepFailed(f"`harness/hs app` failed (exit {r.returncode})", self.hint)
        return {"cmd": "harness/hs app"}


class Defaults(Step):
    id, label = "defaults", "Moonglade's default settings"
    hint = "Check env/settings.json is valid JSON (or delete it to start from the defaults)."

    def done(self, ctx):
        from . import settings
        try:
            data = settings.load()
        except settings.SettingsError as e:
            return False, str(e)
        missing = [k for k in settings.SETTINGS_FILE_KEYS if k not in data]
        return (not missing, "set" if not missing else f"not set yet: {', '.join(missing)}")

    def run(self, ctx):
        from . import settings
        try:
            return settings.write_missing_defaults()
        except settings.SettingsError as e:
            raise StepFailed(str(e), self.hint) from None


STEPS = [Brew(), Build(), Data(), Database(), Wine(), Client(), PlayClient(), App(), Defaults()]


class PlanStep(Step):
    """A step of a plan-only run ($HS_TEST_POWER_PLAN_ONLY): done() and run() are a record in
    <plan dir>/bootstrap-state.json; the build step runs the real `hs build` (plan-only there too)."""

    def __init__(self, real: Step, state: Path):
        self.real, self.state = real, state
        self.id, self.label, self.hint = real.id, real.label, real.hint

    def _done_ids(self) -> list[str]:
        try:
            return list(json.loads(self.state.read_text()).get("done", []))
        except (OSError, ValueError):
            return []

    def done(self, ctx):
        return (self.id in self._done_ids(), "plan only: recorded as done")

    def run(self, ctx):
        res = {"plan_only": True}
        if isinstance(self.real, Build):
            self.real.run_build(ctx)
            res["cmd"] = "harness/hs " + " ".join(Build.args(ctx))
        self.state.write_text(json.dumps({"done": self._done_ids() + [self.id]}) + "\n")
        return res


# ---------------------------------------------------------------- preflight

def preflight(ctx: Ctx, todo_ids: list[str] | None = None) -> dict:
    """Refuse what can't work, before anything changes. Raises StepFailed with the exact fix."""
    pr = ctx.probe
    ev: dict = {}
    ev["arch"] = arch = pr.arch()
    if arch != "arm64":
        raise StepFailed(f"this Mac has an Intel processor ({arch}); Moonglade needs an Apple Silicon Mac (M1 or newer)",
                         "There's no fix on this Mac: the server build and the Wine runtime are for Apple Silicon.", ev)
    ev["macos"] = ver = pr.macos()
    try:
        vt = tuple(int(x) for x in (ver.split(".") + ["0"])[:2])
    except ValueError:
        vt = (0, 0)
    if vt < MIN_MACOS:
        raise StepFailed(f"this Mac runs macOS {ver}; Moonglade needs macOS {MIN_MACOS[0]}.{MIN_MACOS[1]} or newer "
                         "(the Wine runtime it uses is built for that)",
                         "Update macOS in System Settings > General > Software Update, then run ./bootstrap again.", ev)
    ev["rosetta"] = pr.rosetta()
    if not ev["rosetta"]:
        raise StepFailed("Rosetta 2 isn't installed (the Windows game runs through it)",
                         "Install it with: softwareupdate --install-rosetta --agree-to-license ; then run ./bootstrap "
                         "again.", ev)
    ev["command_line_tools"] = pr.clt()
    if not ev["command_line_tools"]:
        raise StepFailed("the Xcode Command Line Tools aren't installed (they provide the compiler)",
                         "Install them with: xcode-select --install ; then run ./bootstrap again.", ev)
    ev["homebrew"] = pr.brew()
    if not ev["homebrew"]:
        raise StepFailed(f"Homebrew isn't installed at {P.BREW} (Moonglade's build expects it there)",
                         f"Install it from https://brew.sh with: {HOMEBREW_INSTALL} ; then run ./bootstrap again.", ev)
    if ctx.client is None and todo_ids is not None and "client" not in todo_ids:
        ev["client"] = {"skipped": "no client given, and client/ is already installed and intact"}
    else:
        ev["client"] = ctx.client_info = check_client_source(ctx.client)
    from . import stack
    running = {k: v for k, v in stack.stack_processes().items() if v}
    lock = P.RUN / "play.lock"
    if running or _locked(lock):
        raise StepFailed("Moonglade (or its server) is running right now",
                         "Quit the game (or run harness/hs down), then run ./bootstrap again.", ev | {"running": running})
    free = pr.free_bytes()
    todo = todo_ids if todo_ids is not None else [s.id for s in STEPS]
    need_gb = round(sum(NEED_GB.get(i, 0.0) for i in todo) + 1.0, 1)        # + 1 GB headroom
    ev["disk"] = {"free_gb": round(free / 1e9, 1), "need_gb": need_gb, "for_steps": todo}
    if free < need_gb * 1e9:
        raise StepFailed(f"not enough free disk space: {free / 1e9:.1f} GB free, setup needs about {need_gb:.0f} GB "
                         f"for what's left to do", "Free up disk space (e.g. empty the Trash), then run ./bootstrap "
                                                   "again.", ev)
    return ev


def _locked(path: Path) -> bool:
    """Is a ./play holding its lock? (flock held by another process)"""
    if not path.exists():
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


# ---------------------------------------------------------------- the run

def _inject(allow: bool) -> tuple[dict | None, str | None]:
    raw = os.environ.get(TEST_ENV)
    if not raw:
        return None, None
    if not allow or not os.environ.get("HS_HARNESS_RUN"):
        return None, (f"{TEST_ENV} is set: that's a test switch for the harness, not for setting up. Unset it "
                      f"(`unset {TEST_ENV}`) and run ./bootstrap again.")
    try:
        inj = json.loads(raw)
    except ValueError as e:
        return None, f"{TEST_ENV} isn't valid JSON: {e}"
    return (inj if isinstance(inj, dict) else None), (None if isinstance(inj, dict) else f"{TEST_ENV} must be an object")


def run(client: str | None, yes: bool = False, check_only: bool = False, allow_test_inject: bool = False,
        jobs: int | None = None, ignore_power: bool = False) -> int:
    from . import power
    t_all = time.time()
    if allow_test_inject and not os.environ.get("HS_HARNESS_RUN"):
        say(f"{TEST_FLAG} is for the harness only.")
        return 2
    why = power.refusal("bootstrap", allow_test_inject)
    if why:
        say(why)
        return 2
    if jobs is not None and jobs < 1:
        say(f"--jobs must be 1 or more (got {jobs}); leave it out to use this Mac's performance cores.")
        return 2
    inj, err = _inject(allow_test_inject)
    if err:
        say(err)
        return 2
    try:
        pdir = power.plan_dir()
    except power.JobsError as e:
        say(str(e))
        return 2
    plan = Path(pdir) if pdir else None
    steps = [PlanStep(s, plan / "bootstrap-state.json") for s in STEPS] if plan else STEPS
    # Homebrew's tools (cmake, mysql, ...) for the steps, even when `brew shellenv` isn't in this shell's profile
    path = orig_path = os.environ.get("PATH", "")
    for d in (str(P.BREW / "sbin"), str(P.BREW / "bin")):
        if d not in path.split(":"):
            path = f"{path}:{d}" if path else d
    os.environ["PATH"] = path
    ctx = Ctx(client, yes, Probe(inj), jobs, ignore_power, plan)
    n = len(steps) + 1
    say("Moonglade setup" + (" (check only: nothing will be changed)" if check_only else "") + f" in {P.ROOT}")
    emit(event="start", root=str(P.ROOT), check_only=check_only, test_inject=sorted(inj) if inj else None,
         plan_only=bool(plan), jobs=jobs, ignore_power=ignore_power)
    guard = power.Guard("setup (./bootstrap)", "./bootstrap", ignore_power, say=lambda m: say("   " + m))

    def fail(i, step_id, label, e: StepFailed):
        say(f"\nSetup stopped at step {i}/{n}: {label}.\n  Problem: {e}\n  What to do: {e.hint or 'see the messages above'}")
        emit(event="step", step=step_id, state="failed", error=str(e), hint=e.hint, evidence=e.evidence,
             power=guard.record(), power_stop=isinstance(e, PowerStopped), total_s=round(time.time() - t_all, 1))
        return power.STOP_EXIT if isinstance(e, PowerStopped) else 1

    # 1. preflight (always; it changes nothing)
    say(f"[1/{n}] Checking this Mac and your client ...")
    emit(event="step", step="preflight", state="start")
    if plan:
        say("   plan only: preflight skipped")
        emit(event="step", step="preflight", state="skipped", why="plan only")
    try:
        if plan:
            raise _SkipPreflight
        # which steps are left decides the disk need; done() checks are read-only and cheap. The brew step is judged
        # by its own check below (it can't change disk use much).
        pre_done = {}
        for s in STEPS:
            if s.id == "brew":
                continue
            try:
                pre_done[s.id] = s.done(ctx)
            except Exception as e:  # noqa: BLE001  (a done() that can't decide counts as not done)
                pre_done[s.id] = (False, f"{type(e).__name__}: {e}")
        todo = [i for i, (ok, _w) in pre_done.items() if not ok]
        ev = preflight(ctx, todo)
    except _SkipPreflight:
        ev = None
    except StepFailed as e:
        return fail(1, "preflight", "Checking this Mac and your client", e)
    if ev is not None:
        cl = ev["client"]
        say(f"   ok: Apple Silicon, macOS {ev['macos']}, Rosetta, Command Line Tools, Homebrew; client "
            + (f"{cl['wow_exe_version']} enUS ({cl['kind']})" if "wow_exe_version" in cl else "already installed")
            + f"; {ev['disk']['free_gb']} GB free")
        emit(event="step", step="preflight", state="done", evidence=ev)

    for i, s in enumerate(steps, start=2):
        say(f"[{i}/{n}] {s.label} ...")
        t0 = time.time()
        try:
            ok, why = s.done(ctx)
        except Exception as e:  # noqa: BLE001
            ok, why = False, f"{type(e).__name__}: {e}"
        if ok:
            say(f"   already done ({why})")
            emit(event="step", step=s.id, state="already_done", why=why)
            continue
        if check_only:
            say(f"   to do ({why})")
            emit(event="step", step=s.id, state="todo", why=why)
            continue
        try:
            pause = guard.check(s.label)       # power-guard: pause below 35 % until 50 %; stop at 15 %
        except power.PowerStop as e:
            return fail(i, s.id, s.label, PowerStopped(f"the battery fell to {e.record.get('end_pct')} % while "
                                                       "paused, so setup stopped before this step", POWER_HINT,
                                                       {"pause": e.record}))
        if pause:
            emit(event="power", step=s.id, state="resumed", pause=pause)
        emit(event="step", step=s.id, state="start", why=why)
        try:
            res = s.run(ctx)
        except StepFailed as e:
            return fail(i, s.id, s.label, e)
        except Exception as e:  # noqa: BLE001
            return fail(i, s.id, s.label, StepFailed(f"{type(e).__name__}: {e}", s.hint))
        dt = time.time() - t0
        say(f"   done ({_fmt_s(dt)})")
        emit(event="step", step=s.id, state="done", seconds=round(dt, 1), result=res)
    total = time.time() - t_all
    if check_only:
        say("\nCheck complete: nothing was changed.")
        emit(event="end", ok=True, check_only=True, total_s=round(total, 1))
        return 0
    if plan:
        say("\nPlan complete: nothing was built or installed (plan only).")
        emit(event="end", ok=True, plan_only=True, total_s=round(total, 1), power=guard.record())
        return 0
    say(f"\nMoonglade is ready ({_fmt_s(total)}). Open {P.ROOT / 'dist' / 'Moonglade.app'} (double-click it in "
        "Finder, or drag it to the Dock), or run ./play in a terminal.")
    if str(P.BREW / "bin") not in orig_path.split(":"):
        say(f"To run ./play from a terminal, put Homebrew on your PATH first: eval \"$({BREW_BIN} shellenv)\" "
            "(add that line to ~/.zprofile to keep it). The app doesn't need this.")
    emit(event="end", ok=True, total_s=round(total, 1), power=guard.record())
    return 0


class _SkipPreflight(Exception):
    pass


def main(client: str | None, yes: bool, check_only: bool, allow_test_inject: bool, jobs: int | None = None,
         ignore_power: bool = False) -> int:
    # one bootstrap per folder: a flock on the repo folder itself, so a refused run creates no file
    fd = os.open(P.ROOT, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        say("Another ./bootstrap is already running in this folder; let it finish first.")
        os.close(fd)
        return 2
    try:
        return run(client, yes, check_only, allow_test_inject, jobs, ignore_power)
    finally:
        os.close(fd)
