"""front-menu-saves: per-character saves, server side (openspec/changes/front-menu-saves, capability character-saves).

Headless: the probe plays the saves account's characters (hslib/saves.py: P.SAVES_USER, never PLAYER or HSTEST) and
talks to mod-moonglade exactly as the Moonglade add-on does ("MGSAVE" add-on whispers); the launcher's side is SOAP
(`.moonglade save ...`). Every check that saves or loads first calls saves.before_writes(): the PLAYER characters'
rows are fingerprinted once (saves.player_untouched compares at the end) and a safety copy of the characters database
is written to env/tmp/saves-safety-<run>/ (the path is in the evidence). All saves land in the saves account's own
folder of this lane's store (P.SAVES/HSSAVES/<character id>/)."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import signal
import time
from pathlib import Path

from .. import config, db, paths as P, saves as S, soap, stack
from ..registry import check, require
from ..session import run_children

CAP = "character-saves"
REFUSE_COMBAT = "You can't save in combat."
REFUSE_DEAD = "You can't save while dead."
FORBIDDEN_WORDS = ("server", "realm", "account", "database")


def _run(fn, ev):
    try:
        return fn()
    except S.SavesError as e:
        require(False, str(e), **(ev | {"error_evidence": e.evidence}))


def _status() -> str:
    return S.gm(".moonglade save status")


def _given_enabled(ev):
    st = _status()
    ev["status"] = st
    require(st.startswith("Moonglade saves: enabled"), "GIVEN not met: saves aren't enabled on the server", **ev)


def _guid(name):
    r = S.char_row(name)
    return r["guid"] if r else None


def _new_ids(guid, before):
    return sorted(x for x in S.store_ids(guid) if x not in before)


# ---------------------------------------------------------------- classification

def coverage_report(tables: dict[str, list[tuple[str, str]]], module: dict, design: list[str]) -> dict:
    """tables: every table of the character database with its (column, type) list; module: the running module's
    lists; design: the in-save tables of design.md section 1. Returns the findings (pure, for counterexamples)."""
    char_col = re.compile(r"^(guid|owner|owner_guid|receiver|player.*|character_guid|memberguid|leaderguid|"
                          r"captainguid|creator|invitee|sender|ownerguid|playerguid|spammerguid|friend)$", re.I)
    item_col = re.compile(r"item", re.I)
    relevant = sorted(t for t, cols in tables.items()
                      if any(char_col.match(c) or (item_col.search(c) and ty in ("int", "bigint")) for c, ty in cols))
    lists = {k: set(v) for k, v in module.items()}
    unclassified = [t for t in relevant if not any(t in v for v in lists.values())]
    twice = [t for t in tables if sum(t in v for v in lists.values()) > 1]
    unknown = sorted(t for v in lists.values() for t in v if t not in tables)
    return {"relevant": relevant, "unclassified": unclassified, "on_two_lists": twice, "listed_but_missing": unknown,
            "in_save_matches_design": sorted(module["in_save"]) == sorted(design),
            "in_save_not_in_design": sorted(set(module["in_save"]) - set(design)),
            "design_not_in_save": sorted(set(design) - set(module["in_save"]))}


def design_save_tables() -> list[str]:
    live = P.ROOT / "openspec" / "changes" / "front-menu-saves" / "design.md"
    arch = sorted((P.ROOT / "openspec" / "changes" / "archive").glob("*-front-menu-saves/design.md"))
    text = (live if live.exists() else arch[-1]).read_text()
    sec = text[text.index("**In the save**"):text.index("**In the core's dump but not in our save:**")]
    out = []
    for line in sec.splitlines():
        if line.startswith("| `"):
            names = re.findall(r"`(\w+)`", line.split("|")[1])
            out += [("character_queststatus" + n) if n.startswith("_") else n for n in names]
    return out


def module_lists() -> dict:
    out = {}
    for line in S.gm(".moonglade save tables").splitlines():
        m = re.match(r"Moonglade save tables: (in-save|removed-on-load|kept) (.*)$", line.strip())
        if m:
            out[m.group(1).replace("-", "_")] = m.group(2).split(",")
    return out


# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "modules/mod-moonglade/src/MoongladeSaves.*",
    "modules/mod-moonglade/src/logic/Saves*",
    "modules/mod-moonglade/src/MoongladeAddon.*",
    "client-mods/addons/Moonglade/*",
    "modules/mod-moonglade/conf/mod_moonglade.conf.dist#Moonglade.Saves",
    "harness/hslib/backups.py",
    "harness/hslib/charsaves.py",
    "harness/hslib/checks/saves_checks.py",
    "harness/hslib/frontmenu.py",
    "harness/hslib/mpq.py",
    "harness/hslib/pause.py",
    "harness/hslib/saves.py",
)


@check("saves.table_coverage", order=470, capability=CAP, stack="up", layer="integration")
def table_coverage(ctx):
    """Every table of the running character database with a column naming a character (guid, owner, owner_guid,
    receiver, player*, and the other character-reference names) or an item (an integer column named *item*) is on
    exactly one of the running module's lists (`.moonglade save tables`: in the save, removed on load, kept); the
    in-save list equals design.md section 1's table and the module's source header."""
    ev = {}
    rows = db.query("SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME, ORDINAL_POSITION", "characters")
    tables: dict = {}
    for t, c, ty in rows:
        tables.setdefault(t, []).append((c, ty))
    module = module_lists()
    ev["module"] = {k: len(v) for k, v in module.items()}
    require(set(module) == {"in_save", "removed_on_load", "kept"}, "the module didn't print its three lists", raw=module, **ev)
    header = S.classification()
    ev["header_matches_server"] = {k: sorted(header[k]) == sorted(module[k]) for k in module}
    design = design_save_tables()
    ev["design_in_save"] = design
    rep = coverage_report(tables, module, design)
    ev |= rep
    require(all(ev["header_matches_server"].values()), "the running module's lists differ from MoongladeSaves.h", **ev)
    require(not rep["unclassified"], f"{len(rep['unclassified'])} table(s) on no list", **ev)
    require(not rep["on_two_lists"] and not rep["listed_but_missing"], "a table is on two lists or doesn't exist", **ev)
    require(rep["in_save_matches_design"], "the in-save list doesn't match design.md section 1", **ev)
    return ev


# ---------------------------------------------------------------- saving

def manual(ctx):
    """A probe character in the world, out of combat, sends `MGSAVE SAVE manual <name>`: within 5 s a complete save
    folder exists under the saves account's store with a manifest naming the character, level, zone and the name; the
    file's sha256 matches the manifest; the probe gets a success line; no .tmp-* folder is left."""
    ev = {"before_writes": S.before_writes()}
    _given_enabled(ev)
    name = f"Manual {int(time.time()) % 100000}"
    with S.Session(S.ident().alt) as s:
        g = s.guid
        before = S.store_ids(g)
        w = S.who(g)
        ev["who"] = w
        require(w.get("online") and not w["combat"], "GIVEN not met: not in the world out of combat", **ev)
        t0 = time.time()
        ev["replies"] = s.ask(f"SAVE manual {name}")
        new = []
        while time.time() - t0 < 5 and not new:
            new = [x for x in _new_ids(g, before) if not x.startswith(".tmp-")]
            time.sleep(0.1)
        ev["new"] = new
        ev["after_s"] = round(time.time() - t0, 2)
        require(len(new) == 1, "no single new save folder within 5 s", **ev)
        d = S.store_dir(g) / new[0]
        m = S.manifest(g, new[0])
        ev["manifest"] = m
        sha = hashlib.sha256((d / "character.json.gz").read_bytes()).hexdigest()
        ev["sha256_file"] = sha
        row = S.char_row(S.ident().alt)
        require(m["label"] == name and m["name"] == S.ident().alt and m["level"] == row["level"] and m["kind"] == "manual"
                and m["zone"] == row["zone"] and m["zone_name"], "the manifest doesn't name the character, level, "
                "zone and the given name", row=row, **ev)
        require(sha == m["sha256"], "the file's sha256 doesn't match the manifest", **ev)
        require(any(r == f"OK\tSAVE\t{name}" for r in ev["replies"]), "no success line for the save", **ev)
        require(not S.store_tmp(g), "a .tmp-* folder is left", **ev)
    return ev


