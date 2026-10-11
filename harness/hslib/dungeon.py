"""Dungeon driver for the scale.* and lock.* checks (openspec/changes/m4-dungeon-scaling, design.md §5-6), reused by
m4-dungeon-companions.

A headless probe logs in the dungeon checks' own character and types what a player (or a GM, for the shortcuts)
would type. Never the PLAYER account, never HSTEST and never the companion checks' Hscomp:

  account    P.DUNGEON_USER, created through the server (SOAP `account create`) at security level 2, like HSCOMP
             (companion.py): .go/.npc info/.gps/.damage/.revive, the playerbots master commands (addclass, `bot self`,
             `initself`), `.ab mapstat|creaturestat` (SEC_PLAYER) and instant logout.
  character  P.DUNGEON_CHAR, human warrior, created with the probe's CMSG_CHAR_CREATE; its level is set offline to the
             dungeon's target level (`.character level` over SOAP) before each session.

Every dungeon fact comes from DBC or the world DB by the rules in design.md §6 (FACT_RULES, copied into evidence);
nothing about a dungeon is hard-coded except the LFG ids of the listed dungeons (proposal table).

Findings from prototype 1.1 that shape this module (design.md "Prototype results"):
- `.die` gives no encounter credit (Die.Command.Mode = 1, Unit.cpp:13554); `.damage <current health>` does.
- `.npc info <spawn>` reports the selected creature first, so the selection is cleared before a spawn read.
- `.gps` on a unit the probe can't see returns the probe's own position.
- AutoBalance counts players when they enter or leave the map (AddPlayerToMap), so GM mode only matters at entry.
- A dead selfbot releases its spirit by itself (graveyard outside the dungeon)."""
import math
import re
import struct
import time

from . import companion as C, db, dbc, fixtures as F, paths as P, soap
from .probe import client as probe
from .probe import proto as W

GM_LEVEL = 2
START_TELE = C.START_TELE     # where each session starts (outside every dungeon)

# The listed dungeons (proposal "Listed dungeons"): LFGDungeons.dbc ids. Everything else is read by rule.
DEADMINES, RAZORFEN_DOWNS, RAMPARTS, UTGARDE_KEEP = 6, 20, 136, 202
UTGARDE_KEEP_HEROIC = 242
LISTED = (DEADMINES, RAZORFEN_DOWNS, RAMPARTS, UTGARDE_KEEP)

# LFGDungeons.dbc field indexes (DBCStructure.h LFGDungeonEntry, DBCfmt.h:82)
LFG_MIN, LFG_MAX, LFG_TARGET, LFG_MAP, LFG_DIFF, LFG_TYPE = 18, 19, 20, 23, 24, 26
# DungeonEncounter.dbc field indexes (DBCfmt.h:47): id, mapId, difficulty, orderIndex, encounterIndex (the mask bit)
DE_MAP, DE_DIFF, DE_ORDER, DE_BIT = 1, 2, 3, 4

FACT_RULES = {
    "levels_and_map": "LFGDungeons.dbc row by ID: MinLevel [18], MaxLevel [19], Target_Level [20], MapID [23], "
                      "Difficulty [24], TypeID [26]",
    "encounters": "DungeonEncounter.dbc rows with mapId [1] = the map and difficulty [2] = the row's, by orderIndex "
                  "[3]; bit = encounterIndex [4]; joined to world.instance_encounters (entry = encounter id) for "
                  "creditType, creditEntry, lastEncounterDungeon",
    "final_boss": "the instance_encounters row whose lastEncounterDungeon = this LFG id",
    "spawns": "world.creature rows with id = creditEntry on the map whose spawnMask has the difficulty's bit",
    "entrance": "the world.areatrigger_teleport row with target_map = the map (lowest ID)",
}


class DungeonError(C.CompanionError):
    pass


# ----------------------------------------------------------------------------- facts by rule (design.md §6)

def facts(lfg_id: int) -> dict:
    recs, _ = dbc.read_dbc("LFGDungeons.dbc")
    r = next((r for r in recs if r[0] == lfg_id), None)
    if r is None:
        raise DungeonError(f"LFGDungeons.dbc has no row {lfg_id}")
    f = {"lfg_id": lfg_id, "min_level": r[LFG_MIN], "max_level": r[LFG_MAX], "target_level": r[LFG_TARGET],
         "map": r[LFG_MAP], "difficulty": r[LFG_DIFF], "type": r[LFG_TYPE], "rules": FACT_RULES}
    enc, _ = dbc.read_dbc("DungeonEncounter.dbc")
    rows = sorted((e for e in enc if e[DE_MAP] == f["map"] and e[DE_DIFF] == f["difficulty"]), key=lambda e: e[DE_ORDER])
    out = []
    for e in rows:
        ie = db.query(f"SELECT creditType, creditEntry, lastEncounterDungeon FROM instance_encounters "
                      f"WHERE entry={e[0]}", "world")
        ct, ce, last = map(int, ie[0]) if ie else (None, None, None)
        spawns = db.query(
            f"SELECT c.guid, c.position_x, c.position_y, c.position_z, ct.name, ct.minlevel, ct.`rank` "
            f"FROM creature c JOIN creature_template ct ON ct.entry = c.id WHERE c.id = {ce} AND c.map = {f['map']} "
            f"AND (c.spawnMask & {1 << f['difficulty']}) ORDER BY c.guid", "world") if ce else []
        out.append({"encounter": e[0], "order": e[DE_ORDER], "bit": e[DE_BIT], "credit_type": ct, "credit_entry": ce,
                    "last": last, "spawns": [{"spawn_id": int(g), "x": float(x), "y": float(y), "z": float(z),
                                              "name": n, "level": int(lv), "rank": int(rk)}
                                             for g, x, y, z, n, lv, rk in spawns]})
    f["encounters"] = out
    finals = [e for e in out if e["last"] == lfg_id]
    if len(finals) != 1:
        raise DungeonError(f"LFG {lfg_id}: expected one instance_encounters row with lastEncounterDungeon = it, "
                           f"found {len(finals)}", facts=f)
    f["final"] = finals[0]
    at = db.query(f"SELECT ID, target_position_x, target_position_y, target_position_z FROM areatrigger_teleport "
                  f"WHERE target_map = {f['map']} ORDER BY ID LIMIT 1", "world")
    if not at:
        raise DungeonError(f"no areatrigger_teleport into map {f['map']}", facts=f)
    i, x, y, z = at[0]
    f["entrance"] = {"trigger": int(i), "x": float(x), "y": float(y), "z": float(z)}
    return f


