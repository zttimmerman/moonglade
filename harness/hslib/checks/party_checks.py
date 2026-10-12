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
import struct
import time

from .. import companion as C, db, dbc, dungeon as D, fixtures as F, partyplan as PP, paths as P, soap
from ..probe import proto as W
from ..registry import CheckFail, check, require

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


@check("dng.party_clear", order=532, capability=CAP, stack="up", layer="gate", profile="scaling-on",
       covers=RUN_COVERS)
def party_clear(ctx):
    """openspec/changes/m4-dungeon-companions specs/dungeon-runs "Party clears each listed dungeon" (slice 3.4: The
    Deadmines only). GIVEN the dungeon probe at the dungeon's target level (LFGDungeons.dbc) with `initself` gear, a
    /mg party damage-dealer party (the add-on's plan, hslib/partyplan.py) and a fresh copy entered with GM mode off
    (AutoBalance counts 5 players). WHEN dungeon.run_party takes them to each required encounter in
    DungeonEncounter.dbc order (design.md §4: staging by `.mmap path` from the level-3 nav account; gather farther back
    on the path, walk in together, GM mode off, wait until everyone is within 15 yd and out of combat, then the pull),
    with the probe fighting as the fifth member through `.playerbots bot self`. THEN within 45 minutes every required
    encounter's bit is set in the probe's saved completed-encounter mask, with at most 2 wipes per encounter; the
    evidence holds per encounter the time, wipes, deaths by role and the boss's scaled health."""
    say = _run_log("party")
    plan = PP.plan()
    ev: dict = {"dungeons": []}
    t0 = time.time()
    for lfg in (D.DEADMINES,):          # slice 3.4: The Deadmines; the other listed dungeons follow
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
                d["party"] = [add_companion(s, c) for c in PP.commands(plan, "damage")]
                say({"party": [(c["companion"], c["name"]) for c in d["party"]]})
                d["run"] = {}
                try:
                    D.run_party(s, f, d["party"], d["nav"], selfbot=True, log=say, out=d["run"])
                finally:
                    d["leave"] = s.leave()
                    d["clear"] = clear_party(s, plan)
        except C.CompanionError as e:
            _keep("party", {**ev, **e.evidence})
            raise CheckFail(str(e), {**ev, **e.evidence})
        except C.probe.ProbeError as e:
            _keep("party", {**ev, **e.info})
            raise CheckFail(f"probe: {e}", {**ev, **e.info})
        except BaseException:
            _keep("party", ev)
            raise
        d["problems"] = party_clear_problems(d)
        say({"table": d["run"].get("table"), "total_s": d["run"].get("total_s"), "cleared": d["run"].get("cleared"),
             "problems": d["problems"]})
    ev["session_s"] = round(time.time() - t0, 1)
    _keep("party", ev)
    problems = [f"{d['dungeon']}: {p}" for d in ev["dungeons"] for p in d["problems"]]
    require(not problems, "; ".join(problems), **ev)
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
        if not any((a.get("boss_before") or {}).get("max_health") for a in r.get("attempts") or []):
            out.append(f"{r['boss']}: no scaled health read")
    if mask is None:
        out.append("no completed-encounter mask read")
    return out


@check("dng.solo_two_deadmines", order=533, capability=CAP, stack="up", layer="gate", profile="scaling-on",
       covers=RUN_COVERS)
def solo_two_deadmines(ctx):
    """(m4-dungeon-companions task 1.1, optional) The probe alone with the playerbots self-AI (`.playerbots bot self`,
    gear by `initself=<AutoGearQualityLimit>`) against The Deadmines' first two required bosses, staged like the party
    run (dungeon.run_party, selfbot=True)."""
    say = _run_log("solo")
    f = D.facts(D.DEADMINES)
    req, _ = D.required_encounters(f)
    ev: dict = {"dungeon": f["lfg_id"], "level": f["target_level"]}
    try:
        ev["nav"] = D.nav_stages(f, req[:2], say)
        with D.Session(f["target_level"]) as s:
            q = D.gear_quality()
            ev["initself"] = {"quality": q, "reply": s.cmd(f".playerbots bot initself={q}", settle=1.5)}
            s.cmd(".revive", settle=0.3)
            ev["run"] = {}
            try:
                D.run_party(s, f, [], ev["nav"], selfbot=True, only=2, log=say, out=ev["run"])
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
    _keep("solo", ev)
    say({"table": ev["run"].get("table"), "cleared": ev["run"].get("cleared")})
    require(ev["run"].get("cleared"), f"not cleared: {ev['run'].get('table') or ev['run'].get('why')}", **ev)
    return ev
