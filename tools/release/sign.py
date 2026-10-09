#!/usr/bin/env python3
"""Release app signing and notarization (openspec/changes/release-signing, design 1-5). Used by release.py's `sign` and
`notarize` steps; not run on its own.

The mode comes from one checked-in file, tools/release/signing.json ({"mode": "ad-hoc"} or {"mode": "developer-id"});
release.py --signing-config may name a scratch copy under env/release/ or harness/out/ (checks only).

  ad-hoc        never reads a secret. Signs inside out: every Mach-O in the payload that isn't Oracle's (Oracle MySQL's
                files keep Oracle's Developer ID signature; their cdhashes are recorded at `payload` and must not
                change), then the launcher, then the bundle with the designated requirement pinned to the bundle id.
                Every file gets the hardened runtime; executables get exactly their role's
                tools/release/entitlements/<role>.plist (none when the role has no file); libraries get none.
  developer-id  first checks that every credential it needs is in the environment (names only, never values) and fails
                naming the missing ones before touching anything; refused outside the release workflow
                (GITHUB_ACTIONS=true). Same inside-out signing with the Developer ID identity from the keychain the
                workflow prepared, a secure timestamp, and Apple's default designated requirement (team + identifier).
                `notarize` submits the zip, saves the notary log, fails unless Accepted with no issues, staples,
                validates, re-zips and refreshes SHA256SUMS / SOURCES.json.

Records: <work>/build/sign.json (mode, identity kind, each signed file's identifier, cdhash, flags, entitlements,
Oracle's recorded and current cdhashes, the bundle's designated requirement, the environment keys this step read),
<work>/build/bundle-files.json (every file in the signed bundle with its sha256; symlinks with their target) and, in
developer-id mode, <work>/build/notary-log.json.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CONFIG = HERE / "signing.json"
ENTITLEMENTS = HERE / "entitlements"
MODES = ("ad-hoc", "developer-id")
# --signing-config is accepted only from these folders, so a stray flag can't redirect a CI build (design 1)
CONFIG_ROOTS = (ROOT / "env" / "release", ROOT / "harness" / "out")
BUNDLE_ID = "io.github.zttimmerman.moonglade"
ADHOC_REQUIREMENT = f'designated => identifier "{BUNDLE_ID}"'
PAYLOAD_REL = Path("Contents") / "Resources" / "Moonglade"
ORACLE_TEAM = "VB5E2TV963"
ORACLE_AUTHORITY = "Developer ID Application: Oracle America, Inc. (VB5E2TV963)"
ORACLE_DIR = "mysql"                       # payload folder holding Oracle MySQL's signed files
# Developer ID credentials (design 5; names only). The .p12 pair is consumed by the workflow's keychain step, so the
# packaging process never sees the certificate; it needs the identity name, the team and the notary key.
DEVID_CREDENTIALS = ("MOONGLADE_DEVID_IDENTITY", "MOONGLADE_TEAM_ID", "MOONGLADE_NOTARY_KEY_P8_BASE64",
                     "MOONGLADE_NOTARY_KEY_ID", "MOONGLADE_NOTARY_ISSUER_ID")
WORKFLOW_CREDENTIALS = ("MOONGLADE_DEVID_P12_BASE64", "MOONGLADE_DEVID_P12_PASSWORD") + DEVID_CREDENTIALS
# executables by role (payload-relative, plus the bundle's main executable); every other Mach-O must be a library
ROLES = {
    "Contents/MacOS/Moonglade": "launcher",
    "bin/Moonglade": "launcher",
    "python/bin/python3.13": "python",
    "server/bin/authserver": "server",
    "server/bin/worldserver": "server",
}
# code may only sit here (payload-relative folders; release-signing spec "Release app identity and layout")
CODE_DIRS = ("bin", "server/bin", "server/lib", "mysql/bin", "mysql/lib", "python/bin", "python/lib")
CODE_LOCATIONS = ("Contents/MacOS", "Contents/Frameworks", "Contents/Helpers", "Contents/PlugIns")
MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
MH_EXECUTE = 2
CODESIGN = "/usr/bin/codesign"
DITTO_ZIP = ("/usr/bin/ditto", "-c", "-k", "--keepParent", "--norsrc", "--noextattr", "--noqtn")


class SignError(RuntimeError):
    pass


class EnvReader:
    """os.environ access that remembers which keys were read (the record lists them; ad-hoc reads none)."""

    def __init__(self, env=None):
        self.env = os.environ if env is None else env
        self.read: list[str] = []

    def get(self, key: str) -> str | None:
        if key not in self.read:
            self.read.append(key)
        v = self.env.get(key)
        return v if v else None


# ------------------------------------------------------------------------------------------------------ mode

def config_path(arg: Path | None) -> Path:
    if arg is None:
        return CONFIG
    p = Path(arg).resolve()
    if not any(p == r.resolve() or r.resolve() in p.parents for r in CONFIG_ROOTS):
        raise SignError(f"--signing-config {arg}: only a file under env/release/ or harness/out/ is accepted "
                        f"(the release reads its mode from {CONFIG.relative_to(ROOT)})")
    return p


def read_mode(path: Path) -> str:
    allowed = " or ".join(f'"{m}"' for m in MODES)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise SignError(f"signing mode file {path} is missing; it must hold {{\"mode\": {allowed}}}") from None
    except (OSError, ValueError) as e:
        raise SignError(f"signing mode file {path} can't be read ({e}); it must hold {{\"mode\": {allowed}}}") from None
    mode = data.get("mode") if isinstance(data, dict) else None
    if mode not in MODES:
        raise SignError(f"signing mode file {path}: unknown mode {mode!r}; allowed modes: {allowed}")
    return mode


def missing_credentials(env: EnvReader) -> list[str]:
    return [k for k in DEVID_CREDENTIALS if env.get(k) is None]


# ---------------------------------------------------------------------------------------------- inspecting

def _run(cmd, *, check=True, env=None) -> subprocess.CompletedProcess:
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise SignError(f"{' '.join(map(str, cmd))} failed ({r.returncode}): {(r.stderr + r.stdout)[-1200:]}")
    return r


def is_macho(p: Path) -> bool:
    if p.is_symlink() or not p.is_file():
        return False
    with open(p, "rb") as fh:
        return fh.read(4) in MACHO_MAGIC


def macho_filetype(p: Path) -> int:
    """The Mach-O filetype (MH_EXECUTE, MH_DYLIB, MH_BUNDLE...) of a thin file or a fat file's first slice."""
    with open(p, "rb") as fh:
        head = fh.read(4096)
    magic = head[:4]
    if magic in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
        off = struct.unpack(">I", head[16:20])[0]        # first fat_arch: cputype, cpusubtype, offset, ...
        with open(p, "rb") as fh:
            fh.seek(off)
            head = fh.read(16)
        magic = head[:4]
    endian = "<" if magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe") else ">"
    return struct.unpack(endian + "I", head[12:16])[0]


