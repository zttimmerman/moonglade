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
                               the dev app and a release app (test hook; launcher binaries run directly, never `open`),
                               which start and write nothing; with the hook and the flag but a run id that isn't live,
                               the release app's window (first-launch or play-loop error) names the switch in plain
                               words and only logs are written. Counterexample: with the flag inside this harness run
                               the switch is honoured.
release.stack_from_payload     (slow) a scratch copy of the packaged Moonglade.app, a scratch HOME and scratch ports,
                               under sandbox-exec (no /opt/homebrew, /usr/local, or the repo's vendor/ server/ build/
                               data/ env/ except env/release and the run registry; writes only to the scratch):
                               release mode brings the stack up with m0 and down; worldserver answers SOAP, the running
                               mysqld is the payload's, the DB updater names the payload's sql/, the servers' env has
                               OPENSSL_CONF = HOME's file and the AC_* module keys, m0's random-bot count (set only via
                               AC_*) is in effect, shutdown is graceful. Counterexample: the drop-AC_* test switch makes
                               the playerbots default (500) take effect, which the check sees.
release.app_readonly, release.app_moved, release.play_standin, release.client_smoke, release.player_data_untouched,
release.readline_stub          see each check's docstring (release-first-launch builder c; design "As built").
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
import tempfile
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


# ------------------------------------------------------------------------------------------- disk images (app_readonly)

def attached_images() -> list[dict]:
    """Every attached disk image: {"image": image-path, "devs": whole-disk dev entries, "mounts": mount points}
    (hdiutil info -plist). An image stays listed under its original path after its file is deleted."""
    r = subprocess.run(["/usr/bin/hdiutil", "info", "-plist"], capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"hdiutil info failed: {r.stderr.decode(errors='replace')[-300:]}")
    out = []
    for img in plistlib.loads(r.stdout).get("images", []):
        ents = img.get("system-entities", [])
        out.append({"image": img.get("image-path", ""),
                    "devs": sorted({e["dev-entry"] for e in ents if re.fullmatch(r"/dev/disk\d+", e.get("dev-entry", ""))}),
                    "mounts": [e["mount-point"] for e in ents if e.get("mount-point")]})
    return out


def _real(p) -> str:
    return os.path.realpath(str(p))


def detach_image(image: Path) -> list[str]:
    """Detach every device of `image` (by its image-path; mounted or not). Returns the devices detached."""
    done = []
    for img in attached_images():
        if _real(img["image"]) != _real(image):
            continue
        for dev in img["devs"]:
            r = subprocess.run(["/usr/bin/hdiutil", "detach", "-force", dev], capture_output=True, text=True,
                               timeout=120)
            if r.returncode == 0:
                done.append(dev)
    return done


ATTACH_RETRY_S = (5, 15, 30)


def attach_readonly(dmg: Path, mnt: Path, run=None) -> dict:
    """`hdiutil attach -readonly` with retries: a "Resource temporarily unavailable" (EAGAIN) answer is retried with
    backoff (ATTACH_RETRY_S). After every attempt that didn't succeed, every device of this image is detached (hdiutil
    can report a failure and still leave the image attached, unmounted: seen 2026-10-09). `run` replaces
    subprocess.run (the counterexample's simulated failure). Returns {"ok", "attempts": [...], "detached": [...]}."""
    run = run or subprocess.run
    cmd = ["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-noverify", "-mountpoint",
           str(mnt), str(dmg)]
    out: dict = {"ok": False, "attempts": [], "detached": []}
    for i in range(len(ATTACH_RETRY_S) + 1):
        t0 = time.time()
        r = run(cmd, capture_output=True, text=True, timeout=300)
        text = ((r.stdout or "") + (r.stderr or "")).strip()
        out["attempts"].append({"rc": r.returncode, "s": round(time.time() - t0, 1), "out": text[-300:]})
        if r.returncode == 0:
            out["ok"] = True
            return out
        out["detached"] += detach_image(dmg)
        if "temporarily unavailable" not in text.lower() or i == len(ATTACH_RETRY_S):
            break
        time.sleep(ATTACH_RETRY_S[i])
    return out


def _scratch_owner_alive(folder_name: str) -> bool:
    from .. import procs
    pid = folder_name.rsplit("-", 1)[-1]
    if not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)
        return not procs.is_zombie(int(pid))
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def heal_images() -> list[str]:
    """Detach every attached image whose image-path is under harness/out/release-scratch/ and whose scratch folder is
    gone or belongs to a run that is no longer alive (a killed or failed app_readonly; the folder may already be
    deleted while the image stays attached)."""
    root = _real(SCRATCH_ROOT)
    out = []
    for img in attached_images():
        ip = _real(img["image"])
        if not ip.startswith(root + os.sep):
            continue
        folder = Path(root) / Path(ip).relative_to(root).parts[0]
        if folder.is_dir() and _scratch_owner_alive(folder.name):
            continue
        for dev in img["devs"]:
            r = subprocess.run(["/usr/bin/hdiutil", "detach", "-force", dev], capture_output=True, text=True,
                               timeout=120)
            if r.returncode == 0:
                out.append(f"{dev} ({img['image']})")
    return out


