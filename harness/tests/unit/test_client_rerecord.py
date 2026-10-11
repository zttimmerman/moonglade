"""clientinst.rerecord_source (test-pyramid verifier C1b): a source replaced by its owner with the same client gets
its size/mtime re-recorded in the manifest only; a source whose contents differ is refused and the manifest kept."""
import json
import os

from hslib import clientinst as CI

from .base import UnitCase


class Rerecord(UnitCase):
    def _setup(self):
        src = self.tmp / "src"
        src.mkdir()
        (src / "Wow.exe").write_bytes(b"MZ-wow")
        (src / "Data").mkdir()
        (src / "Data" / "common.MPQ").write_bytes(b"mpq")
        man = self.tmp / "manifest.json"
        so = CI._open_source(src)
        files = {r: {"size": so.size(r), **({"sha256": CI._src_sha(so, r)} if CI._is_hashed(r) else {})}
                 for r in so.members}
        st = CI.source_stat(src)
        man.write_text(json.dumps({"source": {"path": str(src.resolve()), "kind": "dir", "before": st, "after": st},
                                   "files": files}))
        return src, man

    def test_same_contents_new_mtime_rerecorded(self):
        src, man = self._setup()
        os.utime(src / "Wow.exe", ns=(2 * 10**18, 2 * 10**18))
        res = CI.rerecord_source(src, man)
        m = json.loads(man.read_text())
        self.assertTrue(res["rerecorded"])
        self.assertEqual(m["source"]["before"], CI.source_stat(src))
        self.assertEqual(len(m["source"]["rerecorded"]), 1)

    def test_counterexample_changed_binary_refused(self):
        src, man = self._setup()
        before = man.read_text()
        (src / "Wow.exe").write_bytes(b"MZ-pat")             # same size, other bytes
        with self.assertRaises(CI.ClientInstallError):
            CI.rerecord_source(src, man)
        self.assertEqual(man.read_text(), before)
