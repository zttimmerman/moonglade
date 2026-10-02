"""Core patch checks (openspec/changes/core-server-mail-guard, ADR-0006): the server-mail login race.

ServerMailReward::OnPlayerLogin (core src/server/scripts/World/server_mail.cpp) queues async SELECTs whose callbacks
called SendServerMail(session->GetPlayer(), ...) with no null check; a logout before they ran crashed worldserver.
patches/core/0001-server-mail-guard-async-player.patch makes the callback return unless the session's current
player exists and has the GUID the lookup was queued for."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .. import baseline as B, build, fixtures as F, paths as P, soap, stack
from ..registry import check, require

HS = str(P.HARNESS / "hs")
PATCH_NAME = "0001-server-mail-guard-async-player.patch"
SERVER_MAIL_CPP = B.SERVER_MAIL_CPP
RACE_ATTEMPTS = 20          # spec: "20 times in a row" / "within the 20 attempts"
DIAG = Path.home() / "Library/Logs/DiagnosticReports"
CRASH_REPORT_WAIT_S = 120   # ReportCrash writes the .ips some seconds after the process dies; generous bound
# Frames that identify the server-mail defect in a crash report (spec: "naming ServerMailMgr::SendServerMail or
# the server_mail.cpp callback"); ServerMailCondition::CheckCondition is the frame that dereferences the player.
DEFECT_FRAMES = ("ServerMailMgr::SendServerMail", "ServerMailReward::OnPlayerLogin", "ServerMailCondition::")


# ----------------------------------------------------------------------------- patch content

GUARD_RE = re.compile(r"Player\*\s+player\s*=\s*session->GetPlayer\(\);\s*"
                      r"if\s*\(\s*!player\s*\|\|\s*player->GetGUID\(\)\s*!=\s*guid\s*\)\s*return;")


def guard_problems(text: str) -> list[str]:
    """What's missing for the GUID guard in a server_mail.cpp text (empty = guarded)."""
    probs = []
    if not re.search(r"ObjectGuid\s+guid\s*=\s*player->GetGUID\(\);", text):
        probs.append("character GUID not captured before queuing the lookups (ObjectGuid guid = player->GetGUID())")
    if not re.search(r"auto\s+callback\s*=\s*\[[^\]]*\bguid\b[^\]]*\]", text):
        probs.append("callback lambda does not capture guid")
    g = GUARD_RE.search(text)
    send = text.find("sServerMailMgr->SendServerMail(")
    if not g:
        probs.append("no `if (!player || player->GetGUID() != guid) return;` guard in the callback")
    elif send < 0 or g.start() > send:
        probs.append("guard is not before the SendServerMail call")
    if not re.search(r"SendServerMail\(\s*player\s*,", text):
        probs.append("SendServerMail is not called with the guarded `player`")
    if re.search(r"SendServerMail\(\s*session->GetPlayer\(\)", text):
        probs.append("SendServerMail still called with session->GetPlayer()")
    return probs


def apply_to_pin(patch: Path, sha: str) -> dict:
    """Apply `patch` to a pristine core_sha tree held in a throwaway git index (GIT_INDEX_FILE in a temp dir): the
    real vendor working tree and index are never touched. Returns git's output and the patched server_mail.cpp."""
    with tempfile.TemporaryDirectory(prefix="hs-core-patch-") as td:
        env = os.environ | {"GIT_INDEX_FILE": str(Path(td) / "index")}

        def g(*a):
            return subprocess.run(["git", "-C", str(P.CORE_SRC), *a], capture_output=True, text=True, env=env)
        rt = g("read-tree", sha)
        chk = g("apply", "--cached", "--check", "--whitespace=nowarn", str(patch))
        ap = g("apply", "--cached", "--whitespace=nowarn", str(patch)) if chk.returncode == 0 else None
        files = g("diff", "--cached", "--name-only", sha).stdout.split() if ap and ap.returncode == 0 else []
        text = g("show", f":{SERVER_MAIL_CPP}").stdout if ap and ap.returncode == 0 else ""
    return {"read_tree_exit": rt.returncode, "apply_check_exit": chk.returncode, "apply_check_stderr": chk.stderr[-800:],
            "apply_exit": ap.returncode if ap else None, "files_changed": files, "patched_text": text}


