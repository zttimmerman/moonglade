"""The pristine client (`client/`) can't be written (easy-setup, play-client "The pristine client can't be written").

Four layers (design.md "As built", pristine-client protection):
1. OS write-protection. `lock()` removes every write bit (`chmod a-w`) and sets the user-immutable flag
   (`chflags uchg`) on every file and folder of the install, the folder itself included. Measured (2026-10-07, APFS,
   macOS 26.6): for the owner, overwrite, `>`/`>>`, rename, delete, create, `rm -rf`, `os.replace` onto a file, a write
   through a symlinked path and even `chmod u+w` all fail (EPERM). Only `chflags nouchg` lifts it, which only
   `unlocked()` does, inside `hs client-install` (bootstrap's client step runs that command).
   Kept writable: the game's own state folders (STATE_DIRS: stock mode runs Wow.exe from client/, and the game writes
   its settings, cache, logs and screenshots there; created empty by `lock()` when missing, since the locked folder
   can't get new entries) and mtld3d's shader cache file. None of them is part of the install record.
   An APFS clone (`cp -cR`, what clientbuild uses) inherits the flag on files and the read-only folders, so
   clientbuild clears both on its clone (`make_writable`), which never touches the source's inodes.
2. Writer guards by identity: `inside()` (moved here from mpq._under_client) decides by st_dev/st_ino whether a path
   is, or lies inside, the pristine client (symlinks, `..` and case variants included). Every play-client writer
   calls `refuse_inside()` before writing.
3. Per-run fingerprint: `fingerprint()` at the start and end of every harness run; `report.client_pristine` compares.
4. The agent hook (.claude/hooks/guard-client.sh) is separate (no Python import).

Test switch: $HS_TEST_PRISTINE_CLIENT names a scratch install that stands in for client/ as the root the run
fingerprints (report.client_pristine's self-test). Honoured only inside a harness run ($HS_HARNESS_RUN, set by
`hs run`); anywhere else it raises (./play refuses it like its other test switches). It never weakens a guard: the
writer guards protect the real client/ AND the stand-in.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import stat
from pathlib import Path

from . import paths as P

TEST_ENV = "HS_TEST_PRISTINE_CLIENT"
STATE_DIRS = ("WTF", "Cache", "Logs", "Errors", "Screenshots")   # the game's own state (stock mode writes them)
WRITABLE_FILES = ("mtld3d_shaders.bin",)                        # mtld3d's shader cache (stock mode, cwd = client/)
WRITE_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


class PristineError(RuntimeError):
    pass


# ---------------------------------------------------------------- which folder is pristine

def test_root(environ=None) -> Path | None:
    """The scratch stand-in for client/ ($HS_TEST_PRISTINE_CLIENT), only inside a harness run."""
    env = os.environ if environ is None else environ
    v = env.get(TEST_ENV)
    if not v:
        return None
    if env.get("HS_HARNESS_RUN") != "1":
        raise PristineError(f"${TEST_ENV} is a harness test switch; it is refused outside `harness/hs run`")
    if inside(v, P.CLIENT) or inside(P.CLIENT, v):
        raise PristineError(f"${TEST_ENV} must name a scratch install, never client/ or a folder holding it: {v}")
    return Path(v)


def run_root(environ=None) -> Path:
    """The folder this run fingerprints: client/, or the test stand-in."""
    return test_root(environ) or P.CLIENT


def roots(environ=None) -> list[Path]:
    """Every folder the writer guards protect: client/ always, plus the test stand-in when set."""
    out = [P.CLIENT]
    try:
        t = test_root(environ)
    except PristineError:
        t = None
    if t is not None:
        out.append(t)
    return out


# ---------------------------------------------------------------- identity

def inside(p, client_root=None) -> bool:
    """True if `p` (existing or not) is `client_root` (default: client/) or would land inside it.

    Decided by file-system identity, not by comparing strings: APFS is case-insensitive here, so `CLIENT/` or
    `Client/` name the same folder, and realpath doesn't normalise case. Every existing ancestor of the path (both the
    symlink-resolved path and the lexical absolute path, so `..` and symlinks are covered) is compared with the root
    by st_dev/st_ino. If the root doesn't exist, fall back to a case-folded path comparison."""
    root = Path(client_root) if client_root is not None else P.CLIENT
    try:
        rst = os.stat(root)
    except OSError:
        rst = None
    cands = {Path(os.path.realpath(p)), Path(os.path.abspath(p))}
    for c in cands:
        for anc in (c, *c.parents):
            if rst is None:
                for r in {os.path.realpath(root), os.path.abspath(root)}:
                    if os.path.normcase(str(anc)).casefold() == os.path.normcase(r).casefold():
                        return True
                continue
            try:
                st = os.stat(anc)
            except OSError:
                continue                       # not created yet: walk up to the nearest existing parent
            if (st.st_dev, st.st_ino) == (rst.st_dev, rst.st_ino):
                return True
    return False


