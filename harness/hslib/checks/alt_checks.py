"""companion-bots, m4-alt-companions: the player's own characters as companions (`/mg bring`, `/mg dismiss`).

One companion-probe login (P.COMP_USER / P.COMP_CHAR, the comp.* checks' GM-level harness account; never PLAYER, never
HSTEST's character except as the refused foreign one) records four scenarios (design.md §4):
- alt.bring_dismiss (the session): the probe sends the /mg bring list parsed from the add-on's MG_BRING_PLAN
  (partyplan.bring_plan) for ALT_CHAR, a second character on the same account that the check creates with
  CMSG_CHAR_CREATE; later the /mg dismiss list;
- alt.foreign_refused: `.playerbots bot add` for the HSTEST fixture account's character is refused;
- alt.gear_untouched: with the alt in the party, one /mg party companion is called alongside it (its addclass command
  and whisper steps from MG_PARTY_PLAN, autogear included), the master whispers the alt `autogear`, and the party
  kills a creature; the alt's equipped items and talents don't change and the whisper gets the playerbots refusal;
- alt.progress_kept: before the trip the master's per-character save is taken (front-menu-saves, `.moonglade save
  take`); the party kills two creatures; after dismiss the alt has more XP, and loading the master's pre-trip save
  (`.moonglade save load`, the launcher's load path) changes none of the alt's rows.

Kills: the master GM-teleports onto a creature picked by the comp.assists creature rule (companion.creature_candidates,
SQL/DBC rules, never typed in), summons the alt beside it and deals the killing blow with `.damage` (cs_misc.cpp
HandleDamageCommand, GM level 2), so the kill is the party's (Player::RewardPlayerAndGroupAtKill shares the XP with
group members nearby). The pass rules are pure functions below with unit-level counterexamples
(tests/unit/test_alt_checks.py)."""
import re
import time

from .. import companion as C, db, fixtures as F, partyplan as PP, paths as P, saves as S, soap
from ..probe import proto as W
from ..registry import CheckFail, check, require
from .dungeon_checks import _scenario

CAP = "companion-bots"
COVERS = (
    "harness/hslib/checks/alt_checks.py",
    "harness/hslib/companion.py",
    "harness/hslib/partyplan.py",
    "client-mods/addons/Moonglade/MoongladeParty.lua",
)
ALT_CHAR = "Hscompalt"          # the alt: a second human warrior on the companion account (P.COMP_USER)
CHILDREN = ("alt.foreign_refused", "alt.gear_untouched", "alt.progress_kept")
JOIN_S = 30                     # spec: online and in the party within 30 s
LEAVE_S = 30                    # spec: out of the party within 30 s of dismiss
OFFLINE_S = 60                  # spec: offline within 60 s
REPLY_S = 20                    # the reset answer; resent every MG_BRING_PLAN.resend s (a fresh login can miss one)
REFUSAL_S = 10                  # playerbots reports errors to the master every AiPlayerbot.ErrorDelay (100 ms)
FOREIGN_WATCH_S = 5             # the refused character stays offline and out of the party this long
KILL_S = 10
NEAR_YD, NEAR_S = 10, 10        # a summoned companion beside the master before the kill (comp.assists: 10 yd)
COMPANION_WATCH_S = 2.0         # the /mg party companion's autogear refusal, if any (errors arrive within ErrorDelay)
KILLS = 2                       # spec: at least two creatures
# AddPlayerBot's refusal for a character the master may not control (PlayerbotMgr.cpp:110-114 at our pin: not the
# master's account, not a guild member, not addclass, not a linked account)
FOREIGN_REFUSAL = "You are not allowed to control bot"
# AutoGearAction::Execute with AiPlayerbot.AutoGearCommandAltBots = 0 (TrainerAction.cpp:578-584), delivered as a
# system line "<bot>|cfff00000: <text>" (PlayerbotMgr::CheckTellErrors)
AUTOGEAR_REFUSAL = "You cannot use autogear on alt bots"
EQUIP_SLOTS = 19                # Player.h EQUIPMENT_SLOT_END: bag 0 slots 0-18


# ----------------------------------------------------------------------------- pass rules (unit-tested)

