"""signing.notarized's judgement (openspec/changes/release-signing, "Notarized, stapled release"), stdlib only so the
release workflow can run it on the macOS runner with the system Python, outside the harness:

    /usr/bin/python3 -I harness/hslib/notarycheck.py --build env/release/build --out env/release/out \
        --smoke env/release/smoke.json

It reads that run's records only: build/sign.json (every Mach-O we signed: Developer ID authority, the team, a secure
timestamp, the hardened runtime; the bundle's default Developer ID requirement), build/notary-log.json (status
Accepted, no issues), build/notarize.json (the step's own record: Accepted, `stapler validate` passed, the re-zipped
file's sha256), build/spctl.txt (the workflow's `spctl --assess --type execute -vv` on a fresh unzip of the final
zip: accepted, source=Notarized Developer ID), the smoke record (passed, on the signed app) and out/SHA256SUMS (lists
the stapled zip with the hash of the file that is there). Exit 0 and "PASS" only when every clause holds.

Locally the harness check `signing.notarized` never judges a real release (local builds are never Developer ID
signed); it runs `self_test()` (a synthetic good record set passes, the same set with an issue in the notary log
fails) and reports not applicable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def _load(p: Path, probs: list[str], what: str):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError) as e:
        probs.append(f"{what} ({p}) unreadable: {type(e).__name__}: {e}")
        return None


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def problems(build: Path, out: Path, smoke: Path) -> tuple[list[str], dict]:
    probs: list[str] = []
    ev: dict = {"build": str(build), "out": str(out), "smoke": str(smoke)}
    sign = _load(build / "sign.json", probs, "sign record") or {}
    ev["mode"] = sign.get("mode")
    if sign.get("mode") != "developer-id":
        probs.append(f"the app wasn't signed in developer-id mode ({sign.get('mode')!r})")
    team = (sign.get("bundle") or {}).get("team")
    signed = sign.get("signed") or []
    ev["signed_files"] = len(signed)
    ev["team"] = team
    if not team:
        probs.append("the bundle signature names no team")
    if not signed:
        probs.append("the sign record lists no signed files")
    for f in signed:
        auth = (f.get("authority") or [""])[0] or ""
        flags = str(f.get("flags") or "")
        if not auth.startswith("Developer ID Application:") or f.get("team") != team:
            probs.append(f"{f.get('path')}: not the team's Developer ID ({auth!r}, team {f.get('team')!r})")
        if not f.get("timestamp"):
            probs.append(f"{f.get('path')}: no secure timestamp")
        if "runtime" not in flags and "0x10000" not in flags:
            probs.append(f"{f.get('path')}: no hardened runtime flag ({flags})")
    b = sign.get("bundle") or {}
    if not str((b.get("authority") or [""])[0]).startswith("Developer ID Application:"):
        probs.append(f"bundle: not Developer ID signed ({b.get('authority')})")
    if not b.get("designated") or "cdhash" in str(b.get("designated")) or "anchor apple generic" not in str(b.get("designated")):
        probs.append(f"bundle: designated requirement isn't Apple's Developer ID default ({b.get('designated')!r})")
    log = _load(build / "notary-log.json", probs, "notary log") or {}
    ev["notary_log"] = {"status": log.get("status"), "issues": log.get("issues")}
    if log.get("status") != "Accepted":
        probs.append(f"notary log status {log.get('status')!r}, not Accepted")
    if log.get("issues"):
        probs.append(f"the notary log lists {len(log['issues'])} issue(s)")
    nz = _load(build / "notarize.json", probs, "notarize record") or {}
    ev["notarize"] = {k: nz.get(k) for k in ("status", "issues", "stapler_validate", "rezip")}
    if nz.get("status") != "Accepted":
        probs.append(f"notarize step status {nz.get('status')!r}")
    if (nz.get("stapler_validate") or {}).get("rc") != 0:
        probs.append("stapler validate didn't pass")
    try:
        spctl = (build / "spctl.txt").read_text()
    except OSError as e:
        spctl = ""
        probs.append(f"no spctl assessment ({e})")
    ev["spctl"] = spctl.strip()[-400:]
    if "accepted" not in spctl or "source=Notarized Developer ID" not in spctl or "rejected" in spctl:
        probs.append("spctl on the unzipped final asset isn't accepted with source=Notarized Developer ID")
    sm = _load(smoke, probs, "smoke record") or {}
    ev["smoke_ok"] = sm.get("ok")
    if sm.get("ok") is not True:
        probs.append("the no-Homebrew smoke on the signed app didn't pass")
    zips = sorted(out.glob("*.zip")) if out.is_dir() else []
    ev["zips"] = [z.name for z in zips]
    if len(zips) != 1:
        probs.append(f"expected one zip in {out}, found {len(zips)}")
    else:
        z = zips[0]
        actual = _sha256(z)
        try:
            sums = dict(reversed(line.split(None, 1)) for line in (out / "SHA256SUMS").read_text().splitlines()
                        if line.strip())
        except (OSError, ValueError) as e:
            sums = {}
            probs.append(f"SHA256SUMS unreadable: {e}")
        listed = sums.get(z.name)
        rz = (nz.get("rezip") or {}).get("sha256")
        ev["zip"] = {"name": z.name, "sha256": actual, "in_SHA256SUMS": listed, "rezip_record": rz}
        if listed != actual:
            probs.append("SHA256SUMS doesn't list the zip that is there")
        if rz != actual:
            probs.append("the zip isn't the stapled one the notarize step re-zipped")
    return probs, ev


def self_test(scratch: Path) -> dict:
    """A synthetic Developer ID record set that passes, and the same with one issue in the notary log (fails)."""
    build, out = scratch / "build", scratch / "out"
    build.mkdir(parents=True)
    out.mkdir()
    z = out / "Moonglade-vT-macos-arm64.zip"
    z.write_bytes(b"synthetic stapled zip\n")
    h = _sha256(z)
    (out / "SHA256SUMS").write_text(f"{h}  {z.name}\n")
    dev = "Developer ID Application: Synthetic (TEAMSYNTH1)"
    signed = [{"path": p, "authority": [dev], "team": "TEAMSYNTH1", "timestamp": "Oct 9, 2026 at 12:00:00",
               "flags": "0x10000(runtime)"} for p in ("Contents/MacOS/Moonglade", "server/bin/worldserver")]
    (build / "sign.json").write_text(json.dumps({
        "mode": "developer-id", "signed": signed,
        "bundle": {"authority": [dev], "team": "TEAMSYNTH1",
                   "designated": 'designated => anchor apple generic and identifier "io.github.zttimmerman.moonglade" '
                                 'and (certificate leaf[field.1.2.840.113635.100.6.1.13] /* exists */ and '
                                 'certificate leaf[subject.OU] = TEAMSYNTH1)'}}))
    (build / "notarize.json").write_text(json.dumps({"status": "Accepted", "issues": None,
                                                     "stapler_validate": {"rc": 0}, "rezip": {"sha256": h}}))
    (build / "spctl.txt").write_text(f"{scratch}/Moonglade.app: accepted\nsource=Notarized Developer ID\n")
    smoke = scratch / "smoke.json"
    smoke.write_text(json.dumps({"ok": True}))
    res = {}
    for name, issues in (("good", None), ("issue_in_log", [{"severity": "error", "path": "x",
                                                            "message": "The signature does not include a secure timestamp."}])):
        (build / "notary-log.json").write_text(json.dumps({"status": "Accepted", "issues": issues}))
        probs, _ = problems(build, out, smoke)
        res[name] = probs
    res["ok"] = not res["good"] and bool(res["issue_in_log"])
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="notarycheck.py")
    ap.add_argument("--build", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--smoke", type=Path, required=True)
    a = ap.parse_args(argv)
    probs, ev = problems(a.build, a.out, a.smoke)
    print(json.dumps({"check": "signing.notarized", "problems": probs, **ev}, indent=2))
    print("signing.notarized: " + ("PASS" if not probs else f"FAIL ({len(probs)} problem(s))"))
    return 0 if not probs else 1


if __name__ == "__main__":
    sys.exit(main())
