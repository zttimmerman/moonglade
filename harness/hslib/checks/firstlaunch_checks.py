"""first-launch (openspec/changes/release-first-launch): the release app's first-launch window and setup steps.

firstlaunch.preflight  (quick) the first-launch engine (`-m hslib first-launch`, the payload's Python with this
                       checkout's harness, a scratch HOME) stops before writing anything but its log for each injected
                       problem: an Intel Mac, macOS 26.1, no Rosetta, Moonglade already running, and free space 1 GB
                       below the need (whose message names the needed and free GB and the disk); and for two scratch
                       clients whose sizes differ by a known amount the needed bytes differ by exactly that and equal
                       client size + the cited fixed part (firstlaunch.FIXED_BYTES + HEADROOM_BYTES). Counterexample:
                       with enough injected free space preflight passes (so the refusals above are the checks' doing).
firstlaunch.flow       (slow) a scratch copy of this run's packaged Moonglade.app, run directly (no LaunchServices,
                       see design "As built", TCC) with the test hook, a scratch HOME, scratch ports, the scripted pick
                       of the dev Mac's client/ (read-only source) and downloads from a loopback server serving the
                       cached pinned files: the window asks for the folder, runs every step in order with progress,
                       the bar never moves backward, the done screen says the folder can be deleted, the app exits 0
                       with nothing left running, and HOME holds a verified client copy, the play client, the pinned
                       server data and Wine runtime, databases that pass the database step's check and the defaults.
                       Counterexample (same scenario, "can be deleted only after the copy is verified"): with the
                       corrupt-copy test switch the client step fails with the plain message, "can now delete" is never
                       shown, and the next launch redoes the client copy (verified) before it continues.
firstlaunch.second_open_plays, .source_untouched, .client_refusals, .downloads, .kill_resume: see each docstring.
Shared helpers (for the other firstlaunch.* / release.* checks): LoopbackServer, ensure_cache, scratch_client,
run_engine, setup_events; the run's shared set-up HOME (fixture), the app's play loop with the stand-in (app_play,
play_ok) and task 4.1's measurements (measure_home).
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .. import paths as P
from ..registry import check, require
from .release_runtime_checks import (NEW_APP, SCRATCH_PORTS, SCRATCH_ROOT, _clean_env, _hook_events, _proc_info, _q,
                                     exec_with_hook, heal_scratch, quick_app, release_env, release_fixture_app)

CAP = "first-launch"
FL_CACHE = P.ENV / "release" / "firstlaunch-cache"     # the pinned Wine runtime + server data, verified (ensure_cache)
CACHE_PINS = ("wine-runtime", "client-data")


# ================================================================================================ helpers

def ensure_cache() -> dict:
    """The pinned files the loopback server serves, verified against fork.lock (downloaded once if missing)."""
    from .. import downloads
    out = {}
    for name in CACHE_PINS:
        got = downloads.obtain(name, cache=FL_CACHE)
        out[name] = {"file": Path(got["path"]).name, "from": got["from"], "bytes": Path(got["path"]).stat().st_size}
    return out


class LoopbackServer:
    """A loopback HTTP server for `root` (files by name). `mutate` = {name: byte offset} serves those files with one
    byte changed. Counts requests and bytes sent."""

    def __init__(self, root: Path, mutate: dict | None = None):
        self.root, self.mutate, self.requests = Path(root), dict(mutate or {}), []
        srv = self

        class H(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                name = self.path.lstrip("/").split("?", 1)[0]
                f = srv.root / name
                srv.requests.append(name)
                if "/" in name or not f.is_file():
                    self.send_error(404)
                    return
                size = f.stat().st_size
                self.send_response(200)
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Type", "application/octet-stream")
                self.end_headers()
                flip = srv.mutate.get(name)
                with open(f, "rb") as fh:
                    pos = 0
                    while True:
                        b = fh.read(1 << 20)
                        if not b:
                            break
                        if flip is not None and pos <= flip < pos + len(b):
                            b = bytearray(b)
                            b[flip - pos] ^= 0xFF
                            b = bytes(b)
                        try:
                            self.wfile.write(b)
                        except (BrokenPipeError, ConnectionResetError):
                            return
                        pos += len(b)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(root)))
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()


def scratch_client(d: Path, extra_bytes: int = 0) -> Path:
    """A tiny client folder: APFS clones of client/Wow.exe and Data/enUS/realmlist.wtf (write protection lifted on the
    clones only), plus a filler file of `extra_bytes`. Enough for the client check (version, enUS) and the disk need."""
    from .. import pristine
    d.mkdir(parents=True)
    (d / "Data" / "enUS").mkdir(parents=True)
    for rel in ("Wow.exe", "Data/enUS/realmlist.wtf"):
        subprocess.run(["/bin/cp", "-c", str(P.CLIENT / rel), str(d / rel)], check=True)
    pristine.unlock(d)                      # the clones inherit client/'s lock; d is a scratch folder
    if extra_bytes:
        with open(d / "filler.bin", "wb") as fh:
            fh.truncate(extra_bytes)
    return d


def setup_events(lines: str) -> list[dict]:
    out = []
    for line in lines.splitlines():
        if line.startswith("[bootstrap] "):
            try:
                out.append(json.loads(line[12:]))
            except ValueError:
                pass
    return out


def run_engine(app: Path, home: Path, args: list[str], env_extra: dict, cwd: Path, timeout: int = 120) -> dict:
    """The first-launch engine as the window runs it (payload Python, -I -B -m hslib first-launch), output appended to
    HOME/logs/first-launch.log like the launcher does. Returns rc, events, output."""
    env = release_env(home, PATH="/usr/bin:/bin:/usr/sbin:/sbin", **env_extra)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    logf = home / "logs" / "first-launch.log"
    start = logf.stat().st_size if logf.exists() else 0
    with open(logf, "ab") as fh:
        try:
            r = subprocess.run([str(app / "python/bin/python3"), "-I", "-B", "-m", "hslib", "first-launch", *args,
                                P.RELEASE_TEST_FLAG], stdout=fh, stderr=fh, env=env, cwd=cwd, timeout=timeout)
            rc = r.returncode
        except subprocess.TimeoutExpired:
            rc = "timeout"
    with open(logf, "rb") as fh:
        fh.seek(start)
        out = fh.read().decode("utf-8", "replace")
    return {"rc": rc, "events": setup_events(out), "out_tail": out[-1500:]}


def home_listing(home: Path) -> list[str]:
    return sorted(str(p.relative_to(home)) for p in home.rglob("*")) if home.exists() else []


def _failed(evs: list[dict]) -> dict | None:
    return next((e for e in evs if e.get("event") == "step" and e.get("state") == "failed"), None)


def _cleanup(d: Path) -> None:
    from .. import pristine
    for pinfo in _proc_info(lambda a: any(str(d) in x for x in a)):
        try:
            os.kill(pinfo["pid"], 9)
        except ProcessLookupError:
            pass
    if d.exists():
        pristine.unlock(d)                  # scratch HOMEs hold a write-protected client copy
        shutil.rmtree(d, ignore_errors=True)


# ================================================================================================ preflight

# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "harness/app/launcher.m",
    "fork.lock#wowsilicon_*",
    "fork.lock#client_data_*",
    "harness/hslib/checks/firstlaunch_checks.py",
    "harness/hslib/checks/release_checks.py",
    "harness/hslib/checks/release_runtime_checks.py",
    "harness/hslib/clientinst.py",
    "harness/hslib/downloads.py",
    "harness/hslib/firstlaunch.py",
    "harness/hslib/pe.py",
    "harness/hslib/pristine.py",
)


@check("firstlaunch.preflight", order=15, capability=CAP, stack="any", layer="unit", parallel_safe=True)
def preflight(ctx):
    """Each injected preflight problem stops before writing (HOME holds only the log) with its plain message; the disk
    need is the client's size + the cited fixed part, so two clients' needs differ by their size difference."""
    from .. import firstlaunch as FL
    heal_scratch()
    d = SCRATCH_ROOT / f"pf-{os.getpid()}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    ev: dict = {}
    try:
        app = quick_app(d)
        c1 = scratch_client(d / "c1")
        delta = 5_000_000_000
        c2 = scratch_client(d / "c2", extra_bytes=delta)        # sparse: no real disk use
        ports = {"HS_RELEASE_PORTS": json.dumps(SCRATCH_PORTS)}

        homes: list[str] = []

        def one(tag: str, inject: dict, client: Path | None) -> dict:
            homes.append(tag)
            home = d / f"h{len(homes)}"       # short: HOME/run/mysql.sock must fit 103 bytes (preflight checks it)
            r = run_engine(app, home, ["--client", str(client)] if client else [],
                           {**ports, P.FIRSTLAUNCH_TEST_ENV: json.dumps(inject)}, d)
            f = _failed(r["events"]) or {}
            return {"rc": r["rc"], "error_id": f.get("error_id"), "title": f.get("title"), "message": f.get("message"),
                    "evidence": f.get("evidence"), "home": home_listing(home),
                    "need_client": any(e.get("event") == "need_client" for e in r["events"]),
                    "out_tail": r["out_tail"][-400:] if not f else None}

        cases = {
            "intel": ({"arch": "x86_64"}, "not_apple_silicon", ["Apple Silicon", "Nothing was changed"]),
            "macos": ({"macos": "26.1"}, "macos_too_old", ["26.2", "26.1", "Software Update"]),
            "rosetta": ({"rosetta": False}, "no_rosetta", ["softwareupdate --install-rosetta", "Nothing was changed"]),
            "running": ({"running": True}, "already_running", ["is running right now", "Quit it first"]),
        }
        ev["cases"] = {}
        for tag, (inject, want, phrases) in cases.items():
            c = ev["cases"][tag] = one(tag, inject, c1)
            require(c["rc"] == 1 and c["error_id"] == want, f"{tag}: preflight didn't stop with {want}", **ev)
            require(all(p in (c["message"] or "") for p in phrases), f"{tag}: the message lacks {phrases}", **ev)
            require(c["home"] == ["logs", "logs/first-launch.log"], f"{tag}: HOME holds more than the log", **ev)
        # disk: the need with the client given, from a refusal at 0 free; then 1 GB below it
        n1 = one("need1", {"free_bytes": 0}, c1)
        n2 = one("need2", {"free_bytes": 0}, c2)
        need1 = (n1["evidence"] or {}).get("need_bytes")
        need2 = (n2["evidence"] or {}).get("need_bytes")
        size1 = sum(f.stat().st_size for f in c1.rglob("*") if f.is_file())
        fixed = sum(FL.FIXED_BYTES.values()) + FL.HEADROOM_BYTES
        ev["disk"] = {"need1": need1, "need2": need2, "client1_bytes": size1, "delta": delta, "fixed": fixed,
                      "fixed_parts": FL.FIXED_BYTES, "headroom": FL.HEADROOM_BYTES, "n1": n1, "n2": n2}
        require(n1["error_id"] == n2["error_id"] == "not_enough_space", "free space 0 wasn't refused", **ev)
        require(need1 == size1 + fixed, "the need isn't client size + the fixed part", **ev)
        require(need2 - need1 == delta, "the needs don't differ by the clients' size difference", **ev)
        short = one("short", {"free_bytes": need1 - 1_000_000_000}, c1)
        ev["disk"]["short"] = short
        gb = lambda b: f"{b / 1e9:.1f}"      # noqa: E731
        vol = ((short["evidence"] or {}).get("volume") or {}).get("name")
        require(short["error_id"] == "not_enough_space" and gb(need1) in short["message"]
                and gb(need1 - 1_000_000_000) in short["message"] and vol and vol in short["message"],
                "1 GB short: the message doesn't name the needed GB, the free GB and the disk", **ev)
        require(short["home"] == ["logs", "logs/first-launch.log"], "1 GB short: HOME holds more than the log", **ev)
        # without a client: stops after the Mac checks asking for one, writing nothing but the log
        ask = one("ask", {"free_bytes": 10**13}, None)
        ev["ask"] = ask
        require(ask["rc"] == FL.EXIT_NEED_CLIENT and ask["need_client"] and ask["home"] == ["logs",
                "logs/first-launch.log"], "without a client the engine didn't stop to ask for one", **ev)
        # counterexample: enough free space and a good client -> preflight passes; the client step copies it and
        # the data step stops at a closed loopback port (nothing is downloaded)
        ok = run_engine(app, d / "h0", ["--client", str(c1)],
                        {**ports, P.FIRSTLAUNCH_TEST_ENV: json.dumps({"free_bytes": 10**13}),
                         P.RELEASE_DOWNLOAD_BASE_ENV: "http://127.0.0.1:9/"}, d)
        pre_done = any(e.get("step") == "preflight" and e.get("state") == "done" for e in ok["events"])
        f = _failed(ok["events"]) or {}
        ev["counterexample"] = {"rc": ok["rc"], "preflight_done": pre_done, "failed": f.get("error_id"),
                                "failed_step": f.get("step"), "details": (f.get("details") or "")[-600:],
                                "steps": [(e.get("step"), e.get("state")) for e in ok["events"]
                                          if e.get("event") == "step"]}
        require(pre_done, "counterexample: preflight didn't pass with enough space", **ev)
    finally:
        _cleanup(d)
    return ev


# ================================================================================================ flow

def _client_check(home: Path) -> dict:
    from .. import clientinst
    man = clientinst.load_manifest(home / "client-manifest.json")
    if not man:
        return {"manifest": False}
    return {"manifest": True, "mismatches": clientinst.mismatches(home / "client", man)[:5],
            "files": len(man["files"]), "wow": (man.get("wow_exe_version") or {}).get("file_version")}


def home_end_state(home: Path) -> tuple[dict, list[str]]:
    """A finished first launch's HOME (firstlaunch.flow's end-state checks, shared with kill_resume): the client copy
    matches its record, the play client exists, server data and Wine runtime match their fork.lock pins, the four
    databases have tables and the database marker names this HOME, the default settings are written, home.json
    records completion and no partial file is left. Returns (state, problems)."""
    from .. import clientbuild
    cc = _client_check(home)
    lock = P.fork_lock()
    stamp = clientbuild.read_stamp(home / "client-play")
    try:
        cd_marker = json.loads((home / "client-data" / ".hs-client-data.json").read_text())
    except (OSError, ValueError):
        cd_marker = {}
    wine_marker = (home / "wine/runtime/.hs-runtime-sha256").read_text().strip() \
        if (home / "wine/runtime/.hs-runtime-sha256").exists() else None
    try:
        dbm = json.loads((home / ".hs-db-ready.json").read_text())
    except (OSError, ValueError):
        dbm = {}
    dbs = {n: any((home / "mysql" / n).glob("*.ibd")) for n in ("acore_auth", "acore_world", "acore_characters",
                                                                 "acore_playerbots")}
    try:
        settings = json.loads((home / "settings.json").read_text())
    except (OSError, ValueError):
        settings = {}
    try:
        homej = json.loads((home / "home.json").read_text())
    except (OSError, ValueError):
        homej = {}
    st = {"client": cc, "play_client_stamp": bool(stamp), "client_data_sha": cd_marker.get("sha256"),
          "wine_marker": wine_marker, "db_marker": dbm, "dbs": dbs, "settings": settings, "home_json": homej,
          "leftover_parts": [str(p) for p in home.rglob("*") if p.name.endswith((".part", ".hs-part", ".unpack"))]}
    probs = []
    if not (cc.get("manifest") and not cc.get("mismatches") and cc.get("wow") == "3.3.5.12340"):
        probs.append("HOME's client copy doesn't match its record")
    if not (stamp and (home / "client-play/Wow.exe").is_file()):
        probs.append("no play client in HOME")
    if not (cd_marker.get("sha256") == lock["client_data_sha256"] and wine_marker == lock["wowsilicon_runtime_sha256"]):
        probs.append("the server data / Wine runtime don't match their pins")
    if not (all(dbs.values()) and dbm.get("home") == str(home)):
        probs.append("the databases didn't pass the step's check")
    if not (settings.get("world_size") and settings.get("client")):
        probs.append("the default settings weren't written")
    if (homej.get("first_launch") or {}).get("complete") is not True:
        probs.append("home.json doesn't record completion")
    if st["leftover_parts"]:
        probs.append("partial files left in HOME")
    return st, probs


ENOUGH_FREE = {"free_bytes": 10**13}      # injected free space (the disk rule is firstlaunch.preflight's to check)


@check("firstlaunch.flow", order=15, capability=CAP, stack="down", layer="gate")
def flow(ctx):
    """Fresh scratch HOME to ready through the release app's first-launch window (test hook, scripted pick of
    client/, loopback downloads); counterexample: a corrupted copy is never called deletable and is redone."""
    from .. import pristine
    from .release_checks import _need_payload
    healed = heal_scratch()
    ev: dict = {"healed": healed, "started": time.time()}
    _need_payload(ctx)
    ev["cache"] = ensure_cache()
    d = SCRATCH_ROOT / f"fl-{os.getpid()}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    src = P.CLIENT
    fp0 = pristine.fingerprint(src)
    try:
        app = release_fixture_app(d)
        bundle = app.parents[2]
        home = d / "h s"
        (d / "tmp").mkdir()
        with LoopbackServer(FL_CACHE) as srv:
            base_env = {P.RELEASE_HOME_ENV: str(home), P.RELEASE_PORTS_ENV: json.dumps(SCRATCH_PORTS),
                        P.RELEASE_DOWNLOAD_BASE_ENV: srv.base, P.HARNESS_RUN_REGISTRY_ENV: str(P.RUN_REGISTRY),
                        "TMPDIR": str(d / "tmp") + "/"}
            hook = d / "hook.jsonl"
            t0 = time.time()
            free_before = shutil.disk_usage(d).free
            # preflight's free-space rule has its own check (firstlaunch.preflight); here free space is injected so the
            # flow doesn't depend on the dev Mac's disk (the client is cloned, so real use is far below the need)
            r = exec_with_hook(bundle, hook, {**base_env, P.APP_TEST_CLIENT_PICK_ENV: f"{src};quit",
                                              P.FIRSTLAUNCH_TEST_ENV: json.dumps(ENOUGH_FREE)},
                               [P.RELEASE_TEST_FLAG], timeout=2400)
            ev["run_s"] = round(time.time() - t0, 1)
            evs = r["events"]
            ev["requests"] = srv.requests
        win = [e for e in evs if e.get("kind") == "setup_window"]
        fl = [e for e in evs if e.get("kind") == "firstlaunch"]
        steps = [(e.get("step"), e.get("state")) for e in fl if e.get("event") == "step"]
        prog = [e for e in fl if e.get("event") == "progress"]
        bars = [e.get("bar") for e in evs if e.get("kind") in ("firstlaunch", "setup_window")]   # in order
        done_win = next((e for e in win if e.get("state") == "done"), {})
        ev["app"] = {"rc": r["rc"], "windows": [(e["state"], e["title"]) for e in win], "steps": steps,
                     "progress": {k: [(p["done"], p["total"]) for p in prog if p.get("name") == k][-3:]
                                  for k in {p.get("name") for p in prog}},
                     "done_text": done_win.get("text"), "exit": [e for e in evs if e.get("kind") == "exit"]}
        require(r["rc"] == 0 and any(e.get("kind") == "exit" and e.get("code") == 0 for e in evs),
                "the app didn't exit 0", **ev, log_tail=(home / "logs/first-launch.log").read_text(
                    errors="replace")[-2000:] if (home / "logs/first-launch.log").exists() else None)
        require([w["state"] for w in win][:2] == ["progress", "choose"] and done_win,
                "the window didn't ask for the folder and end on the done screen", **ev)
        order = [s for s, st in steps if st == "done"]
        require(order == ["preflight", "client", "data", "wine", "database", "playclient", "defaults"],
                "the steps didn't all run, in order", **ev)
        for name in ("Data.zip", "wowsilicon-runtime.tar.xz", "copy"):
            ps = [(p["done"], p["total"]) for p in prog if p.get("name") == name]
            require(ps and all(b[0] >= a[0] for a, b in zip(ps, ps[1:])) and ps[-1][0] == ps[-1][1],
                    f"progress for {name} went backward or didn't reach its total", **ev)
        require(all(b is not None for b in bars) and all(y >= x for x, y in zip(bars, bars[1:])),
                "the bar moved backward", **ev, bars=bars)
        require("can now delete" in (done_win.get("text") or "") and str(src) in done_win.get("text", ""),
                "the done screen doesn't say the folder given can be deleted", **ev)
        # HOME's end state
        ev["home"], probs = home_end_state(home)
        for why in probs:
            require(False, why, **ev)
        require(not _proc_info(lambda a: any(str(d) in x for x in a)), "processes of the scratch HOME remain", **ev)
        ev["measure"] = measure_home(home, free_before)
        fp1 = pristine.fingerprint(src)
        ev["source_unchanged"] = not pristine.diff(fp0, fp1)
        require(ev["source_unchanged"], "the source client changed", **ev, diff=pristine.diff(fp0, fp1)[:5])

        # counterexample: a corrupted copy is never called deletable; the next launch redoes the copy
        home2 = d / "h2 s"
        hook2 = d / "hook2.jsonl"
        with LoopbackServer(FL_CACHE) as srv:
            env2 = {**base_env, P.RELEASE_HOME_ENV: str(home2), P.RELEASE_DOWNLOAD_BASE_ENV: srv.base,
                    P.APP_TEST_CLIENT_PICK_ENV: f"{src};quit",
                    P.FIRSTLAUNCH_TEST_ENV: json.dumps({"corrupt_copy": True, **ENOUGH_FREE})}
            r2 = exec_with_hook(bundle, hook2, env2, [P.RELEASE_TEST_FLAG], timeout=600)
        evs2 = r2["events"]
        win2 = [e for e in evs2 if e.get("kind") == "setup_window"]
        err = next((e for e in win2 if e.get("state") == "error"), {})
        texts = " ".join(str(e.get("text")) for e in evs2)
        ev["corrupt"] = {"rc": r2["rc"], "windows": [(e["state"], e["title"]) for e in win2],
                         "error_text": err.get("text"), "client": _client_check(home2),
                         "test_event": [e for e in evs2 if e.get("event") == "test"]}
        require(err.get("title") == "The copy of your game didn't check out" and r2["rc"] == 1,
                "counterexample: the corrupted copy didn't stop the client step with its message", **ev)
        require("can now delete" not in texts and not any(e.get("state") == "done" for e in win2),
                "counterexample: 'can be deleted' was shown for a corrupted copy", **ev)
        require(not ev["corrupt"]["client"].get("manifest"), "counterexample: the corrupted copy got a record", **ev)
        # next launch (the engine, as the window runs it): the client step runs again and verifies; the data step
        # then stops at a closed loopback port (download_failed), so this stays short
        nxt = run_engine(app, home2, ["--client", str(src)], {P.RELEASE_PORTS_ENV: json.dumps(SCRATCH_PORTS),
                                                               P.FIRSTLAUNCH_TEST_ENV: json.dumps(ENOUGH_FREE),
                                                               P.RELEASE_DOWNLOAD_BASE_ENV: "http://127.0.0.1:9/",
                                                               "TMPDIR": str(d / "tmp") + "/"}, d, timeout=600)
        st = [(e.get("step"), e.get("state")) for e in nxt["events"] if e.get("event") == "step"]
        f2 = _failed(nxt["events"]) or {}
        ev["corrupt"]["next"] = {"rc": nxt["rc"], "steps": st, "failed": f2.get("error_id"),
                                 "message": f2.get("message"), "client": _client_check(home2)}
        require(("client", "done") in st and ev["corrupt"]["next"]["client"].get("manifest")
                and not ev["corrupt"]["next"]["client"].get("mismatches"),
                "counterexample: the next launch didn't redo the client copy", **ev)
        require(f2.get("error_id") == "download_failed" and "internet connection" in (f2.get("message") or ""),
                "the closed download port didn't give the no-connection message", **ev)
        _cleanup(home2)
        adopt_fixture(d, bundle, home, "firstlaunch.flow (the app's first-launch window)")
        ev["fixture"] = "kept as this run's shared set-up HOME (removed when the run ends)"
    finally:
        if _FIXTURE.get("d") != str(d):
            _cleanup(d)
    return ev


# ================================================================================================ shared fixture
# One set-up scratch HOME per run (design "Checks": one shared first-launch fixture per run). firstlaunch.flow makes
# it through the app's first-launch window and keeps it; a check that runs without flow (a filtered run) makes it
# with the engine. Checks that use it run after flow (orders 15-17), in place and one at a time; release.app_moved
# moves its app. It is removed when the harness run ends (atexit), and heal_scratch removes it after a killed run.

_FIXTURE: dict = {}
_ATEXIT = []


def _fixture_cleanup() -> None:
    d = _FIXTURE.get("d")
    _FIXTURE.clear()
    if d:
        _cleanup(Path(d))


def adopt_fixture(d: Path, bundle: Path, home: Path, how: str) -> dict:
    if not _ATEXIT:
        import atexit
        atexit.register(_fixture_cleanup)
        _ATEXIT.append(True)
    _FIXTURE.clear()
    _FIXTURE.update({"d": str(d), "bundle": str(bundle), "home": str(home), "made_by": how, "pid": os.getpid()})
    return dict(_FIXTURE)


def _fixture_ok() -> bool:
    if not _FIXTURE or _FIXTURE.get("pid") != os.getpid():
        return False
    home, bundle = Path(_FIXTURE["home"]), Path(_FIXTURE["bundle"])
    try:
        done = json.loads((home / "home.json").read_text())["first_launch"]["complete"] is True
    except (OSError, ValueError, KeyError, TypeError):
        done = False
    return done and (bundle / "Contents/MacOS/Moonglade").is_file()


def fixture(ctx) -> dict:
    """{d, bundle, home, made_by}: a scratch HOME first launch completed (from client/, loopback downloads), with the
    app copy that set it up. Made once per run."""
    if _fixture_ok():
        return dict(_FIXTURE)
    from .release_checks import _need_payload
    _need_payload(ctx)
    ensure_cache()
    d = SCRATCH_ROOT / f"fx-{os.getpid()}"
    _cleanup(d)
    d.mkdir(parents=True)
    app = release_fixture_app(d)
    home = d / "h s"
    (d / "tmp").mkdir()
    with LoopbackServer(FL_CACHE) as srv:
        r = run_engine(app, home, ["--client", str(P.CLIENT)],
                       {P.RELEASE_PORTS_ENV: json.dumps(SCRATCH_PORTS), P.RELEASE_DOWNLOAD_BASE_ENV: srv.base,
                        "TMPDIR": str(d / "tmp") + "/"}, d, timeout=2400)
    if r["rc"] != 0:
        _cleanup(d)
        require(False, "the shared fixture's first launch failed", fixture_engine=r)
    return adopt_fixture(d, app.parents[2], home, "the first-launch engine (firstlaunch.flow didn't run first)")


def measure_home(home: Path, free_before: int | None = None) -> dict:
    """Task 4.1: HOME's size per folder (du: a clone counts in full), the disk really used (free space before minus
    after: clones share blocks), first launch's time per step and the database first start's phases."""
    sizes = {}
    for x in sorted(home.iterdir()):
        r = subprocess.run(["/usr/bin/du", "-sk", str(x)], capture_output=True, text=True)
        try:
            sizes[x.name] = int(r.stdout.split()[0]) * 1024
        except (IndexError, ValueError):
            sizes[x.name] = None
    try:
        hj = json.loads((home / "home.json").read_text())
    except (OSError, ValueError):
        hj = {}
    try:
        db = json.loads((home / "logs/first-launch-database.json").read_text())
    except (OSError, ValueError):
        db = {}
    ph, ll = db.get("phases_s") or {}, db.get("log_lines") or {}

    def span(a, b):
        return round(b - a, 1) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
    split = {"initialise_s": span(ph.get("initialise_start"), ph.get("initialise_end")),
             # SQL import: from the first "auto populating" to the last "is up-to-date"/"Applying" line
             "sql_import_s": span((ll.get("populating") or {}).get("first_s"),
                                  max([(ll.get(k) or {}).get("last_s") or 0 for k in ("up_to_date", "applying")])),
             # bot setup: the last SQL import line to world-ready (SOAP): the world loading and playerbots creating
             # and logging in its random bots (playerbots' own lines run from its schema import to here)
             "bot_setup_s": span(max([(ll.get(k) or {}).get("last_s") or 0 for k in ("up_to_date", "applying",
                                                                                     "populating")]), ph.get("ready")),
             "shutdown_s": span(ph.get("down_start"), ph.get("down_end")), "first_start_total_s": ph.get("ready")}
    free_after = shutil.disk_usage(home).free
    return {"du_bytes": sizes, "du_total_bytes": sum(v for v in sizes.values() if v),
            "disk_used_bytes": (free_before - free_after) if free_before is not None else None,
            "steps_s": {k: (v or {}).get("seconds") for k, v in ((hj.get("first_launch") or {}).get("steps") or {}).items()},
            "client_copy": (hj.get("client_source") or {}).get("copy"),
            "database_split": split, "database_phases": ph, "database_log_lines": ll}


# ================================================================================================ the app's play loop

def standin(d: Path, tag: str) -> dict:
    """play_checks' stand-in .bat (exits when its stop file appears) in a scratch folder of this check."""
    si = d / f"si-{tag}"
    si.mkdir(parents=True, exist_ok=True)
    win = lambda p: "Z:" + str(p).replace("/", "\\")      # noqa: E731
    bat, started, stop = si / "hs-standin.bat", si / "started", si / "stop"
    bat.write_text("\r\n".join(["@echo off", f"echo started> {win(started)}", ":loop",
                                f"if exist {win(stop)} goto done", "ping -n 2 127.0.0.1 >nul", "goto loop", ":done",
                                "exit 0", ""]))
    return {"dir": si, "bat": bat, "started": started, "stop": stop}


