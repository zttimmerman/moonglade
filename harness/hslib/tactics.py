"""m4-instance-tactics: what mod-playerbots' dungeon tactics should look like, read from the pinned source, and the
pure judgement dng.instance_strategy applies to the companions' `co ?` answers.

The strategy names and the maps they belong to are never typed in here: they are parsed from
PlayerbotAI::ApplyInstanceStrategies (vendor/mod-playerbots/src/Bot/PlayerbotAI.cpp), which removes every name in its
`allInstanceStrategies` list and then adds the one its `switch (mapId)` gives for the bot's map. The dungeons' map ids
come from LFGDungeons.dbc (hslib/dungeon.py facts, MapID [23])."""
import re

SOURCE = "src/Bot/PlayerbotAI.cpp"          # under P.BOTS_SRC
FUNC = "void PlayerbotAI::ApplyInstanceStrategies("


class TacticsError(Exception):
    pass


def _body(src: str) -> tuple[str, int, int]:
    """The function's body text, the line its signature is on and the line its body starts on. Fail-closed."""
    i = src.find(FUNC)
    if i < 0:
        raise TacticsError(f"{FUNC.strip()} not found in {SOURCE}")
    start = src.index("{", i)
    depth, j = 0, start
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    if depth:
        raise TacticsError(f"unbalanced braces in {FUNC.strip()}")
    return src[start:j + 1], src.count("\n", 0, i) + 1, src.count("\n", 0, start) + 1


def instance_table(src: str) -> dict:
    """{"table": {map id: strategy}, "all": [names removed on every map change], "lines": {map id: source line},
    "func_line": n} from ApplyInstanceStrategies's source."""
    body, line0, body_line = _body(src)
    m = re.search(r"allInstanceStrategies\s*=\s*\{(.*?)\};", body, re.S)
    if not m:
        raise TacticsError("allInstanceStrategies list not found")
    all_names = re.findall(r'"([^"]+)"', m.group(1))
    table, lines = {}, {}
    for c in re.finditer(r"case\s+(\d+)\s*:\s*strategyName\s*=\s*\"([^\"]+)\"", body):
        mid = int(c.group(1))
        if mid in table:
            raise TacticsError(f"map {mid} appears twice in the switch")
        table[mid] = c.group(2)
        lines[mid] = body_line + body.count("\n", 0, c.start())
    if not table:
        raise TacticsError("no `case <map>: strategyName = \"...\"` entries in the switch")
    labels = len(re.findall(r"\bcase\s+\d+\s*:", body))
    if labels != len(table):
        # e.g. a fall-through `case A: case B: strategyName = ...` would drop map A silently
        raise TacticsError(f"{labels} case labels but {len(table)} parsed entries in the switch")
    stray = sorted(set(table.values()) - set(all_names))
    if stray:
        # a strategy the switch adds but the function never removes would survive leaving the dungeon
        raise TacticsError(f"switch strategies missing from allInstanceStrategies: {stray}")
    return {"table": table, "all": all_names, "lines": lines, "func_line": line0}


def expectations(table: dict, maps: list[int]) -> list[dict]:
    """Per visited map, in order: the strategy each companion must list there (None: the map has none) and the other
    visited maps' strategies, which it must not list (left behind on the way in)."""
    out = []
    for mid in maps:
        want = table.get(mid)
        out.append({"map": mid, "want": want,
                    "not": sorted({table[o] for o in maps if o != mid and o in table} - {want})})
    return out


def judge(expect: list[dict], reads: list[dict]) -> list[str]:
    """Problems with the `co ?` answers. `reads[i]` is {companion name: [strategies] or None} for `expect[i]`'s map.
    Fail-closed: a missing dungeon read, an empty party or an unanswered companion is a problem."""
    problems = []
    if len(reads) != len(expect):
        problems.append(f"{len(reads)} dungeon reads for {len(expect)} dungeons")
    for e, r in zip(expect, reads):
        if not r:
            problems.append(f"map {e['map']}: no companion answers")
            continue
        for name, strats in sorted(r.items()):
            if strats is None:
                problems.append(f"map {e['map']}: {name} never answered `co ?`")
                continue
            if e["want"] and e["want"] not in strats:
                problems.append(f"map {e['map']}: {name} doesn't list {e['want']}")
            stale = [x for x in e["not"] if x in strats]
            if stale:
                problems.append(f"map {e['map']}: {name} still lists {', '.join(stale)}")
    return problems
