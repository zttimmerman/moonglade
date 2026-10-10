"""Vendor sync (pin + patches + module links) and cmake build/install. ADR-0001.

Clean builds are input-hash aware (openspec/changes/harness-tiers, server-build "Clean build from pinned
sources"): a passing from-empty build writes a stamp of the build-input hash to build/.hs-input-hash and next
to the installed binaries. `--clean` then does a from-empty build only when the inputs hash differs from that
stamp (or there is no valid stamp); otherwise it runs mode "verified-current": the installed stamp must equal
the current hash and an incremental build must leave the installed binaries byte-identical.
`--force-clean` always builds from empty."""
import hashlib
import json
import os
import shutil
import subprocess
import time

from . import paths as P
from .util import log, run


class BuildError(RuntimeError):
    def __init__(self, msg: str, info: dict | None = None):
        super().__init__(msg)
        self.info = info or {}


def _git(repo, *args, capture=True, check=True):
    return run(["git", "-C", repo, *args], capture=capture, check=check)


def _head(repo) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _ensure_clone(repo_dir, url, branch, sha):
    if (repo_dir / ".git").exists() and _git(repo_dir, "rev-parse", "HEAD", check=False).returncode != 0:
        log(f"{repo_dir} is an unfinished clone (an interrupted setup?); cloning it again")
        shutil.rmtree(repo_dir)
    try:
        if not (repo_dir / ".git").exists():
            P.VENDOR.mkdir(exist_ok=True)
            run(["git", "clone", "--branch", branch, url, repo_dir])
        if _git(repo_dir, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
            _git(repo_dir, "fetch", "origin", capture=False)
    except subprocess.CalledProcessError as e:
        # easy-setup: a plain message (bootstrap shows it) instead of a traceback; a half-made clone is removed so a
        # re-run starts the clone again
        if (repo_dir / ".git").exists() and _git(repo_dir, "rev-parse", "HEAD", check=False).returncode != 0:
            shutil.rmtree(repo_dir, ignore_errors=True)
        raise BuildError(f"couldn't get the pinned sources from {url} ({' '.join(map(str, e.cmd[:3]))} exited "
                         f"{e.returncode}); check the network connection and run it again") from None


def pin_state(repo_dir, sha, ignore_untracked_prefix=None) -> list[str]:
    """Return a list of human-readable problems (empty = pinned and clean)."""
    problems = []
    head = _head(repo_dir)
    if head != sha:
        problems.append(f"{repo_dir.name}: HEAD {head[:12]} != pinned {sha[:12]}")
    st = _git(repo_dir, "status", "--porcelain", "--ignored=no").stdout.splitlines()
    if ignore_untracked_prefix:
        st = [l for l in st if not l[3:].startswith(ignore_untracked_prefix)]
    if st:
        problems.append(f"{repo_dir.name}: working tree dirty ({len(st)} entries, e.g. {st[0].strip()})")
    return problems


def expected_patched_tree(repo_dir, sha, patches) -> tuple[str | None, list[str]]:
    """Tree id of `sha` with every patch applied, built in a throwaway index (GIT_INDEX_FILE in a temp dir) so the
    real index and working tree are untouched. Returns (tree id or None, problems)."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="hs-pin-") as td:
        env = os.environ | {"GIT_INDEX_FILE": os.path.join(td, "index")}

        def g(*a):
            return subprocess.run(["git", "-C", str(repo_dir), *a], capture_output=True, text=True, env=env)
        r = g("read-tree", sha)
        if r.returncode != 0:
            return None, [f"{repo_dir.name}: cannot read {sha[:12]}: {r.stderr.strip()}"]
        for p in patches:
            r = g("apply", "--cached", "--whitespace=nowarn", str(p))
            if r.returncode != 0:
                return None, [f"{repo_dir.name}: {p.name} does not apply to {sha[:12]}: {r.stderr.strip()[:300]}"]
        return g("write-tree").stdout.strip(), []


def patched_pin_state(repo_dir, sha, patches, ignore_untracked_prefix=None) -> list[str]:
    """Like pin_state, for a tree that should be `sha` + exactly `patches` (server-build "Unpinned or dirty vendor
    tree is rejected": the only allowed uncommitted changes are those from patches/core/). Problems if HEAD != sha,
    the index's tree != sha + patches, or the working tree differs from the index (incl. untracked files)."""
    problems = []
    head = _head(repo_dir)
    if head != sha:
        problems.append(f"{repo_dir.name}: HEAD {head[:12]} != pinned {sha[:12]}")
    want, probs = expected_patched_tree(repo_dir, sha, patches)
    problems += probs
    have = _git(repo_dir, "write-tree", check=False).stdout.strip()
    if want and have != want:
        changed = _git(repo_dir, "diff", "--name-only", want, have, check=False).stdout.split()
        problems.append(f"{repo_dir.name}: index differs from {sha[:12]} + {len(patches)} patch(es) "
                        f"({len(changed)} paths, e.g. {changed[:3]})")
    st = _git(repo_dir, "status", "--porcelain", "--ignored=no").stdout.splitlines()
    unstaged = [l for l in st if l[1] != " " or l.startswith("??")]
    if ignore_untracked_prefix:
        unstaged = [l for l in unstaged if not l[3:].startswith(ignore_untracked_prefix)]
    if unstaged:
        problems.append(f"{repo_dir.name}: working tree differs from index ({len(unstaged)} entries, e.g. {unstaged[0].strip()})")
    return problems


def _expected_links() -> dict:
    links = {"mod-playerbots": P.BOTS_SRC, "mod-autobalance": P.AB_SRC}
    for m in sorted(P.MODULES.iterdir()) if P.MODULES.exists() else []:
        if m.is_dir() and not m.name.startswith("."):
            links[m.name] = m
    return links


def stray_module_entries() -> list[str]:
    """Entries in core modules/ that are neither git-tracked nor a harness symlink to an expected module.
    The core .gitignore ignores /modules/*, so git status/clean can't see these; with MODULES=static they compile in."""
    mods = P.CORE_SRC / "modules"
    tracked = {l.split("/", 1)[1].split("/")[0] for l in _git(P.CORE_SRC, "ls-files", "modules").stdout.splitlines()}
    links = _expected_links()
    stray = []
    for e in sorted(mods.iterdir()) if mods.exists() else []:
        if e.name in tracked:
            continue
        if e.is_symlink() and e.name in links and os.readlink(e) == str(links[e.name]):
            continue
        stray.append(e.name)
    return stray


def core_patches() -> list:
    """patches/core/*.patch in apply order (ADR-0006)."""
    return sorted(P.PATCHES.glob("*.patch"))


def playerbots_patches() -> list:
    """patches/playerbots/*.patch in apply order, applied to vendor/mod-playerbots like the core queue (ADR-0008)."""
    return sorted(P.BOTS_PATCHES.glob("*.patch"))


def sync_vendor() -> dict:
    """Force vendor trees to the pinned state, apply the core and playerbots patch queues, link modules."""
    lock = P.fork_lock()
    _ensure_clone(P.CORE_SRC, lock["core_repo"], lock["core_branch"], lock["core_sha"])
    _ensure_clone(P.BOTS_SRC, lock["playerbots_repo"], lock["playerbots_branch"], lock["playerbots_sha"])
    # m4-dungeon-scaling (ADR-0014): mod-autobalance, pinned and unpatched (no patch queue)
    _ensure_clone(P.AB_SRC, lock["autobalance_repo"], lock["autobalance_branch"], lock["autobalance_sha"])

    before = {
        "core": pin_state(P.CORE_SRC, lock["core_sha"], "modules/"),
        "playerbots": patched_pin_state(P.BOTS_SRC, lock["playerbots_sha"], playerbots_patches()),
        "autobalance": pin_state(P.AB_SRC, lock["autobalance_sha"]),
    }
    before["core_stray_modules"] = stray_module_entries()
    for repo, sha in ((P.CORE_SRC, lock["core_sha"]), (P.BOTS_SRC, lock["playerbots_sha"]),
                      (P.AB_SRC, lock["autobalance_sha"])):
        _git(repo, "checkout", "-f", "--detach", sha)
        # -x: ignored files go too (both builds are out-of-tree). core modules/ is pruned separately below.
        _git(repo, "clean", "-fdx", "-e", "/modules/")
    for entry in stray_module_entries():
        p = P.CORE_SRC / "modules" / entry
        log(f"removing stray entry from core modules/: {p}")
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()

    patches = core_patches()
    for p in patches:
        r = _git(P.CORE_SRC, "apply", "--index", "--whitespace=nowarn", str(p), check=False)
        if r.returncode != 0:
            raise BuildError(f"core patch failed to apply: {p.name}\n{r.stdout}")
    bots_patches = playerbots_patches()
    for p in bots_patches:
        r = _git(P.BOTS_SRC, "apply", "--index", "--whitespace=nowarn", str(p), check=False)
        if r.returncode != 0:
            raise BuildError(f"playerbots patch failed to apply: {p.name}\n{r.stdout}")

    mods = P.CORE_SRC / "modules"
    links = _expected_links()
    for name, target in links.items():
        dst = mods / name
        if dst.is_symlink() and os.readlink(dst) == str(target):
            continue
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        elif dst.exists():
            raise BuildError(f"{dst} exists and is not a harness symlink; refusing to replace")
        dst.symlink_to(target)

    after = {
        "core_head": _head(P.CORE_SRC),
        "playerbots_head": _head(P.BOTS_SRC),
        "core_ok": _head(P.CORE_SRC) == lock["core_sha"],
        "playerbots_ok": _head(P.BOTS_SRC) == lock["playerbots_sha"]
                         and not patched_pin_state(P.BOTS_SRC, lock["playerbots_sha"], bots_patches),
        "autobalance_head": _head(P.AB_SRC),
        "autobalance_ok": not pin_state(P.AB_SRC, lock["autobalance_sha"]),
    }
    if patches:
        after["patches_applied"] = [p.name for p in patches]
    if bots_patches:
        after["playerbots_patches_applied"] = [p.name for p in bots_patches]
    # the tree must be core_sha + exactly the patch queue (equals pin_state's check when there are no patches)
    dirty = patched_pin_state(P.CORE_SRC, lock["core_sha"], patches, "modules/")
    if dirty:
        raise BuildError("core tree is not core_sha + patches/core after sync: " + "; ".join(dirty))
    after["core_stray_modules"] = stray_module_entries()
    if after["core_stray_modules"]:
        raise BuildError(f"stray entries remain in core modules/: {after['core_stray_modules']}")
    if not (after["core_ok"] and after["playerbots_ok"] and after["autobalance_ok"]):
        raise BuildError(f"vendor not at pinned SHAs after sync: {after}")
    return {"before": before, "after": after, "modules": sorted(links)}


def cmake_args() -> list[str]:
    b = P.BREW / "opt"
    return [
        f"-DCMAKE_INSTALL_PREFIX={P.SERVER}",
        "-DCMAKE_BUILD_TYPE=Release",  # RelWithDebInfo build/ was 26 GB; disk is tight
        "-DCMAKE_C_COMPILER=clang", "-DCMAKE_CXX_COMPILER=clang++",
        "-DCMAKE_C_COMPILER_LAUNCHER=ccache", "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
        "-DAPPS_BUILD=all", "-DTOOLS_BUILD=none", "-DSCRIPTS=static", "-DMODULES=static",
        "-DWITH_WARNINGS=0",
        f"-DMYSQL_ADD_INCLUDE_PATH={b}/mysql/include/mysql",
        f"-DMYSQL_LIBRARY={b}/mysql/lib/libmysqlclient.dylib",
        f"-DREADLINE_INCLUDE_DIR={b}/readline/include",
        f"-DREADLINE_LIBRARY={b}/readline/lib/libreadline.dylib",
        f"-DOPENSSL_ROOT_DIR={b}/openssl@3",
        f"-DOPENSSL_INCLUDE_DIR={b}/openssl@3/include",
        f"-DOPENSSL_SSL_LIBRARIES={b}/openssl@3/lib/libssl.dylib",
        f"-DOPENSSL_CRYPTO_LIBRARIES={b}/openssl@3/lib/libcrypto.dylib",
        f"-DBoost_ROOT={b}/boost",
    ]


# ----------------------------------------------------------------------------- build-input hash + stamps

STAMP_NAME = ".hs-input-hash"
IGNORED_NAMES = {".DS_Store"}     # Finder metadata; never a build input


def build_stamp_path():
    return P.BUILD / STAMP_NAME


def install_stamp_path():
    return P.SERVER_BIN / STAMP_NAME


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_digest(root) -> dict:
    """{relative path: sha256 of contents | 'symlink:<target>'} for every file under root (contents, not mtimes)."""
    out = {}
    if not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames) + sorted(d for d in dirnames if os.path.islink(os.path.join(dirpath, d))):
            if name in IGNORED_NAMES:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            out[rel] = f"symlink:{os.readlink(full)}" if os.path.islink(full) else _sha256_file(full)
    return out


def compiler_identity() -> dict:
    """Toolchain identity: `--version` of the compilers and cmake named in cmake_args()."""
    ident = {}
    for tool in ("clang", "clang++", "cmake"):
        r = subprocess.run([tool, "--version"], capture_output=True, text=True)
        ident[tool] = (r.stdout or r.stderr).strip()
    return ident


def _resolved_deps() -> dict:
    """Where the Homebrew opt/ links in cmake_args() point (opt/boost -> Cellar/boost/<version>): a dependency
    upgrade changes the libraries the binaries link against, so it counts as a cmake-argument change."""
    b = P.BREW / "opt"
    return {n: os.path.realpath(b / n) for n in ("mysql", "readline", "openssl@3", "boost")}


# fork.lock keys that determine the server build. The other pins (Ghidra, Wine runtime, client data, VMaNGOS)
# don't affect authserver/worldserver, so bumping them must not force a from-empty build.
BUILD_PINS = ("core_repo", "core_branch", "core_sha", "playerbots_repo", "playerbots_branch", "playerbots_sha",
              "autobalance_repo", "autobalance_branch", "autobalance_sha")


def build_pins(path=None) -> dict:
    """The server-build pins of a fork.lock file (same parsing as paths.fork_lock); missing keys map to None."""
    lock = {}
    for line in (path or (P.ROOT / "fork.lock")).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            lock[k.strip()] = v.strip()
    return {k: lock.get(k) for k in BUILD_PINS}


def input_manifest(*, fork_lock_path=None, patches_dir=None, modules_dir=None, cmake=None, compiler=None,
                   bots_patches_dir=None) -> dict:
    """Everything a build's output depends on that the harness controls. Keyword overrides exist so checks
    can show a changed input changes the hash without touching the real inputs (sandbox copies)."""
    fl = fork_lock_path or (P.ROOT / "fork.lock")
    return {
        "fork_lock_build_pins": build_pins(fl),
        "patches_core": tree_digest(patches_dir or P.PATCHES),
        "patches_playerbots": tree_digest(bots_patches_dir or P.BOTS_PATCHES),
        "modules": tree_digest(modules_dir or P.MODULES),
        "cmake_args": cmake if cmake is not None else [str(a) for a in cmake_args()] + [f"dep:{k}={v}" for k, v in sorted(_resolved_deps().items())],
        "compiler": compiler if compiler is not None else compiler_identity(),
    }


def input_hash(manifest: dict | None = None) -> str:
    m = manifest if manifest is not None else input_manifest()
    return hashlib.sha256(json.dumps(m, sort_keys=True).encode()).hexdigest()


def read_stamp(path) -> str | None:
    try:
        return json.loads(path.read_text())["input_hash"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_stamps(h: str, manifest: dict):
    doc = json.dumps({"input_hash": h, "written": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                      "by": "passing from-empty build (harness/hslib/build.py)", "manifest": manifest}, indent=2)
    for p in (build_stamp_path(), install_stamp_path()):
        p.write_text(doc)


def _drop_stamps() -> list[str]:
    gone = []
    for p in (install_stamp_path(), build_stamp_path()):
        if p.exists():
            p.unlink()
            gone.append(str(p))
    return gone


def decide_mode(current: str, build_stamp: str | None, install_stamp: str | None, force: bool) -> tuple[str, str]:
    """('from-empty' | 'verified-current', reason)."""
    if force:
        return "from-empty", "--force-clean"
    if not build_stamp:
        return "from-empty", "no stamp from a passing from-empty build in build/"
    if build_stamp != current:
        return "from-empty", f"build inputs changed (stamp {build_stamp[:12]} != current {current[:12]})"
    if install_stamp != current:
        return "from-empty", f"installed binaries not stamped with the current input hash ({str(install_stamp)[:12]})"
    return "verified-current", "inputs hash equals the last passing from-empty build's stamp"


def installed_digest() -> dict:
    """sha256 of every installed artifact cmake --install writes: bin/* (minus the stamp) and etc/**/*.dist."""
    d = {f"bin/{k}": v for k, v in tree_digest(P.SERVER_BIN).items() if k != STAMP_NAME}
    d |= {f"etc/{k}": v for k, v in tree_digest(P.SERVER_ETC).items() if k.endswith(".dist")}
    return d


# ----------------------------------------------------------------------------- moved repo (easy-setup, F30)
# CMake bakes absolute paths into the build tree (CMakeCache.txt) and the binaries (e.g. the module config dir), so a
# server built in another folder (the repo moved or renamed) misbehaves. The install record says which repo folder
# the installed server was built for; `hs up` and ./play refuse a mismatch with the command to run, `hs build`
# rebuilds from empty, and the baseline build (baseline.py) discards a foreign build tree by itself.

INSTALL_RECORD = P.SERVER / ".hs-install.json"     # not under bin/ or etc/: never part of installed_digest()


def cmake_cache_info(build_dir) -> dict | None:
    """{cache_dir, home, prefix} recorded in build_dir/CMakeCache.txt, or None when there is no readable cache."""
    try:
        text = (build_dir / "CMakeCache.txt").read_text(errors="replace")
    except OSError:
        return None
    out = {}
    for line in text.splitlines():
        for key, name in (("CMAKE_CACHEFILE_DIR:INTERNAL=", "cache_dir"), ("CMAKE_HOME_DIRECTORY:INTERNAL=", "home"),
                          ("CMAKE_INSTALL_PREFIX:PATH=", "prefix")):
            if line.startswith(key):
                out[name] = line[len(key):].strip()
    return out or None


def foreign_build_tree(build_dir, src=None) -> dict | None:
    """The CMakeCache in build_dir was made for another directory (or another source tree): {cache_dir, home, ...}."""
    info = cmake_cache_info(build_dir)
    if not info:
        return None
    here = os.path.realpath(build_dir)
    if info.get("cache_dir") and os.path.realpath(info["cache_dir"]) != here:
        return info | {"expected_cache_dir": here}
    if src is not None and info.get("home") and os.path.realpath(info["home"]) != os.path.realpath(src):
        return info | {"expected_home": os.path.realpath(src)}
    return None


def read_install_record(path=None) -> dict | None:
    try:
        rec = json.loads((path or INSTALL_RECORD).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def write_install_record() -> dict:
    rec = {"repo": str(P.ROOT.resolve()), "build": str(P.BUILD.resolve()), "server": str(P.SERVER.resolve()),
           "written": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "by": "harness/hs build"}
    INSTALL_RECORD.write_text(json.dumps(rec, indent=2) + "\n")
    return rec


MOVED_FIX = "harness/hs build --force-clean"


def moved_problem() -> dict | None:
    """Was the installed server (its install record) or the build tree (its CMakeCache) made in another repo folder?
    Returns {what, old_repo, new_repo, message} or None. A server with no install record (built before easy-setup) is
    judged by its build tree alone."""
    new = os.path.realpath(P.ROOT)
    rec = read_install_record()
    found = None
    if rec and rec.get("repo") and os.path.realpath(rec["repo"]) != new:
        found = {"what": "the installed server (server/)", "old_repo": rec["repo"], "evidence": str(INSTALL_RECORD)}
    else:
        f = foreign_build_tree(P.BUILD, P.CORE_SRC)
        if f:
            old = f.get("cache_dir") or f.get("home") or "?"
            found = {"what": "the server build tree (build/)", "old_repo": os.path.dirname(old.rstrip("/")),
                     "evidence": str(P.BUILD / "CMakeCache.txt"), "cache": f}
    if not found:
        return None
    found["new_repo"] = new
    found["message"] = (f"This Moonglade folder was moved or renamed: {found['what']} was built in "
                        f"{found['old_repo']}, but the folder is now {new}. The server has its old folder built in, "
                        f"so it has to be rebuilt once (about 15 minutes): {MOVED_FIX}")
    return found


def build(clean=False, jobs=None, force_clean=False, ignore_power=False, resume="harness/hs build") -> dict:
    """`jobs` None = this Mac's performance cores (power.default_jobs; power-guard). The battery guard runs before
    each cmake step unless `ignore_power`; a power stop raises power.PowerStop (stamps handled like a failed step)."""
    from . import power
    jobs, jobs_why = power.resolve_jobs(jobs)
    info = {"jobs": {"n": jobs, "why": jobs_why}}
    moved = moved_problem()
    if moved:
        # F30: a build tree or install made in another folder can't be reused; wipe it before anything else
        log(moved["message"].split(" so it has")[0] + "; rebuilding from empty.")
        moved["stamps_dropped"] = _drop_stamps()
        if P.BUILD.exists():
            shutil.rmtree(P.BUILD)
        moved["build_dir_removed"] = not P.BUILD.exists()
        info["moved"] = moved
        force_clean = True
    info["sync"] = sync_vendor()
    manifest = input_manifest()
    h = input_hash(manifest)
    stamps = {"build": read_stamp(build_stamp_path()), "install": read_stamp(install_stamp_path())}
    if clean or force_clean:
        mode, reason = decide_mode(h, stamps["build"], stamps["install"], force_clean)
        if moved:
            reason = f"built in another folder ({moved['old_repo']})"
    else:
        mode, reason = "incremental", "plain `hs build`"
    info["clean"] = {"mode": mode, "reason": reason, "input_hash": h, "stamps_before": stamps,
                     "from_empty_build_done": mode == "from-empty"}
    log(f"build mode: {mode} ({reason}); input hash {h[:12]}")
    if mode == "from-empty":
        info["clean"]["stamps_dropped"] = _drop_stamps()
        if P.BUILD.exists():
            log(f"removing {P.BUILD}")
            shutil.rmtree(P.BUILD)
    P.BUILD.mkdir(exist_ok=True)
    if mode == "from-empty":
        info["clean"]["build_dir_entries_before_configure"] = sorted(os.listdir(P.BUILD))
        if info["clean"]["build_dir_entries_before_configure"]:
            raise BuildError(f"build dir not empty before a from-empty build: {P.BUILD}")
    before = installed_digest() if mode == "verified-current" else None
    if mode == "incremental" and (stamps["build"] != h or stamps["install"] != h):
        # an incremental build from inputs no passing from-empty build vouched for: the install stops matching
        # any stamp, so the next --clean must build from empty
        info["clean"]["stamps_dropped"] = _drop_stamps()
    P.OUT.mkdir(parents=True, exist_ok=True)
    blog = P.OUT / "build.log"
    try:
        info["power"] = _run_steps(blog, jobs, jobs_why, ignore_power, resume=resume)
    except (BuildError, power.PowerStop):
        if mode == "verified-current":
            _drop_stamps()     # a failed (or stopped) rebuild may have replaced installed files
        raise
    bins = {n: (P.SERVER_BIN / n) for n in ("authserver", "worldserver")}
    info["binaries"] = {n: str(p) for n, p in bins.items()}
    missing = [n for n, p in bins.items() if not (p.exists() and os.access(p, os.X_OK))]
    if missing:
        raise BuildError(f"missing binaries after install: {missing}")
    info["log"] = str(blog)
    info["install_record"] = write_install_record()
    c = info["clean"]
    if mode == "from-empty":
        _write_stamps(h, manifest)
        c["stamp_written"] = {"build": read_stamp(build_stamp_path()), "install": read_stamp(install_stamp_path())}
    elif mode == "verified-current":
        after = installed_digest()
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        c |= {"installed_digest_before": before, "installed_digest_after": after, "changed_files": changed,
              "installed_stamp": read_stamp(install_stamp_path()),
              "note": "no from-empty build was done: the install is proven current by its input-hash stamp "
                      "and a no-op incremental build"}
        if changed:
            c["stamps_dropped"] = _drop_stamps()   # the next --clean builds from empty
            raise BuildError(f"verified-current: the incremental build changed installed files {changed}; the "
                             f"install did not match the stamped inputs (stamps dropped; rerun, or use --force-clean)",
                             info)
        if c["installed_stamp"] != h:
            raise BuildError(f"verified-current: installed stamp {c['installed_stamp']} != input hash {h}", info)
    return info


def build_steps(jobs: int) -> list[tuple[str, list[str]]]:
    """(guard step name, command) of a server build, in order."""
    return [
        ("the configure step (cmake)", [str(s) for s in ["cmake", "-S", P.CORE_SRC, "-B", P.BUILD, *cmake_args()]]),
        ("the compile step (cmake --build)", [str(s) for s in ["cmake", "--build", P.BUILD, "-j", str(jobs)]]),
        ("the install step (cmake --install)", [str(s) for s in ["cmake", "--install", P.BUILD]]),
    ]


def _exec_step(step, fh) -> int:
    return subprocess.run(step, stdout=fh, stderr=subprocess.STDOUT).returncode


def run_guarded_steps(blog, steps, jobs: int, jobs_why: str, ignore_power: bool, what: str, resume: str,
                      execute=_exec_step) -> dict:
    """Write the job count and why to `blog`, then for each (name, command): the battery guard (power.Guard), then
    `execute(command, fh)`. Returns the guard's record (pauses with start, end, percents); a power stop raises
    power.PowerStop with that record as `.guard`; a failing step raises BuildError."""
    from . import power
    with open(blog, "w") as fh:
        def sink(m):
            fh.write(m + "\n")
            fh.flush()
        line = power.jobs_line(jobs, jobs_why)
        log(line)
        sink(line)
        guard = power.Guard(what, resume, ignore_power, say=log, sink=sink)
        try:
            for name, step in steps:
                guard.check(name)
                log("$ " + " ".join(step) + f"   (log: {blog})")
                fh.write("\n$ " + " ".join(step) + "\n"); fh.flush()
                rc = execute(step, fh)
                if rc != 0:
                    raise BuildError(f"build step failed (exit {rc}): {' '.join(step)}; see {blog}")
        except power.PowerStop as e:
            e.guard = guard.record()
            raise
        return guard.record()


def _run_steps(blog, jobs, jobs_why="", ignore_power=False, resume="harness/hs build"):
    return run_guarded_steps(blog, build_steps(jobs), jobs, jobs_why, ignore_power, "the server build", resume)


def _plan_exec(step, fh) -> int:
    fh.write("(plan only: not run)\n")
    fh.flush()
    return 0


def plan_only(jobs, ignore_power: bool, out_dir, resume="harness/hs build") -> dict:
    """power-guard test switch $HS_TEST_POWER_PLAN_ONLY: the build plan (job count, steps, guard) logged to
    <out_dir>/build.log, with nothing synced, configured, built or installed."""
    from . import power
    jobs, jobs_why = power.resolve_jobs(jobs)
    blog = os.path.join(out_dir, "build.log")
    steps = build_steps(jobs)
    out = {"plan_only": True, "jobs": {"n": jobs, "why": jobs_why}, "steps": [c for _n, c in steps], "log": blog}
    try:
        out["power"] = run_guarded_steps(blog, steps, jobs, jobs_why, ignore_power, "the server build", resume,
                                         execute=_plan_exec)
    except power.PowerStop as e:
        e.info = out | {"power": e.guard}
        raise
    return out
