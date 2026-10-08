"""public-repo (openspec/changes/go-public): the repo can be published without personal data or game files.

public.license        LICENSE / NOTICE / README carry the licence, the notices and the disclaimer.
public.tree_scrub     no tracked file matches the built-in personal/wording patterns or the local denylist
                      (env/public-denylist.txt, untracked); a planted hit in a temporary index is found.
public.no_game_files  no client/game asset or docs screenshot is tracked; client patches stay byte recipes.
public.history_dryrun a fresh mirror clone of origin, rewritten by tools/go-public/rewrite.sh, scans clean; the
                      branch's tree is unchanged; origin is untouched; the tool refuses this checkout and a GitHub
                      push URL.

The denylist's strings are personal, so evidence never contains them: hits name file, line and "denylist #N".
Tests pass `denylist=` (a path) to the check functions; no environment variable changes where the denylist is read.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .. import paths as P
from .. import publicscan as S
from ..registry import check, require

CAP = "public-repo"

# ----------------------------------------------------------------------------- public.license

# sha256 of https://www.gnu.org/licenses/old-licenses/gpl-2.0.txt (fetched 2026-10-08), the text LICENSE ends with.
GPL2_SHA256 = "edaef632cbb643e4e7a221717a6c441a4c1a7c918e6e4d56debc3d8739b233f6"
LICENSE_SEPARATOR = "-" * 80 + "\n\n"
LICENSE_NEEDS = ("either version 2 of the License, or (at your option) any later version",
                 "SPDX-License-Identifier: GPL-2.0-or-later")
NOTICE_NEEDS = ("not affiliated with or endorsed by Blizzard Entertainment", "OpenSpec", "MIT License",
                "Permission is hereby granted, free of charge", "VMaNGOS", "GPL-2.0", "branding/",
                "all rights reserved", "no game files are distributed")
README_NEEDS = ("GPL-2.0-or-later", "not affiliated with or endorsed by Blizzard Entertainment",
                "Bring your own client; no game files are distributed", "[LICENSE](LICENSE)", "[NOTICE](NOTICE)")
# Tracked files OpenSpec generated (MIT): each must be covered by a glob NOTICE lists.
OPENSPEC_GLOBS = (".claude/skills/openspec-*/SKILL.md", ".claude/commands/opsx/*.md")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).casefold()


def license_problems(texts: dict, tracked: list[str]) -> list[str]:
    """Problems with {"LICENSE": str|None, "NOTICE": ..., "README.md": ...} (None = missing)."""
    probs = []
    lic = texts.get("LICENSE")
    if lic is None:
        probs.append("LICENSE is missing")
    else:
        head, sep, body = lic.partition(LICENSE_SEPARATOR)
        if not sep:
            probs.append("LICENSE has no separator line before the GPL text")
        elif hashlib.sha256(body.encode()).hexdigest() != GPL2_SHA256:
            probs.append("LICENSE's licence text isn't the verbatim GPL-2.0 text (sha256 mismatch)")
        for need in LICENSE_NEEDS:
            if _norm(need) not in _norm(head):
                probs.append(f"LICENSE header lacks: {need}")
    notice = texts.get("NOTICE")
    if notice is None:
        probs.append("NOTICE is missing")
    else:
        for need in NOTICE_NEEDS:
            if _norm(need) not in _norm(notice):
                probs.append(f"NOTICE lacks: {need}")
        listed = [g for g in OPENSPEC_GLOBS if g in notice]
        if len(listed) != len(OPENSPEC_GLOBS):
            probs.append(f"NOTICE doesn't list the OpenSpec globs {sorted(set(OPENSPEC_GLOBS) - set(listed))}")
        uncovered = [f for f in tracked if ("openspec" in f or "/opsx/" in f) and f.startswith(".claude/")
                     and not any(fnmatch.fnmatchcase(f, g) for g in OPENSPEC_GLOBS)]
        if uncovered:
            probs.append(f"OpenSpec-generated files not covered by NOTICE: {uncovered}")
    readme = texts.get("README.md")
    if readme is None:
        probs.append("README.md is missing")
    else:
        for need in README_NEEDS:
            if _norm(need) not in _norm(readme):
                probs.append(f"README lacks: {need}")
    return probs


def _read(rel: str) -> str | None:
    p = P.ROOT / rel
    return p.read_text(encoding="utf-8") if p.is_file() else None


@check("public.license", order=6, capability=CAP, stack="any", tier="quick")
def public_license(ctx):
    """LICENSE is the verbatim GPL-2.0 text under a GPL-2.0-or-later notice; NOTICE has the Blizzard disclaimer, the
    OpenSpec MIT files and licence, the VMaNGOS data and the branding terms; README states the licence, the
    disclaimer and "bring your own client; no game files are distributed". Mutated copies (each item removed) fail."""
    from .naming_checks import tracked_files
    tracked = tracked_files(P.ROOT)
    texts = {k: _read(k) for k in ("LICENSE", "NOTICE", "README.md")}
    probs = license_problems(texts, tracked)
    ev = {"problems": probs, "gpl2_sha256": GPL2_SHA256,
          "openspec_files": sorted(f for f in tracked if any(fnmatch.fnmatchcase(f, g) for g in OPENSPEC_GLOBS))}
    # self-test: each mutation must be caught
    lic, notice, readme = texts["LICENSE"] or "", texts["NOTICE"] or "", texts["README.md"] or ""
    muts = {
        "no or-later notice": {**texts, "LICENSE": re.sub(r"or\s+\(at\s+your\s+option\)\s+any\s+later\s+version", "only",
                                                          lic, count=1)},
        "GPL text altered": {**texts, "LICENSE": lic.replace("Version 2, June 1991", "Version 3", 1)},
        "no Blizzard disclaimer in NOTICE": {**texts, "NOTICE": re.sub(r"(?i)not\s+affiliated", "", notice)},
        "no MIT in NOTICE": {**texts, "NOTICE": notice.replace("MIT License", "")},
        "no branding terms": {**texts, "NOTICE": re.sub(r"(?i)all\s+rights\s+reserved", "", notice)},
        "no README disclaimer": {**texts, "README.md": re.sub(r"(?i)not\s+affiliated", "", readme)},
        "README missing": {**texts, "README.md": None},
    }
    ev["selftest"] = {k: license_problems(v, tracked) for k, v in muts.items()}
    require(all(ev["selftest"].values()), "self-test: a mutated LICENSE/NOTICE/README passed", **ev)
    require(not probs, "; ".join(probs)[:300] or "licence files incomplete", **ev)
    return ev


# ----------------------------------------------------------------------------- public.tree_scrub

def _home_plant() -> str:
    return "/" + "Users" + "/someone/notes.txt"       # spelled in pieces: this file must not match itself


def scrub_selftest(denylist: Path | None) -> dict:
    """Plant one denylist entry and one home path in a scratch file staged in a TEMPORARY index (and a temporary
    object directory, so the repo's own object store and index are untouched; nothing is committed), scan that
    index, and require exactly those two hits on their lines on top of the real scan's. Then require a missing and
    an empty denylist to raise DenylistError."""
    entries = S.load_denylist(denylist)
    tmp = Path(tempfile.mkdtemp(prefix="hs-public-scrub-")).resolve()
    out: dict = {"dir": str(tmp)}
    try:
        common = Path(subprocess.run(["git", "-C", str(P.ROOT), "rev-parse", "--path-format=absolute",
                                      "--git-common-dir"], capture_output=True, text=True, check=True).stdout.strip())
        real_index = Path(subprocess.run(["git", "-C", str(P.ROOT), "rev-parse", "--path-format=absolute",
                                          "--git-path", "index"], capture_output=True, text=True,
                                         check=True).stdout.strip())
        (tmp / "objects").mkdir()
        shutil.copyfile(real_index, tmp / "index")
        env = {"GIT_INDEX_FILE": str(tmp / "index"), "GIT_OBJECT_DIRECTORY": str(tmp / "objects"),
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(common / "objects")}
        index_before = hashlib.sha256(real_index.read_bytes()).hexdigest()
        rel = "zz-public-scrub-plant.txt"
        # an entry whose line triggers only its own pattern (entries can contain each other, or an email)
        pats = S.builtin_patterns() + S.denylist_patterns(entries)
        pick = next((i for i, x in enumerate(entries, 1)
                     if S.find_hits(x.encode(), pats) == [(f"denylist #{i}", 1)]), None)
        if pick is None:
            return out | {"ok": False, "error": "no denylist entry can be planted on its own (each also matches "
                                                "another pattern)"}
        content = f"planted by public.tree_scrub's self-test\n{entries[pick - 1]}\nsee {_home_plant()}\n".encode()
        e = {**os.environ, **env}
        sha = subprocess.run(["git", "-C", str(P.ROOT), "hash-object", "-w", "--stdin"], input=content,
                             capture_output=True, env=e, check=True).stdout.decode().strip()
        subprocess.run(["git", "-C", str(P.ROOT), "update-index", "--add", "--cacheinfo", f"100644,{sha},{rel}"],
                       capture_output=True, env=e, check=True)
        planted = S.scan_tree(P.ROOT, denylist, env=env)
        got = sorted((h["line"], h["pattern"]) for h in planted["hits"] if h["file"] == rel)
        want = [(2, f"denylist #{pick}"), (3, "home path")]
        real = S.scan_tree(P.ROOT, denylist)
        others_same = [h for h in planted["hits"] if h["file"] != rel] == real["hits"]
        out |= {"planted_file": rel, "planted_hits": got, "expected": want, "other_hits_unchanged": others_same,
                "real_index_unchanged": hashlib.sha256(real_index.read_bytes()).hexdigest() == index_before,
                "plant_object_in_repo": (common / "objects" / sha[:2] / sha[2:]).exists()}
        errors = {}
        for name, path in (("missing", tmp / "no-such-denylist.txt"), ("empty", tmp / "empty-denylist.txt")):
            if name == "empty":
                path.write_text("\n# only a comment\n")
            try:
                S.scan_tree(P.ROOT, path)
                errors[name] = "no error (scan ran)"
            except S.DenylistError as ex:
                errors[name] = f"DenylistError: {str(ex).replace(str(tmp), '<tmp>')}"
        out["denylist_errors"] = errors
        out["ok"] = (got == want and others_same and out["real_index_unchanged"] and not out["plant_object_in_repo"]
                     and all(v.startswith("DenylistError") for v in errors.values()))
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@check("public.tree_scrub", order=7, capability=CAP, stack="any", tier="quick")
def public_tree_scrub(ctx, denylist: Path | None = None):
    """Every tracked file (staged blob and path) is free of the built-in patterns (non-noreply emails, home paths,
    Claude Code session URLs, claims that the repo is private or never shared) and of every local denylist line; a
    planted denylist entry and home path in a temporary index are both reported with file and line; a missing or
    empty denylist is an error, never a pass."""
    res = S.scan_tree(P.ROOT, denylist)            # DenylistError -> the check errors (fail-closed)
    ev = {"denylist": "env/public-denylist.txt" if denylist is None else "(test path)",
          "tracked_files": res["files"], "patterns_used": res["patterns"], "denylist_entries": res["denylist_entries"],
          "hit_count": len(res["hits"]), "hits": res["hits"][:100], "wording_allowed": res["allowed"],
          "wording_allowlist": [g for g, _ in S.WORDING_ALLOW]}
    require(res["files"] > 100, "the index listed too few files to trust the scan", **ev)
    ev["selftest"] = scrub_selftest(denylist)
    require(ev["selftest"]["ok"], "self-test: the planted hits weren't reported exactly (or a bad denylist passed)",
            **ev)
    require(not res["hits"], f"{len(res['hits'])} hit(s) in tracked files", **ev)
    return ev


# ----------------------------------------------------------------------------- public.no_game_files

GAME_EXT = (".mpq", ".dbc", ".exe", ".dll", ".blp", ".m2", ".wdt", ".adt", ".wmo", ".skin", ".wav", ".mp3",
            ".ogg", ".ttf", ".otf")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tga", ".webp", ".icns", ".tif", ".tiff", ".heic", ".ico")
MAGIC = ((b"MPQ\x1a", "an MPQ archive"), (b"WDBC", "a DBC file"), (b"BLP2", "a BLP texture"),
         (b"MD20", "an M2 model"))
PATCH_MAX = 16 * 1024


def game_file_problems(files: dict) -> list[str]:
    """{rel: first bytes (or whole content for patches/client)} -> problems."""
    probs = []
    for rel, head in sorted(files.items()):
        low = rel.lower()
        if low.endswith(GAME_EXT):
            probs.append(f"{rel}: game/client file type")
        if low.startswith("docs/research/img/"):
            probs.append(f"{rel}: screenshot under docs/research/img/")
        if low.endswith(IMAGE_EXT) and not rel.startswith("branding/"):
            probs.append(f"{rel}: image outside branding/")
        for magic, what in MAGIC:
            if head.startswith(magic):
                probs.append(f"{rel}: content is {what}")
        if head.startswith(b"MZ") and b"PE\0\0" in head[:1024]:
            probs.append(f"{rel}: content is a Windows executable (PE)")
        if rel.startswith("patches/client/"):
            if not low.endswith(".json") or len(head) > PATCH_MAX:
                probs.append(f"{rel}: client patch isn't a small JSON recipe")
                continue
            try:
                rec = json.loads(head)
            except ValueError:
                probs.append(f"{rel}: client patch isn't valid JSON")
                continue
            if rec.get("kind") == "binary":
                hexes = [rec.get("input_sha256"), rec.get("output_sha256")]
                if not all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) for h in hexes):
                    probs.append(f"{rel}: binary patch isn't keyed by the stock input sha256 (and output sha256)")
                for e in rec.get("edits") or [{}]:
                    o, r = e.get("original", ""), e.get("replacement", "")
                    if not (isinstance(o, str) and isinstance(r, str) and 0 < len(o) <= 64 and len(o) == len(r)):
                        probs.append(f"{rel}: an edit isn't a short byte recipe")
            elif rec.get("kind") == "mpq":
                srcs = list((rec.get("files") or {}).values())
                if any(not (isinstance(s, str) and s.startswith("client-mods/")) for s in srcs):
                    probs.append(f"{rel}: data patch packs something other than our own client-mods/ files")
    return probs


@check("public.no_game_files", order=8, capability=CAP, stack="any", tier="quick")
def public_no_game_files(ctx):
    """No tracked path has a client/game asset extension (MPQ, DBC, EXE, DLL, BLP, M2, WDT/ADT, WMO, WAV/MP3/OGG,
    TTF/OTF) or such a file's magic bytes, nothing is under docs/research/img/, every image is under branding/, and
    every client patch is a small JSON recipe (binary patches keyed by the stock input sha256, data patches packing
    only client-mods/ files). Planted counterexamples are each caught."""
    from .naming_checks import tracked_files
    tracked = tracked_files(P.ROOT)
    files = {}
    for rel in tracked:
        p = P.ROOT / rel
        if p.is_symlink() or not p.is_file():
            continue
        with open(p, "rb") as f:
            files[rel] = f.read() if rel.startswith("patches/client/") else f.read(1024)
    probs = game_file_problems(files)
    images = sorted(r for r in tracked if r.lower().endswith(IMAGE_EXT))
    ev = {"tracked_files": len(tracked), "images": images,
          "client_patches": sorted(r for r in tracked if r.startswith("patches/client/")), "problems": probs}
    stock = files.get("patches/client/0001-gluexml-signature.json", b"{}")
    plants = {
        "Data/patch-4.MPQ": b"MPQ\x1a....", "docs/research/img/shot.jpg": b"\xff\xd8\xff",
        "docs/shot.png": b"\x89PNG", "tools/Wow.exe": b"MZ\x90\0", "notes/blob.bin": b"MZ\x90\0" + b"\0" * 100 + b"PE\0\0",
        "music/theme.mp3": b"ID3", "patches/client/0009-big.json": b"{" + b" " * (PATCH_MAX + 1) + b"}",
        "patches/client/0010-unkeyed.json": stock.replace(b'"input_sha256"', b'"input_sha256_gone"'),
    }
    ev["selftest"] = {k: game_file_problems({k: v}) for k, v in plants.items()}
    ev["selftest_clean_control"] = game_file_problems({"branding/AppIcon.png": b"\x89PNG", "README.md": b"# x"})
    require(all(ev["selftest"].values()) and not ev["selftest_clean_control"],
            "self-test: a planted game file wasn't caught (or a clean control was flagged)", **ev)
    require(len(tracked) > 100, "git ls-files returned too few files to trust the scan", **ev)
    require(not probs, f"{len(probs)} game-content problem(s)", **ev)
    return ev


# ----------------------------------------------------------------------------- public.history_dryrun

GO = P.ROOT / "tools" / "go-public"


def _g(repo: Path, *args, check=True, env=None) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])}: {r.stderr.strip()[-300:]}")
    return r.stdout


def _ls_remote(repo: Path) -> dict:
    out = _g(repo, "ls-remote", "origin")
    return dict(reversed(ln.split("\t")) for ln in out.splitlines() if "\t" in ln)


def _refs(repo: Path) -> str:
    return _g(repo, "for-each-ref", "--format=%(objectname) %(refname)") + _g(repo, "rev-parse", "HEAD", check=False)


def _ls_tree(repo: Path, ref: str) -> dict:
    out = _g(repo, "ls-tree", "-r", "-z", ref)
    res = {}
    for rec in out.split("\0"):
        if rec:
            meta, path = rec.split("\t", 1)
            res[path] = meta.split()[2]
    return res


def _run(cmd: list, timeout=900) -> dict:
    t = time.time()
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout)
    mask = lambda s: s.replace(str(P.ROOT), "<repo>")      # evidence names no home folder
    return {"cmd": [mask(str(c)) for c in cmd], "rc": r.returncode,
            "stdout_tail": mask(r.stdout[-600:]), "stderr_tail": mask(r.stderr[-600:]), "s": round(time.time() - t, 1)}


@check("public.history_dryrun", order=9, capability=CAP, stack="any", tier="slow")
def public_history_dryrun(ctx, denylist: Path | None = None, rewrite_cmd: list | None = None):
    """In a scratch dir: tools/go-public/rewrite.sh refuses this checkout and a mirror whose push URL is GitHub,
    changing nothing; a fresh `git clone --mirror` of origin (which scans with hits before) is rewritten, and then the
    scan finds zero hits in every reachable commit/tag (message, author, committer), blob and path; no message has the
    old project name, no __pycache__/.pyc/docs/research/img path remains, every author/committer email is noreply;
    the current branch's rewritten tree equals this checkout's HEAD tree; origin's refs are unchanged."""
    from .naming_checks import OLD_NAME
    t_start = time.time()
    ev: dict = {}
    branch = _g(P.ROOT, "symbolic-ref", "--short", "HEAD", check=False).strip()
    head = _g(P.ROOT, "rev-parse", "HEAD").strip()
    head_tree = _g(P.ROOT, "rev-parse", "HEAD^{tree}").strip()
    origin_before = _ls_remote(P.ROOT)
    ev |= {"branch": branch, "head": head, "head_tree": head_tree, "origin_refs": len(origin_before)}
    require(branch and origin_before.get(f"refs/heads/{branch}") == head,
            "GIVEN not met: this checkout's branch isn't pushed to origin at HEAD (push it first)", **ev)
    base = P.ENV / "tmp" if (P.ENV / "tmp").is_dir() else None
    tmp = Path(tempfile.mkdtemp(prefix="hs-public-history-", dir=base)).resolve()
    mirror = tmp / "mirror.git"
    rewrite = [GO / "rewrite.sh"] + (["--denylist", denylist] if denylist is not None else [])
    try:
        # refusal 1: this working checkout (origin = GitHub)
        before = _refs(P.ROOT)
        ev["refuse_checkout"] = r = _run(rewrite + [P.ROOT])
        require(r["rc"] != 0 and "REFUSED" in r["stderr_tail"] and _refs(P.ROOT) == before,
                "rewrite.sh didn't refuse this checkout cleanly", **ev)

        ev["mirror"] = r = _run([GO / "mirror.sh", mirror])
        require(r["rc"] == 0 and (mirror / "HEAD").exists(), "mirror clone failed", **ev)
        require(_g(mirror, "rev-parse", "--is-bare-repository").strip() == "true"
                and not (mirror / "objects" / "info" / "alternates").exists(),
                "the mirror isn't a bare, self-contained clone", **ev)

        # refusal 2: a mirror whose push URL points at GitHub
        fetch_url = _g(mirror, "remote", "get-url", "origin").strip()
        _g(mirror, "remote", "set-url", "--push", "origin", fetch_url)
        before = _refs(mirror)
        ev["refuse_github_push"] = r = _run(rewrite + [mirror])
        _g(mirror, "remote", "set-url", "--push", "origin", "no-push")
        require(r["rc"] != 0 and "REFUSED" in r["stderr_tail"] and "GitHub" in r["stderr_tail"]
                and _refs(mirror) == before, "rewrite.sh didn't refuse a GitHub push URL cleanly", **ev)

        # the scan must see the personal data before the rewrite (it can fail on this run's data)
        pre = S.scan_history(mirror, denylist)
        ev["before"] = {k: pre[k] for k in ("refs", "commits", "tags", "blobs", "paths", "hit_count", "by_pattern",
                                            "by_kind")}
        require(pre["hit_count"] > 0 and pre["by_pattern"].get("email address")
                and pre["by_pattern"].get("claude session link"),
                "self-test: the history scan found no email or session-link hits before the rewrite", **ev)
        ref = f"refs/heads/{branch}"
        orig_tree = _g(mirror, "rev-parse", f"{ref}^{{tree}}").strip()
        orig_files = _ls_tree(mirror, ref)
        main_orig = _g(mirror, "rev-parse", "refs/heads/main^{tree}", check=False).strip()

        ev["rewrite"] = r = _run(rewrite_cmd + [mirror] if rewrite_cmd else rewrite + [mirror])
        require(r["rc"] == 0, "rewrite.sh failed", **ev)

        post = S.scan_history(mirror, denylist)
        ev["after"] = {k: post[k] for k in ("refs", "commits", "tags", "blobs", "paths", "hit_count", "by_pattern",
                                            "by_kind")}
        ev["after_hits"] = post["hits"][:40]
        msgs = _g(mirror, "log", "--all", "--format=%H%x00%B%x1e")
        old_msgs = [m.strip().split("\0")[0][:12] for m in msgs.split("\x1e") if re.search(OLD_NAME, m, re.I)]
        trailer_msgs = [m.strip().split("\0")[0][:12] for m in msgs.split("\x1e") if "Claude-Session" in m]
        emails = sorted(set(_g(mirror, "log", "--all", "--format=%ae%n%ce").split()))
        paths = {ln.partition(" ")[2] for ln in _g(mirror, "rev-list", "--all", "--objects").splitlines()}
        dropped_left = sorted(p for p in paths if re.search(r"(^|/)__pycache__(/|$)|\.pyc$|^docs/research/img(/|$)", p))
        new_tree = _g(mirror, "rev-parse", f"{ref}^{{tree}}", check=False).strip()
        new_files = _ls_tree(mirror, ref) if new_tree else {}
        diff = sorted(p for p in set(orig_files) | set(new_files) if orig_files.get(p) != new_files.get(p))
        ev |= {"messages_with_old_name": old_msgs[:20], "messages_with_session_trailer": trailer_msgs[:20],
               "emails_after": emails, "dropped_paths_left": dropped_left[:20],
               "tree": {"head_tree": head_tree, "mirror_tree_before": orig_tree, "rewritten_tree": new_tree,
                        "paths_changed_by_rewrite": diff[:50],
                        "main_tree_before": main_orig,
                        "main_tree_after": _g(mirror, "rev-parse", "refs/heads/main^{tree}", check=False).strip()},
               "remotes_after": _g(mirror, "remote").split()}
        origin_after = _ls_remote(P.ROOT)
        ev["origin_unchanged"] = origin_after == origin_before
        ev["elapsed_s"] = round(time.time() - t_start, 1)
        require(ev["origin_unchanged"], "origin's refs changed during the dry run", **ev)
        require(post["hit_count"] == 0 and not trailer_msgs,
                f"{post['hit_count']} hit(s) left in the rewritten history", **ev)
        require(not old_msgs, "commit messages still carry the old project name", **ev)
        require(not dropped_left, "dropped paths are still in the rewritten history", **ev)
        require(all(re.search(r"(?i)noreply", e) for e in emails), "a non-noreply email is left", **ev)
        require(orig_tree == head_tree, "GIVEN not met: origin's branch tree isn't this checkout's HEAD tree", **ev)
        require(new_tree == head_tree, f"the rewrite changed the branch's tree ({len(diff)} path(s))", **ev)
        return ev
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
