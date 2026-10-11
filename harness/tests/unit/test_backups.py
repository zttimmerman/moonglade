"""backups.rotate / list_backups over fake backups in a temp folder (backups.rotation's rules; Zach 2026-10-06:
keep 10 automatic and 3 before-restore)."""
import json

from hslib import backups as B

from .base import UnitCase


def fake(root, bid, kind=None, valid=True):
    d = root / bid
    d.mkdir(parents=True)
    kind = kind or bid.rsplit("-", 1)[-1] if "before-restore" not in bid else "before-restore"
    m = {"id": bid, "kind": kind, "files": {}, "databases": {k: {} for k in B.DATABASES}}
    if valid:
        (d / "manifest.json").write_text(json.dumps(m))
    return d


def ids(n, kind="auto", day=1):
    return [f"202610{day:02d}-{h:02d}0000-{kind}" for h in range(n)]


class Rotation(UnitCase):
    def test_keeps_newest_ten_auto(self):
        for i in ids(13):
            fake(self.tmp, i)
        gone = [g["id"] for g in B.rotate(self.tmp)]
        self.assertEqual(gone, ids(13)[:3][::-1])
        self.assertEqual([m["id"] for m in B.list_backups(self.tmp)], ids(13)[3:][::-1])

    def test_kinds_rotate_separately(self):
        for i in ids(10):
            fake(self.tmp, i)
        for i in ids(5, "before-restore", day=2):
            fake(self.tmp, i, "before-restore")
        gone = sorted(g["id"] for g in B.rotate(self.tmp))
        self.assertEqual(gone, sorted(ids(5, "before-restore", day=2)[:2]))

    def test_nothing_to_rotate(self):
        for i in ids(10):
            fake(self.tmp, i)
        self.assertEqual(B.rotate(self.tmp), [])

    def test_incomplete_folders_ignored(self):
        for i in ids(12):
            fake(self.tmp, i)
        fake(self.tmp, "20261001-230000-auto", valid=False)    # no manifest: a backup in progress
        (self.tmp / ".del-20261001-000000-auto").mkdir()
        gone = [g["id"] for g in B.rotate(self.tmp)]
        self.assertEqual(sorted(gone), ids(12)[:2])
        self.assertTrue((self.tmp / "20261001-230000-auto").is_dir())

    def test_missing_root_lists_nothing(self):
        self.assertEqual(B.list_backups(self.tmp / "nope"), [])

    def test_wrong_databases_not_listed(self):
        d = fake(self.tmp, "20261001-010000-auto")
        m = json.loads((d / "manifest.json").read_text())
        m["databases"] = {"characters": {}}
        (d / "manifest.json").write_text(json.dumps(m))
        self.assertEqual(B.list_backups(self.tmp), [])
