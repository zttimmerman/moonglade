"""m4-dungeon-companions task 1.1 (prototype): the run driver's pure rules in hslib/dungeon.py (design.md §4): encounter
order and the required set, the staging point rule (candidate, `.mmap path` parse, primary verdict, the path-point
fallback), the probe's create-block parser that reads `.mmap path`'s waypoints, and the wipe accounting. Stand-in
facts and packets with made-up values; the live run is the gate check dng.party_clear.

Slice 3.4 adds the gather point and walk-in, the gather readiness rule, the selfbot toggle and the probe-as-fifth-member
fight setup, each against a stand-in session that records what it was asked to do."""
import math
import os
import struct

from hslib import dungeon as D, play
from hslib import paths as P
from hslib.checks import party_checks as PCK
from hslib.probe import client as PC

from .base import UnitCase


def enc(eid, order, bit, spawns=True, name="B"):
    sp = [{"spawn_id": eid * 10, "x": 0.0, "y": 0.0, "z": 0.0, "name": name, "level": 1, "rank": 1}] if spawns else []
    return {"encounter": eid, "order": order, "bit": bit, "credit_entry": eid + 1000, "spawns": sp}


class Order(UnitCase):
    def test_required_by_order_index_and_event_bosses_skipped(self):
        f = {"encounters": [enc(3, 750, 2), enc(1, 0, 0), enc(2, 500, 1, spawns=False)]}
        req, skipped = D.required_encounters(f)
        self.assertEqual([e["encounter"] for e in req], [1, 3])
        self.assertEqual([s["encounter"] for s in skipped], [2])
        self.assertIn("no static spawn", skipped[0]["why"])


