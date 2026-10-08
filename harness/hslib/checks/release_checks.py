"""release-build (openspec/changes/release-build): the prebuilt release payload is relocatable, runs without Homebrew,
carries no game files, ships its sources, uses pinned inputs and leaves the dev install alone.

release.pins                   each pinned input (fork.lock release_*) whose cached bytes are wrong fails packaging,
                               naming it, removes the bad file and leaves no payload; the real cache passes.   (full)
The rest share one packaging run per harness run (`tools/release/package.sh all`, incremental build; `_packaged`),
with the dev fingerprints taken right before and after it:                                                    (slow)
release.dev_untouched          server/bin, build/CMakeCache.txt, env/player.json, env/settings.json, env/client-play,
                               the client/ manifest and the Homebrew Cellar are unchanged by packaging.
release.no_abs_paths           every Mach-O's load commands, install names and LC_RPATHs avoid /opt/homebrew,
                               /usr/local and /Users/, every @rpath dependency resolves inside the payload, and every
                               embedded path string is on tools/release/allowlist.json; a planted rpath is caught.
release.runs_without_homebrew  tools/release/smoke.py under sandbox-exec (Homebrew unreadable, the repo unwritable
                               except the scratch dir): mysqld init + start, mysql query, worldserver/authserver
                               --version, python -I -m hslib --help, at a path with spaces and again after a move.
release.no_game_files          no game-file path in the zip or the source tarballs; the zip is under 200 MB; a planted
                               MPQ in a scratch zip is caught.
release.source_alongside       the source assets exist and are in SHA256SUMS with matching hashes; the core and
                               playerbots tarballs hold exactly the pinned commit + patches tree (git blob ids); the
                               Moonglade tarball holds the payload's commit; THIRD_PARTY has the required licences and
                               covers every bundled library.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

from .. import paths as P
from ..registry import check, require

CAP = "release-build"
TOOLS = P.ROOT / "tools" / "release"
PACKAGE = TOOLS / "package.sh"
WORK = P.ENV / "release"
PAYLOAD = WORK / "payload" / "Moonglade"
OUT = WORK / "out"
ALLOWLIST = TOOLS / "allowlist.json"
FORBIDDEN = ("/opt/homebrew", "/usr/local", "/Users/")
SYSTEM = ("/usr/lib/", "/System/")
MAX_ZIP_BYTES = 200 * 1000 * 1000          # spec: "the download stays under 200 MB"
PACKAGE_TIMEOUT = 3 * 3600                 # a from-empty portable build is ~30 min on the dev Mac; ccache makes reruns fast


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _scratch(tag: str) -> Path:
    d = WORK / f"check-{tag}-{os.getpid()}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    return d


# ================================================================================================ dev fingerprints

def _stat_tree(root: Path) -> dict:
    out = {}
    if not root.exists():
        return {"missing": True}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for n in sorted(filenames):
            f = Path(dirpath) / n
            st = f.lstat()
            out[str(f.relative_to(root))] = [st.st_size, st.st_mtime_ns, st.st_ino]
    return out


def _file_fp(p: Path) -> dict | None:
    if not p.is_file():
        return None
    st = p.stat()
    return {"sha256": _sha256(p), "mtime_ns": st.st_mtime_ns, "ino": st.st_ino}


def dev_targets() -> dict:
    """name -> function producing that target's fingerprint (spec: server/bin, build/CMakeCache.txt,
    env/player.json, env/client-play, the client/ manifest; plus env/settings.json and the Homebrew Cellar)."""
    from .. import clientbuild

    def server_bin():
        return {f.name: _file_fp(f) for f in sorted(P.SERVER_BIN.iterdir())} if P.SERVER_BIN.is_dir() else None

    def cellar():
        c = P.BREW / "Cellar"
        if not c.is_dir():
            return None
        return {f"{f.name}/{v.name}": v.stat().st_mtime_ns for f in sorted(c.iterdir()) if f.is_dir()
                for v in sorted(f.iterdir())}
    return {
        "server/bin": server_bin,
        "build/CMakeCache.txt": lambda: _file_fp(P.BUILD / "CMakeCache.txt"),
        "env/player.json": lambda: _file_fp(P.PLAYER_FILE),
        "env/settings.json": lambda: _file_fp(P.SETTINGS_FILE),
        "env/client-play": lambda: clientbuild.fingerprint(clientbuild.PLAY),
        "client/ manifest": lambda: {"manifest": _file_fp(P.CLIENT_MANIFEST), "tree": _stat_tree(P.CLIENT)},
        "Homebrew Cellar": cellar,
    }


def fingerprint(targets: dict) -> dict:
    return {k: fn() for k, fn in targets.items()}


def fp_changes(before: dict, after: dict) -> list[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


# ================================================================================================ shared packaging

def _packaged(ctx: dict) -> dict:
    """Run `package.sh all` once per harness run (dev fingerprints right before and after). Later calls return the
    same record; a failed packaging is recorded and every check that needs the payload fails on it."""
    if "release_package" in ctx:
        return ctx["release_package"]
    targets = dev_targets()
    rec: dict = {"started": time.time()}
    rec["fp_before"] = fingerprint(targets)
    record = WORK / "last-package-run.json"
    record.unlink(missing_ok=True)
    log = WORK / "last-package-run.log"
    WORK.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("HS_TEST_")}
    with open(log, "w") as fh:
        r = subprocess.run([str(PACKAGE), "all", "--record", str(record)], stdout=fh, stderr=subprocess.STDOUT,
                           env=env, cwd=P.ROOT, timeout=PACKAGE_TIMEOUT)
    rec["rc"] = r.returncode
    rec["finished"] = time.time()
    rec["log"] = str(log)
    rec["log_tail"] = log.read_text(errors="replace")[-1500:]
    try:
        rec["record"] = json.loads(record.read_text())
    except (OSError, ValueError):
        rec["record"] = None
    rec["fp_after"] = fingerprint(targets)
    ctx["release_package"] = rec
    return rec


def _need_payload(ctx: dict) -> dict:
    rec = _packaged(ctx)
    ev = {"package_rc": rec["rc"], "package_log": rec["log"], "package_log_tail": rec["log_tail"][-800:]}
    require(rec["rc"] == 0, "tools/release/package.sh all failed", **ev)
    ver = PAYLOAD / "VERSION"
    require(ver.is_file() and ver.stat().st_mtime >= rec["started"],
            "the payload's VERSION isn't from this run's packaging", **ev, version_file=str(ver))
    ev["payload"] = str(PAYLOAD)
    ev["version"] = json.loads(ver.read_text())
    ev["package_seconds"] = round(rec["finished"] - rec["started"])
    steps = (rec.get("record") or {}).get("steps", {})
    ev["build_seconds"] = (steps.get("build") or {}).get("seconds")
    return ev


# ================================================================================================ Mach-O scanning

MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
PATH_STRING = re.compile(rb"(?:/opt/homebrew|/usr/local|/Users/)[\x21-\x7e]*")


def _is_macho(p: Path) -> bool:
    if p.is_symlink() or not p.is_file():
        return False
    with open(p, "rb") as fh:
        return fh.read(4) in MACHO_MAGIC


def _otool(p: Path) -> dict:
    """{"id", "deps", "rpaths"} parsed from `otool -l` (this check's own parser; delocate.py has another)."""
    out = subprocess.run(["/usr/bin/otool", "-l", str(p)], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"otool -l {p} failed: {out.stderr[:300]}")
    ident, deps, rpaths, cmd = None, set(), set(), None
    for line in out.stdout.splitlines():
        s = line.strip()
        if s.startswith("cmd "):
            cmd = s.split()[1]
        elif s.startswith(("name ", "path ")):
            v = s.split(" ", 1)[1].rsplit(" (offset", 1)[0]
            if cmd == "LC_ID_DYLIB":
                ident = v
            elif cmd == "LC_RPATH":
                rpaths.add(v)
            elif cmd and cmd.startswith("LC_") and "DYLIB" in cmd:
                deps.add(v)
    return {"id": ident, "deps": sorted(deps), "rpaths": sorted(rpaths)}


def _resolve(dep: str, image: Path, rpaths: list[str], is_lib: bool) -> Path | None:
    exe_dir = image.parent if not is_lib else (image.parent.parent / "bin" if image.parent.name == "lib" else None)

    def sub(s):
        if s.startswith("@loader_path"):
            return str(image.parent) + s[len("@loader_path"):]
        if s.startswith("@executable_path"):
            return None if exe_dir is None else str(exe_dir) + s[len("@executable_path"):]
        return s
    cands = []
    if dep.startswith("@rpath/"):
        cands = [Path(b) / dep[7:] for b in (sub(r) for r in rpaths) if b]
    elif sub(dep):
        cands = [Path(sub(dep))]
    return next((c for c in cands if c.is_file()), None)


def scan_payload(root: Path, allow: list[dict]) -> dict:
    root = root.resolve()
    files = sorted(p for p in root.rglob("*") if _is_macho(p))
    problems, allowlisted = [], []
    for p in files:
        rel = str(p.relative_to(root))
        o = _otool(p)
        for kind, vals in (("install name", [o["id"]] if o["id"] else []), ("LC_RPATH", o["rpaths"]),
                           ("load command", o["deps"])):
            problems += [f"{rel}: {kind} {v}" for v in vals if v.startswith(FORBIDDEN)]
        for d in o["deps"]:
            if d.startswith(SYSTEM) or d == o["id"] or d.startswith(FORBIDDEN):
                continue
            if not d.startswith("@"):
                problems.append(f"{rel}: absolute non-system dependency {d}")
                continue
            got = _resolve(d, p, o["rpaths"], o["id"] is not None)
            if got is None or root not in got.resolve().parents:
                problems.append(f"{rel}: {d} doesn't resolve inside the payload ({got})")
        for s in sorted({m.group(0).decode("ascii", "replace") for m in PATH_STRING.finditer(p.read_bytes())}):
            hit = next((e for e in allow if fnmatch.fnmatchcase(rel, e["file"]) and re.fullmatch(e["match"], s)), None)
            if hit:
                allowlisted.append({"file": rel, "string": s, "reason": hit["reason"]})
            else:
                problems.append(f"{rel}: embedded path {s}")
    return {"macho_files": len(files), "problems": problems, "allowlisted": allowlisted}


def load_allowlist() -> list[dict]:
    entries = json.loads(ALLOWLIST.read_text())["entries"]
    bad = [e for e in entries if not (e.get("file") and e.get("match") and len(e.get("reason", "")) >= 20)]
    if bad:
        raise RuntimeError(f"allowlist entries without file, match and a written reason: {bad}")
    return entries


# ================================================================================================ release.pins

def pin_refusals(cache_src: Path) -> dict:
    """For each release pin: a scratch work dir whose cache holds APFS clones of the other pins and junk under this
    pin's name; `package.sh all --work/--cache` must fail naming it, remove the junk and leave no payload/out."""
    from .. import downloads as D
    lock = P.fork_lock()
    res = {}
    for name in D.RELEASE_PINS:
        work = _scratch(f"pins-{name}")
        cache = work / "cache"
        cache.mkdir()
        try:
            for other in D.RELEASE_PINS:
                f = D.pin(other, lock)["file"]
                if other == name:
                    (cache / f).write_bytes(b"not the pinned file " + name.encode() + b"\n" * 64)
                else:
                    subprocess.run(["/bin/cp", "-c", str(cache_src / f), str(cache / f)], check=True)
            junk = cache / D.pin(name, lock)["file"]
            r = subprocess.run([str(PACKAGE), "all", "--work", str(work), "--cache", str(cache)],
                               capture_output=True, text=True, timeout=600, cwd=P.ROOT)
            text = r.stdout + r.stderr
            left = sorted(str(p.relative_to(work)) for p in work.rglob("*")
                          if p.relative_to(work).parts[0] in ("payload", "out", "payload.tmp", "build"))
            res[name] = {"rc": r.returncode, "names_input": f"pinned input {name}" in text,
                         "says_mismatch": "SHA-256 mismatch" in text, "junk_removed": not junk.exists(),
                         "left_behind": left[:10], "stderr_tail": r.stderr[-400:]}
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return res


@check("release.pins", order=13, capability=CAP, stack="any", tier="full")
def pins(ctx):
    """A pinned release input whose cached bytes don't match fork.lock fails packaging, naming the input, and no payload
    is produced; the real cached inputs pass their pins."""
    from .. import downloads as D
    lock = P.fork_lock()
    ev: dict = {"pins": {}}
    for name in D.RELEASE_PINS:
        ukey, hkey = D.PINS[name][0], D.PINS[name][1]
        ev["pins"][name] = {"url": lock.get(ukey), "sha256": lock.get(hkey)}
        require(lock.get(ukey, "").startswith("https://") and re.fullmatch(r"[0-9a-f]{64}", lock.get(hkey, "")),
                f"fork.lock doesn't pin {name} by https URL and sha256", **ev)
    # control: the real cache (downloaded if missing) passes every pin
    r = subprocess.run([str(PACKAGE), "fetch"], capture_output=True, text=True, timeout=3600, cwd=P.ROOT)
    ev["control_fetch"] = {"rc": r.returncode, "stdout_tail": r.stdout[-600:], "stderr_tail": r.stderr[-400:]}
    require(r.returncode == 0 and r.stdout.count("sha256 ok") == len(D.RELEASE_PINS),
            "control: the real cached inputs didn't pass their pins", **ev)
    ev["refusals"] = pin_refusals(D.RELEASE_CACHE)
    for name, x in ev["refusals"].items():
        require(x["rc"] != 0 and x["names_input"] and x["says_mismatch"],
                f"{name}: a wrong-hash input wasn't refused by name", **ev)
        require(x["junk_removed"], f"{name}: the wrong-hash file was kept", **ev)
        require(not x["left_behind"], f"{name}: packaging left {x['left_behind']}", **ev)
    return ev


# ================================================================================================ slow: one packaging

@check("release.dev_untouched", order=13, capability=CAP, stack="any", tier="slow")
def dev_untouched(ctx):
    """A full local packaging run leaves server/bin, build/CMakeCache.txt, env/player.json, env/settings.json,
    env/client-play, the client/ manifest and the Homebrew Cellar unchanged."""
    rec = _packaged(ctx)
    ev = {"package_rc": rec["rc"], "package_log_tail": rec["log_tail"][-600:],
          "targets": sorted(rec["fp_before"]),
          "missing_targets": sorted(k for k, v in rec["fp_before"].items() if v is None)}
    require(rec["rc"] == 0, "packaging failed, so it wasn't a full packaging run", **ev)
    changed = fp_changes(rec["fp_before"], rec["fp_after"])
    ev["changed"] = changed
    ev["server_bin_files"] = sorted((rec["fp_before"].get("server/bin") or {}))
    ev["client_tree_files"] = len((rec["fp_before"].get("client/ manifest") or {}).get("tree") or {})
    require(not changed, f"packaging changed {changed}", **ev)
    # the fingerprints are real: the targets the spec names exist on this machine
    require(not ev["missing_targets"] or ev["missing_targets"] == ["env/settings.json"],
            f"dev targets missing, so nothing was compared: {ev['missing_targets']}", **ev)
    return ev


@check("release.no_abs_paths", order=13, capability=CAP, stack="any", tier="slow")
def no_abs_paths(ctx):
    """Every Mach-O in the payload loads only system or in-payload libraries, no load command/rpath/string names
    /opt/homebrew, /usr/local or /Users/ outside the reviewed allowlist, and a planted Homebrew rpath is caught."""
    ev = _need_payload(ctx)
    allow = load_allowlist()
    res = scan_payload(PAYLOAD, allow)
    ev |= {"macho_files": res["macho_files"], "problems": res["problems"][:40], "allowlisted": res["allowlisted"]}
    require(res["macho_files"] >= 10, "too few Mach-O files found; the scan didn't see the payload", **ev)
    require(not res["problems"], f"{len(res['problems'])} forbidden or unresolved paths", **ev)
    # the scanner can fail: authserver plus its libraries with a planted /opt/homebrew rpath
    d = _scratch("abs")
    try:
        (d / "server/bin").mkdir(parents=True)
        shutil.copy2(PAYLOAD / "server/bin/authserver", d / "server/bin/authserver")
        shutil.copytree(PAYLOAD / "server/lib", d / "server/lib")
        clean = scan_payload(d, allow)
        subprocess.run(["/usr/bin/install_name_tool", "-add_rpath", "/opt/homebrew/lib",
                        str(d / "server/bin/authserver")], check=True, capture_output=True)
        planted = scan_payload(d, allow)
        ev["self_test"] = {"clean_problems": clean["problems"][:5], "planted_problems": planted["problems"][:5]}
        require(not clean["problems"] and any("LC_RPATH /opt/homebrew/lib" in x for x in planted["problems"]),
                "self-test: the planted /opt/homebrew rpath wasn't caught", **ev)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ev


@check("release.runs_without_homebrew", order=13, capability=CAP, stack="any", tier="slow")
def runs_without_homebrew(ctx):
    """With /opt/homebrew and /usr/local unreadable (sandbox-exec) and the repo unwritable outside a scratch dir, the
    payload at a path with spaces initialises and starts mysqld on a scratch port, queries it with the bundled client,
    runs worldserver/authserver --version and python -I -m hslib --help, and does it again after a move."""
    ev = _need_payload(ctx)
    d = _scratch("smoke")
    out = d.parent / f"{d.name}.json"
    try:
        r = subprocess.run([sys.executable, "-I", str(TOOLS / "smoke.py"),
                            "--payload", str(PAYLOAD), "--scratch", str(d), "--sandbox", "--deny-write", str(P.ROOT),
                            "--json", str(out)], capture_output=True, text=True, timeout=1800, cwd=P.ROOT)
        rec = json.loads(out.read_text()) if out.is_file() else {}
        ev["smoke"] = {"rc": r.returncode, "stdout": r.stdout[-400:], "stderr": r.stderr[-800:],
                       "profile": rec.get("profile"), "profile_proof": rec.get("profile_proof"),
                       "port": rec.get("port"), "failed": rec.get("failed"),
                       "locations": [{"path": loc["path"], "steps": [
                           {k: s.get(k) for k in ("step", "rc", "ok", "seconds", "stdout_tail", "stderr_tail",
                                                  "log_error_tail") if k in s} for s in loc["steps"]]}
                           for loc in rec.get("locations", [])]}
        require(rec.get("started", 0) >= ctx.get("release_package", {}).get("started", 0),
                "the smoke record isn't from this run", **ev)
        proof = rec.get("profile_proof") or {}
        require(proof.get("opt_homebrew_exists") is False or proof.get("ls_opt_homebrew_rc_sandboxed", 0) != 0,
                "the sandbox profile doesn't hide /opt/homebrew", **ev)
        require(rec.get("port") not in P.LANE_PORTS["a"].values() and rec.get("port") not in P.LANE_PORTS["b"].values(),
                "the smoke used a stack port", **ev)
        locs = rec.get("locations", [])
        require(len(locs) == 2 and all(" " in Path(x["path"]).parent.name for x in locs),
                "the smoke didn't run at two paths with spaces", **ev)
        require(r.returncode == 0 and rec.get("ok") is True and not rec.get("failed"),
                f"smoke failed: {rec.get('failed')}", **ev)
    finally:
        shutil.rmtree(d, ignore_errors=True)
        out.unlink(missing_ok=True)
    return ev


# Game-file paths (spec: MPQ, Wow.exe, DBC, maps/vmaps/mmaps, client-data): client archives and executables by name,
# extracted server data by its file types and by the folder names client-data unpacks into (lowercase, exact; source
# folders like AzerothCore's src/.../Maps/ are code), and client-data itself.
GAME_PATTERNS = (r"(?i)\.mpq$", r"(?i)(^|/)wow(-64)?\.exe$", r"(?i)\.dbc$", r"(?i)\.db2$",
                 r"(^|/)(maps|vmaps|mmaps|dbc|Cameras)/", r"(?i)(^|/)[0-9]{7}\.map$",
                 r"(?i)\.(vmtree|vmtile|vmo|mmap|mmtile|m2|wmo|blp|adt|wdt|skin|anim)$",
                 r"(?i)(^|/)client-data/", r"(?i)(^|/)data\.zip$", r"(?i)(^|/)WTF/")


def game_hits(names: list[str]) -> list[str]:
    """Names matching a game-file pattern. Git's empty-folder markers (.gitkeep) don't count: AzerothCore's tree has
    var/extractors/{maps,vmaps,mmaps,dbc,Cameras}/.gitkeep, which hold nothing."""
    return [n for n in names if not n.endswith("/.gitkeep") and any(re.search(p, n) for p in GAME_PATTERNS)]


def _archive_names(p: Path) -> list[str]:
    if p.suffix == ".zip":
        with zipfile.ZipFile(p) as z:
            return z.namelist()
    with tarfile.open(p, "r:*") as t:
        return t.getnames()


@check("release.no_game_files", order=13, capability=CAP, stack="any", tier="slow")
def no_game_files(ctx):
    """No release asset lists a game-file path (MPQ, Wow.exe, DBC, maps/vmaps/mmaps, client-data), the zip is under
    200 MB, and a planted MPQ in a scratch zip is caught."""
    ev = _need_payload(ctx)
    assets = sorted(p for p in OUT.iterdir() if p.suffix in (".zip", ".gz"))
    ev["assets"] = {}
    zips = [p for p in assets if p.suffix == ".zip"]
    require(len(zips) == 1, f"expected one payload zip in {OUT}, found {[p.name for p in zips]}", **ev)
    for a in assets:
        names = _archive_names(a)
        hits = game_hits(names)
        ev["assets"][a.name] = {"entries": len(names), "bytes": a.stat().st_size, "game_hits": hits[:20]}
    ev["zip_bytes"] = zips[0].stat().st_size
    ev["zip_mb"] = round(ev["zip_bytes"] / 1e6, 1)
    require(all(v["entries"] > 0 for v in ev["assets"].values()), "an asset lists no entries", **ev)
    require(not any(v["game_hits"] for v in ev["assets"].values()), "game files in a release asset", **ev)
    require(ev["zip_bytes"] < MAX_ZIP_BYTES, f"the download is {ev['zip_mb']} MB (limit 200 MB)", **ev)
    d = _scratch("nogame")
    try:
        bad = d / "planted.zip"
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("Moonglade/README.md", "x")
            z.writestr("Moonglade/Data/common.MPQ", "x")
        ev["self_test_hits"] = game_hits(_archive_names(bad))
        require(ev["self_test_hits"] == ["Moonglade/Data/common.MPQ"], "self-test: a planted MPQ wasn't caught", **ev)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ev


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def tarball_blobs(p: Path, prefix: str) -> dict:
    """{path below prefix: git blob id} of every file and symlink in a .tar.gz."""
    out = {}
    with tarfile.open(p, "r:gz") as t:
        for m in t:
            if m.isdir() and m.name.rstrip("/") == prefix:
                continue
            if not m.name.startswith(prefix + "/"):
                out[f"<outside prefix> {m.name}"] = "?"
                continue
            rel = m.name[len(prefix) + 1:]
            if m.isfile():
                out[rel] = _blob_id(t.extractfile(m).read())
            elif m.issym():
                out[rel] = _blob_id(m.linkname.encode())
    return out


def tree_blobs(repo: Path, tree: str) -> dict:
    r = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "-z", tree], capture_output=True, text=True, check=True)
    out = {}
    for ent in r.stdout.split("\0"):
        if ent:
            meta, path = ent.split("\t", 1)
            mode, typ, oid = meta.split()
            if typ == "blob":
                out[path] = oid
    return out


