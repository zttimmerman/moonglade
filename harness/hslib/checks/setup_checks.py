"""easy-setup checks (openspec/changes/easy-setup): bootstrap, preflight, pinned downloads, moved-repo detection,
portability, and the player's play client never being rebuilt by a harness run (F28).

Scratch space lives in harness/out/setup-scratch/ (gitignored, harness output): never in the main checkout's env/,
data/, client/, server/ or dist/. Light scratch copies hold the repo's tracked (and new, unignored) files only.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import zipfile
from pathlib import Path

from .. import clientbuild, paths as P, pristine
from ..registry import check, require

SCRATCH_ROOT = P.OUT / "setup-scratch"
CACHE = SCRATCH_ROOT / "cache"           # pinned downloads for setup.fresh_clone, kept between runs (verified on use)
CLONE = SCRATCH_ROOT / "clone"           # fixed path: a later run's server build reuses ccache entries
# harness env vars a scratch repo's commands must not inherit (they'd point at the main checkout's test setup)
SCRUB_ENV = ("PYTHONPATH", "HS_SETTINGS", "HS_PROFILE", "HS_PLAY_CLIENT_DIR", "HS_CLIENT_DIR", "HS_TEST_OWN_CLIENT",
             "HS_GLUE_TEST", "HS_PLAY_SKIP_PATCHES", "HS_PLAYER_FILE", "HS_PLAYER_DEFAULT_NAME", "HS_BACKUP_DIR",
             "HS_DOWNLOAD_CACHE", "HS_BOOTSTRAP_TEST", "HS_WINE", "HS_RE_DIR",
             "HS_TEST_PRISTINE_CLIENT", "HS_TEST_PRISTINE_MUTATE")
# and every test switch by prefix (B32: report.client_pristine's self-test runs nested `hs run`s with
# $HS_TEST_PRISTINE_CLIENT set, and a scratch clone's commands must never see it or any later HS_TEST_* switch)
SCRUB_PREFIXES = ("HS_TEST_", "HSDOCK_TEST")


def scratch_env(**extra) -> dict:
    """The environment for a scratch repo's commands: the harness's switches removed (SCRUB_ENV, SCRUB_PREFIXES),
    then `extra` added."""
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV and not k.startswith(SCRUB_PREFIXES)}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env | {k: str(v) for k, v in extra.items()}


def repo_files(root: Path = P.ROOT) -> list[str]:
    """The repo's tracked files plus new files that aren't ignored (what a commit of the working tree would hold)."""
    r = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                       capture_output=True, text=True, check=True)
    return sorted(f for f in set(r.stdout.split("\0")) if f and (root / f).is_file())


def light_copy(tag: str) -> Path:
    """A scratch copy of the repo's files (no vendor/, build/, server/, data/, env/, client/, dist/)."""
    dest = SCRATCH_ROOT / f"{tag}-{os.getpid()}"
    shutil.rmtree(dest, ignore_errors=True)
    for rel in repo_files():
        d = dest / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(P.ROOT / rel, d, follow_symlinks=False)
    return dest


def drop(d: Path | None) -> None:
    if d and SCRATCH_ROOT in Path(d).resolve().parents:
        pristine.remove_tree(d)               # a scratch install inside is write-protected (pristine.py)


