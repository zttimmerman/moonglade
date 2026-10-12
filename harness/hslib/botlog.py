"""Playerbots.log helpers (m2-living-world task 1.1).

worldserver.conf.dist: `Appender.Playerbots=2,5,0,Playerbots.log,w` (file appender, level 5 = debug, no prefix
flags, mode w = truncated at each worldserver start) and `Logger.playerbots=5,Console Server Playerbots`, so the
file holds this worldserver run's playerbots messages. LOG_* lines carry no timestamp or level prefix; callers
match message text from mod-playerbots source (cite the line in the check)."""
import re
from pathlib import Path

from . import paths as P
from .util import ANSI

LOG = P.LOGS / "Playerbots.log"


def offset(path: Path = LOG) -> int:
    """Current size of the log: pass it as `since` to read only what comes after."""
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def lines(since: int = 0, path: Path = LOG) -> list[str]:
    """Log lines from byte offset `since` (0 = whole file), ANSI codes stripped. A log shorter than `since` was
    truncated by a worldserver restart and is read from the start."""
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(since if since <= size else 0)
            text = fh.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return []
    return [ANSI.sub("", l) for l in text.splitlines()]


def grep(pattern: str, since: int = 0, path: Path = LOG, flags: int = 0) -> list[str]:
    """Lines matching regex `pattern` (re.search) from byte offset `since`."""
    rx = re.compile(pattern, flags)
    return [l for l in lines(since, path) if rx.search(l)]


def count(pattern: str, since: int = 0, path: Path = LOG) -> int:
    return len(grep(pattern, since, path))


WORLDSERVER_LOG = P.LOGS / "worldserver.out"


def marks(paths: dict[str, Path] | None = None) -> dict[str, tuple[Path, int]]:
    """Offsets of the server logs now: pass the result to `excerpt` to keep only what came after (a check's start)."""
    paths = paths if paths is not None else {"playerbots": LOG, "worldserver": WORLDSERVER_LOG}
    return {name: (path, offset(path)) for name, path in paths.items()}


def excerpt(at: dict[str, tuple[Path, int]], max_lines: int = 150, pattern: str | None = None) -> dict[str, list[str]]:
    """The last `max_lines` lines of each log written since `marks()` (a log truncated by a worldserver restart in
    between is read from its start, see `lines`), only those matching regex `pattern` (re.search, case-insensitive)
    when given. Evidence for a failure that has no other server-side trace."""
    rx = re.compile(pattern, re.I) if pattern else None
    return {name: [l for l in lines(off, path) if not rx or rx.search(l)][-max_lines:]
            for name, (path, off) in at.items()}