def source_problems(out_dir: Path, payload: Path) -> tuple[list[str], dict]:
    from .. import build as B
    probs, ev = [], {}
    lock = P.fork_lock()
    try:
        sources = json.loads((out_dir / "SOURCES.json").read_text())
    except (OSError, ValueError) as e:
        return [f"SOURCES.json unreadable: {e}"], ev
    sums = {}
    try:
        for line in (out_dir / "SHA256SUMS").read_text().splitlines():
            h, name = line.split("  ", 1)
            sums[name] = h
    except (OSError, ValueError) as e:
        return [f"SHA256SUMS unreadable: {e}"], ev
    files = sorted(p.name for p in out_dir.iterdir() if p.is_file() and p.name != "SHA256SUMS")
    ev["assets"] = files
    for f in files:
        if f not in sums:
            probs.append(f"{f} isn't in SHA256SUMS")
        elif _sha256(out_dir / f) != sums[f]:
            probs.append(f"{f}: sha256 doesn't match SHA256SUMS")
    probs += [f"SHA256SUMS lists missing {n}" for n in sums if n not in files]
    ver = json.loads((payload / "VERSION").read_text())
    want = {
        "moonglade": f"moonglade-{ver['version']}-source.tar.gz",
        "azerothcore": f"azerothcore-{lock['core_sha'][:12]}-with-patches-source.tar.gz",
        "mod-playerbots": f"mod-playerbots-{lock['playerbots_sha'][:12]}-with-patches-source.tar.gz",
        "mysql": Path(lock["release_mysql_source_url"]).name,
    }
    ev["expected_sources"] = want
    for k, n in want.items():
        if n not in files:
            probs.append(f"source asset {k} ({n}) missing")
    if want["mysql"] in files and _sha256(out_dir / want["mysql"]) != lock["release_mysql_source_sha256"]:
        probs.append("the MySQL source tarball isn't the pinned one")
    trees = {"azerothcore": (P.CORE_SRC, lock["core_sha"], B.core_patches()),
             "mod-playerbots": (P.BOTS_SRC, lock["playerbots_sha"], B.playerbots_patches())}
    ev["trees"] = {}
    for key, (repo, sha, patches) in trees.items():
        if want[key] not in files:
            continue
        tree, tp = B.expected_patched_tree(repo, sha, patches)
        if not tree:
            probs += tp
            continue
        a, b = tarball_blobs(out_dir / want[key], key), tree_blobs(repo, tree)
        diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        ev["trees"][key] = {"expected_tree": tree, "patches": [p.name for p in patches], "files": len(a),
                            "differences": diff[:10]}
        if diff:
            probs.append(f"{key} tarball differs from {sha[:12]} + patches ({len(diff)} paths, e.g. {diff[:3]})")
    if want["moonglade"] in files:
        commit = ver["commit"]
        a, b = tarball_blobs(out_dir / want["moonglade"], f"moonglade-{ver['version']}"), tree_blobs(P.ROOT, commit)
        diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        ev["trees"]["moonglade"] = {"commit": commit, "files": len(a), "differences": diff[:10]}
        if diff:
            probs.append(f"moonglade source tarball differs from {commit[:12]} ({len(diff)} paths)")
    if sources.get("version") != ver["version"]:
        probs.append("SOURCES.json is for another version")
    return probs, ev


