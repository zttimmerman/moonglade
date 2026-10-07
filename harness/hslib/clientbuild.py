"""`hs client-build`: the derived, revertable play client (ADR-0009; openspec/changes/in-client-login, play-client).

    env/client-play/ = APFS clone of client/ (`cp -cR`, copy-on-write: unchanged files share storage)
                       + the queue patches/client/NNNN-*.json, applied in order:
        kind "binary": file, input/output SHA-256, edits [{offset, original, replacement}]. Refused unless the input
                       hash AND every original byte match exactly (a wrong client or an already-patched exe stops the
                       build with an error naming the mismatch).
        kind "mpq":    a patch MPQ built from repo sources (client-mods/...) by mpq.write_archive (the writer behind
                       `hs mpq write`); a "toc" entry is derived from the base client's own effective file at build time.
        kind "files":  loose add-on files copied from repo sources (seamless-experience: the Moonglade wording add-on,
                       Interface/AddOns/<name>/). Only under Interface/AddOns/ (never FrameXML or GlueXML), only new
                       paths (a file the base client has is refused), each output hashed into the stamp.

Rules
- client/ is never written. It is verified against env/client-manifest.json (sizes + .exe/.dll hashes) before and
  after every build; the clone is made into a temp dir next to the destination and only renamed into place when every
  patch applied, so a refused build writes nothing.
- Deterministic: the build stamp (<dest>/hs-build.json) records the base manifest hash, each patch record's and
  source's hash, and every output hash. A rebuild with identical inputs (and intact outputs) is a no-op.
- Player state survives rebuilds: WTF/, Screenshots/, Cache/, Logs/ and Errors/ of an existing play client are moved
  into the new tree. The first build carries client/WTF over (a copy; never the other way).
- Revert: `hs client-build --skip 0001` (or deleting env/client-play/, or the launcher setting client=stock).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import mpq, paths as P
from .clientinst import load_manifest, mismatches, sha256_file

QUEUE = P.ROOT / "patches" / "client"
PLAY = P.ENV / "client-play"
STAMP = "hs-build.json"
SCHEMA = 1
STATE_DIRS = ("WTF", "Screenshots", "Cache", "Logs", "Errors")   # the player's / the game's own state


class ClientBuildError(RuntimeError):
    def __init__(self, msg: str, evidence: dict | None = None):
        super().__init__(msg)
        self.evidence = evidence or {}


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def queue(queue_dir: Path = QUEUE, skip: tuple[str, ...] = ()) -> list[dict]:
    """The patch records in order (NNNN-*.json), minus `skip` (ids like "0001")."""
    out = []
    for f in sorted(queue_dir.glob("[0-9][0-9][0-9][0-9]-*.json")):
        rec = json.loads(f.read_text())
        rec["_path"] = str(f)
        rec["_sha256"] = _sha_bytes(f.read_bytes())
        if rec["id"] != f.name[:4]:
            raise ClientBuildError(f"{f.name}: id {rec['id']!r} doesn't match the file name")
        if rec["id"] not in skip:
            out.append(rec)
    return out


def locale(client: Path) -> str:
    locs = sorted(d.name for d in (client / "Data").iterdir() if d.is_dir() and len(d.name) == 4 and d.name[2:].isupper())
    if not locs:
        raise ClientBuildError(f"no locale folder under {client}/Data")
    return locs[0]


# ---------------------------------------------------------------- binary patches

def apply_binary(rec: dict, target: Path) -> dict:
    """Apply one binary record to `target` (a file in the build tree) after checking input hash and original bytes.
    Raises ClientBuildError naming the mismatch; never writes on a mismatch."""
    have = sha256_file(target)
    ev = {"patch": rec["id"], "file": rec["file"], "input_expected": rec["input_sha256"], "input_actual": have}
    if have != rec["input_sha256"]:
        if have == rec["output_sha256"]:
            raise ClientBuildError(f"patch {rec['id']}: {rec['file']} is already patched (its SHA-256 equals the "
                                   f"patch's output hash {have}); refusing to apply twice", ev)
        raise ClientBuildError(f"patch {rec['id']}: {rec['file']} SHA-256 {have} doesn't match the patch's input hash "
                               f"{rec['input_sha256']} (wrong base client); refusing", ev)
    data = bytearray(target.read_bytes())
    for e in rec["edits"]:
        off, orig, new = int(e["offset"], 16), bytes.fromhex(e["original"]), bytes.fromhex(e["replacement"])
        if len(orig) != len(new):
            raise ClientBuildError(f"patch {rec['id']}: edit at {e['offset']} changes length", ev)
        got = bytes(data[off:off + len(orig)])
        if got != orig:
            raise ClientBuildError(f"patch {rec['id']}: original bytes at {e['offset']} are {got.hex()}, the record "
                                   f"expects {orig.hex()}; refusing", ev | {"offset": e["offset"]})
    for e in rec["edits"]:
        off, new = int(e["offset"], 16), bytes.fromhex(e["replacement"])
        data[off:off + len(new)] = new
    out = _sha_bytes(bytes(data))
    if out != rec["output_sha256"]:
        raise ClientBuildError(f"patch {rec['id']}: result SHA-256 {out} isn't the recorded output "
                               f"{rec['output_sha256']}; refusing", ev | {"output_actual": out})
    # replace via a new file: the clone shares blocks with client/, and a new inode never touches them
    tmp = target.with_name(target.name + ".hs-new")
    tmp.write_bytes(bytes(data))
    os.chmod(tmp, target.stat().st_mode & 0o777)
    os.replace(tmp, target)
    return ev | {"output_sha256": out, "edits": len(rec["edits"])}


# ---------------------------------------------------------------- data (MPQ) patches

def _archive_priority(client: Path, loc: str) -> list[Path]:
    """Base-client archives that can hold interface files, highest priority first (patch-<loc>-N > patch-<loc> >
    locale-<loc>; the order the login-screen spike verified by recomputing the GlueXML digest)."""
    d = client / "Data" / loc
    numbered = sorted(d.glob(f"patch-{loc}-?.MPQ"), key=lambda p: p.name.lower(), reverse=True)
    return numbered + [p for p in (d / f"patch-{loc}.MPQ", d / f"locale-{loc}.MPQ") if p.exists()]


def effective_file(client: Path, inside: str) -> tuple[bytes, str]:
    loc = locale(client)
    for arc_path in _archive_priority(client, loc):
        with mpq.Archive(arc_path) as arc:
            try:
                return arc.read(inside), str(arc_path.relative_to(client))
            except mpq.MPQError:
                continue
    raise ClientBuildError(f"{inside} not found in the base client's archives")


def _derived_toc(base: Path, spec: dict) -> tuple[bytes, dict]:
    data, src = effective_file(base, spec["inside"])
    lines = data.decode("latin-1").splitlines()
    eol = "\r\n" if b"\r\n" in data else "\n"
    add = list(spec["add"])
    idx = next((i for i, l in enumerate(lines) if l.strip() == spec["insert_before"]), len(lines))
    new = lines[:idx] + add + lines[idx:]
    out = (eol.join(new) + eol).encode("latin-1")
    return out, {"from": src, "base_sha256": _sha_bytes(data), "inserted": add, "at_line": idx + 1}


def mpq_files(rec: dict, base: Path) -> tuple[dict, dict]:
    """{inside: bytes} for a data record, plus provenance evidence."""
    files, prov = {}, {}
    for inside, src in rec["files"].items():
        b = (P.ROOT / src).read_bytes()
        files[inside] = b
        prov[inside] = {"source": src, "sha256": _sha_bytes(b)}
    if rec.get("toc"):
        b, info = _derived_toc(base, rec["toc"])
        files[rec["toc"]["inside"]] = b
        prov[rec["toc"]["inside"]] = info | {"sha256": _sha_bytes(b)}
    return files, prov


FILES_ROOT = "Interface/AddOns/"           # the only place a "files" record may write (an add-on folder)


def _base_archives(base: Path) -> list[Path]:
    return sorted(base.glob("Data/*.MPQ")) + sorted(base.glob("Data/*/*.MPQ"))


def loose_files(rec: dict, base: Path, shadow_check: bool = False) -> dict:
    """{dest rel path: (source Path, sha256)} for a "files" record. Refuses: a destination outside an add-on folder
    under Interface/AddOns/, any FrameXML/GlueXML path, a folder named like Blizzard's own add-ons (Blizzard_*) or one
    the base client already has loose, a path the base client already has, and a source outside the repo (resolved,
    so a symlink or an absolute path can't escape). With shadow_check (the actual build, not the input hashing): also a
    destination that any of the base client's archives holds (a loose file would shadow it)."""
    out = {}
    root = P.ROOT.resolve()
    for dest, src in rec["files"].items():
        rel = dest.replace("\\", "/")
        parts = rel.split("/")
        if (not rel.startswith(FILES_ROOT) or len(parts) < 4 or ".." in parts or "" in parts
                or any(x.lower() in ("framexml", "gluexml") for x in parts)):
            raise ClientBuildError(f"patch {rec['id']}: {dest} is not inside an add-on folder under {FILES_ROOT}")
        folder = parts[2]
        if folder.lower().startswith("blizzard_") or (base / FILES_ROOT / folder).exists():
            raise ClientBuildError(f"patch {rec['id']}: {dest} is in add-on folder {folder}, which is Blizzard's or "
                                   "already in the base client; refusing")
        if (base / rel).exists():
            raise ClientBuildError(f"patch {rec['id']}: {rel} already exists in the base client; refusing to overwrite it")
        srcp = (P.ROOT / src).resolve()
        if root not in srcp.parents or not srcp.is_file():
            raise ClientBuildError(f"patch {rec['id']}: source {src} is not a file inside the repo; refusing")
        out[rel] = (srcp, _sha_bytes(srcp.read_bytes()))
    if shadow_check:
        for arc_path in _base_archives(base):
            with mpq.Archive(arc_path) as arc:
                for rel in out:
                    if arc.find(rel.replace("/", "\\")) is not None:
                        raise ClientBuildError(f"patch {rec['id']}: {rel} is in the base client's "
                                               f"{arc_path.relative_to(base)}; a loose copy would shadow it; refusing")
    return out


def archive_rel(rec: dict, loc: str) -> str:
    return rec["archive"].replace("{locale}", loc)


# ---------------------------------------------------------------- inputs, stamp

def inputs(base: Path, recs: list[dict]) -> dict:
    """Everything the output depends on (compared with the stamp for the no-op rebuild)."""
    man_path = P.CLIENT_MANIFEST
    out = {"schema": SCHEMA, "base": str(base.resolve()),
           "base_manifest_sha256": _sha_bytes(man_path.read_bytes()) if man_path.exists() else None,
           "patches": []}
    for r in recs:
        item = {"id": r["id"], "kind": r["kind"], "record_sha256": r["_sha256"]}
        if r["kind"] == "binary":
            item["base_file_sha256"] = sha256_file(base / r["file"])
        elif r["kind"] == "files":
            item["sources"] = {k: v[1] for k, v in loose_files(r, base).items()}
        else:
            _, prov = mpq_files(r, base)
            item["sources"] = {k: v["sha256"] for k, v in prov.items()}
        out["patches"].append(item)
    return out


def read_stamp(dest: Path = PLAY) -> dict | None:
    try:
        return json.loads((dest / STAMP).read_text())
    except (FileNotFoundError, ValueError):
        return None


def _outputs_intact(dest: Path, stamp: dict) -> list[dict]:
    bad = []
    for rel, want in (stamp.get("outputs") or {}).items():
        p = dest / rel
        got = sha256_file(p) if p.is_file() else None
        if got != want:
            bad.append({"path": rel, "expected": want, "actual": got})
    return bad


def expected_binaries(dest: Path = PLAY) -> dict:
    """The play client's .exe/.dll hashes: the manifest's, with the build's binary outputs on top."""
    man = load_manifest() or {"binaries_sha256": {}}
    exp = dict(man["binaries_sha256"])
    st = read_stamp(dest) or {}
    for rel, h in (st.get("outputs") or {}).items():
        if rel in exp:
            exp[rel] = h
    return exp


# ---------------------------------------------------------------- build

def _check_base(base: Path, ev: dict, when: str) -> None:
    """client/ must match its manifest (sizes + binary hashes). Scratch bases (checks) skip this."""
    if base.resolve() != P.CLIENT.resolve():
        ev[f"base_manifest_{when}"] = "skipped (scratch base)"
        return
    man = load_manifest()
    if not man:
        raise ClientBuildError("no env/client-manifest.json; run `harness/hs client-install` first", ev)
    mm = mismatches(base, man)
    ev[f"base_manifest_{when}"] = {"mismatches": mm}
    if mm:
        raise ClientBuildError(f"client/ doesn't match env/client-manifest.json {when} the build: {mm[:3]}", ev)


def build(dest: Path = PLAY, base: Path = P.CLIENT, queue_dir: Path = QUEUE, skip: tuple[str, ...] = (),
          force: bool = False) -> dict:
    t0 = time.time()
    base, dest = Path(base), Path(dest)
    if dest.resolve() == base.resolve() or base.resolve() in dest.resolve().parents:
        raise ClientBuildError("the play client can't be inside the base client")
    if dest.resolve() == P.CLIENT.resolve() or P.CLIENT.resolve() in dest.resolve().parents:
        raise ClientBuildError("refusing to build into client/")
    ev: dict = {"dest": str(dest), "base": str(base), "skip": list(skip)}
    _check_base(base, ev, "before")
    recs = queue(queue_dir, skip)
    ev["queue"] = [r["id"] for r in recs]
    want = inputs(base, recs)
    old = read_stamp(dest)
    if not force and old and old.get("inputs") == want and dest.is_dir():
        bad = _outputs_intact(dest, old)
        if not bad:
            _check_base(base, ev, "after")
            return ev | {"built": False, "reason": "up to date (same inputs, outputs intact)", "stamp": old,
                         "seconds": round(time.time() - t0, 1)}
        ev["outputs_changed"] = bad

    tmp = dest.with_name(dest.name + f".build-{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(["cp", "-cR", str(base), str(tmp)], capture_output=True, text=True)
        if r.returncode != 0:
            raise ClientBuildError(f"cp -cR failed: {r.stderr.strip()}", ev)
        for d in STATE_DIRS:                       # the clone's copies of client/ state; replaced below
            shutil.rmtree(tmp / d, ignore_errors=True)
        loc = locale(tmp)
        applied, outputs = [], {}
        for rec in recs:
            if rec["kind"] == "binary":
                a = apply_binary(rec, tmp / rec["file"])
                outputs[rec["file"]] = a["output_sha256"]
            elif rec["kind"] == "mpq":
                rel = archive_rel(rec, loc)
                target = tmp / rel
                if target.exists():
                    raise ClientBuildError(f"patch {rec['id']}: {rel} already exists in the base client; refusing "
                                           "to overwrite a stock archive", ev)
                files, prov = mpq_files(rec, base)
                w = mpq.write_archive(target, files)
                mpq.verify_written(target, files)
                outputs[rel] = sha256_file(target)
                a = {"patch": rec["id"], "archive": rel, "files": prov, "bytes": w["bytes"]}
            elif rec["kind"] == "files":
                written = {}
                for rel, (src, sha) in loose_files(rec, base, shadow_check=True).items():
                    target = tmp / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, target)
                    got = sha256_file(target)
                    if got != sha:
                        raise ClientBuildError(f"patch {rec['id']}: {rel} was written with SHA-256 {got}, expected {sha}", ev)
                    outputs[rel] = written[rel] = got
                a = {"patch": rec["id"], "files": written}
            else:
                raise ClientBuildError(f"patch {rec['id']}: unknown kind {rec['kind']!r}", ev)
            applied.append(a)
        # player state: keep the existing play client's, else carry client/WTF over once (copy)
        state = {}
        for d in STATE_DIRS:
            if (dest / d).exists():
                shutil.move(str(dest / d), str(tmp / d))
                state[d] = "kept from the previous play client"
        if "WTF" not in state and (base / "WTF").is_dir():
            shutil.copytree(base / "WTF", tmp / "WTF", symlinks=True)
            state["WTF"] = f"carried over (copied) from {base / 'WTF'}"
        stamp = {"inputs": want, "outputs": outputs, "applied": applied, "state": state,
                 "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        (tmp / STAMP).write_text(json.dumps(stamp, indent=2) + "\n")
        if dest.exists():
            trash = dest.with_name(dest.name + f".old-{os.getpid()}")
            dest.rename(trash)
            tmp.rename(dest)
            shutil.rmtree(trash, ignore_errors=True)
        else:
            tmp.rename(dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    _check_base(base, ev, "after")
    return ev | {"built": True, "stamp": stamp, "seconds": round(time.time() - t0, 1)}


def cli(dest: Path | None, skip: list[str], force: bool) -> int:
    from .util import log
    try:
        res = build(dest=dest or PLAY, skip=tuple(skip or ()), force=force)
    except ClientBuildError as e:
        log(f"CLIENT-BUILD FAILED: {e}")
        print(json.dumps(e.evidence, indent=2, default=str))
        return 1
    st = res["stamp"]
    print(json.dumps({"dest": res["dest"], "built": res["built"], "reason": res.get("reason"), "queue": res["queue"],
                      "outputs": st["outputs"], "state": st.get("state"), "seconds": res["seconds"]}, indent=2))
    log(("Built " if res["built"] else "Up to date: ") + res["dest"])
    return 0