def bring_problems(e: dict) -> list[str]:
    """alt.bring_dismiss: joined (online, in the party) within JOIN_S with its account unchanged; after dismiss out of
    the party within LEAVE_S and offline within OFFLINE_S."""
    out = []
    if e.get("joined_s") is None or e["joined_s"] > JOIN_S:
        out.append(f"the alt wasn't online in the party within {JOIN_S} s (joined_s={e.get('joined_s')})")
    if e.get("account_before") is None or e.get("account_before") != e.get("account_after"):
        out.append(f"characters.account changed: {e.get('account_before')} -> {e.get('account_after')}")
    if e.get("left_party_s") is None or e["left_party_s"] > LEAVE_S:
        out.append(f"the alt was still in the party {LEAVE_S} s after dismiss (left_party_s={e.get('left_party_s')})")
    if e.get("offline_s") is None or e["offline_s"] > OFFLINE_S:
        out.append(f"the alt was still online {OFFLINE_S} s after dismiss (offline_s={e.get('offline_s')})")
    return out


def foreign_problems(e: dict) -> list[str]:
    """alt.foreign_refused: the reply carries the playerbots refusal, and the character never came online or into the
    party while watched."""
    out = []
    if e.get("account") is None or e.get("account") == e.get("master_account"):
        out.append(f"GIVEN: the foreign character isn't on another account ({e.get('account')} vs the master's "
                   f"{e.get('master_account')})")
    if not any(FOREIGN_REFUSAL in x for x in e.get("reply") or []):
        out.append(f"no playerbots refusal in the reply: {e.get('reply')}")
    if e.get("online_seen") or e.get("in_party_seen"):
        out.append(f"the foreign character came online ({e.get('online_seen')}) or into the party "
                   f"({e.get('in_party_seen')})")
    return out


def gear_problems(e: dict) -> list[str]:
    """alt.gear_untouched: equipped item entries (slots 0-18) and talents unchanged, and the autogear whisper refused.
    Also the GIVEN that the /mg party companion's own autogear wasn't refused (it ran alongside, as the add-on's)."""
    out = []
    if e.get("equip_before") is None or e.get("equip_before") != e.get("equip_after"):
        out.append(f"equipped items changed: {e.get('equip_before')} -> {e.get('equip_after')}")
    if not e.get("equip_before"):
        out.append("GIVEN: the alt has no equipped items to protect")
    if e.get("talents_before") is None or e.get("talents_before") != e.get("talents_after"):
        out.append(f"talents changed: {e.get('talents_before')} -> {e.get('talents_after')}")
    if not e.get("refusal"):
        out.append(f"no `{AUTOGEAR_REFUSAL}` answer to the alt's autogear whisper")
    if e.get("companion_refused"):
        out.append(f"GIVEN: the /mg party companion's autogear was refused too: {e.get('companion_refused')}")
    if (e.get("kills") or 0) < 1:
        out.append("GIVEN: the party didn't kill a creature")
    return out


def progress_problems(e: dict) -> list[str]:
    """alt.progress_kept: more XP (or a higher level) than before the trip, at least KILLS kills, the master's load
    done, and none of the alt's rows changed by it."""
    out = []
    b, a = e.get("before") or {}, e.get("after_trip") or {}
    if (e.get("kills") or 0) < KILLS:
        out.append(f"GIVEN: the party killed {e.get('kills')} creatures, want {KILLS}")
    if not b or not a or (a["level"], a["xp"]) <= (b["level"], b["xp"]):
        out.append(f"the alt gained no XP: level/xp {b.get('level')}/{b.get('xp')} -> {a.get('level')}/{a.get('xp')}")
    if a and b and a.get("money", 0) < b.get("money", 0):
        out.append(f"the alt's money went down on the trip: {b.get('money')} -> {a.get('money')}")
    if (e.get("load") or {}).get("state") != "done":
        out.append(f"the master's save didn't load: {e.get('load')}")
    if e.get("rows_diff") is None:
        out.append("the alt's rows weren't compared around the load")
    elif e["rows_diff"]:
        out.append(f"loading the master's save changed the alt's rows: {sorted(e['rows_diff'])}")
    return out


# ----------------------------------------------------------------------------- database reads (SELECT only)

def char_of(name: str) -> dict | None:
    rows = db.query(f"SELECT guid, account, level, xp, money, online FROM characters WHERE name='{name}'", "characters")
    if not rows:
        return None
    g, a, lv, xp, m, on = (int(x) for x in rows[0])
    return {"guid": g, "account": a, "level": lv, "xp": xp, "money": m, "online": on}