def protected_root_of(p, extra=()) -> Path | None:
    """The protected folder `p` is (inside), or None. Checks client/, the test stand-in and `extra` roots."""
    for r in [*roots(), *extra]:
        if inside(p, r):
            return Path(r)
    return None


def refuse_inside(p, what: str, extra=(), exc=PristineError) -> None:
    """Raise `exc` naming the pristine client when `p` is, or lies inside, a protected folder."""
    r = protected_root_of(p, extra)
    if r is not None:
        raise exc(f"refusing to {what} {p}: it is inside the pristine client {r} (resolved by file identity); "
                  "client/ is never written; play-client files go to a play client (env/client-play/ or a scratch)")


# ---------------------------------------------------------------- the OS lock

def _exempt(rel: Path) -> bool:
    parts = rel.parts
    if not parts:
        return False
    if parts[0] in STATE_DIRS:
        return True
    return len(parts) == 1 and parts[0] in WRITABLE_FILES


def _walk(root: Path):
    """(path, rel) for everything under root and root itself, children before their folder (so a folder is locked
    after its entries and unlocked before them), the client folder last. The state folders' contents are skipped
    (the state folders themselves are yielded; the caller exempts them)."""
    root = Path(root)
    stack, order = [root], []
    while stack:                                   # pre-order, then reversed = every child before its folder
        d = stack.pop()
        order.append(d)
        rel_d = d.relative_to(root)
        if rel_d.parts and rel_d.parts[0] in STATE_DIRS:
            continue
        with os.scandir(d) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                else:
                    order.append(Path(e.path))
    for p in reversed(order):
        yield p, p.relative_to(root)


def _set(p: Path, lock: bool) -> None:
    st = os.lstat(p)
    if stat.S_ISLNK(st.st_mode):
        flags = (st.st_flags | stat.UF_IMMUTABLE) if lock else (st.st_flags & ~stat.UF_IMMUTABLE)
        if flags != st.st_flags:
            os.chflags(p, flags, follow_symlinks=False)
        return
    if lock:
        if st.st_mode & WRITE_BITS:
            os.chmod(p, stat.S_IMODE(st.st_mode) & ~WRITE_BITS)
        if not st.st_flags & stat.UF_IMMUTABLE:
            os.chflags(p, st.st_flags | stat.UF_IMMUTABLE)
    else:
        if st.st_flags & stat.UF_IMMUTABLE:
            os.chflags(p, st.st_flags & ~stat.UF_IMMUTABLE)
        if not st.st_mode & stat.S_IWUSR:
            os.chmod(p, stat.S_IMODE(st.st_mode) | stat.S_IWUSR)


def lock(root: Path) -> dict:
    """Write-protect an installed client (everything but the game's state folders and mtld3d's cache file)."""
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise PristineError(f"not a client folder: {root}")
    made = [d for d in STATE_DIRS if not (root / d).exists()]
    if made:                                  # the game writes these in stock mode; a locked folder can't get them later
        _set(root, False)                     # (root is locked again last, below)
        for d in made:
            (root / d).mkdir()
    n = 0
    # files and folders bottom-up, the client folder itself last
    for p, rel in _walk(root):
        if _exempt(rel):
            continue
        _set(p, True)
        n += 1
    return {"locked": n, "state_dirs_created": made}


def unlock(root: Path) -> dict:
    """Lift the protection (only `unlocked()` calls this, and tests on scratch installs)."""
    root = Path(root)
    if not root.exists():
        return {"unlocked": 0}
    n = 0
    _set(root, False)                         # folders first (top-down), so their entries can be changed
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        d = Path(dirpath)
        for name in [*dirnames, *filenames]:
            p = d / name
            st = os.lstat(p)
            if st.st_flags & stat.UF_IMMUTABLE or (not stat.S_ISLNK(st.st_mode) and not st.st_mode & stat.S_IWUSR):
                _set(p, False)
                n += 1
    return {"unlocked": n}


def status(root: Path, sample: int = 5) -> dict:
    """{"locked": bool, "paths": n checked, "writable": [first few unprotected paths]} (read-only)."""
    root = Path(root)
    if not root.is_dir():
        return {"locked": False, "paths": 0, "writable": [], "why": f"{root} is not a folder"}
    bad, n = [], 0
    for p, rel in _walk(root):
        if _exempt(rel):
            continue
        n += 1
        st = os.lstat(p)
        ok = bool(st.st_flags & stat.UF_IMMUTABLE) and (stat.S_ISLNK(st.st_mode) or not st.st_mode & WRITE_BITS)
        if not ok:
            bad.append(str(rel) or ".")
    return {"locked": not bad, "paths": n, "writable": bad[:sample], "writable_count": len(bad)}


