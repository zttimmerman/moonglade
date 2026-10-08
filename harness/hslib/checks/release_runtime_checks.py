"""release-runtime (openspec/changes/release-first-launch): one resolver for dev and release paths, test switches that
can't reach a normal start, and release servers that run from the payload alone.

release.mode_resolution        (quick) dev: the full resolver table (every name paths.py exports, layout() of both
                               lanes, and the path constants of the modules moved onto the resolver) equals the table
                               recorded from the parent commit (release_paths_golden.json), for lanes A and B; a static
                               scan finds no P.ROOT/P.ENV/P.DATA/P.BREW use in hslib outside paths.py and the reviewed
                               dev-only list (DEV_ONLY_FILES / DEV_ONLY_LINES). Release: a scratch app (the packaged
                               payload's Python + this checkout's harness + VERSION) resolves to release mode with every
                               writable path under the scratch HOME and every read-only input under APP; without the
                               switch HOME is the user's ~/Library/Application Support/Moonglade, which the check never
                               creates or changes. Counterexamples: HS_WINE=whisky differs from the golden table; a
                               planted P.ENV line is found by the scan; the switch without the test flag isn't honoured.
release.test_switches_refused  (quick) each release test switch, without the test flag or outside a live harness run,
                               is refused (naming it) by ./play, harness/hs, the payload's `-m hslib.play`/`-m hslib`,
                               the dev app and a release app (test hook), which start and write nothing. Counterexample:
                               with the flag inside this harness run the switch is honoured.
release.stack_from_payload     (slow) a scratch copy of the packaged Moonglade.app, a scratch HOME and scratch ports,
                               under sandbox-exec (no /opt/homebrew, /usr/local, or the repo's vendor/ server/ build/
                               data/ env/ except env/release and the run registry; writes only to the scratch):
                               release mode brings the stack up with m0 and down; worldserver answers SOAP, the running
                               mysqld is the payload's, the DB updater names the payload's sql/, the servers' env has
                               OPENSSL_CONF = HOME's file and the AC_* module keys, m0's random-bot count (set only via
                               AC_*) is in effect, shutdown is graceful. Counterexample: the drop-AC_* test switch makes
                               the playerbots default (500) take effect, which the check sees.
"""
from __future__ import annotations

import json
import os
import plistlib
import pwd
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import paths as P
from ..registry import check, require

CAP = "release-runtime"
GOLDEN = Path(__file__).with_name("release_paths_golden.json")
SCRATCH_ROOT = P.OUT / "release-scratch"          # paths.SCRATCH_PARTS: the only place a scratch HOME may be
RELEASE_WORK = P.ENV / "release"
NEW_APP = RELEASE_WORK / "payload" / "Moonglade.app"
NEW_PAYLOAD = NEW_APP / "Contents" / "Resources" / "Moonglade"
OLD_PAYLOAD = RELEASE_WORK / "payload" / "Moonglade"       # release-build's layout, before release-first-launch
OPEN = "/usr/bin/open"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SCRATCH_PORTS = {"mysql": 13327, "auth": 13744, "world": 18105, "soap": 17898}

# The reviewed dev-only list (task 1.2): uses of P.ROOT / P.ENV / P.DATA / P.BREW that stay on purpose. Release mode
# never runs these (dev tooling: building the server or the dev app, the harness runner and lanes, the Ghidra and
# era-diff tools, release packaging, the public scan), or the line is inside a dev-only branch.
DEV_ONLY_FILES = {
    "app.py": "builds the dev app dist/Moonglade.app with the checkout's repo-path",
    "baseline.py": "dev baseline tool (the unpatched worldserver comparison)",
    "build.py": "builds the server from vendor/ with Homebrew's toolchain",
    "era.py": "era-diff tooling over the checkout's SQL sources",
    "ghidra.py": "read-only client RE tooling (ADR-0005)",
    "icon.py": "regenerates branding/ from env/branding",
    "lane.py": "harness lane-B worker",
    "lanes.py": "harness two-lane scheduler",
    "lanestack.py": "harness lane-B stack",
    "nightly.py": "nightly flake hunt over the checkout",
    "population.py": "population measurement helpers for checks (session files, disk numbers)",
    "publicscan.py": "public-repo scan of the checkout",
    "runner.py": "the harness runner (results, git state, disk in the checkout)",
    "timingbase.py": "harness timing baseline",
}
DEV_ONLY_DIRS = {
    "checks": "harness checks run only from a checkout",
    "probe": "dev diagnostic probe CLI",
}
DEV_ONLY_LINES = {
    "bootstrap.py": {
        'BREWFILE = P.ROOT / "Brewfile"': "dev ./bootstrap: Homebrew packages",
        'BREW_BIN = P.BREW / "bin" / "brew" if P.BREW else None    # dev only': "dev ./bootstrap: Homebrew",
        'if not rec or rec.get("repo") != str(P.ROOT.resolve()):': "dev step: the server install record's checkout",
        'if m.get("repo") != str(P.ROOT.resolve()):': "dev step: the database marker's checkout",
        'DB_MARKER.write_text(json.dumps({"repo": str(P.ROOT.resolve()), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),':
            "dev step: the database marker's checkout",
        'raise StepFailed(f"Homebrew isn\'t installed at {P.BREW} (Moonglade\'s build expects it there)",':
            "dev step: Homebrew",
        'for d in (str(P.BREW / "sbin"), str(P.BREW / "bin")):': "dev ./bootstrap: PATH for Homebrew",
        'say("Moonglade setup" + (" (check only: nothing will be changed)" if check_only else "") + f" in {P.ROOT}")':
            "dev ./bootstrap: message naming the checkout",
        'emit(event="start", root=str(P.ROOT), check_only=check_only, test_inject=sorted(inj) if inj else None,':
            "dev ./bootstrap: event naming the checkout",
        "say(f\"\\nMoonglade is ready ({_fmt_s(total)}). Open {P.ROOT / 'dist' / 'Moonglade.app'} (double-click it in \"":
            "dev ./bootstrap: the dev app's path",
        'if str(P.BREW / "bin") not in orig_path.split(":"):': "dev ./bootstrap: PATH hint for Homebrew",
        "fd = os.open(P.ROOT, os.O_RDONLY)": "dev ./bootstrap: the single-instance lock on the checkout",
    },
    "downloads.py": {
        '"ghidra": ("ghidra_url", "ghidra_sha256", P.ENV / "ghidra.zip", False),': "dev RE tool download",
        '"release-mysql": ("release_mysql_url", "release_mysql_sha256", P.ENV / "release/cache/mysql.tar.gz", True),':
            "release packaging input (tools/release)",
        'P.ENV / "release/cache/mysql-source.tar.gz", True),': "release packaging input (tools/release)",
        '"release-python": ("release_python_url", "release_python_sha256", P.ENV / "release/cache/python.tar.gz", True),':
            "release packaging input (tools/release)",
        'P.ENV / "release/cache/python-full.tar.zst", True),': "release packaging input (tools/release)",
        'RELEASE_CACHE = P.ENV / "release" / "cache"': "release packaging cache (tools/release)",
    },
    "dockshim.py": {
        'DIR = P.ENV / "dockshim"': "dev build output of the shim (release uses the payload's bin/hsdock.dylib)",
        'st = {"source": str(SOURCE.relative_to(P.ROOT)), "source_sha256": source_sha256(), "archs": list(ARCHS),':
            "dev shim build stamp",
    },
    "frontmenu.py": {
        'r = subprocess.run(["git", "-C", str(P.ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)':
            "dev branch of about_info (release reads VERSION)",
        'dirty = subprocess.run(["git", "-C", str(P.ROOT), "status", "--porcelain", "--untracked-files=no"],':
            "dev branch of about_info (release reads VERSION)",
    },
}
REPO_PATH_USE = re.compile(r"\b(?:P|_P|_P0|paths)\.(ROOT|ENV|DATA|BREW)\b")

