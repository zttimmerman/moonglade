"""dungeon-companions, slice 1 (openspec/changes/m4-party-preset): dng.party_preset and its child scenario
dng.party_preset_tank_player, in one dungeon-probe login (budget 90 s).

The probe (dungeon.py's own account and character, P.DUNGEON_USER / P.DUNGEON_CHAR; never PLAYER) sends exactly the
commands /mg party sends, read from the add-on's MG_PARTY_PLAN (hslib/partyplan.py), as chat lines the way the client
sends them (`.playerbots bot addclass` typed in chat, then whispers to the companion that joined). Then it reads what
the server made of them:
- strategies: each companion answers the whisper `co ?` with "Strategies: a, b, ..." (Engine::ChangeStrategy '?',
  Engine::ListStrategies, vendor/mod-playerbots/src/Bot/Engine/Engine.cpp:515-529, :663-667);
- the premade spec: `talents spec <name>` answers "Picking <name>" or "Spec <name> not found"
  (ChangeTalentsAction::SpecPick, ChangeTalentsAction.cpp:134-159), and the name must be a
  AiPlayerbot.PremadeSpecName of the class in the rendered playerbots.conf;
- level and gear: characters.level and character_inventory (bag 0, equipment slots), after `saveall` (cs_misc.cpp
  HandleSaveAllCommand) writes every online character.
GIVEN, the probe's own role: its talents are reset (`reset talents`, cs_reset.cpp) and one talent point is put in the
tree the role needs, with the client's CMSG_LEARN_TALENT (SkillHandler.cpp:25-32); /mg party reads the role from the
tree holding the most points (MoongladeParty.lua MoongladeParty_Role), so one point decides it. The talent is chosen by
rule from DBC, never typed in: TalentTab.dbc rows with the class in ClassMask [20] and tabpage [22] = the tree
(protection: WARRIOR_TAB_PROTECTION = 2, arms: WARRIOR_TAB_ARMS = 0; PlayerbotAI.h tab enums), then the Talent.dbc row
in that tab [1] with Row [2] = 0 and the lowest Col [3] (DBCStructure.h:1958-1986); rank 1 is RankID[0] [4]. It is
confirmed from character_talent after `saveall`. At the end the probe's talents are reset again."""
import math
import struct
import time

from .. import companion as C, config, db, dbc, dungeon as D, fixtures as F, partyplan as PP, paths as P, soap, stack
from .. import population as pop
from ..probe import proto as W
from ..registry import CheckFail, CheckSkip, check, require

CAP = "dungeon-companions"
COVERS = (
    "client-mods/addons/Moonglade/MoongladeParty.lua",
    "harness/hslib/partyplan.py",
    "harness/hslib/checks/party_checks.py",
    "harness/hslib/dungeon.py",
)
CHILD = "dng.party_preset_tank_player"
CMSG_LEARN_TALENT = 0x251          # Opcodes.h:623
CLASS_WARRIOR = 1
TAB_ARMS, TAB_PROTECTION = 0, 2    # PlayerbotAI.h WARRIOR_TABS (the talent frame's order, TalentTab.dbc tabpage)
JOIN_S = 20                        # per companion: addclass answers in 1-5 s when a character is free (comp.add_remove)
REPLY_S = 10                       # a whisper's answer
ASK_EVERY_S = 2.5                  # `co ?` again if unanswered (see strategies)
STRATEGIES_S = 25                  # every companion's `co ?` answer (see strategies)
LEVEL_SLACK = 3                    # spec: within 3 levels
# armour slots (Player.h EQUIPMENT_SLOT_*): every equipment slot whose item is ITEM_CLASS_ARMOR (cloth to plate, the
# cloak, neck, rings, trinkets), i.e. all of 0-18 but the spec's exceptions, shirt (BODY 3) and tabard (18), and the
# weapon slots (main hand 15, off hand 16: empty with a two-hander; ranged 17). autogear skips trinkets below level 50,
# neck and head below 30 and rings below 20 (PlayerbotFactory.cpp:2361-2371), so the check runs at Hellfire Ramparts'
# target level (LFGDungeons.dbc, 63), where every armour slot is in play.
ARMOUR_SLOTS = {0: "head", 1: "neck", 2: "shoulders", 4: "chest", 5: "waist", 6: "legs", 7: "feet", 8: "wrists",
                9: "hands", 10: "finger1", 11: "finger2", 12: "trinket1", 13: "trinket2", 14: "back"}
AUTOGEAR_ALL_SLOTS_LEVEL = 50
DPS_STRATEGIES = ("dps assist", "threat", "wait for attack")
HEAL_STRATEGIES = ("heal", "holy heal")


# ----------------------------------------------------------------------------- the probe's role (GIVEN)

def tree_talent(class_id: int, tabpage: int) -> dict:
    """The rule's talent: first column of the first row of the class's tree `tabpage` (module doc)."""
    tabs, _ = dbc.read_dbc("TalentTab.dbc")
    tab = next((t for t in tabs if t[20] & (1 << (class_id - 1)) and t[22] == tabpage), None)
    if not tab:
        raise C.CompanionError(f"no TalentTab.dbc row for class {class_id} tree {tabpage}")
    talents, _ = dbc.read_dbc("Talent.dbc")
    row0 = sorted((t for t in talents if t[1] == tab[0] and t[2] == 0), key=lambda t: t[3])
    if not row0:
        raise C.CompanionError(f"no first-row talent in TalentTab {tab[0]}")
    t = row0[0]
    return {"talent_id": t[0], "tab_id": tab[0], "tabpage": tabpage, "row": t[2], "col": t[3], "rank1_spell": t[4],
            "rule": "TalentTab.dbc ClassMask[20] & class bit, tabpage[22]; Talent.dbc tab[1], Row[2]=0, lowest Col[3]; "
                    "RankID[0][4]"}


def saved_talents(guid: int) -> list[int]:
    return [int(r[0]) for r in db.query(f"SELECT spell FROM character_talent WHERE guid={int(guid)}", "characters")]


def set_role(s, tabpage: int) -> dict:
    """Reset the probe's talents, learn the rule's talent in tree `tabpage`, and confirm it saved."""
    t = tree_talent(CLASS_WARRIOR, tabpage)
    ev = {"talent": t, "reset": soap.gm(f"reset talents {P.DUNGEON_CHAR}").evidence()}
    time.sleep(0.5)
    s.ws.send(CMSG_LEARN_TALENT, struct.pack("<II", t["talent_id"], 0))
    time.sleep(0.8)
    ev["saveall"] = soap.gm("saveall").evidence()
    got, ok, dt = F.poll(lambda: saved_talents(s.guid), lambda v: v == [t["rank1_spell"]], timeout=10)
    ev |= {"character_talent": got, "confirmed_s": dt}
    require(ok, f"GIVEN: the probe's talents are {got}, not just the {['arms', '?', 'protection'][tabpage]} talent "
            f"{t['rank1_spell']}", **ev)
    return ev


# ----------------------------------------------------------------------------- sending the plan

def _whispers_from(s, guid: int, since: int) -> list[str]:
    return [e["msg"] for e in s.ws.events_since(since) if e["kind"] == "chat" and e["type"] == W.CHAT_MSG_WHISPER
            and e.get("sender") == guid]