def heal_scratch() -> dict:
    """Kill-safety: a killed earlier run leaves scratch folders (and their mysqld/servers, an attached image) under
    harness/out/release-scratch/<tag>-<pid>. Every folder whose run pid is gone is cleaned: processes whose argv names
    it are killed, the folder removed. Every attached image whose image-path is under release-scratch/ is detached
    when its folder is gone or its run isn't alive (heal_images; by image-path, so an image whose folder was already
    deleted is found too). Folders and images of live runs are left alone."""
    from .. import procs
    out = {"killed": [], "detached": [], "removed": []}
    if not SCRATCH_ROOT.is_dir():
        out["detached"] += heal_images()
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
        out["detached"] += heal_images()        # the folder's run is gone: its images (mounted or not) go too
        from .. import pristine
        try:
            pristine.unlock(x)              # a scratch HOME's client copy is write-protected (first launch)
        except OSError:
            pass
        shutil.rmtree(x, ignore_errors=True)
        out["removed"].append(x.name)
    out["detached"] += heal_images()        # images whose scratch folder is already gone
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
            plistlib.dump({"CFBundleExecutable": "Moonglade", "CFBundleIdentifier": "io.github.zttimmerman.moonglade",
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


def exec_with_hook(bundle: Path, hook: Path, env: dict, args: list[str], timeout: float = 60,
                   outside_run: bool = False) -> dict:
    """Run the app's launcher binary directly with the test hook (no LaunchServices, so no per-copy privacy prompt);
    stopped after `timeout`. outside_run=True leaves this harness run's markers (HS_HARNESS_RUN/_ID) out."""
    full = _clean_env(HS_APP_TEST_HOOK=str(hook), **env)
    if outside_run:
        full = _outside_run(full)
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
        # Every case is an independent process (its own environment, hook file, scratch HOME); they all refuse before
        # starting anything, so they run side by side (quick-tier headroom; each case's assertion is unchanged) and
        # are judged afterwards in the order they are listed.
        jobs = []                                   # (switch, kind, label, fn) in the listed order
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
                jobs.append((name, "entry", label, lambda args=args, env=env, cwd=cwd: _run(args, env, cwd)))
            # the apps (test hook; the launcher binary run directly, never through LaunchServices): a release app
            # without the flag, with the flag outside a harness run, (for HS_RELEASE_HOME) with the flag but a HOME
            # outside the scratch rule, and the dev app dist/Moonglade.app: each refuses at startup
            sh = {} if name == P.RELEASE_HOME_ENV else {P.RELEASE_HOME_ENV: str(d / "home-refused")}
            app_cases = [("release app (test hook, no flag)", bundle, {name: val}, [], False),
                         ("release app (test hook, flag, outside a harness run)", bundle, {name: val, **sh},
                          [P.RELEASE_TEST_FLAG], True)]
            if name == P.RELEASE_HOME_ENV:
                app_cases.append(("release app (test hook, flag, HOME outside .../harness/out/release-scratch/)",
                                  bundle, {name: str(d.parent.parent / "not-scratch-home")}, [P.RELEASE_TEST_FLAG],
                                  False))
            else:
                app_cases.append(("release app (test hook, flag, no scratch HOME)", bundle, {name: val},
                                  [P.RELEASE_TEST_FLAG], False))
            if dev_app_ok:
                app_cases.append(("dev app (test hook)", A.APP, {name: val}, [], False))
            for label, target, env, args, outside in app_cases:
                hook = d / f"hook-{len(jobs)}.jsonl"
                jobs.append((name, "app", label, lambda target=target, hook=hook, env=env, args=args, outside=outside:
                             exec_with_hook(target, hook, env, args, timeout=30, outside_run=outside)))
            # the release app with the hook, the flag, a scratch HOME and a harness run id that isn't live: the app
            # passes the switches on, the payload's Python refuses them, and the window names them in plain words
            # (first-launch window for a HOME not set up, the play loop's error for a set-up one); only logs written
            for where in ("setup", "play"):
                ch = d / f"stale-{where}-{len(jobs)}"
                if where == "play":
                    ch.mkdir(parents=True)
                    (ch / "home.json").write_text(json.dumps({"layout": P.HOME_LAYOUT_VERSION,
                                                              "first_launch": {"complete": True}}) + "\n")
                env = {**{name: val}, P.RELEASE_HOME_ENV: str(ch), "HS_HARNESS_RUN": "1",
                       "HS_HARNESS_RUN_ID": "1-0", P.HARNESS_RUN_REGISTRY_ENV: str(P.RUN_REGISTRY)}
                hook = d / f"hook-{len(jobs)}.jsonl"
                jobs.append((name, f"stale:{where}:{ch}", f"release app (test hook, flag, run id not live; {where})",
                             lambda hook=hook, env=env: exec_with_hook(bundle, hook, env, [P.RELEASE_TEST_FLAG],
                                                                        timeout=60)))
        from concurrent.futures import ThreadPoolExecutor
        t_jobs = time.time()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda j: j[3](), jobs))
        ev["cases_wall_s"] = round(time.time() - t_jobs, 1)
        for (name, kind, label, _fn), res in zip(jobs, results):
            if kind == "entry":
                r = res
                ok = r["rc"] != 0 and name in r["out"] and "test switch" in r["out"]
                ev["cases"].append({"switch": name, "entry": label, "ok": ok, **r})
                if not ok:
                    bad.append(f"{label} with {name}")
            elif kind == "app":
                refused = [e for e in res["events"] if e.get("kind") == "refused"]
                ex = [e for e in res["events"] if e.get("kind") == "exit"]
                ok = (bool(refused) and name in refused[0].get("switches", []) and name in refused[0].get("text", "")
                      and bool(ex) and ex[-1].get("code") == 2 and res["rc"] == 2
                      and not any(e.get("kind") in ("status", "stage", "detail", "setup_window")
                                  for e in res["events"]))
                ev["cases"].append({"switch": name, "entry": label, "ok": ok, "events": res["events"][:3],
                                    "rc": res["rc"]})
                if not ok:
                    bad.append(f"{label} with {name}")
            else:
                _, where, ch = kind.split(":", 2)
                ch = Path(ch)
                evs = res["events"]
                shown = [e for e in evs if (e.get("kind") == "setup_window" and e.get("state") == "error")
                         or (e.get("kind") == "dialog" and e.get("id") == "test_switch_refused")]
                text = (shown[-1].get("text") or "") if shown else ""
                left = sorted(str(x.relative_to(ch)) for x in ch.rglob("*")) if ch.exists() else []
                allowed = {"home.json"} if where == "play" else set()
                ok = (bool(shown) and shown[-1].get("title") == "Moonglade can't start" and name in text
                      and "test setting" in text and "refused" in text and res["rc"] not in (0, "timeout")
                      and not any(e.get("kind") in ("stage", "firstlaunch") for e in evs)
                      and all(x in allowed or x == "logs" or x.startswith("logs/") for x in left))
                ev["cases"].append({"switch": name, "entry": label, "ok": ok, "rc": res["rc"], "shown": shown[-1:],
                                    "home_after": left})
                if not ok:
                    bad.append(f"release app, run id not live ({where}) with {name}")
        ev["dev_app_tested"] = dev_app_ok
        ev["refused_cases"] = sum(1 for c in ev["cases"] if c["ok"])
        ev["failed_cases"] = bad
        ev["scratch_home_created"] = (d / "home-refused").exists()      # every refusal above is before any write
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
        # a set-up HOME (release-first-launch 3.5: a HOME whose first launch isn't complete gets the first-launch
        # window instead of the play loop; this control is about the play loop)
        (home2 / "home.json").write_text(json.dumps({"layout": P.HOME_LAYOUT_VERSION,
                                                     "first_launch": {"complete": True}}) + "\n")
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


class SqlFileWatch:
    """Polls (lsof) the .sql files open in the processes whose executable is `exe` while the block runs: the DB
    updater holds each file it applies open as the mysql client's input (StartProcess)."""

    def __init__(self, exe: Path, every: float = 0.3):
        import threading
        self.exe, self.every, self.files, self.polls = str(exe), every, set(), 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        from .. import procs
        while not self._stop.is_set():
            pids = [str(pid) for pid, argv in procs.argv_table() if argv[:1] == [self.exe]]
            if pids:
                r = subprocess.run(["/usr/sbin/lsof", "-nP", "-Fn", "-p", ",".join(pids)], capture_output=True,
                                   text=True, timeout=30)
                self.polls += 1
                self.files |= {x[1:] for x in r.stdout.splitlines() if x.startswith("n") and x.endswith(".sql")}
            self._stop.wait(self.every)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(timeout=60)


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
        with SqlFileWatch(app / "server/bin/worldserver") as watch:
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
        # the files the updater applied: AzerothCore's updater logs only file names (DBUpdater.cpp ">> Applying
        # '<name>'", StartProcess's argument trace is off for it: secure=true), so the check reads the .sql files the
        # worldserver had open (lsof, polled during `up`) and checks each "Applying" name against them
        sql_root = str(app / "sql") + "/"
        applied = re.findall(r">> Applying '([^']+)'", wlog)
        seen = sorted(watch.files)
        # the one .sql file the updater writes itself: CreateDatabase (DBUpdater.cpp:238-262) writes a one-line
        # "CREATE DATABASE" query to create_table.sql in the worldserver's cwd (HOME/run), applies it and removes it,
        # when a database doesn't exist yet. Allowed only at exactly that path and only if the log says it created one.
        temp_sql = str(home / "run" / "create_table.sql")
        created = "Creating database" in wlog
        ev["updater"]["create_table_sql"] = {"path": temp_sql, "seen_open": temp_sql in seen, "log_creating_database": created}
        seen = [f for f in seen if not (f == temp_sql and created)]
        ev["updater"]["sql_files_open"] = {"count": len(seen), "polls": watch.polls, "sample": seen[:5],
                                           "outside_app_sql": [f for f in seen if not f.startswith(sql_root)][:5],
                                           "applying_names": len(applied),
                                           "applying_names_seen_open": len({Path(f).name for f in seen}
                                                                           & set(applied))}
        require(seen and not ev["updater"]["sql_files_open"]["outside_app_sql"]
                and ev["updater"]["sql_files_open"]["applying_names_seen_open"] > 0,
                "the DB updater didn't apply .sql files from the payload's sql/ (as its log names them)", **ev)
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
# Both run first launch and/or the release play loop through the release app itself (the launcher binary run
# directly with the test hook: no LaunchServices, so no per-copy TCC prompt; see design "As built", TCC), with the
# stand-in client (firstlaunch_checks.app_play) and the sandbox profile above.

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


