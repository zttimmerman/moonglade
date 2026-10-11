"""true-pause (openspec/changes/true-pause, ADR-0013): Esc stops the whole world, closing the menu resumes it.

Headless checks: the probe logs in the companion harness character (hslib/pause.py: GM level, never PLAYER or HSTEST)
and asks for the pause exactly as the Moonglade add-on does (an "MGPAUSE" add-on whisper to itself); the server's
state comes from `.moonglade pause status` (SOAP) and the module's GM read-outs (`.moonglade pause unit|cooldown`), the
client's view from the packets the probe receives. Each pause lasts PAUSE_S unless the scenario says otherwise.
"Lost time" (pause.lost_ms) is how much a remaining time dropped beyond the unpaused wall time between two read-outs,
with the paused time measured by the probe (the server's "paused" answer to its "running" answer): ~0 when it froze,
~the pause when it ran on.
client.pause_menu (seamless_checks.py style) drives the real play client with hardware Esc presses."""
from __future__ import annotations

import time

from .. import config, db, pause as Z, soap, stack
from ..registry import check, require

CAP = "true-pause"
PAUSE_S = 20.0              # spec: paused for 20 s
FROZEN_MS = 1000            # spec: unchanged within 1 s
SWING_AFTER_S = 5.0         # spec: a swing arrives within 5 s of the resume
BOT_ACTS_S = 15.0           # the bot acts again after the resume (moves or casts) within this
SESSION_PAUSE_S = 300.0     # spec: 5 minutes
LEASH_SAMPLE_S = 6.0        # after the resume: kept chasing (leash on the frozen clock) vs. evaded at once (wall clock)


def _run(fn):
    try:
        return fn()
    except Z.C.CompanionError as e:
        require(False, str(e), **e.evidence)
    except Z.probe.ProbeError as e:
        require(False, f"probe: {e}", **e.info)


def _session(**kw) -> Z.Session:
    s = Z.Session(**kw)
    _run(s.__enter__)
    return s


def _close(s: Z.Session, ev: dict) -> None:
    """End the session (resume, remove companions, log out). A session the server already dropped can't run the
    cleanup commands; that is recorded, never raised over the check's own result."""
    try:
        s.__exit__(None, None, None)
    except (Z.probe.ProbeError, OSError) as e:
        ev["session_end_error"] = f"{type(e).__name__}: {e}"
        if s.ws:
            s.ws.close()
    ev["session_end"] = {k: s.ev.get(k) for k in ("resume_at_exit", "logout", "cleanup_errors")}


def _status_running(ev: dict, key: str = "status_before") -> dict:
    st = Z.status()
    ev[key] = st
    require(st["ok"] and st["state"] == "running" and st["enabled"] == 1,
            "GIVEN not met: the world isn't running with pausing enabled before the check", **ev)
    return st


# ---------------------------------------------------------------- the world pauses

# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "modules/mod-moonglade/src/MoongladePause.cpp",
    "modules/mod-moonglade/src/logic/Pause*",
    "modules/mod-moonglade/src/MoongladeAddon.*",
    "client-mods/addons/Moonglade/*",
    "modules/mod-moonglade/conf/mod_moonglade.conf.dist#Moonglade.Pause",
    "harness/hslib/checks/clientinput.py",
    "harness/hslib/checks/pause_checks.py",
    "harness/hslib/checks/seamless_checks.py",
    "harness/hslib/pause.py",
)






@check("pause.respawn_frozen", order=444, capability=CAP, stack="up", layer="integration", profile="scaling-off",
       identity=True)
def respawn_frozen(ctx):
    """Two pauses, one per GIVEN: the creature with the shortest respawn by rule stands where no creature can chase the
    character, and creatures only update with a player near them (Map::Update marks the cells around players), so
    the character stays beside each one. (1) GIVEN a creature killed with `.die` (pause.RESPAWN_RULE; R = its respawn
    remainder right after the kill), WHEN the world is paused for longer than R s and resumed, THEN it is still dead
    at the resume and comes back only once R s of unpaused time have passed since the kill (not earlier than 1 s
    before; the map checks respawns every 5 s, so up to 6 s later). (2) GIVEN a creature chasing the character beyond
    its leash radius (it attacked the character, which then ran pause.KITE_YD away), WHEN the world is paused for
    longer than R s and resumed, THEN it is still in combat LEASH_SAMPLE_S after the resume (its leash timer reads
    the frozen game clock; on the wall clock it would have evaded at once)."""
    ident = ctx.get("identity")
    if ident is not None:            # the fixture's identity (security level 3: `.npc add/set/delete` need it)
        from .. import companion as C
        with C.as_identity(ident.user, ident.password, ident.alt):
            return _respawn(ctx, {"fixture_identity": {"account": ident.user, "character": ident.alt}})
    return _respawn(ctx, {})


