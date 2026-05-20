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
from agents.application.risk_gate import RiskGate
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


@dataclass
class ScannerExecutorConfig:
    poll_seconds: int = 2
    max_decision_age_seconds: int = 180
    batch_limit: int = 50
    position_size_usdc: float = 1.0
    reserve_usdc: float = 0.0
    min_score: float = 0.80
    min_raw_ev: float = 0.04
    min_net_ev: float = 0.03
    round_trip_cost_pct: float = 0.04
    max_entry_drift_pct: float = 0.04
    require_timing_now: bool = True
    allow_wait_with_high_score: bool = False
    wait_override_min_score: float = 0.79
    max_open_positions: int = 4
    reentry_cooldown_hours: int = 12
    heartbeat_path: str = "/app/data/scanner_executor_heartbeat"

    @classmethod
    def from_env(cls) -> "ScannerExecutorConfig":
        return cls(
            poll_seconds=_env_int("SCANNER_EXECUTOR_POLL_SEC", 2),
            max_decision_age_seconds=_env_int("SCANNER_EXECUTOR_MAX_DECISION_AGE_SEC", 180),
            batch_limit=_env_int("SCANNER_EXECUTOR_BATCH_LIMIT", 50),
            position_size_usdc=_env_float("SCANNER_EXECUTOR_POSITION_SIZE_USDC", 1.0),
            reserve_usdc=_env_float("SCANNER_EXECUTOR_RESERVE_USDC", 0.0),
            min_score=_env_float("SCANNER_EXECUTOR_MIN_SCORE", 0.80),
            min_raw_ev=_env_float("SCANNER_EXECUTOR_MIN_RAW_EV", 0.04),
            min_net_ev=_env_float("SCANNER_EXECUTOR_MIN_NET_EV", 0.03),
            round_trip_cost_pct=_env_float("SCANNER_EXECUTOR_ROUND_TRIP_COST_PCT", 0.04),
            max_entry_drift_pct=_env_float("SCANNER_EXECUTOR_MAX_ENTRY_DRIFT_PCT", 0.04),
            require_timing_now=_env_bool("SCANNER_EXECUTOR_REQUIRE_TIMING_NOW", True),
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
            heartbeat_path=os.getenv(
                "SCANNER_EXECUTOR_HEARTBEAT_PATH",
                "/app/data/scanner_executor_heartbeat",
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

    def run_once(self) -> dict:
        stats = {
            "seen": 0,
            "executed": 0,
            "shadow": 0,
            "skipped": 0,
            "failed": 0,
        }
        rows = self.trade_log.recent_scanner_trade_opportunities(
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
        side = str(features.get("selected_side") or row.get("action") or "").upper()
        token_id = str(features.get("selected_token_id") or row.get("token_id") or "")
        question = str(features.get("question") or market_id)

        meta_timing = str(features.get("meta_timing") or "")
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
        if score < self.cfg.min_score:
            self._record_reject(row, "score_below_executor_min", {"score": score})
            return "skipped"
        if side not in {"BUY", "SELL"} or not token_id:
            self._record_reject(row, "missing_execution_metadata", {"side": side, "token_id": bool(token_id)})
            return "skipped"

        outcomes = _as_list(features.get("outcomes")) or ["Yes", "No"]
        token_ids = _as_list(features.get("clob_token_ids"))
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

        estimated_prob = _safe_float(features.get("estimated_win_probability"), score)
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
        }

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
                FILLED,
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

    def _record_reject(self, row: dict, reason: str, features: dict) -> None:
        row_features = _parse_features(row.get("features_json"))
        signal_source = str(row.get("signal_source") or row_features.get("signal_source") or "market_scanner")
        token_id = str((row_features.get("selected_token_id") or ""))
        action = str(row.get("action") or row_features.get("selected_side") or "")
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
                **features,
            },
        )
        logger.info(
            "scanner_executor skip: decision=%s market=%s reason=%s",
            row["id"], str(row["market_id"])[:20], reason,
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
                **extra,
            },
        )
        logger.info(
            "scanner_executor %s: decision=%s market=%s price=%.4f size=%.4f ev=%.4f",
            reason, row["id"], str(row["market_id"])[:20], entry_price, amount_usdc, raw_ev,
        )

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