@check("release.app_readonly", order=14, capability=CAP, stack="down", tier="slow")
def app_readonly(ctx):
    """Moonglade.app on a read-only disk image (hdiutil attach -readonly) at a path with spaces and a scratch HOME,
    under the no-Homebrew sandbox: the app's first launch runs headless (test hook, scripted pick of client/, loopback
    downloads) through every step, its Play goes on to the play loop with the stand-in client, which reaches ready
    and quits; the servers ran from the image, no log line reports a read-only or permission error, and the image's
    contents hash the same before and after. Counterexample: writing into the app on the image fails and that failure
    is caught by the same log scan."""
    from .firstlaunch_checks import FL_CACHE, LoopbackServer, _cleanup, app_play, ensure_cache, play_ok
    healed = heal_scratch()
    from .release_checks import _need_payload
    ev: dict = {"healed": healed, "started": time.time()}
    _need_payload(ctx)
    ensure_cache()
    d = SCRATCH_ROOT / f"ro-{os.getpid()}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    mnt = d / "Read Only Disk"
    dmg = d / "Moonglade.dmg"
    try:
        stage = d / "stage"
        stage.mkdir()
        subprocess.run(["/bin/cp", "-c", "-R", str(NEW_APP), str(stage / "Moonglade.app")], check=True)
        t0 = time.time()
        r = subprocess.run(["/usr/bin/hdiutil", "create", "-quiet", "-srcfolder", str(stage), "-volname", "Moonglade RO",
                            "-format", "UDRO", "-fs", "APFS", str(dmg)], capture_output=True, text=True, timeout=900)
        require(r.returncode == 0, "hdiutil create failed", **ev, out=(r.stdout + r.stderr)[-400:])
        shutil.rmtree(stage)
        mnt.mkdir()
        att = attach_readonly(dmg, mnt)
        ev["attach"] = att
        require(att["ok"], "hdiutil attach -readonly failed (after retries)", **ev,
                still_attached=[i for i in attached_images() if _real(i["image"]) == _real(dmg)])
        ev["image_s"] = round(time.time() - t0, 1)
        bundle = mnt / "Moonglade.app"
        app = bundle / "Contents" / "Resources" / "Moonglade"
        mount_info = subprocess.run(["/sbin/mount"], capture_output=True, text=True).stdout
        ev["mount"] = [l for l in mount_info.splitlines() if str(mnt) in l]
        require(ev["mount"] and "read-only" in ev["mount"][0], "the image isn't mounted read-only", **ev)
        before = tree_hash(mnt)
        home = d / "h"
        prof = d / "release.sb"
        prof.write_text(sandbox_profile(d))
        with LoopbackServer(FL_CACHE) as srv:
            run = app_play(bundle, home, d, "ro", pick=f"{P.CLIENT};play", sandbox=prof, timeout=2400,
                           extra_env={P.RELEASE_DOWNLOAD_BASE_ENV: srv.base})
        ev["app"] = {k: v for k, v in run.items() if k != "_events"}
        after = tree_hash(mnt)
        ev["image_hash"] = {"before": before, "after": after}
        probs = problem_lines(home / "logs")
        ev["problem_lines"] = probs[:10]
        steps = [s for s, st in run["firstlaunch_steps"] if st == "done"]
        require(steps == ["preflight", "client", "data", "wine", "database", "playclient", "defaults"]
                and any(w[0] == "done" for w in run["setup_windows"]),
                "first launch didn't run every step from the read-only image", **ev)
        pp = play_ok(run)
        require(not pp, f"the play loop from the read-only image didn't reach ready and stop cleanly: {pp}", **ev)
        require([x["exe"] for x in run["while_ready"]["mysqld"]] == [str(app / "mysql/bin/mysqld")]
                and [x["exe"] for x in run["while_ready"]["worldserver"]] == [str(app / "server/bin/worldserver")],
                "the servers didn't run from the image", **ev)
        # the done screen's Play button started the play loop (the scripted pick answers "play" on the done screen)
        evs = run["_events"]
        i_play = next((i for i, e in enumerate(evs) if e.get("kind") == "setup_button" and e.get("button") == "play"
                       and e.get("state") == "done"), None)
        i_stage = next((i for i, e in enumerate(evs) if e.get("kind") == "stage"), None)
        app_log = (home / "logs" / "app.log").read_text(errors="replace") if (home / "logs/app.log").is_file() else ""
        fl_exit, loop_at = app_log.find("first launch exited with 0"), app_log.find("play loop pid")
        ev["play_button"] = {"button_event": i_play, "first_stage_event": i_stage, "first_launch_exit_at": fl_exit,
                             "play_loop_line_at": loop_at}
        require(i_play is not None and i_stage is not None and i_play < i_stage and 0 <= fl_exit < loop_at,
                "the done screen's Play button didn't start the play loop", **ev)
        require(not probs, "a log line reports a read-only or permission error", **ev)
        require(before == after and before["entries"] > 1000, "the image's contents changed", **ev)
        # counterexample: a write into the app on the image fails, and the log scan catches it
        ce_log = d / "ce.log"
        with open(ce_log, "w") as fh:
            subprocess.run([str(app / "python/bin/python3"), "-I", "-B", "-c",
                            f"open({str(app / 'written-by-moonglade')!r}, 'w')"], stdout=fh,
                           stderr=subprocess.STDOUT, timeout=60)
        ev["counterexample"] = problem_lines(ce_log)[:2]
        require(ev["counterexample"], "counterexample: a write into the read-only app wasn't caught", **ev)
    finally:
        for pinfo in _proc_info(lambda a: any(str(d) in x for x in a)):
            try:
                os.kill(pinfo["pid"], 9)
            except ProcessLookupError:
                pass
        ev["detached_at_end"] = detach_image(dmg)        # by image-path: mounted or not, whatever attach said
        _cleanup(d)
    ev["images_left"] = [i["image"] for i in attached_images() if _real(i["image"]).startswith(_real(d))]
    require(not ev["images_left"], "a scratch image is still attached after the check", **ev)
    ev["counterexample_attach_failure"] = attach_failure_counterexample()
    return ev


def attach_failure_counterexample() -> dict:
    """A small scratch image whose `hdiutil attach` really attaches it but is reported as EAGAIN (the 2026-10-09
    verifier case): attach_readonly retries and detaches after each "failed" attempt, so nothing stays attached; and
    an image left attached (unmounted) whose scratch folder is then deleted is detached by heal_scratch."""
    d = SCRATCH_ROOT / f"ax-{os.getpid()}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    ev: dict = {}
    try:
        (d / "src").mkdir()
        (d / "src" / "f").write_text("x")
        dmg = d / "small.dmg"
        r = subprocess.run(["/usr/bin/hdiutil", "create", "-quiet", "-srcfolder", str(d / "src"), "-format", "UDRO",
                            "-fs", "APFS", str(dmg)], capture_output=True, text=True, timeout=300)
        require(r.returncode == 0, "counterexample: hdiutil create failed", out=(r.stdout + r.stderr)[-300:])
        calls = []

        def fake_run(cmd, **kw):                     # attaches for real, then answers like the flaky hdiutil did
            real = subprocess.run(cmd, **kw)
            calls.append(real.returncode)
            return subprocess.CompletedProcess(cmd, 1, "", "hdiutil: attach failed - Resource temporarily unavailable")
        mnt = d / "mnt"
        mnt.mkdir()
        old = globals()["ATTACH_RETRY_S"]
        globals()["ATTACH_RETRY_S"] = (1, 1)
        try:
            att = attach_readonly(dmg, mnt, run=fake_run)
        finally:
            globals()["ATTACH_RETRY_S"] = old
        left = [i for i in attached_images() if _real(i["image"]) == _real(dmg)]
        ev["simulated"] = {"real_attach_rcs": calls, "result_ok": att["ok"], "attempts": len(att["attempts"]),
                           "detached": att["detached"], "still_attached": left}
        require(calls and all(c == 0 for c in calls) and not att["ok"] and len(att["attempts"]) == 3
                and len(att["detached"]) >= 3 and not left,
                "counterexample: a reported attach failure left the image attached (or wasn't retried)", **ev)
        # heal: attached without a mount point, folder deleted (as the verifier saw), owner pid gone
        ghost = SCRATCH_ROOT / f"ghost-{_dead_pid()}"
        ghost.mkdir()
        g = ghost / "g.dmg"
        shutil.copy2(dmg, g)
        r = subprocess.run(["/usr/bin/hdiutil", "attach", "-readonly", "-nomount", "-noverify", str(g)],
                           capture_output=True, text=True, timeout=300)
        shutil.rmtree(ghost)
        before = [i["image"] for i in attached_images() if _real(i["image"]) == _real(g)]
        healed = heal_scratch()
        after = [i["image"] for i in attached_images() if _real(i["image"]) == _real(g)]
        ev["heal"] = {"attach_rc": r.returncode, "attached_before": before, "healed": healed["detached"],
                      "attached_after": after}
        require(r.returncode == 0 and before and not after,
                "counterexample: heal_scratch didn't detach an image whose scratch folder is gone", **ev)
    finally:
        detach_image(d / "small.dmg")
        shutil.rmtree(d, ignore_errors=True)
    return ev