def server_of_home(argv: list[str], env: dict, home: Path) -> bool:
    """Whether an authserver/worldserver (its argv and environment) is the one of release HOME `home`. Its argv names
    HOME when the -c path is absolute; under a HOME with a space (the real one is ~/Library/Application Support/...)
    -c is relative to HOME/run (stack.conf_value), so then its environment does: release mode sets OPENSSL_CONF to
    HOME's openssl.cnf (stack.spawn_env)."""
    h = str(home).rstrip("/") + "/"
    return any(h in x for x in argv) or str(env.get("OPENSSL_CONF") or "").startswith(h)


def app_play(bundle: Path, home: Path, d: Path, tag: str, *, pick: str | None = None, extra_env: dict | None = None,
             sandbox: Path | None = None, timeout: float = 1500, stop_after_ready: bool = True) -> dict:
    """The release app (launcher binary run directly, test hook, no LaunchServices) with a scratch HOME and ports,
    the stand-in client (`--client-cmd`) and m0. Waits for the stand-in and the play loop's "ready" stage, notes what
    runs, then stops the stand-in and waits for the app to exit. `pick` scripts a first-launch folder pick (then the
    done screen's Play continues into the play loop when the script says `play`)."""
    s = standin(d, tag)
    hook = d / f"hook-{tag}.jsonl"
    hook.unlink(missing_ok=True)
    (d / "tmp").mkdir(exist_ok=True)
    env = _clean_env(HS_APP_TEST_HOOK=str(hook), HS_PROFILE="m0", TMPDIR=str(d / "tmp") + "/",
                     **{P.RELEASE_HOME_ENV: str(home), P.RELEASE_PORTS_ENV: json.dumps(SCRATCH_PORTS),
                        P.HARNESS_RUN_REGISTRY_ENV: str(P.RUN_REGISTRY)}, **(extra_env or {}))
    if pick is not None:
        env[P.APP_TEST_CLIENT_PICK_ENV] = pick
    cmd = [str(bundle / "Contents/MacOS/Moonglade"), P.RELEASE_TEST_FLAG, "--client-cmd", str(s["bat"])]
    if sandbox is not None:
        cmd = ["/usr/bin/sandbox-exec", "-f", str(sandbox), *cmd]
    t0 = time.time()
    proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    ev: dict = {"tag": tag, "cmd": [Path(c).name if "/" in c else c for c in cmd[:3]]}
    app_dir = bundle / "Contents/Resources/Moonglade"
    ready_at = up_at = None
    try:
        while time.time() - t0 < timeout and proc.poll() is None:
            evs = _hook_events(hook)
            if ready_at is None and any(e.get("kind") == "stage" and e.get("id") == "ready" for e in evs):
                ready_at = round(time.time() - t0, 1)
            if up_at is None and s["started"].exists():
                up_at = round(time.time() - t0, 1)
            if ready_at is not None and up_at is not None:
                break
            time.sleep(1)
        my_cnf = f"--defaults-file={home / 'my.cnf'}"
        running = {"mysqld": _proc_info(lambda a: my_cnf in a),
                   **{n: [x for x in _proc_info(lambda a, n=n: a[:1] and a[0].endswith(f"/server/bin/{n}"))
                          if server_of_home(x["argv"], x["env"], home)] for n in ("worldserver", "authserver")}}
        ev["while_ready"] = {k: [{"pid": x["pid"], "exe": x["exe"]} for x in v] for k, v in running.items()}
        ev["while_ready_env"] = {k: {kk: vv for kk, vv in (v[0]["env"] if v else {}).items()
                                     if kk in ("OPENSSL_CONF", "AC_AI_PLAYERBOT_MAX_RANDOM_BOTS")}
                                 for k, v in running.items()}
        ev["app_dir"] = str(app_dir)
        if stop_after_ready:
            s["stop"].write_text("quit")
        try:
            rc = proc.wait(timeout=600)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, 9)
            rc = "timeout"
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, 9)
            except ProcessLookupError:
                pass
    evs = _hook_events(hook)
    try:
        last = json.loads((home / "run/play-last.json").read_text())
    except (OSError, ValueError):
        last = {}
    ev |= {"rc": rc, "s": round(time.time() - t0, 1), "ready_at_s": ready_at, "standin_up_at_s": up_at,
           "stages": [(e.get("id"), e.get("state")) for e in evs if e.get("kind") == "stage"][-12:],
           "setup_windows": [(e.get("state"), e.get("title")) for e in evs if e.get("kind") == "setup_window"],
           "firstlaunch_steps": [(e.get("step"), e.get("state")) for e in evs
                                 if e.get("kind") == "firstlaunch" and e.get("event") == "step"],
           "dialogs": [e for e in evs if e.get("kind") == "dialog"][:3],
           "exit": [e.get("code") for e in evs if e.get("kind") == "exit"],
           "play_last": {k: last.get(k) for k in ("pid", "outcome", "exit_code", "account", "backup", "cleanup",
                                                 "backup_plan")},
           "remaining": [{"pid": x["pid"], "argv": x["argv"][:2]} for x in
                         _proc_info(lambda a: any(str(home) in x or str(bundle) in x for x in a))]}
    ev["_events"] = evs
    shutil.rmtree(s["dir"], ignore_errors=True)
    return ev