def quick_slot(ctx):
    """A character with an existing quicksave quicksaves twice more: exactly one quicksave exists for it, and it is
    the newest (its id and creation time are after every earlier quicksave's)."""
    ev = {"before_writes": S.before_writes()}
    _given_enabled(ev)
    with S.Session(S.ident().alt) as s:
        g = s.guid
        ev["first"] = s.ask("QUICK")
        quick = lambda: [x for x in S.store_ids(g) if "-quick" in x and not x.startswith(".tmp-")]   # noqa: E731
        given = quick()
        ev["given"] = given
        require(len(given) == 1 and any(r.startswith("OK\tQUICK") for r in ev["first"]),
                "GIVEN not met: no existing quicksave", **ev)
        seen = list(given)
        for i in range(2):
            time.sleep(1.1)
            ev[f"quick{i + 2}"] = s.ask("QUICK")
            seen += [x for x in quick() if x not in seen]
        after = quick()
        ev["after"] = after
        ev["seen"] = seen
        require(len(after) == 1, f"{len(after)} quicksaves exist", **ev)
        m = S.manifest(g, after[0])
        ev["newest_manifest"] = {k: m[k] for k in ("id", "created", "kind")}
        require(after[0] == max(seen) and after[0] not in given, "the remaining quicksave isn't the newest", **ev)
    return ev


def _fight_alt(s) -> dict:
    s.ev["level"] = S.gm(f"character level {s.name} 8")
    return S.engage(s, level=8)


def refused_in_combat(ctx):
    """A probe character in melee with a creature asks for a manual save and a quicksave, and an autosave trigger fires
    during the fight; later the same character is dead and asks again: no save folder is created, it gets "You can't
    save in combat." / "You can't save while dead.", and the autosave is skipped, not queued (none appears after the
    fight ends)."""
    ev = {"before_writes": S.before_writes()}
    _given_enabled(ev)
    ev["interval"] = S.gm(".moonglade save interval 1")
    try:
        with S.Session(S.ident().alt) as s:
            g = s.guid
            before = S.store_ids(g)
            ev["fight"] = _run(lambda: S.engage(s, level=8), ev)
            ev["who_in_fight"] = S.who(g)
            require(ev["who_in_fight"].get("combat"), "GIVEN not met: not in combat", **ev)
            ev["manual"] = s.ask("SAVE manual in combat")
            ev["quick"] = s.ask("QUICK")
            ev["trigger"] = S.gm(f".moonglade save trigger {g} Arrived")
            time.sleep(2.0)                                   # the trigger is due 1 s after it fires
            ev["who_after_trigger"] = S.who(g)
            ev["combatstop"] = s.cmd(".combatstop")
            time.sleep(3.0)                                    # an autosave queued earlier would land now
            ev["after_fight"] = _new_ids(g, before)
            s.ws.set_selection(g)
            ev["die"] = s.cmd(".die")
            ev["who_dead"] = S.who(g)
            ev["dead_quick"] = s.ask("QUICK")
            ev["dead_manual"] = s.ask("SAVE manual while dead")
            ev["revive"] = s.cmd(".revive")
            ev["after_all"] = _new_ids(g, before)
        log = (P.LOGS / "Server.log").read_text(errors="replace").splitlines()
        ev["autosave_log"] = [l for l in log if f"of {g} skipped" in l][-3:]
    finally:
        S.gm(f".moonglade save interval {config_interval()}")
    require(ev["who_after_trigger"].get("combat"), "GIVEN not met: the fight ended before the trigger was due", **ev)
    require(f"NO\tSAVE\t{REFUSE_COMBAT}" in ev["manual"] and f"NO\tQUICK\t{REFUSE_COMBAT}" in ev["quick"],
            "no combat refusal for the manual save and the quicksave", **ev)
    require(ev["who_dead"].get("dead"), "GIVEN not met: the character didn't die", **ev)
    require(f"NO\tQUICK\t{REFUSE_DEAD}" in ev["dead_quick"] and f"NO\tSAVE\t{REFUSE_DEAD}" in ev["dead_manual"],
            "no while-dead refusal", **ev)
    require(any("Arrived" in l and "You can't save in combat" in l for l in ev["autosave_log"]),
            "the autosave in combat wasn't skipped (no log line)", **ev)
    require(not ev["after_fight"] and not ev["after_all"], "a save folder was created", **ev)
    return ev


def config_interval() -> int:
    conf = (P.SERVER_ETC / "modules" / "mod_moonglade.conf")
    m = re.search(r"^Moonglade\.Saves\.AutoMinIntervalS\s*=\s*(\d+)", conf.read_text(), re.M) if conf.exists() else None
    return int(m.group(1)) if m else 120


def autosave_triggers(ctx):
    """With the autosave interval set short (`.moonglade save interval`), the character enters a city (a rest area),
    takes a flight and teleports to another map, repeatedly, until more than 5 autosaves were triggered: each trigger
    produced one autosave labelled with its moment ("Rested", "Took a flight", "Arrived"); a trigger inside the
    interval produced none; only the 5 newest autosaves remain; a manual save made first was not rotated."""
    from .. import pause as Z
    ev = {"before_writes": S.before_writes()}
    _given_enabled(ev)
    interval = 4
    ev["interval"] = S.gm(f".moonglade save interval {interval}")
    taxi = Z.pick_taxi(S.START_TELE)
    ev["taxi"] = {k: taxi[k] for k in ("from", "to", "path", "rule")} | {"flightmaster": taxi["flightmaster"]["name"]}
    produced = []
    try:
        with S.Session(S.ident().alt) as s:
            g = s.guid
            S.clear_store(g)
            ev["manual"] = s.ask("SAVE manual keep me")
            manual_id = [x for x in S.store_ids(g) if "-manual" in x]
            require(len(manual_id) == 1, "GIVEN not met: the manual save wasn't written", **ev)
            ev["taxicheat"] = s.cmd(".cheat taxi on")

            def autos():
                return sorted(x for x in S.store_ids(g) if "-auto" in x)

            def step(kind, fn):
                before = set(autos())
                fn()
                time.sleep(interval + 1.5)
                new = [x for x in autos() if x not in before]
                labels = [S.manifest(g, x)["label"] for x in new]
                produced.append({"kind": kind, "new": new, "labels": labels})
                return new

            def wait_who(key, want, timeout=8.0):
                t0 = time.time()
                while time.time() - t0 < timeout:
                    w = S.who(g)
                    if w.get(key) == want:
                        return True
                    time.sleep(0.25)
                return False

            def city():
                s.cmd(".tele ElwynnForest")           # out in the open first (no rest flag), then into Stormwind
                ev["not_resting_before_city"] = wait_who("resting", False)
                time.sleep(1.5)                        # the module samples the flag once a second
                s.cmd(".tele Stormwind")
                ev["resting_in_city"] = wait_who("resting", True)

            def other_map():
                s.cmd(".tele Darnassus")

            def back():
                s.cmd(".tele ElwynnForest")

            def flight():
                s.ws.set_selection(g)
                s.cmd(f".modify money {taxi['price'] + 1000}")        # the fare (TaxiPath.dbc price), and some
                fm = taxi["flightmaster"]
                S.go(s, fm["pos"])
                fg = S.npc_guid(s, fm["spawn_id"])
                s.ws.send(S.CMSG_ACTIVATETAXI, __import__("struct").pack("<QII", fg or 0, taxi["from"], taxi["to"]))
                ev["flying"] = wait_who("flying", True)

            # moments, each after the interval, plus one trigger inside the interval
            # (the flight master stands in Stormwind: the flight follows the city, so going there adds no rest edge)
            for kind, fn in (("Rested", city), ("Took a flight", flight), ("Arrived", other_map), ("Arrived", back)):
                step(kind, fn)
            s.cmd(".combatstop")
            # a trigger inside the interval: two map changes within it, only the first saves
            before = set(autos())
            s.cmd(".tele Darnassus")
            time.sleep(1.8)
            s.cmd(".tele ElwynnForest")
            time.sleep(interval + 1.5)
            inside = [x for x in autos() if x not in before]
            produced.append({"kind": "two arrivals inside the interval", "new": inside,
                             "labels": [S.manifest(g, x)["label"] for x in inside]})
            # more until over 5 in total
            for kind, fn in (("Arrived", other_map), ("Arrived", back)):
                step(kind, fn)
            ev["autos_final"] = autos()
            ev["manual_still"] = [x for x in S.store_ids(g) if "-manual" in x]
    finally:
        S.gm(f".moonglade save interval {config_interval()}")
    ev["produced"] = produced
    log = (P.LOGS / "Server.log").read_text(errors="replace").splitlines()
    ev["server_log"] = [l[:160] for l in log if "autosave" in l or "auto save" in l][-30:]
    total = sum(len(p["new"]) for p in produced)
    ev["total_autosaves_triggered"] = total
    require(ev.get("not_resting_before_city") and ev.get("resting_in_city") and ev.get("flying"),
            "GIVEN not met: the server didn't see the character leave and enter a rest area, or fly", **ev)
    single = [p for p in produced if p["kind"] != "two arrivals inside the interval"]
    require(all(len(p["new"]) == 1 and p["labels"][0].startswith(p["kind"] + ":") for p in single),
            "a trigger didn't produce exactly one autosave labelled with its moment", **ev)
    inside = next(p for p in produced if p["kind"] == "two arrivals inside the interval")
    require(len(inside["new"]) == 1, "a trigger inside the interval produced a save (or the first produced none)", **ev)
    require(total > 5 and len(ev["autos_final"]) == 5, "not exactly the 5 newest autosaves remain", **ev)
    every = sorted(x for p in produced for x in p["new"])
    require(ev["autos_final"] == every[-5:], "the remaining autosaves aren't the 5 newest", **ev)
    require(ev["manual_still"] == manual_id, "the manual save was rotated", **ev)
    return ev


