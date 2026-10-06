"""client-connectivity: the play client's default resolution (openspec/changes/launcher-fixes; harness/hslib/resolution.py).

Scratch play clients only (env/check-play-res*/); the player's env/client-play/ is never written (its Config.wtf is
hashed before and after). Part A drives `resolution.apply` (the function `launch.prepare` calls) with injected
displays; part B runs the real ./play + Wow.exe on a fresh scratch play client with an injected display
($HS_TEST_DISPLAY, test-only) and reads the client's own back-buffer size from mtld3d's log ("present: guest WxH")."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .. import clientbuild, launch, paths as P, play, resolution as R, stack
from ..registry import check, require
from .play_checks import PLAY, is_clear, remaining, require_given_clear
from .playclient_util import PlayRun, drop_scratch, fresh_scratch

CAP = "client-connectivity"
ASPECT_TOL = 0.02                            # spec: aspect ratio within 2%
DESIGN = P.ROOT / "openspec" / "changes" / "launcher-fixes" / "design.md"
DESIGN_ARCHIVED_GLOB = "openspec/changes/archive/*-launcher-fixes/design.md"
D1 = R.parse_display("1512x982@2/33")       # a notched 14" MacBook Pro's built-in display
D2 = R.parse_display("1920x1080@1/0")       # an external 16:9 monitor
# Zach (2026-10-05): the default must be dynamic on any Mac and display. Each case: the value must have the
# full-screen area's aspect ratio within 2% and be a size the client takes.
DISPLAYS = {
    "notched 14in MacBook Pro": "1512x982@2/33",
    "no-notch 16:10 laptop": "1440x900@2/0",
    "external 16:9": "1920x1080@1/0",
    "ultrawide 21:9": "3440x1440@1/0",
    "16:9 at Retina scale 2": "2560x1440@2/0",
    "16:9 at scale 1": "2560x1440@1/0",
}
# What the client takes (design.md, investigation): in windowed mode any <w>x<h>, real client tried from 1024x768 up
# to 7680x4320, aspect ratios 1.33 to 2.39 (it opens a window of that shape that fits the screen)
ACCEPT_MIN, ACCEPT_MAX = (640, 480), (7680, 4320)
E2E_DISPLAYS = ("3440x1440@2/0", "1920x1080@1/0")   # largest value (6880x2880, 2.39) and a scale-1 16:9


def _sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def design_decision() -> str | None:
    """The native-vs-half-size default recorded in design.md ("Decision: default = half|native")."""
    files = [DESIGN] + sorted(P.ROOT.glob(DESIGN_ARCHIVED_GLOB))
    for f in files:
        if f.exists():
            m = re.search(r"Decision:\s*default\s*=\s*(half|native)\b", f.read_text())
            if m:
                return m.group(1)
    return None


def _close(value: str | None, d: dict) -> tuple[float | None, float]:
    w, h = R.fullscreen_area(d)
    a = R.aspect(value or "")
    return a, w / h


def verify_sequence(client: Path, decision: str | None) -> dict:
    """GIVEN a play client with no resolution: prepare (display D1), prepare after the display changed (D2), prepare
    after the player set their own value. Returns evidence; raises CheckFail."""
    cfg = client / "WTF" / "Config.wtf"
    text = cfg.read_text(errors="replace")
    cfg.write_text("".join(l for l in text.splitlines(keepends=True) if not re.match(r"^\s*SET\s+gxResolution\s", l, re.I)))
    (client / "WTF" / R.MARKER).unlink(missing_ok=True)
    ev: dict = {"given": {"gxResolution": R.current(client), "marker": R.marker(client)}, "decision_in_design": decision}
    require(ev["given"]["gxResolution"] is None and ev["given"]["marker"] is None,
            "GIVEN not met: the scratch play client already has a resolution", **ev)
    require(decision in ("half", "native"), "design.md records no native-vs-half-size decision", **ev)

    s1 = R.apply(client, D1)
    a1, want1 = _close(R.current(client), D1)
    ev["first"] = {"apply": s1, "config_value": R.current(client), "aspect": a1, "display_aspect": round(want1, 4)}
    require(s1["action"] == "set" and a1 and abs(a1 / want1 - 1) <= ASPECT_TOL,
            "first launch: the written resolution doesn't match the display's aspect ratio within 2%", **ev)
    require(R.current(client) == expected(D1, decision), "first launch: the value doesn't follow the design.md "
            f"decision ({decision}: {expected(D1, decision)})", **ev)

    s2 = R.apply(client, D2)
    a2, want2 = _close(R.current(client), D2)
    ev["second"] = {"apply": s2, "config_value": R.current(client), "aspect": a2, "display_aspect": round(want2, 4)}
    require(R.current(client) != ev["first"]["config_value"] and a2 and abs(a2 / want2 - 1) <= ASPECT_TOL,
            "after the display changed, the resolution wasn't re-picked to match the new display", **ev)

    player = "1280x1024"                                         # what the in-game Video options would write
    t = cfg.read_text(errors="replace")
    cfg.write_text(re.sub(r'(?im)^(\s*SET\s+gxResolution\s+)"[^"]*"', rf'\g<1>"{player}"', t))
    before = cfg.read_bytes()
    s3 = R.apply(client, D1)
    ev["third"] = {"player_value": player, "apply": s3, "config_value": R.current(client),
                   "file_unchanged": cfg.read_bytes() == before}
    require(R.current(client) == player and ev["third"]["file_unchanged"],
            "the player's own resolution was overwritten", **ev)
    return ev


def expected(spec_or_display, decision: str | None) -> str:
    """The value the decision implies, computed here (not by resolution.choose): the full-screen area (points minus
    the top inset) times the backing scale for native, in points for half."""
    d = R.parse_display(spec_or_display) if isinstance(spec_or_display, str) else spec_or_display
    w, h = d["points"]
    h = h - d.get("top_inset", 0)
    k = d["scale"] if decision == "native" else 1
    return f"{int(round(w * k))}x{int(round(h * k))}"


def accepted(value: str) -> bool:
    m = re.match(r"^(\d+)x(\d+)$", value or "")
    return bool(m) and ACCEPT_MIN[0] <= int(m.group(1)) <= ACCEPT_MAX[0] and ACCEPT_MIN[1] <= int(m.group(2)) <= ACCEPT_MAX[1]


def verify_displays(client: Path, decision: str | None) -> dict:
    """Every display case from a client with no resolution: the right aspect ratio within 2%, the design.md decision,
    a value the client takes."""
    out, bad = {}, []
    for name, spec in DISPLAYS.items():
        cfg = client / "WTF" / "Config.wtf"
        cfg.write_text("".join(l for l in cfg.read_text(errors="replace").splitlines(keepends=True)
                               if not re.match(r"^\s*SET\s+gxResolution\s", l, re.I)))
        (client / "WTF" / R.MARKER).unlink(missing_ok=True)
        d = R.parse_display(spec)
        a = R.apply(client, d)
        v = R.current(client)
        got, want = _close(v, d)
        row = {"display": spec, "value": v, "action": a["action"], "aspect": round(got, 4) if got else None,
               "full_screen_aspect": round(want, 4), "expected": expected(d, decision),
               "follows_decision": v == expected(d, decision),
               "client_takes_it": accepted(v)}
        row["ok"] = bool(a["action"] == "set" and got and abs(got / want - 1) <= ASPECT_TOL and row["follows_decision"]
                         and row["client_takes_it"])
        out[name] = row
        if not row["ok"]:
            bad.append(name)
    require(not bad, f"display case(s) {bad}: wrong aspect, not the design.md decision, or a size the client "
            "doesn't take", cases=out)
    return out


def guest_sizes(text: str) -> list[list[int]]:
    clean = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return [[int(a), int(b)] for a, b in re.findall(r"present: guest (\d+)x(\d+)", clean)]


def e2e(ev: dict) -> dict:
    """The real launch path: ./play (test account, fresh scratch play client, injected display) writes the value and
    the real client opens with that aspect ratio (mtld3d's back buffer). Also: a normal ./play refuses the switch."""
    r = subprocess.run([PLAY, "--world-datadir", "/nonexistent/hs-res-refuse"], cwd=P.ROOT, capture_output=True,
                       text=True, timeout=300, env={**os.environ, R.TEST_ENV: E2E_DISPLAYS[0], "HS_PROFILE": "m0"})
    ev["refused"] = {"exit": r.returncode, "named": f"{R.TEST_ENV} is set" in r.stdout, "output": r.stdout[-300:]}
    require(r.returncode != 0 and ev["refused"]["named"], f"a normal ./play accepted ${R.TEST_ENV}", **ev)
    ev["e2e"] = {}
    for spec in E2E_DISPLAYS:
        e2e_one(ev, spec)
    return ev


def e2e_one(ev: dict, spec: str) -> None:
    require_given_clear(ev)
    scratch = fresh_scratch("res")
    run = None
    try:
        clientbuild.build(dest=scratch)
        ev["e2e_given"] = {"gxResolution": R.current(scratch), "marker": R.marker(scratch)}
        require(ev["e2e_given"]["gxResolution"] is None, "GIVEN not met: the fresh scratch client has a resolution", **ev)
        off = P.WINE_LOG.stat().st_size if P.WINE_LOG.exists() else 0
        t0 = time.time()
        run = PlayRun("res", scratch=scratch, extra_env={R.TEST_ENV: spec})
        run.start()
        up = run.wait_client()
        require(up.get("up"), "the real Wow.exe never came up", output=run.output()[-2000:], **ev)
        sizes: list = []
        while time.time() - t0 < 300 and not sizes:
            with open(P.WINE_LOG, errors="replace") as f:
                f.seek(off)
                sizes = guest_sizes(f.read())
            time.sleep(1)
        ev["stop"] = run.stop()
        rec = run.record()
        d = R.parse_display(spec)
        value = R.current(scratch)
        want = R.fullscreen_area(d)[0] / R.fullscreen_area(d)[1]
        e = ev["e2e"][spec] = {"config_value": value, "prepare_resolution": (rec.get("prepare") or {}).get(
            "resolution"), "client_back_buffers": sizes, "display_aspect": round(want, 4),
            "back_buffer_aspect": round(sizes[0][0] / sizes[0][1], 4) if sizes else None, "remaining_after": remaining()}
        require((e["prepare_resolution"] or {}).get("action") == "set" and value == expected(d, design_decision()),
                f"{spec}: the launch didn't write the default resolution into the scratch play client", **ev)
        require(sizes and abs(e["back_buffer_aspect"] / want - 1) <= ASPECT_TOL,
                f"{spec}: the real client didn't take the resolution's aspect ratio (mtld3d back buffer)", **ev)
        require(is_clear(e["remaining_after"]), "something is still running", **ev)
    finally:
        if run is not None:
            run.cleanup()
        drop_scratch(scratch)


@check("client.resolution_default", order=386, capability=CAP, stack="down", tier="full")
def resolution_default(ctx):
    """A play client with no resolution (injected displays): the first launch writes a resolution with the display's
    full-screen aspect ratio (within 2%) following design.md's native-vs-half decision; after the display changes it
    is re-picked; a player's own value is left untouched. Six display cases (notched 14", no-notch 16:10 laptop,
    16:9 external, 21:9 ultrawide, 16:9 at scale 2 and 1) each get the right aspect and a size the client takes. End
    to end, ./play writes it into a fresh scratch play client and the real client opens with that aspect ratio
    (ultrawide at scale 2 and 16:9 at scale 1). The player's env/client-play/ is never written."""
    zach = clientbuild.PLAY / "WTF" / "Config.wtf"
    ev: dict = {"player_config_sha_before": _sha(zach)}
    scratch = fresh_scratch("resunit")
    try:
        clientbuild.build(dest=scratch)
        ev["sequence"] = verify_sequence(scratch, design_decision())
        ev["displays"] = verify_displays(scratch, design_decision())
    finally:
        drop_scratch(scratch)
    e2e(ev)
    ev["player_config_sha_after"] = _sha(zach)
    require(ev["player_config_sha_after"] == ev["player_config_sha_before"], "the player's play client config changed",
            **ev)
    return ev


# ---------------------------------------------------------------- the player's own play client is never written by
# a harness run (verifier FAIL 2026-10-05: play.player_account would have added the resolution to it)

def player_config_sha() -> str | None:
    """sha256 of the player's own env/client-play/WTF/Config.wtf (None if absent)."""
    return _sha(clientbuild.PLAY / "WTF" / "Config.wtf")


def player_marker_sha() -> str | None:
    """sha256 of the launcher's resolution marker in the player's own WTF/ (None if absent)."""
    return _sha(clientbuild.PLAY / "WTF" / R.MARKER)


def player_marker_exists() -> bool:
    return (clientbuild.PLAY / "WTF" / R.MARKER).exists()


GATE_CASES = [
    # (label, argv, environ, client: "own" | "scratch", may write?)
    ("plain ./play (the player)", [], {}, "own", True),
    ("the app (the player)", [], {"HS_FROM_APP": "1", "HS_APP_BUNDLE": "/x/Moonglade.app"}, "own", True),
    ("play.player_account: --account player --client-cmd + $HS_PLAYER_FILE",
     ["--account", "player", "--client-cmd", "/tmp/x.bat"], {"HS_PLAYER_FILE": "/tmp/p.json"}, "own", False),
    ("--client-cmd alone", ["--client-cmd", "/tmp/x.bat"], {}, "own", False),
    ("--account test (client.stock_mode, app.shim_perf)", ["--account", "test"], {}, "own", False),
    ("any check's subprocess ($HS_HARNESS_RUN)", [], {"HS_HARNESS_RUN": "1"}, "own", False),
    ("scratch settings ($HS_SETTINGS)", [], {"HS_SETTINGS": "/tmp/s.json"}, "own", False),
    ("pinned profile ($HS_PROFILE)", [], {"HS_PROFILE": "m0"}, "own", False),
    ("shim test knob", [], {"HSDOCK_TEST_FLICKER_MS": "100"}, "own", False),
    ("--world-datadir", ["--world-datadir", "/nonexistent"], {}, "own", False),
    ("scratch play client (client.resolution_default)", ["--account", "test", "--allow-test-client-dir"],
     {"HS_PLAY_CLIENT_DIR": "/tmp/check-play-res", "HS_HARNESS_RUN": "1"}, "scratch", True),
    ("scratch client standing in for the player's ($HS_TEST_OWN_CLIENT, client.config_gate)",
     ["--account", "test", "--client-cmd", "/tmp/x.bat", "--allow-test-client-dir"],
     {"HS_TEST_OWN_CLIENT": str(P.ENV / "check-play-res")}, "scratch", False),
]


def verify_gate(gate_fn=None) -> dict:
    from .. import play as PL
    gate_fn = gate_fn or PL.resolution_gate
    rows, bad = {}, []
    for label, argv, environ, which, want in GATE_CASES:
        client = clientbuild.PLAY if which == "own" else P.ENV / "check-play-res"
        g = gate_fn(PL._parse(argv), client, environ)
        rows[label] = {"argv": argv, "env": sorted(environ), "client": which, "apply": g["apply"], "want": want,
                       "signs": g.get("harness_signs")}
        if g["apply"] != want:
            bad.append(label)
    require(not bad, f"the resolution gate is wrong for: {bad}", cases=rows)
    return {"cases": rows}


# ---------------------------------------------------------------- every config write is gated (rename-moonglade
# cycle 2, verifier FAIL 2026-10-05: play.loop_clean_exit appended `SET gxApi "d3d9"` to the player's Config.wtf
# through launch.prepare's ungated configure(client_dir(), defaults=WINE_DEFAULTS))

def player_config_copy(dest: Path) -> dict:
    """GIVEN of the verifier's case on a scratch copy: dest/WTF/Config.wtf = the player's Config.wtf (read only) with
    no gxApi line, plus his realmlist.wtf files. Returns what was copied."""
    src = clientbuild.PLAY / "WTF" / "Config.wtf"
    text = src.read_bytes() if src.exists() else b'SET gxWindow "1"\r\n'
    text = b"".join(l for l in text.splitlines(keepends=True) if not l.lower().startswith(b"set gxapi "))
    (dest / "WTF").mkdir(parents=True, exist_ok=True)
    (dest / "WTF" / "Config.wtf").write_bytes(text)
    copied = ["WTF/Config.wtf"]
    for rl in (clientbuild.PLAY / "Data").glob("*/realmlist.wtf"):
        (dest / "Data" / rl.parent.name).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(rl, dest / "Data" / rl.parent.name / "realmlist.wtf")
        copied.append(f"Data/{rl.parent.name}/realmlist.wtf")
    return {"from_player_config": src.exists(), "copied": copied, "has_gxapi": b"gxapi" in text.lower()}


def config_hashes(d: Path) -> dict:
    """sha256 of every config file the launch could write: WTF/*.wtf, Data/*/realmlist.wtf, the login archive."""
    from .. import frontmenu
    files = list((d / "WTF").glob("*.wtf")) + list((d / "Data").glob("*/realmlist.wtf"))
    out = {str(f.relative_to(d)): _sha(f) for f in sorted(files)}
    try:
        arch = frontmenu.archive_path(d)
        out[str(arch.relative_to(d))] = _sha(arch)
    except Exception:  # noqa: BLE001  (no locale dir: nothing to hash)
        pass
    return out


def verify_config_step() -> dict:
    """launch.client_config_step on a scratch copy of the player's config (gxApi missing): with the gate closed
    nothing changes; the old ungated call (write_client_config=True) adds gxApi there (so the GIVEN can show a
    write: the self-counterexample)."""
    scratch = fresh_scratch("cfgstep")
    ev: dict = {"scratch": str(scratch)}
    try:
        clientbuild.build(dest=scratch)               # a real (scratch) play client: configure() checks for Wow.exe
        ev["given"] = player_config_copy(scratch)
        require(not ev["given"]["has_gxapi"], "GIVEN not met: the scratch config has a gxApi line", **ev)
        launch.select_client(scratch)
        before = config_hashes(scratch)
        ev["gated"] = launch.client_config_step(False, say=lambda *_: None)
        ev["gated_changed"] = sorted(k for k, v in config_hashes(scratch).items() if before.get(k) != v)
        ev["ungated"] = launch.client_config_step(True, say=lambda *_: None)
        cfg = (scratch / "WTF" / "Config.wtf").read_bytes().lower()
        ev["ungated_added_gxapi"] = b'set gxapi "d3d9"' in cfg
        require(not ev["gated_changed"], "the gated config step wrote into the (stand-in) player's client", **ev)
        require(ev["ungated_added_gxapi"], "self-test: the ungated call didn't write gxApi, so the case proves nothing",
                **ev)
        return ev
    finally:
        launch.select_client(None)
        drop_scratch(scratch)


@check("client.resolution_gate", order=385, capability=CAP, stack="any", tier="quick")
def resolution_gate(ctx):
    """The play loop writes config (the resolution default, and since rename-moonglade cycle 2 every config write:
    realmlist, Config.wtf defaults, the login file) into the player's own env/client-play/ only from a genuine
    player launch (the app or plain ./play); every harness-shaped launch (test arguments, $HS_HARNESS_RUN, scratch
    settings, pinned profile, shim test knobs, the player-account check's --client-cmd + $HS_PLAYER_FILE) is a
    no-op there; a scratch play client still gets it. The config step itself writes nothing when the gate is closed
    (scratch copy of the player's config, gxApi missing), and the old ungated call does write there."""
    ev = verify_gate()
    ev["config_step"] = verify_config_step()
    return ev


def config_gate_run(scratch: Path, own: bool) -> dict:
    """One harness-shaped ./play (stand-in, test account, patched, scratch play client) with the scratch standing
    in for the player's own client when `own`. Returns evidence incl. config hashes before/after and whether the
    login archive ever appeared."""
    from .. import frontmenu
    from .play_checks import StandIn
    s = StandIn("cfggate-own" if own else "cfggate-control")
    sfile = s.dir / "settings.json"
    sfile.write_text(json.dumps({"client": "patched"}))
    env = {"HS_SETTINGS": str(sfile), "HS_PLAY_CLIENT_DIR": str(scratch), **({"HS_TEST_OWN_CLIENT": str(scratch)} if own else {})}
    ev: dict = {"own": own, "env": sorted(env)}
    arch = frontmenu.archive_path(scratch)
    try:
        before = config_hashes(scratch)
        s.start_play("--allow-test-client-dir", env=env)
        seen_archive, t0 = False, time.time()
        up = None
        while time.time() - t0 < 900:
            seen_archive = seen_archive or arch.exists()
            if s.proc.poll() is not None:
                up = {"up": False, "why": f"./play exited early with {s.proc.returncode}"}
                break
            if s.started.exists() and play.client_processes(s.bat.name.lower()):
                up = {"up": True, "after_s": round(time.time() - t0, 1)}
                break
            time.sleep(0.5)
        seen_archive = seen_archive or arch.exists()
        ev["client_up"] = up or {"up": False, "why": "timeout"}
        s.stop_file.write_text("quit")
        rc, _ = s.wait_exit(timeout=300)
        rec = json.loads(play.LAST.read_text()) if play.LAST.exists() else {}
        after = config_hashes(scratch)
        ev |= {"exit_code": rc, "gate": rec.get("resolution_gate"),
               "prepare_client_config": (rec.get("prepare") or {}).get("client_config"),
               "profiles": (rec.get("client") or {}).get("profiles", {}).get("skipped") if isinstance(
                   (rec.get("client") or {}).get("profiles"), dict) else None,
               "changed": sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k)),
               "login_archive_seen": seen_archive,
               "gxapi_after": b'set gxapi "d3d9"' in (scratch / "WTF" / "Config.wtf").read_bytes().lower(),
               "remaining_after": remaining(s)}
        return ev
    finally:
        s.cleanup()


