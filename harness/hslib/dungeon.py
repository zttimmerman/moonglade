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

# The dungeon checks' identity; the run driver's navmesh pass uses NAV (design.md §4 GM permissions: `.mmap path` is
# security 3 in the command table, the dungeon account is 2, so a separate level-3 harness account does the path
# queries, never PLAYER).
DUNGEON_ID = {"user": P.DUNGEON_USER, "password": P.DUNGEON_PASS, "char": P.DUNGEON_CHAR, "gmlevel": GM_LEVEL}
NAV_ID = {"user": P.DUNGEON_NAV_USER, "password": P.DUNGEON_NAV_PASS, "char": P.DUNGEON_NAV_CHAR, "gmlevel": 3}


def account_id(who: dict = DUNGEON_ID) -> int | None:
    v = db.scalar(f"SELECT id FROM account WHERE username='{who['user']}'", "auth")
    return int(v) if v else None


def char_row(who: dict = DUNGEON_ID) -> dict | None:
    rows = db.query(f"SELECT guid, account, level, online, map FROM characters WHERE name='{who['char']}'",
                    "characters")
    if not rows:
        return None
    g, a, lv, on, m = rows[0]
    return {"guid": int(g), "account": int(a), "level": int(lv), "online": int(on), "map": int(m)}


def ensure_account(who: dict = DUNGEON_ID) -> dict:
    ev = {}
    lvl = who["gmlevel"]
    if account_id(who) is None:
        ev["create"] = C.gm(f"account create {who['user']} {who['password']}").evidence()
        _, ok, _ = F.poll(lambda: account_id(who), bool, timeout=15)
        if not ok:
            raise DungeonError("dungeon account not in auth.account after `account create`", **ev)
    acct = account_id(who)
    access = F.access_rows(acct)
    if access != [[str(lvl), "-1"]]:
        ev["gmlevel"] = C.gm(f"account set gmlevel {who['user']} {lvl} -1").evidence()
        access, ok, _ = F.poll(lambda: F.access_rows(acct), lambda v: v == [[str(lvl), "-1"]], timeout=15)
        if not ok:
            raise DungeonError("dungeon account security level not set", access=access, **ev)
    return ev | {"account": who["user"], "id": acct, "account_access": access}


def ensure_character(who: dict = DUNGEON_ID) -> dict:
    if char_row(who):
        return {"character": who["char"], "created": False}
    _, ws, _ = probe.open_world(who["user"], who["password"])
    with ws:
        code = ws.char_create(who["char"], F.RACE_HUMAN, F.CLASS_WARRIOR)
        if code != W.CHAR_CREATE_SUCCESS:
            raise DungeonError("CMSG_CHAR_CREATE rejected", result=f"0x{code:02X} {W.RESPONSE_NAMES.get(code, '?')}")
    _, ok, _ = F.poll(lambda: char_row(who), bool, timeout=15)
    if not ok:
        raise DungeonError("dungeon character not in characters DB after CHAR_CREATE_SUCCESS")
    return {"character": who["char"], "created": True}


def prepare_offline(level: int, tele: str | None = START_TELE, who: dict = DUNGEON_ID) -> dict:
    """Level (and, unless tele is None, place) the offline character through the server."""
    _, ok, _ = F.poll(lambda: char_row(who)["online"], lambda v: v == 0, timeout=30)
    if not ok:
        raise DungeonError("dungeon character still online; offline preparation needs it logged out", row=char_row(who))
    ev = {}
    if char_row(who)["level"] != level:
        ev["level"] = C.gm(f"character level {who['char']} {level}").evidence()
        _, ok, _ = F.poll(lambda: char_row(who)["level"], lambda v: v == level, timeout=15)
        if not ok:
            raise DungeonError("dungeon character level not saved", **ev)
    if tele:
        t = F.tele_row(tele)
        ev["tele"] = C.gm(f"tele name {who['char']} {t['name']}").evidence()
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

    def __init__(self, level: int, tele: str | None = START_TELE, who: dict = DUNGEON_ID):
        super().__init__(level=level, tele=tele or START_TELE)
        self.place = tele
        self.selfbot = False
        self.who = who

    def __enter__(self):
        who = self.who
        self.ev["account"] = ensure_account(who)
        self.ev["character"] = ensure_character(who)
        self.ev["prepare"] = prepare_offline(self.level, self.place, who)
        _, ws, _ = probe.open_world(who["user"], who["password"])
        self.ws = ws
        try:
            chars = ws.char_enum()
            c = next((c for c in chars if c["name"].lower() == who["char"].lower()), None)
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
        pinfo = soap.gm(f"pinfo {self.who['char']}").output
        alive = re.search(r"Alive \?: (\w+)", pinfo)
        ev = {"alive_before": alive.group(1) if alive else None}
        if alive and alive.group(1) != "Yes":
            ev["revive"] = self.ws.command(".revive", settle=0.5, timeout=5)
            alive = re.search(r"Alive \?: (\w+)", soap.gm(f"pinfo {self.who['char']}").output)
            ev["alive_after"] = alive.group(1) if alive else None
            if ev["alive_after"] != "Yes":
                raise DungeonError("dungeon probe is dead and `.revive` didn't bring it back", **ev)
        return ev

    def alive(self, name: str | None = None) -> str | None:
        m = re.search(r"Alive \?: (\w+)", soap.gm(f"pinfo {name or self.who['char']}").output)
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
            # poll the map's count for up to 4 s per round of whispers (was a fixed 4 s wait; design 16.3)
            t_try = time.time()
            while True:
                time.sleep(0.5)
                ms = self.mapstat()
                if ms.get("players") == n_expected:
                    return {"ok": True, "s": round(time.time() - t0, 1), "players": ms["players"], "tries": tries}
                if time.time() - t_try >= 4:
                    break
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


