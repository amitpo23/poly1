import json
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT,
    side TEXT,
    price REAL,
    size_usdc REAL,
    confidence REAL,
    status TEXT NOT NULL,
    response_json TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_status_ts ON trades(market_id, status, ts);
CREATE INDEX IF NOT EXISTS idx_status_ts ON trades(status, ts);

CREATE TABLE IF NOT EXISTS scalper_pairs (
    slug TEXT PRIMARY KEY,
    period_ts INTEGER NOT NULL,
    up_token TEXT NOT NULL,
    down_token TEXT NOT NULL,
    qty_up REAL NOT NULL DEFAULT 0,
    qty_down REAL NOT NULL DEFAULT 0,
    cost_up REAL NOT NULL DEFAULT 0,
    cost_down REAL NOT NULL DEFAULT 0,
    attempts_up INTEGER NOT NULL DEFAULT 0,
    attempts_down INTEGER NOT NULL DEFAULT 0,
    last_price_up REAL,
    last_price_down REAL,
    state TEXT NOT NULL,
    opened_ts INTEGER NOT NULL,
    closed_ts INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_scalper_state ON scalper_pairs(state);
CREATE INDEX IF NOT EXISTS idx_scalper_period ON scalper_pairs(period_ts);
"""


PENDING = "pending"
SUBMITTED = "submitted"
FILLED = "filled"
FAILED = "failed"
MAY_HAVE_FIRED = "may_have_fired"  # crashed mid-execute; needs manual review
SKIPPED_DEDUPE = "skipped_dedupe"
SKIPPED_GATE = "skipped_gate"
SKIPPED_DRY_RUN = "skipped_dry_run"
SCALPER_LEG = "scalper_leg"

# Statuses that block re-trading the same market within the dedupe window.
TIME_BOUNDED_ACTIVE_STATUSES = (PENDING, SUBMITTED, FILLED)
# MAY_HAVE_FIRED blocks unconditionally — the order may have actually executed
# on the exchange even though we never recorded the response; re-submitting
# could double-fill. Operator must verify on-chain and clear the row manually.
UNBOUNDED_BLOCKING_STATUSES = (MAY_HAVE_FIRED,)
ACTIVE_STATUSES = TIME_BOUNDED_ACTIVE_STATUSES + UNBOUNDED_BLOCKING_STATUSES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TradeLog:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.getenv("TRADE_LOG_DB", "./data/trade_log.db")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
        self.recover_stranded_pendings()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def new_cycle_id(self) -> str:
        return str(uuid.uuid4())

    def has_active_trade_for_market(self, market_id: str, hours: int = 6) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        time_bounded_placeholders = ",".join("?" for _ in TIME_BOUNDED_ACTIVE_STATUSES)
        unbounded_placeholders = ",".join("?" for _ in UNBOUNDED_BLOCKING_STATUSES)
        # MAY_HAVE_FIRED rows block forever (operator must clear manually);
        # other active statuses block only within the dedupe window.
        sql = (
            f"SELECT 1 FROM trades WHERE market_id = ? AND ("
            f"  (status IN ({time_bounded_placeholders}) AND ts >= ?)"
            f"  OR status IN ({unbounded_placeholders})"
            f") LIMIT 1"
        )
        with self._lock, self._connect() as conn:
            row = conn.execute(
                sql,
                (
                    str(market_id),
                    *TIME_BOUNDED_ACTIVE_STATUSES,
                    cutoff,
                    *UNBOUNDED_BLOCKING_STATUSES,
                ),
            ).fetchone()
            return row is not None

    def insert_pending(
        self,
        cycle_id: str,
        market_id: str,
        token_id: Optional[str],
        side: Optional[str],
        price: Optional[float],
        size_usdc: Optional[float],
        confidence: Optional[float],
    ) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO trades (ts, cycle_id, market_id, token_id, side, price, "
                "size_usdc, confidence, status) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    _now(),
                    cycle_id,
                    str(market_id),
                    token_id,
                    side,
                    price,
                    size_usdc,
                    confidence,
                    PENDING,
                ),
            )
            return cur.lastrowid

    def mark(
        self,
        trade_id: int,
        status: str,
        response: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> None:
        response_json = json.dumps(response, default=str) if response is not None else None
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE trades SET status = ?, response_json = ?, error = ?, ts = ? "
                "WHERE id = ?",
                (status, response_json, error, _now(), trade_id),
            )

    def insert_terminal(
        self,
        cycle_id: str,
        market_id: str,
        status: str,
        token_id: Optional[str] = None,
        side: Optional[str] = None,
        price: Optional[float] = None,
        size_usdc: Optional[float] = None,
        confidence: Optional[float] = None,
        response: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> int:
        response_json = json.dumps(response, default=str) if response is not None else None
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO trades (ts, cycle_id, market_id, token_id, side, price, "
                "size_usdc, confidence, status, response_json, error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _now(),
                    cycle_id,
                    str(market_id),
                    token_id,
                    side,
                    price,
                    size_usdc,
                    confidence,
                    status,
                    response_json,
                    error,
                ),
            )
            return cur.lastrowid

    def count_recent(self, status, hours: int = 1) -> int:
        """Count rows whose status matches `status` (str or iterable) within the window."""
        statuses = [status] if isinstance(status, str) else list(status)
        if not statuses:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        placeholders = ",".join("?" for _ in statuses)
        sql = (
            f"SELECT COUNT(*) AS n FROM trades WHERE status IN ({placeholders}) "
            f"AND ts >= ?"
        )
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, (*statuses, cutoff)).fetchone()
            return int(row["n"])

    def recover_stranded_pendings(self, older_than_minutes: int = 10) -> int:
        """Mark old pending rows as MAY_HAVE_FIRED — the order may have executed
        on the exchange even though we never recorded the response. Operator
        must verify against on-chain state before re-trading these markets."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
        ).isoformat()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE trades SET status = ?, error = ? WHERE status = ? AND ts < ?",
                (
                    MAY_HAVE_FIRED,
                    "stranded_pending_at_startup_needs_manual_verification",
                    PENDING,
                    cutoff,
                ),
            )
            n = cur.rowcount
        if n:
            logger.warning(
                "trade_log: %d stranded pending rows flagged MAY_HAVE_FIRED — "
                "verify on-chain before clearing",
                n,
            )
        return n

    def recent(self, limit: int = 20) -> list:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