@check("client.config_gate", order=386, capability=CAP, stack="down", tier="full")
def config_gate(ctx):
    """The verifier's case on a scratch copy of the player's client only (his file is never written): a scratch play
    client with the player's Config.wtf minus gxApi, standing in for his own client ($HS_TEST_OWN_CLIENT): a
    harness ./play stand-in run leaves every config file byte-identical and never writes the login file; the same run
    on a copy without the stand-in flag (ungated) does write gxApi, so the run would write if it weren't gated; and
    the player's own Config.wtf is unchanged."""
    zach = clientbuild.PLAY / "WTF" / "Config.wtf"
    ev: dict = {"player_config_sha_before": _sha(zach)}
    require_given_clear(ev)
    scratch = fresh_scratch("cfggate")
    try:
        clientbuild.build(dest=scratch)
        ev["given"] = player_config_copy(scratch)
        require(not ev["given"]["has_gxapi"], "GIVEN not met: the scratch config has a gxApi line", **ev)
        ev["own_run"] = o = config_gate_run(scratch, own=True)
        require(o["exit_code"] == 0 and o["client_up"].get("up") and is_clear(o["remaining_after"]),
                "the stand-in ./play run didn't run and end cleanly", **ev)
        require((o["gate"] or {}).get("players_own_client") is True and (o["gate"] or {}).get("apply") is False,
                "GIVEN not met: the gate didn't treat the scratch as the player's own client", **ev)
        require(not o["changed"] and not o["gxapi_after"], "the harness run wrote config into the player's client",
                **ev)
        require(not o["login_archive_seen"], "the harness run wrote the login file into the player's client", **ev)
        # the ungated control on a copy (APFS clone of the scratch as the gated run left it): the same harness run,
        # just without the switch, so its config step is ungated; it must add the missing default
        copy = fresh_scratch("cfggate-copy")
        cp = subprocess.run(["/bin/cp", "-cR", str(scratch), str(copy)], capture_output=True, text=True)
        require(cp.returncode == 0, f"couldn't clone the scratch client: {cp.stderr.strip()}", **ev)
        ev["control_copy"] = str(copy)
        ev["control_run"] = c = config_gate_run(copy, own=False)
        require(c["exit_code"] == 0 and c["gxapi_after"] and (c["gate"] or {}).get("apply") is True,
                "control: the ungated run on a copy didn't write gxApi (so the case proves nothing)", **ev)
    finally:
        drop_scratch(scratch)
        drop_scratch(P.ENV / "check-play-cfggate-copy")
    ev["player_config_sha_after"] = _sha(zach)
    require(ev["player_config_sha_after"] == ev["player_config_sha_before"], "the player's play client config changed",
            **ev)
    return ev


