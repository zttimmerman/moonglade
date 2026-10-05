"""Minimal WDBC (3.3.5a client DBC) reader, plus AreaTable helpers. Stdlib only.

Moved here from tools/era-diff/era_diff.py (m2-living-world task 1.1) so harness checks can read client data at
runtime (CLAUDE.md §7: zone/map IDs come from DBC or game_tele, never from memory). era-diff imports it.

Field layouts are AzerothCore's (vendor/azerothcore-wotlk/src/server/shared/DataStores/DBCStructure.h and
DBCfmt.h), cited where used."""
import struct
from pathlib import Path

from . import paths as P

DBC_DIR = P.CLIENT_DATA / "dbc"


def read_dbc(name: str, dbc_dir: Path = DBC_DIR):
    """Read a WDBC file -> (records as tuples of uint32 fields, string block bytes)."""
    data = (Path(dbc_dir) / name).read_bytes()
    magic, nrec, nfield, recsize, strsize = struct.unpack("<4s4I", data[:20])
    assert magic == b"WDBC", name
    recs = []
    for i in range(nrec):
        off = 20 + i * recsize
        recs.append(struct.unpack_from(f"<{nfield}I", data, off))
    strings = data[20 + nrec * recsize:]
    return recs, strings


def dbc_string(strings: bytes, off: int) -> str:
    end = strings.index(b"\0", off)
    return strings[off:end].decode("utf-8", "replace")


# AreaTable.dbc, AreaTableEntry (DBCStructure.h:518-530): 0 ID, 1 mapid, 2 zone (parent; 0 = this is a zone),
# 4 flags, 10 area_level, 11 area_name[enUS], 28 team.
AREA_ID, AREA_MAP, AREA_PARENT, AREA_FLAGS, AREA_NAME, AREA_TEAM = 0, 1, 2, 4, 11, 28
AREA_FLAG_CAPITAL = 0x00000100      # DBCEnums.h:242 "main capital city flag"
AREATEAM_ALLY, AREATEAM_HORDE, AREATEAM_ANY = 2, 4, 6   # DBCEnums.h:56-59 (AreaTeams)


def area_table(dbc_dir: Path = DBC_DIR) -> dict[int, dict]:
    """AreaTable.dbc by ID: {id, map, parent, flags, team, name}."""
    recs, strings = read_dbc("AreaTable.dbc", dbc_dir)
    return {r[AREA_ID]: {"id": r[AREA_ID], "map": r[AREA_MAP], "parent": r[AREA_PARENT], "flags": r[AREA_FLAGS],
                         "team": r[AREA_TEAM], "name": dbc_string(strings, r[AREA_NAME])}
            for r in recs}


def top_zone(areas: dict[int, dict], area_id: int) -> int | None:
    """Walk AreaTable parents up to the zone (parent 0). None for an unknown area."""
    seen = set()
    while area_id in areas and areas[area_id]["parent"] and area_id not in seen:
        seen.add(area_id)
        area_id = areas[area_id]["parent"]
    return area_id if area_id in areas else None


# ChrClasses.dbc, ChrClassesEntryfmt "nxixssss..." (DBCfmt.h:35): 0 ClassID, 4 name[enUS] (the struct comment in
# DBCStructure.h says 5-20, but the format string puts the 16 locale names at 4-19; read back: 1 -> "Warrior").
CLASS_ID, CLASS_NAME = 0, 4


def class_names(dbc_dir: Path = DBC_DIR) -> dict[int, str]:
    """{class id: English class name} from the game's own ChrClasses.dbc."""
    recs, strings = read_dbc("ChrClasses.dbc", dbc_dir)
    return {r[CLASS_ID]: dbc_string(strings, r[CLASS_NAME]) for r in recs}
