"""Impact selection rules (harness/hslib/selection.py; harness-reporting "Impact selection is fail-safe") and the
budget and pr-average rules (checks/pyramid_checks.py), on synthetic inputs."""
import shutil
import tempfile
from pathlib import Path

from hslib import selection as S
from hslib.checks import pyramid_checks as PC

from .base import UnitCase


class Selection(UnitCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp(prefix="hs-unit-sel-"))
        cls.cases = PC.selection_cases(cls.d)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def test_docs_only_selects_unit_layer(self):
        self.assertTrue(self.cases["verdict"]["a_only_unit"])

    def test_owned_cpp_selects_its_tests(self):
        self.assertTrue(self.cases["verdict"]["b_pause_tests_and_journey_no_saves"])

    def test_unowned_and_shared_select_smoke(self):
        self.assertTrue(self.cases["verdict"]["c_smoke_with_reason"])
        self.assertTrue(self.cases["verdict"]["h_smoke_with_reason"])

    def test_foundations_and_git_failure_are_full(self):
        for k in ("d_full", "e_full", "j_full"):
            self.assertTrue(self.cases["verdict"][k], k)

    def test_helper_selects_declarers_of_its_importer(self):
        self.assertTrue(self.cases["verdict"]["f_only_declarers"])

    def test_config_section_and_pin_keys(self):
        self.assertTrue(self.cases["verdict"]["g_saves_no_pause"])
        self.assertTrue(self.cases["verdict"]["i_autobalance_declarers_not_full"])

    def test_smoke_set_shape(self):
        self.assertTrue(self.cases["verdict"]["smoke_is_first_play_plus_cheapest_per_area"])


class Keys(UnitCase):
    def test_changed_keys_ignore_prose(self):
        old = "# Saves keep this many\nMoonglade.Saves.KeepAuto = 5\n"
        new = "# Saves keep this many automatic saves\nMoonglade.Saves.KeepAuto = 6\n"
        self.assertEqual(S.changed_keys(old, new), {"Moonglade.Saves.KeepAuto"})
        self.assertIsNone(S.changed_keys(None, new))

    def test_match_globs(self):
        self.assertTrue(S.match("a/b/c.py", ("a/**",)))
        self.assertTrue(S.match("x/CMakeLists.txt", ("**/CMakeLists.txt",)))
        self.assertFalse(S.match("a.md.txt", ("*.md",)))


class Budgets(UnitCase):
    def test_each_budget_miss_fails_and_g_passes(self):
        v = PC.synthetic_budget_cases()["verdict"]
        self.assertEqual([k for k, ok in v.items() if not ok], [])

    def test_pr_average_cases(self):
        v = PC.synthetic_pr_cases()["verdict"]
        self.assertEqual([k for k, ok in v.items() if not ok], [])