def patch_applied_evidence(patch: Path, vendor_file: Path, install_stamp: Path) -> dict:
    lock = P.fork_lock()
    a = apply_to_pin(patch, lock["core_sha"])
    patched = a.pop("patched_text")
    ev = {"patch": str(patch), "core_sha": lock["core_sha"], "pristine_apply": a,
          "pristine_patched_guard_problems": guard_problems(patched) if patched else ["patch did not apply"]}
    vendor_text = vendor_file.read_text() if vendor_file.exists() else ""
    ev["vendor_file"] = str(vendor_file)
    ev["vendor_guard_problems"] = guard_problems(vendor_text)
    # the installed binaries were built from this patch: the stamp a passing from-empty build wrote next to them
    # records the patch queue's digest
    try:
        stamp = json.loads(install_stamp.read_text())
    except (OSError, ValueError):
        stamp = {}
    stamped = stamp.get("manifest", {}).get("patches_core", {})
    ev["installed_stamp"] = {"path": str(install_stamp), "patches_core": stamped,
                             "input_hash": stamp.get("input_hash")}
    ev["patch_sha256"] = build._sha256_file(patch) if patch.exists() else None
    # build-input hash: the real inputs vs scratch copies of patches/core (unchanged / one patch edited)
    real = build.input_manifest()
    h_real = build.input_hash(real)
    with tempfile.TemporaryDirectory(prefix="hs-core-hash-") as td:
        same, changed = Path(td) / "same", Path(td) / "changed"
        shutil.copytree(P.PATCHES, same)
        shutil.copytree(P.PATCHES, changed)
        cp = changed / patch.name
        cp.write_text(cp.read_text() + "# hs core.patch_applied: content change\n")

        def h(d):
            return build.input_hash(build.input_manifest(patches_dir=d, cmake=real["cmake_args"],
                                                         compiler=real["compiler"]))
        ev["input_hash"] = {"real": h_real, "copy_unchanged": h(same), "copy_patch_edited": h(changed),
                            "current_matches_installed_stamp": stamp.get("input_hash") == h_real}
    return ev


@check("core.patch_applied", order=22, capability="server-build", stack="any", tier="quick")
def patch_applied(ctx):
    """0001-server-mail-guard-async-player.patch applies cleanly to core_sha (pristine scratch index), the patched
    and the installed source contain the GUID guard, the installed build is stamped with this patch, and the
    build-input hash changes when the patch file changes."""
    patch = P.PATCHES / PATCH_NAME
    require(patch.exists(), f"{patch} missing", patches=sorted(p.name for p in P.PATCHES.glob("*")))
    ev = patch_applied_evidence(patch, P.CORE_SRC / SERVER_MAIL_CPP, build.install_stamp_path())
    a = ev["pristine_apply"]
    require(a["read_tree_exit"] == 0, "could not read core_sha into a scratch index", **ev)
    require(a["apply_check_exit"] == 0 and a["apply_exit"] == 0, "patch does not apply cleanly to core_sha", **ev)
    require(a["files_changed"] == [SERVER_MAIL_CPP], "patch changes files other than server_mail.cpp", **ev)
    require(not ev["pristine_patched_guard_problems"], "patched core_sha source lacks the GUID guard", **ev)
    require(not ev["vendor_guard_problems"], "the vendor source tree the build used lacks the GUID guard", **ev)
    require(ev["installed_stamp"]["patches_core"].get(PATCH_NAME) == ev["patch_sha256"],
            "installed binaries are not stamped as built with this patch (run `hs build --clean`)", **ev)
    hs = ev["input_hash"]
    require(hs["copy_unchanged"] == hs["real"], "input hash of an unchanged patches/ copy differs (hash unstable)", **ev)
    require(hs["copy_patch_edited"] != hs["real"], "build-input hash did not change when the patch file changed", **ev)
    return ev


# ----------------------------------------------------------------------------- the race loop

def crash_reports() -> dict[str, float]:
    """worldserver crash reports in ~/Library/Logs/DiagnosticReports: {file name: mtime}."""
    if not DIAG.exists():
        return {}
    return {p.name: p.stat().st_mtime for p in DIAG.glob("worldserver*.ips")}


def parse_report(path: Path) -> dict:
    """pid, exception and the faulting thread's top frames of a macOS .ips crash report (JSON header + body)."""
    hdr, body = path.read_text().split("\n", 1)
    d = json.loads(body)
    imgs = d.get("usedImages", [])
    th = d["threads"][d.get("faultingThread", 0)]
    frames = []
    for f in th.get("frames", [])[:12]:
        img = imgs[f["imageIndex"]].get("name", "?") if "imageIndex" in f and f["imageIndex"] < len(imgs) else "?"
        frames.append(f"{img} {f.get('symbol', '?')}"[:300])
    return {"file": path.name, "app": json.loads(hdr).get("app_name"), "pid": d.get("pid"),
            "exception": d.get("exception"), "top_frames": frames}


