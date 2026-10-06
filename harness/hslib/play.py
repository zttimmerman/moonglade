"""`./play`: the one-command offline play loop (openspec/changes/m1-offline-play-loop).

up → client-config → fixtures → start Wow.exe under Wine (ADR-0004) → block until the client exits →
close the Wine prefix → graceful `stack.down()` → exit 0. Everything is composed from launch/stack/fixtures.

Exit codes: 0 client quit normally and everything stopped; 1 startup or other failure; 2 another ./play is
running; 3 the client crashed or was killed; 128+signal after SIGINT/SIGTERM/SIGHUP.

Signals: the first SIGINT/SIGTERM/SIGHUP aborts whatever is running (startup or the wait) and goes to cleanup.
Cleanup sets those signals to SIG_IGN, which the cleanup's own subprocesses (mysqladmin, ps, lsof,
wineserver -k) inherit, so a second Ctrl-C can't interrupt or double-kill anything.

Single instance: env/run/play.lock, `fcntl.flock`ed for the life of the loop and holding its pid. The kernel
drops a flock when its holder dies, so a lock file left by a killed ./play is simply re-locked (reclaimed).

Login: the player's own account, never the harness test account (every full harness run deletes and recreates
HSTEST). Its name and password live in env/player.json (gitignored); the first ./play writes it (account PLAYER,
random password) and creates the account through the server (SOAP `account create`; not a GM; no character,
the player makes one at character select). Nothing in the harness touches that account.

Test-only: `--account test` uses the harness test account + character instead (the play.* checks).
`--client-cmd FILE` runs FILE (a Windows .bat/.cmd/.exe, outside client/) instead of Wow.exe under
the same Wine layer and prefix, so harness checks can drive the loop without the GUI. `--world-datadir`
forwards to `stack.up` to provoke a startup failure. Each run writes env/run/play-last.json (harness evidence).

Client (in-client-login, ADR-0009): the setting `client` (env/settings.json, or $HS_SETTINGS; `hs client-mode`) picks
the game client. `patched` (default): the play client env/client-play/, (re)built from client/ + patches/client/ by
`clientbuild.build` (a no-op when up to date), whose login screen lists the profiles and logs in by itself; the
profile file with the login is generated into the play client at each start and removed at cleanup (frontmenu.py).
`stock`: the untouched client/, exactly as before (type the login). Test-only: $HS_PLAY_CLIENT_DIR builds/uses a
scratch play client instead of env/client-play/ (needs --allow-test-client-dir), $HS_PLAY_SKIP_PATCHES leaves
patches out of that scratch build (counterexamples), $HS_GLUE_TEST passes the GlueXML test hook (frontmenu.py).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import secrets
import signal
import string
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import launch, paths as P, stack

LOCK = P.RUN / "play.lock"
PLAYER_FILE = P.ENV / "player.json"
DEFAULT_PLAYER = "PLAYER"


def player_file() -> Path:
    """env/player.json; HS_PLAYER_FILE overrides it (test-only: play.player_account uses a scratch file)."""
    return Path(os.environ["HS_PLAYER_FILE"]) if os.environ.get("HS_PLAYER_FILE") else PLAYER_FILE


def default_player() -> str:
    """Account name for a newly written player file; HS_PLAYER_DEFAULT_NAME overrides it (test-only)."""
    return os.environ.get("HS_PLAYER_DEFAULT_NAME") or DEFAULT_PLAYER
_NAME_RE = re.compile(r"^[A-Za-z0-9]{1,16}$")          # AccountMgr: name < 17, password <= 16 chars
LAST = P.RUN / "play-last.json"
HANDLED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

EXIT_OK, EXIT_FAIL, EXIT_BUSY, EXIT_CRASH = 0, 1, 2, 3


def say(msg: str) -> None:
    from .stages import write_line        # one write per line: the world-progress thread prints [stage] lines too
    write_line(f"[play] {msg}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Interrupted(BaseException):
    """Raised (once) by the signal handler. BaseException so library `except Exception` blocks don't eat it."""

    def __init__(self, signum: int):
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


