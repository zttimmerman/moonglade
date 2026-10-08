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
    ev: dict = {"started": time.time()}
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


@check("release.test_switches_refused", order=14, capability=CAP, stack="down", tier="quick")
def test_switches_refused(ctx):
    """Each release test switch is refused, naming it, by ./play, harness/hs, the payload's -m hslib.play / -m hslib,
    the dev app (test hook) and a release app (test hook) without the test flag or outside a live harness run; nothing
    starts and the scratch and real HOMEs stay absent/unchanged. With the flag in this run the switch is honoured."""
    from .. import app as A, stack
    ev: dict = {"started": time.time(), "cases": []}
    d = scratch_dir("switches")
    real_before = real_home_state()
    try:
        app = quick_app(d, with_launcher=True)
        bundle = app.parents[2]
        vals = switch_values(d)
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
                ("payload -m hslib.play", [str(app / "python/bin/python3"), "-I", "-m", "hslib.play"],
                 release_env(None, **{name: val}), d),
                ("payload -m hslib.play --allow-test-release outside a harness run",
                 [str(app / "python/bin/python3"), "-I", "-m", "hslib.play", P.RELEASE_TEST_FLAG],
                 release_env(None, flag_run=False, **{name: val}), d),
                ("payload -m hslib status", [str(app / "python/bin/python3"), "-I", "-m", "hslib", "status"],
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
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ev
