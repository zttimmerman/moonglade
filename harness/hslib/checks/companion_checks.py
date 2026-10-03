"""companion-bots (m2-living-world): add/remove, follow, assist, shared quest.

Each check logs in the companion harness character (P.COMP_CHAR on the GM-level P.COMP_USER account, never PLAYER or
HSTEST) through the probe and types the playerbots chat commands a player would type. Driver and data rules:
hslib/companion.py. Each check takes well under 2 minutes, so they are `full` tier (design.md "Companion checks")."""
import time

from .. import companion as C, soap
from ..probe import proto as W
from ..registry import check, require

ADD_REMOVE_S = 30      # spec: within 30 s (both directions)
FOLLOW_MIN_MOVE_YD = 100
FOLLOW_NEAR_YD = 15
FOLLOW_S = 60
ASSIST_ENGAGE_S = 20
ASSIST_AGGRO_S = 15    # setup: the creature attacks the master after the teleport (aggressive creature, aggro radius)
ASSIST_DEATH_S = 60    # the spec gives no time for the kill; 60 s after the master entered combat (measured: ~10 s)
QUEST_S = 30


def _run(fn):
    """Run a companion step; turn CompanionError / ProbeError into a CheckFail with its evidence."""
    try:
        return fn()
    except C.CompanionError as e:
        require(False, str(e), **e.evidence)
    except C.probe.ProbeError as e:
        require(False, f"probe: {e}", **e.info)


def _session_ev(s: C.Session) -> dict:
    return {k: s.ev.get(k) for k in ("account", "character", "prepare", "login", "gm_off", "stale_group")}


@check("comp.add_remove", order=130, capability="companion-bots", stack="up", tier="full")
def add_remove(ctx):
    """`.playerbots bot addclass priest` → within 30 s SMSG_GROUP_LIST shows the test character's party with an online
    member whose characters.class is Priest (an addclass-pool bot); `.playerbots bot remove <name>` → within 30 s it
    is no longer a member of the party."""
    ev = {}
    s = C.Session()
    _run(s.__enter__)
    try:
        ev["session"] = _session_ev(s)
        add = _run(lambda: s.addclass("priest", timeout=ADD_REMOVE_S))
        bot = add["bot"]
        ev["add"] = {k: v for k, v in add.items() if k != "group_list"}
        ev["group_after_add"] = add["group_list"]
        ev["db_group_after_add"] = s.group_rows()
        require(add["elapsed_s"] <= ADD_REMOVE_S, "priest joined later than 30 s", **ev)
        require(bot["class"] == C.CLASS_IDS["priest"] and bot["online"] == 1, "party member is not an online priest", **ev)
        require(bot["account_type"] == C.ADDCLASS_ACCOUNT_TYPE, "the priest is not from the addclass bot pool", **ev)
        rem = _run(lambda: s.remove(bot["name"], timeout=ADD_REMOVE_S))
        ev["remove"] = rem
        ev["bot_after_remove"] = C.bot_info(bot["guid"])
        ev["db_group_after_remove"] = s.group_rows()
        require(rem["left_party_after_s"] is not None,
                f"{bot['name']} is still a party member {ADD_REMOVE_S} s after `.playerbots bot remove` "
                f"(SMSG_GROUP_LIST still lists it; online={[m['online'] for m in s.members() if m['guid'] == bot['guid']]})",
                **ev)
    finally:
        s.__exit__(None, None, None)
        ev["session_end"] = {k: s.ev.get(k) for k in ("left_group_at_exit", "logout", "cleanup_errors")}
    return ev


@check("comp.follows", order=131, capability="companion-bots", stack="up", tier="full")
def follows(ctx):
    """With a companion beside it, the test character is GM-teleported (`.tele name`) >= 100 yd within its zone →
    within 60 s the companion is within 15 yd of it (live positions from `.gps`)."""
    a, b = _run(C.pick_follow_points)
    ev = {"points": {"from": a, "to": b, "rule": C.FOLLOW_RULE}}
    s = C.Session(tele=a["name"])
    _run(s.__enter__)
    try:
        ev["session"] = _session_ev(s)
        add = _run(lambda: s.addclass("priest"))
        bot = add["bot"]
        ev["add"] = {k: v for k, v in add.items() if k != "group_list"}
        pre = _run(lambda: s.wait_near(bot["guid"], FOLLOW_NEAR_YD, 30))
        ev["before_move"] = pre["samples"][-1]
        require(pre["ok"], "precondition: companion never came within 15 yd before the move", **ev)
        start = s.gps()
        since = s.ws.mark()
        ev["tele"] = soap.gm(f"tele name {C.P.COMP_CHAR} {b['name']}").evidence()
        ack = s.ws.wait_event(lambda e: e["kind"] in ("teleport_near", "new_world"), since, timeout=15)
        t0 = time.time()
        ev["teleport_event"] = ack
        require(ev["tele"]["ok"] and ack is not None, "master teleport not received/acked", **ev)
        moved = s.gps()
        ev["master_before"], ev["master_after"] = start, moved
        ev["moved_yd"] = round(C.dist(start, moved), 1) if "x" in moved else None
        require(ev["moved_yd"] is not None and ev["moved_yd"] >= FOLLOW_MIN_MOVE_YD, "master moved < 100 yd", **ev)
        require(moved.get("zone") == start.get("zone") and moved.get("map") == start.get("map"),
                "master left its zone (scenario needs a move within the same zone)", **ev)
        res = s.wait_near(bot["guid"], FOLLOW_NEAR_YD, FOLLOW_S - (time.time() - t0))
        ev["follow"] = {"ok": res["ok"], "elapsed_s": round(time.time() - t0, 1),
                        "distances_yd": [x["distance_yd"] for x in res["samples"]],
                        "last_sample": res["samples"][-1]}
        require(res["ok"], f"companion not within {FOLLOW_NEAR_YD} yd of the master {FOLLOW_S} s after the move", **ev)
        require(time.time() - t0 <= FOLLOW_S + 2, "follow took longer than 60 s", **ev)
    finally:
        s.__exit__(None, None, None)
        ev["session_end"] = {k: s.ev.get(k) for k in ("left_group_at_exit", "logout", "cleanup_errors")}
    return ev


