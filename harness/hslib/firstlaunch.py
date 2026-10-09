"""First launch of a release (openspec/changes/release-first-launch, design 4 and 5; tasks 3.1-3.4, 3.6).

`hs first-launch [--client <folder or zip>]` (release mode only; the release app runs it as a child and shows its
events in the first-launch window). It is bootstrap's framework (bootstrap.Step / StepFailed / the `[bootstrap] {json}`
events, honest done() checks, stop at the first failure) with the release step list:

    preflight    this Mac (Apple Silicon, macOS >= bootstrap.MIN_MACOS, Rosetta), nothing of Moonglade running, HOME's
                 path short enough for the database's socket, then (once the client is known) the client itself and
                 free disk = the client's size + the fixed part (FIXED_BYTES) for the steps still to do
    client       Moonglade's copy of the friend's client in HOME/client (clientinst.install: the source is only read;
                 APFS clones on the same volume, else a byte copy; verified against what was read before the install
                 record is written, so "you can delete the folder" is only ever said about a verified copy)
    data         the pinned server data (downloads.install_client_data)
    wine         the pinned Wine runtime (wineinst.install)
    database     the payload's mysqld initialises HOME/mysql, then the first start (stack.up m0, then stack.down)
    playclient   the play client HOME/client-play (clientbuild.build)
    defaults     Moonglade's default settings (settings.write_missing_defaults)

When the client step isn't done and no client was given, the run stops after the Mac checks with a `need_client`
event and exit code EXIT_NEED_CLIENT (the window then asks for the folder and runs this again with --client). Every
failure is one entry of the plain-language catalog (ERRORS, design 5): a `failed` event carries its id, title,
message (what happened, whether anything changed, what to do), whether the friend may choose another folder, the
technical details and the log path.

Writes: nothing in HOME but logs/ until preflight has passed (HOME/logs/first-launch.log is the launcher's capture of
this output); then home.json (layout version, attempts, steps done, the client source, the database attempt record)
and each step's outputs. Kill-safe: done() never trusts a marker alone, partial files (.part, .hs-part, .unpack) are
removed or redone by the next run, servers a killed run left behind are stopped at the next start, and an unfinished
database is moved aside (never deleted) only when this setup created it and it has no characters.

Test switches (release only, honoured with --allow-test-release inside a live harness run; paths.firstlaunch_test()):
$HS_FIRSTLAUNCH_TEST = {"arch", "macos", "rosetta", "running", "free_bytes" (injected Mac facts), "corrupt_copy"
(a relative path, or true for Wow.exe: one byte of the copy is changed before it is verified), "force_byte_copy"
(no clones), "drop_ac_env" (stack.spawn_env)}; $HS_RELEASE_DOWNLOAD_BASE (downloads.download_url).
"""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import signal
import shutil
import subprocess
import threading
import time
import traceback
import urllib.error
import http.client
import zipfile
from pathlib import Path

from . import paths as P
from .bootstrap import MIN_MACOS, Probe, Step, StepFailed, _db_dirs_ok, _locked, emit, say

EXIT_OK, EXIT_FAILED, EXIT_BUSY, EXIT_NEED_CLIENT = 0, 1, 2, 3
LOG_FILE = P.LOGS / "first-launch.log"           # written by whoever runs this (the launcher: its child's output)
PHASES_FILE = P.LOGS / "first-launch-database.json"
SOCKET_MAX = 103                                  # sockaddr_un.sun_path is 104 bytes on macOS, NUL included
EXPECTED_BUILD = "3.3.5.12340"
NEEDED = "World of Warcraft 3.3.5a (build 12340), in English"

# The fixed part of the disk need (design 4 "Disk check"; measured, design.md "Measurements", task 4.1), bytes, from
# `du` of a scratch HOME set up by first launch on the dev Mac (2026-10-08, firstlaunch.flow / second_open_plays):
# server data unpacked 3.29 GB + its zip while unpacking 1.20 GB -> 4.5 GB; databases 1.20 GB after the first start
# and a play session -> 1.25 GB; Wine runtime 0.41 GB + the prefix the first play creates 0.36 GB -> 0.8 GB; plus 2 GB
# headroom (backups are ~27 MB each, 10 kept). The play client is an APFS clone of HOME/client (~0).
FIXED_BYTES = {"data": 4_500_000_000, "database": 1_250_000_000, "wine": 800_000_000}
HEADROOM_BYTES = 2_000_000_000
PROGRESS_EVERY_S = 0.25


# ================================================================================ the plain-language catalog (3.6)