def equipped(guid: int) -> dict:
    """{slot: item entry} for bag 0 slots 0-18 (character_inventory bag 0 is the character itself)."""
    return {int(s): int(e) for s, e in db.query(
        f"SELECT ci.slot, ii.itemEntry FROM character_inventory ci JOIN item_instance ii ON ii.guid = ci.item "
        f"WHERE ci.guid = {int(guid)} AND ci.bag = 0 AND ci.slot < {EQUIP_SLOTS}", "characters")}


def items(guid: int) -> list[int]:
    return sorted(int(e) for (e,) in db.query(
        f"SELECT ii.itemEntry FROM character_inventory ci JOIN item_instance ii ON ii.guid = ci.item "
        f"WHERE ci.guid = {int(guid)}", "characters"))


def talents(guid: int) -> list[list[int]]:
    return sorted([int(s), int(m)] for s, m in db.query(
        f"SELECT spell, specMask FROM character_talent WHERE guid = {int(guid)}", "characters"))


# ----------------------------------------------------------------------------- GIVEN

def ensure_alt() -> dict:
    """The alt on the companion account, offline at the master's level (C.LEVEL, the creature rule's level)."""
    ev = {}
    if not char_of(ALT_CHAR):
        from ..probe import client as probe
        lr, ws, _ = probe.open_world(P.COMP_USER, P.COMP_PASS)
        with ws:
            code = ws.char_create(ALT_CHAR, F.RACE_HUMAN, F.CLASS_WARRIOR)
        ev["char_create"] = f"0x{code:02X} {W.RESPONSE_NAMES.get(code, '?')}"
        if code != W.CHAR_CREATE_SUCCESS:
            raise C.CompanionError("CMSG_CHAR_CREATE rejected for the alt", **ev)
        _, ok, _ = F.poll(lambda: char_of(ALT_CHAR), bool, timeout=15)
        if not ok:
            raise C.CompanionError("the alt isn't in the characters DB after CHAR_CREATE_SUCCESS", **ev)
    _, ok, _ = F.poll(lambda: char_of(ALT_CHAR)["online"], lambda v: v == 0, timeout=OFFLINE_S)
    if not ok:
        raise C.CompanionError("GIVEN: the alt is online before the check", alt=char_of(ALT_CHAR), **ev)
    if char_of(ALT_CHAR)["level"] != C.LEVEL:
        ev["level"] = C.gm(f"character level {ALT_CHAR} {C.LEVEL}").evidence()
        _, ok, _ = F.poll(lambda: char_of(ALT_CHAR)["level"], lambda v: v == C.LEVEL, timeout=15)
        if not ok:
            raise C.CompanionError("the alt's level wasn't saved", **ev)
    return ev | {"alt": char_of(ALT_CHAR)}


def _comp_identity() -> S.Identity:
    return S.Identity(P.COMP_USER, P.COMP_PASS, P.COMP_CHAR, ALT_CHAR)


# ----------------------------------------------------------------------------- in the world

def _sys_since(s, since: int) -> list[str]:
    return [e["msg"] for e in s.ws.events_since(since) if e["kind"] == "chat" and e["type"] == W.CHAT_MSG_SYSTEM]


def bring(s, lists: dict, guid: int) -> dict:
    """The /mg bring list: the add command, wait for the alt online in the party (SMSG_GROUP_LIST, as the add-on waits
    for PARTY_MEMBERS_CHANGED), then its whisper steps, each resent every `resend` s until its reply (as the add-on)."""
    t0 = time.time()
    reply = s.ws.command(lists["add"], settle=0.5, timeout=5)
    joined = None
    while time.time() - t0 < JOIN_S + 5 and joined is None:
        if any(m["guid"] == guid and m["online"] for m in s.members()):
            joined = round(time.time() - t0, 2)
        else:
            time.sleep(0.2)
    ev = {"command": lists["add"], "reply": reply, "joined_s": joined}
    if joined is None:
        return ev
    s.companions[ALT_CHAR] = C.bot_info(guid)
    ev["steps"] = []
    for st in lists["steps"]:
        rec, t1, got = {"send": st["send"], "sent": 0}, time.time(), None
        mark = s.ws.mark()
        while got is None and time.time() - t1 < REPLY_S:
            for w in st["send"]:
                s.ws.chat(w, W.CHAT_MSG_WHISPER, to=ALT_CHAR)
            rec["sent"] += 1
            if not st["reply"]:
                break
            got = s.ws.wait_event(lambda e: e["kind"] == "chat" and e["type"] == W.CHAT_MSG_WHISPER
                                  and e.get("sender") == guid and st["reply"] in e["msg"], mark,
                                  timeout=float(lists.get("resend") or 5))
        rec["reply"] = got["msg"] if got else None
        rec["answer_s"] = round(time.time() - t1, 2)
        ev["steps"].append(rec)
    return ev


