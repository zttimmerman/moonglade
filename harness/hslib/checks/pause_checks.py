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

from .. import config, pause as Z, soap, stack
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

@check("pause.combat_frozen", order=440, capability=CAP, stack="up", tier="full")
def combat_frozen(ctx):
    """GIVEN the companion character in melee with a creature (it attacks the character; the character auto-attacks
    it), WHEN the probe sends the pause add-on message and waits 20 s, THEN the character's and the creature's health
    (`.moonglade pause unit`) are the same at the start and the end of the pause, no SMSG_ATTACKERSTATEUPDATE arrives
    between the server's "paused" answer and the resume request, and after the resume a melee swing arrives within
    5 s."""
    ev = {}
    _status_running(ev)
    s = _session()
    try:
        fight = _run(s.engage)
        ev["fight"] = fight
        cg = fight["guid"]
        i_given = s.ws.mark()
        first = s.ws.wait_event(lambda e: e["kind"] == "melee_damage" and {e["attacker"], e["victim"]} == {cg, s.guid},
                                0, timeout=10)
        ev["swing_before_pause"] = first
        require(first is not None, "GIVEN not met: no melee swing between the character and the creature", **ev)
        p = _run(s.pause)
        ev["pause"] = p
        start = {"me": _run(lambda: s.unit(s.guid)), "creature": _run(lambda: s.unit(cg))}
        Z.wait_until(p["reply_t"] + PAUSE_S)
        end = {"me": _run(lambda: s.unit(s.guid)), "creature": _run(lambda: s.unit(cg))}
        ev["at_pause_start"], ev["at_pause_end"] = start, end
        ev["status_paused"] = Z.status()
        r = _run(s.resume)
        ev["resume"] = r
        swing = s.ws.wait_event(lambda e: e["kind"] == "melee_damage" and {e["attacker"], e["victim"]} == {cg, s.guid}
                                and e["t"] >= r["sent_t"], 0, timeout=SWING_AFTER_S + 1)
        ev["first_swing_after_resume"] = swing
        during = s.swings(p["reply_i"], r["sent_i"])
        ev["swings_during_pause"] = during[:10]
        ev["swings_since_given"] = len(s.swings(i_given - 1))
        ev["paused_s"] = round(r["sent_t"] - p["reply_t"], 2)
        require(ev["status_paused"]["state"] == "paused", "the server didn't report paused during the pause", **ev)
        require(start["me"]["health"] == end["me"]["health"], "the character's health changed during the pause", **ev)
        require(start["creature"]["health"] == end["creature"]["health"], "the creature's health changed during the pause",
                **ev)
        require(not during, f"{len(during)} SMSG_ATTACKERSTATEUPDATE arrived during the pause", **ev)
        require(ev["paused_s"] >= PAUSE_S, "the pause was shorter than 20 s", **ev)
        require(swing is not None and swing["t"] - r["sent_t"] <= SWING_AFTER_S,
                "no melee swing within 5 s of the resume", **ev)
    finally:
        _close(s, ev)
    return ev


