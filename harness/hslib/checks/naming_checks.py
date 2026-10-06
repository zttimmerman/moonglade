"""harness-reporting: the old project name stays retired (openspec/changes/rename-moonglade).

repo.naming scans every tracked file (its working-tree bytes, and its path) case-insensitively for the old project
name and fails on any hit outside ALLOW_PATHS / ALLOW_LINES. It also proves on this run that it can fail: it plants
the word in scratch copies of tracked files and requires the scanner to report exactly those plants.

repo.rename_script runs tools/rename-folder.sh only against scratch paths in a temp dir (a scratch repo with two
linked worktrees, a scratch Claude Code projects folder), under a sandbox profile that denies and logs every file
write outside that temp dir.

This file is the guard itself, so it's the one harness file allowed to spell the old name (OLD_NAME); other checks
that need it (play.stage_lines' baseline mapping, app.bundle_valid's launcher-text scan) import it from here.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .. import paths as P
from ..registry import check, require
from .play_checks import SANDBOX_EXEC, _denials

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
    ("tools/rename-folder.sh", r'DEFAULT_OLD="\$HOME/Documents/repos/[redacted]"',
     "the folder-rename script's default source path is the old folder"),
    # Build inputs the rename brief kept out of scope ("no server, module, patch ... changes"): comment/title text
    # only, nothing a player sees. Renaming them changes build or client-patch inputs; left for a change that may.
    ("modules/mod-era-profile/conf/mod_era_profile.conf.dist",
     r'# mod-era-profile: Hearthstone "vanilla mode" \(openspec/changes/m3-vanilla-mode, Gate 1 decisions in',
     "module source comment (build input, out of the rename's scope)"),
    ("modules/mod-era-profile/src/EraProfile.cpp",
     r' \* mod-era-profile: Hearthstone "vanilla mode" \(openspec/changes/m3-vanilla-mode\)\.',
     "module source comment (build input, out of the rename's scope)"),
    ("patches/client/0002-glue-front-menu.json",
     r'  "title": "Hearthstone front menu \(GlueXML\): profile list, auto-login with cancel, backdrop picker",',
     "client patch record title (patch input, out of the rename's scope)"),
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


def plant_selftest() -> dict:
    """Plant the word in scratch copies and require the scanner to flag exactly the plants that aren't allowed:
    a non-allowlisted tracked file (README.md), a second line in a line-allowlisted file, and a renamed path; and to
    accept one in an allowlisted area (docs/research)."""
    tmp = Path(tempfile.mkdtemp(prefix="hs-naming-")).resolve()
    try:
        plants = {
            "README.md": (P.ROOT / "README.md").read_text() + f"\nSee also {OLD_NAME.lower()} docs.\n",
            "tools/rename-folder.sh": (P.ROOT / "tools/rename-folder.sh").read_text() + f"# {OLD_NAME} again\n",
            f"docs/{OLD_NAME}-notes.txt": "no word inside\n",
            "docs/research/planted.md": f"{OLD_NAME.upper()} in history is fine\n",
        }
        for rel, text in plants.items():
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / rel).write_text(text)
        res = scan(tmp, list(plants))
        flagged = sorted({h["file"] for h in res["hits"]})
        want = sorted(["README.md", "tools/rename-folder.sh", f"docs/{OLD_NAME}-notes.txt"])
        ok = flagged == want and "history: research notes stay as written" in res["allowed"] \
            and any(h["file"] == "tools/rename-folder.sh" and OLD_NAME in h["text"] for h in res["hits"]) \
            and "the folder-rename script's default source path is the old folder" in res["allowed"]
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


# ---------------------------------------------------------------- folder rename script

SCRIPT = P.ROOT / "tools" / "rename-folder.sh"


def encode(path: str) -> str:
    """Claude Code's folder name for a project path: every character that isn't a letter or digit becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def tree_listing(root: Path) -> dict:
    """path -> (kind, size, sha256 or link target) for everything under root."""
    out = {}
    for dp, dns, fns in os.walk(root):
        for n in dns + fns:
            p = Path(dp) / n
            rel = str(p.relative_to(root))
            if p.is_symlink():
                out[rel] = ["link", os.readlink(p)]
            elif p.is_dir():
                out[rel] = ["dir"]
            else:
                out[rel] = ["file", p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest()]
    return out


def _git(*args, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *(["-C", str(cwd)] if cwd else []), *args], capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "hs", "GIT_AUTHOR_EMAIL": "hs@localhost",
                               "GIT_COMMITTER_NAME": "hs", "GIT_COMMITTER_EMAIL": "hs@localhost"})


