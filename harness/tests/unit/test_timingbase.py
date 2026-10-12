"""timingbase.join: a full run joins every check the baseline lacks; a pr run joins only checks named one by one that
passed there (merged checks no full run has executed yet), and existing entries never change."""
import json
import unittest

from hslib import timingbase as T
from tests.unit.base import UnitCase


def _run(tier: str, checks: list[tuple[str, str, float]], filt=None, full=None) -> dict:
    return {"meta": {"tier": tier, "full_run": (tier == "full") if full is None else full, "filter": filt or [],
                     "repo_sha": "abc", "started": "s", "finished": "f", "wall_s": 1.0, "host": {"machine": "arm64"}},
            "checks": [{"id": i, "status": st, "duration_s": s} for i, st, s in checks]}


class Join(UnitCase):
    def setUp(self):
        super().setUp()
        self.base = self.tmp / "tb.json"
        self.base.write_text(json.dumps({"schema": 2, "runs": {"old": {}},
                                         "checks": {"a.old": {"s": 5.0, "run": "old"}}}))

    def _write(self, name, doc):
        p = self.tmp / name
        p.write_text(json.dumps(doc))
        return p

    def test_pr_run_joins_only_named_passed_checks(self):
        p = self._write("pr.json", _run("pr", [("a.old", "pass", 99.0), ("dng.x", "pass", 55.0),
                                               ("alt.new", "pass", 41.0)]))
        res = T.join(p, "pr-x", self.base, ids=["dng.x"])
        b = json.loads(self.base.read_text())
        self.assertEqual(res["added"], ["dng.x"])
        self.assertEqual(b["checks"]["dng.x"]["s"], 55.0)
        self.assertNotIn("alt.new", b["checks"])                 # not named: the change's own new check stays new
        self.assertEqual(b["checks"]["a.old"]["s"], 5.0)         # existing time unchanged
        self.assertEqual(b["runs"]["pr-x"]["tier"], "pr")

    def test_counterexample_pr_run_without_ids_refused(self):
        p = self._write("pr.json", _run("pr", [("dng.x", "pass", 55.0)]))
        with self.assertRaises(T.BaselineError):
            T.join(p, "pr-x", self.base)

    def test_counterexample_failed_or_absent_named_check_refused(self):
        p = self._write("pr.json", _run("pr", [("dng.x", "fail", 55.0)]))
        with self.assertRaises(T.BaselineError):
            T.join(p, "pr-x", self.base, ids=["dng.x"])
        with self.assertRaises(T.BaselineError):
            T.join(p, "pr-y", self.base, ids=["dng.absent"])
        self.assertNotIn("dng.x", json.loads(self.base.read_text())["checks"])

    def test_counterexample_filtered_pr_run_refused(self):
        p = self._write("pr.json", _run("pr", [("dng.x", "pass", 55.0)], filt=["dng.*"]))
        with self.assertRaises(T.BaselineError):
            T.join(p, "pr-x", self.base, ids=["dng.x"])

    def test_full_run_still_joins_everything_new(self):
        p = self._write("full.json", _run("full", [("a.old", "pass", 9.0), ("b.new", "pass", 3.0)]))
        res = T.join(p, "full-1", self.base)
        self.assertEqual(res["added"], ["b.new"])
        self.assertEqual(json.loads(self.base.read_text())["checks"]["a.old"]["s"], 5.0)


if __name__ == "__main__":
    unittest.main()
