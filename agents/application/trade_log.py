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

CREATE TABLE IF NOT EXISTS news_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    headline TEXT NOT NULL,
    source TEXT,
    url TEXT,
    market_id TEXT NOT NULL,
    market_question TEXT,
    direction TEXT NOT NULL,
    materiality REAL NOT NULL,
    relevance_score REAL NOT NULL,
    latency_ms INTEGER,
    model TEXT,
    status TEXT NOT NULL,
    reasoning TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_signals_ts ON news_signals(ts);
CREATE INDEX IF NOT EXISTS idx_news_signals_market_ts ON news_signals(market_id, ts);
CREATE INDEX IF NOT EXISTS idx_news_signals_status_ts ON news_signals(status, ts);

CREATE TABLE IF NOT EXISTS brain_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    agent TEXT NOT NULL,
    strategy TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT,
    approved INTEGER NOT NULL,
    reason TEXT NOT NULL,
    score REAL NOT NULL,
    market_type TEXT,
    asset TEXT,
    features_json TEXT,
    action TEXT,
    outcome_status TEXT,
    outcome_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_brain_decisions_ts ON brain_decisions(ts);
CREATE INDEX IF NOT EXISTS idx_brain_decisions_agent_ts ON brain_decisions(agent, ts);
CREATE INDEX IF NOT EXISTS idx_brain_decisions_market_ts ON brain_decisions(market_id, ts);
CREATE INDEX IF NOT EXISTS idx_brain_decisions_reason_ts ON brain_decisions(reason, ts);

CREATE TABLE IF NOT EXISTS decision_reflections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    decision_id INTEGER,
    agent TEXT NOT NULL,
    strategy TEXT NOT NULL,
    market_id TEXT NOT NULL,
    lesson_type TEXT NOT NULL,
    lesson TEXT NOT NULL,
    outcome_status TEXT,
    metrics_json TEXT,
    FOREIGN KEY(decision_id) REFERENCES brain_decisions(id)
);
CREATE INDEX IF NOT EXISTS idx_decision_reflections_ts ON decision_reflections(ts);
CREATE INDEX IF NOT EXISTS idx_decision_reflections_agent_ts ON decision_reflections(agent, ts);
CREATE INDEX IF NOT EXISTS idx_decision_reflections_market_ts ON decision_reflections(market_id, ts);

CREATE TABLE IF NOT EXISTS wallet_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    wallet_profit_usdc REAL,
    wallet_trades_30d INTEGER,
    market_id TEXT NOT NULL,
    market_question TEXT,
    direction TEXT NOT NULL,
    token_id TEXT,
    yes_price REAL,
    wallet_entry_price REAL,
    wallet_size_usdc REAL,
    status TEXT NOT NULL DEFAULT 'fresh'
);
CREATE INDEX IF NOT EXISTS idx_wallet_signals_ts ON wallet_signals(ts);
CREATE INDEX IF NOT EXISTS idx_wallet_signals_market ON wallet_signals(market_id, ts);
CREATE INDEX IF NOT EXISTS idx_wallet_signals_status_ts ON wallet_signals(status, ts);
CREATE INDEX IF NOT EXISTS idx_wallet_signals_wallet_ts ON wallet_signals(wallet_address, ts);