def tree_fingerprint(root: Path) -> dict:
    """{relative path: (kind, size, mtime_ns)} for everything under root (directories included)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            f = Path(dirpath) / n
            st = f.lstat()
            out[str(f.relative_to(root))] = ("d" if f.is_dir() else "f", st.st_size if not f.is_dir() else 0,
                                             st.st_mtime_ns)
    return out


def _tree_diff(a: dict, b: dict) -> list[str]:
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))[:20]


def bootstrap_events(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith("[bootstrap] "):
            try:
                out.append(json.loads(line[len("[bootstrap] "):]))
            except ValueError:
                pass
    return out


# ================================================================ setup.preflight

def _fake_clients(root: Path) -> dict:
    """Clients that aren't 3.3.5a 12340 enUS, plus paths that aren't a zip or a folder (all outside the scratch repo)."""
    root.mkdir(parents=True, exist_ok=True)
    out = {}
    # another version: a real PE with a version resource that isn't 3.3.5.12340 (the client's own Repair.exe,
    # 3.0.1.301, copied read-only) standing in for Wow.exe
    wv = root / "client-wrong-version"
    (wv / "Data" / "enUS").mkdir(parents=True)
    shutil.copyfile(P.CLIENT / "Repair.exe", wv / "Wow.exe")
    (wv / "Data" / "enUS" / "realmlist.wtf").write_text("set realmlist 127.0.0.1\n")
    out["wrong_version_folder"] = wv
    # right build, wrong language, as a zip: the real Wow.exe with a deDE data folder
    z = root / "client-deDE.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
        zf.write(P.CLIENT / "Wow.exe", "WoW-3.3.5a-deDE/Wow.exe")
        zf.writestr("WoW-3.3.5a-deDE/Data/deDE/realmlist.wtf", "set realmlist 127.0.0.1\n")
    out["wrong_locale_zip"] = z
    t = root / "client.txt"
    t.write_text("not a client\n")
    out["text_file"] = t
    out["missing_path"] = root / "no-such-client.zip"
    return out


def run_bootstrap(repo: Path, args: list[str], env: dict, timeout: float = 300) -> dict:
    t0 = time.time()
    try:
        r = subprocess.run([str(repo / "bootstrap"), *args], cwd=repo, capture_output=True, text=True, env=env,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        rc, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        rc, out, err = None, (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""), "timeout"
    return {"rc": rc, "stdout": out, "stderr_tail": (err or "")[-1500:], "events": bootstrap_events(out),
            "seconds": round(time.time() - t0, 1)}


PREFLIGHT_CASES = [
    # (case, client: "good" or a fake key, injection, text the message must contain, failing step)
    ("intel_mac", "good", {"arch": "x86_64"}, ["Apple Silicon"], "preflight"),
    ("old_macos", "good", {"macos": "15.5"}, ["macOS 26.2 or newer", "Software Update"], "preflight"),
    ("no_rosetta", "good", {"rosetta": False}, ["softwareupdate --install-rosetta"], "preflight"),
    ("no_command_line_tools", "good", {"clt": False}, ["xcode-select --install"], "preflight"),
    ("no_homebrew", "good", {"brew": False}, ["https://brew.sh", "install.sh"], "preflight"),
    ("brew_missing_declined", "good", {"brew_missing": ["cmake"], "answer": "n"},
     ["cmake", "brew bundle install --no-upgrade", "declined"], "brew"),
    ("low_disk", "good", {"free_bytes": 3 * 10**9}, ["not enough free disk space", "3.0 GB free"], "preflight"),
    ("client_wrong_version", "wrong_version_folder", {}, ["3.0.1.301", "not 3.3.5.12340"], "preflight"),
    ("client_wrong_locale", "wrong_locale_zip", {}, ["deDE", "not enUS"], "preflight"),
    ("not_zip_or_folder", "text_file", {}, ["neither a zip file nor a folder"], "preflight"),
    ("path_missing", "missing_path", {}, ["doesn't exist"], "preflight"),
]


# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "Brewfile",
    "bootstrap",
    "fork.lock#*_url",
    "fork.lock#*_sha256",
    "fork.lock#ghidra_version",
    "harness/hslib/baseline.py",
    "harness/hslib/build.py",
    "harness/hslib/checks/setup_checks.py",
    "harness/hslib/clientinst.py",
    "harness/hslib/downloads.py",
    "harness/hslib/pristine.py",
)


@check("setup.preflight", order=31, capability="easy-setup", stack="any", layer="unit", parallel_safe=True)
def setup_preflight(ctx):
    """In a scratch copy of the repo, ./bootstrap stops before changing anything (scratch tree byte-for-byte unchanged),
    exits nonzero and names the problem and the fix for: an Intel Mac, an old macOS, no Rosetta, no Command Line Tools,
    no Homebrew, a missing Brewfile package with the prompt declined, too little disk, a client of another version,
    another language, a path that isn't a zip or folder, and a missing path (injected through $HS_BOOTSTRAP_TEST, which
    is refused without the test flag or outside a harness run). The un-injected check on this Mac passes."""
    repo = light_copy("preflight")
    fakes_dir = SCRATCH_ROOT / f"preflight-clients-{os.getpid()}"
    ev: dict = {"scratch": str(repo), "cases": {}}
    try:
        # belt and braces: if preflight ever let a run through, the build step fails at once instead of cloning
        lock = repo / "fork.lock"
        lock.write_text(re.sub(r"^(core_repo|playerbots_repo|autobalance_repo)=.*$", r"\1=file:///nonexistent/hs-preflight-check",
                               lock.read_text(), flags=re.M))
        fakes = _fake_clients(fakes_dir)
        fakes["good"] = P.CLIENT
        base_env = scratch_env(HS_HARNESS_RUN="1")
        for case, client, inj, texts, step in PREFLIGHT_CASES:
            before = tree_fingerprint(repo)
            env = base_env | ({"HS_BOOTSTRAP_TEST": json.dumps(inj)} if inj else {})
            r = run_bootstrap(repo, [str(fakes[client]), "--allow-test-inject"], env)
            after = tree_fingerprint(repo)
            out = r["stdout"]
            failed = [e for e in r["events"] if e.get("state") == "failed"]
            started = [e.get("step") for e in r["events"] if e.get("state") in ("start", "done")
                       and e.get("step") not in ("preflight", step)]
            rec = {"rc": r["rc"], "injected": inj, "client": str(fakes[client]), "failed_step": [e.get("step") for e in failed],
                   "message": [l for l in out.splitlines() if l.strip().startswith(("Problem:", "What to do:"))],
                   "missing_text": [t for t in texts if t not in out], "steps_started": started,
                   "tree_changes": _tree_diff(before, after), "seconds": r["seconds"]}
            ev["cases"][case] = rec
            require(r["rc"] not in (0, None), f"{case}: ./bootstrap exited {r['rc']}, expected a nonzero exit", **ev)
            require([e.get("step") for e in failed] == [step], f"{case}: didn't stop at {step}", **ev)
            require(not rec["missing_text"], f"{case}: the message doesn't name {rec['missing_text']}", **ev)
            require(not started, f"{case}: a setup step started ({started})", **ev)
            require(not rec["tree_changes"], f"{case}: the repo tree changed: {rec['tree_changes']}", **ev)
        # the test switch can't leak: refused without the flag, and with the flag outside a harness run
        before = tree_fingerprint(repo)
        inj = json.dumps({"arch": "arm64"})
        r1 = run_bootstrap(repo, [str(P.CLIENT), "--check"], base_env | {"HS_BOOTSTRAP_TEST": inj})
        r2 = run_bootstrap(repo, [str(P.CLIENT), "--check", "--allow-test-inject"],
                           {k: v for k, v in base_env.items() if k != "HS_HARNESS_RUN"} | {"HS_BOOTSTRAP_TEST": inj})
        ev["switch_refused"] = {"without_flag": {"rc": r1["rc"], "out": r1["stdout"][-300:]},
                                "outside_harness": {"rc": r2["rc"], "out": r2["stdout"][-300:]},
                                "tree_changes": _tree_diff(before, tree_fingerprint(repo))}
        require(r1["rc"] == 2 and "test switch" in r1["stdout"] and not r1["events"],
                "$HS_BOOTSTRAP_TEST without --allow-test-inject wasn't refused", **ev)
        require(r2["rc"] == 2 and "harness only" in r2["stdout"] and not r2["events"],
                "--allow-test-inject outside a harness run wasn't refused", **ev)
        # control: the real preflight on this Mac with the real client passes (so the failures above are the injected
        # conditions, not a preflight that always fails)
        before = tree_fingerprint(repo)
        r3 = run_bootstrap(repo, [str(P.CLIENT), "--check"], base_env)
        pre = next((e for e in r3["events"] if e.get("step") == "preflight" and e.get("state") == "done"), None)
        ev["control"] = {"rc": r3["rc"], "preflight": pre, "seconds": r3["seconds"],
                         "tree_changes": _tree_diff(before, tree_fingerprint(repo))}
        require(r3["rc"] == 0 and pre, "control: the un-injected preflight didn't pass on this Mac", **ev)
        require(not ev["control"]["tree_changes"], "control: --check changed the repo tree", **ev)
        return ev
    finally:
        drop(repo)
        drop(fakes_dir)


# ================================================================ build.downloads_pinned

# Every place the harness fetches over the network: the pinned downloads, Ghidra's own pinned fetch, and the local SOAP
# client (127.0.0.1 only, not a download). A new caller fails build.downloads_pinned until it's reviewed here.
NETWORK_CALLERS = {"harness/hslib/downloads.py": "pinned downloads (PINS, sha256-verified)",
                   "harness/hslib/ghidra.py": "the pinned Ghidra (ghidra_sha256-verified)",
                   "harness/hslib/soap.py": "SOAP to the local worldserver at 127.0.0.1 (not a download)",
                   "harness/hslib/bootstrap.py": "prints Homebrew's install command (curl ... install.sh) for the "
                                                 "person to run; never runs it"}


@check("build.downloads_pinned", order=32, capability="server-build", stack="any", layer="unit", parallel_safe=True)
def downloads_pinned(ctx):
    """Every download URL in fork.lock (all *_url keys but the git remotes) has a sha256 pin, and every download path
    goes through those pins; in a scratch copy of the repo, a cached file with a wrong hash is refused for each pin
    (client-data via `hs download client-data`, the Wine runtime via `hs wine-install`, VMaNGOS and Ghidra via
    `hs download`): the bad file is removed and nothing is unpacked; and the real VMaNGOS zip passes its pin."""
    from .. import downloads
    lock = P.fork_lock()
    url_keys = sorted(k for k in lock if k.endswith("_url") or k.endswith("_repo"))
    file_urls = sorted(k for k in url_keys if k not in downloads.GIT_URL_KEYS)
    covered = {v[0]: v[1] for v in downloads.PINS.values()}
    ev: dict = {"fork_lock_url_keys": url_keys, "git_remotes": list(downloads.GIT_URL_KEYS),
                "pins": {k: {"sha256_key": covered.get(k), "sha256": lock.get(covered.get(k, ""))} for k in file_urls}}
    bad_pins = [k for k in file_urls if k not in covered or not re.fullmatch(r"[0-9a-f]{64}", lock.get(covered[k], ""))]
    require(not bad_pins, f"download URL(s) without a sha256 pin: {bad_pins}", **ev)
    # every network fetch in the harness goes through downloads.py (ghidra.py keeps its own, verified, fetch)
    hits = {}
    for f in sorted((P.HARNESS / "hslib").rglob("*.py")):
        if f.resolve() == Path(__file__).resolve():
            continue                                      # this scanner names the call it looks for
        t = f.read_text(errors="replace")
        if "urlopen(" in t or "curl " in t:
            hits[str(f.relative_to(P.ROOT))] = len(re.findall(r"urlopen\(|curl ", t))
    ev["network_fetch_callers"] = hits
    ev["allowed_callers"] = NETWORK_CALLERS
    require(set(hits) <= set(NETWORK_CALLERS), "a network fetch outside the pinned download paths", **ev)
    gsrc = (P.HARNESS / "hslib" / "ghidra.py").read_text()
    require("ghidra_sha256" in gsrc and "SHA-256 mismatch" in gsrc, "ghidra.py doesn't verify its pin", **ev)

    repo = light_copy("downloads")
    try:
        cache = repo.parent / f"downloads-cache-{os.getpid()}"
        cache.mkdir(parents=True, exist_ok=True)
        env = scratch_env(HS_HARNESS_RUN="1", HS_DOWNLOAD_CACHE=str(cache))
        cases = {"client-data": [str(repo / "harness/hs"), "download", "client-data"],
                 "wine-runtime": [str(repo / "harness/hs"), "wine-install"],
                 "vmangos": [str(repo / "harness/hs"), "download", "vmangos"],
                 "ghidra": [str(repo / "harness/hs"), "download", "ghidra"]}
        ev["refusals"] = {}
        for name, cmd in cases.items():
            p = downloads.pin(name, lock)
            junk = cache / p["file"]
            junk.write_bytes(b"not the pinned file " + name.encode() + b"\n" * 100)
            r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120, cwd=repo)
            leftovers = sorted(str(x.relative_to(repo)) for x in repo.rglob("*")
                               if x.name.endswith((".part", ".unpack")) or x.name in ("client-data", "wowsilicon-r17"))
            rec = {"cmd": " ".join(cmd[1:]), "rc": r.returncode, "stderr_tail": r.stderr[-400:],
                   "cached_bad_file_removed": not junk.exists(), "unpacked_or_partial": leftovers,
                   "mismatch_named": "SHA-256 mismatch" in (r.stderr + r.stdout)}
            ev["refusals"][name] = rec
            require(r.returncode != 0 and rec["mismatch_named"], f"{name}: a wrong-hash file wasn't refused", **ev)
            require(rec["cached_bad_file_removed"], f"{name}: the wrong-hash file wasn't removed", **ev)
            require(not leftovers, f"{name}: something was unpacked or left partial: {leftovers}", **ev)
        # control: the real VMaNGOS zip (the main checkout's, read-only; APFS clone) passes its pin and is accepted
        real = P.DOWNLOADS / "vmangos-db-sqlite.zip"
        if real.is_file():
            p = downloads.pin("vmangos", lock)
            cp = subprocess.run(["/bin/cp", "-c", str(real), str(cache / p["file"])], capture_output=True, text=True)
            r = subprocess.run(cases["vmangos"], capture_output=True, text=True, env=env, timeout=120, cwd=repo)
            ev["control_vmangos"] = {"rc": r.returncode, "copy_rc": cp.returncode, "stdout": r.stdout[-500:]}
            require(cp.returncode == 0 and r.returncode == 0 and '"from": "cache"' in r.stdout,
                    "control: the real VMaNGOS zip wasn't accepted against its pin", **ev)
        else:
            ev["control_vmangos"] = "skipped: no data/downloads/vmangos-db-sqlite.zip on this machine"
        return ev
    finally:
        drop(repo)
        drop(repo.parent / f"downloads-cache-{os.getpid()}")


# ================================================================ repo.portable

def portability_patterns() -> list[tuple[str, str]]:
    """(label, regex) for machine-specific things: home paths of any user, this Mac's user name, home folder,
    hostnames, the repo's own absolute path, the dev Mac's repo-folder convention, and an nvm install path."""
    user = getpass.getuser()
    home = str(Path.home())
    hosts = {socket.gethostname(), socket.gethostname().removesuffix(".local")}
    for key in ("ComputerName", "LocalHostName"):
        r = subprocess.run(["/usr/sbin/scutil", "--get", key], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            hosts.add(r.stdout.strip())
    users_dir = "/" + "Users" + "/"                    # spelled so this file doesn't match itself
    pats = [("absolute home path", re.escape(users_dir) + r"[A-Za-z0-9._-]+"),
            ("encoded home path", r"-" + "Users" + r"-[A-Za-z0-9._]+"),
            ("tilde-user path", r"(?<![\w~])~[a-z_][a-z0-9_-]*/"),
            ("user name", rf"(?<![A-Za-z0-9]){re.escape(user)}(?![A-Za-z0-9])"),
            ("home folder", re.escape(home)),
            ("repo path", re.escape(str(P.ROOT.resolve()))),
            ("repo-folder convention", r"Documents/" + r"repos/"),
            ("nvm install path", r"\.nvm/" + r"versions/")]
    pats += [("hostname", re.escape(h)) for h in sorted(hosts) if h]
    return pats


# Reviewed allowlist: (path glob, pattern label or "*", reason). Short on purpose; every entry says why.
PORTABLE_ALLOW = [
    ("STATUS.md", "*", "the orchestrator's status log of the dev Mac (builders may not edit it); its Node path and "
                       "old-folder notes describe that Mac, not setup"),
    ("openspec/changes/archive/2026-10-06-rename-moonglade/*", "*", "archived record of the folder rename (history)"),
    ("openspec/changes/easy-setup/specs/easy-setup/spec.md", "absolute home path",
     "the scenario names the pattern the scan looks for"),
    ("openspec/specs/easy-setup/spec.md", "absolute home path", "the same scenario after archiving"),
    ("docs/research/login-screen-spike.md", "user name", "a heading anchor (#recommendation-for-...) naming the person "
                                                         "the report was for, not this Mac's account"),
]


def _allowed(rel: str, label: str) -> str | None:
    import fnmatch
    for glob, lab, why in PORTABLE_ALLOW:
        if fnmatch.fnmatch(rel, glob) and lab in ("*", label):
            return why
    return None


def portability_scan(root: Path, files: list[str]) -> dict:
    pats = [(label, re.compile(rx)) for label, rx in portability_patterns()]
    hits, allowed, binary = [], [], 0
    for rel in files:
        try:
            data = (root / rel).read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            binary += 1
            continue
        text = data.decode("utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            for label, rx in pats:
                m = rx.search(line)
                if m:
                    rec = {"file": rel, "line": n, "pattern": label, "match": m.group(0)}
                    why = _allowed(rel, label)
                    (allowed if why else hits).append(rec | ({"allowed": why} if why else {}))
    return {"files": len(files), "binary_skipped": binary, "hits": hits, "allowed": allowed,
            "patterns": [l for l, _ in pats]}


@check("repo.portable", order=33, capability="easy-setup", stack="any", layer="unit", parallel_safe=True)
def repo_portable(ctx):
    """No tracked (or new, unignored) file has a home path, this Mac's user name, home folder or hostname, the repo's
    absolute path, the dev Mac's repo-folder convention or an nvm install path, except the reviewed allowlist; and in a
    scratch copy with one of each planted, the scan reports every planted hit."""
    files = repo_files()
    scan = portability_scan(P.ROOT, files)
    ev = {"scanned": scan["files"], "binary_skipped": scan["binary_skipped"], "patterns": scan["patterns"],
          "hits": scan["hits"][:40], "allowlist": [{"path": g, "pattern": l, "why": w} for g, l, w in PORTABLE_ALLOW],
          "allowed_hits": len(scan["allowed"]),
          "allowed_by_file": sorted({a["file"] for a in scan["allowed"]})}
    require(scan["files"] > 100, "the scan saw too few files (git ls-files failed?)", **ev)
    require(not scan["hits"], f"{len(scan['hits'])} machine-specific hit(s) in tracked files", **ev)
    # the scan can fail: plant one hit per pattern in a scratch copy
    repo = light_copy("portable")
    try:
        planted = {}
        user, home = getpass.getuser(), str(Path.home())
        samples = {"absolute home path": "/" + "Users/someone/game", "encoded home path": "-" + "Users-someone-game",
                   "tilde-user path": "~" + "someone/game", "user name": f"owner {user} here", "home folder": home + "/x",
                   "repo path": str(P.ROOT.resolve()) + "/env", "repo-folder convention": "~/Documents/" + "repos/game",
                   "nvm install path": "~/.nvm/" + "versions/node/v20.0.0/bin"}
        for label, rx in portability_patterns():
            text = samples.get(label) or (re.sub(r"\\(.)", r"\1", rx) if label == "hostname" else None)
            if text is None:
                continue
            f = repo / "planted" / f"{len(planted):02d}.md"
            f.parent.mkdir(exist_ok=True)
            f.write_text(f"planted: {text}\n")
            planted[str(f.relative_to(repo))] = label
        # also inside an existing tracked file
        readme = repo / "README.md"
        readme.write_text(readme.read_text() + "\nSee /" + "Users/someone/notes.\n")
        planted["README.md"] = "absolute home path"
        res = portability_scan(repo, repo_files_in(repo))
        found = {(h["file"], h["pattern"]) for h in res["hits"]}
        missed = [(f, l) for f, l in planted.items() if (f, l) not in found]
        ev["planted"] = planted
        ev["planted_missed"] = missed
        ev["planted_scan_hits"] = len(res["hits"])
        require(not missed, f"the scan missed planted hits: {missed}", **ev)
        return ev
    finally:
        drop(repo)


def repo_files_in(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


# ================================================================ build.moved_repo

OLD_REPO = "/Volumes/hs-old-location/project-moonglade"        # a folder the planted records name (doesn't exist)


def _plant_moved(repo: Path) -> dict:
    """In a light scratch copy: fake installed binaries, an install record and a CMakeCache naming OLD_REPO."""
    (repo / "server" / "bin").mkdir(parents=True)
    for n in ("authserver", "worldserver"):
        f = repo / "server" / "bin" / n
        f.write_text("#!/bin/sh\necho 'planted binary: must never run' >&2\nexit 1\n")
        f.chmod(0o755)
    (repo / "server" / ".hs-install.json").write_text(json.dumps({"repo": OLD_REPO, "build": OLD_REPO + "/build",
                                                                  "server": OLD_REPO + "/server", "by": "planted"}))
    src_cache = P.BUILD / "CMakeCache.txt"
    text = src_cache.read_text(errors="replace").replace(str(P.ROOT.resolve()), OLD_REPO)
    (repo / "build").mkdir()
    (repo / "build" / "CMakeCache.txt").write_text(text)
    lock = repo / "fork.lock"         # a build that gets past the moved check fails at the clone, fast and offline
    lock.write_text(re.sub(r"^(core_repo|playerbots_repo|autobalance_repo)=.*$", r"\1=file:///nonexistent/hs-moved-check",
                           lock.read_text(), flags=re.M))
    return {"install_record": OLD_REPO, "cmake_cache_dir": OLD_REPO + "/build"}


@check("build.moved_repo", order=21, capability="server-build", stack="down", layer="gate")
def moved_repo(ctx):
    """A scratch copy whose install record and CMakeCache name another folder: `hs up` and ./play refuse before
    starting anything, naming the old and the new folder and `harness/hs build --force-clean`; `hs build` discards the
    foreign build tree and starts from empty; and the baseline build, given a planted foreign CMakeCache, discards it
    and configures a fresh tree."""
    from .. import baseline, build, procs, stack
    from .play_checks import is_clear, remaining
    ev: dict = {"given_nothing_running": remaining()}
    require(is_clear(ev["given_nothing_running"]), "GIVEN not met: something from this repo is running", **ev)
    repo = light_copy("moved")
    try:
        ev["planted"] = _plant_moved(repo)
        env = scratch_env(HS_HARNESS_RUN="1")
        new = str(repo.resolve())
        for name, cmd in (("hs_up", [str(repo / "harness/hs"), "up"]), ("play", [str(repo / "play")])):
            r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120, cwd=repo)
            out = r.stdout + r.stderr
            rec = {"rc": r.returncode, "output_tail": out[-700:], "names_old": OLD_REPO in out, "names_new": new in out,
                   "names_fix": build.MOVED_FIX in out,
                   "scratch_env_created": sorted(str(x.relative_to(repo)) for x in (repo / "env").rglob("*"))
                   if (repo / "env").exists() else [],
                   "scratch_processes": [c for _pid, c in procs.table() if new in c],
                   "ports_open": [p for p in P.STACK_PORTS if stack.port_open(p)]}
            ev[name] = rec
            require(r.returncode != 0, f"{name}: didn't refuse (exit {r.returncode})", **ev)
            require(rec["names_old"] and rec["names_new"] and rec["names_fix"],
                    f"{name}: the message doesn't name the old folder, the new folder and `{build.MOVED_FIX}`", **ev)
            require(not rec["scratch_env_created"] and not rec["scratch_processes"] and not rec["ports_open"],
                    f"{name}: something was started or written before the refusal", **ev)
        # hs build: discards the foreign tree and starts from empty (the scratch's broken remote stops it right after)
        r = subprocess.run([str(repo / "harness/hs"), "build"], capture_output=True, text=True, env=env, timeout=300,
                           cwd=repo)
        ev["hs_build"] = {"rc": r.returncode, "stderr_tail": r.stderr[-1200:],
                          "says_rebuilding_from_empty": "rebuilding from empty" in r.stderr,
                          "planted_build_tree_gone": not (repo / "build" / "CMakeCache.txt").exists(),
                          "stopped_at_clone": "hs-moved-check" in r.stderr,
                          "install_record_still_old": (build.read_install_record(repo / "server/.hs-install.json")
                                                       or {}).get("repo") == OLD_REPO}
        h = ev["hs_build"]
        require(h["says_rebuilding_from_empty"] and h["planted_build_tree_gone"],
                "hs build didn't discard the foreign build tree and start from empty", **ev)
        require(h["stopped_at_clone"] and h["install_record_still_old"],
                "hs build didn't proceed to the (from-empty) sync as expected, or rewrote the record without building",
                **ev)
    finally:
        drop(repo)
    # the baseline build: a planted foreign CMakeCache in a scratch build dir is discarded and a fresh tree configured
    src = baseline.SRC if (baseline.SRC / "CMakeLists.txt").is_file() else P.CORE_SRC
    bdir = P.ROOT / "baseline" / f"check-moved-build-{os.getpid()}"
    shutil.rmtree(bdir, ignore_errors=True)
    bdir.mkdir(parents=True)
    try:
        base_cache = baseline.BUILD / "CMakeCache.txt"
        text = (base_cache if base_cache.is_file() else P.BUILD / "CMakeCache.txt").read_text(errors="replace")
        (bdir / "CMakeCache.txt").write_text(text.replace(str(P.ROOT.resolve()), OLD_REPO))
        ev["baseline"] = {"src": str(src), "build_dir": str(bdir),
                          "planted_cache": build.cmake_cache_info(bdir)}
        src_status_before = subprocess.run(["git", "-C", str(src), "status", "--porcelain"], capture_output=True,
                                           text=True).stdout
        try:
            res = baseline.build_install(build_dir=bdir, src=src, configure_only=True,
                                         log_path=P.OUT / "moved-check-configure.log")
        except baseline.BaselineError as e:
            ev["baseline"]["error"] = {"msg": str(e), **e.evidence,
                                       "log_tail": (P.OUT / "moved-check-configure.log").read_text(errors="replace")[-1500:]}
            require(False, "the baseline build didn't configure after a planted foreign CMakeCache", **ev)
        ev["baseline"] |= {"result": res, "src_status_unchanged": subprocess.run(
            ["git", "-C", str(src), "status", "--porcelain"], capture_output=True, text=True).stdout == src_status_before}
        info = res.get("cache") or {}
        require(res.get("discarded_foreign_build") and res["discarded_foreign_build"].get("removed"),
                "the baseline build didn't discard the foreign build tree", **ev)
        require(os.path.realpath(info.get("cache_dir", "")) == os.path.realpath(bdir)
                and all(s["exit"] == 0 for s in res["steps"]),
                "the baseline's fresh configure didn't produce a cache for this folder", **ev)
        return ev
    finally:
        shutil.rmtree(bdir, ignore_errors=True)


# ================================================================ client.player_client_not_rebuilt

def _perturb_inputs(scratch: Path) -> dict:
    """Make a scratch play client's recorded patch inputs differ from the current ones, as a changed client-mods/ file
    would (the stamp's record of one source's hash is changed; the build outputs are untouched)."""
    st = clientbuild.read_stamp(scratch)
    for p in st["inputs"]["patches"]:
        if p.get("sources"):
            k = sorted(p["sources"])[0]
            p["sources"][k] = "0" * 64
            (scratch / clientbuild.STAMP).write_text(json.dumps(st, indent=2) + "\n")
            return {"patch": p["id"], "source": k}
    raise RuntimeError("no patch with sources to perturb")


@check("client.player_client_not_rebuilt", order=998, capability="play-launcher", stack="down", layer="integration")
def player_client_not_rebuilt(ctx):
    """The player's env/client-play/ build (folder inode; hs-build.json, Wow.exe and every patch output: SHA-256, inode,
    mtime) is identical to the start of the run. Probe: a harness-shaped ./play stand-in run on a scratch play client
    standing in for the player's ($HS_TEST_OWN_CLIENT) whose patch inputs differ from its build leaves it identical and
    records "not rebuilt"; control: the play loop's genuine-launch path on another such scratch does rebuild it."""
    from .. import play
    from .play_checks import StandIn, is_clear, remaining, require_given_clear
    from .playclient_util import drop_scratch, fresh_scratch
    start = (ctx or {}).get("player_play_client_fingerprint_start", "missing")
    ev: dict = {"player_fingerprint_start": start,
                "player_inputs_current_at_start": (ctx or {}).get("player_play_client_current_start")}
    require(start != "missing" and not (isinstance(start, dict) and "error" in start),
            "the runner didn't record the player's play client at the start of the run", **ev)
    require_given_clear(ev)
    probe, control = fresh_scratch("f28-probe"), fresh_scratch("f28-control")
    try:
        clientbuild.build(dest=probe)
        ev["probe_perturbed"] = _perturb_inputs(probe)
        ev["probe_given_inputs_differ"] = clientbuild.up_to_date(probe)
        require(not ev["probe_given_inputs_differ"]["current"], "GIVEN not met: the probe's inputs don't differ", **ev)
        before = clientbuild.fingerprint(probe)
        s = StandIn("f28")
        sfile = s.dir / "settings.json"
        sfile.write_text(json.dumps({"client": "patched"}))
        try:
            s.start_play("--allow-test-client-dir", env={"HS_SETTINGS": str(sfile), "HS_PLAY_CLIENT_DIR": str(probe),
                                                         "HS_TEST_OWN_CLIENT": str(probe)})
            up = s.wait_client_up()
            s.stop_file.write_text("quit")
            rc, _ = s.wait_exit(timeout=300)
            rec = json.loads(play.LAST.read_text()) if play.LAST.exists() else {}
            rec = rec if rec.get("pid") == (s.proc.pid if s.proc else None) else {}
            ev["probe_run"] = {"client_up": up.get("up"), "exit_code": rc, "record_client": rec.get("client"),
                               "remaining_after": remaining(s)}
        finally:
            s.cleanup()
        after = clientbuild.fingerprint(probe)
        ev["probe_fingerprint_unchanged"] = before == after
        c = ev["probe_run"]["record_client"] or {}
        require(ev["probe_run"]["exit_code"] == 0 and ev["probe_run"]["client_up"],
                "probe: the harness-shaped stand-in run didn't run and end cleanly", **ev)
        require(c.get("players_own") is True and c.get("built") is False and c.get("current") is False,
                "probe: the run didn't treat the scratch as the player's stale play client and leave it unbuilt", **ev)
        require(ev["probe_fingerprint_unchanged"], "probe: the harness run rebuilt the player-standing scratch", **ev,
                fingerprint_before=before, fingerprint_after=after)
        require(is_clear(ev["probe_run"]["remaining_after"]), "probe: something is still running", **ev)
        # control: the genuine-launch path (no harness signs) on another stale scratch marked as the player's
        clientbuild.build(dest=control)
        ev["control_perturbed"] = _perturb_inputs(control)
        cbefore = clientbuild.fingerprint(control)
        genuine = play._parse([])
        res = play.prepare_play_client(control, genuine, environ={}, player_dir=control)
        cafter = clientbuild.fingerprint(control)
        ev["control"] = {"harness_signs": res.get("harness_signs"), "players_own": res.get("players_own"),
                         "built": res.get("built"), "rebuilt_fingerprint_changed": cbefore != cafter,
                         "dir_inode_changed": (cbefore or {}).get("dir_ino") != (cafter or {}).get("dir_ino")}
        require(res.get("players_own") and res.get("harness_signs") == [] and res.get("built")
                and ev["control"]["rebuilt_fingerprint_changed"],
                "control: the genuine-launch path didn't rebuild a stale player client (so the guard proves nothing)",
                **ev)
        # the same in-process path, harness-shaped: not rebuilt
        _perturb_inputs(control)
        c2 = clientbuild.fingerprint(control)
        res2 = play.prepare_play_client(control, genuine, environ={"HS_HARNESS_RUN": "1"}, player_dir=control)
        ev["guarded_in_process"] = {"built": res2.get("built"), "signs": res2.get("harness_signs"),
                                    "unchanged": clientbuild.fingerprint(control) == c2}
        require(res2.get("built") is False and ev["guarded_in_process"]["unchanged"],
                "the harness-shaped in-process path rebuilt the player-standing scratch", **ev)
    finally:
        drop_scratch(probe)
        drop_scratch(control)
    now = clientbuild.fingerprint(clientbuild.PLAY)
    ev["player_fingerprint_now"] = now
    require(now == start, "the player's play client build changed during this run (rebuilt, re-patched or replaced)",
            **ev)
    return ev


