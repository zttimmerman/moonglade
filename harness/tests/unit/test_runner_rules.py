"""Runner and fixture rules fixed in test-pyramid part 2 (unit layer, synthetic inputs):
- the shared-server reset never runs after lifecycle.down (it stops the stack; a reset would read a stopped server as
  leaked state), and describe() names a changed save interval only when the interval changed;
- the release bracket covers only the release.*/firstlaunch.* checks the acceptance segments run (never unit-layer
  ones, which run in the unit layer and never reach the execution log the bracket is judged on);
- report.layer_budgets' "new selected checks" counts only acceptance checks (integration and journeys);
- warm journeys run before `hs down`, cold ones after it."""
from hslib import fixture, runner
from hslib.checks import pyramid_checks as PC
from hslib.registry import Check

from .base import UnitCase


def _c(cid, layer="integration", stack="up", order=100, scope="exclusive"):
    return Check(cid, lambda ctx: {"ran": cid}, order, "synthetic", stack, "", layer, None, False, False, (), scope)


class FakeFixture:
    def __init__(self):
        self.calls = []

    def run(self, batch, ctx, ensure, say):
        self.calls.append([c.id for c in batch])
        return [runner.run_one(c, ctx, ensure) for c in batch]


class ResetRules(UnitCase):
    def test_no_reset_after_lifecycle_down(self):
        fx = FakeFixture()
        checks = [_c("gm.server_info", order=10), _c("lifecycle.down", order=190)]
        recs = runner.execute(checks, {"fixture": fx}, ensure=lambda *_a: None, quiet=True)
        self.assertEqual(fx.calls, [["gm.server_info"]])
        self.assertEqual({r["id"]: r["status"] for r in recs}, {"gm.server_info": "pass", "lifecycle.down": "pass"})

    def test_counterexample_a_shared_check_still_goes_through_the_fixture(self):
        fx = FakeFixture()
        runner.execute([_c("x.shared", order=5)], {"fixture": fx}, ensure=lambda *_a: None, quiet=True)
        self.assertEqual(fx.calls, [["x.shared"]])

    def test_describe_interval_change(self):
        d = {"saves": {"expected": {"enabled": True, "interval_s": 120}, "actual": {"enabled": True, "interval_s": 5}}}
        self.assertEqual(fixture.describe(d), ["save interval changed (120 -> 5 s)"])

    def test_describe_stopped_server_isnt_an_interval_change(self):
        d = {"saves": {"expected": {"enabled": True, "interval_s": 120}, "actual": None},
             "error": {"expected": None, "actual": "URLError: connection refused"}}
        words = fixture.describe(d)
        self.assertFalse(any("interval" in w for w in words), words)
        self.assertIn("error changed", words)

    def test_describe_saves_switched_off(self):
        d = {"saves": {"expected": {"enabled": True, "interval_s": 120}, "actual": {"enabled": False, "interval_s": 120}}}
        self.assertTrue(fixture.describe(d)[0].startswith("saves settings changed"))


class BracketPlan(UnitCase):
    def test_unit_layer_release_checks_are_outside_the_bracket(self):
        checks = [_c("release.mode_resolution", layer="unit", stack="any", order=5),
                  _c("release.pins", stack="down", order=300), _c("firstlaunch.downloads", layer="gate", order=310),
                  _c(runner.PDU_ID, stack="any", order=995)]
        plan = runner.release_bracket_plan(checks, "rid", state_dir=self.tmp)
        self.assertEqual(plan["inner"], ["release.pins", "firstlaunch.downloads"])
        self.assertEqual(plan["start_check"], "release.pins")

    def test_counterexample_only_unit_release_checks(self):
        checks = [_c("release.mode_resolution", layer="unit", stack="any", order=5),
                  _c(runner.PDU_ID, stack="any", order=995)]
        plan = runner.release_bracket_plan(checks, "rid", state_dir=self.tmp)
        self.assertEqual(plan["inner"], [])
        self.assertEqual(plan["start_check"], runner.PDU_ID)


class NewSelectedChecks(UnitCase):
    def _doc(self):
        return {"meta": {"tier": "pr", "full_run": False, "selection": {"full": False, "selected": {
                    "i.new": ["x"], "j.new": ["x"], "g.new": ["x"]}},
                         "timing": {"new_checks_s": {"i.new": 50.0, "j.new": 60.0, "g.new": 900.0}},
                         "layers": {"unit": {"wall_s": 40.0}, "selection": {"wall_s": 1.0}}},
                "checks": [{"id": "i.new", "layer": "integration"}, {"id": "j.new", "layer": "journey"},
                           {"id": "g.new", "layer": "gate"}]}

    def test_gate_checks_dont_count(self):
        rec = PC.layer_budgets_record(self._doc())
        self.assertEqual(rec["evidence"]["new_selected_checks_s"], 110.0)
        self.assertEqual(rec["status"], "pass", rec.get("message"))

    def test_counterexample_acceptance_over_budget_fails(self):
        doc = self._doc()
        doc["meta"]["timing"]["new_checks_s"]["i.new"] = 70.0
        rec = PC.layer_budgets_record(doc)
        self.assertEqual(rec["status"], "fail")
        self.assertIn("new selected checks", rec["message"])


