#!/usr/bin/env python3
"""Make Mach-O files in a release payload relocatable (openspec/changes/release-build, task 2.1).

    delocate.py fix  --root PAYLOAD --libdir server/lib [--search DIR ...] [--record OUT.json] FILE ...
    delocate.py gate --root PAYLOAD [--allowlist tools/release/allowlist.json] [--json OUT.json]

fix: for each FILE (payload-relative), every non-system dependency (absolute, @loader_path, @executable_path or @rpath,
resolved against the file's own LC_RPATHs and --search) is copied into LIBDIR, its references are rewritten to
@rpath/<name>, each copied library gets the install name @rpath/<name> and the rpath @loader_path, executables under
a bin/ get exactly one rpath, @executable_path/../lib, and every changed file is re-signed ad hoc. Two different
files with one name are refused, except libssl.3/libcrypto.3 (the server's OpenSSL serves Oracle's libmysqlclient
too; OpenSSL 3 keeps its ABI within 3.x). --record writes what was bundled and where it came from (THIRD_PARTY uses
it).

gate: walks every Mach-O under ROOT and fails (exit 1) when a load command, install name or LC_RPATH names
/opt/homebrew, /usr/local or /Users/, when an @rpath/@loader_path/@executable_path dependency doesn't resolve inside
ROOT, or when an embedded string names one of those prefixes and no allowlist entry (file glob + regex + reason)
covers it.

System libraries are /usr/lib/* and /System/*. Uses otool, install_name_tool and codesign from the Command Line
Tools; Python stdlib only.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

FORBIDDEN = ("/opt/homebrew", "/usr/local", "/Users/")
SYSTEM = ("/usr/lib/", "/System/")
MACHO_MAGIC = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
               b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce"}
DEDUP_OK = {"libssl.3.dylib", "libcrypto.3.dylib"}
STRING_RE = re.compile(rb"(?:/opt/homebrew|/usr/local|/Users/)[\x21-\x7e]*")


class DelocateError(RuntimeError):
    pass


def is_macho(p: Path) -> bool:
    try:
        if p.is_symlink() or not p.is_file():
            return False
        with open(p, "rb") as fh:
            return fh.read(4) in MACHO_MAGIC
    except OSError:
        return False


def _run(cmd: list, check=True) -> str:
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise DelocateError(f"{' '.join(map(str, cmd))} failed ({r.returncode}): {(r.stderr or r.stdout).strip()[:400]}")
    return r.stdout


def load_info(p: Path) -> dict:
    """{"id": install name or None, "deps": [...], "rpaths": [...]} from `otool -l` (all architectures)."""
    out = _run(["/usr/bin/otool", "-l", p])
    ident, deps, rpaths = None, [], []
    cmd = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("cmd "):
            cmd = s.split()[1]
        elif s.startswith("name ") and cmd in ("LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB",
                                               "LC_LAZY_LOAD_DYLIB", "LC_LOAD_UPWARD_DYLIB"):
            n = s[5:].rsplit(" (offset", 1)[0]
            if n not in deps:
                deps.append(n)
        elif s.startswith("name ") and cmd == "LC_ID_DYLIB":
            ident = s[5:].rsplit(" (offset", 1)[0]
        elif s.startswith("path ") and cmd == "LC_RPATH":
            r = s[5:].rsplit(" (offset", 1)[0]
            if r not in rpaths:
                rpaths.append(r)
    return {"id": ident, "deps": deps, "rpaths": rpaths}


def is_system(dep: str) -> bool:
    return dep.startswith(SYSTEM)


def _subst(path: str, loader_dir: Path, exe_dir: Path | None) -> str | None:
    if path.startswith("@loader_path"):
        return str(loader_dir) + path[len("@loader_path"):]
    if path.startswith("@executable_path"):
        return None if exe_dir is None else str(exe_dir) + path[len("@executable_path"):]
    return path


def resolve(dep: str, loader_dir: Path, exe_dir: Path | None, rpaths: list[str], search: list[Path]) -> Path | None:
    """The file `dep` names, for an image living in loader_dir; None when it can't be found."""
    cands = []
    if dep.startswith("@rpath/"):
        rest = dep[len("@rpath/"):]
        for r in rpaths:
            base = _subst(r, loader_dir, exe_dir)
            if base:
                cands.append(Path(base) / rest)
        cands += [d / rest for d in search]
    else:
        s = _subst(dep, loader_dir, exe_dir)
        if s:
            cands.append(Path(s))
    for c in cands:
        if c.is_file():
            return c
    return None


