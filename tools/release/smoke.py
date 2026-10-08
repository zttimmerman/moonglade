#!/usr/bin/env python3
"""Smoke-test a release payload where Homebrew can't be seen (openspec/changes/release-build, "Runs without Homebrew").

    smoke.py --payload DIR --scratch DIR [--port N] [--sandbox] [--deny-write DIR] [--json OUT]

Copies the payload to "<scratch>/Place One/Moonglade" (a path with spaces), then:
  1. the bundled mysqld initialises a scratch datadir and starts on a scratch port (TCP on 127.0.0.1 only),
  2. the bundled mysql client queries it (SELECT VERSION()), then shuts it down,
  3. `worldserver --version` and `authserver --version` run,
  4. the bundled Python runs `-I -m hslib --help`;
then moves the copy to "<scratch>/Second Place/Moonglade" and runs the same again.

--sandbox runs every payload command under `sandbox-exec` with a profile that denies file reads under /opt/homebrew
and /usr/local (and, with --deny-write, file writes under that folder except the scratch dir). The profile itself is
proven first: `/bin/ls /opt/homebrew` must fail under it. CI also moves Homebrew aside before running this.

Stdlib only, Python >= 3.9, so CI can run it with the payload's own Python while Homebrew is moved aside.
Exit 0 when every step passed; the JSON record has each command, its exit code and output tails.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
RESERVED_PORTS = {3306, 3307, 3317, 3724, 3734, 8085, 8095, 7878, 7888}


def profile(scratch: Path, deny_write: Path | None) -> str:
    rules = ['(version 1)', '(allow default)',
             '(deny file-read* (subpath "/opt/homebrew") (subpath "/usr/local"))']
    if deny_write:
        rules.append(f'(deny file-write* (subpath "{deny_write.resolve()}"))')
        rules.append(f'(allow file-write* (subpath "{scratch.resolve()}"))')
    return "\n".join(rules) + "\n"


def free_port() -> int:
    for _ in range(50):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        if port not in RESERVED_PORTS:
            return port
    raise RuntimeError("no free port")


class Runner:
    def __init__(self, sandbox_profile: Path | None, env: dict):
        self.prof = sandbox_profile
        self.env = env
        self.log: list[dict] = []

    def cmd(self, argv: list) -> list[str]:
        argv = [str(a) for a in argv]
        return [SANDBOX_EXEC, "-f", str(self.prof), *argv] if self.prof else argv

    def run(self, label: str, argv: list, cwd: Path, timeout: int = 120, expect: str | None = None) -> dict:
        t0 = time.time()
        try:
            r = subprocess.run(self.cmd(argv), capture_output=True, text=True, cwd=cwd, env=self.env, timeout=timeout)
            rc, out, err = r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired as e:
            rc, out, err = "timeout", str(e.stdout or ""), str(e.stderr or "")
        rec = {"step": label, "argv": [str(a) for a in argv], "cwd": str(cwd), "rc": rc,
               "seconds": round(time.time() - t0, 1), "stdout_tail": out[-600:], "stderr_tail": err[-600:]}
        if expect is not None:
            rec["expect"] = expect
            rec["expect_found"] = expect in out + err
        self.log.append(rec)
        return rec


def smoke_once(run: Runner, app: Path, work: Path, port: int) -> dict:
    """Steps 1-4 against the payload copy at `app`. Returns {"ok": bool, "steps": [...], "failed": [...]}."""
    steps, failed = [], []
    data, tmp = work / "mysql data", work / "tmp"
    for d in (data, tmp):
        shutil.rmtree(d, ignore_errors=True)
    tmp.mkdir(parents=True)
    sock = work / "my.sock"
    base = app / "mysql"
    common = ["--no-defaults", f"--basedir={base}", f"--datadir={data}", f"--tmpdir={tmp}",
              f"--lc-messages-dir={base / 'share'}"]
    r = run.run("mysqld --initialize-insecure", [base / "bin/mysqld", *common, "--initialize-insecure",
                                                 f"--log-error={work / 'init.err'}"], cwd=work, timeout=300)
    steps.append(r)
    if r["rc"] != 0:
        r["log_error_tail"] = (work / "init.err").read_text(errors="replace")[-800:] if (work / "init.err").exists() else ""
        failed.append(r["step"])
        return {"ok": False, "steps": steps, "failed": failed}
    errlog = work / "mysqld.err"
    srv = subprocess.Popen(run.cmd([base / "bin/mysqld", *common, f"--port={port}", "--bind-address=127.0.0.1",
                                    "--mysqlx=OFF", f"--socket={sock}", f"--pid-file={work / 'mysqld.pid'}",
                                    f"--log-error={errlog}", "--performance-schema=OFF", "--disable-log-bin"]),
                           cwd=work, env=run.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = [base / "bin/mysql", "--no-defaults", "--protocol=TCP", "-h127.0.0.1", f"-P{port}", "-uroot",
              "--batch", "--skip-column-names"]
    try:
        ok = False
        t0 = time.time()
        while time.time() - t0 < 90 and srv.poll() is None:
            probe = subprocess.run(run.cmd([*client, "-e", "SELECT 1"]), capture_output=True, text=True,
                                   env=run.env, cwd=work, timeout=30)
            if probe.returncode == 0:
                ok = True
                break
            time.sleep(1)
        start = {"step": "mysqld start", "rc": 0 if ok else 1, "seconds": round(time.time() - t0, 1),
                 "mysqld_exit": srv.poll()}
        if not ok:
            start["log_error_tail"] = errlog.read_text(errors="replace")[-800:] if errlog.exists() else ""
            failed.append("mysqld start")
        steps.append(start)
        if ok:
            r = run.run("mysql client query", [*client, "-e", "SELECT VERSION(), @@basedir, @@datadir"], cwd=work)
            r["ok"] = r["rc"] == 0 and r["stdout_tail"].strip().startswith("8.4.")
            steps.append(r)
            if not r["ok"]:
                failed.append(r["step"])
            r = run.run("mysql client shutdown", [*client, "-e", "SHUTDOWN"], cwd=work)
            steps.append(r)
            if r["rc"] != 0:
                failed.append(r["step"])
    finally:
        try:
            srv.wait(timeout=60)
        except subprocess.TimeoutExpired:
            srv.kill()
            srv.wait()
            failed.append("mysqld didn't stop after SHUTDOWN")
    for n in ("worldserver", "authserver"):
        r = run.run(f"{n} --version", [app / "server/bin" / n, "--version"], cwd=work, expect="AzerothCore")
        r["ok"] = r["rc"] == 0 and r["expect_found"]
        steps.append(r)
        if not r["ok"]:
            failed.append(r["step"])
    r = run.run("python -I -m hslib --help", [app / "python/bin/python3", "-I", "-m", "hslib", "--help"], cwd=work,
                expect="Moonglade harness")
    r["ok"] = r["rc"] == 0 and r["expect_found"]
    steps.append(r)
    if not r["ok"]:
        failed.append(r["step"])
    # and that hslib came from this payload copy (site-packages/moonglade-harness.pth), not from anywhere else
    r = run.run("hslib is the payload's", [app / "python/bin/python3", "-I", "-c", "import hslib; print(hslib.__file__)"],
                cwd=work)
    r["ok"] = r["rc"] == 0 and r["stdout_tail"].strip().startswith(str(app / "harness" / "hslib") + "/")
    steps.append(r)
    if not r["ok"]:
        failed.append(r["step"])
    return {"ok": not failed, "steps": steps, "failed": failed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="smoke.py")
    ap.add_argument("--payload", type=Path, required=True)
    ap.add_argument("--scratch", type=Path, required=True)
    ap.add_argument("--port", type=int)
    ap.add_argument("--sandbox", action="store_true")
    ap.add_argument("--deny-write", type=Path)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args(argv)
    scratch = a.scratch.resolve()
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    rec: dict = {"payload": str(a.payload), "scratch": str(scratch), "started": time.time()}
    prof = None
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(scratch), "TMPDIR": str(scratch) + "/",
           "LANG": "en_US.UTF-8"}
    if a.sandbox:
        prof = scratch / "deny-homebrew.sb"
        prof.write_text(profile(scratch, a.deny_write))
        rec["profile"] = prof.read_text()
        # the profile works: Homebrew is unreadable under it, and readable without it
        under = subprocess.run([SANDBOX_EXEC, "-f", str(prof), "/bin/ls", "/opt/homebrew"], capture_output=True,
                               text=True)
        rec["profile_proof"] = {"ls_opt_homebrew_rc_sandboxed": under.returncode,
                                "stderr": under.stderr.strip()[-200:],
                                "opt_homebrew_exists": os.path.isdir("/opt/homebrew")}
        if os.path.isdir("/opt/homebrew") and under.returncode == 0:
            rec["ok"] = False
            rec["failed"] = ["the sandbox profile doesn't deny reads of /opt/homebrew"]
            return _finish(a, rec)
    port = a.port or free_port()
    if port in RESERVED_PORTS:
        print(f"smoke: port {port} belongs to a Moonglade stack; pick another", file=sys.stderr)
        return 2
    rec["port"] = port
    run = Runner(prof, env)
    places = [scratch / "Place One", scratch / "Second Place"]
    app = places[0] / "Moonglade"
    places[0].mkdir()
    t0 = time.time()
    shutil.copytree(a.payload, app, symlinks=True)
    rec["copy_seconds"] = round(time.time() - t0, 1)
    rec["locations"] = []
    for i, place in enumerate(places):
        if i:
            place.mkdir()
            new = place / "Moonglade"
            os.rename(app, new)
            app = new
        res = smoke_once(run, app, place, port)
        rec["locations"].append({"path": str(app), **res})
    rec["failed"] = [f"{Path(loc['path']).parent.name}: {f}" for loc in rec["locations"] for f in loc["failed"]]
    rec["ok"] = not rec["failed"]
    shutil.rmtree(scratch / "Second Place" / "Moonglade", ignore_errors=True)
    return _finish(a, rec)


def _finish(a, rec) -> int:
    rec["finished"] = time.time()
    if a.json:
        a.json.parent.mkdir(parents=True, exist_ok=True)
        a.json.write_text(json.dumps(rec, indent=2) + "\n")
    for f in rec.get("failed", []):
        print(f"SMOKE FAILED: {f}", file=sys.stderr)
    print(f"smoke: {'ok' if rec.get('ok') else 'FAILED'}")
    return 0 if rec.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