def add_companion(s, comp: dict) -> dict:
    """Send one companion's plan lines: the addclass command, wait for a new online party member of the class
    (SMSG_GROUP_LIST, as the add-on waits for PARTY_MEMBERS_CHANGED), then its whispers in order."""
    want = PP.CLASS_IDS[comp["class"]]
    before = {m["guid"] for m in s.members()}
    since = s.ws.mark()
    t0 = time.time()
    s.ws.chat(comp["addclass"])
    found = None
    while time.time() - t0 < JOIN_S and not found:
        e = s.ws.wait_event(lambda e: e["kind"] == "group_list", since, timeout=max(0.1, JOIN_S - (time.time() - t0)))
        if not e:
            break
        since = e["i"] + 1
        for m in e["members"]:
            if m["guid"] not in before and m["online"]:
                info = C.bot_info(m["guid"])
                if info.get("class") == want:
                    found = (m, info)
    sys_lines = [e["msg"] for e in s.ws.events_since(0) if e["kind"] == "chat" and e["type"] == W.CHAT_MSG_SYSTEM][-3:]
    ev = {"companion": comp["companion"], "command": comp["addclass"], "join_s": round(time.time() - t0, 2)}
    if not found:
        raise C.CompanionError(f"no online {comp['class']} joined the party within {JOIN_S} s", **ev,
                               system_lines=sys_lines, members=s.members())
    m, info = found
    s.companions[m["name"]] = info
    ev |= {"name": m["name"], "guid": m["guid"], "steps": []}
    # the whisper steps, as the add-on sends them: a step's whispers together, the next step after the step's reply
    for st in comp["steps"]:
        mark = s.ws.mark()
        for w in st["send"]:
            s.ws.chat(w, W.CHAT_MSG_WHISPER, to=m["name"])
        rec = {"send": st["send"]}
        ev["steps"].append(rec)
        if st["reply"]:
            e = s.ws.wait_event(lambda e: e["kind"] == "chat" and e["type"] == W.CHAT_MSG_WHISPER
                                and e.get("sender") == m["guid"] and st["reply"] in e["msg"], mark, timeout=REPLY_S)
            rec |= {"reply": e["msg"] if e else None, "answers": _whispers_from(s, m["guid"], mark)[-3:]}
            require(e is not None, f"{m['name']} ({comp['companion']}) didn't answer {st['send']} with "
                    f"{st['reply']!r} within {REPLY_S} s (the add-on stops here too)", **ev)
    return ev


def strategies(s, comps: list[dict]) -> dict:
    """Whisper `co ?` to every companion and parse each "Strategies: a, b, c" -> {guid: {"strategies", "asked"}}.
    Unanswered companions are asked again every ASK_EVERY_S, up to STRATEGIES_S: a companion handles the commands that
    arrived since its last update together, one event per command word (PlayerbotAI::HandleCommands,
    ExternalEventHelper), so a `co ?` landing in the same update as the plan's `co +...` can be dropped, and a
    companion in a long cast answers late (measured: the hunter, up to ~10 s after its last step; it revives its pet)."""
    since = since0 = s.ws.mark()
    out = {c["guid"]: {"strategies": None, "asked": 0} for c in comps}
    t0, last_ask = time.time(), 0.0
    while time.time() - t0 < STRATEGIES_S:
        evs = s.ws.events_since(since)
        since += len(evs)
        for e in evs:
            if e["kind"] == "chat" and e["type"] == W.CHAT_MSG_WHISPER and e.get("sender") in out \
                    and e["msg"].startswith("Strategies:"):
                body = e["msg"][len("Strategies:"):]
                out[e["sender"]]["strategies"] = sorted(x.strip() for x in body.split(",") if x.strip())
                out[e["sender"]]["answered_s"] = round(time.time() - t0, 1)
        # the latest answer counts; a companion is settled once it shows a role (its last plan step has landed)
        waiting = [c for c in comps if role_of(out[c["guid"]]["strategies"]) in (None, "other")]
        if not waiting:
            break
        if time.time() - last_ask >= ASK_EVERY_S:
            for c in waiting:
                s.ws.chat("co ?", W.CHAT_MSG_WHISPER, to=c["name"])
                out[c["guid"]]["asked"] += 1
            last_ask = time.time()
        time.sleep(0.2)
    for c in comps:
        if out[c["guid"]]["strategies"] is None:
            out[c["guid"]]["raw"] = _whispers_from(s, c["guid"], since0)[-3:]
    return out


def gear_and_level(guids: list[int]) -> dict:
    """characters.level and the filled equipment slots (character_inventory bag 0, slot < 19) per companion."""
    out = {}
    for g in guids:
        lv = db.scalar(f"SELECT level FROM characters WHERE guid={int(g)}", "characters")
        slots = sorted(int(r[0]) for r in db.query(
            f"SELECT slot FROM character_inventory WHERE guid={int(g)} AND bag=0 AND slot < 19", "characters"))
        out[g] = {"level": int(lv) if lv else None, "slots": slots,
                  "armour_missing": [n for k, n in ARMOUR_SLOTS.items() if k not in slots]}
    return out


def role_of(strats: list[str] | None) -> str | None:
    if not strats:
        return None
    if "tank" in strats:
        return "tank"
    if any(h in strats for h in HEAL_STRATEGIES):
        return "healer"
    if all(d in strats for d in DPS_STRATEGIES):
        return "damage"
    return "other"


def call_party(s, plan: dict, role: str, conf_text: str) -> dict:
    """Send the role's plan, then read every companion's spec answer, strategies, level and gear."""
    t0 = time.time()
    cmds = PP.commands(plan, role)
    ev = {"role": role, "plan": cmds, "companions": []}
    for comp in cmds:
        ev["companions"].append(add_companion(s, comp))
    for comp, c in zip(cmds, ev["companions"]):
        spec = PP.spec_of(comp["whispers"])
        c |= {"spec": spec, "spec_in_conf": spec in PP.premade_specs(conf_text, comp["class"])}
    st = strategies(s, ev["companions"])
    for c in ev["companions"]:
        c |= st[c["guid"]]
        c["role"] = role_of(c.get("strategies"))
    ev["saveall"] = soap.gm("saveall").evidence()
    time.sleep(1.0)
    gl = gear_and_level([c["guid"] for c in ev["companions"]])
    probe_level = db.scalar(f"SELECT level FROM characters WHERE guid={int(s.guid)}", "characters")
    ev["probe_level"] = int(probe_level) if probe_level else None
    for c in ev["companions"]:
        c |= gl[c["guid"]]
    ev["online"] = [m["name"] for m in s.members() if m["online"]]
    ev["elapsed_s"] = round(time.time() - t0, 1)
    return ev


def judge(ev: dict, n_tank: int, n_heal: int, n_dps: int) -> None:
    comps = ev["companions"]
    names = [c["name"] for c in comps]
    require(len(comps) == 4 and set(names) <= set(ev["online"]),
            f"the party lists {len(set(names) & set(ev['online']))} of 4 companions online", **ev)
    bad_spec = [(c["name"], c["spec"]) for c in comps if not c["spec_in_conf"]]
    require(not bad_spec, f"plan specs that aren't a PremadeSpecName of the class in playerbots.conf: {bad_spec}", **ev)
    roles = [c["role"] for c in comps]
    require(roles.count("tank") == n_tank and roles.count("healer") == n_heal and roles.count("damage") == n_dps,
            f"roles {roles} (from `co ?`), want {n_tank} tank, {n_heal} healer, {n_dps} damage dealers "
            f"({'+'.join(DPS_STRATEGIES)})", **ev)
    lv = ev["probe_level"]
    far = [(c["name"], c["level"]) for c in comps if c["level"] is None or lv is None or abs(c["level"] - lv) > LEVEL_SLACK]
    require(not far, f"companions not within {LEVEL_SLACK} levels of the probe's {lv}: {far}", **ev)
    bare = [(c["name"], c["armour_missing"]) for c in comps if c["armour_missing"]]
    require(not bare, f"armour slots empty (character_inventory): {bare}", **ev)


def clear_party(s, plan: dict) -> dict:
    """The plan's clear command (/mg party clear), then wait until no companion is online in the party."""
    t0 = time.time()
    reply = s.cmd(plan["clear"], settle=0.5)
    _, ok, dt = F.poll(lambda: [m for m in s.members() if m["online"]], lambda v: not v, timeout=20)
    ev = {"command": plan["clear"], "reply": reply, "all_gone": ok, "s": dt}
    if ok:
        s.companions.clear()
    ev["left_group"] = s.leave_stale_group()
    ev["elapsed_s"] = round(time.time() - t0, 1)
    return ev


@check("dng.party_preset", order=530, capability=CAP, stack="up", layer="integration", budget_s=90,
       children=(CHILD,))
