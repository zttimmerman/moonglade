"""SPIKE modes for harness/spike_m4.py (throwaway, spike/m4-dungeon only)."""
import math
import time

from spike_m4 import Run, facts, log, save_json
from hslib import companion as C, db
from hslib.probe import proto as W

PARTY = [("warrior", "prot pve"), ("priest", "holy pve"), ("mage", "frost pve"), ("rogue", "combat pve")]


def _enter(r, f):
    e = f["entrance"][0]
    r.ev["enter"] = r.go(e["x"], e["y"], e["z"], f["map"])
    log("enter event", r.ev["enter"]["event"])
    return r.ev["enter"]["event"] and r.ev["enter"]["event"]["kind"] == "new_world" and r.ev["enter"]["event"]["map"] == f["map"]


def _add(r, cls, spec):
    a = r.s.addclass(cls)
    name = a["member"]["name"]
    r.whisper(name, f"talents spec {spec}", wait=1.5)
    r.whisper(name, "autogear", wait=2.0)
    return name, a["bot"]


def _summon_all(r):
    for n in list(r.s.companions):
        r.s.summon(n)
    time.sleep(3)


def _boss(f, order=0):
    enc = [e for e in f["encounters"] if e["spawns"]][order]
    return enc, enc["spawns"][0]


def scaling(*_):
    """(d) mapstat + boss max health with 1, 3, 5 players."""
    f = facts()
    with Run(f["target_level"]) as r:
        r.ev["facts"] = f
        assert _enter(r, f), "could not enter"
        enc, sp = _boss(f)
        rows = []

        def sample(label):
            time.sleep(4)   # AutoBalance re-evaluates on the map update after a player change
            ms = r.mapstat()
            bc = r.creature(sp["spawn_id"])
            rows.append({"label": label, "mapstat": ms, "boss": bc})
            log(label, "players", ms["players"], "boss max", bc["max_health"])
        sample("1 player")
        for cls, spec in PARTY[:2]:
            _add(r, cls, spec)
        _summon_all(r)
        sample("3 players")
        for cls, spec in PARTY[2:]:
            _add(r, cls, spec)
        _summon_all(r)
        sample("5 players")
        r.cmd(".gm on")
        sample("5 players, probe gm on")
        r.cmd(".gm off")
        r.ev["samples"] = rows
        r.dump("scaling")


def stat(spawn_id="79168"):
    with Run(19) as r:
        f = facts()
        _enter(r, f)
        print(r.creature(int(spawn_id)))
        print(r.mapstat())


STAGE_BACK_YD = 30


def _mmap_end(r, guid):
    r.s.ws.set_selection(guid)
    time.sleep(0.3)
    out = r.cmd(".mmap path", settle=0.5)
    import re
    m = re.search(r"ActualEndPosition\s+\((-?[\d.]+), (-?[\d.]+), (-?[\d.]+)\)", "\n".join(out))
    res = re.search(r"Result: (\w+) - Length: (\d+) - Type: (\d+)", "\n".join(out))
    return ({"x": float(m.group(1)), "y": float(m.group(2)), "z": float(m.group(3))} if m else None,
            {"raw": out, "result": res.groups() if res else None})


def _stage(r, f, sp, prev):
    """design.md (companions) §4 rule, spike version: 30 yd back from the boss toward prev, snapped by `.mmap path`
    (the boss's path to the master's position; ActualEndPosition)."""
    r.s.ws.set_selection(0)
    dx, dy = prev["x"] - sp["x"], prev["y"] - sp["y"]
    d = math.hypot(dx, dy)
    p = {"x": sp["x"] + dx / d * STAGE_BACK_YD, "y": sp["y"] + dy / d * STAGE_BACK_YD, "z": sp["z"] + 2}
    r.go(p["x"], p["y"], p["z"])
    info = r.s.npc_info(sp["spawn_id"])
    end, ev = _mmap_end(r, info["guid"]) if info.get("guid") else (None, {"err": "no boss guid"})
    ev["line_point"] = p
    if end:
        ev["snapped"] = end
        ev["off_line_yd"] = round(math.hypot(end["x"] - p["x"], end["y"] - p["y"]), 1)
        r.go(end["x"], end["y"], end["z"] + 0.5)
    return end or p, ev


def _alive(name):
    import re
    from hslib import soap
    m = re.search(r"Alive \?: (\w+)", soap.gm(f"pinfo {name}").output)
    return m.group(1) if m else None


