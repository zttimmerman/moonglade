"""true-pause driver for the pause.* checks (openspec/changes/true-pause, ADR-0013).

The checks log in a harness character through the headless probe and pause the world the way the Moonglade add-on
does: an add-on whisper to the character's own name, prefix "MGPAUSE", text "1" (pause) or "0" (resume), which arrives
as CMSG_MESSAGECHAT with LANG_ADDON (ChatHandler.cpp HandleMessagechatOpcode; SendAddonMessage joins prefix and text
with a tab). mod-moonglade (modules/mod-moonglade/src/MoongladePause.cpp) answers with an add-on whisper "MGPAUSE"
"paused" (sent from the first world tick that runs paused) | "running" | "refused" | "disabled". The server's own
state comes from GM commands: `.moonglade pause status` over SOAP, and the harness read-outs `.moonglade pause unit`
and `.moonglade pause cooldown <spell>` typed by the probe character (the selected unit).

Accounts: the companion harness account and character (P.COMP_USER / P.COMP_CHAR, GM level 2, companion.Session) for
everything that needs GM commands, and a plain account (PAUSE_USER, security 0, no instant logout) for
pause.logout_unpauses, whose 20 s logout countdown is the thing under test. Never PLAYER, never HSTEST.

Every world-data choice (spells, creatures, flight path) is made by a SQL/DBC rule written here, never from memory
(CLAUDE.md section 7). Packet layouts and constants cite the pinned core (vendor/azerothcore-wotlk, paths under
src/server/)."""
from __future__ import annotations

import math
import re
import struct
import threading
import time

from . import companion as C, db, dbc, fixtures as F, paths as P, soap
from .probe import client as probe
from .probe import proto as W

# ----------------------------------------------------------------------------- protocol (pinned core)

PREFIX = "MGPAUSE"
LANG_ADDON = 0xFFFFFFFF          # shared/SharedDefines.h Language::LANG_ADDON
CMSG_ATTACKSWING = 0x141         # game/Server/Protocol/Opcodes.h
CMSG_ACTIVATETAXI = 0x1AD
SMSG_ACTIVATETAXIREPLY = 0x1AE
SMSG_FORCE_MOVE_ROOT = 0x0E8
SMSG_FORCE_MOVE_UNROOT = 0x0EA
SMSG_CAST_FAILED = 0x130
SMSG_SPELL_START = 0x131
SMSG_SPELL_GO = 0x132
SMSG_SPELL_FAILURE = 0x133
SMSG_SPELL_COOLDOWN = 0x134
SMSG_PERIODICAURALOG = 0x24E
SMSG_SPELL_FAILED_OTHER = 0x2A6
SMSG_MODIFY_COOLDOWN = 0x491
SMSG_AURA_UPDATE = 0x496
SPELL_FAILED_INTERRUPTED = 40    # shared/SharedDefines.h SpellCastResult
ERR_TAXIOK = 0                   # game/Entities/Player/Player.h ActivateTaxiReply
LOGOUT_COUNTDOWN_S = 20          # game/Server/WorldSession.h ShouldLogOut: logout 20 s after the request
REFUSAL_TEXT = "You can't pause while travelling."   # MoongladePause.cpp Pause()


def _pguid(b: bytes, i: int) -> tuple[int, int]:
    """ByteBuffer::readPackGUID: mask byte, then the non-zero bytes."""
    mask, i, g = b[i], i + 1, 0
    for k in range(8):
        if mask & (1 << k):
            g |= b[i] << (8 * k)
            i += 1
    return g, i


