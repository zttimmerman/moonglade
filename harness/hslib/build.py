"""Vendor sync (pin + patches + module links) and cmake build/install. ADR-0001."""
import os
import shutil
import subprocess

from . import paths as P
from .util import log, run


class BuildError(RuntimeError):
    pass


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


def build(clean=False, jobs=8) -> dict:
    info = {"sync": sync_vendor()}
    if clean and P.BUILD.exists():
        log(f"removing {P.BUILD}")
        shutil.rmtree(P.BUILD)
    P.BUILD.mkdir(exist_ok=True)
    P.OUT.mkdir(parents=True, exist_ok=True)
    blog = P.OUT / "build.log"
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
    bins = {n: (P.SERVER_BIN / n) for n in ("authserver", "worldserver")}
    info["binaries"] = {n: str(p) for n, p in bins.items()}
    missing = [n for n, p in bins.items() if not (p.exists() and os.access(p, os.X_OK))]
    if missing:
        raise BuildError(f"missing binaries after install: {missing}")
    info["log"] = str(blog)
    return info