# Release-mode names that must be under HOME (writable) or under APP (read-only inputs).
WRITABLE = ("HOME_FILE", "ENV", "DATA", "GLOBAL_RUN", "RUN", "LOGS", "TMP", "LANE_ROOT", "LANE_ETC", "MY_CNF",
            "MYSQL_DATA", "CLIENT_DATA", "DOWNLOADS", "WINE_RUNTIME", "WINE_RUNTIME_ARCHIVE", "WINEPREFIX", "WINE_BIN",
            "WINE_LOG", "CLIENT", "CLIENT_MANIFEST", "PLAYER_PLAY_CLIENT", "SETTINGS_FILE", "PLAYER_FILE",
            "PLAYER_BACKUPS", "OPENSSL_CONF", "SERVER_CWD", "DB_MARKER", "APP_LOG", "OUT", "RESULTS", "SUMMARY",
            "RUN_REGISTRY")
READ_ONLY = ("SERVER", "SERVER_BIN", "SERVER_ETC", "MYSQL_BASE", "MYSQLD", "MYSQLADMIN", "MYSQL", "MYSQLDUMP",
             "SQL_SOURCE", "HARNESS", "PATCHES", "BOTS_PATCHES", "CLIENT_PATCH_QUEUE", "MODULES")

# What dev mode's table is recorded from (identical to how release_paths_golden.json was made at f205d67).
DUMP_SRC = r'''
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hslib import paths as P
def norm(v):
    if isinstance(v, Path): v = str(v)
    if isinstance(v, str):
        return v.replace(str(P.ROOT), "<ROOT>").replace(str(Path.home()), "<USERHOME>")
    if isinstance(v, dict): return {str(k): norm(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)): return [norm(x) for x in v]
    if v is None or isinstance(v, (bool, int, float)): return v
    return repr(v)
out = {}
for k in sorted(vars(P)):
    v = getattr(P, k)
    if not k[:1].isupper() or callable(v) or isinstance(v, type): continue
    out[k] = norm(v)
out["layout(a)"] = norm(P.layout("a")); out["layout(b)"] = norm(P.layout("b"))
from hslib import stack, db, backups, settings, play, clientbuild, wineinst, downloads, bootstrap, launch, app, config
mods = {}
for mod, names in ((stack, ("MYSQLD", "MYSQLADMIN", "MY_CNF", "LOG", "ERRLOG", "PIDFILE", "PROFILE_FILE")), (db, ("MYSQL",)),
                   (backups, ("PLAYER_ROOT", "MYSQLDUMP")), (settings, ("FILE", "ERA_CONF_DIST")),
                   (play, ("LOCK", "PLAYER_FILE", "LAST")), (clientbuild, ("QUEUE", "PLAY")),
                   (wineinst, ("RUNTIME_DIR", "MARKER")), (downloads, ("PINS", "RELEASE_CACHE", "CLIENT_DATA", "CLIENT_DATA_MARKER")),
                   (bootstrap, ("HS", "BREWFILE", "BREW_BIN", "DB_MARKER")), (launch, ("WINE", "WINESERVER", "DOCK_TOKEN")),
                   (app, ("DIST", "APP", "TMP", "LOG"))):
    for n in names:
        mods[f"{mod.__name__.split('.')[-1]}.{n}"] = norm(getattr(mod, n))
mods["config.common()"] = norm(config.common())
with tempfile.TemporaryDirectory() as d:
    stack.MY_CNF = Path(d) / "my.cnf"
    stack._write_my_cnf()
    mods["stack.my_cnf_text"] = norm(stack.MY_CNF.read_text())
print(json.dumps({"mode": P.MODE, "paths": out, "modules": mods}, sort_keys=True))
'''

TABLE_SRC = r'''
import json, sys
from hslib import paths as P
print(json.dumps({"table": P.path_table(), "python": sys.executable, "hslib": __import__("hslib").__file__}))
'''


# ================================================================================================ shared helpers

def _clean_env(**extra) -> dict:
    """This process's environment without lane/Wine/release switches (plus `extra`)."""
    env = {k: v for k, v in os.environ.items()
           if k not in (P.LANE_ENV, "HS_WINE", P.HARNESS_RUN_REGISTRY_ENV) and k not in P.RELEASE_TEST_SWITCHES
           and not k.startswith("PYTHON")}
    env.update(extra)
    return env


def _outside_run(env: dict) -> dict:
    return {k: v for k, v in env.items() if k not in ("HS_HARNESS_RUN", "HS_HARNESS_RUN_ID")}


def heal_scratch() -> dict:
    """Kill-safety: a killed earlier run leaves scratch folders (and their mysqld/servers, a mounted image) under
    harness/out/release-scratch/<tag>-<pid>. Every folder whose run pid is gone is cleaned: processes whose argv names
    it are killed, images mounted inside it detached, the folder removed. Folders of live runs are left alone."""
    from .. import procs
    out = {"killed": [], "detached": [], "removed": []}
    if not SCRATCH_ROOT.is_dir():
        return out
    stale = []
    for x in SCRATCH_ROOT.iterdir():
        pid = x.name.rsplit("-", 1)[-1]
        if not x.is_dir() or not pid.isdigit():
            continue
        try:
            os.kill(int(pid), 0)
            alive = not procs.is_zombie(int(pid))
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        if not alive:
            stale.append(x)
    for x in stale:
        for pid, argv in procs.argv_table():
            if pid != os.getpid() and any(str(x) in a for a in argv):
                try:
                    os.kill(pid, 9)
                    out["killed"].append(pid)
                except ProcessLookupError:
                    pass
        info = subprocess.run(["/usr/bin/hdiutil", "info"], capture_output=True, text=True).stdout
        for line in info.splitlines():
            if str(x) in line and "\t" in line:
                mp = line.split("\t")[-1].strip()
                subprocess.run(["/usr/bin/hdiutil", "detach", "-force", mp], capture_output=True, timeout=120)
                out["detached"].append(mp)
        shutil.rmtree(x, ignore_errors=True)
        out["removed"].append(x.name)
    return out


def scratch_dir(tag: str) -> Path:
    d = SCRATCH_ROOT / f"{tag}-{os.getpid()}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    return d


def packaged_payload() -> Path | None:
    """The payload tools/release last built: Moonglade.app's (release-first-launch) or the older flat one."""
    for p in (NEW_PAYLOAD, OLD_PAYLOAD):
        if (p / "VERSION").is_file() and (p / "python/bin/python3").is_file():
            return p
    return None


def compile_launcher(out: Path) -> dict:
    """harness/app/launcher.m compiled like hslib/app.py does (this checkout's code), ad-hoc signed."""
    from .. import app as A
    tmp = out.parent / "cc-tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = os.environ | {"TMPDIR": str(tmp) + "/"}
    sdk = subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, text=True, env=env).stdout.strip()
    r = subprocess.run(["/usr/bin/clang", "-isysroot", sdk, "-fobjc-arc", "-O2", "-Wall", "-Werror",
                        f"-mmacosx-version-min={A.DEPLOYMENT_TARGET}", "-framework", "Cocoa", str(A.SOURCE),
                        "-o", str(out)], capture_output=True, text=True, env=env)
    shutil.rmtree(tmp, ignore_errors=True)
    if r.returncode != 0:
        raise RuntimeError(f"compiling the launcher failed: {(r.stderr or r.stdout)[-600:]}")
    return {"launcher": str(out)}