def _by(guid, victim):
    return lambda e: (e["kind"] in ("attack_start", "melee_damage", "spell_damage")
                      and e["attacker"] == guid and e["victim"] == victim)


@check("comp.assists", order=132, capability="companion-bots", stack="up", tier="full")
def assists(ctx):
    """Master with a warrior companion enters combat with a hostile, level-appropriate creature (SQL/DBC rule; the
    master is GM-teleported onto it and it attacks) → within
    20 s the companion attacks that creature (SMSG_ATTACKSTART / combat log, attacker = companion) and the creature
    dies (`.npc info` health 0) while the master sends no attack or spell and deals no damage."""
    ev = {}
    s = C.Session()
    _run(s.__enter__)
    try:
        ev["session"] = _session_ev(s)
        add = _run(lambda: s.addclass("warrior"))
        bot = add["bot"]
        ev["add"] = {k: v for k, v in add.items() if k != "group_list"}
        cr = _run(s.pick_live_creature)
        ev["creature_picked"] = cr
        rule_entries = {c["entry"] for c in C.creature_candidates(n=500)}
        since = s.ws.mark()
        p = cr["npc_info"]["pos"] or cr["spawn"]
        ev["go"] = s.ws.command(f".go xyz {p['x']:.2f} {p['y']:.2f} {p['z']:.2f}", settle=0.5, timeout=5)
        tp = s.ws.wait_event(lambda e: e["kind"] == "teleport_near", since, timeout=5)
        ev["teleport_event"] = tp
        require(tp is not None, "master teleport not received/acked", **ev)
        s.ws.heartbeat(tp["x"], tp["y"], tp["z"])   # report our position once landed, as a client does
        # "the master enters combat with that creature": the first creature that attacks the master after the
        # teleport (usually the picked one; a neighbour of the same rule can aggro first). It must match the rule.
        # seen as SMSG_ATTACKSTART or a combat-log hit on the master (right after a teleport the ATTACKSTART can
        # precede the creature becoming visible to us, so only the hit arrives)
        aggro = s.ws.wait_event(lambda e: e["kind"] in ("attack_start", "melee_damage", "spell_damage")
                                and e["victim"] == s.guid and W.guid_high(e["attacker"]) == W.HIGHGUID_UNIT,
                                since, timeout=ASSIST_AGGRO_S)
        ev["master_enters_combat"] = aggro
        if aggro is None:
            ev["combat_events"] = [e for e in s.ws.events_since(since) if e["kind"] in
                                   ("attack_start", "attack_stop", "melee_damage", "spell_damage")][:40]
        require(aggro is not None, f"precondition: no creature attacked the master within {ASSIST_AGGRO_S} s", **ev)
        cg = aggro["attacker"]
        require(W.guid_entry(cg) in rule_entries, "precondition: the creature fighting the master doesn't match the "
                "rule (hostile, normal, level-appropriate)", rule_entries=sorted(rule_entries), **ev)
        t0 = aggro["t"]
        s.ws.set_selection(cg)              # the master targets it; from here on it does nothing
        ev["creature"] = s.npc_info()       # the selected creature: spawn id, GUID, health
        require(ev["creature"]["guid"] == cg, "selected creature's GUID mismatch", **ev)
        trail_mark = len(s.ws.log)
        ev["summon"] = s.summon(bot["name"])
        eng = s.ws.wait_event(_by(bot["guid"], cg), since, timeout=max(0.0, t0 + ASSIST_ENGAGE_S - time.time()))
        ev["companion_engages"] = eng
        ev["companion_engage_after_s"] = round(eng["t"] - t0, 2) if eng else None
        require(eng is not None and eng["t"] - t0 <= ASSIST_ENGAGE_S,
                f"companion not in combat with the creature within {ASSIST_ENGAGE_S} s of the master", **ev)
        dead, info = None, None
        while time.time() < t0 + ASSIST_DEATH_S:
            info = s.npc_info()             # selection = the creature
            if info["health"] == 0:
                dead = round(time.time() - t0, 1)
                break
            time.sleep(1.0)
        ev["creature_dead_after_s"], ev["npc_info_last"] = dead, info
        evs = s.ws.events_since(since)
        ev["creature_attack_stop_dead"] = [e for e in evs if e["kind"] == "attack_stop" and e["attacker"] == cg
                                           and e["now_dead"]]
        ev["damage_to_creature"] = {}
        for e in evs:
            if e["kind"] in ("melee_damage", "spell_damage") and e["victim"] == cg:
                k = "companion" if e["attacker"] == bot["guid"] else ("master" if e["attacker"] == s.guid
                                                                       else hex(e["attacker"]))
                ev["damage_to_creature"][k] = ev["damage_to_creature"].get(k, 0) + e["damage"]
        sent = sorted({op for d, op in s.ws.log[trail_mark:] if d == "C>S"})
        ev["master_sent_after_combat"] = sent
        require(dead is not None, f"creature not dead {ASSIST_DEATH_S} s after the master entered combat", **ev)
        require(ev["damage_to_creature"].get("master", 0) == 0, "the master damaged the creature", **ev)
        allowed = {"CMSG_MESSAGECHAT", "CMSG_KEEP_ALIVE", "CMSG_TIME_SYNC_RESP", "MSG_MOVE_TELEPORT_ACK"}   # chat = .summon / .npc info
        require(set(sent) <= allowed, "the master sent game actions after entering combat", **ev)
    finally:
        s.__exit__(None, None, None)
        ev["session_end"] = {k: s.ev.get(k) for k in ("left_group_at_exit", "logout", "cleanup_errors")}
    return ev