def parse(op: int, d: bytes) -> dict | None:
    """The packets the pause checks look at, as dicts (None for others). Layouts:
    SMSG_SPELL_START / SMSG_SPELL_GO (Spells/Spell.cpp SendSpellStart/SendSpellGo): packed item-or-caster guid, packed
    caster guid, u8 cast count, u32 spell. SMSG_SPELL_FAILURE / SMSG_SPELL_FAILED_OTHER (Spell::SendInterrupted):
    packed caster guid, u8 cast count, u32 spell, u8 result. SMSG_CAST_FAILED (Spell::WriteCastResultInfo): u8 cast
    count, u32 spell, u8 result. SMSG_PERIODICAURALOG (Unit::SendPeriodicAuraLog): packed victim, packed caster, u32
    spell. SMSG_AURA_UPDATE (AuraApplication::ClientUpdate/BuildUpdatePacket): packed target guid, u8 slot, u32 spell,
    [u8 flags, u8 level, u8 stack, packed caster unless AFLAG_CASTER 0x08, u32 max + u32 duration if AFLAG_DURATION
    0x20]. SMSG_SPELL_COOLDOWN (Unit::BuildCooldownPacket): u64 guid, u8 flags, then (u32 spell, u32 ms) pairs.
    SMSG_MODIFY_COOLDOWN (Player::ModifySpellCooldown): u32 spell, u64 guid, i32 ms. SMSG_FORCE_MOVE_(UN)ROOT
    (Unit::SendMoveRoot): packed guid, u32 counter. SMSG_ACTIVATETAXIREPLY (WorldSession::SendActivateTaxiReply): u32."""
    try:
        if op in (SMSG_SPELL_START, SMSG_SPELL_GO):
            _, i = _pguid(d, 0)
            caster, i = _pguid(d, i)
            return {"kind": "spell_start" if op == SMSG_SPELL_START else "spell_go", "caster": caster,
                    "spell": struct.unpack_from("<I", d, i + 1)[0]}
        if op in (SMSG_SPELL_FAILURE, SMSG_SPELL_FAILED_OTHER):
            caster, i = _pguid(d, 0)
            return {"kind": "spell_failure" if op == SMSG_SPELL_FAILURE else "spell_failed_other", "caster": caster,
                    "spell": struct.unpack_from("<I", d, i + 1)[0], "result": d[i + 5]}
        if op == SMSG_CAST_FAILED:
            return {"kind": "cast_failed", "spell": struct.unpack_from("<I", d, 1)[0], "result": d[5]}
        if op == SMSG_PERIODICAURALOG:
            victim, i = _pguid(d, 0)
            caster, i = _pguid(d, i)
            return {"kind": "periodic", "victim": victim, "caster": caster, "spell": struct.unpack_from("<I", d, i)[0]}
        if op == SMSG_AURA_UPDATE:
            g, i = _pguid(d, 0)
            slot, sid = d[i], struct.unpack_from("<I", d, i + 1)[0]
            ev = {"kind": "aura_update", "guid": g, "slot": slot, "spell": sid}
            if sid:
                fl = d[i + 5]
                j = i + 8
                if not fl & 0x08:
                    _, j = _pguid(d, j)
                if fl & 0x20:
                    ev["max"], ev["duration"] = struct.unpack_from("<ii", d, j)
                ev["flags"] = fl
            return ev
        if op == SMSG_SPELL_COOLDOWN:
            g, fl = struct.unpack_from("<QB", d, 0)
            pairs = [struct.unpack_from("<II", d, k) for k in range(9, len(d) - 7, 8)]
            return {"kind": "spell_cooldown", "guid": g, "flags": fl, "cooldowns": [{"spell": s, "ms": m} for s, m in pairs]}
        if op == SMSG_MODIFY_COOLDOWN:
            s, g, ms = struct.unpack_from("<IQi", d, 0)
            return {"kind": "modify_cooldown", "spell": s, "guid": g, "ms": ms}
        if op in (SMSG_FORCE_MOVE_ROOT, SMSG_FORCE_MOVE_UNROOT):
            g, _ = _pguid(d, 0)
            return {"kind": "root" if op == SMSG_FORCE_MOVE_ROOT else "unroot", "guid": g}
        if op == SMSG_ACTIVATETAXIREPLY:
            return {"kind": "taxi_reply", "reply": struct.unpack_from("<I", d, 0)[0]}
    except (IndexError, struct.error) as e:
        return {"kind": "parse_error", "op": hex(op), "error": str(e)}
    return None


class Tap:
    """Records the parsed packets above (with receive time) from a pumped probe session. Installed by wrapping the
    session's own dispatch (the probe keeps parsing everything it parses today). Each packet also gets "seq", the
    number of probe events emitted before it: packets that arrive in one read share a timestamp, so whether a packet
    came before or after an event (the server's "paused" answer) is told by order, not time: seq > event["i"]."""

    def __init__(self, ws: probe.WorldSession):
        self.packets: list[dict] = []
        self._lock = threading.Lock()
        orig = ws._dispatch

        def dispatch(op, data):
            p = parse(op, data)
            if p is not None:
                with self._lock:
                    self.packets.append(p | {"t": round(time.time(), 3), "seq": ws.mark()})
            return orig(op, data)
        ws._dispatch = dispatch

    def between(self, t0: float, t1: float | None = None, kind: str | None = None, **match) -> list[dict]:
        with self._lock:
            return [p for p in self.packets if p["t"] >= t0 and (t1 is None or p["t"] <= t1)
                    and (kind is None or p["kind"] == kind) and all(p.get(k) == v for k, v in match.items())]

    def after(self, i0: int, i1: int | None = None, kind: str | None = None, **match) -> list[dict]:
        """Packets that arrived after probe event i0 (and before event i1, if given), in stream order."""
        with self._lock:
            return [p for p in self.packets if p["seq"] > i0 and (i1 is None or p["seq"] <= i1)
                    and (kind is None or p["kind"] == kind) and all(p.get(k) == v for k, v in match.items())]


# ----------------------------------------------------------------------------- spells (Spell.dbc rules)

