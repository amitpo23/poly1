#!/usr/bin/env python3
"""Read-only operational snapshot for poly1.

Prints the facts an operator needs before deciding whether to trade:
runtime mode, Docker-independent heartbeat freshness, wallet balance from the
local sync table, and open/unmanaged positions from the trade log.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
DB_PATH = Path(os.getenv("TRADE_LOG_DB", DATA / "trade_log.db"))
RUNTIME_CONTROL = DATA / "runtime_control.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _age_label(path: Path) -> str:
    if not path.exists():
        return "missing"
    age = max(0.0, time.time() - path.stat().st_mtime)
    if age < 120:
        return f"{age:.0f}s ago"
    if age < 7200:
        return f"{age / 60:.1f}m ago"
    return f"{age / 3600:.1f}h ago"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _print_runtime() -> None:
    print("== Runtime ==")
    if RUNTIME_CONTROL.exists():
        data = json.loads(RUNTIME_CONTROL.read_text())
        print(f"mode: {data.get('mode')}")
        print(f"allowed_live_agents: {', '.join(data.get('allowed_live_agents') or [])}")
        print(f"budget_usdc: {data.get('budget_usdc')}")
        print(f"requires_halt: {data.get('requires_halt')}")
        print(f"updated_at: {data.get('updated_at')}")
    else:
        print("runtime_control: missing")
    print()


def _print_wallet(conn: sqlite3.Connection) -> None:
    print("== Wallet ==")
    if _table_exists(conn, "pm_wallet"):
        row = conn.execute(
            "SELECT usdc_balance, synced_at FROM pm_wallet WHERE id=1"
        ).fetchone()
        if row:
            print(f"clob_pusd_balance: {float(row['usdc_balance'] or 0):.6f}")
            print(f"balance_synced_at: {row['synced_at']}")
        else:
            print("clob_pusd_balance: unknown (pm_wallet empty)")
    else:
        print("clob_pusd_balance: unknown (pm_wallet missing)")
    print()


def _print_positions(conn: sqlite3.Connection) -> None:
    print("== Positions ==")
    try:
        from agents.application.trade_log import TradeLog

        open_rows = TradeLog(str(DB_PATH)).filled_positions_with_id()
        print(f"journal_open_positions: {len(open_rows)}")
        if open_rows:
            for row in open_rows[:20]:
                print(
                    f"open id={row['id']} status={row['status']} "
                    f"size=${float(row['size_usdc'] or 0):.4f} "
                    f"market={row['market_id']} token={str(row['token_id'])[:18]}"
                )
    except Exception as exc:
        print(f"journal_open_positions: unavailable ({type(exc).__name__}: {exc})")

    if _table_exists(conn, "settlement_reconciliation"):
        print("settlement_reconciliation_raw:")
        rows = conn.execute(
            """
            SELECT status,
                   COUNT(*) AS n,
                   ROUND(COALESCE(SUM(cost_basis_usdc), 0), 4) AS cost,
                   ROUND(COALESCE(SUM(recoverable_usdc), 0), 4) AS recoverable
            FROM settlement_reconciliation
            GROUP BY status
            ORDER BY status
            """
        ).fetchall()
        if rows:
            for row in rows:
                print(
                    f"{row['status']}: n={row['n']} "
                    f"cost=${row['cost']:.4f} recoverable=${row['recoverable']:.4f}"
                )
        else:
            print("none")
    else:
        print("settlement_reconciliation: missing")
    print()


def _print_recent_trades(conn: sqlite3.Connection) -> None:
    print("== Recent Trades ==")
    if not _table_exists(conn, "trades"):
        print("trades: missing")
        return
    rows = conn.execute(
        """
        SELECT id, ts, market_id, side, price, size_usdc, status
        FROM trades
        ORDER BY id DESC
        LIMIT 10
        """
    ).fetchall()
    for row in rows:
        print(
            f"{row['id']} {row['ts']} {row['status']} "
            f"{row['side'] or ''} ${float(row['size_usdc'] or 0):.4f} "
            f"@ {float(row['price'] or 0):.4f} market={row['market_id']}"
        )
    print()


def _print_heartbeats() -> None:
    print("== Heartbeats ==")
    for path in sorted(DATA.glob("*_heartbeat")):
        print(f"{path.name}: {_age_label(path)}")
    print()


def main() -> int:
    print(f"poly1 ops snapshot generated_utc={datetime.now(timezone.utc).isoformat()}")
    print(f"db={DB_PATH}")
    print()
    _print_runtime()
    _print_heartbeats()
    if not DB_PATH.exists():
        print("trade_log.db: missing")
        return 1
    with _connect() as conn:
        _print_wallet(conn)
        _print_positions(conn)
        _print_recent_trades(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