@check("pause.aura_frozen", order=441, capability=CAP, stack="up", tier="full")
def aura_frozen(ctx):
    """GIVEN a buff with >= 30 s left on the companion character and a DoT ticking on a creature (spells by
    pause.SPELL_RULES, applied with `.aura`), WHEN paused 20 s and resumed, THEN the buff's lost time (pause.lost_ms
    between a read-out just before the pause and one just after the resume, `.list auras id`) is under 1 s, no
    SMSG_PERIODICAURALOG for the DoT arrives during the pause, and after the resume the client gets an
    SMSG_AURA_UPDATE for the buff whose remaining duration is within 1 s of the server's."""
    ev = {"spells": {k: v for k, v in _run(Z.pick_spells).items() if k in ("buff", "dot")}}
    buff, dot = ev["spells"]["buff"]["id"], ev["spells"]["dot"]["id"]
    _status_running(ev)
    s = _session()
    try:
        fight = _run(lambda: s.engage(swing=False))
        ev["fight"] = fight
        cg = fight["guid"]
        s.ws.set_selection(s.guid)
        ev["aura_buff"] = s.ws.command(f".aura {buff}", settle=0.3, timeout=4)
        s.ws.set_selection(cg)
        ev["aura_dot"] = s.ws.command(f".aura {dot}", settle=0.3, timeout=4)
        t_dot = time.time()
        tick = None
        while tick is None and time.time() - t_dot < 8:
            tick = next(iter(s.tap.between(t_dot - 1, kind="periodic", spell=dot, victim=cg)), None)
            time.sleep(0.2)
        ev["dot_tick_before_pause"] = tick
        require(tick is not None, "GIVEN not met: the DoT never ticked on the creature", **ev)
        b0 = _run(lambda: s.aura(s.guid, buff))
        ev["buff_before"] = b0
        require((b0["duration"] or 0) >= 30000, "GIVEN not met: the buff has less than 30 s left", **ev)
        st0 = Z.status()
        p = _run(s.pause)
        ev["pause"] = p
        bp = _run(lambda: s.aura(s.guid, buff))
        Z.wait_until(p["reply_t"] + PAUSE_S)
        bp2 = _run(lambda: s.aura(s.guid, buff))
        ev["buff_during"] = [bp, bp2]
        r = _run(s.resume)
        ev["resume"] = r
        b1 = _run(lambda: s.aura(s.guid, buff))
        st1 = Z.status()
        ev["buff_after"], ev["status"] = b1, [st0, st1]
        # the paused time as the probe saw it (the server's "paused" to its "running" answer), not the server's own
        # paused total (kept as evidence): a server whose clock didn't stop could also miscount it
        paused_ms = round((r["reply_t"] - p["reply_t"]) * 1000)
        ev["paused_ms"], ev["server_paused_total_delta_ms"] = paused_ms, st1["paused_total_ms"] - st0["paused_total_ms"]
        ev["buff_lost_ms"] = Z.lost_ms(b0["duration"], b0["t"], b1["duration"], b1["t"], paused_ms)
        ticks = s.tap.after(p["reply_i"], r["sent_i"], kind="periodic", spell=dot)
        ev["dot_ticks_during_pause"] = ticks[:5]
        time.sleep(4)
        ev["dot_ticks_after_resume"] = len(s.tap.between(r["sent_t"], kind="periodic", spell=dot))
        upd = s.tap.between(r["sent_t"], kind="aura_update", guid=s.guid, spell=buff)
        ev["client_buff_updates_after_resume"] = upd[:3]
        require(paused_ms >= PAUSE_S * 1000, "the pause was shorter than 20 s", **ev)
        require(abs(ev["buff_lost_ms"]) < FROZEN_MS, f"the buff lost {ev['buff_lost_ms']} ms during the pause", **ev)
        require(not ticks, f"{len(ticks)} DoT ticks during the pause", **ev)
        u = upd[0] if upd else None
        require(u is not None and u["t"] - r["sent_t"] <= 3 and "duration" in u,
                "the client got no SMSG_AURA_UPDATE with the buff's duration after the resume", **ev)
        server_at_update = b1["duration"] + (b1["t"] - u["t"]) * 1000
        ev["client_vs_server_ms"] = round(u["duration"] - server_at_update)
        require(abs(ev["client_vs_server_ms"]) < FROZEN_MS,
                "the buff duration sent to the client differs from the server's by 1 s or more", **ev)
    finally:
        _close(s, ev)
    return ev