def first_boss(f: dict) -> dict:
    """The first encounter (by orderIndex) with a static spawn: the 'first boss' of the lock.* scenarios."""
    e = next((e for e in f["encounters"] if e["spawns"] and e is not f["final"]), None)
    if not e:
        raise DungeonError("no non-final encounter with a static spawn", facts=f)
    return e


def unscaled_max_health(entry: int, level: int) -> dict:
    """The health the core gives a creature with no module involved (scale.disabled_stock):
    ceil(BaseHealth[exp] x HealthModifier) (CreatureData.h CreatureBaseStats::GenerateHealth; BaseHealth =
    creature_classlevelstats.basehp<exp> for unit_class and level), times the rank's Rate.Creature.*.HP from the rendered
    worldserver.conf (Creature::_GetHealthMod): era.creature_max_hp / era.ac_base_hp / era.hp_rate."""
    from . import era as E
    cls, exp, mod, rank = db.query(f"SELECT unit_class, exp, HealthModifier, `rank` FROM creature_template "
                                   f"WHERE entry = {int(entry)}", "world")[0]
    base = E.ac_base_hp(level, int(cls), int(exp))
    return {"health": E.creature_max_hp(base, float(mod), int(rank)), "base_hp": base, "health_modifier": float(mod),
            "rank": int(rank), "exp": int(exp), "unit_class": int(cls), "level": level, "hp_rate": E.hp_rate(int(rank)),
            "rule": "ceil(creature_classlevelstats.basehp<exp> x creature_template.HealthModifier) x the rank's "
                    "Rate.Creature.*.HP (era.creature_max_hp)"}


def within(a: float | None, b: float | None, rel: float = 0.01) -> bool:
    return a is not None and b is not None and b != 0 and abs(a - b) <= rel * abs(b)


# ----------------------------------------------------------------------------- account + character

def account_id() -> int | None:
    v = db.scalar(f"SELECT id FROM account WHERE username='{P.DUNGEON_USER}'", "auth")
    return int(v) if v else None


def char_row() -> dict | None:
    rows = db.query(f"SELECT guid, account, level, online, map FROM characters WHERE name='{P.DUNGEON_CHAR}'",
                    "characters")
    if not rows:
        return None
    g, a, lv, on, m = rows[0]
    return {"guid": int(g), "account": int(a), "level": int(lv), "online": int(on), "map": int(m)}


def ensure_account() -> dict:
    ev = {}
    if account_id() is None:
        ev["create"] = C.gm(f"account create {P.DUNGEON_USER} {P.DUNGEON_PASS}").evidence()
        _, ok, _ = F.poll(account_id, bool, timeout=15)
        if not ok:
            raise DungeonError("dungeon account not in auth.account after `account create`", **ev)
    acct = account_id()
    access = F.access_rows(acct)
    if access != [[str(GM_LEVEL), "-1"]]:
        ev["gmlevel"] = C.gm(f"account set gmlevel {P.DUNGEON_USER} {GM_LEVEL} -1").evidence()
        access, ok, _ = F.poll(lambda: F.access_rows(acct), lambda v: v == [[str(GM_LEVEL), "-1"]], timeout=15)
        if not ok:
            raise DungeonError("dungeon account security level not set", access=access, **ev)
    return ev | {"account": P.DUNGEON_USER, "id": acct, "account_access": access}


def ensure_character() -> dict:
    if char_row():
        return {"character": P.DUNGEON_CHAR, "created": False}
    _, ws, _ = probe.open_world(P.DUNGEON_USER, P.DUNGEON_PASS)
    with ws:
        code = ws.char_create(P.DUNGEON_CHAR, F.RACE_HUMAN, F.CLASS_WARRIOR)
        if code != W.CHAR_CREATE_SUCCESS:
            raise DungeonError("CMSG_CHAR_CREATE rejected", result=f"0x{code:02X} {W.RESPONSE_NAMES.get(code, '?')}")
    _, ok, _ = F.poll(char_row, bool, timeout=15)
    if not ok:
        raise DungeonError("dungeon character not in characters DB after CHAR_CREATE_SUCCESS")
    return {"character": P.DUNGEON_CHAR, "created": True}