def quick_app(root: Path, with_launcher: bool = False) -> Path:
    """A minimal release Moonglade.app under `root` ("App Copy/Moonglade.app", a path with spaces) for the quick
    checks: the packaged payload's Python (an APFS clone), this checkout's harness/ (copied, so the resolver under
    test is the current one), VERSION and fork.lock; optionally this checkout's launcher as Contents/MacOS/Moonglade
    with a release Info.plist. Returns the payload folder (APP)."""
    src = packaged_payload()
    if src is None:
        raise RuntimeError("no packaged payload under env/release (run tools/release/package.sh all once)")
    app = root / "App Copy" / "Moonglade.app"
    pl = app / "Contents" / "Resources" / "Moonglade"
    pl.mkdir(parents=True)
    subprocess.run(["/bin/cp", "-c", "-R", str(src / "python"), str(pl / "python")], check=True)
    shutil.copytree(P.HARNESS, pl / "harness", symlinks=True,
                    ignore=shutil.ignore_patterns("out", "__pycache__", "*.pyc", "results.json", "summary.md",
                                                  "*.log"))
    for n in ("VERSION", "fork.lock"):
        shutil.copy2(src / n, pl / n)
    if with_launcher:
        (app / "Contents" / "MacOS").mkdir(parents=True)
        compile_launcher(app / "Contents" / "MacOS" / "Moonglade")
        with open(app / "Contents" / "Info.plist", "wb") as f:
            plistlib.dump({"CFBundleExecutable": "Moonglade", "CFBundleIdentifier": "local.moonglade.release",
                           "CFBundleName": "Moonglade", "CFBundlePackageType": "APPL",
                           "CFBundleInfoDictionaryVersion": "6.0", "LSMinimumSystemVersion": "13.0"}, f)
    return pl


def release_env(home: Path | None, *, flag_run: bool = True, **extra) -> dict:
    """Environment for a release-mode process: the scratch HOME switch (if given), this run's registry (so the
    payload can see the live harness run), and `extra`. flag_run=False leaves the harness-run markers out."""
    env = _clean_env(**extra)
    env[P.HARNESS_RUN_REGISTRY_ENV] = str(P.RUN_REGISTRY)
    if home is not None:
        env[P.RELEASE_HOME_ENV] = str(home)
    return env if flag_run else _outside_run(env)


def release_table(app: Path, env: dict, flag: bool, cwd: Path) -> dict:
    args = [str(app / "python/bin/python3"), "-I", "-c", TABLE_SRC] + ([P.RELEASE_TEST_FLAG] if flag else [])
    r = subprocess.run(args, capture_output=True, text=True, env=env, cwd=cwd, timeout=120)
    if r.returncode != 0:
        return {"rc": r.returncode, "stderr": r.stderr[-800:]}
    return {"rc": 0, **json.loads(r.stdout)}


def _under(p: str | None, root: Path) -> bool:
    if not p:
        return False
    q = Path(os.path.abspath(p))
    return q == root or root in q.parents


def release_table_problems(t: dict, app: Path, home: Path, scratch: Path) -> list[str]:
    """Why a release table isn't right: not release mode, a writable path not under `home`, a read-only input not
    under `app`, or any path naming the dev repo (outside `scratch`), /opt/homebrew or /usr/local."""
    probs = []
    if t.get("MODE") != "release":
        probs.append(f"MODE is {t.get('MODE')!r}, not release")
    if t.get("HOME") != str(home):
        probs.append(f"HOME is {t.get('HOME')}, not {home}")
    if t.get("APP") != str(app):
        probs.append(f"APP is {t.get('APP')}, not {app}")
    for k in WRITABLE:
        if not _under(t.get(k), home):
            probs.append(f"{k} = {t.get(k)} isn't under HOME")
    for k in READ_ONLY:
        if not _under(t.get(k), app):
            probs.append(f"{k} = {t.get(k)} isn't under APP")
    repo = P.ROOT.resolve()

    def walk(k, v):
        if isinstance(v, dict):
            for kk, vv in v.items():
                walk(f"{k}.{kk}", vv)
        elif isinstance(v, list):
            for i, vv in enumerate(v):
                walk(f"{k}[{i}]", vv)
        elif isinstance(v, str) and v.startswith("/"):
            if v.startswith(("/opt/homebrew", "/usr/local")):
                probs.append(f"{k} = {v} names {v.split('/')[1]}/{v.split('/')[2]}")
            elif _under(v, repo) and not _under(v, scratch):
                probs.append(f"{k} = {v} names the dev checkout")
    for k, v in t.items():
        if k not in ("RELEASE_HOME_DEFAULT", "WHISKY_LIBS"):      # outside both by definition; checked separately
            walk(k, v)
    return probs


def _dev_dump(lane: str, **extra) -> dict:
    env = _clean_env(HS_LANE=lane, **extra)
    r = subprocess.run([sys.executable, "-I", "-c", DUMP_SRC, str(P.HARNESS)], capture_output=True, text=True,
                       env=env, cwd=P.ROOT, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"dumping the dev table (lane {lane}) failed: {r.stderr[-800:]}")
    return json.loads(r.stdout)


def golden_diff(golden: dict, now: dict) -> list[str]:
    """Entries of the golden table that are missing or different now (new names are allowed: they're additions)."""
    out = []
    for part in ("paths", "modules"):
        for k, v in golden[part].items():
            if now[part].get(k, "<missing>") != v:
                out.append(f"{part}.{k}: was {json.dumps(v)[:160]}, now {json.dumps(now[part].get(k, '<missing>'))[:160]}")
    return out


def scan_repo_paths(hslib: Path) -> dict:
    """P.ROOT / P.ENV / P.DATA / P.BREW uses in `hslib` outside paths.py and the reviewed dev-only list."""
    found, allowed = [], 0
    for f in sorted(hslib.rglob("*.py")):
        rel = f.relative_to(hslib)
        if rel.parts[0] in DEV_ONLY_DIRS or str(rel) == "paths.py" or str(rel) in DEV_ONLY_FILES:
            continue
        ok_lines = DEV_ONLY_LINES.get(str(rel), {})
        for i, line in enumerate(f.read_text().splitlines(), 1):
            code = line.split("#", 1)[0] if not line.lstrip().startswith(("'", '"')) else line
            if REPO_PATH_USE.search(code) or REPO_PATH_USE.search(line.strip()):
                if line.strip() in ok_lines:
                    allowed += 1
                    continue
                found.append(f"{rel}:{i}: {line.strip()[:160]}")
    return {"violations": found, "reviewed_lines_hit": allowed}


def real_home_state() -> dict:
    h = P.RELEASE_HOME_DEFAULT
    if not h.exists():
        return {"exists": False}
    st = h.stat()
    return {"exists": True, "mtime_ns": st.st_mtime_ns, "listing": sorted(x.name for x in h.iterdir())}


# ================================================================================================ mode_resolution

