"""Read-only client analysis with Ghidra (ADR-0005). Binaries are copied into env/re/bin and analyzed there;
nothing under client/ is ever opened for writing. `hs ghidra-install` pins the tool via fork.lock."""
import hashlib
import os
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path

from . import paths as P
from .util import log

# Ghidra rejects project paths containing a '.'-prefixed element (e.g. a worktree under .claude/); HS_RE_DIR overrides.
RE = Path(os.environ.get("HS_RE_DIR", P.ENV / "re")).resolve()
RE_BIN = RE / "bin"
RE_OUT = RE / "out"
SCRIPTS = P.HARNESS / "ghidra_scripts"
JAVA_HOME = P.BREW / "opt/openjdk@21/libexec/openjdk.jdk/Contents/Home"


class GhidraError(RuntimeError):
    pass


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ghidra_home() -> Path:
    return P.ENV / f"ghidra_{P.fork_lock()['ghidra_version']}_PUBLIC"


def java_info() -> dict:
    java = JAVA_HOME / "bin/java"
    if not java.exists():
        raise GhidraError(f"JDK 21 not found at {JAVA_HOME}; run `brew install openjdk@21`")
    r = subprocess.run([str(java), "-version"], capture_output=True, text=True)
    first = (r.stderr or r.stdout).splitlines()[0]
    major = int(first.split('"')[1].split(".")[0])
    if major < 21:
        raise GhidraError(f"Ghidra needs JDK 21+, found {first}")
    return {"java_home": str(JAVA_HOME), "version": first}


def install(force: bool = False) -> dict:
    lock = P.fork_lock()
    home, want = ghidra_home(), lock["ghidra_sha256"]
    marker = home / ".hs-sha256"
    jinfo = java_info()
    if not force and (home / "support/analyzeHeadless").exists():
        if not marker.exists():
            marker.write_text(want + "\n")          # unpacked by hand from the verified zip before this command existed
        if marker.read_text().strip() == want:
            return {"ghidra": str(home), "version": lock["ghidra_version"], "installed": False,
                    "reason": "already installed at pinned hash", "java": jinfo}
    if shutil.disk_usage(P.ROOT).free < 2 * 2**30:
        raise GhidraError("need about 2 GB free to download and unpack Ghidra")
    tmp = P.ENV / "ghidra.zip.part"
    log(f"downloading {lock['ghidra_url']}")
    with urllib.request.urlopen(lock["ghidra_url"], timeout=300) as r, open(tmp, "wb") as fh:
        shutil.copyfileobj(r, fh)
    got = _sha256(tmp)
    if got != want:
        tmp.unlink()
        raise GhidraError(f"SHA-256 mismatch: got {got}, fork.lock pins {want}; refusing to unpack")
    if home.exists():
        shutil.rmtree(home)
    with zipfile.ZipFile(tmp) as z:
        z.extractall(P.ENV)
    tmp.unlink()
    for f in (home / "support").glob("*"):          # zipfile drops exec bits
        f.chmod(0o755)
    for f in home.rglob("*"):
        if f.is_file() and (f.suffix in ("", ".sh") and f.parent.name in ("support", "os", "mac_arm_64", "mac_x86_64")):
            f.chmod(0o755)
    marker.write_text(want + "\n")
    return {"ghidra": str(home), "version": lock["ghidra_version"], "installed": True, "sha256": got, "java": jinfo}


def _headless(project_dir: Path, project: str, *args, timeout=3600) -> subprocess.CompletedProcess:
    home = ghidra_home()
    tool = home / "support/analyzeHeadless"
    if not tool.exists():
        raise GhidraError("Ghidra not installed; run `harness/hs ghidra-install`")
    dotted = [part for part in project_dir.resolve().parts if part.startswith(".")]
    if dotted:
        raise GhidraError(f"Ghidra can't use a project path containing {dotted}; set HS_RE_DIR to a plain path")
    project_dir.mkdir(parents=True, exist_ok=True)
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(Path.home()), "JAVA_HOME": str(JAVA_HOME)}
    cmd = [str(tool), str(project_dir), project, *map(str, args), "-scriptPath", str(SCRIPTS)]
    log("$ " + " ".join(cmd))
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)


def stage_copy(name: str = "Wow.exe") -> Path:
    """Copy a client binary into env/re/bin (analysis never touches client/)."""
    src = P.CLIENT / name
    if not src.is_file():
        raise GhidraError(f"no {name} in client/")
    RE_BIN.mkdir(parents=True, exist_ok=True)
    dst = RE_BIN / name
    if not dst.exists() or _sha256(dst) != _sha256(src):
        shutil.copyfile(src, dst)                 # content only; never alters src
    return dst


def run_report(mode: str, arg: str | None = None, name: str = "Wow.exe", analyze: bool = False,
               project_dir: Path | None = None, project: str = "client", fresh_import: bool = False) -> dict:
    """Import (if needed) a copy of `name` and run HsReport in the given mode. Returns parsed lines + raw text."""
    project_dir = project_dir or (RE / "projects")
    copy = stage_copy(name)
    RE_OUT.mkdir(parents=True, exist_ok=True)
    out = RE_OUT / f"{name}.{mode}.txt"
    if out.exists():
        out.unlink()
    script = ["-postScript", "HsReport.java", mode, str(out)] + ([arg] if arg else [])
    have = (project_dir / f"{project}.rep" / "idata").exists() and not fresh_import
    if have:
        r = _headless(project_dir, project, "-process", name, "-readOnly", "-noanalysis", *script)
    else:
        r = _headless(project_dir, project, "-import", copy, "-overwrite",
                      *([] if analyze else ["-noanalysis"]), *script)
    text = out.read_text() if out.exists() else ""
    if r.returncode != 0 or not text:
        raise GhidraError(f"analyzeHeadless failed (exit {r.returncode}); tail:\n" + "\n".join(r.stdout.splitlines()[-25:]))
    return {"mode": mode, "binary_copy": str(copy), "report": str(out), "lines": text.splitlines()}


def analyze(name: str = "Wow.exe") -> dict:
    """Full auto-analysis into the shared project (slow once; enables decompile/xrefs)."""
    return run_report("entry", name=name, analyze=True, fresh_import=True)