def prepare_offline(level: int, tele: str | None = START_TELE) -> dict:
    """Level (and, unless tele is None, place) the offline character through the server."""
    _, ok, _ = F.poll(lambda: char_row()["online"], lambda v: v == 0, timeout=30)
    if not ok:
        raise DungeonError("dungeon character still online; offline preparation needs it logged out", row=char_row())
    ev = {}
    if char_row()["level"] != level:
        ev["level"] = C.gm(f"character level {P.DUNGEON_CHAR} {level}").evidence()
        _, ok, _ = F.poll(lambda: char_row()["level"], lambda v: v == level, timeout=15)
        if not ok:
            raise DungeonError("dungeon character level not saved", **ev)
    if tele:
        t = F.tele_row(tele)
        ev["tele"] = C.gm(f"tele name {P.DUNGEON_CHAR} {t['name']}").evidence()
        ev["tele_row"] = t
    return ev


# ----------------------------------------------------------------------------- parsers

def parse_mapstat(lines: list[str]) -> dict:
    """`.ab mapstat` (ABCommandScript.cpp HandleABMapStatsCommand): '<name> (<n>-player Normal|Heroic) | ID <map>-<id>',
    'Players on map: <n> (Lvl ..)', 'Adjusted Player Count: <n>', 'World health multiplier: <x>'."""
    t = "\n".join(lines)
    i = re.search(r"\| ID (\d+)-(\d+)", t)
    pl = re.search(r"Players on map: (\d+)", t)
    adj = re.search(r"Adjusted Player Count: (\d+)", t)
    wh = re.search(r"World health multiplier: ([\d.]+)", t)
    return {"map": int(i.group(1)) if i else None, "instance": int(i.group(2)) if i else None,
            "players": int(pl.group(1)) if pl else None, "adjusted": int(adj.group(1)) if adj else None,
            "world_health_multiplier": float(wh.group(1)) if wh else None, "disabled": "AutoBalance DISABLED" in t,
            "raw": lines[:12]}


def parse_creaturestat(lines: list[str]) -> dict:
    """`.ab creaturestat` (HandleABCreatureStatsCommand): 'Creature difficulty level: <n> player(s)', 'Health
    multiplier: <x>' (or 'Health multiplier: <x> -> <y>' with level scaling), 'Damage multiplier: <x>'."""
    t = "\n".join(lines)
    h = re.search(r"Health multiplier: ([\d.]+)(?: -> ([\d.]+))?", t)
    d = re.search(r"Damage multiplier: ([\d.]+)", t)
    lv = re.search(r"Creature difficulty level: (\d+)", t)
    return {"health_multiplier": float(h.group(2) or h.group(1)) if h else None,
            "damage_multiplier": float(d.group(1)) if d else None,
            "player_count": int(lv.group(1)) if lv else None, "raw": lines[:14]}


# ----------------------------------------------------------------------------- the session