# ================================================================ setup.fresh_clone

def _standin_bat(d: Path) -> tuple[Path, Path, Path]:
    d.mkdir(parents=True, exist_ok=True)
    bat, started, stop = d / "hs-standin.bat", d / "started", d / "stop"
    win = lambda p: "Z:" + str(p).replace("/", "\\")
    bat.write_text("\r\n".join(["@echo off", f"echo started> {win(started)}", ":loop",
                                f"if exist {win(stop)} goto done", "ping -n 2 127.0.0.1 >nul", "goto loop", ":done",
                                "exit 0", ""]))
    return bat, started, stop


def _clone_processes(clone: Path) -> list[str]:
    from .. import procs
    s = str(clone.resolve())
    return [c[:200] for _pid, c in procs.table() if s in c]


def _play_in_clone(clone: Path, tag: str) -> dict:
    """./play in the scratch clone with a stand-in client: reaches "ready", quits, everything stops."""
    from .. import stack
    d = SCRATCH_ROOT / f"standin-{tag}"
    shutil.rmtree(d, ignore_errors=True)
    bat, started, stop = _standin_bat(d)
    out = SCRATCH_ROOT / f"play-{tag}.out"
    env = scratch_env(HS_HARNESS_RUN="1", HS_PROFILE="m0")
    t0 = time.time()
    with open(out, "w") as fh:
        proc = subprocess.Popen([str(clone / "play"), "--account", "test", "--client-cmd", str(bat)], cwd=clone,
                                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, env=env,
                                start_new_session=True)
    ready_at = None
    try:
        while time.time() - t0 < 1800 and proc.poll() is None:
            if '"id":"ready"' in out.read_text(errors="replace") or '"id": "ready"' in out.read_text(errors="replace"):
                ready_at = round(time.time() - t0, 1)
                break
            time.sleep(2)
        stop.write_text("quit")
        try:
            rc = proc.wait(timeout=600)
        except subprocess.TimeoutExpired:
            rc = None
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    text = out.read_text(errors="replace")
    time.sleep(2)
    left = _clone_processes(clone)
    ev = {"exit_code": rc, "ready_after_s": ready_at, "standin_started": started.exists(),
          "total_s": round(time.time() - t0, 1), "all_stopped_line": "All stopped." in text,
          "processes_left": left, "ports_open": [p for p in P.STACK_PORTS if stack.port_open(p)],
          "output_tail": text[-1500:]}
    shutil.rmtree(d, ignore_errors=True)
    return ev


