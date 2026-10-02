"""client-analysis checks (ADR-0005): pinned tooling, and analysis that provably leaves client/ untouched."""
import hashlib
import shutil
import subprocess

from .. import ghidra, paths as P
from ..registry import check, require


# Folders/files the game itself writes while running (settings, logs, caches, crash reports, shader cache).
# A concurrently running client may change these; Ghidra never opens client/ at all, so they're out of scope.
RUNTIME_WRITTEN = ("WTF/", "Logs/", "Cache/", "Errors/", "Screenshots/")
RUNTIME_FILES = ("mtld3d_shaders.bin",)


def _snapshot() -> dict:
    """Every file under client/ except game-written runtime paths (path -> size, mtime_ns), plus SHA-256 for .exe/.dll."""
    snap = {}
    for f in sorted(P.CLIENT.rglob("*")):
        rel = f.relative_to(P.CLIENT).as_posix()
        if rel.startswith(RUNTIME_WRITTEN) or rel in RUNTIME_FILES:
            continue
        if f.is_file():
            st = f.stat()
            rec = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
            if f.suffix.lower() in (".exe", ".dll"):
                rec["sha256"] = hashlib.sha256(f.read_bytes()).hexdigest()
            snap[rel] = rec
    return snap


@check("re.ghidra_installed", order=220, capability="client-analysis", stack="any")
def ghidra_installed(ctx):
    """`hs ghidra-install` on an installed tree: exit 0, no download, pinned version, JDK 21+, marker = pinned SHA."""
    r = subprocess.run([str(P.HARNESS / "hs"), "ghidra-install"], capture_output=True, text=True, timeout=600)
    lock = P.fork_lock()
    marker = ghidra.ghidra_home() / ".hs-sha256"
    ev = {"cmd": "harness/hs ghidra-install", "exit": r.returncode, "stdout": r.stdout[-1500:], "stderr": r.stderr[-800:],
          "marker": marker.read_text().strip() if marker.exists() else None, "pinned_sha256": lock["ghidra_sha256"]}
    require(r.returncode == 0, "ghidra-install failed", **ev)
    require('"installed": false' in r.stdout, "ghidra-install re-downloaded an already-installed tree", **ev)
    require(f'"version": "{lock["ghidra_version"]}"' in r.stdout, "reported version != pinned", **ev)
    require('openjdk version "2' in r.stdout or 'openjdk version \\"2' in r.stdout, "no JDK 21+ reported", **ev)
    require(ev["marker"] == lock["ghidra_sha256"], "install marker != pinned SHA-256", **ev)
    return ev


@check("re.ghidra_readonly", order=221, capability="client-analysis", stack="any")
def ghidra_readonly(ctx):
    """Headless import + report on a copy of Wow.exe: entry point and a string match reported; client/ unchanged."""
    before = _snapshot()
    scratch = ghidra.RE / "check-project"
    if scratch.exists():
        shutil.rmtree(scratch)
    entry = ghidra.run_report("entry", project_dir=scratch, project="check", fresh_import=True)
    strings = ghidra.run_report("findascii", "GlueXML", project_dir=scratch, project="check")
    after = _snapshot()
    changed = sorted(k for k in before if k in after and before[k] != after[k])
    added, removed = sorted(set(after) - set(before)), sorted(set(before) - set(after))
    entries = [l for l in entry["lines"] if l.startswith("entry\t")]
    matches = [l for l in strings["lines"] if l.startswith("match\t")]
    ev = {"binary_copy": entry["binary_copy"], "project": str(scratch), "entries": entries[:5],
          "string_matches": matches[:10], "client_files_checked": len(before),
          "changed": changed, "added": added, "removed": removed}
    require(entries, "no entry point reported", **ev)
    require(matches, "no 'GlueXML' string match reported", **ev)
    require(str(ghidra.RE) in entry["binary_copy"] and str(ghidra.RE) in str(scratch),
            "analysis copy/project not under env/re", **ev)
    require(not changed and not added and not removed, "client/ changed during analysis", **ev)
    return ev
