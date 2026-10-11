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


def real(root, bid="20261001-010000-auto", files=None):
    """A complete backup folder whose manifest lists `files` {rel: bytes} with their real sha256."""
    import hashlib
    d = root / bid
    d.mkdir(parents=True)
    files = files if files is not None else {"characters.sql.gz": b"c", "auth.sql.gz": b"a"}
    rec = {}
    for rel, data in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(data)
        rec[rel] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    m = {"id": bid, "kind": bid.split("-", 2)[-1], "files": rec, "databases": {k: {} for k in B.DATABASES}}
    (d / "manifest.json").write_text(json.dumps(m))
    return d


class Manifest(UnitCase):
    """backups.read_manifest / verify (save-backups: a backup is listed and restorable only when every file matches
    its manifest checksum)."""
    def test_complete_backup_verifies(self):
        d = real(self.tmp)
        self.assertTrue(B.verify(d)["ok"])
        self.assertEqual(B.verify(d)["checked"], 2)

    def test_changed_byte_is_a_checksum_mismatch(self):
        d = real(self.tmp)
        (d / "auth.sql.gz").write_bytes(b"A")
        v = B.verify(d)
        self.assertFalse(v["ok"])
        self.assertEqual([(b["file"], b["why"]) for b in v["bad"]], [("auth.sql.gz", "checksum mismatch")])

    def test_missing_file_is_bad(self):
        d = real(self.tmp)
        (d / "characters.sql.gz").unlink()
        v = B.verify(d)
        self.assertFalse(v["ok"])
        self.assertEqual(v["bad"][0]["file"], "characters.sql.gz")
        self.assertTrue(v["bad"][0]["why"].startswith("unreadable"))

    def test_extra_file_not_trusted(self):
        d = real(self.tmp)
        (d / "planted.sql.gz").write_bytes(b"x")
        v = B.verify(d)
        self.assertEqual([(b["file"], b["why"]) for b in v["bad"]], [("planted.sql.gz", "not in the manifest")])

    def test_invalid_manifests_are_not_backups(self):
        for bid, mut in (("20261001-020000-auto", lambda m: m.update(id="other")),
                         ("20261001-030000-auto", lambda m: m.update(kind="weekly")),
                         ("20261001-040000-auto", lambda m: m.update(databases={"world": {}}))):
            d = real(self.tmp, bid)
            m = json.loads((d / "manifest.json").read_text())
            mut(m)
            (d / "manifest.json").write_text(json.dumps(m))
            self.assertIsNone(B.read_manifest(d), bid)
            self.assertFalse(B.verify(d)["ok"], bid)
        self.assertIsNone(B.read_manifest(real(self.tmp, "not-a-backup-id")))


class Refusals(UnitCase):
    """The restore target and the test switches refuse anything but a scratch target / the harness test flag
    (save-backups restore refusals; the player's databases and backup folder are never a test target)."""
    def test_scratch_target_names_three_scratch_schemas(self):
        self.assertEqual(B.scratch_target("hsbk_rt"), {k: f"hsbk_rt_{k}" for k in B.DATABASES})
        self.assertFalse(B.is_live(B.scratch_target("hsbk_rt")))

    def test_live_and_protected_targets_refused(self):
        for bad in ("", "live", "acore", "acore_characters", "hsbk_t", "hsbk_", "HSBK_rt", "hsbk_rt;drop"):
            with self.assertRaises(B.Refused, msg=bad):
                B.scratch_target(bad)
        self.assertTrue(B.is_live(dict(B.LIVE)))

    def test_backup_dir_switch_needs_the_test_flag(self):
        class A:
            allow_test_client_dir = False
        with self.assertRaises(B.Refused) as e:
            B.plan(A(), {B.TEST_ROOT_ENV: str(self.tmp)})
        self.assertIn(B.TEST_ROOT_ENV, str(e.exception))

    def test_backup_dir_never_the_players_folder(self):
        class A:
            allow_test_client_dir = True
        for root in (B.PLAYER_ROOT, B.PLAYER_ROOT / "x", B.PLAYER_ROOT.parent):
            with self.assertRaises(B.Refused, msg=str(root)):
                B.plan(A(), {B.TEST_ROOT_ENV: str(root)})
        self.assertEqual(B.plan(A(), {B.TEST_ROOT_ENV: str(self.tmp)})["root"], self.tmp.resolve())

    def test_knobs_without_a_scratch_folder_refused(self):
        class A:
            allow_test_client_dir = True
        for k in B.TEST_KNOB_ENVS:
            with self.assertRaises(B.Refused, msg=k):
                B.plan(A(), {k: "1"})


class Notice(UnitCase):
    """The next launch's notice after a failed backup (save-backups "A failed backup never blocks quitting")."""
    def test_failure_named_with_its_reason(self):
        B.write_status(self.tmp, {"ok": False, "at_local": "2026-10-11 09:00", "error": "only 1 MB free"})
        self.assertEqual(B.failure_notice(self.tmp),
                         "Last backup failed (2026-10-11 09:00): only 1 MB free. Your earlier backups are kept.")

    def test_no_notice_after_success_or_without_status(self):
        self.assertIsNone(B.failure_notice(self.tmp))
        self.assertIsNone(B.failure_notice(None))
        B.write_status(self.tmp, {"ok": True})
        self.assertIsNone(B.failure_notice(self.tmp))
        (self.tmp / B.STATUS).write_text("{broken")
        self.assertIsNone(B.failure_notice(self.tmp))
