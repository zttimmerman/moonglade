"""play-client: the derived, revertable play client (ADR-0009; openspec/changes/in-client-login).

Every build here goes into a scratch destination (env/check-play-*), never env/client-play/ except where the spec
names it (client.stock_mode), and never into client/, whose manifest match is asserted before and after."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .. import clientbuild, launch, paths as P, pristine
from ..clientinst import load_manifest, mismatches, sha256_file
from ..registry import check, require
from .play_checks import is_clear, remaining, require_given_clear
from .playclient_util import PlayRun, drop_scratch, fresh_scratch

CAP = "play-client"
RECORD = clientbuild.QUEUE / "0001-gluexml-signature.json"
ADR = P.ROOT / "docs" / "adr" / "0009-client-patch-overlay.md"


def _client_untouched(ev: dict, when: str) -> None:
    man = load_manifest()
    mm = mismatches(P.CLIENT, man)
    ev[f"client_manifest_mismatches_{when}"] = mm
    ev[f"client_wow_sha256_{when}"] = sha256_file(P.CLIENT / "Wow.exe")
    require(not mm, f"client/ doesn't match env/client-manifest.json {when}", **ev)


def verify_record(rec: dict, adr_text: str, exe: bytes) -> dict:
    """Problems with a patch-0001 record against the pristine exe bytes and the ADR text (empty = fine)."""
    problems, ev = [], {}
    for k in ("file", "input_sha256", "output_sha256", "edits"):
        if not rec.get(k):
            problems.append(f"record lacks {k}")
    if problems:
        return {"problems": problems}
    ev["input_matches_client"] = hashlib.sha256(exe).hexdigest() == rec["input_sha256"]
    if not ev["input_matches_client"]:
        problems.append("input_sha256 isn't client/Wow.exe's hash")
    data = bytearray(exe)
    ev["edits"] = []
    for e in rec["edits"]:
        off, orig, new = int(e["offset"], 16), bytes.fromhex(e["original"]), bytes.fromhex(e["replacement"])
        got = bytes(exe[off:off + len(orig)])
        item = {"offset": e["offset"], "va": e.get("va"), "original": e["original"], "replacement": e["replacement"],
                "bytes_at_offset": got.hex()}
        if got != orig:
            problems.append(f"original bytes at {e['offset']} are {got.hex()}, record says {orig.hex()}")
        if len(orig) != len(new):
            problems.append(f"edit at {e['offset']} changes length")
        data[off:off + len(new)] = new
        # the ADR appendix must cite the same values and the disassembly address
        spaced = lambda h: " ".join(h[i:i + 2] for i in range(0, len(h), 2))
        cites = {"offset": e["offset"] in adr_text, "va": bool(e.get("va")) and e["va"] in adr_text,
                 "original": spaced(e["original"]) in adr_text, "replacement": spaced(e["replacement"]) in adr_text}
        item["adr_cites"] = cites
        problems += [f"ADR appendix doesn't cite the {k} of the edit at {e['offset']}" for k, v in cites.items() if not v]
        ev["edits"].append(item)
    ev["computed_output_sha256"] = hashlib.sha256(bytes(data)).hexdigest()
    if ev["computed_output_sha256"] != rec["output_sha256"]:
        problems.append("applying the edits doesn't give output_sha256")
    for k in ("input_sha256", "output_sha256"):
        if rec[k] not in adr_text:
            problems.append(f"ADR appendix doesn't cite {k}")
    if "Appendix A" not in adr_text:
        problems.append("ADR-0009 has no Appendix A")
    return ev | {"problems": problems}


# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "patches/client/**",
    "client-mods/**",
    "harness/hslib/checks/client_play_checks.py",
    "harness/hslib/clientinst.py",
    "harness/hslib/pristine.py",
)


@check("client.patch0001_recorded", order=225, capability=CAP, stack="any", layer="unit", parallel_safe=True)
def patch0001_recorded(ctx):
    """Patch 0001's record names offsets, original/new bytes, input/output SHA-256 (consistent with client/Wow.exe and
    with each other), and the ADR-0009 appendix cites the same values and the disassembly addresses."""
    rec = json.loads(RECORD.read_text())
    adr = ADR.read_text()
    ev = {"record": str(RECORD), "adr": str(ADR)}
    ev |= verify_record(rec, adr, (P.CLIENT / "Wow.exe").read_bytes())
    require(not ev["problems"], "; ".join(ev["problems"]), **ev)
    return ev


def _flip_copy(base: Path, dest: Path, how: str) -> None:
    """A scratch base: APFS clone of client/ with Wow.exe changed (`how`: "byte" = one byte elsewhere, "patched" =
    patch 0001 already applied)."""
    pristine.remove_tree(dest)
    subprocess.run(["cp", "-cR", os.path.realpath(base), str(dest)], check=True)
    pristine.unlock(dest)                       # the clone inherits client/'s write protection (pristine.py)
    wow = dest / "Wow.exe"
    data = bytearray(wow.read_bytes())
    wow.unlink()                                # new inode: never write through the clone's shared blocks
    if how == "byte":
        data[0x1000] ^= 0xFF
    else:
        rec = json.loads(RECORD.read_text())
        for e in rec["edits"]:
            off, new = int(e["offset"], 16), bytes.fromhex(e["replacement"])
            data[off:off + len(new)] = new
    wow.write_bytes(bytes(data))


@check("client.play_build", order=226, capability=CAP, stack="any", layer="unit", parallel_safe=True)
def play_build(ctx):
    """`hs client-build` twice gives identical stamped outputs (second run a no-op), the patched Wow.exe equals patch
    0001's output hash, client/ matches its manifest before and after; a wrong base, an already-patched exe and wrong
    original bytes are each refused with an error naming the mismatch, writing nothing."""
    rec = json.loads(RECORD.read_text())
    ev: dict = {}
    _client_untouched(ev, "before")
    dest = fresh_scratch("build")
    scratch_base = P.ENV / "check-play-base"
    bad_queue = P.ENV / "check-play-queue"
    try:
        runs = []
        for i in (1, 2):
            r = subprocess.run([str(P.HARNESS / "hs"), "client-build", "--dest", str(dest)], capture_output=True,
                               text=True, timeout=600)
            runs.append({"rc": r.returncode, "stdout": r.stdout[-1500:], "stderr": r.stderr[-800:],
                         "stamp": clientbuild.read_stamp(dest)})
            require(r.returncode == 0, f"hs client-build run {i} failed", runs=runs, **ev)
        ev["runs"] = [{"rc": x["rc"], "output_tail": x["stdout"][-600:]} for x in runs]
        s1, s2 = runs[0]["stamp"], runs[1]["stamp"]
        ev["stamp_outputs"] = [s1["outputs"], s2["outputs"]]
        ev["second_run_noop"] = '"built": false' in runs[1]["stdout"]
        ev["patched_wow_sha256"] = sha256_file(dest / "Wow.exe")
        require(s1["outputs"] == s2["outputs"], "the two builds recorded different output hashes", **ev)
        require(ev["second_run_noop"], "the second build with identical inputs wasn't a no-op", **ev)
        require(ev["patched_wow_sha256"] == rec["output_sha256"] == s1["outputs"]["Wow.exe"],
                "patched Wow.exe hash isn't patch 0001's output hash", **ev)
        _client_untouched(ev, "after")

        # refusals: each must raise naming the mismatch and leave the destination absent
        refusals = {}
        for how, want in (("byte", "doesn't match the patch's input hash"), ("patched", "already patched")):
            _flip_copy(P.CLIENT, scratch_base, how)
            d = fresh_scratch(f"refuse-{how}")
            try:
                clientbuild.build(dest=d, base=scratch_base)
                refusals[how] = {"refused": False}
            except clientbuild.ClientBuildError as e:
                refusals[how] = {"refused": True, "error": str(e), "names_mismatch": want in str(e),
                                 "dest_written": d.exists(),
                                 "temp_left": sorted(p.name for p in d.parent.glob(d.name + ".build-*"))}
        # a record whose original bytes don't match (hashes right): copy the queue with 0001's first edit altered
        shutil.rmtree(bad_queue, ignore_errors=True)
        shutil.copytree(clientbuild.QUEUE, bad_queue)
        bad = json.loads(RECORD.read_text())
        bad["edits"][0]["original"] = "90" * (len(bad["edits"][0]["original"]) // 2)
        (bad_queue / RECORD.name).write_text(json.dumps(bad, indent=2))
        d = fresh_scratch("refuse-bytes")
        try:
            clientbuild.build(dest=d, queue_dir=bad_queue)
            refusals["original_bytes"] = {"refused": False}
        except clientbuild.ClientBuildError as e:
            refusals["original_bytes"] = {"refused": True, "error": str(e), "names_mismatch": "original bytes" in str(e),
                                          "dest_written": d.exists()}
        ev["refusals"] = refusals
        for k, v in refusals.items():
            require(v.get("refused") and v.get("names_mismatch") and not v.get("dest_written") and not v.get("temp_left"),
                    f"build with {k} wasn't refused cleanly", **ev)
        _client_untouched(ev, "after_refusals")
        return ev
    finally:
        for d in (dest, scratch_base, bad_queue, fresh_scratch("refuse-byte"), fresh_scratch("refuse-patched"),
                  fresh_scratch("refuse-bytes")):
            shutil.rmtree(d, ignore_errors=True)


@check("client.play_revert", order=227, capability=CAP, stack="any", layer="unit", parallel_safe=True)
def play_revert(ctx):
    """Rebuilding a built play client with patch 0001 removed from the queue (`hs client-build --skip 0001`) gives a
    Wow.exe byte-identical to client/Wow.exe; adding it back gives the patched hash again."""
    rec = json.loads(RECORD.read_text())
    ev: dict = {}
    dest = fresh_scratch("revert")
    try:
        steps = []
        for args in ([], ["--skip", "0001"], []):
            r = subprocess.run([str(P.HARNESS / "hs"), "client-build", "--dest", str(dest), *args],
                               capture_output=True, text=True, timeout=600)
            steps.append({"args": args, "rc": r.returncode, "wow_sha256": sha256_file(dest / "Wow.exe")
                          if (dest / "Wow.exe").exists() else None})
            require(r.returncode == 0, f"hs client-build {args} failed: {r.stdout[-800:]}", steps=steps)
        ev["steps"] = steps
        stock = sha256_file(P.CLIENT / "Wow.exe")
        ev["client_wow_sha256"] = stock
        require(steps[0]["wow_sha256"] == rec["output_sha256"], "first build isn't patched", **ev)
        require(steps[1]["wow_sha256"] == stock, "with 0001 skipped, Wow.exe isn't identical to client/Wow.exe", **ev)
        require(steps[2]["wow_sha256"] == rec["output_sha256"], "re-adding 0001 didn't patch again", **ev)
        # byte-for-byte too, not just the hash, on the reverted state
        subprocess.run([str(P.HARNESS / "hs"), "client-build", "--dest", str(dest), "--skip", "0001"],
                       capture_output=True, timeout=600)
        ev["reverted_identical_bytes"] = (dest / "Wow.exe").read_bytes() == (P.CLIENT / "Wow.exe").read_bytes()
        require(ev["reverted_identical_bytes"], "reverted Wow.exe differs byte-wise from client/Wow.exe", **ev)
        return ev
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def _launched(run: PlayRun, ev_key: str, ev: dict) -> dict:
    run.start()
    up = run.wait_client()
    info = {"client_up": {k: v for k, v in up.items() if k != "t"}}
    if up.get("up"):
        time.sleep(5)
        w = run.wow_alive()
        info["wow_cmd"] = [p["cmd"] for p in w]
    stop = run.stop()
    rec = run.record()
    info |= {"stop": stop, "record_client": rec.get("client"), "output_tail": run.output()[-1200:]}
    ev[ev_key] = info
    return info


# client.stock_mode: a scenario of a journey (journey_checks.py), not a check of its own (test-pyramid part 2)
def stock_mode(ctx):
    """With `client = stock` (scratch settings) the play loop launches the real client/Wow.exe (pristine hash); with
    `client = patched` it launches the play client's Wow.exe (patched hash): a scratch play client built by the same
    queue for the real game (a check never runs it in the player's own env/client-play/), and, with no scratch
    override, the play loop selects env/client-play/ itself, whose Wow.exe is patch 0001's output (stand-in run,
    config writes gated off; the player's Config.wtf is unchanged)."""
    rec0001 = json.loads(RECORD.read_text())
    ev: dict = {}
    require_given_clear(ev)
    man = load_manifest()
    # rename-moonglade cycle 2: the patched run plays a scratch play client built by the same queue (the real Wow.exe
    # never runs in the player's own env/client-play/ from a check; play.py refuses that), so "the play client's
    # Wow.exe" is the scratch's, with the same patch 0001 output hash env/client-play/Wow.exe has
    scratch = fresh_scratch("mode-patched")
    try:
        for mode in ("stock", "patched"):
            run = PlayRun(f"mode-{mode}", mode=mode, scratch=scratch if mode == "patched" else None)
            try:
                info = _launched(run, mode, ev)
            finally:
                run.cleanup()
            require(info["client_up"].get("up"), f"{mode}: the real Wow.exe never came up", **ev)
    finally:
        drop_scratch(scratch)
    win = lambda p: "Z:" + str(p).replace("/", "\\")
    stock_exe, play_exe = win(P.CLIENT / "Wow.exe").lower(), win(scratch / "Wow.exe").lower()
    ev["player_play_client_exe_sha256"] = sha256_file(clientbuild.PLAY / "Wow.exe") \
        if (clientbuild.PLAY / "Wow.exe").exists() else None
    s, pt = ev["stock"], ev["patched"]
    ev["expected"] = {"stock_exe": stock_exe, "stock_sha256": man["binaries_sha256"]["Wow.exe"],
                      "patched_exe": play_exe, "patched_sha256": rec0001["output_sha256"]}
    require(any(c.lower().startswith(stock_exe) for c in s.get("wow_cmd", [])),
            "stock mode didn't launch client/Wow.exe", **ev)
    require((s["record_client"] or {}).get("exe_sha256") == man["binaries_sha256"]["Wow.exe"]
            and (s["record_client"] or {}).get("mode") == "stock", "stock mode's exe hash isn't the pristine one", **ev)
    require(any(c.lower().startswith(play_exe) for c in pt.get("wow_cmd", [])),
            "patched mode didn't launch the play client's Wow.exe (scratch play client)", **ev)
    require((pt["record_client"] or {}).get("exe_sha256") == rec0001["output_sha256"],
            "patched mode's exe hash isn't patch 0001's output", **ev)
    # cycle 3: the player's half of the THEN ("env/client-play/ for the player"), on the play loop's own selection
    ev["player_path"] = pp = player_path_run()
    rc_ = pp.get("record_client") or {}
    require(pp["exit_code"] == 0 and pp["client_up"].get("up"), "the patched stand-in run didn't run and end cleanly",
            **ev)
    require(rc_.get("mode") == "patched" and rc_.get("dir") == str(clientbuild.PLAY.resolve()),
            "with no scratch override, patched mode didn't select the player's env/client-play/", **ev)
    require(rc_.get("exe") == str(clientbuild.PLAY.resolve() / "Wow.exe")
            and rc_.get("exe_sha256") == rec0001["output_sha256"],
            "the player's play client Wow.exe isn't patch 0001's output", **ev)
    require(pp["player_config_sha_after"] == pp["player_config_sha_before"],
            "the patched stand-in run changed the player's Config.wtf", **ev)
    ev["remaining_after"] = remaining()
    require(is_clear(ev["remaining_after"]), "something is still running", **ev)
    return ev


def player_path_run() -> dict:
    """Patched mode with no scratch override (no $HS_PLAY_CLIENT_DIR): ./play with the stand-in (--client-cmd), so the
    real game never runs in the player's client and the config writes there are gated off. Returns the play loop's
    own record of the client it selected (dir, exe, exe sha256) and the player's Config.wtf hash before/after."""
    from .play_checks import StandIn
    from .. import play
    player_cfg = clientbuild.PLAY / "WTF" / "Config.wtf"
    ev: dict = {"player_config_sha_before": sha256_file(player_cfg) if player_cfg.exists() else None,
                "env_overrides_present": [k for k in ("HS_PLAY_CLIENT_DIR", "HS_TEST_OWN_CLIENT", "HS_CLIENT_DIR")
                                          if os.environ.get(k)]}
    require(not ev["env_overrides_present"], "GIVEN not met: a client override is set in the harness environment", **ev)
    s = StandIn("mode-player")
    sfile = s.dir / "settings.json"
    sfile.write_text(json.dumps({"client": "patched"}))
    try:
        s.start_play(env={"HS_SETTINGS": str(sfile)})
        ev["client_up"] = s.wait_client_up()
        s.stop_file.write_text("quit")
        rc, _ = s.wait_exit(timeout=300)
        rec = json.loads(play.LAST.read_text()) if play.LAST.exists() else {}
        rec = rec if rec.get("pid") == (s.proc.pid if s.proc else None) else {"stale_record_pid": rec.get("pid")}
        ev |= {"exit_code": rc, "record_client": {k: (rec.get("client") or {}).get(k) for k in ("mode", "dir", "exe",
                                                                                              "exe_sha256")},
               "gate": rec.get("resolution_gate"), "client_config": (rec.get("prepare") or {}).get("client_config"),
               "player_config_sha_after": sha256_file(player_cfg) if player_cfg.exists() else None}
        return ev
    finally:
        s.cleanup()
