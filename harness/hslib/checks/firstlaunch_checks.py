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
Shared helpers (for the other firstlaunch.* / release.* checks): LoopbackServer, ensure_cache, scratch_client,
run_engine, setup_events.
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .. import paths as P
from ..registry import check, require
from .release_runtime_checks import (NEW_APP, SCRATCH_PORTS, SCRATCH_ROOT, _clean_env, _hook_events, _proc_info,
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

@check("firstlaunch.preflight", order=15, capability=CAP, stack="any", tier="quick")
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


@check("firstlaunch.flow", order=15, capability=CAP, stack="down", tier="slow")
def flow(ctx):
    """Fresh scratch HOME to ready through the release app's first-launch window (test hook, scripted pick of
    client/, loopback downloads); counterexample: a corrupted copy is never called deletable and is redone."""
    from .. import clientbuild, pristine
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
        home = d / "h"
        (d / "tmp").mkdir()
        with LoopbackServer(FL_CACHE) as srv:
            base_env = {P.RELEASE_HOME_ENV: str(home), P.RELEASE_PORTS_ENV: json.dumps(SCRATCH_PORTS),
                        P.RELEASE_DOWNLOAD_BASE_ENV: srv.base, P.HARNESS_RUN_REGISTRY_ENV: str(P.RUN_REGISTRY),
                        "TMPDIR": str(d / "tmp") + "/"}
            hook = d / "hook.jsonl"
            t0 = time.time()
            r = exec_with_hook(bundle, hook, {**base_env, P.APP_TEST_CLIENT_PICK_ENV: f"{src};quit"},
                               [P.RELEASE_TEST_FLAG], timeout=2400)
            ev["run_s"] = round(time.time() - t0, 1)
            evs = r["events"]
            ev["requests"] = srv.requests
        win = [e for e in evs if e.get("kind") == "setup_window"]
        fl = [e for e in evs if e.get("kind") == "firstlaunch"]
        steps = [(e.get("step"), e.get("state")) for e in fl if e.get("event") == "step"]
        prog = [e for e in fl if e.get("event") == "progress"]
        bars = [e.get("bar") for e in fl] + [e.get("bar") for e in win]
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
        homej = json.loads((home / "home.json").read_text())
        ev["home"] = {"client": cc, "play_client_stamp": bool(stamp), "client_data_sha": cd_marker.get("sha256"),
                      "wine_marker": wine_marker, "db_marker": dbm, "dbs": dbs, "settings": settings,
                      "home_json": homej, "leftover_parts": [str(p) for p in home.rglob("*")
                                                             if p.name.endswith((".part", ".hs-part", ".unpack"))]}
        require(cc.get("manifest") and not cc.get("mismatches") and cc.get("wow") == "3.3.5.12340",
                "HOME's client copy doesn't match its record", **ev)
        require(stamp and (home / "client-play/Wow.exe").is_file(), "no play client in HOME", **ev)
        require(cd_marker.get("sha256") == lock["client_data_sha256"] and wine_marker == lock["wowsilicon_runtime_sha256"],
                "the server data / Wine runtime don't match their pins", **ev)
        require(all(dbs.values()) and dbm.get("home") == str(home), "the databases didn't pass the step's check", **ev)
        require(settings.get("world_size") and settings.get("client"), "the default settings weren't written", **ev)
        require((homej.get("first_launch") or {}).get("complete") is True, "home.json doesn't record completion", **ev)
        require(not ev["home"]["leftover_parts"], "partial files left in HOME", **ev)
        require(not _proc_info(lambda a: any(str(d) in x for x in a)), "processes of the scratch HOME remain", **ev)
        fp1 = pristine.fingerprint(src)
        ev["source_unchanged"] = not pristine.diff(fp0, fp1)
        require(ev["source_unchanged"], "the source client changed", **ev, diff=pristine.diff(fp0, fp1)[:5])

        # counterexample: a corrupted copy is never called deletable; the next launch redoes the copy
        home2 = d / "h2"
        hook2 = d / "hook2.jsonl"
        with LoopbackServer(FL_CACHE) as srv:
            env2 = {**base_env, P.RELEASE_HOME_ENV: str(home2), P.RELEASE_DOWNLOAD_BASE_ENV: srv.base,
                    P.APP_TEST_CLIENT_PICK_ENV: f"{src};quit",
                    P.FIRSTLAUNCH_TEST_ENV: json.dumps({"corrupt_copy": True})}
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
    finally:
        _cleanup(d)
    return ev
