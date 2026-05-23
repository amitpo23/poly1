"""Scanner Executor — turns fresh brain-approved scanner signals into orders.

market_scanner is deliberately read-only: it discovers opportunities and writes
auditable brain_decisions.  This bridge is the only component allowed to consume
those decisions for live entry, and it still re-checks metadata, EV, order-book
executability, risk, dedupe, and runtime control before an order can fire.
"""
from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agents.application.decision_council import DecisionCouncil
from agents.application.regime_router import family_from_signal, route_for_features
from agents.application.risk_gate import RiskGate
from agents.application.signal_contract import TradeProposal
from agents.application.sizing import kelly_size_usdc
from agents.application.trade_log import FILLED, TradeLog
from agents.utils.notify import _safe_balance, notify_trade
from agents.utils.objects import TradeRecommendation

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass
class ScannerExecutorConfig:
    poll_seconds: int = 2
    max_decision_age_seconds: int = 180
    batch_limit: int = 50
    position_size_usdc: float = 1.0
    reserve_usdc: float = 0.0
    min_score: float = 0.80
    min_proven_calibrated_score: float = 0.54
    min_raw_ev: float = 0.04
    min_net_ev: float = 0.03
    round_trip_cost_pct: float = 0.04
    max_entry_drift_pct: float = 0.04
    max_immediate_exit_loss_pct: float = 0.03
    prefer_maker_for_fast_markets: bool = True
    maker_tick_size: float = 0.01
    maker_min_profit_cents: float = 0.01
    require_timing_now: bool = True
    require_calibrated_probability: bool = True
    allow_wait_with_high_score: bool = False
    wait_override_min_score: float = 0.79
    max_open_positions: int = 4
    reentry_cooldown_hours: int = 12
    market_loss_cooldown_hours: float = 1.0
    shadow_entry_cooldown_minutes: int = 10
    heartbeat_path: str = "/app/data/scanner_executor_heartbeat"
    provider_scorecard_path: str = "./data/provider_scorecard.json"
    strategy_scorecard_path: str = "./data/strategy_scorecard.json"
    require_promotable_strategy: bool = False
    enforce_regime_router: bool = False
    learning_guard_enabled: bool = False
    learning_preferred_side: str = "BUY"
    learning_min_entry_price: float = 0.40
    learning_max_entry_price: float = 0.50
    learning_allow_proven_side_override: bool = False
    learning_allow_proven_price_override: bool = False
    learning_guard_ttl_hours: float = 24.0
    repeat_reject_cache_ttl_seconds: int = 300
    repeat_reject_quarantine_threshold: int = 5
    repeat_reject_quarantine_seconds: int = 3600
    candidate_agents: tuple[str, ...] = ("market_scanner",)

    @classmethod
    def from_env(cls) -> "ScannerExecutorConfig":
        return cls(
            poll_seconds=_env_int("SCANNER_EXECUTOR_POLL_SEC", 2),
            max_decision_age_seconds=_env_int("SCANNER_EXECUTOR_MAX_DECISION_AGE_SEC", 180),
            batch_limit=_env_int("SCANNER_EXECUTOR_BATCH_LIMIT", 50),
            position_size_usdc=_env_float("SCANNER_EXECUTOR_POSITION_SIZE_USDC", 1.0),
            reserve_usdc=_env_float("SCANNER_EXECUTOR_RESERVE_USDC", 0.0),
            min_score=_env_float("SCANNER_EXECUTOR_MIN_SCORE", 0.80),
            min_proven_calibrated_score=_env_float(
                "SCANNER_EXECUTOR_MIN_PROVEN_CALIBRATED_SCORE",
                0.54,
            ),
            min_raw_ev=_env_float("SCANNER_EXECUTOR_MIN_RAW_EV", 0.04),
            min_net_ev=_env_float("SCANNER_EXECUTOR_MIN_NET_EV", 0.03),
            round_trip_cost_pct=_env_float("SCANNER_EXECUTOR_ROUND_TRIP_COST_PCT", 0.04),
            max_entry_drift_pct=_env_float("SCANNER_EXECUTOR_MAX_ENTRY_DRIFT_PCT", 0.04),
            max_immediate_exit_loss_pct=_env_float(
                "SCANNER_EXECUTOR_MAX_IMMEDIATE_EXIT_LOSS_PCT",
                0.03,
            ),
            prefer_maker_for_fast_markets=_env_bool(
                "SCANNER_EXECUTOR_PREFER_MAKER_FOR_FAST_MARKETS",
                True,
            ),
            maker_tick_size=_env_float("SCANNER_EXECUTOR_MAKER_TICK_SIZE", 0.01),
            maker_min_profit_cents=_env_float("SCANNER_EXECUTOR_MAKER_MIN_PROFIT_CENTS", 0.01),
            require_timing_now=_env_bool("SCANNER_EXECUTOR_REQUIRE_TIMING_NOW", True),
            require_calibrated_probability=_env_bool(
                "SCANNER_EXECUTOR_REQUIRE_CALIBRATED_PROBABILITY",
                True,
            ),
            allow_wait_with_high_score=_env_bool(
                "SCANNER_EXECUTOR_ALLOW_WAIT_WITH_HIGH_SCORE",
                False,
            ),
            wait_override_min_score=_env_float(
                "SCANNER_EXECUTOR_WAIT_OVERRIDE_MIN_SCORE",
                0.79,
            ),
            max_open_positions=_env_int("SCANNER_EXECUTOR_MAX_OPEN", 4),
            reentry_cooldown_hours=_env_int("SCANNER_EXECUTOR_REENTRY_COOLDOWN_HOURS", 12),
            market_loss_cooldown_hours=_env_float(
                "SCANNER_EXECUTOR_MARKET_LOSS_COOLDOWN_HOURS",
                1.0,
            ),
            shadow_entry_cooldown_minutes=_env_int(
                "SCANNER_EXECUTOR_SHADOW_ENTRY_COOLDOWN_MINUTES",
                10,
            ),
            heartbeat_path=os.getenv(
                "SCANNER_EXECUTOR_HEARTBEAT_PATH",
                "/app/data/scanner_executor_heartbeat",
            ),
            provider_scorecard_path=os.getenv(
                "PROVIDER_SCORECARD_PATH",
                "./data/provider_scorecard.json",
            ),
            strategy_scorecard_path=os.getenv(
                "STRATEGY_SCORECARD_PATH",
                "./data/strategy_scorecard.json",
            ),
            require_promotable_strategy=_env_bool(
                "SCANNER_EXECUTOR_REQUIRE_PROMOTABLE_STRATEGY",
                False,
            ),
            enforce_regime_router=_env_bool(
                "SCANNER_EXECUTOR_ENFORCE_REGIME_ROUTER",
                False,
            ),
            learning_guard_enabled=_env_bool(
                "SCANNER_EXECUTOR_LEARNING_GUARD_ENABLED",
                False,
            ),
            learning_preferred_side=os.getenv(
                "SCANNER_EXECUTOR_LEARNING_PREFERRED_SIDE",
                "BUY",
            ).strip().upper(),
            learning_min_entry_price=_env_float(
                "SCANNER_EXECUTOR_LEARNING_MIN_ENTRY_PRICE",
                0.40,
            ),
            learning_max_entry_price=_env_float(
                "SCANNER_EXECUTOR_LEARNING_MAX_ENTRY_PRICE",
                0.50,
            ),
            learning_allow_proven_side_override=_env_bool(
                "SCANNER_EXECUTOR_LEARNING_ALLOW_PROVEN_SIDE_OVERRIDE",
                False,
            ),
            learning_allow_proven_price_override=_env_bool(
                "SCANNER_EXECUTOR_LEARNING_ALLOW_PROVEN_PRICE_OVERRIDE",
                False,
            ),
            learning_guard_ttl_hours=_env_float(
                "SCANNER_EXECUTOR_LEARNING_GUARD_TTL_HOURS",
                24.0,
            ),
            repeat_reject_cache_ttl_seconds=_env_int(
                "SCANNER_EXECUTOR_REPEAT_REJECT_CACHE_TTL_SEC",
                300,
            ),
            repeat_reject_quarantine_threshold=_env_int(
                "SCANNER_EXECUTOR_REPEAT_REJECT_QUARANTINE_THRESHOLD",
                5,
            ),
            repeat_reject_quarantine_seconds=_env_int(
                "SCANNER_EXECUTOR_REPEAT_REJECT_QUARANTINE_SEC",
                3600,
            ),
            candidate_agents=_env_list(
                "SCANNER_EXECUTOR_CANDIDATE_AGENTS",
                _env_list("ROUTER_LIVE_ENTRY_AGENTS", ("market_scanner",)),
            ),
        )


