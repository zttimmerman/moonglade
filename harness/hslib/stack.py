"""Isolated mysqld + authserver + worldserver lifecycle (ADR-0002). State lives in env/ and data/ (lane A) or
env/lanes/b/ (lane B, ADR-0011): every path and port comes from paths.py's lane layer, and processes are matched by
their lane's config path (my.cnf, the -c conf), so one lane's down never touches the other lane."""
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from . import config, db, paths as P, procs, soap, srp6
from .util import log, tail

MYSQLD = P.MYSQLD                 # dev: Homebrew's; release: the payload's (paths.MYSQL_BASE)
MYSQLADMIN = P.MYSQLADMIN
MY_CNF = P.MY_CNF
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

def _conf_arg(argv: list[str]) -> str | None:
    """The value of `-c <conf>` (or `-c<conf>`, `--config <conf>`, `--config=<conf>`) in an argv, else None."""
    for i, a in enumerate(argv):
        if a in ("-c", "--config") and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--config="):
            return a.split("=", 1)[1]
        if a.startswith("-c") and len(a) > 2:
            return a[2:]
    return None


def stack_processes(lane: str | None = None) -> dict[str, list[int]]:
    """All processes belonging to *this repo's* stack in `lane` (default: this process's lane), found by command line
    (not just pidfiles): mysqld by its lane's --defaults-file, authserver/worldserver by the server binary plus their
    lane's `-c` conf (no -c = the compiled-in default, server/etc = lane A's)."""
    # procs.table() reads the kernel's process table directly: /bin/ps (setuid) can't run under sandbox-exec.
    L = P.layout(lane or P.LANE)
    my_cnf = f"--defaults-file={L['my_cnf']}"
    found = {"mysqld": [], "authserver": [], "worldserver": []}
    # argv from the kernel, not a joined command line: release HOME (~/Library/Application Support/...) and app paths
    # have spaces
    for pid, argv in procs.argv_table():
        cmd = " ".join(argv)
        if "mysqld" in cmd and my_cnf in argv:
            found["mysqld"].append(pid)
            continue
        for name in ("authserver", "worldserver"):
            if argv and argv[0] == str(P.SERVER_BIN / name):
                conf = _conf_arg(argv[1:]) or str(P.SERVER_ETC / f"{name}.conf")
                if conf == str(L["etc"] / f"{name}.conf"):
                    found[name].append(pid)
    return found


def all_lanes_processes() -> dict[str, dict[str, list[int]]]:
    """{lane: stack_processes(lane)} for every lane (nightly's "in use", the lane checks)."""
    return {lane: stack_processes(lane) for lane in P.LANES}


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

def my_cnf_text() -> str:
    """This lane's my.cnf. Release mode adds the payload MySQL's share dirs (error messages, character sets)."""
    release = (f"lc-messages-dir={P.MYSQL_BASE / 'share'}\n"
               f"character-sets-dir={P.MYSQL_BASE / 'share' / 'charsets'}\n") if P.MODE == "release" else ""
    return (
        "[mysqld]\n"
        f"basedir={P.MYSQL_BASE}\n"
        + release +
        f"datadir={P.MYSQL_DATA}\n"
        f"port={P.MYSQL_PORT}\n"
        "bind-address=127.0.0.1\n"
        "mysqlx=OFF\n"
        f"socket={P.RUN / 'mysql.sock'}\n"
        f"pid-file={PIDFILE['mysqld']}\n"
        f"log-error={LOG['mysqld']}\n"
        "innodb_buffer_pool_size=512M\n"
        # m2 performance budget (mysqld <= 1.0 GB): the defaults' performance_schema (~150 MB of instrumentation)
        # and 64 MB log buffer pushed mysqld's footprint to ~1.01 GB with the M0 profile (measured 2026-10-02);
        # nothing in the harness or AzerothCore reads performance_schema.
        "performance_schema=OFF\n"
        "innodb_log_buffer_size=16M\n"
        "max_connections=300\n"
        "skip-name-resolve\n"
        "max_allowed_packet=64M\n"
        "disable-log-bin\n"
    )


