#!/usr/bin/env python3
"""Compute synthetic PnL edge for external_conviction agents.

Operator 2026-05-25: 7 external_conviction_* agents are emitting
SHADOW_BUY_YES/NO decisions into brain_decisions but never reach the
decision_journal SHADOW_ENTER pipeline → no markouts → invisible to
the Bayesian calibrator.

This script directly joins brain_decisions to orderbook_snapshots:
  - For each SHADOW_BUY_YES (BUY YES at signal_price): synthetic
    PnL = (best_bid_at_t+5min - signal_price) / signal_price.
  - For each SHADOW_BUY_NO (BUY NO at signal_price): the NO-side
    price ≈ 1 - YES-side mid; synthetic PnL = (1-mid_at_t+5min -
    (1-signal_price)) / (1-signal_price).

Reports aggregate edge per agent and per (agent, signal_source).
Reads only — never writes to the DB.

Usage:
  python3 scripts/external_conviction_edge_report.py [--db path] [--hours 24]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _parse_ts(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _price_band(price: float) -> str:
    if price < 0.40: return "<0.40"
    if price < 0.50: return "0.40-0.49"
    if price < 0.55: return "0.50-0.54"
    if price < 0.65: return "0.55-0.64"
    if price < 0.75: return "0.65-0.74"
    return ">=0.75"


def compute_edge(db_path: str, *, hours: int, horizon_min: int) -> dict:
    """Walk SHADOW_BUY_* decisions and compute synthetic markout edge."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    horizon_sec = horizon_min * 60

    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        # Get all external_conviction SHADOW decisions in window
        decisions = conn.execute(
            """
            SELECT id, ts, agent, action, signal_source, token_id, market_id,
                   features_json
            FROM brain_decisions
            WHERE agent LIKE 'external_conviction_%'
              AND action IN ('SHADOW_BUY_YES', 'SHADOW_BUY_NO')
              AND ts > ?
              AND token_id IS NOT NULL
              AND token_id != ''
            ORDER BY ts
            """,
            (cutoff,),
        ).fetchall()

        results = []
        for d in decisions:
            d = dict(d)
            ts0 = _parse_ts(d["ts"])
            if ts0 is None:
                continue
            try:
                features = json.loads(d.get("features_json") or "{}")
            except (ValueError, TypeError):
                features = {}
            entry_price = features.get("entry_price")
            try:
                entry_price = float(entry_price) if entry_price is not None else None
            except (ValueError, TypeError):
                entry_price = None
            if entry_price is None or entry_price <= 0 or entry_price >= 1:
                continue

            # Find orderbook_snapshot closest to ts0 + horizon, within ±60s tolerance
            target_ts = (ts0 + timedelta(seconds=horizon_sec)).isoformat()
            row = conn.execute(
                """
                SELECT ts, best_bid, best_ask
                FROM orderbook_snapshots
                WHERE token_id = ?
                  AND ts BETWEEN ? AND ?
                ORDER BY ts ASC
                LIMIT 1
                """,
                (
                    str(d["token_id"]),
                    target_ts,
                    (ts0 + timedelta(seconds=horizon_sec + 60)).isoformat(),
                ),
            ).fetchone()
            if not row:
                continue
            row = dict(row)
            best_bid = row.get("best_bid")
            if best_bid is None:
                continue

            # Compute synthetic exit price + PnL based on side
            if d["action"] == "SHADOW_BUY_YES":
                # We bought YES at entry_price; exit at best_bid (selling)
                exit_price = float(best_bid)
                pnl_pct = (exit_price - entry_price) / entry_price
            else:  # SHADOW_BUY_NO
                # We bought NO at (1 - entry_price). Exit by selling NO at (1 - best_ask).
                no_entry = 1.0 - entry_price
                best_ask = row.get("best_ask") or best_bid
                no_exit = 1.0 - float(best_ask)
                if no_entry <= 0:
                    continue
                pnl_pct = (no_exit - no_entry) / no_entry

            results.append({
                "agent": d["agent"],
                "signal_source": d.get("signal_source") or "",
                "action": d["action"],
                "entry_price": entry_price,
                "price_band": _price_band(entry_price),
                "exit_price": float(best_bid),
                "pnl_pct": pnl_pct,
            })

    return _aggregate(results)


def _aggregate(rows: list[dict]) -> dict:
    """Bucket by (agent), (agent, signal_source), (agent, price_band, action)."""
    by_agent = defaultdict(list)
    by_source = defaultdict(list)
    by_band_action = defaultdict(list)
    for r in rows:
        by_agent[r["agent"]].append(r["pnl_pct"])
        by_source[(r["agent"], r["signal_source"])].append(r["pnl_pct"])
        by_band_action[(r["agent"], r["price_band"], r["action"])].append(r["pnl_pct"])

    def _stats(pnls: list[float]) -> dict:
        if not pnls:
            return {"n": 0}
        n = len(pnls)
        avg = sum(pnls) / n
        wins = sum(1 for p in pnls if p > 0)
        return {
            "n": n,
            "winrate": round(wins / n, 3),
            "avg_pnl_pct": round(avg, 4),
            "total_pnl_pct": round(sum(pnls), 4),
        }

    return {
        "total_decisions": len(rows),
        "by_agent": {a: _stats(v) for a, v in by_agent.items()},
        "by_agent_source": {
            f"{a}|{s}": _stats(v) for (a, s), v in by_source.items()
        },
        "by_band_action": {
            f"{a}|{b}|{x}": _stats(v) for (a, b, x), v in by_band_action.items()
            if len(v) >= 3
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="./data/trade_log.db")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--horizon-min", type=int, default=5,
                        help="Markout horizon in minutes (default 5)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"ERROR: db not found: {args.db}", file=sys.stderr)
        return 2

    summary = compute_edge(args.db, hours=args.hours, horizon_min=args.horizon_min)

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"\n=== External Conviction Edge — {args.hours}h, {args.horizon_min}min horizon ===\n")
        print(f"Total matched decisions: {summary['total_decisions']}")
        print()
        print("By agent (n>=5):")
        for agent, st in sorted(
            summary["by_agent"].items(),
            key=lambda kv: -kv[1].get("n", 0),
        ):
            if st.get("n", 0) < 5: continue
            ev_marker = "✅ +EV" if st["avg_pnl_pct"] > 0.005 else ("➖" if st["avg_pnl_pct"] > -0.005 else "❌")
            print(f"  {agent}: n={st['n']} wr={st['winrate']:.0%} avg={st['avg_pnl_pct']:+.4f} total={st['total_pnl_pct']:+.4f} {ev_marker}")
        print()
        print("By agent × source (n>=5, ranked by avg PnL):")
        agent_src = sorted(
            summary["by_agent_source"].items(),
            key=lambda kv: -kv[1].get("avg_pnl_pct", 0),
        )
        for key, st in agent_src:
            if st.get("n", 0) < 5: continue
            print(f"  {key}: n={st['n']} wr={st['winrate']:.0%} avg={st['avg_pnl_pct']:+.4f}")
        print()
        print("Most-positive (agent, band, action) segments:")
        bands = sorted(
            summary["by_band_action"].items(),
            key=lambda kv: -kv[1].get("avg_pnl_pct", 0),
        )
        for key, st in bands[:10]:
            print(f"  {key}: n={st['n']} wr={st['winrate']:.0%} avg={st['avg_pnl_pct']:+.4f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
