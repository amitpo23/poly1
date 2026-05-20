#!/usr/bin/env python3
"""Update decision_journal markouts for shadow entries.

The goal is not to prove live profitability from one point sample.  It is to
turn every shadow decision into measurable evidence: after 1/3/5/15/60 minutes,
was the exit book better or worse than the simulated entry?
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.application.trade_log import TradeLog


HORIZON_COLUMNS = {
    1: "outcome_1m_json",
    3: "outcome_3m_json",
    5: "outcome_5m_json",
    15: "outcome_15m_json",
    60: "outcome_60m_json",
}


def _parse_ts(value: object) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _entries(book, side: str) -> list:
    rows = getattr(book, side, None)
    if rows is None and isinstance(book, dict):
        rows = book.get(side, [])
    return rows or []


def _price_size(row) -> tuple[float, float]:
    if hasattr(row, "price"):
        return float(row.price), float(row.size)
    return float(row["price"]), float(row["size"])


def _book_to_snapshot(book, token_id: str) -> dict:
    bids = sorted((_price_size(x) for x in _entries(book, "bids")), reverse=True)
    asks = sorted((_price_size(x) for x in _entries(book, "asks")))
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    return {
        "token_id": token_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": "clob_live_fallback",
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_depth_usdc": sum(p * s for p, s in bids),
        "ask_depth_usdc": sum(p * s for p, s in asks),
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "target_lag_seconds": None,
        "fallback": "live_clob_latest",
    }


class LiveBookFallback:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._client = None

    def get(self, token_id: str) -> dict | None:
        if not self.enabled:
            return None
        try:
            if self._client is None:
                from py_clob_client_v2.client import ClobClient
                from py_clob_client_v2.constants import POLYGON

                self._client = ClobClient("https://clob.polymarket.com", chain_id=POLYGON)
            return _book_to_snapshot(self._client.get_order_book(str(token_id)), str(token_id))
        except Exception:
            return None


def _eligible_rows(db_path: str, minutes: int, limit: int) -> list[dict]:
    column = HORIZON_COLUMNS[int(minutes)]
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=int(minutes))).isoformat()
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT *
            FROM decision_journal
            WHERE decision IN ('SHADOW_ENTER', 'SHADOW_QUOTE')
              AND token_id IS NOT NULL
              AND token_id != ''
              AND ts <= ?
              AND {column} IS NULL
            ORDER BY id ASC
            LIMIT ?
            """,
            (cutoff, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]


def _markout_payload(row: dict, snapshot: dict, minutes: int) -> dict:
    features = _json(row.get("features_json"))
    decision = str(row.get("decision") or "")
    best_bid = _float(snapshot.get("best_bid"))
    best_ask = _float(snapshot.get("best_ask"))
    bid_depth = _float(snapshot.get("bid_depth_usdc"))
    ask_depth = _float(snapshot.get("ask_depth_usdc"))
    entry_price = _float(row.get("live_entry_price"))
    maker_bid = _float(row.get("market_price"))
    maker_ask = _float(row.get("live_entry_price"))
    payload = {
        "minutes": int(minutes),
        "snapshot_ts": snapshot.get("ts"),
        "target_lag_seconds": snapshot.get("target_lag_seconds"),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_depth_usdc": bid_depth,
        "ask_depth_usdc": ask_depth,
        "question": features.get("question"),
    }
    if decision == "SHADOW_ENTER" and entry_price and best_bid is not None:
        pnl_pct = (best_bid / entry_price) - 1.0
        payload.update(
            {
                "model": "taker_entry_exit_at_future_bid",
                "entry_price": entry_price,
                "pnl_pct": round(pnl_pct, 6),
                "hit_take_profit_5pct": pnl_pct >= 0.05,
                "hit_stop_3pct": pnl_pct <= -0.03,
            }
        )
    elif decision == "SHADOW_QUOTE" and maker_bid and best_bid is not None:
        bid_markout = (best_bid / maker_bid) - 1.0
        payload.update(
            {
                "model": "maker_bid_markout",
                "maker_bid": maker_bid,
                "maker_ask": maker_ask,
                "bid_markout_pct": round(bid_markout, 6),
                "quoted_spread_pct": (
                    None
                    if maker_ask is None
                    else round((maker_ask - maker_bid) / maker_bid, 6)
                ),
                "future_ask_crossed_maker_bid": (
                    None if best_ask is None else best_ask <= maker_bid
                ),
            }
        )
    else:
        payload["error"] = "missing_price_for_markout"
    return payload


def _float(value) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def update_markouts(
    *,
    db_path: str,
    horizons: list[int],
    limit: int,
    max_lag_seconds: float,
    live_fallback: bool,
) -> dict:
    log = TradeLog(db_path=db_path)
    fallback = LiveBookFallback(live_fallback)
    summary = {"updated": 0, "missing_snapshot": 0, "live_fallback": 0, "by_horizon": {}}
    for minutes in horizons:
        rows = _eligible_rows(db_path, minutes, limit)
        horizon_stats = {
            "eligible": len(rows),
            "updated": 0,
            "missing_snapshot": 0,
            "live_fallback": 0,
        }
        for row in rows:
            ts = _parse_ts(row.get("ts"))
            if ts is None:
                horizon_stats["missing_snapshot"] += 1
                summary["missing_snapshot"] += 1
                continue
            snapshot = log.orderbook_snapshot_at_or_after(
                str(row["token_id"]),
                ts + timedelta(minutes=int(minutes)),
                max_lag_seconds=max_lag_seconds,
            )
            if snapshot is None:
                snapshot = fallback.get(str(row["token_id"]))
                if snapshot is None:
                    horizon_stats["missing_snapshot"] += 1
                    summary["missing_snapshot"] += 1
                    continue
                horizon_stats["live_fallback"] += 1
                summary["live_fallback"] += 1
            payload = _markout_payload(row, snapshot, minutes)
            log.update_decision_journal_markout(int(row["id"]), minutes=minutes, payload=payload)
            horizon_stats["updated"] += 1
            summary["updated"] += 1
        summary["by_horizon"][str(minutes)] = horizon_stats
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Update shadow decision markouts")
    parser.add_argument("--db", default="./data/trade_log.db")
    parser.add_argument("--horizons", default="1,3,5,15")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--max-lag-seconds", type=float, default=90.0)
    parser.add_argument(
        "--no-live-fallback",
        action="store_true",
        help="do not use current CLOB book when historical snapshots are missing",
    )
    args = parser.parse_args()
    horizons = [int(x.strip()) for x in args.horizons.split(",") if x.strip()]
    payload = update_markouts(
        db_path=args.db,
        horizons=horizons,
        limit=args.limit,
        max_lag_seconds=args.max_lag_seconds,
        live_fallback=not args.no_live_fallback,
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