# ----------------------------------------------------------------------------------------------------------- fix

def fix(root: Path, libdir_rel: str, files: list[str], search: list[Path]) -> dict:
    root = root.resolve()
    libdir = root / libdir_rel
    libdir.mkdir(parents=True, exist_ok=True)
    bundled: dict[str, dict] = {}            # basename -> {"from": realpath, "refs": [...]}
    dedup: list[dict] = []
    changed: set[Path] = set()
    # (payload file, the directory its original lived in, for @loader_path)
    queue: list[tuple[Path, Path]] = []
    for f in files:
        p = (root / f)
        if not is_macho(p):
            raise DelocateError(f"{f} is not a Mach-O file")
        origin = p.parent
        queue.append((p, origin))
    # libraries already in libdir (e.g. Oracle's libmysqlclient, copied in by the packager): their originals live in
    # the search dirs
    for p in sorted(libdir.iterdir()):
        if is_macho(p) and p.name not in bundled:
            src = next((d / p.name for d in search if (d / p.name).is_file()), p)
            bundled[p.name] = {"from": str(src.resolve()), "refs": [], "preplaced": True}
            queue.append((p, src.parent))
    seen: set[Path] = set()
    exe_dir_for = {}
    while queue:
        p, origin = queue.pop(0)
        if p in seen:
            continue
        seen.add(p)
        info = load_info(p)
        is_lib = info["id"] is not None
        exe_dir = origin if not is_lib else None
        exe_dir_for[p] = exe_dir
        for dep in info["deps"]:
            if is_system(dep):
                continue
            if dep == info["id"]:
                continue
            src = resolve(dep, origin, exe_dir, info["rpaths"], search)
            if src is None:
                raise DelocateError(f"{p.relative_to(root)}: can't find dependency {dep} "
                                    f"(rpaths {info['rpaths']}, search {[str(s) for s in search]})")
            real = src.resolve()
            name = Path(dep).name
            if name in bundled:
                if bundled[name]["from"] != str(real):
                    if name not in DEDUP_OK:
                        raise DelocateError(f"two different {name}: {bundled[name]['from']} and {real} "
                                            f"(needed by {p.relative_to(root)})")
                    dedup.append({"name": name, "kept": bundled[name]["from"], "dropped": str(real),
                                  "for": str(p.relative_to(root))})
            else:
                dst = libdir / name
                shutil.copy2(real, dst)
                os.chmod(dst, 0o755)
                bundled[name] = {"from": str(real), "refs": []}
                queue.append((dst, real.parent))
            bundled[name]["refs"].append(str(p.relative_to(root)))
            new = f"@rpath/{name}"
            if dep != new:
                _run(["/usr/bin/install_name_tool", "-change", dep, new, p])
                changed.add(p)
        # install name + rpaths
        if is_lib:
            want_id = f"@rpath/{p.name}"
            if info["id"] != want_id:
                _run(["/usr/bin/install_name_tool", "-id", want_id, p])
                changed.add(p)
            want_rpaths = ["@loader_path"]
        else:
            want_rpaths = ["@executable_path/../lib"]
        if info["rpaths"] != want_rpaths:
            for r in info["rpaths"]:
                _run(["/usr/bin/install_name_tool", "-delete_rpath", r, p])
            for r in want_rpaths:
                _run(["/usr/bin/install_name_tool", "-add_rpath", r, p])
            changed.add(p)
    signed = []
    for p in sorted(changed | {libdir / n for n in bundled}):
        if p.exists():
            _run(["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none", p])
            _run(["/usr/bin/codesign", "--verify", "--strict", p])
            signed.append(str(p.relative_to(root)))
    return {"libdir": libdir_rel, "bundled": bundled, "dedup": dedup, "signed": signed}


