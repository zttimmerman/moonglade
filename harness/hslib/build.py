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
    if not (repo_dir / ".git").exists():
        P.VENDOR.mkdir(exist_ok=True)
        run(["git", "clone", "--branch", branch, url, repo_dir])
    if _git(repo_dir, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
        _git(repo_dir, "fetch", "origin", capture=False)


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


def _expected_links() -> dict:
    links = {"mod-playerbots": P.BOTS_SRC}
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


def sync_vendor() -> dict:
    """Force vendor trees to the pinned state, apply core patches, link modules."""
    lock = P.fork_lock()
    _ensure_clone(P.CORE_SRC, lock["core_repo"], lock["core_branch"], lock["core_sha"])
    _ensure_clone(P.BOTS_SRC, lock["playerbots_repo"], lock["playerbots_branch"], lock["playerbots_sha"])

    before = {
        "core": pin_state(P.CORE_SRC, lock["core_sha"], "modules/"),
        "playerbots": pin_state(P.BOTS_SRC, lock["playerbots_sha"]),
    }
    before["core_stray_modules"] = stray_module_entries()
    for repo, sha in ((P.CORE_SRC, lock["core_sha"]), (P.BOTS_SRC, lock["playerbots_sha"])):
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

    patches = sorted(P.PATCHES.glob("*.patch"))
    for p in patches:
        r = _git(P.CORE_SRC, "apply", "--index", "--whitespace=nowarn", str(p), check=False)
        if r.returncode != 0:
            raise BuildError(f"core patch failed to apply: {p.name}\n{r.stdout}")

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
        "playerbots_ok": _head(P.BOTS_SRC) == lock["playerbots_sha"] and not pin_state(P.BOTS_SRC, lock["playerbots_sha"]),
    }
    if patches:
        # with patches applied, the tree differs from HEAD by exactly the patches' index state
        after["patches_applied"] = [p.name for p in patches]
    elif pin_state(P.CORE_SRC, lock["core_sha"], "modules/"):
        raise BuildError("core tree still dirty after sync: " + "; ".join(pin_state(P.CORE_SRC, lock["core_sha"], "modules/")))
    after["core_stray_modules"] = stray_module_entries()
    if after["core_stray_modules"]:
        raise BuildError(f"stray entries remain in core modules/: {after['core_stray_modules']}")
    if not (after["core_ok"] and after["playerbots_ok"]):
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


def input_manifest(*, fork_lock_path=None, patches_dir=None, modules_dir=None, cmake=None, compiler=None) -> dict:
    """Everything a build's output depends on that the harness controls. Keyword overrides exist so checks
    can show a changed input changes the hash without touching the real inputs (sandbox copies)."""
    fl = fork_lock_path or (P.ROOT / "fork.lock")
    return {
        "fork_lock": _sha256_file(fl),
        "patches_core": tree_digest(patches_dir or P.PATCHES),
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


def build(clean=False, jobs=8, force_clean=False) -> dict:
    info = {"sync": sync_vendor()}
    manifest = input_manifest()
    h = input_hash(manifest)
    stamps = {"build": read_stamp(build_stamp_path()), "install": read_stamp(install_stamp_path())}
    if clean or force_clean:
        mode, reason = decide_mode(h, stamps["build"], stamps["install"], force_clean)
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
        _run_steps(blog, jobs)
    except BuildError:
        if mode == "verified-current":
            _drop_stamps()     # a failed rebuild may have replaced installed files
        raise
    bins = {n: (P.SERVER_BIN / n) for n in ("authserver", "worldserver")}
    info["binaries"] = {n: str(p) for n, p in bins.items()}
    missing = [n for n, p in bins.items() if not (p.exists() and os.access(p, os.X_OK))]
    if missing:
        raise BuildError(f"missing binaries after install: {missing}")
    info["log"] = str(blog)
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


def _run_steps(blog, jobs):
    with open(blog, "w") as fh:
        for step in (
            ["cmake", "-S", P.CORE_SRC, "-B", P.BUILD, *cmake_args()],
            ["cmake", "--build", P.BUILD, "-j", str(jobs)],
            ["cmake", "--install", P.BUILD],
        ):
            step = [str(s) for s in step]
            log("$ " + " ".join(step) + f"   (log: {blog})")
            fh.write("\n$ " + " ".join(step) + "\n"); fh.flush()
            r = subprocess.run(step, stdout=fh, stderr=subprocess.STDOUT)
            if r.returncode != 0:
                raise BuildError(f"build step failed (exit {r.returncode}): {' '.join(step)}; see {blog}")