def _outputs_snapshot(clone: Path) -> dict:
    """(size, mtime_ns, inode) of the build, data, database, Wine, client, play client and app outputs."""
    rels = ["server/bin/worldserver", "server/bin/authserver", "server/bin/.hs-input-hash", "server/.hs-install.json",
            "build/CMakeCache.txt", "build/.hs-input-hash", "data/client-data/.hs-client-data.json",
            "data/client-data/dbc/Spell.dbc", "data/.hs-db-ready.json", "env/wowsilicon-r17/.hs-runtime-sha256",
            "env/wowsilicon-r17/.wine-runtime/bin/wine", "client/Wow.exe", "env/client-manifest.json",
            "env/client-play/hs-build.json", "env/client-play/Wow.exe", "dist/Moonglade.app/Contents/MacOS/Moonglade",
            "dist/app-stamp.json", "env/settings.json"]
    out = {}
    for r in rels:
        f = clone / r
        st = f.stat() if f.exists() else None
        out[r] = [st.st_size, st.st_mtime_ns, st.st_ino] if st else None
    for d in ("build", "data/client-data", "client", "env/client-play", "dist/Moonglade.app", "vendor/azerothcore-wotlk"):
        f = clone / d
        out[d + "/"] = [f.stat().st_mtime_ns, f.stat().st_ino] if f.exists() else None
    return out