@contextlib.contextmanager
def unlocked(root: Path):
    """Lift the protection for a deliberate install, and restore it on the way out (even when the install fails).
    A SIGKILL can't run the finally: the next `hs client-install` or bootstrap re-locks, and bootstrap's client
    step reports an unlocked client as not done."""
    root = Path(root)
    existed = root.exists()
    if existed:
        unlock(root)
    try:
        yield
    finally:
        if root.is_dir():
            lock(root)


def remove_tree(p: Path) -> None:
    """Delete a scratch tree that may hold a locked (scratch) install: lift the protection first."""
    import shutil
    p = Path(p)
    if not p.exists():
        return
    if inside(p, P.CLIENT) or inside(P.CLIENT, p):
        raise PristineError(f"refusing to delete {p}: it is, or holds, the pristine client {P.CLIENT}")
    unlock(p)
    shutil.rmtree(p, ignore_errors=True)


def make_writable(clone: Path) -> dict:
    """A play-client clone (`cp -cR` of client/) inherits the lock; give it back to its owner. The clone's files are
    new inodes, so this never touches the pristine client (refused if `clone` is inside it)."""
    refuse_inside(clone, "make writable")
    return unlock(clone)


# ---------------------------------------------------------------- per-run fingerprint

def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(root: Path | None = None, manifest: dict | None = None) -> dict:
    """client/ at one moment: the folder's identity, every file's (outside the state folders) inode, size, mtime and
    flags, the .exe/.dll SHA-256s, the install-record comparison (clientinst.mismatches) and the lock state."""
    from .clientinst import load_manifest, mismatches
    root = Path(root) if root is not None else run_root()
    if not root.is_dir():
        return {"root": str(root), "exists": False}
    # the install record describes client/; a scratch stand-in is compared by identity only
    man = manifest if manifest is not None else (load_manifest() if inside(root, P.CLIENT) and inside(P.CLIENT, root)
                                                 else None)
    files = {}
    for p, rel in _walk(root):
        if _exempt(rel) or p == root:
            continue
        st = os.lstat(p)
        rec = {"ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "flags": st.st_flags,
               "mode": stat.S_IMODE(st.st_mode), "kind": "dir" if stat.S_ISDIR(st.st_mode) else
               ("link" if stat.S_ISLNK(st.st_mode) else "file")}
        if rec["kind"] == "file" and rel.suffix.lower() in (".exe", ".dll"):
            rec["sha256"] = _sha(p)
        files[rel.as_posix()] = rec
    rst = os.stat(root)
    return {"root": str(root), "exists": True, "dir_ino": rst.st_ino, "dir_dev": rst.st_dev,
            "files": files, "manifest_mismatches": mismatches(root, man) if man else None,
            "manifest": bool(man), "lock": {k: v for k, v in status(root).items() if k != "paths"}}


def diff(a: dict, b: dict) -> list[dict]:
    """What changed between two fingerprints of the same root (empty = unchanged)."""
    out = []
    if not a or not b or "error" in a or "error" in b:
        return [{"path": ".", "change": "fingerprint missing or unreadable", "start": a and a.get("error"),
                 "end": b and b.get("error")}]
    if a.get("exists") != b.get("exists") or a.get("dir_ino") != b.get("dir_ino"):
        out.append({"path": ".", "change": "the client folder itself was replaced or removed",
                    "start": a.get("dir_ino"), "end": b.get("dir_ino")})
    fa, fb = a.get("files") or {}, b.get("files") or {}
    for rel in sorted(set(fa) | set(fb)):
        x, y = fa.get(rel), fb.get(rel)
        if x is None:
            out.append({"path": rel, "change": "added"})
        elif y is None:
            out.append({"path": rel, "change": "removed"})
        elif x != y:
            out.append({"path": rel, "change": "changed",
                        "fields": sorted(k for k in set(x) | set(y) if x.get(k) != y.get(k))})
    if (a.get("manifest_mismatches") or []) != (b.get("manifest_mismatches") or []):
        out.append({"path": ".", "change": "install-record comparison changed",
                    "start": a.get("manifest_mismatches"), "end": b.get("manifest_mismatches")})
    if (a.get("lock") or {}).get("locked") != (b.get("lock") or {}).get("locked"):
        out.append({"path": ".", "change": "write protection changed",
                    "start": (a.get("lock") or {}).get("locked"), "end": (b.get("lock") or {}).get("locked")})
    return out
