#!/usr/bin/env python3
"""Hourly Telegram dashboard for poly1.

Read-only except for Telegram delivery. The report is deliberately batched so
operators get a broad live picture once per hour instead of noisy minute spam.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
DB_PATH = Path(os.getenv("TRADE_LOG_DB", DATA / "trade_log.db"))
RUNTIME_CONTROL = DATA / "runtime_control.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.application.trading_policy import AGENT_MANIFEST, TELEGRAM_REPORT_SECONDS
from agents.utils.notify import notify_telegram


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _age(path: Path) -> str:
    if not path.exists():
        return "missing"
    seconds = max(0.0, time.time() - path.stat().st_mtime)
    if seconds < 120:
        return f"{seconds:.0f}s"
    if seconds < 7200:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _runtime_line() -> str:
    if not RUNTIME_CONTROL.exists():
        return "runtime: missing"
    try:
        data = json.loads(RUNTIME_CONTROL.read_text())
    except Exception as exc:
        return f"runtime: unreadable ({type(exc).__name__})"
    allowed = ",".join(data.get("allowed_live_agents") or []) or "none"
    return f"runtime: {data.get('mode')} | live={allowed} | budget=${float(data.get('budget_usdc') or 0):.2f}"


def _wallet_line(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute(
            "SELECT usdc_balance, synced_at FROM pm_wallet WHERE id=1"
        ).fetchone()
    except sqlite3.Error:
        return "wallet: unknown"
    if not row:
        return "wallet: unknown"
    return f"wallet: ${float(row['usdc_balance'] or 0):.2f} | synced={row['synced_at']}"


def _status_counts(conn: sqlite3.Connection, since: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS n, ROUND(COALESCE(SUM(size_usdc), 0), 2) AS usd
        FROM trades
        WHERE ts >= ?
        GROUP BY status
        ORDER BY n DESC
        LIMIT 8
        """,
        (since,),
    ).fetchall()
    return [f"{r['status']}={r['n']} (${float(r['usd'] or 0):.2f})" for r in rows]


def _agent_activity(conn: sqlite3.Connection, since: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT agent,
               SUM(CASE WHEN approved=1 THEN 1 ELSE 0 END) AS approved,
               COUNT(*) AS total,
               ROUND(AVG(score), 3) AS avg_score
        FROM brain_decisions
        WHERE ts >= ?
        GROUP BY agent
        ORDER BY total DESC
        LIMIT 8
        """,
        (since,),
    ).fetchall()
    return [
        f"{r['agent']}: {int(r['approved'] or 0)}/{int(r['total'] or 0)} avg={float(r['avg_score'] or 0):.3f}"
        for r in rows
    ]


def _open_positions(conn: sqlite3.Connection) -> tuple[int, float]:
    terminal = (
        "closed_take_profit", "closed_stop_loss", "closed_timeout",
        "closed_dust", "resolved_yes", "resolved_no", "resolved_loss",
    )
    open_statuses = (
        "filled", "btc_daily_open", "near_resolution_open",
        "news_shock_open", "wallet_follow_open", "btc_5min_open",
    )
    ph_t = ",".join("?" for _ in terminal)
    ph_o = ",".join("?" for _ in open_statuses)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n, ROUND(COALESCE(SUM(t.size_usdc), 0), 2) AS usd
        FROM trades t
        LEFT JOIN (
          SELECT token_id, MAX(id) AS terminal_id
          FROM trades
          WHERE status IN ({ph_t}) AND token_id IS NOT NULL AND token_id != ''
          GROUP BY token_id
        ) x ON x.token_id = t.token_id
        WHERE t.status IN ({ph_o})
          AND t.token_id IS NOT NULL AND t.token_id != ''
          AND t.id > COALESCE(x.terminal_id, 0)
        """,
        (*terminal, *open_statuses),
    ).fetchone()
    return int(row["n"] or 0), float(row["usd"] or 0.0)


def _heartbeat_lines() -> list[str]:
    names = sorted(p.name for p in DATA.glob("*_heartbeat"))
    return [f"{name.replace('_heartbeat', '')}: {_age(DATA / name)}" for name in names[:10]]


def build_report() -> str:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=1)).isoformat()
    lines = [
        f"poly1 hourly dashboard | {now.strftime('%Y-%m-%d %H:%M UTC')}",
        _runtime_line(),
    ]
    if not DB_PATH.exists():
        lines.append(f"db: missing ({DB_PATH})")
        return "\n".join(lines)

    with _connect() as conn:
        lines.append(_wallet_line(conn))
        open_n, open_usd = _open_positions(conn)
        lines.append(f"open positions: {open_n} | capital=${open_usd:.2f}")
        counts = _status_counts(conn, since)
        lines.append("last hour trades: " + ("; ".join(counts) if counts else "none"))
        agents = _agent_activity(conn, since)
        lines.append("brain activity: " + ("; ".join(agents) if agents else "none"))

    heartbeats = _heartbeat_lines()
    lines.append("heartbeats: " + ("; ".join(heartbeats) if heartbeats else "none"))
    live_agents = [
        name for name, meta in AGENT_MANIFEST.items()
        if meta["places_orders"] in {"yes", "sell only"}
    ]
    lines.append("live-capable agents: " + ", ".join(live_agents))
    lines.append("commands: /status /positions /agents /risk /pnl /halt")
    return "\n".join(lines)[:3900]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--print", action="store_true", dest="print_only")
    args = parser.parse_args()

    interval = int(os.getenv("TELEGRAM_REPORT_SECONDS", str(TELEGRAM_REPORT_SECONDS)))
    send_on_start = os.getenv(
        "TELEGRAM_REPORT_SEND_ON_START", "false"
    ).lower() in {"1", "true", "yes", "on"}
    while True:
        if args.daemon and not send_on_start:
            time.sleep(max(60, interval))
            send_on_start = True
        report = build_report()
        if args.print_only:
            print(report)
        else:
            notify_telegram(report, blocking=True, force=True)
        if not args.daemon:
            return 0
        time.sleep(max(60, interval))


if __name__ == "__main__":
    raise SystemExit(main())