REQUIRED_LICENCES = ("azerothcore", "mod-playerbots", "mysql", "openssl@3", "boost", "python")


def third_party_problems(payload: Path) -> tuple[list[str], dict]:
    tp = payload / "THIRD_PARTY"
    probs = []
    try:
        index = json.loads((tp / "INDEX.json").read_text())
    except (OSError, ValueError) as e:
        return [f"THIRD_PARTY/INDEX.json unreadable: {e}"], {}
    ev = {"components": sorted(index)}
    for comp in REQUIRED_LICENCES:
        if comp not in index:
            probs.append(f"no THIRD_PARTY licence for {comp}")
    for comp, e in index.items():
        if not e.get("files"):
            probs.append(f"{comp}: no licence files listed")
        for f in e.get("files", []):
            p = tp / f
            if not p.is_file() or p.stat().st_size < 100:
                probs.append(f"{comp}: licence file {f} missing or empty")
    covered = {c for e in index.values() for c in e.get("covers", [])}
    libs = sorted(f"server/lib/{p.name}" for p in (payload / "server/lib").iterdir() if p.name.endswith(".dylib"))
    ev["server_libs"] = libs
    probs += [f"bundled {lib} has no THIRD_PARTY entry" for lib in libs if lib not in covered]
    for d in ("mysql/", "python/"):
        if d not in covered:
            probs.append(f"{d} isn't covered by a THIRD_PARTY entry")
    return probs, ev


