"""Shared-server fixture rules (test-pyramid verifier cycle F1/F6; unit layer, fakes only):
- fixture character names never break AzerothCore's name rules, prefix and suffix included (run tag tuv7x: stem
  "eftgxx" + "x" made "Hseftgxxx", refused as CHAR_NAME_THREE_CONSECUTIVE);
- a failed identity setup fails the test that needed it (with evidence) instead of crashing the run;
- the fixture removes its identities before the first check that stops the stack, and a cleanup error fails
  report.fixture_cleanup."""
import itertools
from unittest import mock

from hslib import fixture, runner, sharedfix
from hslib.checks import reporting
from hslib.registry import Check

from .base import UnitCase


def _c(cid, layer="integration", stack="up", order=100, scope="exclusive"):
    return Check(cid, lambda ctx: {"ran": cid}, order, "synthetic", stack, "", layer, None, False, False, (), scope)


class Names(UnitCase):
    def test_tuv7x_is_skipped(self):
        k = int("tuv7x", 36)
        self.assertEqual(sharedfix._letters(k), "eftgxx")
        self.assertIsNone(sharedfix.identity_names(k))

    def test_many_run_tags_give_valid_distinct_names(self):
        tags = [int("tuv7x", 36) // 1000, 0, 1, 99999] + list(range(0, 100000, 37))
        for tag in tags:
            with mock.patch.object(sharedfix, "_COUNTER", itertools.count()):
                for _ in range(20):
                    i = sharedfix.new_identity(tag)
                    for n in (i.char, i.alt):
                        self.assertTrue(n.isalpha() and 2 <= len(n) <= 12, n)
                        low = n.lower()
                        self.assertFalse(any(low[j] == low[j - 1] == low[j - 2] for j in range(2, len(low))), n)
                    self.assertNotEqual(i.char, i.alt)

    def test_counterexample_rule_catches_suffix_triple(self):
        self.assertFalse(sharedfix.valid_char_name("Hseftgxxx"))
        self.assertFalse(sharedfix.valid_char_name("HsAaa"))          # case-insensitive
        self.assertTrue(sharedfix.valid_char_name("Hseftgxxz"[:-1] + "z"))


class FailClosed(UnitCase):
    def test_identity_setup_error_fails_the_test_not_the_run(self):
        chk = _c("x.char", scope="character")
        chk.identity = True

        def boom():
            raise RuntimeError("account create refused")
        fx = fixture.SharedServer([chk])
        with mock.patch.object(sharedfix.S, "ensure", boom), \
                mock.patch.object(sharedfix, "cleanup_stale", lambda: {"errors": []}), \
                mock.patch.object(sharedfix, "shared_state", lambda own=frozenset(): {"state": {}, "duration_s": 0.0}), \
                mock.patch("hslib.companion.world_facts_warm", lambda: {"from": "fake"}), \
                mock.patch.object(sharedfix, "cleanup_identities", lambda ids: {"errors": [], "n": len(ids)}):
            recs = fx.run([chk], {}, lambda *_a: None, lambda *_a: None)
            meta = fx.finish({})
        self.assertEqual(recs[0]["status"], "fail")
        self.assertIn("account create refused", recs[0]["message"])
        self.assertIn("fixture_identity_setup", recs[0]["evidence"])
        self.assertEqual(meta["cleanup"]["n"], 1)               # the half-made identity is still removed
        rep = reporting.fixture_cleanup_record({"meta": {"fixture": meta}})
        self.assertEqual(rep["status"], "fail")


class CleanupOrder(UnitCase):
    def test_cleanup_runs_before_lifecycle_down(self):
        events = []

        class FakeFx:
            base = {"state": {}}

            def run(self, batch, ctx, ensure, say):
                events.append(("test", batch[0].id))
                return [runner.run_one(c, ctx, ensure) for c in batch]

            def cleanup_identities(self):
                events.append(("cleanup",))

        def down_fn(ctx):
            events.append(("down",))
            return {}
        down = Check("lifecycle.down", down_fn, 190, "synthetic", "up", "", "integration", None, False, False, (),
                     "exclusive")
        runner.execute([_c("gm.server_info", order=10), down, _c("journey.first_play", layer="journey", stack="down",
                                                                   order=460)],
                       {"fixture": FakeFx()}, ensure=lambda *_a: None, quiet=True)
        self.assertEqual(events[:3], [("test", "gm.server_info"), ("cleanup",), ("down",)])

    def test_counterexample_cleanup_error_fails_the_report(self):
        good = {"identities": {"a": {}}, "cleanup": {"errors": []}}
        bad = {"identities": {"a": {}}, "cleanup": {"error": "OperationalError: Can't connect to MySQL"}}
        never = {"identities": {"a": {}}, "cleanup": None}
        self.assertEqual(reporting.fixture_cleanup_problems(good), [])
        self.assertTrue(reporting.fixture_cleanup_problems(bad))
        self.assertTrue(reporting.fixture_cleanup_problems(never))
