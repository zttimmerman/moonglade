import re
import shlex
import subprocess
import sys


def log(msg: str) -> None:
    print(f"[hs] {msg}", file=sys.stderr, flush=True)


def run(cmd, *, cwd=None, check=True, capture=False, env=None, timeout=None, input=None):
    """Run a command. Returns CompletedProcess. Text mode."""
    if isinstance(cmd, str):
        shown = cmd
    else:
        cmd = [str(c) for c in cmd]
        shown = shlex.join(cmd)
    log(f"$ {shown}")
    return subprocess.run(
        cmd, cwd=cwd, check=check, text=True, env=env, timeout=timeout, input=input,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        shell=isinstance(cmd, str),
    )


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def tail(path, n=40) -> str:
    """Last n non-blank lines of a log, ANSI colour codes stripped."""
    try:
        lines = ANSI.sub("", open(path, errors="replace").read()).splitlines()
    except FileNotFoundError:
        return f"(no log at {path})"
    lines = [l for l in lines if l.strip()]
    return "\n".join(lines[-n:])


class keep_awake:
    """Context manager: hold macOS no-sleep assertions (`caffeinate -s -i -d -u -w <our pid>`) for the duration.

    Why (F12, m2-living-world design "Notes from later work"): a mysqld abort ("Semaphore wait > 600 s") happened
    while the display slept during a long run; runs under caffeinate were clean. -s: no system sleep while on AC
    power (PreventSystemSleep; added 2026-10-03 after the Mac slept mid-run, worldserver stalled 5 and 15 min);
    -i: no idle system sleep;
    -d: no display sleep; -u: declare the user active (wakes a dark display); -w: the assertion also ends if this
    process dies. Entering returns evidence {"cmd", "pid"} (None pid when caffeinate isn't available).

    Limit: closing the lid of a MacBook with no external display still sleeps it ("Clamshell Sleep" in
    `pmset -g log`), whatever the assertions say. Keep the lid open (or an external display attached) during runs."""

    def __init__(self):
        self.proc = None

    def __enter__(self) -> dict:
        import os
        import shutil
        exe = shutil.which("caffeinate") or "/usr/bin/caffeinate"
        cmd = [exe, "-s", "-i", "-d", "-u", "-w", str(os.getpid())]
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        except OSError as e:
            log(f"keep_awake: caffeinate unavailable ({e}); the Mac may sleep during long runs")
            return {"cmd": shlex.join(cmd), "pid": None, "error": str(e)}
        return {"cmd": shlex.join(cmd), "pid": self.proc.pid}

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        return False


def awake_assertions(pid: int | None) -> list[str]:
    """Lines of `pmset -g assertions` owned by `pid` (evidence that the no-sleep assertion is held)."""
    if not pid:
        return []
    r = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True, errors="replace", timeout=20)
    return [l.strip() for l in r.stdout.splitlines() if f"pid {pid}(" in l]


# ---------------------------------------------------------------- keeping passwords out of reports

_PW_RE = __import__("re").compile(r"""(?i)((?<!set )password(?:["']?\s*[:=]\s*["']?|\s+))([^\s"',()][^\s"',]*)""")


def _known_secrets() -> list[str]:
    """The player's real password (env/player.json, read-only) and $HS_PLAYER_FILE's, when present."""
    import json
    import os
    from pathlib import Path
    from . import paths as P
    out = []
    for f in {P.ENV / "player.json", *([Path(os.environ["HS_PLAYER_FILE"])] if os.environ.get("HS_PLAYER_FILE") else [])}:
        try:
            pw = str(json.loads(Path(f).read_text())["password"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if len(pw) >= 6:
            out.append(pw)
    return out


_GM_PW_RE = __import__("re").compile(
    r"(?i)(account\s+create\s+\S+\s+)(\S+)|(account\s+set\s+password\s+\S+\s+)(\S+)(\s+)(\S+)")


def _gm_sub(m):
    if m.group(1):
        return m.group(1) + "[redacted]"
    return m.group(3) + "[redacted]" + m.group(5) + "[redacted]"


def _fixed_secrets() -> list:
    """The harness's own account passwords (paths.py), matched as whole words and case-sensitively, so names such as
    HSTEST or Hstester stay readable."""
    import re
    from . import paths as P
    return [re.compile(rf"(?<![A-Za-z0-9]){re.escape(pw)}(?![A-Za-z0-9])") for pw in (P.TEST_PASS, P.GM_PASS, P.COMP_PASS)]


def redact(obj, secrets: list[str] | None = None):
    """A copy of obj (dicts/lists/strings) with every "password <value>" / "password": "<value>" value and every
    known secret replaced by [redacted]. Used on harness results before they're written."""
    secrets = _known_secrets() if secrets is None else secrets

    fixed = _fixed_secrets()

    def s(x: str) -> str:
        x = _GM_PW_RE.sub(_gm_sub, x)               # GM commands: account create <name> <pw>, account set password
        x = _PW_RE.sub(lambda m: m.group(1) + "[redacted]", x)
        for rx in fixed:
            x = rx.sub("[redacted]", x)
        for sec in secrets:
            x = x.replace(sec, "[redacted]")
        return x
    if isinstance(obj, str):
        return s(obj)
    if isinstance(obj, dict):
        return {k: (("[redacted]" if str(k).lower() == "password" and isinstance(v, str) else redact(v, secrets)))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v, secrets) for v in obj]
    return obj