@check("pause.cooldown_frozen", order=442, capability=CAP, stack="up", tier="full")
def cooldown_frozen(ctx):
    """GIVEN a player spell on a >= 30 s cooldown (`.cooldown` then `.cast`, spell by pause.SPELL_RULES player_cd)
    and a creature ability on cooldown (creature_cd: `.cast back`, then `.unaura` starts the creature's cooldown),
    WHEN paused 20 s and resumed, THEN both remaining cooldowns on the server (`.moonglade pause cooldown`) lost under
    1 s (pause.lost_ms), and after the resume the client gets a cooldown correction for the player spell
    (SMSG_MODIFY_COOLDOWN or SMSG_SPELL_COOLDOWN naming it)."""
    ev = {"spells": {k: v for k, v in _run(Z.pick_spells).items() if k in ("player_cd", "creature_cd")}}
    pcd, ccd = ev["spells"]["player_cd"]["id"], ev["spells"]["creature_cd"]["id"]
    _status_running(ev)
    s = _session()
    try:
        s.ws.set_selection(s.guid)
        cr = _run(s.pick_live_creature)
        cg = cr["guid"]
        p0 = cr["npc_info"]["pos"]
        ev["creature"] = {k: cr[k] for k in ("spawn_id", "name", "guid")}
        # stand 25 yd from it (within `.cast back` range) without pulling it: the staging point rule's direction
        here = s.gps()
        stage = _run(lambda: Z.kite_point(p0, here, here.get("map", 0), dist=25))
        ev["stand_at"] = stage
        s.ws.command(f".go xyz {stage['x']:.2f} {stage['y']:.2f}", settle=0.4, timeout=4)
        s.ws.set_selection(s.guid)
        ev["clear_cooldowns"] = s.ws.command(".cooldown", settle=0.3, timeout=4)
        ev["cast_player"] = s.ws.command(f".cast {pcd}", settle=0.4, timeout=4)
        s.ws.set_selection(cg)
        ev["cast_back"] = s.ws.command(f".cast back {ccd}", settle=0.4, timeout=4)
        ev["unaura"] = s.ws.command(f".unaura {ccd}", settle=0.4, timeout=4)
        c0 = {"player": _run(lambda: s.cooldown(s.guid, pcd)), "creature": _run(lambda: s.cooldown(cg, ccd))}
        ev["cooldowns_before"] = c0
        require(c0["player"]["ms"] >= 30000 - 5000 and c0["creature"]["ms"] > 0,
                "GIVEN not met: the player spell or the creature ability isn't on cooldown", **ev)
        st0 = Z.status()
        p = _run(s.pause)
        ev["pause"] = p
        Z.wait_until(p["reply_t"] + PAUSE_S)
        r = _run(s.resume)
        ev["resume"] = r
        c1 = {"player": _run(lambda: s.cooldown(s.guid, pcd)), "creature": _run(lambda: s.cooldown(cg, ccd))}
        st1 = Z.status()
        ev["cooldowns_after"], ev["status"] = c1, [st0, st1]
        # the paused time as the probe saw it (the server's "paused" to its "running" answer), not the server's own
        # paused total (kept as evidence): a server whose clock didn't stop could also miscount it
        paused_ms = round((r["reply_t"] - p["reply_t"]) * 1000)
        ev["paused_ms"], ev["server_paused_total_delta_ms"] = paused_ms, st1["paused_total_ms"] - st0["paused_total_ms"]
        ev["lost_ms"] = {k: Z.lost_ms(c0[k]["ms"], c0[k]["t"], c1[k]["ms"], c1[k]["t"], paused_ms) for k in c0}
        corr = ([x for x in s.tap.between(r["sent_t"], kind="modify_cooldown") if x["spell"] == pcd and x["guid"] == s.guid]
                + [x for x in s.tap.between(r["sent_t"], kind="spell_cooldown")
                   if x["guid"] == s.guid and any(c["spell"] == pcd for c in x["cooldowns"])])
        ev["client_corrections"] = corr[:3]
        require(paused_ms >= PAUSE_S * 1000, "the pause was shorter than 20 s", **ev)
        for k, v in ev["lost_ms"].items():
            require(abs(v) < FROZEN_MS, f"the {k} cooldown lost {v} ms during the pause", **ev)
        require(corr and corr[0]["t"] - r["sent_t"] <= 3,
                "the client got no cooldown correction for the player spell after the resume", **ev)
    finally:
        _close(s, ev)
    return ev


@check("pause.cast_cancelled", order=443, capability=CAP, stack="up", tier="full")
def cast_cancelled(ctx):
    """GIVEN the companion character casting a >= 2 s cast-time spell (pause.SPELL_RULES cast, `.cast`; its
    SMSG_SPELL_START seen), WHEN the pause begins mid-cast, THEN the client gets the interrupted notice
    (SMSG_SPELL_FAILURE / SMSG_SPELL_FAILED_OTHER with SPELL_FAILED_INTERRUPTED, or SMSG_CAST_FAILED) for it, no
    SMSG_SPELL_GO for it arrives during the pause or within 5 s after the resume and its aura isn't on the character,
    the character gets SMSG_FORCE_MOVE_ROOT with the pause and SMSG_FORCE_MOVE_UNROOT only after the resume request."""
    ev = {"spell": _run(Z.pick_spells)["cast"]}
    sp = ev["spell"]["id"]
    _status_running(ev)
    s = _session()
    try:
        s.ws.command(".combatstop", settle=0.3, timeout=3)
        s.ws.set_selection(s.guid)
        t0 = time.time()
        s.ws.chat(f".cast {sp}")
        start = None
        while start is None and time.time() - t0 < 5:
            start = next(iter(s.tap.between(t0, kind="spell_start", spell=sp, caster=s.guid)), None)
            time.sleep(0.05)
        ev["spell_start"] = start
        require(start is not None, "GIVEN not met: the cast never started (no SMSG_SPELL_START)", **ev)
        p = _run(s.pause)
        ev["pause"] = p
        ev["pause_after_cast_start_s"] = round(p["sent_t"] - start["t"], 3)
        require(p["sent_t"] - start["t"] < ev["spell"]["cast_ms"] / 1000,
                "GIVEN not met: the pause request went out after the cast would have finished", **ev)
        Z.wait_until(p["reply_t"] + PAUSE_S)
        unrooted_early = s.tap.after(p["reply_i"], s.ws.mark(), kind="unroot", guid=s.guid)
        r = _run(s.resume)
        ev["resume"] = r
        time.sleep(5)
        ev["aura_after"] = _run(lambda: s.aura(s.guid, sp))
        fails = [x for x in s.tap.between(start["t"]) if x.get("spell") == sp
                 and ((x["kind"] in ("spell_failure", "spell_failed_other") and x["result"] == Z.SPELL_FAILED_INTERRUPTED)
                      or x["kind"] == "cast_failed")]
        gos = s.tap.between(start["t"], kind="spell_go", spell=sp, caster=s.guid)
        roots = s.tap.between(p["sent_t"], p["reply_t"] + 1, kind="root", guid=s.guid)
        unroots = s.tap.between(r["sent_t"], r["reply_t"] + 1, kind="unroot", guid=s.guid)
        ev.update({"interrupted": fails[:3], "spell_go": gos[:3], "root": roots[:2], "unroot_during_pause": unrooted_early,
                   "unroot_after_resume": unroots[:2]})
        require(fails and fails[0]["t"] <= p["reply_t"] + 0.5, "the client got no interrupted notice for the cast", **ev)
        require(not gos, "the cast's SMSG_SPELL_GO arrived (the spell went off)", **ev)
        require(ev["aura_after"]["duration"] is None, "the cast's aura is on the character", **ev)
        require(roots, "the character wasn't rooted when the pause began", **ev)
        require(not unrooted_early and unroots, "the character wasn't kept rooted until the resume", **ev)
    finally:
        _close(s, ev)
    return ev