def _party_up(r):
    names = {}
    for cls, spec in PARTY:
        n, b = _add(r, cls, spec)
        names[cls] = (n, b)
    return names


def _gather(r, names, yd=15, timeout=40):
    """Whisper `summon` (playerbots SummonAction; the GM .summon refuses non-GM targets into an instance), then wait
    until AutoBalance counts everyone on the map and each companion's `.gps` is within yd (a `.gps` identical to the
    master's means the selection wasn't found, so it counts as far)."""
    for n, _b in names.values():
        r.whisper(n, "summon", wait=0.3)
    t0 = time.time()
    far = None
    while time.time() - t0 < timeout:
        ms = r.mapstat()
        me = r.s.gps()
        far = []
        for n, b in names.values():
            g = r.s.gps(b["guid"])
            if "x" not in g or "x" not in me or (g["x"], g["y"]) == (me["x"], me["y"]) or C.dist(me, g) > yd:
                far.append(n)
        if not far and ms["players"] == 1 + len(names):
            return {"ok": True, "s": round(time.time() - t0, 1), "players": ms["players"]}
        time.sleep(2)
    return {"ok": False, "far": far, "players": ms["players"]}


def _unit(r, sp, entry=None):
    """The boss by spawn id with no selection (npc info prefers the selection); entry checked when given."""
    import re
    r.s.ws.set_selection(0)
    time.sleep(0.25)
    i = r.s.npc_info(sp["spawn_id"])
    m = re.search(r"Entry: (\d+)", "\n".join(i.get("raw") or []))
    i["entry"] = int(m.group(1)) if m else None
    if entry and i["entry"] != entry:
        i["health"] = None
    return i


def _fight(r, names, sp, timeout=180, idle_abort=60, entry=None):
    """Pull: the master selects the boss and whispers the tank `attack`, then the party `attack` after 3 s."""
    info = _unit(r, sp, entry)
    g = info["guid"]
    r.s.ws.set_selection(g)
    mark = r.s.ws.mark()
    t0 = time.time()
    tank = names["warrior"][0]
    r.whisper(tank, "attack", wait=3.0)
    r.party("attack", wait=0.5)
    r.s.ws.set_selection(g)
    hp = []
    dead_at = None
    while time.time() - t0 < timeout:
        i = _unit(r, sp, entry)
        hp.append((round(time.time() - t0), i.get("health"), i.get("max_health")))
        if i.get("health") == 0 or i.get("guid") is None:
            dead_at = round(time.time() - t0, 1)
            break
        alive = [n for n, _ in names.values() if _alive(n) == "Yes"]
        if not alive:
            break
        if time.time() - t0 > idle_abort and i.get("health") == i.get("max_health"):
            break
        time.sleep(2)
    evs = r.s.ws.events_since(mark)
    deaths = sorted({hex(e["victim"]) for e in evs if e["kind"] == "attack_stop" and e["now_dead"]})
    dmg = {}
    for e in evs:
        if e["kind"] in ("melee_damage", "spell_damage") and e["victim"] == g:
            dmg[hex(e["attacker"])] = dmg.get(hex(e["attacker"]), 0) + e["damage"]
    taken = {}
    for e in evs:
        if e["kind"] in ("melee_damage", "spell_damage") and e["attacker"] == g:
            taken[hex(e["victim"])] = taken.get(hex(e["victim"]), 0) + 1
    status = {n: _alive(n) for n, _ in names.values()} | {"master": _alive(C.P.COMP_CHAR)}
    return {"dead_after_s": dead_at, "hp_trace": hp[::3], "boss_guid": hex(g), "damage_to_boss": dmg,
            "boss_hits_on": taken, "alive_after": status, "attack_stop_dead": deaths}


def _revive_all(r, names):
    from hslib import soap
    for n, _ in names.values():
        if _alive(n) != "Yes":
            soap.gm(f"revive {n}")
    if _alive(C.P.COMP_CHAR) != "Yes":
        r.cmd(".revive")


