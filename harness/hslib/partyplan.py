"""The /mg party plan, read from the add-on itself (openspec/changes/m4-party-preset; m4-dungeon-companions design.md
§2): MG_PARTY_PLAN in client-mods/addons/Moonglade/MoongladeParty.lua, parsed here so dng.party_preset sends exactly
what the slash command sends and the two can't drift.

The reader takes a Lua table constructor of literals only (strings, numbers, booleans, nested tables; `key = v`,
`["key"] = v`, `[n] = v` and list items; `--` comments) and refuses anything else (fail-closed: a plan the reader
can't read is an error, never an empty plan)."""
import re

from . import paths as P

PARTY_LUA = P.ADDON_SRC / "MoongladeParty.lua"
ROLES = ("damage", "tank", "healer")
# addclass class words -> class ids (PlayerbotMgr.cpp:1100-1137 at our pin); the premade spec names are read from
# AiPlayerbot.PremadeSpecName.<class id>.<n> (playerbots.conf.dist:1786-2132)
CLASS_IDS = {"warrior": 1, "paladin": 2, "hunter": 3, "rogue": 4, "priest": 5, "dk": 6, "shaman": 7, "mage": 8,
             "warlock": 9, "druid": 11}


class PlanError(ValueError):
    pass


_TOKEN = re.compile(r"""(?P<str>"(?:[^"\\\n]|\\.)*")|(?P<num>-?\d+(?:\.\d+)?)|(?P<name>[A-Za-z_]\w*)|"""
                    r"""(?P<sym>[{}\[\]=,;])""")
_SKIP = re.compile(r"\s+|--[^\n]*")


