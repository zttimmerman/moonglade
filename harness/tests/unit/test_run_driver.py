"""m4-dungeon-companions task 1.1 (prototype): the run driver's pure rules in hslib/dungeon.py (design.md §4): encounter
order and the required set, the staging point rule (candidate, `.mmap path` parse, primary verdict, the path-point
fallback), the probe's create-block parser that reads `.mmap path`'s waypoints, and the wipe accounting. Stand-in
facts and packets with made-up values; the live run is the gate check dng.party_clear_deadmines."""
import math
import struct

from hslib import dungeon as D
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
