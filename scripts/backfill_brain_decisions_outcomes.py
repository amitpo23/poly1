#!/usr/bin/env python3
"""Backfill brain_decisions.outcome_status from trades.status.

The meta_brain reliability layer (WinRateAdvisor) reads `outcome_status`
to compute per-source win rate. The original `resolution_sync` only
populates it when a Polymarket market RESOLVES naturally; trades that
close via TP/SL/timeout never had their brain_decisions annotated,
which left 229k+ rows with NULL outcome_status and made the brain
unable to learn.

This script walks the `trades` table for any rows in a terminal status
(closed_take_profit, closed_stop_loss, closed_timeout, resolved_*), and
for each one, annotates the matching brain_decisions (by market_id +
token_id) that don't have an outcome_status yet.

Safe to run repeatedly; only updates rows where outcome_status IS NULL.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


TERMINAL_STATUSES = (
    "closed_take_profit",
    "closed_stop_loss",
    "closed_timeout",
    "closed_dust",
    "closed_manual",
    "resolved_yes",
    "resolved_no",
    "resolved_loss",
    "resolved_skipped_no",
)


def backfill(
    db_path: str,
    *,
    days: int,
    max_match_age_hours: int,
    dry_run: bool,
) -> dict:
    placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    summary = {
        "scanned_closes": 0,
        "annotated_decisions": 0,
        "by_status": Counter(),
        "skipped_no_market_token": 0,
        "skipped_already_annotated": 0,
    }
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT id, ts, market_id, token_id, status, response_json
            FROM trades
            WHERE status IN ({placeholders})
              AND ts >= ?
            ORDER BY ts
            """,
            (*TERMINAL_STATUSES, cutoff),
        ).fetchall()
        summary["scanned_closes"] = len(rows)
        for row in rows:
            market_id = row["market_id"]
            token_id = row["token_id"]
            status = row["status"]
            if not market_id or not token_id:
                summary["skipped_no_market_token"] += 1
                continue
            try:
                resp = json.loads(row["response_json"] or "{}")
            except (TypeError, ValueError):
                resp = {}
            pnl = resp.get("pnl_usdc_real")
            outcome_payload = {
                "source": "backfill_brain_decisions_outcomes",
                "trade_id": int(row["id"]),
                "trade_ts": row["ts"],
                "pnl_usdc_real": pnl,
            }
            ts_floor = (
                datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
                - timedelta(hours=int(max_match_age_hours))
            ).isoformat()
            # Match: same market_id + token_id, decisions older than the close,
            # within max_match_age_hours, and not already annotated.
            target_rows = conn.execute(
                """
                SELECT id FROM brain_decisions
                WHERE market_id = ? AND token_id = ?
                  AND outcome_status IS NULL
                  AND ts >= ? AND ts <= ?
                """,
                (str(market_id), str(token_id), ts_floor, row["ts"]),
            ).fetchall()
            if not target_rows:
                summary["skipped_already_annotated"] += 1
                continue
            if not dry_run:
                outcome_json = json.dumps(outcome_payload, default=str)
                for trow in target_rows:
                    conn.execute(
                        "UPDATE brain_decisions SET outcome_status = ?, outcome_json = ? "
                        "WHERE id = ?",
                        (status, outcome_json, int(trow["id"])),
                    )
                conn.commit()
            summary["annotated_decisions"] += len(target_rows)
            summary["by_status"][status] += len(target_rows)
    summary["by_status"] = dict(summary["by_status"])
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/trade_log.db")
    parser.add_argument("--days", type=int, default=30, help="lookback in days for trades")
    parser.add_argument(
        "--max-match-age-hours",
        type=int,
        default=48,
        help=(
            "Match a brain_decision row only if it was made within this "
            "many hours BEFORE the close. Protects against accidentally "
            "tagging a long-ago decision with an unrelated future outcome."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2

    summary = backfill(
        args.db,
        days=args.days,
        max_match_age_hours=args.max_match_age_hours,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