def _write_my_cnf():
    P.LANE_ROOT.mkdir(parents=True, exist_ok=True)
    P.RUN.mkdir(parents=True, exist_ok=True)
    P.LOGS.mkdir(parents=True, exist_ok=True)
    MY_CNF.write_text(my_cnf_text())


def mysql_ready() -> bool:
    try:
        return db.scalar("SELECT 1", root=True, timeout=5) == "1"
    except Exception:
        return False


# lane B: `hs lane init` (lanestack.init) is the only caller allowed to --initialize a fresh lane-B datadir; every
# other lane-B start clones the template (lanestack.clone_datadir) or refuses.
ALLOW_INITIALIZE_LANE_B = False


def mysql_up(timeout=120):
    _write_my_cnf()
    procs = stack_processes()["mysqld"]
    if procs:
        if _wait(mysql_ready, 30):
            return {"mysqld": "already running", "pids": procs}
        raise StackError(f"mysqld running (pids {procs}) but not answering", tail(LOG["mysqld"]))
    cloned = None
    if P.LANE != "a" and not (P.MYSQL_DATA / "mysql").exists():
        from . import lanestack
        if lanestack.template_ready():
            cloned = lanestack.clone_datadir()
        elif not ALLOW_INITIALIZE_LANE_B:
            raise StackError(f"lane {P.LANE.upper()} has no template datadir ({P.MYSQL_TEMPLATE}); "
                             "run `harness/hs lane init` inside a harness run")
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
    return {"mysqld": "started", "pid": proc.pid, **({"cloned_from_template": cloned} if cloned else {})}


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

SAFETY_AC_KEYS = ("AC_PLAYERBOTS_DATABASE_INFO", "AC_AI_PLAYERBOT_COMMAND_SERVER_PORT")