def bots_skip(ctx):
    """The saves account's alt logged in as a playerbot companion of its main passes autosave triggers (the main
    changes maps with it in the party, and a trigger fired for the alt itself): no save appears for the alt, and the
    main's own autosave happens as normal."""
    ev = {"before_writes": S.before_writes()}
    _given_enabled(ev)
    interval = 3
    ev["interval"] = S.gm(f".moonglade save interval {interval}")
    alt = _guid(S.ident().alt)
    try:
        with S.Session(S.ident().char) as s:
            g = s.guid
            alt_before = S.store_ids(alt)
            main_before = S.store_ids(g)
            ev["add"] = s.cmd(f".playerbots bot add {S.ident().alt}", settle=2.0, timeout=15)
            t = time.time()
            w = {}
            while time.time() - t < 20:
                w = S.who(alt)
                if w.get("online"):
                    break
                time.sleep(0.5)
            ev["alt_who"] = w
            require(w.get("online") and w.get("bot"), "GIVEN not met: the alt isn't online as a companion", **ev)
            s.after_login()
            ev["tele_main"] = s.cmd(".tele Darnassus")
            time.sleep(2)
            ev["summon_alt"] = s.cmd(f".summon {S.ident().alt}")         # the companion arrives on the main's new map
            time.sleep(interval + 4)
            ev["trigger_alt"] = S.gm(f".moonglade save trigger {alt} Arrived")
            time.sleep(2)
            ev["alt_who_after"] = S.who(alt)
            ev["alt_new"] = _new_ids(alt, alt_before)
            ev["main_new"] = [x for x in _new_ids(g, main_before) if "-auto" in x]
            ev["remove"] = s.cmd(f".playerbots bot remove {S.ident().alt}", settle=2.0, timeout=15)
            s.cmd(".tele ElwynnForest")
    finally:
        S.gm(f".moonglade save interval {config_interval()}")
        S.wait_offline(S.ident().alt, timeout=90)
    require(ev["alt_who_after"].get("map") == 1 and ev["alt_who"].get("map") != 1,
            "GIVEN not met: the companion didn't change maps with its main", **ev)
    require(not ev["alt_new"], "a save appeared for the companion", **ev)
    require("AI players never save" in ev["trigger_alt"], "the trigger for the companion wasn't ignored", **ev)
    require(ev["main_new"], "the main's own autosave didn't happen", **ev)
    return ev


@check("saves.atomic", order=476, capability=CAP, stack="up", layer="integration")
def atomic(ctx):
    """A save paused by the test switch after its first file; the worldserver is killed (SIGKILL) and the stack
    started again: no partial save is listed, the .tmp-* folder is removed at start, and the previous saves still
    load."""
    ev = {"before_writes": S.before_writes()}
    _given_enabled(ev)
    g = _guid(S.ident().alt)
    require(S.wait_offline(S.ident().alt), "GIVEN not met: the alt is online", **ev)
    prev = S.take(g, "before the kill")
    ev["previous"] = prev["id"]
    ev["switch"] = S.gm(".moonglade save test pause 30")
    pid_before = stack.stack_processes()["worldserver"]
    ev["take"] = S.gm(f".moonglade save take {g} killed mid save")
    t = time.time()
    tmp = []
    while time.time() - t < 10 and not tmp:
        tmp = S.store_tmp(g)
        time.sleep(0.1)
    ev["tmp_during_pause"] = tmp
    ev["tmp_files"] = sorted(os.listdir(S.store_dir(g) / tmp[0])) if tmp else None
    require(tmp and ev["tmp_files"] == ["character.json.gz"], "GIVEN not met: no paused save after its first file", **ev)
    for p in pid_before:
        os.kill(int(p["pid"] if isinstance(p, dict) else p), signal.SIGKILL)
    time.sleep(1.5)
    ev["tmp_after_kill"] = S.store_tmp(g)
    ev["up"] = {k: v for k, v in stack.up().items() if k in ("worldserver", "ready_s")}
    ev["tmp_after_start"] = S.store_tmp(g)
    ev["start_log"] = [l for l in (P.LOGS / "Server.log").read_text(errors="replace").splitlines() if "unfinished save" in l][-1:]
    listed = [x["id"] for x in S.server_list(g)]
    ev["listed"] = listed
    ev["load_previous"] = S.load(g, prev["id"])
    require(ev["tmp_after_kill"], "GIVEN not met: the kill didn't leave the .tmp folder", **ev)
    require(not ev["tmp_after_start"], "the .tmp-* folder wasn't removed at start", **ev)
    require(not [x for x in listed if x.startswith(".tmp") or x == tmp[0][5:]], "a partial save is listed", **ev)
    require(prev["id"] in listed and ev["load_previous"]["state"] == "done", "the previous save doesn't load", **ev)
    return ev


# ---------------------------------------------------------------- loading

def _content(guid, ignore=None):
    return S.normalize(S.db_rows(guid), ignore)


IGNORE_AFTER_LOGIN = {}


def roundtrip(ctx):
    """The saves account's hunter with a pet, out of the world, with level, XP, gold, equipped items, items in bags
    and bank, an equipment set, an active quest, a talent, a profession skill and a reputation, and a save of it; it
    then levels up, spends gold, destroys and gains items, abandons the quest, resets talents, gains reputation and
    changes zone; the save is loaded: every save-set row equals the save's content (items, pets and equipment sets by
    content, not id), the position is within 0.01 yards, the character id and name are unchanged, and logged in, the
    server reports the restored level, gold and quest state."""
    ev = {"before_writes": S.before_writes()}
    _given_enabled(ev)
    prep = _run(S.prepare_rich, ev)
    ev["given"] = prep["given"]
    require(all(prep["given"].values()), "GIVEN not met: the hunter lacks part of its progress", prep=prep, **ev)
    g = prep["guid"]
    save = S.take(g, "round trip")
    ev["save"] = {k: save[k] for k in ("id", "level", "money", "zone")}
    saved = S.normalize(S.file_rows(S.store_dir(g) / save["id"]))
    require(saved == _content(g), "GIVEN not met: the save's content isn't the database's", diff=S.diff(saved, _content(g)), **ev)
    # changes
    fac = S.faction_rule(g)
    with S.Session(S.ident().char, tele=None) as s:
        sel = lambda c: (s.ws.set_selection(g), s.cmd(c))[1]       # noqa: E731
        ch = {"levelup": sel(".levelup 2"), "money": sel(".modify money -50000"),
              "quest": S.gm(f"quest remove {S.F.QUEST_ID} {S.ident().char}"), "talents": S.gm(f"reset talents {S.ident().char}"),
              "rep": sel(f".modify reputation {fac['faction']} {S.REP_CHANGED}")}
        bag = [it for it in S.inventory(g) if it["bag"] == 0 and S.INVENTORY_SLOT_ITEM_START <= it["slot"] < 39]
        if bag:
            S.destroy_item(s.ws, S.INVENTORY_SLOT_BAG_0, bag[0]["slot"])
        ch["gain"] = sel(".additem 2589 5")
        ch["tele"] = sel(".tele Stormwind")
        time.sleep(1)
        ch["save"] = s.cmd(".save")
        time.sleep(1)
        ev["changes"] = ch
    require(S.wait_offline(S.ident().char), "the hunter didn't log out", **ev)
    changed = _content(g)
    ev["changed_tables"] = sorted(S.diff(saved, changed))
    require({"characters", "character_queststatus", "character_talent", "character_reputation"} <= set(ev["changed_tables"])
            and {"item_instance", "character_inventory"} & set(ev["changed_tables"]),
            "GIVEN not met: the changes didn't reach the database", **ev)
    ev["load"] = S.load(g, save["id"])
    require(ev["load"]["state"] == "done", "the load didn't finish", **ev)
    after = _content(g)
    d = S.diff(saved, after)
    ev["diff_after_load"] = d
    pos = db.query(f"SELECT CAST(position_x AS DOUBLE), CAST(position_y AS DOUBLE), CAST(position_z AS DOUBLE), name, guid "
                   f"FROM characters WHERE guid={g}", "characters")[0]
    sp = next(r for r in S.file_rows(S.store_dir(g) / save["id"])["characters"]["rows"])
    cols = [c.split(" ")[0] for c in S.file_rows(S.store_dir(g) / save["id"])["characters"]["columns"]]
    dpos = max(abs(float(pos[i]) - float(sp[cols.index(k)])) for i, k in enumerate(("position_x", "position_y", "position_z")))
    ev["position_delta_yd"] = dpos
    require(not d, "save-set rows differ from the save after the load", **ev)
    require(dpos <= 0.01, "the position is off by more than 0.01 yards", **ev)
    require(pos[3] == S.ident().char and int(pos[4]) == g, "the character id or name changed", **ev)
    with S.Session(S.ident().char, tele=None) as s:
        w = S.who(g)
        ev["who"] = w
    require(w.get("level") == save["level"] and w.get("money") == save["money"]
            and w.get("quests", {}).get(S.F.QUEST_ID) is not None, "the server doesn't report the restored level, gold "
            "and quest", **ev)
    return ev