def boss1(order="0", tries="3", back="30"):
    global STAGE_BACK_YD
    STAGE_BACK_YD = float(back)
    """(b) enter with 4 companions and kill the first boss; record time, deaths, completedEncounters."""
    f = facts()
    with Run(f["target_level"]) as r:
        r.ev["facts"] = f
        assert _enter(r, f)
        names = _party_up(r)
        r.ev["party"] = {k: v[0] for k, v in names.items()}
        r.ev["gather_entrance"] = _gather(r, names)
        r.ev["mapstat_entrance"] = r.mapstat()
        enc, sp = _boss(f, int(order))
        stage, sev = _stage(r, f, sp, f["entrance"][0])
        r.ev["stage"] = sev
        r.ev["attempts"] = []
        for k in range(int(tries)):
            r.ev["attempts"].append({"gather": _gather(r, names)})
            r.ev["attempts"][-1]["boss_before"] = r.creature(sp["spawn_id"])
            res = _fight(r, names, sp)
            r.ev["attempts"][-1]["fight"] = res
            log("attempt", k + 1, res["dead_after_s"], res["alive_after"])
            if res["dead_after_s"] is not None:
                break
            _revive_all(r, names)
            r.go(stage["x"], stage["y"], stage["z"] + 0.5)
            time.sleep(15)
        time.sleep(3)
        r.ev["instance_rows"] = r.instance_row()
        r.ev["mapstat_end"] = r.mapstat()
        log("instance", r.ev["instance_rows"])
        r.dump(f"boss{order}")


def selfbot(order="0", gear="uncommon", timeout="180"):
    """(c) the probe alone as a selfbot: teleport onto the boss, let it fight; record movement, abilities, kill."""
    f = facts()
    with Run(f["target_level"]) as r:
        r.ev["facts"] = f
        assert _enter(r, f)
        r.ev["initself"] = r.cmd(f".playerbots bot initself={gear}", settle=1.0)
        r.ev["self_on"] = r.cmd(".playerbots bot self", settle=1.0)
        r.ev["selfbot"] = any("Enable player botAI" in x for x in r.ev["self_on"])
        r.ev["mapstat"] = r.mapstat()
        enc, sp = _boss(f, int(order))
        stage, sev = _stage(r, f, sp, f["entrance"][0])
        r.ev["stage"] = sev
        time.sleep(3)
        r.ev["boss_before"] = r.creature(sp["spawn_id"])
        g = r.ev["boss_before"]["guid"]
        me0 = r.s.gps()
        mark = r.s.ws.mark()
        t0 = time.time()
        q = r.ev["boss_before"]["pos"]
        # land 8 yd short of the boss on the line to the stage point, so the AI has to move to melee range
        dx, dy = stage["x"] - q["x"], stage["y"] - q["y"]
        d = math.hypot(dx, dy)
        r.ev["engage_go"] = r.go(q["x"] + dx / d * 8, q["y"] + dy / d * 8, q["z"] + 0.5)
        trace, dead_at = [], None
        while time.time() - t0 < float(timeout):
            i = _unit(r, sp, enc["credit_entry"])
            me = r.s.gps(r.s.guid)
            trace.append({"t": round(time.time() - t0), "boss_hp": i.get("health"), "max": i.get("max_health"),
                          "entry": i.get("entry"), "me": me.get("x") and
                          (round(me["x"], 1), round(me["y"], 1)), "dist": i.get("pos") and "x" in me and
                          round(C.dist(i["pos"], me), 1)})
            if i.get("health") == 0 or i.get("guid") is None:
                dead_at = round(time.time() - t0, 1)
                break
            if _alive(C.P.COMP_CHAR) != "Yes":
                break
            time.sleep(2)
        evs = r.s.ws.events_since(mark)
        me_g = r.s.guid
        r.ev["fight"] = {"dead_after_s": dead_at, "trace": trace,
                         "probe_melee": sum(1 for e in evs if e["kind"] == "melee_damage" and e["attacker"] == me_g),
                         "probe_spell_hits": sorted({e["spell"] for e in evs if e["kind"] == "spell_damage"
                                                     and e["attacker"] == me_g}),
                         "probe_damage": sum(e["damage"] for e in evs if e["kind"] in ("melee_damage", "spell_damage")
                                             and e["attacker"] == me_g),
                         "boss_hits_on_probe": sum(1 for e in evs if e["kind"] in ("melee_damage", "spell_damage")
                                                   and e["attacker"] == g and e["victim"] == me_g),
                         "probe_alive": _alive(C.P.COMP_CHAR),
                         "sent_by_probe": sorted({op for d_, op in r.s.ws.log[-400:] if d_ == "C>S"}),
                         "start_pos": me0}
        log("selfbot fight", {k: v for k, v in r.ev["fight"].items() if k != "trace"})
        log("trace", trace[:12])
        time.sleep(2)
        r.ev["instance_rows"] = r.instance_row()
        r.dump(f"selfbot{order}")