def machos(app: Path) -> list[Path]:
    return sorted(p for p in app.rglob("*") if is_macho(p))


def rel_of(app: Path, p: Path) -> str:
    return str(p.relative_to(app))


def payload_rel(app: Path, p: Path) -> str | None:
    try:
        return str(p.relative_to(app / PAYLOAD_REL))
    except ValueError:
        return None


def display(p: Path) -> dict:
    """codesign -dvvv of one file or bundle: identifier, cdhash, flags, authorities, team, timestamp, requirement."""
    r = _run([CODESIGN, "-dvvv", "-r-", p], check=False)
    out = (r.stderr or "") + (r.stdout or "")
    info: dict = {"rc": r.returncode, "authority": []}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k == "Identifier":
            info["identifier"] = v
        elif k == "CDHash":
            info["cdhash"] = v
        elif k == "Authority":
            info["authority"].append(v)
        elif k == "TeamIdentifier":
            info["team"] = None if v == "not set" else v
        elif k == "Timestamp":
            info["timestamp"] = v
        elif k == "Signature":
            info["signature"] = v
        elif k == "CodeDirectory v":
            info["flags"] = v.split("flags=", 1)[1].split()[0] if "flags=" in v else None
        elif line.startswith("designated => "):
            info["designated"] = line
    if r.returncode != 0:
        info["error"] = out.strip()[-400:]
    return info


def entitlements_of(p: Path) -> tuple[dict, bytes]:
    """(entitlements dict, raw XML bytes) as codesign shows them; ({}, b"") when the file carries none."""
    r = subprocess.run([CODESIGN, "-d", "--entitlements", "-", "--xml", str(p)], capture_output=True)
    if r.returncode != 0:
        raise SignError(f"codesign -d --entitlements {p} failed: {r.stderr.decode(errors='replace')[-400:]}")
    raw = r.stdout.strip()
    if not raw:
        return {}, b""
    return plistlib.loads(raw), raw