def _dead_pid() -> int:
    pid = 99999
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid
        except PermissionError:
            pass
        pid -= 1


@check("release.app_moved", order=17, capability=CAP, stack="down", tier="slow")
def app_moved(ctx):
    """The run's shared HOME (first launch completed from the app at one path): the app is moved to a second path
    and opened again (test hook, stand-in client): no first-launch window, the play loop reaches ready and stops
    cleanly, the servers run from the new APP with the same HOME (rendered confs name the new APP's sql/), and
    nothing is written inside the app (its tree hashes the same). Counterexample: the payload's Python without -B
    writes __pycache__ into the app, and the hash comparison sees it."""
    from .firstlaunch_checks import _FIXTURE, app_play, fixture, play_ok
    healed = heal_scratch()
    fx = fixture(ctx)
    ev: dict = {"healed": healed, "started": time.time(), "fixture": fx}
    d, home, bundle1 = Path(fx["d"]), Path(fx["home"]), Path(fx["bundle"])
    h1 = tree_hash(bundle1)
    bundle2 = d / f"Moved Here {int(time.time()) % 10000}" / "Moonglade.app"
    bundle2.parent.mkdir()
    os.rename(bundle1, bundle2)
    _FIXTURE["bundle"] = str(bundle2)                 # later users of the shared HOME follow the move
    app2 = bundle2 / "Contents" / "Resources" / "Moonglade"
    prof = d / "release.sb"
    prof.write_text(sandbox_profile(d))
    run = app_play(bundle2, home, d, "moved", sandbox=prof)
    h2 = tree_hash(bundle2)
    conf = (home / "etc/worldserver.conf").read_text()
    paths_used = [x["exe"] for v in run["while_ready"].values() for x in v]
    ev |= {"moved_from": str(bundle1), "moved_to": str(bundle2), "app": {k: v for k, v in run.items() if k != "_events"},
           "app_hash": {"before": h1, "after": h2}, "paths_used": paths_used,
           "conf_source_dir": (re.search(r'^SourceDirectory\s*=\s*"(.*)"', conf, re.M) or [None, None])[1],
           "conf_data_dir": (re.search(r'^DataDir\s*=\s*"(.*)"', conf, re.M) or [None, None])[1]}
    require(not run["setup_windows"] and not run["firstlaunch_steps"], "the moved app ran first launch again", **ev)
    pp = play_ok(run)
    require(not pp, f"the play loop from the moved app didn't reach ready and stop cleanly: {pp}", **ev)
    require(len(paths_used) == 3 and all(_under(x, app2) for x in paths_used),
            "the servers didn't run from the new APP", **ev)
    require(ev["conf_source_dir"] == str(app2 / "sql") and _under(ev["conf_data_dir"], home),
            "the confs don't follow the moved app with the same HOME", **ev)
    require(h1 == h2, "something was written inside the app", **ev)
    # counterexample: without -B the payload's Python writes __pycache__ into the app, and the hash sees it
    subprocess.run([str(app2 / "python/bin/python3"), "-I", "-c", "import hslib.paths"], capture_output=True,
                   env=release_env(home), cwd=d, timeout=60)
    h3 = tree_hash(bundle2)
    ev["counterexample"] = {"hash_changed": h3 != h2, "entries_added": h3["entries"] - h2["entries"]}
    for x in sorted(app2.rglob("__pycache__"), reverse=True):        # keep the shared app clean for later checks
        shutil.rmtree(x, ignore_errors=True)
    require(h3 != h2, "counterexample: a write inside the app wasn't seen", **ev)
    return ev


# ================================================================================================ play_standin

def dump_distrib(f: Path) -> str | None:
    """The "Distrib x.y.z, for <os>" of a mysqldump .sql.gz (its first line)."""
    r = subprocess.run(["/usr/bin/gzip", "-dc", str(f)], capture_output=True, timeout=120)
    first = r.stdout.split(b"\n", 1)[0].decode("utf-8", "replace")
    m = re.search(r"Distrib (\S+), for (\S+)", first)
    return f"{m.group(1)} {m.group(2)}" if m else None


def payload_distrib(app: Path) -> str | None:
    out = subprocess.run([str(app / "mysql/bin/mysqldump"), "--version"], capture_output=True, text=True).stdout
    m = re.search(r"Ver (\S+) for (\S+)", out)
    return f"{m.group(1)}, {m.group(2)}".replace(",", "") if m else None


@check("release.play_standin", order=16, capability=CAP, stack="down", tier="slow")
def play_standin(ctx):
    """The run's shared set-up HOME, scratch ports, the no-Homebrew sandbox: the release app's play loop with the
    stand-in client reaches ready (servers from the payload), the stand-in exits, the stack stops gracefully, a backup
    appears in HOME's backups made by the payload's mysqldump (its dump header) and holding the PLAYER account the
    loop created in the scratch databases, and settings.json and player.json are in HOME. Counterexample: the same
    "made by the payload's mysqldump" test on a dump made by Homebrew's mysqldump (the dev player's newest backup,
    only read) fails."""
    from .firstlaunch_checks import app_play, fixture, play_ok
    healed = heal_scratch()
    fx = fixture(ctx)
    ev: dict = {"healed": healed, "started": time.time(), "fixture": fx}
    d, home, bundle = Path(fx["d"]), Path(fx["home"]), Path(fx["bundle"])
    app = bundle / "Contents/Resources/Moonglade"
    bk = home / "backups"
    before = sorted(x.name for x in bk.iterdir()) if bk.is_dir() else []
    prof = d / "release.sb"
    prof.write_text(sandbox_profile(d))
    run = app_play(bundle, home, d, "standin", sandbox=prof)
    after = sorted(x.name for x in bk.iterdir()) if bk.is_dir() else []
    new = [x for x in after if x not in before and not x.startswith(".")]
    want = payload_distrib(app)
    got = {f.name: dump_distrib(f) for f in sorted((bk / new[-1]).glob("*.sql.gz"))} if new else {}
    pl = run["play_last"]
    acct = (pl.get("account") or {})
    in_dump = None
    if new and (bk / new[-1] / "auth.sql.gz").is_file():
        txt = subprocess.run(["/usr/bin/gzip", "-dc", str(bk / new[-1] / "auth.sql.gz")], capture_output=True,
                             timeout=120).stdout
        in_dump = f"'{acct.get('account')}'".encode() in txt
    ev |= {"app": {k: v for k, v in run.items() if k != "_events"}, "backups_new": new, "dump_distrib": got,
           "payload_mysqldump": want, "account": {k: acct.get(k) for k in ("kind", "account", "file", "file_created")},
           "account_in_auth_dump": in_dump,
           "home_files": {n: (home / n).is_file() for n in ("settings.json", "player.json")},
           "player_json_mode": oct((home / "player.json").stat().st_mode & 0o777) if (home / "player.json").exists()
           else None}
    pp = play_ok(run)
    require(not pp, f"the play loop didn't reach ready and stop cleanly: {pp}", **ev)
    require([x["exe"] for x in run["while_ready"]["mysqld"]] == [str(app / "mysql/bin/mysqld")],
            "the running mysqld isn't the payload's", **ev)
    require(new and (pl.get("backup") or {}).get("ok") is True, "no backup appeared in HOME's backups", **ev)
    require(got and want and all(v == want for v in got.values()), "the backup wasn't made by the payload's mysqldump",
            **ev)
    require(acct.get("kind") == "player" and _under(acct.get("file"), home) and in_dump,
            "the PLAYER account wasn't created in the scratch databases (with its file in HOME)", **ev)
    require(all(ev["home_files"].values()), "settings.json / player.json aren't in HOME", **ev)
    # counterexample: a Homebrew mysqldump dump fails the same test (read-only look at the dev player's newest backup)
    dev = sorted((P.PLAYER_BACKUPS).glob("*/auth.sql.gz"))
    if dev:
        other = dump_distrib(dev[-1])
        ev["counterexample_homebrew_dump"] = {"distrib": other, "passes": other == want}
        require(other != want, "counterexample: a Homebrew-made dump passed the payload test", **ev)
    else:
        ev["counterexample_homebrew_dump"] = "no dev backup to compare (nothing to read)"
    return ev