def spawn_env(name: str) -> dict:
    """The server's environment: ours without any AC_* variable, plus (lane B and release mode) the AC_* module/DB
    keys (config.lane_env; the worldserver reads module confs from lane A's server/etc/modules otherwise, and a
    release has none it could read). Release mode also sets OPENSSL_CONF to HOME's empty openssl.cnf, so OpenSSL never
    opens the build machine's compiled-in OPENSSLDIR (release-build "As built")."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AC_")}
    if P.LANE != "a" or P.MODE == "release":
        try:
            ac = config.lane_env()
        except config.ConfigError as e:
            raise StackError(str(e)) from e
        if P.MODE == "release" and P.firstlaunch_test().get("drop_ac_env"):
            # counterexample (test switch only): no module settings, so playerbots falls back to its defaults; the DB
            # info and the command-server port stay (never a default 3306 database, never a 0.0.0.0 listener)
            ac = {k: v for k, v in ac.items() if k in SAFETY_AC_KEYS}
        env |= ac
    if P.MODE == "release":
        env["OPENSSL_CONF"] = str(P.OPENSSL_CONF)
    return env


def _spawn(name: str):
    LOG[name].parent.mkdir(parents=True, exist_ok=True)
    env = spawn_env(name)
    # stdout is block-buffered when not a tty while stderr is not, so sharing one file makes the tail of the
    # log non-deterministic; keep them separate and always show both in failure excerpts.
    fh = open(LOG[name], "w")
    eh = open(ERRLOG[name], "w")
    P.SERVER_CWD.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([str(P.SERVER_BIN / name), "-c", str(P.LANE_ETC / f"{name}.conf")],
                            cwd=P.SERVER_CWD, stdin=subprocess.DEVNULL, stdout=fh, stderr=eh,
                            start_new_session=True, env=env)
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
    db.query(f"UPDATE realmlist SET address='127.0.0.1', localAddress='127.0.0.1', port={int(P.WORLD_PORT)}, "
             "gamebuild=12340, flag = flag & ~1 WHERE id=1", "auth")
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


def world_up(timeout, on_stage=None):
    procs = stack_processes()["worldserver"]
    if procs:
        if _wait(soap_ready, 30):
            return {"worldserver": "already running", "pids": procs}
        raise StackError(f"worldserver running (pids {procs}) but SOAP not answering", excerpt("worldserver"))
    proc = _spawn("worldserver")
    if on_stage:                    # worldserver.out was just truncated by _spawn: progress can follow it from here
        on_stage("world", "spawned", log=str(LOG["worldserver"]), pid=proc.pid)
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


PROFILE_FILE = P.RUN / "stack-profile.json"


def running_profile() -> dict | None:
    """The config profile the running worldserver was started with ({"profile", "digest", "pid"}), or None when
    no worldserver runs. A worldserver started before profiles existed (no record, or a record for another pid)
    counts as the default profile with an unknown digest."""
    ws = stack_processes()["worldserver"]
    if not ws:
        return None
    try:
        rec = json.loads(PROFILE_FILE.read_text())
    except (OSError, ValueError):
        rec = None
    if not rec or rec.get("pid") not in ws:
        return {"profile": config.DEFAULT_PROFILE, "digest": None, "pid": ws[0], "recorded": False}
    return rec | {"recorded": True}


def profile_matches(name: str) -> bool:
    rp = running_profile()
    return bool(rp) and rp["profile"] == name and rp.get("digest") == config.profile_digest(name)


def world_matches(name: str, population_n: int | None = None) -> bool:
    """The running worldserver was started with profile `name` and, for the population profile, at N =
    `population_n` (its recorded n and digest both match). False when no worldserver runs."""
    rp = running_profile()
    n = config.population_n(population_n) if name == "population" else None
    return (bool(rp) and rp["profile"] == name and rp.get("n") == n
            and rp.get("digest") == config.profile_digest(name, n))


def describe_world(rp: dict | None) -> str:
    """A running_profile() record in player terms, for messages."""
    if not rp:
        return "no world"
    if rp["profile"] == "population":
        return f"the living world with {rp.get('n')} AI players"
    return f"the {rp['profile']!r} test world" + ("" if rp.get("recorded") else " (started outside the harness)")


def up(world_datadir: Path | None = None, timeout: int | None = None, profile: str | None = None,
       population_n: int | None = None, on_stage=None) -> dict:
    """Start the stack with a config profile (config.PROFILES; default $HS_PROFILE or "m0"); `population_n` sizes
    the population profile (./play passes the user's world-size setting). If a worldserver is already running it
    is left alone (idempotent `hs up`); callers that need a specific profile use `ensure_profile`.
    on_stage(stage, state, **kw), if given, hears the loading stages (stages.py): database/login/world start and
    done, and world "spawned" (with the log path) once the worldserver process exists."""
    stage = on_stage or (lambda *a, **k: None)
    try:
        P.require_lane_allowed("starting the stack")     # lane B only inside a harness run (second-stack)
    except P.LaneRefused as e:
        raise StackError(str(e)) from e
    why = P.test_release_refusal()
    if why:
        raise StackError(f"starting the stack refused: {why}")
    if not (P.SERVER_BIN / "worldserver").exists():
        raise StackError("server not built; run `harness/hs build` first")
    if P.MODE == "dev":
        from .build import moved_problem
        moved = moved_problem()         # F30: a server built in another folder; refuse before starting anything
        if moved:
            raise StackError(moved["message"])
    name = config.profile_name(profile)
    was_running = bool(stack_processes()["worldserver"])
    if was_running:
        rp = running_profile()
        if rp["profile"] != name and (profile or os.environ.get("HS_PROFILE")):
            raise StackError(f"worldserver is already running with profile {rp['profile']!r}, not {name!r}; "
                             "`hs down` first (or use stack.ensure_profile)")
        name = rp["profile"]          # keep the rendered confs matching the running worldserver
        population_n = rp.get("n") if name == "population" else None
    first_run = not db_exists("world") if stack_processes()["mysqld"] else not (P.MYSQL_DATA / "acore_world").exists()
    timeout = timeout or (1800 if first_run else 300)
    n = config.population_n(population_n) if name == "population" else None
    info = {"first_run": first_run, "profile": name, "population_n": n,
            "config": list(config.write_all(world_datadir, profile=name, population_n=n))}
    stage("database", "start")
    info |= mysql_up()
    stage("database", "done")
    stage("login", "start")
    ensure_realmlist()          # before authserver: it refuses to start if every realm has flag 3
    info |= auth_up(timeout)
    ensure_realmlist()          # first run: auth DB only exists once authserver created it
    info["gm_account_id"] = ensure_gm_account()
    stage("login", "done")
    stage("world", "start", first_run=first_run)
    if name == "population" and not was_running and db_exists("playerbots"):
        from . import population      # "fresh world": re-roll random bots before they log in (population.py)
        info["fresh_start"] = population.fresh_start()
    w = world_up(timeout, on_stage=on_stage)
    info |= w
    stage("world", "done")
    if not was_running and w.get("worldserver") == "started":
        PROFILE_FILE.write_text(json.dumps({"profile": name, "n": n, "digest": config.profile_digest(name, n),
                                            "pid": w["pid"]}))
    return info


def ensure_profile(name: str, timeout: int | None = None) -> dict:
    """Make the running stack use profile `name`: restart the worldserver (only) if it runs with another profile
    (or a stale digest), start everything if it's down. Returns what was done."""
    rp = running_profile()
    if rp and profile_matches(name):
        return {"profile": name, "action": "already running"}
    out = {"profile": name, "was": rp}
    if rp:
        out["stop_worldserver"] = stop_worldserver(rp["pid"])
    out["up"] = up(profile=name, timeout=timeout)
    return out


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


def down(on_stage=None, before_database=None) -> dict:
    """Stop worldserver (graceful: saves the world), authserver, mysqld. `on_stage(sid, state, **kw)` (launcher-fixes:
    the shutdown window) hears "world" / "login" / "backup" / "database" with "start" + "done", or "skipped" when not
    running. save-backups: `before_database()`, if given, runs after the world and login servers have stopped and
    before mysqld stops (the play loop's backup on quit); it returns a dict with "ok" (False -> state "failed" with
    its "error") and is skipped when the world server is still running or mysqld isn't."""
    def note(sid, state, **kw):
        if on_stage:
            on_stage(sid, state, **kw)
    info = {}
    procs = stack_processes()
    note("world", "start" if procs["worldserver"] else "skipped")
    for pid in procs["worldserver"]:
        info["worldserver"] = stop_worldserver(pid)
    if procs["worldserver"]:
        note("world", "done")
    note("login", "start" if procs["authserver"] else "skipped")
    for pid in procs["authserver"]:
        _kill(pid, grace=15)
        info["authserver"] = {"pid": pid}
    if procs["authserver"]:
        note("login", "done")
    if before_database is None:
        note("backup", "skipped")
    else:
        now = stack_processes()
        if now["worldserver"] or now["authserver"] or not now["mysqld"]:
            why = ("the world or login server is still running" if now["worldserver"] or now["authserver"]
                   else "the database isn't running")
            info["backup"] = {"ok": False, "skipped": why}
            note("backup", "skipped", why=why)
        else:
            note("backup", "start")
            try:
                info["backup"] = before_database()
            except Exception as e:  # noqa: BLE001  (a hook never stops the shutdown)
                info["backup"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if info["backup"].get("ok"):
                note("backup", "done")
            else:
                note("backup", "failed", error=str(info["backup"].get("error"))[:300])
    note("database", "start" if procs["mysqld"] else "skipped")
    info |= mysql_down()
    if procs["mysqld"]:
        note("database", "done")
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
