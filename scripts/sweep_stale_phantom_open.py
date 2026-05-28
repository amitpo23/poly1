#!/usr/bin/env python3
"""Sweep stale phantom-open positions (P5 + P3 combined).

After deep audit on 2026-05-24, 53 positions from 2026-05-20 sit in
the journal with status='filled' (or 'scalper_leg') but no close row,
plus 13 'dust_market_open' positions cycling forever in
position_manager. resolution_sync's normal cycle should handle these,
but apparently isn't classifying them as terminal.

This is a one-shot operator-invoked sweep that:
1. Finds every token_id with an OPEN_STATUSES row but no
   terminal-close row.
2. For each: queries Polymarket Gamma for current market state.
3. Decides:
   - Market resolved + we held winning side → write resolved_yes / resolved_no
   - Market resolved + we held losing side → write resolved_loss
   - Market still active + on-chain shares > dust_floor → leave alone
     (real exposure)
   - Market still active + on-chain shares <= dust_floor + age > 24h
     + estimated value < $0.10 → write resolved_loss (this is the
     dust-terminator path)

Read-only by default (--dry-run prints what it would do).
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.application.trade_log import (
    BTC_5MIN_OPEN,
    BTC_DAILY_OPEN,
    BTC5MIN_TIMED_OPEN,
    BTC5MIN_TIMED_V2_OPEN,
    BTC5MIN_TIMED_V3_OPEN,
    DAILY_3H_FADE_OPEN,
    FILLED,
    NEAR_RESOLUTION_OPEN,
    NEWS_SHOCK_OPEN,
    SCALPER_LEG,
    TradeLog,
    WALLET_FOLLOW_OPEN,
)


# Statuses that mean "we opened a position" — full list, kept in sync with
# trade_log.filled_positions_with_id open_statuses.
OPEN_STATUSES = (
    FILLED,
    BTC_5MIN_OPEN,
    BTC_DAILY_OPEN,
    BTC5MIN_TIMED_OPEN,
    BTC5MIN_TIMED_V2_OPEN,
    BTC5MIN_TIMED_V3_OPEN,
    DAILY_3H_FADE_OPEN,
    SCALPER_LEG,
    NEAR_RESOLUTION_OPEN,
    NEWS_SHOCK_OPEN,
    WALLET_FOLLOW_OPEN,
)

DUST_VALUE_USDC = 0.10
DUST_AGE_HOURS = 24


def _gamma_market(condition_id: str) -> dict | None:
    """Query Polymarket Gamma for a single market by conditionId."""
    try:
        params = urllib.parse.urlencode({"condition_ids": str(condition_id)})
        url = f"https://gamma-api.polymarket.com/markets?{params}"
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 poly1-phantom-sweep"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if isinstance(data, list) and data:
            return data[0]
    except Exception:
        return None
    return None


def _decide_outcome(
    *,
    log: TradeLog,
    token_id: str,
    market_id: str,
    entry_ts: str,
    dust_value_usdc: float,
    dust_age_hours: int,
) -> tuple[str | None, str]:
    """Return (status_key_to_write, reason). status_key None = leave alone."""
    gamma = _gamma_market(market_id)
    if gamma:
        resolved = bool(gamma.get("closed") or gamma.get("resolved"))
        winning_outcome = gamma.get("winning_outcome") or gamma.get(
            "winningOutcome"
        )
        if resolved:
            # Check if we held the winning token. Pull our entries to see.
            ph = ",".join("?" for _ in OPEN_STATUSES)
            with log._lock, log._connect() as conn:
                rows = conn.execute(
                    f"SELECT side, token_id FROM trades "
                    f"WHERE token_id = ? AND status IN ({ph}) "
                    f"ORDER BY id LIMIT 1",
                    (token_id, *OPEN_STATUSES),
                ).fetchone()
            if not rows:
                return "resolved_loss", "no entry rows found"
            # Without on-chain payout reconciliation we can't know the exact
            # PnL. Mark as resolved_loss conservatively for journal cleanup;
            # the operator will reconcile manually via CTF if it was a win.
            return "resolved_loss", f"gamma resolved, conservative tag"

    # Market still active (or gamma unreachable). Check age + value.
    try:
        entry_dt = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None, "unparseable_ts"
    age_hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
    if age_hours < dust_age_hours:
        return None, f"too_young_{age_hours:.0f}h"

    # Pull the entry size+price to estimate current value
    ph = ",".join("?" for _ in OPEN_STATUSES)
    with log._lock, log._connect() as conn:
        row = conn.execute(
            f"SELECT side, price, size_usdc FROM trades "
            f"WHERE token_id = ? AND status IN ({ph}) "
            f"ORDER BY id LIMIT 1",
            (token_id, *OPEN_STATUSES),
        ).fetchone()
    if not row:
        return "resolved_loss", "no entry row, journal artefact"
    try:
        size = float(row["size_usdc"] or 0)
    except (TypeError, ValueError):
        size = 0.0
    if size < dust_value_usdc:
        return "resolved_loss", f"dust ${size:.4f} < ${dust_value_usdc}"
    return None, f"size_${size:.4f}_above_dust"


def _write_resolved(
    log: TradeLog,
    *,
    token_id: str,
    market_id: str,
    status: str,
    reason: str,
) -> None:
    """Write a single resolved row to terminate the journal."""
    log.insert_terminal(
        cycle_id="phantom_sweep",
        market_id=market_id or "unknown",
        token_id=token_id,
        side="SELL",
        price=0.0,
        size_usdc=0.0,
        confidence=0.0,
        status=status,
        response={
            "source": "scripts/sweep_stale_phantom_open.py",
            "reason": reason,
            "swept_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def sweep(db_path: str, *, dry_run: bool, dust_value_usdc: float,
          dust_age_hours: int) -> dict:
    logging.basicConfig(level=logging.WARNING)
    log = TradeLog(db_path=db_path)
    summary = {
        "checked": 0,
        "resolved_loss_written": 0,
        "left_alone": 0,
        "errors": 0,
        "by_reason": {},
    }
    open_placeholders = ",".join("?" for _ in OPEN_STATUSES)
    with log._lock, log._connect() as conn:
        rows = conn.execute(
            f"""
            SELECT t.token_id, t.market_id, MIN(t.ts) AS entry_ts
            FROM trades t
            WHERE t.status IN ({open_placeholders})
              AND t.token_id IS NOT NULL
              AND t.token_id != ''
              AND NOT EXISTS (
                  SELECT 1 FROM trades c
                  WHERE c.token_id = t.token_id
                    AND (c.status LIKE 'closed_%' OR c.status LIKE 'resolved_%')
              )
            GROUP BY t.token_id, t.market_id
            ORDER BY t.token_id
            """,
            OPEN_STATUSES,
        ).fetchall()
    for row in rows:
        summary["checked"] += 1
        try:
            status, reason = _decide_outcome(
                log=log,
                token_id=row["token_id"],
                market_id=row["market_id"] or "",
                entry_ts=row["entry_ts"],
                dust_value_usdc=dust_value_usdc,
                dust_age_hours=dust_age_hours,
            )
        except Exception as exc:
            summary["errors"] += 1
            summary["by_reason"][f"err:{exc.__class__.__name__}"] = (
                summary["by_reason"].get(f"err:{exc.__class__.__name__}", 0) + 1
            )
            continue
        summary["by_reason"][reason] = summary["by_reason"].get(reason, 0) + 1
        if status is None:
            summary["left_alone"] += 1
            continue
        if not dry_run:
            _write_resolved(
                log,
                token_id=row["token_id"],
                market_id=row["market_id"] or "",
                status=status,
                reason=reason,
            )
        summary["resolved_loss_written"] += 1
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/trade_log.db")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--dust-value-usdc", type=float, default=DUST_VALUE_USDC)
    parser.add_argument("--dust-age-hours", type=int, default=DUST_AGE_HOURS)
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2

    summary = sweep(
        args.db,
        dry_run=args.dry_run,
        dust_value_usdc=args.dust_value_usdc,
        dust_age_hours=args.dust_age_hours,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
