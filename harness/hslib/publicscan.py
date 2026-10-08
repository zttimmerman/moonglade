"""public-repo (openspec/changes/go-public): scanners that keep personal data out of the published repo.

Two scans share one pattern set:
- the tracked tree (every entry of a git index, read from its staged blob): built-in patterns (personal and wording)
  plus every line of the local denylist;
- the full history of a repo (every commit and tag object reachable from any ref, every reachable blob and every path
  name): the personal built-in patterns plus the denylist. Wording patterns (claims that the repo is private or never shared) are tree-only: old
  versions of a document may say what was true then.

The denylist (`env/public-denylist.txt`, untracked, 0600) holds one literal string per line, matched
case-insensitively. Its strings are personal by definition, so nothing here ever prints one: a hit names the file,
the line and the pattern label ("denylist #3"), never the matched text. A missing or empty denylist is an error
(DenylistError), never "no hits".

Command line (used by tools/go-public and the runbook):
    PYTHONPATH=harness python3 -m hslib.publicscan tree
    PYTHONPATH=harness python3 -m hslib.publicscan history <repo-or-mirror>
    PYTHONPATH=harness python3 -m hslib.publicscan files <file>...
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

from . import paths as P

DENYLIST = P.ENV / "public-denylist.txt"
REDACTED = b"[redacted]"


class DenylistError(RuntimeError):
    pass


def load_denylist(path: Path | None = None) -> list[str]:
    """The denylist's literal strings (blank lines and lines starting with '#' ignored). Raises DenylistError when the
    file is missing, unreadable or has no entries (fail-closed)."""
    p = Path(path) if path is not None else DENYLIST
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise DenylistError(f"denylist {p} is missing or unreadable ({e.__class__.__name__})") from None
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines:
        raise DenylistError(f"denylist {p} has no entries")
    return lines


# ----------------------------------------------------------------------------- patterns
# Spelled in pieces so this file doesn't match its own patterns.
_USERS = "/" + "Users" + "/"
_SESSION = r"claude\.ai/code/" + "session_"
EMAIL_RX = re.compile(rb"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Addresses that identify nobody: GitHub/Anthropic noreply addresses and the reserved example domains (RFC 2606).
EMAIL_OK = re.compile(rb"(?i)(noreply|no-reply)[.@]|@users\.noreply\.github\.com$|@(.+\.)?example\.(com|org|net)$"
                      rb"|\.(example|test|invalid|localhost)$")


def builtin_patterns() -> list[tuple[str, str, re.Pattern]]:
    """(label, kind, compiled bytes regex). kind "personal" applies to tree and history, "wording" to the tree only."""
    return [
        ("home path", "personal", re.compile(re.escape(_USERS).encode() + rb"(?!Shared/)[A-Za-z0-9._-]+/")),
        ("claude session link", "personal", re.compile(_SESSION.encode())),
        ("private-visibility claim", "wording", re.compile(rb"(?i)private[ -]repo")),
        ("no-redistribution claim", "wording", re.compile(rb"(?i)never[ -]+redistribut")),
    ]


def kind_of(label: str) -> str:
    return next((k for lab, k, _ in builtin_patterns() if lab == label), "personal")


# Tracked paths where a *wording* pattern may appear, with the reason. Personal patterns and the denylist have no
# allowlist.
WORDING_ALLOW = (
    ("openspec/changes/go-public/*", "this change states the rule, so it names the forbidden wording"),
    ("openspec/changes/archive/*-go-public/*", "the same change after archiving"),
    ("openspec/specs/public-repo/spec.md", "the rule itself after archiving"),
)


def _wording_allowed(rel: str) -> str | None:
    for glob, why in WORDING_ALLOW:
        if fnmatch.fnmatchcase(rel, glob):
            return why
    return None


def denylist_patterns(entries: list[str]) -> list[tuple[str, str, re.Pattern]]:
    return [(f"denylist #{i}", "personal", re.compile(re.escape(e.encode()), re.I)) for i, e in enumerate(entries, 1)]


def find_hits(data: bytes, pats, *, wording: bool = True) -> list[tuple[str, int]]:
    """[(label, line number)] for every pattern hit in `data` (emails other than EMAIL_OK addresses included).
    Never returns the matched text."""
    out = []
    for label, kind, rx in pats:
        if kind == "wording" and not wording:
            continue
        for m in rx.finditer(data):
            out.append((label, data.count(b"\n", 0, m.start()) + 1))
    for m in EMAIL_RX.finditer(data):
        if not EMAIL_OK.search(m.group(0)):
            out.append(("email address", data.count(b"\n", 0, m.start()) + 1))
    return out


# ----------------------------------------------------------------------------- git plumbing

def _git(root: Path, *args, env: dict | None = None, inp: bytes | None = None) -> bytes:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, input=inp,
                       env={**os.environ, **(env or {})})
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {r.stderr.decode(errors='replace')[-300:]}")
    return r.stdout


def _cat_batch(root: Path, shas: list[str], env: dict | None = None):
    """Yield (sha, type, bytes) for each object via one `git cat-file --batch`."""
    if not shas:
        return
    p = subprocess.Popen(["git", "-C", str(root), "cat-file", "--batch"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, env={**os.environ, **(env or {})})
    import threading
    t = threading.Thread(target=lambda: (p.stdin.write(("\n".join(shas) + "\n").encode()), p.stdin.close()))
    t.start()
    try:
        for _ in shas:
            head = p.stdout.readline().split()
            if len(head) != 3:
                raise RuntimeError(f"git cat-file --batch: unexpected header {head!r}")
            sha, typ, size = head[0].decode(), head[1].decode(), int(head[2])
            data = p.stdout.read(size)
            p.stdout.read(1)
            yield sha, typ, data
    finally:
        t.join()
        p.stdout.close()
        if p.wait() != 0:
            raise RuntimeError("git cat-file --batch failed")


# ----------------------------------------------------------------------------- tree scan

def scan_tree(root: Path | None = None, denylist: Path | None = None, *, env: dict | None = None) -> dict:
    """Scan every entry of the index (`env` may point GIT_INDEX_FILE at a temporary index) and its path: the file's
    working-tree bytes when it exists there, else its staged blob. {"files", "patterns", "hits": [{file, line, pattern}], "allowed": {reason: n}}. Raises DenylistError."""
    root = Path(root or P.ROOT)
    pats = builtin_patterns() + denylist_patterns(load_denylist(denylist))
    entries = []
    for rec in _git(root, "ls-files", "-s", "-z", env=env).split(b"\0"):
        if not rec:
            continue
        meta, path = rec.split(b"\t", 1)
        mode, sha, _stage = meta.split()
        if mode == b"160000":             # a submodule: no blob here
            continue
        entries.append((path.decode(), sha.decode()))
    hits, allowed = [], {}
    by_sha: dict[str, list[str]] = {}
    for rel, sha in entries:
        by_sha.setdefault(sha, []).append(rel)
        for label, _ in find_hits(rel.encode(), pats):
            if kind_of(label) == "wording" and _wording_allowed(rel):
                continue
            hits.append({"file": rel, "line": 0, "pattern": label + " (in the path)"})
    blobs = {sha: data for sha, _typ, data in _cat_batch(root, list(by_sha), env)}
    for sha, rels in by_sha.items():
        for rel in rels:
            # the working-tree file when there is one (what a run on this checkout sees, edits included), else the
            # staged blob (a file only in the index, e.g. the self-test's plant in a temporary index)
            p = root / rel
            data = p.read_bytes() if p.is_file() and not p.is_symlink() else blobs[sha]
            for label, line in find_hits(data, pats):
                if kind_of(label) == "wording":
                    why = _wording_allowed(rel)
                    if why:
                        allowed[why] = allowed.get(why, 0) + 1
                        continue
                hits.append({"file": rel, "line": line, "pattern": label})
    hits.sort(key=lambda h: (h["file"], h["line"], h["pattern"]))
    return {"files": len(entries), "patterns": len(pats) + 1, "denylist_entries": len(pats) - len(builtin_patterns()),
            "hits": hits, "allowed": allowed}


# ----------------------------------------------------------------------------- history scan

def scan_history(repo: Path, denylist: Path | None = None) -> dict:
    """Scan everything reachable from every ref of `repo` (a checkout or a bare mirror): each commit and tag object
    whole (message, author, committer), each blob, and each path name. Personal patterns and the denylist only.
    {"refs", "commits", "tags", "blobs", "paths", "hits": [{object, kind, pattern, line, path?}], "hit_count",
    "by_pattern", "by_kind"}. Raises DenylistError."""
    repo = Path(repo)
    pats = builtin_patterns() + denylist_patterns(load_denylist(denylist))
    refs = [ln for ln in _git(repo, "for-each-ref", "--format=%(refname)").decode().splitlines() if ln]
    objs = _git(repo, "rev-list", "--all", "--objects").decode().splitlines()
    first_path: dict[str, str] = {}
    shas, paths = [], set()
    for ln in objs:
        sha, _, path = ln.partition(" ")
        shas.append(sha)
        if path:
            first_path.setdefault(sha, path)
            paths.add(path)
    # annotated tags aren't listed by rev-list --objects as tag objects; add them
    tag_shas = [ln.split()[0] for ln in _git(repo, "for-each-ref", "--format=%(objectname) %(objecttype)")
                .decode().splitlines() if ln.endswith(" tag")]
    hits = []
    counts = {"commit": 0, "tag": 0, "blob": 0, "tree": 0}
    for sha, typ, data in _cat_batch(repo, sorted(set(shas) | set(tag_shas))):
        counts[typ] = counts.get(typ, 0) + 1
        if typ == "tree":
            continue                       # names are scanned below as full paths
        for label, line in find_hits(data, pats, wording=False):
            h = {"object": sha, "kind": typ, "pattern": label, "line": line}
            if typ == "blob":
                h["path"] = first_path.get(sha, "?")
            hits.append(h)
    for path in sorted(paths):
        for label, _ in find_hits(path.encode(), pats, wording=False):
            hits.append({"object": None, "kind": "path", "pattern": label, "line": 0, "path": path})
    by_pattern, by_kind = {}, {}
    for h in hits:
        by_pattern[h["pattern"]] = by_pattern.get(h["pattern"], 0) + 1
        by_kind[h["kind"]] = by_kind.get(h["kind"], 0) + 1
    return {"refs": len(refs), "commits": counts["commit"], "tags": counts["tag"], "blobs": counts["blob"],
            "paths": len(paths), "patterns": len(pats) + 1, "hit_count": len(hits), "hits": hits[:200],
            "by_pattern": by_pattern, "by_kind": by_kind}


def scan_files(files: list[Path], denylist: Path | None = None) -> dict:
    """Personal patterns + denylist over loose files (e.g. saved PR bodies). {"files", "hits": [{file, line,
    pattern}]}. Raises DenylistError."""
    pats = builtin_patterns() + denylist_patterns(load_denylist(denylist))
    hits = [{"file": str(f), "line": line, "pattern": label}
            for f in files for label, line in find_hits(Path(f).read_bytes(), pats, wording=False)]
    return {"files": len(files), "patterns": len(pats) + 1, "hits": hits}


def main(argv: list[str]) -> int:
    import json
    if len(argv) >= 1 and argv[0] == "tree":
        res = scan_tree()
    elif len(argv) == 2 and argv[0] == "history":
        res = scan_history(Path(argv[1]))
        res["hits"] = res["hits"][:50]
    elif len(argv) >= 2 and argv[0] == "files":
        res = scan_files([Path(a) for a in argv[1:]])
    else:
        print(__doc__, file=sys.stderr)
        return 2
    print(json.dumps(res, indent=1))
    n = len(res["hits"]) if "hit_count" not in res else res["hit_count"]
    print(f"{n} hit(s)", file=sys.stderr)
    return 0 if n == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except DenylistError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(3)