def party_preset(ctx):
    """One dungeon-probe login at Hellfire Ramparts' target level, outside any dungeon. (dng.party_preset) The probe in a
    damage-dealing spec (one arms talent) sends the damage-dealer command list parsed from MoongladeParty.lua: within
    90 s the party lists 4 online companions, one with `tank`, one with `heal`/`holy heal`, two with `dps assist`,
    `threat` and `wait for attack` (`co ?`), each within 3 levels of the probe with every armour slot filled
    (character_inventory), and each picked its plan spec, a PremadeSpecName of its class in the rendered
    playerbots.conf. Then the plan's clear command empties the party, and (dng.party_preset_tank_player) the probe in a
    protection spec sends the tank list: no `tank`, one healer, three damage dealers, same level and gear rules."""
    from .dungeon_checks import _scenario
    plan = PP.plan()
    conf_text = (P.LANE_ETC / "modules" / "playerbots.conf").read_text()
    f = D.facts(D.RAMPARTS)
    ev: dict = {"plan_file": "client-mods/addons/Moonglade/" + PP.PARTY_LUA.name, "dungeon": f["lfg_id"], "level": f["target_level"]}
    require(f["target_level"] >= AUTOGEAR_ALL_SLOTS_LEVEL, f"GIVEN: target level {f['target_level']} is below "
            f"{AUTOGEAR_ALL_SLOTS_LEVEL}, where autogear fills every armour slot", **ev)
    res: dict = {}
    t0 = time.time()
    try:
        with D.Session(f["target_level"]) as s:
            ev["session"] = {k: s.ev.get(k) for k in ("login", "stale_group", "revive")}
            ev["login_s"] = round(time.time() - t0, 1)

            def damage():
                e = {"given": set_role(s, TAB_ARMS)}
                t = time.time()
                e |= call_party(s, plan, "damage", conf_text)
                e["within_s"] = round(time.time() - t, 1)
                ev["damage"] = e
                judge(e, 1, 1, 2)
                require(e["within_s"] <= 90, f"the party took {e['within_s']} s (spec: within 90 s)", **e)
                return e

            _scenario(res, "dng.party_preset", damage)
            ev["clear"] = clear_party(s, plan)

            def tank():
                require(ev["clear"]["all_gone"], "GIVEN: the first party didn't clear", clear=ev["clear"])
                e = {"given": set_role(s, TAB_PROTECTION)}
                t = time.time()
                e |= call_party(s, plan, "tank", conf_text)
                e["within_s"] = round(time.time() - t, 1)
                judge(e, 0, 1, 3)
                return e

            _scenario(res, CHILD, tank)
            ev["clear_end"] = clear_party(s, plan)
            ev["talents_reset_end"] = soap.gm(f"reset talents {P.DUNGEON_CHAR}").evidence()
    except C.CompanionError as e:
        raise CheckFail(str(e), {**ev, **e.evidence, "children": res})
    except C.probe.ProbeError as e:
        raise CheckFail(f"probe: {e}", {**ev, **e.info, "children": res})
    ev["session_s"] = round(time.time() - t0, 1)
    ev["children"] = {CHILD: res.get(CHILD) or {"status": "error", "message": "not run", "evidence": {}}}
    main = res.get("dng.party_preset") or {}
    if main.get("status") != "pass":
        raise CheckFail(main.get("message") or "dng.party_preset didn't run", {**ev, "result": main})
    ev["result"] = main
    return ev


# ----------------------------------------------------------------------------- m4-instance-tactics

TACTICS_DUNGEONS = (D.UTGARDE_KEEP, D.RAMPARTS, D.DEADMINES)      # the scenario's order (LFGDungeons.dbc ids)
TACTICS_SPEC_NAMES = {D.UTGARDE_KEEP: "wotlk-uk", D.RAMPARTS: "tbc-ramp", D.DEADMINES: None}   # the spec's wording
TACTICS_COVERS = COVERS + ("harness/hslib/tactics.py", "harness/hslib/checks/dungeon_checks.py")
GATHER_S = 20                      # the four companions summoned into the probe's copy (dungeon.Session.gather)


def _added_lines(s, guids: set, since: int) -> list[dict]:
    """"Added <name> instance strategy" whispers (ApplyInstanceStrategies with tellMaster, after a far teleport)."""
    return [{"sender": e.get("sender"), "msg": e["msg"]} for e in s.ws.events_since(since)
            if e["kind"] == "chat" and e["type"] == W.CHAT_MSG_WHISPER and e.get("sender") in guids
            and e["msg"].startswith("Added ") and e["msg"].endswith(" instance strategy")]


@check("dng.instance_strategy", order=531, capability=CAP, stack="up", layer="integration", budget_s=90,
       covers=TACTICS_COVERS)
def instance_strategy(ctx):
    """(m4-instance-tactics) One dungeon-probe login at Utgarde Keep's target level: the probe calls the /mg party
    damage-dealer party (the add-on's plan, hslib/partyplan.py), then enters Utgarde Keep, Hellfire Ramparts and The
    Deadmines in that order (each a fresh copy: the probe's binds of the three maps are cleared first), summons the
    four companions into each and reads every companion's `co ?` there. Expected names: parsed from
    PlayerbotAI::ApplyInstanceStrategies at the pin (hslib/tactics.py), keyed by LFGDungeons.dbc MapID; inside each
    dungeon every companion lists that dungeon's strategy and none of the other two dungeons'."""
    from .. import tactics as T
    from .dungeon_checks import _enter
    src_path = P.BOTS_SRC / T.SOURCE
    ev: dict = {"source": f"vendor/mod-playerbots/{T.SOURCE}"}
    try:
        tab = T.instance_table(src_path.read_text())
    except (OSError, T.TacticsError) as e:
        raise CheckFail(f"GIVEN: can't read the instance strategy table: {e}", ev)
    fs = [D.facts(x) for x in TACTICS_DUNGEONS]
    maps = [f["map"] for f in fs]
    expect = T.expectations(tab["table"], maps)
    ev |= {"func_line": tab["func_line"], "dungeons": [
        {"lfg_id": f["lfg_id"], "map": f["map"], "target_level": f["target_level"], "strategy": tab["table"].get(f["map"]),
         "source_line": tab["lines"].get(f["map"])} for f in fs], "expect": expect,
        "map_rule": D.FACT_RULES["levels_and_map"]}
    got = {x["lfg_id"]: x["strategy"] for x in ev["dungeons"]}
    require(got == TACTICS_SPEC_NAMES, f"GIVEN: the pinned table gives {got} for the scenario's dungeons, the spec "
            f"names {TACTICS_SPEC_NAMES}", **ev)
    level = max(f["target_level"] for f in fs)
    ev["level"] = level
    plan = PP.plan()
    t0 = time.time()
    try:
        with D.Session(level) as s:
            ev["session"] = {k: s.ev.get(k) for k in ("login", "stale_group", "revive")}
            ev["unbind"] = {m: s.unbind(m) for m in maps}
            require(all(u["ok"] for u in ev["unbind"].values()), "GIVEN: the probe's old binds didn't go away", **ev)
            ev["login_s"] = round(time.time() - t0, 1)
            ev["party"] = [add_companion(s, c) for c in PP.commands(plan, "damage")]
            comps = ev["party"]
            guids = {c["guid"] for c in comps}
            ev["party_s"] = round(time.time() - t0, 1)
            reads, ev["visits"] = [], []
            for f, e in zip(fs, expect):
                v: dict = {"lfg_id": f["lfg_id"], "map": f["map"], "want": e["want"]}
                ev["visits"].append(v)
                mark = s.ws.mark()
                _enter(s, f, v, "enter")
                v["gather"] = g = s.gather(1 + len(comps), timeout=GATHER_S)
                require(g["ok"], f"GIVEN: the companions didn't all arrive in map {f['map']}", **ev)
                st = strategies(s, comps)
                v["co"] = {c["name"]: st[c["guid"]] for c in comps}
                v["added_whispers"] = _added_lines(s, guids, mark)
                reads.append({c["name"]: st[c["guid"]]["strategies"] for c in comps})
                v["at_s"] = round(time.time() - t0, 1)
            ev["problems"] = T.judge(expect, reads)
            ev["leave"] = s.leave()
            ev["clear"] = clear_party(s, plan)
    except C.CompanionError as e:
        raise CheckFail(str(e), {**ev, **e.evidence})
    except C.probe.ProbeError as e:
        raise CheckFail(f"probe: {e}", {**ev, **e.info})
    ev["session_s"] = round(time.time() - t0, 1)
    require(not ev["problems"], "; ".join(ev["problems"]), **ev)
    return ev


