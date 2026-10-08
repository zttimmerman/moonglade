"""`hs client-install <zip-or-dir>`: install a 3.3.5a client into the repo's `client/`.

- The source (a zip or a client folder) is only ever opened read-only. It's never moved,
  renamed, modified, or deleted, and its size + mtime are recorded before and after.
- Zip members are streamed to disk in 1 MiB chunks (zipfile verifies each member's CRC-32
  as it's read). The zip's single top-level folder is stripped, so `client/Wow.exe` exists.
  Each file is written to `<name>.hs-part` and renamed into place only after a full, CRC-clean read.
- `env/client-manifest.json` records the SHA-256 of every `.exe`/`.dll`, the MPQ list, every
  file's size, the Wow.exe PE version, and the source stats. It's written last, so it exists only
  for a complete install.
- Idempotent: when `client/` already matches the manifest (and the source is unchanged), nothing
  is extracted again. Then `client-config` runs on `client/`.
No client binary is ever patched (CLAUDE.md §2); extraction writes them byte-for-byte.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import paths as P
from .pe import PEError, version_info
from .util import log

MIN_FREE_FULL = 19 * 10**9          # refuse a full extraction with fewer than 19 GB free
MARGIN = 1 * 10**9                  # headroom for partial repairs
CHUNK = 1 << 20
HASHED_SUFFIXES = (".exe", ".dll")
# Files `client-config` rewrites by design; their size is not part of the install match.
CONFIG_MANAGED = ("realmlist.wtf",)
EXPECTED_BUILD = "3.3.5.12340"


class ClientInstallError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def source_stat(src: Path) -> dict:
    """Size + mtime of the source. A directory's size is the sum of its files' sizes, and its
    mtime is the newest mtime in the tree (so any file change shows up)."""
    st = src.stat()
    if src.is_file():
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns,
                "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat()}
    size, newest, n = 0, st.st_mtime_ns, 0
    for p in src.rglob("*"):
        s = p.lstat()
        newest = max(newest, s.st_mtime_ns)
        if p.is_file():
            size += s.st_size
            n += 1
    return {"size": size, "mtime_ns": newest, "files": n,
            "mtime": datetime.fromtimestamp(newest / 1e9, timezone.utc).isoformat()}


def _is_hashed(rel: str) -> bool:
    return rel.lower().endswith(HASHED_SUFFIXES)


def _is_config_managed(rel: str) -> bool:
    return PurePosixPath(rel).name.lower() in CONFIG_MANAGED


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- source enumeration

class _ZipSource:
    kind = "zip"

    def __init__(self, path: Path):
        self.path = path
        try:
            self.z = zipfile.ZipFile(path, "r")
        except zipfile.BadZipFile as e:
            raise ClientInstallError(f"not a readable zip: {path}: {e}") from e
        infos = [i for i in self.z.infolist() if not i.is_dir()]
        tops = {i.filename.split("/", 1)[0] for i in self.z.infolist()}
        # Strip a single top-level folder (e.g. MyClient_3.3.5a/) if every member is under it.
        self.prefix = ""
        if len(tops) == 1 and all("/" in i.filename for i in infos):
            self.prefix = next(iter(tops)) + "/"
        self.members = {}
        for i in infos:
            rel = i.filename[len(self.prefix):]
            pp = PurePosixPath(rel)
            if pp.is_absolute() or ".." in pp.parts or not rel or "\\" in rel:
                raise ClientInstallError(f"unsafe zip member path: {i.filename!r}")
            self.members[rel] = i

    def size(self, rel): return self.members[rel].file_size

    def mtime(self, rel):
        return time.mktime(self.members[rel].date_time + (0, 0, -1))

    def open(self, rel): return self.z.open(self.members[rel], "r")

    def info(self) -> dict:
        all_infos = self.z.infolist()
        return {"zip_entries": len(all_infos),
                "zip_uncompressed_bytes": sum(i.file_size for i in all_infos),
                "stripped_prefix": self.prefix}

    def close(self): self.z.close()


class _DirSource:
    kind = "dir"

    def __init__(self, path: Path):
        self.path = path
        self.prefix = ""
        self.members = {}
        for p in sorted(path.rglob("*")):
            if p.is_file() and not p.is_symlink():
                self.members[p.relative_to(path).as_posix()] = p

    def size(self, rel): return self.members[rel].stat().st_size
    def mtime(self, rel): return self.members[rel].stat().st_mtime
    def open(self, rel): return self.members[rel].open("rb")
    def info(self) -> dict: return {"dir_files": len(self.members)}
    def close(self): pass


def _open_source(src: Path):
    if src.is_file():
        return _ZipSource(src)
    if src.is_dir():
        return _DirSource(src)
    raise ClientInstallError(f"source not found: {src}")


# ---------------------------------------------------------------- manifest + matching

def load_manifest(path: Path = P.CLIENT_MANIFEST) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None


def manifest_client_dir(client: Path) -> str:
    """The manifest's `client_dir`: relative to the repo when the install is inside it (normally "client"), so a
    moved or renamed repo folder doesn't leave a stale absolute path (B32: the field still named the
    pre-rename project folder); absolute only for an install outside the repo."""
    c = Path(os.path.abspath(client))
    try:
        return str(c.relative_to(Path(os.path.abspath(P.ROOT))))
    except ValueError:
        return str(c)


def heal_manifest_client_dir(manifest_path: Path = P.CLIENT_MANIFEST, client: Path = P.CLIENT) -> dict:
    """Rewrite only the manifest's `client_dir` field when it isn't manifest_client_dir(client) (an older absolute
    path). Reads and writes the manifest file only (env/), never anything in the install folder."""
    m = load_manifest(Path(manifest_path))
    if m is None:
        return {"healed": False, "reason": "no manifest"}
    want = manifest_client_dir(client)
    if m.get("client_dir") == want:
        return {"healed": False, "client_dir": want}
    old = m.get("client_dir")
    m["client_dir"] = want
    tmp = Path(manifest_path).with_name(Path(manifest_path).name + ".tmp")
    tmp.write_text(json.dumps(m, indent=2))
    os.replace(tmp, manifest_path)
    return {"healed": True, "from": old, "to": want}


def mismatches(client: Path, manifest: dict, hash_binaries: bool = True) -> list[dict]:
    """Differences between client/ and the manifest (missing files, wrong sizes, wrong hashes).
    Config-managed files (realmlist.wtf) are skipped; client-config rewrites them by design."""
    out = []
    for rel, rec in manifest["files"].items():
        if _is_config_managed(rel):
            continue
        p = client / rel
        if not p.is_file():
            out.append({"path": rel, "problem": "missing"})
            continue
        size = p.stat().st_size
        if size != rec["size"]:
            out.append({"path": rel, "problem": "size", "expected": rec["size"], "actual": size})
            continue
        if hash_binaries and "sha256" in rec:
            h = sha256_file(p)
            if h != rec["sha256"]:
                out.append({"path": rel, "problem": "sha256", "expected": rec["sha256"], "actual": h})
    return out


# ---------------------------------------------------------------- install

def _stream(srcobj, rel: str, dest: Path, want_hash: bool) -> str | None:
    """Stream one member to dest via a .hs-part file; returns its SHA-256 if asked."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".hs-part")
    h = hashlib.sha256() if want_hash else None
    try:
        with srcobj.open(rel) as fin, part.open("wb") as fout:
            while True:
                chunk = fin.read(CHUNK)          # zipfile raises BadZipFile on CRC mismatch at EOF
                if not chunk:
                    break
                if h:
                    h.update(chunk)
                fout.write(chunk)
        if part.stat().st_size != srcobj.size(rel):
            raise ClientInstallError(f"{rel}: wrote {part.stat().st_size} bytes, expected {srcobj.size(rel)}")
        mt = srcobj.mtime(rel)
        os.utime(part, (mt, mt))
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return h.hexdigest() if h else None


