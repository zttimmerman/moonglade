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
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from .. import baseline as B, build, crashtrace as CT, fixtures as F, paths as P, soap, stack
from ..registry import check, require

HS = str(P.HARNESS / "hs")
PATCH_NAME = "0001-server-mail-guard-async-player.patch"
SERVER_MAIL_CPP = B.SERVER_MAIL_CPP
RACE_ATTEMPTS = 20          # spec: "20 times in a row" / "within the 20 attempts"
DIAG = Path.home() / "Library/Logs/DiagnosticReports"
IPS_WAIT_S = 10             # supplementary only: how long to look for a fresh .ips for the baseline pid
# Frames that identify the server-mail defect in a crash backtrace (spec: "naming ServerMailMgr::SendServerMail or
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
            "exception": d.get("exception"), "top_frames": frames,
            "binary_uuid": (imgs[0].get("uuid") or "").lower() if imgs else None}


def binary_uuid(path: Path) -> str | None:
    """LC_UUID of a Mach-O binary (`dwarfdump --uuid`), lower-case, as .ips usedImages report it."""
    r = subprocess.run(["dwarfdump", "--uuid", str(path)], capture_output=True, text=True)
    m = re.search(r"UUID: ([0-9A-Fa-f-]{36})", r.stdout)
    return m.group(1).lower() if m else None


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


CRASHTRACE_DIR = B.TMP / "crashtrace"