ERRORS = {
    "not_apple_silicon": ("This Mac can't run Moonglade",
                          "Moonglade needs a Mac with Apple Silicon (M1 or newer), and this Mac has an Intel "
                          "processor. Nothing was changed."),
    "macos_too_old": ("macOS needs an update",
                      "Moonglade needs macOS {need} or newer, and this Mac has macOS {found}. Nothing was changed. "
                      "Update macOS in System Settings > General > Software Update, then open Moonglade again."),
    "no_rosetta": ("Rosetta isn't installed",
                   "The game runs through Rosetta, Apple's translator for Intel programs, and it isn't installed on "
                   "this Mac. Nothing was changed, and Moonglade doesn't install it for you. To install it, open "
                   "Terminal and run:  softwareupdate --install-rosetta --agree-to-license  (or open any Intel app "
                   "and macOS offers to install it). Then open Moonglade again."),
    "already_running": ("Moonglade is already running",
                        "Another copy of Moonglade, or its game world, is running right now. Nothing was changed. "
                        "Quit it first, then open Moonglade again."),
    "home_path_too_long": ("Moonglade can't use this user folder",
                           "The path to Moonglade's folder in your user folder ({home}) is too long for the game "
                           "world's save data to work ({n} characters; at most {max} fit). Nothing was changed. "
                           "Use a user account with a shorter name."),
    "not_enough_space": ("Not enough free space",
                         "Setting up needs about {need} GB free on the disk “{disk}”, and it has {free} GB "
                         "free. Nothing was changed. Free up some space (for example, empty the Trash), then open "
                         "Moonglade again."),
    "folder_missing": ("That folder isn't there",
                       "Moonglade couldn't find {path}. No files were changed. Choose the folder that holds your "
                       "game (the one with Wow.exe in it), or the .zip you downloaded. Moonglade needs " + NEEDED +
                       "."),
    "not_wow_folder": ("That isn't a World of Warcraft folder",
                       "{name} {why}. Moonglade needs " + NEEDED + ": the folder that holds Wow.exe, or the .zip you "
                       "downloaded. No files were changed. Choose it again."),
    "wrong_version": ("That's a different version of World of Warcraft",
                      "The Wow.exe in {name} is version {found}. Moonglade needs " + NEEDED + " (version "
                      + EXPECTED_BUILD + "). No files were changed. Choose a 3.3.5a game folder."),
    "not_english": ("That game isn't the English version",
                    "The game in {name} is in {langs}. Moonglade needs " + NEEDED + " (US English, enUS). No files "
                    "were changed. Choose an English 3.3.5a game folder."),
    "cant_read_folder": ("Moonglade isn't allowed to read that folder",
                         "macOS didn't let Moonglade read {path}, so it can't check that it holds " + NEEDED + ". "
                         "No files were changed. To allow it, open System "
                         "Settings > Privacy & Security > Files and Folders and turn on that folder for Moonglade "
                         "(or move the game folder somewhere else, like your home folder), then choose it again."),
    "source_damaged": ("Your game files are damaged",
                       "Some files in {name} couldn't be read correctly ({n} damaged), so Moonglade stopped copying. "
                       "Your folder or zip wasn't changed. Get a good copy of the game and choose it again."),
    "copy_failed": ("The copy of your game didn't check out",
                    "Moonglade copied your game, but the copy doesn't match the original ({n} file(s) differ). Your "
                    "folder wasn't changed, and don't delete it yet. Open Moonglade again to copy it again; if this "
                    "keeps happening, the disk may have a problem."),
    "download_failed": ("Moonglade couldn't download what it needs",
                        "Setting up downloads {what} once, and the download didn't work. Check your internet "
                        "connection, then open Moonglade again: it continues where it stopped, and everything done "
                        "so far is kept."),
    "download_damaged": ("A download arrived damaged",
                         "{what} didn't arrive complete and correct, so Moonglade threw it away. Open Moonglade "
                         "again to download it again: it continues where it stopped, and everything done so far is "
                         "kept."),
    "database_failed": ("Moonglade couldn't create your game world",
                        "Creating the game world's save data didn't work. Your game folder wasn't changed. Open "
                        "Moonglade again to try once more. If it keeps happening, the log says what went wrong: "
                        "{log}"),
    "database_kept": ("Moonglade found saved game data it won't replace",
                      "Moonglade's folder already holds game world data that {why}, so setup stopped instead of "
                      "replacing it. Nothing was changed. The data is in {path}."),
    "unexpected": ("Something went wrong while setting up",
                   "Setup stopped at “{step}” because of a problem Moonglade didn't expect. Open Moonglade "
                   "again to continue where it stopped. If it keeps happening, the log says what went wrong: {log}"),
}
# Failures where choosing another folder is the fix (the window offers "Choose again").
CHOOSE_AGAIN = {"folder_missing", "not_wow_folder", "wrong_version", "not_english", "cant_read_folder",
                "source_damaged"}


class Refusal(StepFailed):
    """A failure of the catalog: `error_id` names the entry, `fmt` fills it, `details` is the technical text."""

    def __init__(self, error_id: str, details: str = "", evidence: dict | None = None, **fmt):
        title, text = ERRORS[error_id]
        fmt.setdefault("log", str(LOG_FILE))
        try:
            message = text.format(**fmt)
        except (KeyError, IndexError):
            message = text
        super().__init__(message, "", evidence or {})
        self.error_id, self.title, self.message, self.details = error_id, title, message, details or message


class Stopped(BaseException):
    """SIGTERM/SIGINT/SIGHUP (the window was closed): unwinds every step's cleanup; never caught as a failure."""

    def __init__(self, signum: int):
        super().__init__(f"signal {signum}")
        self.signum = signum


# ================================================================================ home.json