class Busy(Exception):
    def __init__(self, pid: str):
        super().__init__(pid)
        self.pid = pid


# ---------------------------------------------------------------- single-instance lock

def acquire_lock() -> tuple[int, dict]:
    """flock env/run/play.lock (non-blocking) and write our pid into it. Raises Busy(pid of the holder)."""
    P.RUN.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = ""
        for _ in range(10):          # the holder writes its pid right after locking; allow for that instant
            os.lseek(fd, 0, os.SEEK_SET)
            holder = os.read(fd, 64).decode(errors="replace").strip()
            if holder:
                break
            time.sleep(0.1)
        os.close(fd)
        raise Busy(holder or "unknown")
    prev = os.read(fd, 64).decode(errors="replace").strip()
    info = {"path": str(LOCK), "pid": os.getpid()}
    if prev:                          # a previous ./play died without releasing: the kernel already dropped its flock
        info["reclaimed_stale_pid"] = prev
        info["stale_pid_alive"] = prev.isdigit() and stack.alive(int(prev))
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"{os.getpid()}\n".encode())
    os.fsync(fd)
    return fd, info


def release_lock(fd: int) -> None:
    # Truncate (not unlink: unlinking a flocked path lets a racing ./play lock a different inode), then unlock.
    try:
        os.ftruncate(fd, 0)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------------------------------------------------------------- the player's login

class PlayerAccountError(Exception):
    pass