class Session(C.Session):
    """The dungeon probe in the world (companion.Session's helpers, on P.DUNGEON_USER / P.DUNGEON_CHAR). On exit the
    companions are removed, GM mode and the selfbot are switched off and the probe logs out."""

    def __init__(self, level: int, tele: str | None = START_TELE):
        super().__init__(level=level, tele=tele or START_TELE)
        self.place = tele
        self.selfbot = False

    def __enter__(self):
        self.ev["account"] = ensure_account()
        self.ev["character"] = ensure_character()
        self.ev["prepare"] = prepare_offline(self.level, self.place)
        _, ws, _ = probe.open_world(P.DUNGEON_USER, P.DUNGEON_PASS)
        self.ws = ws
        try:
            chars = ws.char_enum()
            c = next((c for c in chars if c["name"].lower() == P.DUNGEON_CHAR.lower()), None)
            if not c:
                raise DungeonError("dungeon character not in char enum", enum=chars)
            self.guid = c["guid"]
            self.ev["login"] = ws.player_login(self.guid)
            self.ev["login_map"] = c.get("map")
            self.ev["settle"] = F.settle_login(ws, self.guid)
            ws.start_pump()
            self.ev["gm_off"] = ws.command(".gm off")
            self.ev["revive"] = self.revive_if_dead()
            self.ev["stale_group"] = self.leave_stale_group()
            if self.place:
                # normal difficulty unless a check asks for heroic: a killed heroic check can't leave it set
                self.ev["difficulty"] = self.set_difficulty(0)
        except Exception:
            ws.close()
            raise
        return self

    def __exit__(self, *exc):
        if self.ws:
            try:
                if self.selfbot:
                    self.cmd(".playerbots bot self", settle=0.5)
                self.cmd(".gm off", settle=0.3)
            except Exception as e:      # noqa: BLE001  best-effort; logout below
                self.ev.setdefault("cleanup_errors", []).append(str(e))
        super().__exit__(*exc)

    def revive_if_dead(self) -> dict:
        pinfo = soap.gm(f"pinfo {P.DUNGEON_CHAR}").output
        alive = re.search(r"Alive \?: (\w+)", pinfo)
        ev = {"alive_before": alive.group(1) if alive else None}
        if alive and alive.group(1) != "Yes":
            ev["revive"] = self.ws.command(".revive", settle=0.5, timeout=5)
            alive = re.search(r"Alive \?: (\w+)", soap.gm(f"pinfo {P.DUNGEON_CHAR}").output)
            ev["alive_after"] = alive.group(1) if alive else None
            if ev["alive_after"] != "Yes":
                raise DungeonError("dungeon probe is dead and `.revive` didn't bring it back", **ev)
        return ev

    def alive(self, name: str | None = None) -> str | None:
        m = re.search(r"Alive \?: (\w+)", soap.gm(f"pinfo {name or P.DUNGEON_CHAR}").output)
        return m.group(1) if m else None

    # -- commands and movement
    def cmd(self, c: str, settle: float = 0.6, timeout: float = 8) -> list[str]:
        return self.ws.command(c, settle=settle, timeout=timeout)

    def go(self, x: float, y: float, z: float, map_id: int | None = None, timeout: float = 20) -> dict:
        """`.go xyz` (cs_go.cpp HandleGoXYZCommand); waits for SMSG_NEW_WORLD (another map) or the near teleport, and
        returns any SMSG_TRANSFER_ABORTED that came instead."""
        mark = self.ws.mark()
        reply = self.cmd(f".go xyz {x:.2f} {y:.2f} {z:.2f}" + (f" {map_id}" if map_id is not None else ""), settle=0.4)
        e = self.ws.wait_event(lambda e: e["kind"] in ("new_world", "teleport_near", "transfer_aborted"), mark,
                               timeout=timeout)
        if e and e["kind"] != "transfer_aborted":
            time.sleep(1.0)
            self.ws.heartbeat(e["x"], e["y"], e["z"])
        return {"reply": reply, "event": e}

    def enter(self, f: dict) -> dict:
        """Enter the dungeon as a player would arrive (GM mode off: AutoBalance and the creatures ignore GMs): `.go xyz`
        to the entrance area trigger's target (design.md §6). Returns {ok, event, instance, mapstat}."""
        self.ev.setdefault("gm_off_before_enter", []).append(self.cmd(".gm off", settle=0.3))
        e = f["entrance"]
        self.entrance = e
        r = self.go(e["x"], e["y"], e["z"], f["map"])
        ev = r["event"]
        ok = bool(ev and ev["kind"] == "new_world" and ev["map"] == f["map"])
        out = {"ok": ok, "event": ev, "reply": r["reply"]}
        if ok:
            ms = self.mapstat()
            out |= {"instance": ms["instance"], "mapstat": ms}
        return out

    def leave(self) -> dict:
        t = F.tele_row(START_TELE)
        r = self.go(t["x"], t["y"], t["z"], t["map"])
        ok = bool(r["event"] and r["event"]["kind"] == "new_world" and r["event"]["map"] == t["map"])
        return {"ok": ok, "event": r["event"], "to": t["name"]}

    def set_difficulty(self, mode: int) -> dict:
        """MSG_SET_DUNGEON_DIFFICULTY (the client's difficulty menu; HandleSetDungeonDifficultyOpcode), outside."""
        self.ws.send(W.MSG_SET_DUNGEON_DIFFICULTY, struct.pack("<I", mode))
        time.sleep(1.0)
        return {"sent": mode}

    # -- reads
    def mapstat(self) -> dict:
        return parse_mapstat(self.cmd(".ab mapstat", settle=0.5))

    def unit(self, spawn_id: int, entry: int | None = None) -> dict:
        """`.npc info <spawn>` with no selection (it reports the selection first); an entry mismatch voids the read."""
        self.ws.set_selection(0)
        time.sleep(0.25)
        i = self.npc_info(spawn_id)
        m = re.search(r"Entry: (\d+)", "\n".join(i.get("raw") or []))
        i["entry"] = int(m.group(1)) if m else None
        if entry and i["entry"] != entry:
            i["entry_mismatch"] = True
            i["health"] = i["max_health"] = None
        return i

    def creaturestat(self, guid: int) -> dict:
        self.ws.set_selection(guid)
        time.sleep(0.3)
        out = parse_creaturestat(self.cmd(".ab creaturestat", settle=0.5))
        self.ws.set_selection(0)
        return out

    def boss_near(self, enc: dict, n_players: int | None = None, timeout: float = 20) -> dict:
        """Read the boss from close by: AutoBalance rescales a creature only on that creature's own update
        (ABAllCreatureScript OnAllCreatureUpdate), which runs only in grids near a player. The companions are told to
        `stay`, the probe goes there in GM mode (not attacked; the map's count was fixed at entry) to READ_NEAR_YD from
        the boss toward the entrance, waits until the boss's `.ab creaturestat` player count is n_players (default:
        `.ab mapstat`'s), reads, and comes back to where it was."""
        back = self.gps()
        for n in list(self.companions):
            self.ws.chat("stay", W.CHAT_MSG_WHISPER, to=n)
        sp = enc["spawns"][0]
        want = n_players if n_players is not None else self.mapstat()["players"]
        self.cmd(".gm on", settle=0.3)
        ent = getattr(self, "entrance", None) or {"x": sp["x"] + 1, "y": sp["y"]}
        dx, dy = ent["x"] - sp["x"], ent["y"] - sp["y"]
        d = math.hypot(dx, dy) or 1.0
        self.go(sp["x"] + dx / d * READ_NEAR_YD, sp["y"] + dy / d * READ_NEAR_YD, sp["z"] + 1)
        t0 = time.time()
        while True:
            b = self.boss(enc)
            if (b["creaturestat"] or {}).get("player_count") == want or time.time() - t0 > timeout:
                break
            time.sleep(1)
        b["rescale_wait_s"] = round(time.time() - t0, 1)
        b["want_players"] = want
        if "x" in back:
            self.go(back["x"], back["y"], back["z"])
        self.cmd(".gm off", settle=0.3)
        for n in list(self.companions):
            self.ws.chat("follow", W.CHAT_MSG_WHISPER, to=n)
        return b

    def read_selected_boss(self, guid: int, entry: int, tries: int = 3) -> dict:
        """`.npc info` and `.ab creaturestat` on a creature by GUID (a summoned boss has no spawn id to read by). The
        self-AI may change the probe's selection between the two commands, so the selection is set again right before
        each, up to `tries` times until both read."""
        info, stat, m = {}, {}, None
        for _ in range(tries):
            self.ws.set_selection(guid)
            time.sleep(0.3)
            info = C.parse_npc_info(self.cmd(".npc info", settle=0.5))
            m = re.search(r"Entry: (\d+)", "\n".join(info.get("raw") or []))
            self.ws.set_selection(guid)
            time.sleep(0.3)
            stat = parse_creaturestat(self.cmd(".ab creaturestat", settle=0.5))
            if m and info.get("max_health") and stat.get("health_multiplier"):
                break
        return {"guid": guid, "entry": int(m.group(1)) if m else None, "max_health": info.get("max_health"),
                "health_multiplier": stat.get("health_multiplier"), "creaturestat": stat,
                "ok": bool(m and int(m.group(1)) == entry and info.get("max_health") and stat.get("health_multiplier"))}

    def boss(self, enc: dict) -> dict:
        """Live max health and AutoBalance multiplier of an encounter's first spawn."""
        sp = enc["spawns"][0]
        u = self.unit(sp["spawn_id"], enc["credit_entry"])
        stat = self.creaturestat(u["guid"]) if u.get("guid") else {}
        return {"spawn_id": sp["spawn_id"], "entry": u.get("entry"), "guid": u.get("guid"),
                "max_health": u.get("max_health"), "health": u.get("health"), "in_combat": u.get("in_combat"),
                "pos": u.get("pos"), "creaturestat": stat}

    def binds(self, map_id: int | None = None, guid: int | None = None) -> list[dict]:
        """characters.character_instance + instance rows of a character (default: the probe)."""
        g = (guid or self.guid) & 0xFFFFFFFF
        rows = db.query(f"SELECT ci.instance, ci.permanent, i.map, i.difficulty, i.completedEncounters "
                        f"FROM character_instance ci JOIN instance i ON i.id = ci.instance WHERE ci.guid = {g}"
                        + (f" AND i.map = {int(map_id)}" if map_id is not None else ""), "characters")
        return [{"instance": int(a), "permanent": int(b), "map": int(c), "difficulty": int(d), "mask": int(e)}
                for a, b, c, d, e in rows]

    def wait_binds(self, pred, map_id: int, guid: int | None = None, timeout: float = 15) -> list[dict]:
        v, _ok, _ = F.poll(lambda: self.binds(map_id, guid), pred, timeout=timeout, interval=0.3)
        return v

    def unbind(self, map_id: int) -> dict:
        """`.instance unbind <map>` on the probe itself (cs_instance.cpp HandleInstanceUnbindCommand: the selected player,
        else self; every difficulty; never the map the player is in), so the next entry makes a fresh copy."""
        self.ws.set_selection(0)
        time.sleep(0.2)
        out = self.cmd(f".instance unbind {int(map_id)}", settle=0.5)
        left = self.wait_binds(lambda v: not v, map_id)
        return {"reply": out, "binds_after": left, "ok": not left}

    # -- shortcuts (lockout checks only)
    def kill(self, enc: dict, map_id: int | None = None) -> dict:
        """Kill an encounter's boss with encounter credit: `.damage <current health>` on it (security 2; `.die` alone
        gives no credit: Unit::Kill rewards only when players dealt enough of the damage, Unit.cpp:13554). The probe is
        in GM mode while it moves (not attacked); AutoBalance counts players at map entry, so this doesn't change the
        scaling. With map_id, success is the encounter's bit in the probe's saved mask; a boss whose script refuses a
        lethal hit (Ingvar the Plunderer: DamageTaken stops at 1 HP and starts a resurrection phase) gets the fallback:
        GM mode off with `.cheat god on` (it engages the probe, which can't die), `.damage <health - 2>` (players dealt
        the damage) and `.die` (Unit::Kill, which skips the script's DamageTaken and rewards)."""
        sp = enc["spawns"][0]
        self.cmd(".gm on", settle=0.3)
        self.go(sp["x"], sp["y"], sp["z"] + 1)
        ev = {"boss": sp["name"], "spawn_id": sp["spawn_id"], "hits": []}
        bit = 1 << enc["bit"]
        try:
            for k in range(KILL_TRIES):
                u = self.unit(sp["spawn_id"], enc["credit_entry"])
                hp = u.get("health")
                if k and hp is not None and hp <= 1:
                    time.sleep(2)          # still feigning / evading: wait for a normal state
                    continue
                if u.get("guid") and hp:
                    self.ws.set_selection(u["guid"])
                    time.sleep(0.3)
                    if k == 0:
                        ev["hits"].append({"health": hp, "reply": self.cmd(f".damage {hp}", settle=0.5)})
                    else:
                        if "god" not in ev:
                            ev["god"] = self.cmd(".cheat god on", settle=0.3)
                            self.cmd(".gm off", settle=0.3)
                            time.sleep(1.0)
                            continue
                        ev["hits"].append({"health": hp, "reply": self.cmd(f".damage {max(1, hp - 2)}", settle=0.3),
                                           "die": self.cmd(".die", settle=0.3)})
                    self.ws.set_selection(0)
                if map_id is None:
                    _, ok, _ = F.poll(lambda: self.unit(sp["spawn_id"]).get("health"), lambda h: h == 0, timeout=10)
                    ev["dead"] = ok
                    break
                rows, ok, _ = F.poll(lambda: self.binds(map_id), lambda v: bool(v and v[0]["mask"] & bit),
                                     timeout=8 if k == 0 else 3)
                ev["dead"] = ok
                if ok:
                    break
        finally:
            self.ws.set_selection(0)
            if "god" in ev:
                self.cmd(".cheat god off", settle=0.3)
            self.cmd(".gm off", settle=0.3)
        ev["probe_alive"] = self.alive()
        if ev["probe_alive"] != "Yes":
            ev["revive"] = self.cmd(".revive", settle=0.3)
        return ev

    # -- companions
    def add(self, cls: str, spec: str | None = None, gear: bool = False) -> dict:
        a = self.addclass(cls)
        name = a["member"]["name"]
        out = {"name": name, "guid": a["bot"]["guid"], "class": cls, "addclass": {k: a[k] for k in ("reply", "elapsed_s")}}
        if spec:
            out["spec"] = self.whisper(name, f"talents spec {spec}", wait=1.5)
        if gear:
            out["autogear"] = self.whisper(name, "autogear", wait=2.0)
        return out

    def whisper(self, name: str, msg: str, wait: float = 1.0) -> list[str]:
        mark = self.ws.mark()
        self.ws.chat(msg, W.CHAT_MSG_WHISPER, to=name)
        time.sleep(wait)
        return [e["msg"] for e in self.ws.events_since(mark) if e["kind"] == "chat" and e["type"] == W.CHAT_MSG_WHISPER]

    def gather(self, n_expected: int, timeout: float = 45) -> dict:
        """Whisper `summon` to each companion (playerbots SummonAction; the GM .summon refuses non-GM targets into an
        instance, cs_misc.cpp) until AutoBalance counts n_expected players on the map."""
        t0, tries, ms = time.time(), 0, {}
        while time.time() - t0 < timeout:
            for n in list(self.companions):
                self.ws.chat("summon", W.CHAT_MSG_WHISPER, to=n)
            tries += 1
            time.sleep(4)
            ms = self.mapstat()
            if ms.get("players") == n_expected:
                return {"ok": True, "s": round(time.time() - t0, 1), "players": ms["players"], "tries": tries}
        return {"ok": False, "s": round(time.time() - t0, 1), "players": ms.get("players"), "tries": tries,
                "mapstat": ms}