# ================================================================================================ client_smoke

SMOKE_SRC = r"""
import json, sys, time
from pathlib import Path
from hslib import paths as P, launch as L
client = Path(sys.argv[1])
L.select_client(client)
ev = {"client": str(L.client_dir()), "wine": str(L.WINE), "prefix_dir": str(P.WINEPREFIX)}
try:
    ev["prefix"] = L.ensure_prefix()
    t0 = time.time()
    proc = L.start_client()
    win = None
    while time.time() - t0 < 120:
        win = L.client_window()
        if win or (proc.poll() is not None and not L.prefix_processes()):
            break
        time.sleep(1)
    ev["window"] = win
    ev["window_after_s"] = round(time.time() - t0, 1)
    ev["wow"] = [{"pid": p["pid"], "cmd": p["cmd"][-80:]} for p in L.wow_processes()]
except Exception as e:
    ev["error"] = f"{type(e).__name__}: {e}"
ev["stop"] = L.stop_prefix()
ev["remaining"] = L.prefix_processes()
print(json.dumps(ev, default=str))
"""


def _smoke(app: Path, home: Path, client: Path, d: Path) -> dict:
    env = release_env(home, PATH="/usr/bin:/bin:/usr/sbin:/sbin", TMPDIR=str(d / "tmp") + "/")
    (d / "tmp").mkdir(exist_ok=True)
    r = subprocess.run([str(app / "python/bin/python3"), "-I", "-B", "-c", SMOKE_SRC, str(client),
                        P.RELEASE_TEST_FLAG], capture_output=True, text=True, env=env, cwd=d, timeout=600)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {"rc": r.returncode, "stdout": r.stdout[-500:], "stderr": r.stderr[-800:]}
    return out


@check("release.client_smoke", order=16, capability=CAP, stack="down", tier="slow", gui=True)
def client_smoke(ctx):
    """The run's shared set-up HOME (first launch from client/): HOME's play client Wow.exe started under HOME's
    Wine runtime and prefix (the harness's client launch: launch.start_client, release mode) shows its first window,
    then the prefix is stopped with no Wine or Wow.exe process left. Counterexample: a scratch "client" folder with
    no Wow.exe shows no window, which the check sees."""
    from .firstlaunch_checks import fixture
    healed = heal_scratch()
    fx = fixture(ctx)
    ev: dict = {"healed": healed, "started": time.time(), "fixture": fx}
    d, home, bundle = Path(fx["d"]), Path(fx["home"]), Path(fx["bundle"])
    app = bundle / "Contents/Resources/Moonglade"
    play_client = home / "client-play"
    out = _smoke(app, home, play_client, d)
    ev["smoke"] = out
    left = _proc_info(lambda a: any(str(home) in x for x in a))
    ev["remaining_after"] = [{"pid": x["pid"], "argv": x["argv"][:2]} for x in left]
    require(out.get("client") == str(play_client) and _under(out.get("wine"), home)
            and out.get("prefix_dir") == str(home / "wine/prefix"),
            "the smoke didn't use HOME's play client, Wine runtime and prefix", **ev)
    require(out.get("window") and out.get("wow"), "Wow.exe showed no window", **ev)
    require(not out.get("remaining") and not left, "Wine / Wow.exe processes remain", **ev)
    # counterexample: no Wow.exe -> no window
    empty = d / "no-client"
    empty.mkdir(exist_ok=True)
    cx = _smoke(app, home, empty, d)
    ev["counterexample_no_wow"] = {k: cx.get(k) for k in ("window", "error", "remaining", "window_after_s")}
    shutil.rmtree(empty, ignore_errors=True)
    require(not cx.get("window"), "counterexample: a folder without Wow.exe showed a window", **ev)
    return ev


# ================================================================================================ player_data_untouched

# The live databases' fingerprint (scenario "Player fingerprints unchanged"): CHECKSUM TABLE of every table of
# acore_auth and acore_characters when lane A's mysqld is up (read-only), else their datadir files (with the shared
# InnoDB files that hold changes not yet written into a table's own file). Never a write, never a mysqld on the live
# datadir. Lane A's world/auth servers change these tables by themselves, so a fingerprint taken while they run is
# an error (fail-closed), not a value.
FP_SCHEMAS = ("acore_auth", "acore_characters")
FP_SHARED = ("ibdata1", "undo_*", "#innodb_redo/*")


def db_checksums(mysql: Path, conn: list[str], schemas=FP_SCHEMAS) -> dict:
    """{"tables": {schema.table: checksum}} by CHECKSUM TABLE over every base table of `schemas`, or {"error"}."""
    def q(sql):
        r = subprocess.run([str(mysql), "--no-defaults", *conn, "--batch", "--skip-column-names", "-e", sql],
                           capture_output=True, text=True, timeout=600)
        return r.returncode, r.stdout, r.stderr.strip()[-300:]
    rc, out, err = q("SELECT CONCAT('`', table_schema, '`.`', table_name, '`') FROM information_schema.tables WHERE "
                     f"table_schema IN ({', '.join(repr(x) for x in schemas)}) AND table_type = 'BASE TABLE' "
                     "ORDER BY table_schema, table_name")
    names = [x for x in out.splitlines() if x.strip()]
    if rc != 0 or not names:
        return {"error": f"listing the tables failed: {err or 'no tables'}"}
    rc, out, err = q("CHECKSUM TABLE " + ", ".join(names))
    rows = dict(x.split("\t", 1) for x in out.splitlines() if "\t" in x)
    if rc != 0 or len(rows) != len(names) or any(v in ("NULL", "") for v in rows.values()):
        return {"error": f"CHECKSUM TABLE failed: {err or 'missing or NULL rows'}"}
    return {"tables": rows}


def datadir_files(datadir: Path, schemas=FP_SCHEMAS) -> dict:
    """{relative path: [size, mtime_ns, sha256]} of the schemas' folders and the shared InnoDB files."""
    from ..backups import sha256_file
    files = [f for sc in schemas for f in sorted((datadir / sc).rglob("*")) if f.is_file()]
    files += [f for pat in FP_SHARED for f in sorted(datadir.glob(pat)) if f.is_file()]
    out = {}
    for f in files:
        st = f.stat()
        out[str(f.relative_to(datadir))] = [st.st_size, st.st_mtime_ns, sha256_file(f)]
    return out