class Staging(UnitCase):
    def test_candidate_is_30_yd_back_toward_previous(self):
        c = D.stage_candidate({"x": 0, "y": 0, "z": 0}, {"x": 100, "y": 0, "z": 10})
        self.assertAlmostEqual(c["x"], 30)
        self.assertAlmostEqual(c["y"], 0)
        self.assertAlmostEqual(c["z"], 3)          # the height follows the line
        near = D.stage_candidate({"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 20, "z": 4})
        self.assertAlmostEqual(near["y"], 30)       # beyond a closer previous point, along the same ray
        self.assertAlmostEqual(near["z"], 4)

    RAW = ["mmap path:", "Boss's path to Probe:", "Building: SmoothPath", "Result: true - Length: 27 - Type: 1",
           "StartPosition     (-94.98, -828.17, 38.63)", "EndPosition       (-56.67, -790.28, 42.99)",
           "ActualEndPosition (-56.67, -790.28, 38.95)"]

    def test_parse_mmap_path(self):
        p = D.parse_mmap_path(self.RAW)
        self.assertEqual((p["result"], p["length"], p["type"]), (True, 27, 1))
        self.assertEqual(p["end"], {"x": -56.67, "y": -790.28, "z": 42.99})
        self.assertEqual(p["actual_end"], {"x": -56.67, "y": -790.28, "z": 38.95})
        self.assertAlmostEqual(p["start"]["x"], -94.98)
        self.assertFalse(D.parse_mmap_path(["NavMesh not loaded for current map."])["nav_loaded"])

    def test_primary_only_when_snapped_within_10_yd(self):
        boss = {"x": 0, "y": 0, "z": 0}
        cand = {"x": 30, "y": 0, "z": 0}
        ok = D.judge_stage(boss, cand, {"type": 1, "actual_end": {"x": 28, "y": 3, "z": 0}})
        self.assertEqual(ok["verdict"], "primary")
        far = D.judge_stage(boss, cand, {"type": 0x84, "actual_end": {"x": 12, "y": 0, "z": 0}})
        self.assertIsNone(far["verdict"])
        self.assertIn("off the candidate", far["why"])
        # counterexample: a NOPATH answer is refused even when its end equals the candidate (shortcut through rock)
        nopath = D.judge_stage(boss, cand, {"type": D.PATH_NOPATH, "actual_end": dict(cand)})
        self.assertIsNone(nopath["verdict"])

    def test_fallback_last_point_at_least_25_yd_walking_in(self):
        boss = {"x": 0, "y": 0, "z": 0}
        # boss -> previous staging point; the path doubles back, so the last >= 25 yd point walking in is at x=26,
        # not the first one past 25 yd from the boss's side (x=30 then back to 20)
        pts = [{"x": x, "y": 0, "z": 0} for x in (2, 10, 20, 26, 22, 30, 50, 80)]
        self.assertEqual(D.fallback_point(pts, boss), {"x": 26, "y": 0, "z": 0})
        self.assertIsNone(D.fallback_point([{"x": 5, "y": 0, "z": 0}], boss))

    def test_walk_distance_along_the_path(self):
        pts = [{"x": 0, "y": 0, "z": 0}, {"x": 0, "y": 10, "z": 0}, {"x": 10, "y": 10, "z": 0}, {"x": 10, "y": 0, "z": 0}]
        self.assertEqual(D.walk_to(pts, {"x": 10, "y": 1, "z": 0}), 30.0)   # 10 yd away straight, 30 on foot
        self.assertIsNone(D.walk_to(pts, {"x": 50, "y": 50, "z": 0}))     # not on the path at all
        self.assertIsNone(D.walk_to([], {"x": 0, "y": 0, "z": 0}))

    def test_approach_walks_the_path_toward_the_boss(self):
        boss = {"x": 0, "y": 0, "z": 0}
        pts = [{"x": 4 * k, "y": 0, "z": 0} for k in range(11)]        # boss -> previous staging point, 4 yd apart
        got = D.approach_points(pts, {"x": 31, "y": 1, "z": 0}, boss)   # the stage is nearest point 8 (x=32)
        self.assertEqual([p["x"] for p in got], [24, 16, 8])            # every 2nd point; stops 8 yd from the boss
        self.assertEqual(D.approach_points([], {"x": 1, "y": 0, "z": 0}, boss), [])

    def test_path_points_ordered_by_guid_counter_and_waypoints_only(self):
        def g(entry, counter):
            return (0xF130 << 48) | (entry << 24) | counter
        creates = [{"guid": g(1, 7), "pos": {"x": 3, "y": 0, "z": 0, "o": 0}},
                   {"guid": g(1, 5), "pos": {"x": 1, "y": 0, "z": 0, "o": 0}},
                   {"guid": g(644, 6), "pos": {"x": 99, "y": 0, "z": 0}},     # the boss itself, not a waypoint
                   {"guid": g(1, 5), "pos": {"x": 1, "y": 0, "z": 0}}]        # the same create twice
        self.assertEqual([p["x"] for p in D.path_points(creates)], [1, 3])


def _packed(guid: int) -> bytes:
    mask, body = 0, b""
    for i in range(8):
        b = (guid >> (8 * i)) & 0xFF
        if b:
            mask |= 1 << i
            body += bytes([b])
    return bytes([mask]) + body


def _create_living(guid, x, y, z, spline_nodes=0) -> bytes:
    mf = PC.MF_SPLINE_ENABLED if spline_nodes else 0
    out = bytes([PC.UPDATETYPE_CREATE_OBJECT]) + _packed(guid) + bytes([3])          # TYPEID_UNIT
    out += struct.pack("<H", PC.UF_LIVING | PC.UF_LOWGUID | PC.UF_HAS_TARGET)
    out += struct.pack("<IHI4f", mf, 0, 1234, x, y, z, 1.0) + struct.pack("<I", 0) + struct.pack("<9f", *[1.0] * 9)
    if spline_nodes:
        out += struct.pack("<I", PC.SPLINE_FINAL_POINT) + struct.pack("<3f", 1, 2, 3)
        out += struct.pack("<iiIffff", 0, 1000, 7, 1, 1, 0, 0) + struct.pack("<I", spline_nodes)
        out += struct.pack(f"<{3 * spline_nodes}f", *[0.5] * (3 * spline_nodes)) + b"\x00" + struct.pack("<3f", 4, 5, 6)
    out += struct.pack("<I", 0x0B) + b"\x00"                                         # LOWGUID, HAS_TARGET (none)
    out += bytes([1]) + struct.pack("<I", 0b101) + struct.pack("<II", 11, 22)        # values: fields 0 and 2
    return out


class CreateBlocks(UnitCase):
    def test_reads_every_create_with_position(self):
        g1, g2 = (0xF130 << 48) | (1 << 24) | 5, (0xF130 << 48) | (644 << 24) | 6
        body = _create_living(g2, 9, 8, 7, spline_nodes=2) + _create_living(g1, -1.5, 2.25, 3.0)
        blocks, stop = PC.parse_update_blocks(struct.pack("<I", 2) + body)
        self.assertIsNone(stop)
        self.assertEqual([b["guid"] for b in blocks], [g2, g1])
        self.assertEqual((blocks[1]["pos"]["x"], blocks[1]["pos"]["y"], blocks[1]["pos"]["z"]), (-1.5, 2.25, 3.0))
        self.assertEqual(blocks[1]["fields"], {0: 11, 2: 22})

    def test_counterexample_truncated_packet_keeps_what_parsed(self):
        g = (0xF130 << 48) | (1 << 24) | 5
        one = _create_living(g, 1, 2, 3)
        blocks, stop = PC.parse_update_blocks(struct.pack("<I", 2) + one + one[:9])
        self.assertEqual(len(blocks), 1)
        self.assertIn("short", stop)


class Accounting(UnitCase):
    def test_deaths_and_wipes(self):
        self.assertEqual(D.new_deaths({"a": True, "b": False}, {"a": False, "b": False}), ["a"])
        self.assertEqual(D.new_deaths({"a": True}, {"a": None}), [])
        self.assertTrue(D.is_wipe({"a": False, "b": False}))
        self.assertFalse(D.is_wipe({"a": False, "b": None}))       # unknown counts as alive
        self.assertFalse(D.is_wipe({}))

    def test_retry_until_two_wipes(self):
        self.assertEqual(D.next_step(True, 2), "next")
        self.assertEqual(D.next_step(False, 1), "retry")
        self.assertEqual(D.next_step(False, 2), "retry")            # the second wipe still gets a third pull
        self.assertEqual(D.next_step(False, 3), "fail")
        with self.assertRaises(TypeError):
            D.next_step(False, 1, True)                              # the unused timed_out argument is gone

    def test_summary_row(self):
        rec = {"boss": "B", "encounter": 1, "killed": True, "attempts": [
            {"wipe": True, "fight_s": 40, "deaths": ["T", "H", "M", "R"], "gather": {"s": 5}},
            {"wipe": False, "fight_s": 30.5, "deaths": ["M"], "killed": True, "gather": {"s": 4}}]}
        roles = {"T": "tank", "H": "healer", "M": "damage", "R": "damage"}
        row = D.encounter_summary(rec, roles)
        self.assertEqual(row["wipes"], 1)
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["deaths_by_role"], {"tank": 1, "healer": 1, "damage": 3})
        self.assertEqual(row["time_s"], 70.5)
        self.assertEqual(row["with_gather_s"], 79.5)
        self.assertTrue(row["killed"])
        self.assertEqual(D.role_of_key("mage"), "damage")
        self.assertTrue(math.isclose(D.dist3({"x": 0, "y": 0, "z": 0}, {"x": 3, "y": 4, "z": 0}), 5))


# ----------------------------------------------------------------------------- slice 3.4

def line(n, step=4.0):
    """A straight path from the boss (x=0) away from it, `step` yd apart."""
    return [{"x": step * k, "y": 0.0, "z": 0.0} for k in range(n)]


BOSS = {"x": 0.0, "y": 0.0, "z": 0.0}


class GatherPoint(UnitCase):
    def test_gather_point_is_20_yd_farther_on_foot(self):
        pts = line(20)                                        # 0..76 yd
        g = D.gather_point(pts, {"x": 28, "y": 1, "z": 0}, BOSS)   # stage nearest x=28 (index 7)
        self.assertEqual(g["point"]["x"], 48)                 # first point >= 20 yd farther on foot
        self.assertEqual((g["index"], g["stage_index"], g["back_yd"]), (12, 7, 20.0))
        self.assertIsNone(g["why"])

    def test_counterexample_a_path_doubling_back_toward_the_boss_is_skipped(self):
        # 20 yd on foot past the stage, but the path has turned back to 8 yd from the boss: not a gather point
        pts = line(8) + [{"x": 24, "y": 4, "z": 0}, {"x": 16, "y": 4, "z": 0}, {"x": 8, "y": 4, "z": 0},
                         {"x": 8, "y": 30, "z": 0}, {"x": 8, "y": 60, "z": 0}]
        g = D.gather_point(pts, {"x": 28, "y": 0, "z": 0}, BOSS)
        self.assertGreaterEqual(D.dist3(g["point"], BOSS), D.dist3(pts[g["stage_index"]], BOSS))
        self.assertEqual(g["point"], {"x": 8, "y": 30, "z": 0})

    def test_short_path_uses_its_last_point_and_no_path_means_none(self):
        g = D.gather_point(line(10), {"x": 28, "y": 0, "z": 0}, BOSS)   # only 8 yd beyond the stage
        self.assertEqual(g["point"]["x"], 36)
        self.assertIn("last point", g["why"])
        self.assertIsNone(D.gather_point(line(8), {"x": 28, "y": 0, "z": 0}, BOSS)["point"])   # stage is the end
        self.assertIsNone(D.gather_point([], {"x": 28, "y": 0, "z": 0}, BOSS)["point"])
        off = D.gather_point(line(20), {"x": 28, "y": 40, "z": 0}, BOSS)                   # stage off the path
        self.assertIsNone(off["point"])
        self.assertIn("not on the path", off["why"])

    def test_walk_in_hops_end_on_the_stage(self):
        pts = line(20)
        stage = {"x": 28, "y": 1, "z": 0}
        g = D.gather_point(pts, stage, BOSS)
        hops = D.walk_in_points(pts, g, stage)
        self.assertEqual([h["x"] for h in hops], [40, 32, 28])         # every 2nd point toward the boss, then the stage
        self.assertEqual(hops[-1], stage)
        self.assertEqual(D.walk_in_points(pts, {"point": None}, stage), [])


class GatherReady(UnitCase):
    def test_ready_only_when_all_there_alive_and_quiet(self):
        self.assertEqual(D.gather_ready([], [], 6.0), (True, "ready"))
        ok, why = D.gather_ready([{"name": "Healer", "yd": 40.0}], [], 99)
        self.assertFalse(ok)
        self.assertIn("arriving: Healer", why)
        self.assertEqual(D.gather_ready([], [], D.QUIET_S - 0.1), (False, "in combat"))
        ok, why = D.gather_ready([], ["Tank"], 99)
        self.assertFalse(ok)
        self.assertIn("dead: Tank", why)


class FakeWs:
    def __init__(self, events=None):
        self.events = list(events or [])
        self.sent = []
        self.selection = None

    def mark(self):
        return 0

    def set_selection(self, g):
        self.selection = g
        self.sent.append(("select", g))

    def chat(self, msg, kind, to=None):
        self.sent.append(("whisper", to, msg))
        if hasattr(self, "log"):
            self.log.append(("whisper", to, msg))

    def wait_event(self, pred, mark, timeout=0):
        return next((e for e in self.events if pred(e)), None)

    def events_since(self, mark, kind=None):
        return [e for e in self.events if kind is None or e["kind"] == kind]


class FakeSession:
    """Records commands; `.playerbots bot self` toggles like PlayerbotMgr (Enable/Disable player botAI)."""

    def __init__(self, self_on=False, refuse=False, positions=None, alive=None, events=None, boss_guid=77):
        self.selfbot = False
        self._ai = self_on
        self.refuse = refuse
        self.log = []
        self.ws = FakeWs(events)
        self.guid = 1
        self.who = {"char": "Probe"}
        self.positions = positions or {}
        self.alive_names = alive or {}
        self.boss_guid = boss_guid
        self.ws.log = self.log

    def cmd(self, c, settle=0.6, timeout=8):
        self.log.append(c)
        if c == ".playerbots bot self":
            if self.refuse:
                return ["Self-bot is disabled"]
            self._ai = not self._ai
            return [D.SELF_ON if self._ai else D.SELF_OFF]
        return []

    def gps(self, guid=None):
        return dict(self.positions.get(guid or self.guid, self.positions.get(self.guid, {})))

    def go(self, x, y, z, map_id=None, timeout=20):
        self.log.append(("go", round(x), round(y)))
        self.positions[self.guid] = {"x": x, "y": y, "z": z, "map": 36}
        return {"event": {"kind": "teleport_near"}}

    def alive(self, name=None):
        return self.alive_names.get(name, "Yes")

    def unit(self, spawn_id, entry=None):
        return {"guid": self.boss_guid, "health": 100, "max_health": 100, "in_combat": False} if self.boss_guid else \
            {"guid": None, "entry": None}

    def binds(self, map_id=None):
        return [{"mask": 1}]


COMPS = [{"companion": "tank", "name": "Tank", "guid": 11}, {"companion": "healer", "name": "Heal", "guid": 12}]
ENC = {"encounter": 1, "bit": 0, "credit_entry": 1001, "spawns": [{"spawn_id": 10, "x": 0, "y": 0, "z": 0, "name": "B"}]}


class SelfBot(UnitCase):
    def test_turns_on_from_off(self):
        s = FakeSession(self_on=False)
        r = D.set_selfbot(s, True)
        self.assertTrue(r["ok"])
        self.assertTrue(s.selfbot)
        self.assertEqual(s.log.count(".playerbots bot self"), 1)

    def test_already_on_is_toggled_back_on(self):
        s = FakeSession(self_on=True)            # left on: the first toggle answers "Disable"
        r = D.set_selfbot(s, True)
        self.assertTrue(r["ok"] and s.selfbot)
        self.assertEqual(s.log.count(".playerbots bot self"), 2)

    def test_counterexample_refused_is_not_ok(self):
        s = FakeSession(refuse=True)
        r = D.set_selfbot(s, True)
        self.assertFalse(r["ok"])
        self.assertFalse(s.selfbot)
        self.assertEqual(r["why"], "refused")
        self.assertIsNone(D.self_state(["You do not have permission to enable player botAI"]))


class FightSetup(UnitCase):
    def fight(self, s, comps=COMPS, engaged=False):
        orig = D.POLL_S
        D.POLL_S = 0
        try:
            return D.fight(s, {"map": 36}, ENC, comps, ["Tank", "Heal", "Probe"], "Tank", True, lambda *a: None,
                           [], engaged=engaged)
        finally:
            D.POLL_S = orig

    def test_order_then_selfbot_on_then_off_after_the_kill(self):
        ev = [{"kind": "attack_start", "attacker": 11, "victim": 77}]
        s = FakeSession(events=ev)
        a = self.fight(s)
        self.assertTrue(a["killed"])
        whispers = [x for x in s.ws.sent if x[0] == "whisper"]
        self.assertEqual(whispers[:2], [("whisper", "Tank", "tank attack"), ("whisper", "Tank", "attack")])
        self.assertIn(("select", 77), s.ws.sent)
        self.assertTrue(a["self_on"]["ok"])
        self.assertTrue(a["self_off"]["ok"])
        self.assertFalse(s.selfbot)                                  # never left on after the pull
        self.assertEqual(s.log.count(".playerbots bot self"), 2)
        self.assertTrue(a["pull"]["engaged"])
        # GM mode goes off right before the order, and the self-AI comes on after it
        self.assertLess(s.log.index(".gm off"), s.log.index(("whisper", "Tank", "tank attack")))
        self.assertLess(s.log.index(("whisper", "Tank", "attack")), s.log.index(".playerbots bot self"))

    def test_already_engaged_skips_the_order(self):
        s = FakeSession()
        a = self.fight(s, engaged=True)
        self.assertNotIn("pull", a)
        self.assertFalse([x for x in s.ws.sent if x[0] == "whisper"])
        self.assertTrue(a["self_on"]["ok"])

    def test_counterexample_selfbot_refused_stops_the_pull(self):
        s = FakeSession(refuse=True)
        a = self.fight(s)
        self.assertEqual(a["why"], "selfbot not enabled")
        self.assertTrue(a["timeout"])
        self.assertNotIn("killed", a)

    def test_boss_not_found_is_an_explicit_reason(self):
        s = FakeSession(boss_guid=None)
        a = self.fight(s)
        self.assertEqual(a["why"], "boss not found")
        self.assertTrue(a["timeout"])
        self.assertFalse([x for x in s.ws.sent if x[0] == "whisper"])   # nothing pulled

    def test_probe_death_switches_the_selfbot_off_at_once(self):
        class Dies(FakeSession):
            n = 0

            def alive(self, name=None):
                return "No" if name == "Probe" else "Yes"

            def binds(self, map_id=None):
                self.n += 1
                return [{"mask": 1 if self.n > 2 else 0}]
        s = Dies(events=[{"kind": "attack_start", "attacker": 11, "victim": 77}])
        a = self.fight(s)
        self.assertEqual(a["deaths"], ["Probe"])
        self.assertTrue(a["self_off_on_death"]["ok"])
        self.assertFalse(s.selfbot)


class GatherAt(UnitCase):
    def run_gather(self, s, comps=COMPS, limit=0.5):
        orig = D.POLL_S
        D.POLL_S = 0.01
        try:
            return D.gather_at(s, comps, 77, limit=limit)
        finally:
            D.POLL_S = orig

    def test_ready_when_everyone_is_close_and_quiet(self):
        pos = {1: {"x": 0, "y": 0, "z": 0, "map": 36}, 11: {"x": 5, "y": 0, "z": 0, "map": 36},
               12: {"x": 0, "y": 9, "z": 0, "map": 36}}
        g = self.run_gather(FakeSession(positions=pos))
        self.assertTrue(g["ok"])

    def test_counterexample_a_companion_still_arriving_is_summoned_not_pulled(self):
        pos = {1: {"x": 0, "y": 0, "z": 0, "map": 36}, 11: {"x": 5, "y": 0, "z": 0, "map": 36},
               12: {"x": 0, "y": 40, "z": 0, "map": 36}}
        s = FakeSession(positions=pos)
        g = self.run_gather(s)
        self.assertFalse(g["ok"])
        self.assertIn("arriving: Heal", g["why"])
        self.assertIn(("whisper", "Heal", "summon"), s.ws.sent)
        self.assertNotIn(("whisper", "Tank", "summon"), s.ws.sent)

    def test_counterexample_recent_fight_is_not_ready(self):
        import time as _t
        pos = {1: {"x": 0, "y": 0, "z": 0, "map": 36}, 11: {"x": 5, "y": 0, "z": 0, "map": 36},
               12: {"x": 0, "y": 9, "z": 0, "map": 36}}
        ev = [{"kind": "melee_damage", "attacker": 99, "victim": 11, "t": _t.time() + 60}]
        g = self.run_gather(FakeSession(positions=pos, events=ev))
        self.assertFalse(g["ok"])
        self.assertEqual(g["why"], "in combat")

    def test_boss_engaged_ends_the_gather(self):
        import time as _t
        pos = {1: {"x": 0, "y": 0, "z": 0, "map": 36}}
        ev = [{"kind": "melee_damage", "attacker": 77, "victim": 11, "t": _t.time()}]
        g = self.run_gather(FakeSession(positions=pos, events=ev))
        self.assertTrue(g["boss_engaged"])


class PartyClearJudge(UnitCase):
    def ev(self, **kw):
        enc = {"boss": "B", "encounter": 1, "bit": 0, "killed": True,
               "attempts": [{"boss_before": {"max_health": 4490}}], "summary": {"wipes": 1}}
        run = {"cleared": True, "mode": "party", "total_s": 900, "required": [1], "final_mask": 1, "encounters": [enc]}
        run |= kw
        return {"run": run}

    def test_pass(self):
        self.assertEqual(PCK.party_clear_problems(self.ev()), [])

    def test_counterexamples(self):
        self.assertTrue(PCK.party_clear_problems(self.ev(cleared=False)))
        self.assertTrue(PCK.party_clear_problems(self.ev(mode="watch")))         # the probe only watched
        self.assertTrue(PCK.party_clear_problems(self.ev(total_s=45 * 60 + 1)))
        self.assertTrue(PCK.party_clear_problems(self.ev(final_mask=0)))
        self.assertTrue(PCK.party_clear_problems(self.ev(final_mask=None)))
        self.assertTrue(PCK.party_clear_problems(self.ev(required=[1, 2])))       # an encounter never reached
        three = self.ev()
        three["run"]["encounters"][0]["summary"]["wipes"] = 3
        self.assertTrue(PCK.party_clear_problems(three))
        nohp = self.ev()
        nohp["run"]["encounters"][0]["attempts"] = [{"boss_before": {}}]
        self.assertTrue(PCK.party_clear_problems(nohp))
        self.assertTrue(PCK.party_clear_problems({}))
        unready = self.ev()
        unready["run"]["encounters"][0]["attempts"][0] |= {"n": 1, "unready": True, "gather": {"why": "arriving: H"}}
        self.assertIn("before the party was gathered", PCK.party_clear_problems(unready)[0])


class ReservedAccounts(UnitCase):
    """play.player_login refuses every harness account as the player's login (any case); the player's file is a
    scratch file through a monkeypatched play.player_file, never env/player.json."""
    RESERVED = ("TEST_USER", "GM_USER", "COMP_USER", "SAVES_USER", "DUNGEON_USER", "DUNGEON_NAV_USER")

    def login_as(self, account):
        import json
        pf = self.tmp / "player.json"
        pf.write_text(json.dumps({"account": account, "password": "abc123"}))
        orig = play.player_file
        play.player_file = lambda: pf
        try:
            return play.player_login()
        finally:
            play.player_file = orig

    def test_every_reserved_account_is_refused(self):
        for attr in self.RESERVED:
            for name in (getattr(P, attr), getattr(P, attr).lower()):
                with self.subTest(account=attr, name=name):
                    with self.assertRaises(play.PlayerAccountError) as cm:
                        self.login_as(name)
                    self.assertIn("reserved", str(cm.exception))

    def test_counterexample_a_normal_name_is_accepted(self):
        login, new = self.login_as("Someone")
        self.assertEqual(login["account"], "Someone")
        self.assertFalse(new)


class HoldParty(UnitCase):
    def test_every_companion_is_told_to_stay(self):
        s = FakeSession()
        orig = D.time.sleep
        D.time.sleep = lambda x: None
        try:
            self.assertEqual(D.hold_party(s, COMPS), ["Tank", "Heal"])
        finally:
            D.time.sleep = orig
        self.assertEqual([x for x in s.ws.sent if x[0] == "whisper"],
                         [("whisper", "Tank", "stay"), ("whisper", "Heal", "stay")])


# ----------------------------------------------------------------------------- slice 3.4b: all four listed dungeons

class ListedDungeons(UnitCase):
    def test_default_is_every_listed_dungeon_and_not_partial(self):
        self.assertEqual(PCK.party_clear_dungeons({}), (D.LISTED, False))
        self.assertEqual(PCK.party_clear_dungeons({"HS_HARNESS_RUN": "1"}), (D.LISTED, False))

    def test_builder_selector_narrows_in_listed_order_and_is_partial(self):
        env = {"HS_HARNESS_RUN": "1", PCK.ONLY_ENV: f"{D.UTGARDE_KEEP},{D.RAZORFEN_DOWNS}"}
        self.assertEqual(PCK.party_clear_dungeons(env), ((D.RAZORFEN_DOWNS, D.UTGARDE_KEEP), True))

    def test_counterexample_selector_outside_a_harness_run_or_unlisted_is_refused(self):
        with self.assertRaises(PCK.CheckFail):
            PCK.party_clear_dungeons({PCK.ONLY_ENV: str(D.RAZORFEN_DOWNS)})
        for bad in (str(D.UTGARDE_KEEP_HEROIC), "deadmines", ","):
            with self.assertRaises(PCK.CheckFail):
                PCK.party_clear_dungeons({"HS_HARNESS_RUN": "1", PCK.ONLY_ENV: bad})

    def test_cli_refuses_the_selector_outside_hs_run(self):
        from hslib import cli
        orig = os.environ.get(PCK.ONLY_ENV)
        os.environ[PCK.ONLY_ENV] = str(D.RAZORFEN_DOWNS)
        try:
            self.assertEqual(cli.main(["status"]), 2)
        finally:
            if orig is None:
                os.environ.pop(PCK.ONLY_ENV, None)
            else:
                os.environ[PCK.ONLY_ENV] = orig


ANCHOR = {"spawn_id": 99, "x": 5.0, "y": 6.0, "z": 30.0, "name": "Herald", "entry": 500, "event": True, "rule": "r",
          "stage": {"x": 1.0, "y": 2.0, "z": 3.0, "path_entry": 600, "source": "script"}}


class EventFinal(UnitCase):
    def facts(self, final_spawns=False):
        fin = enc(4, 2000, 2, spawns=final_spawns) | {"last": 136}
        gong = enc(1, 0, 0, spawns=False)
        return {"encounters": [gong, enc(2, 500, 1), fin], "final": fin}

    def test_final_without_static_spawn_is_required_on_its_summoner(self):
        req, skipped = D.required_encounters(self.facts(), anchor=lambda f: ANCHOR)
        self.assertEqual([e["encounter"] for e in req], [2, 4])
        self.assertEqual([s["encounter"] for s in skipped], [1])          # the gong, not the final
        fin = req[-1]
        self.assertEqual(D.unit_entry(fin), 500)                          # live reads: the summoner
        self.assertEqual(fin["credit_entry"], 1004)                       # the kill credit stays the boss's
        self.assertEqual((fin["spawns"][0]["x"], fin["spawns"][0]["z"]), (1.0, 3.0))   # staged at the script's point
        self.assertEqual(fin["spawns"][0]["spawn_id"], 99)
        self.assertEqual(D.path_entry(fin), 600)

    def test_final_with_a_static_spawn_needs_no_anchor(self):
        def boom(f):
            raise AssertionError("anchor asked for")
        req, skipped = D.required_encounters(self.facts(final_spawns=True), anchor=boom)
        self.assertEqual([e["encounter"] for e in req], [2, 4])
        self.assertIsNone(req[-1].get("event"))
        self.assertEqual(D.unit_entry(req[-1]), 1004)
        self.assertIsNone(D.path_entry(req[-1]))

    def test_stall_breaker(self):
        hit = {"kind": "melee_damage", "attacker": 11, "victim": 900}
        self.assertFalse(D.stall_breaker(hit, 77))                         # not the boss
        self.assertTrue(D.stall_breaker(hit | {"victim": 77}, 77))
        self.assertTrue(D.stall_breaker(hit, 77, {11, 12}))                # event: any fighter in a fight
        self.assertFalse(D.stall_breaker({"kind": "chat", "attacker": 11}, 77, {11}))
        self.assertFalse(D.stall_breaker(hit | {"attacker": 5}, None, {11}))

    def test_event_boss_guids_from_combat_and_the_self_target(self):
        boss = (0xF130 << 48) | (17537 << 24) | 5
        other = (0xF130 << 48) | (17517 << 24) | 6
        evs = [{"kind": "melee_damage", "attacker": 11, "victim": other},
               {"kind": "self_values", "fields": {D.UNIT_FIELD_TARGET: boss & 0xFFFFFFFF,
                                                  D.UNIT_FIELD_TARGET + 1: boss >> 32}},
               {"kind": "spell_damage", "attacker": boss, "victim": 11}]
        self.assertEqual(D.event_boss_guids(evs, 17537), [boss])
        self.assertEqual(D.event_boss_guids(evs[:1], 17537), [])

    def test_event_pull_steps_onto_the_stage_without_a_tank_order(self):
        s = FakeSession(events=[{"kind": "attack_start", "attacker": 11, "victim": 900}])
        e = dict(ENC) | {"event": {"stage": ANCHOR["stage"]}, "credit_entry": 17537}
        orig = D.POLL_S
        D.POLL_S = 0
        try:
            a = D.fight(s, {"map": 543}, e, COMPS, ["Tank", "Heal", "Probe"], "Tank", True, lambda *x: None, [])
        finally:
            D.POLL_S = orig
        self.assertTrue(a["pull"]["ordered"])
        self.assertEqual(a["pull"]["event_stage"], {"x": 1.0, "y": 2.0, "z": 3.0})
        self.assertIn(("go", 1, 2), s.log)
        self.assertFalse([x for x in s.ws.sent if x[0] == "whisper"])    # nothing to order at a passive summoner
        self.assertLess(s.log.index(".gm off"), s.log.index(("go", 1, 2)))
        self.assertLess(s.log.index(("go", 1, 2)), s.log.index(".playerbots bot self"))


class EvidenceRow(UnitCase):
    def test_timed_out_and_stalled_pulls_are_in_the_row(self):
        rec = {"boss": "B", "encounter": 1, "killed": True, "attempts": [
            {"timeout": True, "stalled": True, "fight_s": 60, "deaths": []},
            {"timeout": True, "fight_s": 300, "deaths": ["T"]},
            {"wipe": True, "fight_s": 20, "deaths": ["T", "H"]},
            {"killed": True, "fight_s": 30, "deaths": []}]}
        row = D.encounter_summary(rec, {"T": "tank", "H": "healer"})
        self.assertEqual((row["wipes"], row["timeouts"], row["stalled"], row["attempts"]), (1, 2, 1, 4))

    def test_event_boss_health_comes_from_its_own_read(self):
        def ev(attempt):
            e = {"boss": "Herald", "encounter": 4, "bit": 2, "killed": True, "event": {"summoner": "Herald"},
                 "attempts": [attempt], "summary": {"wipes": 0}}
            return {"run": {"cleared": True, "mode": "party", "total_s": 900, "required": [4], "final_mask": 4,
                            "encounters": [e]}}
        self.assertEqual(PCK.party_clear_problems(ev({"event_boss": {"ok": True, "max_health": 30000}})), [])
        # counterexample: the summoner's health (boss_before) is not the boss's
        self.assertIn("no scaled health read", PCK.party_clear_problems(ev({"boss_before": {"max_health": 9}}))[0])

    def test_initself_answer(self):
        self.assertTrue(PCK.initself_ok(["initself ok"]))
        self.assertFalse(PCK.initself_ok(["ERROR: Only GM can use this command."]))
        self.assertFalse(PCK.initself_ok([]))


class SessionExit(UnitCase):
    def test_exit_switches_the_selfbot_off_by_its_answer(self):
        class S(D.Session):
            def __init__(self):
                FakeSession.__init__(self, self_on=True)
                self.selfbot = True
                self.ev = {}
            cmd = FakeSession.cmd
        s = S()
        orig = D.C.Session.__exit__
        D.C.Session.__exit__ = lambda self, *exc: None
        try:
            s.__exit__(None, None, None)
        finally:
            D.C.Session.__exit__ = orig
        self.assertTrue(s.ev["self_off_on_exit"]["ok"])
        self.assertFalse(s.selfbot)
        self.assertEqual(s.log[-1], ".gm off")


class PerDungeonSetup(UnitCase):
    """dng.party_clear against stand-ins for the stack: each listed dungeon's run gets its own facts (target level from
    LFGDungeons.dbc), its own nav pass and session at that level, and a partial run never passes."""

    def setUp(self):
        super().setUp()
        self.calls = []
        calls = self.calls
        test = self

        class Sess:
            def __init__(self, level, *a, **k):
                self.level = level
                self.ev = {}
                calls.append(("session", level))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cmd(self, c, settle=0.6, timeout=8):
                calls.append(("cmd", c))
                return [test.initself_reply] if "initself" in c else []

            def leave(self):
                return {"ok": True}

        def facts(lfg):
            return {"lfg_id": lfg, "map": lfg + 1000, "target_level": lfg % 80 + 1, "encounters": [enc(1, 0, 0)],
                    "final": enc(1, 0, 0)}

        def run_party(s, f, comps, nav, selfbot=False, log=None, out=None, **k):
            calls.append(("run", f["lfg_id"], s.level, nav["lfg"]))
            out |= {"cleared": True, "mode": "party", "total_s": 10, "required": [1], "final_mask": 1,
                    "encounters": [{"boss": "B", "encounter": 1, "bit": 0, "killed": True,
                                    "attempts": [{"boss_before": {"max_health": 5}}], "summary": {"wipes": 0}}]}
            return out

        self.initself_reply = "initself ok"
        self.patches = {(D, "facts"): facts, (D, "Session"): Sess, (D, "run_party"): run_party,
                        (D, "nav_stages"): lambda f, req, say: {"lfg": f["lfg_id"], "stages": []},
                        (D, "gear_quality"): lambda: "rare", (PCK, "add_companion"): lambda s, c: c,
                        (PCK, "clear_party"): lambda s, plan: {}, (PCK.PP, "plan"): lambda: {},
                        (PCK.PP, "commands"): lambda plan, role: [{"companion": "tank", "name": "T", "guid": 2}],
                        (PCK, "_keep"): lambda name, ev: None, (PCK, "_run_log"): lambda name: (lambda x: None)}
        self.saved = {k: getattr(*k) for k in self.patches}
        for (mod, name), v in self.patches.items():
            setattr(mod, name, v)
        self.env = {k: os.environ.get(k) for k in (PCK.ONLY_ENV, "HS_HARNESS_RUN")}

    def tearDown(self):
        for (mod, name), v in self.saved.items():
            setattr(mod, name, v)
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def test_every_listed_dungeon_runs_at_its_own_target_level(self):
        os.environ.pop(PCK.ONLY_ENV, None)
        ev = PCK.party_clear({})
        runs = [c for c in self.calls if c[0] == "run"]
        self.assertEqual([r[1] for r in runs], list(D.LISTED))
        self.assertEqual([r[2] for r in runs], [lfg % 80 + 1 for lfg in D.LISTED])     # facts' target level
        self.assertEqual([r[3] for r in runs], list(D.LISTED))                         # its own nav pass
        self.assertFalse(ev["partial"])
        self.assertEqual([d["dungeon"] for d in ev["dungeons"]], list(D.LISTED))

    def test_partial_run_skips_never_passes(self):
        os.environ |= {PCK.ONLY_ENV: str(D.RAMPARTS), "HS_HARNESS_RUN": "1"}
        with self.assertRaises(PCK.CheckSkip) as cm:
            PCK.party_clear({})
        self.assertEqual([c[1] for c in self.calls if c[0] == "run"], [D.RAMPARTS])
        self.assertIn("not a gate pass", str(cm.exception))

    def test_counterexample_initself_without_ok_fails(self):
        os.environ.pop(PCK.ONLY_ENV, None)
        self.initself_reply = "ERROR: Only GM can use this command."
        with self.assertRaises(PCK.CheckFail) as cm:
            PCK.party_clear({})
        self.assertIn("initself", str(cm.exception))
        self.assertFalse([c for c in self.calls if c[0] == "run"])


class GroundRetry(UnitCase):
    """Utgarde Keep: the boss's path from the entrance has no navmesh route (type 10 = shortcut | no path), so the
    fallback is refused and the staging point is retried at the boss's own floor height, turned around the boss."""

    def test_no_path_types_are_unusable(self):
        self.assertTrue(D.path_usable(D.PATH_NORMAL))
        self.assertTrue(D.path_usable(D.PATH_INCOMPLETE))
        self.assertFalse(D.path_usable(10))                    # PATHFIND_SHORTCUT | PATHFIND_NOPATH
        self.assertFalse(D.path_usable(D.PATH_NOPATH))
        self.assertFalse(D.path_usable(None))

    def test_turned_candidates_at_the_boss_height(self):
        boss, prev = {"x": 0.0, "y": 0.0, "z": 40.0}, {"x": 0.0, "y": -100.0, "z": 12.0}
        c = D.turned_candidate(boss, prev, 30, 0)
        self.assertTrue(math.isclose(c["y"], -30, abs_tol=1e-6) and math.isclose(c["x"], 0, abs_tol=1e-6))
        self.assertEqual(c["z"], 40.0)                         # the boss's floor, not the interpolated height
        c = D.turned_candidate(boss, prev, 30, 90)
        self.assertTrue(math.isclose(c["x"], 30, abs_tol=1e-6) and math.isclose(c["y"], 0, abs_tol=1e-6))
        self.assertEqual(D.GROUND_TRIES[0], (D.STAGE_BACK_YD, 0))   # the design's point first

    def path(self, end, t=D.PATH_NORMAL):
        return {"type": t, "actual_end": end}

    def test_judge_ground(self):
        here = {"x": 30.0, "y": 0.0, "z": 0.0}
        pts = line(9)                                          # boss at 0 .. 32 yd, 4 yd apart
        j = D.judge_ground(BOSS, here, self.path(here), pts)
        self.assertEqual(j["verdict"], "primary")
        self.assertEqual(j["walk_yd"], 28.0)                   # path point nearest 30 yd: 28 yd on foot

    def test_counterexamples_judge_ground(self):
        here = {"x": 30.0, "y": 0.0, "z": 0.0}
        self.assertIsNone(D.judge_ground(BOSS, here, self.path(here, 10), line(9))["verdict"])     # no path
        self.assertIsNone(D.judge_ground(BOSS, here, self.path({"x": 50.0, "y": 0.0, "z": 0.0}), line(9))["verdict"])
        near = {"x": 12.0, "y": 0.0, "z": 0.0}
        self.assertIn("from the boss", D.judge_ground(BOSS, near, self.path(near), line(9))["why"])  # < 25 yd
        zig = [{"x": 0.0, "y": 0.0, "z": 0.0}] + [{"x": 0.0, "y": 4.0 * k, "z": 0.0} for k in range(1, 15)] + \
              [{"x": 4.0 * k, "y": 56.0 - 2 * k, "z": 0.0} for k in range(1, 9)] + [here]
        self.assertIn("walk", D.judge_ground(BOSS, here, self.path(here), zig)["why"])        # far on foot
