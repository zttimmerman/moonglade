"""Pinned downloads (openspec/changes/easy-setup, server-build "Every download is pinned by hash").

Every file setup downloads is pinned in fork.lock by URL and SHA-256 (PINS below) and verified before use. A file whose
SHA-256 doesn't match is refused and removed, and nothing is unpacked from it.

Download cache: $HS_DOWNLOAD_CACHE names a folder of pinned files, looked up by the URL's file name. A cached file is
verified exactly like a fresh download (a mismatch is refused and removed), so the folder is safe to point anywhere:
it's honoured outside the harness too (the repeat-setup case the design names). A verified download is also kept
there when the variable is set. Without it, files go to data/downloads/ and are removed after unpacking (except the
VMaNGOS zip, which tools/era-diff reads from there).

`harness/hs download <name>` fetches one pin (and unpacks server data: `hs download client-data`); bootstrap's
"server data" step is install_client_data().
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path

from . import paths as P
from .util import log

CACHE_ENV = "HS_DOWNLOAD_CACHE"

# name -> (fork.lock url key, fork.lock sha256 key, where a non-cached download is kept, keep it after use?)
PINS = {
    "client-data": ("client_data_url", "client_data_sha256", P.DOWNLOADS / "Data.zip", False),
    "vmangos": ("vmangos_db_url", "vmangos_db_sha256", P.DOWNLOADS / "vmangos-db-sqlite.zip", True),
    "wine-runtime": ("wowsilicon_runtime_url", "wowsilicon_runtime_sha256",
                     P.ENV / "wowsilicon-runtime.tar.xz", False),
    "ghidra": ("ghidra_url", "ghidra_sha256", P.ENV / "ghidra.zip", False),
}
# fork.lock *_url keys that are git remotes (pinned by commit SHA instead of a file hash)
GIT_URL_KEYS = ("core_repo", "playerbots_repo")

CLIENT_DATA = P.CLIENT_DATA
CLIENT_DATA_MARKER = CLIENT_DATA / ".hs-client-data.json"
CLIENT_DATA_DIRS = ("Cameras", "dbc", "maps", "mmaps", "vmaps")


class DownloadError(RuntimeError):
    def __init__(self, msg: str, evidence: dict | None = None):
        super().__init__(msg)
        self.evidence = evidence or {}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pin(name: str, lock: dict | None = None) -> dict:
    """{name, url, sha256, file (URL basename), keep_path, keep} for one pinned download; raises when unpinned."""
    if name not in PINS:
        raise DownloadError(f"unknown download {name!r}; known: {sorted(PINS)}")
    lock = lock or P.fork_lock()
    ukey, hkey, keep_path, keep = PINS[name]
    url, want = lock.get(ukey), lock.get(hkey)
    if not url:
        raise DownloadError(f"fork.lock has no {ukey}")
    if not (want and len(want) == 64 and all(c in "0123456789abcdef" for c in want)):
        raise DownloadError(f"fork.lock pins no SHA-256 for {ukey} ({hkey}={want!r}); refusing to download unverified")
    return {"name": name, "url": url, "sha256": want, "file": url.rsplit("/", 1)[-1], "keep_path": keep_path,
            "keep": keep, "url_key": ukey, "sha256_key": hkey}


def cache_dir() -> Path | None:
    v = os.environ.get(CACHE_ENV)
    return Path(v).expanduser().resolve() if v else None


def _verify_or_remove(path: Path, want: str, what: str) -> str:
    got = sha256_file(path)
    if got != want:
        path.unlink(missing_ok=True)
        raise DownloadError(f"{what}: SHA-256 mismatch (got {got}, fork.lock pins {want}); the file was removed and "
                            "nothing was unpacked. Run the command again to download a fresh copy.",
                            {"file": str(path), "got": got, "want": want, "removed": not path.exists()})
    return got


def _download(url: str, dest: Path, timeout: int = 300) -> None:
    """URL -> dest via dest.part (renamed into place only when complete)."""
    part = dest.with_name(dest.name + ".part")
    part.unlink(missing_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"downloading {url}")
    t0, n = time.time(), 0
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r, open(part, "wb") as fh:
            last = t0
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                fh.write(b)
                n += len(b)
                if time.time() - last > 15:
                    log(f"  {n / 1e6:.0f} MB")
                    last = time.time()
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dest)
    log(f"downloaded {n / 1e6:.0f} MB in {time.time() - t0:.0f} s")


def obtain(name: str, lock: dict | None = None, cache: Path | None = None) -> dict:
    """A verified local copy of pin `name`. Returns {path, from: cache|kept|downloaded, sha256, delete_after}.
    Looks in $HS_DOWNLOAD_CACHE (or `cache`), then at the keep path (a verified earlier download), else downloads.
    Any file that fails its hash is refused and removed (DownloadError)."""
    p = pin(name, lock)
    cache = Path(cache).resolve() if cache else cache_dir()
    ev = {"name": name, "url": p["url"], "sha256": p["sha256"], "cache": str(cache) if cache else None}
    if cache:
        c = cache / p["file"]
        if c.is_file():
            _verify_or_remove(c, p["sha256"], f"cached {p['file']}")
            return ev | {"path": c, "from": "cache", "delete_after": False}
        cache.mkdir(parents=True, exist_ok=True)
        _download(p["url"], c)
        _verify_or_remove(c, p["sha256"], f"downloaded {p['file']}")
        return ev | {"path": c, "from": "downloaded (kept in the cache)", "delete_after": False}
    k = p["keep_path"]
    if k.is_file():
        _verify_or_remove(k, p["sha256"], str(k.relative_to(P.ROOT)))
        return ev | {"path": k, "from": "kept", "delete_after": not p["keep"]}
    _download(p["url"], k)
    _verify_or_remove(k, p["sha256"], f"downloaded {p['file']}")
    return ev | {"path": k, "from": "downloaded", "delete_after": not p["keep"]}


# ---------------------------------------------------------------- server data (client-data, ADR-0003)

def _tree_stats(root: Path) -> dict:
    files, size = 0, 0
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            if n.startswith(".hs-"):
                continue
            files += 1
            size += os.lstat(os.path.join(dirpath, n)).st_size
    return {"files": files, "bytes": size}


def client_data_state(lock: dict | None = None) -> dict:
    """Is data/client-data/ a complete unpack of the pinned zip? Not a marker alone: the marker (written after a
    complete unpack) must name the pinned SHA-256, the five folders must exist, and the file count and total size on
    disk must equal what the unpack recorded."""
    want = pin("client-data", lock)["sha256"]
    st = {"dir": str(CLIENT_DATA), "pinned_sha256": want}
    try:
        m = json.loads(CLIENT_DATA_MARKER.read_text())
    except (OSError, ValueError):
        return st | {"done": False, "why": "no record of a complete unpack"}
    if m.get("sha256") != want:
        return st | {"done": False, "why": f"unpacked from another zip ({str(m.get('sha256'))[:12]})"}
    missing = [d for d in CLIENT_DATA_DIRS if not (CLIENT_DATA / d).is_dir()]
    if missing:
        return st | {"done": False, "why": f"missing folders {missing}"}
    now = _tree_stats(CLIENT_DATA)
    if now != {"files": m.get("files"), "bytes": m.get("bytes")}:
        return st | {"done": False, "why": f"contents differ from the unpack ({now} vs {m.get('files')} files)"}
    return st | {"done": True, **now}


def install_client_data(lock: dict | None = None) -> dict:
    """Download (or take from the cache), verify, and unpack the pinned server data into data/client-data/. The zip
    is unpacked into a temp folder next to it and renamed into place only when complete."""
    s = client_data_state(lock)
    if s["done"]:
        return s | {"installed": False, "reason": "already unpacked from the pinned zip"}
    free = shutil.disk_usage(P.ROOT).free
    if free < 5 * 10**9:
        raise DownloadError(f"not enough free disk for the server data: {free / 1e9:.1f} GB free, need about 5 GB")
    got = obtain("client-data", lock)
    hand_made = CLIENT_DATA.exists() and not CLIENT_DATA_MARKER.exists() and any(CLIENT_DATA.iterdir())
    if hand_made:
        # an unpack made by hand (the old manual setup): adopt it when every member of the verified zip is on disk
        # with its size; otherwise move it aside (never deleted) and unpack fresh
        with zipfile.ZipFile(got["path"]) as z:
            members = [i for i in z.infolist() if not i.is_dir()]
        bad = [i.filename for i in members
               if not (CLIENT_DATA / i.filename).is_file() or (CLIENT_DATA / i.filename).stat().st_size != i.file_size]
        if not bad:
            stats = _tree_stats(CLIENT_DATA)
            CLIENT_DATA_MARKER.write_text(json.dumps(
                {"sha256": got["sha256"], "url": got["url"], **stats, "adopted": f"{len(members)} members matched "
                 "by size", "unpacked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2) + "\n")
            if got["delete_after"]:
                Path(got["path"]).unlink(missing_ok=True)
            return client_data_state(lock) | {"installed": False, "reason": "adopted an existing unpack that matches "
                                                                            "the pinned zip"}
        aside = CLIENT_DATA.with_name(f"client-data.old-{time.strftime('%Y%m%d-%H%M%S')}")
        log(f"{CLIENT_DATA} doesn't match the pinned zip ({len(bad)} files differ); moving it aside to {aside}")
        CLIENT_DATA.rename(aside)
    tmp = CLIENT_DATA.with_name("client-data.unpack")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        log(f"unpacking {got['path'].name} into {CLIENT_DATA}")
        r = subprocess.run(["/usr/bin/unzip", "-q", "-o", str(got["path"]), "-d", str(tmp)], capture_output=True,
                           text=True)
        if r.returncode != 0:
            raise DownloadError(f"unzip failed (rc {r.returncode}): {(r.stderr or r.stdout).strip()[-500:]}")
        missing = [d for d in CLIENT_DATA_DIRS if not (tmp / d).is_dir()]
        if missing:
            raise DownloadError(f"the server data zip has no {missing} folders at its top level")
        stats = _tree_stats(tmp)
        (tmp / CLIENT_DATA_MARKER.name).write_text(json.dumps(
            {"sha256": got["sha256"], "url": got["url"], **stats,
             "unpacked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2) + "\n")
        shutil.rmtree(CLIENT_DATA, ignore_errors=True)
        tmp.rename(CLIENT_DATA)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if got["delete_after"]:
        Path(got["path"]).unlink(missing_ok=True)
    return client_data_state(lock) | {"installed": True, "zip_from": got["from"]}


def cli(name: str) -> int:
    from .util import log as _log
    try:
        if name == "client-data":
            res = install_client_data()
        else:
            got = obtain(name)
            if got["delete_after"]:          # `hs download` keeps what it fetched: that's what was asked for
                got["delete_after"] = False
            res = {k: str(v) for k, v in got.items()}
    except DownloadError as e:
        _log(f"DOWNLOAD FAILED: {e}")
        print(json.dumps(e.evidence, indent=2, default=str))
        return 1
    print(json.dumps(res, indent=2, default=str))
    return 0
