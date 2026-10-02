"""Isolated mysqld + authserver + worldserver lifecycle (ADR-0002). State lives in env/ and data/."""
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from . import config, db, paths as P, procs, soap, srp6
from .util import log, tail

MYSQLD = P.BREW / "opt/mysql/bin/mysqld"
MYSQLADMIN = P.BREW / "opt/mysql/bin/mysqladmin"
MY_CNF = P.ENV / "my.cnf"
LOG = {
    "mysqld": P.LOGS / "mysqld.err",
    "authserver": P.LOGS / "authserver.out",
    "worldserver": P.LOGS / "worldserver.out",
}
ERRLOG = {
    "authserver": P.LOGS / "authserver.err",
    "worldserver": P.LOGS / "worldserver.err",
}


def excerpt(name: str, n=40) -> str:
    return tail(LOG[name], n) + f"\n----- {name} stderr -----\n" + tail(ERRLOG[name], 20)


PIDFILE = {
    "mysqld": P.RUN / "mysqld.pid",
    "authserver": P.RUN / "authserver.pid",
    "worldserver": P.RUN / "worldserver.pid",
}


class StackError(RuntimeError):
    def __init__(self, msg, log_excerpt=""):
        super().__init__(msg)
        self.log_excerpt = log_excerpt


# ---------------------------------------------------------------- process discovery

def stack_processes() -> dict[str, list[int]]:
    """All processes belonging to *this repo's* stack, found by command line (not just pidfiles)."""
    # procs.table() reads the kernel's process table directly: /bin/ps (setuid) can't run under sandbox-exec.
    found = {"mysqld": [], "authserver": [], "worldserver": []}
    for pid, cmd in procs.table():
        if str(MY_CNF) in cmd and "mysqld" in cmd:
            found["mysqld"].append(pid)
        elif cmd.startswith(str(P.SERVER_BIN / "authserver")):
            found["authserver"].append(pid)
        elif cmd.startswith(str(P.SERVER_BIN / "worldserver")):
            found["worldserver"].append(pid)
    return found


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not procs.is_zombie(pid)


