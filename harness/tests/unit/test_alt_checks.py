"""m4-alt-companions: unit-level counterexamples for the alt.* pass rules (hslib/checks/alt_checks.py). Each rule passes
the shape a passing game run records and fails each way the spec's THEN can break."""
from hslib.checks import alt_checks as A

from .base import UnitCase

BRING_OK = {"joined_s": 1.2, "account_before": 349, "account_after": 349, "left_party_s": 0.6, "offline_s": 0.7}
FOREIGN_OK = {"account": 1368, "master_account": 349, "online_seen": False, "in_party_seen": False,
              "reply": ["Failure: You are not allowed to control bot Hstester", "add: Hstester - ok"]}
GEAR_OK = {"equip_before": {3: 38, 6: 39, 7: 40, 15: 49778}, "equip_after": {3: 38, 6: 39, 7: 40, 15: 49778},
           "talents_before": [], "talents_after": [], "kills": 1, "companion_refused": [],
           "refusal": "Hscompalt|cfff00000: You cannot use autogear on alt bots."}
PROGRESS_OK = {"before": {"level": 8, "xp": 0, "money": 0}, "after_trip": {"level": 8, "xp": 186, "money": 0},
               "kills": 2, "load": {"state": "done"}, "rows_diff": {}}


class AltRules(UnitCase):
    def test_passing_shapes_pass(self):
        self.assertEqual(A.bring_problems(BRING_OK), [])
        self.assertEqual(A.foreign_problems(FOREIGN_OK), [])
        self.assertEqual(A.gear_problems(GEAR_OK), [])
        self.assertEqual(A.progress_problems(PROGRESS_OK), [])

    def test_bring_counterexamples(self):
        for change in ({"joined_s": None}, {"joined_s": 31}, {"account_after": 350}, {"account_before": None},
                       {"left_party_s": None}, {"left_party_s": 30.5}, {"offline_s": None}, {"offline_s": 61}):
            with self.subTest(change=change):
                self.assertEqual(len(A.bring_problems(BRING_OK | change)), 1)

    def test_foreign_counterexamples(self):
        # the design's counterexample: with the accounts linked, the add succeeds (no refusal, the character comes in)
        linked = FOREIGN_OK | {"reply": ["add: Hstester - ok"], "online_seen": True, "in_party_seen": True}
        self.assertEqual(len(A.foreign_problems(linked)), 2)
        for change in ({"reply": ["add: Hstester - ok"]}, {"reply": []}, {"online_seen": True},
                       {"in_party_seen": True}, {"account": 349}, {"account": None}):
            with self.subTest(change=change):
                self.assertEqual(len(A.foreign_problems(FOREIGN_OK | change)), 1)

    def test_gear_counterexamples(self):
        autogeared = {3: 38, 6: 2387, 7: 40, 15: 49778}
        for change in ({"equip_after": autogeared}, {"equip_after": {}}, {"talents_after": [[12295, 1]]},
                       {"talents_before": None}, {"refusal": None},
                       {"companion_refused": ["Tank|cfff00000: You cannot use autogear on alt bots."]}, {"kills": 0}):
            with self.subTest(change=change):
                self.assertEqual(len(A.gear_problems(GEAR_OK | change)), 1)
        self.assertIn("GIVEN: the alt has no equipped items to protect",
                      A.gear_problems(GEAR_OK | {"equip_before": {}, "equip_after": {}}))

    def test_progress_counterexamples(self):
        # spec counterexample (tasks 3.2): an alt-row change during the load must FAIL the comparison
        changed = {"characters": {"only_a": [["Hscompalt", "186"]], "only_b": [["Hscompalt", "0"]],
                                  "count_a": 1, "count_b": 1}}
        self.assertEqual(A.progress_problems(PROGRESS_OK | {"rows_diff": changed}),
                         ["loading the master's save changed the alt's rows: ['characters']"])
        for change in ({"rows_diff": None}, {"after_trip": {"level": 8, "xp": 0, "money": 0}}, {"kills": 1},
                       {"load": {"state": "refused"}}, {"load": None},
                       {"after_trip": {"level": 8, "xp": 186, "money": -1}}):
            with self.subTest(change=change):
                self.assertEqual(len(A.progress_problems(PROGRESS_OK | change)), 1)
        # a level-up resets xp: still progress
        self.assertEqual(A.progress_problems(PROGRESS_OK | {"after_trip": {"level": 9, "xp": 10, "money": 0}}), [])


class FakeWorld:
    """The server side of kill_one's commands: `.npc info [spawn]` answers for the selected creature when there is
    one, else the spawn (cs_npc.cpp HandleNpcInfoCommand); `.damage` kills the selected creature."""
    def __init__(self, spawns):
        self.hp = {sid: 100 for sid in spawns}
        self.guid_of = {sid: 0xF130000000000000 + sid for sid in spawns}
        self.selected = 0
        self.n = 0

    def mark(self):
        return self.n

    def set_selection(self, guid):
        self.selected = guid

    def heartbeat(self, *a):
        pass

    def wait_event(self, pred, since, timeout=0):
        return {"kind": "teleport_near", "x": 1.0, "y": 2.0, "z": 3.0}

    def command(self, cmd, settle=0, timeout=0):
        self.n += 1
        if cmd.startswith(".npc info"):
            by_guid = {g: sid for sid, g in self.guid_of.items()}
            sid = by_guid.get(self.selected) or int(cmd.split()[-1])
            return [f"current GUID: GUID Full: 0x{self.guid_of[sid]:X}", f"Health (base): 100. (max): 100. "
                    f"(current): {self.hp[sid]}.", "Unit Flags: 0.", "Position: 1.0 2.0 3.0.", f"DB GUID: {sid}"]
        if cmd.startswith(".damage"):
            by_guid = {g: sid for sid, g in self.guid_of.items()}
            self.hp[by_guid[self.selected]] = 0
        return []


class KillOne(UnitCase):
    def _session(self, world):
        s = A.C.Session.__new__(A.C.Session)
        s.ws, s.level, s.tele = world, 8, "ElwynnForest"
        return s

    def test_two_kills_in_a_row(self):
        from unittest import mock
        world = FakeWorld([11, 12, 13])
        cands = [{"spawn_id": sid, "entry": 1, "spawn": {"x": 1.0, "y": 2.0, "z": 3.0}} for sid in (11, 12, 13)]
        s = self._session(world)
        with mock.patch.object(A.C, "creature_candidates", lambda *a, **k: [dict(c) for c in cands]), \
                mock.patch.object(A.time, "sleep", lambda *_: None):
            k1 = A.kill_one(s, set(), ["Hscompalt"], {"x": 0.0, "y": 0.0, "z": 0.0})
            k2 = A.kill_one(s, {k1["spawn_id"]}, ["Hscompalt"], {"x": 0.0, "y": 0.0, "z": 0.0})
        self.assertEqual((k1["spawn_id"], k1["dead"], k2["spawn_id"], k2["dead"]), (11, True, 12, True))

    def test_counterexample_a_selected_corpse_hides_every_candidate(self):
        # what the 2026-10-12 game runs hit before the selection was cleared
        from unittest import mock
        world = FakeWorld([11, 12])
        world.hp[11], world.selected = 0, world.guid_of[11]
        s = self._session(world)
        cands = [{"spawn_id": sid, "entry": 1, "spawn": {"x": 1.0, "y": 2.0, "z": 3.0}} for sid in (11, 12)]
        with mock.patch.object(A.C, "creature_candidates", lambda *a, **k: [dict(c) for c in cands]):
            with self.assertRaises(A.C.CompanionError):
                A.C.Session.pick_live_creature(s, exclude={11})
