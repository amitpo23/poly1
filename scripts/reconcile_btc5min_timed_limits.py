#!/usr/bin/env python3
"""Reconcile btc5min_timed positions whose resting LIMIT TP already filled.

After 2026-05-25 R25 we discovered: the btc5min_timed daemon places
GTC limit SELL orders for TP and stores tp_resting_order_id in
response_json, but NOTHING polls those orders for fills. The
positions show as btc5min_timed_open in the trade_log even after
Polymarket has matched the limit and the position is effectively
closed on-chain.

This script:
  1. Finds every btc5min_timed_open with a tp_resting_order_id
  2. Queries Polymarket for each order's status
  3. If MATCHED → writes a close row with the realized PnL
     (price = limit_price, side flipped, status = closed_take_profit)
  4. If LIVE → leaves alone, daemon's max_hold or PM will handle
  5. If CANCELED → leaves alone

Read-only by default. Use --apply to write the close rows.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def reconcile(db_path: str, *, apply: bool = False) -> dict:
    from agents.application.trade_log import TradeLog
    from agents.polymarket.polymarket import Polymarket

    tl = TradeLog(db_path=db_path)
    pm = Polymarket(live=True)

    summary = {"scanned": 0, "matched_to_close": 0, "still_live": 0, "errors": 0}

    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT t1.id, t1.ts, t1.cycle_id, t1.market_id, t1.token_id,
                   t1.side, t1.price, t1.size_usdc, t1.response_json
            FROM trades t1
            WHERE t1.status = 'btc5min_timed_open'
              AND NOT EXISTS (
                SELECT 1 FROM trades t2
                WHERE t2.token_id = t1.token_id
                  AND t2.id > t1.id
                  AND (t2.status LIKE 'closed_%' OR t2.status LIKE 'resolved_%')
              )
            """
        ).fetchall()

    for r in rows:
        summary["scanned"] += 1
        r = dict(r)
        try:
            resp = json.loads(r["response_json"] or "{}")
        except Exception:
            summary["errors"] += 1
            continue

        tp_order_id = resp.get("tp_resting_order_id")
        if not tp_order_id:
            continue

        try:
            order = pm.client.get_order(tp_order_id)
        except Exception as exc:
            print(f"id={r['id']}: get_order failed: {exc}", file=sys.stderr)
            summary["errors"] += 1
            continue

        if not isinstance(order, dict):
            summary["errors"] += 1
            continue

        status = order.get("status", "").upper()
        if status != "MATCHED":
            summary["still_live"] += 1
            print(f"id={r['id']}: LIMIT {tp_order_id[:18]}... still {status}")
            continue

        # MATCHED — write close row
        size_matched = float(order.get("size_matched", 0))
        exit_price = float(order.get("price", 0))
        if size_matched <= 0 or exit_price <= 0:
            print(f"id={r['id']}: invalid size/price ({size_matched}/{exit_price})", file=sys.stderr)
            summary["errors"] += 1
            continue

        # PnL calculation. Entry side stored in resp['side']:
        # - 'BUY' = we bought YES at live_price; our_token_entry = live_price
        # - 'SELL' = we SOLD YES = held NO at (1 - live_price)
        entry_side = resp.get("side", r.get("side", "BUY"))
        entry_price = float(r["price"])
        if entry_side == "BUY":
            our_token_entry = entry_price
        else:
            our_token_entry = max(0.01, 1.0 - entry_price)
        # We SOLD `size_matched` shares of our token at `exit_price`.
        # Cost basis = size_matched * our_token_entry
        # Proceeds  = size_matched * exit_price
        proceeds = size_matched * exit_price
        cost_basis = size_matched * our_token_entry
        pnl = proceeds - cost_basis

        print(
            f"id={r['id']}: MATCHED → entry={our_token_entry:.4f} exit={exit_price:.4f} "
            f"shares={size_matched:.4f} PnL=${pnl:+.4f}"
        )
        summary["matched_to_close"] += 1

        if apply:
            close_response = {
                "source": "reconcile_btc5min_timed_limits",
                "tp_resting_order_id": tp_order_id,
                "exit_price": exit_price,
                "size_matched": size_matched,
                "shares_sold": size_matched,
                "actual_proceeds_usdc": round(proceeds, 4),
                "cost_basis_usdc": round(cost_basis, 4),
                "pnl_usdc_real": round(pnl, 4),
                "status": "matched",
                "reconciled_at": datetime.now(timezone.utc).isoformat(),
            }
            cycle_id = f"close:{r['token_id'][:12]}"
            tl.insert_terminal(
                cycle_id=cycle_id,
                market_id=str(r["market_id"]),
                status="closed_take_profit",
                token_id=str(r["token_id"]),
                side="SELL",
                price=exit_price,
                size_usdc=proceeds,
                confidence=None,
                response=close_response,
            )

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="./data/trade_log.db")
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write close rows. Without this, dry-run only.",
    )
    args = parser.parse_args()
    if not Path(args.db).exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2
    summary = reconcile(args.db, apply=args.apply)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