@check("core.login_logout_race_baseline", order=126, capability="server-build", stack="up", tier="full")
def login_logout_race_baseline(ctx):
    """The pinned core built WITHOUT the patch (baseline/: own worktree, build dir and install) crashes within
    20 immediate login/logout attempts: it dies of SIGSEGV, and the backtrace that THIS crash recorded (crashtrace
    shim, a real bad-access fault in the baseline binary) names the server-mail callback path; the real server/
    install is untouched and the normal stack is restored afterwards."""
    ev = {"server_digest_before": _server_digest(), "fixture": F.ensure()}
    guid = F.char_guid()
    F.poll(lambda: F.char_state(guid), lambda s: s.get("online") == 0, timeout=30)
    t0 = time.time()
    try:
        ev["baseline_src"] = B.sync_src()
        ev["baseline_build"] = B.build_install()
        # harness-reporting timing guard: this rebuild of the baseline server is build time, reported apart and left
        # out of the per-check comparison (runner.check_build_s reads `build_s`)
        ev["build_s"] = ev["baseline_build"]["wall_s"]
        dylib = CT.build(B.TMP / "crashtrace.dylib")
    except (B.BaselineError, CT.CrashTraceError) as e:
        require(False, f"baseline build failed: {e}", **ev, **getattr(e, "evidence", {}))
    binary = B.SERVER_BIN / "worldserver"
    ev["baseline_binary_uuid"] = binary_uuid(binary)
    ev["real_worldserver_sha256"] = build._sha256_file(P.SERVER_BIN / "worldserver")
    main_pids = stack.stack_processes()["worldserver"]
    require(len(main_pids) == 1, "expected exactly one real worldserver", worldservers=main_pids, **ev)
    ev["real_worldserver_stop"] = stack.stop_worldserver(main_pids[0])
    proc = None
    try:
        ev["crashtrace_records_cleared"] = CT.clear(CRASHTRACE_DIR)
        proc, ev["baseline_start"] = B.start_worldserver(extra_env=CT.env(dylib, CRASHTRACE_DIR))
        lsof = subprocess.run(["lsof", "-p", str(proc.pid), "-Fn"], capture_output=True, text=True).stdout
        ev["crashtrace_loaded"] = str(dylib) in lsof
        reports_before = crash_reports()
        rows = race_loop(proc.pid)
        crashed = not stack.alive(proc.pid)
        ev |= {"crashed": crashed, "crashed_on_attempt": rows[-1]["i"] if crashed and rows else None,
               "summary": _summ_rows(rows), "attempts": rows}
        if crashed:
            ev["baseline_returncode"] = proc.wait(timeout=30)
        ev["crash_trace"] = CT.read(CRASHTRACE_DIR, proc.pid, binary)
        # Supplementary, never gating: a fresh ReportCrash .ips for this pid, if macOS wrote one (it may coalesce).
        if crashed:
            time.sleep(IPS_WAIT_S)
            ev["supplementary_ips_reports"] = new_reports_for(proc.pid, reports_before)
    except CT.CrashTraceError as e:
        ev["crash_trace_error"] = str(e)
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
    require(ev["crashtrace_loaded"], "the crashtrace shim is not loaded in the baseline worldserver", **ev)
    require(ev["crashed"], f"unpatched baseline worldserver survived {len(ev['attempts'])} attempts", **ev)
    require(ev.get("baseline_returncode") == -signal.SIGSEGV,
            f"baseline worldserver did not die of SIGSEGV (returncode {ev.get('baseline_returncode')})", **ev)
    tr = ev.get("crash_trace")
    require(tr is not None and tr.get("pid") == proc.pid and tr.get("complete"),
            "no complete crash backtrace recorded by this run's baseline worldserver pid", **ev)
    require(tr.get("signo") == signal.SIGSEGV, f"recorded signal is {tr.get('signo')}, not SIGSEGV", **ev)
    require(ev["baseline_binary_uuid"] and tr.get("uuid") == ev["baseline_binary_uuid"],
            "the crashed process's main image UUID is not the baseline worldserver's", **ev)
    require(tr.get("hardware_fault"), "SIGSEGV was not a bad-access fault (ESR exception class "
            f"{tr.get('esr_exception_class')}; e.g. sent with kill)", **ev)
    frames = " | ".join(tr.get("top_frames", []))
    named = [n for n in DEFECT_FRAMES if n in frames]
    ev["defect_frames_named"] = named
    require("ServerMailMgr::SendServerMail" in frames or "ServerMailReward::OnPlayerLogin" in frames,
            "this crash's top frames don't name ServerMailMgr::SendServerMail or the server_mail.cpp callback", **ev)
    require(ev["baseline_build"]["worldserver_sha256"] != ev["real_worldserver_sha256"],
            "baseline worldserver binary is identical to the real (patched) one", **ev)
    require(ev["server_digest_after"] == ev["server_digest_before"], "the real server/ install changed", **ev)
    require(not ev["baseline_worldserver_pids_after"], "a baseline worldserver is still running", **ev)
    require(len(ev["real_worldserver_pids_after"]) == 1 and isinstance(ev["soap_after_restore"], dict)
            and ev["soap_after_restore"].get("ok"), "normal stack not restored (worldserver/SOAP)", **ev)
    return ev


# ----------------------------------------------------------------------------- playerbots-facing patches (ADR-0007/0008)

HOOK_PATCH = "0002-playerbots-hook-packet-copy.patch"
WORLDSESSION_CPP = "src/server/game/Server/WorldSession.cpp"
BOTS_PATCH = "0001-remove-bot-from-group-on-logout.patch"
BOTS_FILES = ["src/Bot/PlayerbotMgr.cpp", "src/Bot/PlayerbotMgr.h", "src/Script/WorldThr/PlayerbotOperations.h"]
HOOK_SITE_RE = re.compile(r"#ifdef MOD_PLAYERBOTS\s*WorldPacket hookPacket\(\*packet\);[^\n]*\n\s*#endif\s*"
                          r"opHandle->Call\(this, \*packet\);\s*LogUnprocessedTail\(packet\);\s*"
                          r"#ifdef MOD_PLAYERBOTS\s*sScriptMgr->OnPacketReceived\(this, hookPacket\);\s*#endif")