# Spell.dbc field indices = the DBC column comments in shared/DataStores/DBCStructure.h struct SpellEntry
S_ID, S_CATEGORY, S_ATTR, S_STANCES, S_FOCUS, S_CASTER_AURA_STATE, S_CASTER_AURA_SPELL = 0, 1, 4, 12, 18, 20, 24
S_CAST_TIME_INDEX, S_RECOVERY, S_CATEGORY_RECOVERY, S_PROC_FLAGS, S_DURATION_INDEX = 28, 29, 30, 34, 40
S_MANA_COST, S_TOTEM, S_REAGENT, S_EQUIPPED_CLASS = 42, (50, 51), slice(52, 60), 68
S_EFFECT, S_EFFECT_BASE_POINTS, S_TARGET_A, S_AURA, S_AMPLITUDE = slice(71, 74), 80, slice(86, 89), slice(95, 98), 98
S_NAME, S_MANA_PCT, S_START_RECOVERY_CATEGORY, S_START_RECOVERY_TIME = 136, 204, 205, 206
SPELL_EFFECT_APPLY_AURA = 6      # shared/SharedDefines.h SpellEffects
TARGET_UNIT_CASTER = 1           # shared/SharedDefines.h Targets
SPELL_ATTR0_PASSIVE = 0x40       # shared/SharedDefines.h SpellAttr0
SPELL_ATTR0_COOLDOWN_ON_EVENT = 0x02000000
AURA_PERIODIC_DAMAGE, AURA_DUMMY, AURA_MOD_STEALTH, AURA_MOD_STAT = 3, 4, 16, 29   # Spells/Auras/SpellAuraDefines.h

PLAIN = ("one effect, SPELL_EFFECT_APPLY_AURA; no power cost (mana cost and mana %), reagents, totems, spell focus, "
         "stance, equipped item (-1), caster aura state or caster aura; not passive (SPELL_ATTR0_PASSIVE); no spell "
         "script (world.spell_script_names)")
SPELL_RULES = {
    "buff": "the lowest Spell.dbc ID with " + PLAIN + "; aura SPELL_AURA_MOD_STAT on TARGET_UNIT_CASTER; duration "
            "(SpellDuration.dbc) 60-600 s; no proc flags",
    "dot": "the lowest Spell.dbc ID with " + PLAIN + "; aura SPELL_AURA_PERIODIC_DAMAGE, base points >= 0, period "
           "(EffectAmplitude) 1-3 s, duration 30-120 s",
    "player_cd": "the lowest Spell.dbc ID with " + PLAIN + "; aura SPELL_AURA_DUMMY or SPELL_AURA_MOD_STAT on "
                 "TARGET_UNIT_CASTER; cast time 0 (SpellCastTimes.dbc); cooldown max(RecoveryTime, "
                 "CategoryRecoveryTime) 30-300 s; no global cooldown category; no proc flags",
    "creature_cd": "the lowest Spell.dbc ID with " + PLAIN + "; on TARGET_UNIT_CASTER; SPELL_ATTR0_COOLDOWN_ON_EVENT "
                   "(the caster's cooldown starts when the aura goes: Spells/Auras/SpellAuras.cpp _UnapplyForTarget -> "
                   "Creature::AddSpellCooldown); cast time 0; RecoveryTime 30-300 s",
    "cast": "the lowest Spell.dbc ID with " + PLAIN + "; aura SPELL_AURA_DUMMY on TARGET_UNIT_CASTER; cast time 2-5 s; "
            "no RecoveryTime, CategoryRecoveryTime or category",
}
_SPELLS: dict | None = None


def _i32(v: int) -> int:
    return struct.unpack("<i", struct.pack("<I", v))[0]