def role_entitlements(role: str) -> tuple[Path | None, dict]:
    f = ENTITLEMENTS / f"{role}.plist"
    if not f.is_file():
        return None, {}
    raw = f.read_bytes()
    if raw.startswith(b"bplist00"):
        raise SignError(f"{f} is a binary plist; entitlements must be XML (notarization rejects bplist00)")
    data = plistlib.loads(raw)
    if not isinstance(data, dict) or any(v is not True for v in data.values()):
        raise SignError(f"{f}: entitlements must be a dict of keys set to true")
    if "com.apple.security.get-task-allow" in data:
        raise SignError(f"{f}: get-task-allow is never allowed (notarization rejects it)")
    return f, data


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bundle_files(app: Path) -> dict:
    """Every entry in the bundle: files with sha256, symlinks with their target (what seal_survives_use compares)."""
    out = {}
    for p in sorted(app.rglob("*")):
        r = rel_of(app, p)
        if p.is_symlink():
            out[r] = {"symlink": os.readlink(p)}
        elif p.is_file():
            out[r] = {"sha256": sha256(p), "mode": oct(p.stat().st_mode & 0o777)}
    return out


# ------------------------------------------------------------------------------------------------ oracle

def oracle_files(payload: Path) -> dict:
    """At `payload`: Oracle MySQL's Mach-O files with their Developer ID signature, recorded so `sign` leaves them
    alone and checks can prove they are unchanged. Fails if one isn't validly Oracle-signed."""
    rec = {}
    d = payload / ORACLE_DIR
    for p in sorted(d.rglob("*")) if d.is_dir() else []:
        if not is_macho(p):
            continue
        info = display(p)
        v = _run([CODESIGN, "--verify", "--strict", p], check=False)
        rel = str(p.relative_to(payload))
        if v.returncode != 0 or info.get("team") != ORACLE_TEAM or ORACLE_AUTHORITY not in info["authority"]:
            raise SignError(f"Oracle MySQL file {rel} doesn't carry a valid Oracle Developer ID signature: "
                            f"{info.get('authority')} {info.get('team')} {(v.stderr or '')[-200:]}")
        rec[rel] = {"cdhash": info.get("cdhash"), "identifier": info.get("identifier"), "team": info["team"],
                    "authority": ORACLE_AUTHORITY, "flags": info.get("flags"), "timestamp": info.get("timestamp")}
    return rec


# ------------------------------------------------------------------------------------------------ layout

def layout_problems(app: Path) -> list[str]:
    """Code only in allowed places, nothing but Mach-O in code locations, Contents/MacOS holding only the launcher."""
    probs = []
    pl = app / PAYLOAD_REL
    for p in machos(app):
        r = rel_of(app, p)
        prel = payload_rel(app, p)
        if r == "Contents/MacOS/Moonglade":
            continue
        if prel is None or not any(prel == d or prel.startswith(d + "/") for d in CODE_DIRS):
            probs.append(f"Mach-O outside the allowed code places: {r}")
    for loc in CODE_LOCATIONS:
        d = app / loc
        if not d.exists():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_dir() and not p.is_symlink():
                continue
            if not is_macho(p):
                probs.append(f"non-Mach-O in a code location: {rel_of(app, p)}")
    macos = sorted(x.name for x in (app / "Contents/MacOS").iterdir()) if (app / "Contents/MacOS").is_dir() else []
    if macos != ["Moonglade"]:
        probs.append(f"Contents/MacOS must hold only the launcher, has {macos}")
    if not pl.is_dir():
        probs.append(f"no payload at {PAYLOAD_REL}")
    return probs


# ------------------------------------------------------------------------------------------------- signing