def _respawn(ctx: dict, ev0: dict) -> dict:
    ev = {"rule": Z.RESPAWN_RULE, "scratch_rule": SCRATCH_RULE} | ev0
    _status_running(ev)
    s = _session()
    scratch = None
    try:
        start = s.gps()
        cands = _run(lambda: Z.respawn_candidates())
        require(cands, "GIVEN not met: no creature matches the rule", **ev)
        c = cands[0]
        # a scratch spawn of the rule's creature where the rule's spawn stands (no creature can chase there)
        s.ws.set_selection(s.guid)
        s.ws.command(f".go xyz {c['spawn']['x']:.2f} {c['spawn']['y']:.2f} {c['spawn']['z']:.2f}", settle=0.4, timeout=4)
        top0 = int(db.scalar("SELECT COALESCE(MAX(guid), 0) FROM creature", "world") or 0)
        ev["npc_add"] = s.ws.command(f".npc add {c['entry']}", settle=0.6, timeout=5)
        rows = db.query(f"SELECT guid FROM creature WHERE guid > {top0} AND id = {c['entry']} ORDER BY guid", "world")
        require(len(rows) == 1, "GIVEN not met: `.npc add` didn't make exactly one scratch spawn", rows=rows, **ev)
        scratch = int(rows[0][0])
        ctx.setdefault("undo", []).append(lambda sid=scratch: _drop_scratch_spawn(sid))
        info = s.npc_info(scratch)
        require(info.get("guid"), "GIVEN not met: the scratch spawn isn't in the world", info=info, **ev)
        s.ws.set_selection(info["guid"])
        ev["spawntime"] = s.ws.command(f".npc set spawntime {SCRATCH_RESPAWN_S}", settle=0.4, timeout=4)
        st_db = db.scalar(f"SELECT spawntimesecs FROM creature WHERE guid = {scratch}", "world")
        victim = c | {"spawn_id": scratch, "guid": info["guid"], "spawntimesecs": int(st_db or 0), "scratch": True}
        ev["killed"] = victim
        require(0 < victim["spawntimesecs"] <= 15, "GIVEN not met: the scratch spawn's respawn time isn't at most 15 s", **ev)
        s.ws.set_selection(victim["guid"])
        ev["die"] = s.ws.command(".die", settle=0.3, timeout=4)
        t_kill = time.time()
        k0 = _run(lambda: Z.respawn_remaining(s, victim["spawn_id"]))
        ev["after_kill"] = k0
        require(k0["health"] == 0 and k0["remain_s"], "GIVEN not met: the creature isn't dead with a respawn time", **ev)
        R = k0["remain_s"]
        ev["R_s"] = R
        require(R <= 15, "GIVEN not met: the respawn remainder R is over 15 s", **ev)
        # (1) the respawn
        st0 = Z.status()
        p = _run(s.pause)
        ev["respawn_pause"] = p
        Z.wait_until(p["reply_t"] + R + RESPAWN_PAUSE_OVER_R_S)
        r = _run(s.resume)
        ev["respawn_resume"] = r
        k1 = _run(lambda: Z.respawn_remaining(s, victim["spawn_id"]))
        st1 = Z.status()
        ev["at_resume"], ev["status_respawn"] = k1, [st0, st1]
        paused_ms = round((r["reply_t"] - p["reply_t"]) * 1000)      # as the probe saw it (see cooldown_frozen)
        ev["respawn_paused_ms"] = paused_ms
        ev["server_paused_total_delta_ms"] = st1["paused_total_ms"] - st0["paused_total_ms"]
        expect = t_kill + R + paused_ms / 1000
        ev["respawn_expected_t"] = round(expect, 1)
        back = None
        while time.time() < expect + 10:
            k = _run(lambda: Z.respawn_remaining(s, victim["spawn_id"]))
            if k.get("health"):
                back = k
                break
            time.sleep(0.5)
        ev["respawned"] = back
        ev["respawn_vs_expected_s"] = round(back["t"] - expect, 1) if back else None
        # (2) the leash: the kite point's ground height first (`.go xyz x y` puts the character on the ground), then
        # the fight, then a run (a teleport would end the fight) away from the creature
        s.ws.command(f".go xyz {start['x']:.2f} {start['y']:.2f} {start['z']:.2f}", settle=0.4, timeout=4)
        s.ws.set_selection(s.guid)
        cr = _run(s.pick_live_creature)
        kp = _run(lambda: Z.kite_point(cr["npc_info"]["pos"], start, start.get("map", 0)))
        s.ws.command(f".go xyz {kp['x']:.2f} {kp['y']:.2f}", settle=0.4, timeout=4)
        kp["z"] = s.gps().get("z", cr["npc_info"]["pos"]["z"])
        ev["kite_to"] = kp
        fight = _run(lambda: s.engage(swing=False, creature=cr))
        ev["chaser"] = fight
        me = _run(lambda: s.unit(s.guid))["pos"]
        ev["run"] = s.run_to(me, kp)
        time.sleep(0.5)
        ch0 = _run(lambda: s.unit(fight["guid"]))
        ev["chaser_before_pause"] = ch0
        require(ch0["combat"] == 1, "GIVEN not met: the creature isn't chasing the character", **ev)
        p2 = _run(s.pause)
        ev["leash_pause"] = p2
        ev["leash_pause_after_engage_s"] = round(p2["reply_t"] - fight["attacked"]["t"], 1)
        Z.wait_until(p2["reply_t"] + R + LEASH_PAUSE_OVER_R_S)
        r2 = _run(s.resume)
        ev["leash_resume"] = r2
        Z.wait_until(r2["reply_t"] + LEASH_SAMPLE_S)
        ch1 = _run(lambda: s.unit(fight["guid"]))
        ev["chaser_after_resume"] = ch1
        ev["chaser_from_home_yd"] = round(Z.math.dist((ch1["pos"]["x"], ch1["pos"]["y"]),
                                                     (fight["pos"]["x"], fight["pos"]["y"])), 1)
        require(paused_ms / 1000 > R and (r2["sent_t"] - p2["reply_t"]) > R, "a pause wasn't longer than R", **ev)
        require(k1["health"] == 0, "the killed creature was alive at the resume", **ev)
        require(back is not None and -1 <= ev["respawn_vs_expected_s"] <= 6,
                "the killed creature didn't come back once R s of unpaused time had passed", **ev)
        require(ch1["combat"] == 1, "the chasing creature evaded (left combat) after the resume", **ev)
    finally:
        if scratch is not None and s.ws is not None:
            try:                        # in the world: `.npc delete <spawn>` finds it on the player's map
                ev["scratch_delete"] = s.ws.command(f".npc delete {scratch}", settle=0.6, timeout=5)
            except Exception as e:  # noqa: BLE001
                ev["scratch_delete_error"] = f"{type(e).__name__}: {e}"
        _close(s, ev)
        if scratch is not None:
            ev["scratch_dropped"] = _drop_scratch_spawn(scratch)
            if ctx.get("undo"):
                ctx["undo"] = [u for u in ctx["undo"] if getattr(u, "__defaults__", None) != (scratch,)]
    require(ev.get("scratch_dropped", {}).get("ok"), "the scratch spawn or its respawn data is still there", **ev)
    return ev


SCRATCH_RESPAWN_S = 5
# spec (true-pause "Respawn and leash freeze", harness-control "the two pauses each longer than that time"): each pause
# lasts R s plus this margin
RESPAWN_PAUSE_OVER_R_S = 3
LEASH_PAUSE_OVER_R_S = 5
SCRATCH_RULE = ("a scratch spawn (`.npc add`) of pause.RESPAWN_RULE's first creature at that creature's spawn point, "
                f"respawn time {SCRATCH_RESPAWN_S} s (`.npc set spawntime`), under the scaling-off profile (5 s corpse "
                "decay, config.PROFILES); removed afterwards with `.npc delete` and checked gone from world.creature and "
                "characters.creature_respawn")


def _drop_scratch_spawn(spawn_id: int) -> dict:
    """Check a scratch spawn is gone (row and respawn time); if its row is still there (the in-world delete didn't
    happen), try the GM command over SOAP once, then check again. Fail-closed: a query error raises."""
    out = {"spawn_id": spawn_id}
    if db.query(f"SELECT guid FROM creature WHERE guid = {int(spawn_id)}", "world"):
        out["delete_soap"] = soap.gm(f"npc delete {int(spawn_id)}", timeout=10).evidence()
    left = db.query(f"SELECT guid FROM creature WHERE guid = {int(spawn_id)}", "world")
    resp = db.query(f"SELECT guid FROM creature_respawn WHERE guid = {int(spawn_id)}", "characters")
    out |= {"creature_rows": left, "respawn_rows": resp, "ok": not left and not resp}
    return out



