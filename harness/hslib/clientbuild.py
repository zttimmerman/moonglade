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
  into the new tree. The first build carries client/WTF over (a copy; never the other way). Then the first-start
  settings (clientcfg.FIRST_START_SETTINGS + realmName) are added to WTF/Config.wtf where they aren't set yet.
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

from . import mpq, paths as P, pristine
from .clientinst import load_manifest, mismatches, sha256_file

QUEUE = P.CLIENT_PATCH_QUEUE
PLAY = P.PLAYER_PLAY_CLIENT
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
    pristine.refuse_inside(target, f"apply patch {rec['id']} to", exc=ClientBuildError)
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
        b = (P.APP / src).read_bytes()
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
    root = P.APP.resolve()
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
        srcp = (P.APP / src).resolve()
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


def up_to_date(dest: Path = PLAY, base: Path = P.CLIENT, queue_dir: Path = QUEUE) -> dict:
    """Would `build(dest)` be a no-op? Read-only: {"current": bool, "why": ...} (same comparison build() makes)."""
    old = read_stamp(dest)
    if not old or not dest.is_dir():
        return {"current": False, "why": "no play client build stamp"}
    try:
        want = inputs(base, queue(queue_dir))
    except (OSError, ClientBuildError, ValueError) as e:
        return {"current": False, "why": f"inputs unreadable: {e}"}
    if old.get("inputs") != want:
        return {"current": False, "why": "patch inputs changed since the play client was built"}
    bad = _outputs_intact(dest, old)
    return {"current": not bad, "why": "up to date" if not bad else f"{len(bad)} output(s) changed"}


def fingerprint(dest: Path = PLAY) -> dict | None:
    """A play client's build, fingerprinted (play-launcher "Harness runs never rebuild the player's play client"): the
    folder's inode, and for hs-build.json, Wow.exe and every stamped patch output its SHA-256, inode and mtime. A rebuild
    replaces the folder (new inode) even when the bytes come out the same. None when there is no play client."""
    dest = Path(dest)
    if not dest.is_dir():
        return None
    st = read_stamp(dest) or {}

    def one(rel):
        f = dest / rel
        if not f.is_file():
            return None
        s_ = f.stat()
        return {"sha256": sha256_file(f), "ino": s_.st_ino, "mtime_ns": s_.st_mtime_ns}
    files = {rel: one(rel) for rel in sorted({STAMP, "Wow.exe", *(st.get("outputs") or {})})}
    return {"dir": str(dest), "dir_ino": dest.stat().st_ino, "files": files}


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


def _drop_tmp(tmp: Path) -> None:
    """Remove a staging folder; a symlink is unlinked, never followed (it may point at the pristine client)."""
    if os.path.islink(tmp):
        os.unlink(tmp)
    elif tmp.exists():
        pristine.remove_tree(tmp)


def _check_clone(tmp: Path, src: Path, ev: dict) -> None:
    """The clone must be a real, separate folder: not a symlink, not the base itself, not inside a pristine client."""
    st, bs = os.lstat(tmp), os.stat(src)
    ok = (not os.path.islink(tmp) and tmp.is_dir() and (st.st_dev, st.st_ino) != (bs.st_dev, bs.st_ino)
          and pristine.protected_root_of(tmp, extra=(src,)) is None)
    ev["clone"] = {"path": str(tmp), "is_symlink": os.path.islink(tmp), "ino": st.st_ino, "base_ino": bs.st_ino}
    if not ok:
        raise ClientBuildError(f"the play client clone {tmp} isn't a separate folder (it resolves to the pristine "
                               f"client {src}); refusing to write", ev)


def build(dest: Path = PLAY, base: Path = P.CLIENT, queue_dir: Path = QUEUE, skip: tuple[str, ...] = (),
          force: bool = False, first_start: bool = True) -> dict:
    """Build (or keep, when current) the play client. first_start=False leaves out the first-start settings
    (client.first_start_settings' counterexample only; no entry point passes it)."""
    t0 = time.time()
    base, dest = Path(base), Path(dest)
    tmp = dest.with_name(dest.name + f".build-{os.getpid()}")
    trash = dest.with_name(dest.name + f".old-{os.getpid()}")
    # pristine-client protection (easy-setup): nothing this build writes may be, or lie inside, the pristine client
    # (client/, the harness's test stand-in) or the base it clones, decided by file identity (symlinks, `..`, case)
    # and before anything is written. 2026-10-07: a scratch repo whose client/ was a symlink cloned the symlink
    # itself (`cp -cR <link>`) and patched the real client/Wow.exe through it.
    for what, path in (("build the play client into", dest), ("stage the play client build in", tmp),
                       ("move the old play client to", trash)):
        pristine.refuse_inside(path, what, extra=(base,), exc=ClientBuildError)
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

    _drop_tmp(tmp)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        # the base's real folder (a symlinked base would otherwise be copied as the link itself)
        src = Path(os.path.realpath(base))
        r = subprocess.run(["cp", "-cR", str(src), str(tmp)], capture_output=True, text=True)
        if r.returncode != 0:
            raise ClientBuildError(f"cp -cR failed: {r.stderr.strip()}", ev)
        _check_clone(tmp, src, ev)
        ev["clone_unlocked"] = pristine.make_writable(tmp)     # the clone inherits client/'s lock (pristine.py)
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
                    pristine.refuse_inside(target, "copy an add-on file to", extra=(base,), exc=ClientBuildError)
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
        # first-start settings, only the ones not set yet (clientcfg.FIRST_START_SETTINGS: no intro movie, no
        # EULA/TOS screens before the login, no hardware detection replacing the video settings, the realm chosen)
        if first_start:
            from . import clientcfg
            want_settings, realm = clientcfg.first_start_settings()
            cfg = tmp / "WTF" / "Config.wtf"
            state["first_start_settings"] = clientcfg.add_missing_settings(cfg, want_settings) | {"realm": realm}
        else:
            state["first_start_settings"] = {"skipped": "first_start=False (test counterexample)"}
        stamp = {"inputs": want, "outputs": outputs, "applied": applied, "state": state,
                 "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        (tmp / STAMP).write_text(json.dumps(stamp, indent=2) + "\n")
        if dest.exists():
            dest.rename(trash)
            tmp.rename(dest)
            shutil.rmtree(trash, ignore_errors=True)
        else:
            tmp.rename(dest)
    except BaseException:
        _drop_tmp(tmp)
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
