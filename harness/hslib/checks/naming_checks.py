"""harness-reporting: the old project name stays retired (openspec/changes/rename-moonglade).

repo.naming scans every tracked file (its working-tree bytes, and its path) case-insensitively for the old project
name and fails on any hit outside ALLOW_PATHS / ALLOW_LINES. It also proves on this run that it can fail: it plants
the word in scratch copies of tracked files and requires the scanner to report exactly those plants.

(repo.rename_script, which tested the one-off tools/rename-folder.sh, was retired with that script by go-public: the
folder rename is done, and the script held the dev Mac's own paths.)

This file is the guard itself, so it's the one harness file allowed to spell the old name (OLD_NAME); other checks
that need it (play.stage_lines' baseline mapping, app.bundle_valid's launcher-text scan) import it from here.
"""
from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import paths as P
from ..registry import check, require

CAP = "harness-reporting"
OLD_NAME = "Hearthstone"
NEW_NAME = "Moonglade"
_WORD = re.compile(OLD_NAME.encode(), re.I)

# Whole paths (fnmatch globs on the repo-relative path) where the old name may appear, with the reason.
ALLOW_PATHS = (
    ("openspec/changes/archive/*", "history: archived changes stay as written"),
    ("docs/research/*", "history: research notes stay as written"),
    ("docs/adr/*.md", "history: ADR bodies stay as written (each carries a one-line rename note)"),
    ("openspec/changes/rename-moonglade/*", "this change describes the rename"),
    ("harness/hslib/checks/naming_checks.py", "the guard itself"),
)
# Blizzard's own game data and strings (the in-game item "Hearthstone"): none is tracked today (client data, MPQs
# and DBCs live in client/ and env/, never in git). A tracked file that holds such data gets its exact path here,
# with its source, never a directory glob.
BLIZZARD_CONTENT: tuple = ()
# Single lines (exact repo-relative path + a regex the whole line must match) where the old name may appear.
ALLOW_LINES = (
    # go-public: tools/rename-folder.sh and its three allowed lines are gone (the script was deleted).
    # F29 (seamless-experience): the three build-input lines the rename left (two mod-era-profile comments, patch
    # 0002's title) are renamed; their entries are gone.
    # The guard's own requirement and the rename scenario must name the old word to state the rule.
    ("openspec/specs/harness-reporting/spec.md",
     r'Tracked current files SHALL NOT use the old project name "Hearthstone", except in recorded history and in Blizzard\'s own game content\.',
     "the naming guard's own requirement text"),
    ("openspec/specs/harness-reporting/spec.md",
     r'- \*\*WHEN\*\* the naming guard scans tracked files case-insensitively for "hearthstone"',
     "the naming guard's own scenario text"),
    ("openspec/specs/mac-app/spec.md",
     r'- \*\*THEN\*\* the launcher\'s and the game process\'s LaunchServices display names are "Moonglade", and no user-visible launcher text says "Hearthstone"',
     "the rename scenario states the forbidden name"),
)


def tracked_files(root: Path) -> list[str]:
    r = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {r.stderr.decode(errors='replace')[-300:]}")
    return [p for p in r.stdout.decode().split("\0") if p]


def _path_allowed(rel: str) -> str | None:
    for pat, why in ALLOW_PATHS:
        if fnmatch.fnmatchcase(rel, pat):
            return why
    if rel in BLIZZARD_CONTENT:
        return "Blizzard game content"
    return None


def _line_allowed(rel: str, line: str) -> str | None:
    for path, rx, why in ALLOW_LINES:
        if rel == path and re.fullmatch(rx, line):
            return why
    return None


