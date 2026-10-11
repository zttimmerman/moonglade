"""Ownership-based selection rules (test-pyramid design 13; prototype hslib/ownership.py)."""
from hslib import ownership as O

from .base import UnitCase

CONF = O.MOD + "conf/mod_moonglade.conf.dist"
OWN = {"tests": {
    "saves.manual": {"layer": "integration", "area": "saves", "covers": (O.MOD + "src/MoongladeSaves.cpp",)},
    "pause.freeze_matrix": {"layer": "integration", "area": "pause", "covers": (O.MOD + "src/MoongladePause.cpp",)},
    "journey.first_play": {"layer": "journey", "area": "journey", "covers": ("play",)},
}}
SMOKE = ["journey.first_play", "saves.manual"]


class Ownership(UnitCase):
    def test_declared_path_selects_owner_only(self):
        r = O.select([O.MOD + "src/MoongladePause.cpp"], OWN, SMOKE)
        self.assertEqual(sorted(r["selected"]), ["pause.freeze_matrix"])

    def test_undeclared_path_selects_smoke_not_full(self):
        r = O.select(["harness/hslib/util.py"], OWN, SMOKE)
        self.assertFalse(r["full"])
        self.assertEqual(sorted(r["selected"]), sorted(SMOKE))

    def test_foundations_are_full(self):
        self.assertTrue(O.select(["patches/core/0003-world-pause.patch"], OWN, SMOKE)["full"])
        self.assertTrue(O.select(["harness/hslib/build.py"], OWN, SMOKE)["full"])

    def test_fork_lock_only_core_and_bots_pins(self):
        old = "core_sha=a\nplayerbots_sha=b\nlua51_url=x\n"
        self.assertFalse(O.fork_lock_foundation(old, old.replace("lua51_url=x", "lua51_url=y")))
        self.assertTrue(O.fork_lock_foundation(old, old.replace("core_sha=a", "core_sha=c")))
        self.assertTrue(O.fork_lock_foundation(None, old))

    def test_conf_section_ownership(self):
        old = "Moonglade.Saves.KeepAuto = 5\nMoonglade.Pause.Enable = 1\n"
        self.assertEqual(O.conf_areas(CONF, old, old.replace("KeepAuto = 5", "KeepAuto = 6")), {"saves"})
        self.assertIsNone(O.conf_areas(CONF, old, old + "Moonglade.Other.X = 1\n"))
