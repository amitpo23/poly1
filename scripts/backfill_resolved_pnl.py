#!/usr/bin/env python3
"""Backfill pnl_usdc_real into existing resolved_* rows.

Discovered 2026-05-25 during adversarial review: resolution_sync was
writing realized_pnl_usdc, but calibrator reads pnl_usdc_real.
resolution_sync.py was fixed forward, but existing rows still have
NULL pnl_usdc_real.

For resolved_loss: pnl = -total_cost_usdc (full write-off).
For resolved_yes/resolved_no: pnl = payout_usdc - total_cost_usdc.

Both values are already in response_json (just under different keys).
This script reads them and writes the canonical field.

Read-only by default. Use --apply to write.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def backfill(db_path: str, *, apply: bool = False) -> dict:
    summary = {"scanned": 0, "would_write": 0, "wrote": 0, "skipped": 0}
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, status, response_json FROM trades "
            "WHERE status LIKE 'resolved_%' AND response_json IS NOT NULL"
        ).fetchall()
        for r in rows:
            summary["scanned"] += 1
            try:
                payload = json.loads(r["response_json"])
            except (ValueError, TypeError):
                summary["skipped"] += 1
                continue
            if not isinstance(payload, dict):
                summary["skipped"] += 1
                continue
            if payload.get("pnl_usdc_real") is not None:
                continue  # already filled
            # Derive from existing fields.
            pnl = payload.get("realized_pnl_usdc")
            if pnl is None:
                # resolved_loss without realized_pnl_usdc → use -total_cost
                if r["status"] == "resolved_loss":
                    cost = payload.get("total_cost_usdc")
                    if cost is not None:
                        pnl = -float(cost)
                    else:
                        summary["skipped"] += 1
                        continue
                else:
                    summary["skipped"] += 1
                    continue
            payload["pnl_usdc_real"] = round(float(pnl), 4)
            summary["would_write"] += 1
            if apply:
                conn.execute(
                    "UPDATE trades SET response_json=? WHERE id=?",
                    (json.dumps(payload), r["id"]),
                )
                summary["wrote"] += 1
        if apply:
            conn.commit()
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="./data/trade_log.db")
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write changes. Without this, dry-run only.",
    )
    args = parser.parse_args()
    if not Path(args.db).exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2
    summary = backfill(args.db, apply=args.apply)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
