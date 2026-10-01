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