# ============================================================================ run driver (m4-dungeon-companions §4)
# Prototype (tasks.md 1.1, docs/research/m4-full-run.md): the companion party clears a dungeon's required encounters
# boss by boss. Positions come from the world DB (boss spawns, the entrance) and the core's path query; nothing is
# hand-placed.

STAGE_BACK_YD = 30            # design.md §4: the boss spawn moved 30 yd back toward the previous staging point
STAGE_OFF_LINE_YD = 10        # design.md §4: a navmesh point farther than this from the candidate is refused
STAGE_MIN_BOSS_YD = 25        # design.md §4 fallback: at least this far from the boss
STAGE_WALK_MAX_YD = 45        # prototype: the line's point is refused when it is farther than this on foot (1.5 x 30)
# `.mmap path` prints only the path's start, end and actual end (cs_mmaps.cpp HandleMmapPathCommand); the points
# themselves are summoned as VISUAL_WAYPOINT creatures (MotionMaster.h, entry 1; 9 s timed despawn), which the probe
# reads from their create blocks (probe.client.parse_update_blocks, track_creates).
VISUAL_WAYPOINT = 1
WAYPOINT_WAIT_S = 2.5         # the waypoints' create packets after the command (or after the probe's teleport)
PATH_NORMAL, PATH_INCOMPLETE, PATH_NOPATH = 0x01, 0x04, 0x08     # PathGenerator.h enum PathType
GATHER_YD = 15                # design.md §4: everyone within 15 yd
GATHER_S = 60                 # design.md §4: or 60 s
QUIET_S = 5                   # "out of combat": no melee/spell damage event involving a party member for this long
FIGHT_S = 300                 # one pull, at most
STALL_S = 60                  # a pull with no fight involving the boss after this long is abandoned
MAX_WIPES = 2                 # design.md §4: max 2 wipes, then FAIL
RESET_S = 90                  # a wiped-on boss evades and heals back to full within this
POLL_S = 1.0
SOLO_PULL_YD = 6              # the selfbot run: stand this far from the boss, inside its aggro radius (SOLO_STAND_YD)
RUN_RULE = ("order: required encounters (a static spawn of the credit entry; m4-dungeon-scaling design §6) by "
            "DungeonEncounter.dbc orderIndex. Staging: the boss spawn moved 30 yd back toward the previous staging "
            "point (the entrance for the first), snapped by `.mmap path` (the boss's path to the candidate; its "
            "ActualEndPosition) and kept if within 10 yd of the candidate and <= 45 yd from the boss on foot; else the boss's `.mmap path` to the "
            "previous staging point, read point by point (its VISUAL_WAYPOINT summons), and the last point on it that "
            "is >= 25 yd from the boss (walking from the previous staging point). Pull: the probe selects the boss, "
            "whispers `tank attack` and `attack` to the tank. Wipe: every fighter dead; `.revive`, back to staging, "
            "wait for the boss's reset, retry; max 2 wipes.")


def required_encounters(f: dict) -> tuple[list[dict], list[dict]]:
    """(required, skipped): encounters with a static spawn of their credit entry, by orderIndex; the rest are reported
    as skipped (an event-summoned boss, m4-dungeon-scaling design.md §6)."""
    req = sorted((e for e in f["encounters"] if e["spawns"]), key=lambda e: e["order"])
    skipped = [{"encounter": e["encounter"], "credit_entry": e["credit_entry"], "order": e["order"],
                "why": "skipped: no static spawn"} for e in f["encounters"] if not e["spawns"]]
    return req, skipped