def _tokens(text: str) -> list[tuple[str, str]]:
    out, i = [], 0
    while True:
        while True:                                 # whitespace and comments
            m = _SKIP.match(text, i)
            if not m or m.end() == i:
                break
            i = m.end()
        if i >= len(text):
            return out
        m = _TOKEN.match(text, i)
        if not m or m.end() == i:
            raise PlanError(f"unexpected text at {i}: {text[i:i + 30]!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        i = m.end()


def parse_table(text: str):
    """Parse one Lua table constructor (the whole of `text`). Lists come back as Python lists, other tables as dicts."""
    toks = _tokens(text)
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else (None, None)

    def take(want=None):
        nonlocal pos
        t = peek()
        if t[0] is None or (want is not None and t[1] != want):
            raise PlanError(f"expected {want or 'a value'}, got {t[1]!r}")
        pos += 1
        return t

    def value():
        kind, v = peek()
        if kind == "str":
            take()
            return bytes(v[1:-1], "utf-8").decode("unicode_escape")
        if kind == "num":
            take()
            return float(v) if "." in v else int(v)
        if kind == "name" and v in ("true", "false", "nil"):
            take()
            return {"true": True, "false": False, "nil": None}[v]
        if v == "{":
            return table()
        raise PlanError(f"not a literal: {v!r}")

    def table():
        take("{")
        items, keyed = [], {}
        while peek()[1] != "}":
            kind, v = peek()
            if kind == "name" and pos + 1 < len(toks) and toks[pos + 1][1] == "=":
                take()
                take("=")
                keyed[v] = value()
            elif v == "[":
                take("[")
                k = value()
                take("]")
                take("=")
                keyed[k] = value()
            else:
                items.append(value())
            if peek()[1] in (",", ";"):
                take()
            elif peek()[1] != "}":
                raise PlanError(f"expected , or }} got {peek()[1]!r}")
        take("}")
        if items and keyed:
            raise PlanError("a table mixes list items and keys")
        return items if items or not keyed else keyed

    t = value()
    if pos != len(toks):
        raise PlanError("text after the table")
    return t


def lua_table(name: str, path=PARTY_LUA):
    """A global table literal from the add-on file: the constructor after `<name> =`, up to its matching brace."""
    text = path.read_text()
    m = re.search(rf"^{re.escape(name)}\s*=\s*\{{", text, re.M)
    if not m:
        raise PlanError(f"no {name} table in {path.name}")
    start, depth, i, in_str, in_comment = m.end() - 1, 0, m.end() - 1, False, False
    while i < len(text):
        c = text[i]
        if in_comment:
            in_comment = c != "\n"
        elif in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif text.startswith("--", i):
            in_comment = True
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return parse_table(text[start:i + 1])


def plan(path=PARTY_LUA) -> dict:
    """MG_PARTY_PLAN from the add-on file."""
    p = lua_table("MG_PARTY_PLAN", path)
    for k in ("addclass", "clear", "companions", "party"):
        if k not in p:
            raise PlanError(f"MG_PARTY_PLAN has no {k}")
    for role in ROLES:
        if role not in p["party"]:
            raise PlanError(f"MG_PARTY_PLAN.party has no {role}")
    return p


def commands(p: dict, role: str) -> list[dict]:
    """The slash command's commands for a player of `role`, in order (MoongladeParty_Commands): per companion its
    addclass command, then its whisper steps (each step's whispers go together; the next step waits for a companion
    whisper containing the step's `reply`). [{"companion", "class", "addclass", "steps": [{"send": [...],
    "reply": str | None}], "whispers": [every whisper, in order]}]."""
    out = []
    for key in p["party"][role]:
        c = p["companions"][key]
        steps = [{"send": list(st["send"]), "reply": st.get("reply")} for st in c["steps"]]
        out.append({"companion": key, "class": c["class"], "addclass": p["addclass"] + c["class"], "steps": steps,
                    "whispers": [w for st in steps for w in st["send"]]})
    return out


def spec_of(whispers: list[str]) -> str | None:
    """The premade spec a companion's whispers pick (`talents spec <name>`)."""
    for w in whispers:
        if w.startswith("talents spec "):
            return w[len("talents spec "):]
    return None


def premade_specs(conf_text: str, class_word: str) -> list[str]:
    """AiPlayerbot.PremadeSpecName.<class id>.<n> values for a class (a playerbots.conf or its .dist)."""
    cid = CLASS_IDS[class_word]
    return re.findall(rf"^AiPlayerbot\.PremadeSpecName\.{cid}\.\d+\s*=\s*(.*?)\s*$", conf_text, re.M)


# m4-alt-companions (design.md §2): what is never sent to a player's own character brought along with /mg bring
ALT_FORBIDDEN = ("autogear", "talents", "init", "maintenance")


def bring_plan(path=PARTY_LUA) -> dict:
    """MG_BRING_PLAN from the add-on file (/mg bring, /mg dismiss): {"add", "remove", "steps": [{"send", "reply"}],
    "resend"}; fail-closed like plan()."""
    p = lua_table("MG_BRING_PLAN", path)
    if not isinstance(p, dict):
        raise PlanError("MG_BRING_PLAN is not a keyed table")
    for k in ("add", "remove", "steps"):
        if k not in p:
            raise PlanError(f"MG_BRING_PLAN has no {k}")
    if not isinstance(p["steps"], list) or not all(isinstance(st, dict) and isinstance(st.get("send"), list)
                                                   for st in p["steps"]):
        raise PlanError("MG_BRING_PLAN.steps is not a list of {send = {...}} steps")
    return p


def bring_commands(p: dict, name: str) -> dict:
    """The bring and dismiss lists for one character: {"add": command, "steps": [{"send", "reply"}] (whispers to it
    once it has joined), "whispers": [...], "remove": command}."""
    steps = [{"send": list(st["send"]), "reply": st.get("reply")} for st in p["steps"]]
    return {"add": p["add"] + name, "steps": steps, "whispers": [w for st in steps for w in st["send"]],
            "remove": p["remove"] + name}


def alt_forbidden(lines: list[str]) -> list[str]:
    """The lines of a command list sent to an alt that would change its gear or talents (first word in ALT_FORBIDDEN,
    or `init=`)."""
    out = []
    for line in lines:
        word = line.strip().split(" ", 1)[0].split("=", 1)[0].lower()
        if word in ALT_FORBIDDEN:
            out.append(line)
    return out