def plan(app: Path, oracle: dict) -> list[dict]:
    """Inside-out order: payload libraries, payload executables, the launcher; Oracle's files skipped."""
    libs, exes = [], []
    unknown = []
    for p in machos(app):
        r = rel_of(app, p)
        prel = payload_rel(app, p)
        if prel is not None and prel in oracle:
            continue
        if prel is not None and prel.startswith(ORACLE_DIR + "/"):
            unknown.append(f"{r} (in {ORACLE_DIR}/ but not in the Oracle record)")
            continue
        if r == "Contents/MacOS/Moonglade":
            continue
        ft = macho_filetype(p)
        key = prel if prel is not None else r
        if ft == MH_EXECUTE:
            role = ROLES.get(key)
            if role is None:
                unknown.append(r)
                continue
            exes.append({"path": p, "rel": r, "role": role, "identifier": f"{BUNDLE_ID}.{p.name}"})
        else:
            libs.append({"path": p, "rel": r, "role": "library", "identifier": f"{BUNDLE_ID}.{p.name}"})
    if unknown:
        raise SignError(f"Mach-O files with no signing role (executables need one in sign.ROLES; mysql/ holds only "
                        f"Oracle's recorded files): {unknown}")
    gone = [k for k in oracle if not (app / PAYLOAD_REL / k).is_file()]
    if gone:
        raise SignError(f"recorded Oracle files missing from the app: {gone}")
    launcher = app / "Contents/MacOS/Moonglade"
    if not is_macho(launcher):
        raise SignError(f"no launcher at {launcher}")
    return libs + exes + [{"path": launcher, "rel": "Contents/MacOS/Moonglade", "role": "launcher",
                           "identifier": BUNDLE_ID}]


def codesign_args(identity: str, timestamp: bool) -> list[str]:
    return [CODESIGN, "--force", "--sign", identity, "--options", "runtime",
            "--timestamp" if timestamp else "--timestamp=none"]


def sign_app(app: Path, mode: str, oracle: dict, env: EnvReader) -> dict:
    if mode == "ad-hoc":
        identity, timestamp, requirement = "-", False, ADHOC_REQUIREMENT
    else:
        identity, timestamp, requirement = env.get("MOONGLADE_DEVID_IDENTITY"), True, None
    probs = layout_problems(app)
    if probs:
        raise SignError(f"the app's layout isn't signable: {probs}")
    steps = plan(app, oracle)
    ent_files: dict[str, str | None] = {}
    for s in steps:
        cmd = codesign_args(identity, timestamp) + ["--identifier", s["identifier"]]
        if s["role"] != "library":
            f, data = role_entitlements(s["role"])
            ent_files[s["role"]] = str(f.relative_to(ROOT)) if f else None
            if f:
                cmd += ["--entitlements", f]
        _run(cmd + [s["path"]])
    # the bundle (re-signs the main executable as part of it, with the launcher's entitlements)
    f, _ = role_entitlements("launcher")
    cmd = codesign_args(identity, timestamp) + ["--identifier", BUNDLE_ID]
    if f:
        cmd += ["--entitlements", f]
    if requirement:
        cmd += [f"-r={requirement}"]
    _run(cmd + [app])
    return {"steps": steps, "entitlement_files": ent_files, "requirement": requirement}