def hook_problems(text: str) -> list[str]:
    """What's missing for 0002 in a WorldSession.cpp text: every OnPacketReceived call gets a copy taken before the
    handler runs (typed handlers move the packet out, Opcodes.cpp PacketHandler::Call)."""
    probs = []
    calls = len(re.findall(r"sScriptMgr->OnPacketReceived\(", text))
    sites = len(HOOK_SITE_RE.findall(text))
    if calls == 0:
        probs.append("no OnPacketReceived call")
    if sites != calls:
        probs.append(f"{calls - sites} of {calls} OnPacketReceived calls don't get a pre-handler copy")
    if re.search(r"OnPacketReceived\(this, \*packet\)", text):
        probs.append("OnPacketReceived still gets *packet (emptied by typed handlers)")
    return probs


def remove_problems(texts: dict[str, str]) -> list[str]:
    """What's missing for playerbots 0001: `remove` logs out with leaveGroup, the cleanup op removes the bot from the
    captured group by GUID, and stays valid after the bot is gone."""
    probs = []
    mgr, ops, hdr = texts.get(BOTS_FILES[0], ""), texts.get(BOTS_FILES[2], ""), texts.get(BOTS_FILES[1], "")
    if not re.search(r'cmd == "remove".*?LogoutPlayerBot\(guid, true\);', mgr, re.S):
        probs.append("the remove command doesn't call LogoutPlayerBot(guid, true)")
    if "void LogoutPlayerBot(ObjectGuid guid, bool leaveGroup = false);" not in hdr:
        probs.append("LogoutPlayerBot has no leaveGroup parameter")
    if not re.search(r"BotLogoutGroupCleanupOperation>\(guid, leaveGroupGuid\)", mgr):
        probs.append("LogoutPlayerBot doesn't pass the bot's group to the cleanup operation")
    if not re.search(r"group->IsMember\(m_botGuid\)\)\s*group->RemoveMember\(m_botGuid\);", ops):
        probs.append("cleanup operation doesn't RemoveMember the bot")
    if "return !m_leaveGroupGuid.IsEmpty() || ObjectAccessor::FindPlayer(m_botGuid) != nullptr;" not in ops:
        probs.append("cleanup operation is skipped once the bot is offline (IsValid)")
    return probs


def _apply_queue(repo: Path, sha: str, patches: list[Path], files: list[str]) -> dict:
    """Apply `patches` in order to `sha` in a throwaway index; return per-patch git results and the patched files."""
    out = {"applied": [], "files": {}}
    with tempfile.TemporaryDirectory(prefix="hs-queue-") as td:
        env = os.environ | {"GIT_INDEX_FILE": str(Path(td) / "index")}

        def g(*a):
            return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, env=env)
        out["read_tree_exit"] = g("read-tree", sha).returncode
        for p in patches:
            before = g("write-tree").stdout.strip()
            r = g("apply", "--cached", "--whitespace=nowarn", str(p))
            changed = g("diff", "--name-only", before, g("write-tree").stdout.strip()).stdout.split() if r.returncode == 0 else []
            out["applied"].append({"patch": p.name, "exit": r.returncode, "stderr": r.stderr[-500:], "files": changed})
        out["files"] = {f: g("show", f":{f}").stdout for f in files}
    return out