# ----------------------------------------------------------------------------- m4-dungeon-companions 1.1 (prototype)

RUN_COVERS = COVERS
RUN_DIR = P.OUT / "m4-full-run"


def _run_log(name: str):
    """Progress lines to stderr and to harness/out/m4-full-run/<name>.log (not committed): a run lasts minutes."""
    import json
    import sys
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    path = RUN_DIR / f"{name}.log"

    def say(x):
        line = f"{time.strftime('%H:%M:%S')} {json.dumps(x, default=str)}"
        print(line, file=sys.stderr, flush=True)
        with path.open("a") as fh:
            fh.write(line + "\n")
    return say


def _keep(name: str, ev: dict) -> None:
    import json
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_DIR / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(ev, indent=1, default=str))


ONLY_ENV = "HS_DNG_PARTY_ONLY"     # builder runs: one listed dungeon's LFG id (or several, comma-separated)
INITSELF_OK = "initself ok"        # PlayerbotMgr.cpp HandlePlayerbotCommand "initself": messages.push_back("initself ok")


def party_clear_dungeons(env: dict) -> tuple[tuple, bool]:
    """(the LFG ids dng.party_clear runs, partial). Every listed dungeon (dungeon.LISTED, in order) unless ONLY_ENV
    names some of them; it's honoured only inside a harness run (HS_HARNESS_RUN=1, which `hs run` sets), and a
    narrowed run is partial: the check then never passes (it skips with the dungeon's evidence, or fails). An id that
    isn't a listed dungeon is refused."""
    raw = (env.get(ONLY_ENV) or "").strip()
    if not raw:
        return D.LISTED, False
    if env.get("HS_HARNESS_RUN") != "1":
        raise CheckFail(f"${ONLY_ENV} is a builder switch for `hs run` only; unset it")
    try:
        ids = tuple(int(x) for x in raw.split(",") if x.strip())
    except ValueError:
        raise CheckFail(f"${ONLY_ENV}={raw!r}: expected LFGDungeons.dbc ids of listed dungeons {D.LISTED}")
    bad = [i for i in ids if i not in D.LISTED]
    if bad or not ids:
        raise CheckFail(f"${ONLY_ENV}={raw!r}: {bad or 'nothing'} not among the listed dungeons {D.LISTED}")
    return tuple(i for i in D.LISTED if i in ids), True


def initself_ok(reply: list[str]) -> bool:
    return any(INITSELF_OK in x for x in reply or [])


def party_dungeon(lfg: int, plan: dict, say, ev: dict, keep: str, during=None) -> dict:
    """One listed dungeon's party run (dng.party_clear, dng.run_budget): its facts, nav pass, a session at its target
    level with `initself` gear, the /mg party damage-dealer party, dungeon.run_party with the probe as the fifth
    member (selfbot), then leave and clear the party. The dungeon's evidence is appended to ev["dungeons"] and
    returned. `during(d)`, if given, is a context manager entered just around run_party (dng.run_budget's sampler).
    A failure keeps the evidence under `keep` and raises CheckFail."""
    import contextlib
    f = D.facts(lfg)
    req, skipped = D.required_encounters(f)
    d: dict = {"dungeon": f["lfg_id"], "map": f["map"], "level": f["target_level"],
               "required": [(e["encounter"], e["spawns"][0]["name"]) for e in req], "skipped": skipped}
    ev["dungeons"].append(d)
    try:
        d["nav"] = D.nav_stages(f, req, say)
        say({"stages": [(x["boss"], x["verdict"], x["point"], (x.get("gather") or {}).get("back_yd"))
                        for x in d["nav"]["stages"]]})
        with D.Session(f["target_level"]) as s:
            d["session"] = {k: s.ev.get(k) for k in ("login", "stale_group", "revive")}
            q = D.gear_quality()
            d["initself"] = {"quality": q, "reply": s.cmd(f".playerbots bot initself={q}", settle=1.5)}
            if not initself_ok(d["initself"]["reply"]):
                raise C.CompanionError(f"initself: no '{INITSELF_OK}' answer", initself=d["initself"])
            d["party"] = [add_companion(s, c) for c in PP.commands(plan, "damage")]
            say({"party": [(c["companion"], c["name"]) for c in d["party"]]})
            d["run"] = {}
            try:
                with (during(d) if during else contextlib.nullcontext()):
                    D.run_party(s, f, d["party"], d["nav"], selfbot=True, log=say, out=d["run"])
            finally:
                d["leave"] = s.leave()
                d["clear"] = clear_party(s, plan)
    except C.CompanionError as e:
        _keep(keep, {**ev, **e.evidence})
        raise CheckFail(str(e), {**ev, **e.evidence})
    except C.probe.ProbeError as e:
        _keep(keep, {**ev, **e.info})
        raise CheckFail(f"probe: {e}", {**ev, **e.info})
    except BaseException:
        _keep(keep, ev)
        raise
    return d


@check("dng.party_clear", order=532, capability=CAP, stack="up", layer="gate", profile="scaling-on",
       covers=RUN_COVERS)
def party_clear(ctx):
    """openspec/changes/m4-dungeon-companions specs/dungeon-runs "Party clears each listed dungeon": The Deadmines,
    Razorfen Downs, Hellfire Ramparts and Utgarde Keep (normal), one after another (dungeon.LISTED). GIVEN the dungeon probe at the dungeon's target level (LFGDungeons.dbc) with `initself` gear, a
    /mg party damage-dealer party (the add-on's plan, hslib/partyplan.py) and a fresh copy entered with GM mode off
    (AutoBalance counts 5 players). WHEN dungeon.run_party takes them to each required encounter in
    DungeonEncounter.dbc order (design.md §4: staging by `.mmap path` from the level-3 nav account; gather farther back
    on the path, walk in together, GM mode off, wait until everyone is within 15 yd and out of combat, then the pull),
    with the probe fighting as the fifth member through `.playerbots bot self`. THEN within 45 minutes every required
    encounter's bit is set in the probe's saved completed-encounter mask, with at most 2 wipes per encounter; the
    evidence holds per encounter the time, wipes, deaths by role and the boss's scaled health. Every dungeon fact
    (map, target level, entrance, encounters and their order) is read by dungeon.facts' rules (LFGDungeons.dbc,
    DungeonEncounter.dbc, world DB); the instance tactics (wotlk-uk for 574, tbc-ramp for 543) are playerbots' own
    ApplyInstanceStrategies on entry. The 45-minute clock is run_party's: from the unbind before entry to the last
    encounter (the nav pass, the login and the party's setup are outside it). $HS_DNG_PARTY_ONLY narrows a builder
    run to some dungeons (party_clear_dungeons); such a run never passes."""
    import os
    say = _run_log("party")
    lfgs, partial = party_clear_dungeons(os.environ)
    plan = PP.plan()
    ev: dict = {"dungeons": [], "listed": list(D.LISTED), "ran": list(lfgs), "partial": partial}
    t0 = time.time()
    for lfg in lfgs:
        d = party_dungeon(lfg, plan, say, ev, "party")
        d["problems"] = party_clear_problems(d)
        say({"table": d["run"].get("table"), "total_s": d["run"].get("total_s"), "cleared": d["run"].get("cleared"),
             "problems": d["problems"]})
    ev["session_s"] = round(time.time() - t0, 1)
    _keep("party", ev)
    problems = [f"{d['dungeon']}: {p}" for d in ev["dungeons"] for p in d["problems"]]
    require(not problems, "; ".join(problems), **ev)
    if partial:
        raise CheckSkip(f"partial run (${ONLY_ENV}): only {list(lfgs)} of {list(D.LISTED)}, all cleared; "
                        f"not a gate pass", ev)
    return ev


CLEAR_LIMIT_S = 45 * 60            # spec: within 45 minutes per dungeon


