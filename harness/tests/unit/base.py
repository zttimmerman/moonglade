"""Shared base for Python unit tests (test-pyramid design 3, "Isolation"): a fresh temp root per test, and a
fingerprint of the player's files (env/player.json, env/settings.json, env/saves/, env/backups/) before and after,
failing the test that changed one. Prototype: the subprocess guard and process-table sample come with task 2.1."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from hslib import paths as P

GUARDED = ("player.json", "settings.json", "saves", "backups")


def _fingerprint() -> dict:
    out = {}
    for name in GUARDED:
        p = P.ROOT / "env" / name
        if p.is_file():
            st = p.stat()
            out[name] = (st.st_size, st.st_mtime_ns)
        elif p.is_dir():
            out[name] = sorted((str(f.relative_to(p)), f.stat().st_size, f.stat().st_mtime_ns)
                               for f in p.rglob("*") if f.is_file())
        else:
            out[name] = None
    return out


class UnitCase(unittest.TestCase):
    def setUp(self):
        self._fp = _fingerprint()
        self.tmp = Path(tempfile.mkdtemp(prefix="hs-unit-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        after = _fingerprint()
        changed = [k for k in GUARDED if after[k] != self._fp[k]]
        self.assertEqual(changed, [], f"a unit test changed the player's files: {changed}")