def dist3(a: dict, b: dict) -> float:
    return math.sqrt((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2 + (a["z"] - b["z"]) ** 2)


def stage_candidate(boss: dict, prev: dict, back: float = STAGE_BACK_YD) -> dict:
    """The boss spawn moved `back` yd (2D) along the straight line toward `prev`; the height follows the line
    (linear between the two, held at prev's beyond it)."""
    dx, dy = prev["x"] - boss["x"], prev["y"] - boss["y"]
    d = math.hypot(dx, dy)
    if d < 1e-6:
        return {"x": boss["x"], "y": boss["y"], "z": boss["z"]}
    t = back / d
    return {"x": boss["x"] + dx * t, "y": boss["y"] + dy * t, "z": boss["z"] + (prev["z"] - boss["z"]) * min(t, 1.0)}


_V3 = r"\((-?[\d.]+(?:e-?\d+)?), (-?[\d.]+(?:e-?\d+)?), (-?[\d.]+(?:e-?\d+)?)\)"


def parse_mmap_path(lines: list[str]) -> dict:
    """`.mmap path` (cs_mmaps.cpp HandleMmapPathCommand): 'Result: <true|false> - Length: <n> - Type: <PathType>',
    'StartPosition (x, y, z)', 'EndPosition (..)', 'ActualEndPosition (..)'; 'NavMesh not loaded for current map.'"""
    t = "\n".join(lines)
    out = {"nav_loaded": "NavMesh not loaded" not in t, "raw": lines[:8]}
    r = re.search(r"Result: (true|false) - Length: (\d+) - Type: (\d+)", t)
    if r:
        out |= {"result": r.group(1) == "true", "length": int(r.group(2)), "type": int(r.group(3))}
    for key, label in (("start", "StartPosition"), ("end", "EndPosition"), ("actual_end", "ActualEndPosition")):
        m = re.search(rf"(?<!Actual){label}\s+{_V3}", t)
        if m:
            out[key] = {"x": float(m.group(1)), "y": float(m.group(2)), "z": float(m.group(3))}
    return out


def judge_stage(boss: dict, cand: dict, path: dict) -> dict:
    """The design's candidate: 'primary' when the core's path from the boss reaches within 10 yd of it."""
    ae = path.get("actual_end")
    out = {"candidate": cand, "path_type": path.get("type"), "verdict": None}
    if not ae or path.get("type") is None or path["type"] & PATH_NOPATH:
        return out | {"why": "no path"}
    off, boss_yd = dist3(ae, cand), dist3(ae, boss)
    out |= {"point": ae, "off_yd": round(off, 1), "boss_yd": round(boss_yd, 1)}
    if off > STAGE_OFF_LINE_YD:
        return out | {"why": f"snapped {off:.1f} yd off the candidate"}
    return out | {"verdict": "primary"}


def path_points(creates: list[dict]) -> list[dict]:
    """The waypoints of one `.mmap path` in path order (from the selected unit to the probe): the VISUAL_WAYPOINT
    creates, deduplicated by GUID and sorted by the GUID's counter (SummonCreature in path order)."""
    seen = {}
    for c in creates:
        if guid_entry(c["guid"]) == VISUAL_WAYPOINT and c.get("pos"):
            seen.setdefault(c["guid"], c["pos"])
    return [{"x": p["x"], "y": p["y"], "z": p["z"]} for g, p in sorted(seen.items(), key=lambda kv: kv[0] & 0xFFFFFF)]


def walk_to(points: list[dict], point: dict) -> float | None:
    """Walking distance from the boss (points[0]) along the path to the path point nearest `point`; None when no path
    point is within STAGE_OFF_LINE_YD of it."""
    if not points:
        return None
    i = min(range(len(points)), key=lambda k: dist3(points[k], point))
    if dist3(points[i], point) > STAGE_OFF_LINE_YD:
        return None
    return round(sum(dist3(points[k], points[k + 1]) for k in range(i)), 1)


def fallback_point(points: list[dict], boss: dict, min_yd: float = STAGE_MIN_BOSS_YD) -> dict | None:
    """design.md §4 fallback: on the path from the boss to the previous staging point, the last point (walking from the
    previous staging point toward the boss) that is >= min_yd from the boss."""
    last = None
    for p in reversed(points):          # points run boss -> previous staging point
        if dist3(p, boss) >= min_yd:
            last = p
    return last


def nav_stages(f: dict, req: list[dict], log=None) -> dict:
    """The navmesh pass: NAV_ID (security 3) in GM mode, alone in a fresh copy of its own, asks the core's path query
    for every required encounter's staging point (RUN_RULE). Returns {"stages": [{encounter, boss, point, tries}]}."""
    say = log or (lambda *a: None)
    out = {"account": NAV_ID["user"], "gmlevel": NAV_ID["gmlevel"], "stages": []}
    t0 = time.time()
    with Session(f["target_level"], who=NAV_ID) as s:
        out["unbind"] = s.unbind(f["map"])
        s.cmd(".gm on", settle=0.3)
        e = f["entrance"]
        r = s.go(e["x"], e["y"], e["z"], f["map"])
        out["enter"] = r["event"]
        if not (r["event"] and r["event"]["kind"] == "new_world"):
            raise DungeonError("nav pass: didn't arrive in the dungeon", **out)
        prev = {"x": e["x"], "y": e["y"], "z": e["z"]}
        for enc in req:
            sp = enc["spawns"][0]
            boss = {"x": sp["x"], "y": sp["y"], "z": sp["z"]}
            rec = {"encounter": enc["encounter"], "boss": sp["name"], "spawn": boss, "prev": prev, "point": None,
                   "verdict": None}
            cand = stage_candidate(boss, prev)
            s.go(cand["x"], cand["y"], cand["z"])
            u = s.unit(sp["spawn_id"], enc["credit_entry"])
            if u.get("guid"):
                s.ws.set_selection(u["guid"])
                time.sleep(0.3)
                path = parse_mmap_path(s.cmd(".mmap path", settle=0.6))
                rec["primary"] = judge_stage(boss, cand, path) | {"raw": path.get("raw")}
                if rec["primary"]["verdict"]:
                    rec |= {"point": rec["primary"]["point"], "verdict": "primary"}
            # the path is read for every encounter: the fallback point, and the approach the pull walks if the tank
            # can't see the boss from the staging point (playerbots AttackAction::Attack needs line of sight)
            rec["fallback"] = fb = _path_fallback(s, sp, enc, boss, prev)
            if rec["point"] and fb.get("points"):
                walk = walk_to(fb["points"], rec["point"])
                rec["primary"]["walk_yd"] = walk
                if walk is None or walk > STAGE_WALK_MAX_YD:
                    # the line's point is near on the map but far on foot (another deck of the ship): refused
                    rec["primary"] |= {"verdict": None, "why": f"walk {walk} yd from the boss"}
                    rec |= {"point": None, "verdict": None}
            if not rec["point"] and fb.get("point"):
                rec |= {"point": fb["point"], "verdict": "fallback"}
            if rec["point"]:
                rec["approach"] = approach_points(fb.get("points") or [], rec["point"], boss)
                rec["gather"] = gather_point(fb.get("points") or [], rec["point"], boss)
                rec["walk_in"] = walk_in_points(fb.get("points") or [], rec["gather"], rec["point"])
            say({"boss": sp["name"], "verdict": rec["verdict"], "point": rec["point"],
                 "gather": {k: (rec.get("gather") or {}).get(k) for k in ("point", "back_yd", "why")},
                 "primary_why": (rec.get("primary") or {}).get("why"),
                 "fallback": {k: (rec.get("fallback") or {}).get(k) for k in ("n_points", "path_type", "why")}})
            out["stages"].append(rec)
            if rec["point"]:
                prev = rec["point"]
        s.ws.set_selection(0)
        out["leave"] = s.leave()
        s.cmd(".gm off", settle=0.3)
    out["s"] = round(time.time() - t0, 1)
    return out


def _creates_settle(s: "Session", mark: int, quiet: float = 0.75, cap: float = WAYPOINT_WAIT_S) -> int:
    """Poll until no new waypoint create arrived for `quiet` s (at most `cap` s); returns how many arrived."""
    t0 = last_t = time.time()
    n = 0
    while time.time() - t0 < cap:
        k = sum(1 for e in s.ws.events_since(mark, "unit_create") if guid_entry(e["guid"]) == VISUAL_WAYPOINT)
        if k != n:
            n, last_t = k, time.time()
        elif n and time.time() - last_t >= quiet:
            break
        time.sleep(0.2)
    return n


def _path_fallback(s: "Session", sp: dict, enc: dict, boss: dict, prev: dict) -> dict:
    """The boss's path to the previous staging point, point by point: the probe (GM) stands on the previous staging
    point, selects the boss and runs `.mmap path`; the waypoints in sight arrive as creates, and the probe then steps
    to the boss so the ones near it (out of sight from far away) arrive too, within their 9 s."""
    s.go(prev["x"], prev["y"], prev["z"] + 0.5)
    u = s.unit(sp["spawn_id"], enc["credit_entry"])
    if not u.get("guid"):
        return {"why": "boss not found from the previous staging point"}
    s.ws.track_creates = True
    try:
        mark = s.ws.mark()
        s.ws.set_selection(u["guid"])
        time.sleep(0.3)
        path = parse_mmap_path(s.cmd(".mmap path", settle=0.6))
        _creates_settle(s, mark)
        s.go(boss["x"], boss["y"], boss["z"] + 1)
        _creates_settle(s, mark)
        pts = path_points(s.ws.events_since(mark, "unit_create"))
    finally:
        s.ws.track_creates = False
    pt = fallback_point(pts, boss)
    return {"path_type": path.get("type"), "length": path.get("length"), "raw": path.get("raw"), "n_points": len(pts),
            "points": pts, "point": pt, "why": None if pt else "no path point >= 25 yd from the boss",
            "boss_yd": round(dist3(pt, boss), 1) if pt else None}


APPROACH_STEP = 2             # path points per approach step (SMOOTH_PATH_STEP_SIZE 4 yd apart: ~8 yd)
APPROACH_MIN_YD = 8           # the approach stops this close to the boss
APPROACH_STEPS = 12
NOT_IN_SIGHT = "not in my sight"   # AttackAction.cpp "attack_target_not_in_sight_error"


def approach_points(points: list[dict], stage: dict, boss: dict) -> list[dict]:
    """The pull's approach: the path points (boss -> previous staging point) from the one nearest the staging point
    toward the boss, every APPROACH_STEP-th, stopping APPROACH_MIN_YD from the boss."""
    if not points:
        return []
    i = min(range(len(points)), key=lambda k: dist3(points[k], stage))
    out = []
    for k in range(i - APPROACH_STEP, -1, -APPROACH_STEP):
        if dist3(points[k], boss) < APPROACH_MIN_YD:
            break
        out.append(points[k])
    return out


# -- gather farther back, then walk in together (slice 3.4; docs/research/m4-full-run.md finding 2)

GATHER_BACK_YD = 20           # the gather point: at least this much farther from the boss on foot than the staging point
WALK_STEP = 2                 # path points per walk-in hop (~8 yd, like the approach)
HOP_WAIT_S = 10               # per hop: wait this long for the party to catch up (within GATHER_YD)
READY_S = 120                 # the staging readiness wait, at most (then the pull goes ahead, flagged `unready`)


def _cum_walk(points: list[dict]) -> list[float]:
    out = [0.0]
    for k in range(1, len(points)):
        out.append(out[-1] + dist3(points[k - 1], points[k]))
    return out


def gather_point(points: list[dict], stage: dict, boss: dict, back: float = GATHER_BACK_YD) -> dict:
    """Where the party gathers before an encounter: on the boss's path to the previous staging point (points run boss ->
    previous staging point), the first point at least `back` yd farther on foot than the staging point and no nearer
    the boss in a straight line; else the path's last point if it is beyond the staging point at all. None (gather at
    the staging point, the prototype's way) when the staging point isn't on the path or nothing lies beyond it."""
    if not points:
        return {"point": None, "why": "no path"}
    i = min(range(len(points)), key=lambda k: dist3(points[k], stage))
    if dist3(points[i], stage) > STAGE_OFF_LINE_YD:
        return {"point": None, "why": "staging point not on the path"}
    cum = _cum_walk(points)
    near = dist3(points[i], boss)
    for j in range(i + 1, len(points)):
        if cum[j] - cum[i] >= back and dist3(points[j], boss) >= near:
            return {"point": points[j], "index": j, "stage_index": i, "back_yd": round(cum[j] - cum[i], 1), "why": None}
    if i < len(points) - 1:
        j = len(points) - 1
        return {"point": points[j], "index": j, "stage_index": i, "back_yd": round(cum[j] - cum[i], 1),
                "why": "path ends sooner: its last point"}
    return {"point": None, "why": "nothing on the path beyond the staging point"}


def walk_in_points(points: list[dict], gather: dict, stage: dict) -> list[dict]:
    """The hops from the gather point to the staging point along the path: every WALK_STEP-th path point toward the
    boss, then the staging point itself. Empty when there is no gather point."""
    if not gather.get("point"):
        return []
    gi, si = gather["index"], gather["stage_index"]
    return [points[k] for k in range(gi - WALK_STEP, si, -WALK_STEP)] + [stage]


def gather_ready(far: list[dict], dead: list[str], quiet_s: float) -> tuple[bool, str]:
    """design.md §4 step 3, slice 3.4: ready only when nobody is dead, nobody is still arriving (everyone within
    GATHER_YD of the probe) and nobody fought for QUIET_S."""
    if dead:
        return False, "dead: " + ", ".join(dead)
    if far:
        return False, "arriving: " + ", ".join(x["name"] for x in far)
    if quiet_s < QUIET_S:
        return False, "in combat"
    return True, "ready"


# -- the probe as the fifth party member (`.playerbots bot self`; PlayerbotMgr.cpp HandlePlayerbotCommand "self")

SELF_ON, SELF_OFF = "Enable player botAI", "Disable player botAI"


def self_state(reply: list[str]) -> bool | None:
    """The toggle's answer: True (now on), False (now off), None (refused: 'Self-bot is disabled', no permission)."""
    t = "\n".join(reply)
    if SELF_ON in t:
        return True
    if SELF_OFF in t:
        return False
    return None


def set_selfbot(s: "Session", want: bool) -> dict:
    """`.playerbots bot self` is a toggle; send it until the answer says the wanted state (at most twice: an answer
    of the other state means it was already in the wanted one and has just flipped)."""
    replies = []
    for _ in range(2):
        r = s.cmd(".playerbots bot self", settle=1.0)
        replies.append(r)
        st = self_state(r)
        if st is None:
            return {"ok": False, "on": s.selfbot, "replies": replies, "why": "refused"}
        s.selfbot = st
        if st == want:
            return {"ok": True, "on": st, "replies": replies}
    return {"ok": False, "on": s.selfbot, "replies": replies, "why": "toggle didn't settle"}


# -- run accounting (pure)

def role_of_key(key: str) -> str:
    """The plan's companion key -> role (MG_PARTY_PLAN: the `tank` and `healer` companions; the rest deal damage)."""
    return key if key in ("tank", "healer") else "damage"


def new_deaths(prev: dict, now: dict) -> list[str]:
    """Names alive in prev and dead in now ({name: True if alive, False if dead, None if unknown})."""
    return [n for n, a in now.items() if a is False and prev.get(n) is not False]


def is_wipe(alive: dict) -> bool:
    """Every fighter dead (unknown counts as alive)."""
    return bool(alive) and all(a is False for a in alive.values())


def next_step(killed: bool, wipes: int) -> str:
    """After a pull: 'next' (boss dead), 'retry' (a wipe or a stalled pull, wipes still <= MAX_WIPES) or 'fail'."""
    if killed:
        return "next"
    return "retry" if wipes <= MAX_WIPES else "fail"


def encounter_summary(rec: dict, roles: dict) -> dict:
    """One encounter's row: total time over its attempts, wipes, deaths by role, killed (the encounter bit)."""
    by_role: dict = {}
    for a in rec["attempts"]:
        for n in a.get("deaths", []):
            r = roles.get(n, "probe")
            by_role[r] = by_role.get(r, 0) + 1
    return {"boss": rec["boss"], "encounter": rec["encounter"], "attempts": len(rec["attempts"]),
            "wipes": sum(1 for a in rec["attempts"] if a.get("wipe")), "deaths_by_role": by_role,
            "time_s": round(sum(a.get("fight_s") or 0 for a in rec["attempts"]), 1),
            "with_gather_s": round(sum((a.get("fight_s") or 0) + (a.get("gather", {}).get("s") or 0)
                                       for a in rec["attempts"]), 1),
            "killed": bool(rec.get("killed"))}


# -- the live run

def _alive_map(s: "Session", names: list[str]) -> dict:
    out = {}
    for n in names:
        a = s.alive(n)
        out[n] = None if a is None else a == "Yes"
    return out


def _combat_quiet(s: "Session", guids: set, since: int) -> float:
    """Seconds since the last melee/spell damage event involving any of guids (since the mark)."""
    last = None
    for e in s.ws.events_since(since):
        if e["kind"] in ("melee_damage", "spell_damage") and (e.get("attacker") in guids or e.get("victim") in guids):
            last = e["t"]
    return time.time() - last if last else 1e9


def _far(s: "Session", comps: list[dict], me: dict | None = None) -> list[dict]:
    """Companions not within GATHER_YD of the probe (another map, or a `.gps` that answered with the probe's own
    position because the companion is out of sight, counts as far)."""
    me = me or s.gps()
    far = []
    for c in comps:
        p = s.gps(c["guid"])
        d = dist3(me, p) if "x" in me and "x" in p and p.get("map") == me.get("map") else None
        if d is None or d > GATHER_YD or (d < 0.01 and c["guid"] != s.guid and p == me):
            far.append({"name": c["name"], "yd": None if d is None else round(d, 1)})
    return far


def gather_at(s: "Session", comps: list[dict], boss_guid: int | None = None, limit: float = GATHER_S,
              revive: bool = True) -> dict:
    """design.md §4 steps 2-3 at the probe's position: whisper `summon` to the companions that are away until
    gather_ready (all alive, all within GATHER_YD, nobody fought for QUIET_S) or `limit` s, or until the boss is in a
    fight (it was pulled without an order). A companion that died here (trash) is revived once the fight is over, as a
    player would rez. Healer mana isn't readable headless (prototype: not used)."""
    t0, since, rounds = time.time(), s.ws.mark(), 0
    guids = {c["guid"] for c in comps}
    names = [c["name"] for c in comps]
    revived: dict = {}
    while True:
        far = _far(s, comps)
        alive = _alive_map(s, names)
        dead = [n for n, v in alive.items() if v is False]
        quiet = _combat_quiet(s, guids | {s.guid}, since)
        if boss_guid and _combat_quiet(s, {boss_guid}, since) < 1e8:
            return {"ok": False, "boss_engaged": True, "s": round(time.time() - t0, 1), "rounds": rounds, "far": far,
                    "revived": revived}
        ok, why = gather_ready(far, dead, quiet)
        if ok:
            return {"ok": True, "s": round(time.time() - t0, 1), "rounds": rounds, "revived": revived}
        if time.time() - t0 > limit:
            return {"ok": False, "s": round(time.time() - t0, 1), "rounds": rounds, "far": far, "dead": dead,
                    "why": why, "quiet_s": round(min(quiet, 999), 1), "revived": revived}
        if dead and revive and quiet >= QUIET_S:
            revived |= revive_dead(s, dead)
            continue
        if far and rounds * 4 <= time.time() - t0:
            for x in far:
                if alive.get(x["name"]) is not False:
                    s.ws.chat("summon", W.CHAT_MSG_WHISPER, to=x["name"])
            rounds += 1
        time.sleep(POLL_S)


def hold_party(s: "Session", comps: list[dict]) -> list[str]:
    """Whisper `stay` (playerbots ChatTriggerContext "stay": the stay movement strategy) to every companion, so none of
    them runs after the probe's next teleport; `follow` (walk_in) undoes it."""
    for c in comps:
        s.ws.chat("stay", W.CHAT_MSG_WHISPER, to=c["name"])
    time.sleep(0.5)
    return [c["name"] for c in comps]


def walk_in(s: "Session", comps: list[dict], hops: list[dict]) -> dict:
    """Walk the gathered party in: the probe (GM mode on, so nothing aggroes on it alone) hops along the path toward
    the staging point; the companions follow it on foot (`follow`), and each hop waits until all of them are within
    GATHER_YD (HOP_WAIT_S at most)."""
    t0 = time.time()
    for c in comps:
        s.ws.chat("follow", W.CHAT_MSG_WHISPER, to=c["name"])
    steps = []
    for p in hops:
        s.go(p["x"], p["y"], p["z"] + 0.5)
        me = s.gps()
        far, ok, dt = F.poll(lambda: _far(s, comps, me), lambda x: not x, timeout=HOP_WAIT_S, interval=0.5)
        steps.append({"to": {k: round(p[k], 1) for k in ("x", "y", "z")}, "caught_up": ok, "wait_s": dt,
                      "far": None if ok else far})
    return {"hops": len(hops), "steps": steps, "s": round(time.time() - t0, 1)}


def revive_dead(s: "Session", names: list[str]) -> dict:
    """`.revive <name>` (cs_misc.cpp HandleReviveCommand, security 2) for every dead party member."""
    out = {}
    for n in names:
        if s.alive(n) != "Yes":
            out[n] = s.cmd(f".revive {n}", settle=0.4)
    return out


def wait_boss_reset(s: "Session", enc: dict, timeout: float = RESET_S) -> dict:
    sp = enc["spawns"][0]
    v, ok, dt = F.poll(lambda: s.unit(sp["spawn_id"], enc["credit_entry"]),
                       lambda u: bool(u.get("health")) and u["health"] == u.get("max_health") and not u.get("in_combat"),
                       timeout=timeout, interval=POLL_S)
    return {"ok": ok, "s": dt, "health": v.get("health"), "max": v.get("max_health")}


def pull(s: "Session", boss_guid: int, tank: str | None, comps: list[dict], approach: list[dict]) -> dict:
    """Select the boss and whisper `tank attack` + `attack` (AttackMyTargetAction: the master's target) to the tank.
    Until the boss is in a fight (a damage or attack-start event with it) within 3 s of the order, the probe (GM) steps
    along the approach path, the companions follow, and the order is given again, up to APPROACH_STEPS times."""
    steps = []
    tank_guid = next((c["guid"] for c in comps if c["name"] == tank), None)
    for k in range(APPROACH_STEPS + 1):
        s.ws.set_selection(boss_guid)
        time.sleep(0.3)
        if not tank:
            return {"steps": steps, "ordered": False}
        mark = s.ws.mark()
        s.ws.chat("tank attack", W.CHAT_MSG_WHISPER, to=tank)
        time.sleep(0.3)
        s.ws.chat("attack", W.CHAT_MSG_WHISPER, to=tank)
        e = s.ws.wait_event(lambda e: (e["kind"] == "chat" and e.get("sender") == tank_guid and NOT_IN_SIGHT in e["msg"])
                            or (e["kind"] in ("melee_damage", "spell_damage", "attack_start")
                                and boss_guid in (e.get("attacker"), e.get("victim"))), mark, timeout=3)
        if e and e["kind"] != "chat":
            return {"steps": steps, "ordered": True, "engaged": True}
        # no answer and no fight within 3 s counts like "not in my sight": the order is dropped silently unless the
        # bot is verbose (AttackAction::Attack TellError only `if (verbose)`)
        answer = e["msg"] if e else None
        if k >= len(approach):
            return {"steps": steps, "ordered": True, "engaged": False, "why": "approach path exhausted"}
        p = approach[k]
        s.go(p["x"], p["y"], p["z"] + 0.5)
        steps.append({"to": p, "tank_answer": answer})
        for c in comps:
            s.ws.chat("follow", W.CHAT_MSG_WHISPER, to=c["name"])
        me = s.gps()
        F.poll(lambda: s.gps(tank_guid), lambda q: "x" in q and "x" in me and dist3(q, me) <= 6 and q != me,
               timeout=8, interval=0.5)
    return {"steps": steps, "ordered": True, "engaged": False, "why": "too many steps"}


def fight(s: "Session", f: dict, enc: dict, comps: list[dict], fighters: list[str], tank: str | None,
          selfbot: bool, say, approach: list[dict] | None = None, engaged: bool = False) -> dict:
    """One pull: select the boss, order the tank in (or, solo, stand inside the boss's aggro radius with the self-AI),
    then poll the encounter bit and the fighters' life until the boss is dead, everyone is dead or FIGHT_S. With
    companions and selfbot the probe fights too: its self-AI goes on right after the order (engaged: the boss is
    already in a fight, so no order) and off when the probe dies or the pull ends."""
    probe_name = s.who["char"]
    sp = enc["spawns"][0]
    bit = 1 << enc["bit"]
    a: dict = {"deaths": [], "wipe": False}
    u = s.unit(sp["spawn_id"], enc["credit_entry"])
    a["boss_before"] = {k: u.get(k) for k in ("guid", "health", "max_health", "in_combat")}
    if not u.get("guid"):
        # no live read of the boss (not spawned, an entry mismatch, or not in an active grid): nothing to pull. It
        # counts like a stalled pull.
        return a | {"why": "boss not found", "timeout": True, "fight_s": 0.0, "boss_read": {
            k: u.get(k) for k in ("entry", "entry_mismatch", "raw")}}
    mark = s.ws.mark()
    if selfbot and not comps:
        stage = s.gps()
        if "x" in stage:
            d = math.hypot(stage["x"] - sp["x"], stage["y"] - sp["y"]) or 1.0
            s.cmd(".gm on", settle=0.3)
            s.go(sp["x"] + (stage["x"] - sp["x"]) / d * SOLO_PULL_YD, sp["y"] + (stage["y"] - sp["y"]) / d * SOLO_PULL_YD,
                 sp["z"] + 1)
            s.cmd(".gm off", settle=0.3)
    else:
        if not engaged:
            a["pull"] = pull(s, u["guid"], tank, comps, approach or [])
        if selfbot:
            # the probe joins as the fifth fighter after the order (its self-AI picks its own targets)
            a["self_on"] = set_selfbot(s, True)
            if not a["self_on"]["ok"]:
                return a | {"why": "selfbot not enabled", "timeout": True, "fight_s": 0.0}
    t0 = time.time()
    alive = {n: True for n in fighters}
    trace = []
    while True:
        time.sleep(POLL_S)
        rows = s.binds(f["map"])
        mask = rows[0]["mask"] if rows else 0
        now = _alive_map(s, fighters)
        died = new_deaths(alive, now)
        for n in died:
            a["deaths"].append(n)
            say({"died": n, "t": round(time.time() - t0, 1)})
            if n == probe_name and s.selfbot:
                # a dead selfbot that leads the group releases its spirit at once (AutoReleaseSpiritAction::
                # ShouldAutoRelease: groupLeader == bot) and lands outside the dungeon; switched off, it stays a corpse
                a["self_off_on_death"] = set_selfbot(s, False)
        alive = {n: (now[n] if now[n] is not None else alive[n]) for n in fighters}
        el = time.time() - t0
        if not trace or el - trace[-1]["t"] >= 10:
            trace.append({"t": round(el), "mask": mask, "alive": sum(1 for v in alive.values() if v)})
        if mask & bit:
            a["killed"] = True
            break
        if is_wipe(alive):
            a["wipe"] = True
            break
        if el > FIGHT_S:
            a["timeout"] = True
            break
        if el > STALL_S and not any(e["kind"] in ("melee_damage", "spell_damage", "attack_start")
                                    and u.get("guid") in (e.get("attacker"), e.get("victim"))
                                    for e in s.ws.events_since(mark)):
            a["timeout"] = a["stalled"] = True       # nobody ever fought the boss
            break
    a["fight_s"] = round(time.time() - t0, 1)
    a["trace"] = trace
    evs = s.ws.events_since(mark)
    guids = {c["guid"]: c["name"] for c in comps} | {s.guid: "probe"}
    a["hits_taken"] = {}
    for e in evs:
        if e["kind"] in ("melee_damage", "spell_damage") and e.get("victim") in guids:
            n = guids[e["victim"]]
            a["hits_taken"][n] = a["hits_taken"].get(n, 0) + e["damage"]
    a["boss_dmg_seen"] = sum(e["damage"] for e in evs if e["kind"] in ("melee_damage", "spell_damage")
                             and e.get("victim") == u.get("guid"))
    if comps and s.selfbot:
        a["self_off"] = set_selfbot(s, False)
    if not a.get("killed"):
        b = s.unit(sp["spawn_id"], enc["credit_entry"])
        a["boss_after"] = {k: b.get(k) for k in ("health", "max_health", "in_combat")}
    return a


def run_party(s: "Session", f: dict, comps: list[dict], stages: dict | None, selfbot: bool = False,
              only: int | None = None, log=None, out: dict | None = None) -> dict:
    """design.md §4: clear the required encounters of f in order with the companions already in the party (comps:
    [{"companion": plan key, "name", "guid"}]; empty with selfbot=True for the solo run). The probe enters a fresh copy
    with GM mode off (so AutoBalance counts it, design.md §6). Per pull, with companions (slice 3.4): gather at the
    encounter's gather point (farther back on the path), walk in together to the staging point, GM mode off there,
    wait until everyone is there and out of combat, then the pull; with selfbot the probe fights as the fifth member
    (else it watches in GM mode, the prototype). Solo (selfbot, no companions): the self-AI fights alone. Returns the
    evidence with a summary row per encounter."""
    say = log or (lambda *a: None)
    req, skipped = required_encounters(f)
    if only:
        req = req[:only]
    ev: dict = out if out is not None else {}        # filled in place, so an interrupted run keeps its evidence
    mode = "party" if comps and selfbot else "solo" if selfbot else "watch"
    ev |= {"rule": RUN_RULE, "required": [e["encounter"] for e in req], "skipped": skipped, "encounters": [],
           "selfbot": selfbot, "mode": mode}
    roles = {c["name"]: role_of_key(c["companion"]) for c in comps}
    names = [c["name"] for c in comps]
    probe_name = s.who["char"]
    tank = next((c["name"] for c in comps if c["companion"] == "tank"), None)
    t_run = time.time()
    ev["unbind"] = s.unbind(f["map"])
    e = s.enter(f)
    ev["enter"] = {k: e.get(k) for k in ("ok", "instance")}
    if not e["ok"]:
        return ev | {"cleared": False, "why": "didn't enter"}
    ev["gather_in"] = s.gather(1 + len(comps), timeout=45)
    ev["mapstat"] = s.mapstat()
    if ev["mapstat"].get("players") != 1 + len(comps):
        return ev | {"cleared": False, "why": f"AutoBalance counts {ev['mapstat'].get('players')} players, not "
                                              f"{1 + len(comps)}"}
    fighters = names + ([probe_name] if selfbot else [])
    if mode == "solo":
        ev["self_on"] = set_selfbot(s, True)
        if not ev["self_on"]["ok"]:
            return ev | {"cleared": False, "why": "selfbot not enabled"}
    by_enc = {x["encounter"]: x for x in (stages or {}).get("stages", [])}
    for enc in req:
        sp = enc["spawns"][0]
        st = by_enc.get(enc["encounter"]) or {}
        point = st.get("point")
        gpt = (st.get("gather") or {}).get("point")
        rec: dict = {"boss": sp["name"], "encounter": enc["encounter"], "bit": enc["bit"], "stage": point,
                     "stage_verdict": st.get("verdict"), "gather_point": gpt,
                     "gather_why": (st.get("gather") or {}).get("why"), "attempts": []}
        ev["encounters"].append(rec)
        if not point:
            rec["why"] = "no staging point"
            break
        wipes = 0
        while True:
            a: dict = {"n": len(rec["attempts"]) + 1}
            engaged = False
            if mode == "solo":
                s.cmd(".gm on", settle=0.3)
                s.go(point["x"], point["y"], point["z"] + 0.5, f["map"])
                s.cmd(".gm off", settle=0.3)
                a["gather"] = {"ok": True, "s": 0}
                if not s.selfbot:           # switched off when the probe died on the last pull
                    a["self_on"] = set_selfbot(s, True)
            else:
                s.cmd(".gm on", settle=0.3)
                first = gpt if gpt and mode == "party" else point
                if first is gpt:
                    # run 1 of slice 3.4: companions that ran after the probe's teleport crossed trash and the next
                    # boss on the way (deaths, bosses engaged while members were 30-50 yd behind). They stay put, and
                    # the gather summons them to the gather point instead.
                    a["hold"] = hold_party(s, comps)
                s.go(first["x"], first["y"], first["z"] + 0.5, f["map"])
                boss_guid = s.unit(sp["spawn_id"], enc["credit_entry"]).get("guid")
                if first is gpt:
                    # gather farther back (out of the boss's reach), then walk in together
                    a["gather_back"] = gather_at(s, comps, boss_guid)
                    engaged = bool(a["gather_back"].get("boss_engaged"))
                    if not engaged:
                        a["walk_in"] = walk_in(s, comps, st.get("walk_in") or [point])
                if mode == "party":
                    a["gm_off"] = s.cmd(".gm off", settle=0.3)
                if not engaged:
                    a["gather"] = gather_at(s, comps, boss_guid, limit=READY_S if mode == "party" else GATHER_S)
                    engaged = bool(a["gather"].get("boss_engaged"))
                    a["unready"] = not a["gather"]["ok"] and not engaged
                a["boss_engaged_before_pull"] = engaged
            a |= fight(s, f, enc, comps, fighters, tank, selfbot, say, st.get("approach"), engaged=engaged)
            rec["attempts"].append(a)
            say({"boss": sp["name"], "attempt": a["n"], "killed": a.get("killed"), "wipe": a["wipe"],
                 "fight_s": a["fight_s"], "deaths": a["deaths"], "why": a.get("why"),
                 "engaged_early": a.get("boss_engaged_before_pull"), "unready": a.get("unready")})
            if a.get("why") == "selfbot not enabled":
                rec["why"] = a["why"]
                break
            if a.get("wipe") or a.get("timeout"):
                wipes += 1
            step = next_step(bool(a.get("killed")), wipes)
            if step == "next":
                a["revive"] = revive_dead(s, fighters)
                rec["killed"] = True
                break
            if step == "fail":
                a["revive"] = revive_dead(s, fighters)
                break
            if probe_name in fighters:
                # the probe gets up first (it may have released outside the dungeon), then GM mode, then the stage
                a["revive_probe"] = revive_dead(s, [probe_name])
            s.cmd(".gm on", settle=0.3)
            s.go(point["x"], point["y"], point["z"] + 0.5, f["map"])
            # the boss resets first, then the dead get up: revived where they fell (next to the boss and its adds),
            # they'd be killed again before the next gather (run 7, Gilnid)
            a["reset"] = wait_boss_reset(s, enc)
            a["revive"] = revive_dead(s, names)
            if mode == "solo":
                s.cmd(".gm off", settle=0.3)
        rec["summary"] = encounter_summary(rec, roles)
        if not rec.get("killed"):
            break
    rows = s.binds(f["map"])
    ev["final_mask"] = rows[0]["mask"] if rows else None
    ev["cleared"] = bool(req) and all(r.get("killed") for r in ev["encounters"]) and len(ev["encounters"]) == len(req)
    ev["total_s"] = round(time.time() - t_run, 1)
    ev["table"] = [r.get("summary") or {"boss": r["boss"], "killed": False, "why": r.get("why")} for r in ev["encounters"]]
    if s.selfbot:
        ev["self_off"] = set_selfbot(s, False)
    s.cmd(".gm off", settle=0.3)
    return ev
