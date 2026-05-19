"""Execution-quality scoring from live Polymarket order-book data.

This layer answers a narrower question than MetaBrain's forecasting logic:
"Even if the signal is right, can this order enter and exit with positive
expectancy after spread, depth, and slippage?"
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from agents.application.trade_log import TradeLog


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ExecutionQuality:
    ok: bool
    score: float
    reason: str
    features: dict = field(default_factory=dict)


class ExecutionQualityAdvisor:
    """Read orderbook_latest and produce a pre-trade execution gate."""

    def __init__(self, trade_log: Optional[TradeLog] = None, db_path: Optional[str] = None):
        self.trade_log = trade_log
        self.db_path = db_path

    def _log(self) -> TradeLog:
        if self.trade_log is None:
            self.trade_log = TradeLog(db_path=self.db_path)
        return self.trade_log

    def evaluate(
        self,
        *,
        token_id: Optional[str],
        intended_usdc: Optional[float] = None,
        max_age_seconds: Optional[float] = None,
        max_spread_pct: Optional[float] = None,
        min_bid_depth_usdc: Optional[float] = None,
        max_avg_slippage_pct: Optional[float] = None,
        min_score: Optional[float] = None,
        require_fresh: Optional[bool] = None,
    ) -> ExecutionQuality:
        if not token_id:
            return ExecutionQuality(True, 0.5, "no_token_id", {"execution_quality_skipped": True})

        max_age_seconds = (
            _env_float("EXECUTION_QUALITY_MAX_AGE_SEC", 10.0)
            if max_age_seconds is None else float(max_age_seconds)
        )
        max_spread_pct = (
            _env_float("EXECUTION_QUALITY_MAX_SPREAD_PCT", 0.05)
            if max_spread_pct is None else float(max_spread_pct)
        )
        min_bid_depth_usdc = (
            _env_float("EXECUTION_QUALITY_MIN_BID_DEPTH_USDC", 20.0)
            if min_bid_depth_usdc is None else float(min_bid_depth_usdc)
        )
        max_avg_slippage_pct = (
            _env_float("EXECUTION_QUALITY_MAX_AVG_SLIPPAGE_PCT", 0.025)
            if max_avg_slippage_pct is None else float(max_avg_slippage_pct)
        )
        min_score = (
            _env_float("EXECUTION_QUALITY_MIN_SCORE", 0.65)
            if min_score is None else float(min_score)
        )
        require_fresh = (
            _env_bool("EXECUTION_QUALITY_REQUIRE_FRESH", True)
            if require_fresh is None else bool(require_fresh)
        )
        try:
            snapshot = self._log().latest_orderbook_snapshot(
                str(token_id),
                max_age_seconds=max_age_seconds,
            )
        except Exception as exc:
            ok = not require_fresh
            return ExecutionQuality(
                ok,
                0.0 if require_fresh else 0.5,
                "orderbook_unavailable" if require_fresh else "orderbook_unavailable_fail_open",
                {
                    "execution_quality_token_id": str(token_id),
                    "execution_quality_error": f"{type(exc).__name__}: {exc}",
                    "execution_quality_max_age_sec": max_age_seconds,
                },
            )
        if snapshot is None:
            ok = not require_fresh
            return ExecutionQuality(
                ok,
                0.0 if require_fresh else 0.5,
                "no_fresh_orderbook" if require_fresh else "no_fresh_orderbook_fail_open",
                {
                    "execution_quality_token_id": str(token_id),
                    "execution_quality_max_age_sec": max_age_seconds,
                },
            )

        spread = _float(snapshot.get("spread_pct"))
        bid_depth = _float(snapshot.get("bid_depth_usdc"))
        ask_depth = _float(snapshot.get("ask_depth_usdc"))
        imbalance = _float(snapshot.get("imbalance"))
        intended_usdc = intended_usdc or 3.0
        slippage_key = _slippage_key(float(intended_usdc))
        avg_slippage = _float(snapshot.get(slippage_key))

        spread_score = _inverse_score(spread, max_spread_pct)
        bid_depth_score = _floor_score(bid_depth, min_bid_depth_usdc)
        ask_depth_score = _floor_score(ask_depth, max(float(intended_usdc), 1.0) * 2.0)
        slippage_score = _inverse_score(avg_slippage, max_avg_slippage_pct)
        imbalance_score = 0.5
        if imbalance is not None:
            # Bid-heavy books are easier to exit.  Map [-1, 1] to [0, 1].
            imbalance_score = max(0.0, min(1.0, (imbalance + 1.0) / 2.0))

        score = round(
            0.30 * spread_score
            + 0.30 * bid_depth_score
            + 0.20 * slippage_score
            + 0.10 * ask_depth_score
            + 0.10 * imbalance_score,
            4,
        )
        features = {
            "execution_quality_score": score,
            "execution_quality_token_id": str(token_id),
            "orderbook_age_sec": round(float(snapshot.get("age_seconds") or 0.0), 3),
            "orderbook_source": snapshot.get("source"),
            "best_bid": snapshot.get("best_bid"),
            "best_ask": snapshot.get("best_ask"),
            "spread_pct": spread,
            "bid_depth_usdc": bid_depth,
            "ask_depth_usdc": ask_depth,
            "orderbook_imbalance": imbalance,
            "avg_slippage_pct": avg_slippage,
            "avg_slippage_key": slippage_key,
            "min_execution_quality_score": min_score,
        }
        blockers: list[str] = []
        if spread is None or spread > max_spread_pct:
            blockers.append(f"spread:{spread}>{max_spread_pct}")
        if bid_depth is None or bid_depth < min_bid_depth_usdc:
            blockers.append(f"bid_depth:{bid_depth}<{min_bid_depth_usdc}")
        if avg_slippage is not None and avg_slippage > max_avg_slippage_pct:
            blockers.append(f"slippage:{avg_slippage}>{max_avg_slippage_pct}")
        if score < min_score:
            blockers.append(f"score:{score}<{min_score}")
        if blockers:
            reason = "execution_quality_blocked:" + ",".join(blockers)
            features["execution_quality_reason"] = reason
            return ExecutionQuality(False, score, reason, features)
        features["execution_quality_reason"] = "execution_quality_ok"
        return ExecutionQuality(True, score, "execution_quality_ok", features)


def _float(value) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _inverse_score(value: Optional[float], ceiling: float) -> float:
    if value is None:
        return 0.0
    if ceiling <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (float(value) / float(ceiling))))


def _floor_score(value: Optional[float], floor: float) -> float:
    if value is None:
        return 0.0
    if floor <= 0:
        return 1.0
    return max(0.0, min(1.0, float(value) / float(floor)))


def _slippage_key(intended_usdc: float) -> str:
    if intended_usdc <= 1.5:
        return "slippage_buy_1_pct"
    if intended_usdc <= 4.0:
        return "slippage_buy_3_pct"
    return "slippage_buy_5_pct"