def _quest_status(qid: int, name: str) -> str:
    out = soap.gm(f"quest status {int(qid)} {name}").output
    # LANG_CMD_QUEST_STATUS 'Quest: <title> (<id>)' / 'Status: <QuestStatusToString>' (cs_quest.cpp), then reasons
    return out.split("Status:")[-1].strip().splitlines()[0].strip() if "Status:" in out else out.strip()


@check("comp.quest_shared", order=133, capability="companion-bots", stack="up", tier="full")
def quest_shared(ctx):
    """Master has a starter-zone kill quest (SQL rule), companion of a level that can take it, master shares it
    (CMSG_PUSHQUESTTOPARTY) → within 30 s `.quest status` shows it in the companion's log (Incomplete/Complete)."""
    q = _run(C.pick_quest)
    ev = {"quest": q}
    s = C.Session()
    _run(s.__enter__)
    try:
        ev["session"] = _session_ev(s)
        add = _run(lambda: s.addclass("priest"))
        bot = add["bot"]
        ev["add"] = {k: v for k, v in add.items() if k != "group_list"}
        require(bot["level"] >= q["min_level"], "precondition: companion level below the quest's MinLevel", **ev)
        # clean slate for both, then the master gets the quest (server command)
        ev["remove_bot"] = soap.gm(f"quest remove {q['id']} {bot['name']}").evidence()
        ev["remove_master"] = soap.gm(f"quest remove {q['id']} {C.P.COMP_CHAR}").evidence()
        ev["add_master"] = soap.gm(f"quest add {q['id']} {C.P.COMP_CHAR}").evidence()
        ev["master_status"] = _quest_status(q["id"], C.P.COMP_CHAR)
        ev["companion_status_before"] = _quest_status(q["id"], bot["name"])
        require(ev["master_status"] == "Incomplete", "precondition: master doesn't have the quest", **ev)
        require(ev["companion_status_before"] == "Not Taken", "precondition: companion already has the quest", **ev)
        since = s.ws.mark()
        t0 = time.time()
        s.ws.push_quest_to_party(q["id"])
        status, polls = None, []
        while time.time() - t0 <= QUEST_S:
            status = _quest_status(q["id"], bot["name"])
            polls.append({"t": round(time.time() - t0, 1), "status": status})
            if status in ("Incomplete", "Complete"):
                break
            time.sleep(1.0)
        ev["companion_status_polls"] = polls
        ev["push_results"] = [e for e in s.ws.events_since(since) if e["kind"] == "quest_push_result"]
        ev["companion_whispers"] = [e["msg"] for e in s.ws.events_since(since)
                                    if e["kind"] == "chat" and e["sender"] == bot["guid"]]
        require(status in ("Incomplete", "Complete") and polls[-1]["t"] <= QUEST_S,
                f"shared quest not in the companion's log within {QUEST_S} s (last status: {status})", **ev)
    finally:
        s.__exit__(None, None, None)
        ev["session_end"] = {k: s.ev.get(k) for k in ("left_group_at_exit", "logout", "cleanup_errors")}
    return ev