CREATE TABLE IF NOT EXISTS position_marks (
    token_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    first_seen_ts TEXT NOT NULL,
    last_seen_ts TEXT NOT NULL,
    entry_price REAL NOT NULL,
    current_price REAL NOT NULL,
    max_price REAL NOT NULL,
    min_price REAL NOT NULL,
    mfe_pct REAL NOT NULL,
    mae_pct REAL NOT NULL,
    peak_drawdown_pct REAL NOT NULL,
    shares REAL,
    status TEXT NOT NULL,
    notes_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_position_marks_market ON position_marks(market_id);
CREATE INDEX IF NOT EXISTS idx_position_marks_status_ts ON position_marks(status, last_seen_ts);

CREATE TABLE IF NOT EXISTS market_quarantine (
    market_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    first_seen_ts TEXT NOT NULL,
    last_seen_ts TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_market_quarantine_ts ON market_quarantine(last_seen_ts);

CREATE TABLE IF NOT EXISTS agent_promotion_ledger (
    agent TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    reason TEXT NOT NULL,
    score REAL,
    expected_value REAL,
    realized_pnl_usdc REAL,
    sample_size INTEGER NOT NULL DEFAULT 0,
    updated_ts TEXT NOT NULL,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_promotion_state ON agent_promotion_ledger(state, updated_ts);
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
SCALPER_EXIT = "scalper_exit"
BTC_DAILY_OPEN = "btc_daily_open"
NEAR_RESOLUTION_OPEN = "near_resolution_open"
NEWS_SHOCK_OPEN = "news_shock_open"
WALLET_FOLLOW_OPEN = "wallet_follow_open"
# Resolution-sync statuses (added 2026-05-08): written when a Polymarket
# market resolves and on-chain CTF balance hits dust on a token we held.
# Realized P&L is recorded in `size_usdc` as the payout (shares × $1 if won,
# 0 if lost). Used by allocator to feed actual outcomes into agent scoring.
RESOLVED_YES = "resolved_yes"
RESOLVED_NO = "resolved_no"
RESOLVED_LOSS = "resolved_loss"  # token resolved against us, payout = 0

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

    def has_filled_position_for_market(self, market_id: str) -> bool:
        """Return True if there is a FILLED row with no subsequent terminal
        close row. A terminal row (closed_*, resolved_*) written after the
        last FILLED row means the position has been exited, so re-entry is
        allowed. This prevents the old 'block forever on any historical fill'
        behaviour that left stale filled rows blocking markets indefinitely
        after position_manager had already closed them.
        """
        _TERMINAL = (
            "closed_take_profit", "closed_stop_loss", "closed_timeout",
            "closed_dust", "resolved_yes", "resolved_no", "resolved_loss",
        )
        terminal_ph = ",".join("?" * len(_TERMINAL))
        sql = f"""
            SELECT 1 FROM trades
            WHERE market_id = ? AND status = 'filled'
              AND id > COALESCE(
                (SELECT MAX(id) FROM trades
                 WHERE market_id = ? AND status IN ({terminal_ph})), 0
              )
            LIMIT 1
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                sql, (str(market_id), str(market_id), *_TERMINAL)
            ).fetchone()
            return row is not None

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

    def count_recent_failures_for_market(
        self,
        market_id: str,
        hours: int = 6,
        error_like: Optional[list[str]] = None,
    ) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        params: list = [str(market_id), FAILED, cutoff]
        sql = "SELECT COUNT(*) AS n FROM trades WHERE market_id = ? AND status = ? AND ts >= ?"
        if error_like:
            clauses = []
            for pattern in error_like:
                clauses.append("error LIKE ?")
                params.append(pattern)
            sql += " AND (" + " OR ".join(clauses) + ")"
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return int(row["n"])

    def quarantine_market(self, market_id: str, reason: str) -> None:
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO market_quarantine
                    (market_id, reason, first_seen_ts, last_seen_ts, count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(market_id) DO UPDATE SET
                    reason=excluded.reason,
                    last_seen_ts=excluded.last_seen_ts,
                    count=market_quarantine.count + 1
                """,
                (str(market_id), reason, now, now),
            )

    def is_market_quarantined(self, market_id: str, max_age_hours: int = 24) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM market_quarantine
                WHERE market_id = ? AND last_seen_ts >= ?
                LIMIT 1
                """,
                (str(market_id), cutoff),
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

    def filled_positions_with_id(self) -> list:
        """Like filled_positions() but includes id, ts, and response_json.
        Used by position_manager to aggregate fills + read per-position
        overrides (e.g. tp_pct_override on manual entries)."""
        open_statuses = (FILLED, BTC_DAILY_OPEN, NEAR_RESOLUTION_OPEN, NEWS_SHOCK_OPEN, WALLET_FOLLOW_OPEN)
        placeholders = ",".join("?" for _ in open_statuses)
        sql = (
            "SELECT id, ts, market_id, token_id, side, price, size_usdc, "
            "status, response_json "
            f"FROM trades WHERE status IN ({placeholders}) AND token_id IS NOT NULL "
            "AND token_id != '' "
            "AND (error IS NULL OR error NOT LIKE 'SHADOW%') "
            "ORDER BY id"
        )
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, open_statuses).fetchall()
            return [dict(r) for r in rows]

    def has_close_attempt_for_token(self, token_id: str) -> bool:
        """Return True if a LIVE successful closed_* row exists for this
        token. Used by position_manager to avoid double-closing the same
        position across daemon cycles or restarts.

        Shadow rows (error LIKE 'SHADOW%') are excluded — paper decisions
        shouldn't block a real execution after the operator flips to live.

        `close_failed` rows are also excluded — those are FAILED close
        attempts (allowance issue, balance issue, etc.) and the engine
        should retry on the next cycle. The retry rate is bounded by
        `MAINTAIN_POLL_SEC` (60s default).
        """
        sql = (
            "SELECT 1 FROM trades WHERE token_id = ? "
            "AND status IN ('closed_take_profit','closed_stop_loss',"
            "'closed_timeout','closed_dust',"
            "'resolved_yes','resolved_no','resolved_loss') "
            "AND (error IS NULL OR error NOT LIKE 'SHADOW%') "
            "LIMIT 1"
        )
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, (str(token_id),)).fetchone()
            return row is not None

    def has_resolved_marker_for_token(self, token_id: str) -> bool:
        """Return True if the token has a resolved_* status row.

        Used by position_manager._already_closed to suppress the dust-override
        for markets with no CLOB orderbook (resolved/delisted). On-chain tokens
        for such markets must be redeemed via the CTF contract, not sold via CLOB.
        """
        sql = (
            "SELECT 1 FROM trades WHERE token_id = ? "
            "AND status IN ('resolved_yes','resolved_no','resolved_loss') "
            "LIMIT 1"
        )
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, (str(token_id),)).fetchone()
            return row is not None

    def has_dust_close_for_token(self, token_id: str) -> bool:
        """Return True if the most-recent terminal close for this token was
        closed_dust.

        Used by position_manager._already_closed to suppress the dust-override
        for positions already evaluated as sub-minimum notional.  Retrying a
        dust close just produces another closed_dust row indefinitely.
        """
        sql = (
            "SELECT status FROM trades WHERE token_id = ? "
            "AND status IN ('closed_take_profit','closed_stop_loss',"
            "'closed_timeout','closed_dust',"
            "'resolved_yes','resolved_no','resolved_loss') "
            "AND (error IS NULL OR error NOT LIKE 'SHADOW%') "
            "ORDER BY id DESC LIMIT 1"
        )
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, (str(token_id),)).fetchone()
            return row is not None and row[0] == "closed_dust"

    def count_close_failed_for_token(self, token_id: str) -> int:
        """Count close_failed rows for this token.

        Used by position_manager to detect permanently-stuck positions: if a
        FAK sell keeps bouncing (e.g., illiquid market, 400 'no orders found
        to match with FAK order') beyond MAINTAIN_MAX_CLOSE_FAILURES cycles,
        escalate to resolved_loss so the retry loop stops.
        """
        sql = (
            "SELECT COUNT(*) FROM trades WHERE token_id = ? "
            "AND status = 'close_failed'"
        )
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, (str(token_id),)).fetchone()
            return row[0] if row else 0

    def upsert_position_mark(
        self,
        *,
        token_id: str,
        market_id: str,
        entry_price: float,
        current_price: float,
        shares: Optional[float] = None,
        status: str = "open",
        notes: Optional[dict] = None,
    ) -> dict:
        """Persist MFE/MAE state for a position and return the updated row."""
        now = _now()
        token_id = str(token_id)
        entry = float(entry_price)
        current = float(current_price)
        notes_json = json.dumps(notes, default=str) if notes is not None else None
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM position_marks WHERE token_id = ?",
                (token_id,),
            ).fetchone()
            if existing:
                max_price = max(float(existing["max_price"]), current)
                min_price = min(float(existing["min_price"]), current)
                first_seen = existing["first_seen_ts"]
            else:
                max_price = current
                min_price = current
                first_seen = now
            mfe = ((max_price - entry) / entry) if entry > 0 else 0.0
            mae = ((min_price - entry) / entry) if entry > 0 else 0.0
            peak_dd = ((max_price - current) / max_price) if max_price > 0 else 0.0
            conn.execute(
                """
                INSERT INTO position_marks
                    (token_id, market_id, first_seen_ts, last_seen_ts,
                     entry_price, current_price, max_price, min_price,
                     mfe_pct, mae_pct, peak_drawdown_pct, shares, status,
                     notes_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(token_id) DO UPDATE SET
                    market_id=excluded.market_id,
                    last_seen_ts=excluded.last_seen_ts,
                    entry_price=excluded.entry_price,
                    current_price=excluded.current_price,
                    max_price=excluded.max_price,
                    min_price=excluded.min_price,
                    mfe_pct=excluded.mfe_pct,
                    mae_pct=excluded.mae_pct,
                    peak_drawdown_pct=excluded.peak_drawdown_pct,
                    shares=excluded.shares,
                    status=excluded.status,
                    notes_json=COALESCE(excluded.notes_json, position_marks.notes_json)
                """,
                (
                    token_id,
                    str(market_id),
                    first_seen,
                    now,
                    entry,
                    current,
                    max_price,
                    min_price,
                    mfe,
                    mae,
                    peak_dd,
                    shares,
                    status,
                    notes_json,
                ),
            )
            row = conn.execute(
                "SELECT * FROM position_marks WHERE token_id = ?",
                (token_id,),
            ).fetchone()
            return dict(row)

    def mark_position_closed(self, token_id: str, status: str = "closed") -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE position_marks SET status=?, last_seen_ts=? WHERE token_id=?",
                (status, _now(), str(token_id)),
            )

    def upsert_agent_promotion(
        self,
        *,
        agent: str,
        state: str,
        reason: str,
        score: Optional[float] = None,
        expected_value: Optional[float] = None,
        realized_pnl_usdc: Optional[float] = None,
        sample_size: int = 0,
        metadata: Optional[dict] = None,
    ) -> None:
        """Persist the latest promotion state for a trading/research agent."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_promotion_ledger
                    (agent, state, reason, score, expected_value,
                     realized_pnl_usdc, sample_size, updated_ts, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent) DO UPDATE SET
                    state=excluded.state,
                    reason=excluded.reason,
                    score=excluded.score,
                    expected_value=excluded.expected_value,
                    realized_pnl_usdc=excluded.realized_pnl_usdc,
                    sample_size=excluded.sample_size,
                    updated_ts=excluded.updated_ts,
                    metadata_json=excluded.metadata_json
                """,
                (
                    agent,
                    state,
                    reason,
                    score,
                    expected_value,
                    realized_pnl_usdc,
                    int(sample_size),
                    _now(),
                    json.dumps(metadata, default=str) if metadata is not None else None,
                ),
            )

    def filled_positions(self) -> list:
        """Return one row per filled trade with token_id present.

        Each row is a dict with market_id, token_id, side, price, size_usdc —
        the fields needed to mark the position to market. There is currently
        no closing flow (maintain_positions is a stub), so every filled row
        is treated as an open position.

        Note: scalper legs use status='scalper_leg', not 'filled', so they
        are NOT included here. Once the scalper goes live, mark-to-market
        of scalper positions must be added separately (e.g., from
        `scalper_pairs`) or scalper-deployed cash will read as drawdown.
        """
        sql = (
            "SELECT market_id, token_id, side, price, size_usdc "
            "FROM trades WHERE status = ? AND token_id IS NOT NULL "
            "AND token_id != '' "
            "ORDER BY id"
        )
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, (FILLED,)).fetchall()
            return [dict(r) for r in rows]

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

    def insert_news_signal(
        self,
        headline: str,
        market_id: str,
        direction: str,
        materiality: float,
        relevance_score: float,
        status: str,
        source: Optional[str] = None,
        url: Optional[str] = None,
        market_question: Optional[str] = None,
        latency_ms: Optional[int] = None,
        model: Optional[str] = None,
        reasoning: Optional[str] = None,
    ) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO news_signals (ts, headline, source, url, market_id, "
                "market_question, direction, materiality, relevance_score, "
                "latency_ms, model, status, reasoning) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _now(),
                    headline,
                    source,
                    url,
                    str(market_id),
                    market_question,
                    direction,
                    float(materiality),
                    float(relevance_score),
                    latency_ms,
                    model,
                    status,
                    reasoning,
                ),
            )
            return cur.lastrowid

    def recent_news_signals(self, limit: int = 50) -> list:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM news_signals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def insert_brain_decision(
        self,
        agent: str,
        strategy: str,
        decision_type: str,
        market_id: str,
        approved: bool,
        reason: str,
        score: float,
        token_id: Optional[str] = None,
        market_type: Optional[str] = None,
        asset: Optional[str] = None,
        features: Optional[dict] = None,
        action: Optional[str] = None,
    ) -> int:
        features_json = json.dumps(features, default=str) if features is not None else None
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO brain_decisions (ts, agent, strategy, decision_type, "
                "market_id, token_id, approved, reason, score, market_type, asset, "
                "features_json, action) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _now(),
                    agent,
                    strategy,
                    decision_type,
                    str(market_id),
                    token_id,
                    1 if approved else 0,
                    reason,
                    float(score),
                    market_type,
                    asset,
                    features_json,
                    action,
                ),
            )
            return cur.lastrowid

    def recent_brain_decisions(self, limit: int = 50) -> list:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM brain_decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def update_brain_decision_outcome(
        self,
        decision_id: int,
        outcome_status: str,
        outcome: Optional[dict] = None,
    ) -> None:
        outcome_json = json.dumps(outcome, default=str) if outcome is not None else None
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE brain_decisions SET outcome_status = ?, outcome_json = ? "
                "WHERE id = ?",
                (outcome_status, outcome_json, int(decision_id)),
            )

    def insert_decision_reflection(
        self,
        agent: str,
        strategy: str,
        market_id: str,
        lesson_type: str,
        lesson: str,
        decision_id: Optional[int] = None,
        outcome_status: Optional[str] = None,
        metrics: Optional[dict] = None,
    ) -> int:
        metrics_json = json.dumps(metrics, default=str) if metrics is not None else None
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO decision_reflections (ts, decision_id, agent, strategy, "
                "market_id, lesson_type, lesson, outcome_status, metrics_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    _now(),
                    int(decision_id) if decision_id is not None else None,
                    agent,
                    strategy,
                    str(market_id),
                    lesson_type,
                    lesson,
                    outcome_status,
                    metrics_json,
                ),
            )
            return cur.lastrowid

    def recent_decision_reflections(self, limit: int = 50) -> list:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM decision_reflections ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