def install(src: Path, client: Path = P.CLIENT, manifest_path: Path = P.CLIENT_MANIFEST,
            run_config: bool = True) -> dict:
    """Install (or repair) the client. The install folder is write-protected at the file-system level (pristine.py):
    the protection is lifted for the install only and restored on the way out, also when the install fails; a killed
    install leaves it lifted, and the next install (or bootstrap) restores it."""
    from . import pristine
    client_p = Path(client)
    if client_p.is_symlink():
        raise ClientInstallError(f"the install folder {client_p} is a symlink; refusing (install into a real folder)")
    with pristine.unlocked(client_p):
        res = _install(src, client_p, Path(manifest_path), run_config)
    res["write_protection"] = {k: v for k, v in pristine.status(client_p).items() if k != "paths"}
    return res


def _install(src: Path, client: Path, manifest_path: Path, run_config: bool) -> dict:
    src = Path(src).expanduser().resolve()
    client = Path(client).resolve()
    if not src.exists():
        raise ClientInstallError(f"source not found: {src}")
    if src == client or client in src.parents or src in client.parents:
        raise ClientInstallError(f"source {src} overlaps the install dir {client}")

    t0 = time.time()
    before = source_stat(src)
    srcobj = _open_source(src)
    try:
        members = srcobj.members
        if not any(r.lower() == "wow.exe" for r in members):
            raise ClientInstallError(f"no Wow.exe at the top of {src} (after stripping "
                                     f"{srcobj.prefix!r}); not a 3.3.5a client")
        manifest = load_manifest(manifest_path)
        src_info = {"path": str(src), "kind": srcobj.kind, **srcobj.info()}

        # Idempotence: manifest present, same source, same member list, client/ matches it.
        if manifest and client.is_dir() and manifest.get("source", {}).get("path") == str(src) \
                and manifest["source"].get("before", {}).get("size") == before["size"] \
                and manifest["source"].get("before", {}).get("mtime_ns") == before["mtime_ns"] \
                and set(manifest.get("files", {})) == set(members):
            bad = mismatches(client, manifest)
            if not bad:
                log("client/ already matches env/client-manifest.json; skipping extraction")
                result = {"action": "skipped (client/ matches manifest)", "client_dir": str(client),
                          "manifest_client_dir": heal_manifest_client_dir(manifest_path, client),
                          "manifest": str(manifest_path), "source_now": before,
                          "wow_exe_version": manifest.get("wow_exe_version")}
                if run_config:
                    result["client_config"] = _configure(client)
                return result
            log(f"client/ differs from manifest in {len(bad)} file(s); repairing")

        # Decide which members need writing (missing / wrong size / wrong binary hash).
        todo = []
        for rel in members:
            p = client / rel
            if not p.is_file() or p.stat().st_size != srcobj.size(rel) or _is_config_managed(rel):
                todo.append(rel)
            elif _is_hashed(rel) and manifest and manifest.get("files", {}).get(rel, {}).get("sha256") \
                    != sha256_file(p):
                todo.append(rel)
            elif _is_hashed(rel) and not manifest:
                todo.append(rel)               # no trusted record of what's there: rewrite binaries
        full = len(todo) == len(members)
        to_write = sum(srcobj.size(r) for r in todo)
        free = shutil.disk_usage(P.ROOT).free
        # A full-size client (the 3.3.5a enUS client is ~17.9 GB) needs 19 GB free; smaller
        # writes (repairs, test trees) need what they write plus 1 GB of headroom.
        need = max(MIN_FREE_FULL, to_write + MARGIN) if full and to_write >= 10 * 10**9 else to_write + MARGIN
        if free < need:
            raise ClientInstallError(
                f"not enough free disk: {free / 1e9:.1f} GB free, need {need / 1e9:.1f} GB "
                f"({to_write / 1e9:.2f} GB to extract{'; a full-size client install needs at least 19 GB free' if need >= MIN_FREE_FULL else ' + 1 GB headroom'}). "
                f"Nothing was written.")
        log(f"extracting {len(todo)}/{len(members)} files ({to_write / 1e9:.2f} GB) from {src} → {client} "
            f"(free {free / 1e9:.1f} GB)")

        # Invalidate the old manifest first: a manifest only ever describes a complete install.
        if manifest_path.exists():
            manifest_path.unlink()
        client.mkdir(parents=True, exist_ok=True)
        # easy-setup (kill-safe): a SIGKILLed install leaves its in-progress <name>.hs-part behind; drop those first
        stale = [p for p in client.rglob("*.hs-part") if p.is_file()]
        for p in stale:
            p.unlink()
        if stale:
            log(f"removed {len(stale)} unfinished file(s) left by an interrupted install")
        done_bytes, last = 0, time.time()
        hashes: dict[str, str] = {}
        corrupt = []
        for i, rel in enumerate(sorted(todo, key=lambda r: srcobj.size(r))):
            try:
                h = _stream(srcobj, rel, client / rel, _is_hashed(rel))
            except (zipfile.BadZipFile, zlib.error, EOFError) as e:
                # Keep going so one pass finds every bad member; the bad file isn't left on disk.
                log(f"  CORRUPT in source: {rel}: {e}")
                corrupt.append({"member": srcobj.prefix + rel, "error": f"{type(e).__name__}: {e}"})
                continue
            if h:
                hashes[rel] = h
            done_bytes += srcobj.size(rel)
            if time.time() - last > 15:
                log(f"  {done_bytes / 1e9:.2f}/{to_write / 1e9:.2f} GB ({i + 1}/{len(todo)} files)")
                last = time.time()

        if corrupt:
            now = source_stat(src)
            log(f"source stat before {before['size']} bytes mtime_ns {before['mtime_ns']}; "
                f"after {now['size']} bytes mtime_ns {now['mtime_ns']} "
                f"({'unchanged' if (now['size'], now['mtime_ns']) == (before['size'], before['mtime_ns']) else 'CHANGED'})")
            raise ClientInstallError(
                f"{len(corrupt)} member(s) of {src} failed their zip CRC-32 check, so the source itself is "
                f"damaged (e.g. an incomplete copy): "
                + "; ".join(c["member"] for c in corrupt)
                + ". Every other member was extracted; no manifest was written, so client/ is not a valid "
                  "install. Repair or replace the source, then re-run `harness/hs client-install` and only "
                  "the missing files will be extracted.")

        files = {}
        for rel in sorted(members):
            rec = {"size": srcobj.size(rel)}
            if _is_hashed(rel):
                rec["sha256"] = hashes.get(rel) or sha256_file(client / rel)
            files[rel] = rec
    finally:
        srcobj.close()

    after = source_stat(src)
    if (after["size"], after["mtime_ns"]) != (before["size"], before["mtime_ns"]):
        raise ClientInstallError(f"source changed during install: before {before}, after {after}")

    wow = next(client / r for r in files if r.lower() == "wow.exe")
    try:
        ver = version_info(wow)
    except PEError as e:
        raise ClientInstallError(f"cannot read Wow.exe version resource: {e}") from e
    binaries = {r: v["sha256"] for r, v in files.items() if "sha256" in v}
    mpqs = sorted(r for r in files if r.lower().endswith(".mpq"))
    manifest = {
        "schema": 1,
        "installed_at": _now(),
        "client_dir": manifest_client_dir(client),
        "source": {**src_info, "before": before, "after": after},
        "wow_exe_version": {"file_version": ver["file_version"], "product_version": ver["product_version"],
                            "strings": ver["strings"]},
        "binaries_sha256": binaries,
        "mpqs": [{"path": r, "size": files[r]["size"]} for r in mpqs],
        "files": files,
        "totals": {"files": len(files), "bytes": sum(v["size"] for v in files.values())},
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = manifest_path.with_name(manifest_path.name + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    os.replace(tmp, manifest_path)
    result = {
        "action": "full install" if full else f"repaired {len(todo)} file(s)",
        "client_dir": str(client), "manifest": str(manifest_path),
        "files_written": len(todo), "bytes_written": to_write, "seconds": round(time.time() - t0, 1),
        "source": manifest["source"], "wow_exe_version": manifest["wow_exe_version"],
        "binaries_sha256": binaries, "mpq_count": len(mpqs),
        "disk_free_gb_after": round(shutil.disk_usage(P.ROOT).free / 1e9, 1),
        "version_ok": ver["file_version"] == EXPECTED_BUILD,
    }
    if ver["file_version"] != EXPECTED_BUILD:
        log(f"WARNING: Wow.exe file version is {ver['file_version']}, expected {EXPECTED_BUILD}")
    if run_config:
        result["client_config"] = _configure(client)
    return result


def _configure(client: Path) -> dict:
    from .clientcfg import configure
    r = configure(client)
    return {"changed_files": r["changed_files"], "wow_exe_unchanged": r["wow_exe"]["unchanged"]}