def scan(root: Path, files: list[str]) -> dict:
    """{"hits": [...not allowed...], "allowed": {reason: count}, "read_errors": [...]} over `files` under `root`.
    Fail-closed: an unreadable file is reported as a read error (which fails the check), never skipped."""
    hits, allowed, errors = [], {}, []
    for rel in files:
        why = _path_allowed(rel)
        if why:
            p = root / rel
            if _WORD.search(rel.encode()) or (p.is_file() and _WORD.search(p.read_bytes())):
                allowed[why] = allowed.get(why, 0) + 1
            continue
        if _WORD.search(rel.encode()):
            hits.append({"file": rel, "line": 0, "text": "(the path itself)"})
        p = root / rel
        if p.is_symlink() or not p.exists():
            continue                     # a tracked symlink's target is scanned as its own file; deleted = no content
        try:
            data = p.read_bytes()
        except OSError as e:
            errors.append({"file": rel, "error": str(e)})
            continue
        if not _WORD.search(data):
            continue
        for n, raw in enumerate(data.split(b"\n"), 1):
            if _WORD.search(raw):
                line = raw.decode("utf-8", errors="replace").rstrip("\r")
                lw = _line_allowed(rel, line)
                if lw:
                    allowed[lw] = allowed.get(lw, 0) + 1
                else:
                    hits.append({"file": rel, "line": n, "text": line.strip()[:200]})
    return {"hits": hits, "allowed": allowed, "read_errors": errors}


SPEC_LINE_FILE = "openspec/specs/harness-reporting/spec.md"     # a line-allowlisted file (ALLOW_LINES)


def plant_selftest() -> dict:
    """Plant the word in scratch copies and require the scanner to flag exactly the plants that aren't allowed:
    a non-allowlisted tracked file (README.md), a second line in a line-allowlisted file, and a renamed path; and to
    accept one in an allowlisted area (docs/research). The line-allowlisted file is the harness-reporting spec (its
    allowed requirement line must still be accepted next to the plant)."""
    tmp = Path(tempfile.mkdtemp(prefix="hs-naming-")).resolve()
    try:
        plants = {
            "README.md": (P.ROOT / "README.md").read_text() + f"\nSee also {OLD_NAME.lower()} docs.\n",
            SPEC_LINE_FILE: (P.ROOT / SPEC_LINE_FILE).read_text() + f"# {OLD_NAME} again\n",
            f"docs/{OLD_NAME}-notes.txt": "no word inside\n",
            "docs/research/planted.md": f"{OLD_NAME.upper()} in history is fine\n",
        }
        for rel, text in plants.items():
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / rel).write_text(text)
        res = scan(tmp, list(plants))
        flagged = sorted({h["file"] for h in res["hits"]})
        want = sorted(["README.md", SPEC_LINE_FILE, f"docs/{OLD_NAME}-notes.txt"])
        ok = flagged == want and "history: research notes stay as written" in res["allowed"] \
            and any(h["file"] == SPEC_LINE_FILE and OLD_NAME in h["text"] for h in res["hits"]) \
            and "the naming guard's own requirement text" in res["allowed"]
        return {"dir": str(tmp), "flagged": flagged, "expected_flagged": want, "allowed": res["allowed"],
                "hits": res["hits"], "ok": ok}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("repo.naming", order=4, capability=CAP, stack="any", tier="quick")
def naming(ctx):
    """Every case-insensitive hit of the old project name in tracked files (content and path) is inside the
    documented allowlist (archived changes, research docs, ADR bodies, Blizzard game content, this change, the guard
    itself, and named single lines); planting the word in any other tracked file makes the scanner fail."""
    files = tracked_files(P.ROOT)
    res = scan(P.ROOT, files)
    ev = {"tracked_files": len(files), "hits": res["hits"][:50], "hit_count": len(res["hits"]),
          "allowed_hits_by_reason": res["allowed"], "read_errors": res["read_errors"],
          "allow_paths": [p for p, _ in ALLOW_PATHS], "blizzard_content": list(BLIZZARD_CONTENT),
          "allow_lines": [[p, rx] for p, rx, _ in ALLOW_LINES]}
    require(len(files) > 100, "git ls-files returned too few files to trust the scan", **ev)
    require(not res["read_errors"], "some tracked files couldn't be read (fail-closed)", **ev)
    ev["selftest"] = plant_selftest()
    require(ev["selftest"]["ok"], "self-test: the scanner didn't flag exactly the planted, non-allowed hits", **ev)
    require(not res["hits"], f"the old project name appears outside the allowlist ({len(res['hits'])} hit(s))", **ev)
    return ev