@check("pause.respawn_frozen", order=444, capability=CAP, stack="up", tier="full")
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
    ev = {"rule": Z.RESPAWN_RULE}
    _status_running(ev)
    s = _session()
    try:
        start = s.gps()
        cands = _run(lambda: Z.respawn_candidates())
        victim = None
        for c in cands:
            s.ws.set_selection(s.guid)
            s.ws.command(f".go xyz {c['spawn']['x']:.2f} {c['spawn']['y']:.2f} {c['spawn']['z']:.2f}", settle=0.4, timeout=4)
            info = s.npc_info(c["spawn_id"])
            if info.get("health") and info["guid"]:
                victim = c | {"guid": info["guid"]}
                break
        ev["killed"] = victim
        require(victim is not None, "GIVEN not met: none of the rule's creatures is alive", candidates=cands, **ev)
        s.ws.set_selection(victim["guid"])
        ev["die"] = s.ws.command(".die", settle=0.3, timeout=4)
        t_kill = time.time()
        k0 = _run(lambda: Z.respawn_remaining(s, victim["spawn_id"]))
        ev["after_kill"] = k0
        require(k0["health"] == 0 and k0["remain_s"], "GIVEN not met: the creature isn't dead with a respawn time", **ev)
        R = k0["remain_s"]
        ev["R_s"] = R
        # (1) the respawn
        st0 = Z.status()
        p = _run(s.pause)
        ev["respawn_pause"] = p
        Z.wait_until(p["reply_t"] + R + 5)
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
        Z.wait_until(p2["reply_t"] + R + 5)
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
        _close(s, ev)
    return ev


