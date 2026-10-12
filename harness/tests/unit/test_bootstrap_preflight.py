"""bootstrap preflight's disk rule and setup.preflight's control (fix/pr-tier-budgets): the need is what's left to do;
the control injects only the steps a set-up checkout has really finished, never free space or the Mac's facts."""
import unittest
from unittest import mock

from hslib import bootstrap as B
from hslib.checks import setup_checks as S
from tests.unit.base import UnitCase

GB = 10**9
ALL = [s.id for s in B.STEPS if s.id != "brew"]
FRESH_NEED = sum(B.NEED_GB.get(i, 0.0) for i in ALL) + 1.0


def _probe(**inj):
    base = {"arch": "arm64", "macos": "26.2", "rosetta": True, "clt": True, "brew": True}
    return B.Probe(base | inj)


class DiskRule(UnitCase):
    def _preflight(self, free_gb, todo):
        ctx = B.Ctx(None, False, _probe(free_bytes=int(free_gb * GB)))
        with mock.patch.object(B, "check_client_source", return_value={"kind": "folder"}), \
                mock.patch("hslib.stack.stack_processes", return_value={}), \
                mock.patch.object(B, "_locked", return_value=False):
            return B.preflight(ctx, todo)

    def test_set_up_tree_passes_on_a_nearly_full_mac(self):
        ev = self._preflight(31.0, ["build", "data", "database", "playclient", "defaults"])
        self.assertLess(ev["disk"]["need_gb"], 31.0)

    def test_counterexample_fresh_tree_fails_on_the_same_mac(self):
        self.assertGreater(FRESH_NEED, 31.0)
        with self.assertRaises(B.StepFailed) as e:
            self._preflight(31.0, ALL)
        self.assertIn("not enough free disk space", str(e.exception))

    def test_counterexample_low_disk_fails_even_with_little_left(self):
        with self.assertRaises(B.StepFailed):
            self._preflight(3.0, ["build", "data"])


class Todo(unittest.TestCase):
    def test_done_steps_removed_from_what_is_left(self):
        pre = {"build": (False, "x"), "client": (False, "not installed"), "wine": (True, "ok")}
        self.assertEqual(B.preflight_todo(pre), ["build", "client"])
        self.assertEqual(B.preflight_todo(pre, ["client"]), ["build"])

    def test_no_injection_means_no_extra_done_steps(self):
        self.assertEqual(B.Probe(None).done_steps(), [])
        self.assertEqual(B.Probe({"done_steps": ["client"]}).done_steps(), ["client"])


class Control(unittest.TestCase):
    def test_control_injects_only_really_finished_steps(self):
        here = {"build": (False, "changed"), "client": (True, "intact"), "wine": (True, "installed")}
        self.assertEqual(S.control_injection(here), {"done_steps": ["client", "wine"]})

    def test_set_up_here_reads_done_checks_and_counts_errors_as_not_done(self):
        class Ok(B.Step):
            id = "ok"

            def done(self, ctx):
                return True, "yes"

        class Boom(B.Step):
            id = "boom"

            def done(self, ctx):
                raise OSError("unreadable")

        class Brew(B.Step):
            id = "brew"

            def done(self, ctx):
                raise AssertionError("brew is judged by its own check, not here")

        with mock.patch.object(B, "STEPS", [Brew(), Ok(), Boom()]):
            here = S.set_up_here()
        self.assertEqual(here["ok"], (True, "yes"))
        self.assertFalse(here["boom"][0])
        self.assertNotIn("brew", here)
        self.assertEqual(S.control_injection(here), {"done_steps": ["ok"]})


if __name__ == "__main__":
    unittest.main()