# ---------------------------------------------------------------------------------------------------------- gate

def load_allowlist(path: Path | None) -> list[dict]:
    if not path:
        return []
    doc = json.loads(Path(path).read_text())
    entries = doc["entries"] if isinstance(doc, dict) else doc
    for e in entries:
        if not (e.get("file") and e.get("match") and e.get("reason")):
            raise DelocateError(f"allowlist entry without file/match/reason: {e}")
    return entries


def allowed(rel: str, s: str, allow: list[dict]) -> dict | None:
    for e in allow:
        if fnmatch.fnmatchcase(rel, e["file"]) and re.fullmatch(e["match"], s):
            return e
    return None


def embedded_strings(p: Path) -> list[str]:
    data = p.read_bytes()
    return sorted({m.group(0).decode("ascii", "replace") for m in STRING_RE.finditer(data)})


def _exe_dir_guess(root: Path, p: Path) -> Path | None:
    """Where @executable_path points for an image: its own dir for an executable; for a library in <x>/lib, <x>/bin."""
    if p.parent.name == "lib" and (p.parent.parent / "bin").is_dir():
        return p.parent.parent / "bin"
    if p.parent.name == "bin":
        return p.parent
    return None


def gate(root: Path, allow: list[dict]) -> dict:
    root = root.resolve()
    machos = sorted(p for p in root.rglob("*") if is_macho(p))
    problems, allowed_hits = [], []
    for p in machos:
        rel = str(p.relative_to(root))
        info = load_info(p)
        is_lib = info["id"] is not None
        exe_dir = p.parent if not is_lib else _exe_dir_guess(root, p)
        for kind, items in (("install name", [info["id"]] if info["id"] else []), ("LC_RPATH", info["rpaths"]),
                            ("load command", info["deps"])):
            for it in items:
                if it.startswith(FORBIDDEN):
                    problems.append(f"{rel}: {kind} {it}")
        for dep in info["deps"]:
            if is_system(dep) or dep == info["id"]:
                continue
            if dep.startswith("@"):
                got = resolve(dep, p.parent, exe_dir, info["rpaths"], [])
                if got is None:
                    problems.append(f"{rel}: {dep} doesn't resolve inside the payload")
                elif not str(got.resolve()).startswith(str(root) + os.sep):
                    problems.append(f"{rel}: {dep} resolves outside the payload ({got.resolve()})")
            elif not dep.startswith(FORBIDDEN):
                problems.append(f"{rel}: absolute non-system dependency {dep}")
        for s in embedded_strings(p):
            e = allowed(rel, s, allow)
            if e:
                allowed_hits.append({"file": rel, "string": s, "reason": e["reason"]})
            else:
                problems.append(f"{rel}: embedded path {s}")
    return {"root": str(root), "macho_files": len(machos), "problems": problems, "allowlisted": allowed_hits}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="delocate.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fix")
    f.add_argument("--root", type=Path, required=True)
    f.add_argument("--libdir", required=True)
    f.add_argument("--search", type=Path, action="append", default=[])
    f.add_argument("--record", type=Path)
    f.add_argument("files", nargs="+")
    g = sub.add_parser("gate")
    g.add_argument("--root", type=Path, required=True)
    g.add_argument("--allowlist", type=Path)
    g.add_argument("--json", type=Path)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "fix":
            rec = fix(a.root, a.libdir, a.files, a.search)
            if a.record:
                a.record.write_text(json.dumps(rec, indent=2) + "\n")
            print(f"delocate: bundled {len(rec['bundled'])} libraries into {a.libdir}, re-signed {len(rec['signed'])}")
            return 0
        res = gate(a.root, load_allowlist(a.allowlist))
        if a.json:
            a.json.write_text(json.dumps(res, indent=2) + "\n")
        for pr in res["problems"]:
            print(f"FORBIDDEN: {pr}", file=sys.stderr)
        print(f"delocate gate: {res['macho_files']} Mach-O files, {len(res['problems'])} problems, "
              f"{len(res['allowlisted'])} allowlisted strings")
        return 1 if res["problems"] else 0
    except DelocateError as e:
        print(f"delocate: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
