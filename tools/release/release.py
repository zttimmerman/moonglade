#!/usr/bin/env python3
"""Prebuilt-release packaging (openspec/changes/release-build). Driven by tools/release/package.sh.

    release.py [options] STEP ...      STEP: clean fetch build payload sign assets notarize | all (= every step)

Signing (openspec/changes/release-signing): `sign` signs the app in the mode set by tools/release/signing.json
(ad-hoc by default; developer-id only in the release workflow) and `notarize` notarizes and staples it (developer-id;
"not applicable" in ad-hoc). See tools/release/sign.py.

Everything is written under env/release/ (work dir; --work, --cache and --out must stay inside it):
    env/release/cache/     pinned downloads (fork.lock release_*; harness/hslib/downloads.py verifies every byte)
    env/release/inputs/    unpacked inputs, one folder per pinned sha256
    env/release/build/     the portable server build (cmake tree build/cmake, install build/install, readline stub)
    env/release/payload/Moonglade.app/   the release app (what the zip holds; release-first-launch design 2):
        Contents/MacOS/Moonglade          the prebuilt launcher (a copy of the payload's bin/Moonglade)
        Contents/Info.plist               bundle id io.github.zttimmerman.moonglade (release-signing)
        Contents/Resources/AppIcon.icns   branding/AppIcon.icns
        Contents/Resources/Moonglade/     the payload (below); every Mach-O in it signed on its own, inside out
        Contents/_CodeSignature/          the bundle signature (sign step)
    env/release/build/oracle.json         Oracle MySQL files' signatures (cdhashes) as recorded at payload
    env/release/build/sign.json           the sign step's record (+ bundle-files.json: every file and its sha256)
    env/release/out/       the release assets: the zip, the source tarballs, SOURCES.json, SHA256SUMS

A zip built anywhere but CI ($GITHUB_ACTIONS) carries this Mac's repo path in the server binaries (AzerothCore's
compiled-in revision.h defaults), so it is labelled "local test build, not for publishing" in VERSION ("build") and in
its file name; only CI-built zips are published.

It never writes build/, server/, data/, client/, env/client-play, env/player.json, env/settings.json, env/backups or the
live databases, never installs Homebrew packages, and reads vendor/ without changing it (--sync-vendor, for CI, runs
the harness's normal vendor sync first). Heavy steps run behind the harness's battery guard (hslib.power) with the
default job count unless --jobs is given.

The payload:
    VERSION, LICENSE, NOTICE, README.md, fork.lock
    server/bin/{authserver,worldserver}   rpath @executable_path/../lib; built with readline replaced by a stub
    server/lib/                           bundled dylibs (delocate.py), incl. Oracle's libmysqlclient
    server/etc/                           *.conf.dist (+ modules/)
    mysql/                                Oracle MySQL: bin/{mysqld,mysql,mysqldump,mysqladmin}, lib/ dylibs, share/{english,charsets}
    python/                               python-build-standalone, pruned; site-packages/moonglade-harness.pth -> harness/
    harness/ play bootstrap patches/ client-mods/ branding/   the repo's tracked files
    sql/data/sql/{base,create,custom,updates} + sql/modules/<module>/data/sql   the DB updater's SourceDirectory
    bin/Moonglade, bin/hsdock.dylib       prebuilt launcher and Wine host shim (harness/app)
    THIRD_PARTY/<component>/              licence texts; THIRD_PARTY/INDEX.json maps every bundled library to one
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(HERE))

from hslib import build as B          # noqa: E402
from hslib import downloads as D      # noqa: E402
from hslib import paths as P          # noqa: E402
from hslib import power               # noqa: E402
import delocate                       # noqa: E402
import sign as SG                     # noqa: E402

RELEASE_DIR = ROOT / "env" / "release"
DEPLOYMENT_TARGET = "26.0"
PAYLOAD_NAME = "Moonglade"
APP_NAME = "Moonglade.app"
APP_PAYLOAD = Path("Contents") / "Resources" / PAYLOAD_NAME     # the payload's place inside the app
RELEASE_BUNDLE_ID = SG.BUNDLE_ID          # io.github.zttimmerman.moonglade; never the dev app's local.moonglade.app
LOCAL_LABEL = "local test build, not for publishing"
LOCAL_SUFFIX = "-local-test-build-not-for-publishing"
MYSQL_KEEP_BIN = ("mysqld", "mysql", "mysqldump", "mysqladmin")   # mysqladmin: stack.mysql_down (release mode)
MYSQL_KEEP_SHARE = ("english", "charsets")
# python-build-standalone parts a release never uses (tk/tcl GUI, tests, pip, headers, the shared libpython that only
# embedders need)
PY_PRUNE = ("include", "share", "lib/pkgconfig", "lib/libpython3.13.dylib", "lib/tcl9", "lib/tcl9.0", "lib/tk9.0",
            "lib/itcl4.3.8", "lib/thread3.0.6", "lib/libtcl9.0.dylib", "lib/libtcl9tk9.0.dylib",
            "lib/python3.13/test", "lib/python3.13/idlelib", "lib/python3.13/tkinter", "lib/python3.13/turtledemo",
            "lib/python3.13/ensurepip", "lib/python3.13/site-packages/pip",
            "lib/python3.13/config-3.13-darwin")
PY_PRUNE_GLOBS = ("lib/python3.13/lib-dynload/_tkinter*", "lib/python3.13/site-packages/pip-*", "bin/pip*",
                  "bin/idle*", "bin/pydoc*", "bin/python3*-config", "lib/python3.13/turtle.py",
                  # the only Python extension module (the rest are built in); the harness never uses dbm, and under
                  # the hardened runtime an ad-hoc process can't load it (library validation; release-signing 2.2)
                  "lib/python3.13/lib-dynload/_dbm*")
# repo paths (tracked files) the payload carries
REPO_PARTS = ("harness", "play", "bootstrap", "patches", "client-mods", "branding", "fork.lock", "LICENSE", "NOTICE",
              "README.md")
CORE_SQL = ("data/sql/base", "data/sql/create", "data/sql/custom", "data/sql/updates")
# licence texts by Homebrew formula when the keg has none (sha256 recorded in tools/release/licenses/SOURCES)
FALLBACK_LICENSES = {"boost": HERE / "licenses" / "BSL-1.0.txt"}
LICENSE_NAMES = ("LICENSE*", "LICENCE*", "COPYING*", "License*", "NOTICE*")
ALLOWLIST = HERE / "allowlist.json"


class ReleaseError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[release {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sh(cmd, *, cwd=None, env=None, check=True, capture=True) -> subprocess.CompletedProcess:
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, capture_output=capture, text=True)
    if check and r.returncode != 0:
        tail = ((r.stderr or "") + (r.stdout or ""))[-1500:] if capture else ""
        raise ReleaseError(f"{' '.join(map(str, cmd))} failed ({r.returncode}): {tail}")
    return r


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def inside(p: Path, parent: Path) -> bool:
    p, parent = p.resolve(), parent.resolve()
    return p == parent or parent in p.parents


class Ctx:
    def __init__(self, a):
        self.work = (a.work or RELEASE_DIR).resolve()
        self.cache = (a.cache or self.work / "cache").resolve()
        self.out = (a.out or self.work / "out").resolve()
        for name, p in (("--work", self.work), ("--cache", self.cache), ("--out", self.out)):
            if not inside(p, RELEASE_DIR):
                raise ReleaseError(f"{name} {p} is outside {RELEASE_DIR}; packaging only writes under env/release/")
        self.inputs = self.work / "inputs"
        self.build = self.work / "build"
        self.payload_parent = self.work / "payload"
        self.app = self.payload_parent / APP_NAME
        self.payload = self.app / APP_PAYLOAD
        self.ci = os.environ.get("GITHUB_ACTIONS") == "true"
        self.build_root = ROOT.resolve()          # the checkout this packaging run builds from (allowlist anchor)
        self.jobs, self.jobs_why = power.resolve_jobs(a.jobs)
        self.ignore_power = a.ignore_power
        self.sync_vendor = a.sync_vendor
        self.lock = P.fork_lock()
        self.version = a.version or default_version()
        self.guard = power.Guard("release packaging", "tools/release/package.sh", self.ignore_power, say=log)
        self.fetched: dict[str, Path] = {}
        self.signing_config = SG.config_path(a.signing_config)

    def record(self) -> dict:
        return {"work": str(self.work), "cache": str(self.cache), "out": str(self.out), "version": self.version,
                "build_root": str(self.build_root), "ci": self.ci, "signing_config": str(self.signing_config),
                "jobs": {"n": self.jobs, "why": self.jobs_why}}


def default_version() -> str:
    if os.environ.get("GITHUB_REF_TYPE") == "tag" and os.environ.get("GITHUB_REF_NAME"):
        return os.environ["GITHUB_REF_NAME"]
    sha = sh(["git", "-C", ROOT, "rev-parse", "--short=12", "HEAD"]).stdout.strip()
    dirty = sh(["git", "-C", ROOT, "status", "--porcelain", "--untracked-files=no"]).stdout.strip()
    return f"0.0.0-dev+{sha}" + (".dirty" if dirty else "")


# ------------------------------------------------------------------------------------------------------- clean

def step_clean(c: Ctx) -> dict:
    """Remove the previous payload and assets, so a failed run never leaves one that looks current."""
    gone = []
    for p in (c.payload_parent, c.out, c.work / "payload.tmp"):
        if p.exists():
            shutil.rmtree(p)
            gone.append(str(p))
    return {"removed": gone}


# ------------------------------------------------------------------------------------------------------- fetch

def step_fetch(c: Ctx) -> dict:
    """Every pinned input, from the cache or downloaded, verified against fork.lock (a mismatch fails, naming it)."""
    rec = {}
    for name in D.RELEASE_PINS:
        try:
            got = D.obtain(name, c.lock, cache=c.cache)
        except D.DownloadError as e:
            raise ReleaseError(f"pinned input {name} ({D.PINS[name][0]}) refused: {e}") from None
        c.fetched[name] = Path(got["path"])
        rec[name] = {"file": str(got["path"]), "from": got["from"], "sha256": got["sha256"]}
        log(f"{name}: {got['from']}, sha256 ok")
    return rec


def _fetched(c: Ctx, name: str) -> Path:
    if name not in c.fetched:
        step_fetch(c)
    return c.fetched[name]


def unpack(c: Ctx, name: str) -> Path:
    """The top folder of pin `name`'s archive, unpacked once into inputs/<name>-<sha12>/."""
    pin = D.pin(name, c.lock)
    dest = c.inputs / f"{name}-{pin['sha256'][:12]}"
    marker = dest / ".unpacked"
    if not marker.is_file():
        src = _fetched(c, name)
        if dest.exists():
            shutil.rmtree(dest)
        tmp = dest.with_name(dest.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        log(f"unpacking {src.name}")
        sh(["/usr/bin/tar", "-xf", src, "-C", tmp])
        tmp.rename(dest)
        marker.write_text(pin["sha256"] + "\n")
    tops = [p for p in dest.iterdir() if p.is_dir()]
    if len(tops) != 1:
        raise ReleaseError(f"{name}: expected one top folder in the archive, found {[p.name for p in tops]}")
    return tops[0]


# ------------------------------------------------------------------------------------------------------- build

def vendor_state(c: Ctx) -> list[str]:
    probs = B.patched_pin_state(P.CORE_SRC, c.lock["core_sha"], B.core_patches(), "modules/")
    probs += B.patched_pin_state(P.BOTS_SRC, c.lock["playerbots_sha"], B.playerbots_patches())
    stray = B.stray_module_entries()
    if stray:
        probs.append(f"stray entries in core modules/: {stray}")
    for name, target in B._expected_links().items():
        link = P.CORE_SRC / "modules" / name
        if not (link.is_symlink() and os.readlink(link) == str(target)):
            probs.append(f"core modules/{name} isn't the harness's link to {target}")
    return probs


def readline_stub(c: Ctx) -> dict:
    """tools/release/readline-stub compiled to a static library (release servers ship no GPL-3 readline)."""
    src = HERE / "readline-stub"
    out = c.build / "readline-stub"
    lib = out / "lib" / "libreadline.a"
    (out / "lib").mkdir(parents=True, exist_ok=True)
    if (out / "include").exists():
        shutil.rmtree(out / "include")
    shutil.copytree(src / "include", out / "include")
    sdk = sh(["/usr/bin/xcrun", "--show-sdk-path"]).stdout.strip()
    obj = out / "readline.o"
    sh(["/usr/bin/clang", "-isysroot", sdk, f"-mmacosx-version-min={DEPLOYMENT_TARGET}", "-O2", "-Wall", "-Wextra",
        "-Werror", "-I", out / "include", "-c", src / "readline.c", "-o", obj])
    lib.unlink(missing_ok=True)
    sh(["/usr/bin/ar", "rcs", lib, obj])
    return {"include": str(out / "include"), "library": str(lib)}


def cmake_args(c: Ctx, mysql: Path, rl: dict) -> list[str]:
    b = P.BREW / "opt"
    maps = [f"-ffile-prefix-map={ROOT}/=moonglade/", f"-ffile-prefix-map={P.BREW}/=homebrew/"]
    flags = " ".join(maps)
    return [
        f"-DCMAKE_INSTALL_PREFIX={c.build / 'install'}",
        "-DCONF_DIR=etc",                                   # relative: no absolute config dir compiled in
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_C_COMPILER=clang", "-DCMAKE_CXX_COMPILER=clang++",
        "-DCMAKE_C_COMPILER_LAUNCHER=ccache", "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
        f"-DCMAKE_OSX_DEPLOYMENT_TARGET={DEPLOYMENT_TARGET}",
        "-DCMAKE_INSTALL_RPATH=@executable_path/../lib",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        f"-DCMAKE_C_FLAGS={flags}", f"-DCMAKE_CXX_FLAGS={flags}",
        "-DAPPS_BUILD=all", "-DTOOLS_BUILD=none", "-DSCRIPTS=static", "-DMODULES=static", "-DWITH_WARNINGS=0",
        # Oracle's MySQL (headers, client library, mysql_config, mysql), never Homebrew's
        f"-DMYSQL_CONFIG={mysql / 'bin/mysql_config'}",
        f"-DMYSQL_ADD_INCLUDE_PATH={mysql / 'include'}",
        f"-DMYSQL_INCLUDE_DIR={mysql / 'include'}",
        f"-DMYSQL_LIBRARY={mysql / 'lib/libmysqlclient.24.dylib'}",
        f"-DMYSQL_EXECUTABLE={mysql / 'bin/mysql'}",
        f"-DREADLINE_INCLUDE_DIR={rl['include']}",
        f"-DREADLINE_LIBRARY={rl['library']}",
        f"-DOPENSSL_ROOT_DIR={b}/openssl@3",
        f"-DOPENSSL_INCLUDE_DIR={b}/openssl@3/include",
        f"-DOPENSSL_SSL_LIBRARIES={b}/openssl@3/lib/libssl.dylib",
        f"-DOPENSSL_CRYPTO_LIBRARIES={b}/openssl@3/lib/libcrypto.dylib",
        f"-DBoost_ROOT={b}/boost",
    ]


def step_build(c: Ctx) -> dict:
    if c.sync_vendor:
        log("syncing vendor/ to the pins (--sync-vendor)")
        B.sync_vendor()
    probs = vendor_state(c)
    if probs:
        raise ReleaseError("vendor/ isn't the pinned sources + patches/ (run `harness/hs build` once, or pass "
                           "--sync-vendor in CI): " + "; ".join(probs))
    mysql = unpack(c, "release-mysql")
    c.build.mkdir(parents=True, exist_ok=True)
    rl = readline_stub(c)
    tree, inst = c.build / "cmake", c.build / "install"
    foreign = B.foreign_build_tree(tree, P.CORE_SRC)
    if foreign:
        log(f"discarding a build tree made elsewhere ({foreign})")
        shutil.rmtree(tree)
    if inst.exists():
        shutil.rmtree(inst)
    steps = [
        ("the configure step (cmake)", ["cmake", "-S", str(P.CORE_SRC), "-B", str(tree), *cmake_args(c, mysql, rl)]),
        ("the compile step (cmake --build)", ["cmake", "--build", str(tree), "-j", str(c.jobs)]),
        ("the install step (cmake --install)", ["cmake", "--install", str(tree)]),
    ]
    blog = c.build / "build.log"
    env = os.environ | {"MACOSX_DEPLOYMENT_TARGET": DEPLOYMENT_TARGET}
    t0 = time.time()

    def execute(step, fh):
        return subprocess.run(step, stdout=fh, stderr=subprocess.STDOUT, env=env).returncode
    try:
        guard = B.run_guarded_steps(blog, steps, c.jobs, c.jobs_why, c.ignore_power, "the release server build",
                                    "tools/release/package.sh", execute=execute)
    except B.BuildError as e:
        raise ReleaseError(f"{e}") from None
    secs = round(time.time() - t0)
    # what the build really used
    cc = json.loads((tree / "compile_commands.json").read_text())
    bad_inc = sorted({x["file"] for x in cc if "/opt/mysql" in x["command"] or "Cellar/mysql" in x["command"]
                      or "/readline/include" in x["command"] and str(rl["include"]) not in x["command"]})
    if bad_inc:
        raise ReleaseError(f"compiled against Homebrew's MySQL or readline headers: {bad_inc[:5]}")
    deps = {}
    for n in ("authserver", "worldserver"):
        info = delocate.load_info(inst / "bin" / n)
        deps[n] = info
        bad = [d for d in info["deps"] if "mysql/lib" in d or "readline" in d]
        if bad:
            raise ReleaseError(f"{n} links {bad}; expected Oracle's @rpath/libmysqlclient and no readline dylib")
    return {"seconds": secs, "log": str(blog), "guard": guard, "mysql_input": str(mysql), "readline_stub": rl,
            "deps": deps}


# ----------------------------------------------------------------------------------------------------- payload

def tracked(paths: tuple[str, ...]) -> list[str]:
    r = sh(["git", "-C", ROOT, "ls-files", "-z", "--", *paths])
    return sorted(f for f in r.stdout.split("\0") if f)


def git_tree_extract(repo: Path, tree: str, paths: tuple[str, ...], dst: Path) -> int:
    """Extract `paths` of git tree `tree` into dst (git archive | tar), keeping their relative paths."""
    dst.mkdir(parents=True, exist_ok=True)
    a = subprocess.Popen(["git", "-C", str(repo), "archive", "--format=tar", tree, "--", *paths],
                         stdout=subprocess.PIPE)
    t = subprocess.run(["/usr/bin/tar", "-xf", "-", "-C", str(dst)], stdin=a.stdout, capture_output=True, text=True)
    a.stdout.close()
    if a.wait() != 0 or t.returncode != 0:
        raise ReleaseError(f"extracting {paths} of {repo.name}@{tree[:12]} failed: {t.stderr[-400:]}")
    return sum(1 for p in dst.rglob("*") if p.is_file())


def patched_trees(c: Ctx) -> dict:
    out = {}
    for key, repo, sha, patches in (("azerothcore", P.CORE_SRC, c.lock["core_sha"], B.core_patches()),
                                    ("mod-playerbots", P.BOTS_SRC, c.lock["playerbots_sha"], B.playerbots_patches())):
        tree, probs = B.expected_patched_tree(repo, sha, patches)
        if not tree:
            raise ReleaseError(f"{key}: can't build pinned sha + patches: {probs}")
        out[key] = {"repo": repo, "sha": sha, "tree": tree, "patches": [p.name for p in patches]}
    return out


def archive_placeholders(tree: str, sql: Path) -> int:
    """Empty files named like AzerothCore's data/sql/archive/<db>/*.sql (release-first-launch task 2.2).

    The base databases list every archived update as applied with state ARCHIVED and updates_include names the archive
    folders (data/sql/base/db_*/updates*.sql). DBUpdater (UpdateFetcher::Update) skips a file that is ARCHIVED both on
    disk and in the database without reading it (Updates.ArchivedRedundancy = 0, release mode pins it), but a file it
    can't find at all is a "dead reference": 2,200 warnings and a "Cleanup is disabled" error on every start. The real
    archive is 213 MB (the zip must stay under 200 MB), so the release ships the names only; the AzerothCore source
    asset holds the files."""
    names = sh(["git", "-C", P.CORE_SRC, "ls-tree", "-r", "--name-only", tree, "data/sql/archive"]).stdout.split()
    n = 0
    for f in names:
        if f.endswith(".sql"):
            dst = sql / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(b"")
            n += 1
    (sql / "data/sql/archive/README.md").write_text(
        "Names only: these files are empty on purpose. The databases mark every one of them as an archived update "
        "that is already applied, so the server never reads them; the full files are in the AzerothCore source "
        "asset of this release (tools/release/release.py archive_placeholders).\n")
    return n


def build_app_binaries(c: Ctx, dst: Path) -> dict:
    """The launcher (harness/app/launcher.m) and the Wine host shim (harness/app/dockshim.m), compiled like
    hslib/app.py and hslib/dockshim.py do, into the payload's bin/."""
    from hslib import app as A, dockshim as S
    tmp = c.work / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = os.environ | {"TMPDIR": str(tmp) + "/"}
    sdk = sh(["/usr/bin/xcrun", "--show-sdk-path"], env=env).stdout.strip()
    dst.mkdir(parents=True, exist_ok=True)
    sh(["/usr/bin/clang", "-isysroot", sdk, "-fobjc-arc", "-O2", "-Wall", "-Werror",
        f"-mmacosx-version-min={A.DEPLOYMENT_TARGET}", "-framework", "Cocoa", A.SOURCE, "-o", dst / A.EXE_NAME], env=env)
    sh(["/usr/bin/clang", "-isysroot", sdk, *[x for a in S.ARCHS for x in ("-arch", a)], "-fobjc-arc", "-O2", "-Wall",
        "-Werror", "-dynamiclib", "-mmacosx-version-min=13.0", "-framework", "Foundation", S.SOURCE,
        "-install_name", "@loader_path/hsdock.dylib", "-o", dst / "hsdock.dylib"], env=env)
    for f in (dst / A.EXE_NAME, dst / "hsdock.dylib"):
        sh(["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", f])
    shutil.rmtree(tmp, ignore_errors=True)
    return {"launcher": A.EXE_NAME, "dockshim": "hsdock.dylib"}


def _license_files(d: Path) -> list[Path]:
    import fnmatch
    hits = []
    for p in sorted(d.iterdir()) if d.is_dir() else []:
        if p.is_file() and any(fnmatch.fnmatchcase(p.name, g) for g in LICENSE_NAMES):
            hits.append(p)
    return hits


def third_party(c: Ctx, root: Path, delo: dict, mysql: Path, trees: dict) -> dict:
    """THIRD_PARTY/<component>/ licence texts and INDEX.json {component: {files, covers}}."""
    tp = root / "THIRD_PARTY"
    index: dict[str, dict] = {}

    def add(comp: str, files: list[Path], covers: list[str], note: str = "") -> None:
        if not files:
            raise ReleaseError(f"no licence text found for {comp}")
        d = tp / comp
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, d / f.name)
        e = index.setdefault(comp, {"files": [], "covers": []})
        e["files"] += [f"{comp}/{f.name}" for f in files]
        e["covers"] += covers
        if note:
            e["note"] = note

    # AzerothCore (+ the libraries in its deps/ that are compiled into the server) and mod-playerbots
    ac = c.work / "tmp-ac-licences"
    shutil.rmtree(ac, ignore_errors=True)
    git_tree_extract(P.CORE_SRC, trees["azerothcore"]["tree"], ("LICENSE",), ac)
    add("azerothcore", [ac / "LICENSE"], ["server/bin/authserver", "server/bin/worldserver", "sql/data/sql"])
    deps_tree = sh(["git", "-C", P.CORE_SRC, "ls-tree", "--name-only", "-r", trees["azerothcore"]["tree"], "deps"])
    import fnmatch
    lic = [f for f in deps_tree.stdout.splitlines()
           if f.count("/") == 2 and any(fnmatch.fnmatchcase(f.split("/")[-1], g) for g in LICENSE_NAMES)]
    git_tree_extract(P.CORE_SRC, trees["azerothcore"]["tree"], tuple(lic), ac)
    for f in lic:
        dep = f.split("/")[1]
        add(f"azerothcore-deps/{dep}", [ac / f], [f"compiled into the server from AzerothCore deps/{dep}"])
    shutil.rmtree(ac, ignore_errors=True)
    add("mod-playerbots", [P.BOTS_SRC / "LICENSE"] if (P.BOTS_SRC / "LICENSE").is_file() else [],
        ["server/bin/worldserver", "sql/modules/mod-playerbots"])
    # Oracle MySQL (its LICENSE lists the third-party code it bundles, OpenSSL included)
    add("mysql", [mysql / "LICENSE", mysql / "README"], ["mysql/", "server/lib/libmysqlclient.24.dylib"])
    # python-build-standalone: CPython plus every library linked into it (the build's own licences/ folder)
    pyl = unpack(c, "release-python-licenses")
    add("python", sorted((pyl / "licenses").glob("*")), ["python/"],
        "python-build-standalone 20261003 (CPython 3.13.16) licences folder")
    # every bundled dylib: the Homebrew keg's licence, or Oracle's
    for name, b in sorted(delo["bundled"].items()):
        src = Path(b["from"])
        parts = src.parts
        if str(src).startswith(str(mysql)):
            index["mysql"]["covers"].append(f"server/lib/{name}")
            continue
        if "Cellar" in parts:
            i = parts.index("Cellar")
            formula, keg = parts[i + 1], Path(*parts[: i + 3])
            files = _license_files(keg) or ([FALLBACK_LICENSES[formula]] if formula in FALLBACK_LICENSES else [])
            add(formula, files, [f"server/lib/{name}"])
            continue
        raise ReleaseError(f"bundled {name} comes from {src}: no licence source known")
    (tp / "INDEX.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    return index


def step_payload(c: Ctx) -> dict:
    dirty = sh(["git", "-C", ROOT, "status", "--porcelain", "--untracked-files=no"]).stdout.strip()
    if os.environ.get("GITHUB_ACTIONS") == "true" and dirty:
        raise ReleaseError("CI checkout has uncommitted changes; a release must be built from the tagged commit")
    inst = c.build / "install"
    for n in ("authserver", "worldserver"):
        if not (inst / "bin" / n).is_file():
            raise ReleaseError(f"no {inst / 'bin' / n}; run the build step first")
    mysql = unpack(c, "release-mysql")
    py = unpack(c, "release-python")
    trees = patched_trees(c)
    stage = c.work / "payload.tmp" / PAYLOAD_NAME
    shutil.rmtree(stage.parent, ignore_errors=True)
    stage.mkdir(parents=True)
    rec: dict = {}
    # server
    (stage / "server/bin").mkdir(parents=True)
    for n in ("authserver", "worldserver"):
        shutil.copy2(inst / "bin" / n, stage / "server/bin" / n)
    shutil.copytree(inst / "etc", stage / "server/etc")
    (stage / "server/lib").mkdir()
    shutil.copy2(mysql / "lib/libmysqlclient.24.dylib", stage / "server/lib/libmysqlclient.24.dylib")
    rec["delocate"] = delocate.fix(stage, "server/lib", ["server/bin/authserver", "server/bin/worldserver"],
                                   [mysql / "lib"])
    (c.build / "delocate.json").write_text(json.dumps(rec["delocate"], indent=2) + "\n")
    # mysql (Oracle's signed binaries, unmodified)
    m = stage / "mysql"
    (m / "bin").mkdir(parents=True)
    for n in MYSQL_KEEP_BIN:
        shutil.copy2(mysql / "bin" / n, m / "bin" / n)
    (m / "lib").mkdir()
    # only the libraries the kept programs load (their @loader_path closure), not Kerberos/FIDO plugins' libraries
    need, todo = set(), [mysql / "bin" / n for n in MYSQL_KEEP_BIN]
    while todo:
        img = todo.pop()
        for dep in delocate.load_info(img)["deps"]:
            if delocate.is_system(dep):
                continue
            src = delocate.resolve(dep, img.parent, mysql / "bin", [], [])
            if src is None:
                raise ReleaseError(f"Oracle MySQL: {img.name} needs {dep}, which isn't in the tarball")
            real = src.resolve()
            if real not in need:
                need.add(real)
                todo.append(real)
    rec["mysql_libs"] = sorted(p.name for p in need)
    for real in sorted(need):
        if real.parent != (mysql / "lib").resolve():
            raise ReleaseError(f"Oracle MySQL: unexpected library location {real}")
        shutil.copy2(real, m / "lib" / real.name)
    # Oracle's dylib links in bin/, only those pointing at a library we keep (a dangling link makes the bundle
    # signature fail to verify: release-signing 2.1)
    for p in sorted((mysql / "bin").iterdir()):
        if p.is_symlink() and p.name.endswith(".dylib") and (m / "bin" / os.readlink(p)).is_file():
            os.symlink(os.readlink(p), m / "bin" / p.name)
    for n in MYSQL_KEEP_SHARE:
        shutil.copytree(mysql / "share" / n, m / "share" / n)
    for n in ("LICENSE", "README"):
        shutil.copy2(mysql / n, m / n)
    # python
    shutil.copytree(py, stage / "python", symlinks=True)
    for rel in PY_PRUNE:
        p = stage / "python" / rel
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        elif p.exists() or p.is_symlink():
            p.unlink()
    for g in PY_PRUNE_GLOBS:
        for p in (stage / "python").glob(g):
            shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
    for p in list((stage / "python").rglob("__pycache__")):
        shutil.rmtree(p)
    site = stage / "python/lib/python3.13/site-packages"
    site.mkdir(parents=True, exist_ok=True)
    (site / "moonglade-harness.pth").write_text("../../../../harness\n")
    # repo parts, from the commit (HEAD; the tag in CI), so the payload matches the Moonglade source asset exactly
    head = sh(["git", "-C", ROOT, "rev-parse", "HEAD"]).stdout.strip()
    rec["repo_files"] = git_tree_extract(ROOT, head, REPO_PARTS, stage)
    # SQL source dir for the DB updater
    sql = stage / "sql"
    n_core = git_tree_extract(P.CORE_SRC, trees["azerothcore"]["tree"], CORE_SQL, sql)
    n_bots = git_tree_extract(P.BOTS_SRC, trees["mod-playerbots"]["tree"], ("data/sql",),
                              sql / "modules" / "mod-playerbots")
    ours = sorted({"/".join(f.split("/")[:4]) for f in tracked(("modules",)) if f.split("/")[2:4] == ["data", "sql"]})
    n_ours = git_tree_extract(ROOT, head, tuple(ours), sql) if ours else 0
    rec["sql_files"] = {"core": n_core, "mod-playerbots": n_bots, "ours": n_ours,
                        "archive_placeholders": archive_placeholders(trees["azerothcore"]["tree"], sql)}
    # prebuilt launcher + Wine host shim
    rec["app"] = build_app_binaries(c, stage / "bin")
    # licences
    rec["third_party"] = sorted(third_party(c, stage, rec["delocate"], mysql, trees))
    # VERSION
    (stage / "VERSION").write_text(json.dumps({
        "version": c.version, "commit": head, "built": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "core_sha": c.lock["core_sha"], "playerbots_sha": c.lock["playerbots_sha"],
        "core_tree_with_patches": trees["azerothcore"]["tree"],
        "playerbots_tree_with_patches": trees["mod-playerbots"]["tree"],
        "mysql": Path(c.lock["release_mysql_url"]).name, "python": Path(c.lock["release_python_url"]).name,
        "deployment_target": DEPLOYMENT_TARGET,
        "build": "ci" if c.ci else LOCAL_LABEL}, indent=2) + "\n")
    # the forbidden-path gate over the whole payload; the build-tree allowlist entries are anchored to this run's
    # checkout (release-first-launch 5.1)
    g = delocate.gate(stage, delocate.load_allowlist(ALLOWLIST, c.build_root))
    (c.build / "gate.json").write_text(json.dumps(g, indent=2) + "\n")
    if g["problems"]:
        raise ReleaseError(f"forbidden paths in the payload ({len(g['problems'])}): {g['problems'][:8]}")
    rec["gate"] = {"macho_files": g["macho_files"], "allowlisted": len(g["allowlisted"])}
    # Oracle's signatures as shipped (release-signing design 3): `sign` leaves these files alone; checks compare
    oracle = SG.oracle_files(stage)
    (c.build / "oracle.json").write_text(json.dumps(oracle, indent=2, sort_keys=True) + "\n")
    rec["oracle_files"] = oracle
    (c.build / "sign.json").unlink(missing_ok=True)        # a new app is unsigned until `sign` runs
    app = c.work / "payload.tmp" / APP_NAME
    rec["app"] = wrap_app(stage, app)
    if c.payload_parent.exists():
        shutil.rmtree(c.payload_parent)
    c.payload_parent.mkdir(parents=True)
    app.rename(c.app)
    shutil.rmtree(app.parent, ignore_errors=True)
    rec["payload"] = str(c.payload)
    rec["payload_bytes"] = sum(p.lstat().st_size for p in c.payload.rglob("*") if p.is_file())
    return rec


# ------------------------------------------------------------------------------------------------------ assets

def zip_name(c: Ctx) -> str:
    return f"Moonglade-{c.version}-macos-arm64{'' if c.ci else LOCAL_SUFFIX}.zip"


def release_info_plist() -> dict:
    """The release app's Info.plist. No "not for redistribution" line and no final bundle id yet (release-signing)."""
    return {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": "Moonglade",
        "CFBundleExecutable": "Moonglade",
        "CFBundleIconFile": "AppIcon",
        "CFBundleIdentifier": RELEASE_BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "Moonglade",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1",
        "CFBundleVersion": "1",
        "LSApplicationCategoryType": "public.app-category.role-playing-games",
        "LSMinimumSystemVersion": DEPLOYMENT_TARGET,
        "NSHighResolutionCapable": True,
    }


def wrap_app(payload: Path, app: Path) -> dict:
    """Moonglade.app around the payload: the payload moves to Contents/Resources/Moonglade, the prebuilt launcher is
    copied to Contents/MacOS/Moonglade, plus Info.plist, PkgInfo and the icon."""
    import plistlib
    if app.exists():
        shutil.rmtree(app)
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    payload.rename(app / APP_PAYLOAD)
    pl = app / APP_PAYLOAD
    shutil.copy2(pl / "bin" / "Moonglade", contents / "MacOS" / "Moonglade")
    with open(contents / "Info.plist", "wb") as f:
        plistlib.dump(release_info_plist(), f, fmt=plistlib.FMT_XML, sort_keys=True)
    (contents / "PkgInfo").write_text("APPL????")
    shutil.copy2(pl / "branding" / "AppIcon.icns", contents / "Resources" / "AppIcon.icns")
    return {"app": str(app), "bundle_id": RELEASE_BUNDLE_ID, "payload": str(APP_PAYLOAD)}


def git_archive(repo: Path, ref: str, prefix: str, dst: Path) -> None:
    with open(dst, "wb") as fh:
        a = subprocess.Popen(["git", "-C", str(repo), "archive", "--format=tar", f"--prefix={prefix}/", ref],
                             stdout=subprocess.PIPE)
        g = subprocess.run(["/usr/bin/gzip", "-n", "-9"], stdin=a.stdout, stdout=fh)
        a.stdout.close()
        if a.wait() != 0 or g.returncode != 0:
            raise ReleaseError(f"git archive {repo.name} {ref} failed")


def step_sign(c: Ctx) -> dict:
    """Sign the app in the checked-in mode (release-signing design 1, 3, 4); writes build/sign.json."""
    try:
        return SG.sign(c.app, c.build, c.signing_config, c.build / "oracle.json")
    except SG.SignError as e:
        raise ReleaseError(str(e)) from None


def signed_now(c: Ctx) -> dict:
    """This app's sign record, refusing an app that was never signed, was signed in another mode, or changed since."""
    sj = c.build / "sign.json"
    if not sj.is_file() or sj.stat().st_mtime < (c.payload / "VERSION").stat().st_mtime:
        raise ReleaseError(f"the app isn't signed (no {sj} newer than the payload); run the sign step first")
    rec = json.loads(sj.read_text())
    mode = SG.read_mode(c.signing_config)
    if rec.get("mode") != mode:
        raise ReleaseError(f"the app was signed in {rec.get('mode')} mode but {c.signing_config} says {mode}; sign again")
    now = SG.display(c.app).get("cdhash")
    v = sh(["/usr/bin/codesign", "--verify", "--deep", "--strict", c.app], check=False)
    if v.returncode != 0 or now != (rec.get("bundle") or {}).get("cdhash"):
        raise ReleaseError(f"the app changed since it was signed (cdhash {now}, verify rc {v.returncode}); sign again")
    return rec


def zip_app(c: Ctx) -> Path:
    z = c.out / zip_name(c)
    z.unlink(missing_ok=True)
    sh([*SG.DITTO_ZIP, c.app, z])
    return z


def write_sums(c: Ctx) -> None:
    lines = [f"{sha256(p)}  {p.name}" for p in sorted(c.out.iterdir()) if p.is_file() and p.name != "SHA256SUMS"]
    (c.out / "SHA256SUMS").write_text("\n".join(lines) + "\n")


def step_assets(c: Ctx) -> dict:
    if not (c.payload / "VERSION").is_file():
        raise ReleaseError(f"no payload at {c.payload}; run the payload step first")
    if c.ci != (json.loads((c.payload / "VERSION").read_text()).get("build") == "ci"):
        raise ReleaseError("the payload's VERSION build label doesn't match this run (CI vs local); rebuild the payload")
    signed = signed_now(c)
    c.guard.check("the assets step (zip + source tarballs)")
    trees = patched_trees(c)
    if c.out.exists():
        shutil.rmtree(c.out)
    c.out.mkdir(parents=True)
    assets: dict[str, dict] = {}
    z = zip_app(c)
    assets[z.name] = {"what": "Moonglade.app (macOS arm64)" + ("" if c.ci else f"; {LOCAL_LABEL}"),
                      "signing": signed["mode"], "notarized": False}
    # the Moonglade source at this commit (the tag in CI)
    head = sh(["git", "-C", ROOT, "rev-parse", "HEAD"]).stdout.strip()
    src = c.out / f"moonglade-{c.version}-source.tar.gz"
    git_archive(ROOT, head, f"moonglade-{c.version}", src)
    assets[src.name] = {"what": "Moonglade source", "commit": head}
    for key, t in trees.items():
        name = f"{key}-{t['sha'][:12]}-with-patches-source.tar.gz"
        git_archive(t["repo"], t["tree"], key, c.out / name)
        assets[name] = {"what": f"{key} source at the pinned commit with patches/ applied", "commit": t["sha"],
                        "tree": t["tree"], "patches": t["patches"]}
    ms = _fetched(c, "release-mysql-source")
    dst = c.out / Path(c.lock["release_mysql_source_url"]).name
    sh(["/bin/cp", "-c", ms, dst])          # APFS clone: no extra disk
    assets[dst.name] = {"what": "MySQL source for the bundled Oracle MySQL", "sha256": c.lock["release_mysql_source_sha256"]}
    (c.out / "SOURCES.json").write_text(json.dumps({"version": c.version, "assets": assets}, indent=2) + "\n")
    write_sums(c)
    sizes = {p.name: p.stat().st_size for p in sorted(c.out.iterdir())}
    return {"out": str(c.out), "assets": sorted(sizes), "sizes": sizes, "zip": str(z), "signing": signed["mode"]}


def step_notarize(c: Ctx) -> dict:
    """developer-id: notarize the zip, staple the app, zip it again and refresh SOURCES.json / SHA256SUMS. ad-hoc: a
    no-op recorded as not applicable."""
    z = c.out / zip_name(c)

    def rezip() -> dict:
        signed_now(c)
        zip_app(c)
        src = json.loads((c.out / "SOURCES.json").read_text())
        src["assets"][z.name].update({"notarized": True, "stapled": True})
        (c.out / "SOURCES.json").write_text(json.dumps(src, indent=2) + "\n")
        write_sums(c)
        return {"zip": str(z), "sha256": sha256(z), "bytes": z.stat().st_size}
    try:
        return SG.notarize(c.app, z, c.build, c.signing_config, rezip)
    except SG.SignError as e:
        raise ReleaseError(str(e)) from None


STEPS = {"clean": step_clean, "fetch": step_fetch, "build": step_build, "payload": step_payload, "sign": step_sign,
         "assets": step_assets, "notarize": step_notarize}
ALL = ("clean", "fetch", "build", "payload", "sign", "assets", "notarize")
SIGNING_STEPS = ("sign", "assets", "notarize")      # steps that depend on the signing mode


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="release.py", description="Moonglade release packaging (tools/release)")
    ap.add_argument("steps", nargs="+", choices=[*STEPS, "all"])
    ap.add_argument("--work", type=Path, help="work dir (default env/release; must be inside it)")
    ap.add_argument("--cache", type=Path, help="pinned-download cache (default <work>/cache)")
    ap.add_argument("--out", type=Path, help="release assets dir (default <work>/out)")
    ap.add_argument("--version", help="release version (default: the tag in CI, else 0.0.0-dev+<sha>)")
    ap.add_argument("-j", "--jobs", type=int, default=None, help="compile jobs (default: performance cores)")
    ap.add_argument("--ignore-power", action="store_true", help="don't pause on low battery")
    ap.add_argument("--sync-vendor", action="store_true", help="CI: sync vendor/ to the pins first")
    ap.add_argument("--record", type=Path, help="write the run record (JSON) here")
    ap.add_argument("--signing-config", type=Path, default=None,
                    help="checks only: a scratch signing-mode file under env/release/ or harness/out/ "
                         "(default tools/release/signing.json)")
    a = ap.parse_args(argv)
    steps = ALL if "all" in a.steps else tuple(s for s in ALL if s in a.steps)
    for sw in power.set_switches():
        print(f"release: refusing the harness test switch ${sw}", file=sys.stderr)
        return 2
    t0 = time.time()
    rec: dict = {"steps": {}, "ok": False}
    try:
        c = Ctx(a)
        rec["ctx"] = c.record()
        if any(s in SIGNING_STEPS for s in steps):
            # release-signing: the mode (and, when this run signs, developer-id's credential presence and CI) is
            # checked before any step runs, so `all` with a bad setting refuses before clean/fetch/build/payload.
            # Only names are ever printed. (CI's later assets/notarize-only runs check the mode alone.)
            rec["signing_preflight"] = {"mode": SG.preflight(c.signing_config, SG.EnvReader()) if "sign" in steps
                                        else SG.read_mode(c.signing_config)}
        for s in steps:
            log(f"step {s}")
            ts = time.time()
            rec["steps"][s] = STEPS[s](c)
            rec["steps"][s] = {"seconds": round(time.time() - ts), **(rec["steps"][s] or {})}
        rec["ok"] = True
        return 0
    except (ReleaseError, delocate.DelocateError, power.JobsError, SG.SignError) as e:
        rec["error"] = str(e)
        print(f"release: FAILED: {e}", file=sys.stderr)
        return 1
    except power.PowerStop as e:
        rec["error"] = str(e)
        print(f"release: stopped by the battery guard: {e}", file=sys.stderr)
        return power.STOP_EXIT
    finally:
        rec["seconds"] = round(time.time() - t0)
        if a.record:
            a.record.parent.mkdir(parents=True, exist_ok=True)
            a.record.write_text(json.dumps(rec, indent=2, default=str) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
