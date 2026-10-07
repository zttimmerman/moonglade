"""play-client (easy-setup): the pristine client can't be written. OS lock, writer guards, per-run fingerprint, agent
hook (design.md "As built", pristine-client protection). Every check works on SCRATCH installs (APFS clones of
client/, or small made-up folders): nothing here writes, locks or unlocks the real client/."""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .. import clientbuild, clientinst, paths as P, pristine
from ..registry import check, require

CAP = "play-client"
SCRATCH = P.ENV / "check-pristine"          # scratch installs (APFS clones share storage with client/)
HOOK = P.ROOT / ".claude" / "hooks" / "guard-client.sh"
SETTINGS = P.ROOT / ".claude" / "settings.json"
PROBE_ENV = "HS_TEST_PRISTINE_MUTATE"      # report.client_pristine's self-test: the probe changes this file


def _fresh(name: str) -> Path:
    d = SCRATCH / name
    pristine.remove_tree(d)
    d.parent.mkdir(parents=True, exist_ok=True)
    return d


def _fresh_dir(name: str) -> Path:
    d = _fresh(name)
    d.mkdir()
    return d


def clone_client(dest: Path) -> Path:
    """A scratch install: an APFS clone of client/ (read-only use of client/), unlocked, then locked like an install."""
    require(P.CLIENT.is_dir(), "GIVEN not met: no client/ installed to clone", client=str(P.CLIENT))
    r = subprocess.run(["/bin/cp", "-cR", os.path.realpath(P.CLIENT), str(dest)], capture_output=True, text=True)
    require(r.returncode == 0, "couldn't clone client/ into a scratch install", stderr=r.stderr[-500:])
    pristine.unlock(dest)
    return dest


def _attempt(desc: str, argv: list[str]) -> dict:
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    return {"attempt": desc, "exit": r.returncode, "refused": r.returncode != 0, "stderr": r.stderr.strip()[-160:]}


_PY_ATTEMPT = r"""
import os, sys
op, a, b = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else None)
try:
    if op == "replace": os.replace(a, b)
    elif op == "chmod": os.chmod(a, 0o644)
    elif op == "write": open(a, "r+b").write(b"x")
    elif op == "rmtree":
        import shutil; shutil.rmtree(a)
    print("DONE")
except OSError as e:
    print(f"{type(e).__name__} errno={e.errno}"); sys.exit(1)
"""


def write_attempts(root: Path, via: str) -> list[dict]:
    """A plain process's write attempts into an installed client (as `via` names it: the folder or a symlink)."""
    r = Path(via)
    loc = clientbuild.locale(root)
    probe = root.parent / "probe-src.txt"
    probe.write_text("probe\n")
    sh = lambda c: ["/bin/sh", "-c", c]
    py = lambda *a: [sys.executable, "-c", _PY_ATTEMPT, *a]
    folder = [] if Path(via).is_symlink() else [        # renaming/deleting a symlink itself is no write into the client
        _attempt("delete the client (shutil.rmtree)", py("rmtree", str(r))),
        _attempt("rename the client folder", ["/bin/mv", str(r), str(r) + ".moved"]),
    ]
    return folder + [
        _attempt("overwrite Wow.exe (>)", sh(f"printf x > '{r}/Wow.exe'")),
        _attempt("append to Wow.exe (>>)", sh(f"printf x >> '{r}/Wow.exe'")),
        _attempt("write into Wow.exe (r+b)", py("write", f"{r}/Wow.exe")),
        _attempt("replace Wow.exe (os.replace)", py("replace", str(probe), f"{r}/Wow.exe")),
        _attempt("cp over Wow.exe", ["/bin/cp", str(probe), f"{r}/Wow.exe"]),
        _attempt("rename Wow.exe", ["/bin/mv", f"{r}/Wow.exe", f"{r}/Wow.old"]),
        _attempt("delete Wow.exe", ["/bin/rm", "-f", f"{r}/Wow.exe"]),
        _attempt("create a file next to Wow.exe", ["/usr/bin/touch", f"{r}/d3d9.dll"]),
        _attempt(f"create an archive in Data/{loc}", ["/usr/bin/touch", f"{r}/Data/{loc}/patch-{loc}-4.MPQ"]),
        _attempt("make a folder", ["/bin/mkdir", f"{r}/Interface/AddOns/Moonglade"]),
        _attempt("chmod u+w Wow.exe", py("chmod", f"{r}/Wow.exe")),
        _attempt("delete Data/ (rm -rf)", ["/bin/rm", "-rf", f"{r}/Data"]),
    ]