def live_db_fingerprint() -> dict:
    """Lane A's acore_auth and acore_characters (the player's save): CHECKSUM TABLE through lane A's mysqld when it is
    up, else the datadir files. Fail-closed: anything it can't read is an "error"."""
    import hashlib
    from .. import stack
    L = P.layout("a")
    st = stack.stack_processes("a")
    if st["worldserver"] or st["authserver"]:
        return {"error": f"lane A's servers are running ({st}); their tables change by themselves"}
    try:
        if st["mysqld"] or stack.port_open(L["ports"]["mysql"]):
            got = db_checksums(P.MYSQL, ["--protocol=TCP", "-h127.0.0.1", f"-P{L['ports']['mysql']}",
                                         f"-u{P.DB_USER}", f"-p{P.DB_PASS}"])
            if "error" in got:
                return got | {"via": "CHECKSUM TABLE (lane A's mysqld)"}
            return {"via": "CHECKSUM TABLE (lane A's mysqld)", "tables": len(got["tables"]),
                    "sha256": hashlib.sha256(json.dumps(got["tables"], sort_keys=True).encode()).hexdigest(),
                    "values": got["tables"]}
        datadir = Path(L["mysql_data"])
        if not all((datadir / sc).is_dir() for sc in FP_SCHEMAS):
            return {"via": "datadir files", "error": f"no {FP_SCHEMAS} under {datadir}"}
        files = datadir_files(datadir)
        return {"via": "datadir files (lane A's mysqld is down)", "files": len(files),
                "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(), "values": files}
    except (OSError, subprocess.SubprocessError) as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _player_name(player_json: Path) -> str | None:
    try:
        return str(json.loads(player_json.read_text())["account"]).upper()
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _file_fp(f: Path):
    import hashlib
    if not f.exists():
        return None
    return {"sha256": hashlib.sha256(f.read_bytes()).hexdigest(), "mtime_ns": f.stat().st_mtime_ns}


def player_data_fingerprint(roots: dict | None = None, db=None) -> dict:
    """The dev player's data (scenario "Player fingerprints unchanged"): env/backups, env/player.json,
    env/settings.json, env/client-play (clientbuild.fingerprint), the client/ manifest, the live acore_auth and
    acore_characters (live_db_fingerprint: CHECKSUM TABLE, or the datadir files when lane A's mysqld is down) and the
    real ~/Library/Application Support/Moonglade (existence + listing). `roots` overrides the file locations and `db`
    the database fingerprint function (the counterexample's scratch copies)."""
    from .. import backups, clientbuild
    r = roots or {"backups": P.PLAYER_BACKUPS, "player_json": P.PLAYER_FILE, "settings_json": P.SETTINGS_FILE,
                  "client_play": P.PLAYER_PLAY_CLIENT, "client_manifest": P.CLIENT_MANIFEST,
                  "real_home": P.RELEASE_HOME_DEFAULT}
    real = Path(r["real_home"])
    out = {"at": time.time(), "backups": backups.fingerprint(Path(r["backups"])),
           "player_json": _file_fp(Path(r["player_json"])), "settings_json": _file_fp(Path(r["settings_json"])),
           "client_play": clientbuild.fingerprint(Path(r["client_play"])) if Path(r["client_play"]).exists() else None,
           "client_manifest": _file_fp(Path(r["client_manifest"])),
           "real_home": ({"exists": True, "listing": sorted(x.name for x in real.iterdir()),
                          "mtime_ns": real.stat().st_mtime_ns} if real.exists() else {"exists": False})}
    out["db"] = (db or live_db_fingerprint)()
    return out


def fp_differences(a: dict, b: dict) -> list[str]:
    out = []
    for k in sorted((set(a) | set(b)) - {"at"}):
        if k == "db":
            da, db_ = a.get(k) or {}, b.get(k) or {}
            if "error" in da or "error" in db_:
                out.append(f"db: can't compare ({da.get('error') or db_.get('error')})")
            elif da.get("via") != db_.get("via"):
                out.append(f"db: read differently at the two ends ({da.get('via')} / {db_.get('via')}): lane A's "
                           "mysqld was started or stopped in between")
            elif da.get("sha256") != db_.get("sha256"):
                va, vb = da.get("values") or {}, db_.get("values") or {}
                out.append("db: changed: " + ", ".join(sorted(x for x in set(va) | set(vb) if va.get(x) != vb.get(x))[:8]))
        elif a.get(k) != b.get(k):
            out.append(k)
    return out


def _slim_fp(fp) -> dict:
    if not isinstance(fp, dict):
        return fp
    return {k: ({kk: vv for kk, vv in v.items() if kk != "values"} if k == "db" and isinstance(v, dict) else
                (bool(v) if k in ("backups", "client_play") else v)) for k, v in fp.items()}


BRACKET_PREFIXES = ("release.", "firstlaunch.")
PDU_ID = "release.player_data_untouched"


def _load_state(path) -> tuple[dict | None, str | None]:
    try:
        return json.loads(Path(path).read_text()), None
    except (OSError, ValueError, TypeError) as e:
        return None, f"{type(e).__name__}: {e}"


def _ok_fp(fp) -> bool:
    return isinstance(fp, dict) and "error" not in fp and isinstance(fp.get("db"), dict) and "error" not in fp["db"]


def bracket_verdict(plan: dict | None, state: dict | None, xlog: list, lane_b: dict | None = None,
                    run_id: str | None = None) -> tuple[list[str], dict]:
    """Whether the run's bracket is complete and clean, from the run's plan, its persisted state and the actual
    execution log of lane A (every execute call of the run). Returns (problems, info); info["mode"] is "bracketed"
    when any release.*/firstlaunch.* check ran or was planned in the run, else "none_ran". Fail-closed: anything
    missing is a problem naming what is missing."""
    execd = [e for e in xlog if "id" in e]
    rel_ran = [e["id"] for e in execd if e["id"].startswith(BRACKET_PREFIXES) and e["id"] != PDU_ID]
    info: dict = {"release_ran": rel_ran, "planned": (plan or {}).get("inner")}
    probs: list[str] = []
    if not plan:
        return ["the runner planned no bracket for this run (no release_bracket_plan in the run's state)"], info
    if state is None:
        return ["the run's bracket state file is missing or unreadable"], info
    if run_id is not None and state.get("run_id") != run_id:
        probs.append(f"the bracket state belongs to run {state.get('run_id')}, not this run ({run_id})")
    if state.get("save_error"):
        probs.append(f"persisting the bracket failed: {state['save_error']}")
    marks = {e["bracket"]: i for i, e in enumerate(xlog) if "bracket" in e}
    info["markers"] = {k: {"log_index": i, "at": xlog[i].get("at"), "check": xlog[i].get("check")} for k, i in marks.items()}
    if not rel_ran and not plan.get("inner"):
        info["mode"] = "none_ran"
        if "start" not in marks or not _ok_fp(state.get("start")):
            probs.append("no release.*/firstlaunch.* check ran, and the start fingerprint (before this check) is "
                         "missing or erroneous")
        return probs, info
    info["mode"] = "bracketed"
    missing = [w for w in ("start", "end") if w not in marks or not _ok_fp(state.get(w))]
    if missing:
        why = {w: (state[w].get("error") or (state[w].get("db") or {}).get("error") or "incomplete record")
                  if isinstance(state.get(w), dict) else "not taken" for w in missing}
        probs.append(f"release.*/firstlaunch.* checks ran in this run ({rel_ran}) but the bracket's "
                     f"{' and '.join(missing)} fingerprint{'s are' if len(missing) > 1 else ' is'} missing or "
                     f"erroneous ({why})")
        return probs, info
    i0, i1 = marks["start"], marks["end"]
    if not i0 < i1:
        return probs + [f"the end fingerprint (log {i1}) isn't after the start (log {i0})"], info
    inside = [e["id"] for e in xlog[i0 + 1:i1] if "id" in e]
    info["inside"] = inside
    outside_rel = [i for i in rel_ran if i not in inside]
    if outside_rel:
        probs.append(f"release.*/firstlaunch.* checks ran outside the bracket: {outside_rel}")
    foreign = [i for i in inside if not i.startswith(BRACKET_PREFIXES) or i == PDU_ID]
    if foreign:
        probs.append(f"checks other than release.*/firstlaunch.* ran inside the bracket: {foreign}")
    not_run = [i for i in plan.get("inner") or [] if i not in rel_ran]
    if not_run:
        probs.append(f"planned release.*/firstlaunch.* checks didn't run, so the bracket isn't the run's: {not_run}")
    if list(state.get("ran") or []) != inside:
        probs.append(f"the persisted bracket ({state.get('ran')}) differs from the run's execution log ({inside})")
    if lane_b and lane_b.get("started") is not None:
        t0, t1 = xlog[i0]["at"], xlog[i1]["at"]
        b0, b1 = lane_b["started"], lane_b.get("finished") or float("inf")
        info["lane_b"] = {"started": b0, "finished": lane_b.get("finished"), "overlaps_bracket": b0 < t1 and b1 > t0}
        if b0 < t1 and b1 > t0:
            probs.append(f"lane B ran during the bracket ({b0:.0f}-{b1:.0f} vs {t0:.0f}-{t1:.0f})")
    return probs, info


@check(PDU_ID, order=998, capability=CAP, stack="down", tier="full")
def player_data_untouched(ctx):
    """Bracketing every release.* and firstlaunch.* check of the WHOLE run (all lane-A segments: the runner plans
    the bracket from the run's selection, takes the start fingerprint right before the first of them, after
    bringing lane A down, and the end right after the last, and persists both under the run's output dir keyed by
    the run id; the run's execution log must show that no other check ran in between and lane B didn't run then):
    env/backups, env/player.json, env/settings.json, env/client-play, the client/ manifest, CHECKSUM TABLE of the
    live acore_auth / acore_characters (their datadir files while lane A's mysqld is down) and the real
    ~/Library/Application Support/Moonglade are unchanged. Fail-closed: if any release.*/firstlaunch.* check ran
    and the start or end fingerprint is missing, it fails naming which. Counterexample: on an APFS clone of the live
    datadir (a throwaway mysqld, never the live databases), a write to a row that isn't the PLAYER's changes both
    the CHECKSUM TABLE and the datadir-file fingerprint, while the PLAYER's own rows are unchanged."""
    ctx = ctx or {}
    plan = ctx.get("release_bracket_plan")
    state, err = _load_state(plan["state_file"]) if plan else (None, None)
    probs, info = bracket_verdict(plan, state, ctx.get("exec_log") or [], ctx.get("lane_b_window"), ctx.get("run_id"))
    from ..registry import REGISTRY
    from ..runner import bracket_intruders
    intruders = bracket_intruders(REGISTRY.values())      # the whole registry, whatever this run selected
    info["registry_intruders"] = intruders
    if intruders:
        probs.append(f"checks registered inside the bracket's (order, id) range would run inside it in a full run: "
                     f"{intruders}")
    st = state or {}
    ev: dict = {"bracket": info | {"state_file": (plan or {}).get("state_file"), "state_error": err,
                                   "start_check": st.get("start_check"), "end_check": st.get("end_check")},
                "start": _slim_fp(st.get("start", "missing"))}
    require(not probs, "; ".join(probs), **ev)
    start = st["start"]
    if info["mode"] == "none_ran":                 # no release/firstlaunch check in the whole run: compare with now
        end = json.loads(json.dumps(player_data_fingerprint(), default=str))   # same JSON round trip as the start
        ev["end_taken"] = "now (no release.*/firstlaunch.* check ran in this run)"
    else:
        end = st["end"]
    ev["end"] = _slim_fp(end)
    require("error" not in start["db"] and "error" not in end.get("db", {}),
            "the live databases couldn't be fingerprinted", **ev)
    diff = fp_differences(start, end)
    ev["differences"] = diff
    require(not diff, f"the dev player's data changed: {diff}", **ev)
    ev["counterexample"] = _pdu_counterexample()
    ev["counterexample"]["bracket"] = _bracket_counterexamples()
    return ev


def _bracket_counterexamples() -> dict:
    """bracket_verdict on two-lane-shaped synthetic runs (segment 1 runs the release checks, lane B starts after it,
    the check runs in a later segment): complete bracket -> no problem; end missing -> problem naming "end"; start
    missing -> "start"; a dev check inside -> problem; lane B overlapping -> problem; a release check that ran but
    the plan has none (state empty) -> problem, never "none ran"."""
    ok_fp = {"db": {"sha256": "x"}}
    plan = {"run_id": "R", "inner": ["firstlaunch.preflight", "release.pins"], "start_check": "firstlaunch.preflight",
            "end_check": "release.pins"}
    log_full = [{"id": "build.clean"}, {"bracket": "start", "at": 10.0, "check": "firstlaunch.preflight"},
                {"id": "firstlaunch.preflight"}, {"id": "release.pins"}, {"bracket": "end", "at": 20.0, "check": "release.pins"},
                {"id": "lane.x"}, {"id": PDU_ID}]
    state = {"run_id": "R", "start": ok_fp, "end": ok_fp, "ran": ["firstlaunch.preflight", "release.pins"]}
    out = {}
    out["complete"] = bracket_verdict(plan, state, log_full, {"started": 25.0, "finished": 90.0}, "R")[0]
    out["end_missing"] = bracket_verdict(plan, {k: v for k, v in state.items() if k != "end"},
                                         [e for e in log_full if e.get("bracket") != "end"], {"started": 25.0}, "R")[0]
    out["start_missing"] = bracket_verdict(plan, {k: v for k, v in state.items() if k != "start"},
                                           [e for e in log_full if e.get("bracket") != "start"], None, "R")[0]
    log_dev = log_full[:3] + [{"id": "lane.x"}] + log_full[3:]
    out["dev_inside"] = bracket_verdict(plan, state | {"ran": ["firstlaunch.preflight", "lane.x", "release.pins"]},
                                        log_dev, None, "R")[0]
    out["lane_b_overlap"] = bracket_verdict(plan, state, log_full, {"started": 15.0, "finished": 90.0}, "R")[0]
    out["no_plan_but_ran"] = bracket_verdict({"run_id": "R", "inner": []}, {"run_id": "R"}, log_full[:1] + log_full[2:4] + [log_full[-1]], None, "R")[0]
    # the registry guard (runner.bracket_intruders): a non-bracket check ordered between release checks is named
    from types import SimpleNamespace as NS
    from ..runner import bracket_intruders
    reg_ok = [NS(id="release.pins", order=13), NS(id="firstlaunch.preflight", order=15), NS(id="signing.x", order=18),
              NS(id="build.pin_enforced", order=10), NS(id=PDU_ID, order=998)]
    out["registry_clean"] = bracket_intruders(reg_ok)
    out["registry_intruder"] = bracket_intruders(reg_ok + [NS(id="signing.y", order=13)])
    out["registry_pdu_inside"] = bracket_intruders(reg_ok + [NS(id="release.zz", order=999)])
    require(out["registry_clean"] == [] and out["registry_intruder"] == ["signing.y"]
            and out["registry_pdu_inside"] == ["signing.x", PDU_ID],
            "bracket counterexample: the registry guard doesn't name exactly the checks ordered inside", **out)
    require(out["complete"] == [], "bracket counterexample: a complete bracket was refused", **out)
    require(any("end fingerprint is missing" in p for p in out["end_missing"]), "bracket counterexample: a missing end "
            "wasn't caught", **out)
    require(any("start fingerprint is missing" in p for p in out["start_missing"]), "bracket counterexample: a missing "
            "start wasn't caught", **out)
    require(any("lane.x" in p for p in out["dev_inside"]), "bracket counterexample: a dev check inside wasn't caught", **out)
    require(any("lane B ran during" in p for p in out["lane_b_overlap"]), "bracket counterexample: lane B overlap "
            "wasn't caught", **out)
    require(out["no_plan_but_ran"] and not any("none" in p for p in out["no_plan_but_ran"]),
            "bracket counterexample: release checks that ran without a bracket weren't caught", **out)
    return out


def _pdu_counterexample() -> dict:
    """On a clone of lane A's datadir (cp -c; lane A's mysqld is down: the check's stack is "down"), with a throwaway
    socket-only mysqld: the fingerprint before and after an UPDATE of one AI player's character (not the PLAYER's)
    differs by CHECKSUM TABLE (characters) and, after a clean shutdown, by the datadir files (characters.ibd); the
    PLAYER's account and characters rows (the old, weaker fingerprint) are the same. Plus: a changed player.json in
    a scratch copy of the player files is seen."""
    from .. import stack
    L = P.layout("a")
    ev: dict = {}
    st = stack.stack_processes("a")
    require(not any(st.values()), "counterexample: lane A isn't down, so its datadir can't be cloned", lane_a=st)
    d = scratch_dir("pdu")
    proc = None
    try:
        # one changed byte of player.json in a scratch copy of the player files is seen
        (d / "backups").mkdir()
        (d / "backups" / "x").write_text("b")
        pj = d / "player.json"
        pj.write_text(json.dumps({"account": "NOSUCHPLAYER", "password": "x"}))
        roots = {"backups": d / "backups", "player_json": pj, "settings_json": d / "settings.json",
                 "client_play": d / "client-play", "client_manifest": d / "m.json", "real_home": d / "real"}
        a = player_data_fingerprint(roots, db=lambda: {"via": "none", "sha256": "-"})
        pj.write_text(json.dumps({"account": "NOSUCHPLAYER", "password": "y"}))
        b = player_data_fingerprint(roots, db=lambda: {"via": "none", "sha256": "-"})
        ev["player_json"] = fp_differences(a, b)
        require("player_json" in ev["player_json"], "counterexample: a changed player.json wasn't seen", **ev)
        # the databases: a scratch clone of the live datadir
        t = Path(tempfile.mkdtemp(prefix="pdu-", dir="/tmp"))          # short socket path
        data = t / "data"
        subprocess.run(["/bin/cp", "-cR", str(L["mysql_data"]), str(data)], check=True, capture_output=True)
        for f in list(data.glob("*.pid")) + list(data.glob("*.sock*")):
            f.unlink()
        files0 = datadir_files(data)
        sock = t / "s"
        (t / "my.cnf").write_text(f"[mysqld]\nbasedir={P.MYSQL_BASE}\ndatadir={data}\nsocket={sock}\n"
                                  f"pid-file={t / 'm.pid'}\nlog-error={t / 'err.log'}\nskip-networking\nmysqlx=OFF\n"
                                  "performance_schema=OFF\ndisable-log-bin\ninnodb_buffer_pool_size=128M\n")
        proc = subprocess.Popen([str(P.MYSQLD), f"--defaults-file={t / 'my.cnf'}"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        conn = [f"--socket={sock}", "-uroot"]

        def q(sql):
            r = subprocess.run([str(P.MYSQL), "--no-defaults", *conn, "--batch", "--skip-column-names", "-e", sql],
                               capture_output=True, text=True, timeout=120)
            return r.returncode, (r.stdout if r.returncode == 0 else r.stderr).strip()
        t0 = time.time()
        while time.time() - t0 < 180 and proc.poll() is None:
            if sock.exists():
                rc, out = q("SELECT 1")
                if rc == 0:
                    break
                if "Access denied" in out:
                    conn = [f"--socket={sock}", f"-u{P.DB_USER}", f"-p{P.DB_PASS}"]
            time.sleep(1)
        player = _player_name(P.PLAYER_FILE) or "NOSUCHPLAYER"
        require(player.replace("_", "").isalnum(), "counterexample: unexpected PLAYER account name")
        player_sql = (f"SELECT id, username, HEX(salt), HEX(verifier), last_login FROM acore_auth.account WHERE "
                      f"username = '{player}'; SELECT * FROM acore_characters.characters WHERE account = (SELECT id "
                      f"FROM acore_auth.account WHERE username = '{player}') ORDER BY guid")
        rc_p0, p0 = q(player_sql)
        k0 = db_checksums(P.MYSQL, conn)
        rc, target = q("SELECT c.guid FROM acore_characters.characters c JOIN acore_auth.account a ON a.id = c.account "
                       f"WHERE a.username <> '{player}' ORDER BY c.guid LIMIT 1")
        require(rc == 0 and target.isdigit(), "counterexample: no non-PLAYER character in the clone", **ev, out=target)
        rc, out = q(f"UPDATE acore_characters.characters SET money = money + 1 WHERE guid = {target}")
        k1 = db_checksums(P.MYSQL, conn)
        rc_p1, p1 = q(player_sql)
        subprocess.run([str(P.MYSQLADMIN), "--no-defaults", *conn, "shutdown"], capture_output=True, timeout=120)
        proc.wait(timeout=120)
        files1 = datadir_files(data)
        kd = sorted(x for x in set(k0.get("tables", {})) | set(k1.get("tables", {}))
                    if k0.get("tables", {}).get(x) != k1.get("tables", {}).get(x))
        fd = sorted(x for x in set(files0) | set(files1) if files0.get(x) != files1.get(x))
        ev["database"] = {"updated_guid": int(target), "update_rc": rc, "checksum_tables": len(k0.get("tables", {})),
                          "checksum_changed": kd, "files_changed": fd[:10],
                          "player_rows_same": rc_p0 == 0 and rc_p1 == 0 and p0 == p1}
        require(rc == 0 and "error" not in k0 and "error" not in k1 and kd == ["acore_characters.characters"]
                and "acore_characters/characters.ibd" in fd and ev["database"]["player_rows_same"],
                "counterexample: a write to a non-PLAYER row wasn't seen by CHECKSUM TABLE and the datadir files "
                "(or the PLAYER's rows changed)", **ev)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        if "t" in locals():
            shutil.rmtree(t, ignore_errors=True)
        shutil.rmtree(d, ignore_errors=True)
    return ev


# ================================================================================================ readline_stub (5.5)

STUB_DIR = P.ROOT / "tools" / "release" / "readline-stub"
STUB_PRE_FIX = "52238f5"            # the last commit whose stub read lines through stdio (task 5.5 counterexample)
STUB_TEST_MAIN = r'''
#include "readline/readline.h"
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
static int hook(void) { return 0; }
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }
int main(void) {
    rl_event_hook = hook;
    double t0 = now();
    for (int i = 0; i < 4; i++) {
        char *l = readline("");
        if (!l) { printf("EOF %.3f\n", now() - t0); fflush(stdout); return 0; }
        printf("LINE %s %.3f\n", l, now() - t0); fflush(stdout); free(l);
    }
    return 0;
}
'''


def _stub_program(src: str, out: Path) -> Path:
    """`src` (readline.c text) + the test main, compiled against the stub's header into `out`."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "readline.c").write_text(src)
    (out / "main.c").write_text(STUB_TEST_MAIN)
    sdk = subprocess.run(["/usr/bin/xcrun", "--show-sdk-path"], capture_output=True, text=True).stdout.strip()
    exe = out / "t"
    r = subprocess.run(["/usr/bin/clang", "-isysroot", sdk, "-O2", "-Wall", "-Wextra", "-Werror", "-I",
                        str(STUB_DIR / "include"), str(out / "readline.c"), str(out / "main.c"), "-o", str(exe)],
                       capture_output=True, text=True, env=os.environ | {"TMPDIR": str(out) + "/"})
    if r.returncode != 0:
        raise RuntimeError(f"compiling the stub test failed: {(r.stderr or r.stdout)[-600:]}")
    return exe


def _stub_piped(exe: Path, hold_s: float = 2.0) -> dict:
    """Two lines in ONE write, the pipe held open `hold_s`, then "third" without a newline and EOF. When each line
    came back (seconds after the write) and whether it came before the pipe was closed."""
    import select
    proc = subprocess.Popen([str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    t0 = time.time()
    os.write(proc.stdin.fileno(), b"first\nsecond\n")
    got, buf, closed_at = [], b"", None
    fd = proc.stdout.fileno()
    while time.time() - t0 < 10:
        if closed_at is None and time.time() - t0 >= hold_s:
            os.write(proc.stdin.fileno(), b"third")
            proc.stdin.close()
            closed_at = round(time.time() - t0, 3)
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            b = os.read(fd, 4096)
            if not b:
                break
            buf += b
            *lines, buf = buf.split(b"\n")
            for x in lines:
                got.append({"line": x.decode(), "at": round(time.time() - t0, 3)})
    proc.kill()
    proc.wait()
    return {"lines": got, "closed_at": closed_at}


def _stub_ok(res: dict) -> bool:
    ls = res["lines"]
    names = [x["line"].split(" ")[1] if x["line"].startswith("LINE ") else x["line"].split(" ")[0] for x in ls]
    return (names == ["first", "second", "third", "EOF"] and res["closed_at"] is not None
            and all(x["at"] < 1.0 and x["at"] < res["closed_at"] for x in ls[:2]))


@check("release.readline_stub", order=14, capability=CAP, stack="any", tier="quick")
def readline_stub(ctx):
    """tools/release/readline-stub (the release servers' console input, task 5.5): two lines arriving in one read
    both come back at once (well under 1 s, while the pipe is still open), a last line without a newline comes back
    at EOF, then EOF. Counterexample: the stub as of 52238f5 (lines read through stdio) leaves the second line waiting
    until the pipe closes."""
    ev: dict = {"started": time.time()}
    d = scratch_dir("rl")
    try:
        now = _stub_piped(_stub_program((STUB_DIR / "readline.c").read_text(), d / "now"))
        ev["now"] = now
        require(_stub_ok(now), "the stub didn't return both piped lines at once, then the last line and EOF", **ev)
        old = subprocess.run(["git", "-C", str(P.ROOT), "show", f"{STUB_PRE_FIX}:tools/release/readline-stub/readline.c"],
                             capture_output=True, text=True)
        require(old.returncode == 0, "couldn't read the pre-fix stub from git", **ev, git=old.stderr[-300:])
        before = _stub_piped(_stub_program(old.stdout, d / "old"))
        ev["counterexample_pre_fix"] = before
        require(not _stub_ok(before), "counterexample: the pre-fix stub passed the piped two-line test", **ev)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ev
