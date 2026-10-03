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