REFUSE_WITHIN_S = 90      # the refusal comes before the server starts (~2 s); 90 s = it's starting things instead


@check("client.real_client_refused", order=386, capability=CAP, stack="down", tier="quick")
def real_client_refused(ctx):
    """A harness-shaped ./play that would start the real Wow.exe in the player's own play client (stand-in flag,
    no --client-cmd) is refused before the server starts (no server or Wine process sampled at 10 Hz while it runs,
    the play loop never reached prepare), nothing is launched and no config is changed."""
    from .play_checks import StandIn
    scratch = fresh_scratch("cfgrefuse")
    s = StandIn("cfgrefuse")
    ev: dict = {}
    try:
        clientbuild.build(dest=scratch)
        player_config_copy(scratch)
        before = config_hashes(scratch)
        sfile = s.dir / "settings.json"
        sfile.write_text(json.dumps({"client": "patched"}))
        env = {**os.environ, "HS_PROFILE": "m0", "HS_SETTINGS": str(sfile), "HS_PLAY_CLIENT_DIR": str(scratch),
               "HS_TEST_OWN_CLIENT": str(scratch)}
        out = s.dir / "play.out"
        with open(out, "w") as fh:
            proc = subprocess.Popen([PLAY, "--account", "test", "--allow-test-client-dir"], cwd=P.ROOT, env=env,
                                    stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
        seen = {"stack": [], "wine": []}             # sampled while ./play runs: "before the server starts"
        t0 = time.time()
        while proc.poll() is None and time.time() - t0 < REFUSE_WITHIN_S:
            st = {k: v for k, v in stack.stack_processes().items() if v}
            wn = launch.prefix_processes()
            if st:
                seen["stack"].append({"t": round(time.time() - t0, 2), **st})
            if wn:
                seen["wine"].append({"t": round(time.time() - t0, 2), "n": len(wn)})
            if st or wn:                             # already past the point of refusing: stop it now
                break
            time.sleep(0.1)
        rc = proc.poll()
        if rc is None:                               # not refused: it's starting the server/real game; stop it all
            os.killpg(proc.pid, 9)
            proc.wait()
            if launch.prefix_processes():
                launch.stop_prefix()
            if any(stack.stack_processes().values()):
                stack.down()
        ev["seen_while_running"] = {k: v[:5] for k, v in seen.items()}
        rec = json.loads(play.LAST.read_text()) if play.LAST.exists() else {}
        rec = rec if rec.get("pid") == proc.pid else {}
        ev |= {"rc": rc, "output_tail": out.read_text(errors="replace")[-1500:], "outcome": rec.get("outcome"),
               "error": rec.get("error"), "client_started": rec.get("client_started"),
               "changed": sorted(k for k, v in config_hashes(scratch).items() if before.get(k) != v),
               "remaining_after": remaining()}
        require(rc not in (None, 0) and "can't start the real game in your own play client" in (rec.get("error") or ""),
                "the real-client harness run on the player's client wasn't refused", **ev)
        ev["record_has_prepare"] = "prepare" in rec
        ev["record_steps"] = [x.get("step") for x in rec.get("steps", [])]
        require(not seen["stack"] and not seen["wine"] and not ev["record_has_prepare"],
                "the refused run got as far as the server or Wine before refusing", **ev)
        require(rec.get("client_started") is False and not ev["changed"] and is_clear(ev["remaining_after"]),
                "the refused run started something or wrote config", **ev)
        return ev
    finally:
        s.cleanup()
        drop_scratch(scratch)


@check("client.player_config_untouched", order=999, capability=CAP, stack="any", tier="quick")
def player_config_untouched(ctx):
    """Last check of every run: the player's own env/client-play/WTF/Config.wtf is byte-identical to the start of
    the run (sha256 recorded by the runner), so no check wrote it; and the launcher's marker file there
    (WTF/hs-resolution.json) wasn't created, removed or changed (its sha256, or absence, is the same as at the start)."""
    start = (ctx or {}).get("player_config_sha256_start", "missing")
    m_start = (ctx or {}).get("player_marker_sha256_start", "missing")
    now, m_now = player_config_sha(), player_marker_sha()
    ev = {"file": str(clientbuild.PLAY / "WTF" / "Config.wtf"), "sha256_at_run_start": start, "sha256_now": now,
          "marker": str(clientbuild.PLAY / "WTF" / R.MARKER), "marker_sha256_at_run_start": m_start,
          "marker_sha256_now": m_now, "launcher_marker_present": m_now is not None}
    require(start != "missing" and m_start != "missing",
            "the runner didn't record the player's config and marker at the start of the run", **ev)
    require(now == start, "a check changed the player's own play client config during this run", **ev)
    require(m_now == m_start, "a check created, removed or changed the launcher marker in the player's play client",
            **ev)
    return ev