@check("client.pristine_locked", order=212, capability=CAP, stack="any", tier="quick")
def pristine_locked(ctx):
    """On a protected scratch install (an APFS clone of client/), every write attempt of a plain process fails,
    directly and through a symlinked path; every file still matches the install record and the fingerprint is
    unchanged; a play client built from it is writable, unflagged and patched. `client-install` (the install code)
    lifts and restores the protection on a reinstall, on an install that fails part-way and, after one is killed
    part-way, the next install restores it."""
    ev: dict = {"scratch": str(SCRATCH)}
    man = clientinst.load_manifest()
    require(man, "GIVEN not met: no install record (env/client-manifest.json)", **ev)
    try:
        # --- scenario 1: writes into the installed client are refused by the OS
        inst = clone_client(_fresh("install"))
        ev["lock"] = pristine.lock(inst)
        st0 = pristine.status(inst)
        ev["status_before"] = st0
        require(st0["locked"], "GIVEN not met: the scratch install isn't write-protected after lock()", **ev)
        fp0 = pristine.fingerprint(inst, manifest=man)
        link = SCRATCH / "link-to-install"
        link.unlink(missing_ok=True)
        link.symlink_to(inst)
        ev["direct"] = write_attempts(inst, str(inst))
        ev["via_symlink"] = write_attempts(inst, str(link))
        allowed = ([a["attempt"] for a in ev["direct"] if not a["refused"]]
                   + [a["attempt"] + " (via symlink)" for a in ev["via_symlink"] if not a["refused"]])
        ev["allowed"] = allowed
        fp1 = pristine.fingerprint(inst, manifest=man)
        ev["changes"] = pristine.diff(fp0, fp1)
        ev["manifest_mismatches"] = clientinst.mismatches(inst, man)
        require(not allowed, f"write attempts succeeded on a protected install: {allowed}", **ev)
        require(not ev["changes"] and not ev["manifest_mismatches"],
                "the protected install changed (fingerprint or install record)", **ev)
        # the game's own state folder stays writable (stock mode)
        cfg = inst / "WTF" / "hs-probe.txt"
        cfg.write_text("x")
        cfg.unlink()
        ev["state_folder_writable"] = True

        # a play client built from it: writable, unflagged, patched
        play = SCRATCH / "play"
        pristine.remove_tree(play)
        b = clientbuild.build(dest=play, base=inst)
        recs = {r["id"]: r for r in clientbuild.queue()}
        want_exe = next((r["output_sha256"] for r in recs.values() if r["kind"] == "binary" and r["file"] == "Wow.exe"), None)
        pst = pristine.status(play)
        flagged = [str(p.relative_to(play)) for p in play.rglob("*") if os.lstat(p).st_flags & stat.UF_IMMUTABLE][:5]
        ro = [str(p.relative_to(play)) for p in [play, *play.rglob("*")]
              if not p.is_symlink() and not os.lstat(p).st_mode & stat.S_IWUSR][:5]
        w = play / "Data" / clientbuild.locale(play) / "hs-write-probe"
        w.write_text("x")
        w.unlink()
        ev["play_client"] = {"built": b["built"], "outputs": b["stamp"]["outputs"], "clone_unlocked": b.get("clone_unlocked"),
                             "immutable_files": flagged, "read_only_paths": ro, "wow_exe_patched": want_exe,
                             "status_locked": pst["locked"]}
        require(b["built"] and (want_exe is None or b["stamp"]["outputs"].get("Wow.exe") == want_exe),
                "the play client built from the protected install isn't patched", **ev)
        require(not flagged and not ro, "the play client inherited the protection (flagged or read-only paths)", **ev)
        fp2 = pristine.fingerprint(inst, manifest=man)
        ev["changes_after_build"] = pristine.diff(fp0, fp2)
        require(not ev["changes_after_build"], "building the play client changed the protected install", **ev)

        # --- scenario 2: reinstall lifts and restores the protection (install code, into a scratch target)
        src = _fresh("src")
        (src / "Data" / "enUS").mkdir(parents=True)
        subprocess.run(["/bin/cp", "-c", str(inst / "Wow.exe"), str(src / "Wow.exe")], check=True)
        (src / "Data" / "enUS" / "realmlist.wtf").write_text("set realmlist us.logon.worldofwarcraft.com\r\n")
        (src / "Data" / "enUS" / "small.txt").write_text("data\n")
        tgt, mpath = SCRATCH / "target", SCRATCH / "target-manifest.json"
        pristine.remove_tree(tgt)
        mpath.unlink(missing_ok=True)
        r1 = clientinst.install(src, client=tgt, manifest_path=mpath, run_config=True)
        r2 = clientinst.install(src, client=tgt, manifest_path=mpath, run_config=True)     # reinstall (no-op path)
        ev["install"] = {"first": r1.get("write_protection"), "reinstall": r2.get("write_protection"),
                         "reinstall_action": r2.get("action")}
        require(pristine.status(tgt)["locked"] and r1["write_protection"]["locked"] and r2["write_protection"]["locked"],
                "the install (or reinstall) didn't leave the folder write-protected", **ev)
        # an install that fails part-way (a source file it can't read)
        bad = src / "Data" / "enUS" / "unreadable.bin"
        bad.write_bytes(b"\0" * 4096)
        os.chmod(bad, 0)
        failed = None
        try:
            clientinst.install(src, client=tgt, manifest_path=mpath)
        except Exception as e:  # noqa: BLE001  (the failure is the point)
            failed = f"{type(e).__name__}: {e}"[:200]
        finally:
            os.chmod(bad, 0o644)
            bad.unlink()
        ev["install_failed"] = {"error": failed, "status_after": pristine.status(tgt)}
        require(failed and pristine.status(tgt)["locked"], "after an install failed part-way the folder isn't "
                "write-protected (or the install didn't fail)", **ev)
        # an install killed part-way (SIGKILL while the protection is lifted), then the next install
        (src / "Data" / "enUS" / "added.txt").write_text("new member\n")
        code = ("import sys, time; sys.path.insert(0, %r)\n"
                "from hslib import clientinst\n"
                "orig = clientinst._stream\n"
                "def slow(*a, **k):\n    print('STREAMING', flush=True); time.sleep(60); return orig(*a, **k)\n"
                "clientinst._stream = slow\n"
                "clientinst.install(%r, client=%r, manifest_path=%r)\n") % (str(P.HARNESS), str(src), str(tgt), str(mpath))
        pr = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        line = pr.stdout.readline()
        mid = pristine.status(tgt)
        pr.send_signal(signal.SIGKILL)
        pr.wait(timeout=30)
        child_err = pr.stderr.read()[-800:]
        after_kill = pristine.status(tgt)
        r3 = clientinst.install(src, client=tgt, manifest_path=mpath)
        ev["install_killed"] = {"child_said": line.strip(), "child_stderr": child_err, "child_exit": pr.returncode,
                                "status_while_installing": mid,
                                "status_after_kill": after_kill, "next_install": r3.get("write_protection"),
                                "next_install_action": r3.get("action")}
        require(line.strip() == "STREAMING" and not mid["locked"],
                "GIVEN not met: the killed install never reached the write phase with the protection lifted", **ev)
        require(not after_kill["locked"], "unexpected: the protection was back right after the SIGKILL", **ev)
        require(r3["write_protection"]["locked"] and pristine.status(tgt)["locked"],
                "the next install didn't restore the protection after a killed one", **ev)
        return ev
    finally:
        for name in ("link-to-install",):
            try:
                (SCRATCH / name).unlink()
            except FileNotFoundError:
                pass
        pristine.remove_tree(SCRATCH)


