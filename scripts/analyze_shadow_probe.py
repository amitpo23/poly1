#!/usr/bin/env python3
"""Analyze scanner_executor shadow entries against the current CLOB book."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.constants import POLYGON


def _entries(book, side: str) -> list:
    rows = getattr(book, side, None)
    if rows is None and isinstance(book, dict):
        rows = book.get(side, [])
    return rows or []


def _price_size(row) -> tuple[float, float]:
    if hasattr(row, "price"):
        return float(row.price), float(row.size)
    return float(row["price"]), float(row["size"])


def _best_bid_ask(book) -> tuple[float | None, float | None, float, float]:
    bids = sorted((_price_size(x) for x in _entries(book, "bids")), reverse=True)
    asks = sorted((_price_size(x) for x in _entries(book, "asks")))
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    bid_depth = sum(p * s for p, s in bids)
    ask_depth = sum(p * s for p, s in asks)
    return best_bid, best_ask, bid_depth, ask_depth


def _safe_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def load_shadow_entries(db_path: str, since: str, limit: int) -> list[dict]:
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, ts, market_id, token_id, action, reason, signal_source,
                   live_entry_price, internal_probability, raw_ev, net_ev,
                   mode, features_json
            FROM decision_journal
            WHERE decision = 'SHADOW_ENTER'
              AND ts >= ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (since, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def analyze(db_path: str, since: str, limit: int = 100) -> dict:
    client = ClobClient("https://clob.polymarket.com", chain_id=POLYGON)
    results = []
    for row in load_shadow_entries(db_path, since, limit):
        features = _safe_json(row.get("features_json"))
        token_id = str(row.get("token_id") or "")
        amount_usdc = float(features.get("amount_usdc") or 1.0)
        entry_price = float(row.get("live_entry_price") or 0.0)
        result = {
            "id": row["id"],
            "ts": row["ts"],
            "market_id": row["market_id"],
            "token_id": token_id,
            "question": features.get("question"),
            "entry_price": round(entry_price, 4),
            "amount_usdc": round(amount_usdc, 4),
            "internal_probability": row.get("internal_probability"),
            "net_ev_at_entry": row.get("net_ev"),
            "mode": row.get("mode"),
            "signal_source": row.get("signal_source"),
        }
        if not token_id or entry_price <= 0:
            result["error"] = "missing_token_or_entry"
            results.append(result)
            continue
        try:
            book = client.get_order_book(token_id)
            best_bid, best_ask, bid_depth, ask_depth = _best_bid_ask(book)
            result.update(
                {
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "bid_depth_usdc": round(bid_depth, 2),
                    "ask_depth_usdc": round(ask_depth, 2),
                }
            )
            if best_bid is None:
                result["error"] = "no_exit_bid"
            else:
                shares = amount_usdc / entry_price
                exit_value = shares * best_bid
                pnl_usdc = exit_value - amount_usdc
                pnl_pct = (best_bid / entry_price) - 1.0
                result.update(
                    {
                        "exit_value_usdc": round(exit_value, 6),
                        "pnl_usdc": round(pnl_usdc, 6),
                        "pnl_pct": round(pnl_pct, 4),
                        "hit_5pct_now": pnl_pct >= 0.05,
                        "hit_stop_now": pnl_pct <= -0.03,
                    }
                )
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        results.append(result)

    complete = [r for r in results if "pnl_pct" in r]
    wins = [r for r in complete if r["pnl_pct"] > 0]
    take_profit_now = [r for r in complete if r.get("hit_5pct_now")]
    stops_now = [r for r in complete if r.get("hit_stop_now")]
    summary = {
        "since": since,
        "entries": len(results),
        "priced": len(complete),
        "positive_now": len(wins),
        "take_profit_now": len(take_profit_now),
        "stop_loss_now": len(stops_now),
        "avg_pnl_pct_now": (
            None
            if not complete
            else round(sum(r["pnl_pct"] for r in complete) / len(complete), 4)
        ),
        "avg_pnl_usdc_now": (
            None
            if not complete
            else round(sum(r["pnl_usdc"] for r in complete) / len(complete), 6)
        ),
        "unique_markets": len({r.get("market_id") for r in results}),
    }
    return {"summary": summary, "entries": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze scanner shadow probe entries")
    parser.add_argument("--db", default="./data/trade_log.db")
    parser.add_argument("--since", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    payload = analyze(args.db, args.since, args.limit)
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