def new_reports_for(pid: int | None, before: dict) -> list[dict]:
    out = []
    for name, mt in crash_reports().items():
        if before.get(name) == mt:
            continue
        try:
            r = parse_report(DIAG / name)
        except (OSError, ValueError, KeyError, IndexError) as e:
            r = {"file": name, "parse_error": f"{type(e).__name__}: {e}", "pid": None}
        if pid is None or r.get("pid") == pid:
            out.append(r)
    return out


def _probe_json(stdout: str) -> dict:
    try:
        return json.JSONDecoder().raw_decode(stdout[stdout.index("{"):])[0]
    except ValueError:
        return {}


def race_loop(server_pid: int, attempts: int = RACE_ATTEMPTS, stop_on_death=True) -> list[dict]:
    """`hs probe --login <test char> --no-settle` `attempts` times: login, then an immediate logout request (no
    settle_login barrier). Stops early when the worldserver dies (stop_on_death)."""
    rows = []
    for i in range(attempts):
        t0 = time.time()
        r = subprocess.run([HS, "probe", "--login", P.TEST_CHAR, "--no-settle"], capture_output=True, text=True,
                           timeout=180)
        out = _probe_json(r.stdout)
        lo = out.get("logout") or {}
        rows.append({"i": i + 1, "exit": r.returncode, "s": round(time.time() - t0, 2),
                     "login_verify_world": bool(out.get("login_verify_world")),
                     "logout_result": lo.get("logout_result"), "instant": lo.get("instant"),
                     "logout_complete": "complete_after_s" in lo, "settle": lo.get("settle", "n/a"),
                     "error": out.get("error") or (r.stderr[-300:] if r.returncode else None),
                     "server_alive_after": stack.alive(server_pid)})
        if stop_on_death and not rows[-1]["server_alive_after"]:
            break
    return rows


def _summ_rows(rows):
    return {"attempts": len(rows), "ok": sum(1 for r in rows if r["exit"] == 0 and r["logout_complete"]),
            "per_attempt_s": [r["s"] for r in rows]}


@check("core.login_logout_race", order=125, capability="server-build", stack="up", tier="full")
def login_logout_race(ctx):
    """20 probe logins of the test character, each logging out immediately (no settle_login): worldserver keeps
    the same pid, SOAP answers, no new worldserver crash report, every logout completed."""
    ev = {"fixture": F.ensure()}
    guid = F.char_guid()
    F.poll(lambda: F.char_state(guid), lambda s: s.get("online") == 0, timeout=30)
    pids = stack.stack_processes()["worldserver"]
    require(len(pids) == 1, "expected exactly one worldserver before the loop", worldservers=pids, **ev)
    pid = pids[0]
    reports_before = crash_reports()
    t0 = time.time()
    rows = race_loop(pid, stop_on_death=False)
    ev["loop_s"] = round(time.time() - t0, 1)
    pids_after = stack.stack_processes()["worldserver"]
    try:
        si = soap.gm("server info", timeout=15)
        ev["soap_after"] = si.evidence()
        soap_ok = si.ok
    except OSError as e:
        ev["soap_after"] = f"{type(e).__name__}: {e}"
        soap_ok = False
    time.sleep(3)    # ReportCrash latency: a crash report for a death during the loop would appear by now
    new = new_reports_for(None, reports_before)
    st, offline, _ = F.poll(lambda: F.char_state(guid), lambda s: s.get("online") == 0, timeout=15)
    ev |= {"worldserver_pid_before": pid, "worldserver_pids_after": pids_after, "alive_after": stack.alive(pid),
           "new_worldserver_crash_reports": new, "char_offline_after": offline, "summary": _summ_rows(rows),
           "attempts": rows}
    require(len(rows) == RACE_ATTEMPTS, "loop did not run all attempts", **ev)
    require(ev["alive_after"] and pids_after == [pid], "worldserver died or was replaced during the loop", **ev)
    require(all(r["settle"] is None for r in rows if r["logout_complete"]), "probe did not skip settle_login", **ev)
    require(soap_ok, "SOAP does not answer after the loop", **ev)
    require(not new, "a new worldserver crash report appeared", **ev)
    bad = [r for r in rows if not (r["exit"] == 0 and r["login_verify_world"] and r["logout_result"] == 0
                                   and r["instant"] is True and r["logout_complete"])]
    require(not bad, f"{len(bad)} of {len(rows)} login/logout attempts did not complete", bad=bad, **ev)
    require(offline, "test character not saved offline after the loop", **ev)
    return ev