def verify_app(app: Path, mode: str, oracle: dict, signed: dict, env: EnvReader) -> dict:
    """Per-file and bundle verification of what sign_app did; any problem raises."""
    probs: list[str] = []
    files = []
    team = env.get("MOONGLADE_TEAM_ID") if mode == "developer-id" else None
    for s in signed["steps"]:
        p = s["path"]
        v = _run([CODESIGN, "--verify", "--strict", p], check=False)
        info = display(p)
        ents, raw = entitlements_of(p)
        if s["role"] == "library":
            want = {}
        else:
            want = role_entitlements(s["role"])[1]
        if v.returncode != 0:
            probs.append(f"{s['rel']}: codesign --verify --strict failed: {(v.stderr or '').strip()[-200:]}")
        if "runtime" not in (info.get("flags") or "") and "0x10000" not in (info.get("flags") or ""):
            probs.append(f"{s['rel']}: no hardened runtime flag ({info.get('flags')})")
        if ents != want:
            probs.append(f"{s['rel']}: entitlements {sorted(ents)} != role {s['role']}'s {sorted(want)}")
        if raw.startswith(b"bplist00"):
            probs.append(f"{s['rel']}: entitlements shown as a binary plist")
        if mode == "ad-hoc":
            if info.get("signature") != "adhoc":
                probs.append(f"{s['rel']}: not ad hoc ({info.get('signature')})")
        else:
            if not any(a.startswith("Developer ID Application:") for a in info["authority"]) or info.get("team") != team:
                probs.append(f"{s['rel']}: not signed by the team's Developer ID ({info['authority'][:1]}, {info.get('team')})")
            if not info.get("timestamp"):
                probs.append(f"{s['rel']}: no secure timestamp")
        if info.get("identifier") != s["identifier"]:
            probs.append(f"{s['rel']}: identifier {info.get('identifier')} != {s['identifier']}")
        files.append({"path": s["rel"], "role": s["role"], "identifier": info.get("identifier"),
                      "cdhash": info.get("cdhash"), "flags": info.get("flags"), "signature": info.get("signature"),
                      "authority": info["authority"][:1], "team": info.get("team"), "timestamp": info.get("timestamp"),
                      "entitlements": sorted(ents),
                      "entitlements_sha256": hashlib.sha256(raw).hexdigest() if raw else None})
    oracle_now = {}
    for rel, want in oracle.items():
        p = app / PAYLOAD_REL / rel
        info = display(p)
        v = _run([CODESIGN, "--verify", "--strict", p], check=False)
        oracle_now[rel] = {"cdhash": info.get("cdhash"), "team": info.get("team"),
                           "authority": info["authority"][:1], "verify_rc": v.returncode}
        if info.get("cdhash") != want["cdhash"] or v.returncode != 0 or info.get("team") != ORACLE_TEAM:
            probs.append(f"Oracle file {rel} changed: cdhash {info.get('cdhash')} (recorded {want['cdhash']}), "
                         f"team {info.get('team')}, verify rc {v.returncode}")
    deep = _run([CODESIGN, "--verify", "--deep", "--strict", "--verbose=2", app], check=False)
    binfo = display(app)
    if deep.returncode != 0:
        probs.append(f"bundle: codesign --verify --deep --strict failed: {(deep.stderr or '').strip()[-400:]}")
    if binfo.get("identifier") != BUNDLE_ID:
        probs.append(f"bundle identifier {binfo.get('identifier')} != {BUNDLE_ID}")
    if mode == "ad-hoc" and binfo.get("designated") != ADHOC_REQUIREMENT:
        probs.append(f"bundle designated requirement {binfo.get('designated')!r} != {ADHOC_REQUIREMENT!r}")
    if mode == "developer-id" and (not binfo.get("designated") or "cdhash" in (binfo.get("designated") or "")):
        probs.append(f"bundle designated requirement {binfo.get('designated')!r} isn't the Developer ID default")
    if probs:
        raise SignError(f"signature verification failed ({len(probs)}): {probs[:10]}")
    return {"files": files, "oracle": {"recorded": oracle, "now": oracle_now},
            "bundle": {"identifier": binfo.get("identifier"), "cdhash": binfo.get("cdhash"),
                       "flags": binfo.get("flags"), "signature": binfo.get("signature"),
                       "authority": binfo["authority"][:1], "team": binfo.get("team"),
                       "designated": binfo.get("designated"),
                       "verify_deep_strict": (deep.stderr or "").strip()[-400:]}}


def sign(app: Path, build: Path, config: Path, oracle_record: Path, env: EnvReader | None = None) -> dict:
    """The `sign` step. Order: mode, (developer-id) credentials then CI-only, then sign inside out and verify."""
    env = env or EnvReader()
    mode = read_mode(config)
    rec: dict = {"mode": mode, "config": str(config), "app": str(app)}
    if mode == "developer-id":
        missing = missing_credentials(env)
        if missing:
            raise SignError("developer-id signing needs these credentials, missing from the environment: "
                            + ", ".join(missing) + f" (mode set in {config})")
        if env.get("GITHUB_ACTIONS") != "true":
            raise SignError("developer-id signing runs only in the release workflow (GITHUB_ACTIONS=true); "
                            "local builds use ad-hoc")
    if not (app / "Contents/Info.plist").is_file():
        raise SignError(f"no app at {app}; run the payload step first")
    (build / "sign.json").unlink(missing_ok=True)        # only a signing that verified leaves a record
    odir = app / PAYLOAD_REL / ORACLE_DIR
    version = app / PAYLOAD_REL / "VERSION"
    if oracle_record.is_file() and version.is_file() and oracle_record.stat().st_mtime < version.stat().st_mtime:
        raise SignError(f"the Oracle signature record {oracle_record} is older than the payload; run payload again")
    if oracle_record.is_file():
        oracle = json.loads(oracle_record.read_text())
    elif odir.is_dir() and any(is_macho(p) for p in odir.rglob("*")):
        raise SignError(f"no Oracle signature record {oracle_record}; run the payload step first")
    else:
        oracle = {}
    build.mkdir(parents=True, exist_ok=True)
    signed = sign_app(app, mode, oracle, env)
    ver = verify_app(app, mode, oracle, signed, env)
    manifest = build / "bundle-files.json"
    manifest.write_text(json.dumps(bundle_files(app), indent=1, sort_keys=True) + "\n")
    rec.update({
        "identity": "adhoc" if mode == "ad-hoc" else "developer-id",
        "bundle_id": BUNDLE_ID,
        "designated_requirement_set": signed["requirement"],
        "entitlement_files": signed["entitlement_files"],
        "signed": ver["files"],
        "oracle": ver["oracle"],
        "bundle": ver["bundle"],
        "bundle_files": str(manifest),
        "env_read": list(env.read),
    })
    (build / "sign.json").write_text(json.dumps(rec, indent=2) + "\n")
    return rec


