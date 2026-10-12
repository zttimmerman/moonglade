"""selection.add_needs: a check whose GIVEN continues another's state brings that check into the selection."""
import unittest
from types import SimpleNamespace as NS

from hslib import selection as S
from hslib.registry import REGISTRY, load_all


class Needs(unittest.TestCase):
    def test_needed_check_is_added_transitively(self):
        tests = {"a": NS(needs=("b",)), "b": NS(needs=("c",)), "c": NS(needs=()), "d": NS(needs=())}
        sel = {"a": ["covers x"]}
        S.add_needs(sel, tests)
        self.assertEqual(set(sel), {"a", "b", "c"})
        self.assertEqual(sel["b"], ["needed by a (its GIVEN)"])

    def test_counterexample_nothing_needed_adds_nothing(self):
        tests = {"a": NS(needs=()), "b": NS(needs=())}
        sel = {"a": ["smoke set"]}
        S.add_needs(sel, tests)
        self.assertEqual(set(sel), {"a"})

    def test_dungeon_session2_needs_session1(self):
        load_all()
        self.assertEqual(REGISTRY["dng.session2"].needs, ("dng.session1",))
        sel = {"dng.session2": ["smoke set: harness/hslib/config.py"]}
        S.add_needs(sel, REGISTRY)
        self.assertIn("dng.session1", sel)


if __name__ == "__main__":
    unittest.main()