def party_clear_problems(d: dict) -> list[str]:
    """The THEN of "Party clears each listed dungeon" for one dungeon's evidence: every required encounter killed
    (its bit set, read live from the probe's bind), at most MAX_WIPES wipes each, within CLEAR_LIMIT_S, and every
    encounter row carries its scaled health. Missing evidence is a problem, never a pass."""
    run = d.get("run") or {}
    out = []
    if not run.get("cleared"):
        out.append(f"not cleared: {run.get('table') or run.get('why')}")
    if run.get("mode") != "party":
        out.append(f"the probe didn't fight as a party member (mode {run.get('mode')})")
    total = run.get("total_s")
    if total is None or total > CLEAR_LIMIT_S:
        out.append(f"run time {total} s, limit {CLEAR_LIMIT_S} s")
    req = set(run.get("required") or [])
    mask = run.get("final_mask")
    bits = {r["encounter"]: r for r in run.get("encounters") or []}
    for e in req:
        r = bits.get(e)
        if not r:
            out.append(f"encounter {e}: not reached")
            continue
        sm = r.get("summary") or {}
        if (sm.get("wipes") or 0) > D.MAX_WIPES:
            out.append(f"{r['boss']}: {sm.get('wipes')} wipes")
        if mask is not None and r.get("bit") is not None and not mask & (1 << r["bit"]):
            out.append(f"{r['boss']}: bit {r['bit']} not in the saved mask {mask}")
        for a in r.get("attempts") or []:
            if a.get("unready"):
                out.append(f"{r['boss']} pull {a.get('n')}: pulled before the party was gathered "
                           f"({(a.get('gather') or {}).get('why')})")
        # the boss's scaled health: read before the pull, or for an event boss (summoned during the fight) by GUID
        key = "event_boss" if r.get("event") else "boss_before"
        if not any((a.get(key) or {}).get("max_health") for a in r.get("attempts") or []):
            out.append(f"{r['boss']}: no scaled health read")
    if mask is None:
        out.append("no completed-encounter mask read")
    return out


SOLO_MAX_DEATHS = 2                # spec "Solo clear of The Deadmines": at most 2 deaths per encounter
GM_OFF = "GM mode is OFF"          # cs_gm.cpp HandleGMCommand "off": LANG_GM_OFF


def solo_clear_problems(ev: dict) -> list[str]:
    """The THEN of "Solo clear of The Deadmines" (and its GIVEN as far as the run shows it): the probe alone (AutoBalance
    counted 1 player at entry), driven by its self-AI (run mode "solo"), every required encounter's bit set in the
    saved mask within CLEAR_LIMIT_S, at most SOLO_MAX_DEATHS deaths per encounter, and GM mode answered off before
    every pull that fought. Missing evidence is a problem, never a pass."""
    run = ev.get("run") or {}
    out = []
    if not run.get("cleared"):
        out.append(f"not cleared: {run.get('table') or run.get('why')}")
    if run.get("mode") != "solo":
        out.append(f"not a solo self-AI run (mode {run.get('mode')})")
    players = (run.get("mapstat") or {}).get("players")
    if players != 1:
        out.append(f"AutoBalance counted {players} players at entry, not the probe alone")
    total = run.get("total_s")
    if total is None or total > CLEAR_LIMIT_S:
        out.append(f"run time {total} s, limit {CLEAR_LIMIT_S} s")
    want = [e for e, _name in ev.get("required") or []]
    req = run.get("required") or []
    if not req or (want and sorted(want) != sorted(req)):
        out.append(f"the run's encounters {req} aren't the dungeon's required {want}")
    mask = run.get("final_mask")
    if mask is None:
        out.append("no completed-encounter mask read")
    bits = {r["encounter"]: r for r in run.get("encounters") or []}
    for e in req:
        r = bits.get(e)
        if not r:
            out.append(f"encounter {e}: not reached")
            continue
        deaths = sum(len(a.get("deaths") or []) for a in r.get("attempts") or [])
        if deaths > SOLO_MAX_DEATHS:
            out.append(f"{r['boss']}: {deaths} deaths (at most {SOLO_MAX_DEATHS})")
        if mask is not None and r.get("bit") is not None and not mask & (1 << r["bit"]):
            out.append(f"{r['boss']}: bit {r['bit']} not in the saved mask {mask}")
        for a in r.get("attempts") or []:
            if a.get("why"):              # nothing fought on this pull (boss not found, self-AI refused)
                continue
            if not any(GM_OFF in x for x in a.get("gm_off") or []):
                out.append(f"{r['boss']} pull {a.get('n')}: GM mode not confirmed off ({a.get('gm_off')})")
    return out


@check("dng.solo_clear", order=533, capability=CAP, stack="up", layer="gate", profile="scaling-on",
       covers=RUN_COVERS)
def solo_clear(ctx):
    """openspec/changes/m4-dungeon-companions specs/dungeon-runs "Solo clear of The Deadmines": GIVEN the dungeon
    probe alone at The Deadmines' target level (LFGDungeons.dbc) with `initself` gear, entered with GM mode off
    (AutoBalance counts 1 player), driven by the playerbots self-AI (`.playerbots bot self`), in a fresh copy (the
    unbind before entry). WHEN dungeon.run_party (no companions, selfbot: mode "solo") takes it to each required
    encounter in DungeonEncounter.dbc order, staged by the same nav pass as dng.party_clear; per pull the probe stands
    inside the boss's aggro radius with GM mode off and its self-AI fights. THEN within 45 minutes (run_party's clock)
    every required encounter's bit is set in the saved completed-encounter mask, with at most 2 deaths per encounter
    (solo_clear_problems). Replaces the task 1.1 prototype dng.solo_two_deadmines."""
    say = _run_log("solo")
    f = D.facts(D.DEADMINES)
    req, skipped = D.required_encounters(f)
    ev: dict = {"dungeon": f["lfg_id"], "map": f["map"], "level": f["target_level"],
                "required": [(e["encounter"], e["spawns"][0]["name"]) for e in req], "skipped": skipped}
    try:
        ev["nav"] = D.nav_stages(f, req, say)
        with D.Session(f["target_level"]) as s:
            ev["session"] = {k: s.ev.get(k) for k in ("login", "stale_group", "revive")}
            q = D.gear_quality()
            ev["initself"] = {"quality": q, "reply": s.cmd(f".playerbots bot initself={q}", settle=1.5)}
            if not initself_ok(ev["initself"]["reply"]):
                raise C.CompanionError(f"initself: no '{INITSELF_OK}' answer", initself=ev["initself"])
            ev["run"] = {}
            try:
                D.run_party(s, f, [], ev["nav"], selfbot=True, log=say, out=ev["run"])
            finally:
                ev["leave"] = s.leave()
    except C.CompanionError as e:
        _keep("solo", {**ev, **e.evidence})
        raise CheckFail(str(e), {**ev, **e.evidence})
    except C.probe.ProbeError as e:
        _keep("solo", {**ev, **e.info})
        raise CheckFail(f"probe: {e}", {**ev, **e.info})
    except BaseException:
        _keep("solo", ev)
        raise
    ev["problems"] = solo_clear_problems(ev)
    _keep("solo", ev)
    say({"table": ev["run"].get("table"), "total_s": ev["run"].get("total_s"), "cleared": ev["run"].get("cleared"),
         "problems": ev["problems"]})
    require(not ev["problems"], "; ".join(ev["problems"]), **ev)
    return ev


# ----------------------------------------------------------------------------- dng.run_budget

BUDGET_MAX_GAP_S = 2 * pop.SAMPLE_EVERY_S     # samples every 10 s: a gap over 20 s means a sample went missing


