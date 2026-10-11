"""Build-input stamps (server-build; test-pyramid part 3 fix): after a passing incremental build only the install is
stamped (with the inputs it was built from, so core.patch_applied and the run's "server current" step see the right
patch queue), the build tree is not, and the next `--clean` still builds from empty."""
from unittest import mock

from hslib import build

from .base import UnitCase


class Stamps(UnitCase):
    def setUp(self):
        super().setUp()
        self.b, self.i = self.tmp / "build-stamp", self.tmp / "install-stamp"
        self.p = [mock.patch.object(build, "build_stamp_path", lambda: self.b),
                  mock.patch.object(build, "install_stamp_path", lambda: self.i)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()
        super().tearDown()

    def test_incremental_stamps_only_the_install(self):
        build._write_stamps("h1", {"patches_core": {"0001.patch": "abc"}}, incremental=True)
        self.assertFalse(self.b.exists())
        self.assertEqual(build.read_stamp(self.i), "h1")
        mode, _why = build.decide_mode("h1", build.read_stamp(self.b), build.read_stamp(self.i), False)
        self.assertEqual(mode, "from-empty")         # no from-empty build vouched for these inputs

    def test_from_empty_stamps_both(self):
        build._write_stamps("h2", {})
        self.assertEqual((build.read_stamp(self.b), build.read_stamp(self.i)), ("h2", "h2"))
        mode, _why = build.decide_mode("h2", build.read_stamp(self.b), build.read_stamp(self.i), False)
        self.assertEqual(mode, "verified-current")

    def test_counterexample_stale_install_stamp_isnt_current(self):
        build._write_stamps("old", {}, incremental=True)
        self.assertNotEqual(build.read_stamp(self.i), "new")