def read_home() -> dict:
    try:
        d = json.loads(P.HOME_FILE.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_home(d: dict) -> None:
    """Atomic: a killed write leaves the old file (or none), never half of one."""
    P.HOME.mkdir(parents=True, exist_ok=True)
    tmp = P.HOME_FILE.with_name(P.HOME_FILE.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(d, indent=2, default=str) + "\n")
    os.replace(tmp, P.HOME_FILE)


def update_home(**changes) -> dict:
    d = read_home()
    for k, v in changes.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            d[k] = {**d[k], **v}
        else:
            d[k] = v
    write_home(d)
    return d


def setup_complete(d: dict | None = None) -> bool:
    """What the launcher reads to choose between the first-launch window and the play loop."""
    d = read_home() if d is None else d
    return bool((d.get("first_launch") or {}).get("complete"))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ================================================================================ preflight (3.1)

def volume_of(path: Path) -> dict:
    """{"mount", "name"} of the volume holding `path` (or its nearest existing parent)."""
    p = Path(os.path.abspath(path))
    while not p.exists() and p != p.parent:
        p = p.parent
    dev = os.stat(p).st_dev
    mount = p
    while mount != mount.parent and os.stat(mount.parent).st_dev == dev:
        mount = mount.parent
    name = None
    try:
        r = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", str(mount)], capture_output=True, timeout=20)
        if r.returncode == 0:
            name = plistlib.loads(r.stdout).get("VolumeName")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return {"mount": str(mount), "name": name or (mount.name if str(mount) != "/" else "Macintosh HD"),
            "probe_path": str(p)}


def mac_checks(probe: Probe) -> dict:
    """Apple Silicon, macOS, Rosetta and HOME's path length. Changes nothing."""
    ev: dict = {"arch": probe.arch()}
    if ev["arch"] != "arm64":
        raise Refusal("not_apple_silicon", f"hw.optional.arm64 / machine: {ev['arch']}", ev)
    ev["macos"] = ver = probe.macos()
    try:
        vt = tuple(int(x) for x in (str(ver).split(".") + ["0"])[:2])
    except ValueError:
        vt = (0, 0)
    if vt < MIN_MACOS:
        raise Refusal("macos_too_old", f"sw_vers -productVersion: {ver}; minimum {MIN_MACOS}", ev,
                      need=f"{MIN_MACOS[0]}.{MIN_MACOS[1]}", found=ver)
    ev["rosetta"] = probe.rosetta()
    if not ev["rosetta"]:
        raise Refusal("no_rosetta", "arch -x86_64 /usr/bin/true failed", ev)
    sock = os.fsencode(str(P.RUN / "mysql.sock"))
    ev["socket_path_bytes"] = len(sock)
    if len(sock) > SOCKET_MAX:
        raise Refusal("home_path_too_long", f"{P.RUN / 'mysql.sock'} is {len(sock)} bytes; a Unix socket path "
                      f"fits {SOCKET_MAX}", ev, home=str(P.HOME), n=len(sock), max=SOCKET_MAX)
    return ev


def running_check(probe: Probe) -> dict:
    """Nothing of Moonglade running: no play loop (its lock), no server on Moonglade's ports (e.g. a dev stack). Servers
    of this HOME left by a killed first launch (no lock held) are stopped first, gracefully."""
    from . import stack
    ev: dict = {}
    if probe.inject.get("running"):
        raise Refusal("already_running", "injected: running", ev | {"injected": True})
    if _locked(P.RUN / "play.lock"):
        raise Refusal("already_running", f"{P.RUN / 'play.lock'} is held by a running play loop", ev)
    mine = {k: v for k, v in stack.stack_processes().items() if v}
    if mine:
        say("   Stopping the game world a stopped setup left running ...")
        ev["leftovers"] = {"found": mine, "down": stack.down()}
        if ev["leftovers"]["down"].get("remaining"):
            raise Refusal("already_running", f"left-over servers wouldn't stop: {ev['leftovers']['down']}", ev)
    busy = [p for p in P.STACK_PORTS if stack.port_open(p)]
    ev["ports_in_use"] = busy
    if busy:
        raise Refusal("already_running", f"ports in use: {busy} (Moonglade's: {list(P.STACK_PORTS)})", ev)
    return ev


def need_bytes(todo: list[str], client_bytes: int) -> int:
    return client_bytes + sum(FIXED_BYTES.get(s, 0) for s in todo) + HEADROOM_BYTES


def disk_check(probe: Probe, todo: list[str], client_bytes: int) -> dict:
    vol = volume_of(P.HOME)
    free = int(probe.free_bytes() if "free_bytes" in probe.inject else shutil.disk_usage(vol["probe_path"]).free)
    need = need_bytes(todo, client_bytes)
    ev = {"free_bytes": free, "need_bytes": need, "client_bytes": client_bytes, "for_steps": todo,
          "fixed_bytes": {k: FIXED_BYTES[k] for k in todo if k in FIXED_BYTES}, "headroom_bytes": HEADROOM_BYTES,
          "volume": vol}
    if free < need:
        raise Refusal("not_enough_space", f"free {free} bytes on {vol['mount']}, need {need} (client {client_bytes} "
                      f"+ fixed {sum(FIXED_BYTES.get(s, 0) for s in todo)} + headroom {HEADROOM_BYTES})", ev,
                      need=_gb(need), free=_gb(free), disk=vol["name"])
    return ev


def _gb(b: int) -> str:
    return f"{b / 1e9:.1f}"


# ================================================================================ the client the friend gives (3.2)

def inspect_client(src: str) -> dict:
    """A folder or zip holding a Windows 3.3.5a build 12340 enUS client; only reads it. Raises a Refusal (catalog)
    naming what was found. Returns {source, kind, wow_exe_version, locales, bytes, files}."""
    from .clientinst import ClientInstallError, _open_source
    from .pe import PEError, version_info
    p = Path(os.path.abspath(os.path.expanduser(src)))
    name = f"“{p.name}”"
    if not os.path.lexists(p):
        raise Refusal("folder_missing", f"{p} doesn't exist", {"source": str(p)}, path=str(p))
    try:
        if p.is_dir():
            os.listdir(p)
        elif p.is_file():
            with open(p, "rb"):
                pass
    except PermissionError as e:
        raise Refusal("cant_read_folder", f"{type(e).__name__}: {e}", {"source": str(p)}, path=str(p)) from None
    if not (p.is_dir() or p.is_file()):
        raise Refusal("not_wow_folder", f"{p} is neither a folder nor a file", {"source": str(p)}, name=name,
                      why="isn't a folder or a zip")
    if p.is_file() and not zipfile.is_zipfile(p):
        raise Refusal("not_wow_folder", f"{p} isn't a zip", {"source": str(p)}, name=name,
                      why="is a file, not a game folder or a .zip")
    try:
        so = _open_source(p)
    except ClientInstallError as e:
        raise Refusal("not_wow_folder", str(e), {"source": str(p)}, name=name, why="couldn't be opened as a .zip")
    except PermissionError as e:
        raise Refusal("cant_read_folder", f"{type(e).__name__}: {e}", {"source": str(p)}, path=str(p)) from None
    try:
        names = {r.lower(): r for r in so.members}
        if "wow.exe" not in names:
            raise Refusal("not_wow_folder", f"no Wow.exe at the top of {p} (stripped {so.prefix!r}); "
                          f"{len(so.members)} files", {"source": str(p), "files": len(so.members)}, name=name,
                          why="is empty" if not so.members else "doesn't have the game's Wow.exe in it")
        try:
            with so.open(names["wow.exe"]) as fh:
                data = fh.read()
        except PermissionError as e:
            raise Refusal("cant_read_folder", f"{type(e).__name__}: {e}", {"source": str(p)}, path=str(p)) from None
        try:
            vi = version_info(data)
        except PEError as e:
            raise Refusal("wrong_version", f"Wow.exe has no readable version resource: {e}", {"source": str(p)},
                          name=name, found="unknown (it has no version information)") from None
        locales = sorted({r.split("/")[1] for r in so.members if r.lower().startswith("data/") and r.count("/") >= 2
                          and len(r.split("/")[1]) == 4 and r.split("/")[1][2:].isupper()})
        total = sum(so.size(r) for r in so.members)
        info = {"source": str(p), "kind": so.kind, "wow_exe_version": vi["file_version"], "locales": locales,
                "bytes": total, "files": len(so.members)}
    except PermissionError as e:
        raise Refusal("cant_read_folder", f"{type(e).__name__}: {e}", {"source": str(p)}, path=str(p)) from None
    finally:
        so.close()
    if vi["file_version"] != EXPECTED_BUILD:
        raise Refusal("wrong_version", f"Wow.exe file version {vi['file_version']}, need {EXPECTED_BUILD}", info,
                      name=name, found=vi["file_version"])
    if "enUS" not in locales:
        raise Refusal("not_english", f"locales {locales}, need enUS", info, name=name,
                      langs=", ".join(locales) if locales else "an unknown language")
    return info


def _overlap_problem(src: Path) -> str | None:
    """The client step never writes inside the source and the source never lies inside HOME or the app."""
    s = Path(os.path.realpath(src))
    for what, root in (("Moonglade's own folder", P.HOME), ("the Moonglade app", P.APP)):
        r = Path(os.path.realpath(root)) if Path(root).exists() else Path(os.path.abspath(root))
        if s == r or r in s.parents or s in r.parents:
            return f"{s} overlaps {what} ({r})"
    return None


# ================================================================================ progress events (3.3)

class Progress:
    """Throttled `progress` events (at most every PROGRESS_EVERY_S, always the first and the last)."""

    def __init__(self, step: str, what: str):
        self.step, self.what, self.last, self.last_done = step, what, 0.0, -1

    def __call__(self, name, done, total=None):
        # clientinst calls (done, total); downloads call (name, done, total)
        if total is None and not isinstance(name, str):
            name, done, total = self.what, name, done
        now = time.time()
        final = total is not None and done >= total
        if done == self.last_done or (not final and done and now - self.last < PROGRESS_EVERY_S):
            return
        self.last, self.last_done = now, done
        emit(event="progress", step=self.step, what=self.what, name=name, done=done, total=total,
             fraction=round(done / total, 4) if total else None)


# ================================================================================ the steps

class Ctx:
    def __init__(self, client: str | None, probe: Probe):
        self.client, self.probe = client, probe
        self.client_info: dict | None = None
        self.test = probe.inject


class ClientStep(Step):
    id, label = "client", "Copying your game into Moonglade"

    def done(self, ctx):
        from .clientinst import load_manifest, mismatches
        from .pristine import status
        man = load_manifest(P.CLIENT_MANIFEST)
        if not man or not P.CLIENT.is_dir():
            return False, "not copied yet"
        bad = mismatches(P.CLIENT, man)
        if bad:
            return False, f"{len(bad)} file(s) differ from the copy's record"
        if not status(P.CLIENT)["locked"]:
            return False, "copied, but not write-protected"
        return True, f"copied and verified (Wow.exe {(man.get('wow_exe_version') or {}).get('file_version')})"

    def run(self, ctx):
        from . import clientinst, pristine
        info = ctx.client_info
        src = Path(info["source"])
        why = _overlap_problem(src)
        if why:
            raise Refusal("not_wow_folder", why, info, name=f"“{src.name}”",
                          why="is inside Moonglade's own folders")
        pristine.protect(src)               # every writer guard now refuses the friend's folder too
        corrupt = ctx.test.get("corrupt_copy")

        def before_verify(client: Path):    # test switch: one byte of the copy changes before it is verified
            if not corrupt:
                return
            rel = corrupt if isinstance(corrupt, str) else next(
                r for r in (x.relative_to(client).as_posix() for x in client.iterdir()) if r.lower() == "wow.exe")
            f = client / rel
            with open(f, "r+b") as fh:
                fh.seek(f.stat().st_size // 2)
                b = fh.read(1)
                fh.seek(-1, 1)
                fh.write(bytes([(b[0] ^ 0xFF) if b else 0]))
            emit(event="test", what="corrupt_copy", file=rel)

        prog = Progress(self.id, "copy")
        try:
            res = clientinst.install(src, P.CLIENT, P.CLIENT_MANIFEST, run_config=False,
                                     clone=not ctx.test.get("force_byte_copy"), verify=True,
                                     before_verify=before_verify, progress=prog)
        except clientinst.ClientInstallError as e:
            name = f"“{src.name}”"
            if e.kind == "copy_mismatch":
                raise Refusal("copy_failed", str(e), e.evidence, n=len(e.evidence.get("mismatches") or [1]))
            if e.kind == "source_damaged":
                raise Refusal("source_damaged", str(e), e.evidence, name=name, n=len(e.evidence.get("corrupt") or []))
            raise Refusal("unexpected", f"ClientInstallError: {e}", {"kind": e.kind}, step=self.label)
        except PermissionError as e:
            raise Refusal("cant_read_folder", f"{type(e).__name__}: {e}", {}, path=str(src)) from None
        update_home(client_source={"path": str(src), "kind": info["kind"], "bytes": info["bytes"],
                                   "files": info["files"], "verified_at": _now(),
                                   "copy": (res.get("copy_check") or {}).get("methods")})
        return {k: res.get(k) for k in ("action", "files_written", "bytes_written", "seconds", "copy_check")}


def _download_refusal(e: BaseException, what: str, step: str) -> Refusal:
    from .downloads import DownloadError
    cause = e if isinstance(e, DownloadError) else (e.__cause__ or e)
    if isinstance(cause, DownloadError) and cause.kind == "damaged":
        return Refusal("download_damaged", str(cause), cause.evidence, what=what[:1].upper() + what[1:])
    net = (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException)
    if isinstance(cause, net) or isinstance(e, net):
        return Refusal("download_failed", f"{type(cause).__name__}: {cause}", {}, what=what)
    return Refusal("unexpected", f"{type(e).__name__}: {e}", {}, step=step)


class DataStep(Step):
    id, label = "data", "Downloading the world's maps (about 1.2 GB)"
    what = "the world's maps (about 1.2 GB)"

    def done(self, ctx):
        from .downloads import client_data_state
        s = client_data_state()
        return s["done"], (f"unpacked from the pinned zip ({s['files']} files)" if s["done"] else s["why"])

    def run(self, ctx):
        from . import downloads
        downloads.PROGRESS = Progress(self.id, "download")
        try:
            return downloads.install_client_data()
        except Exception as e:  # noqa: BLE001  (mapped to the catalog)
            raise _download_refusal(e, self.what, self.label) from None
        finally:
            downloads.PROGRESS = None


class WineStep(Step):
    id, label = "wine", "Downloading the part that runs the Windows game on this Mac (about 64 MB)"
    what = "the part that runs the Windows game on this Mac (about 64 MB)"

    def done(self, ctx):
        from . import wineinst
        want = P.fork_lock()["wowsilicon_runtime_sha256"]
        ok = ((wineinst.RUNTIME_DIR / ".wine-runtime/bin/wine").exists() and wineinst.MARKER.exists()
              and wineinst.MARKER.read_text().strip() == want)
        return ok, ("the pinned runtime is installed" if ok else "not installed")

    def run(self, ctx):
        from . import downloads, wineinst
        if wineinst.RUNTIME_DIR.exists() and not self.done(ctx)[0]:
            # never adopted unverified (wineinst refuses it): an earlier, unfinished setup's runtime folder
            aside = wineinst.RUNTIME_DIR.with_name(f"runtime.unfinished-{time.strftime('%Y%m%d-%H%M%S')}")
            wineinst.RUNTIME_DIR.rename(aside)
            shutil.rmtree(aside, ignore_errors=True)
        downloads.PROGRESS = Progress(self.id, "download")
        try:
            return wineinst.install()
        except Exception as e:  # noqa: BLE001
            raise _download_refusal(e, self.what, self.label) from None
        finally:
            downloads.PROGRESS = None


# ---------------------------------------------------------------- database (3.4)

# What the database step timestamps in worldserver.out (first and last line arrival time of each), so task 4.1 can
# split the first start: SQL import (the updater populating / applying) vs bot setup (playerbots after world-ready).
DB_PATTERNS = {
    "populating": "auto populating",
    "applying": ">> Applying",
    "up_to_date": "is up-to-date",
    "world_initialized": "World initialized",
    "playerbots": "layerbot",
}


class _LogWatch(threading.Thread):
    """Line arrival times of DB_PATTERNS in a growing log file (first and last), until stop()."""

    def __init__(self, path: Path):
        super().__init__(daemon=True)
        self.path, self.seen, self._stop = path, {}, threading.Event()
        self.lines = 0

    def run(self):
        pos, buf = 0, b""
        while not self._stop.is_set():
            try:
                with open(self.path, "rb") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos += len(chunk)
            except OSError:
                chunk = b""
            if chunk:
                buf += chunk
                *lines, buf = buf.split(b"\n")
                now = time.time()
                for raw in lines:
                    self.lines += 1
                    line = raw.decode("utf-8", "replace")
                    for k, pat in DB_PATTERNS.items():
                        if pat.lower() in line.lower():
                            rec = self.seen.setdefault(k, {"first": now, "count": 0})
                            rec["last"], rec["count"] = now, rec["count"] + 1
            self._stop.wait(0.5)

    def stop(self):
        self._stop.set()
        self.join(timeout=5)


# The AI players' own characters: playerbots creates its random-bot accounts (username prefix
# AiPlayerbot.RandomBotAccountPrefix, "rndbot" in playerbots.conf.dist, which Moonglade doesn't change; RandomPlayerbotMgr
# finds them with `username LIKE '<prefix>%'`) and their characters during the first start, so an interrupted first
# start already has some. Only other characters (a player's) protect a datadir from the reset; a character whose
# account is missing from acore_auth counts as a player's.
BOT_ACCOUNT_PREFIX = "rndbot"
PLAYER_CHARACTERS = (f"SELECT COUNT(*) FROM {P.DB_NAMES['characters']}.characters c LEFT JOIN "
                     f"{P.DB_NAMES['auth']}.account a ON a.id = c.account "
                     f"WHERE a.username IS NULL OR a.username NOT LIKE '{BOT_ACCOUNT_PREFIX}%'")


PROBE_DIR_PREFIX = "dc-"                 # short: the probe socket must fit 103 bytes too


def _stop_probes() -> list[int]:
    """Kill the throwaway mysqld of a datadir check a killed run left behind, and remove its folders."""
    from . import procs
    killed = []
    for pid, argv in procs.argv_table():
        if pid != os.getpid() and any(str(P.TMP / PROBE_DIR_PREFIX) in a for a in argv):
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except ProcessLookupError:
                pass
    if P.TMP.is_dir():
        for x in P.TMP.glob(PROBE_DIR_PREFIX + "*"):
            shutil.rmtree(x, ignore_errors=True)
    return killed


def _characters_in_datadir() -> tuple[int | None, str]:
    """(number of player characters, how it was found) in the existing HOME/mysql; None when it can't be told
    (fail-closed: the caller then keeps the datadir). The datadir itself is never opened: its APFS clone in HOME/tmp
    is, by a throwaway mysqld (socket only, no network), which is stopped and removed afterwards."""
    if not (P.MYSQL_DATA / "acore_characters").exists():
        return 0, "no acore_characters database folder"
    from . import stack
    _stop_probes()
    d = P.TMP / f"{PROBE_DIR_PREFIX}{os.getpid()}"
    sock = d / "s"
    proc = None
    try:
        d.mkdir(parents=True)
        r = subprocess.run(["/bin/cp", "-cR", str(P.MYSQL_DATA), str(d / "data")], capture_output=True, text=True)
        if r.returncode != 0:
            return None, f"couldn't clone the datadir: {r.stderr.strip()[-300:]}"
        for f in (d / "data").glob("*.pid"):
            f.unlink()
        cnf = d / "my.cnf"
        cnf.write_text("[mysqld]\n" + "".join(f"{k}={v}\n" for k, v in (
            ("basedir", P.MYSQL_BASE), ("datadir", d / "data"), ("socket", sock), ("pid-file", d / "m.pid"),
            ("log-error", d / "err.log"), ("innodb_buffer_pool_size", "128M"))) +
            ("" if P.MODE == "dev" else f"lc-messages-dir={P.MYSQL_BASE / 'share'}\n"
             f"character-sets-dir={P.MYSQL_BASE / 'share' / 'charsets'}\n") +
            "skip-networking\nmysqlx=OFF\nperformance_schema=OFF\ndisable-log-bin\n")
        proc = subprocess.Popen([str(stack.MYSQLD), f"--defaults-file={cnf}"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        def q(sql: str) -> tuple[int, str]:
            r_ = subprocess.run([str(P.MYSQL), "--no-defaults", f"--socket={sock}", "-uroot", "--batch",
                                 "--skip-column-names", "-e", sql], capture_output=True, text=True, timeout=60)
            return r_.returncode, (r_.stdout.strip() if r_.returncode == 0 else r_.stderr.strip())
        t0 = time.time()
        while time.time() - t0 < 120 and proc.poll() is None:
            if sock.exists() and q("SELECT 1")[0] == 0:
                break
            time.sleep(0.5)
        else:
            err = (d / "err.log").read_text(errors="replace")[-400:] if (d / "err.log").exists() else ""
            return None, f"mysqld didn't start on the datadir's clone: {err}"
        rc, total = q(f"SELECT COUNT(*) FROM {P.DB_NAMES['characters']}.characters")
        if rc != 0:
            return (0, "no characters table yet") if "doesn't exist" in total else (None, f"query failed: {total}")
        if total == "0":
            return 0, "SELECT COUNT(*) FROM characters -> 0"
        rc, n = q(PLAYER_CHARACTERS)
        if rc != 0 or not n.isdigit():
            return None, f"{total} character(s); the player-character query failed: {n[-300:]}"
        return int(n), f"{total} character(s), {n} not on the AI players' {BOT_ACCOUNT_PREFIX}* accounts"
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"{type(e).__name__}: {e}"
    finally:
        if proc is not None and proc.poll() is None:
            subprocess.run([str(P.MYSQLADMIN), "--no-defaults", f"--socket={sock}", "-uroot", "shutdown"],
                           capture_output=True, timeout=120)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        shutil.rmtree(d, ignore_errors=True)


class DatabaseStep(Step):
    id, label = "database", "Creating your game world (the first time takes 10-30 minutes)"

    def done(self, ctx):
        ok, why = _db_dirs_ok()
        if not ok:
            return False, why
        try:
            m = json.loads(P.DB_MARKER.read_text())
        except (OSError, ValueError):
            return False, "no completed first start recorded"
        if m.get("home") != str(P.HOME):
            return False, "first start recorded for another folder"
        return True, f"first start completed {m.get('at')}"

    def _reset_unfinished(self) -> dict:
        """An existing, unfinished datadir: moved aside (never deleted) only when this setup created it (the attempt
        record in home.json) and it holds no player characters (the AI players' own, made by every first start, don't
        count); anything else, including "can't tell", stops with database_kept."""
        rec = (read_home().get("database") or {})
        path = str(P.MYSQL_DATA)
        if rec.get("created_by") != "first-launch":
            raise Refusal("database_kept", f"{path} exists and home.json has no first-launch record for it", {},
                          why="this setup didn't create", path=path)
        n, how = _characters_in_datadir()
        ev = {"attempt_record": rec, "characters": n, "how": how}
        if n is None:
            raise Refusal("database_kept", f"can't tell whether {path} holds characters ({how})", ev,
                          why="might hold characters", path=path)
        if n > 0:
            raise Refusal("database_kept", f"{path} holds {n} character(s)", ev, why="holds characters", path=path)
        aside = P.MYSQL_DATA.with_name(f"mysql.unfinished-{time.strftime('%Y%m%d-%H%M%S')}")
        P.MYSQL_DATA.rename(aside)
        P.DB_MARKER.unlink(missing_ok=True)
        say(f"   An unfinished game world from an earlier attempt was moved aside to {aside}")
        return ev | {"moved_aside_to": str(aside)}

    def run(self, ctx):
        from . import stack
        t0 = time.time()
        ev: dict = {}
        if P.MYSQL_DATA.exists() and any(P.MYSQL_DATA.iterdir()):
            ev["reset"] = self._reset_unfinished()
        d = update_home(database={"created_by": "first-launch", "attempt": (read_home().get("database") or {})
                                  .get("attempt", 0) + 1, "at": _now(), "datadir": str(P.MYSQL_DATA)})
        ev["attempt"] = d["database"]["attempt"]
        phases: dict = {"start": t0}
        watch = None
        try:
            stack._write_my_cnf()
            if not (P.MYSQL_DATA / "mysql").exists():
                P.MYSQL_DATA.mkdir(parents=True, exist_ok=True)
                phases["initialise_start"] = time.time()
                r = subprocess.run([str(stack.MYSQLD), f"--defaults-file={stack.MY_CNF}", "--initialize-insecure"],
                                   capture_output=True, text=True)
                phases["initialise_end"] = time.time()
                if r.returncode != 0:
                    raise Refusal("database_failed", f"mysqld --initialize-insecure failed: "
                                  f"{(r.stdout + r.stderr)[-600:]}", ev)

            def on_stage(sid, state, **kw):
                phases[f"{sid}_{state}"] = time.time()
                nonlocal watch
                if sid == "world" and state == "spawned" and watch is None:
                    watch = _LogWatch(Path(kw.get("log") or stack.LOG["worldserver"]))
                    watch.start()
                emit(event="phase", step=self.id, phase=f"{sid}_{state}", t=round(time.time() - t0, 1))
            try:
                ev["up"] = {k: v for k, v in stack.up(profile="m0", on_stage=on_stage).items()
                            if k in ("first_run", "profile", "mysqld", "worldserver", "ready_s")}
            except stack.StackError as e:
                raise Refusal("database_failed", f"{e}\n{(e.log_excerpt or '')[-1500:]}", ev) from None
            phases["ready"] = time.time()
        finally:
            if watch is not None:
                watch.stop()
            phases["down_start"] = time.time()
            try:
                ev["down"] = stack.down()
            except Exception as e:  # noqa: BLE001
                ev["down"] = {"error": f"{type(e).__name__}: {e}"}
            phases["down_end"] = time.time()
            rec = {"phases_s": {k: round(v - t0, 1) for k, v in phases.items()},
                   "log_lines": {k: {"first_s": round(v["first"] - t0, 1), "last_s": round(v["last"] - t0, 1),
                                     "count": v["count"]} for k, v in (watch.seen if watch else {}).items()},
                   "worldserver_lines": watch.lines if watch else 0, "at": _now(), "attempt": ev["attempt"]}
            try:
                P.LOGS.mkdir(parents=True, exist_ok=True)
                PHASES_FILE.write_text(json.dumps(rec, indent=2) + "\n")
            except OSError:
                pass
            ev["phases"] = rec
        if ev["down"].get("remaining") or "error" in ev["down"]:
            raise Refusal("database_failed", f"the servers didn't stop after the first start: {ev['down']}", ev)
        ok, why = _db_dirs_ok()
        if not ok:
            raise Refusal("database_failed", f"the first start finished but {why}", ev)
        P.DB_MARKER.write_text(json.dumps({"home": str(P.HOME), "at": _now(), "first_start_s":
                                           round(time.time() - t0, 1), "attempt": ev["attempt"]}, indent=2) + "\n")
        return {"first_start_s": round(time.time() - t0, 1), "phases": ev["phases"]["phases_s"],
                "reset": ev.get("reset")}


class PlayClientStep(Step):
    id, label = "playclient", "Preparing the game"

    def done(self, ctx):
        from .clientbuild import PLAY, up_to_date
        u = up_to_date(PLAY)
        return u["current"], u["why"]

    def run(self, ctx):
        from . import clientbuild
        try:
            r = clientbuild.build()
        except clientbuild.ClientBuildError as e:
            raise Refusal("unexpected", f"ClientBuildError: {e}", e.evidence, step=self.label) from None
        return {"built": r.get("built"), "seconds": r.get("seconds")}


class DefaultsStep(Step):
    id, label = "defaults", "Saving Moonglade's settings"

    def done(self, ctx):
        from . import settings
        try:
            data = settings.load()
        except settings.SettingsError as e:
            return False, str(e)
        missing = [k for k in settings.SETTINGS_FILE_KEYS if k not in data]
        return (not missing, "set" if not missing else f"not set yet: {', '.join(missing)}")

    def run(self, ctx):
        from . import settings
        try:
            return settings.write_missing_defaults()
        except settings.SettingsError as e:
            raise Refusal("unexpected", f"SettingsError: {e}", {}, step=self.label) from None


STEPS = [ClientStep(), DataStep(), WineStep(), DatabaseStep(), PlayClientStep(), DefaultsStep()]
STEP_IDS = ["preflight"] + [s.id for s in STEPS]
PREFLIGHT_LABEL = "Checking this Mac"


# ================================================================================ the run

def _heal_partials() -> list[str]:
    """Leftovers of a killed run that the next steps don't clean themselves: unfinished downloads (.part) and a
    half-written home.json temp file. (.hs-part files are clientinst's; .unpack folders the data/wine steps'.)"""
    out = []
    for d in (P.DOWNLOADS,):
        if d.is_dir():
            for f in d.glob("*.part"):
                f.unlink(missing_ok=True)
                out.append(str(f))
    if P.HOME.is_dir():
        for f in P.HOME.glob("home.json.tmp-*"):
            f.unlink(missing_ok=True)
            out.append(str(f))
    return out


def _fail(step: str, e: StepFailed, t_all: float) -> int:
    if isinstance(e, Refusal):
        rid, title, msg, details = e.error_id, e.title, e.message, e.details
    else:
        r = Refusal("unexpected", str(e), e.evidence, step=step)
        rid, title, msg, details = r.error_id, r.title, r.message, r.details
    say(f"\nSetup stopped: {title}.\n  {msg}")
    emit(event="step", step=step, state="failed", error_id=rid, title=title, message=msg, details=details[-4000:],
         choose_again=rid in CHOOSE_AGAIN, log=str(LOG_FILE), evidence=e.evidence,
         total_s=round(time.time() - t_all, 1))
    try:
        if P.HOME_FILE.exists():
            update_home(first_launch={"last_failure": {"step": step, "error_id": rid, "at": _now()}})
    except OSError:
        pass
    return EXIT_FAILED


def run(client: str | None = None) -> int:
    t_all = time.time()
    if P.MODE != "release":
        say("`hs first-launch` sets up a release (Moonglade.app). In a checkout, use ./bootstrap.")
        return EXIT_BUSY
    P.LOGS.mkdir(parents=True, exist_ok=True)
    # one first launch per HOME: a flock on HOME/logs itself (no lock file is created)
    lock_fd = os.open(P.LOGS, os.O_RDONLY)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        r = Refusal("already_running", "another first launch holds the HOME/logs lock")
        emit(event="step", step="preflight", state="failed", error_id=r.error_id, title=r.title, message=r.message,
             details=r.details, choose_again=False, log=str(LOG_FILE))
        return EXIT_BUSY

    def on_signal(signum, _frame):
        for s_ in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):     # one stop: the cleanup isn't cut short
            signal.signal(s_, signal.SIG_IGN)
        raise Stopped(signum)
    old = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        return _run(client, t_all)
    except Stopped as e:
        say("Setup was stopped; it continues from here the next time Moonglade opens.")
        emit(event="stopped", signal=e.signum, total_s=round(time.time() - t_all, 1))
        return 128 + e.signum
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        os.close(lock_fd)


def _run(client: str | None, t_all: float) -> int:
    probe = Probe(P.firstlaunch_test())
    ctx = Ctx(client, probe)
    home = read_home()
    say(f"Setting up Moonglade in {P.HOME}")
    emit(event="start", home=str(P.HOME), app=str(P.APP), client_given=bool(client),
         attempts=(home.get("first_launch") or {}).get("attempts", 0), steps=STEP_IDS,
         labels={"preflight": PREFLIGHT_LABEL, **{s.id: s.label for s in STEPS}},
         test=sorted(probe.inject) if probe.inject else None)

    # 1. preflight: this Mac, nothing running, (with the client) the client and the disk. Writes nothing.
    emit(event="step", step="preflight", state="start", label=PREFLIGHT_LABEL)
    pre: dict = {}
    try:
        pre["mac"] = mac_checks(probe)
        pre["running"] = running_check(probe)
        healed = _heal_partials()
        if healed:
            pre["healed"] = healed
        status = {}
        for s in STEPS:
            try:
                status[s.id] = s.done(ctx)
            except Exception as e:  # noqa: BLE001  (a done() that can't decide counts as not done)
                status[s.id] = (False, f"{type(e).__name__}: {e}")
        todo = [i for i, (ok, _w) in status.items() if not ok]
        pre["todo"] = todo
        if "client" in todo:
            if not client:
                # before asking for the folder: is there room for at least the fixed part?
                pre["disk_before_client"] = disk_check(probe, todo, 0)
                say("Choose your World of Warcraft 3.3.5a folder to continue.")
                emit(event="step", step="preflight", state="need_client", evidence=pre)
                emit(event="need_client", needed=NEEDED, todo=todo)
                return EXIT_NEED_CLIENT
            ctx.client_info = pre["client"] = inspect_client(client)
            why = _overlap_problem(Path(ctx.client_info["source"]))
            if why:
                raise Refusal("not_wow_folder", why, pre, name=f"“{Path(client).name}”",
                              why="is inside Moonglade's own folders")
        pre["disk"] = disk_check(probe, todo, ctx.client_info["bytes"] if ctx.client_info else 0)
    except Refusal as e:
        e.evidence = {**pre, **e.evidence}
        return _fail("preflight", e, t_all)
    emit(event="step", step="preflight", state="done", evidence=pre)

    # from here on Moonglade writes into HOME
    fl = (read_home().get("first_launch") or {})
    update_home(layout=P.HOME_LAYOUT_VERSION, created_at=read_home().get("created_at") or _now(),
                first_launch={"attempts": fl.get("attempts", 0) + 1, "last_start": _now(), "complete": False,
                              "steps": fl.get("steps") or {}})
    for s in STEPS:
        t0 = time.time()
        try:
            ok, why = s.done(ctx)
        except Exception as e:  # noqa: BLE001
            ok, why = False, f"{type(e).__name__}: {e}"
        if ok:
            say(f"{s.label}: already done ({why})")
            emit(event="step", step=s.id, state="already_done", label=s.label, why=why)
            continue
        say(f"{s.label} ...")
        emit(event="step", step=s.id, state="start", label=s.label, why=why)
        try:
            if s.id == "client" and not ctx.client_info:
                raise Refusal("unexpected", "the client step isn't done and no client was given", step=s.label)
            res = s.run(ctx)
            ok, why = s.done(ctx)
            if not ok:
                raise Refusal("unexpected", f"the step finished but isn't complete: {why}", step=s.label)
        except Stopped:
            raise
        except StepFailed as e:
            return _fail(s.id, e, t_all)
        except Exception as e:  # noqa: BLE001
            return _fail(s.id, Refusal("unexpected", f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2500:]}",
                                       step=s.label), t_all)
        dt = round(time.time() - t0, 1)
        update_home(first_launch={"steps": {**(read_home().get("first_launch") or {}).get("steps", {}),
                                            s.id: {"done_at": _now(), "seconds": dt}}})
        say(f"   done ({dt} s)")
        emit(event="step", step=s.id, state="done", seconds=dt, result=res)
    src = (read_home().get("client_source") or {})
    update_home(first_launch={"complete": True, "completed_at": _now(), "last_failure": None})
    total = round(time.time() - t_all, 1)
    say(f"\nMoonglade is set up ({total} s). The folder you gave it ({src.get('path')}) can now be deleted: "
        "Moonglade keeps its own copy.")
    emit(event="end", ok=True, source=src.get("path"), source_kind=src.get("kind"),
         can_delete=bool(src.get("verified_at")), total_s=total)
    return EXIT_OK


def main(client: str | None) -> int:
    return run(client)
