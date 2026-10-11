"""Warm journeys (play_checks.WARM; harness-control "Warm journeys reuse a running stack only in the harness"):
"nothing left running" ignores the shared stack only while it is the very stack the journey found (same pids); a
changed or partly stopped stack still counts; ./play gets the keep-stack switch only inside a warm journey."""
from unittest import mock

from hslib.checks import play_checks as PC

from .base import UnitCase

STACK = {"mysqld": [101], "authserver": [102], "worldserver": [103]}


class WarmRemaining(UnitCase):
    def _remaining(self, procs, warm):
        with mock.patch.object(PC.stack, "stack_processes", return_value=procs), \
                mock.patch.object(PC.stack, "port_open", side_effect=lambda p: any(procs.values())), \
                mock.patch.object(PC.launch, "prefix_processes", return_value=[]), \
                mock.patch.object(PC.launch, "wow_processes", return_value=[]), \
                mock.patch.object(PC, "play_processes", return_value=[]):
            old = PC.WARM
            PC.WARM = warm
            try:
                return PC.remaining()
            finally:
                PC.WARM = old

    def test_same_stack_is_clear_in_a_warm_journey(self):
        self.assertTrue(PC.is_clear(self._remaining(STACK, {"pids": {k: sorted(v) for k, v in STACK.items()}})))

    def test_counterexample_cold_the_stack_counts(self):
        self.assertFalse(PC.is_clear(self._remaining(STACK, None)))

    def test_counterexample_a_restarted_worldserver_counts(self):
        warm = {"pids": {"mysqld": [101], "authserver": [102], "worldserver": [999]}}
        self.assertFalse(PC.is_clear(self._remaining(STACK, warm)))

    def test_counterexample_a_partly_stopped_stack_counts(self):
        part = {"mysqld": [101], "authserver": [], "worldserver": [103]}
        self.assertFalse(PC.is_clear(self._remaining(part, {"pids": {"mysqld": [101], "authserver": [], "worldserver": [103]}})))

    def test_keep_stack_switch_only_in_a_warm_journey(self):
        old = PC.WARM
        try:
            PC.WARM = None
            self.assertEqual(PC.warm_env(), {})
            PC.WARM = {"pids": {}}
            self.assertEqual(PC.warm_env(), {PC.play.KEEP_STACK_ENV: "1", "HS_HARNESS_RUN": "1"})
        finally:
            PC.WARM = old
