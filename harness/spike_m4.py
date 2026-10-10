"""SPIKE (m4-dungeon-scaling task 1.1, branch spike/m4-dungeon; throwaway, never merged).

Headless probe on the harness companion account (P.COMP_USER, never PLAYER) driving The Deadmines.
Every dungeon fact comes from DBC/SQL by the rules in openspec/changes/m4-dungeon-scaling/design.md §6.
Usage: harness/spike_m4.py <mode> [args]; evidence JSON goes to env/spike-m4/<mode>-<time>.json (not committed).
"""
import json
import math
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hslib import companion as C, db, dbc, paths as P, soap   # noqa: E402
from hslib.probe import proto as W   # noqa: E402

LFG_ID = 6          # The Deadmines (proposal table; LFGDungeons.dbc row 6)
OUT = P.ENV / "spike-m4"
T0 = time.time()


def log(*a):
    print(f"[{time.time() - T0:7.1f}s]", *a, flush=True)


# ----------------------------------------------------------------------------- facts by rule (design.md §6)

def facts(lfg_id=LFG_ID) -> dict:
    recs, _ = dbc.read_dbc("LFGDungeons.dbc")
    r = next(r for r in recs if r[0] == lfg_id)
    f = {"lfg_id": lfg_id, "min_level": r[18], "max_level": r[19], "target_level": r[20], "map": r[23],
         "difficulty": r[24], "type": r[26]}
    enc, _ = dbc.read_dbc("DungeonEncounter.dbc")
    rows = sorted((e for e in enc if e[1] == f["map"] and e[2] == f["difficulty"]), key=lambda e: e[3])
    out = []
    for e in rows:
        ie = db.query(f"SELECT creditType, creditEntry, lastEncounterDungeon FROM instance_encounters "
                      f"WHERE entry={e[0]}", "world")
        ct, ce, last = map(int, ie[0]) if ie else (None, None, None)
        spawn = db.query(f"SELECT c.guid, c.position_x, c.position_y, c.position_z, ct.name, ct.minlevel, ct.rank "
                         f"FROM creature c JOIN creature_template ct ON ct.entry=c.id WHERE c.id={ce} "
                         f"AND c.map={f['map']}", "world") if ce else []
        out.append({"encounter": e[0], "order": e[3], "bit": e[4], "credit_type": ct, "credit_entry": ce,
                    "last": last, "spawns": [{"spawn_id": int(g), "x": float(x), "y": float(y), "z": float(z),
                                              "name": n, "level": int(lv), "rank": int(rk)}
                                             for g, x, y, z, n, lv, rk in spawn]})
    f["encounters"] = out
    at = db.query(f"SELECT ID, target_position_x, target_position_y, target_position_z, target_orientation "
                  f"FROM areatrigger_teleport WHERE target_map={f['map']}", "world")
    f["entrance"] = [{"trigger": int(i), "x": float(x), "y": float(y), "z": float(z)} for i, x, y, z, _o in at]
    f["rules"] = "LFGDungeons.dbc row by ID; DungeonEncounter.dbc map+difficulty by orderIndex joined to " \
                 "instance_encounters; boss spawn = creature.id1 = creditEntry on the map; entrance = " \
                 "areatrigger_teleport.target_map"
    return f


# ----------------------------------------------------------------------------- parsers

def parse_mapstat(lines):
    t = "\n".join(lines)
    m = re.search(r"Players on map: (\d+)", t)
    a = re.search(r"[Aa]djusted.*?(\d+)", t)
    i = re.search(r"\| ID (\d+)-(\d+)", t)
    return {"players": int(m.group(1)) if m else None, "adjusted_line": a.group(0) if a else None,
            "map": int(i.group(1)) if i else None, "instance": int(i.group(2)) if i else None, "raw": lines}