def pick_spells() -> dict:
    """SPELL_RULES applied to the client's Spell.dbc, SpellDuration.dbc and SpellCastTimes.dbc (data/client-data/dbc)
    and world.spell_script_names. Returns {role: {"id", "name", "duration_ms", "cast_ms", "cooldown_ms", "rule"}}."""
    global _SPELLS
    if _SPELLS is not None:
        return _SPELLS
    recs, strs = dbc.read_dbc("Spell.dbc")
    dur = {r[0]: _i32(r[1]) for r in dbc.read_dbc("SpellDuration.dbc")[0]}       # SpellDurationEntry: ID, Duration[0]
    cast = {r[0]: _i32(r[1]) for r in dbc.read_dbc("SpellCastTimes.dbc")[0]}     # SpellCastTimesEntry: ID, CastTime
    scripted = {int(r[0]) for r in db.query("SELECT DISTINCT spell_id FROM spell_script_names WHERE spell_id > 0", "world")}

    def plain(r):
        return (r[S_MANA_COST] == 0 and r[S_MANA_PCT] == 0 and not any(r[S_REAGENT]) and not any(r[t] for t in S_TOTEM)
                and r[S_FOCUS] == 0 and r[S_STANCES] == 0 and _i32(r[S_EQUIPPED_CLASS]) == -1
                and r[S_CASTER_AURA_STATE] == 0 and r[S_CASTER_AURA_SPELL] == 0 and not r[S_ATTR] & SPELL_ATTR0_PASSIVE
                and r[S_EFFECT] == (SPELL_EFFECT_APPLY_AURA, 0, 0) and r[0] not in scripted)

    def info(r, role):
        return {"id": r[S_ID], "name": dbc.dbc_string(strs, r[S_NAME]), "duration_ms": dur.get(r[S_DURATION_INDEX], 0),
                "cast_ms": cast.get(r[S_CAST_TIME_INDEX], 0), "cooldown_ms": max(r[S_RECOVERY], r[S_CATEGORY_RECOVERY]),
                "period_ms": r[S_AMPLITUDE], "rule": SPELL_RULES[role]}

    tests = {
        "buff": lambda r: (r[S_AURA][0] == AURA_MOD_STAT and r[S_TARGET_A][0] == TARGET_UNIT_CASTER
                           and 60000 <= dur.get(r[S_DURATION_INDEX], 0) <= 600000 and r[S_PROC_FLAGS] == 0),
        "dot": lambda r: (r[S_AURA][0] == AURA_PERIODIC_DAMAGE and _i32(r[S_EFFECT_BASE_POINTS]) >= 0
                          and 1000 <= r[S_AMPLITUDE] <= 3000 and 30000 <= dur.get(r[S_DURATION_INDEX], 0) <= 120000),
        "player_cd": lambda r: (r[S_AURA][0] in (AURA_DUMMY, AURA_MOD_STAT) and r[S_TARGET_A][0] == TARGET_UNIT_CASTER
                                and cast.get(r[S_CAST_TIME_INDEX], 0) == 0
                                and 30000 <= max(r[S_RECOVERY], r[S_CATEGORY_RECOVERY]) <= 300000
                                and r[S_START_RECOVERY_CATEGORY] == 0 and r[S_PROC_FLAGS] == 0),
        "creature_cd": lambda r: (r[S_ATTR] & SPELL_ATTR0_COOLDOWN_ON_EVENT and r[S_TARGET_A][0] == TARGET_UNIT_CASTER
                                  and cast.get(r[S_CAST_TIME_INDEX], 0) == 0 and 30000 <= r[S_RECOVERY] <= 300000),
        "cast": lambda r: (r[S_AURA][0] == AURA_DUMMY and r[S_TARGET_A][0] == TARGET_UNIT_CASTER
                           and 2000 <= cast.get(r[S_CAST_TIME_INDEX], 0) <= 5000 and r[S_RECOVERY] == 0
                           and r[S_CATEGORY_RECOVERY] == 0 and r[S_CATEGORY] == 0),
    }
    out = {}
    for r in sorted(recs, key=lambda r: r[S_ID]):
        if not plain(r):
            continue
        for role, ok in tests.items():
            if role not in out and ok(r):
                out[role] = info(r, role)
        if len(out) == len(tests):
            break
    missing = sorted(set(tests) - set(out))
    if missing:
        raise C.CompanionError(f"no spell matches the rule for {missing}", rules={k: SPELL_RULES[k] for k in missing})
    _SPELLS = out
    return out


# ----------------------------------------------------------------------------- the session

NUM = r"(-?\d+(?:\.\d+)?)"


class PauseError(C.CompanionError):
    pass


