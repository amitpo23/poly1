#!/usr/bin/env python3
"""Create a recoverable SQLite backup of data/trade_log.db.

Rotates older backups by `--keep` (default 6 → keeps ~24h of 4-hourly
backups). At ~750MB per backup, unbounded retention was eating ~4.5GB
per day of disk; that triggered a 91% full alert on 2026-05-23.
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


BACKUP_PREFIX = "trade_log-"
BACKUP_SUFFIX = ".db"


def backup(db_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = out_dir / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=30) as src:
        with sqlite3.connect(out) as dst:
            src.backup(dst)
    return out


def rotate(out_dir: Path, keep: int) -> list[Path]:
    """Delete the oldest backups, keeping `keep` newest. Returns the list
    of deleted paths. Files are sorted by name (timestamps are ISO-format
    so lexicographic == chronological). Only files matching the
    BACKUP_PREFIX/SUFFIX are considered — won't touch unrelated files."""
    if keep < 0:
        raise ValueError(f"keep must be >= 0, got {keep}")
    if not out_dir.exists():
        return []
    candidates = sorted(
        p for p in out_dir.iterdir()
        if p.is_file()
        and p.name.startswith(BACKUP_PREFIX)
        and p.name.endswith(BACKUP_SUFFIX)
    )
    if len(candidates) <= keep:
        return []
    to_delete = candidates[:-keep] if keep > 0 else candidates
    deleted = []
    for path in to_delete:
        try:
            path.unlink()
            deleted.append(path)
        except OSError:
            # Don't crash the backup cron over a single rotation failure;
            # next run will retry. Leaving a file behind costs disk but is
            # safe.
            pass
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/trade_log.db")
    parser.add_argument("--out-dir", default="data/backups")
    parser.add_argument(
        "--keep",
        type=int,
        default=6,
        help="Number of newest backups to retain (default 6 ≈ 24h of 4-hourly)",
    )
    args = parser.parse_args()
    out = backup(Path(args.db), Path(args.out_dir))
    print(out)
    deleted = rotate(Path(args.out_dir), args.keep)
    if deleted:
        print(f"rotated {len(deleted)} old backup(s):")
        for p in deleted:
            print(f"  removed {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