@check("saves.scope", order=481, capability=CAP, stack="up", layer="integration", identity=True)
def scope(ctx):
    """Two characters A and B; a mail in A's mailbox, an auction by A, A on B's friend list, a dungeon lockout of A
    (A entered the Deadmines); with the world paused
    (`.moonglade pause on`, so bots and respawns stand still) a checksum of every table of the character and playerbots
    databases is taken (and taken twice before, to show it's stable); A's save is loaded: B's rows, A's mail, the
    auction and its item, the friend entry, A's instance lockouts and every table outside the save set are
    byte-identical; inside the save set only A's rows (and A's corpse) changed."""
    ev = {"before_writes": S.before_writes()}
    _given_enabled(ev)
    S.ensure()
    a, b = _guid(S.ident().char), _guid(S.ident().alt)
    # test-pyramid: a fresh fixture character has no history; level it so it can enter the Deadmines (min level 10)
    # and post an auction like the long-lived saves character could
    if (S.char_row(S.ident().char) or {}).get("level", 0) < S.RICH_LEVEL:
        ev["level"] = S.gm(f"character level {S.ident().char} {S.RICH_LEVEL}")
    # GIVEN: mail to A, an auction by A, A on B's friend list
    ev["mail"] = S.gm(f'send items {S.ident().char} "hs scope" "hs scope" 2589')
    with S.Session(S.ident().char) as s:
        s.ws.set_selection(a)
        s.cmd(".additem 2589 1")
        s.cmd(".save")
        time.sleep(1)
        # the added linen (a fresh character's starting bag items are soulbound and can't be auctioned)
        bag = [it for it in S.inventory(a) if it["bag"] == 0 and S.INVENTORY_SLOT_ITEM_START <= it["slot"] < 39
               and it["entry"] == 2589]
        require(bag, "GIVEN not met: no bag item to auction", **ev)
        ev["auction"] = S.sell_auction(s, bag[0]["item"])
        s.cmd(".save")
    with S.Session(S.ident().char, tele=None) as s:
        # a real dungeon lockout for A (character_instance): entering the dungeon binds it (game_tele "Deadmines",
        # map 36: Map.dbc InstanceType 1)
        if not db.query(f"SELECT 1 FROM character_instance WHERE guid={a}", "characters"):
            ev["dungeon"] = s.cmd(".tele Deadmines")
            time.sleep(3)
            s.cmd(".tele Stormwind")
            time.sleep(1)
        s.cmd(".save")
    with S.Session(S.ident().alt) as s:
        S.add_friend(s, S.ident().char)
    require(S.wait_offline(S.ident().char) and S.wait_offline(S.ident().alt), "GIVEN not met: characters online", **ev)
    q = lambda sql: int(db.scalar(sql, "characters") or 0)      # noqa: E731
    given = {"mail": q(f"SELECT COUNT(*) FROM mail WHERE receiver={a}"),
             "auction": q(f"SELECT COUNT(*) FROM auctionhouse WHERE itemowner={a}"),
             "friend": q(f"SELECT COUNT(*) FROM character_social WHERE guid={b} AND friend={a}"),
             "lockout": q(f"SELECT COUNT(*) FROM character_instance WHERE guid={a}")}
    ev["given"] = given
    ev["lockout_rows"] = db.query(f"SELECT * FROM character_instance WHERE guid={a}", "characters")
    require(all(given.values()), "GIVEN not met: no mail, auction, friend entry or dungeon lockout", **ev)
    save = S.take(a, "scope")
    with S.Session(S.ident().char, tele=None) as s:
        s.ws.set_selection(a)
        s.cmd(".levelup 1")
        s.cmd(".save")
    require(S.wait_offline(S.ident().char), "A didn't log out", **ev)
    save_tables = {t["name"]: t["where"] for t in S.save_set()}
    ev["pause_on"] = S.gm("moonglade pause on")
    try:
        c0 = checksums(save_tables, a)
        time.sleep(1.0)
        c1 = checksums(save_tables, a)
        ev["stable_without_load"] = diff_checksums(c0, c1)
        ev["load"] = S.load(a, save["id"])
        c2 = checksums(save_tables, a)
    finally:
        ev["pause_off"] = S.gm("moonglade pause off")
    ev["changed_by_load"] = diff_checksums(c1, c2)
    ev["a_level_after"] = int(db.scalar(f"SELECT level FROM characters WHERE guid={a}", "characters"))
    ev["save_level"] = save["level"]
    ev["tables_checked"] = len(c2)
    require(not ev["stable_without_load"], "GIVEN not met: tables change with the world paused and no load", **ev)
    require(ev["load"]["state"] == "done", "the load didn't finish", **ev)
    require(not ev["changed_by_load"], "rows outside A's save-set rows changed", **ev)
    require(ev["a_level_after"] == save["level"], "A's own rows didn't change to the save", **ev)
    after = {k: q(v) for k, v in (("mail", f"SELECT COUNT(*) FROM mail WHERE receiver={a}"),
                                   ("auction", f"SELECT COUNT(*) FROM auctionhouse WHERE itemowner={a}"),
                                   ("friend", f"SELECT COUNT(*) FROM character_social WHERE guid={b} AND friend={a}"),
                                   ("lockout", f"SELECT COUNT(*) FROM character_instance WHERE guid={a}"))}
    ev["after"] = after
    ev["lockout_rows_after"] = db.query(f"SELECT * FROM character_instance WHERE guid={a}", "characters")
    require(after == given and ev["lockout_rows_after"] == ev["lockout_rows"],
            "A's mail, auction, friend entry or dungeon lockout changed", **ev)
    return ev


def checksums(save_tables: dict, a: int) -> dict:
    """{db.table: digest}: every table of the characters and playerbots databases; save-set tables (and corpse)
    without A's rows (their where clause negated, with A's own subqueries)."""
    out = {}
    for dbname in ("characters", "playerbots"):
        schema = P.DB_NAMES[dbname]
        tables = [r[0] for r in db.query(f"SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA='{schema}' "
                                         "AND TABLE_TYPE='BASE TABLE'", dbname)]
        for t in tables:
            if dbname == "characters" and t in save_tables:
                where = "NOT (" + save_tables[t].replace("{g}", str(a)) + ")"
            elif dbname == "characters" and t == "corpse":
                where = f"guid <> {a}"
            else:
                where = "1"
            rows = db.query(f"SELECT * FROM `{t}` WHERE {where}", dbname)
            out[f"{dbname}.{t}"] = hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest()
    return out


def diff_checksums(x: dict, y: dict) -> list[str]:
    return sorted(k for k in set(x) | set(y) if x.get(k) != y.get(k))


