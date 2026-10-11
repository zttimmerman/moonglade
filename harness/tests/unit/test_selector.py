"""The runner's selection and budget rules (test-pyramid design 8; spec "Impact selection is fail-safe"), on the
prototype selector with a synthetic plan (no registry, no git)."""
from hslib import selector as S, unitrun as U

from .base import UnitCase

PLAN = {"tests": {
    "pause.freeze_matrix": {"layer": "integration", "from": ["pause.combat_frozen"], "covers": (
        "modules/mod-moonglade/src/MoongladePause.cpp",), "files": {"harness/hslib/checks/pause_checks.py"}},
    "saves.manual": {"layer": "integration", "from": ["saves.manual"], "covers": (),
                     "files": {"harness/hslib/checks/saves_checks.py"}},
    "journey.pause": {"layer": "journey", "from": ["client.pause_menu"], "covers": (
        "modules/mod-moonglade/src/MoongladePause.cpp",), "files": {"harness/hslib/checks/pause_checks.py"}},
    "journey.first_play": {"layer": "journey", "from": ["login.auto_login"], "covers": ("play",),
                           "files": {"harness/hslib/checks/login_checks.py"}},
}, "unit_owned": {}}
CLOSURES = {"harness/hslib/checks/pause_checks.py": {"harness/hslib/checks/pause_checks.py", "harness/hslib/pause.py"},
            "harness/hslib/checks/saves_checks.py": {"harness/hslib/checks/saves_checks.py", "harness/hslib/saves.py"},
            "harness/hslib/checks/login_checks.py": {"harness/hslib/checks/login_checks.py",
                                                     "harness/hslib/checks/playclient_util.py"}}


def sel(paths, **kw):
    return S.select(paths, pl=PLAN, closures=CLOSURES, **kw)


class Selection(UnitCase):
    def test_docs_only_selects_unit_layer(self):
        r = sel(["docs/x.md", "openspec/changes/a/tasks.md"])
        self.assertFalse(r["full"])
        self.assertEqual(r["selected"], {})

    def test_pause_cpp_selects_pause_and_journeys(self):
        r = sel(["modules/mod-moonglade/src/MoongladePause.cpp"])
        self.assertFalse(r["full"], r["full_reasons"])
        self.assertEqual(sorted(r["selected"]), ["journey.first_play", "journey.pause", "pause.freeze_matrix"])

    def test_unmapped_path_is_full(self):
        r = sel(["harness/hslib/newthing.py"])
        self.assertTrue(r["full"])
        self.assertIn("unmapped", r["full_reasons"][0])

    def test_full_trigger_and_git_failure(self):
        self.assertTrue(sel(["patches/core/0003-world-pause.patch"])["full"])
        self.assertTrue(sel(None)["full"])

    def test_shared_helper_selects_importing_journeys(self):
        r = sel(["harness/hslib/checks/playclient_util.py"])
        self.assertEqual(sorted(r["selected"]), ["journey.first_play"])

    def test_constants_rule(self):
        old = "A = 1\nSAVES_X = 2\ndef f():\n    return 1\n"
        self.assertEqual(S.const_change(old, old.replace("SAVES_X = 2", "SAVES_X = 3")), {"SAVES_X"})
        self.assertIsNone(S.const_change(old, old.replace("return 1", "return 2")))

    def test_selection_is_fast(self):
        r = sel(["modules/mod-moonglade/src/MoongladePause.cpp", "harness/hslib/saves.py"])
        self.assertLessEqual(r["duration_s"], 10.0)


class Budget(UnitCase):
    def test_unit_test_over_cap_fails(self):
        recs = U.parse_tap("ok a.fast 3.0\nok a.slow 2500.0\n# tests 2 failed 0\n", "unit:cpp:")
        self.assertEqual([r["duration_s"] for r in recs], [0.003, 2.5])
        self.assertGreater(recs[1]["duration_s"], U.UNIT_TEST_CAP_S)