# ----------------------------------------------------------------------------- solo final boss (scale.solo_final_boss)

UNIT_FIELD_HEALTH = 6 + 0x12      # UpdateFields.h: OBJECT_END + 0x0012
UNIT_FIELD_MAXHEALTH = 6 + 0x1A   # OBJECT_END + 0x001A
UNIT_FIELD_TARGET = 6 + 0x0C      # OBJECT_END + 0x000C, 2 fields (low, high): the unit the probe's self-AI attacks


def guid_entry(guid: int) -> int:
    """ObjectGuid::GetEntry for a creature GUID (HighGuid::Unit 0xF130): bits 24-47."""
    return (guid >> 24) & 0xFFFFFF if (guid >> 48) == 0xF130 else 0
READ_NEAR_YD = 25                 # boss_near: close enough for an active grid, outside the usual aggro reach
KILL_TRIES = 12                   # `.damage` rounds per boss (second phases) before giving up
SOLO_STAND_YD = 6                 # GM teleport this far from the boss toward the entrance (inside its aggro radius)
SOLO_ATTEMPTS = 2                 # spec: at most 2 attempts (a wipe resets the boss)
SOLO_LIMIT_S = 600                # spec: within 10 minutes
GEAR_QUALITY = {2: "uncommon", 3: "rare", 4: "epic"}   # PlayerbotMgr.cpp `initself=<quality>`
SOLO_RULE = ("placement: GM-on teleport to the final boss's spawn moved SOLO_STAND_YD (6) yd toward the entrance "
             "(2D), at the spawn's height + 1 (an event boss with no static spawn of its credit entry: the static spawn "
             "of the creature whose script summons it, ground height by `.go xyz x y`); then GM off and the playerbots "
             "self-AI fights. Gear: `.playerbots bot initself=<AiPlayerbot.AutoGearQualityLimit>` (playerbots.conf).")