def etime_s(text: str) -> int | None:
    """`ps -o etime=` ([[dd-]hh:]mm:ss) in seconds; None when unreadable."""
    t = (text or "").strip()
    try:
        days, _, rest = t.rpartition("-")
        parts = [int(x) for x in rest.split(":")]
        if not 2 <= len(parts) <= 3:
            return None
        while len(parts) < 3:
            parts.insert(0, 0)
        return int(days or 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    except ValueError:
        return None


def budget_problems(samples: list[dict], errors: list[str], t_start: float | None, t_end: float | None,
                    cleared: bool) -> list[str]:
    """The THEN of "A clear within the performance budget": samples cover the run (the first within one sampling
    interval of its start, the last within one of its end, none more than BUDGET_MAX_GAP_S apart), every sample meets every limit of the
    performance-budget requirement (population.violations, as perf.server_budget), and the sampler had no errors. A
    run that didn't clear is a problem (the WHEN didn't happen). No samples is a problem, never a pass."""
    out = []
    if not cleared:
        out.append("the party didn't clear The Deadmines")
    if not samples:
        return out + ["no samples"]
    if errors:
        out.append(f"{len(errors)} sampler error(s): {errors[0]}")
    ts = [x["t"] for x in samples]
    if t_start is None or t_end is None:
        out.append("no run window (start/end) in the run's evidence")
    else:
        # a sample's "t" is stamped after its readings (perf_sample), so the first one, taken as the run starts,
        # carries a time just after the start: one interval of slack at each end
        if ts[0] > t_start + pop.SAMPLE_EVERY_S:
            out.append(f"first sample {ts[0] - t_start:.0f} s after the run started")
        if ts[-1] < t_end - pop.SAMPLE_EVERY_S:
            out.append(f"last sample {t_end - ts[-1]:.0f} s before the run ended")
    gap = max((b - a for a, b in zip(ts, ts[1:])), default=0)
    if gap > BUDGET_MAX_GAP_S:
        out.append(f"a {gap:.0f} s gap between samples (every {pop.SAMPLE_EVERY_S} s)")
    bad = pop.violations(samples)
    if bad:
        out.append(f"{len(bad)} budget violation(s): {bad[0]}")
    return out


def warm_up(ctx: dict, rp: dict, say) -> dict:
    """The performance-budget requirement measures after warm-up. If this run's population session already ran (the
    perf.* checks, same worldserver), the world is warm; else wait until the worldserver has been up pop.WARMUP_S."""
    s = ctx.get("population_session")
    if isinstance(s, dict) and (s.get("running_profile") or {}).get("pid") == rp.get("pid"):
        return {"from": "this run's population session (same worldserver)", "waited_s": 0}
    import subprocess
    r = subprocess.run(["ps", "-o", "etime=", "-p", str(rp["pid"])], capture_output=True, text=True)
    age = etime_s(r.stdout)
    if age is None:
        raise CheckFail(f"worldserver {rp['pid']}: no uptime from ps ({r.stdout!r})")
    wait = max(0, pop.WARMUP_S - age)
    say({"warm_up": {"worldserver_age_s": age, "wait_s": wait}})
    time.sleep(wait)
    return {"worldserver_age_s": age, "waited_s": wait, "warmup_s": pop.WARMUP_S,
            "online_random_bots": pop.online_bots()}


@check("dng.run_budget", order=534, capability=CAP, stack="up", layer="gate", profile="population",
       covers=RUN_COVERS)
def run_budget(ctx):
    """openspec/changes/m4-dungeon-companions specs/dungeon-runs "A clear within the performance budget": GIVEN the
    population profile at its default size (the running worldserver's recorded profile and N) after warm-up, and the
    probe with a /mg party party. WHEN the party clears The Deadmines with the run driver (party_dungeon, the same
    routine as dng.party_clear's Deadmines run; it can't share that run, which uses the 10-bot scaling-on profile).
    THEN the samples, taken every 10 s from the start of run_party to its end (population._Sampler, plus one at the
    end), meet every limit of performance-budget "Server-side budget under the population profile" (population.LIMITS:
    worldserver <= 6.0 GB, mysqld <= 1.0 GB, all three <= 7.0 GB as max(RSS, footprint); world-update p95 <= 100 ms and
    median <= 50 ms from `.server info`), checked per sample by population.violations; the evidence holds the
    samples (budget_problems)."""
    import contextlib
    import threading
    say = _run_log("budget")
    rp = stack.running_profile()
    n = config.population_n()
    ev: dict = {"running_profile": rp, "n": n, "limits": pop.LIMITS}
    require(bool(rp) and rp["profile"] == "population" and rp.get("n") == n,
            f"the worldserver isn't running the population profile at its default size N={n}", **ev)
    ev["warm_up"] = warm_up(ctx, rp, say)
    box: dict = {}

    @contextlib.contextmanager
    def sampling(_d):
        stop = threading.Event()
        sm = pop._Sampler(duration_s=CLEAR_LIMIT_S + 600, stop=stop)
        box["sampler"] = sm
        sm.start()
        try:
            yield
        finally:
            stop.set()
            sm.join(timeout=60)
            try:
                sm.samples.append(pop.perf_sample())        # the run's end
            except Exception as e:  # noqa: BLE001  (recorded; fails the budget)
                sm.errors.append(f"{type(e).__name__}: {e}")

    run_ev: dict = {"dungeons": []}
    try:
        d = party_dungeon(D.DEADMINES, PP.plan(), say, run_ev, "budget", during=sampling)
    except CheckFail as e:
        sm = box.get("sampler")
        raise CheckFail(str(e), {**ev, **e.evidence, "samples": sm.samples if sm else None})
    sm = box["sampler"]
    run = d["run"]
    ev |= {"dungeon": d["dungeon"], "cleared": run.get("cleared"), "total_s": run.get("total_s"),
           "table": run.get("table"), "window": [run.get("t_start"), run.get("t_end")],
           "samples_taken": len(sm.samples), "summary": pop.summarize(sm.samples),
           "violations": pop.violations(sm.samples)[:40], "sampler_errors": sm.errors, "samples": sm.samples}
    ev["problems"] = budget_problems(sm.samples, sm.errors, run.get("t_start"), run.get("t_end"),
                                     bool(run.get("cleared")))
    _keep("budget", {**ev, "party": run_ev})
    say({"summary": ev["summary"], "problems": ev["problems"], "total_s": ev["total_s"]})
    require(not ev["problems"], "; ".join(ev["problems"]), **ev)
    return ev


# ----------------------------------------------------------------------------- m4-dungeon-companions 3.3: dng.rti_focus

RTI_ICON = "skull"
PACK_YD = 10                       # spec: at least one other hostile spawn within 10 yd
RTI_SHARE = 0.8                    # spec: at least 80% of the damage dealers' damage events on the skull target
RTI_FIGHT_S = 120                  # the skull target dies within this after the orders (the spec gives no time)
RTI_POLL_S = 0.5                   # between `.npc info` rounds over the pack
RTI_QUIET_S = 5                    # GIVEN: no damage event involving the party for this long before the mark
RTI_QUIET_MAX_S = 60
RTI_STAND_YD = 30                  # the party waits at least this far (3D) from every pack member: outside aggro reach
RTI_ECHO_S = 5                     # the server's MSG_RAID_TARGET_UPDATE broadcast of the mark
DAMAGE_KINDS = ("melee_damage", "spell_damage")
UNIT_FLAG_NON_ATTACKABLE, UNIT_FLAG_IMMUNE_TO_PC, UNIT_FLAG_NOT_SELECTABLE = 0x2, 0x100, 0x2000000   # UnitDefines.h
RANK_BOSS = 3                      # creature_template.rank CREATURE_ELITE_WORLDBOSS (SharedDefines.h)
PACK_RULE = ("pack (spec): world.creature spawns on the dungeon's map in its difficulty's spawnMask bit whose template "
             "is hostile to players (FactionTemplate.dbc hostileMask [5] & FACTION_MASK_PLAYER), attackable "
             "(unit_flags without NON_ATTACKABLE 0x2, IMMUNE_TO_PC 0x100, NOT_SELECTABLE 0x2000000), not a boss (rank "
             "<> 3 and not an instance_encounters creditEntry); the one nearest (3D) the first required encounter's "
             "staging point (dungeon.nav_stages) that has at least one other such spawn within 10 yd (3D), with those "
             "neighbours. Marked: the pack member farthest from the staging point (so the nearest target is never the "
             "mark), lowest spawn id on a tie")


def pack_rule(spawns: list[dict], stage: dict, near: float = PACK_YD) -> dict | None:
    """PACK_RULE on candidate spawns [{spawn_id, entry, x, y, z, ...}]: {"anchor", "members", "marked"} or None."""
    def d(a, b):
        return math.dist((a["x"], a["y"], a["z"]), (b["x"], b["y"], b["z"]))
    for a in sorted(spawns, key=lambda c: (d(c, stage), c["spawn_id"])):
        nb = [c for c in spawns if c["spawn_id"] != a["spawn_id"] and d(a, c) <= near]
        if nb:
            members = sorted([a] + nb, key=lambda c: c["spawn_id"])
            marked = max(members, key=lambda c: (round(d(c, stage), 3), -c["spawn_id"]))
            return {"anchor": a["spawn_id"], "members": members, "marked": marked["spawn_id"],
                    "stage_yd": {c["spawn_id"]: round(d(c, stage), 1) for c in members}}
    return None


def stand_point(points: list[dict], members: list[dict], avoid: list[dict], min_yd: float = RTI_STAND_YD) -> dict | None:
    """Where the party waits "next to the pack" (STAND_RULE): of the nav pass's walkable points, the one nearest (3D)
    the pack among those at least min_yd from every pack member and every other spawn in `avoid` (the map's hostile
    spawns, bosses included); None if none is."""
    def d(a, b):
        return math.dist((a["x"], a["y"], a["z"]), (b["x"], b["y"], b["z"]))
    keep_off = list(members) + list(avoid)
    ok = [q for q in points if q and all(d(q, c) >= min_yd for c in keep_off)]
    if not ok:
        return None
    q = min(ok, key=lambda q: (min(d(q, c) for c in members), q["x"], q["y"]))
    return {"x": q["x"], "y": q["y"], "z": q["z"], "pack_yd": round(min(d(q, c) for c in members), 1),
            "nearest_hostile_yd": round(min(d(q, c) for c in keep_off), 1)}


STAND_RULE = ("stand point: of the nav pass's walkable points for the first encounter (staging point, gather point, "
              "the boss's `.mmap path` points), the one nearest (3D) the pack among those at least RTI_STAND_YD (30) "
              "yd from every pack member and every encounter's boss spawn. Before the party arrives the probe (GM "
              "mode on) removes with `.die` every other PACK_RULE candidate spawn within RTI_CLEAR_YD (30) yd of the "
              "stand point or of a pack member (clear_set), so only the pack can join the fight. Measured 2026-10-12: "
              "at the staging point, 19 yd from the pack, the party was fighting before the mark; a point 31 yd from "
              "the pack on the boss's path pulled Rhahk'Zor; the Deadmines mine has no walkable point 30 yd from "
              "every hostile spawn within 100 yd of the pack (playerbots' SightDistance)")
RTI_CLEAR_YD = 30


def clear_set(cands: list[dict], members: list[dict], stand: dict, radius: float = RTI_CLEAR_YD) -> list[dict]:
    """The non-pack candidate spawns within `radius` (3D) of the stand point or of any pack member, nearest the stand
    point first (STAND_RULE)."""
    def d(a, b):
        return math.dist((a["x"], a["y"], a["z"]), (b["x"], b["y"], b["z"]))
    ids = {c["spawn_id"] for c in members}
    near = [c for c in cands if c["spawn_id"] not in ids
            and (d(c, stand) <= radius or any(d(c, m) <= radius for m in members))]
    return sorted(near, key=lambda c: (d(c, stand), c["spawn_id"]))


def _remove(s, c: dict) -> dict:
    """GM `.die` (cs_misc.cpp HandleDieCommand: Unit::Kill on the selection) on one spawn, read back by `.npc info`."""
    s.go(c["x"], c["y"], c["z"] + 1)
    u = s.unit(c["spawn_id"], c["entry"])
    out = {"spawn_id": c["spawn_id"], "name": c["name"], "health_before": u.get("health")}
    if u.get("guid") and u.get("health"):
        s.ws.set_selection(u["guid"])
        out["reply"] = s.cmd(".die", settle=0.3)
        s.ws.set_selection(0)
        _, ok, _ = F.poll(lambda: s.unit(c["spawn_id"]).get("health"), lambda h: h == 0, timeout=5)
        out["dead"] = ok
    else:
        out["dead"] = u.get("health") == 0
    return out


def pack_candidates(f: dict) -> list[dict]:
    """The PACK_RULE spawns of a dungeon (SQL + FactionTemplate.dbc)."""
    recs, _ = dbc.read_dbc("FactionTemplate.dbc")
    fac = ",".join(str(r[0]) for r in recs if r[C.FT_HOSTILE_MASK] & C.FACTION_MASK_PLAYER)
    bad = UNIT_FLAG_NON_ATTACKABLE | UNIT_FLAG_IMMUNE_TO_PC | UNIT_FLAG_NOT_SELECTABLE
    credits = ",".join(str(e["credit_entry"]) for e in f["encounters"] if e["credit_entry"]) or "0"
    rows = db.query(
        f"SELECT c.guid, c.id, ct.name, c.position_x, c.position_y, c.position_z FROM creature c "
        f"JOIN creature_template ct ON ct.entry = c.id WHERE c.map = {int(f['map'])} "
        f"AND (c.spawnMask & {1 << f['difficulty']}) AND ct.faction IN ({fac}) AND (ct.unit_flags & {bad}) = 0 "
        f"AND ct.`rank` <> {RANK_BOSS} AND c.id NOT IN ({credits}) ORDER BY c.guid", "world")
    return [{"spawn_id": int(g), "entry": int(e), "name": n, "x": float(x), "y": float(y), "z": float(z)}
            for g, e, n, x, y, z in rows]


def rti_problems(rec: dict, share: float = RTI_SHARE) -> list[str]:
    """dng.rti_focus's pass rule on the recorded fight. rec: {"orders": {"rti": [names whispered `rti skull`],
    "companions": [every companion], "tank_attack": bool}, "mark_echo": bool, "skull": guid, "deaths_s": {guid: s},
    "dps_events_before_death": [{"victim": guid}, ...]}. Every way the scenario's WHEN or THEN can break is a
    problem."""
    p = []
    o = rec.get("orders") or {}
    missing = sorted(set(o.get("companions") or []) - set(o.get("rti") or []))
    if not o.get("companions") or missing:
        p.append(f"WHEN: `rti {RTI_ICON}` not whispered to {missing or 'any companion'}")
    if not o.get("tank_attack"):
        p.append("WHEN: `tank attack` not whispered to the tank")
    if not rec.get("mark_echo"):
        p.append(f"WHEN: the server never broadcast the {RTI_ICON} mark on the target (MSG_RAID_TARGET_UPDATE)")
    deaths = rec.get("deaths_s") or {}
    skull = rec.get("skull")
    t_skull = deaths.get(skull) if skull is not None else None
    if t_skull is None:
        p.append(f"THEN: the {RTI_ICON} target didn't die within {RTI_FIGHT_S} s")
    else:
        earlier = [g for g, t in deaths.items() if g != skull and t is not None and t < t_skull]
        if earlier:
            p.append(f"THEN: {len(earlier)} other creature(s) died before the {RTI_ICON} target")
    evs = rec.get("dps_events_before_death") or []
    if not evs:
        p.append("THEN: no damage event from the damage dealers before the target died")
    else:
        on = sum(1 for e in evs if e.get("victim") == skull)
        if on / len(evs) < share:
            p.append(f"THEN: {on}/{len(evs)} ({on / len(evs):.0%}) of the damage dealers' damage events were on the "
                     f"{RTI_ICON} target, below {share:.0%}")
    return p


def _pack_state(s, members: list[dict]) -> dict:
    """spawn id -> `.npc info` read (GUID, health) of each pack member, by spawn id (no selection)."""
    return {c["spawn_id"]: s.unit(c["spawn_id"], c["entry"]) for c in members}


@check("dng.rti_focus", order=535, capability=CAP, stack="up", layer="integration", budget_s=240,
       covers=TACTICS_COVERS)
def rti_focus(ctx):
    """(m4-dungeon-companions "Kill skull first") GIVEN the dungeon probe at The Deadmines' target level with the
    /mg party damage-dealer party (the add-on's plan), in a fresh copy, all five at the
    stand point (STAND_RULE, near the first encounter's staging point from dungeon.nav_stages) out of combat for 5 s,
    next to the PACK_RULE pack. WHEN the probe (GM mode on,
    so it is never in the fight) marks the PACK_RULE creature with the skull (MSG_RAID_TARGET_UPDATE, confirmed by
    the server's broadcast), whispers `rti skull` to every companion and `tank attack` to the tank. THEN (rti_problems)
    the skull target is the first creature of the pack to die (`.npc info` health 0, polled over the pack every
    RTI_POLL_S; the combat log's overkill can't tell a kill, Unit.cpp writes damage minus the health left) and,
    before it dies, at least 80% of the damage-dealing companions' damage events (combat log: melee and spell
    damage, attacker = a `damage` companion of the plan) are on it."""
    f = D.facts(D.DEADMINES)
    req, _ = D.required_encounters(f)
    first = req[0]
    ev: dict = {"dungeon": f["lfg_id"], "map": f["map"], "level": f["target_level"], "first_encounter":
                (first["encounter"], first["spawns"][0]["name"]), "rule": PACK_RULE}
    nav = D.nav_stages(f, [first])
    st = nav["stages"][0]
    ev["stage"] = {k: st.get(k) for k in ("point", "verdict")}
    require(st.get("point"), "GIVEN: no staging point for the first encounter", nav=nav, **ev)
    stage = st["point"]
    cands = pack_candidates(f)
    pack = pack_rule(cands, stage)
    require(pack is not None, f"GIVEN: no hostile spawn with another within {PACK_YD} yd on map {f['map']}", **ev)
    ev["pack"] = pack
    members = pack["members"]
    walkable = [stage, (st.get("gather") or {}).get("point")] + list((st.get("fallback") or {}).get("points") or [])
    bosses = [sp for e in f["encounters"] for sp in e["spawns"]]
    stand = stand_point(walkable, members, bosses)
    ev["stand"] = {"point": stand, "rule": STAND_RULE, "candidates": len([q for q in walkable if q])}
    require(stand is not None, f"GIVEN: no walkable point {RTI_STAND_YD} yd from the pack and every boss", **ev)
    ev["clear_set"] = [(c["spawn_id"], c["name"]) for c in clear_set(cands, members, stand)]
    marked = next(c for c in members if c["spawn_id"] == pack["marked"])
    plan = PP.plan()
    t0 = time.time()
    rec: dict = {}
    try:
        with D.Session(f["target_level"]) as s:
            ev["session"] = {k: s.ev.get(k) for k in ("login", "stale_group", "revive")}
            ev["unbind"] = s.unbind(f["map"])
            require(ev["unbind"]["ok"], "GIVEN: the probe's old bind didn't go away", **ev)
            ev["party"] = comps = [add_companion(s, c) for c in PP.commands(plan, "damage")]
            guids = {c["guid"] for c in comps}
            dps = {c["guid"] for c in comps if D.role_of_key(c["companion"]) == "damage"}
            tank = next(c for c in comps if c["companion"] == "tank")
            ev["enter"] = s.enter(f)
            require(ev["enter"]["ok"], "GIVEN: the probe didn't enter a fresh copy", **ev)
            s.cmd(".gm on", settle=0.3)
            ev["cleared"] = [_remove(s, c) for c in clear_set(cands, members, stand)]
            require(all(x["dead"] for x in ev["cleared"]), "GIVEN: a spawn near the pack or the stand point didn't "
                    "go away", **ev)
            ev["go_stand"] = s.go(stand["x"], stand["y"], stand["z"])
            ev["gather"] = s.gather(1 + len(comps), timeout=GATHER_S)
            require(ev["gather"]["ok"], "GIVEN: the companions didn't all arrive", **ev)
            ev["at_stage"] = {"probe": s.gps(), "companions_far": D._far(s, comps)}
            # GIVEN: out of combat (no damage event involving the party) for RTI_QUIET_S
            mark0, tq = s.ws.mark(), time.time()
            while time.time() - tq < RTI_QUIET_MAX_S:
                if time.time() - tq >= RTI_QUIET_S and D._combat_quiet(s, guids | {s.guid}, mark0) >= RTI_QUIET_S:
                    break
                time.sleep(0.5)
            early = [e for e in s.ws.events_since(mark0) if e["kind"] in DAMAGE_KINDS
                     and {e.get("attacker"), e.get("victim")} & guids]
            ev["quiet_wait_s"] = round(time.time() - tq, 1)
            require(time.time() - tq >= RTI_QUIET_S and D._combat_quiet(s, guids | {s.guid}, mark0) >= RTI_QUIET_S,
                    f"GIVEN: the party was still fighting {RTI_QUIET_MAX_S} s after gathering", early=early[-5:], **ev)
            before = _pack_state(s, members)
            ev["pack_before"] = {k: {x: v.get(x) for x in ("guid", "health", "max_health", "in_combat")}
                                 for k, v in before.items()}
            alive = [k for k, v in before.items() if v.get("guid") and v.get("health")]
            require(len(alive) == len(members), f"GIVEN: {len(members) - len(alive)} pack member(s) not alive and "
                    "readable before the mark", **ev)
            require(not any(v.get("in_combat") for v in before.values()), "GIVEN: the pack was already in combat", **ev)
            skull = before[marked["spawn_id"]]["guid"]
            pack_guids = {v["guid"]: k for k, v in before.items()}
            # WHEN: the mark, then the orders
            since = s.ws.mark()
            s.ws.set_selection(skull)          # the player clicks the creature, then sets its marker
            s.ws.set_raid_target(W.RAID_ICONS[RTI_ICON], skull)
            echo = s.ws.wait_event(lambda e: e["kind"] == "raid_target" and e["icons"].get(W.RAID_ICONS[RTI_ICON])
                                   == skull, since, timeout=RTI_ECHO_S)
            ev["mark_echo"] = echo
            orders = {"companions": [c["name"] for c in comps], "rti": [], "tank_attack": False}
            for c in comps:
                s.ws.chat(f"rti {RTI_ICON}", W.CHAT_MSG_WHISPER, to=c["name"])
                orders["rti"].append(c["name"])
            s.ws.chat("tank attack", W.CHAT_MSG_WHISPER, to=tank["name"])
            orders["tank_attack"] = True
            t_orders = time.time()
            deaths: dict = {}
            polls = []
            while time.time() - t_orders < RTI_FIGHT_S and skull not in deaths:
                now = _pack_state(s, members)
                t = round(time.time() - t_orders, 1)
                polls.append({"t": t, "health": {k: v.get("health") for k, v in now.items()}})
                for k, v in now.items():
                    g = before[k]["guid"]
                    if v.get("health") == 0 and g not in deaths:
                        deaths[g] = t
                time.sleep(RTI_POLL_S)
            evs = s.ws.events_since(since)
            t_dead = deaths.get(skull)
            cut = (t_orders + t_dead) if t_dead is not None else time.time()
            dps_evs = [{"victim": e["victim"], "attacker": e["attacker"], "kind": e["kind"], "t": round(e["t"] - t_orders, 1)}
                       for e in evs if e["kind"] in DAMAGE_KINDS and e.get("attacker") in dps and e["t"] <= cut]
            rec = {"orders": orders, "mark_echo": echo is not None, "skull": skull, "deaths_s": deaths,
                   "dps_events_before_death": dps_evs}
            by_victim: dict = {}
            for e in dps_evs:
                k = "skull" if e["victim"] == skull else pack_guids.get(e["victim"], hex(e["victim"]))
                by_victim[str(k)] = by_victim.get(str(k), 0) + 1
            ev |= {"orders": orders, "skull_guid": skull, "deaths_s": {hex(g): t for g, t in deaths.items()},
                   "dps_events_by_victim": by_victim, "dps_events": len(dps_evs), "polls": polls[-6:],
                   "fight_s": round(time.time() - t_orders, 1)}
            ev["problems"] = rti_problems(rec)
            # leave the rest of the pack to the party, then go
            ev["leave"] = s.leave()
            ev["clear"] = clear_party(s, plan)
    except C.CompanionError as e:
        raise CheckFail(str(e), {**ev, **e.evidence})
    except C.probe.ProbeError as e:
        raise CheckFail(f"probe: {e}", {**ev, **e.info})
    ev["session_s"] = round(time.time() - t0, 1)
    require(not ev["problems"], "; ".join(ev["problems"]), **ev)
    return ev