class Run:
    def __init__(self, level):
        self.level = level
        self.s = C.Session(level=level)
        self.ev = {"level": level}

    def __enter__(self):
        self.ev["pool_out_of_dungeon"] = pool_out_of_dungeon()
        self.s.__enter__()
        self.ev["session"] = {k: self.s.ev.get(k) for k in ("gm_off", "revive", "stale_group")}
        return self

    def __exit__(self, *exc):
        try:
            if self.selfbot_on():
                self.cmd(".playerbots bot self")
        except Exception:
            pass
        self.s.__exit__(*exc)

    def cmd(self, c, settle=0.6, timeout=8):
        r = self.s.ws.command(c, settle=settle, timeout=timeout)
        log(c, "->", r[:6])
        return r

    def selfbot_on(self):
        return self.ev.get("selfbot") is True

    def whisper(self, name, msg, wait=1.0):
        mark = self.s.ws.mark()
        self.s.ws.chat(msg, W.CHAT_MSG_WHISPER, to=name)
        time.sleep(wait)
        rep = [e["msg"] for e in self.s.ws.events_since(mark) if e["kind"] == "chat" and e["type"] == W.CHAT_MSG_WHISPER]
        log(f"w {name}: {msg} ->", rep[:4])
        return rep

    def party(self, msg, wait=1.0):
        mark = self.s.ws.mark()
        self.s.ws.chat(msg, W.CHAT_MSG_PARTY)
        time.sleep(wait)
        rep = [e["msg"] for e in self.s.ws.events_since(mark) if e["kind"] == "chat"]
        log(f"p: {msg} ->", rep[:6])
        return rep

    def go(self, x, y, z, m=None, timeout=15):
        mark = self.s.ws.mark()
        out = self.cmd(f".go xyz {x:.2f} {y:.2f} {z:.2f}" + (f" {m}" if m is not None else ""), settle=0.5)
        kind = "new_world" if m is not None else "teleport_near"
        e = self.s.ws.wait_event(lambda e: e["kind"] in ("new_world", "teleport_near"), mark, timeout=timeout)
        if e:
            time.sleep(1.5)
            self.s.ws.heartbeat(e["x"], e["y"], e["z"])
        return {"reply": out, "event": e, "wanted": kind}

    def mapstat(self):
        return parse_mapstat(self.cmd(".ab mapstat", settle=0.5))

    def creature(self, spawn_id):
        self.s.ws.set_selection(0)      # `.npc info <spawn>` reports the SELECTED creature first (companion.py)
        time.sleep(0.3)
        info = self.s.npc_info(spawn_id)
        stat = []
        if info.get("guid"):
            self.s.ws.set_selection(info["guid"])
            time.sleep(0.3)
            stat = self.cmd(".ab creaturestat", settle=0.5)
        return {"spawn_id": spawn_id, "guid": info.get("guid"), "health": info.get("health"),
                "max_health": info.get("max_health"), "in_combat": info.get("in_combat"), "pos": info.get("pos"),
                "creaturestat": stat}

    def instance_row(self):
        g = self.s.guid
        return db.query(f"SELECT ci.instance, ci.permanent, i.map, i.completedEncounters FROM character_instance ci "
                        f"JOIN instance i ON i.id=ci.instance WHERE ci.guid={g & 0xFFFFFFFF}", "characters")

    def dump(self, name):
        OUT.mkdir(parents=True, exist_ok=True)
        p = OUT / f"{name}-{time.strftime('%H%M%S')}.json"
        p.write_text(json.dumps(self.ev, indent=1, default=str))
        log("evidence:", p)


POOL_RULE = ("addclass-pool characters (playerbots_account_type.account_type = 2) that are offline with a saved "
             "position on a dungeon map (Map.dbc InstanceType 1) are moved out with `tele name <char> ElwynnForest` "
             "(SOAP, server-side): logged in there they land in a stale copy of the dungeon, and the playerbots "
             "summon (SummonAction::Teleport -> TeleportTo, same map id) only near-teleports inside that copy")


def pool_out_of_dungeon(dungeon_maps=(36,)):
    rows = db.query(f"SELECT c.name, c.map FROM characters c JOIN {P.DB_NAMES['playerbots']}.playerbots_account_type t "
                    f"ON t.account_id=c.account WHERE t.account_type=2 AND c.online=0 "
                    f"AND c.map IN ({','.join(map(str, dungeon_maps))})", "characters")
    out = []
    for n, m in rows:
        out.append({"name": n, "map": int(m), "tele": soap.gm(f"tele name {n} {C.START_TELE}").ok})
    if out:
        log("moved pool characters out of dungeons:", out)
    return out


def save_json(name, obj):
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{name}-{time.strftime('%H%M%S')}.json"
    p.write_text(json.dumps(obj, indent=1, default=str))
    return p


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "facts":
        print(json.dumps(facts(), indent=1))
    else:
        import importlib
        mod = importlib.import_module("spike_m4_modes")
        getattr(mod, mode.replace("-", "_"))(*sys.argv[2:])