def _verify_install(clone: Path, tag: str, ev: dict) -> dict:
    """THEN of "Fresh clone to a playable app" (everything but ./play): server, data, DB, Wine, client.inspect, play
    client, signed app, defaults (+ the docs table)."""
    v: dict = {}
    pin = P.fork_lock()
    v["server_binaries"] = {n: os.access(clone / "server/bin" / n, os.X_OK) for n in ("authserver", "worldserver")}
    try:
        v["install_record_repo"] = json.loads((clone / "server/.hs-install.json").read_text()).get("repo")
    except (OSError, ValueError):
        v["install_record_repo"] = None
    v["client_data_dirs"] = sorted(p.name for p in (clone / "data/client-data").iterdir()) \
        if (clone / "data/client-data").is_dir() else []
    v["databases"] = {n: (clone / "data/mysql" / n).is_dir() for n in
                      ("acore_auth", "acore_world", "acore_characters", "acore_playerbots")}
    m = clone / "env/wowsilicon-r17/.hs-runtime-sha256"
    v["wine_runtime_pinned"] = m.exists() and m.read_text().strip() == pin["wowsilicon_runtime_sha256"] \
        and (clone / "env/wowsilicon-r17/.wine-runtime/bin/wine").exists()
    rdir = clone / "harness" / "out" / f"inspect-{tag}"      # the runner writes results inside its own repo
    shutil.rmtree(rdir, ignore_errors=True)
    r = subprocess.run([str(clone / "harness/hs"), "run", "--tier", "unit", "--only", "client.inspect",
                        "--results-dir", str(rdir)], capture_output=True, text=True, env=scratch_env(HS_HARNESS_RUN="1"),
                       cwd=clone, timeout=600)
    try:
        recs = {c["id"]: c for c in json.loads((rdir / "results.json").read_text())["checks"]}
        ci = recs.get("client.inspect") or {}
    except (OSError, ValueError, KeyError):
        ci = {}
    v["client_inspect"] = {"status": ci.get("status"), "message": ci.get("message"),
                           "client_dir": (ci.get("evidence") or {}).get("client_dir"), "run_rc": r.returncode,
                           **({} if ci else {"run_stderr_tail": r.stderr[-800:]})}
    shutil.rmtree(rdir, ignore_errors=True)
    rec0001 = json.loads(sorted((clone / "patches/client").glob("0001-*.json"))[0].read_text())
    from ..clientinst import sha256_file
    pw = clone / "env/client-play/Wow.exe"
    v["play_client"] = {"stamp": (clone / "env/client-play/hs-build.json").is_file(),
                        "wow_exe_is_patch_0001_output": pw.is_file() and sha256_file(pw) == rec0001["output_sha256"]}
    app = clone / "dist/Moonglade.app"
    cs = subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True,
                        text=True)
    dr = subprocess.run(["/usr/bin/codesign", "-d", "-r-", str(app)], capture_output=True, text=True)
    import plistlib
    try:
        plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    except (OSError, ValueError):
        plist = {}
    v["app"] = {"codesign_verify_rc": cs.returncode, "designated": (dr.stdout + dr.stderr).strip()[-200:],
                "bundle_id": plist.get("CFBundleIdentifier"),
                "repo_path": (app / "Contents/Resources/repo-path").read_text().strip()
                if (app / "Contents/Resources/repo-path").exists() else None}
    # defaults, read through the scratch clone's own harness
    def hs_json(*a):
        rr = subprocess.run([str(clone / "harness/hs"), *a], capture_output=True, text=True,
                            env=scratch_env(HS_HARNESS_RUN="1"), cwd=clone, timeout=60)
        try:
            return json.loads(rr.stdout)
        except ValueError:
            return {"rc": rr.returncode, "stderr": rr.stderr[-300:]}
    probe = subprocess.run(["python3", "-c", "import json; from hslib import settings as s; "
                                             "print(json.dumps({'defaults': {k: d['value'] for k, d in s.DEFAULTS.items()}, "
                                             "'table': s.defaults_table_md()}))"],
                           capture_output=True, text=True, cwd=clone, timeout=60,
                           env=scratch_env(PYTHONPATH=str(clone / "harness")))
    try:
        dj = json.loads(probe.stdout)
    except ValueError:
        dj = {"defaults": {}, "table": None, "error": probe.stderr[-300:]}
    docs = (clone / "docs/setup.md").read_text()
    mm = re.search(r"<!-- defaults:begin -->\n(.*?)<!-- defaults:end -->", docs, re.S)
    v["defaults"] = {"declared": dj["defaults"], "world_size": hs_json("world-size").get("setting"),
                     "vanilla_mode": hs_json("vanilla-mode").get("mode"), "client": hs_json("client-mode").get("mode"),
                     "docs_table_matches_source": bool(mm) and mm.group(1) == dj.get("table")}
    ok = (all(v["server_binaries"].values()) and v["install_record_repo"] == str(clone.resolve())
          and {"Cameras", "dbc", "maps", "mmaps", "vmaps"} <= set(v["client_data_dirs"])
          and all(v["databases"].values()) and v["wine_runtime_pinned"] and v["client_inspect"]["status"] == "pass"
          and v["client_inspect"]["client_dir"] == str(clone / "client")
          and v["play_client"]["stamp"] and v["play_client"]["wow_exe_is_patch_0001_output"]
          and v["app"]["codesign_verify_rc"] == 0 and v["app"]["bundle_id"] == "local.moonglade.app"
          and 'identifier "local.moonglade.app"' in v["app"]["designated"]
          and v["app"]["repo_path"] == str(clone.resolve()))
    d = v["defaults"]
    defaults_ok = (d["declared"] == {"world_size": "normal", "vanilla_mode": "on", "client": "patched"}
                   and d["world_size"] == d["declared"].get("world_size") and d["vanilla_mode"] == d["declared"].get(
                       "vanilla_mode") and d["client"] == d["declared"].get("client") and d["docs_table_matches_source"])
    v["install_ok"], v["defaults_ok"] = ok, defaults_ok
    return v