def port_open(port: int, host=P.HOST, timeout=0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def listening_sockets(pids: list[int]) -> list[str]:
    """`lsof` listen sockets for the given pids, as 'pid name addr:port'."""
    if not pids:
        return []
    r = subprocess.run(["lsof", "-nP", "-a", "-iTCP", "-sTCP:LISTEN", "-p", ",".join(map(str, pids))],
                       capture_output=True, text=True)
    rows = []
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        rows.append(f"{parts[1]} {parts[0]} {parts[8]}")
    return rows


def _wait(pred, timeout, interval=1.0, watch_pid=None, what=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        if watch_pid is not None and not alive(watch_pid):
            return False
        time.sleep(interval)
    return pred()


# ---------------------------------------------------------------- mysql

def _write_my_cnf():
    P.RUN.mkdir(parents=True, exist_ok=True)
    P.LOGS.mkdir(parents=True, exist_ok=True)
    MY_CNF.write_text(
        "[mysqld]\n"
        f"basedir={P.BREW / 'opt/mysql'}\n"
        f"datadir={P.MYSQL_DATA}\n"
        f"port={P.MYSQL_PORT}\n"
        "bind-address=127.0.0.1\n"
        "mysqlx=OFF\n"
        f"socket={P.RUN / 'mysql.sock'}\n"
        f"pid-file={PIDFILE['mysqld']}\n"
        f"log-error={LOG['mysqld']}\n"
        "innodb_buffer_pool_size=512M\n"
        "max_connections=300\n"
        "skip-name-resolve\n"
        "max_allowed_packet=64M\n"
        "disable-log-bin\n"
    )


def mysql_ready() -> bool:
    try:
        return db.scalar("SELECT 1", root=True, timeout=5) == "1"
    except Exception:
        return False


def mysql_up(timeout=120):
    _write_my_cnf()
    procs = stack_processes()["mysqld"]
    if procs:
        if _wait(mysql_ready, 30):
            return {"mysqld": "already running", "pids": procs}
        raise StackError(f"mysqld running (pids {procs}) but not answering", tail(LOG["mysqld"]))
    if not (P.MYSQL_DATA / "mysql").exists():
        P.MYSQL_DATA.mkdir(parents=True, exist_ok=True)
        log("initializing repo-local MySQL datadir")
        r = subprocess.run([MYSQLD, f"--defaults-file={MY_CNF}", "--initialize-insecure"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise StackError("mysqld --initialize-insecure failed", r.stdout + r.stderr + tail(LOG["mysqld"]))
    proc = subprocess.Popen([MYSQLD, f"--defaults-file={MY_CNF}"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    if not _wait(mysql_ready, timeout, watch_pid=proc.pid):
        raise StackError("mysqld did not become ready", tail(LOG["mysqld"]))
    db.query(
        f"CREATE USER IF NOT EXISTS '{P.DB_USER}'@'localhost' IDENTIFIED BY '{P.DB_PASS}';"
        f"CREATE USER IF NOT EXISTS '{P.DB_USER}'@'127.0.0.1' IDENTIFIED BY '{P.DB_PASS}';"
        f"GRANT ALL PRIVILEGES ON *.* TO '{P.DB_USER}'@'localhost' WITH GRANT OPTION;"
        f"GRANT ALL PRIVILEGES ON *.* TO '{P.DB_USER}'@'127.0.0.1' WITH GRANT OPTION;"
        "FLUSH PRIVILEGES;", root=True)
    return {"mysqld": "started", "pid": proc.pid}


def mysql_down(timeout=60):
    procs = stack_processes()["mysqld"]
    if not procs:
        return {"mysqld": "not running"}
    subprocess.run([MYSQLADMIN, f"--defaults-file={db.client_cnf(root=True)}", "shutdown"],
                   capture_output=True, text=True)
    if not _wait(lambda: not any(alive(p) for p in procs), timeout):
        for p in procs:
            os.kill(p, signal.SIGTERM)
        _wait(lambda: not any(alive(p) for p in procs), 30)
        return {"mysqld": "stopped (SIGTERM fallback)", "graceful": False}
    return {"mysqld": "stopped", "graceful": True}


# ---------------------------------------------------------------- servers

def _spawn(name: str):
    LOG[name].parent.mkdir(parents=True, exist_ok=True)
    # stdout is block-buffered when not a tty while stderr is not, so sharing one file makes the tail of the
    # log non-deterministic; keep them separate and always show both in failure excerpts.
    fh = open(LOG[name], "w")
    eh = open(ERRLOG[name], "w")
    proc = subprocess.Popen([str(P.SERVER_BIN / name), "-c", str(P.SERVER_ETC / f"{name}.conf")],
                            cwd=P.SERVER_BIN, stdin=subprocess.DEVNULL, stdout=fh, stderr=eh,
                            start_new_session=True)
    PIDFILE[name].write_text(str(proc.pid))
    return proc


def soap_ready() -> bool:
    try:
        return soap.gm("server info", timeout=10).ok
    except OSError:
        return False


def db_exists(name: str) -> bool:
    try:
        return db.scalar(f"SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='{P.DB_NAMES[name]}'") == "1"
    except db.SQLError:
        return False


def ensure_gm_account():
    """Bootstrap the SOAP GM account (the only direct-insert account; see design.md)."""
    acct = db.scalar(f"SELECT id FROM account WHERE username='{P.GM_USER}'", "auth")
    if acct is None:
        salt, ver = srp6.make_verifier(P.GM_USER, P.GM_PASS)
        db.query(f"INSERT INTO account (username, salt, verifier, expansion, joindate) "
                 f"VALUES ('{P.GM_USER}', 0x{salt.hex()}, 0x{ver.hex()}, 2, NOW())", "auth")
        acct = db.scalar(f"SELECT id FROM account WHERE username='{P.GM_USER}'", "auth")
    db.query(f"REPLACE INTO account_access (id, gmlevel, RealmID, comment) VALUES ({acct}, 3, -1, 'harness GM')", "auth")
    return int(acct)


def ensure_realmlist():
    """Pin realm 1 to localhost/12340 and clear a stale VERSION_MISMATCH bit (0x01).

    worldserver sets that bit at startup and clears it only once ready (worldserver/Main.cpp); a worldserver that
    dies mid-startup leaves it set, and the next authserver start ORs in OFFLINE (authserver/Main.cpp) -> flag 3,
    which authserver treats as "no valid realms" and exits. Safe: worldserver re-sets it on its own start."""
    if not db_exists("auth"):
        return False
    db.query("UPDATE realmlist SET address='127.0.0.1', localAddress='127.0.0.1', port=8085, gamebuild=12340, "
             "flag = flag & ~1 WHERE id=1", "auth")
    return True


def auth_up(timeout):
    procs = stack_processes()["authserver"]
    if procs:
        if _wait(lambda: port_open(P.AUTH_PORT), 15):
            return {"authserver": "already running", "pids": procs}
        raise StackError(f"authserver running (pids {procs}) but port {P.AUTH_PORT} closed", excerpt("authserver"))
    proc = _spawn("authserver")
    if not _wait(lambda: port_open(P.AUTH_PORT), timeout, watch_pid=proc.pid):
        try:
            rc = proc.wait(timeout=10)   # reap before reading the log so the final (stderr) lines are on disk
        except subprocess.TimeoutExpired:
            rc = None
        exc = excerpt("authserver") + "\n----- Auth.log -----\n" + tail(P.LOGS / "Auth.log", 15)
        _kill(proc.pid)
        raise StackError("authserver failed to become ready" + (f" (process exited, rc={rc})" if rc is not None else " (timeout)"), exc)
    return {"authserver": "started", "pid": proc.pid}


def world_up(timeout):
    procs = stack_processes()["worldserver"]
    if procs:
        if _wait(soap_ready, 30):
            return {"worldserver": "already running", "pids": procs}
        raise StackError(f"worldserver running (pids {procs}) but SOAP not answering", excerpt("worldserver"))
    proc = _spawn("worldserver")
    t0 = time.time()
    ok = _wait(soap_ready, timeout, interval=2.0, watch_pid=proc.pid)
    if not ok:
        try:
            rc = proc.wait(timeout=10)   # reap before reading the log so the final (stderr) lines are on disk
        except subprocess.TimeoutExpired:
            rc = None
        exc = excerpt("worldserver")
        _kill(proc.pid)
        raise StackError(f"worldserver failed to become ready after {time.time() - t0:.0f}s"
                         + (f" (process exited, rc={rc})" if rc is not None else " (timeout)"), exc)
    return {"worldserver": "started", "pid": proc.pid, "ready_s": round(time.time() - t0, 1)}


def _kill(pid, grace=10):
    if not alive(pid):
        return
    os.kill(pid, signal.SIGTERM)
    if not _wait(lambda: not alive(pid), grace, interval=0.5):
        os.kill(pid, signal.SIGKILL)
        _wait(lambda: not alive(pid), 5, interval=0.2)


def up(world_datadir: Path | None = None, timeout: int | None = None) -> dict:
    if not (P.SERVER_BIN / "worldserver").exists():
        raise StackError("server not built; run `harness/hs build` first")
    first_run = not db_exists("world") if stack_processes()["mysqld"] else not (P.MYSQL_DATA / "acore_world").exists()
    timeout = timeout or (1800 if first_run else 300)
    info = {"first_run": first_run, "config": list(config.write_all(world_datadir))}
    info |= mysql_up()
    ensure_realmlist()          # before authserver: it refuses to start if every realm has flag 3
    info |= auth_up(timeout)
    ensure_realmlist()          # first run: auth DB only exists once authserver created it
    info["gm_account_id"] = ensure_gm_account()
    info |= world_up(timeout)
    return info


def stop_worldserver(pid: int) -> dict:
    """Graceful worldserver shutdown (SOAP `server shutdown 1`), SIGTERM/SIGKILL fallback. Also used to swap in the
    unpatched baseline worldserver (core.login_logout_race_baseline) while mysqld + authserver keep running."""
    graceful = True
    try:
        soap.gm("server shutdown 1", timeout=10)
    except OSError:
        os.kill(pid, signal.SIGTERM)
    if not _wait(lambda: not alive(pid), 90):
        os.kill(pid, signal.SIGTERM)
        if not _wait(lambda: not alive(pid), 30):
            os.kill(pid, signal.SIGKILL)
            graceful = False
    return {"pid": pid, "graceful": graceful}


def down() -> dict:
    info = {}
    procs = stack_processes()
    for pid in procs["worldserver"]:
        info["worldserver"] = stop_worldserver(pid)
    for pid in procs["authserver"]:
        _kill(pid, grace=15)
        info["authserver"] = {"pid": pid}
    info |= mysql_down()
    for f in PIDFILE.values():
        if f.exists():
            f.unlink()
    info["remaining"] = {k: v for k, v in stack_processes().items() if v}
    return info


def status() -> dict:
    procs = stack_processes()
    return {
        "processes": procs,
        "ports": {p: port_open(p) for p in P.STACK_PORTS},
        "mysql_ready": bool(procs["mysqld"]) and mysql_ready(),
        "soap_ready": bool(procs["worldserver"]) and soap_ready(),
    }