@check("client.build_refuses_pristine", order=213, capability=CAP, stack="any", tier="quick")
def build_refuses_pristine(ctx):
    """A play-client build aimed at a scratch "client" through a symlinked folder, a case variant or a `..` path stops
    before writing, with a message naming the pristine client, and the scratch client is unchanged (identity, sizes,
    times, binary hashes). The 2026-10-07 pattern (a folder whose client/ is a symlink to the scratch client, building
    into a real folder next to it) builds a correct play client and leaves the scratch client unchanged. The scratch
    client is NOT write-protected here, so only the writer guards stand in the way."""
    ev: dict = {}
    man = clientinst.load_manifest()
    try:
        base = clone_client(_fresh("base"))           # unlocked: the guards alone are tested
        fp0 = pristine.fingerprint(base, manifest=man)
        other = _fresh_dir("other")
        link = other / "linked"
        link.symlink_to(base)
        variants = {
            "symlinked folder": link / "play",
            "case variant": base.parent / base.name.upper() / "play",
            "dot-dot path": other / ".." / base.name / "Data" / ".." / "play",
            "the client itself": base,
            "symlink to the client as destination": other / "dest-link",
        }
        (other / "dest-link").symlink_to(base)
        res = {}
        for name, dest in variants.items():
            try:
                clientbuild.build(dest=dest, base=base)
                res[name] = {"dest": str(dest), "refused": False}
            except clientbuild.ClientBuildError as e:
                res[name] = {"dest": str(dest), "refused": True, "message": str(e)[:300],
                             "names_pristine": "pristine client" in str(e)
                             and (os.path.realpath(base) in str(e) or str(base) in str(e))}
        ev["variants"] = res
        fp1 = pristine.fingerprint(base, manifest=man)
        ev["changes"] = pristine.diff(fp0, fp1)
        leftovers = sorted(str(p.relative_to(base)) for p in base.iterdir()
                           if p.name.startswith("play") or ".build-" in p.name or ".old-" in p.name)
        ev["leftovers_in_client"] = leftovers
        not_refused = [k for k, v in res.items() if not v["refused"] or not v.get("names_pristine")]
        require(not not_refused, f"builds into the pristine client weren't refused (or the message doesn't name it): "
                f"{not_refused}", **ev)
        require(not ev["changes"] and not leftovers, "the scratch client changed", **ev)

        # the incident's shape: a makeshift repo whose client/ links to the scratch client; its env/ is a real folder
        fake = _fresh_dir("fake-repo")
        (fake / "client").symlink_to(base)
        (fake / "env").mkdir()
        b = clientbuild.build(dest=fake / "env" / "client-play", base=fake / "client")
        play = fake / "env" / "client-play"
        ev["incident_shape"] = {"built": b["built"], "clone": b.get("clone"), "play_is_symlink": os.path.islink(play),
                                "play_wow_exe_ino": os.lstat(play / "Wow.exe").st_ino,
                                "client_wow_exe_ino": os.lstat(base / "Wow.exe").st_ino}
        fp2 = pristine.fingerprint(base, manifest=man)
        ev["changes_after_incident_shape"] = pristine.diff(fp0, fp2)
        require(b["built"] and not os.path.islink(play)
                and ev["incident_shape"]["play_wow_exe_ino"] != ev["incident_shape"]["client_wow_exe_ino"],
                "the build through a symlinked client/ didn't make a separate play client", **ev)
        require(not ev["changes_after_incident_shape"], "building through a symlinked client/ changed the client", **ev)
        return ev
    finally:
        pristine.remove_tree(SCRATCH)


