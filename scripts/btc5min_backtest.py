#!/usr/bin/env python3
"""Grid-search TP/SL for btc_5min from historical fired trades.

Today's calibration revealed btc_5min has 35% winrate (the highest of
any agent) but EV=-$0.045/trade (negative). The reward/risk is wrong:
avg loss exceeds avg win × winrate. Goal: find TP/SL pair that flips
EV positive given the historical price paths.

Strategy:
1. Pull every btc_5min entry from the last N days.
2. For each: replay the price path (orderbook_snapshots OR derived
   from the trade's close price + interpolation).
3. Apply candidate (TP, SL) thresholds — first to hit wins.
4. Report PnL grid.

Uses only the trades table — no live market data fetch.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _btc5min_entries(conn: sqlite3.Connection, *, days: int) -> list[dict]:
    """Pull btc_5min entry rows (status btc_5min_open) within window."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=int(days))
    ).isoformat()
    rows = conn.execute(
        """
        SELECT id, ts, token_id, side, price, size_usdc, cycle_id, market_id
        FROM trades
        WHERE status = 'btc_5min_open' AND ts >= ?
        ORDER BY id
        """,
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def _matched_close(conn: sqlite3.Connection, entry: dict) -> dict | None:
    """Find the close row that ended this position."""
    row = conn.execute(
        """
        SELECT id, ts, status, price, size_usdc, response_json
        FROM trades
        WHERE token_id = ?
          AND id > ?
          AND (status LIKE 'closed_%' OR status LIKE 'resolved_%')
        ORDER BY id LIMIT 1
        """,
        (entry["token_id"], entry["id"]),
    ).fetchone()
    if not row:
        return None
    return dict(row)


def _pnl_for_path(
    entry_price: float,
    side: str,
    exit_price: float,
) -> float:
    """PnL pct for a fired trade given entry + exit prices.

    side='BUY' means we hold the YES token: exit_price / entry_price - 1.
    side='SELL' means we hold the NO token (entry was for outcomes[0]
    YES at price `entry`, NO at price `1-entry`): own NO at 1-entry,
    exit at 1-exit_price ⇒ (1-exit) / (1-entry) - 1.
    """
    if entry_price <= 0 or entry_price >= 1:
        return 0.0
    if exit_price <= 0 or exit_price >= 1:
        return 0.0
    if side == "BUY":
        return (exit_price / entry_price) - 1.0
    if side == "SELL":
        return ((1.0 - exit_price) / (1.0 - entry_price)) - 1.0
    return 0.0


def _simulate(
    entry: dict,
    close: dict,
    *,
    tp_pct: float,
    sl_pct: float,
) -> float:
    """Apply candidate TP/SL to a known entry+close.

    Without intraday tick data we approximate: if the realised close
    PnL exceeds tp_pct, treat as TP fire (capture tp_pct). If below
    -sl_pct, treat as SL fire (cap loss at -sl_pct). Otherwise return
    the actual close PnL (the bot held to whatever exit triggered).

    This is a conservative simulator — real intraday vol may have hit
    a tighter SL before the position recovered. For a more accurate
    backtest we'd need orderbook_snapshots over the hold period.
    """
    realized = _pnl_for_path(
        entry_price=float(entry["price"]),
        side=str(entry["side"] or "BUY"),
        exit_price=float(close["price"]),
    )
    if realized >= tp_pct:
        return tp_pct
    if realized <= -sl_pct:
        return -sl_pct
    return realized


def backtest(db_path: str, *, days: int, position_size_usdc: float) -> dict:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    entries = _btc5min_entries(conn, days=days)
    paired = []
    for entry in entries:
        close = _matched_close(conn, entry)
        if close is None:
            continue
        paired.append((entry, close))

    if not paired:
        return {"total_entries": len(entries), "paired": 0, "results": []}

    tp_grid = [0.04, 0.06, 0.08, 0.10, 0.15, 0.20]
    sl_grid = [0.02, 0.03, 0.04, 0.06, 0.08]
    results = []
    for tp in tp_grid:
        for sl in sl_grid:
            wins, losses, sum_win, sum_loss = 0, 0, 0.0, 0.0
            for entry, close in paired:
                pnl_pct = _simulate(entry, close, tp_pct=tp, sl_pct=sl)
                pnl_usdc = pnl_pct * position_size_usdc
                if pnl_pct > 0.001:
                    wins += 1
                    sum_win += pnl_usdc
                elif pnl_pct < -0.001:
                    losses += 1
                    sum_loss += pnl_usdc
            total = wins + losses
            if total == 0:
                continue
            wr = wins / total
            ev = (sum_win + sum_loss) / total
            results.append(
                {
                    "tp_pct": tp,
                    "sl_pct": sl,
                    "n": total,
                    "wins": wins,
                    "losses": losses,
                    "winrate": round(wr, 4),
                    "avg_win_usdc": round(sum_win / wins, 4) if wins else None,
                    "avg_loss_usdc": round(sum_loss / losses, 4) if losses else None,
                    "ev_per_trade_usdc": round(ev, 4),
                    "total_pnl_usdc": round(sum_win + sum_loss, 4),
                }
            )

    results.sort(key=lambda x: -x["ev_per_trade_usdc"])
    return {
        "total_entries": len(entries),
        "paired": len(paired),
        "position_size_usdc": position_size_usdc,
        "top_results": results[:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/trade_log.db")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument(
        "--position-size-usdc", type=float, default=1.0
    )
    args = parser.parse_args()
    if not Path(args.db).exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2
    result = backtest(args.db, days=args.days, position_size_usdc=args.position_size_usdc)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
