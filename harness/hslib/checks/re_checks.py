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
    ev["ghidra_opened"] = [entry.get("ghidra_opened"), strings.get("ghidra_opened")]
    require(str(ghidra.RE) in entry["binary_copy"] and str(ghidra.RE) in str(scratch),
            "analysis copy/project not under env/re", **ev)
    # what Ghidra itself reports it opened (not just the path we computed)
    require(all(o and str(ghidra.RE) in o and str(P.CLIENT) not in o for o in ev["ghidra_opened"]),
            "Ghidra opened a file outside env/re", **ev)
    require(not changed and not added and not removed, "client/ changed during analysis", **ev)
    return ev


@check("re.ghidra_refuses_mismatch", order=223, capability="client-analysis", stack="any")
def ghidra_refuses_mismatch(ctx):
    """install refuses (a) a download whose SHA-256 differs from the pin and (b) an existing tree with no marker."""
    import tempfile, zipfile as zf
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp(prefix="hs-ghidra-refuse-"))
    try:
        fake = tmp / "fake.zip"
        with zf.ZipFile(fake, "w") as z:
            z.writestr("ghidra_0.0_PUBLIC/support/analyzeHeadless", "#!/bin/sh\n")
        lock = {"ghidra_version": "0.0", "ghidra_url": fake.as_uri(), "ghidra_sha256": "0" * 64}
        env_a = tmp / "env-a"
        try:
            ghidra.install(lock=lock, env_dir=env_a)
            a = {"refused": False}
        except ghidra.GhidraError as e:
            a = {"refused": True, "message": str(e)}
        a["unpacked"] = (env_a / "ghidra_0.0_PUBLIC").exists()
        env_b = tmp / "env-b"
        (env_b / "ghidra_0.0_PUBLIC/support").mkdir(parents=True)
        (env_b / "ghidra_0.0_PUBLIC/support/analyzeHeadless").write_text("#!/bin/sh\n")
        try:
            ghidra.install(lock=lock, env_dir=env_b)
            b = {"refused": False}
        except ghidra.GhidraError as e:
            b = {"refused": True, "message": str(e)}
        b["marker_written"] = (env_b / "ghidra_0.0_PUBLIC/.hs-sha256").exists()
        ev = {"mismatch_download": a, "unmarked_tree": b}
        require(a["refused"] and "SHA-256 mismatch" in a.get("message", "") and not a["unpacked"],
                "a download with the wrong SHA-256 wasn't refused (or was unpacked)", **ev)
        require(b["refused"] and not b["marker_written"], "an unverified existing tree was trusted", **ev)
        return ev
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