def item_ids_fresh(ctx):
    """A save of A holds item X; then A mails X to B; A's save is loaded: A has X again under a new item id, B's mail
    still holds the original X, no primary key is duplicated, the server log shows no item or database error, and
    both characters log in."""
    ev = {"before_writes": S.before_writes()}
    _given_enabled(ev)
    S.ensure()
    a, b = _guid(S.ident().char), _guid(S.ident().alt)
    with S.Session(S.ident().char) as s:
        s.ws.set_selection(a)
        s.cmd(".additem 2589 1")
        s.cmd(".save")
        time.sleep(1)
    bag = [it for it in S.inventory(a) if it["bag"] == 0 and S.INVENTORY_SLOT_ITEM_START <= it["slot"] < 39
           and it["entry"] == 2589]
    require(bag, "GIVEN not met: no item X in A's bags", **ev)
    x = bag[0]
    ev["item_x"] = x
    save = S.take(a, "holds X")
    log_start = (P.LOGS / "Server.log").stat().st_size
    dbe = P.LOGS / "DBErrors.log"
    dbe_start = dbe.stat().st_size if dbe.exists() else 0
    with S.Session(S.ident().char, tele="Stormwind") as s:
        ev["mail"] = S.mail_item(s, S.ident().alt, x["item"])
        s.cmd(".save")
    require(S.wait_offline(S.ident().char), "A didn't log out", **ev)
    in_mail = db.query(f"SELECT mi.mail_id, mi.item_guid, mi.receiver FROM mail_items mi WHERE mi.item_guid={x['item']}",
                       "characters")
    ev["mail_row"] = in_mail
    require(in_mail and int(in_mail[0][2]) == b, "GIVEN not met: X isn't in B's mail", **ev)
    ev["load"] = S.load(a, save["id"])
    inv_after = S.inventory(a)
    xs = [it for it in inv_after if it["entry"] == 2589 and it["slot"] == x["slot"]]
    ev["a_x_after"] = xs
    ev["b_mail_after"] = db.query(f"SELECT mail_id, item_guid, receiver FROM mail_items WHERE item_guid={x['item']}", "characters")
    ev["original_x_row"] = db.query(f"SELECT guid, itemEntry, owner_guid FROM item_instance WHERE guid={x['item']}", "characters")
    log = (P.LOGS / "Server.log").read_bytes()[log_start:].decode(errors="replace").splitlines()
    ev["log_errors"] = [l[:200] for l in log if re.search(r"(?i)error|duplicate", l) and re.search(r"(?i)item|sql|db", l)][:10]
    if not dbe.exists():
        ev["db_errors_new"] = []                       # no DB error was ever logged
    else:
        raw = dbe.read_bytes()
        ev["db_errors_new"] = raw[dbe_start if len(raw) >= dbe_start else 0:].decode(errors="replace").splitlines()[:10]
    ev["db_errors_log_exists"] = dbe.exists()
    ev["duplicate_item_guids"] = {
        "item_instance": db.query("SELECT guid, COUNT(*) FROM item_instance GROUP BY guid HAVING COUNT(*) > 1", "characters"),
        "in_two_inventory_rows": db.query("SELECT item, COUNT(*) FROM character_inventory GROUP BY item HAVING COUNT(*) > 1",
                                          "characters"),
        "both_in_inventory_and_mail": db.query("SELECT ci.item FROM character_inventory ci JOIN mail_items mi "
                                               "ON mi.item_guid = ci.item", "characters")}
    logins = {}
    for n in (S.ident().char, S.ident().alt):
        with S.Session(n, tele=None) as s:
            logins[n] = s.ev.get("login")
    ev["logins"] = logins
    require(ev["load"]["state"] == "done", "the load didn't finish", **ev)
    require(xs and xs[0]["item"] != x["item"], "A doesn't have X again under a new id", **ev)
    require(ev["b_mail_after"] == in_mail and ev["original_x_row"], "B's mail doesn't hold the original X", **ev)
    require(not any(ev["duplicate_item_guids"].values()), "an item id is duplicated", **ev)
    require(not ev["log_errors"] and not ev["db_errors_new"], "the server log or DBErrors.log shows an item or "
            "database error", **ev)
    require(all(logins.values()), "a character didn't log in", **ev)
    return ev


def inworld_load(ctx):
    """A probe character in the world, in combat, with a save from an earlier level, sends `MGSAVE LIST`, then
    `MGSAVE LOAD <generation> <index>` naming that save: it gets SMSG_LOGOUT_COMPLETE within 3 s without a 20 s
    countdown, the next character list shows the saved level and zone, entering the world places it at the saved
    position, and the worldserver pid is unchanged."""
    ev = {"before_writes": S.before_writes()}
    _given_enabled(ev)
    g = _guid(S.ident().alt) or (S.ensure() and _guid(S.ident().alt))
    require(S.wait_offline(S.ident().alt), "GIVEN not met: the alt is online", **ev)
    ev["level0"] = S.gm(f"character level {S.ident().alt} 6")
    S.gm(f"tele name {S.ident().alt} Goldshire")
    save = S.take(g, f"earlier level {int(time.time()) % 1000}")
    ev["save"] = {k: save[k] for k in ("id", "level", "zone")}
    pos = db.query(f"SELECT CAST(position_x AS DOUBLE), CAST(position_y AS DOUBLE), map FROM characters WHERE guid={g}",
                   "characters")[0]
    ev["level1"] = S.gm(f"character level {S.ident().alt} 8")
    pid = stack.stack_processes()["worldserver"]
    with S.Session(S.ident().alt, tele=None) as s:
        ev["fight"] = _run(lambda: S.engage(s, level=8), ev)
        lst = s.list()
        row = next((r for r in lst["rows"] if r["label"] == save["label"]), None)
        ev["list_row"] = row
        require(row and ev["fight"]["who"]["combat"], "GIVEN not met: no listed save or not in combat", **ev)
        since = s.ws.mark()
        t0 = time.time()
        s.send(f"LOAD {lst['gen']} {row['index']}")
        resp = s.ws.wait_event(lambda e: e["kind"] == "logout_response", since, timeout=5)
        done = s.ws.wait_event(lambda e: e["kind"] == "logout_complete", since, timeout=5)
        ev["logout_response"] = resp
        ev["logout_complete_s"] = round(done["t"] - t0, 3) if done else None
        ev["replies"] = [r["line"] for r in s.replies(since)]
        s.ws.stop_pump()
        s.ws.player_guid = None
        c = next(c for c in s.ws.char_enum() if c["guid"] == g)
        ev["char_enum"] = {k: c[k] for k in ("level", "zone", "map", "x", "y")}
        ev["login"] = s.ws.player_login(g)
        S.F.settle_login(s.ws, g)
        s.ws.start_pump()
    ev["pid_after"] = stack.stack_processes()["worldserver"]
    require(done and ev["logout_complete_s"] <= 3 and resp and resp["instant"] == 1,
            "no instant SMSG_LOGOUT_COMPLETE within 3 s", **ev)
    require(c["level"] == save["level"] and c["zone"] == save["zone"], "the character list doesn't show the saved level "
            "and zone", **ev)
    lp = ev["login"]
    require(abs(lp["x"] - float(pos[0])) <= 0.01 and abs(lp["y"] - float(pos[1])) <= 0.01 and lp["map"] == int(pos[2]),
            "entering the world isn't at the saved position", saved_pos=pos, **ev)
    require(ev["pid_after"] == pid, "the worldserver restarted", pid_before=pid, **ev)
    return ev


