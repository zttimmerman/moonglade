"""lanes.preflight_decision (ADR-0011 memory preflight plus power): may lane B start?"""
from hslib import lanes as L

from .base import UnitCase

GOOD = {"pressure_level": 1, "avail_gb": 8.0, "swap_used_gb": 1.0, "swap_free_gb": 3.0}
START = {"swap_used_gb": 1.0}
PLUGGED = {"on_battery": False, "percent": 90}


class Preflight(UnitCase):
    def test_allowed_when_everything_holds(self):
        d = L.preflight_decision(GOOD, START, True, PLUGGED, False)
        self.assertTrue(d["allowed"], d["reasons"])

    def test_low_memory_refused(self):
        d = L.preflight_decision(GOOD | {"avail_gb": 4.6}, START, True, PLUGGED, False)
        self.assertFalse(d["allowed"])
        self.assertIn("4.6 GB available", d["reasons"][0])

    def test_unreadable_probe_fails_closed(self):
        d = L.preflight_decision(GOOD | {"avail_gb": None}, START, True, PLUGGED, False)
        self.assertFalse(d["allowed"])
        self.assertIn("fail closed", d["reasons"][0])

    def test_stack_down_reading_refused(self):
        d = L.preflight_decision(GOOD, START, False, PLUGGED, False)
        self.assertFalse(d["allowed"])

    def test_on_battery_refused_unless_ignored(self):
        bat = {"on_battery": True, "percent": 95}
        self.assertFalse(L.preflight_decision(GOOD, START, True, bat, False)["allowed"])
        self.assertTrue(L.preflight_decision(GOOD, START, True, bat, True)["allowed"])

    def test_pressure_warn_refused(self):
        d = L.preflight_decision(GOOD | {"pressure_level": 2}, START, True, PLUGGED, False)
        self.assertFalse(d["allowed"])