def gear_quality() -> str:
    from . import era as E
    v = E.rendered_option(P.SERVER_ETC / "modules" / "playerbots.conf", "AiPlayerbot.AutoGearQualityLimit")
    return GEAR_QUALITY.get(int(v) if v and v.isdigit() else 3, "rare")


# Event bosses: where the fight starts when the final boss has no static spawn. Hellfire Ramparts: Vazruden the Herald
# summons Vazruden and Nazan only after the two Hellfire Sentries it summons at Reset die (AzerothCore
# src/server/scripts/Outland/HellfireCitadel/HellfireRamparts/boss_vazruden_the_herald.cpp, Reset() and
# SummonedCreatureDies()); the solo probe stands at the first sentry's summon point from that file, so the sentries
# engage it. Keyed by the final encounter's credit entry (instance_encounters.creditEntry).
EVENT_STAGE = {17537: {"x": -1372.56, "y": 1724.31, "z": 82.967,
                       "source": "boss_vazruden_the_herald.cpp Reset(): SummonCreature(NPC_HELLFIRE_SENTRY, -1372.56f, "
                                 "1724.31f, 82.967f)"}}


def solo_anchor(f: dict) -> dict:
    """Where the solo fight is staged: the final encounter's static spawn, or, for an event-summoned final boss, the
    static spawn of the creature that summons it (Hellfire Ramparts: Vazruden the Herald summons Vazruden; found by
    rule: a creature_template on the map whose name ends with the credit creature's name and has a static spawn)."""
    fin = f["final"]
    if fin["spawns"]:
        return fin["spawns"][0] | {"event": False}
    name = db.scalar(f"SELECT name FROM creature_template WHERE entry = {fin['credit_entry']}", "world")
    rows = db.query(f"SELECT c.guid, c.position_x, c.position_y, c.position_z, ct.name, ct.entry FROM creature c JOIN "
                    f"creature_template ct ON ct.entry = c.id WHERE c.map = {f['map']} AND ct.name LIKE "
                    f"'{C._q(name)} %' AND (c.spawnMask & {1 << f['difficulty']}) ORDER BY c.guid LIMIT 1", "world")
    if not rows:
        raise DungeonError(f"no static spawn for the event boss {name}", final=fin)
    g, x, y, z, n, e = rows[0]
    out = {"spawn_id": int(g), "x": float(x), "y": float(y), "z": float(z), "name": n, "entry": int(e), "event": True,
           "rule": "creature on the map named '<credit creature name> ...' with a static spawn"}
    stage = EVENT_STAGE.get(fin["credit_entry"])
    if stage:
        out |= {"stage": stage}
    return out