def player_login() -> tuple[dict, bool]:
    """(login, newly_written) from env/player.json, writing defaults (PLAYER + random password) on first use."""
    pf = player_file()
    if pf.exists():
        try:
            d = json.loads(pf.read_text())
            login = {"account": str(d["account"]), "password": str(d["password"])}
        except (ValueError, KeyError, TypeError) as e:
            raise PlayerAccountError(f"{pf} is not valid ({e}); fix it or delete it to get a new login")
        new = False
    else:
        pw = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(10))
        login, new = {"account": default_player(), "password": pw}, True
    if login["account"].upper() in (P.TEST_USER.upper(), P.GM_USER.upper(), P.COMP_USER.upper()):
        raise PlayerAccountError(f"{pf}: {login['account']} is reserved for the test harness, pick another name")
    for k in ("account", "password"):
        if not _NAME_RE.match(login[k]):
            raise PlayerAccountError(f"{pf}: {k} must be 1-16 letters or digits")
    if new:
        pf.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(pf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(login, f, indent=2)
            f.write("\n")
    return login, new


def ensure_player_account(login: dict) -> dict:
    """Create the player's account through the server if it doesn't exist. Never changes an existing one."""
    from . import db, fixtures, soap
    name = login["account"].upper()

    def acct_id():
        return db.scalar(f"SELECT id FROM account WHERE username='{name}'", "auth")
    existing = acct_id()
    if existing:
        return {"account": name, "created": False, "id": int(existing)}
    r = soap.gm(f"account create {login['account']} {login['password']}")
    acct, ok, _ = fixtures.poll(acct_id, bool, timeout=15)
    if not ok:
        raise PlayerAccountError(f"couldn't create account {name}: {r.output.strip()}")
    return {"account": name, "created": True, "id": int(acct)}


# ---------------------------------------------------------------- client

def client_command(client_cmd: Path | None) -> tuple[list[str] | None, Path | None, str]:
    """(args for launch.start_client, cwd, name to find the client process by)."""
    if client_cmd is None:
        return None, None, "wow.exe"
    f = client_cmd.resolve()
    if not f.is_file():
        raise launch.LaunchError(f"--client-cmd: no such file {f}")
    if P.CLIENT.resolve() in f.parents:
        raise launch.LaunchError("--client-cmd stand-ins must live outside client/ (client.inspect forbids added binaries)")
    win = "Z:" + str(f).replace("/", "\\")        # Wine maps Z: to /
    args = ["cmd", "/c", win] if f.suffix.lower() in (".bat", ".cmd") else [win]
    return args, f.parent, f.name.lower()


def client_processes(name: str) -> list[dict]:
    if name == "wow.exe":
        return launch.wow_processes()
    return [p for p in launch.prefix_processes()
            if name in p["cmd"].lower() and "start.exe" not in p["cmd"].lower()]


PLAY_CLIENT_ENV = "HS_PLAY_CLIENT_DIR"
GLUE_TEST_ENV = "HS_GLUE_TEST"        # test-only GlueXML hook (frontmenu.py); needs --allow-test-client-dir, test account
SKIP_ENV = "HS_PLAY_SKIP_PATCHES"      # test-only (counterexamples): build the scratch play client without these patches


FROM_APP_ENV = launch.FROM_APP_ENV     # set by Moonglade.app (launcher.m); its output goes to env/logs/app.log
WINDOW_POLL_S = 0.5                    # launching -> ready: how often to look for the client's first window
BOTS_POLL_S = 2.0                      # how often the launching stage refreshes the (informational) bot count
READY_FALLBACK_S = 120                 # a client with no window (the .bat stand-in) is "ready" after this long


def shown_password(login: dict) -> str:
    """The password as ./play prints it: in a terminal, the password (the player needs it to type it in); in an app
    run, where every line lands in env/logs/app.log, a pointer to the file that holds it instead."""
    if os.environ.get(FROM_APP_ENV):
        return "(see harness/hslib/paths.py TEST_PASS)" if login["account"].upper() == P.TEST_USER \
            else f"(in {player_file()})"
    return login["password"]


def play_client_dir() -> Path:
    """env/client-play/, or $HS_PLAY_CLIENT_DIR (test-only scratch play client; ./play needs --allow-test-client-dir)."""
    from .clientbuild import PLAY
    v = os.environ.get(PLAY_CLIENT_ENV)
    return Path(v).resolve() if v else PLAY


def choose_world(say=say) -> dict:
    """Which world ./play starts: the living world at the user's world size (env/settings.json, or $HS_SETTINGS),
    unless $HS_PROFILE pins a profile (M2 2.2). Loop.run passes the result to launch.prepare, whose stack step is
    launch.ensure_world; pop.size_setting drives these same two functions."""
    from . import settings
    world = settings.play_world()
    if world["profile"] == "population" and isinstance(world["source"], dict):   # not for a pinned $HS_PROFILE
        say(f"World size: {world['source']['setting']} ({world['population_n']} AI players; change it with "
            "`harness/hs world-size small|normal|large|<number>`).")
    return world


# Signs of a harness (or developer test) run rather than the player starting the game: test arguments, and the
# environment switches the harness sets (HS_HARNESS_RUN is set by `hs run` for every check's subprocesses).
HARNESS_ENV = ("HS_HARNESS_RUN", "HS_PLAYER_FILE", "HS_PLAYER_DEFAULT_NAME", "HS_SETTINGS", "HS_PROFILE",
               "HS_CLIENT_DIR", "HS_GLUE_TEST", "HS_PLAY_SKIP_PATCHES")


def harness_signs(args, environ=None) -> list[str]:
    """Why this ./play isn't a genuine player launch ([] = it is)."""
    environ = os.environ if environ is None else environ
    signs = [f"--{k.replace('_', '-')}" for k in ("client_cmd", "world_datadir") if getattr(args, k, None)]
    if getattr(args, "allow_test_client_dir", False):
        signs.append("--allow-test-client-dir")
    if getattr(args, "account", "player") != "player":
        signs.append(f"--account {args.account}")
    signs += [f"${k}" for k in HARNESS_ENV if environ.get(k)]
    signs += [f"${k}" for k in sorted(environ) if k == "HSDOCK_TEST" or k.startswith("HSDOCK_TEST_")]
    return signs


def resolution_gate(args, client: Path, environ=None) -> dict:
    """launcher-fixes: may this launch write the resolution default into `client`? Scratch play clients: yes. The
    player's own env/client-play/: only from a genuine player launch."""
    from .clientbuild import PLAY as PLAYER_CLIENT
    own = Path(client).resolve() == PLAYER_CLIENT.resolve()
    signs = harness_signs(args, environ)
    return {"players_own_client": own, "harness_signs": signs, "apply": not (own and signs)}


class Loop:
    def __init__(self, args):
        self.args = args
        self.signum: int | None = None
        self.cleaning = False
        self.rec: dict = {"pid": os.getpid(), "started": _now(), "argv": sys.argv[1:],
                          "client_cmd": str(args.client_cmd) if args.client_cmd else None,
                          "client_started": False, "steps": []}
        from .stages import Shutdown, Stages
        self.stages = Stages()
        self.shutdown = Shutdown()

    # -- signals
    def on_signal(self, signum, _frame):
        if self.cleaning or self.signum is not None:
            return
        self.signum = signum
        self.rec["signal"] = {"name": signal.Signals(signum).name, "at": _now()}
        raise Interrupted(signum)

    # -- which game client (in-client-login): the play client (patched) or client/ (stock)
    def select_client(self) -> dict:
        from . import clientbuild, settings
        if os.environ.get("HS_CLIENT_DIR"):
            info = {"mode": "override", "dir": str(launch.client_dir()), "why": "HS_CLIENT_DIR (test-only)"}
        else:
            mode = settings.client_mode()["mode"]
            if mode == "patched":
                dest = play_client_dir()
                skip = tuple(x for x in os.environ.get(SKIP_ENV, "").split(",") if x)
                if skip and not os.environ.get(PLAY_CLIENT_ENV):
                    raise clientbuild.ClientBuildError(f"${SKIP_ENV} is test-only and needs ${PLAY_CLIENT_ENV}")
                b = clientbuild.build(dest=dest, skip=skip)
                launch.select_client(dest)
                info = {"mode": mode, "dir": str(dest), "built": b["built"], "reason": b.get("reason"),
                        "outputs": b["stamp"]["outputs"], "state": b["stamp"].get("state")}
                if b["built"]:
                    say("Prepared the game client (play client with the Moonglade login screen).")
            else:
                launch.select_client(None)
                info = {"mode": mode, "dir": str(P.CLIENT)}
        # a login file left behind by a killed ./play (SIGKILL skips cleanup) goes before anything else happens
        from . import frontmenu
        stale = [frontmenu.remove(d) for d in {play_client_dir(), launch.client_dir()}
                 if (d / "hs-build.json").is_file()]
        info["stale_profiles_removed"] = [x for x in stale if x.get("removed")]
        self.rec["client"] = info
        return info

    def step(self, name: str, **kw):
        self.rec["steps"].append({"step": name, "at": _now(), **kw})

    # -- the loop
    def run(self) -> int:
        args_, cwd, name = client_command(self.args.client_cmd)
        if self.args.account == "test":
            login, accounts = {"account": P.TEST_USER, "password": P.TEST_PASS, "character": P.TEST_CHAR}, None
            self.rec["account"] = {"kind": "test", "account": P.TEST_USER}
        else:
            login, new = player_login()
            self.rec["account"] = {"kind": "player", "account": login["account"].upper(), "file": str(player_file()),
                                   "file_created": new}
            if new:
                say(f"Created your personal login (saved in {player_file()}, keep it):")
                print(f"\n      Account:  {login['account']}\n      Password: {shown_password(login)}\n", flush=True)

            def accounts():
                say(f"Making sure your account ({login['account']}) exists...")
                return ensure_player_account(login)
        say("Starting Moonglade.")
        self.step("prepare")
        st = self.stages
        st.start("prepare")
        client = self.select_client()
        world = choose_world()
        self.rec["world"] = world
        st.profile = world["profile"]
        st.world_lines_of = st._world_lines_of()
        # the player's own play client gets the resolution default only from a genuine player launch (the app, or
        # ./play with no test arguments); harness runs never write it there. Scratch play clients
        # ($HS_PLAY_CLIENT_DIR) get it as a player's would (client.resolution_default).
        gate = resolution_gate(self.args, launch.client_dir())
        self.rec["resolution_gate"] = gate
        man, ev = launch.prepare(say=say, world_datadir=self.args.world_datadir, accounts=accounts,
                                 profile=world["profile"], population_n=world["population_n"], stages=st,
                                 set_resolution=gate["apply"])
        self.rec["prepare"] = {k: ev[k] for k in ("restarted_world", "stack", "fixtures", "accounts", "prefix",
                                                  "binaries_match_manifest_before", "resolution") if k in ev}
        self.man = man

        from .clientinst import sha256_file
        self.rec["client"]["exe"] = str(launch.client_dir() / "Wow.exe")
        self.rec["client"]["exe_sha256"] = sha256_file(launch.client_dir() / "Wow.exe")
        auto = client["mode"] == "patched"
        if auto:
            from . import frontmenu
            self.profiles_dir = launch.client_dir()
            # the GlueXML test hook only ever reaches a test-account run (main() also refuses it without the flag)
            hook = frontmenu.test_hook_from_env() if self.args.account == "test" else None
            self.rec["client"]["profiles"] = frontmenu.write(
                self.profiles_dir, frontmenu.with_context([{"name": login["account"].upper(),
                                                           "account": login["account"],
                                                           "password": login["password"]}]),
                default=login["account"], test=hook, about=frontmenu.about_info())
        st.start("launching")
        say("Starting the game client..." if args_ is None else f"Starting the test stand-in client ({name})...")
        proc = launch.start_client(args_, cwd)
        t_client = time.time()
        self.rec["client_started"] = True
        self.step("client_started", launcher_pid=proc.pid, args=args_ or ["Wow.exe"])
        if auto:
            say(f"The game is starting. On its login screen, press Continue (or Enter) to play as "
                f"{login['account'].upper()}; nothing to type.")
        else:
            say(f"The game is starting. Log in with account {login['account']} and password {shown_password(login)}"
                + (f" (character {login['character']})." if "character" in login else
                   ". Create a character on the realm if you don't have one yet."))
        say("When you quit the game, everything shuts down by itself. Press Ctrl-C here to stop everything.")

        # Wine runs the client as `start.exe /exec <program>`, which waits for the program and returns its
        # Windows exit code; then make sure no client process outlived it. Until the client's first window is on
        # screen, the launching stage polls for it (and the bot count, informational); then `ready`.
        ready, next_win, next_bots = False, 0.0, time.time() + BOTS_POLL_S
        while proc.poll() is None:
            now = time.time()
            if not ready and now >= next_win:
                next_win = now + WINDOW_POLL_S
                win = launch.client_window()
                if win:
                    ready = True
                    self.rec["client_window"] = {**win, "after_client_start_s": round(time.time() - t_client, 2)}
                    st.start("ready", window=win, after_client_start_s=self.rec["client_window"]["after_client_start_s"])
                    st.done("ready")
                    st.save_calibration()
                elif now - t_client > READY_FALLBACK_S:        # a windowless stand-in: don't keep the bar waiting
                    ready = True
                    st.start("ready", window=None, after_client_start_s=round(now - t_client, 2))
                    st.done("ready")
            if not ready and now >= next_bots:
                next_bots = now + BOTS_POLL_S
                st.extra["bots"] = launch.bot_count()
                st.progress("launching", None)
            time.sleep(0.5)
        seen, misses = [], 0
        while misses < 2:                  # debounce: absent in two consecutive samples, 1 s apart
            left = client_processes(name)
            if left:
                seen, misses = left, 0
            else:
                misses += 1
            if misses < 2:
                time.sleep(1)
        rc = proc.returncode
        self.rec["client_exit"] = {"code": rc, "at": _now(), "outlived_launcher": seen}
        self.step("client_exited", code=rc)
        if rc == 0:
            say("The game client has closed.")
            self.rec["outcome"] = "client_exit"
            return EXIT_OK
        why = (f"was killed by {signal.Signals(-rc).name}" if rc < 0
               else f"exited with error code {rc} (crashed or was killed)")
        say(f"The game client crashed: it {why}.")
        self.rec["outcome"] = "client_crash"
        self.rec["crash_message"] = f"client crash: the client {why}"
        return EXIT_CRASH

    def cleanup(self) -> list:
        """Close the client (Wine prefix) and stop the stack gracefully. Returns whatever is still running."""
        say("Shutting down... (closing the game and stopping the server; this takes up to a minute)")
        self.step("cleanup_start")
        sd = self.shutdown                  # [shutdown] lines: the launcher's shutdown window (launcher-fixes)
        clean: dict = {}
        if launch.prefix_processes():
            sd.send("shutdown_game", "start")
            clean["stop_prefix"] = launch.stop_prefix()
            sd.send("shutdown_game", "done")
        else:
            sd.send("shutdown_game", "skipped")
        if getattr(self, "profiles_dir", None):        # the generated login file lives only while the game runs
            from . import frontmenu
            clean["profiles_removed"] = frontmenu.remove(self.profiles_dir)
        clean["down"] = stack.down(on_stage=sd.on_stack)
        remaining = self.leftovers()
        if remaining:                       # one more pass, e.g. a component that came up during an aborted start
            clean["down_retry"] = stack.down()
            if launch.prefix_processes():
                clean["stop_prefix_retry"] = launch.stop_prefix()
            remaining = self.leftovers()
        clean["remaining"] = remaining
        if self.rec.get("client_started") and getattr(self, "man", None):
            clean["binaries_match_manifest_after"] = launch._binary_hashes(self.man) == launch.expected_binaries(self.man)
        self.rec["cleanup"] = clean
        self.step("cleanup_done")
        return remaining

    @staticmethod
    def leftovers() -> list:
        st = stack.stack_processes()
        return ([{"component": k, "pids": v} for k, v in st.items() if v]
                + [{"component": "wine", **p} for p in launch.prefix_processes()]
                + [{"component": "port", "port": p} for p in P.STACK_PORTS if stack.port_open(p)])


def _parse(argv):
    ap = argparse.ArgumentParser(prog="./play", description="Start Moonglade: the local server and the game "
                                 "client. Quit the game (or press Ctrl-C) and everything shuts down.")
    ap.add_argument("--client-cmd", type=Path, help=argparse.SUPPRESS)      # test-only stand-in client
    ap.add_argument("--account", choices=["player", "test"], default="player", help=argparse.SUPPRESS)  # test-only
    ap.add_argument("--world-datadir", type=Path, help=argparse.SUPPRESS)   # test-only: provoke a startup failure
    # test-only: accept HS_CLIENT_DIR (a scratch client copy); without it ./play refuses to run when the variable is set
    ap.add_argument("--allow-test-client-dir", action="store_true", help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = _parse(sys.argv[1:] if argv is None else argv)
    if os.environ.get("HS_CLIENT_DIR") and not args.allow_test_client_dir:
        say(f"HS_CLIENT_DIR is set ({os.environ['HS_CLIENT_DIR']}): that points the game at a test copy of the client, "
            "not your real one. Unset it (`unset HS_CLIENT_DIR`) and run ./play again.")
        return EXIT_FAIL
    if os.environ.get(PLAY_CLIENT_ENV) and not args.allow_test_client_dir:
        say(f"{PLAY_CLIENT_ENV} is set ({os.environ[PLAY_CLIENT_ENV]}): that's a test copy of the play client. "
            f"Unset it (`unset {PLAY_CLIENT_ENV}`) and run ./play again.")
        return EXIT_FAIL
    from .resolution import TEST_ENV as DISPLAY_TEST_ENV
    for var in (GLUE_TEST_ENV, SKIP_ENV, DISPLAY_TEST_ENV):   # test-only switches; refused before anything happens
        if os.environ.get(var) and not args.allow_test_client_dir:
            say(f"{var} is set: that's a test switch for the harness, not for playing. "
                f"Unset it (`unset {var}`) and run ./play again.")
            return EXIT_FAIL
    # the Wine host shim's test-only knobs (ADR-0010) only ever reach a --client-cmd stand-in, never the game
    for var in sorted(k for k in os.environ if k == "HSDOCK_TEST" or k.startswith("HSDOCK_TEST_")):
        if not args.client_cmd:
            say(f"{var} is set: that's a test switch for the harness, not for playing. "
                f"Unset it (`unset {var}`) and run ./play again.")
            return EXIT_FAIL
    launch.allow_dock_test(args.client_cmd is not None)
    try:
        fd, lock = acquire_lock()
    except Busy as b:
        say(f"Moonglade is already running (./play pid {b.pid}). Use that window, or quit the game there first.")
        return EXIT_BUSY

    loop = Loop(args)
    loop.rec["lock"] = lock
    if "reclaimed_stale_pid" in lock:
        say(f"(Cleaned up a leftover lock from an earlier run, pid {lock['reclaimed_stale_pid']}.)")
    for s in HANDLED:
        signal.signal(s, loop.on_signal)
    rc = EXIT_FAIL
    try:
        try:
            rc = loop.run()
        except Interrupted as e:
            rc = 128 + e.signum
            loop.rec["outcome"] = "interrupted"
            loop.stages.stop_world_watch()
            say("Ctrl-C received." if e.signum == signal.SIGINT else f"Stop request received ({e}).")
        except stack.StackError as e:
            loop.rec |= {"outcome": "startup_failed", "error": str(e), "log_excerpt": e.log_excerpt}
            say(f"The game server couldn't start: {e}")
            say("The game was not started. Server log excerpt:")
            print("----- server log excerpt -----\n" + (e.log_excerpt or "(none)") + "\n------------------------------",
                  flush=True)
        except launch.LaunchError as e:
            loop.rec |= {"outcome": "startup_failed", "error": str(e), "evidence": e.evidence}
            say(f"Couldn't start the game: {e}")
        except PlayerAccountError as e:
            loop.rec |= {"outcome": "startup_failed", "error": str(e)}
            say(f"Couldn't set up your login: {e}")
        except Exception as e:  # noqa: BLE001
            from .clientbuild import ClientBuildError
            if isinstance(e, ClientBuildError):
                loop.rec |= {"outcome": "startup_failed", "error": str(e), "evidence": e.evidence}
                say(f"Couldn't prepare the game client: {e}")
                say("To play with the untouched client instead: `harness/hs client-mode stock`.")
            else:
                loop.rec |= {"outcome": "error", "error": f"{type(e).__name__}: {e}",
                             "traceback": traceback.format_exc()}
                say(f"Something went wrong: {type(e).__name__}: {e}")
                traceback.print_exc()
    except Interrupted as e:                 # a signal that landed inside one of the handlers above
        rc = 128 + e.signum
        loop.rec.setdefault("outcome", "interrupted")
    finally:
        if loop.rec.get("outcome") in ("startup_failed", "error"):    # the stage it stopped at, for the launcher
            loop.stages.fail(loop.rec.get("error") or "")
            loop.rec["stage"] = loop.stages.failed_stage()
        loop.stages.stop_world_watch()
        loop.cleaning = True                 # from here on the handler never raises...
        for s in HANDLED:
            signal.signal(s, signal.SIG_IGN)  # ...and cleanup's subprocesses ignore a second Ctrl-C too
        try:
            remaining = loop.cleanup()
        except Exception as e:  # noqa: BLE001
            remaining = [{"cleanup_error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}]
            loop.rec["cleanup_error"] = remaining[0]
        loop.shutdown.finish(remaining)
        if remaining:
            say("WARNING: these are still running after shutdown:")
            for r in remaining:
                print(f"    {r}", flush=True)
            rc = rc or EXIT_FAIL
        else:
            say("All stopped.")
        if loop.rec.get("crash_message"):
            say(f"Exiting with an error because of the {loop.rec['crash_message']}.")
        loop.rec |= {"finished": _now(), "exit_code": rc}
        try:
            LAST.write_text(json.dumps(loop.rec, indent=2, default=str))
        except OSError:
            pass
        release_lock(fd)
    return rc


if __name__ == "__main__":
    sys.exit(main())
