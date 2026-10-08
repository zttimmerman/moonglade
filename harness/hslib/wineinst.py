"""`hs wine-install`: fetch the pinned Wine runtime (fork.lock), verify its SHA-256, unpack it into env/ (ADR-0004).
Idempotent: a present runtime whose marker matches the pinned hash is left alone."""
import hashlib
import shutil
import subprocess
import tarfile

from . import paths as P
from .util import log

RUNTIME_DIR = P.WINE_RUNTIME
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
    free_gb = shutil.disk_usage(P.HOME).free / 2**30
    if free_gb < 1.0:
        raise WineInstallError(f"only {free_gb:.1f} GB free; need about 1 GB to download and unpack the runtime")
    from .downloads import DownloadError, obtain
    try:
        got = obtain("wine-runtime", lock)        # pinned + SHA-256-verified (a mismatch is removed, never unpacked)
    except DownloadError as e:
        raise WineInstallError(str(e)) from None
    tmp = got["path"]
    # unpack next to the destination and rename into place when complete: a killed install leaves only the temp
    # folder, which the next run removes
    unpack = RUNTIME_DIR.with_name(RUNTIME_DIR.name + ".unpack")
    shutil.rmtree(unpack, ignore_errors=True)
    unpack.mkdir(parents=True)
    try:
        with tarfile.open(tmp, "r:xz") as tf:
            tf.extractall(unpack, filter="tar")
        (unpack / MARKER.name).write_text(want + "\n")
        if RUNTIME_DIR.exists():
            shutil.rmtree(RUNTIME_DIR)
        unpack.rename(RUNTIME_DIR)
    except BaseException:
        shutil.rmtree(unpack, ignore_errors=True)
        raise
    if got["delete_after"]:
        tmp.unlink(missing_ok=True)
    got = got["sha256"]
    ver = subprocess.run([str(wine), "--version"], capture_output=True, text=True).stdout.strip()
    if not ver:
        raise WineInstallError(f"unpacked runtime's wine doesn't run: {wine}")
    return {"runtime": str(RUNTIME_DIR), "installed": True, "sha256": got, "wine_version": ver}