@check("pause.session_alive", order=446, capability=CAP, stack="up", layer="gate")
def session_alive(ctx):
    """GIVEN a paused probe session that sends only keep-alives (the probe pump: CMSG_KEEP_ALIVE every 15 s, nothing
    else), WHEN 5 minutes pass, THEN the session is still in the world (the server still reports paused and the
    pump never lost the connection) and it resumes normally ("running", then a `.gps` answer)."""
    ev = {}
    _status_running(ev)
    s = _session()
    try:
        p = _run(s.pause)
        ev["pause"] = p
        log_at_pause = len(s.ws.log)
        while time.time() - p["reply_t"] < SESSION_PAUSE_S:
            time.sleep(5)
            if s.ws.pump_error:
                break
        sent = [n for d, n in s.ws.log[log_at_pause:] if d == "C>S"]
        ev["client_packets_during_pause"] = {n: sent.count(n) for n in sorted(set(sent))}
        ev["pump_error"] = s.ws.pump_error
        ev["status_end"] = Z.status()
        require(not s.ws.pump_error, "the session was lost during the pause", **ev)
        require(set(ev["client_packets_during_pause"]) <= {"CMSG_KEEP_ALIVE"}, "the probe sent more than keep-alives", **ev)
        require(ev["status_end"]["state"] == "paused", "the world wasn't paused at the end of the 5 minutes", **ev)
        r = _run(s.resume)
        ev["resume"] = r
        ev["gps_after"] = s.gps()
        require("x" in ev["gps_after"], "no `.gps` answer after the resume", **ev)
    finally:
        _close(s, ev)
    return ev


# ---------------------------------------------------------------- when pausing is allowed

@check("pause.refused_on_taxi", order=447, capability=CAP, stack="up", layer="integration")
def refused_on_taxi(ctx):
    """GIVEN the companion character on a taxi flight (pause.TAXI_RULE; `.cheat taxi on`, CMSG_ACTIVATETAXI at the
    flight master, SMSG_ACTIVATETAXIREPLY ERR_TAXIOK), WHEN the pause add-on message arrives, THEN the client gets the
    refusal ("refused" answer and the system line "You can't pause while travelling.") and the world keeps running
    (`.moonglade pause status` running, the paused total unchanged, the flight still moving the character)."""
    ev = {"taxi": _run(Z.pick_taxi)}
    t = ev["taxi"]
    _status_running(ev)
    s = _session()
    try:
        fm = t["flightmaster"]
        s.ws.command(".combatstop", settle=0.3, timeout=3)
        s.ws.command(f".go xyz {fm['pos']['x']:.2f} {fm['pos']['y']:.2f} {fm['pos']['z']:.2f}", settle=0.4, timeout=4)
        s.ws.set_selection(s.guid)
        info = s.npc_info(fm["spawn_id"])
        ev["flightmaster_info"] = {k: info.get(k) for k in ("guid", "health", "pos")}
        require(info.get("guid"), "GIVEN not met: the flight master isn't in the world", **ev)
        s.ws.set_selection(s.guid)
        ev["taxicheat"] = s.ws.command(".cheat taxi on", settle=0.3, timeout=3)
        ev["money"] = s.ws.command(f".modify money {max(10000, t['price'] * 10)}", settle=0.3, timeout=3)
        t0 = time.time()
        s.ws.send(Z.CMSG_ACTIVATETAXI, Z.struct.pack("<QII", info["guid"], t["from"], t["to"]))
        rep = None
        while rep is None and time.time() - t0 < 5:
            rep = next(iter(s.tap.between(t0, kind="taxi_reply")), None)
            time.sleep(0.1)
        ev["taxi_reply"] = rep
        require(rep is not None and rep["reply"] == Z.ERR_TAXIOK, "GIVEN not met: the flight didn't start", **ev)
        time.sleep(2)
        st0 = Z.status()
        g0 = s.gps()
        since = s.ws.mark()
        q = s.request(True, ("paused", "refused", "disabled"))
        ev["request"] = q
        time.sleep(2)
        st1 = Z.status()
        g1 = s.gps()
        ev["status"], ev["gps"] = [st0, st1], [g0, g1]
        lines = [e["msg"] for e in s.ws.events_since(since) if e["kind"] == "chat" and e["type"] == Z.W.CHAT_MSG_SYSTEM]
        ev["system_lines"] = lines[:5]
        moved = Z.math.dist((g0["x"], g0["y"]), (g1["x"], g1["y"])) if "x" in g0 and "x" in g1 else None
        ev["flight_moved_yd"] = round(moved, 1) if moved is not None else None
        require(q["reply"] == "refused", f"the server answered {q['reply']!r}, not \"refused\"", **ev)
        require(Z.REFUSAL_TEXT in lines, "the client got no refusal message", **ev)
        require(st1["state"] == "running" and st1["paused_total_ms"] == st0["paused_total_ms"],
                "the world paused (status or paused total changed)", **ev)
        require(moved is not None and moved > 5, "the character didn't keep flying (the world isn't running)", **ev)
    finally:
        try:
            st = s.gps() if s.ws else {}
            s.ws.command(f".go xyz {st.get('x', 0):.2f} {st.get('y', 0):.2f}", settle=0.4, timeout=4) if "x" in st else None
            s.ws.set_selection(s.guid)
            ev["taxicheat_off"] = s.ws.command(".cheat taxi off", settle=0.3, timeout=3)
        except Exception as e:  # noqa: BLE001  (cleanup is best effort; the logout ends any flight anyway)
            ev["cleanup_error"] = str(e)
        _close(s, ev)
    return ev


@check("pause.logout_unpauses", order=448, capability=CAP, stack="up", layer="integration")
def logout_unpauses(ctx):
    """GIVEN a paused world (the plain harness character pause.PAUSE_CHAR, security 0 and so no instant logout,
    paused it with the add-on message), WHEN the character logs out (CMSG_LOGOUT_REQUEST), THEN the server answers
    with a countdown logout (SMSG_LOGOUT_RESPONSE result 0, not instant), the world is running right after the request
    (`.moonglade pause status`), and SMSG_LOGOUT_COMPLETE arrives within the normal 20 s countdown (+2 s)."""
    ev = {"account": _run(Z.ensure_plain_account)}
    _status_running(ev)
    _, ws, _ = _run(lambda: Z.probe.open_world(Z.PAUSE_USER, Z.PAUSE_PASS))
    try:
        c = next((c for c in ws.char_enum() if c["name"].lower() == Z.PAUSE_CHAR.lower()), None)
        require(c is not None, "the plain character isn't in the character list", **ev)
        ev["login"] = ws.player_login(c["guid"])
        ev["settle"] = Z.F.settle_login(ws, c["guid"])
        ws.start_pump()
        since = ws.mark()
        ws.chat(f"{Z.PREFIX}\t1", Z.W.CHAT_MSG_WHISPER, to=Z.PAUSE_CHAR, lang=Z.LANG_ADDON)
        ack = ws.wait_event(lambda e: e["kind"] == "chat" and e["lang"] == -1 and e["msg"].startswith(Z.PREFIX + "\t"),
                            since, 10)
        ev["pause_reply"] = ack and ack["msg"]
        ev["status_paused"] = Z.status()
        require(ack and ack["msg"].endswith("\tpaused") and ev["status_paused"]["state"] == "paused",
                "GIVEN not met: the world didn't pause", **ev)
        time.sleep(2)
        since = ws.mark()
        t0 = time.time()
        ws.send(Z.W.CMSG_LOGOUT_REQUEST)
        resp = ws.wait_event(lambda e: e["kind"] == "logout_response", since, timeout=10)
        ev["logout_response"] = resp
        ev["status_after_request"] = Z.status()
        done = ws.wait_event(lambda e: e["kind"] == "logout_complete", since, timeout=Z.LOGOUT_COUNTDOWN_S + 10)
        ev["logout_complete_after_s"] = round(done["t"] - t0, 1) if done else None
        require(resp is not None and resp["result"] == 0 and not resp["instant"],
                "GIVEN not met: the logout wasn't a countdown logout", **ev)
        require(ev["status_after_request"]["state"] == "running", "the world was still paused after the logout request",
                **ev)
        require(done is not None and ev["logout_complete_after_s"] <= Z.LOGOUT_COUNTDOWN_S + 2,
                "the logout didn't complete within the 20 s countdown", **ev)
    finally:
        ws.close()
        try:
            ev["resume_at_exit"] = soap.gm("moonglade pause off", timeout=10).evidence()
        except OSError as e:
            ev["resume_at_exit"] = str(e)
    return ev


