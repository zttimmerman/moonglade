"""play-client: the derived, revertable play client (ADR-0009; openspec/changes/in-client-login).

Every build here goes into a scratch destination (env/check-play-*), never env/client-play/ except where the spec
names it (client.stock_mode), and never into client/, whose manifest match is asserted before and after."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from .. import clientbuild, launch, paths as P
from ..clientinst import load_manifest, mismatches, sha256_file
from ..registry import check, require
from .play_checks import is_clear, remaining, require_given_clear
from .playclient_util import PlayRun, fresh_scratch

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


@check("client.patch0001_recorded", order=225, capability=CAP, stack="any", tier="quick")
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
    shutil.rmtree(dest, ignore_errors=True)
    subprocess.run(["cp", "-cR", str(base), str(dest)], check=True)
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


@check("client.play_build", order=226, capability=CAP, stack="any", tier="quick")
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


@check("client.play_revert", order=227, capability=CAP, stack="any", tier="quick")
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


@check("client.stock_mode", order=386, capability=CAP, stack="down", tier="full")
def stock_mode(ctx):
    """With `client = stock` (scratch settings) the play loop launches the real client/Wow.exe (pristine hash); with
    `client = patched` it launches env/client-play/Wow.exe (patched hash)."""
    rec0001 = json.loads(RECORD.read_text())
    ev: dict = {}
    require_given_clear(ev)
    man = load_manifest()
    for mode in ("stock", "patched"):
        run = PlayRun(f"mode-{mode}", mode=mode)
        try:
            info = _launched(run, mode, ev)
        finally:
            run.cleanup()
        require(info["client_up"].get("up"), f"{mode}: the real Wow.exe never came up", **ev)
    win = lambda p: "Z:" + str(p).replace("/", "\\")
    stock_exe, play_exe = win(P.CLIENT / "Wow.exe").lower(), win(clientbuild.PLAY / "Wow.exe").lower()
    s, pt = ev["stock"], ev["patched"]
    ev["expected"] = {"stock_exe": stock_exe, "stock_sha256": man["binaries_sha256"]["Wow.exe"],
                      "patched_exe": play_exe, "patched_sha256": rec0001["output_sha256"]}
    require(any(c.lower().startswith(stock_exe) for c in s.get("wow_cmd", [])),
            "stock mode didn't launch client/Wow.exe", **ev)
    require((s["record_client"] or {}).get("exe_sha256") == man["binaries_sha256"]["Wow.exe"]
            and (s["record_client"] or {}).get("mode") == "stock", "stock mode's exe hash isn't the pristine one", **ev)
    require(any(c.lower().startswith(play_exe) for c in pt.get("wow_cmd", [])),
            "patched mode didn't launch env/client-play/Wow.exe", **ev)
    require((pt["record_client"] or {}).get("exe_sha256") == rec0001["output_sha256"],
            "patched mode's exe hash isn't patch 0001's output", **ev)
    ev["remaining_after"] = remaining()
    require(is_clear(ev["remaining_after"]), "something is still running", **ev)
    return ev