def dismiss(s, lists: dict, guid: int) -> dict:
    t0 = time.time()
    reply = s.ws.command(lists["remove"], settle=0.5, timeout=5)
    left = None
    while time.time() - t0 < LEAVE_S and left is None:
        if not any(m["guid"] == guid for m in s.members()):
            left = round(time.time() - t0, 2)
        else:
            time.sleep(0.2)
    _, ok, _ = F.poll(lambda: char_of(ALT_CHAR)["online"], lambda v: v == 0, timeout=OFFLINE_S, interval=0.25)
    offline = round(time.time() - t0, 2) if ok else None
    if left is not None:
        s.companions.pop(ALT_CHAR, None)
    return {"command": lists["remove"], "reply": reply, "left_party_s": left, "offline_s": offline,
            "group_after": s.ws.group}


def _go(s, p: dict) -> dict | None:
    mark = s.ws.mark()
    s.ws.command(f".go xyz {p['x']:.2f} {p['y']:.2f} {p['z']:.2f}", settle=0.5, timeout=5)
    tp = s.ws.wait_event(lambda e: e["kind"] == "teleport_near", mark, timeout=5)
    if tp:
        s.ws.heartbeat(tp["x"], tp["y"], tp["z"])
    return tp


def kill_one(s, exclude: set, summon: list[str], home: dict) -> dict:
    """Teleport onto a live creature by the comp.assists creature rule, summon the companions beside the master, and
    kill it with `.damage` (the master's blow; the party shares the kill). The master first goes back to the session's
    start point (`home`), which the rule's candidates are nearest to, so their grids are loaded for `.npc info`."""
    _go(s, home)
    # `.npc info <spawn>` answers for the master's selected creature when there is one (cs_npc.cpp
    # HandleNpcInfoCommand: getSelectedCreature first), so the last kill's corpse must not stay selected: every
    # candidate would read as dead (run 2026-10-12: the second kill found "no live, unhurt creature")
    s.ws.set_selection(0)
    cr = None
    for _ in range(3):
        try:
            cr = s.pick_live_creature(exclude=exclude)
            break
        except C.CompanionError:
            time.sleep(1.0)
    if cr is None:
        cr = s.pick_live_creature(exclude=exclude)
    p = cr["npc_info"]["pos"] or cr["spawn"]
    _go(s, p)
    ev = {"spawn_id": cr["spawn_id"], "entry": cr.get("entry"), "summon": {n: s.summon(n) for n in summon}}
    ev["near"] = {}
    for n in summon:                     # the summoned land beside the master (in range for the party's XP)
        g = (getattr(s, "companions", {}).get(n) or {}).get("guid")
        if g:
            w = s.wait_near(g, NEAR_YD, NEAR_S)
            ev["near"][n] = {"ok": w["ok"], "elapsed_s": w["elapsed_s"], "distance_yd": w["samples"][-1]["distance_yd"]}
    s.ws.set_selection(cr["guid"])
    ev["damage"] = s.ws.command(".damage 5000", settle=0.5, timeout=5)
    dead, t0 = False, time.time()
    while time.time() - t0 < KILL_S and not dead:
        dead = s.npc_info(cr["spawn_id"]).get("health") == 0
        if not dead:
            time.sleep(0.5)
    ev["dead"] = dead
    s.ws.set_selection(0)
    return ev


def _foreign(s, lists_for, ev: dict) -> dict:
    fixture = F.ensure() if not F.char_guid() else None
    name = P.TEST_CHAR
    row = char_of(name)
    e = {"fixture": fixture, "character": name, "account": row and row["account"],
         "master_account": C.account_id(), "online_before": row and row["online"]}
    require(row is not None and row["online"] == 0, "GIVEN: the HSTEST character is missing or online", **e)
    lists = lists_for(name)
    e["command"] = lists["add"]
    e["reply"] = s.ws.command(lists["add"], settle=1.0, timeout=5)
    t0, on, party = time.time(), False, False
    while time.time() - t0 < FOREIGN_WATCH_S:
        on = on or bool(char_of(name)["online"])
        party = party or any(m["name"].lower() == name.lower() for m in s.members())
        time.sleep(0.25)
    e["online_seen"], e["in_party_seen"] = on, party
    if on or party:                       # never leave a foreign character logged in as a bot
        e["cleanup"] = s.ws.command(PP.bring_commands(PP.bring_plan(), name)["remove"], settle=0.5, timeout=5)
    bad = foreign_problems(e)
    require(not bad, "; ".join(bad), **e)
    return e