class Session(C.Session):
    """companion.Session (P.COMP_CHAR in the world, pumped) plus the pause driver. On exit the world is resumed
    (`.moonglade pause off` over SOAP) before the companion session's own cleanup and logout."""

    def __enter__(self):
        super().__enter__()
        self.tap = Tap(self.ws)
        return self

    def __exit__(self, *exc):
        try:
            self.ev["resume_at_exit"] = soap.gm("moonglade pause off", timeout=10).evidence()
        except OSError as e:
            self.ev["resume_at_exit"] = f"SOAP unreachable: {e}"
        super().__exit__(*exc)

    # -- server read-outs (the probe character's own GM commands; the reply line is matched, with its receive time)
    def query(self, cmd: str, pattern: str, select: int | None = None, timeout: float = 8.0) -> tuple:
        """Type `cmd` (with `select` as the selected unit) and wait for the first system line matching `pattern`.
        Returns (match or None, receive time, the system lines seen, the matching event's index or None)."""
        if select is not None:
            self.ws.set_selection(select)
        since = self.ws.mark()
        self.ws.chat(cmd)
        seen = []
        rx = re.compile(pattern)
        deadline = time.time() + timeout
        while time.time() < deadline:
            e = self.ws.wait_event(lambda e: (e["kind"] == "notification" or (e["kind"] == "chat" and e["type"] == W.CHAT_MSG_SYSTEM)),
                                   since, timeout=max(0.05, deadline - time.time()))
            if e is None:
                break
            since = e["i"] + 1
            text = e.get("text") if e["kind"] == "notification" else e["msg"]
            seen.append(text)
            m = rx.search(text or "")
            if m:
                return m, e["t"], seen, e["i"]
        return None, time.time(), seen, None

    def unit(self, guid: int) -> dict:
        """`.moonglade pause unit` on `guid`: {"health", "power", "powertype", "pos", "spell", "channel", "combat", "t"}."""
        m, t, seen, _ = self.query(".moonglade pause unit", rf"Moonglade unit: guid (\d+) health (\d+) power (\d+) powertype (\d+) "
                                rf"x {NUM} y {NUM} z {NUM} spell (\d+) channel (\d+) combat (\d)", select=guid)
        if not m or int(m.group(1)) != guid:
            raise PauseError("no `.moonglade pause unit` read-out for the unit", guid=guid, seen=seen[-5:])
        return {"health": int(m.group(2)), "power": int(m.group(3)), "powertype": int(m.group(4)),
                "pos": {"x": float(m.group(5)), "y": float(m.group(6)), "z": float(m.group(7))},
                "spell": int(m.group(8)), "channel": int(m.group(9)), "combat": int(m.group(10)), "t": t}

    def cooldown(self, guid: int, spell: int) -> dict:
        m, t, seen, _ = self.query(f".moonglade pause cooldown {spell}", rf"Moonglade cooldown: spell {spell} remaining (\d+) ms",
                                select=guid)
        if not m:
            raise PauseError("no `.moonglade pause cooldown` read-out", guid=guid, spell=spell, seen=seen[-5:])
        return {"ms": int(m.group(1)), "t": t}

    def aura(self, guid: int, spell: int, settle: float = 0.6) -> dict:
        """`.list auras id <spell>` (scripts/Commands/cs_list.cpp ListAurasCommand: the header LANG_COMMAND_TARGET_LISTAURAS
        'Target unit has {} auras:' counts all auras, then one LANG_COMMAND_TARGET_AURADETAIL line 'id: {} ... duration:
        {} maxduration: {}' per matching aura): {"duration" (ms; None when no line for the spell follows the header
        within `settle` s), "t"}."""
        m, t, seen, i = self.query(f".list auras id {spell}", r"Target unit has \d+ auras:", select=guid)
        if not m:
            raise PauseError("no `.list auras id` read-out", guid=guid, spell=spell, seen=seen[-5:])
        e = self.ws.wait_event(lambda e: e["kind"] == "chat" and e["type"] == W.CHAT_MSG_SYSTEM
                               and re.search(rf"^id: {spell} .*?duration: (-?\d+) maxduration", e["msg"] or ""),
                               i + 1, timeout=settle)
        if not e:
            return {"duration": None, "t": t}
        d = re.search(rf"^id: {spell} .*?duration: (-?\d+) maxduration", e["msg"])
        return {"duration": int(d.group(1)), "t": e["t"]}

    # -- the pause, the way the add-on asks for it
    def request(self, on: bool, want: tuple[str, ...] | None = None, timeout: float = 10.0) -> dict:
        """Send the add-on message ("1" pause / "0" resume) and wait for the server's add-on answer. Returns
        {"sent_t", "sent_i" (events before the request), "reply", "reply_t", "reply_i" (the answer's event index)};
        reply None on timeout."""
        since = self.ws.mark()
        t = time.time()
        self.ws.chat(f"{PREFIX}\t{'1' if on else '0'}", W.CHAT_MSG_WHISPER, to=C._id()[2], lang=LANG_ADDON)
        e = self.ws.wait_event(lambda e: e["kind"] == "chat" and e["type"] == W.CHAT_MSG_WHISPER and e["lang"] == -1
                               and e["msg"].startswith(PREFIX + "\t")
                               and (want is None or e["msg"].split("\t", 1)[1] in want), since, timeout)
        return {"sent_t": round(t, 3), "sent_i": since, "reply": e["msg"].split("\t", 1)[1] if e else None,
                "reply_t": e["t"] if e else None, "reply_i": e["i"] if e else None}

    def pause(self) -> dict:
        r = self.request(True, ("paused", "refused", "disabled"))
        if r["reply"] != "paused":
            raise PauseError(f"the server didn't answer the pause request with \"paused\" (got {r['reply']!r})", request=r,
                             status=status())
        return r

    def resume(self) -> dict:
        r = self.request(False, ("running",))
        if r["reply"] != "running":
            raise PauseError("the server didn't answer the resume request with \"running\"", request=r, status=status())
        return r

    # -- a fight
    def engage(self, exclude: set | None = None, swing: bool = True, timeout: float = 15.0, creature: dict | None = None) -> dict:
        """GIVEN "in melee with a creature": a live, unhurt creature by companion.CREATURE_RULE; the character is put on
        its live position (`.go xyz`), faces it and auto-attacks (CMSG_ATTACKSWING), and the creature attacks the
        character (SMSG_ATTACKSTART or a melee hit with it as attacker and the character as victim)."""
        self.ws.set_selection(self.guid)      # `.npc info <spawn>` reads the selected creature if there is one
        cr = creature or self.pick_live_creature(exclude)
        p = cr["npc_info"]["pos"]
        ev = {"creature": {k: cr[k] for k in ("spawn_id", "entry", "name", "guid")} | {"pos": p}, "rule": C.CREATURE_RULE}
        since = self.ws.mark()
        hit = None
        deadline = time.time() + timeout
        tries = 0
        while time.time() < deadline and not hit:
            tries += 1
            mark = self.ws.mark()
            self.ws.command(f".go xyz {p['x']:.2f} {p['y']:.2f} {p['z']:.2f}", settle=0.3, timeout=4)
            tp = self.ws.wait_event(lambda e: e["kind"] == "teleport_near", mark, timeout=4)
            if tp:
                self.ws.heartbeat(tp["x"], tp["y"], tp["z"])
            hit = self.ws.wait_event(lambda e: e["kind"] in ("attack_start", "melee_damage") and e.get("victim") == self.guid
                                     and e.get("attacker") == cr["guid"], since, timeout=3)
            if not hit:
                p = self.npc_info(cr["spawn_id"])["pos"] or p
        ev["tries"] = tries
        if not hit:
            raise PauseError("precondition: the creature never attacked the character", **ev)
        ev["attacked"] = hit
        if swing:
            me = self.unit(self.guid)["pos"]
            o = math.atan2(p["y"] - me["y"], p["x"] - me["x"]) % (2 * math.pi)
            self.ws.heartbeat(me["x"], me["y"], me["z"], o)
            self.ws.set_selection(cr["guid"])
            self.ws.send(CMSG_ATTACKSWING, struct.pack("<Q", cr["guid"]))
        return ev | {"guid": cr["guid"], "spawn_id": cr["spawn_id"], "pos": p}

    def run_to(self, frm: dict, to: dict, step_yd: float = 3.0, step_s: float = 0.1) -> dict:
        """Move on foot from `frm` to `to` the way a client reports a run: MSG_MOVE_HEARTBEAT with MOVEMENTFLAG_FORWARD
        (0x1, game/Entities/Unit/UnitDefines.h) every `step_s` along the straight line, then one standing heartbeat.
        A GM teleport would end the fight (Player::TeleportTo calls CombatStop unless TELE_TO_NOT_LEAVE_COMBAT,
        game/Entities/Player/Player.cpp), so a kite has to be a run. z is interpolated between the two ends."""
        d = math.hypot(to["x"] - frm["x"], to["y"] - frm["y"])
        o = math.atan2(to["y"] - frm["y"], to["x"] - frm["x"]) % (2 * math.pi)
        n = max(1, math.ceil(d / step_yd))
        for k in range(1, n + 1):
            f = k / n
            x, y, z = (frm[a] + (to[a] - frm[a]) * f for a in ("x", "y", "z"))
            self.ws.send(W.MSG_MOVE_HEARTBEAT, W.pack_guid(self.guid)
                         + struct.pack("<IHIffffI", 0x1 if k < n else 0, 0, int(time.monotonic() * 1000) & 0xFFFFFFFF,
                                       x, y, z, o, 0))
            time.sleep(step_s)
        return {"from": {a: round(frm[a], 2) for a in ("x", "y", "z")}, "to": {a: round(to[a], 2) for a in ("x", "y", "z")},
                "yd": round(d, 1), "steps": n}

    def swings(self, i0: int, i1: int | None = None) -> list[dict]:
        """Melee hits (SMSG_ATTACKERSTATEUPDATE) after probe event i0 and before event i1, in stream order."""
        return [e for e in self.ws.events_since(i0 + 1) if e["kind"] == "melee_damage" and (i1 is None or e["i"] < i1)]