def scratch_world(tmp: Path) -> dict:
    """A scratch repo (one commit, a linked worktree inside it and one outside it), a scratch Claude Code projects
    folder with data for the repo and the inside worktree, and decoys that must not move."""
    old, new = tmp / "repos" / "project-old", tmp / "repos" / "project-new"
    projects = tmp / "claude" / "projects"
    old.mkdir(parents=True)
    for args in (("init", "-q", "-b", "main"), ("commit", "-q", "--allow-empty", "-m", "scratch")):
        r = _git(*args, cwd=old)
        if r.returncode:
            raise RuntimeError(f"git {args[0]}: {r.stderr}")
    wt_in, wt_out = old / ".claude" / "worktrees" / "wt-in", tmp / "elsewhere" / "wt-out"
    for wt, br in ((wt_in, "b-in"), (wt_out, "b-out")):
        r = _git("worktree", "add", "-q", "-b", br, str(wt), cwd=old)
        if r.returncode:
            raise RuntimeError(f"git worktree add: {r.stderr}")
    data = {encode(str(old)): "memory of the repo", encode(str(wt_in)): "memory of the inside worktree",
            encode(str(old)) + "-agent": "decoy: another project whose name starts the same"}
    for d, text in data.items():
        (projects / d / "memory").mkdir(parents=True)
        (projects / d / "memory" / "MEMORY.md").write_text(text + "\n")
        (projects / d / "transcript.jsonl").write_text('{"t": 1}\n')
    (tmp / "repos" / "other").mkdir()
    (tmp / "repos" / "other" / "keep.txt").write_text("unrelated\n")
    return {"old": old, "new": new, "projects": projects, "wt_in": wt_in, "wt_out": wt_out}


def _guard_profile(path: Path, allowed: Path, tag: str) -> None:
    path.write_text(f"""(version 1)
(allow default)
(deny file-write*
  (require-all
    (require-not (subpath "{allowed}"))
    (require-not (literal "/dev/null"))
    (require-not (literal "/dev/zero"))
    (require-not (literal "/dev/dtracehelper"))
    (require-not (regex #"^/dev/(tty|fd/|ttys)")))
  (with message "{tag}"))
""")


def run_script(prof: Path, *args) -> dict:
    r = subprocess.run([SANDBOX_EXEC, "-f", str(prof), "/bin/bash", str(SCRIPT), *map(str, args)],
                       capture_output=True, text=True, timeout=120)
    return {"args": list(map(str, args)), "rc": r.returncode, "stdout": r.stdout[-3000:], "stderr": r.stderr[-1500:]}


def worktrees(repo: Path) -> list[dict]:
    r = _git("worktree", "list", "--porcelain", cwd=repo)
    out, cur = [], None
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[len("worktree "):]}
            out.append(cur)
        elif cur is not None and line:
            k, _, v = line.partition(" ")
            cur[k] = v or True
    return out


def real_outside_listing() -> dict:
    """What a stray write to the real locations would change: the real Claude projects folder's entries and the
    real repo folders' existence."""
    home_projects = Path.home() / ".claude" / "projects"
    # names only: this very session keeps writing transcripts inside its own folder, so mtimes move on their own
    entries = sorted(d.name for d in home_projects.iterdir()) if home_projects.is_dir() else []
    return {"claude_projects": entries, "repo_exists": P.ROOT.is_dir(),
            "moonglade_folder_exists": (P.ROOT.parent / "project-moonglade").exists()}