@check("release.source_alongside", order=13, capability=CAP, stack="any", tier="slow")
def source_alongside(ctx):
    """Every source asset exists and is in SHA256SUMS with a matching hash, the core and playerbots tarballs hold the
    pinned commit + patches tree, the Moonglade tarball holds the payload's commit, and THIRD_PARTY carries the
    required licences and covers every bundled library; a tampered copy fails."""
    ev = _need_payload(ctx)
    probs, sev = source_problems(OUT, PAYLOAD)
    tprobs, tev = third_party_problems(PAYLOAD)
    ev |= {"sources": sev, "third_party": tev, "problems": probs + tprobs}
    require(not probs and not tprobs, f"{len(probs) + len(tprobs)} source/licence problems", **ev)
    # the checks can fail: a tampered SHA256SUMS and a missing licence, in scratch copies
    d = _scratch("src")
    try:
        o = d / "out"
        o.mkdir()
        for p in OUT.iterdir():
            subprocess.run(["/bin/cp", "-c", str(p), str(o / p.name)], check=True)
        s = (o / "SHA256SUMS").read_text()
        (o / "SHA256SUMS").write_text(s.replace(s[:8], "00000000", 1))
        bad_sums, _ = source_problems(o, PAYLOAD)
        pl = d / "payload"
        shutil.copytree(PAYLOAD / "THIRD_PARTY", pl / "THIRD_PARTY")
        shutil.copytree(PAYLOAD / "server/lib", pl / "server/lib")
        idx = json.loads((pl / "THIRD_PARTY/INDEX.json").read_text())
        idx.pop("boost", None)
        (pl / "THIRD_PARTY/INDEX.json").write_text(json.dumps(idx))
        bad_tp, _ = third_party_problems(pl)
        ev["self_test"] = {"tampered_sums": bad_sums[:3], "missing_boost": bad_tp[:3]}
        require(any("SHA256SUMS" in x for x in bad_sums) and any("boost" in x for x in bad_tp),
                "self-test: a tampered SHA256SUMS or a missing licence wasn't caught", **ev)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ev