DROP_RESUME_S = 5.0         # spec: "within a few seconds" (the session timeout it must not wait for is 60 s of game time)
OFFLINE_WAIT_S = 90         # afterwards: the dropped session's character goes offline (WorldSessionMgr, 60 s)


@check("pause.disconnect_unpauses", order=451, capability=CAP, stack="up", layer="integration", identity=True)
def disconnect_unpauses(ctx):
    """GIVEN a world paused by a connected client (the companion probe session; the server answered "paused" and
    `.moonglade pause status` says paused), WHEN that client's connection is cut without a logout (the probe's socket
    is shut down and closed; no CMSG_LOGOUT_REQUEST), THEN `.moonglade pause status` reports running within 5 s of
    the cut. test-pyramid: the session is the test's own fixture character (ctx["identity"]: account and its human
    warrior), so nothing waits for it to go offline afterwards (no later test uses it)."""
    ev = {}
    ident = ctx.get("identity")
    if ident is not None:
        from .. import companion as C
        ev["fixture_identity"] = {"account": ident.user, "character": ident.alt}
        with C.as_identity(ident.user, ident.password, ident.alt):
            return _disconnect(ev)
    return _disconnect(ev, wait_offline=True)


def _disconnect(ev: dict, wait_offline: bool = False) -> dict:
    _status_running(ev)
    s = _session()
    cut = False
    try:
        p = _run(s.pause)
        ev["pause"] = p
        st = Z.status()
        ev["status_paused"] = st
        require(st["state"] == "paused", "GIVEN not met: the world isn't paused", **ev)
        time.sleep(2)
        s.ws.stop_pump()
        t_cut = time.time()
        import socket as _socket
        try:
            s.ws.sock.shutdown(_socket.SHUT_RDWR)
        except OSError as e:
            ev["shutdown_error"] = str(e)
        s.ws.sock.close()
        s.ws.sock = None
        cut = True
        ev["cut_t"] = round(t_cut, 3)
        samples, running = [], None
        while time.time() - t_cut < DROP_RESUME_S + 5:
            st = Z.status()
            samples.append({"after_s": round(st["t"] - t_cut, 2), "state": st["state"]})
            if st["state"] == "running":
                running = st | {"after_cut_s": round(st["t"] - t_cut, 2)}
                break
            time.sleep(0.25)
        ev["status_samples"], ev["status_running"] = samples[-8:], running
        require(running is not None and running["after_cut_s"] <= DROP_RESUME_S,
                "the world didn't unpause within 5 s of the client's connection being cut", **ev)
    finally:
        if not cut:
            _close(s, ev)
        else:
            try:
                ev["resume_at_exit"] = soap.gm("moonglade pause off", timeout=10).evidence()
            except OSError as e:
                ev["resume_at_exit"] = str(e)
            if wait_offline:            # the shared companion character: later checks log it in
                t0 = time.time()
                while time.time() - t0 < OFFLINE_WAIT_S and (Z.C.char_row() or {}).get("online"):
                    time.sleep(2)
                ev["character_offline_after_s"] = round(time.time() - t0, 1)
    return ev


@check("pause.disabled_noop", order=449, capability=CAP, stack="up", layer="integration", profile="pause-off")
def disabled_noop(ctx):
    """GIVEN Moonglade.Pause.Enable = 0 (the stack restarted with the pause-off config profile; `.moonglade pause
    status` says enabled 0), WHEN the pause add-on message arrives during a fight, THEN nothing pauses: the server
    answers "disabled", reports running with an unchanged paused total, and melee swings keep arriving (one within
    5 s after the answer). test-pyramid: the run's pause-off profile group provides the restart (runner.segment and
    _ensure_stack); the next default-profile test restarts back."""
    ev = {"profile": stack.running_profile()}
    if True:
        st0 = Z.status()
        ev["status_before"] = st0
        require(st0["enabled"] == 0, "GIVEN not met: Moonglade.Pause.Enable isn't 0 on the server", **ev)
        s = _session()
        try:
            fight = _run(s.engage)
            ev["fight"] = fight
            cg = fight["guid"]
            q = s.request(True, ("paused", "refused", "disabled"))
            ev["request"] = q
            swing = s.ws.wait_event(lambda e: e["kind"] == "melee_damage" and {e["attacker"], e["victim"]} == {cg, s.guid},
                                    (q["reply_i"] or q["sent_i"]) + 1, timeout=SWING_AFTER_S + 1)
            ev["swing_after_request"] = swing
            st1 = Z.status()
            ev["status_after"] = st1
            require(q["reply"] == "disabled", f"the server answered {q['reply']!r}, not \"disabled\"", **ev)
            require(st1["state"] == "running" and st1["paused_total_ms"] == st0["paused_total_ms"],
                    "the world paused", **ev)
            require(swing is not None and swing["t"] - (q["reply_t"] or q["sent_t"]) <= SWING_AFTER_S,
                    "no melee swing within 5 s after the answer (combat stopped)", **ev)
        finally:
            _close(s, ev)
    return ev


# ---------------------------------------------------------------- Esc in the real client

PAUSE_HOOK = '-- planted by the harness (pause_checks.py): the add-on\'s pause test mode\r\nMoongladeTest = {\r\n\t["hook"] = "pause",\r\n}\r\n'
MENU_GLUE = {"marker": False, "script": [{"t": 4, "act": "shot"}, {"t": 20, "act": "continue"}],
             "charselect": [{"t": 2, "act": "play"}]}
WORLD_TIMEOUT = 300         # the client up -> Continue at 20 s -> character select -> Play -> in the world
ESC_TRIES, ESC_WAIT_S = 6, 6
REPORT_TIMEOUT_S = 120
BANNER_GOLD_MIN = 40        # gold text pixels (GameFontNormalHuge, 1.0/0.82/0) in the banner box of the paused shot
# The dimming (spec: "the world view dimmed"): mean brightness of a world region away from the menu (centre), the
# banner (top centre), the unit frame (top left), the chat and action bars (bottom) and the tutorial/minimap (right),
# as fractions of the screenshot. The add-on dims with black at 50 % opacity, which halves it; "clearly lower" = at
# most DIM_MAX_RATIO of the pre-Esc shot, "back" = within RESTORE_TOL of it (idle animations, the day/night sky).
DIM_REGION = (0.04, 0.26, 0.30, 0.56)
DIM_MAX_RATIO = 0.75
RESTORE_TOL = 0.12


