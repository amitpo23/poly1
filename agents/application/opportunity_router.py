"""Opportunity router.

The router is the discipline layer between discovery and trading agents. It
does not place orders. It converts scout/research/news/database evidence into
an explicit route: reject, paper, backtest, or live_probe.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RouterConfig:
    min_live_score: float = 0.75
    max_live_risk: float = 0.35
    min_backtest_score: float = 0.55
    min_paper_ev: float = 0.01
    min_live_ev: float = 0.03
    min_liquidity_usd: float = 25_000.0
    max_spread_cents: float = 8.0
    default_error_margin: float = 0.03
    default_slippage: float = 0.01

    @classmethod
    def from_env(cls) -> "RouterConfig":
        return cls(
            min_live_score=_env_float("OPPORTUNITY_ROUTER_MIN_LIVE_SCORE", 0.75),
            max_live_risk=_env_float("OPPORTUNITY_ROUTER_MAX_LIVE_RISK", 0.35),
            min_backtest_score=_env_float("OPPORTUNITY_ROUTER_MIN_BACKTEST_SCORE", 0.55),
            min_paper_ev=_env_float("OPPORTUNITY_ROUTER_MIN_PAPER_EV", 0.01),
            min_live_ev=_env_float("OPPORTUNITY_ROUTER_MIN_LIVE_EV", 0.03),
            min_liquidity_usd=_env_float("OPPORTUNITY_ROUTER_MIN_LIQUIDITY_USD", 25_000.0),
            max_spread_cents=_env_float("OPPORTUNITY_ROUTER_MAX_SPREAD_CENTS", 8.0),
            default_error_margin=_env_float("OPPORTUNITY_ROUTER_ERROR_MARGIN", 0.03),
            default_slippage=_env_float("OPPORTUNITY_ROUTER_DEFAULT_SLIPPAGE", 0.01),
        )


@dataclass(frozen=True)
class OpportunityRoute:
    market_slug: str
    market_id: Optional[str]
    strategy: str
    route: str
    score: float
    risk_score: float
    estimated_true_probability: Optional[float]
    entry_price: Optional[float]
    expected_value: Optional[float]
    slippage: float
    error_margin: float
    liquidity_usd: Optional[float]
    spread_cents: Optional[float]
    catalyst_score: float
    historical_edge: Optional[float]
    reasons: list[str]


@dataclass(frozen=True)
class LiveRouteCheck:
    allowed: bool
    reason: str
    route: Optional[str] = None
    expected_value: Optional[float] = None


class OpportunityRouter:
    ROUTE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS opportunity_routes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_ts TEXT NOT NULL,
        market_slug TEXT NOT NULL,
        market_id TEXT,
        strategy_match TEXT NOT NULL,
        route TEXT NOT NULL,
        score REAL NOT NULL,
        risk_score REAL NOT NULL,
        estimated_true_probability REAL,
        entry_price REAL,
        expected_value REAL,
        slippage REAL NOT NULL,
        error_margin REAL NOT NULL,
        liquidity REAL,
        spread_cents REAL,
        catalyst_score REAL NOT NULL,
        historical_edge REAL,
        reasons_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_opportunity_routes_ts ON opportunity_routes(created_ts);
    CREATE INDEX IF NOT EXISTS ix_opportunity_routes_route_ts ON opportunity_routes(route, created_ts);
    CREATE UNIQUE INDEX IF NOT EXISTS ux_opportunity_routes_dedupe
        ON opportunity_routes(market_slug, strategy_match, date(created_ts));
    """

    def __init__(self, cfg: Optional[RouterConfig] = None):
        self.cfg = cfg or RouterConfig.from_env()

    def evaluate_row(self, row: dict) -> OpportunityRoute:
        reasons: list[str] = []
        final_score = float(row.get("final_score") or row.get("score") or 0.0)
        risk_score = float(row.get("risk_score") or 1.0)
        approved_for_live = bool(int(row.get("approved_for_live") or 0))
        approved_for_backtest = bool(int(row.get("approved_for_backtest") or 0))
        features = self._loads(row.get("features_json"))

        liquidity = self._float(
            features.get("liquidity_usd")
            or row.get("liquidity")
            or row.get("scout_liquidity")
        )
        spread = self._float(
            features.get("spread_cents")
            or row.get("spread_cents")
            or row.get("scout_spread_cents")
        )
        entry_price = self._entry_price(row, features)
        true_probability, probability_source = self._estimated_true_probability(
            row, features, entry_price, final_score, risk_score
        )
        slippage = self._slippage(row, features, spread)
        error_margin = self._error_margin(features)
        expected_value = self._expected_value(
            true_probability=true_probability,
            entry_price=entry_price,
            slippage=slippage,
            error_margin=error_margin,
        )
        catalyst_score = self._catalyst_score(row, features)
        historical_edge = self._historical_edge(features)

        if liquidity is not None and liquidity < self.cfg.min_liquidity_usd:
            reasons.append(f"liquidity_below_floor:{liquidity:.0f}")
        if spread is not None and spread > self.cfg.max_spread_cents:
            reasons.append(f"spread_too_wide:{spread:.2f}")
        if risk_score > self.cfg.max_live_risk:
            reasons.append(f"risk_above_live_limit:{risk_score:.3f}")
        if entry_price is None:
            reasons.append("missing_entry_price")
        if true_probability is None:
            reasons.append("missing_true_probability")
        elif probability_source != "explicit":
            reasons.append(f"probability_is_model_estimate:{probability_source}")
        if historical_edge is None:
            reasons.append("missing_historical_edge")
        elif historical_edge <= 0:
            reasons.append(f"non_positive_historical_edge:{historical_edge:.3f}")
        if expected_value is not None and expected_value <= 0:
            reasons.append(f"non_positive_ev:{expected_value:.3f}")

        live_blockers = [r for r in reasons if r.startswith((
            "liquidity_below_floor",
            "spread_too_wide",
            "risk_above_live_limit",
            "non_positive_ev",
            "missing_entry_price",
            "missing_true_probability",
            "probability_is_model_estimate",
            "missing_historical_edge",
            "non_positive_historical_edge",
        ))]
        paper_blockers = [r for r in reasons if r.startswith((
            "liquidity_below_floor",
            "spread_too_wide",
            "missing_entry_price",
            "missing_true_probability",
            "non_positive_ev",
        ))]

        if (
            approved_for_live
            and final_score >= self.cfg.min_live_score
            and expected_value is not None
            and expected_value >= self.cfg.min_live_ev
            and not live_blockers
        ):
            route = "live_probe"
            reasons.append("live_probe_allowed_by_router")
        elif (
            expected_value is not None
            and expected_value >= self.cfg.min_paper_ev
            and final_score >= 0.45
            and not paper_blockers
        ):
            route = "paper"
            reasons.append("positive_ev_paper_probe")
        elif approved_for_backtest or final_score >= self.cfg.min_backtest_score:
            route = "backtest"
            reasons.append("requires_backtest_before_capital")
        elif final_score > 0.35 or catalyst_score > 0.25:
            route = "paper"
            reasons.append("paper_only_until_edge_is_observed")
        else:
            route = "reject"
            reasons.append("insufficient_evidence")

        return OpportunityRoute(
            market_slug=str(row.get("market_slug") or row.get("slug") or ""),
            market_id=row.get("market_id"),
            strategy=str(row.get("strategy_match") or row.get("strategy") or ""),
            route=route,
            score=round(final_score, 3),
            risk_score=round(risk_score, 3),
            estimated_true_probability=(
                round(true_probability, 3) if true_probability is not None else None
            ),
            entry_price=round(entry_price, 3) if entry_price is not None else None,
            expected_value=round(expected_value, 3) if expected_value is not None else None,
            slippage=round(slippage, 3),
            error_margin=round(error_margin, 3),
            liquidity_usd=round(liquidity, 2) if liquidity is not None else None,
            spread_cents=round(spread, 3) if spread is not None else None,
            catalyst_score=round(catalyst_score, 3),
            historical_edge=round(historical_edge, 3) if historical_edge is not None else None,
            reasons=reasons,
        )

    def latest_from_scout_db(self, db_path: str, limit: int = 25) -> list[OpportunityRoute]:
        if not Path(db_path).exists():
            return []
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            if self._table_exists(conn, "research_reports"):
                rows = conn.execute(
                    """
                    WITH latest_scout AS (
                        SELECT *
                        FROM scout_opportunities
                        WHERE id IN (
                            SELECT MAX(id)
                            FROM scout_opportunities
                            GROUP BY market_slug, strategy_match
                        )
                    )
                    SELECT r.*,
                           s.score AS scout_score,
                           s.yes_price AS scout_yes_price,
                           s.no_price AS scout_no_price,
                           s.spread_cents AS scout_spread_cents,
                           s.volume_24h AS scout_volume_24h,
                           s.liquidity AS scout_liquidity,
                           s.news_count AS scout_news_count,
                           s.top_news_headline AS scout_top_news_headline,
                           s.reason AS scout_reason
                    FROM research_reports r
                    LEFT JOIN latest_scout s
                      ON s.market_slug = r.market_slug
                     AND s.strategy_match = r.strategy_match
                    ORDER BY r.id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            elif self._table_exists(conn, "scout_opportunities"):
                rows = conn.execute(
                    """
                    SELECT *
                    FROM scout_opportunities
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            else:
                rows = []
        return [self.evaluate_row(dict(row)) for row in rows]

    def persist_latest_routes(self, db_path: str, limit: int = 100) -> int:
        routes = self.latest_from_scout_db(db_path, limit=limit)
        if not routes:
            return 0
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with sqlite3.connect(db_path, isolation_level=None) as conn:
            conn.executescript(self.ROUTE_SCHEMA)
            written = 0
            for route in routes:
                cur = conn.execute(
                    """
                    INSERT OR REPLACE INTO opportunity_routes
                        (created_ts, market_slug, market_id, strategy_match,
                         route, score, risk_score, estimated_true_probability,
                         entry_price, expected_value, slippage, error_margin,
                         liquidity, spread_cents, catalyst_score, historical_edge,
                         reasons_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        route.market_slug,
                        route.market_id,
                        route.strategy,
                        route.route,
                        route.score,
                        route.risk_score,
                        route.estimated_true_probability,
                        route.entry_price,
                        route.expected_value,
                        route.slippage,
                        route.error_margin,
                        route.liquidity_usd,
                        route.spread_cents,
                        route.catalyst_score,
                        route.historical_edge,
                        json.dumps(route.reasons, sort_keys=True),
                    ),
                )
                written += max(0, cur.rowcount)
        return written

    @staticmethod
    def _loads(raw) -> dict:
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    @staticmethod
    def _float(value) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _entry_price(self, row: dict, features: dict) -> Optional[float]:
        explicit = self._float(features.get("entry_price"))
        if explicit is not None:
            return explicit
        strategy = str(row.get("strategy_match") or row.get("strategy") or "")
        if strategy == "nothing_happens":
            return self._float(row.get("no_price") or row.get("scout_no_price"))
        return self._float(row.get("yes_price") or row.get("scout_yes_price"))

    def _estimated_true_probability(
        self,
        row: dict,
        features: dict,
        entry_price: Optional[float],
        final_score: float,
        risk_score: float,
    ) -> tuple[Optional[float], str]:
        explicit = self._float(
            features.get("estimated_true_probability")
            or features.get("true_probability")
            or features.get("probability_estimate")
        )
        if explicit is not None:
            return min(0.99, max(0.01, explicit)), "explicit"
        if entry_price is None:
            return None, "missing"

        confidence = self._float(row.get("confidence")) or 0.0
        historical_edge = self._historical_edge(features) or 0.0
        catalyst = self._catalyst_score(row, features)
        score_edge = max(0.0, final_score - 0.50) * 0.10
        risk_discount = max(0.0, 1.0 - risk_score)
        estimate = (
            entry_price
            + score_edge * max(0.2, confidence) * risk_discount
            + max(0.0, historical_edge) * 0.50
            + catalyst * 0.01
        )
        return min(0.99, max(0.01, estimate)), "score_history_catalyst"

    def _slippage(self, row: dict, features: dict, spread_cents: Optional[float]) -> float:
        explicit = self._float(features.get("slippage") or row.get("slippage"))
        if explicit is not None:
            return max(0.0, explicit)
        if spread_cents is not None:
            return max(self.cfg.default_slippage, spread_cents / 200.0)
        return self.cfg.default_slippage

    def _error_margin(self, features: dict) -> float:
        explicit = self._float(features.get("error_margin"))
        if explicit is not None:
            return max(0.0, explicit)
        data_depth = self._float(features.get("data_depth"))
        if data_depth is not None:
            return max(0.01, self.cfg.default_error_margin * (1.5 - min(1.0, data_depth)))
        return self.cfg.default_error_margin

    def _catalyst_score(self, row: dict, features: dict) -> float:
        explicit = self._float(features.get("catalyst_score"))
        if explicit is not None:
            return min(1.0, max(0.0, explicit))
        news_count = self._float(row.get("news_count") or row.get("scout_news_count")) or 0.0
        news_score = self._float(features.get("news_score")) or 0.0
        return min(1.0, max(news_score, news_count / 5.0))

    def _historical_edge(self, features: dict) -> Optional[float]:
        for key in ("historical_edge", "backtest_edge", "paper_edge", "live_probe_edge"):
            value = self._float(features.get(key))
            if value is not None:
                return value
        return None

    @staticmethod
    def _expected_value(
        *,
        true_probability: Optional[float],
        entry_price: Optional[float],
        slippage: float,
        error_margin: float,
    ) -> Optional[float]:
        if true_probability is None or entry_price is None:
            return None

        return float(true_probability) - float(entry_price) - float(slippage) - float(error_margin)

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return bool(row)


def live_route_allowed(
    *,
    db_path: str,
    market_slug: str,
    strategy: str,
    max_age_hours: float = 24.0,
) -> LiveRouteCheck:
    """Return whether a live entry is allowed by the latest router row.

    Missing/stale routes block live trading. This is deliberate: agents should
    gather research/paper evidence before spending capital.
    """
    path = Path(db_path)
    if not path.exists():
        return LiveRouteCheck(False, f"missing_router_db:{db_path}")
    cutoff = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - max_age_hours * 3600,
        tz=timezone.utc,
    ).isoformat(timespec="seconds")
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        if not OpportunityRouter._table_exists(conn, "opportunity_routes"):
            return LiveRouteCheck(False, "missing_opportunity_routes")
        row = conn.execute(
            """
            SELECT route, expected_value, reasons_json, created_ts
            FROM opportunity_routes
            WHERE market_slug = ?
              AND (strategy_match = ? OR ? = '')
              AND created_ts >= ?
            ORDER BY created_ts DESC, id DESC
            LIMIT 1
            """,
            (str(market_slug), str(strategy), str(strategy), cutoff),
        ).fetchone()
    if row is None:
        return LiveRouteCheck(False, "no_fresh_live_probe_route")
    route = str(row["route"] or "")
    ev = row["expected_value"]
    if route != "live_probe":
        return LiveRouteCheck(False, f"route_is_{route}", route=route, expected_value=ev)
    return LiveRouteCheck(True, "live_probe_allowed_by_router", route=route, expected_value=ev)
