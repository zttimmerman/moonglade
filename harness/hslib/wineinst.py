"""`hs wine-install`: fetch the pinned Wine runtime (fork.lock), verify its SHA-256, unpack it into env/ (ADR-0004).
Idempotent: a present runtime whose marker matches the pinned hash is left alone."""
import hashlib
import shutil
import subprocess
import tarfile
import urllib.request

from . import paths as P
from .util import log

RUNTIME_DIR = P.ENV / "wowsilicon-r17"
MARKER = RUNTIME_DIR / ".hs-runtime-sha256"


class WineInstallError(RuntimeError):
    pass


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install(force: bool = False) -> dict:
    lock = P.fork_lock()
    url, want = lock["wowsilicon_runtime_url"], lock["wowsilicon_runtime_sha256"]
    wine = RUNTIME_DIR / ".wine-runtime/bin/wine"
    if not force and wine.exists() and MARKER.exists() and MARKER.read_text().strip() == want:
        return {"runtime": str(RUNTIME_DIR), "installed": False, "reason": "already installed at pinned hash"}
    if not force and RUNTIME_DIR.exists():
        # Never adopt an unverified tree: the marker is written only after a SHA-256-verified download.
        raise WineInstallError(f"{RUNTIME_DIR} exists but isn't a verified install of the pinned runtime "
                               f"(marker missing or different); re-run with --force to reinstall")
    free_gb = shutil.disk_usage(P.ROOT).free / 2**30
    if free_gb < 1.0:
        raise WineInstallError(f"only {free_gb:.1f} GB free; need about 1 GB to download and unpack the runtime")
    tmp = P.ENV / "wowsilicon-runtime.tar.xz.part"
    log(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as fh:
        shutil.copyfileobj(r, fh)
    got = _sha256(tmp)
    if got != want:
        tmp.unlink()
        raise WineInstallError(f"SHA-256 mismatch: got {got}, fork.lock pins {want}; refusing to unpack")
    if RUNTIME_DIR.exists():
        shutil.rmtree(RUNTIME_DIR)
    RUNTIME_DIR.mkdir(parents=True)
    with tarfile.open(tmp, "r:xz") as tf:
        tf.extractall(RUNTIME_DIR, filter="tar")
    tmp.unlink()
    MARKER.write_text(want + "\n")
    ver = subprocess.run([str(wine), "--version"], capture_output=True, text=True).stdout.strip()
    if not ver:
        raise WineInstallError(f"unpacked runtime's wine doesn't run: {wine}")
    return {"runtime": str(RUNTIME_DIR), "installed": True, "sha256": got, "wine_version": ver}
