#!/usr/bin/env python3
"""24h strategy scoreboard for poly1 + swarm.

Read-only by design. It summarizes local journals so operators can compare
agents by decisions, executions, vetoes, errors, and live/dry-run activity.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


DEFAULT_POLY_DB = "./data/trade_log.db"
DEFAULT_SWARM_DB = os.path.expanduser("~/Desktop/poly/bot/data/swarm.db")


def _connect(path: str) -> sqlite3.Connection | None:
    db_path = Path(path).expanduser().resolve()
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _print_rows(title: str, rows: Iterable[sqlite3.Row], empty: str = "none") -> None:
    print(f"\n## {title}")
    seen = False
    for row in rows:
        seen = True
        print("  " + " | ".join(f"{k}={row[k]}" for k in row.keys()))
    if not seen:
        print(f"  {empty}")


def _heartbeat_age(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return "missing"
    age = max(0.0, time.time() - p.stat().st_mtime)
    return f"{age:.0f}s"


def poly_report(conn: sqlite3.Connection, since_iso: str, limit: int) -> None:
    _print_rows(
        "poly1 trades by status",
        conn.execute(
            """
            SELECT status, COUNT(*) AS n,
                   ROUND(COALESCE(SUM(size_usdc), 0), 4) AS size_usdc
            FROM trades
            WHERE ts >= ?
            GROUP BY status
            ORDER BY n DESC, status
            """,
            (since_iso,),
        ),
    )

    _print_rows(
        "poly1 latest trades",
        conn.execute(
            """
            SELECT id, substr(ts, 1, 19) AS ts, status, market_id, side,
                   price, ROUND(COALESCE(size_usdc, 0), 4) AS size_usdc,
                   substr(COALESCE(error, ''), 1, 90) AS error
            FROM trades
            WHERE ts >= ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (since_iso, limit),
        ),
    )

    if _table_exists(conn, "brain_decisions"):
        _print_rows(
            "brain decisions by agent/reason",
            conn.execute(
                """
                SELECT agent, decision_type, approved, reason, COUNT(*) AS n,
                       ROUND(AVG(score), 4) AS avg_score
                FROM brain_decisions
                WHERE ts >= ?
                GROUP BY agent, decision_type, approved, reason
                ORDER BY n DESC, agent, reason
                """,
                (since_iso,),
            ),
        )

    if _table_exists(conn, "scalper_pairs"):
        _print_rows(
            "scalper pair states",
            conn.execute(
                """
                SELECT state, COUNT(*) AS n,
                       ROUND(SUM(cost_up + cost_down), 4) AS deployed_usdc,
                       SUM(attempts_up + attempts_down) AS attempts
                FROM scalper_pairs
                GROUP BY state
                ORDER BY n DESC, state
                """
            ),
        )


def swarm_report(conn: sqlite3.Connection, since_ms: int, limit: int) -> None:
    if _table_exists(conn, "fills"):
        _print_rows(
            "swarm fills by agent",
            conn.execute(
                """
                SELECT agent, COUNT(*) AS fills,
                       ROUND(SUM(size), 4) AS size,
                       ROUND(SUM(fee), 6) AS fees
                FROM fills
                WHERE ts_ms >= ?
                GROUP BY agent
                ORDER BY fills DESC, agent
                """,
                (since_ms,),
            ),
        )

    if _table_exists(conn, "pending_orders"):
        _print_rows(
            "swarm orders by agent/status",
            conn.execute(
                """
                SELECT agent, status, COUNT(*) AS n,
                       ROUND(SUM(size_usd), 4) AS size_usd
                FROM pending_orders
                WHERE created_ms >= ?
                GROUP BY agent, status
                ORDER BY n DESC, agent, status
                """,
                (since_ms,),
            ),
        )

        _print_rows(
            "swarm latest orders",
            conn.execute(
                """
                SELECT id, agent, status, market_id, side, outcome,
                       ROUND(size_usd, 4) AS size_usd,
                       price_cents, substr(COALESCE(note, ''), 1, 90) AS note
                FROM pending_orders
                WHERE created_ms >= ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (since_ms, limit),
            ),
        )

    if _table_exists(conn, "pnl_events"):
        _print_rows(
            "swarm realized pnl events",
            conn.execute(
                """
                SELECT agent, COUNT(*) AS events, ROUND(SUM(pnl), 4) AS pnl
                FROM pnl_events
                WHERE ts_ms >= ?
                GROUP BY agent
                ORDER BY pnl DESC, agent
                """,
                (since_ms,),
            ),
        )

    if _table_exists(conn, "agent_state"):
        _print_rows(
            "swarm agent state freshness",
            conn.execute(
                """
                SELECT agent,
                       CAST((strftime('%s','now') * 1000 - updated_ms) / 1000 AS INT)
                         AS age_seconds
                FROM agent_state
                ORDER BY agent
                """
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--poly-db", default=DEFAULT_POLY_DB)
    parser.add_argument("--swarm-db", default=DEFAULT_SWARM_DB)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=args.hours)
    since_iso = since.isoformat()
    since_ms = int(since.timestamp() * 1000)

    print("# Strategy scoreboard")
    print(f"window_hours={args.hours}")
    print(f"generated_utc={now.isoformat()}")
    print(f"since_utc={since_iso}")
    print("\n## heartbeats")
    print(f"  trader={_heartbeat_age('./data/heartbeat')}")
    print(f"  scalper={_heartbeat_age('./data/scalper_heartbeat')}")
    print(f"  btc_daily={_heartbeat_age('./data/btc_daily_heartbeat')}")
    print(f"  position_manager={_heartbeat_age('./data/position_manager_heartbeat')}")

    poly = _connect(args.poly_db)
    if poly is None:
        print(f"\n## poly1\n  missing db: {args.poly_db}")
    else:
        try:
            poly_report(poly, since_iso, args.limit)
        finally:
            poly.close()

    swarm = _connect(args.swarm_db)
    if swarm is None:
        print(f"\n## swarm\n  missing db: {args.swarm_db}")
    else:
        try:
            swarm_report(swarm, since_ms, args.limit)
        finally:
            swarm.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
