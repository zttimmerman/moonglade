"""era.session_toggle's lockstep driver (checks/era_checks.drive_together): two scenarios share one restart per
profile step, each gets the restart's record, and a failing one doesn't stop the other (test-pyramid part 2)."""
from unittest import mock

from hslib.checks import era_checks as E
from hslib.registry import CheckFail, require

from .base import UnitCase


def scenario(log, fail_at=None):
    got = []
    got.append((yield "off"))
    log.append("a-off")
    if fail_at == "off":
        require(False, "counterexample failure", got=got)
    got.append((yield "on"))
    return {"got": got}


class DriveTogether(UnitCase):
    def test_one_restart_per_step_for_both(self):
        calls, log = [], []
        with mock.patch.object(E.stack, "ensure_profile", side_effect=lambda p: calls.append(p) or {"profile": p}):
            out = E.drive_together({"a": scenario(log), "b": scenario(log)})
        self.assertEqual(calls, ["off", "on"])
        self.assertEqual({k: v["status"] for k, v in out.items()}, {"a": "pass", "b": "pass"})
        self.assertEqual(out["a"]["evidence"]["got"], [{"profile": "off"}, {"profile": "on"}])

    def test_counterexample_a_failure_is_its_own_and_the_other_continues(self):
        calls, log = [], []
        with mock.patch.object(E.stack, "ensure_profile", side_effect=lambda p: calls.append(p) or {"profile": p}):
            out = E.drive_together({"a": scenario(log, fail_at="off"), "b": scenario(log)})
        self.assertEqual(out["a"]["status"], "fail")
        self.assertEqual(out["b"]["status"], "pass")
        self.assertEqual(calls, ["off", "on"])

    def test_different_profiles_are_an_error_not_a_guess(self):
        def other():
            yield "elsewhere"
        with mock.patch.object(E.stack, "ensure_profile", side_effect=AssertionError("must not restart")):
            out = E.drive_together({"a": scenario([]), "b": other()})
        self.assertEqual({v["status"] for v in out.values()}, {"error"})