class JourneySegments(UnitCase):
    def test_warm_before_down_cold_after(self):
        warm = _c("journey.pause", layer="journey", stack="up")
        cold = _c("journey.saves", layer="journey", stack="down")
        down = _c("lifecycle.down")
        self.assertLess(runner.segment(warm), runner.segment(down))
        self.assertGreater(runner.segment(cold), runner.segment(down))
        self.assertEqual(runner.segment(_c(runner.FIRST_PLAY, layer="journey", stack="up")), 7)


class GateNeverInPr(UnitCase):
    """harness-reporting "Gate-layer checks" (test-pyramid part 3): a pr run never runs a gate check, FULL or not; the
    selected ones are owed, with the path that selected them."""

    def _reg(self):
        return {c.id: c for c in (_c("u.x", layer="unit", stack="any", order=1), _c("i.x", order=100),
                                  _c("j.x", layer="journey", order=450), _c("g.x", layer="gate", order=30),
                                  _c("guard.x", layer="integration", stack="any", order=995))}

    def test_full_selection_runs_no_gate(self):
        from hslib import selection as S
        reg = self._reg()
        sel = {"full": True, "selected": {}}
        ids = [c.id for c in runner.select(reg, "pr", None, S.pr_chosen(sel, reg))]
        self.assertEqual(sorted(ids), ["guard.x", "i.x", "j.x", "u.x"])

    def test_selected_gate_is_owed_not_run(self):
        from hslib import selection as S
        reg = self._reg()
        sel = {"full": False, "selected": {"g.x": ["covers harness/hslib/lanes.py"], "i.x": ["covers play"]}}
        sel["gate_owed"] = S.gate_owed(sel["selected"], reg)
        ids = [c.id for c in runner.select(reg, "pr", None, S.pr_chosen(sel, reg))]
        self.assertNotIn("g.x", ids)
        self.assertEqual(sel["gate_owed"], {"g.x": ["covers harness/hslib/lanes.py"]})
        self.assertTrue(any("g.x" in ln and "lanes.py" in ln for ln in runner.owed_lines(sel)))

    def test_counterexample_full_tier_runs_the_gate(self):
        ids = [c.id for c in runner.select(self._reg(), "full", None)]
        self.assertIn("g.x", ids)

    def test_active_changes_is_no_impact(self):
        from hslib import selection as S
        self.assertTrue(S.match("harness/active-changes", S.NO_IMPACT))
        self.assertFalse(S.match("harness/hslib/runner.py", S.NO_IMPACT))


class TargetedRules(UnitCase):
    """harness-reporting "Targeted integration checks" (test-pyramid design 16): a pr run picks a targeted check only
    when its covers selected it, never on FULL or as a run guard; a full run runs it; its time is reported apart."""

    def _reg(self):
        t = _c("t.slow", order=300)
        t.targeted = True
        g = _c("t.guard", stack="down", order=998)
        g.targeted = True
        return {c.id: c for c in (_c("u.x", layer="unit", stack="any", order=1), _c("i.x", order=100), t, g,
                                  _c("guard.x", layer="integration", stack="any", order=995))}

    def test_full_selection_skips_targeted(self):
        from hslib import selection as S
        reg = self._reg()
        ids = [c.id for c in runner.select(reg, "pr", None, S.pr_chosen({"full": True, "selected": {}}, reg))]
        self.assertEqual(sorted(ids), ["guard.x", "i.x", "u.x"])

    def test_covers_selects_targeted(self):
        from hslib import selection as S
        reg = self._reg()
        sel = {"full": True, "selected": {"t.slow": ["covers harness/hslib/backups.py"]}}
        ids = [c.id for c in runner.select(reg, "pr", None, S.pr_chosen(sel, reg))]
        self.assertIn("t.slow", ids)
        self.assertNotIn("t.guard", ids)

    def test_counterexample_full_tier_runs_targeted(self):
        ids = [c.id for c in runner.select(self._reg(), "full", None)]
        self.assertIn("t.slow", ids)
        self.assertIn("t.guard", ids)

    def test_targeted_time_is_apart(self):
        L = PC.synthetic_targeted_layers(integration_s=100.0, journeys_s=50.0, targeted_s=70.0)
        self.assertEqual((L["integration"]["wall_s"], L["acceptance"]["wall_s"], L["targeted"]["wall_s"]),
                         (100.0, 150.0, 70.0))
        L = PC.synthetic_targeted_layers(integration_s=100.0, journeys_s=50.0, targeted_s=70.0, mark=False)
        self.assertEqual((L["integration"]["wall_s"], L["targeted"]["wall_s"]), (170.0, 0.0))