class _DocMarket:
    def __init__(self, metadata: dict):
        self._metadata = metadata

    def dict(self) -> dict:
        return {"metadata": self._metadata}


class ScannerExecutor:
    def __init__(
        self,
        *,
        cfg: Optional[ScannerExecutorConfig] = None,
        trade_log: Optional[TradeLog] = None,
        polymarket=None,
        risk_gate: Optional[RiskGate] = None,
        execute: Optional[bool] = None,
    ):
        self.cfg = cfg or ScannerExecutorConfig.from_env()
        self.trade_log = trade_log or TradeLog()
        self.execute = (
            bool(execute)
            if execute is not None
            else _env_bool("EXECUTE_SCANNER_EXECUTOR", False)
        )
        if polymarket is None:
            from agents.polymarket.polymarket import Polymarket
            read_book_in_shadow = _env_bool(
                "SCANNER_EXECUTOR_READ_ORDERBOOK_IN_SHADOW",
                True,
            )
            polymarket = Polymarket(live=self.execute or read_book_in_shadow)
        self.polymarket = polymarket
        self.risk_gate = risk_gate or RiskGate(
            trade_log=self.trade_log,
            polymarket=self.polymarket,
            scanner_executor_reserve_usdc=self.cfg.reserve_usdc,
            max_open_positions=self.cfg.max_open_positions,
        )
        self.decision_council = DecisionCouncil.from_env(
            min_raw_ev=self.cfg.min_raw_ev,
            min_net_ev=self.cfg.min_net_ev,
            round_trip_cost_pct=self.cfg.round_trip_cost_pct,
        )
        self._processed: set[int] = set()
        self._learning_guard_started_ts = time.time()
        self._repeat_rejects: dict[tuple[str, str], tuple[int, float]] = {}
        self._market_quarantine_until: dict[str, float] = {}

    def run_once(self) -> dict:
        stats = {
            "seen": 0,
            "executed": 0,
            "shadow": 0,
            "skipped": 0,
            "failed": 0,
        }
        rows = self.trade_log.recent_entry_trade_opportunities(
            agents=self._candidate_agents(),
            max_age_seconds=self.cfg.max_decision_age_seconds,
            limit=self.cfg.batch_limit,
        )
        for row in rows:
            decision_id = int(row["id"])
            if decision_id in self._processed:
                continue
            stats["seen"] += 1
            try:
                result = self._handle_decision(row)
            except Exception as exc:
                logger.exception("scanner_executor: decision %s failed", decision_id)
                self._record_reject(row, "executor_exception", {"error": str(exc)[:300]})
                result = "failed"
            self._processed.add(decision_id)
            if result in stats:
                stats[result] += 1
        self._heartbeat()
        return stats

    def _handle_decision(self, row: dict) -> str:
        features = _parse_features(row.get("features_json"))
        decision_id = int(row["id"])
        market_id = str(row["market_id"])
        score = float(row.get("score") or 0.0)
        try:
            proposal = TradeProposal.from_brain_decision(row, features)
        except ValueError as exc:
            reason = (
                "missing_execution_metadata"
                if str(row.get("agent") or "") == "market_scanner"
                else "proposal_missing_execution_fields"
            )
            self._record_reject(
                row,
                reason,
                {
                    "proposal_error": str(exc),
                    "source_agent": row.get("agent"),
                    "source_strategy": row.get("strategy"),
                    "source_signal_source": row.get("signal_source"),
                    "has_selected_side": bool(features.get("selected_side") or row.get("action")),
                    "has_selected_token_id": bool(features.get("selected_token_id") or row.get("token_id")),
                    "has_selected_entry_price": bool(
                        features.get("selected_entry_price") or features.get("entry_price")
                    ),
                },
            )
            return "skipped"
        side = proposal.side
        token_id = proposal.token_id
        question = str(features.get("question") or market_id)
        quarantine_remaining = self._market_quarantine_remaining(market_id)
        if quarantine_remaining > 0:
            self._record_reject(
                row,
                "market_recent_reject_quarantine",
                {"quarantine_remaining_sec": round(quarantine_remaining, 3)},
            )
            return "skipped"

        meta_timing = str(features.get("meta_timing") or "")
        if not meta_timing and str(row.get("decision_type") or "") in {"trade_plan", "shadow_trade_plan"}:
            meta_timing = "now"
        timing_override = False
        if self.cfg.require_timing_now and meta_timing != "now":
            timing_override = (
                self.cfg.allow_wait_with_high_score
                and meta_timing == "wait"
                and score >= self.cfg.wait_override_min_score
            )
            if not timing_override:
                self._record_reject(row, "timing_not_now", {"meta_timing": meta_timing})
                return "skipped"
        min_score = self._min_score_for_decision(row, features)
        if score < min_score:
            self._record_reject(
                row,
                "score_below_executor_min",
                {"score": score, "min_score": min_score},
            )
            return "skipped"
        if (
            self.cfg.require_calibrated_probability
            and not bool(features.get("estimated_win_probability_calibrated"))
        ):
            self._record_reject(
                row,
                "probability_not_calibrated",
                {
                    "estimated_win_probability": features.get("estimated_win_probability"),
                    "estimated_win_probability_source": features.get(
                        "estimated_win_probability_source",
                        "missing",
                    ),
                },
            )
            return "skipped"
        proof_features = self._proof_snapshot(row, features)
        if (
            self.cfg.require_promotable_strategy
            and proof_features.get("proof_strategy_state") != "promotable"
        ):
            self._record_reject(
                row,
                "strategy_scorecard_not_promotable",
                proof_features,
            )
            return "skipped"
        if side not in {"BUY", "SELL"} or not token_id:
            self._record_reject(row, "missing_execution_metadata", {"side": side, "token_id": bool(token_id)})
            return "skipped"
        if (
            self._learning_guard_active()
            and self.cfg.learning_preferred_side in {"BUY", "SELL"}
            and side != self.cfg.learning_preferred_side
            and not (
                self.cfg.learning_allow_proven_side_override
                and self._has_proven_override(row, features)
            )
        ):
            self._record_reject(
                row,
                "today_lesson_side_blocked",
                {
                    "side": side,
                    "preferred_side": self.cfg.learning_preferred_side,
                    "lesson": "2026-05-21_live_buy_up_outperformed_sell_down",
                },
            )
            return "skipped"

        outcomes = _as_list(features.get("outcomes")) or ["Yes", "No"]
        token_ids = _as_list(features.get("clob_token_ids"))
        if len(token_ids) != 2 and token_id:
            hydrated = self._hydrate_market_metadata(token_id)
            if hydrated:
                outcomes = hydrated.get("outcomes") or outcomes
                token_ids = hydrated.get("token_ids") or token_ids
                features = {
                    **features,
                    "outcomes": outcomes,
                    "clob_token_ids": token_ids,
                    "outcome_prices": hydrated.get("outcome_prices") or features.get("outcome_prices"),
                    "question": hydrated.get("question") or features.get("question"),
                    "gamma_market_id": hydrated.get("gamma_market_id") or features.get("gamma_market_id"),
                }
        if len(token_ids) != 2:
            self._record_reject(row, "missing_clob_token_ids", {"tokens": token_ids})
            return "skipped"

        entry_index = 0 if side == "BUY" else 1
        if token_id != str(token_ids[entry_index]):
            self._record_reject(
                row,
                "side_token_mismatch",
                {"side": side, "token_id": token_id, "token_ids": token_ids},
            )
            return "skipped"

        if self.trade_log.has_active_trade_for_market(market_id, token_id=token_id):
            self._record_reject(row, "active_trade_exists", {"token_id": token_id})
            return "skipped"
        if self.trade_log.has_recent_close_for_market(
            market_id,
            hours=self.cfg.reentry_cooldown_hours,
            token_id=token_id,
        ):
            self._record_reject(row, "recent_close_cooldown", {"hours": self.cfg.reentry_cooldown_hours})
            return "skipped"
        if self.cfg.market_loss_cooldown_hours > 0:
            recent_market_losses = self.trade_log.count_recent_closes_for_market(
                market_id,
                hours=self.cfg.market_loss_cooldown_hours,
                statuses=("closed_stop_loss", "resolved_loss"),
            )
            if recent_market_losses > 0:
                self._record_reject(
                    row,
                    "recent_market_loss_cooldown",
                    {
                        "hours": self.cfg.market_loss_cooldown_hours,
                        "recent_market_losses": recent_market_losses,
                        "lesson": "do_not_reenter_same_market_immediately_after_loss",
                    },
                )
                return "skipped"
        if (
            not self.execute
            and self.trade_log.has_recent_decision_journal(
                agent="scanner_executor",
                market_id=market_id,
                token_id=token_id,
                decisions=("SHADOW_ENTER", "SHADOW_QUOTE"),
                minutes=self.cfg.shadow_entry_cooldown_minutes,
            )
        ):
            self._record_reject(
                row,
                "shadow_recent_entry_exists",
                {"minutes": self.cfg.shadow_entry_cooldown_minutes},
            )
            return "skipped"

        estimated_prob = proposal.probability
        regime_features = self._regime_snapshot(row, features)
        if self.cfg.enforce_regime_router and not bool(regime_features.get("regime_family_allowed", True)):
            self._record_reject(row, "strategy_family_blocked_by_regime", regime_features)
            return "skipped"
        try:
            if hasattr(self.polymarket, "_fillable_market_buy_with_quality"):
                live_price, fillable_usdc, avg_price, book_quality = (
                    self.polymarket._fillable_market_buy_with_quality(
                        token_id,
                        self.cfg.position_size_usdc,
                    )
                )
            else:
                live_price, fillable_usdc, avg_price = self.polymarket._fillable_market_buy(
                    token_id,
                    self.cfg.position_size_usdc,
                )
                book_quality = {}
        except Exception as exc:
            self._record_reject(row, "orderbook_not_executable", {"error": str(exc)[:240]})
            return "skipped"

        executable_entry_price = _safe_float(avg_price, live_price) or live_price
        if (
            self._learning_guard_active()
            and not (
                self.cfg.learning_min_entry_price
                <= executable_entry_price
                < self.cfg.learning_max_entry_price
            )
            and not (
                self.cfg.learning_allow_proven_price_override
                and self._has_proven_override(row, features)
            )
        ):
            self._record_reject(
                row,
                "today_lesson_price_band_blocked",
                {
                    "live_entry_price": round(live_price, 4),
                    "avg_entry_price": round(executable_entry_price, 4),
                    "min_entry_price": self.cfg.learning_min_entry_price,
                    "max_entry_price": self.cfg.learning_max_entry_price,
                    "lesson": "2026-05-21_live_best_band_0.40_0.50",
                    **_round_float_payload(book_quality),
                },
            )
            return "skipped"
        scanner_entry_price = _safe_float(features.get("selected_entry_price"), 0.0)
        if scanner_entry_price > 0:
            entry_drift = (executable_entry_price - scanner_entry_price) / scanner_entry_price
            if entry_drift > self.cfg.max_entry_drift_pct:
                self._record_reject(
                    row,
                    "entry_price_drift_too_high",
                    {
                        "scanner_entry_price": round(scanner_entry_price, 4),
                        "live_entry_price": round(live_price, 4),
                        "avg_entry_price": round(executable_entry_price, 4),
                        "entry_drift": round(entry_drift, 4),
                        "max_entry_drift_pct": self.cfg.max_entry_drift_pct,
                        **_round_float_payload(book_quality),
                    },
                )
                return "skipped"

        council = self.decision_council.review_entry(
            features=features,
            score=score,
            live_entry_price=live_price,
            avg_entry_price=executable_entry_price,
            fillable_usdc=fillable_usdc,
            book_quality=book_quality,
            signal_source=str(row.get("signal_source") or features.get("signal_source") or "market_scanner"),
        )
        estimated_prob = council.internal_probability or estimated_prob
        raw_ev = council.raw_ev
        net_ev = council.net_ev
        if council.reason == "raw_ev_below_council_min":
            self._record_reject(
                row,
                "raw_ev_below_council_min",
                {
                    "estimated_win_probability": round(estimated_prob, 4),
                    "live_entry_price": round(live_price, 4),
                    "avg_entry_price": round(executable_entry_price, 4),
                    "raw_ev": round(raw_ev, 4),
                    **_round_float_payload(book_quality),
                    **council.features,
                },
            )
            return "skipped"
        if not council.approved:
            self._record_reject(
                row,
                council.reason,
                {
                    "estimated_win_probability": round(estimated_prob, 4),
                    "live_entry_price": round(live_price, 4),
                    "avg_entry_price": round(executable_entry_price, 4),
                    "raw_ev": round(raw_ev, 4),
                    "net_ev": round(net_ev, 4),
                    "round_trip_cost_pct": self.cfg.round_trip_cost_pct,
                    **_round_float_payload(book_quality),
                    **council.features,
                },
            )
            return "skipped"

        maker_shadow_candidate = (
            not self.execute
            and self.cfg.prefer_maker_for_fast_markets
            and _is_fast_market(features, market_type=str(row.get("market_type") or ""))
        )
        best_bid = _safe_float(book_quality.get("best_bid"), None)
        if not maker_shadow_candidate and best_bid is not None and executable_entry_price > 0:
            immediate_exit_loss_pct = max(0.0, 1.0 - (best_bid / executable_entry_price))
            if immediate_exit_loss_pct >= self.cfg.max_immediate_exit_loss_pct:
                self._record_reject(
                    row,
                    "taker_entry_below_stop_on_spread",
                    {
                        "best_bid": round(best_bid, 4),
                        "avg_entry_price": round(executable_entry_price, 4),
                        "immediate_exit_loss_pct": round(immediate_exit_loss_pct, 4),
                        "max_immediate_exit_loss_pct": self.cfg.max_immediate_exit_loss_pct,
                        **_round_float_payload(book_quality),
                        **council.features,
                    },
                )
                return "skipped"

        risk_reason = self.risk_gate.reason()
        if risk_reason:
            self._record_reject(row, "risk_gate_blocked", {"risk_reason": risk_reason})
            return "skipped"

        balance = None
        try:
            balance = self.polymarket.get_usdc_balance()
        except Exception:
            pass
        sizing = kelly_size_usdc(
            balance_usdc=balance,
            win_probability=estimated_prob,
            entry_price=executable_entry_price,
            fallback_amount_usdc=min(self.cfg.position_size_usdc, fillable_usdc),
        )
        amount_usdc = min(fillable_usdc, sizing.amount_usdc or self.cfg.position_size_usdc)
        if amount_usdc <= 0:
            self._record_reject(row, "kelly_size_zero", sizing.features())
            return "skipped"
        execution_features = {
            "trade_proposal": proposal.to_dict(),
            "proposal_source_agent": proposal.source_agent,
            "proposal_source_strategy": proposal.source_strategy,
            "proposal_strategy_type": proposal.strategy_type,
            "proposal_exit_policy": proposal.exit_policy,
            "meta_timing": meta_timing,
            "timing_override": timing_override,
            "live_entry_price": round(live_price, 4),
            "avg_entry_price": round(executable_entry_price, 4),
            "raw_ev": round(raw_ev, 4),
            "net_ev": round(net_ev, 4),
            "round_trip_cost_pct": self.cfg.round_trip_cost_pct,
            **_round_float_payload(book_quality),
            **council.features,
            **sizing.features(),
            **proof_features,
            **regime_features,
            "learning_guard_enabled": self._learning_guard_active(),
            "learning_preferred_side": self.cfg.learning_preferred_side,
            "learning_min_entry_price": self.cfg.learning_min_entry_price,
            "learning_max_entry_price": self.cfg.learning_max_entry_price,
        }

        if maker_shadow_candidate:
            maker_plan = self._maker_shadow_plan(book_quality)
            if maker_plan is None:
                self._record_reject(
                    row,
                    "maker_quote_no_room",
                    {
                        "maker_tick_size": self.cfg.maker_tick_size,
                        "maker_min_profit_cents": self.cfg.maker_min_profit_cents,
                        **_round_float_payload(book_quality),
                    },
                )
                return "skipped"
            self._record_maker_quote(
                row,
                maker_bid=maker_plan["maker_bid"],
                maker_ask=maker_plan["maker_ask"],
                amount_usdc=amount_usdc,
                raw_ev=raw_ev,
                net_ev=net_ev,
                extra={**execution_features, **maker_plan},
            )
            return "shadow"

        anchor_price = live_price if side == "BUY" else 1.0 - live_price
        pending_id = self.trade_log.insert_pending(
            cycle_id=f"scanner_executor:{decision_id}",
            market_id=market_id,
            token_id=token_id,
            side=side,
            price=anchor_price,
            size_usdc=amount_usdc,
            confidence=estimated_prob,
        )
        recommendation = TradeRecommendation(
            price=anchor_price,
            size_fraction=0.0,
            side=side,
            confidence=estimated_prob,
            amount_usdc=amount_usdc,
        )
        market = self._market_tuple(features, market_id, outcomes, token_ids)

        if not self.execute:
            self.trade_log.mark(
                pending_id,
                "shadow_filled",
                response={
                    "shadow": True,
                    "source_decision_id": decision_id,
                    "entry_mode": "scanner_executor",
                    "raw_ev": round(raw_ev, 4),
                    "net_ev": round(net_ev, 4),
                    "meta_timing": meta_timing,
                    "timing_override": timing_override,
                    **sizing.features(),
                },
                error="SHADOW: scanner_executor",
                price=live_price,
                size_usdc=amount_usdc,
            )
            self._record_approval(row, "shadow_executed", live_price, amount_usdc, raw_ev, net_ev, execution_features)
            return "shadow"

        try:
            response = self.polymarket.execute_market_order(market, recommendation)
        except Exception as exc:
            self.trade_log.mark(pending_id, "failed", error=f"execute_market_order raised: {exc}")
            self._record_reject(row, "execute_market_order_raised", {"error": str(exc)[:300]})
            return "failed"

        if not response or response.get("status") not in {"matched", "filled"}:
            self.trade_log.mark(pending_id, "failed", response=response, error="entry not matched")
            self._record_reject(row, "entry_not_matched", {"response": response})
            return "failed"

        entry_price = _safe_float(response.get("order_avg_price_estimate"), live_price)
        entry_size = _safe_float(response.get("amount_usdc"), amount_usdc)
        self.trade_log.mark(
            pending_id,
            FILLED,
            response={
                **(response or {}),
                "source_decision_id": decision_id,
                "entry_mode": "scanner_executor",
                "raw_ev": round(raw_ev, 4),
                "net_ev": round(net_ev, 4),
                "meta_timing": meta_timing,
                "timing_override": timing_override,
                **sizing.features(),
            },
            price=entry_price,
            size_usdc=entry_size,
        )
        self._record_approval(row, "live_executed", entry_price, entry_size, raw_ev, net_ev, execution_features)
        notify_trade(
            event="fill",
            agent="scanner_executor",
            market_id=market_id,
            side=side,
            price=entry_price,
            size_usdc=entry_size,
            reason=f"scanner_decision={decision_id} score={score:.3f} ev={raw_ev:.3f} net={net_ev:.3f} {question[:80]}",
            balance_usdc=_safe_balance(self.polymarket),
        )
        return "executed"

    def _market_tuple(self, features: dict, market_id: str, outcomes: list, token_ids: list):
        prices = _as_list(features.get("outcome_prices")) or [
            features.get("yes_price", 0.5),
            features.get("no_price", 0.5),
        ]
        metadata = {
            "id": str(features.get("gamma_market_id") or market_id),
            "conditionId": market_id,
            "question": str(features.get("question") or market_id),
            "outcomes": repr([str(x) for x in outcomes[:2]]),
            "clob_token_ids": repr([str(x) for x in token_ids[:2]]),
            "outcome_prices": repr([str(x) for x in prices[:2]]),
        }
        return (_DocMarket(metadata), 0.0)

    def _hydrate_market_metadata(self, token_id: str) -> dict:
        getter = getattr(self.polymarket, "get_market", None)
        if not callable(getter):
            return {}
        try:
            market = getter(token_id)
            metadata = market[0].dict().get("metadata", {})
            return {
                "gamma_market_id": metadata.get("id"),
                "question": metadata.get("question"),
                "outcomes": _as_list(metadata.get("outcomes")),
                "token_ids": _as_list(metadata.get("clob_token_ids")),
                "outcome_prices": _as_list(metadata.get("outcome_prices")),
            }
        except Exception as exc:
            logger.info("scanner_executor metadata hydration failed for token=%s: %s", token_id[:18], exc)
            return {}

    def _min_score_for_decision(self, row: dict, features: dict) -> float:
        if not bool(features.get("estimated_win_probability_calibrated")):
            return self.cfg.min_score
        source = str(features.get("estimated_win_probability_source") or "")
        signal_source = str(row.get("signal_source") or features.get("signal_source") or "")
        proven_sources = (
            "alphainsider_proven_family_plus_crypto_tape",
            "wallet_external_winrate",
        )
        proven_markers = (
            "alphainsider_proven",
            "proven_wallet",
        )
        if source in proven_sources or any(marker in signal_source for marker in proven_markers):
            return min(self.cfg.min_score, self.cfg.min_proven_calibrated_score)
        return self.cfg.min_score

    def _has_proven_override(self, row: dict, features: dict) -> bool:
        source = str(features.get("estimated_win_probability_source") or "")
        signal_source = str(row.get("signal_source") or features.get("signal_source") or "")
        return (
            source in {
                "alphainsider_proven_family_plus_crypto_tape",
                "wallet_external_winrate",
            }
            or "alphainsider_proven" in signal_source
            or "proven_wallet" in signal_source
        )

    def _record_reject(self, row: dict, reason: str, features: dict) -> None:
        row_features = _parse_features(row.get("features_json"))
        signal_source = str(row.get("signal_source") or row_features.get("signal_source") or "market_scanner")
        token_id = str((row_features.get("selected_token_id") or ""))
        action = str(row.get("action") or row_features.get("selected_side") or "")
        proof_features = self._proof_snapshot(row, row_features)
        regime_features = self._regime_snapshot(row, row_features)
        self.trade_log.insert_brain_decision(
            agent="scanner_executor",
            strategy="execute_scanner_trade_opportunity",
            decision_type="entry",
            market_id=str(row["market_id"]),
            token_id=token_id,
            approved=False,
            reason=reason,
            score=float(row.get("score") or 0.0),
            market_type="scanner_executor",
            features={
                "source_decision_id": int(row["id"]),
                "scanner_signal_source": signal_source,
                **proof_features,
                **regime_features,
                **features,
            },
            action=action,
            signal_source=signal_source,
        )
        self.trade_log.insert_decision_journal(
            decision_id=int(row["id"]),
            agent="scanner_executor",
            strategy="execute_scanner_trade_opportunity",
            market_id=str(row["market_id"]),
            token_id=token_id,
            action=action,
            decision="REJECT",
            reason=reason,
            signal_source=signal_source,
            market_price=_safe_float(row_features.get("selected_entry_price"), None),
            live_entry_price=_safe_float(features.get("avg_entry_price") or features.get("live_entry_price"), None),
            internal_probability=_safe_float(
                features.get("decision_council_internal_probability")
                or features.get("estimated_win_probability"),
                None,
            ),
            raw_ev=_safe_float(features.get("raw_ev") or features.get("decision_council_raw_ev"), None),
            net_ev=_safe_float(features.get("net_ev") or features.get("decision_council_net_ev"), None),
            score=float(row.get("score") or 0.0),
            mode=str(features.get("decision_council_mode") or ""),
            features={
                "source_decision_id": int(row["id"]),
                "question": row_features.get("question"),
                **proof_features,
                **regime_features,
                **features,
            },
        )
        logger.info(
            "scanner_executor skip: decision=%s market=%s reason=%s",
            row["id"], str(row["market_id"])[:20], reason,
        )
        self._remember_reject(str(row["market_id"]), reason)

    def _learning_guard_active(self) -> bool:
        if not self.cfg.learning_guard_enabled:
            return False
        ttl_hours = float(self.cfg.learning_guard_ttl_hours or 0.0)
        if ttl_hours <= 0:
            return True
        return (time.time() - self._learning_guard_started_ts) <= ttl_hours * 3600.0

    def _market_quarantine_remaining(self, market_id: str) -> float:
        until = float(self._market_quarantine_until.get(str(market_id)) or 0.0)
        remaining = until - time.time()
        if remaining <= 0:
            self._market_quarantine_until.pop(str(market_id), None)
            return 0.0
        return remaining

    def _remember_reject(self, market_id: str, reason: str) -> None:
        if reason == "market_recent_reject_quarantine":
            return
        threshold = int(self.cfg.repeat_reject_quarantine_threshold or 0)
        if threshold <= 0:
            return
        now = time.time()
        ttl = max(0, int(self.cfg.repeat_reject_cache_ttl_seconds or 0))
        key = (str(market_id), str(reason))
        count, first_ts = self._repeat_rejects.get(key, (0, now))
        if ttl and now - first_ts > ttl:
            count, first_ts = 0, now
        count += 1
        self._repeat_rejects[key] = (count, first_ts)
        if count >= threshold:
            quarantine_sec = max(0, int(self.cfg.repeat_reject_quarantine_seconds or 0))
            if quarantine_sec:
                self._market_quarantine_until[str(market_id)] = now + quarantine_sec
                logger.warning(
                    "scanner_executor quarantined market=%s reason=%s count=%s ttl=%ss",
                    str(market_id)[:20],
                    reason,
                    count,
                    quarantine_sec,
                )

    def _record_approval(
        self,
        row: dict,
        reason: str,
        entry_price: float,
        amount_usdc: float,
        raw_ev: float,
        net_ev: float,
        extra: dict,
    ) -> None:
        features = _parse_features(row.get("features_json"))
        signal_source = str(row.get("signal_source") or features.get("signal_source") or "market_scanner")
        token_id = str(features.get("selected_token_id") or "")
        action = str(features.get("selected_side") or row.get("action") or "")
        proof_features = self._proof_snapshot(row, features)
        regime_features = self._regime_snapshot(row, features)
        self.trade_log.insert_brain_decision(
            agent="scanner_executor",
            strategy="execute_scanner_trade_opportunity",
            decision_type="entry",
            market_id=str(row["market_id"]),
            token_id=token_id,
            approved=True,
            reason=reason,
            score=float(row.get("score") or 0.0),
            market_type="scanner_executor",
            features={
                "source_decision_id": int(row["id"]),
                "entry_price": round(entry_price, 4),
                "amount_usdc": round(amount_usdc, 4),
                "raw_ev": round(raw_ev, 4),
                "net_ev": round(net_ev, 4),
                "scanner_signal_source": signal_source,
                **proof_features,
                **regime_features,
                **extra,
            },
            action=action,
            signal_source=signal_source,
        )
        self.trade_log.insert_decision_journal(
            decision_id=int(row["id"]),
            agent="scanner_executor",
            strategy="execute_scanner_trade_opportunity",
            market_id=str(row["market_id"]),
            token_id=token_id,
            action=action,
            decision="ENTER" if reason == "live_executed" else "SHADOW_ENTER",
            reason=reason,
            signal_source=signal_source,
            market_price=_safe_float(features.get("selected_entry_price"), None),
            live_entry_price=entry_price,
            internal_probability=_safe_float(
                extra.get("decision_council_internal_probability")
                or extra.get("kelly_win_probability"),
                None,
            ),
            raw_ev=raw_ev,
            net_ev=net_ev,
            score=float(row.get("score") or 0.0),
            mode=str(extra.get("decision_council_mode") or ""),
            features={
                "source_decision_id": int(row["id"]),
                "question": features.get("question"),
                "amount_usdc": round(amount_usdc, 4),
                **proof_features,
                **regime_features,
                **extra,
            },
        )
        logger.info(
            "scanner_executor %s: decision=%s market=%s price=%.4f size=%.4f ev=%.4f",
            reason, row["id"], str(row["market_id"])[:20], entry_price, amount_usdc, raw_ev,
        )

    def _maker_shadow_plan(self, book_quality: dict) -> Optional[dict]:
        best_bid = _safe_float(book_quality.get("best_bid"), None)
        best_ask = _safe_float(book_quality.get("best_ask"), None)
        if best_bid is None or best_ask is None or best_bid <= 0 or best_ask >= 1:
            return None
        maker_bid = round(best_bid + self.cfg.maker_tick_size, 4)
        maker_ask = round(best_ask - self.cfg.maker_tick_size, 4)
        profit = round(maker_ask - maker_bid, 4)
        if maker_bid <= 0 or maker_ask >= 1 or maker_bid >= maker_ask:
            return None
        if profit < self.cfg.maker_min_profit_cents:
            return None
        return {
            "maker_bid": maker_bid,
            "maker_ask": maker_ask,
            "maker_profit_cents": profit,
            "maker_tick_size": self.cfg.maker_tick_size,
            "maker_min_profit_cents": self.cfg.maker_min_profit_cents,
        }

    def _record_maker_quote(
        self,
        row: dict,
        *,
        maker_bid: float,
        maker_ask: float,
        amount_usdc: float,
        raw_ev: float,
        net_ev: float,
        extra: dict,
    ) -> None:
        features = _parse_features(row.get("features_json"))
        signal_source = str(row.get("signal_source") or features.get("signal_source") or "market_scanner")
        token_id = str(features.get("selected_token_id") or "")
        action = str(features.get("selected_side") or row.get("action") or "")
        proof_features = self._proof_snapshot(row, features)
        regime_features = self._regime_snapshot(row, features)
        payload = {
            "source_decision_id": int(row["id"]),
            "question": features.get("question"),
            "amount_usdc": round(amount_usdc, 4),
            "entry_style": "maker_first_shadow",
            **proof_features,
            **regime_features,
            **extra,
        }
        self.trade_log.insert_brain_decision(
            agent="scanner_executor",
            strategy="execute_scanner_trade_opportunity",
            decision_type="entry",
            market_id=str(row["market_id"]),
            token_id=token_id,
            approved=True,
            reason="shadow_maker_quoted",
            score=float(row.get("score") or 0.0),
            market_type="scanner_executor",
            features=payload,
            action=action,
            signal_source=signal_source,
        )
        self.trade_log.insert_decision_journal(
            decision_id=int(row["id"]),
            agent="scanner_executor",
            strategy="execute_scanner_trade_opportunity",
            market_id=str(row["market_id"]),
            token_id=token_id,
            action=action,
            decision="SHADOW_QUOTE",
            reason="shadow_maker_quoted",
            signal_source=signal_source,
            market_price=maker_bid,
            live_entry_price=maker_ask,
            internal_probability=_safe_float(
                extra.get("decision_council_internal_probability")
                or extra.get("kelly_win_probability"),
                None,
            ),
            raw_ev=raw_ev,
            net_ev=net_ev,
            score=float(row.get("score") or 0.0),
            mode=str(extra.get("decision_council_mode") or "maker_shadow"),
            features=payload,
        )
        logger.info(
            "scanner_executor shadow_maker_quoted: decision=%s market=%s bid=%.4f ask=%.4f ev=%.4f",
            row["id"], str(row["market_id"])[:20], maker_bid, maker_ask, raw_ev,
        )

    def _proof_snapshot(self, row: dict, features: dict) -> dict:
        """Attach local proof state from backtest/shadow scorecards.

        Calibrated external evidence can lead a trade, but every executor
        decision should still say whether our own shadow/backtest loop has
        promoted that strategy/source.  Operators can keep this as observability
        or enable ``SCANNER_EXECUTOR_REQUIRE_PROMOTABLE_STRATEGY`` as a hard
        live gate.
        """
        proof: dict = {}
        strategy = self._strategy_score()
        if strategy:
            proof.update(
                {
                    "proof_strategy_state": strategy.get("promotion_state"),
                    "proof_strategy_decisions": strategy.get("decisions"),
                    "proof_strategy_approvals": strategy.get("approvals"),
                    "proof_strategy_markout_samples": strategy.get("markout_samples"),
                    "proof_strategy_avg_markout_pct": strategy.get("avg_markout_pct"),
                    "proof_strategy_blockers": strategy.get("blockers") or [],
                }
            )
        else:
            proof["proof_strategy_state"] = "missing_scorecard"

        provider = self._provider_score(features, str(row.get("signal_source") or ""))
        if provider:
            proof.update(
                {
                    "proof_provider_source": provider.get("source"),
                    "proof_provider_matched": provider.get("matched"),
                    "proof_provider_winrate": provider.get("winrate"),
                    "proof_provider_wilson_lower": provider.get("wilson_lower"),
                }
            )
        else:
            proof["proof_provider_source"] = "missing_scorecard_match"
        return proof

    def _regime_snapshot(self, row: dict, features: dict) -> dict:
        family = family_from_signal(
            strategy_id=str(row.get("strategy") or ""),
            agent=str(row.get("agent") or ""),
            signal_source=str(row.get("signal_source") or features.get("signal_source") or ""),
            features=features,
        )
        return route_for_features(features).features_for_family(family)

    def _strategy_score(self) -> Optional[dict]:
        payload = _read_json(Path(self.cfg.strategy_scorecard_path))
        for row in payload.get("strategies") or []:
            if (
                str(row.get("agent")) == "scanner_executor"
                and str(row.get("strategy")) == "execute_scanner_trade_opportunity"
            ):
                return row
        return None

    def _provider_score(self, features: dict, signal_source: str) -> Optional[dict]:
        payload = _read_json(Path(self.cfg.provider_scorecard_path))
        providers = payload.get("providers") or []
        if not providers:
            return None
        wanted = _source_candidates(
            features.get("estimated_win_probability_source"),
            signal_source,
            features.get("evidence_route"),
        )
        for provider in providers:
            if str(provider.get("source")) in wanted:
                return provider
        return None

    def _candidate_agents(self) -> list[str]:
        agents = ["market_scanner"]
        agents.extend(str(agent).strip() for agent in self.cfg.candidate_agents if str(agent).strip())
        blocked = {
            "scanner_executor",
            "position_manager",
            "position_manager_llm",
            "trading_supervisor",
        }
        seen = set()
        agents = [
            agent
            for agent in agents
            if agent not in blocked and not (agent in seen or seen.add(agent))
        ]
        return agents or ["market_scanner"]

    def _heartbeat(self) -> None:
        try:
            p = Path(self.cfg.heartbeat_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
        except Exception:
            logger.debug("scanner_executor heartbeat failed", exc_info=True)


class ScannerExecutorDaemon:
    def __init__(self, db_path: Optional[str] = None):
        cfg = ScannerExecutorConfig.from_env()
        self.engine = ScannerExecutor(cfg=cfg, trade_log=TradeLog(db_path=db_path))
        self.cfg = cfg
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            signal.signal(signal.SIGTERM, lambda *_: self.stop())
            signal.signal(signal.SIGINT, lambda *_: self.stop())
        except (ValueError, OSError):
            pass
        logger.info(
            "ScannerExecutorDaemon: starting execute=%s poll=%ss",
            self.engine.execute,
            self.cfg.poll_seconds,
        )
        while not self._stop.is_set():
            try:
                stats = self.engine.run_once()
                if stats["seen"] or stats["executed"] or stats["skipped"]:
                    logger.info("scanner_executor cycle: %s", stats)
            except Exception:
                logger.exception("scanner_executor cycle failed")
            self._stop.wait(self.cfg.poll_seconds)
        logger.info("ScannerExecutorDaemon: exited")


def _parse_features(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _is_fast_market(features: dict, *, market_type: str = "") -> bool:
    haystack = " ".join(
        str(features.get(k) or "")
        for k in ("slug", "question", "horizon", "market_type")
    ).lower()
    market_type = market_type.lower()
    return (
        "5m" in haystack
        or "5 minute" in haystack
        or "5-minute" in haystack
        or "updown-5m" in haystack
        or "crypto_5m" in market_type
    )


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            try:
                parsed = ast.literal_eval(value)
            except Exception:
                return []
        return parsed if isinstance(parsed, list) else []
    return []


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round_float_payload(payload: dict) -> dict:
    return {
        key: round(value, 4) if isinstance(value, float) else value
        for key, value in (payload or {}).items()
    }


def _read_json(path: Path) -> dict:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _source_candidates(*values) -> set[str]:
    candidates: set[str] = set()
    for value in values:
        if not value:
            continue
        if isinstance(value, dict):
            for key in ("reason", "leader", "provider", "source", "source_id"):
                item = value.get(key)
                if item:
                    candidates.update(_source_candidates(item))
            continue
        for chunk in str(value).replace(";", ",").split(","):
            item = chunk.strip()
            if not item:
                continue
            candidates.add(item)
            if item.startswith("expert_solo:"):
                candidates.add(item.split("expert_solo:", 1)[1])
            if item.startswith("expert_conflict:"):
                candidates.add(item.split("expert_conflict:", 1)[1])
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute fresh market_scanner approvals")
    parser.add_argument("--once", action="store_true", help="run one executor cycle and exit")
    parser.add_argument("--db", default=None, help="override DB path")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.once:
        engine = ScannerExecutor(trade_log=TradeLog(db_path=args.db))
        print(json.dumps(engine.run_once(), indent=2))
        return 0
    ScannerExecutorDaemon(db_path=args.db).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