def play_ok(r: dict) -> list[str]:
    """Why a play-loop run (app_play) isn't a clean stand-in session reaching ready."""
    pl = r["play_last"]
    down = ((pl.get("cleanup") or {}).get("down") or {})
    probs = []
    if r["ready_at_s"] is None or r["standin_up_at_s"] is None:
        probs.append("the stand-in / the ready stage never came")
    if r["rc"] != 0 or r["exit"][-1:] != [0]:
        probs.append(f"the app exited {r['rc']} (hook {r['exit']})")
    if pl.get("outcome") != "client_exit" or pl.get("exit_code") != 0:
        probs.append(f"the play loop ended {pl.get('outcome')} / {pl.get('exit_code')}")
    if not ((down.get("worldserver") or {}).get("graceful") is True and down.get("graceful") is True
            and not down.get("remaining")):
        probs.append(f"the stack didn't stop gracefully: {json.dumps(down)[:300]}")
    if r["remaining"]:
        probs.append(f"processes remain: {r['remaining'][:3]}")
    if r["dialogs"]:
        probs.append(f"a dialog was shown: {r['dialogs'][:1]}")
    return probs


def _slim(r: dict) -> dict:
    return {k: v for k, v in r.items() if k != "_events"}


# ================================================================================================ second_open_plays