# ---------------------------------------------------------------------------------------------- notarize

def notarize(app: Path, zip_path: Path, build: Path, config: Path, rezip, env: EnvReader | None = None) -> dict:
    """The `notarize` step: developer-id only (ad-hoc records "not applicable"). `rezip()` rebuilds the zip from the
    stapled app and refreshes SHA256SUMS / SOURCES.json (release.py owns those)."""
    env = env or EnvReader()
    mode = read_mode(config)
    if mode == "ad-hoc":
        return {"mode": mode, "notarize": "not applicable (ad-hoc)", "env_read": list(env.read)}
    missing = missing_credentials(env)
    if missing:
        raise SignError("notarization needs these credentials, missing from the environment: " + ", ".join(missing))
    if env.get("GITHUB_ACTIONS") != "true":
        raise SignError("notarization runs only in the release workflow (GITHUB_ACTIONS=true)")
    sj = build / "sign.json"
    if not sj.is_file() or json.loads(sj.read_text()).get("mode") != "developer-id":
        raise SignError("the app wasn't signed in developer-id mode by this run's sign step")
    if not zip_path.is_file():
        raise SignError(f"no zip at {zip_path}; run the assets step first")
    log_path = build / "notary-log.json"
    sub_id = status = None
    keydir = Path(tempfile.mkdtemp(prefix="notary-", dir=env.get("RUNNER_TEMP") or None))
    try:
        key = keydir / "AuthKey.p8"
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(base64.b64decode(env.get("MOONGLADE_NOTARY_KEY_P8_BASE64")))
        auth = ["--key", str(key), "--key-id", env.get("MOONGLADE_NOTARY_KEY_ID"),
                "--issuer", env.get("MOONGLADE_NOTARY_ISSUER_ID")]
        sub = _run(["/usr/bin/xcrun", "notarytool", "submit", zip_path, *auth, "--wait", "--output-format", "json"],
                   check=False)
        try:
            result = json.loads(sub.stdout)
        except ValueError:
            raise SignError(f"notarytool submit gave no JSON (rc {sub.returncode}): {sub.stderr[-600:]}") from None
        sub_id, status = result.get("id"), result.get("status")
        if sub_id:
            _run(["/usr/bin/xcrun", "notarytool", "log", sub_id, *auth, log_path], check=False)
    finally:
        shutil.rmtree(keydir, ignore_errors=True)
    try:
        log = json.loads(log_path.read_text())
    except (OSError, ValueError):
        log = None
    issues = (log or {}).get("issues")
    rec = {"mode": mode, "submission": sub_id, "status": status, "log": str(log_path),
           "log_status": (log or {}).get("status"), "issues": issues}
    if status != "Accepted" or log is None or issues:
        raise SignError(f"notarization not accepted cleanly: status {status}, issues {issues}, log {log_path}")
    _run(["/usr/bin/xcrun", "stapler", "staple", app])
    v = _run(["/usr/bin/xcrun", "stapler", "validate", app], check=False)
    rec["stapler_validate"] = {"rc": v.returncode, "out": (v.stdout + v.stderr).strip()[-300:]}
    if v.returncode != 0:
        raise SignError(f"stapler validate failed: {rec['stapler_validate']['out']}")
    deep = _run([CODESIGN, "--verify", "--deep", "--strict", app], check=False)
    if deep.returncode != 0:
        raise SignError(f"the stapled app doesn't verify: {deep.stderr[-300:]}")
    rec["rezip"] = rezip()
    rec["env_read"] = list(env.read)
    (build / "notarize.json").write_text(json.dumps(rec, indent=2) + "\n")
    return rec