def stand_point(anchor: dict, entrance: dict, yd: float = SOLO_STAND_YD) -> dict:
    if anchor.get("stage"):
        st = anchor["stage"]
        return {"x": st["x"], "y": st["y"], "z": st["z"] + 1}
    dx, dy = entrance["x"] - anchor["x"], entrance["y"] - anchor["y"]
    d = math.hypot(dx, dy) or 1.0
    return {"x": anchor["x"] + dx / d * yd, "y": anchor["y"] + dy / d * yd, "z": None if anchor["event"] else anchor["z"] + 1}


def solo_final_boss(s: "Session", f: dict, log=None) -> dict:
    """The probe (already at the target level, outside) alone with the self-AI against the final boss of a fresh copy.
    Returns the evidence: per attempt the fight length, the boss's scaled health, the probe's lowest health, deaths;
    and whether the final encounter's bit is set."""
    say = log or (lambda *a: None)
    ev = {"rule": SOLO_RULE, "anchor": solo_anchor(f), "attempts": []}
    ev["unbind"] = s.unbind(f["map"])
    q = gear_quality()
    ev["initself"] = {"quality": q, "reply": s.cmd(f".playerbots bot initself={q}", settle=1.5)}
    s.cmd(".revive", settle=0.3)
    e = s.enter(f)
    ev["enter"] = {k: e.get(k) for k in ("ok", "instance")}
    if not e["ok"]:
        return ev | {"killed": False, "why": "didn't enter"}
    ev["self_on"] = s.cmd(".playerbots bot self", settle=1.0)
    s.selfbot = any("Enable player botAI" in l for l in ev["self_on"])
    if not s.selfbot:
        return ev | {"killed": False, "why": "selfbot not enabled"}
    bit = 1 << f["final"]["bit"]
    t_start = time.time()
    fin = f["final"]
    for k in range(SOLO_ATTEMPTS):
        a = {"n": k + 1}
        ev["attempts"].append(a)
        stand = stand_point(ev["anchor"], f["entrance"])
        s.cmd(".gm on", settle=0.3)
        r = s.go(stand["x"], stand["y"], stand["z"] if stand["z"] is not None else ev["anchor"]["z"])
        if stand["z"] is None:
            s.cmd(f".go xyz {stand['x']:.2f} {stand['y']:.2f}", settle=0.4)
        a["stand"] = s.gps()
        if fin["spawns"]:
            a["boss_before"] = s.boss(fin)
            st = a["boss_before"].get("creaturestat") or {}
            if a["boss_before"].get("max_health") and st.get("health_multiplier"):
                ev.setdefault("boss_scaled", {"max_health": a["boss_before"]["max_health"],
                                              "health_multiplier": st["health_multiplier"], "how": "static spawn, before the pull"})
        mark = s.ws.mark()
        s.cmd(".gm off", settle=0.3)
        t0 = time.time()
        a["trace"], low, maxhp = [], None, None
        while time.time() - t_start < SOLO_LIMIT_S:
            time.sleep(2)
            for x in s.ws.events_since(mark, "self_values"):
                fl = x["fields"]
                if UNIT_FIELD_MAXHEALTH in fl:
                    maxhp = fl[UNIT_FIELD_MAXHEALTH]
                if UNIT_FIELD_HEALTH in fl:
                    low = fl[UNIT_FIELD_HEALTH] if low is None else min(low, fl[UNIT_FIELD_HEALTH])
            u = s.unit(fin["spawns"][0]["spawn_id"], fin["credit_entry"]) if fin["spawns"] else {}
            if not fin["spawns"] and "boss_scaled" not in ev:
                # a summoned final boss: found as the self-AI's target (the probe's own UNIT_FIELD_TARGET) once its
                # GUID's entry is the encounter's credit entry, then read by GUID. The self-AI changes the probe's
                # selection on its own (a read once found `.ab creaturestat` answering "You should select a
                # creature" right after `.npc info` had read the boss), so every target GUID seen so far is retried
                # on every poll until one read is complete.
                lo = hi = 0
                seen = []
                for x in s.ws.events_since(mark, "self_values"):
                    fl = x["fields"]
                    if UNIT_FIELD_TARGET in fl or UNIT_FIELD_TARGET + 1 in fl:
                        lo, hi = fl.get(UNIT_FIELD_TARGET, lo), fl.get(UNIT_FIELD_TARGET + 1, hi)
                        g = (hi << 32) | lo
                        if g and g not in seen and guid_entry(g) == fin["credit_entry"]:
                            seen.append(g)
                for g in seen:
                    r = s.read_selected_boss(g, fin["credit_entry"])
                    a.setdefault("boss_reads", []).append(r)
                    if r["ok"]:
                        ev["boss_scaled"] = {"max_health": r["max_health"],
                                             "health_multiplier": r["health_multiplier"],
                                             "how": "summoned boss, the self-AI's target, read by GUID"}
                        u = {"health": None, "max_health": r["max_health"]}
                        break
            rows = s.binds(f["map"])
            mask = rows[0]["mask"] if rows else 0
            alive = s.alive()
            a["trace"].append({"t": round(time.time() - t0), "boss_hp": u.get("health"), "boss_max": u.get("max_health"),
                               "mask": mask, "probe_alive": alive, "probe_low": low})
            say(a["trace"][-1])
            if mask & bit:
                a["killed_after_s"] = round(time.time() - t0, 1)
                break
            if alive != "Yes":
                a["probe_died_after_s"] = round(time.time() - t0, 1)
                break
        evs = s.ws.events_since(mark)
        for x in s.ws.events_since(0, "self_values"):
            if UNIT_FIELD_MAXHEALTH in x["fields"]:
                maxhp = x["fields"][UNIT_FIELD_MAXHEALTH]
        a["probe_low_health"] = low
        a["probe_max_health"] = maxhp
        a["boss_scaled_max"] = next((t["boss_max"] for t in a["trace"] if t.get("boss_max")), None)
        a.pop("_tgt_lo", None)
        a.pop("_tgt_hi", None)
        a["probe_hits_taken"] = sum(1 for x in evs if x["kind"] in ("melee_damage", "spell_damage") and x["victim"] == s.guid)
        a["probe_damage_taken"] = sum(x["damage"] for x in evs if x["kind"] in ("melee_damage", "spell_damage")
                                      and x["victim"] == s.guid)
        a["trace"] = a["trace"][::3] + a["trace"][-2:]
        if a.get("killed_after_s") is not None or time.time() - t_start >= SOLO_LIMIT_S:
            break
        # a wipe: the selfbot releases to the graveyard outside; revive, wait for the boss to reset, go back in
        time.sleep(15)
        a["revive"] = s.cmd(".revive", settle=0.5)
        a["reenter"] = {k: v for k, v in s.enter(f).items() if k in ("ok", "instance")}
    rows = s.binds(f["map"])
    ev["final_mask"] = rows[0]["mask"] if rows else None
    ev["killed"] = bool(rows and rows[0]["mask"] & bit)
    ev["deaths"] = sum(1 for a in ev["attempts"] if a.get("probe_died_after_s") is not None)
    ev["total_s"] = round(time.time() - t_start, 1)
    if fin["spawns"]:
        u = s.unit(fin["spawns"][0]["spawn_id"])
        ev["boss_stat"] = s.creaturestat(u["guid"]) if u.get("guid") else None
    s.cmd(".playerbots bot self", settle=0.8)
    s.selfbot = False
    s.cmd(".revive", settle=0.3)
    return ev