@check("firstlaunch.second_open_plays", order=15, capability=CAP, stack="down", layer="gate")
def second_open_plays(ctx):
    """A scratch HOME first launch completed (the run's shared fixture): opening the release app again (test hook,
    stand-in client) shows no first-launch window and the play loop's stages run to ready; the stand-in quits and the
    app exits 0 with the stack stopped gracefully. Counterexample: a HOME whose home.json doesn't say complete gets
    the first-launch window, which the same assertion sees."""
    heal_scratch()
    fx = fixture(ctx)
    ev: dict = {"started": time.time(), "fixture": fx}
    d, home, bundle = Path(fx["d"]), Path(fx["home"]), Path(fx["bundle"])
    r = app_play(bundle, home, d, "second")
    ev["second_open"] = _slim(r)
    ev["measure_after_play"] = {x: int(subprocess.run(["/usr/bin/du", "-sk", str(home / x)], capture_output=True,
                                                      text=True).stdout.split()[0]) * 1024
                                for x in ("wine", "mysql", "backups") if (home / x).exists()}
    require(not r["setup_windows"] and not r["firstlaunch_steps"], "the second open showed the first-launch window",
            **ev)
    probs = play_ok(r)
    require(not probs, f"the play loop didn't run to ready and stop cleanly: {probs}", **ev)
    # counterexample: setup not complete -> the first-launch window (scripted pick: quit at the folder question)
    c = d / "cx"
    _cleanup(c)
    (c / "h" / "logs").mkdir(parents=True)
    (c / "h" / "home.json").write_text(json.dumps({"layout": P.HOME_LAYOUT_VERSION,
                                                   "first_launch": {"complete": False}}) + "\n")
    try:
        rc = app_play(bundle, c / "h", c, "cx", pick="quit", timeout=120, stop_after_ready=False)
        ev["counterexample_incomplete"] = {k: rc[k] for k in ("rc", "setup_windows", "stages", "exit")}
        require(rc["setup_windows"] and rc["ready_at_s"] is None,
                "counterexample: an incomplete HOME didn't get the first-launch window", **ev)
    finally:
        _cleanup(c)
    return ev