@check("saves.load_ordering", order=484, capability=CAP, stack="up", layer="integration", targeted=True, identity=True)
def load_ordering(ctx):
    """20 times: the character moves and gains gold (a periodic save pending; in every other round a save of it is
    queued too) and 0.1 s later loads a save in the game: every time the database afterwards holds the save's position and gold, not the pre-load values. The rendered
    CharacterDatabase.WorkerThreads is 1; with the test profile setting it to 2, the module refuses to enable saves
    (logged reason)."""
    ev = {"before_writes": S.before_writes()}
    _given_enabled(ev)
    conf = (P.LANE_ETC / "worldserver.conf").read_text()
    m = re.search(r"^CharacterDatabase\.WorkerThreads\s*=\s*(\d+)", conf, re.M)
    ev["worker_threads"] = int(m.group(1)) if m else None
    require(ev["worker_threads"] == 1, "GIVEN not met: CharacterDatabase.WorkerThreads isn't 1", **ev)
    g = _guid(S.ident().alt)
    require(S.wait_offline(S.ident().alt), "GIVEN not met: the alt is online", **ev)
    S.gm(f"tele name {S.ident().alt} ElwynnForest")
    save = S.take(g, "ordering base")
    sx = db.query(f"SELECT CAST(position_x AS DOUBLE), CAST(position_y AS DOUBLE), money FROM characters WHERE guid={g}",
                  "characters")[0]
    rounds = []
    for i in range(20):
        with S.Session(S.ident().alt, tele=None) as s:
            lst = s.list()
            row = next(r for r in lst["rows"] if r["label"] == save["label"])
            s.ws.set_selection(g)
            s.cmd(".modify money 777", settle=0.1, timeout=2)
            s.ws.heartbeat(float(sx[0]) + 7.0, float(sx[1]) + 7.0, float(s.ev["login"]["z"]))
            if i % 2 == 0:
                s.ws.chat(".save")          # an earlier write of the character queued right before the load
            time.sleep(0.1)
            since = s.ws.mark()
            s.send(f"LOAD {lst['gen']} {row['index']}")
            done = s.ws.wait_event(lambda e: e["kind"] == "logout_complete", since, timeout=5)
            s.ws.stop_pump()
            s.ws.player_guid = None
        t = time.time()
        r = None
        while time.time() - t < 5:
            r = db.query(f"SELECT CAST(position_x AS DOUBLE), CAST(position_y AS DOUBLE), money, online FROM characters "
                         f"WHERE guid={g}", "characters")[0]
            if r[3] == "0":
                break
            time.sleep(0.1)
        time.sleep(0.5)
        r = db.query(f"SELECT CAST(position_x AS DOUBLE), CAST(position_y AS DOUBLE), money FROM characters WHERE guid={g}",
                     "characters")[0]
        bl = sorted(x for x in S.store_ids(g) if "-before-load" in x)
        pre = None
        if bl:
            rows = S.file_rows(S.store_dir(g) / bl[-1])["characters"]
            cols = [c.split(" ")[0] for c in rows["columns"]]
            pre = {k: rows["rows"][0][cols.index(k)] for k in ("money", "position_x", "position_y")}
        # the pre-load values reached the database: the load's own before-load save (read back from the database
        # right before the restore) holds the gold gained and the move
        reached = bool(pre) and int(pre["money"]) == int(sx[2]) + 777 and float(pre["position_x"]) != float(sx[0])
        rounds.append({"i": i, "logout": bool(done), "db": r, "pre_load_in_db": pre, "pre_reached_db": reached,
                       "ok": r == list(sx) and reached})
    ev["save_values"] = sx
    ev["rounds"] = rounds
    require(all(x["pre_reached_db"] for x in rounds), "GIVEN not met: a round's pre-load values never reached the "
            "database", **ev)
    require(all(x["ok"] for x in rounds), f"{sum(not x['ok'] for x in rounds)} of 20 loads left pre-load values", **ev)
    # WorkerThreads = 2: refused (the test profile), then the normal profile again
    # test-pyramid part 2: a worldserver restart with the profile (mysqld and authserver keep running; the setting is
    # the worldserver's own), not the whole stack down and up twice
    try:
        ev["workers2_restart"] = {k: v for k, v in stack.restart_world("saves-workers2").items() if k != "up"}
        ev["workers2_status"] = _status()
        ev["workers2_log"] = [l for l in (P.LOGS / "Server.log").read_text(errors="replace").splitlines()
                              if "saves disabled" in l][-1:]
    finally:
        stack.restart_world()
    require(ev["workers2_status"].startswith("Moonglade saves: disabled") and "WorkerThreads = 2" in ev["workers2_status"]
            and ev["workers2_log"], "with WorkerThreads = 2 the module didn't refuse saves with a logged reason", **ev)
    return ev


def undo(ctx):
    """A character that has just loaded an older save loads its "Before loading" save: its save-set rows equal those
    from before the first load (by content), and at most 3 before-load saves exist for it."""
    ev = {"before_writes": S.before_writes()}
    _given_enabled(ev)
    g = _guid(S.ident().char) or (S.ensure() and _guid(S.ident().char))
    require(S.wait_offline(S.ident().char), "GIVEN not met: the hunter is online", **ev)
    old = S.take(g, "older")
    S.gm(f"character level {S.ident().char} {S.RICH_LEVEL + 3}")
    S.gm(f"tele name {S.ident().char} Stormwind")
    before = _content(g)
    bl_before = {x for x in S.store_ids(g) if "-before-load" in x}
    ev["first_load"] = S.load(g, old["id"])
    bl = sorted(x for x in S.store_ids(g) if "-before-load" in x and x not in bl_before)
    ev["new_before_load"] = bl
    require(ev["first_load"]["state"] == "done" and len(bl) == 1, "GIVEN not met: the first load or its before-load save", **ev)
    ev["undo"] = S.load(g, bl[0])
    after = _content(g)
    ev["diff"] = S.diff(before, after)
    ev["before_load_count"] = len([x for x in S.store_ids(g) if "-before-load" in x])
    require(ev["undo"]["state"] == "done", "the undo load didn't finish", **ev)
    require(not ev["diff"], "the rows aren't those from before the first load", **ev)
    require(ev["before_load_count"] <= 3, "more than 3 before-load saves exist", **ev)
    return ev