# ---------------------------------------------------------------- report.client_pristine (post-run) and its probe

def start_record() -> dict:
    """Taken by the runner at the start of every run."""
    try:
        root = pristine.run_root()
        return {"root": str(root), "test_switch": bool(os.environ.get(pristine.TEST_ENV)),
                "fingerprint": pristine.fingerprint(root)}
    except Exception as e:  # noqa: BLE001  (the post-run record fails on it)
        return {"error": f"{type(e).__name__}: {e}"}


def _summary(fp: dict) -> dict:
    return {k: fp.get(k) for k in ("root", "exists", "dir_ino", "manifest_mismatches", "lock")} | \
           {"files": len(fp.get("files") or {})}


def compare(start: dict) -> dict:
    """{ok, evidence, message} for this run: the pristine client's end fingerprint against the start one."""
    t0 = time.time()
    if not start or "error" in start:
        return {"ok": False, "evidence": {"start": start}, "message": "no start fingerprint of the pristine client"}
    root = Path(start["root"])
    try:
        end = pristine.fingerprint(root)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "evidence": {"start": _summary(start["fingerprint"])},
                "message": f"couldn't fingerprint the pristine client at the end: {e}"}
    changes = pristine.diff(start["fingerprint"], end)
    ev = {"root": str(root), "test_switch": start.get("test_switch"), "start": _summary(start["fingerprint"]),
          "end": _summary(end), "changes": changes[:50], "seconds": round(time.time() - t0, 2)}
    msg = None
    if changes:
        msg = "the pristine client changed during this run: " + "; ".join(
            f"{c['path']} ({c['change']})" for c in changes[:5])
    return {"ok": not changes, "evidence": ev, "message": msg}


SELFTEST_DIR = P.OUT / "pristine-selftest"