@check("release.mode_resolution", order=14, capability=CAP, stack="any", tier="quick")
def mode_resolution(ctx):
    """Dev keeps the parent commit's full path table (lanes A and B) and no new repo-path use outside the reviewed
    list; a scratch release app resolves to release mode with writable paths under the scratch HOME and inputs under
    APP; without the switch HOME is the user's ~/Library/Application Support/Moonglade, untouched."""
    healed = heal_scratch()
    ev: dict = {"healed": healed, "started": time.time()}
    golden = json.loads(GOLDEN.read_text())
    ev["golden_from"] = golden.get("recorded_from")
    # dev, lanes A and B
    ev["dev"] = {}
    for lane in ("a", "b"):
        now = _dev_dump(lane)
        g = {"paths": golden["paths"][lane], "modules": golden["modules"][lane]}
        diff = golden_diff(g, now)
        ev["dev"][lane] = {"mode": now["mode"], "differences": diff[:20], "entries": len(g["paths"]) + len(g["modules"]),
                           "new_names": sorted(set(now["paths"]) - set(g["paths"]))}
        require(now["mode"] == "dev" and not diff, f"dev lane {lane.upper()}: the path table changed", **ev)
    # counterexample: another Wine layer gives a different table, and the comparison sees it
    other = _dev_dump("a", HS_WINE="whisky")
    ce = golden_diff({"paths": golden["paths"]["a"], "modules": golden["modules"]["a"]}, other)
    ev["counterexample_hs_wine_whisky"] = ce[:5]
    require(any(x.startswith("paths.WINE_BIN") for x in ce), "counterexample: HS_WINE=whisky wasn't seen as a change",
            **ev)
    # static scan of hslib, and a planted line
    scan = scan_repo_paths(P.HARNESS / "hslib")
    ev["static_scan"] = scan
    require(not scan["violations"], f"{len(scan['violations'])} P.ROOT/P.ENV/P.DATA/P.BREW use(s) outside paths.py "
                                    "and the reviewed dev-only list", **ev)
    d = scratch_dir("mode")
    try:
        planted = d / "hslib"
        shutil.copytree(P.HARNESS / "hslib", planted, ignore=shutil.ignore_patterns("__pycache__"))
        with open(planted / "play.py", "a") as f:
            f.write('\nPLANTED = P.ENV / "planted"\n')
        ps = scan_repo_paths(planted)
        ev["counterexample_planted"] = ps["violations"][:3]
        require(any(v.startswith("play.py:") and "planted" in v for v in ps["violations"]),
                "counterexample: a planted P.ENV line in play.py wasn't found", **ev)
        shutil.rmtree(planted)
        # release mode: a scratch app + scratch HOME through the test switch, in this harness run
        app = quick_app(d)
        home = d / "home"
        real_before = real_home_state()
        t = release_table(app, release_env(home), True, d)
        require(t.get("rc") == 0, "the payload's Python couldn't print the release table", **ev, release=t)
        tbl = t["table"]
        probs = release_table_problems(tbl, app, home, d)
        ev["release"] = {"app": str(app), "home": str(home), "python": t["python"], "hslib": t["hslib"],
                         "problems": probs[:20], "mode": tbl.get("MODE"),
                         "sample": {k: tbl.get(k) for k in ("RUN", "LANE_ETC", "MYSQL_DATA", "MYSQLD", "SQL_SOURCE",
                                                            "WINEPREFIX", "CLIENT", "PLAYER_FILE", "OPENSSL_CONF",
                                                            "SERVER_CWD", "BREW")}}
        require(t["python"].startswith(str(app) + "/python/") and t["hslib"].startswith(str(app) + "/harness/"),
                "the release table didn't come from the scratch app's own Python and harness", **ev)
        require(not probs, f"{len(probs)} release path problem(s)", **ev)
        require(not home.exists(), "printing the table created the scratch HOME", **ev)
        # default HOME, no switch
        t2 = release_table(app, release_env(None), False, d)
        want = Path(pwd.getpwuid(os.getuid()).pw_dir) / "Library" / "Application Support" / "Moonglade"
        real_after = real_home_state()
        ev["default_home"] = {"home": (t2.get("table") or {}).get("HOME"), "want": str(want),
                              "real_before": real_before, "real_after": real_after}
        require(t2.get("rc") == 0 and t2["table"]["HOME"] == str(want) and t2["table"]["MODE"] == "release",
                "without the switch HOME isn't the user's ~/Library/Application Support/Moonglade", **ev)
        require(real_before == real_after, "the real HOME was created or changed", **ev)
        # counterexample: the switch without the test flag isn't honoured, so the HOME checks fail
        t3 = release_table(app, release_env(home), False, d)
        ce3 = release_table_problems(t3.get("table") or {}, app, home, d)
        ev["counterexample_no_flag"] = ce3[:4]
        require(t3.get("rc") == 0 and any("isn't under HOME" in x for x in ce3),
                "counterexample: the HOME switch was honoured without the test flag", **ev)
        require(real_home_state() == real_before, "the real HOME was created or changed", **ev)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ev


# ================================================================================================ test_switches_refused

def switch_values(d: Path) -> dict:
    return {P.RELEASE_HOME_ENV: str(d / "home-refused"),
            P.RELEASE_PORTS_ENV: json.dumps(SCRATCH_PORTS),
            P.RELEASE_DOWNLOAD_BASE_ENV: "http://127.0.0.1:9/",
            P.FIRSTLAUNCH_TEST_ENV: json.dumps({"free_bytes": 1}),
            P.APP_TEST_CLIENT_PICK_ENV: str(d / "pick")}


def _run(args: list[str], env: dict, cwd: Path, timeout: int = 60) -> dict:
    t0 = time.time()
    r = subprocess.run(args, capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout)
    return {"rc": r.returncode, "out": (r.stdout + r.stderr).strip()[-400:], "s": round(time.time() - t0, 1)}