@check("core.playerbots_patches_applied", order=23, capability="server-build", stack="any", tier="quick")
def playerbots_patches_applied(ctx):
    """patches/core/0002 (hook packet copy, ADR-0007) applies after 0001 to core_sha and patches every
    OnPacketReceived site; patches/playerbots/0001 (remove leaves the party, ADR-0008) applies to playerbots_sha;
    both vendor trees are sha + exactly their queue; the installed build is stamped with both patches; editing either
    patch file changes the build-input hash."""
    lock = P.fork_lock()
    core_q, bots_q = build.core_patches(), build.playerbots_patches()
    ev = {"core_queue": [p.name for p in core_q], "playerbots_queue": [p.name for p in bots_q]}
    require(HOOK_PATCH in ev["core_queue"], f"patches/core/{HOOK_PATCH} missing", **ev)
    require(BOTS_PATCH in ev["playerbots_queue"], f"patches/playerbots/{BOTS_PATCH} missing", **ev)
    c = _apply_queue(P.CORE_SRC, lock["core_sha"], core_q, [WORLDSESSION_CPP])
    b = _apply_queue(P.BOTS_SRC, lock["playerbots_sha"], bots_q, BOTS_FILES)
    ev["core_apply"], ev["playerbots_apply"] = c["applied"], b["applied"]
    hook = next(a for a in c["applied"] if a["patch"] == HOOK_PATCH)
    bots = next(a for a in b["applied"] if a["patch"] == BOTS_PATCH)
    require(all(a["exit"] == 0 for a in c["applied"] + b["applied"]), "a patch does not apply cleanly to its pin", **ev)
    require(hook["files"] == [WORLDSESSION_CPP], "0002 changes files other than WorldSession.cpp", **ev)
    require(sorted(bots["files"]) == sorted(BOTS_FILES), "playerbots 0001 changes unexpected files", **ev)
    ev["pristine_hook_problems"] = hook_problems(c["files"][WORLDSESSION_CPP])
    ev["pristine_remove_problems"] = remove_problems(b["files"])
    ev["vendor_hook_problems"] = hook_problems((P.CORE_SRC / WORLDSESSION_CPP).read_text())
    ev["vendor_remove_problems"] = remove_problems({f: (P.BOTS_SRC / f).read_text() for f in BOTS_FILES})
    ev["pin_state"] = {"core": build.patched_pin_state(P.CORE_SRC, lock["core_sha"], core_q, "modules/"),
                       "playerbots": build.patched_pin_state(P.BOTS_SRC, lock["playerbots_sha"], bots_q)}
    require(not ev["pristine_hook_problems"] and not ev["pristine_remove_problems"],
            "patched pins lack the intended change", **ev)
    require(not ev["vendor_hook_problems"] and not ev["vendor_remove_problems"],
            "the vendor trees the build used lack the patches (run `hs build`)", **ev)
    require(not ev["pin_state"]["core"] and not ev["pin_state"]["playerbots"],
            "a vendor tree is not its sha + exactly its patch queue", **ev)
    try:
        stamp = json.loads(build.install_stamp_path().read_text())
    except (OSError, ValueError):
        stamp = {}
    man = stamp.get("manifest", {})
    ev["installed_stamp"] = {"patches_core": man.get("patches_core"), "patches_playerbots": man.get("patches_playerbots")}
    require((man.get("patches_core") or {}).get(HOOK_PATCH) == build._sha256_file(P.PATCHES / HOOK_PATCH)
            and (man.get("patches_playerbots") or {}).get(BOTS_PATCH) == build._sha256_file(P.BOTS_PATCHES / BOTS_PATCH),
            "installed binaries are not stamped as built with both patches (run `hs build --clean`)", **ev)
    real = build.input_manifest()
    h_real = build.input_hash(real)
    with tempfile.TemporaryDirectory(prefix="hs-bots-hash-") as td:
        td = Path(td)
        for name, src in (("core", P.PATCHES), ("bots", P.BOTS_PATCHES), ("core_x", P.PATCHES), ("bots_x", P.BOTS_PATCHES)):
            shutil.copytree(src, td / name)
        (td / "core_x" / HOOK_PATCH).write_text((td / "core_x" / HOOK_PATCH).read_text() + "# hs: content change\n")
        (td / "bots_x" / BOTS_PATCH).write_text((td / "bots_x" / BOTS_PATCH).read_text() + "# hs: content change\n")

        def h(core, bots):
            return build.input_hash(build.input_manifest(patches_dir=td / core, bots_patches_dir=td / bots,
                                                         cmake=real["cmake_args"], compiler=real["compiler"]))
        ev["input_hash"] = {"real": h_real, "copies_unchanged": h("core", "bots"), "core_0002_edited": h("core_x", "bots"),
                            "playerbots_0001_edited": h("core", "bots_x")}
    hs = ev["input_hash"]
    require(hs["copies_unchanged"] == h_real, "input hash of unchanged patch copies differs (hash unstable)", **ev)
    require(hs["core_0002_edited"] != h_real and hs["playerbots_0001_edited"] != h_real,
            "editing a patch file did not change the build-input hash", **ev)
    return ev