def _server_digest() -> str:
    return hashlib.sha256(json.dumps(build.tree_digest(P.SERVER), sort_keys=True).encode()).hexdigest()


@check("core.login_logout_race_baseline", order=126, capability="server-build", stack="up", tier="full")
def login_logout_race_baseline(ctx):
    """The pinned core built WITHOUT the patch (baseline/: own worktree, build dir and install) crashes within
    20 immediate login/logout attempts, with a crash report whose top frames name the server-mail callback path;
    the real server/ install is untouched and the normal stack is restored afterwards."""
    ev = {"server_digest_before": _server_digest(), "fixture": F.ensure()}
    guid = F.char_guid()
    F.poll(lambda: F.char_state(guid), lambda s: s.get("online") == 0, timeout=30)
    t0 = time.time()
    try:
        ev["baseline_src"] = B.sync_src()
        ev["baseline_build"] = B.build_install()
    except B.BaselineError as e:
        require(False, f"baseline build failed: {e}", **ev, **e.evidence)
    ev["real_worldserver_sha256"] = build._sha256_file(P.SERVER_BIN / "worldserver")
    main_pids = stack.stack_processes()["worldserver"]
    require(len(main_pids) == 1, "expected exactly one real worldserver", worldservers=main_pids, **ev)
    ev["real_worldserver_stop"] = stack.stop_worldserver(main_pids[0])
    proc = None
    try:
        proc, ev["baseline_start"] = B.start_worldserver()
        reports_before = crash_reports()
        rows = race_loop(proc.pid)
        crashed = not stack.alive(proc.pid)
        ev |= {"crashed": crashed, "crashed_on_attempt": rows[-1]["i"] if crashed else None,
               "summary": _summ_rows(rows), "attempts": rows}
        reports = []
        if crashed:
            ev["baseline_returncode"] = proc.wait(timeout=30)
            deadline = time.time() + CRASH_REPORT_WAIT_S
            while not reports and time.time() < deadline:
                time.sleep(2)
                reports = new_reports_for(proc.pid, reports_before)
            ev["crash_report_wait_s"] = round(CRASH_REPORT_WAIT_S - max(0, deadline - time.time()), 1)
        ev["crash_reports"] = reports
    finally:
        if proc is not None:
            ev["baseline_stop"] = B.stop_worldserver(proc)
        try:
            ev["restore"] = stack.up()
        except stack.StackError as e:
            ev["restore"] = {"error": str(e), "log": e.log_excerpt[-2000:]}
    ev["wall_s"] = round(time.time() - t0, 1)
    ev["server_digest_after"] = _server_digest()
    ev["real_worldserver_pids_after"] = stack.stack_processes()["worldserver"]
    ev["baseline_worldserver_pids_after"] = B.worldserver_pids()
    try:
        ev["soap_after_restore"] = soap.gm("server info", timeout=15).evidence()
    except OSError as e:
        ev["soap_after_restore"] = f"{type(e).__name__}: {e}"
    src = ev["baseline_src"]
    require(src["head"] == P.fork_lock()["core_sha"] and not src["status_porcelain"]
            and src["server_mail_cpp_equals_core_sha"] and not src["server_mail_cpp_has_guid_guard"],
            "baseline source is not the pristine pinned core (patch present or tree dirty)", **ev)
    require(ev["baseline_start"]["cmdline_is_baseline"], "the started worldserver is not the baseline binary", **ev)
    require(ev["crashed"], f"unpatched baseline worldserver survived {len(ev['attempts'])} attempts", **ev)
    require(ev["crash_reports"], "no crash report for the baseline worldserver pid", **ev)
    frames = " | ".join(f for r in ev["crash_reports"] for f in r.get("top_frames", []))
    named = [n for n in DEFECT_FRAMES if n in frames]
    ev["defect_frames_named"] = named
    require("ServerMailMgr::SendServerMail" in frames or "ServerMailReward::OnPlayerLogin" in frames,
            "crash report top frames don't name ServerMailMgr::SendServerMail or the server_mail.cpp callback", **ev)
    require(ev["baseline_build"]["worldserver_sha256"] != ev["real_worldserver_sha256"],
            "baseline worldserver binary is identical to the real (patched) one", **ev)
    require(ev["server_digest_after"] == ev["server_digest_before"], "the real server/ install changed", **ev)
    require(not ev["baseline_worldserver_pids_after"], "a baseline worldserver is still running", **ev)
    require(len(ev["real_worldserver_pids_after"]) == 1 and isinstance(ev["soap_after_restore"], dict)
            and ev["soap_after_restore"].get("ok"), "normal stack not restored (worldserver/SOAP)", **ev)
    return ev
