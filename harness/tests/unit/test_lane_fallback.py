"""Lane B refused (lanes.schedule's preflight fallback): every planned lane-B check still runs in lane A, and the
stack-down ones run after the shared segment's `hs down`, not inside the shared segment (test-pyramid part 2)."""
import json
from unittest import mock

from hslib import lanes, power, runner
from hslib.registry import Check, load_all

from .base import UnitCase


def _c(cid, stack, order, layer="integration"):
    return Check(cid, lambda ctx: {"ran": cid}, order, "synthetic", stack, "", layer, None, False, False, (), "exclusive")


class Fallback(UnitCase):
    def run_schedule(self, avail_gb):
        load_all()                       # the LANE_B table is checked against the registry
        sel = [_c("play.loop_clean_exit", "down", 300), _c("gm.server_info", "up", 60),
               _c("lifecycle.up", "down", 40), _c("lifecycle.down", "up", 190), _c("x.cold", "down", 30)]
        order = []
        mem = json.dumps({"pressure_level": 1, "avail_gb": avail_gb, "swap_used_gb": 1.0, "swap_free_gb": 3.0})
        with mock.patch.object(runner, "_ensure_stack", lambda *a, **k: None), \
                mock.patch.object(runner, "run_one", side_effect=lambda c, ctx, ensure: order.append(c.id) or
                                  {"id": c.id, "status": "pass", "layer": c.layer, "duration_s": 0.0, "evidence": {"x": 1}}):
            recs, meta = lanes.schedule(sel, {}, None, requested=2, tier="full", only=None,
                                        switches={lanes.MEMORY_ENV: mem}, battery=power.Battery("100p"),
                                        battery_seq="100p", ignore_power=False, mem_start={}, say=lambda m: None)
        return order, recs, meta

    def test_refused_lane_b_cold_checks_run_after_down(self):
        order, recs, meta = self.run_schedule(2.0)
        self.assertTrue(meta["fallback"].startswith("preflight:"), meta["fallback"])
        self.assertEqual(order, ["x.cold", "lifecycle.up", "gm.server_info", "lifecycle.down", "play.loop_clean_exit"])
        self.assertEqual(sorted(r["id"] for r in recs), sorted(order))
        self.assertEqual(meta["assignment"]["b_to_a"], ["play.loop_clean_exit"])