def _where(name):
    from hslib import soap
    import re
    out = soap.gm(f"pinfo {name}").output
    m = re.search(r"Map: ([^\n]*)", out)
    return m.group(1).strip() if m else out[-300:]


def dbg_summon(*_):
    f = facts()
    with Run(f["target_level"]) as r:
        assert _enter(r, f)
        n, b = _add(r, "warrior", "prot pve")
        log("where after add", _where(n))
        log("summon", r.s.summon(n))
        time.sleep(3)
        log("where after summon", _where(n))
        log("mapstat", r.mapstat()["players"])
        r.whisper(n, "summon", wait=3)
        log("where after whisper summon", _where(n))
        log("mapstat", r.mapstat()["players"])


def _leave(r):
    from hslib import fixtures as F
    t = F.tele_row(C.START_TELE)
    return r.go(t["x"], t["y"], t["z"], t["map"])


def _die(r, sp):
    """Kill a boss with encounter credit: `.die` at full health gives none (Die.Command.Mode = 1 -> Unit::Kill, and
    Unit.cpp:13554 rewards only if IsDamageEnoughForLootingAndReward), so `.damage <current health>` (player damage)
    is used. The probe is in GM mode while it walks between bosses (not attacked) and turns it off before re-entry."""
    r.s.ws.set_selection(0)
    r.go(sp["x"], sp["y"], sp["z"] + 1)
    time.sleep(1)
    i = r.s.npc_info(sp["spawn_id"])
    if not i.get("guid") or not i.get("health"):
        return {"skip": i}
    r.s.ws.set_selection(i["guid"])
    time.sleep(0.3)
    out = r.cmd(f".damage {i['health']}", settle=0.5)
    time.sleep(1)
    after = r.s.npc_info(sp["spawn_id"]).get("health")
    r.s.ws.set_selection(0)
    return {"damage": out, "before": i["health"], "after": after}


def _hp(r, sp):
    r.s.ws.set_selection(0)
    time.sleep(0.3)
    return r.s.npc_info(sp["spawn_id"]).get("health")


def reenter(*_):
    """(e) partial progress is kept; a full clear gives a fresh copy on re-entry (mod-moonglade prototype hook)."""
    f = facts()
    with Run(f["target_level"]) as r:
        st = []
        assert _enter(r, f)
        a = r.mapstat()
        enc, sp0 = _boss(f, 0)
        st.append({"step": "entered", "instance": a["instance"], "rows": r.instance_row()})
        r.cmd(".gm on")
        st.append({"step": "kill boss 1", "res": _die(r, sp0)})
        r.cmd(".gm off")
        time.sleep(2)
        st.append({"step": "after boss 1", "rows": r.instance_row()})
        _leave(r)
        assert _enter(r, f)
        b = r.mapstat()
        st.append({"step": "re-entered after partial", "instance": b["instance"], "rows": r.instance_row(),
                   "same_instance": b["instance"] == a["instance"],
                   "boss1_hp": _hp(r, sp0)})
        r.cmd(".gm on")
        for k, e in enumerate([e for e in f["encounters"] if e["spawns"]]):
            st.append({"step": f"kill {e['spawns'][0]['name']}", "res": _die(r, e["spawns"][0])})
        r.cmd(".gm off")
        time.sleep(2)
        st.append({"step": "after all", "rows": r.instance_row()})
        _leave(r)
        assert _enter(r, f)
        c = r.mapstat()
        st.append({"step": "re-entered after clear", "instance": c["instance"], "rows": r.instance_row(),
                   "fresh": c["instance"] != b["instance"],
                   "boss1_hp": _hp(r, sp0)})
        r.ev["steps"] = st
        for s_ in st:
            log(s_)
        r.dump("reenter")