def self_test() -> dict:
    """The scenario: nested runs whose pristine root is a scratch install ($HS_TEST_PRISTINE_CLIENT). An untouched run
    passes report.client_pristine; a run whose probe check changes one file fails it, naming the file."""
    ev: dict = {}
    root = SELFTEST_DIR / "install"
    pristine.remove_tree(SELFTEST_DIR)
    (root / "Data" / "enUS").mkdir(parents=True)
    (root / "Wow.exe").write_bytes(b"MZ scratch stand-in\n")
    (root / "Data" / "enUS" / "locale-enUS.MPQ").write_bytes(b"MPQ scratch\n")
    pristine.lock(root)
    target = "Data/enUS/locale-enUS.MPQ"
    runs = {}
    for name, extra in (("untouched", {}), ("changed", {PROBE_ENV: target})):
        out = SELFTEST_DIR / name
        env = {k: v for k, v in os.environ.items() if k != PROBE_ENV} | {pristine.TEST_ENV: str(root), **extra}
        cmd = [str(P.HARNESS / "hs"), "run", "--tier", "quick", "--only", "report.client_pristine_probe",
               "--results-dir", str(out), "--leave-up"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
        try:
            doc = json.loads((out / "results.json").read_text())
        except (OSError, ValueError) as e:
            runs[name] = {"exit": r.returncode, "error": f"no results: {e}", "stderr": r.stderr[-800:]}
            continue
        recs = {c["id"]: c for c in doc["checks"]}
        cp = recs.get("report.client_pristine") or {}
        runs[name] = {"exit": r.returncode, "status": cp.get("status"), "message": cp.get("message"),
                      "root": (cp.get("evidence") or {}).get("root"),
                      "probe": (recs.get("report.client_pristine_probe") or {}).get("status")}
    ev["runs"] = runs
    ev["target"] = target
    u, c = runs.get("untouched", {}), runs.get("changed", {})
    ok = (u.get("status") == "pass" and u.get("root") == str(root) and c.get("status") == "fail"
          and target in (c.get("message") or "") and c.get("probe") == "pass" and c.get("root") == str(root))
    pristine.remove_tree(SELFTEST_DIR)
    return {"ok": ok, "evidence": ev}


def post_run_record(start: dict, run_self_test: bool) -> dict:
    t0 = time.time()
    res = compare(start)
    ev = res["evidence"]
    probs = [res["message"]] if not res["ok"] else []
    if run_self_test:
        st = self_test()
        ev["self_test"] = st["evidence"]
        if not st["ok"]:
            probs.append("self-test: a changed scratch install wasn't caught (or an untouched one failed); see self_test")
    rec = {"id": "report.client_pristine", "capability": "play-client", "status": "fail" if probs else "pass",
           "evidence": ev, "duration_s": round(time.time() - t0, 2)}
    if probs:
        rec["message"] = "; ".join(probs)[:400]
    return rec


if os.environ.get(PROBE_ENV) and os.environ.get(pristine.TEST_ENV) and os.environ.get("HS_HARNESS_RUN") == "1":
    @check("report.client_pristine_probe", order=1, capability="harness-reporting", stack="any", tier="quick")
    def client_pristine_probe(ctx):
        """Test-only (registered only under report.client_pristine's self-test switches): changes one file of the
        scratch stand-in during the run, as a misbehaving check would."""
        root = pristine.test_root()
        target = root / os.environ[PROBE_ENV]
        require(not pristine.inside(target, P.CLIENT), "refusing: the probe target is inside client/", target=str(target))
        pristine.unlock(target.parent)
        with open(target, "ab") as f:
            f.write(b"changed by the probe\n")
        return {"changed": str(target)}
elif os.environ.get(pristine.TEST_ENV) and os.environ.get("HS_HARNESS_RUN") == "1":
    @check("report.client_pristine_probe", order=1, capability="harness-reporting", stack="any", tier="quick")
    def client_pristine_probe(ctx):
        """Test-only (registered only under report.client_pristine's self-test switch): changes nothing."""
        return {"changed": None}


# ---------------------------------------------------------------- the agent hook

ALLOWED = [
    "harness/hs run --only client.inspect",
    "shasum -a 256 client/Wow.exe",
    "ls -la client/Data/enUS",
    "cat client/WTF/Config.wtf",
    "git status && git diff --stat",
    "cp client/Wow.exe env/re/bin/Wow.exe",
    "ln -s /opt/homebrew/bin/python3 ~/bin/python3",
    "mkdir -p env/tmp/b && cp x env/tmp/b/",
    "python3 -c \"print(open('client/Wow.exe','rb').read(2))\"",
    "harness/hs client-install ~/Downloads/client.zip",
    "./play",
    "rm -rf env/check-play-seamless",
    "sed -i '' 's/a/b/' patches/client/0001-x.json",
]
BLOCKED = [
    "for d in baseline build client data dist env server vendor; do ln -s $R/$d $d; done",   # the actual 08:21 loop
    "R=/x/repo; ln -s $R/env env",
    "ln -sf ../project-moonglade/data data",
    "cp x client/Wow.exe",
    "mv client/Wow.exe client/Wow.bak",
    "sed -i '' 's/a/b/' client/WTF/Config.wtf",
    "printf x > client/Data/enUS/realmlist.wtf",
    "rm -rf client/Data",
    "chflags -R nouchg client",
    "cd client && touch new.dll",
    "python3 -c \"open('client/Wow.exe','wb').write(b'x')\"",
    "tee client/WTF/Config.wtf < x",
]


def run_hook(payload: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    r = subprocess.run([str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, timeout=20, env=env)
    return {"exit": r.returncode, "stderr": r.stderr.strip()[:240]}


@check("repo.agent_guard_hook", order=8, capability=CAP, stack="any", tier="quick")
def agent_guard_hook(ctx):
    """The committed PreToolUse hook (.claude/settings.json → .claude/hooks/guard-client.sh, for Bash, Write, Edit and
    NotebookEdit) blocks each harmful sample (the 08:21 symlink loop, cp/mv/sed -i/redirect/rm/chflags/cd/inline
    Python into client/, Edit/Write/NotebookEdit on a client path) with exit 2 and a reason, and lets each allowed
    one through (harness/hs, reads, git, scratch work, client-install)."""
    ev: dict = {"hook": str(HOOK.relative_to(P.ROOT))}
    require(HOOK.is_file() and os.access(HOOK, os.X_OK), "the hook script is missing or not executable", **ev)
    cfg = json.loads(SETTINGS.read_text())
    pre = (cfg.get("hooks") or {}).get("PreToolUse") or []
    wired = [h for m in pre for h in m.get("hooks", []) if "guard-client.sh" in h.get("command", "")
             and all(t in (m.get("matcher") or "").split("|") for t in ("Bash", "Write", "Edit", "NotebookEdit"))]
    ev["settings_pretooluse"] = pre
    require(wired, ".claude/settings.json doesn't wire guard-client.sh as a PreToolUse hook for Bash, Write, Edit "
            "and NotebookEdit", **ev)
    cwd = str(P.ROOT)
    table = []
    for cmd in BLOCKED:
        table.append({"tool": "Bash", "input": cmd, "want": "block",
                      **run_hook({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd})})
    # the 08:21 loop as it ran: in a scratch folder outside the repo
    table.append({"tool": "Bash", "input": BLOCKED[0] + "  (cwd: a scratch folder)", "want": "block",
                  **run_hook({"tool_name": "Bash", "tool_input": {"command": BLOCKED[0]},
                              "cwd": tempfile.gettempdir()})})
    for tool, key, path in (("Edit", "file_path", P.CLIENT / "WTF" / "Config.wtf"),
                            ("Write", "file_path", P.CLIENT / "Interface" / "AddOns" / "X" / "x.lua"),
                            ("NotebookEdit", "notebook_path", P.CLIENT / "x.ipynb"),
                            ("Edit", "file_path", P.ROOT / "client" / ".." / "client" / "Wow.exe"),
                            ("Write", "file_path", Path("client/new.txt"))):
        table.append({"tool": tool, "input": str(path), "want": "block",
                      **run_hook({"tool_name": tool, "tool_input": {key: str(path)}, "cwd": cwd})})
    for cmd in ALLOWED:
        table.append({"tool": "Bash", "input": cmd, "want": "allow",
                      **run_hook({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd})})
    for tool, path in (("Edit", P.ROOT / "harness" / "hslib" / "play.py"), ("Write", P.ENV / "tmp" / "x.txt"),
                       ("Edit", P.ROOT / "patches" / "client" / "0001-x.json")):
        table.append({"tool": tool, "input": str(path), "want": "allow",
                      **run_hook({"tool_name": tool, "tool_input": {"file_path": str(path)}, "cwd": cwd})})
    for row in table:
        row["ok"] = (row["exit"] == 2 and "Blocked by" in row["stderr"]) if row["want"] == "block" else row["exit"] == 0
    ev["table"] = table
    wrong = [f"{r['tool']}: {r['input']} (want {r['want']}, exit {r['exit']})" for r in table if not r["ok"]]
    require(not wrong, f"{len(wrong)} sample(s) handled wrongly: {wrong[:4]}", **ev)
    return ev