def _hook_events(path: Path) -> list[dict]:
    try:
        return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def open_with_hook(bundle: Path, hook: Path, env: dict, args: list[str], timeout: float = 30) -> dict:
    """`open -n` the app with the test hook and `env` (--env), wait for its exit event."""
    cmd = [OPEN, "-n", "--env", f"HS_APP_TEST_HOOK={hook}", *[x for k, v in env.items() for x in ("--env", f"{k}={v}")],
           str(bundle), "--args", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    t0 = time.time()
    while time.time() - t0 < timeout:
        evs = _hook_events(hook)
        if any(e.get("kind") == "exit" for e in evs):
            break
        time.sleep(0.25)
    return {"open_rc": r.returncode, "open_out": (r.stdout + r.stderr).strip()[-300:], "events": _hook_events(hook)}


def exec_with_hook(bundle: Path, hook: Path, env: dict, args: list[str], timeout: float = 60) -> dict:
    """Run the app's launcher binary directly with the test hook (no LaunchServices); stopped after `timeout`."""
    full = _clean_env(HS_APP_TEST_HOOK=str(hook), **env)
    proc = subprocess.Popen([str(bundle / "Contents/MacOS/Moonglade"), *args], env=full, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, 9)
        rc = "timeout"
    return {"rc": rc, "events": _hook_events(hook)}


@check("release.test_switches_refused", order=14, capability=CAP, stack="down", tier="quick")
def test_switches_refused(ctx):
    """Each release test switch is refused, naming it, by ./play, harness/hs, the payload's -m hslib.play / -m hslib,
    the dev app (test hook) and a release app (test hook) without the test flag or outside a live harness run; nothing
    starts and the scratch and real HOMEs stay absent/unchanged. With the flag in this run the switch is honoured."""
    healed = heal_scratch()
    from .. import app as A, stack
    ev: dict = {"healed": healed, "started": time.time(), "cases": []}
    d = scratch_dir("switches")
    real_before = real_home_state()
    try:
        app = quick_app(d, with_launcher=True)
        bundle = app.parents[2]
        vals = switch_values(d)
        cur = A.up_to_date()
        if not cur.get("current"):          # a changed launcher.m: rebuild dist/Moonglade.app (as app.bundle_valid does)
            rb = subprocess.run([str(P.HARNESS / "hs"), "app"], capture_output=True, text=True, timeout=600,
                                env=_clean_env())
            ev["dev_app_rebuilt"] = {"why": cur.get("why"), "rc": rb.returncode, "tail": rb.stderr[-300:]}
        dev_app_ok = bool(A.up_to_date().get("current"))
        bad = []
        for name, val in vals.items():
            base = _clean_env(**{name: val})
            entries = [
                ("./play", [str(P.ROOT / "play")], base, P.ROOT),
                ("./play --allow-test-release outside a harness run", [str(P.ROOT / "play"), P.RELEASE_TEST_FLAG],
                 _outside_run(base), P.ROOT),
                ("harness/hs status", [str(P.HARNESS / "hs"), "status"], base, P.ROOT),
                ("harness/hs --allow-test-release status outside a harness run",
                 [str(P.HARNESS / "hs"), P.RELEASE_TEST_FLAG, "status"], _outside_run(base), P.ROOT),
                ("payload -m hslib.play", [str(app / "python/bin/python3"), "-I", "-B", "-m", "hslib.play"],
                 release_env(None, **{name: val}), d),
                ("payload -m hslib.play --allow-test-release outside a harness run",
                 [str(app / "python/bin/python3"), "-I", "-B", "-m", "hslib.play", P.RELEASE_TEST_FLAG],
                 release_env(None, flag_run=False, **{name: val}), d),
                ("payload -m hslib status", [str(app / "python/bin/python3"), "-I", "-B", "-m", "hslib", "status"],
                 release_env(None, **{name: val}), d),
            ]
            for label, args, env, cwd in entries:
                r = _run(args, env, cwd)
                ok = r["rc"] != 0 and name in r["out"] and "test switch" in r["out"]
                ev["cases"].append({"switch": name, "entry": label, "ok": ok, **r})
                if not ok:
                    bad.append(f"{label} with {name}")
            # the apps (test hook): a release app without the flag (and, for HS_RELEASE_HOME, with the flag but a
            # HOME outside the scratch rule), and the dev app dist/Moonglade.app
            app_cases = [("release app (test hook, no flag)", bundle, {name: val}, [])]
            if name == P.RELEASE_HOME_ENV:
                app_cases.append(("release app (test hook, flag, HOME outside .../harness/out/release-scratch/)",
                                  bundle, {name: str(d.parent.parent / "not-scratch-home")}, [P.RELEASE_TEST_FLAG]))
            if dev_app_ok:
                app_cases.append(("dev app (test hook)", A.APP, {name: val}, []))
            for label, target, env, args in app_cases:
                hook = d / f"hook-{len(ev['cases'])}.jsonl"
                res = open_with_hook(target, hook, env, args)
                refused = [e for e in res["events"] if e.get("kind") == "refused"]
                ex = [e for e in res["events"] if e.get("kind") == "exit"]
                ok = (bool(refused) and name in refused[0].get("switches", []) and bool(ex) and ex[-1].get("code") == 2
                      and not any(e.get("kind") in ("status", "stage", "detail") for e in res["events"]))
                ev["cases"].append({"switch": name, "entry": label, "ok": ok, "events": res["events"][:3],
                                    "open_rc": res["open_rc"]})
                if not ok:
                    bad.append(f"{label} with {name}")
        ev["dev_app_tested"] = dev_app_ok
        ev["refused_cases"] = sum(1 for c in ev["cases"] if c["ok"])
        ev["failed_cases"] = bad
        ev["scratch_home_created"] = (d / "home-refused").exists()
        ev["stack_after"] = {k: v for k, v in stack.stack_processes().items() if v}
        ev["real_home"] = {"before": real_before, "after": real_home_state()}
        require(dev_app_ok, "dist/Moonglade.app isn't current (harness/hs app); the dev app case can't run", **ev)
        require(not bad, f"{len(bad)} entry point(s) didn't refuse a switch: {bad[:6]}", **ev)
        require(not ev["scratch_home_created"] and not ev["stack_after"], "something was started or written", **ev)
        require(ev["real_home"]["before"] == ev["real_home"]["after"], "the real HOME was created or changed", **ev)
        # counterexample: with the flag inside this harness run the switch is honoured (the refusal is real)
        home = d / "home-allowed"
        t = release_table(app, release_env(home), True, d)
        ev["counterexample_allowed"] = {"rc": t.get("rc"), "home": (t.get("table") or {}).get("HOME")}
        require(t.get("rc") == 0 and t["table"]["HOME"] == str(home),
                "counterexample: the switch wasn't honoured with the flag inside the harness run", **ev)
        # ... and a release app with the test hook and the flag, inside this run, honours it: its play loop is the
        # payload's Python (-I -B -m hslib.play), its log is in the scratch HOME, and PATH has no Homebrew (tasks 2.3,
        # 5.3). The play loop's single-instance lock is held here, so it stops at once ("already running").
        import fcntl
        home2 = d / "h2"
        (home2 / "run").mkdir(parents=True)
        lock_fd = os.open(home2 / "run" / "play.lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(lock_fd, str(os.getpid()).encode())
            hook = d / "hook-allowed.jsonl"
            run_env = {P.RELEASE_HOME_ENV: str(home2), "HS_HARNESS_RUN": os.environ.get("HS_HARNESS_RUN", ""),
                       "HS_HARNESS_RUN_ID": os.environ.get("HS_HARNESS_RUN_ID", ""),
                       P.HARNESS_RUN_REGISTRY_ENV: str(P.RUN_REGISTRY)}
            # the launcher binary is run directly (not through LaunchServices): its play loop reads the scratch app and
            # HOME under ~/Documents, and a LaunchServices launch of a scratch copy (a new TCC identity per path)
            # makes macOS ask for Documents access every time; run directly, it belongs to the harness's own grant
            res = exec_with_hook(bundle, hook, run_env, [P.RELEASE_TEST_FLAG], timeout=60)
        finally:
            os.close(lock_fd)
        app_log = home2 / "logs" / "app.log"
        text = app_log.read_text(errors="replace") if app_log.is_file() else ""
        loop_line = next((x for x in text.splitlines() if "play loop pid" in x), "")
        ev["release_app_allowed"] = {"events": [e.get("kind") + ":" + str(e.get("id", "")) for e in res["events"]],
                                     "app_log": str(app_log), "loop_line": loop_line[-300:],
                                     "busy_line": next((x for x in text.splitlines() if "already running" in x), "")}
        require(loop_line.endswith(f"{app / 'python/bin/python3'} -I -B -m hslib.play {P.RELEASE_TEST_FLAG}")
                and "already running" in ev["release_app_allowed"]["busy_line"]
                and any(e.get("kind") == "dialog" and e.get("id") == "already_running" for e in res["events"]),
                "the release app (hook + flag, in this run) didn't run the payload's play loop with its log in the "
                "scratch HOME", **ev)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ev


# ================================================================================================ stack_from_payload

def sandbox_profile(scratch: Path) -> str:
    """release-build's no-Homebrew profile, extended (design "Checks"): no reads of /opt/homebrew, /usr/local or the
    dev checkout's vendor/ server/ build/ data/ env/ (except env/release/ and the run registry), and no writes in the
    user's home folder outside `scratch`."""
    repo = P.ROOT.resolve()
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    deny_read = " ".join(f'(subpath "{repo / x}")' for x in ("vendor", "server", "build", "data", "env"))
    return "\n".join([
        "(version 1)", "(allow default)",
        '(deny file-read* (subpath "/opt/homebrew") (subpath "/usr/local"))',
        f"(deny file-read* {deny_read})",
        f'(allow file-read* (subpath "{repo / "env" / "release"}") (subpath "{P.RUN_REGISTRY.resolve()}"))',
        f'(deny file-write* (subpath "{home}"))',
        f'(allow file-write* (subpath "{scratch.resolve()}"))',
    ]) + "\n"


def release_fixture_app(d: Path) -> Path:
    """A clonefile copy of the packaged Moonglade.app at "<d>/Moonglade Test/Moonglade.app". Returns APP (the
    payload folder inside it)."""
    dst = d / "Moonglade Test" / "Moonglade.app"
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["/bin/cp", "-c", "-R", str(NEW_APP), str(dst)], check=True)
    return dst / "Contents" / "Resources" / "Moonglade"


def _q(app: Path, sql: str, port: int, db: str = "acore_auth") -> str | None:
    r = subprocess.run([str(app / "mysql/bin/mysql"), "--no-defaults", "--protocol=TCP", "-h127.0.0.1", f"-P{port}",
                        f"-u{P.DB_USER}", f"-p{P.DB_PASS}", "--batch", "--skip-column-names", db, "-e", sql],
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip() if r.returncode == 0 else None


def _proc_info(pred) -> list[dict]:
    from .. import procs
    out = []
    for pid, argv in procs.argv_table():
        if pred(argv):
            ae = procs.args_env(pid)
            env = dict(x.split("=", 1) for x in (ae[1] if ae else []) if "=" in x)
            out.append({"pid": pid, "exe": procs.path(pid), "argv": argv[:4], "env": env})
    return out


def run_release(app: Path, args: list[str], env: dict, prof: Path, cwd: Path, timeout: int, log: Path) -> dict:
    t0 = time.time()
    with open(log, "a") as fh:
        fh.write(f"\n==== {time.strftime('%H:%M:%S')} {' '.join(args)}\n")
        fh.flush()
        try:
            r = subprocess.run([SANDBOX_EXEC, "-f", str(prof), str(app / "python/bin/python3"), "-I", "-B", "-m", "hslib",
                                P.RELEASE_TEST_FLAG, *args], stdout=subprocess.PIPE, stderr=fh, text=True, env=env,
                               cwd=cwd, timeout=timeout)
            rc, out = r.returncode, r.stdout
        except subprocess.TimeoutExpired as e:
            rc, out = "timeout", str(e.stdout or "")
    try:
        js = json.loads(out) if out.strip().startswith("{") else None
    except ValueError:
        js = None
    return {"rc": rc, "json": js, "stdout_tail": out[-600:], "s": round(time.time() - t0, 1)}


@check("release.stack_from_payload", order=14, capability=CAP, stack="down", tier="slow")
def stack_from_payload(ctx):
    """Release mode brings the stack up (m0) and down from a scratch copy of the packaged app, a scratch HOME and
    scratch ports, under a sandbox hiding Homebrew and the dev checkout: SOAP answers, mysqld is the payload's, the
    updater's SourceDirectory is the payload's sql/ (and it populated the databases), OPENSSL_CONF and the AC_* keys
    are in the servers' environment, m0's random-bot count (only via AC_*) is in effect, and shutdown is graceful.
    Counterexample: without the AC_* module keys the playerbots default (500 bots) takes effect and is seen."""
    healed = heal_scratch()
    from .release_checks import _need_payload
    ev: dict = {"healed": healed, "started": time.time()}
    ev["package"] = {k: v for k, v in _need_payload(ctx).items() if k in ("package_rc", "version", "package_seconds")}
    require((NEW_PAYLOAD / "VERSION").is_file(), "no Moonglade.app payload from this run's packaging", **ev)
    d = SCRATCH_ROOT / f"sp-{os.getpid()}"           # short: HOME/run/mysql.sock must fit a 104-byte socket path
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    home = d / "h"
    log = d / "release.log"
    ports = dict(SCRATCH_PORTS)
    try:
        app = release_fixture_app(d)
        home.mkdir()
        t0 = time.time()
        subprocess.run(["/bin/cp", "-c", "-R", str(P.CLIENT_DATA), str(home / "client-data")], check=True)
        ev["client_data_clone_s"] = round(time.time() - t0, 1)
        prof = d / "release.sb"
        prof.write_text(sandbox_profile(d))
        proof = subprocess.run([SANDBOX_EXEC, "-f", str(prof), "/bin/ls", str(P.ROOT / "vendor")], capture_output=True,
                               text=True)
        ev["sandbox"] = {"profile": prof.read_text(), "ls_vendor_rc": proof.returncode}
        require(proof.returncode != 0, "the sandbox profile doesn't hide the checkout's vendor/", **ev)
        (d / "tmp").mkdir()
        env = release_env(home, HS_RELEASE_PORTS=json.dumps(ports), TMPDIR=str(d / "tmp") + "/",
                          PATH="/usr/bin:/bin:/usr/sbin:/sbin")
        up = run_release(app, ["up", "--profile", "m0"], env, prof, d, 2400, log)
        ev["up"] = {k: up[k] for k in ("rc", "s", "stdout_tail")}
        info = up["json"] or {}
        ev["up_info"] = {k: info.get(k) for k in ("first_run", "profile", "mysqld", "authserver", "worldserver",
                                                  "ready_s")}
        require(up["rc"] == 0 and info.get("worldserver") == "started", "release mode couldn't bring the stack up",
                **ev, log_tail=log.read_text(errors="replace")[-1500:],
                world_log=(home / "logs/worldserver.out").read_text(errors="replace")[-1500:]
                if (home / "logs/worldserver.out").exists() else None)
        # what is running
        my_cnf = f"--defaults-file={home / 'my.cnf'}"
        mysqld = _proc_info(lambda a: my_cnf in a)
        world = _proc_info(lambda a: a[:1] == [str(app / "server/bin/worldserver")])
        auth = _proc_info(lambda a: a[:1] == [str(app / "server/bin/authserver")])
        ws_env = world[0]["env"] if world else {}
        ev["running"] = {"mysqld": [{k: x[k] for k in ("pid", "exe", "argv")} for x in mysqld],
                         "worldserver": [{k: x[k] for k in ("pid", "exe", "argv")} for x in world],
                         "authserver": [{k: x[k] for k in ("pid", "exe")} for x in auth],
                         "worldserver_env": {k: v for k, v in ws_env.items()
                                             if k.startswith("AC_AI_PLAYERBOT_M") or k in ("OPENSSL_CONF",)},
                         "worldserver_ac_keys": len([k for k in ws_env if k.startswith("AC_")])}
        require(len(mysqld) == 1 and mysqld[0]["exe"] == str(app / "mysql/bin/mysqld"),
                "the running mysqld isn't the payload's", **ev)
        require(world and world[0]["exe"] == str(app / "server/bin/worldserver") and auth
                and auth[0]["exe"] == str(app / "server/bin/authserver"), "the servers aren't the payload's", **ev)
        require(ws_env.get("OPENSSL_CONF") == str(home / "etc/openssl.cnf") and (home / "etc/openssl.cnf").is_file()
                and auth[0]["env"].get("OPENSSL_CONF") == str(home / "etc/openssl.cnf"),
                "OPENSSL_CONF isn't HOME's openssl.cnf for the servers", **ev)
        # the updater's source: the rendered key, and it populated the databases from it (vendor/ is unreadable here)
        conf = (home / "etc/worldserver.conf").read_text()
        src = re.search(r'^SourceDirectory\s*=\s*"(.*)"\s*$', conf, re.M)
        mexe = re.search(r'^MySQLExecutable\s*=\s*"(.*)"\s*$', conf, re.M)
        wlog = (home / "logs/worldserver.out").read_text(errors="replace")
        ev["updater"] = {"SourceDirectory": src and src.group(1), "MySQLExecutable": mexe and mexe.group(1),
                         "populated": wlog.count("is empty, auto populating it"),
                         "applying_lines": wlog.count(">> Applying"),
                         "dead_refs": wlog.count("is missing in your update directory"),
                         "errors": [l for l in wlog.splitlines() if "sql.updates" in l.lower() and "error" in l.lower()][:5]}
        require(src and src.group(1) == str(app / "sql") and mexe and mexe.group(1) == str(app / "mysql/bin/mysql"),
                "SourceDirectory / MySQLExecutable aren't the payload's", **ev)
        require(ev["updater"]["populated"] >= 3 and ev["updater"]["applying_lines"] > 0
                and ev["updater"]["dead_refs"] == 0, "the updater didn't populate the databases cleanly", **ev)
        # m0's random-bot count, set only through AC_* (the payload has no readable module conf: cwd HOME/run)
        n_acc = _q(app, f"SELECT COUNT(*) FROM account WHERE username LIKE '{'RNDBOT'}%'", ports["mysql"])
        ev["m0"] = {"AC_AI_PLAYERBOT_MAX_RANDOM_BOTS": ws_env.get("AC_AI_PLAYERBOT_MAX_RANDOM_BOTS"),
                    "rndbot_accounts": n_acc, "etc_modules_under_cwd": (home / "run/etc/modules").exists()}
        require(ws_env.get("AC_AI_PLAYERBOT_MAX_RANDOM_BOTS") == "10" and n_acc is not None and 0 < int(n_acc) <= 10
                and not ev["m0"]["etc_modules_under_cwd"], "m0's random-bot count isn't in effect", **ev)
        down = run_release(app, ["down"], env, prof, d, 300, log)
        dj = down["json"] or {}
        ev["down"] = {"rc": down["rc"], "worldserver": dj.get("worldserver"), "mysqld": dj.get("mysqld"),
                      "remaining": dj.get("remaining"), "stdout_tail": down["stdout_tail"][-300:],
                      "log_tail": log.read_text(errors="replace")[-800:]}
        require(down["rc"] == 0 and (dj.get("worldserver") or {}).get("graceful") is True and dj.get("graceful") is True
                and not dj.get("remaining"), "the stack didn't shut down gracefully", **ev)
        # counterexample: drop the AC_* module keys; the playerbots default (500 random bots -> ~50 accounts) shows
        ce_env = dict(env, **{P.FIRSTLAUNCH_TEST_ENV: json.dumps({"drop_ac_env": True})})
        args = [SANDBOX_EXEC, "-f", str(prof), str(app / "python/bin/python3"), "-I", "-B", "-m", "hslib",
                P.RELEASE_TEST_FLAG, "up", "--profile", "m0"]
        with open(log, "a") as fh:
            fh.write("\n==== counterexample: drop_ac_env\n")
            fh.flush()
            proc = subprocess.Popen(args, stdout=fh, stderr=fh, env=ce_env, cwd=d)
        seen, t1 = None, time.time()
        try:
            while time.time() - t1 < 900 and proc.poll() is None:
                n = _q(app, "SELECT COUNT(*) FROM account WHERE username LIKE 'RNDBOT%'", ports["mysql"])
                if n is not None and int(n) > 10:
                    seen = int(n)
                    break
                time.sleep(2)
            world = _proc_info(lambda a: a[:1] == [str(app / "server/bin/worldserver")])
            ce_env_seen = world[0]["env"] if world else {}
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
            cd = run_release(app, ["down"], env, prof, d, 300, log)
        ev["counterexample"] = {"rndbot_accounts": seen, "after_s": round(time.time() - t1, 1),
                                "AC_AI_PLAYERBOT_MAX_RANDOM_BOTS": ce_env_seen.get("AC_AI_PLAYERBOT_MAX_RANDOM_BOTS"),
                                "ac_keys": sorted(k for k in ce_env_seen if k.startswith("AC_")),
                                "down_rc": cd["rc"], "remaining": (cd["json"] or {}).get("remaining")}
        require(seen is not None and "AC_AI_PLAYERBOT_MAX_RANDOM_BOTS" not in ce_env_seen,
                "counterexample: without the AC_* module keys the default bot count wasn't seen", **ev)
        require(cd["rc"] == 0 and not (cd["json"] or {}).get("remaining"),
                "counterexample: the stack didn't stop afterwards", **ev)
        require(not _proc_info(lambda a: any(str(home) in x for x in a)), "processes of the scratch HOME remain",
                **ev)
    finally:
        # never leave a scratch stack behind
        for pinfo in _proc_info(lambda a: any(str(d) in x for x in a)):
            try:
                os.kill(pinfo["pid"], 9)
            except ProcessLookupError:
                pass
        ev["log_tail"] = log.read_text(errors="replace")[-1200:] if log.exists() else None
        shutil.rmtree(d, ignore_errors=True)
    return ev


# ================================================================================================ app_readonly, app_moved
# Scope note (builder a, 2026-10-08): first launch (tasks 3.x) and the release play loop with the stand-in client
# (release.play_standin) aren't built yet, so these two checks exercise what release mode has today: the resolver and
# the release stack (database first start, servers, shutdown) from a read-only volume and from a moved app. The
# scenarios' "first launch runs headless" and "stand-in play loop reaches ready" parts are to be added to them when
# those exist (tasks 3.5 and 4.2).

PROBLEM_LINES = re.compile(r"read-only file system|permission denied|operation not permitted|\berofs\b|\beacces\b",
                           re.I)


def tree_hash(root: Path) -> dict:
    """sha256 over every path under root (relative path, type, mode, size, content or link target) and the count."""
    import hashlib
    h = hashlib.sha256()
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            f = Path(dirpath) / name
            st = f.lstat()
            rel = str(f.relative_to(root))
            h.update(f"{rel}\0{st.st_mode}\0".encode())
            if f.is_symlink():
                h.update(os.readlink(f).encode())
            elif f.is_file():
                h.update(str(st.st_size).encode())
                with open(f, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
            n += 1
    return {"sha256": h.hexdigest(), "entries": n}


def problem_lines(*files: Path) -> list[str]:
    out = []
    for f in files:
        for p in ([f] if f.is_file() else sorted(x for x in f.rglob("*") if x.is_file()) if f.is_dir() else []):
            try:
                for line in p.read_text(errors="replace").splitlines():
                    if PROBLEM_LINES.search(line):
                        out.append(f"{p.name}: {line.strip()[:200]}")
            except OSError:
                pass
    return out


def stack_cycle(app: Path, home: Path, d: Path, prof: Path, log: Path, tag: str) -> dict:
    """Release `up --profile m0` then `down` with APP `app` and HOME `home` (scratch ports); evidence of both."""
    env = release_env(home, HS_RELEASE_PORTS=json.dumps(SCRATCH_PORTS), TMPDIR=str(d / "tmp") + "/",
                      PATH="/usr/bin:/bin:/usr/sbin:/sbin")
    (d / "tmp").mkdir(exist_ok=True)
    up = run_release(app, ["up", "--profile", "m0"], env, prof, d, 2400, log)
    info = up["json"] or {}
    world = _proc_info(lambda a: a[:1] == [str(app / "server/bin/worldserver")])
    mysqld = _proc_info(lambda a: f"--defaults-file={home / 'my.cnf'}" in a)
    down = run_release(app, ["down"], env, prof, d, 300, log)
    dj = down["json"] or {}
    return {"tag": tag, "up_rc": up["rc"], "up_s": up["s"], "first_run": info.get("first_run"),
            "worldserver": info.get("worldserver"), "mysqld": info.get("mysqld"),
            "initialized": "initializing repo-local MySQL datadir" in log.read_text(errors="replace").split(
                f"==== ")[-2] if log.exists() else None,
            "world_exe": [x["exe"] for x in world], "mysqld_exe": [x["exe"] for x in mysqld],
            "down_rc": down["rc"], "graceful": (dj.get("worldserver") or {}).get("graceful") is True
            and dj.get("graceful") is True, "remaining": dj.get("remaining"),
            "up_tail": up["stdout_tail"][-300:] if up["rc"] != 0 else None}


def _cycle_ok(c: dict, app: Path) -> bool:
    return (c["up_rc"] == 0 and c["worldserver"] == "started" and c["down_rc"] == 0 and c["graceful"]
            and not c["remaining"] and c["world_exe"] == [str(app / "server/bin/worldserver")]
            and c["mysqld_exe"] == [str(app / "mysql/bin/mysqld")])


def _home_prep(d: Path) -> Path:
    home = d / "h"
    home.mkdir()
    subprocess.run(["/bin/cp", "-c", "-R", str(P.CLIENT_DATA), str(home / "client-data")], check=True)
    return home


@check("release.app_readonly", order=14, capability=CAP, stack="down", tier="slow")
def app_readonly(ctx):
    """Moonglade.app on a read-only disk image at a path with spaces: release mode sets up and runs the stack from it
    with a scratch HOME (database first start, m0, graceful shutdown), no log line reports a read-only or permission
    error, and the image's contents hash the same before and after. Counterexample: writing into the app from the
    image fails and that failure is caught by the same log scan. (First launch + the stand-in play loop: see the scope
    note above.)"""
    healed = heal_scratch()
    from .release_checks import _need_payload
    ev: dict = {"healed": healed, "started": time.time(), "scope": "stack only until first launch / play_standin exist (tasks 3.x)"}
    _need_payload(ctx)
    d = SCRATCH_ROOT / f"ro-{os.getpid()}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    mnt = d / "Read Only Disk"
    log = d / "release.log"
    attached = False
    try:
        stage = d / "stage"
        stage.mkdir()
        subprocess.run(["/bin/cp", "-c", "-R", str(NEW_APP), str(stage / "Moonglade.app")], check=True)
        dmg = d / "Moonglade.dmg"
        t0 = time.time()
        r = subprocess.run(["/usr/bin/hdiutil", "create", "-quiet", "-srcfolder", str(stage), "-volname", "Moonglade RO",
                            "-format", "UDRO", "-fs", "APFS", str(dmg)], capture_output=True, text=True, timeout=900)
        require(r.returncode == 0, "hdiutil create failed", **ev, out=(r.stdout + r.stderr)[-400:])
        shutil.rmtree(stage)
        mnt.mkdir()
        r = subprocess.run(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-mountpoint",
                            str(mnt), str(dmg)], capture_output=True, text=True, timeout=300)
        require(r.returncode == 0, "hdiutil attach -readonly failed", **ev, out=(r.stdout + r.stderr)[-400:])
        attached = True
        ev["image_s"] = round(time.time() - t0, 1)
        app = mnt / "Moonglade.app" / "Contents" / "Resources" / "Moonglade"
        mount_info = subprocess.run(["/sbin/mount"], capture_output=True, text=True).stdout
        ev["mount"] = [l for l in mount_info.splitlines() if str(mnt) in l]
        require(ev["mount"] and "read-only" in ev["mount"][0], "the image isn't mounted read-only", **ev)
        before = tree_hash(mnt)
        home = _home_prep(d)
        prof = d / "release.sb"
        prof.write_text(sandbox_profile(d))
        c = stack_cycle(app, home, d, prof, log, "read-only image")
        ev["cycle"] = c
        after = tree_hash(mnt)
        ev["image_hash"] = {"before": before, "after": after}
        probs = problem_lines(log, home / "logs")
        ev["problem_lines"] = probs[:10]
        require(_cycle_ok(c, app) and c["first_run"] is True, "the stack didn't run from the read-only image", **ev)
        require(not probs, "a log line reports a read-only or permission error", **ev)
        require(before == after and before["entries"] > 1000, "the image's contents changed", **ev)
        # counterexample: a write into the app on the image fails, and the log scan catches it
        ce_log = d / "ce.log"
        subprocess.run([str(app / "python/bin/python3"), "-I", "-B", "-c",
                        f"open({str(app / 'written-by-moonglade')!r}, 'w')"], stdout=open(ce_log, "w"),
                       stderr=subprocess.STDOUT, timeout=60)
        ev["counterexample"] = problem_lines(ce_log)[:2]
        require(ev["counterexample"], "counterexample: a write into the read-only app wasn't caught", **ev)
    finally:
        for pinfo in _proc_info(lambda a: any(str(d) in x for x in a)):
            try:
                os.kill(pinfo["pid"], 9)
            except ProcessLookupError:
                pass
        if attached:
            subprocess.run(["/usr/bin/hdiutil", "detach", "-force", str(mnt)], capture_output=True, timeout=120)
        shutil.rmtree(d, ignore_errors=True)
    return ev


@check("release.app_moved", order=14, capability=CAP, stack="down", tier="slow")
def app_moved(ctx):
    """Set up a scratch HOME from the app at one path (database first start), move the app to a second path, and run
    again: no new setup (the datadir is reused, no initialisation), every server runs from the new APP with the same
    HOME, and neither run writes anything inside APP (its tree hashes the same). Counterexample: the payload's Python
    without -B writes __pycache__ into the app, and the hash comparison sees it. (First launch + the stand-in play
    loop: see the scope note above.)"""
    healed = heal_scratch()
    from .release_checks import _need_payload
    ev: dict = {"healed": healed, "started": time.time(), "scope": "stack only until first launch / play_standin exist (tasks 3.x)"}
    _need_payload(ctx)
    d = SCRATCH_ROOT / f"mv-{os.getpid()}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    log = d / "release.log"
    try:
        app1 = release_fixture_app(d)
        bundle1 = app1.parents[2]
        home = _home_prep(d)
        prof = d / "release.sb"
        prof.write_text(sandbox_profile(d))
        h0 = tree_hash(bundle1)
        c1 = stack_cycle(app1, home, d, prof, log, "first path")
        h1 = tree_hash(bundle1)
        bundle2 = d / "Moved Here" / "Moonglade.app"
        bundle2.parent.mkdir()
        os.rename(bundle1, bundle2)
        app2 = bundle2 / "Contents" / "Resources" / "Moonglade"
        c2 = stack_cycle(app2, home, d, prof, log, "after the move")
        h2 = tree_hash(bundle2)
        conf = (home / "etc/worldserver.conf").read_text()
        ev |= {"first": c1, "moved": c2, "app_hash": {"before": h0, "after_first": h1, "after_move": h2},
               "conf_source_dir": (re.search(r'^SourceDirectory\s*=\s*"(.*)"', conf, re.M) or [None, None])[1],
               "home_entries": sorted(x.name for x in home.iterdir())}
        require(_cycle_ok(c1, app1) and c1["first_run"] is True, "the first run didn't set up and run", **ev)
        require(_cycle_ok(c2, app2), "the stack didn't run from the moved app", **ev)
        require(c2["first_run"] is False and c2["initialized"] is False,
                "the moved app set the databases up again", **ev)
        require(ev["conf_source_dir"] == str(app2 / "sql"), "the confs don't follow the moved app", **ev)
        require(h0 == h1 == h2, "something was written inside the app", **ev)
        # counterexample: without -B the payload's Python writes __pycache__ into the app, and the hash sees it
        subprocess.run([str(app2 / "python/bin/python3"), "-I", "-c", "import hslib.paths"], capture_output=True,
                       env=release_env(home), cwd=d, timeout=60)
        h3 = tree_hash(bundle2)
        ev["counterexample"] = {"hash_changed": h3 != h2, "entries_added": h3["entries"] - h2["entries"]}
        require(h3 != h2, "counterexample: a write inside the app wasn't seen", **ev)
    finally:
        for pinfo in _proc_info(lambda a: any(str(d) in x for x in a)):
            try:
                os.kill(pinfo["pid"], 9)
            except ProcessLookupError:
                pass
        shutil.rmtree(d, ignore_errors=True)
    return ev
