"""Tests for scripts/python/backup_trade_log.py — rotation logic."""
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.python.backup_trade_log import BACKUP_PREFIX, BACKUP_SUFFIX, backup, rotate


def _make_backup(out_dir: Path, ts: datetime) -> Path:
    """Create an empty backup file with the given UTC timestamp encoded
    in the filename. Returns the path. Content doesn't matter for the
    rotation tests."""
    stamp = ts.strftime("%Y%m%dT%H%M%SZ")
    p = out_dir / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    p.write_bytes(b"")
    return p


class RotateTests(unittest.TestCase):
    def test_rotate_keeps_newest_n(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            base = datetime(2026, 5, 24, 0, 0, tzinfo=timezone.utc)
            paths = [_make_backup(out, base + timedelta(hours=4 * i)) for i in range(10)]
            deleted = rotate(out, keep=3)
            survivors = sorted(p for p in out.iterdir() if p.name.startswith(BACKUP_PREFIX))
            self.assertEqual(len(survivors), 3)
            self.assertEqual(survivors, sorted(paths[-3:]))  # newest 3
            self.assertEqual(sorted(deleted), sorted(paths[:-3]))

    def test_rotate_keeps_all_when_below_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            base = datetime(2026, 5, 24, 0, 0, tzinfo=timezone.utc)
            for i in range(3):
                _make_backup(out, base + timedelta(hours=i))
            deleted = rotate(out, keep=6)
            self.assertEqual(deleted, [])
            self.assertEqual(
                len([p for p in out.iterdir() if p.name.startswith(BACKUP_PREFIX)]), 3
            )

    def test_rotate_ignores_unrelated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            base = datetime(2026, 5, 24, 0, 0, tzinfo=timezone.utc)
            for i in range(10):
                _make_backup(out, base + timedelta(hours=i))
            (out / "README.md").write_text("hi")
            (out / "trade_log.db").write_bytes(b"")  # the live db itself, no timestamp
            (out / "logfile.txt").write_text("log")
            rotate(out, keep=3)
            # Non-backup files untouched
            self.assertTrue((out / "README.md").exists())
            self.assertTrue((out / "trade_log.db").exists())
            self.assertTrue((out / "logfile.txt").exists())
            survivors = [p.name for p in out.iterdir() if p.name.startswith(BACKUP_PREFIX)]
            self.assertEqual(len(survivors), 3)

    def test_rotate_keep_zero_deletes_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            base = datetime(2026, 5, 24, 0, 0, tzinfo=timezone.utc)
            for i in range(5):
                _make_backup(out, base + timedelta(hours=i))
            deleted = rotate(out, keep=0)
            self.assertEqual(len(deleted), 5)
            self.assertEqual(
                len([p for p in out.iterdir() if p.name.startswith(BACKUP_PREFIX)]), 0
            )

    def test_rotate_missing_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            ghost = Path(tmp) / "nope"
            self.assertEqual(rotate(ghost, keep=3), [])

    def test_rotate_rejects_negative_keep(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaises(ValueError):
                rotate(out, keep=-1)


class BackupAndRotateIntegrationTest(unittest.TestCase):
    def test_backup_and_rotate_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db = tmp_path / "live.db"
            conn = sqlite3.connect(db)
            conn.executescript("CREATE TABLE t (x INTEGER); INSERT INTO t VALUES (1);")
            conn.close()

            out = tmp_path / "backups"
            out.mkdir()
            # Seed 5 fake old backups (so the new one will tip rotation)
            base = datetime(2026, 5, 24, 0, 0, tzinfo=timezone.utc)
            for i in range(5):
                _make_backup(out, base + timedelta(hours=i))

            new = backup(db, out)
            self.assertTrue(new.exists())
            self.assertGreater(new.stat().st_size, 0)

            rotate(out, keep=3)
            survivors = sorted(p for p in out.iterdir() if p.name.startswith(BACKUP_PREFIX))
            self.assertEqual(len(survivors), 3)
            # The brand-new backup must be among the survivors (newest stamp)
            self.assertIn(new, survivors)


if __name__ == "__main__":
    unittest.main()