# ----------------------------------------------------------------------------- status and lost time

def status() -> dict:
    """`.moonglade pause status` over SOAP: {"state", "paused_total_ms", "enabled", "t"}."""
    r = soap.gm("moonglade pause status", timeout=10)
    m = re.search(r"Moonglade pause: (paused|running) \(paused total (\d+) ms, enabled (\d)\)", r.output or "")
    return {"state": m.group(1) if m else None, "paused_total_ms": int(m.group(2)) if m else None,
            "enabled": int(m.group(3)) if m else None, "t": round(time.time(), 3), "raw": (r.output or "")[:200],
            "ok": r.ok}


def lost_ms(before: float, t_before: float, after: float, t_after: float, paused_ms: int) -> int:
    """How much a remaining time (ms) dropped beyond the unpaused wall time between two read-outs: 0 when it froze
    for the whole pause, ~paused_ms when it kept running. paused_ms = the pause as the probe measured it."""
    unpaused = (t_after - t_before) * 1000 - paused_ms
    return round((before - after) - unpaused)


def wait_until(t: float) -> None:
    while time.time() < t:
        time.sleep(min(0.25, max(0.0, t - time.time())))


# ----------------------------------------------------------------------------- respawn and leash

RESPAWN_RULE = ("the killed creature: on the start point's map, creature_template rank 0, npcflag 0, maxlevel <= 10, "
                "not a civilian (flags_extra 0x2) and attackable and selectable (no unit_flags 0x2|0x100|0x2000000), "
                "not in a game event (game_event_creature) or a pool (pool_creature); the smallest "
                "creature.spawntimesecs, then the lowest spawn id; alive (`.npc info`). R = its `.npc info` respawn "
                "remainder right after the kill (Creature::setDeathState: respawn delay + corpse delay)")