def _stop_clone(clone: Path) -> dict:
    """Whatever the scratch clone may still run: its stack (its own hs down) and its Wine prefix."""
    out = {}
    if (clone / "harness/hs").exists() and _clone_processes(clone):
        r = subprocess.run([str(clone / "harness/hs"), "down"], capture_output=True, text=True,
                           env=scratch_env(HS_HARNESS_RUN="1"), cwd=clone, timeout=300)
        out["down_rc"] = r.returncode
        ws = clone / "env/wowsilicon-r17/.wine-runtime/bin/wineserver"
        if ws.exists():
            subprocess.run([str(ws), "-k"], env=scratch_env(WINEPREFIX=str(clone / "env/wineprefix-mtld3d")),
                           capture_output=True, timeout=60)
    left = _clone_processes(clone)
    if left:
        from .. import procs
        for pid, c in procs.table():
            if str(clone.resolve()) in c:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        out["killed"] = left
    return out


def _kill_during(clone: Path, client: Path, env: dict, step: str, out: Path, min_bytes: int) -> dict:
    """Start ./bootstrap, SIGKILL its process group once `step` has started and written `min_bytes` into its output."""
    t0 = time.time()
    with open(out, "w") as fh:
        proc = subprocess.Popen([str(clone / "bootstrap"), str(client)], cwd=clone, stdin=subprocess.DEVNULL,
                                stdout=fh, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    killed_at, written = None, 0
    try:
        while proc.poll() is None and time.time() - t0 < 3600:
            ev = bootstrap_events(out.read_text(errors="replace"))
            if any(e.get("step") == step and e.get("state") == "start" for e in ev):
                written = sum(f.stat().st_size for f in (clone / "client").rglob("*") if f.is_file()) \
                    if (clone / "client").exists() else 0
                if written >= min_bytes:
                    os.killpg(proc.pid, signal.SIGKILL)
                    killed_at = round(time.time() - t0, 1)
                    break
            time.sleep(1)
    finally:
        if proc.poll() is None and killed_at is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    time.sleep(1)
    events = bootstrap_events(out.read_text(errors="replace"))
    return {"killed_after_s": killed_at, "client_bytes_when_killed": written,
            "last_event": events[-1] if events else None, "rc": proc.returncode,
            "processes_left_after_kill": _clone_processes(clone)}


@check("setup.fresh_clone", order=12, capability="easy-setup", stack="down", layer="gate")
def fresh_clone(ctx):
    """A fresh `git clone` of HEAD in harness/out/setup-scratch/clone, with the Homebrew deps installed and a cache of the
    pinned downloads: `./bootstrap <the dev Mac's client/> --yes` exits 0 with a built server, server data, the
    databases, the Wine runtime, the client (client.inspect passes there), the play client and a valid signed app;
    ./play there with a stand-in reaches "ready" and stops cleanly; a re-run says "already done" for every step,
    changes no output timestamps and takes < 2 min; the settings equal the documented defaults (docs table = source);
    a bootstrap SIGKILLed during the client install resumes there (earlier steps untouched) and ends the same way.
    The main checkout's env/, data/, client/ (read-only input), server/ and dist/ are never written."""
    from .. import downloads
    from .play_checks import is_clear, remaining
    ev: dict = {"clone": str(CLONE), "cache": str(CACHE)}
    ev["given_nothing_running"] = remaining()
    require(is_clear(ev["given_nothing_running"]), "GIVEN not met: the main stack (or Wine) is running", **ev)
    brew = subprocess.run([str(P.BREW / "bin/brew"), "bundle", "check", "--no-upgrade", f"--file={P.ROOT / 'Brewfile'}"],
                          capture_output=True, text=True, env={**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1"})
    ev["given_brew_bundle_check"] = {"rc": brew.returncode, "out": (brew.stdout + brew.stderr).strip()[-300:]}
    require(brew.returncode == 0, "GIVEN not met: the Brewfile's packages aren't all installed (this check never "
                                  "installs Homebrew packages)", **ev)
    free = shutil.disk_usage(P.ROOT).free
    ev["given_free_gb"] = round(free / 1e9, 1)
    require(free > 40e9, "GIVEN not met: the scratch install needs about 30 GB plus headroom (40 GB free)", **ev)
    # the cache of pinned files (each verified now, and again by bootstrap when it uses it)
    ev["cache_files"] = {}
    for name in ("client-data", "wine-runtime"):
        got = downloads.obtain(name, cache=CACHE)
        ev["cache_files"][name] = {"file": Path(got["path"]).name, "from": got["from"], "sha256": got["sha256"]}
    main_watch = {d: (P.ROOT / d).stat().st_mtime_ns if (P.ROOT / d).exists() else None
                  for d in ("env", "data", "server", "dist", "client")}
    main_client_fp = {"Wow.exe": (P.CLIENT / "Wow.exe").stat().st_mtime_ns, "manifest": P.CLIENT_MANIFEST.read_bytes()}
    _stop_clone(CLONE)
    pristine.remove_tree(CLONE)               # its client/ is write-protected by its own install
    head = subprocess.run(["git", "-C", str(P.ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    ev["head"] = head
    try:
        r = subprocess.run(["git", "clone", "-q", str(P.ROOT), str(CLONE)], capture_output=True, text=True)
        require(r.returncode == 0, f"git clone failed: {r.stderr[-300:]}", **ev)
        subprocess.run(["git", "-C", str(CLONE), "checkout", "-q", "--detach", head], check=True)
        ev["given_empty"] = {d: not (CLONE / d).exists() for d in ("env", "data", "build", "server", "client", "dist")}
        require(all(ev["given_empty"].values()), "GIVEN not met: the fresh clone isn't empty", **ev)
        env = scratch_env(HS_HARNESS_RUN="1", HS_DOWNLOAD_CACHE=str(CACHE))
        # ---- WHEN: ./bootstrap --yes
        out1 = SCRATCH_ROOT / "bootstrap-1.out"
        t0 = time.time()
        with open(out1, "w") as fh:
            p1 = subprocess.run([str(CLONE / "bootstrap"), str(P.CLIENT), "--yes"], cwd=CLONE, stdin=subprocess.DEVNULL,
                                stdout=fh, stderr=subprocess.STDOUT, env=env, timeout=4 * 3600)
        e1 = bootstrap_events(out1.read_text(errors="replace"))
        ev["fresh"] = {"rc": p1.returncode, "seconds": round(time.time() - t0, 1),
                       "steps": {e["step"]: {"state": e["state"], "s": e.get("seconds")} for e in e1
                                 if e.get("event") == "step" and e.get("state") in ("done", "already_done", "failed")},
                       "end": next((e for e in e1 if e.get("event") == "end"), None),
                       "output_tail": out1.read_text(errors="replace")[-2000:]}
        require(p1.returncode == 0 and (ev["fresh"]["end"] or {}).get("ok"), "the fresh bootstrap failed", **ev)
        ev["fresh"]["disk_gb"] = {d: round(sum(f.stat().st_size for f in (CLONE / d).rglob("*") if f.is_file() and
                                               not f.is_symlink()) / 1e9, 2)
                                  for d in ("vendor", "build", "server", "data", "env", "client", "dist")
                                  if (CLONE / d).exists()}
        ev["fresh"]["verify"] = v1 = _verify_install(CLONE, "1", ev)
        require(v1["install_ok"], "after the fresh bootstrap, an output is missing or invalid", **ev)
        require(v1["defaults_ok"], "the settings after bootstrap aren't the documented defaults (or the docs table "
                                   "doesn't match the source)", **ev)
        ev["fresh"]["play"] = pl = _play_in_clone(CLONE, "1")
        require(pl["exit_code"] == 0 and pl["ready_after_s"] and pl["standin_started"] and pl["all_stopped_line"]
                and not pl["processes_left"] and not pl["ports_open"],
                "./play in the scratch clone didn't reach ready and stop cleanly", **ev)
        # ---- WHEN: re-run
        snap = _outputs_snapshot(CLONE)
        out2 = SCRATCH_ROOT / "bootstrap-2.out"
        t0 = time.time()
        with open(out2, "w") as fh:
            p2 = subprocess.run([str(CLONE / "bootstrap"), str(P.CLIENT)], cwd=CLONE, stdin=subprocess.DEVNULL,
                                stdout=fh, stderr=subprocess.STDOUT, env=env, timeout=1800)
        dt = round(time.time() - t0, 1)
        e2 = bootstrap_events(out2.read_text(errors="replace"))
        states = {e["step"]: e["state"] for e in e2 if e.get("event") == "step" and e.get("step") != "preflight"
                  and e.get("state") != "start"}
        snap2 = _outputs_snapshot(CLONE)
        ev["rerun"] = {"rc": p2.returncode, "seconds": dt, "states": states,
                       "changed_outputs": sorted(k for k in snap if snap[k] != snap2[k]),
                       "missing_outputs": sorted(k for k, x in snap.items() if x is None)}
        require(p2.returncode == 0, "the re-run failed", **ev)
        require(states and all(s == "already_done" for s in states.values()) and len(states) == 9,
                "the re-run didn't report every step as already done", **ev)
        require(not ev["rerun"]["changed_outputs"] and not ev["rerun"]["missing_outputs"],
                "the re-run changed (or lacks) outputs", **ev)
        require(dt < 120, f"the re-run took {dt} s (limit 120 s)", **ev)
        # ---- WHEN: killed during the client install, then resumed
        for d in ("client", "env/client-play"):
            pristine.remove_tree(CLONE / d)   # the clone's client/ is write-protected
        (CLONE / "env/client-manifest.json").unlink(missing_ok=True)
        before_kill = {k: x for k, x in _outputs_snapshot(CLONE).items()
                       if k.startswith(("server/", "build", "data/", "env/wowsilicon", "vendor/"))}
        ev["interrupt"] = _kill_during(CLONE, P.CLIENT, env, "client", SCRATCH_ROOT / "bootstrap-3.out",
                                       min_bytes=3 * 10**9)
        k = ev["interrupt"]
        require(k["killed_after_s"] and (k["last_event"] or {}).get("step") == "client",
                "GIVEN not met: the bootstrap wasn't killed during the client install", **ev)
        ev["interrupt"]["stop_after_kill"] = _stop_clone(CLONE)
        out4 = SCRATCH_ROOT / "bootstrap-4.out"
        t0 = time.time()
        with open(out4, "w") as fh:
            p4 = subprocess.run([str(CLONE / "bootstrap"), str(P.CLIENT)], cwd=CLONE, stdin=subprocess.DEVNULL,
                                stdout=fh, stderr=subprocess.STDOUT, env=env, timeout=3600)
        e4 = bootstrap_events(out4.read_text(errors="replace"))
        st4 = {e["step"]: e["state"] for e in e4 if e.get("event") == "step" and e.get("step") != "preflight"
               and e.get("state") != "start"}
        after = {k_: x for k_, x in _outputs_snapshot(CLONE).items() if k_ in before_kill}
        ev["resume"] = {"rc": p4.returncode, "seconds": round(time.time() - t0, 1), "states": st4,
                        "earlier_outputs_changed": sorted(k_ for k_ in before_kill if before_kill[k_] != after[k_]),
                        "output_tail": out4.read_text(errors="replace")[-1200:]}
        require(p4.returncode == 0, "the resumed bootstrap failed", **ev)
        require(all(st4.get(s) == "already_done" for s in ("brew", "build", "data", "database", "wine"))
                and st4.get("client") == "done",
                "the resumed bootstrap didn't continue at the client install (or redid an earlier step)", **ev)
        require(not ev["resume"]["earlier_outputs_changed"], "the resumed bootstrap touched earlier steps' outputs", **ev)
        ev["resume"]["stale_part_files"] = sorted(str(x.relative_to(CLONE)) for x in (CLONE / "client").rglob("*.hs-part"))
        require(not ev["resume"]["stale_part_files"], "the killed install's unfinished files are still there", **ev)
        ev["resume"]["verify"] = v2 = _verify_install(CLONE, "2", ev)
        require(v2["install_ok"] and v2["defaults_ok"], "after the resumed bootstrap, an output is missing or invalid",
                **ev)
        ev["resume"]["play"] = pl2 = _play_in_clone(CLONE, "2")
        require(pl2["exit_code"] == 0 and pl2["ready_after_s"] and not pl2["processes_left"] and not pl2["ports_open"],
                "./play after the resumed bootstrap didn't reach ready and stop cleanly", **ev)
    finally:
        ev["cleanup"] = _stop_clone(CLONE)
        pristine.remove_tree(CLONE)
        ev["cleanup"]["clone_removed"] = not CLONE.exists()
        for f in SCRATCH_ROOT.glob("*.out"):
            ev.setdefault("logs_kept", []).append(str(f.relative_to(P.ROOT)))
    ev["main_untouched"] = {
        "client_wow_exe_mtime_same": (P.CLIENT / "Wow.exe").stat().st_mtime_ns == main_client_fp["Wow.exe"],
        "client_manifest_same": P.CLIENT_MANIFEST.read_bytes() == main_client_fp["manifest"],
        "top_dirs_mtime": {d: ((P.ROOT / d).stat().st_mtime_ns if (P.ROOT / d).exists() else None) == m
                           for d, m in main_watch.items()}}
    require(ev["main_untouched"]["client_wow_exe_mtime_same"] and ev["main_untouched"]["client_manifest_same"],
            "the main checkout's client/ or its manifest changed", **ev)
    require(ev["cleanup"]["clone_removed"], "the scratch clone wasn't removed", **ev)
    return ev