# ================================================================================================ source_untouched

def source_fingerprint(root: Path) -> dict:
    """Every path under root (and root): type, size, mtime_ns, mode, flags; SHA-256 of .exe/.dll/.wtf files."""
    import hashlib
    out = {}
    for p in [root, *sorted(root.rglob("*"))]:
        st = p.lstat()
        rec = [st.st_mode, st.st_size if not p.is_dir() else 0, st.st_mtime_ns, st.st_flags]
        if p.is_file() and not p.is_symlink() and p.suffix.lower() in (".exe", ".dll", ".wtf"):
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for ch in iter(lambda: fh.read(1 << 20), b""):
                    h.update(ch)
            rec.append(h.hexdigest())
        out[str(p.relative_to(root)) if p != root else "."] = rec
    return out


def _fp_diff(a: dict, b: dict) -> list[str]:
    return sorted({k for k in set(a) | set(b) if a.get(k) != b.get(k)})[:20]


def _deny_lines(since: str, path: Path) -> list[str]:
    """Sandbox denials naming `path` in the unified log since `since` ("YYYY-MM-DD HH:MM:SS")."""
    r = subprocess.run(["/usr/bin/log", "show", "--style", "compact", "--start", since, "--predicate",
                        f'eventMessage CONTAINS "deny" AND eventMessage CONTAINS "{path}"'],
                       capture_output=True, text=True, timeout=300)
    return [x.strip()[:300] for x in r.stdout.splitlines() if "deny" in x and str(path) in x]


def _src_profile(d: Path, src: Path) -> str:
    from .release_runtime_checks import sandbox_profile
    return sandbox_profile(d) + f'(deny file-write* (subpath "{src.resolve()}"))\n'


def _engine_sandboxed(app: Path, home: Path, args: list[str], env_extra: dict, d: Path, prof: Path,
                      timeout: int = 2400, kill_when=None) -> dict:
    """run_engine under sandbox-exec; `kill_when(events)` -> True SIGKILLs the engine and everything naming HOME."""
    env = release_env(home, PATH="/usr/bin:/bin:/usr/sbin:/sbin", **env_extra)
    (home / "logs").mkdir(parents=True, exist_ok=True)
    logf = home / "logs" / "first-launch.log"
    start = logf.stat().st_size if logf.exists() else 0
    killed = None
    with open(logf, "ab") as fh:
        proc = subprocess.Popen(["/usr/bin/sandbox-exec", "-f", str(prof), str(app / "python/bin/python3"), "-I",
                                 "-B", "-m", "hslib", "first-launch", *args, P.RELEASE_TEST_FLAG], stdout=fh,
                                stderr=fh, env=env, cwd=d, start_new_session=True)
        t0 = time.time()
        while proc.poll() is None and time.time() - t0 < timeout:
            if kill_when is not None:
                with open(logf, "rb") as rf:
                    rf.seek(start)
                    evs = setup_events(rf.read().decode("utf-8", "replace"))
                if kill_when(evs):
                    killed = kill_tree(proc, home)
                    break
            time.sleep(0.2)
        if proc.poll() is None:
            killed = killed or kill_tree(proc, home)
        rc = proc.wait()
    with open(logf, "rb") as rf:
        rf.seek(start)
        out = rf.read().decode("utf-8", "replace")
    return {"rc": rc, "killed": killed, "events": setup_events(out), "out_tail": out[-1200:]}


def kill_tree(proc: subprocess.Popen, home: Path) -> dict:
    """SIGKILL the engine's process group and every process naming HOME (mysqld/servers started in their own
    sessions): first launch "killed with its children"."""
    t = time.time()
    try:
        os.killpg(proc.pid, 9)
    except ProcessLookupError:
        pass
    others = [x["pid"] for x in _proc_info(lambda a: any(str(home) in x for x in a))]
    for pid in others:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    proc.wait()
    return {"at": t, "others": others}


@check("firstlaunch.source_untouched", order=15, capability=CAP, stack="down", layer="gate")
def source_untouched(ctx):
    """A scratch client folder (an APFS clone of client/, its write protection lifted so only the sandbox stands in
    the way) fingerprinted (every path, size, mtime, mode, flags; .exe/.dll/.wtf hashes); first launch runs from it to
    the end under a sandbox profile denying writes to it, and again on a second HOME killed during the client copy
    (byte-copy path) and resumed through the client step: the fingerprint is unchanged and the unified log shows no
    sandbox denial naming it. Counterexample: a deliberate write into it under the same profile is denied and that
    denial shows up in the same log query (so an empty result means something)."""
    from .release_checks import _need_payload
    heal_scratch()
    _need_payload(ctx)
    ensure_cache()
    ev: dict = {"started": time.time()}
    d = SCRATCH_ROOT / f"su-{os.getpid()}"
    _cleanup(d)
    d.mkdir(parents=True)
    from .. import pristine
    try:
        src = d / "src"
        subprocess.run(["/bin/cp", "-c", "-R", str(P.CLIENT) + "/", str(src)], check=True)
        pristine.unlock(src)
        fp0 = source_fingerprint(src)
        ev["source"] = {"entries": len(fp0)}
        prof = d / "src.sb"
        prof.write_text(_src_profile(d, src))
        app = release_fixture_app(d)
        since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 2))
        (d / "tmp").mkdir()
        base = {P.RELEASE_PORTS_ENV: json.dumps(SCRATCH_PORTS), "TMPDIR": str(d / "tmp") + "/"}
        with LoopbackServer(FL_CACHE) as srv:
            full = _engine_sandboxed(app, d / "h1", ["--client", str(src)],
                                     {**base, P.RELEASE_DOWNLOAD_BASE_ENV: srv.base}, d, prof)
        ev["full"] = {"rc": full["rc"], "steps": [(e.get("step"), e.get("state")) for e in full["events"]
                                                  if e.get("event") == "step"]}
        require(full["rc"] == 0, "first launch from the scratch client didn't complete", **ev,
                tail=full["out_tail"])
        fp1 = source_fingerprint(src)
        ev["after_full"] = {"differences": _fp_diff(fp0, fp1)}
        require(fp0 == fp1, "the source changed during first launch", **ev)
        # killed during the client copy (byte copy), then resumed through the client step
        seen = {"n": 0}

        def in_copy(evs):
            n = sum(1 for e in evs if e.get("event") == "progress" and e.get("step") == "client")
            seen["n"] = n
            return n >= 3
        k = _engine_sandboxed(app, d / "h2 s", ["--client", str(src)],
                              {**base, P.FIRSTLAUNCH_TEST_ENV: json.dumps({"force_byte_copy": True,
                                                                          "free_bytes": 10**13}),
                               P.RELEASE_DOWNLOAD_BASE_ENV: "http://127.0.0.1:9/"}, d, prof, kill_when=in_copy)
        partial = [str(p.relative_to(d / "h2 s")) for p in (d / "h2 s").rglob("*.hs-part")][:3]
        # free space injected as for the killed run: the disk rule is firstlaunch.preflight's check, and this Mac's
        # free space (the run's other scratch HOMEs on it) must not decide whether the copy resumes
        res = _engine_sandboxed(app, d / "h2 s", ["--client", str(src)],
                                {**base, P.FIRSTLAUNCH_TEST_ENV: json.dumps({"free_bytes": 10**13}),
                                 P.RELEASE_DOWNLOAD_BASE_ENV: "http://127.0.0.1:9/"}, d, prof)
        rst = [(e.get("step"), e.get("state")) for e in res["events"] if e.get("event") == "step"]
        ev["killed_copy"] = {"killed": bool(k["killed"]), "copy_progress_events": seen["n"],
                             "partials_after_kill": partial, "resume_steps": rst, "resume_rc": res["rc"],
                             "client": _client_check(d / "h2 s")}
        if res["rc"] != 0:
            ev["killed_copy"]["resume_tail"] = res["out_tail"]
        require(k["killed"] and seen["n"] >= 3, "the run wasn't killed during the client copy", **ev)
        require(("client", "done") in rst and ev["killed_copy"]["client"].get("manifest")
                and not ev["killed_copy"]["client"].get("mismatches"),
                "the resumed run didn't finish the client copy", **ev)
        fp2 = source_fingerprint(src)
        ev["after_kill_resume"] = {"differences": _fp_diff(fp0, fp2)}
        require(fp0 == fp2, "the source changed during the killed / resumed copy", **ev)
        time.sleep(2)
        denials = _deny_lines(since, src.resolve())
        ev["sandbox_denials"] = denials[:5]
        require(not denials, "the sandbox denied a write to the source (something tried to write it)", **ev)
        # counterexample: a write into the source under the same profile is denied, and the log query finds it
        since2 = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 2))
        w = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(prof), "/usr/bin/touch", str(src / "WTF-written")],
                           capture_output=True, text=True)
        time.sleep(3)
        cx = _deny_lines(since2, src.resolve())
        ev["counterexample"] = {"touch_rc": w.returncode, "denials": cx[:2],
                                "fingerprint_differences": _fp_diff(fp0, source_fingerprint(src))}
        require(w.returncode != 0 and cx, "counterexample: a write into the source wasn't denied and logged", **ev)
    finally:
        _cleanup(d)
    return ev


