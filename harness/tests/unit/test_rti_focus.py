"""m4-dungeon-companions 3.1/3.3: MSG_RAID_TARGET_UPDATE send/parse (formats from Group::SetTargetIcon,
Group::SendTargetIconList and HandleRaidTargetUpdateOpcode) and dng.rti_focus's pack rule and pass rule, with mocked
packets and fight records."""
import struct

from hslib.checks import party_checks as PC
from hslib.probe import proto as W

from .base import UnitCase

SKULL, OTHER = 0xF130000000000101, 0xF130000000000102
NAMES = ["Tanky", "Healy", "Dps1", "Dps2"]


def fight(**change) -> dict:
    rec = {"orders": {"companions": NAMES, "rti": list(NAMES), "tank_attack": True}, "mark_echo": True,
           "skull": SKULL, "deaths_s": {SKULL: 14.0, OTHER: 30.0},
           "dps_events_before_death": [{"victim": SKULL}] * 9 + [{"victim": OTHER}]}
    return rec | change


class RaidTargetPacket(UnitCase):
    def test_send_is_icon_then_guid(self):
        self.assertEqual(W.raid_target(W.RAID_ICONS["skull"], SKULL), struct.pack("<BQ", 7, SKULL))
        self.assertEqual(len(W.raid_target(0, 1)), 9)
        with self.assertRaises(ValueError):
            W.raid_target(8, SKULL)            # TARGETICONCOUNT = 8

    def test_parse_set(self):
        who = 0x22
        data = struct.pack("<BQBQ", 0, who, 7, SKULL)     # Group::SetTargetIcon
        self.assertEqual(W.parse_raid_target(data), {"mode": "set", "who": who, "icons": {7: SKULL}})
        cleared = W.parse_raid_target(struct.pack("<BQBQ", 0, who, 7, 0))
        self.assertEqual(cleared["icons"], {7: 0})

    def test_parse_list(self):
        data = struct.pack("<B", 1) + struct.pack("<BQ", 4, OTHER) + struct.pack("<BQ", 7, SKULL)
        self.assertEqual(W.parse_raid_target(data), {"mode": "list", "who": None, "icons": {4: OTHER, 7: SKULL}})
        self.assertEqual(W.parse_raid_target(b"\x01")["icons"], {})

    def test_short_set_packet_raises(self):
        with self.assertRaises(ValueError):
            W.parse_raid_target(struct.pack("<BQ", 0, 1))

    def test_icon_names_match_playerbots(self):
        self.assertEqual(sorted(W.RAID_ICONS.values()), list(range(8)))
        self.assertEqual((W.RAID_ICONS["star"], W.RAID_ICONS["moon"], W.RAID_ICONS["skull"]), (0, 4, 7))


class PackRule(UnitCase):
    def test_nearest_with_a_neighbour_and_farthest_marked(self):
        stage = {"x": 0, "y": 0, "z": 0}
        spawns = [{"spawn_id": 1, "x": 5, "y": 0, "z": 0},          # nearest, but alone
                  {"spawn_id": 2, "x": 30, "y": 0, "z": 0}, {"spawn_id": 3, "x": 36, "y": 0, "z": 0},
                  {"spawn_id": 4, "x": 31, "y": 5, "z": 0}, {"spawn_id": 5, "x": 60, "y": 0, "z": 0}]
        p = PC.pack_rule(spawns, stage)
        self.assertEqual(p["anchor"], 2)
        self.assertEqual([c["spawn_id"] for c in p["members"]], [2, 3, 4])
        self.assertEqual(p["marked"], 3)

    def test_height_counts_and_no_pack(self):
        stage = {"x": 0, "y": 0, "z": 0}
        spawns = [{"spawn_id": 1, "x": 5, "y": 0, "z": 0}, {"spawn_id": 2, "x": 5, "y": 0, "z": 12}]   # another deck
        self.assertIsNone(PC.pack_rule(spawns, stage))
        self.assertIsNone(PC.pack_rule([], stage))