@check("alt.bring_dismiss", order=135, capability=CAP, stack="up", layer="integration", budget_s=180,
       children=CHILDREN)
def bring_dismiss(ctx):
    """One companion-probe login with the alt (ALT_CHAR, a second character on the companion account, offline):
    alt.foreign_refused (`.playerbots bot add` for HSTEST's character is refused and it stays offline), then the /mg
    bring list from MG_BRING_PLAN (alt.bring_dismiss: online and in the party within 30 s, account unchanged),
    alt.gear_untouched (a /mg party companion alongside, the alt whispered `autogear`, a kill: equipment and talents
    unchanged, the whisper refused), a second kill, the /mg dismiss list (out of the party within 30 s, offline within
    60 s), and alt.progress_kept (more XP; loading the master's pre-trip save leaves the alt's rows unchanged)."""
    bring_plan, party_plan = PP.bring_plan(), PP.plan()
    lists = PP.bring_commands(bring_plan, ALT_CHAR)
    ev: dict = {"plan_file": "client-mods/addons/Moonglade/" + PP.PARTY_LUA.name, "lists": lists,
                "forbidden_in_lists": PP.alt_forbidden(lists["whispers"])}
    res: dict = {}
    t0 = time.time()
    try:
        ev["account"] = C.ensure_account()
        ev["master"] = C.ensure_character()
        ev["alt_given"] = ensure_alt()
        alt = char_of(ALT_CHAR)
        guid = alt["guid"]
        master = C.char_row()
        with S.as_identity(_comp_identity()):
            ev["store_cleared_before"] = S.clear_store(master["guid"])
        before = {"char": alt, "equip": equipped(guid), "talents": talents(guid), "items": items(guid)}
        ev["alt_before"] = before
        # checklist 11: the PLAYER baseline and the characters-database safety copy before the first save or load
        ev["before_writes"] = S.before_writes()
        player_before = S.player_rows()
        # progress GIVEN: the master's pre-trip save (offline; the module writes the lane's store)
        with S.as_identity(_comp_identity()):
            pre = S.take(master["guid"], "alt-pre-trip")
        ev["pre_trip_save"] = {k: pre.get(k) for k in ("id", "kind", "level", "label")}
        trip: dict = {"kills": []}
        s = C.Session()
        s.__enter__()
        try:
            ev["session"] = {k: s.ev.get(k) for k in ("login", "stale_group", "gm_off")}
            home = s.gps()
            require("x" in home, "GIVEN: no `.gps` position for the master", gps=home)

            _scenario(res, "alt.foreign_refused",
                      lambda: _foreign(s, lambda n: PP.bring_commands(bring_plan, n), ev))

            ev["bring"] = bring(s, lists, guid)
            ev["account_in_party"] = (char_of(ALT_CHAR) or {}).get("account")
            if ev["bring"]["joined_s"] is not None:
                def gear():
                    e: dict = {"equip_before": before["equip"], "talents_before": before["talents"]}
                    comp = PP.commands(party_plan, "damage")[0]          # the tank, the first /mg party companion
                    from .party_checks import add_companion
                    mark = s.ws.mark()
                    e["party_companion"] = add_companion(s, comp)
                    cname = e["party_companion"]["name"]
                    # its last step (autogear) has no answer to wait for; watch for a refusal of it
                    s.ws.wait_event(lambda x: x["kind"] == "chat" and x["type"] == W.CHAT_MSG_SYSTEM
                                    and cname in x["msg"] and "autogear" in x["msg"].lower(), mark,
                                    timeout=COMPANION_WATCH_S)
                    e["companion_refused"] = [x for x in _sys_since(s, mark) if cname in x and "autogear" in x.lower()]
                    mark = s.ws.mark()
                    s.ws.chat("autogear", W.CHAT_MSG_WHISPER, to=ALT_CHAR)
                    got = s.ws.wait_event(lambda x: x["kind"] == "chat" and x["type"] == W.CHAT_MSG_SYSTEM
                                          and AUTOGEAR_REFUSAL in x["msg"] and ALT_CHAR in x["msg"], mark,
                                          timeout=REFUSAL_S)
                    e["refusal"] = got["msg"] if got else None
                    e["system_lines"] = _sys_since(s, mark)[-5:]
                    k = kill_one(s, set(), [ALT_CHAR, cname], home)
                    trip["kills"].append(k)
                    e["kill"] = k
                    e["kills"] = int(k["dead"])
                    trip["gear"] = e
                    return e
                _scenario(res, "alt.gear_untouched", gear)
                try:
                    done = {k["spawn_id"] for k in trip["kills"]}
                    while sum(k["dead"] for k in trip["kills"]) < KILLS and len(trip["kills"]) < KILLS + 2:
                        k = kill_one(s, done, [ALT_CHAR], home)
                        done.add(k["spawn_id"])
                        trip["kills"].append(k)
                except C.CompanionError as e:
                    trip["kill_error"] = {"error": str(e), **e.evidence}
                except C.probe.ProbeError as e:
                    trip["kill_error"] = {"error": f"probe: {e}", **e.info}
                ev["alt_after_kills_online"] = char_of(ALT_CHAR)
                ev["dismiss"] = dismiss(s, lists, guid)
        finally:
            s.__exit__(None, None, None)
            ev["session_end"] = {k: s.ev.get(k) for k in ("left_group_at_exit", "logout", "cleanup_errors")}
        ev["alt_after"] = after = char_of(ALT_CHAR)
        b = {"joined_s": ev["bring"]["joined_s"], "account_before": alt["account"],
             "account_after": ev["account_in_party"], "left_party_s": (ev.get("dismiss") or {}).get("left_party_s"),
             "offline_s": (ev.get("dismiss") or {}).get("offline_s")}
        ev["bring_rule"] = b
        if "alt.gear_untouched" in res and res["alt.gear_untouched"]["status"] == "pass":
            g = trip["gear"] | {"equip_after": equipped(guid), "talents_after": talents(guid)}
            bad = gear_problems(g)
            res["alt.gear_untouched"] = {"status": "fail" if bad else "pass", "message": "; ".join(bad)[:500],
                                         "evidence": g, "duration_s": res["alt.gear_untouched"]["duration_s"]}

        def progress():
            e: dict = {"before": before["char"], "after_trip": after, "kills": sum(k["dead"] for k in trip["kills"]),
                       "kill_log": trip["kills"], "kill_error": trip.get("kill_error"),
                       "items_before": before["items"], "items_after": items(guid)}
            require(after and after["online"] == 0, "GIVEN: the alt is still online after dismiss", **e)
            require(S.wait_offline(P.COMP_CHAR, timeout=30), "GIVEN: the master is still online", **e)
            rows_a = S.normalize(S.db_rows(guid))
            with S.as_identity(_comp_identity()):
                e["load"] = S.load(master["guid"], pre["id"])
            rows_b = S.normalize(S.db_rows(guid))
            e["rows_diff"] = S.diff(rows_a, rows_b)
            e["master_after_load"] = C.char_row()
            bad = progress_problems(e)
            require(not bad, "; ".join(bad), **e)
            return e
        _scenario(res, "alt.progress_kept", progress)
    except C.CompanionError as e:
        raise CheckFail(str(e), {**ev, **e.evidence, "children": res})
    except C.probe.ProbeError as e:
        raise CheckFail(f"probe: {e}", {**ev, **e.info, "children": res})
    finally:
        try:
            m = C.char_row()
            if m:
                with S.as_identity(_comp_identity()):
                    ev["store_cleared_after"] = S.clear_store(m["guid"])
        except Exception as e:  # noqa: BLE001  (cleanup: reported, never hides the result)
            ev["store_clear_error"] = str(e)
    ev["session_s"] = round(time.time() - t0, 1)
    ev["children"] = {c: res.get(c) or {"status": "error", "message": "not run", "evidence": {}} for c in CHILDREN}
    ev["player_rows_unchanged"] = S.player_rows() == player_before
    require(ev["player_rows_unchanged"], "the PLAYER account's characters changed during the session", **ev)
    bad = bring_problems(ev["bring_rule"])
    require(not ev["forbidden_in_lists"], f"the bring list sends {ev['forbidden_in_lists']} to an alt", **ev)
    require(not bad, "; ".join(bad), **ev)
    return ev
