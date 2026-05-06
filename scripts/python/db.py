"""Read-only data access for the dashboard.

All functions return plain dicts/lists. Callers never see sqlite3 Row objects.
scalper_pairs queries return [] / {} gracefully if the table does not exist yet.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_DB = os.getenv("TRADE_LOG_DB", "./data/trade_log.db")
_LLM_FILE = os.getenv("LLM_USAGE_FILE", "./data/llm_usage.jsonl")
_LOG_DIR = os.getenv("LOG_DIR", "./data/logs")
_HALT_FILE = Path(os.getenv("KILL_SWITCH_FILE", "./data/HALT"))
_HB_TRADER = Path(os.getenv("HEARTBEAT_PATH", "./data/heartbeat"))
_HB_SCALPER = Path("./data/scalper_heartbeat")
_SWARM_DB = Path(os.path.expanduser(os.getenv(
    "SWARM_DB", "~/Desktop/poly/bot/data/swarm.db"
)))


def _conn() -> sqlite3.Connection:
    uri = f"file:{_DB}?mode=ro"
    c = sqlite3.connect(uri, uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with contextlib.closing(_conn()) as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def _scalar(sql: str, params: tuple = (), default: Any = None) -> Any:
    with contextlib.closing(_conn()) as c:
        row = c.execute(sql, params).fetchone()
        return row[0] if row else default


def _table_exists(name: str) -> bool:
    return bool(_scalar(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ))


def _swarm_conn() -> sqlite3.Connection:
    uri = f"file:{_SWARM_DB}?mode=ro&immutable=1"
    c = sqlite3.connect(uri, uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _swarm_rows(sql: str, params: tuple = ()) -> list[dict]:
    if not _SWARM_DB.exists():
        return []
    with contextlib.closing(_swarm_conn()) as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


# ── heartbeats ────────────────────────────────────────────────────────────────

def heartbeat_age(path: Path) -> float | None:
    """Seconds since last heartbeat write, or None if file missing."""
    try:
        mtime = path.stat().st_mtime
        return datetime.now(timezone.utc).timestamp() - mtime
    except FileNotFoundError:
        return None


def trader_heartbeat_age() -> float | None:
    return heartbeat_age(_HB_TRADER)


def scalper_heartbeat_age() -> float | None:
    return heartbeat_age(_HB_SCALPER)


def is_halted() -> bool:
    return _HALT_FILE.exists()


def halt() -> None:
    _HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HALT_FILE.write_text("halted by dashboard")


def resume() -> None:
    if _HALT_FILE.exists():
        _HALT_FILE.unlink()


# ── trades ────────────────────────────────────────────────────────────────────

def trades_all() -> list[dict]:
    return _rows("SELECT * FROM trades ORDER BY ts DESC")


def trades_by_status(status: str) -> list[dict]:
    return _rows("SELECT * FROM trades WHERE status=? ORDER BY ts DESC", (status,))


# Status strings below are intentionally hardcoded to keep db.py free of
# application imports. Keep in sync with trade_log.py constants.
def trades_filled() -> list[dict]:
    return _rows("SELECT * FROM trades WHERE status='filled' ORDER BY ts")


def last_gate_reason() -> str | None:
    row = _rows(
        "SELECT error FROM trades WHERE status='skipped_gate' ORDER BY ts DESC LIMIT 1"
    )
    return row[0]["error"] if row else None


def daily_capital_deployed() -> list[dict]:
    """Returns [{date, total_usdc}] for filled trades, ordered by date."""
    return _rows(
        "SELECT date(ts) AS day, SUM(size_usdc) AS total_usdc "
        "FROM trades WHERE status='filled' GROUP BY day ORDER BY day"
    )


def trade_status_counts() -> dict[str, int]:
    rows = _rows("SELECT status, COUNT(*) AS n FROM trades GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


def open_positions() -> list[dict]:
    """All filled trades, newest first — capital at risk is approximate until settlement tracking is added."""
    return _rows(
        "SELECT * FROM trades WHERE status='filled' ORDER BY ts DESC"
    )


# ── scalper pairs ─────────────────────────────────────────────────────────────

def scalper_pairs_open() -> list[dict]:
    if not _table_exists("scalper_pairs"):
        return []
    return _rows(
        "SELECT * FROM scalper_pairs WHERE state NOT IN ('expired','redeemed','shadow') "
        "ORDER BY opened_ts DESC"
    )


def scalper_pairs_recent(limit: int = 50) -> list[dict]:
    if not _table_exists("scalper_pairs"):
        return []
    return _rows(
        "SELECT * FROM scalper_pairs ORDER BY opened_ts DESC LIMIT ?", (limit,)
    )


def scalper_state_counts() -> dict[str, int]:
    if not _table_exists("scalper_pairs"):
        return {}
    rows = _rows("SELECT state, COUNT(*) AS n FROM scalper_pairs GROUP BY state")
    return {r["state"]: r["n"] for r in rows}


# ── news classification signals ──────────────────────────────────────────────

def news_signals_recent(limit: int = 50) -> list[dict]:
    if not _table_exists("news_signals"):
        return []
    return _rows(
        "SELECT * FROM news_signals ORDER BY ts DESC LIMIT ?", (limit,)
    )


def news_signal_stats() -> dict[str, int]:
    if not _table_exists("news_signals"):
        return {}
    rows = _rows(
        "SELECT direction || ':' || status AS key, COUNT(*) AS n "
        "FROM news_signals GROUP BY direction, status"
    )
    return {r["key"]: r["n"] for r in rows}


# ── swarm read-only mirror ───────────────────────────────────────────────────

def swarm_db_present() -> bool:
    return _SWARM_DB.exists()


def swarm_pending_orders(limit: int = 50) -> list[dict]:
    return _swarm_rows(
        "SELECT id, agent, market_id, side, outcome, size_usd, price_cents, "
        "status, order_id, created_ms, updated_ms, note "
        "FROM pending_orders ORDER BY id DESC LIMIT ?",
        (limit,),
    )


def swarm_pending_by_status() -> dict[str, dict]:
    rows = _swarm_rows(
        "SELECT status, COUNT(*) AS n, ROUND(SUM(size_usd),2) AS size_usd "
        "FROM pending_orders GROUP BY status ORDER BY status"
    )
    return {
        r["status"]: {"count": int(r["n"] or 0), "size_usd": float(r["size_usd"] or 0)}
        for r in rows
    }


def swarm_agent_summary() -> list[dict]:
    """Per-agent operational and financial summary from swarm SQLite.

    `fills.price` is cents, so executed notional is computed as
    `(price / 100.0) * size` in USD.
    """
    return _swarm_rows(
        "WITH agents AS ("
        "  SELECT DISTINCT agent AS name FROM pending_orders "
        "  UNION "
        "  SELECT DISTINCT agent AS name FROM fills "
        "  UNION "
        "  SELECT DISTINCT agent AS name FROM pnl_events "
        "  UNION "
        "  SELECT DISTINCT agent AS name FROM agent_state"
        "), "
        "pending AS ("
        "  SELECT agent, "
        "         COUNT(*) AS total_rows, "
        "         SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_rows, "
        "         SUM(CASE WHEN status='submitted' THEN 1 ELSE 0 END) AS submitted_rows, "
        "         SUM(CASE WHEN status='filled' THEN 1 ELSE 0 END) AS filled_brake_rows, "
        "         SUM(CASE WHEN status='cleared' THEN 1 ELSE 0 END) AS cleared_rows, "
        "         ROUND(SUM(CASE WHEN status IN ('submitted','filled') THEN size_usd ELSE 0 END),2) AS blocked_usd "
        "  FROM pending_orders GROUP BY agent"
        "), "
        "f AS ("
        "  SELECT agent, "
        "         COUNT(*) AS fill_count, "
        "         ROUND(SUM((price / 100.0) * size), 2) AS executed_usd "
        "  FROM fills GROUP BY agent"
        "), "
        "p AS ("
        "  SELECT agent, "
        "         ROUND(SUM(pnl), 2) AS realized_pnl_usd, "
        "         COUNT(*) AS pnl_events "
        "  FROM pnl_events GROUP BY agent"
        ") "
        "SELECT a.name AS agent, "
        "       COALESCE(pending.total_rows, 0) AS ledger_rows, "
        "       COALESCE(pending.submitted_rows, 0) AS submitted_rows, "
        "       COALESCE(pending.filled_brake_rows, 0) AS filled_brake_rows, "
        "       COALESCE(pending.failed_rows, 0) AS failed_rows, "
        "       COALESCE(pending.cleared_rows, 0) AS cleared_rows, "
        "       COALESCE(f.fill_count, 0) AS fill_count, "
        "       COALESCE(f.executed_usd, 0.0) AS executed_usd, "
        "       COALESCE(pending.blocked_usd, 0.0) AS blocked_usd, "
        "       COALESCE(p.realized_pnl_usd, 0.0) AS realized_pnl_usd, "
        "       COALESCE(p.pnl_events, 0) AS pnl_events "
        "FROM agents a "
        "LEFT JOIN pending ON pending.agent = a.name "
        "LEFT JOIN f ON f.agent = a.name "
        "LEFT JOIN p ON p.agent = a.name "
        "ORDER BY a.name"
    )


def swarm_submitted_unreconciled() -> list[dict]:
    return _swarm_rows(
        "SELECT p.id, p.agent, p.market_id, p.side, p.outcome, p.size_usd, "
        "p.price_cents, p.order_id, p.updated_ms, p.note "
        "FROM pending_orders p "
        "LEFT JOIN fills f ON f.order_id = p.order_id "
        "WHERE p.status='submitted' AND f.order_id IS NULL "
        "AND COALESCE(p.order_id,'') NOT LIKE 'dry_%' "
        "ORDER BY p.updated_ms DESC"
    )


def swarm_recent_fills(limit: int = 30) -> list[dict]:
    return _swarm_rows(
        "SELECT ts_ms, agent, market_id, side, outcome, price, size, fee, order_id "
        "FROM fills ORDER BY id DESC LIMIT ?",
        (limit,),
    )


def swarm_nh_journal(limit: int = 20) -> list[dict]:
    return _swarm_rows(
        "SELECT id, agent, slug, question, no_price_quoted, no_price_filled, "
        "rejected_count, opened_at_ms, last_check_ms, unrealized_pnl "
        "FROM nh_journal ORDER BY id DESC LIMIT ?",
        (limit,),
    )


# ── LLM usage ─────────────────────────────────────────────────────────────────

def llm_records() -> list[dict]:
    path = Path(_LLM_FILE)
    if not path.exists():
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


# ── log tail ──────────────────────────────────────────────────────────────────

def log_tail(n: int = 100) -> str:
    log_path = Path(_LOG_DIR) / "poly1.log"
    if not log_path.exists():
        return "(log file not found)"
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 32_768)
            f.seek(max(0, size - chunk))
            text = f.read().decode("utf-8", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-n:])
    except OSError as e:
        return f"(error reading log: {e})"