# ================================================================================================ client_refusals

def _fakes(f: Path) -> dict:
    """The scenario's bad clients, under `f`. Returns {tag: path}."""
    import zipfile
    f.mkdir(parents=True)
    (f / "empty").mkdir()
    (f / "notes.txt").write_text("not a game\n")
    rep = f / "repair"
    (rep / "Data" / "enUS").mkdir(parents=True)
    subprocess.run(["/bin/cp", "-c", str(P.CLIENT / "Repair.exe"), str(rep / "Wow.exe")], check=True)
    subprocess.run(["/bin/cp", "-c", str(P.CLIENT / "Data/enUS/realmlist.wtf"), str(rep / "Data/enUS/realmlist.wtf")],
                   check=True)
    from .. import pristine
    pristine.unlock(rep)
    with zipfile.ZipFile(f / "wow-de.zip", "w", zipfile.ZIP_STORED) as z:
        z.write(P.CLIENT / "Wow.exe", "World of Warcraft/Wow.exe")
        z.write(P.CLIENT / "Data/enUS/realmlist.wtf", "World of Warcraft/Data/deDE/realmlist.wtf")
    locked = f / "locked"
    locked.mkdir()
    (locked / "Wow.exe").write_text("x")
    os.chmod(locked, 0)
    return {"empty": f / "empty", "text": f / "notes.txt", "repair": rep, "dezip": f / "wow-de.zip",
            "missing": f / "missing", "unreadable": locked}


def only_logs(home: Path) -> bool:
    return all(x == "logs" or x.startswith("logs/") for x in home_listing(home))


@check("firstlaunch.client_refusals", order=15, capability=CAP, stack="down", layer="integration")
def client_refusals(ctx):
    """Through the release app's scripted folder pick (test hook), each bad client is refused before anything is
    copied, with a plain message naming the problem (the found version for the wrong build), what is needed
    (3.3.5a) and that no files were changed, and "Choose Again": an empty folder, a text file, a folder whose Wow.exe is
    the client's own Repair.exe, a deDE-shaped zip, a missing path and an unreadable folder; HOME holds only logs.
    Counterexample: the same pick with a good (tiny) client passes preflight and copies it, so HOME holds more."""
    from ..pe import version_info
    heal_scratch()
    ev: dict = {"started": time.time()}
    d = SCRATCH_ROOT / f"cr-{os.getpid()}"
    _cleanup(d)
    d.mkdir(parents=True)
    try:
        app = quick_app(d, with_launcher=True)
        bundle = app.parents[2]
        fk = _fakes(d / "f")
        repair_ver = version_info((P.CLIENT / "Repair.exe").read_bytes())["file_version"]
        want = {"empty": ("not_wow_folder", ["is empty"]),
                "text": ("not_wow_folder", ["is a file"]),
                "repair": ("wrong_version", [repair_ver]),
                "dezip": ("not_english", ["deDE"]),
                "missing": ("folder_missing", ["couldn't find"]),
                "unreadable": ("cant_read_folder", ["didn't let Moonglade read"])}
        order = list(want)
        home = d / "h s"
        hook = d / "hook.jsonl"
        env = {P.RELEASE_HOME_ENV: str(home), P.RELEASE_PORTS_ENV: json.dumps(SCRATCH_PORTS),
               P.RELEASE_DOWNLOAD_BASE_ENV: "http://127.0.0.1:9/", P.HARNESS_RUN_REGISTRY_ENV: str(P.RUN_REGISTRY),
               P.APP_TEST_CLIENT_PICK_ENV: ";".join(str(fk[t]) for t in order) + ";quit",
               "TMPDIR": str(d) + "/"}
        r = exec_with_hook(bundle, hook, env, [P.RELEASE_TEST_FLAG], timeout=300)
        fails = [e for e in r["events"] if e.get("kind") == "firstlaunch" and e.get("event") == "step"
                 and e.get("state") == "failed"]
        errs = [e for e in r["events"] if e.get("kind") == "setup_window" and e.get("state") == "error"]
        ev["app"] = {"rc": r["rc"], "picks": [e.get("path") for e in r["events"] if e.get("kind") == "client_pick"],
                     "failed": [(e.get("error_id"), e.get("choose_again")) for e in fails],
                     "windows": [(e.get("title"), e.get("buttons")) for e in errs], "home": home_listing(home)}
        require(len(fails) == len(order) and len(errs) == len(order), "not every bad client was refused", **ev)
        ev["cases"] = {}
        for tag, f, w in zip(order, fails, errs):
            wid, phrases = want[tag]
            text = w.get("text") or ""
            c = ev["cases"][tag] = {"error_id": f.get("error_id"), "title": w.get("title"), "text": text,
                                    "buttons": w.get("buttons")}
            require(f.get("error_id") == wid and f.get("choose_again") is True, f"{tag}: not refused as {wid}", **ev)
            require(all(p in text for p in phrases) and "No files were changed" in text and "3.3.5a" in text,
                    f"{tag}: the message doesn't name the problem, what is needed and that nothing changed", **ev)
            require(any("Choose" in str(b) for b in (c["buttons"] or [])), f"{tag}: no Choose Again", **ev)
        require(only_logs(home), "HOME holds more than the logs", **ev)
        # counterexample: a good (tiny) client through the same pick passes preflight and is copied
        good = scratch_client(d / "good")
        home2 = d / "h2 s"
        r2 = exec_with_hook(bundle, d / "hook2.jsonl", {**env, P.RELEASE_HOME_ENV: str(home2),
                                                        P.APP_TEST_CLIENT_PICK_ENV: f"{good};quit"},
                            [P.RELEASE_TEST_FLAG], timeout=300)
        f2 = [(e.get("step"), e.get("state"), e.get("error_id")) for e in r2["events"]
              if e.get("kind") == "firstlaunch" and e.get("event") == "step"]
        ev["counterexample_good_client"] = {"steps": f2, "only_logs": only_logs(home2)}
        require(("client", "done", None) in f2 and not only_logs(home2),
                "counterexample: a good client wasn't accepted and copied", **ev)
    finally:
        lk = d / "f" / "locked"
        if lk.exists():
            os.chmod(lk, 0o755)
        _cleanup(d)
    return ev


# ================================================================================================ downloads

def monotonic_to_total(ps: list[tuple]) -> bool:
    return bool(ps) and all(b[0] >= a[0] for a, b in zip(ps, ps[1:])) and ps[-1][0] == ps[-1][1]


