#!/usr/bin/env python3
"""Rewrite a scratch MIRROR clone's history for publishing (openspec/changes/go-public, "Clean-history dry run").

Run it through tools/go-public/rewrite.sh, which installs the pinned git-filter-repo into env/tools/venv first.

    rewrite.sh [--denylist FILE] <bare-mirror-dir>

What it does, on every ref of the target:
- author and committer emails that aren't already a noreply address -> NOREPLY (Zach's GitHub noreply address);
- commit and tag messages: `Claude-Session:` trailer lines (and any other Claude Code session URL) removed, the old
  project name -> Moonglade (case kept), every denylist string -> [redacted];
- every blob: every Claude Code session URL and every denylist string (case-insensitive) -> [redacted];
- paths dropped from all history: any `__pycache__/` folder, `*.pyc`, `docs/research/img/`.

It refuses, before changing anything, unless the target is a bare repository that isn't this checkout's own git
directory and none of its remotes has a push URL pointing at GitHub. It never pushes; git-filter-repo removes the
`origin` remote when it's done, so a push needs a deliberate `git remote add` (docs/going-public.md).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harness"))

from hslib import publicscan as S                       # noqa: E402
from hslib.checks.naming_checks import NEW_NAME, OLD_NAME  # noqa: E402  (the one place that spells the old name)

NOREPLY = b"93889833+zttimmerman@users.noreply.github.com"
NOREPLY_RX = re.compile(rb"(?i)(^|[.+@])no-?reply[.@]|@users\.noreply\.github\.com$")
DROP_PATH_RX = re.compile(rb"(^|/)__pycache__/|\.pyc$|^docs/research/img/")
SESSION_LINE_RX = re.compile(rb"(?im)^[ \t]*Claude-Session:[^\n]*(\n|$)")
SESSION_URL_RX = re.compile(rb"\S*claude\.ai/code/" + b"session_" + rb"\S*")
GITHUB_RX = re.compile(r"(?i)github\.com")


class Refused(Exception):
    pass


def _git(target: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True)


def refusal(target: Path) -> str | None:
    """Why this target must not be rewritten, or None. Checked before anything is changed."""
    if not target.is_dir():
        return f"{target} is not a directory"
    r = _git(target, "rev-parse", "--is-bare-repository")
    if r.returncode != 0:
        return f"{target} is not a git repository"
    if r.stdout.strip() != "true":
        return f"{target} is a working checkout, not a bare mirror clone (make one with tools/go-public/mirror.sh)"
    own = _git(ROOT, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    tgt = _git(target, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    if own and tgt and Path(own).resolve() == Path(tgt).resolve():
        return f"{target} is this checkout's own git directory"
    for name in _git(target, "remote").stdout.split():
        r = _git(target, "remote", "get-url", "--push", "--all", name)
        urls = r.stdout.split() if r.returncode == 0 else ["(unreadable)"]
        for u in urls:
            if u == "(unreadable)" or GITHUB_RX.search(u):
                return (f"remote {name!r} has a push URL pointing at GitHub ({u}); disable it first "
                        f"(git -C <mirror> remote set-url --push {name} no-push)")
    return None


def rename_old(msg: bytes) -> bytes:
    old, new = OLD_NAME.encode(), NEW_NAME.encode()

    def sub(m: re.Match) -> bytes:
        w = m.group(0)
        if w.isupper():
            return new.upper()
        if w.islower():
            return new.lower()
        return new
    return re.sub(re.escape(old), sub, msg, flags=re.I)


def strip_sessions(msg: bytes) -> bytes:
    msg = SESSION_LINE_RX.sub(b"", msg)
    msg = SESSION_URL_RX.sub(b"", msg)
    return msg.rstrip() + b"\n" if msg.strip() else msg


def redact(data: bytes, deny: list[re.Pattern]) -> bytes:
    for rx in deny:
        data = rx.sub(S.REDACTED, data)
    return data


def clean_message(msg: bytes, deny: list[re.Pattern]) -> bytes:
    """Message rewrite: sessions out, old name -> new name, denylist redacted (in that order, so a denylisted
    folder name that contains the old name is caught either way)."""
    return redact(rename_old(strip_sessions(msg)), deny)


def clean_email(email: bytes) -> bytes:
    return email if NOREPLY_RX.search(email) else NOREPLY


def run(target: Path, denylist: Path | None = None) -> None:
    why = refusal(target)
    if why:
        raise Refused(why)
    entries = S.load_denylist(denylist)
    deny = [re.compile(re.escape(e.encode()), re.I) for e in entries]
    import git_filter_repo as fr

    def blob_cb(blob, _meta):
        blob.data = redact(SESSION_URL_RX.sub(S.REDACTED, blob.data), deny)

    def filename_cb(name):
        return None if DROP_PATH_RX.search(name) else name

    args = fr.FilteringOptions.parse_args(["--force", "--quiet"])
    # --force: git-filter-repo's own "fresh clone" heuristic; this tool's refusal() above is the safety gate (a bare
    # mirror in scratch, never this checkout, no GitHub push URL).
    os.chdir(target)
    fr.RepoFilter(args,
                  message_callback=lambda m: clean_message(m, deny),
                  email_callback=clean_email,
                  blob_callback=blob_cb,
                  filename_callback=filename_cb).run()


def main(argv: list[str]) -> int:
    denylist = None
    if len(argv) >= 2 and argv[0] == "--denylist":
        denylist, argv = Path(argv[1]), argv[2:]
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    target = Path(argv[0]).resolve()
    try:
        run(target, denylist)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    except S.DenylistError as e:
        print(f"error: {e}", file=sys.stderr)
        return 4
    print(f"rewritten: {target} (nothing pushed)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