@check("repo.rename_script", order=5, capability=CAP, stack="any", tier="quick")
def rename_script(ctx):
    """In a temp dir only (sandboxed: writes outside it are denied and logged): the dry run exits 0, prints the plan
    and changes nothing; --apply moves the repo and its Claude Code data folders (repo + inside worktree) to the new
    encoded names, leaves decoys alone, and `git worktree list` shows every worktree at a valid path with working
    links both ways; a second --apply onto an existing destination is refused and changes nothing."""
    ev: dict = {"script": str(SCRIPT)}
    require(SCRIPT.is_file() and os.access(SCRIPT, os.X_OK), "tools/rename-folder.sh is missing or not executable", **ev)
    tmp = Path(tempfile.mkdtemp(prefix="hs-rename-")).resolve()
    tag = f"hs-renameguard-{os.getpid()}-{int(time.time())}"
    prof = tmp / "guard.sb"
    _guard_profile(prof, tmp, tag)
    outside_before = real_outside_listing()
    since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        w = scratch_world(tmp / "w")
        old, new, projects = w["old"], w["new"], w["projects"]
        args = ("--claude-projects", projects, old, new)
        ev["scratch"] = {k: str(v) for k, v in w.items()}

        # sandbox self-test: a write outside the temp dir is blocked (and logged), one inside is allowed
        probe_out = Path(tempfile.gettempdir()).resolve() / f"hs-rename-probe-{os.getpid()}"
        t_out = subprocess.run([SANDBOX_EXEC, "-f", str(prof), "/usr/bin/touch", str(probe_out)], capture_output=True)
        t_in = subprocess.run([SANDBOX_EXEC, "-f", str(prof), "/usr/bin/touch", str(tmp / "probe-in")], capture_output=True)
        ev["sandbox_selftest"] = {"outside_rc": t_out.returncode, "outside_exists": probe_out.exists(),
                                  "inside_rc": t_in.returncode}
        probe_out.unlink(missing_ok=True)
        require(t_out.returncode != 0 and not probe_out.exists() and t_in.returncode == 0,
                "self-test: the sandbox doesn't confine writes to the temp dir", **ev)

        # dry run
        before = tree_listing(tmp / "w")
        ev["dry_run"] = run_script(prof, *args)
        after = tree_listing(tmp / "w")
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        ev["dry_run_changed"] = changed[:20]
        out = ev["dry_run"]["stdout"]
        plan = {"mentions_dry_run": "dry run" in out, "repo_move": f"{old}" in out and f"-> {new}" in out,
                "data_move": f"{projects / encode(str(old))}" in out and f"-> {projects / encode(str(new))}" in out,
                "worktree_line": str(w["wt_in"]) in out}
        ev["dry_run_plan"] = plan
        require(ev["dry_run"]["rc"] == 0, "the dry run failed", **ev)
        require(not changed, "the dry run changed files", **ev)
        require(all(plan.values()), "the dry run didn't print the whole plan", **ev)

        # apply
        data_before = {d.name: tree_listing(d) for d in projects.iterdir()}
        ev["apply"] = run_script(prof, "--apply", *args)
        require(ev["apply"]["rc"] == 0, "--apply failed", **ev)
        wt_in_new = new / w["wt_in"].relative_to(old)
        names_after = sorted(d.name for d in projects.iterdir())
        expect_names = sorted([encode(str(new)), encode(str(wt_in_new)), encode(str(old)) + "-agent"])
        ev["projects_after"] = names_after
        ev["projects_expected"] = expect_names
        require(not old.exists() and (new / ".git").exists(), "the repo folder wasn't moved", **ev)
        require(names_after == expect_names, "the Claude Code data folders aren't exactly the moved + untouched ones",
                **ev)
        moved_ok = (tree_listing(projects / encode(str(new))) == data_before[encode(str(old))]
                    and tree_listing(projects / encode(str(wt_in_new))) == data_before[encode(str(w["wt_in"]))]
                    and tree_listing(projects / (encode(str(old)) + "-agent")) == data_before[encode(str(old)) + "-agent"])
        require(moved_ok, "a data folder's contents changed in the move (or the decoy changed)", **ev)
        require((tmp / "w" / "repos" / "other" / "keep.txt").read_text() == "unrelated\n", "a decoy folder changed", **ev)
        wts = worktrees(new)
        ev["worktree_list"] = wts
        paths = [x["path"] for x in wts]
        require(paths[:1] == [str(new)] and sorted(paths[1:]) == sorted([str(wt_in_new), str(w["wt_out"])])
                and all(Path(p).is_dir() for p in paths), "git worktree list doesn't show the new paths", **ev)
        require(not any("prunable" in x for x in wts), "git reports a prunable (broken) worktree", **ev)
        links = {}
        for p in paths[1:]:
            r = _git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=Path(p))
            s = _git("status", "--porcelain", cwd=Path(p))
            links[p] = {"common_dir": r.stdout.strip(), "rc": r.returncode, "status_rc": s.returncode}
        ev["worktree_links"] = links
        require(all(v["rc"] == 0 and v["status_rc"] == 0 and v["common_dir"] == str(new / ".git")
                    for v in links.values()), "a worktree doesn't link back to the moved repo", **ev)

        # refusal: destination exists -> nonzero, nothing changes
        (tmp / "w" / "repos" / "project-old").mkdir()
        (tmp / "w" / "repos" / "project-old" / ".git").mkdir()
        before = tree_listing(tmp / "w")
        ev["refusal"] = run_script(prof, "--apply", "--claude-projects", projects, old, new)
        after = tree_listing(tmp / "w")
        ev["refusal_changed"] = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))[:20]
        require(ev["refusal"]["rc"] != 0 and "REFUSED" in ev["refusal"]["stderr"] and not ev["refusal_changed"],
                "--apply onto an existing destination wasn't refused cleanly", **ev)

        time.sleep(3)                                  # let the kernel's sandbox reports reach the log
        rows = _denials(tag, since)
        ev["denials"] = rows[:20]
        require(any("hs-rename-probe" in r["target"] for r in rows),
                "self-test: the blocked probe write isn't in the denial log, so the log can't vouch for the script",
                **ev)
        stray = [r for r in rows if "hs-rename-probe" not in r["target"]]
        require(not stray, "the script tried to write outside the temp dir", **ev)
        ev["outside_before"] = outside_before
        ev["outside_after"] = real_outside_listing()
        require(ev["outside_after"] == outside_before, "the real repo or Claude projects folder changed", **ev)
        return ev
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