KITE_YD = 55            # > CreatureLeashRadius 30 (worldserver.conf.dist) + the distance the creature runs before the pause
KITE_CLEAR_YD = 25


def respawn_candidates(tele: str = C.START_TELE, n: int = 5) -> list[dict]:
    t = F.tele_row(tele)
    rows = db.query(
        f"SELECT c.guid, c.id, ct.name, c.spawntimesecs, c.position_x, c.position_y, c.position_z FROM creature c "
        f"JOIN creature_template ct ON ct.entry = c.id WHERE c.map = {t['map']} AND ct.rank = 0 AND ct.npcflag = 0 "
        f"AND ct.maxlevel <= 10 AND (ct.flags_extra & 2) = 0 AND (ct.unit_flags & {0x2 | 0x100 | 0x2000000}) = 0 "
        f"AND NOT EXISTS (SELECT 1 FROM game_event_creature g WHERE g.guid = c.guid) "
        f"AND NOT EXISTS (SELECT 1 FROM pool_creature pc WHERE pc.guid = c.guid) "
        f"ORDER BY c.spawntimesecs, c.guid LIMIT {int(n)}", "world")
    return [{"spawn_id": int(g), "entry": int(e), "name": nm, "spawntimesecs": int(st),
             "spawn": {"x": float(x), "y": float(y), "z": float(z)}} for g, e, nm, st, x, y, z in rows]


def respawn_remaining(s: Session, spawn_id: int) -> dict:
    """`.npc info <spawn>` (scripts/Commands/cs_npc.cpp HandleNpcInfoCommand): for a creature in the world,
    LANG_NPCINFO_HEALTH '... (current): {}.' and LANG_COMMAND_RAWPAWNTIMES 'SpawnTime: Full:{} Remain:{}'
    (secsToTimeString); for a spawn whose corpse is gone and whose respawn is pending (dynamic spawns, Map
    ProcessRespawns), '(Not in world - showing DB data)'. Returns {"in_world", "health", "remain_s", "t"}."""
    # `.npc info` shows the selected creature when there is one: select ourselves
    m, t, seen, _ = s.query(f".npc info {int(spawn_id)}", r"SpawnTime: Full:(.*?) Remain:(.*)$|\(Not in world - showing DB data\)",
                            select=s.guid)
    if not m:
        raise PauseError("no respawn read-out from `.npc info`", spawn_id=spawn_id, seen=seen[-6:])
    if m.group(1) is None:
        return {"in_world": False, "health": 0, "remain_s": None, "t": t}
    hp = next((re.search(r"\(max\): (\d+)\. \(current\): (\d+)\.", x) for x in seen if "(current)" in x), None)
    return {"in_world": True, "remain_s": _secs(m.group(2)), "remain_text": m.group(2).strip(), "full": m.group(1).strip(),
            "health": int(hp.group(2)) if hp else None, "t": t}


def _secs(text: str) -> int | None:
    """secsToTimeString (common/Utilities/Util.cpp, shortText): 'N Day(s) N Hour(s) N Minute(s) N Second(s)' or the
    short form 'Nd Nh Nm Ns'."""
    tot, found = 0, False
    for n, u in re.findall(r"(\d+)\s*([A-Za-z]+)", text):
        k = u[0].lower()
        mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}.get(k)
        if mult:
            tot += int(n) * mult
            found = True
    return tot if found else (0 if text.strip() in ("", "0") else None)


def kite_point(pos: dict, toward: dict, map_id: int, dist: float = KITE_YD) -> dict:
    """A point `dist` yd from the creature's position, bearings tried in 45-degree steps starting toward the start
    point, the first with no hostile spawn within KITE_CLEAR_YD (as companion.staging_point)."""
    recs, _ = dbc.read_dbc("FactionTemplate.dbc")
    fac = ",".join(str(r[0]) for r in recs if r[C.FT_HOSTILE_MASK] & C.FACTION_MASK_PLAYER)
    cx, cy, r = pos["x"], pos["y"], dist + KITE_CLEAR_YD
    spawns = [(float(x), float(y)) for x, y in db.query(
        f"SELECT c.position_x, c.position_y FROM creature c JOIN creature_template ct ON ct.entry = c.id "
        f"WHERE c.map = {int(map_id)} AND ct.faction IN ({fac}) AND c.position_x BETWEEN {cx - r} AND {cx + r} "
        f"AND c.position_y BETWEEN {cy - r} AND {cy + r}", "world")]
    base = math.atan2(toward["y"] - cy, toward["x"] - cx)
    for k in range(8):
        a = base + k * math.pi / 4
        x, y = cx + dist * math.cos(a), cy + dist * math.sin(a)
        if all(math.hypot(sx - x, sy - y) >= KITE_CLEAR_YD for sx, sy in spawns):
            return {"x": round(x, 2), "y": round(y, 2), "bearing_deg": round(math.degrees(a) % 360), "dist_yd": dist}
    raise PauseError("no kite point matches the rule")


