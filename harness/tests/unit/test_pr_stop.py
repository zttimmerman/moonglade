"""A pr run stopped at the unit layer (test-pyramid verifier C2/C3; unit layer, synthetic inputs): nothing after the
unit layer runs except unit-layer guards, it isn't recorded in pr-runs.jsonl, and report.pr_average ignores
aborted or failed records."""
from hslib import runner
from hslib.checks import pyramid_checks as PC
from hslib.registry import Check

from .base import UnitCase


def _c(cid, layer="integration", stack="up", order=100):
    return Check(cid, lambda ctx: {"ran": cid}, order, "synthetic", stack, "", layer, None, False, False, (), "exclusive")


class UnitStop(UnitCase):
    def test_only_unit_layer_guards_run(self):
        rest = [_c("i.x"), _c("j.x", layer="journey"), _c("t.x", stack="down", order=300),
                _c("release.player_data_untouched", stack="down", order=998),
                _c("backups.player_untouched", layer="unit", stack="any", order=998)]
        sk = runner.unit_stop_skips(rest)
        self.assertEqual(sorted(sk), ["i.x", "j.x", "release.player_data_untouched", "t.x"])
        recs = runner.execute(rest, {}, ensure=lambda *_a: None, quiet=True, skipped=sk)
        st = {r["id"]: r["status"] for r in recs}
        self.assertEqual(st["backups.player_untouched"], "pass")
        self.assertEqual(st["release.player_data_untouched"], "skip")
        self.assertTrue(all(r["status"] == "skip" for r in recs if r["id"] in sk))

    def test_counterexample_completed_pr_run_is_recorded(self):
        self.assertTrue(runner.records_pr_run({"tier": "pr"}, None))
        self.assertFalse(runner.records_pr_run({"tier": "pr", "stopped_at_unit_layer": True}, None))
        self.assertFalse(runner.records_pr_run({"tier": "pr", "power": {"stop": {"message": "x"}}}, None))
        self.assertFalse(runner.records_pr_run({"tier": "pr"}, ["x.*"]))


class PrAverageHistory(UnitCase):
    def test_aborted_and_failed_records_ignored(self):
        hist = [{"sha": "a", "acceptance_s": 600.0, "ok": True}, {"sha": "b", "acceptance_s": 0.0, "ok": False},
                {"sha": "c", "acceptance_s": None, "ok": False, "stopped_at_unit_layer": True},
                {"sha": "d", "acceptance_s": 700.0}]
        self.assertEqual([r["sha"] for r in PC.completed_pr_runs(hist)], ["a"])

    def test_counterexample_the_mean_moves_without_the_filter(self):
        ok = [{"sha": f"s{i}", "acceptance_s": 16 * 60.0, "ok": True, "full": False} for i in range(5)]
        bad = [{"sha": f"x{i}", "acceptance_s": 0.0, "ok": False, "full": False} for i in range(5)]
        self.assertTrue(PC.pr_average(ok + bad)["ok"])                       # unfiltered: the aborted runs hide a miss
        self.assertFalse(PC.pr_average(PC.completed_pr_runs(ok + bad))["ok"])