@check("pause.bots_frozen", order=445, capability=CAP, stack="up", tier="full")
def bots_frozen(ctx):
    """GIVEN a playerbot companion in the character's party that is moving (`.playerbots bot addclass priest`; the
    character moves 30 yd and the bot's position changes between two read-outs), WHEN the world is paused for 20 s,
    THEN the bot's position, health, mana and current spells (`.moonglade pause unit`) are the same at the start and
    the end of the pause, and after the resume it acts again (its position changes within 15 s)."""
    ev = {}
    _status_running(ev)
    s = _session()
    try:
        ev["combatstop"] = s.ws.command(".combatstop", settle=0.3, timeout=3)
        add = _run(lambda: s.addclass("priest"))
        bot = add["bot"]
        ev["bot"] = {k: bot.get(k) for k in ("guid", "name", "class")}
        near = s.wait_near(bot["guid"], 10, 30)
        ev["near_before"] = {"ok": near["ok"], "elapsed_s": near["elapsed_s"]}
        me = s.gps()
        s.ws.command(f".go xyz {me['x'] + 30:.2f} {me['y']:.2f}", settle=0.2, timeout=3)
        # GIVEN: the bot moving (two read-outs 0.3 s apart at different positions) or casting, right before the pause
        b0, b1, moving, samples = _run(lambda: s.unit(bot["guid"])), None, 0.0, []
        t_move = time.time()
        while time.time() - t_move < 6:
            time.sleep(0.3)
            b1 = _run(lambda: s.unit(bot["guid"]))
            moving = Z.math.dist((b0["pos"]["x"], b0["pos"]["y"]), (b1["pos"]["x"], b1["pos"]["y"]))
            samples.append(round(moving, 2))
            if moving > 0.2 or b1["spell"] or b1["channel"]:
                break
            b0 = b1
        ev["bot_before"], ev["moved_per_0_3s_yd"] = [b0, b1], samples
        require(moving > 0.2 or b1["spell"] or b1["channel"], "GIVEN not met: the bot was neither moving nor casting", **ev)
        p = _run(s.pause)
        ev["pause"] = p
        u0 = _run(lambda: s.unit(bot["guid"]))
        Z.wait_until(p["reply_t"] + PAUSE_S)
        u1 = _run(lambda: s.unit(bot["guid"]))
        ev["during"] = [u0, u1]
        r = _run(s.resume)
        ev["resume"] = r
        acted = None
        while time.time() - r["reply_t"] < BOT_ACTS_S:
            u = _run(lambda: s.unit(bot["guid"]))
            if u["pos"] != u1["pos"] or u["spell"] != u1["spell"] or u["channel"] != u1["channel"]:
                acted = u
                break
            time.sleep(0.5)
        ev["acted_after_resume"] = acted
        same = {k: u0[k] == u1[k] for k in ("pos", "health", "power", "spell", "channel")}
        ev["unchanged_during_pause"] = same
        require(all(same.values()), f"the bot changed during the pause: {[k for k, v in same.items() if not v]}", **ev)
        require(acted is not None, "the bot didn't act again within 15 s of the resume", **ev)
    finally:
        _close(s, ev)
    return ev


@check("pause.session_alive", order=446, capability=CAP, stack="up", tier="slow")
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

@check("pause.refused_on_taxi", order=447, capability=CAP, stack="up", tier="full")
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


@check("pause.logout_unpauses", order=448, capability=CAP, stack="up", tier="full")
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


@check("pause.disabled_noop", order=449, capability=CAP, stack="up", tier="full")
def disabled_noop(ctx):
    """GIVEN Moonglade.Pause.Enable = 0 (the stack restarted with the pause-off config profile; `.moonglade pause
    status` says enabled 0), WHEN the pause add-on message arrives during a fight, THEN nothing pauses: the server
    answers "disabled", reports running with an unchanged paused total, and melee swings keep arriving (one within
    5 s after the answer). The stack is put back on the profile it ran before."""
    ev = {}
    was = stack.running_profile()
    ev["profile_before"] = was
    ev["to_off"] = stack.ensure_profile("pause-off")
    try:
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
    finally:
        back = (was or {}).get("profile") or config.DEFAULT_PROFILE
        ev["restore"] = stack.ensure_profile(back if back != "pause-off" else config.DEFAULT_PROFILE)
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


@check("client.pause_menu", order=450, capability=CAP, stack="down", tier="full")
def pause_menu(ctx):
    """GIVEN the play client in the world (a scratch patched play client, ./play --account test, the harness test
    account; the add-on's pause test mode planted in its SavedVariables), WHEN a hardware Esc (SendInput in the
    client's Wine prefix) opens the game menu and a second one closes it, THEN `.moonglade pause status` reports
    paused while the menu is open, the add-on's screenshot taken then shows the "Paused" banner (its report: banner and
    menu shown; the screenshot's banner box holds the banner's gold text, the shot after the resume doesn't) and the
    world view dimmed (DIM_REGION's mean brightness at most DIM_MAX_RATIO of the add-on's pre-Esc shot), and after the
    menu closes the server reports running and DIM_REGION's brightness is back within RESTORE_TOL of the pre-Esc
    shot."""
    from . import clientinput
    from .play_checks import is_clear, remaining, require_given_clear
    from .playclient_util import PlayRun, build_scratch, drop_scratch, fresh_scratch, screenshots
    from .seamless_checks import addon_report, addon_sv_path, server_logins
    from .. import paths as P

    ev: dict = {"account": P.TEST_USER, "glue_test": MENU_GLUE}
    scratch = run = None
    try:
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
        run = PlayRun("pause", glue_test=MENU_GLUE, scratch=scratch)
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
    require(is_clear(ev["remaining_after"]), "something is still running", **ev)
    return ev