@check("saves.load_atomic", order=486, capability=CAP, stack="up", layer="integration")
def load_atomic(ctx):
    """A load with the test switch set to fail its transaction after half its statements: the character's rows are
    identical to before (checksums), the load reports failure, and the before-load save exists."""
    ev = {"before_writes": S.before_writes()}
    _given_enabled(ev)
    g = _guid(S.ident().char)
    require(S.wait_offline(S.ident().char), "GIVEN not met: the hunter is online", **ev)
    old = S.take(g, "atomic")
    S.gm(f"character level {S.ident().char} {S.RICH_LEVEL + 1}")
    nstmt = statements_of(g, old["id"])
    half = max(1, nstmt // 2)
    ev["statements"] = nstmt
    before = S.rows_digest(g)
    ids_before = set(S.store_ids(g))
    ev["switch"] = S.gm(f".moonglade save test failafter {half}")
    try:
        ev["load"] = S.load(g, old["id"])
    finally:
        ev["switch_off"] = S.gm(".moonglade save test off")
    after = S.rows_digest(g)
    ev["changed_tables"] = sorted(k for k in before if before[k] != after.get(k))
    ev["new_saves"] = sorted(set(S.store_ids(g)) - ids_before)
    require(ev["load"]["state"] == "failed", "the load didn't report failure", **ev)
    require(not ev["changed_tables"], "the character's rows changed", **ev)
    require(any("-before-load" in x for x in ev["new_saves"]), "no before-load save was written", **ev)
    return ev


def statements_of(guid: int, sid: str) -> int:
    """How many statements the module's restore of this save has: one delete per removed-on-load and save-set table,
    plus one insert per 200 rows of each table (MoongladeSaves.cpp BuildRestore)."""
    rows = S.file_rows(S.store_dir(guid) / sid)
    sset = S.save_set()
    n = len(S.classification()["removed_on_load"]) + len(sset)
    for t in sset:
        k = len(rows[t["name"]]["rows"])
        n += (k + 199) // 200
    return n


def load_refusals(ctx):
    """One save each that is damaged (one byte changed), of another character, of another account, or with an altered
    schema fingerprint, and the target character online (as itself, and as a companion): each load is refused with its
    own reason, no rows change (checksums), and no before-load save is written for a refused load."""
    ev = {"before_writes": S.before_writes()}
    _given_enabled(ev)
    S.ensure()
    a, b = _guid(S.ident().char), _guid(S.ident().alt)
    require(S.wait_offline(S.ident().char) and S.wait_offline(S.ident().alt), "GIVEN not met: characters online", **ev)
    good = S.take(a, "refusals base")
    other = S.take(b, "other character")
    da, db_ = S.store_dir(a), S.store_dir(b)
    made = []

    def clone(src_dir: Path, sid: str, n: int, edit) -> str:
        new = f"{sid[:15]}-manual-{90 + n}"
        dst = da / new
        shutil.copytree(src_dir / sid, dst)
        m = json.loads((dst / "manifest.json").read_text())
        m["id"] = new
        edit(dst, m)
        (dst / "manifest.json").write_text(json.dumps(m))
        made.append(new)
        return new

    def damage(dst, m):
        raw = bytearray((dst / "character.json.gz").read_bytes())
        raw[len(raw) // 2] ^= 0x01
        (dst / "character.json.gz").write_bytes(bytes(raw))

    def other_account(dst, m):
        m["account"] = P.TEST_USER

    def schema(dst, m):
        m["schema"] = "0" * 64

    cases = {"damaged": clone(da, good["id"], 1, damage),
             "another character": clone(db_, other["id"], 2, lambda d, m: None),
             "another account": clone(da, good["id"], 3, other_account),
             "altered schema": clone(da, good["id"], 4, schema)}
    want = {"damaged": "That save is damaged.", "another character": "That save belongs to another character.",
            "another account": "That save belongs to another account.",
            "altered schema": "This save was made by a different version of Moonglade.",
            "online as itself": "That character is in the world.", "online as a companion": "That character is in the world."}
    results = {}
    try:
        for label, sid in cases.items():
            before, ids = S.rows_digest(a), set(S.store_ids(a))
            r = S.load(a, sid)
            results[label] = {"result": r, "rows_changed": S.rows_digest(a) != before,
                              "new_saves": sorted(set(S.store_ids(a)) - ids)}
        with S.Session(S.ident().char, tele=None) as s:
            before, ids = S.rows_digest(a), set(S.store_ids(a))
            r = S.load(a, good["id"])
            results["online as itself"] = {"result": r, "rows_changed": S.rows_digest(a) != before,
                                           "new_saves": sorted(set(S.store_ids(a)) - ids)}
            s.cmd(f".playerbots bot add {S.ident().alt}", settle=2.0, timeout=15)
            t = time.time()
            while time.time() - t < 20 and not S.who(b).get("online"):
                time.sleep(0.5)
            alt_b = S.take(b, "alt offline save") if False else other
            before_b, ids_b = S.rows_digest(b), set(S.store_ids(b))
            r = S.load(b, other["id"])
            results["online as a companion"] = {"result": r, "who": S.who(b), "rows_changed": S.rows_digest(b) != before_b,
                                                "new_saves": sorted(set(S.store_ids(b)) - ids_b)}
            s.cmd(f".playerbots bot remove {S.ident().alt}", settle=2.0, timeout=15)
        S.wait_offline(S.ident().alt, timeout=90)
    finally:
        for x in made:
            shutil.rmtree(da / x, ignore_errors=True)
    ev["results"] = results
    for label, res in results.items():
        require(res["result"]["state"] == "refused" and res["result"]["why"] == want[label],
                f"{label}: not refused with its own reason", **ev)
        require(not res["rows_changed"] and not res["new_saves"], f"{label}: rows changed or a save was written", **ev)
    require(results["online as a companion"]["who"].get("bot"), "GIVEN not met: the alt wasn't online as a companion", **ev)
    return ev


# ---------------------------------------------------------------- lists

def inworld_list(ctx):
    """A probe character with manual, quick, auto and before-load saves sends `MGSAVE LIST`: one reply line per save,
    newest first, each at most 255 bytes, with kind, label, time, level and zone, and the same set the store holds."""
    ev = {"before_writes": S.before_writes()}
    _given_enabled(ev)
    g = _guid(S.ident().alt)
    require(S.wait_offline(S.ident().alt), "GIVEN not met: the alt is online", **ev)
    m = S.take(g, "list manual")
    ev["before_load"] = S.load(g, m["id"])
    S.gm(".moonglade save interval 1")
    try:
        with S.Session(S.ident().alt) as s:
            s.ask("QUICK")
            s.after_login()
            s.cmd(".tele Darnassus")
            time.sleep(3)
            s.cmd(".tele ElwynnForest")
            time.sleep(1)
            lst = s.list()
    finally:
        S.gm(f".moonglade save interval {config_interval()}")
    store = sorted((S.manifest(g, x) for x in S.store_ids(g) if not x.startswith(".tmp-")),
                   key=lambda x: (x["created"], x["id"]), reverse=True)
    ev["kinds_in_store"] = sorted({x["kind"] for x in store})
    ev["lines"] = lst["lines"]
    rows = lst["rows"]
    lens = [len(("MGSAVE\t" + l).encode()) for l in lst["lines"]]
    ev["line_bytes_max"] = max(lens) if lens else None
    require(set(ev["kinds_in_store"]) >= {"manual", "quick", "auto", "before-load"}, "GIVEN not met: kinds missing", **ev)
    require(len(rows) == len(store), "not one line per save", **ev)
    require([(r["kind"], r["label"], int(r["level"])) for r in rows] == [(x["kind"], x["label"], x["level"]) for x in store],
            "the lines aren't the store's saves, newest first", **ev)
    require(all(r["when"] and r["zone"] for r in rows) and max(lens) <= 255, "a line lacks time or zone, or is over 255 "
            "bytes", **ev)
    return ev


def list_wording(ctx):
    """The generated save list for the glue (charsaves.render for the saves account's store) and the add-on's reply
    lines: no save id, path, nonce or password appears; no label contains "server", "realm", "account" or "database";
    the glue file is written 0600 inside a play client only (refused for client/)."""
    from .. import charsaves, frontmenu
    ev = {}
    g = _guid(S.ident().alt)
    content, index = charsaves.build([S.ident().user])
    lua = charsaves.render(content, 3).decode()
    with S.Session(S.ident().alt) as s:
        lines = s.list()["lines"]
    ids = [x for x in S.store_ids(g) if not x.startswith(".tmp-")]
    ev["saves"] = len(index)
    ev["lines"] = len(lines)
    texts = {"glue": lua, "addon": "\n".join(lines)}
    secrets_ = [S.ident().password, P.TEST_PASS, str(P.SAVES), str(P.ROOT)] + ids
    ev["leaks"] = {k: [x for x in secrets_ if x and x in v] for k, v in texts.items()}
    labels = [r["label"] for c in content["chars"] for r in c["saves"]] + [l.split("\t")[-1] for l in lines if l.startswith("E\t")]
    ev["bad_labels"] = [l for l in labels if any(w in l.lower() for w in FORBIDDEN_WORDS)]
    # the file: 0600 in a play client, refused for client/
    tmp = P.ENV / "tmp" / "saves-wording-client"
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / "Data" / "enUS").mkdir(parents=True)
    (tmp / "hs-build.json").write_text("{}")
    try:
        w = frontmenu.write(tmp, [{"name": "X", "account": "X", "password": "x"}], saves_lua=lua.encode())
        ev["write"] = {k: w[k] for k in ("archive", "mode")}
        ev["rewrite"] = frontmenu.rewrite_saves(tmp, lua.encode())
        try:
            frontmenu.rewrite_saves(P.CLIENT, lua.encode())
            ev["client_refused"] = False
        except frontmenu.FrontMenuError as e:
            ev["client_refused"] = str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    require(ev["saves"] and ev["lines"], "GIVEN not met: no saves in the test store", **ev)
    require(not any(ev["leaks"].values()), "a save id, path or password appears", **ev)
    require(not ev["bad_labels"], "a label names a server, realm, account or database", **ev)
    require(ev["write"]["mode"] == "0o600" and ev["client_refused"], "the glue file isn't 0600 or client/ isn't refused", **ev)
    return ev


# ---------------------------------------------------------------- the player's saves

def store_fingerprint(root: Path) -> dict | None:
    from ..backups import fingerprint
    return fingerprint(root)


@check("saves.player_untouched", order=997, capability=CAP, stack="any", layer="integration")
def player_untouched(ctx):
    """env/saves/<PLAYER>/ is identical (names, sizes, sha256) at the end of the run to its start, the PLAYER
    characters' save-set rows are byte-identical to the baseline taken before this run's first save or load, and lane
    B's save store is its own (its rendered Moonglade.Saves.Dir is under env/lanes/b/, never env/saves/).
    (test-pyramid part 3: a pr run no longer runs gate checks before the run guards, so the stack is often down here;
    the guard then starts mysqld alone for its reads and stops it again.)"""
    started = not stack.stack_processes()["mysqld"]
    if started:
        stack.mysql_up()
    try:
        return _player_untouched(ctx)
    finally:
        if started:
            stack.mysql_down()


def _player_untouched(ctx):
    acct = S.player_account()
    ev = {"player": bool(acct)}
    root = P.PLAYER_SAVES / acct if acct else None
    start = (ctx or {}).get("player_saves_fingerprint_start", "missing")
    now = store_fingerprint(root) if root else None
    ev["store_start_entries"] = None if start in (None, "missing") else len(start)
    ev["store_now_entries"] = None if now is None else len(now)
    require(start != "missing", "GIVEN not met: the run didn't record the player's saves at its start", **ev)
    require(start == now, "the player's saves changed during the run", **ev)
    base = S._PLAYER_BASE.get("rows")
    ev["rows_baseline_taken"] = base is not None
    if base is not None:
        cur = S.player_rows()
        ev["player_characters"] = len(cur)
        ev["changed"] = sorted(f"{g}:{t}" for g in cur for t in cur[g] if base.get(g, {}).get(t) != cur[g][t])
        require(set(cur) == set(base) and not ev["changed"], "the PLAYER characters' rows changed", **ev)
    lb = P.layout("b")
    ev["lane_b_root"] = str(lb["root"])
    env_file = lb["etc"] / config.LANE_ENV_FILE
    if env_file.exists():
        env = json.loads(env_file.read_text())
        ev["lane_b_saves_dir"] = env.get(config.env_var_name("Moonglade.Saves.Dir"))
        require(ev["lane_b_saves_dir"] and str(lb["root"]) in ev["lane_b_saves_dir"]
                and str(P.PLAYER_SAVES) not in ev["lane_b_saves_dir"].replace(str(lb["root"]), ""),
                "lane B's save store isn't its own", **ev)
    return ev


# ---------------------------------------------------------------- the add-on

# the base client's archives, lowest priority first (the client's own load order for Data/*.MPQ: common, common-2,
# expansion, lichking, patch, patch-2, patch-3); the last one holding a file wins
BASE_ARCHIVES = ("common.MPQ", "common-2.MPQ", "expansion.MPQ", "lichking.MPQ", "patch.MPQ", "patch-2.MPQ", "patch-3.MPQ")


def default_bindings(client: Path = P.CLIENT) -> dict:
    """WTF\\DefaultBindings.wtf as the installed client has it (its highest-priority base archive with the file),
    read-only: {"archive", "binds": {key: action}}."""
    from .. import mpq
    found = None
    for name in BASE_ARCHIVES:
        a = Path(client) / "Data" / name
        if not a.exists():
            continue
        with mpq.Archive(a) as arc:
            if arc.find("WTF\\DefaultBindings.wtf") is not None:
                found = (name, arc.read("WTF\\DefaultBindings.wtf"))
    if not found:
        return {"archive": None, "binds": {}}
    binds = {}
    for line in found[1].decode("latin-1").splitlines():
        m = re.match(r"^bind (\S+) (\S+)", line.strip())
        if m:
            binds[m.group(1)] = m.group(2)
    return {"archive": f"Data/{found[0]}", "binds": binds}


def addon_report(addon: Path, binds: dict) -> dict:
    """What the add-on declares (pure, for counterexamples): bindings, default keys and whether they are free, the game
    menu button, and the add-on's files in the TOC and in patch 0003."""
    bx = (addon / "Bindings.xml").read_text() if (addon / "Bindings.xml").exists() else ""
    lua = (addon / "MoongladeSaves.lua").read_text() if (addon / "MoongladeSaves.lua").exists() else ""
    toc = (addon / "Moonglade.toc").read_text() if (addon / "Moonglade.toc").exists() else ""
    names = re.findall(r'<Binding\s+name="(\w+)"', bx)
    m = re.search(r"DEFAULT_KEYS = \{ MOONGLADE_QUICKSAVE = \"(\S+?)\", MOONGLADE_QUICKLOAD = \"(\S+?)\" \}", lua)
    keys = {"MOONGLADE_QUICKSAVE": m.group(1), "MOONGLADE_QUICKLOAD": m.group(2)} if m else {}
    menu = re.search(r'CreateFrame\("Button", "GameMenuButtonMoongladeSaves", GameMenuFrame, "GameMenuButtonTemplate"\)', lua)
    patch = json.loads((P.ROOT / "patches" / "client" / "0003-wording-addon.json").read_text())["files"]
    return {"bindings": names, "default_keys": keys,
            "keys_taken_by_client": {k: binds.get(v) for k, v in keys.items() if binds.get(v)},
            "menu_button": bool(menu) and 'menuButton:SetText(TEXT.title)' in lua and 'title = "Saves"' in lua,
            "toc_lists_lua": "MoongladeSaves.lua" in toc.splitlines(),
            "patch_files": sorted(patch)}


@check("saves.addon_static", order=469, capability=CAP, stack="any", layer="unit", parallel_safe=True)
def addon_static(ctx):
    """The Moonglade add-on's files: Bindings.xml declares quicksave and quickload; their default keys are bound to
    nothing in the installed client's own default bindings (WTF\\DefaultBindings.wtf from its base archives, cited in
    the evidence); the game menu (Esc) gets a "Saves" button; the TOC and patch 0003 carry the files."""
    b = default_bindings()
    ev = {"default_bindings": b["archive"], "client_binds_read": len(b["binds"])}
    rep = addon_report(P.ROOT / "client-mods" / "addons" / "Moonglade", b["binds"])
    ev |= rep
    ev["client_binds_for_skyrim_keys"] = {k: b["binds"].get(k) for k in ("F5", "F9")}
    require(b["archive"] and b["binds"], "the client's default bindings couldn't be read", **ev)
    require({"MOONGLADE_QUICKSAVE", "MOONGLADE_QUICKLOAD"} <= set(rep["bindings"]), "Bindings.xml lacks quicksave or "
            "quickload", **ev)
    require(len(rep["default_keys"]) == 2 and not rep["keys_taken_by_client"], "a default key is taken in the client's "
            "default bindings (or none are set)", **ev)
    require(rep["menu_button"], "the game menu has no Saves button", **ev)
    want = {f"Interface/AddOns/Moonglade/{f}" for f in ("Moonglade.toc", "Moonglade.lua", "MoongladeSaves.lua", "Bindings.xml")}
    require(rep["toc_lists_lua"] and want <= set(rep["patch_files"]), "the TOC or patch 0003 lacks the add-on's files", **ev)
    return ev


# ---------------------------------------------------------------- clustered sessions (test-pyramid design 13 rule 3)
# Each session runs its scenarios one after another for the fixture's identity (ctx["identity"], set as the saves
# identity by the fixture); each scenario keeps its id, status and evidence. Character-scoped sessions may run two at
# a time; the interval session changes the server-wide autosave interval (each scenario restores it), so it runs alone.

@check("saves.session_a", order=471, capability=CAP, stack="up", layer="integration", scope="character",
       children=('saves.manual', 'saves.quick_slot', 'saves.inworld_load', 'saves.undo', 'saves.list_wording'))
def session_a(ctx):
    """One identity: a manual save, the quicksave slot, a load in the game, its undo, and the list wording, in order
    (the scenarios saves.manual, saves.quick_slot, saves.inworld_load, saves.undo and saves.list_wording)."""
    return run_children(ctx, [("saves.manual", manual), ("saves.quick_slot", quick_slot), ("saves.inworld_load", inworld_load), ("saves.undo", undo), ("saves.list_wording", list_wording)])


@check("saves.session_b", order=472, capability=CAP, stack="up", layer="integration", scope="character",
       children=('saves.roundtrip', 'saves.item_ids_fresh', 'saves.load_refusals'))
def session_b(ctx):
    """One identity: the round trip of a rich character, item ids after a load, and the refused loads (the scenarios
    saves.roundtrip, saves.item_ids_fresh and saves.load_refusals)."""
    return run_children(ctx, [("saves.roundtrip", roundtrip), ("saves.item_ids_fresh", item_ids_fresh), ("saves.load_refusals", load_refusals)])


@check("saves.session_interval", order=473, capability=CAP, stack="up", layer="integration", targeted=True, identity=True,
       children=('saves.refused_in_combat', 'saves.autosave_triggers', 'saves.bots_skip', 'saves.inworld_list'))
def session_interval(ctx):
    """The scenarios that shorten the server-wide autosave interval (each restores it, and registers the restore as
    an undo): refused in combat, autosave triggers, companions skip, and the in-game list."""
    ctx.setdefault("undo", []).append(_restore_interval)
    return run_children(ctx, [("saves.refused_in_combat", refused_in_combat), ("saves.autosave_triggers", autosave_triggers), ("saves.bots_skip", bots_skip), ("saves.inworld_list", inworld_list)])


def _restore_interval():
    S.gm(f".moonglade save interval {config_interval()}")
