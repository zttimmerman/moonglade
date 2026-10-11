"""Profiles with identical overrides (test-pyramid part 3: scaling-on is m0 under another name): a world started with
one counts as running the other, so ./play's warm path and the fixture don't restart it; a different one never does."""
from unittest import mock

from hslib import config, stack

from .base import UnitCase


def _running(name):
    return lambda: {"profile": name, "digest": config.profile_digest(name), "pid": 1, "recorded": True}


class SameSettings(UnitCase):
    def test_scaling_on_is_m0(self):
        self.assertEqual(config.profile_digest("scaling-on"), config.profile_digest("m0"))
        with mock.patch.object(stack, "running_profile", _running("scaling-on")):
            self.assertTrue(stack.profile_matches("m0"))
            self.assertTrue(stack.world_matches("m0"))

    def test_counterexample_other_settings_restart(self):
        with mock.patch.object(stack, "running_profile", _running("scaling-off")):
            self.assertFalse(stack.profile_matches("m0"))
            self.assertFalse(stack.world_matches("m0"))

    def test_population_never_by_digest(self):
        with mock.patch.object(stack, "running_profile", lambda: {"profile": "m0", "digest": "x", "pid": 1, "n": None}):
            self.assertFalse(stack.world_matches("population"))
