#!/usr/bin/env python3
"""Create a recoverable SQLite backup of data/trade_log.db."""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def backup(db_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = out_dir / f"trade_log-{stamp}.db"
    with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=30) as src:
        with sqlite3.connect(out) as dst:
            src.backup(dst)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/trade_log.db")
    parser.add_argument("--out-dir", default="data/backups")
    args = parser.parse_args()
    out = backup(Path(args.db), Path(args.out_dir))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