# ----------------------------------------------------------------------------- flight path

TAXI_RULE = ("the flight master (creature_template.npcflag & UNIT_NPC_FLAG_FLIGHTMASTER 0x2000, "
             "game/Entities/Unit/UnitDefines.h) on the start point's map nearest the start point; its node = the "
             "TaxiNodes.dbc node on that map nearest the flight master; the destination = the TaxiPath.dbc path from "
             "that node with the lowest ID")
UNIT_NPC_FLAG_FLIGHTMASTER = 0x2000


def pick_taxi(tele: str = C.START_TELE) -> dict:
    t = F.tele_row(tele)
    rows = db.query(
        f"SELECT c.guid, c.id, ct.name, c.position_x, c.position_y, c.position_z, "
        f"SQRT(POW(c.position_x-({t['x']}),2)+POW(c.position_y-({t['y']}),2)) d FROM creature c "
        f"JOIN creature_template ct ON ct.entry = c.id WHERE c.map = {t['map']} AND (ct.npcflag & {UNIT_NPC_FLAG_FLIGHTMASTER}) "
        f"ORDER BY d LIMIT 1", "world")
    if not rows:
        raise PauseError("no flight master matches the rule", rule=TAXI_RULE)
    g, e, nm, x, y, z, _ = rows[0]
    fm = {"spawn_id": int(g), "entry": int(e), "name": nm, "pos": {"x": float(x), "y": float(y), "z": float(z)}}
    nodes = []
    for r in dbc.read_dbc("TaxiNodes.dbc")[0]:          # TaxiNodesEntry: 0 ID, 1 map_id, 2-4 x y z (floats)
        if r[1] == t["map"]:
            nx, ny, nz = struct.unpack("<fff", struct.pack("<III", r[2], r[3], r[4]))
            nodes.append((math.hypot(nx - fm["pos"]["x"], ny - fm["pos"]["y"]), r[0]))
    if not nodes:
        raise PauseError("no taxi node on the map", rule=TAXI_RULE)
    src = min(nodes)[1]
    paths = sorted(r for r in dbc.read_dbc("TaxiPath.dbc")[0] if r[1] == src)   # TaxiPathEntry: ID, from, to, price
    if not paths:
        raise PauseError("no TaxiPath.dbc path from the node", node=src, rule=TAXI_RULE)
    return {"flightmaster": fm, "from": src, "to": paths[0][2], "path": paths[0][0], "price": paths[0][3], "rule": TAXI_RULE}


# ----------------------------------------------------------------------------- the plain account (logout)

PAUSE_USER, PAUSE_PASS, PAUSE_CHAR = "HSPAUSE", "hspause", "Hspause"


def ensure_plain_account() -> dict:
    """PAUSE_USER through the server (SOAP `account create`, security 0) and its human warrior PAUSE_CHAR (probe
    CMSG_CHAR_CREATE), placed at the start point offline (`.tele name`)."""
    ev = {}
    q = lambda: db.scalar(f"SELECT id FROM account WHERE username='{PAUSE_USER}'", "auth")     # noqa: E731
    if not q():
        ev["create"] = C.gm(f"account create {PAUSE_USER} {PAUSE_PASS}").evidence()
        _, ok, _ = F.poll(q, bool, timeout=15)
        if not ok:
            raise PauseError("plain account not in auth.account after `account create`", **ev)
    acct = int(q())
    ev["account_access"] = F.access_rows(acct)
    row = lambda: db.query(f"SELECT guid, online FROM characters WHERE name='{PAUSE_CHAR}'", "characters")  # noqa: E731
    if not row():
        _, ws, _ = probe.open_world(PAUSE_USER, PAUSE_PASS)
        with ws:
            code = ws.char_create(PAUSE_CHAR, F.RACE_HUMAN, F.CLASS_WARRIOR)
            if code != W.CHAR_CREATE_SUCCESS:
                raise PauseError("CMSG_CHAR_CREATE rejected", result=f"0x{code:02X}")
        _, ok, _ = F.poll(row, bool, timeout=15)
        if not ok:
            raise PauseError("plain character not in the characters DB after CHAR_CREATE_SUCCESS")
        ev["created"] = True
    _, ok, _ = F.poll(lambda: row()[0][1], lambda v: v == "0", timeout=30)
    if not ok:
        raise PauseError("plain character still online", **ev)
    ev["tele"] = C.gm(f"tele name {PAUSE_CHAR} {C.START_TELE}").evidence()
    return ev | {"account": PAUSE_USER, "id": acct, "character": PAUSE_CHAR}