@check("firstlaunch.downloads", order=15, capability=CAP, stack="down", layer="gate")
def downloads_check(ctx):
    """Downloads from a loopback server serving the cached pinned files (hashes from fork.lock): with the URL override
    at a closed port the data step stops with the no-connection message; the next launch (one byte of Data.zip
    changed by the server) continues from that step without redoing the client step, stops with "damaged" and keeps
    no file; the next launch (the release app, test hook) downloads both files again with progress per download that
    never goes backward and ends at the file's size, and the window's bar never moves backward (that app has no
    database server, so its run then stops at the database step or is closed, outside this scenario).
    Counterexample: the same progress test on a sequence with one step reversed fails."""
    heal_scratch()
    cache = ensure_cache()
    ev: dict = {"started": time.time(), "cache": cache}
    d = SCRATCH_ROOT / f"dl-{os.getpid()}"
    _cleanup(d)
    d.mkdir(parents=True)
    try:
        app = quick_app(d, with_launcher=True)
        bundle = app.parents[2]
        c1 = scratch_client(d / "c1")
        home = d / "h s"
        (d / "tmp").mkdir()
        base = {P.RELEASE_PORTS_ENV: json.dumps(SCRATCH_PORTS), "TMPDIR": str(d / "tmp") + "/"}
        # 1. no connection
        r0 = run_engine(app, home, ["--client", str(c1)], {**base, P.RELEASE_DOWNLOAD_BASE_ENV: "http://127.0.0.1:9/"},
                        d, timeout=300)
        f0 = _failed(r0["events"]) or {}
        wow = home / "client" / "Wow.exe"
        cst = os.stat(wow) if wow.exists() else None
        client_id = (cst.st_ino, cst.st_mtime_ns) if cst else None
        ev["no_connection"] = {"rc": r0["rc"], "failed": f0.get("error_id"), "step": f0.get("step"),
                               "message": f0.get("message"), "downloads": home_listing(home / "downloads")}
        require(f0.get("error_id") == "download_failed" and f0.get("step") == "data"
                and "internet connection" in (f0.get("message") or "") and "open Moonglade again" in f0["message"],
                "a closed port didn't stop the data step with the no-connection message", **ev)
        # 2. damaged: one byte of Data.zip changed by the server
        dz = cache["client-data"]["file"]
        with LoopbackServer(FL_CACHE, mutate={dz: cache["client-data"]["bytes"] // 2}) as srv:
            ra = run_engine(app, home, [], {**base, P.RELEASE_DOWNLOAD_BASE_ENV: srv.base}, d, timeout=600)
            reqa = list(srv.requests)
        fa = _failed(ra["events"]) or {}
        sta = [(e.get("step"), e.get("state")) for e in ra["events"] if e.get("event") == "step"]
        cst = os.stat(wow) if wow.exists() else None
        ev["damaged"] = {"rc": ra["rc"], "steps": sta, "failed": fa.get("error_id"), "title": fa.get("title"),
                         "message": fa.get("message"),
                         "requests": reqa, "downloads": home_listing(home / "downloads"),
                         "client_unchanged": bool(cst) and (cst.st_ino, cst.st_mtime_ns) == client_id}
        require(("client", "already_done") in sta and ev["damaged"]["client_unchanged"],
                "the next launch redid the client step", **ev)
        require(fa.get("error_id") == "download_damaged" and fa.get("title") == "A download arrived damaged"
                and "threw it away" in (fa.get("message") or "")
                and reqa == [dz], "one changed byte wasn't refused as a damaged download", **ev)
        require(not ev["damaged"]["downloads"] and not (home / "client-data").exists(),
                "the damaged download (or something unpacked from it) was kept", **ev)
        # 3. the release app: both downloads again, with progress; stopped (closed) once the wine step is done
        hook = d / "hook.jsonl"
        with LoopbackServer(FL_CACHE) as srv:
            env = {**base, P.RELEASE_HOME_ENV: str(home), P.RELEASE_DOWNLOAD_BASE_ENV: srv.base,
                   P.HARNESS_RUN_REGISTRY_ENV: str(P.RUN_REGISTRY), P.APP_TEST_CLIENT_PICK_ENV: "quit"}
            proc = subprocess.Popen([str(bundle / "Contents/MacOS/Moonglade"), P.RELEASE_TEST_FLAG],
                                    env=_clean_env(HS_APP_TEST_HOOK=str(hook), **env), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            t0 = time.time()
            while proc.poll() is None and time.time() - t0 < 900:
                if any(e.get("kind") == "firstlaunch" and e.get("step") == "wine" and e.get("state") == "done"
                       for e in _hook_events(hook)):
                    os.kill(proc.pid, 15)            # closing the window: the child is stopped, the app exits
                    break
                time.sleep(0.5)
            try:
                rc = proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, 9)
                rc = "timeout"
            reqb = list(srv.requests)
        evs = _hook_events(hook)
        fl = [e for e in evs if e.get("kind") == "firstlaunch"]
        prog = {}
        for e in fl:
            if e.get("event") == "progress" and e.get("step") in ("data", "wine"):
                prog.setdefault(e.get("name"), []).append((e.get("done"), e.get("total")))
        bars = [e.get("bar") for e in evs if e.get("kind") in ("firstlaunch", "setup_window") and e.get("bar") is not None]
        stb = [(e.get("step"), e.get("state")) for e in fl if e.get("event") == "step"]
        sizes = {"Data.zip": cache["client-data"]["bytes"], "wowsilicon-runtime.tar.xz": cache["wine-runtime"]["bytes"]}
        ev["app"] = {"rc": rc, "requests": reqb, "steps": stb, "bars": len(bars),
                     "progress": {k: {"events": len(v), "first": v[:2], "last": v[-1:]} for k, v in prog.items()},
                     "sizes": sizes}
        require(("data", "done") in stb and ("wine", "done") in stb and sorted(reqb) == sorted(
            [dz, cache["wine-runtime"]["file"]]), "the app's launch didn't download both files again", **ev)
        for name, size in sizes.items():
            ps = prog.get(name) or []
            require(len(ps) >= 2 and monotonic_to_total(ps) and ps[-1][0] == size,
                    f"progress for {name} went backward or didn't end at the file's size", **ev)
        require(bars and all(y >= x for x, y in zip(bars, bars[1:])), "the window's bar moved backward", **ev)
        # counterexample: the same test on a reversed step
        ps = list(prog["Data.zip"])
        bad = ps[:1] + [ps[2], ps[1]] + ps[3:] if len(ps) > 3 else ps[::-1]
        ev["counterexample_reversed"] = {"passes": monotonic_to_total(bad)}
        require(not monotonic_to_total(bad), "counterexample: a reversed progress step passed", **ev)
    finally:
        _cleanup(d)
    return ev


# ================================================================================================ kill_resume

def _ident(p: Path):
    try:
        st = os.stat(p)
        return [st.st_ino, st.st_mtime_ns]
    except OSError:
        return None


def _leftovers(home: Path) -> list[str]:
    pats = ("*.part", "*.hs-part", "client-data.unpack", "home.json.tmp-*", "dc-*")
    out = []
    for pat in pats:
        out += [str(p.relative_to(home)) for p in home.rglob(pat)]
    return sorted(out)


def _scratch_mysqld(app: Path, datadir: Path, d: Path):
    """A throwaway socket-only mysqld on a scratch datadir. Returns (proc, query function, stop function)."""
    sock = d / "s"
    cnf = d / "my.cnf"
    cnf.write_text("[mysqld]\n" + "".join(f"{k}={v}\n" for k, v in (
        ("basedir", app / "mysql"), ("datadir", datadir), ("socket", sock), ("pid-file", d / "m.pid"),
        ("log-error", d / "err.log"), ("lc-messages-dir", app / "mysql/share"),
        ("character-sets-dir", app / "mysql/share/charsets"))) + "skip-networking\nmysqlx=OFF\n")
    proc = subprocess.Popen([str(app / "mysql/bin/mysqld"), f"--defaults-file={cnf}"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def q(sql: str) -> tuple[int, str]:
        r = subprocess.run([str(app / "mysql/bin/mysql"), "--no-defaults", f"--socket={sock}", "-uroot", "--batch",
                            "--skip-column-names", "-e", sql], capture_output=True, text=True, timeout=60)
        return r.returncode, (r.stdout if r.returncode == 0 else r.stderr).strip()

    def stop():
        subprocess.run([str(app / "mysql/bin/mysqladmin"), "--no-defaults", f"--socket={sock}", "-uroot",
                        "shutdown"], capture_output=True, timeout=120)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    t0 = time.time()
    while time.time() - t0 < 120 and proc.poll() is None:
        if sock.exists() and q("SELECT 1")[0] == 0:
            break
        time.sleep(0.5)
    return proc, q, stop


def datadir_fingerprint(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        out[str(p.relative_to(root))] = [st.st_ino, st.st_size, st.st_mtime_ns]
    return out


# the AI players' own characters (firstlaunch.BOT_ACCOUNT_PREFIX accounts) in the first start's scratch databases
BOT_CHARS_SQL = ("SELECT COUNT(*) FROM acore_characters.characters c JOIN acore_auth.account a ON a.id = c.account "
                 "WHERE a.username LIKE 'rndbot%'")


@check("firstlaunch.kill_resume", order=15, capability=CAP, stack="down", layer="gate")
def kill_resume(ctx):
    """First launch on a scratch HOME (packaged app, client/ as the source, loopback downloads) SIGKILLed with its
    children during the client copy (byte-copy path), then during the server-data download, the server-data unpack
    and the database setup, each time followed by a normal launch that continues from the killed step (finished steps
    say "already done" and their outputs keep inode and mtime); the database kill comes once the AI players'
    characters exist (rndbot* accounts), so the next launch counts them on a clone as 0 player characters, moves the
    datadir aside and finishes; no .part, .hs-part, unpack or temp folder is left and the end state passes
    firstlaunch.flow's HOME checks (home_end_state). Counterexample (scenario 2): a player character added to that
    HOME's database (step marked not done) makes the next launch stop with database_kept and leaves every datadir
    file unchanged. Then pruning: of the set-aside folders only the newest stays, an older one without player
    characters is removed and one with a player's character is kept."""
    from .release_checks import _need_payload
    heal_scratch()
    _need_payload(ctx)
    ensure_cache()
    ev: dict = {"started": time.time(), "launches": []}
    d = SCRATCH_ROOT / f"kr-{os.getpid()}"
    _cleanup(d)
    d.mkdir(parents=True)
    try:
        app = release_fixture_app(d)
        home = d / "h s"
        (d / "tmp").mkdir()
        prof = d / "release.sb"
        from .release_runtime_checks import sandbox_profile
        prof.write_text(sandbox_profile(d))
        n_ev = lambda evs, step, what="progress": sum(1 for e in evs if e.get("event") == what   # noqa: E731
                                                      and e.get("step") == step)
        seen_world: dict = {"polls": 0}

        def db_mid(evs):
            # in the first start's bot setup: the world server runs and the AI players' own characters (on their
            # rndbot* accounts) exist, so the killed datadir holds AI players' characters and no player's
            # (spec-change log 1: the reset rule must ignore them); polled at most once a second on the scratch port
            if not any(e.get("event") == "phase" and str(e.get("phase", "")).startswith("world_") for e in evs):
                return False
            if time.time() - seen_world.get("last", 0) < 1:
                return False
            seen_world["last"] = time.time()
            seen_world["polls"] += 1
            n = _q(app, BOT_CHARS_SQL, SCRATCH_PORTS["mysql"], "acore_characters")
            if n is not None and n.isdigit() and int(n) > 0:
                seen_world["bot_characters_at_kill"] = int(n)
                return True
            return False
        phases = [
            ("client copy", {"force_byte_copy": True}, lambda evs: n_ev(evs, "client") >= 3),
            ("server-data download", {}, lambda evs: n_ev(evs, "data") >= 3),
            ("server-data unpack", {}, lambda evs: (home / "client-data.unpack" / "dbc").exists()),
            ("database setup", {}, db_mid),
            ("finish", {}, None),
        ]
        idents: dict = {}
        with LoopbackServer(FL_CACHE) as srv:
            for i, (name, inject, when) in enumerate(phases):
                r = _engine_sandboxed(app, home, ["--client", str(P.CLIENT)],
                                      {P.RELEASE_PORTS_ENV: json.dumps(SCRATCH_PORTS), "TMPDIR": str(d / "tmp") + "/",
                                       P.RELEASE_DOWNLOAD_BASE_ENV: srv.base,
                                       **({P.FIRSTLAUNCH_TEST_ENV: json.dumps(inject)} if inject else {})},
                                      d, prof, kill_when=when)
                st = [(e.get("step"), e.get("state")) for e in r["events"] if e.get("event") == "step"]
                rec = {"phase": name, "rc": r["rc"], "killed": bool(r["killed"]), "steps": st,
                       "failed": (_failed(r["events"]) or {}).get("error_id"),
                       "reset": next((e.get("result", {}) or {}).get("reset") for e in r["events"]
                                     if e.get("step") == "database" and e.get("state") == "done") if name == "finish"
                       else None}
                # finished steps of earlier launches are not redone
                rec["unchanged"] = {k: _ident(Path(k)) == v for k, v in idents.items()}
                ev["launches"].append(rec)
                require(not rec["failed"], f"launch {i + 1} ({name}) failed", **ev, tail=r["out_tail"])
                if when is not None:
                    require(rec["killed"], f"launch {i + 1} wasn't killed during the {name}", **ev,
                            tail=r["out_tail"])
                else:
                    require(r["rc"] == 0, "the last launch didn't finish", **ev, tail=r["out_tail"])
                require(all(rec["unchanged"].values()), f"launch {i + 1} redid an earlier finished step", **ev)
                if i >= 1:
                    require(("client", "already_done") in st or i == 1 and ("client", "done") in st,
                            f"launch {i + 1} didn't continue from where the last one stopped", **ev)
                done_now = {s for s, x in st if x == "done"} | {s for s, x in st if x == "already_done"}
                if "client" in done_now:
                    idents.setdefault(str(home / "client/Wow.exe"), _ident(home / "client/Wow.exe"))
                    idents.setdefault(str(home / "client-manifest.json"), _ident(home / "client-manifest.json"))
                if "data" in done_now:
                    idents.setdefault(str(home / "client-data/.hs-client-data.json"),
                                      _ident(home / "client-data/.hs-client-data.json"))
                if "wine" in done_now:
                    idents.setdefault(str(home / "wine/runtime/.hs-runtime-sha256"),
                                      _ident(home / "wine/runtime/.hs-runtime-sha256"))
        ev["db_kill"] = {k: v for k, v in seen_world.items() if k != "last"}
        last = ev["launches"][-1]
        ev["end_state"], end_probs = home_end_state(home)
        for why in end_probs:
            require(False, f"end state: {why}", **ev)
        ev["end"] = {"client": _client_check(home), "leftovers": _leftovers(home),
                     "home": sorted(x.name for x in home.iterdir()),
                     "complete": (json.loads((home / "home.json").read_text()).get("first_launch") or {}).get("complete"),
                     "db_marker": (home / ".hs-db-ready.json").is_file(), "reset": last["reset"],
                     "play_client": (home / "client-play/Wow.exe").is_file(),
                     "moved_aside": sorted(x.name for x in home.glob("mysql.unfinished-*"))}
        require(ev["end"]["complete"] is True and ev["end"]["db_marker"] and ev["end"]["play_client"]
                and ev["end"]["client"].get("manifest") and not ev["end"]["client"].get("mismatches"),
                "the end state isn't a finished setup", **ev)
        require(not ev["end"]["leftovers"], "partial files or temp folders were left", **ev)
        rs = last["reset"] or {}
        m = re.match(r"(\d+) character\(s\), 0 not on the AI players' rndbot\* accounts", str(rs.get("how")))
        require(rs.get("moved_aside_to") and rs.get("characters") == 0 and m and int(m.group(1)) > 0,
                "the killed database (AI players' characters only) wasn't counted on a clone and moved aside", **ev)
        require(not _proc_info(lambda a: any(str(home) in x for x in a)), "processes of the scratch HOME remain", **ev)
        # counterexample: a player's character in a not-done database -> kept, files unchanged
        md = d / "m"
        md.mkdir()
        proc, q, stop = _scratch_mysqld(app, home / "mysql", md)
        try:
            rc, out = q("SET SESSION sql_mode=''; INSERT INTO acore_characters.characters (guid, account, name, race, "
                        "class, gender, level) VALUES (990001, 990001, 'Keepme', 1, 1, 0, 1); "
                        f"{'SELECT COUNT(*) FROM acore_characters.characters WHERE guid=990001'}")
        finally:
            stop()
        shutil.rmtree(md, ignore_errors=True)
        require(rc == 0 and out.strip().endswith("1"), "couldn't add the character to the scratch database", **ev,
                out=out[-300:])
        (home / ".hs-db-ready.json").unlink()
        fp0 = datadir_fingerprint(home / "mysql")
        r = run_engine(app, home, [], {P.RELEASE_PORTS_ENV: json.dumps(SCRATCH_PORTS), "TMPDIR": str(d / "tmp") + "/"},
                       d, timeout=600)
        f = _failed(r["events"]) or {}
        fp1 = datadir_fingerprint(home / "mysql")
        ev["counterexample_character"] = {"rc": r["rc"], "failed": f.get("error_id"), "step": f.get("step"),
                                          "message": f.get("message"), "details": (f.get("details") or "")[-300:],
                                          "datadir_files": len(fp0), "changed": _fp_diff(fp0, fp1)}
        require(f.get("error_id") == "database_kept" and f.get("step") == "database" and fp0 == fp1,
                "counterexample: a datadir with a player's character wasn't kept unchanged", **ev)
        # set-aside folders don't pile up (fix round 1): only the newest stays; an older one goes only when it holds
        # no player character (counted on its clone); one with a player's character is never removed
        aside = Path(rs["moved_aside_to"])
        older_bots = home / "mysql.unfinished-20000101-000000"        # AI players' characters only
        older_player = home / "mysql.unfinished-20000101-000001"      # holds the player character 'Keepme'
        subprocess.run(["/bin/cp", "-c", "-R", str(aside), str(older_bots)], check=True)
        subprocess.run(["/bin/cp", "-c", "-R", str(home / "mysql"), str(older_player)], check=True)
        md.mkdir()
        proc, q, stop = _scratch_mysqld(app, home / "mysql", md)
        try:
            rc, out = q("DELETE FROM acore_characters.characters WHERE guid=990001; "
                        "SELECT COUNT(*) FROM acore_characters.characters WHERE guid=990001")
        finally:
            stop()
        shutil.rmtree(md, ignore_errors=True)
        require(rc == 0 and out.strip().endswith("0"), "couldn't remove the character again", **ev, out=out[-300:])
        r = run_engine(app, home, [], {P.RELEASE_PORTS_ENV: json.dumps(SCRATCH_PORTS), "TMPDIR": str(d / "tmp") + "/"},
                       d, timeout=1200)
        db_done = next((e.get("result") or {} for e in r["events"]
                        if e.get("step") == "database" and e.get("state") == "done"), {})
        reset = db_done.get("reset") or {}
        pruned = reset.get("pruned") or {}
        ev["prune"] = {"rc": r["rc"], "failed": (_failed(r["events"]) or {}).get("error_id"), "reset": reset,
                       "left": sorted(x.name for x in home.glob("mysql.unfinished-*"))}
        newest = Path(reset.get("moved_aside_to") or "/nonexistent").name
        require(r["rc"] == 0 and sorted(x["name"] for x in pruned.get("removed", [])) == sorted([older_bots.name,
                                                                                                  aside.name])
                and [x["name"] for x in pruned.get("kept_player_data", [])] == [older_player.name]
                and ev["prune"]["left"] == sorted([older_player.name, newest]),
                "set-aside folders: the older ones without player characters weren't removed, or the one with a "
                "player's character wasn't kept", **ev, tail=r["out_tail"])
        ev["end_state_after_prune"], probs2 = home_end_state(home)
        require(not probs2, f"end state after the second reset: {probs2}", **ev)
    finally:
        _cleanup(d)
    return ev
