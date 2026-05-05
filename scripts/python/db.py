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
