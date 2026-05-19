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
CREATE INDEX IF NOT EXISTS idx_token_id_status ON trades(token_id, status, ts);

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
    reasoning TEXT,
    yes_price REAL DEFAULT NULL
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
    outcome_json TEXT,
    signal_source TEXT
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
    wallet_winrate_external REAL,
    wallet_total_trades_external INTEGER,
    wallet_rank INTEGER,
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

CREATE TABLE IF NOT EXISTS settlement_reconciliation (
    token_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    status TEXT NOT NULL,
    action TEXT NOT NULL,
    updated_ts TEXT NOT NULL,
    latest_open_trade_id INTEGER,
    cost_basis_usdc REAL,
    journal_shares REAL,
    on_chain_shares REAL,
    best_bid REAL,
    best_ask REAL,
    recoverable_usdc REAL,
    redeemable_usdc REAL,
    gas_estimate_usdc REAL,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_settlement_status_ts ON settlement_reconciliation(status, updated_ts);
CREATE INDEX IF NOT EXISTS idx_settlement_market_ts ON settlement_reconciliation(market_id, updated_ts);

CREATE TABLE IF NOT EXISTS market_universe (
    slug TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    horizon TEXT NOT NULL,
    asset TEXT NOT NULL,
    period_ts INTEGER NOT NULL,
    market_id TEXT NOT NULL,
    question TEXT,
    liquidity_usdc REAL,
    volume_usdc REAL,
    yes_price REAL,
    no_price REAL,
    up_token TEXT,
    down_token TEXT,
    accepting_orders INTEGER NOT NULL DEFAULT 0,
    route_agent TEXT NOT NULL,
    score REAL NOT NULL,
    winrate_estimate REAL,
    eligible INTEGER NOT NULL DEFAULT 0,
    top_rank INTEGER,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_universe_ts ON market_universe(ts);
CREATE INDEX IF NOT EXISTS idx_market_universe_route_score ON market_universe(route_agent, score);
CREATE INDEX IF NOT EXISTS idx_market_universe_asset_horizon ON market_universe(asset, horizon, period_ts);
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
BTC_5MIN_OPEN = "btc_5min_open"
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


def _parse_json_obj(value: Optional[str]) -> dict:
    if not value:
        return {}
    try:
        payload = json.loads(value)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _utc_day_bounds(day: Optional[str] = None) -> tuple[str, str]:
    """Return ISO UTC day bounds for ledger queries."""
    if day:
        text = str(day).strip()
        if "T" in text:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
    else:
        dt = datetime.now(timezone.utc)
    start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _trade_agent_filter(agent: Optional[str]) -> tuple[str, list]:
    """Best-effort agent attribution for legacy trades rows.

    The trades table predates the agent column. Use only high-confidence
    status/market_id patterns so one agent's losses do not poison another
    agent's intraday win-rate.
    """
    if not agent:
        return "", []
    agent = agent.lower()
    if agent == "scalper":
        return (
            " AND (status LIKE 'scalper_%' OR market_id LIKE ?)",
            ["%-updown-15m-%"],
        )
    if agent == "btc_5min":
        return (
            " AND (status = ? OR market_id LIKE ?)",
            ["btc_5min_open", "%-updown-5m-%"],
        )
    status_by_agent = {
        "btc_daily": "btc_daily_open",
        "near_resolution": "near_resolution_open",
        "news_shock": "news_shock_open",
        "wallet_follow": "wallet_follow_open",
    }
    status = status_by_agent.get(agent)
    if status:
        return " AND status = ?", [status]
    return " AND 1 = 0", []


class TradeLog:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.getenv("TRADE_LOG_DB", "./data/trade_log.db")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # Migrations — safe to re-run (SQLite ignores duplicate ADD COLUMN).
            for _migration in [
                "ALTER TABLE news_signals ADD COLUMN yes_price REAL DEFAULT NULL",
                "ALTER TABLE market_universe ADD COLUMN winrate_estimate REAL DEFAULT NULL",
                "ALTER TABLE market_universe ADD COLUMN eligible INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE market_universe ADD COLUMN top_rank INTEGER DEFAULT NULL",
                "ALTER TABLE brain_decisions ADD COLUMN signal_source TEXT",
                "ALTER TABLE wallet_signals ADD COLUMN wallet_winrate_external REAL",
                "ALTER TABLE wallet_signals ADD COLUMN wallet_total_trades_external INTEGER",
                "ALTER TABLE wallet_signals ADD COLUMN wallet_rank INTEGER",
            ]:
                try:
                    conn.execute(_migration)
                except Exception:
                    pass  # column already exists
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_brain_decisions_signal_source_ts "
                "ON brain_decisions(signal_source, ts)"
            )
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

    def upsert_market_universe(self, row: dict) -> None:
        now = row.get("ts") or _now()
        details = row.get("details_json")
        if isinstance(details, (dict, list)):
            details = json.dumps(details, default=str)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO market_universe (
                    slug, ts, horizon, asset, period_ts, market_id, question,
                    liquidity_usdc, volume_usdc, yes_price, no_price,
                    up_token, down_token, accepting_orders, route_agent,
                    score, winrate_estimate, eligible, top_rank, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    ts=excluded.ts,
                    horizon=excluded.horizon,
                    asset=excluded.asset,
                    period_ts=excluded.period_ts,
                    market_id=excluded.market_id,
                    question=excluded.question,
                    liquidity_usdc=excluded.liquidity_usdc,
                    volume_usdc=excluded.volume_usdc,
                    yes_price=excluded.yes_price,
                    no_price=excluded.no_price,
                    up_token=excluded.up_token,
                    down_token=excluded.down_token,
                    accepting_orders=excluded.accepting_orders,
                    route_agent=excluded.route_agent,
                    score=excluded.score,
                    winrate_estimate=excluded.winrate_estimate,
                    eligible=excluded.eligible,
                    top_rank=excluded.top_rank,
                    details_json=excluded.details_json
                """,
                (
                    row["slug"],
                    now,
                    row["horizon"],
                    row["asset"],
                    int(row["period_ts"]),
                    str(row["market_id"]),
                    row.get("question"),
                    row.get("liquidity_usdc"),
                    row.get("volume_usdc"),
                    row.get("yes_price"),
                    row.get("no_price"),
                    row.get("up_token"),
                    row.get("down_token"),
                    1 if row.get("accepting_orders") else 0,
                    row["route_agent"],
                    float(row["score"]),
                    row.get("winrate_estimate"),
                    1 if row.get("eligible") else 0,
                    row.get("top_rank"),
                    details,
                ),
            )

    def list_market_universe(
        self,
        route_agent: Optional[str] = None,
        horizon: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        clauses = []
        params: list = []
        if route_agent:
            clauses.append("route_agent = ?")
            params.append(route_agent)
        if horizon:
            clauses.append("horizon = ?")
            params.append(horizon)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT * FROM market_universe "
            f"{where} ORDER BY score DESC, liquidity_usdc DESC LIMIT ?"
        )
        params.append(int(limit))
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_market_universe(self, slug: str) -> Optional[dict]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM market_universe WHERE slug = ?", (slug,)
            ).fetchone()
            return dict(row) if row else None

    def is_market_universe_eligible(
        self,
        slug: str,
        *,
        min_winrate: float = 0.52,
        require_top_rank: bool = True,
    ) -> bool:
        row = self.get_market_universe(slug)
        if not row:
            return False
        if require_top_rank and row.get("top_rank") is None:
            return False
        winrate = row.get("winrate_estimate")
        try:
            value = float(winrate if winrate is not None else row.get("score") or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        return bool(row.get("eligible")) and value >= float(min_winrate)

    def daily_trade_journal_stats(
        self,
        *,
        day: Optional[str] = None,
        agent: Optional[str] = None,
        market_id: Optional[str] = None,
        market_type: Optional[str] = None,
        asset: Optional[str] = None,
    ) -> dict:
        """Summarize today's realized behavior for adaptive win-rate scoring.

        The journal blends two auditable sources:
        - brain_decisions outcomes, when the decision was later annotated.
        - terminal trade rows, including scalper exits with response.pnl_pct.

        Failed execution attempts are tracked separately and used by callers as
        a quality penalty rather than counted as market-direction losses.
        """
        start, end = _utc_day_bounds(day)
        win_outcomes = {
            "closed_take_profit", "resolved_yes", "resolved_skipped_no",
        }
        loss_outcomes = {
            "closed_stop_loss", "closed_timeout", "closed_dust",
            "resolved_loss", "resolved_no",
        }
        wins = 0
        losses = 0
        failures = 0
        realized_pnl_usdc = 0.0
        pnl_pct_values: list[float] = []
        brain_rows = 0
        trade_rows = 0

        with self._lock, self._connect() as conn:
            clauses = ["ts >= ?", "ts < ?", "approved = 1", "outcome_status IS NOT NULL"]
            params: list = [start, end]
            if agent:
                clauses.append("agent = ?")
                params.append(agent)
            if market_id:
                clauses.append("market_id = ?")
                params.append(str(market_id))
            if market_type:
                clauses.append("market_type = ?")
                params.append(market_type)
            if asset:
                clauses.append("asset = ?")
                params.append(asset)
            rows = conn.execute(
                "SELECT outcome_status, score FROM brain_decisions WHERE "
                + " AND ".join(clauses),
                params,
            ).fetchall()
            brain_rows = len(rows)
            for row in rows:
                status = row["outcome_status"]
                if status in win_outcomes:
                    wins += 1
                elif status in loss_outcomes:
                    losses += 1

            trade_clauses = ["ts >= ?", "ts < ?"]
            trade_params: list = [start, end]
            if market_id:
                trade_clauses.append("market_id = ?")
                trade_params.append(str(market_id))
            agent_sql, agent_params = _trade_agent_filter(agent)
            trade_params.extend(agent_params)
            rows = conn.execute(
                "SELECT status, response_json, error, size_usdc FROM trades WHERE "
                + " AND ".join(trade_clauses)
                + agent_sql,
                trade_params,
            ).fetchall()
            for row in rows:
                status = row["status"]
                response = _parse_json_obj(row["response_json"])
                error = str(row["error"] or "")
                if error.startswith("SHADOW"):
                    continue
                if status in win_outcomes:
                    wins += 1
                    trade_rows += 1
                elif status in loss_outcomes:
                    losses += 1
                    trade_rows += 1
                elif status == "scalper_exit":
                    pnl_pct = response.get("pnl_pct")
                    try:
                        pnl_pct_f = float(pnl_pct)
                    except (TypeError, ValueError):
                        continue
                    pnl_pct_values.append(pnl_pct_f)
                    if pnl_pct_f > 0:
                        wins += 1
                    elif pnl_pct_f < 0:
                        losses += 1
                    trade_rows += 1
                elif status in {"failed", "close_failed", "may_have_fired"}:
                    failures += 1

                for key in ("realized_pnl_usdc", "pnl_usdc", "pnl"):
                    if key in response:
                        try:
                            realized_pnl_usdc += float(response[key])
                        except (TypeError, ValueError):
                            pass
                        break

        total = wins + losses
        winrate = (wins / total) if total else None
        avg_pnl_pct = (
            sum(pnl_pct_values) / len(pnl_pct_values) if pnl_pct_values else None
        )
        return {
            "day_start": start,
            "day_end": end,
            "agent": agent,
            "market_id": market_id,
            "market_type": market_type,
            "asset": asset,
            "wins": wins,
            "losses": losses,
            "total_with_outcome": total,
            "winrate": winrate,
            "failures": failures,
            "brain_rows": brain_rows,
            "trade_rows": trade_rows,
            "realized_pnl_usdc": round(realized_pnl_usdc, 6),
            "avg_pnl_pct": avg_pnl_pct,
        }

    def has_filled_position_for_market(
        self, market_id: str, token_id: Optional[str] = None,
    ) -> bool:
        """Return True if there is a FILLED row with no subsequent terminal
        close row. A terminal row (closed_*, resolved_*) written after the
        last FILLED row means the position has been exited, so re-entry is
        allowed. This prevents the old 'block forever on any historical fill'
        behaviour that left stale filled rows blocking markets indefinitely
        after position_manager had already closed them.

        When *token_id* is provided the match is broadened: a row matches if
        market_id matches OR (token_id matches and is not NULL). This closes
        the cross-agent dedupe gap where the Trader stores a numeric market ID
        while external_conviction stores a hex token ID.
        """
        _TERMINAL = (
            "closed_take_profit", "closed_stop_loss", "closed_timeout",
            "closed_dust", "resolved_yes", "resolved_no", "resolved_loss",
        )
        terminal_ph = ",".join("?" * len(_TERMINAL))
        if token_id:
            id_clause = "(market_id = ? OR (token_id = ? AND token_id IS NOT NULL))"
            id_params_fill = (str(market_id), str(token_id))
            id_params_term = (str(market_id), str(token_id))
        else:
            id_clause = "market_id = ?"
            id_params_fill = (str(market_id),)
            id_params_term = (str(market_id),)
        sql = f"""
            SELECT 1 FROM trades
            WHERE {id_clause} AND status = 'filled'
              AND id > COALESCE(
                (SELECT MAX(id) FROM trades
                 WHERE {id_clause} AND status IN ({terminal_ph})), 0
              )
            LIMIT 1
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                sql, (*id_params_fill, *id_params_term, *_TERMINAL)
            ).fetchone()
            return row is not None

    def has_active_trade_for_market(
        self, market_id: str, hours: int = 6, token_id: Optional[str] = None,
    ) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        time_bounded_placeholders = ",".join("?" for _ in TIME_BOUNDED_ACTIVE_STATUSES)
        unbounded_placeholders = ",".join("?" for _ in UNBOUNDED_BLOCKING_STATUSES)
        if token_id:
            id_clause = "(market_id = ? OR (token_id = ? AND token_id IS NOT NULL))"
            id_params = (str(market_id), str(token_id))
        else:
            id_clause = "market_id = ?"
            id_params = (str(market_id),)
        # MAY_HAVE_FIRED rows block forever (operator must clear manually);
        # other active statuses block only within the dedupe window.
        sql = (
            f"SELECT 1 FROM trades WHERE {id_clause} AND ("
            f"  (status IN ({time_bounded_placeholders}) AND ts >= ?)"
            f"  OR status IN ({unbounded_placeholders})"
            f") LIMIT 1"
        )
        with self._lock, self._connect() as conn:
            row = conn.execute(
                sql,
                (
                    *id_params,
                    *TIME_BOUNDED_ACTIVE_STATUSES,
                    cutoff,
                    *UNBOUNDED_BLOCKING_STATUSES,
                ),
            ).fetchone()
            return row is not None

    def has_recent_close_for_market(
        self,
        market_id: str,
        hours: int = 12,
        token_id: Optional[str] = None,
    ) -> bool:
        """Return True if a terminal close row exists within *hours*.

        Terminal close statuses: closed_take_profit, closed_stop_loss,
        closed_timeout, closed_dust.  These indicate a position was
        actively exited — re-entering too soon loses the spread.

        Cross-agent token_id matching follows the same pattern as
        has_active_trade_for_market (market_id OR token_id).
        """
        _CLOSE_STATUSES = (
            "closed_take_profit", "closed_stop_loss",
            "closed_timeout", "closed_dust",
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        ph = ",".join("?" for _ in _CLOSE_STATUSES)
        if token_id:
            id_clause = "(market_id = ? OR (token_id = ? AND token_id IS NOT NULL))"
            id_params = (str(market_id), str(token_id))
        else:
            id_clause = "market_id = ?"
            id_params = (str(market_id),)
        sql = (
            f"SELECT 1 FROM trades WHERE {id_clause} "
            f"AND status IN ({ph}) AND ts >= ? LIMIT 1"
        )
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, (*id_params, *_CLOSE_STATUSES, cutoff)).fetchone()
            return row is not None

    def count_recent_fills_for_market(
        self,
        market_id: str,
        hours: int = 24,
        token_id: Optional[str] = None,
    ) -> int:
        """Count filled/agent-open rows for a market within *hours*.

        Used to enforce per-market concentration limits — after the
        dedupe window expires, agents can re-enter endlessly without
        this guard.
        """
        _FILL_STATUSES = (
            "filled", "btc_daily_open", "near_resolution_open",
            "news_shock_open", "wallet_follow_open", "btc_5min_open",
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        ph = ",".join("?" for _ in _FILL_STATUSES)
        if token_id:
            id_clause = "(market_id = ? OR (token_id = ? AND token_id IS NOT NULL))"
            id_params = (str(market_id), str(token_id))
        else:
            id_clause = "market_id = ?"
            id_params = (str(market_id),)
        sql = (
            f"SELECT COUNT(*) AS n FROM trades WHERE {id_clause} "
            f"AND status IN ({ph}) AND ts >= ?"
        )
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, (*id_params, *_FILL_STATUSES, cutoff)).fetchone()
            return int(row["n"])

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
        price: Optional[float] = None,
        size_usdc: Optional[float] = None,
    ) -> None:
        response_json = json.dumps(response, default=str) if response is not None else None
        with self._lock, self._connect() as conn:
            fields = ["status = ?", "response_json = ?", "error = ?", "ts = ?"]
            params: list = [status, response_json, error, _now()]
            if price is not None:
                fields.append("price = ?")
                params.append(float(price))
            if size_usdc is not None:
                fields.append("size_usdc = ?")
                params.append(float(size_usdc))
            params.append(trade_id)
            conn.execute(
                f"UPDATE trades SET {', '.join(fields)} WHERE id = ?",
                params,
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
        open_statuses = (FILLED, BTC_DAILY_OPEN, NEAR_RESOLUTION_OPEN, NEWS_SHOCK_OPEN, WALLET_FOLLOW_OPEN, BTC_5MIN_OPEN)
        terminal_statuses = (
            "closed_take_profit", "closed_stop_loss", "closed_timeout",
            "closed_dust", "resolved_yes", "resolved_no", "resolved_loss",
        )
        placeholders = ",".join("?" for _ in open_statuses)
        terminal_placeholders = ",".join("?" for _ in terminal_statuses)
        sql = (
            "SELECT t.id, t.ts, t.market_id, t.token_id, t.side, t.price, "
            "t.size_usdc, t.status, t.response_json "
            "FROM trades t "
            "LEFT JOIN ("
            "  SELECT token_id, MAX(id) AS terminal_id FROM trades "
            f"  WHERE status IN ({terminal_placeholders}) "
            "  AND token_id IS NOT NULL AND token_id != '' "
            "  GROUP BY token_id"
            ") x ON x.token_id = t.token_id "
            f"WHERE t.status IN ({placeholders}) AND t.token_id IS NOT NULL "
            "AND t.token_id != '' "
            "AND (t.error IS NULL OR t.error NOT LIKE 'SHADOW%') "
            "AND t.id > COALESCE(x.terminal_id, 0) "
            "ORDER BY t.id"
        )
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, (*terminal_statuses, *open_statuses)).fetchall()
            return [dict(r) for r in rows]

    def has_close_attempt_for_token(
        self, token_id: str, after_id: Optional[int] = None
    ) -> bool:
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
        )
        params: list = [str(token_id)]
        if after_id is not None:
            sql += "AND id > ? "
            params.append(int(after_id))
        sql += "LIMIT 1"
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return row is not None

    def has_resolved_marker_for_token(
        self, token_id: str, after_id: Optional[int] = None
    ) -> bool:
        """Return True if the token has a resolved_* status row.

        Used by position_manager._already_closed to suppress the dust-override
        for markets with no CLOB orderbook (resolved/delisted). On-chain tokens
        for such markets must be redeemed via the CTF contract, not sold via CLOB.
        """
        sql = (
            "SELECT 1 FROM trades WHERE token_id = ? "
            "AND status IN ('resolved_yes','resolved_no','resolved_loss') "
        )
        params: list = [str(token_id)]
        if after_id is not None:
            sql += "AND id > ? "
            params.append(int(after_id))
        sql += "LIMIT 1"
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return row is not None

    def has_dust_close_for_token(
        self, token_id: str, after_id: Optional[int] = None
    ) -> bool:
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
        )
        params: list = [str(token_id)]
        if after_id is not None:
            sql += "AND id > ? "
            params.append(int(after_id))
        sql += "ORDER BY id DESC LIMIT 1"
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return row is not None and row[0] == "closed_dust"

    def has_partial_take_profit_for_token(
        self, token_id: str, after_id: Optional[int] = None
    ) -> bool:
        sql = (
            "SELECT 1 FROM trades WHERE token_id = ? "
            "AND status = 'closed_partial_take_profit' "
        )
        params: list = [str(token_id)]
        if after_id is not None:
            sql += "AND id > ? "
            params.append(int(after_id))
        sql += "LIMIT 1"
        with self._lock, self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return row is not None

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

    def set_no_sl_for_straddle_partner(
        self, straddle_id: str, closed_token_id: str
    ) -> int:
        """Patch the open straddle partner leg to add ``no_sl=True``.

        Called by position_manager after a straddle leg exits at TP.  The
        partner leg's cost is now covered; removing the stop-loss lets it
        hold to its own TP without being prematurely cut by a temporary
        adverse move.

        Returns the number of rows updated (normally 0 or 1).
        """
        select_sql = (
            "SELECT id, response_json FROM trades "
            "WHERE status = ? AND token_id != ? "
            "AND response_json LIKE ?"
        )
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                select_sql,
                (NEAR_RESOLUTION_OPEN, closed_token_id, f"%{straddle_id}%"),
            ).fetchall()
            updated = 0
            for row in rows:
                rj = row["response_json"]
                try:
                    payload = json.loads(rj) if rj else {}
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                if not isinstance(payload, dict):
                    continue
                if payload.get("straddle_id") != straddle_id:
                    continue
                payload["no_sl"] = True
                conn.execute(
                    "UPDATE trades SET response_json = ? WHERE id = ?",
                    (json.dumps(payload), row["id"]),
                )
                updated += 1
                logger.debug(
                    "trade_log: set no_sl on straddle partner row_id=%d straddle_id=%s",
                    row["id"], straddle_id,
                )
            return updated

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

    def upsert_settlement_reconciliation(
        self,
        *,
        token_id: str,
        market_id: str,
        status: str,
        action: str,
        latest_open_trade_id: Optional[int] = None,
        cost_basis_usdc: Optional[float] = None,
        journal_shares: Optional[float] = None,
        on_chain_shares: Optional[float] = None,
        best_bid: Optional[float] = None,
        best_ask: Optional[float] = None,
        recoverable_usdc: Optional[float] = None,
        redeemable_usdc: Optional[float] = None,
        gas_estimate_usdc: Optional[float] = None,
        details: Optional[dict] = None,
    ) -> None:
        details_json = json.dumps(details, default=str) if details is not None else None
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settlement_reconciliation
                    (token_id, market_id, status, action, updated_ts,
                     latest_open_trade_id, cost_basis_usdc, journal_shares,
                     on_chain_shares, best_bid, best_ask, recoverable_usdc,
                     redeemable_usdc, gas_estimate_usdc, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(token_id) DO UPDATE SET
                    market_id=excluded.market_id,
                    status=excluded.status,
                    action=excluded.action,
                    updated_ts=excluded.updated_ts,
                    latest_open_trade_id=excluded.latest_open_trade_id,
                    cost_basis_usdc=excluded.cost_basis_usdc,
                    journal_shares=excluded.journal_shares,
                    on_chain_shares=excluded.on_chain_shares,
                    best_bid=excluded.best_bid,
                    best_ask=excluded.best_ask,
                    recoverable_usdc=excluded.recoverable_usdc,
                    redeemable_usdc=excluded.redeemable_usdc,
                    gas_estimate_usdc=excluded.gas_estimate_usdc,
                    details_json=excluded.details_json
                """,
                (
                    str(token_id),
                    str(market_id),
                    status,
                    action,
                    _now(),
                    latest_open_trade_id,
                    cost_basis_usdc,
                    journal_shares,
                    on_chain_shares,
                    best_bid,
                    best_ask,
                    recoverable_usdc,
                    redeemable_usdc,
                    gas_estimate_usdc,
                    details_json,
                ),
            )

    def latest_settlement_reconciliations(
        self,
        *,
        max_age_minutes: Optional[float] = None,
    ) -> list:
        params: list = []
        where = ""
        if max_age_minutes is not None:
            cutoff = (
                datetime.now(timezone.utc)
                - timedelta(minutes=float(max_age_minutes))
            ).isoformat()
            where = "WHERE updated_ts >= ?"
            params.append(cutoff)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM settlement_reconciliation
                {where}
                ORDER BY updated_ts DESC
                """,
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def clear_stale_active_settlement_rows(self, open_token_ids: set[str]) -> int:
        """Clear active reconciliation rows for tokens no longer open in the journal."""
        active_statuses = ("active_unmanaged", "active_managed", "active_recoverable")
        details_json = json.dumps(
            {"reason": "no_open_journal_position", "open_token_count": len(open_token_ids)}
        )
        params: list = [
            "inactive_no_open_position",
            "no_open_journal_position",
            _now(),
            details_json,
            *active_statuses,
        ]
        where = f"status IN ({','.join('?' for _ in active_statuses)})"
        if open_token_ids:
            tokens = sorted(str(t) for t in open_token_ids if t)
            where += f" AND token_id NOT IN ({','.join('?' for _ in tokens)})"
            params.extend(tokens)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE settlement_reconciliation
                SET status = ?, action = ?, updated_ts = ?, details_json = ?
                WHERE {where}
                """,
                params,
            )
            return int(cur.rowcount or 0)

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
        terminal_statuses = (
            "closed_take_profit", "closed_stop_loss", "closed_timeout",
            "closed_dust", "resolved_yes", "resolved_no", "resolved_loss",
        )
        terminal_placeholders = ",".join("?" for _ in terminal_statuses)
        sql = (
            "SELECT t.market_id, t.token_id, t.side, t.price, t.size_usdc "
            "FROM trades t "
            "LEFT JOIN ("
            "  SELECT token_id, MAX(id) AS terminal_id FROM trades "
            f"  WHERE status IN ({terminal_placeholders}) "
            "  AND token_id IS NOT NULL AND token_id != '' "
            "  GROUP BY token_id"
            ") x ON x.token_id = t.token_id "
            "WHERE t.status = ? AND t.token_id IS NOT NULL "
            "AND t.token_id != '' "
            "AND t.id > COALESCE(x.terminal_id, 0) "
            "ORDER BY t.id"
        )
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, (*terminal_statuses, FILLED)).fetchall()
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
        yes_price: Optional[float] = None,
    ) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO news_signals (ts, headline, source, url, market_id, "
                "market_question, direction, materiality, relevance_score, "
                "latency_ms, model, status, reasoning, yes_price) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    float(yes_price) if yes_price is not None else None,
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
        signal_source: Optional[str] = None,
    ) -> int:
        features_json = json.dumps(features, default=str) if features is not None else None
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO brain_decisions (ts, agent, strategy, decision_type, "
                "market_id, token_id, approved, reason, score, market_type, asset, "
                "features_json, action, signal_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    signal_source,
                ),
            )
            return cur.lastrowid

    def recent_brain_decisions(self, limit: int = 50) -> list:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM brain_decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def market_brain_decisions(
        self, market_id: str, hours: float = 6, limit: int = 5
    ) -> list:
        """Return recent external_conviction brain decisions for a specific market.

        Used by the entry gate to skip markets where external signals disapprove.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT approved, score, reason, agent, ts FROM brain_decisions "
                "WHERE market_id = ? AND ts >= ? "
                "ORDER BY id DESC LIMIT ?",
                (str(market_id), cutoff, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def market_news_signals(
        self, market_id: str, hours: float = 24, limit: int = 5
    ) -> list:
        """Return recent news signals for a specific market.

        Used to enrich the LLM prompt context at entry and exit.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT headline, direction, materiality, reasoning, ts "
                "FROM news_signals "
                "WHERE market_id = ? AND ts >= ? AND status != 'classifier_failed' "
                "ORDER BY id DESC LIMIT ?",
                (str(market_id), cutoff, limit),
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