def _gold_pixels(path, box) -> int:
    """Pixels in `box` (x0, y0, x1, y1, fractions of the screenshot) that are the banner's gold text colour."""
    from .playclient_util import _bmp_pixels
    w, h, px = _bmp_pixels(path)
    n = 0
    for y in range(int(box[1] * h), int(box[3] * h), 2):
        for x in range(int(box[0] * w), int(box[2] * w), 2):
            r, g, b = px(x, y)
            n += r > 190 and 120 < g < 230 and b < 90 and r - b > 120
    return n


def _brightness(path, box=DIM_REGION) -> float:
    """Mean (r + g + b) / 3 over `box` (fractions of the screenshot), sampled every 4th pixel."""
    from .playclient_util import _bmp_pixels
    w, h, px = _bmp_pixels(path)
    tot = n = 0
    for y in range(int(box[1] * h), int(box[3] * h), 4):
        for x in range(int(box[0] * w), int(box[2] * w), 4):
            tot += sum(px(x, y)) / 3
            n += 1
    return round(tot / n, 2) if n else 0.0


def _banner_box(path) -> tuple:
    """The banner's frame (MoongladePauseBanner: 320 x 72 UI units, TOP of UIParent at -64), as fractions of the
    screenshot, plus a margin. UIParent is 768 units high; its width follows the aspect ratio."""
    from .playclient_util import _bmp_pixels
    w, h, _ = _bmp_pixels(path)
    u = h / 768
    return ((w / 2 - 170 * u) / w, (60 * u) / h, (w / 2 + 170 * u) / w, (140 * u) / h)


def pause_menu(ctx):
    """GIVEN the play client in the world (a scratch patched play client, ./play --account test, the harness test
    account; the add-on's pause test mode planted in its SavedVariables), WHEN a hardware Esc (SendInput in the
    client's Wine prefix) opens the game menu and a second one closes it, THEN `.moonglade pause status` reports
    paused while the menu is open, the add-on's screenshot taken then shows the "Paused" banner (its report: banner and
    menu shown; the screenshot's banner box holds the banner's gold text, the shot after the resume doesn't) and the
    world view dimmed (DIM_REGION's mean brightness at most DIM_MAX_RATIO of the add-on's pre-Esc shot), and after the
    menu closes the server reports running and DIM_REGION's brightness is back within RESTORE_TOL of the pre-Esc
    shot. test-pyramid (journey.pause, a warm journey): with the shared stack up, ./play runs with HS_PLAY_KEEP_STACK
    and leaves it up; GIVEN nothing else of this repo running (no client, Wine or ./play), and afterwards the same:
    nothing left running except that stack, with the same pids as before."""
    from . import clientinput
    from .play_checks import is_clear, remaining, require_given_clear
    from .playclient_util import PlayRun, build_scratch, drop_scratch, fresh_scratch, screenshots
    from .seamless_checks import addon_report, addon_sv_path, server_logins
    from .. import paths as P

    from .. import play as _play
    ev: dict = {"account": P.TEST_USER, "glue_test": MENU_GLUE}
    scratch = run = None
    warm = all(stack.stack_processes().values())
    ev["warm"] = warm
    try:
        if warm:
            stack_before = _stack_pids()
            ev["stack_before"] = stack_before
            ev["given_nothing_else_running"] = before = _without_stack(remaining())
            require(is_clear(before), "GIVEN not met: something besides the shared stack is already running", **ev)
        else:
            require_given_clear(ev)
        scratch = fresh_scratch("pause")
        build = build_scratch(scratch)
        ev["scratch"] = {"dir": str(scratch), "addon_outputs": sorted(o for o in build["stamp"]["outputs"]
                                                                      if o.startswith("Interface/AddOns/Moonglade/"))}
        require(scratch.parent == P.ENV and scratch.name.startswith("check-play-"),
                "refusing to plant the add-on test hook outside a scratch play client", dir=str(scratch))
        sv = addon_sv_path(scratch)
        sv.parent.mkdir(parents=True, exist_ok=True)
        sv.write_text(PAUSE_HOOK)
        run = PlayRun("pause", glue_test=MENU_GLUE, scratch=scratch,
                      extra_env={_play.KEEP_STACK_ENV: "1", "HS_HARNESS_RUN": "1"} if warm else None)
        run.start()
        up = run.wait_client()
        ev["client_up"] = {k: v for k, v in up.items() if k != "t"}
        require(up.get("up"), "the real Wow.exe never came up", output=run.output()[-2500:], **ev)
        t_up = up["t"]
        t0 = time.time()
        while time.time() - t0 < 120 and not [p for p in screenshots(scratch) if p.stat().st_mtime >= t_up - 5]:
            time.sleep(0.5)
        ev["client_input"] = clientinput.press_shift(run.tmp)
        require(ev["client_input"]["injected"] == 2, "GIVEN not met: the hardware key press didn't reach the client", **ev)
        login = None
        while time.time() - t_up < WORLD_TIMEOUT and not login:
            login = next((x for x in server_logins(P.TEST_CHAR) if x["t"] >= int(t_up)), None)
            time.sleep(2)
        ev["server_login"] = login
        require(login is not None, "GIVEN not met: the character never entered the world", **ev)
        # the add-on's pre-Esc screenshot of the world view (taken once the world UI is up), before the first Esc
        t_w = time.time()
        while time.time() - t_w < 60 and not [p for p in screenshots(scratch) if p.stat().st_mtime >= login["t"]]:
            time.sleep(0.5)
        ev["before_shot_after_s"] = round(time.time() - t_w, 1)
        time.sleep(1)
        # WHEN: Esc opens the game menu
        presses, paused = [], None
        for _ in range(ESC_TRIES):
            presses.append(clientinput.press_key(run.tmp, clientinput.VK_ESCAPE, clientinput.SCAN_ESCAPE, "hs-esc.exe"))
            t_press = time.time()
            while time.time() - t_press < ESC_WAIT_S:
                st = Z.status()
                if st["state"] == "paused":
                    paused = st | {"after_press_s": round(time.time() - t_press, 2)}
                    break
                time.sleep(0.3)
            if paused:
                break
        ev["esc_open"], ev["status_menu_open"] = presses, paused
        require(paused is not None, "the server never reported paused after Esc", **ev)
        n_shots = len(screenshots(scratch))
        t1 = time.time()
        while time.time() - t1 < 15 and len(screenshots(scratch)) <= n_shots:
            time.sleep(0.5)
        time.sleep(1)
        # WHEN: Esc closes it
        ev["esc_close"] = clientinput.press_key(run.tmp, clientinput.VK_ESCAPE, clientinput.SCAN_ESCAPE, "hs-esc.exe")
        t2, running = time.time(), None
        while time.time() - t2 < 10:
            st = Z.status()
            if st["state"] == "running":
                running = st | {"after_press_s": round(time.time() - t2, 2)}
                break
            time.sleep(0.3)
        ev["status_menu_closed"] = running
        t3, rep = time.time(), None
        while time.time() - t3 < REPORT_TIMEOUT_S and rep is None:
            rep = addon_report(scratch)
            time.sleep(2)
        ev["addon_report"] = {k: (rep or {}).get(k) for k in ("at", "errors", "error_msgs", "pause", "diag")}
        shots = [p for p in screenshots(scratch) if p.stat().st_mtime >= t_up - 5]
        ev["screenshots"] = [{"file": p.name, "mtime": round(p.stat().st_mtime, 2)} for p in shots]
        ev["stop"] = run.stop()
        pr = (rep or {}).get("pause") or {}
        pshots = pr.get("shots") or []
        by_tag = {x.get("tag"): x for x in (pshots.values() if isinstance(pshots, dict) else pshots)}
        ev["report_shots"] = by_tag
        # the add-on's three screenshots are the newest three: before Esc, after the pause, after the resume
        if len(shots) >= 3:
            sb, sp, sr = shots[-3], shots[-2], shots[-1]
            ev["brightness"] = {"region": DIM_REGION, "before": _brightness(sb), "paused": _brightness(sp),
                                "running": _brightness(sr), "files": [sb.name, sp.name, sr.name]}
            box = _banner_box(sp)
            ev["banner_box"] = [round(v, 3) for v in box]
            ev["gold_pixels"] = {"paused_shot": _gold_pixels(sp, box), "running_shot": _gold_pixels(sr, box),
                                 "files": [sp.name, sr.name]}
            P.OUT.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copyfile(sp, P.OUT / "pause-menu-paused.jpg")
            shutil.copyfile(sr, P.OUT / "pause-menu-running.jpg")
            shutil.copyfile(sb, P.OUT / "pause-menu-before.jpg")
            ev["kept_shots"] = ["harness/out/pause-menu-before.jpg", "harness/out/pause-menu-paused.jpg",
                                "harness/out/pause-menu-running.jpg"]
        gp = ev.get("gold_pixels") or {}
        require(running is not None, "the server didn't report running after the menu closed", **ev)
        require(rep is not None and pr.get("mode") == "pause", "no add-on report from the pause test mode", **ev)
        require((by_tag.get("paused") or {}).get("banner") == 1 and (by_tag.get("paused") or {}).get("menu") == 1,
                "the add-on's paused screenshot wasn't taken with the banner and the menu shown", **ev)
        require((by_tag.get("running") or {}).get("banner") == 0, "the banner was still shown after the resume", **ev)
        require(gp.get("paused_shot", 0) >= BANNER_GOLD_MIN and gp.get("running_shot", 0) * 4 < gp.get("paused_shot", 0),
                "the paused screenshot doesn't show the banner's text in its box (or the running one does too)", **ev)
        br = ev.get("brightness") or {}
        require(br and (by_tag.get("before") or {}).get("dim") == 0 and (by_tag.get("paused") or {}).get("dim") == 1
                and (by_tag.get("running") or {}).get("dim") == 0,
                "the add-on's report doesn't show the dimming only while paused (or the pre-Esc shot is missing)", **ev)
        require(br["paused"] <= DIM_MAX_RATIO * br["before"],
                "the world view isn't clearly dimmed while paused (region brightness vs. the pre-Esc shot)", **ev)
        require(abs(br["running"] - br["before"]) <= RESTORE_TOL * br["before"],
                "the world view's brightness didn't come back after the menu closed", **ev)
        require(rep.get("errors") == 0, "the add-on counted Lua errors", **ev)
    finally:
        if run is not None:
            run.cleanup()
        ev["remaining_after"] = remaining()
        drop_scratch(scratch)
    if warm:
        ev["stack_after"] = _stack_pids()
        require(is_clear(_without_stack(ev["remaining_after"])), "something besides the shared stack is still running", **ev)
        require(ev["stack_after"] == ev["stack_before"], "the shared stack didn't stay up with the same pids", **ev)
    else:
        require(is_clear(ev["remaining_after"]), "something is still running", **ev)
    return ev