class RtiPassRule(UnitCase):
    def test_passing_fight_passes(self):
        self.assertEqual(PC.rti_problems(fight()), [])

    def test_counterexample_no_rti_order(self):
        # the task's counterexample: without the `rti` order the pass rule fails, both on the missing order and on
        # the behaviour it leaves (damage spread over the pack, another creature dying first)
        no_order = fight(orders={"companions": NAMES, "rti": [], "tank_attack": True})
        self.assertEqual(len(PC.rti_problems(no_order)), 1)
        spread = fight(orders={"companions": NAMES, "rti": [], "tank_attack": True},
                       deaths_s={SKULL: 30.0, OTHER: 12.0},
                       dps_events_before_death=[{"victim": SKULL}] * 5 + [{"victim": OTHER}] * 5)
        self.assertEqual(len(PC.rti_problems(spread)), 3)

    def test_each_way_the_then_breaks(self):
        for change in ({"orders": {"companions": NAMES, "rti": NAMES[:3], "tank_attack": True}},
                       {"orders": {"companions": NAMES, "rti": NAMES, "tank_attack": False}},
                       {"orders": {}}, {"mark_echo": False}, {"deaths_s": {OTHER: 3.0}}, {"deaths_s": {}},
                       {"deaths_s": {SKULL: 14.0, OTHER: 13.9}}, {"dps_events_before_death": []},
                       {"dps_events_before_death": [{"victim": SKULL}] * 7 + [{"victim": OTHER}] * 3}):
            with self.subTest(change=change):
                self.assertTrue(PC.rti_problems(fight(**change)))

    def test_share_threshold_is_inclusive(self):
        at = fight(dps_events_before_death=[{"victim": SKULL}] * 8 + [{"victim": OTHER}] * 2)
        self.assertEqual(PC.rti_problems(at), [])


class StandPoint(UnitCase):
    def test_nearest_point_outside_reach(self):
        pack = [{"x": 0, "y": 0, "z": 0}, {"x": 4, "y": 0, "z": 0}]
        pts = [{"x": 19, "y": 0, "z": 0}, None, {"x": 40, "y": 0, "z": 0}, {"x": 35, "y": 0, "z": 0},
               {"x": -50, "y": 0, "z": 0}]
        self.assertEqual(PC.stand_point(pts, pack, []),
                         {"x": 35, "y": 0, "z": 0, "pack_yd": 31.0, "nearest_hostile_yd": 31.0})
        self.assertIsNone(PC.stand_point([{"x": 19, "y": 0, "z": 0}], pack, []))

    def test_clear_set(self):
        pack = [{"spawn_id": 1, "x": 0, "y": 0, "z": 0}]
        stand = {"x": 50, "y": 0, "z": 0}
        cands = pack + [{"spawn_id": 2, "x": 25, "y": 0, "z": 0}, {"spawn_id": 3, "x": 70, "y": 0, "z": 0},
                        {"spawn_id": 4, "x": 100, "y": 0, "z": 0}, {"spawn_id": 5, "x": -20, "y": 0, "z": 0}]
        self.assertEqual([c["spawn_id"] for c in PC.clear_set(cands, pack, stand)], [3, 2, 5])   # nearest the stand point first

    def test_keeps_off_the_boss_and_other_spawns(self):
        # live run 2 (2026-10-12): the nearest point 30 yd from the pack lay on the boss's path and pulled the boss
        pack = [{"x": 0, "y": 0, "z": 0}]
        boss = {"x": 45, "y": 0, "z": 0}
        pts = [{"x": 31, "y": 0, "z": 0}, {"x": -31, "y": 0, "z": 0}, {"x": 80, "y": 0, "z": 0}]
        self.assertEqual(PC.stand_point(pts, pack, [boss])["x"], -31)
        self.assertIsNone(PC.stand_point(pts[:1], pack, [boss]))