def groupreset(*_):
    """Does dismissing the companions (group disbands) outside the dungeon drop a half-done dungeon? (stock
    Group::RemoveMember -> Player::ResetInstances(INSTANCE_RESET_GROUP_LEAVE), PlayerMisc.cpp:295)"""
    f = facts()
    with Run(f["target_level"]) as r:
        st = []
        assert _enter(r, f)
        a = r.mapstat()
        enc, sp0 = _boss(f, 0)
        r.cmd(".gm on")
        st.append({"step": "kill boss 1", "res": _die(r, sp0)})
        r.cmd(".gm off")
        time.sleep(1)
        n, b = _add(r, "warrior", "prot pve")
        st.append({"step": "boss1 + companion", "instance": a["instance"], "rows": r.instance_row()})
        _leave(r)
        st.append({"step": "remove companion outside", "res": r.s.remove(n), "group_rows": r.s.group_rows()})
        time.sleep(2)
        st.append({"step": "after remove", "rows": r.instance_row()})
        assert _enter(r, f)
        c = r.mapstat()
        st.append({"step": "re-entered", "instance": c["instance"], "kept": c["instance"] == a["instance"],
                   "rows": r.instance_row(), "boss1_hp": _hp(r, sp0)})
        r.ev["steps"] = st
        for s_ in st:
            log({k: v for k, v in s_.items() if k != "res"})
        r.dump("groupreset")


ROLE = {"warrior": "tank", "priest": "healer", "mage": "dps", "rogue": "dps"}


def clear(tries="3", back="12", selfmaster="1", gmstage="0"):
    """(6) scripted party clear through the final boss: per required encounter (static spawn), stage `back` yd from
    the boss toward the previous stage, summon, pull (tank `attack`, then party `attack`), revive on a wipe."""
    global STAGE_BACK_YD
    STAGE_BACK_YD = float(back)
    f = facts()
    with Run(f["target_level"]) as r:
        r.ev["facts"] = f
        assert _enter(r, f)
        names = _party_up(r)
        r.ev["party"] = {k: v[0] for k, v in names.items()}
        if selfmaster == "1":
            r.ev["self_on"] = r.cmd(".playerbots bot self", settle=1.0)
            r.ev["selfbot"] = any("Enable" in x for x in r.ev["self_on"])
        r.ev["gather_entrance"] = _gather(r, names)
        prev = f["entrance"][0]
        r.ev["encounters"] = []
        t_run = time.time()
        for enc in [e for e in f["encounters"] if e["spawns"]]:
            sp = enc["spawns"][0]
            rec = {"boss": sp["name"], "bit": enc["bit"], "attempts": []}
            r.ev["encounters"].append(rec)
            if gmstage == "1":
                r.cmd(".gm on")
            stage, sev = _stage(r, f, sp, prev)
            rec["stage"] = {k: v for k, v in sev.items() if k != "raw"}
            t_enc = time.time()
            if r.creature(sp["spawn_id"]).get("health") == 0:
                rec["already_dead"] = True
            for k in range(int(tries)):
                if rec.get("already_dead"):
                    break
                if gmstage == "1":
                    r.cmd(".gm on")      # not attacked while the party gathers next to the boss
                a = {"gather": _gather(r, names)}
                if gmstage == "1":
                    r.cmd(".gm off")
                a["boss_before"] = {k_: v for k_, v in r.creature(sp["spawn_id"]).items() if k_ != "creaturestat"}
                res = _fight(r, names, sp, entry=enc["credit_entry"])
                a["fight"] = res
                a["deaths_by_role"] = {ROLE[c]: (res["alive_after"].get(n) != "Yes")
                                       for c, (n, _) in names.items()}
                rec["attempts"].append(a)
                log(sp["name"], "attempt", k + 1, "dead after", res["dead_after_s"], res["alive_after"])
                if res["dead_after_s"] is not None:
                    break
                _revive_all(r, names)
                if r.mapstat()["players"] is None:
                    # the selfbot master released its spirit after the wipe (graveyard outside): walk back in
                    a["master_released"] = True
                    _enter(r, f)
                if gmstage == "1":
                    r.cmd(".gm on")
                r.go(stage["x"], stage["y"], stage["z"] + 0.5)
                time.sleep(20)          # let the boss evade and reset
            _revive_all(r, names)
            rec["duration_s"] = round(time.time() - t_enc, 1)
            rec["killed"] = bool(rec["attempts"] and rec["attempts"][-1]["fight"]["dead_after_s"] is not None) \
                or rec.get("already_dead", False)
            rec["mask_after"] = r.instance_row()
            prev = stage
            if not rec["killed"]:
                log("STOP: not killed", sp["name"])
                break
        r.ev["run_s"] = round(time.time() - t_run, 1)
        r.ev["instance_rows"] = r.instance_row()
        log("instance", r.ev["instance_rows"], "run", r.ev["run_s"])
        r.dump("clear")