def _stack_pids() -> dict:
    return {k: sorted(p if isinstance(p, int) else p.get("pid", p) for p in v) for k, v in stack.stack_processes().items()}


def _without_stack(r: dict) -> dict:
    """play_checks.remaining() without the shared stack's processes and ports (a warm journey keeps them)."""
    return {k: v for k, v in r.items() if k not in ("stack", "ports_open")}


# ---------------------------------------------------------------- the freeze matrix (test-pyramid: 5 scenarios, one pause)

FREEZE_CHILDREN = ("pause.combat_frozen", "pause.aura_frozen", "pause.cooldown_frozen", "pause.cast_cancelled",
                   "pause.bots_frozen")


@check("pause.freeze_matrix", order=440, capability=CAP, stack="up", layer="integration", children=FREEZE_CHILDREN)
def freeze_matrix(ctx):
    """One session establishes the GIVENs of the five freeze scenarios together (a player spell and a creature
    ability on cooldown, a moving or casting priest companion, melee with a creature that carries the character's DoT
    while the character carries a buff, a cast in progress), then ONE 20 s pause, then each scenario's THEN, recorded
    under its own id: pause.combat_frozen, pause.aura_frozen, pause.cooldown_frozen, pause.cast_cancelled and
    pause.bots_frozen (each with the assertions of its scenario; a GIVEN that couldn't be established fails that
    scenario)."""
    from ..registry import CheckFail
    ev: dict = {}
    res: dict = {}
    given: dict = {}

    def fail_all(msg, **e):
        for cid in FREEZE_CHILDREN:
            res.setdefault(cid, {"status": "fail", "message": msg, "evidence": e or {"given": False}})
    _status_running(ev)
    spells = _run(Z.pick_spells)
    ev["spells"] = {k: v for k, v in spells.items() if k in ("buff", "dot", "player_cd", "creature_cd", "cast")}
    buff, dot = spells["buff"]["id"], spells["dot"]["id"]
    pcd, ccd, sp = spells["player_cd"]["id"], spells["creature_cd"]["id"], spells["cast"]["id"]
    s = _session()
    t0 = time.time()
    try:
        # the companion
        s.ws.command(".combatstop", settle=0.3, timeout=3)
        add = _run(lambda: s.addclass("priest"))
        bot = add["bot"]
        given["bot"] = {k: bot.get(k) for k in ("guid", "name", "class")}
        given["bot_near"] = s.wait_near(bot["guid"], 10, 30)["ok"]
        # cooldowns: a creature B 25 yd away casts its ability back; the player spell goes on cooldown
        s.ws.set_selection(s.guid)
        crb = _run(s.pick_live_creature)
        cgb = crb["guid"]
        here = s.gps()
        stage = _run(lambda: Z.kite_point(crb["npc_info"]["pos"], here, here.get("map", 0), dist=25))
        s.ws.command(f".go xyz {stage['x']:.2f} {stage['y']:.2f}", settle=0.4, timeout=4)
        s.ws.set_selection(s.guid)
        s.ws.command(".cooldown", settle=0.3, timeout=4)
        s.ws.command(f".cast {pcd}", settle=0.4, timeout=4)
        s.ws.set_selection(cgb)
        s.ws.command(f".cast back {ccd}", settle=0.4, timeout=4)
        s.ws.command(f".unaura {ccd}", settle=0.4, timeout=4)
        given["cooldown"] = {"creature": {k: crb[k] for k in ("spawn_id", "name", "guid")}}
        # melee with creature A (not B), the buff on the character and the DoT on A
        fight = _run(lambda: s.engage(exclude={crb["spawn_id"]}))
        cg = fight["guid"]
        given["fight"] = fight
        i_given = s.ws.mark()
        first = s.ws.wait_event(lambda e: e["kind"] == "melee_damage" and {e["attacker"], e["victim"]} == {cg, s.guid},
                                0, timeout=10)
        given["swing_before_pause"] = first
        s.ws.set_selection(s.guid)
        s.ws.command(f".aura {buff}", settle=0.3, timeout=4)
        s.ws.set_selection(cg)
        s.ws.command(f".aura {dot}", settle=0.3, timeout=4)
        t_dot = time.time()
        tick = None
        while tick is None and time.time() - t_dot < 8:
            tick = next(iter(s.tap.between(t_dot - 1, kind="periodic", spell=dot, victim=cg)), None)
            time.sleep(0.2)
        given["dot_tick"] = tick
        b0 = _run(lambda: s.aura(s.guid, buff))
        given["buff_before"] = b0
        # the bot moving or casting right before the pause
        u_a, moving, bsamples = _run(lambda: s.unit(bot["guid"])), 0.0, []
        u_b = u_a
        t_move = time.time()
        while time.time() - t_move < 6:
            time.sleep(0.3)
            u_b = _run(lambda: s.unit(bot["guid"]))
            moving = Z.math.dist((u_a["pos"]["x"], u_a["pos"]["y"]), (u_b["pos"]["x"], u_b["pos"]["y"]))
            bsamples.append(round(moving, 2))
            if moving > 0.2 or u_b["spell"] or u_b["channel"]:
                break
            u_a = u_b
        given["bot_before"], given["bot_moved_per_0_3s_yd"] = [u_a, u_b], bsamples
        # the cast, then the pause at once
        s.ws.set_selection(s.guid)
        tc = time.time()
        s.ws.chat(f".cast {sp}")
        start = None
        while start is None and time.time() - tc < 5:
            start = next(iter(s.tap.between(tc, kind="spell_start", spell=sp, caster=s.guid)), None)
            time.sleep(0.05)
        given["spell_start"] = start
        c0 = {"player": _run(lambda: s.cooldown(s.guid, pcd)), "creature": _run(lambda: s.cooldown(cgb, ccd))}
        given["cooldown"]["before"] = c0                 # right before the pause (the GIVEN at the WHEN)
        st0 = Z.status()
        p = _run(s.pause)
        ev["pause"] = p
        at = {"me": _run(lambda: s.unit(s.guid)), "creature": _run(lambda: s.unit(cg)),
              "bot": _run(lambda: s.unit(bot["guid"])), "buff": _run(lambda: s.aura(s.guid, buff))}
        Z.wait_until(p["reply_t"] + PAUSE_S)
        end = {"me": _run(lambda: s.unit(s.guid)), "creature": _run(lambda: s.unit(cg)),
               "bot": _run(lambda: s.unit(bot["guid"])), "buff": _run(lambda: s.aura(s.guid, buff))}
        unrooted_early = s.tap.after(p["reply_i"], s.ws.mark(), kind="unroot", guid=s.guid)
        ev["status_paused"] = Z.status()
        r = _run(s.resume)
        ev["resume"] = r
        b1 = _run(lambda: s.aura(s.guid, buff))
        c1 = {"player": _run(lambda: s.cooldown(s.guid, pcd)), "creature": _run(lambda: s.cooldown(cgb, ccd))}
        st1 = Z.status()
        paused_ms = round((r["reply_t"] - p["reply_t"]) * 1000)
        ev |= {"paused_ms": paused_ms, "server_paused_total_delta_ms": st1["paused_total_ms"] - st0["paused_total_ms"],
               "pause_after_given_s": round(p["sent_t"] - t0, 1)}
        # after the resume: one observation window that covers every scenario's "after" (the longest is the bot's 15 s)
        swing = s.ws.wait_event(lambda e: e["kind"] == "melee_damage" and {e["attacker"], e["victim"]} == {cg, s.guid}
                                and e["t"] >= r["sent_t"], 0, timeout=SWING_AFTER_S + 1)
        acted = None
        while time.time() - r["reply_t"] < BOT_ACTS_S:
            u = _run(lambda: s.unit(bot["guid"]))
            if u["pos"] != end["bot"]["pos"] or u["spell"] != end["bot"]["spell"] or u["channel"] != end["bot"]["channel"]:
                acted = u
                break
            time.sleep(0.5)
        if time.time() - r["sent_t"] < 5:
            time.sleep(5 - (time.time() - r["sent_t"]))      # the cast/DoT scenarios observe 5 s after the resume
        aura_cast_after = _run(lambda: s.aura(s.guid, sp))

        def scenario(cid, fn):
            try:
                e = fn()
                res[cid] = {"status": "pass", "evidence": e}
            except CheckFail as x:
                res[cid] = {"status": "fail", "message": str(x)[:500], "evidence": x.evidence}
        common = {"paused_ms": paused_ms, "pause": p, "resume": r}

        def combat():
            during = s.swings(p["reply_i"], r["sent_i"])
            e = common | {"fight": fight, "swing_before_pause": first, "at_pause_start": {k: at[k] for k in ("me", "creature")},
                          "at_pause_end": {k: end[k] for k in ("me", "creature")}, "swings_during_pause": during[:10],
                          "first_swing_after_resume": swing, "status_paused": ev["status_paused"]}
            require(first is not None, "GIVEN not met: no melee swing between the character and the creature", **e)
            require(ev["status_paused"]["state"] == "paused", "the server didn't report paused during the pause", **e)
            require(at["me"]["health"] == end["me"]["health"], "the character's health changed during the pause", **e)
            require(at["creature"]["health"] == end["creature"]["health"], "the creature's health changed during the pause", **e)
            require(not during, f"{len(during)} SMSG_ATTACKERSTATEUPDATE arrived during the pause", **e)
            require(paused_ms >= PAUSE_S * 1000, "the pause was shorter than 20 s", **e)
            require(swing is not None and swing["t"] - r["sent_t"] <= SWING_AFTER_S, "no melee swing within 5 s of the resume", **e)
            return e

        def aura():
            lost = Z.lost_ms(b0["duration"], b0["t"], b1["duration"], b1["t"], paused_ms) if b0["duration"] and b1["duration"] else None
            ticks = s.tap.after(p["reply_i"], r["sent_i"], kind="periodic", spell=dot)
            upd = s.tap.between(r["sent_t"], kind="aura_update", guid=s.guid, spell=buff)
            e = common | {"dot_tick_before_pause": tick, "buff_before": b0, "buff_during": [at["buff"], end["buff"]],
                          "buff_after": b1, "buff_lost_ms": lost, "dot_ticks_during_pause": ticks[:5],
                          "dot_ticks_after_resume": len(s.tap.between(r["sent_t"], kind="periodic", spell=dot)),
                          "client_buff_updates_after_resume": upd[:3]}
            require(tick is not None, "GIVEN not met: the DoT never ticked on the creature", **e)
            require((b0["duration"] or 0) >= 30000, "GIVEN not met: the buff has less than 30 s left", **e)
            require(paused_ms >= PAUSE_S * 1000, "the pause was shorter than 20 s", **e)
            require(lost is not None and abs(lost) < FROZEN_MS, f"the buff lost {lost} ms during the pause", **e)
            require(not ticks, f"{len(ticks)} DoT ticks during the pause", **e)
            u = upd[0] if upd else None
            require(u is not None and u["t"] - r["sent_t"] <= 3 and "duration" in u,
                    "the client got no SMSG_AURA_UPDATE with the buff's duration after the resume", **e)
            e["client_vs_server_ms"] = round(u["duration"] - (b1["duration"] + (b1["t"] - u["t"]) * 1000))
            require(abs(e["client_vs_server_ms"]) < FROZEN_MS,
                    "the buff duration sent to the client differs from the server's by 1 s or more", **e)
            return e

        def cooldown():
            lost = {k: Z.lost_ms(c0[k]["ms"], c0[k]["t"], c1[k]["ms"], c1[k]["t"], paused_ms) for k in c0}
            corr = ([x for x in s.tap.between(r["sent_t"], kind="modify_cooldown") if x["spell"] == pcd and x["guid"] == s.guid]
                    + [x for x in s.tap.between(r["sent_t"], kind="spell_cooldown")
                       if x["guid"] == s.guid and any(c["spell"] == pcd for c in x["cooldowns"])])
            e = common | {"given": given["cooldown"], "cooldowns_after": c1, "lost_ms": lost, "client_corrections": corr[:3]}
            require(c0["player"]["ms"] >= 30000 - 5000 and c0["creature"]["ms"] > 0,
                    "GIVEN not met: the player spell or the creature ability isn't on cooldown", **e)
            require(paused_ms >= PAUSE_S * 1000, "the pause was shorter than 20 s", **e)
            for k, v in lost.items():
                require(abs(v) < FROZEN_MS, f"the {k} cooldown lost {v} ms during the pause", **e)
            require(corr and corr[0]["t"] - r["sent_t"] <= 3,
                    "the client got no cooldown correction for the player spell after the resume", **e)
            return e

        def cast():
            fails = [x for x in s.tap.between(start["t"] if start else tc) if x.get("spell") == sp
                     and ((x["kind"] in ("spell_failure", "spell_failed_other") and x["result"] == Z.SPELL_FAILED_INTERRUPTED)
                          or x["kind"] == "cast_failed")]
            gos = s.tap.between(start["t"] if start else tc, kind="spell_go", spell=sp, caster=s.guid)
            roots = s.tap.between(p["sent_t"], p["reply_t"] + 1, kind="root", guid=s.guid)
            unroots = s.tap.between(r["sent_t"], r["reply_t"] + 1, kind="unroot", guid=s.guid)
            e = common | {"spell": ev["spells"]["cast"], "spell_start": start, "interrupted": fails[:3], "spell_go": gos[:3],
                          "root": roots[:2], "unroot_during_pause": unrooted_early, "unroot_after_resume": unroots[:2],
                          "aura_after": aura_cast_after}
            require(start is not None, "GIVEN not met: the cast never started (no SMSG_SPELL_START)", **e)
            e["pause_after_cast_start_s"] = round(p["sent_t"] - start["t"], 3)
            require(p["sent_t"] - start["t"] < ev["spells"]["cast"]["cast_ms"] / 1000,
                    "GIVEN not met: the pause request went out after the cast would have finished", **e)
            require(fails and fails[0]["t"] <= p["reply_t"] + 0.5, "the client got no interrupted notice for the cast", **e)
            require(not gos, "the cast's SMSG_SPELL_GO arrived (the spell went off)", **e)
            require(aura_cast_after["duration"] is None, "the cast's aura is on the character", **e)
            require(roots, "the character wasn't rooted when the pause began", **e)
            require(not unrooted_early and unroots, "the character wasn't kept rooted until the resume", **e)
            return e

        def bots():
            same = {k: at["bot"][k] == end["bot"][k] for k in ("pos", "health", "power", "spell", "channel")}
            ub = given["bot_before"][1]
            e = common | {"bot": given["bot"], "bot_before": given["bot_before"],
                          "moved_per_0_3s_yd": given["bot_moved_per_0_3s_yd"], "during": [at["bot"], end["bot"]],
                          "acted_after_resume": acted, "unchanged_during_pause": same}
            require(moving > 0.2 or ub["spell"] or ub["channel"], "GIVEN not met: the bot was neither moving nor casting", **e)
            require(all(same.values()), f"the bot changed during the pause: {[k for k, v in same.items() if not v]}", **e)
            require(acted is not None, "the bot didn't act again within 15 s of the resume", **e)
            return e
        for cid, fn in (("pause.combat_frozen", combat), ("pause.aura_frozen", aura), ("pause.cooldown_frozen", cooldown),
                        ("pause.cast_cancelled", cast), ("pause.bots_frozen", bots)):
            scenario(cid, fn)
    except CheckFail as e:
        fail_all(f"the shared session failed before the scenarios' THEN: {e}", **(e.evidence or {}))
    finally:
        _close(s, ev)
    ev["given"] = {k: v for k, v in given.items() if k != "bot_before"}
    ev["children"] = {k: res[k] | {"duration_s": round(time.time() - t0, 1)} for k in FREEZE_CHILDREN if k in res}
    for cid in FREEZE_CHILDREN:
        ev["children"].setdefault(cid, {"status": "error", "message": "not evaluated", "evidence": {"session": "freeze"}})
    return ev


# ---------------------------------------------------------------- journey.pause (test-pyramid: the fixed journey set)

JOURNEY_PAUSE_COVERS = ("client-mods/addons/Moonglade/*", "modules/mod-moonglade/src/MoongladePause.cpp",
                        "modules/mod-moonglade/src/logic/Pause*",
                        "harness/hslib/checks/pause_checks.py", "harness/hslib/pause.py",
                        "harness/hslib/checks/clientinput.py", "harness/hslib/checks/seamless_checks.py")


@check("journey.pause", order=450, capability=CAP, stack="up", layer="journey", budget_s=90,
       covers=JOURNEY_PAUSE_COVERS, children=("client.pause_menu",))
def journey_pause(ctx):
    """The real client in the world (scratch play client, test account; warm: the shared stack stays up): Esc opens the
    Moonglade pause menu, the world pauses with the banner and dimming, Esc again resumes (the scenario
    client.pause_menu, its own record)."""
    from ..session import run_children
    return run_children(ctx, [("client.pause_menu", pause_menu)])
