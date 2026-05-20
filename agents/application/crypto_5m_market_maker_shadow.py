"""Shadow market-maker probe for crypto 5-minute up/down markets.

This agent does not place orders.  It asks a narrower question:
"If we were a tiny maker on both sides, is the current book wide and deep
enough to quote a 1-2c spread without becoming obvious exit liquidity?"

The output is auditable `brain_decisions` + `decision_journal` rows.  Promotion
to live requires shadow evidence, not screenshots or claims.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agents.application.crypto_exchange_tape import CryptoExchangeTapeClient
from agents.application.trade_log import TradeLog

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class MakerShadowConfig:
    poll_seconds: int = 2
    universe_limit: int = 80
    route_agents: tuple[str, ...] = ("btc_5min", "scalper", "unassigned_5min")
    assets: tuple[str, ...] = ("btc", "eth", "sol", "xrp", "doge", "bnb")
    require_eligible: bool = False
    max_orderbook_age_sec: float = 8.0
    quote_size_usdc: float = 1.0
    tick_size: float = 0.01
    target_profit_cents: float = 0.02
    min_profit_cents: float = 0.01
    min_bid_depth_usdc: float = 20.0
    min_ask_depth_usdc: float = 20.0
    max_spread_pct: float = 0.16
    min_seconds_to_expiry: int = 45
    max_seconds_to_expiry: int = 600
    min_mid_price: float = 0.35
    max_mid_price: float = 0.65
    max_pair_ask_sum: float = 1.08
    external_tape_enabled: bool = True
    external_tape_min_confidence: float = 0.58
    min_directional_edge_pct: float = 0.015
    quote_both_when_neutral: bool = True
    heartbeat_path: str = "/app/data/crypto_5m_market_maker_shadow_heartbeat"

    @classmethod
    def from_env(cls) -> "MakerShadowConfig":
        return cls(
            poll_seconds=_env_int("CRYPTO_5M_MM_SHADOW_POLL_SEC", 2),
            universe_limit=_env_int("CRYPTO_5M_MM_SHADOW_UNIVERSE_LIMIT", 80),
            route_agents=_env_tuple(
                "CRYPTO_5M_MM_SHADOW_ROUTE_AGENTS",
                ("btc_5min", "scalper", "unassigned_5min"),
            ),
            assets=_env_tuple(
                "CRYPTO_5M_MM_SHADOW_ASSETS",
                ("btc", "eth", "sol", "xrp", "doge", "bnb"),
            ),
            require_eligible=_env_bool("CRYPTO_5M_MM_SHADOW_REQUIRE_ELIGIBLE", False),
            max_orderbook_age_sec=_env_float("CRYPTO_5M_MM_SHADOW_MAX_BOOK_AGE_SEC", 8.0),
            quote_size_usdc=_env_float("CRYPTO_5M_MM_SHADOW_QUOTE_SIZE_USDC", 1.0),
            tick_size=_env_float("CRYPTO_5M_MM_SHADOW_TICK_SIZE", 0.01),
            target_profit_cents=_env_float("CRYPTO_5M_MM_SHADOW_TARGET_PROFIT_CENTS", 0.02),
            min_profit_cents=_env_float("CRYPTO_5M_MM_SHADOW_MIN_PROFIT_CENTS", 0.01),
            min_bid_depth_usdc=_env_float("CRYPTO_5M_MM_SHADOW_MIN_BID_DEPTH_USDC", 20.0),
            min_ask_depth_usdc=_env_float("CRYPTO_5M_MM_SHADOW_MIN_ASK_DEPTH_USDC", 20.0),
            max_spread_pct=_env_float("CRYPTO_5M_MM_SHADOW_MAX_SPREAD_PCT", 0.16),
            min_seconds_to_expiry=_env_int("CRYPTO_5M_MM_SHADOW_MIN_SECONDS_TO_EXPIRY", 45),
            max_seconds_to_expiry=_env_int("CRYPTO_5M_MM_SHADOW_MAX_SECONDS_TO_EXPIRY", 600),
            min_mid_price=_env_float("CRYPTO_5M_MM_SHADOW_MIN_MID_PRICE", 0.35),
            max_mid_price=_env_float("CRYPTO_5M_MM_SHADOW_MAX_MID_PRICE", 0.65),
            max_pair_ask_sum=_env_float("CRYPTO_5M_MM_SHADOW_MAX_PAIR_ASK_SUM", 1.08),
            external_tape_enabled=_env_bool("CRYPTO_5M_MM_SHADOW_EXTERNAL_TAPE_ENABLED", True),
            external_tape_min_confidence=_env_float("CRYPTO_5M_MM_SHADOW_EXTERNAL_TAPE_MIN_CONFIDENCE", 0.58),
            min_directional_edge_pct=_env_float("CRYPTO_5M_MM_SHADOW_MIN_DIRECTIONAL_EDGE_PCT", 0.015),
            quote_both_when_neutral=_env_bool("CRYPTO_5M_MM_SHADOW_QUOTE_BOTH_WHEN_NEUTRAL", True),
            heartbeat_path=os.getenv(
                "CRYPTO_5M_MM_SHADOW_HEARTBEAT_PATH",
                "/app/data/crypto_5m_market_maker_shadow_heartbeat",
            ),
        )


@dataclass(frozen=True)
class QuotePlan:
    token_id: str
    outcome: str
    approved: bool
    reason: str
    maker_bid: Optional[float]
    maker_ask: Optional[float]
    profit_cents: float
    score: float
    features: dict = field(default_factory=dict)


def evaluate_quote(snapshot: dict, *, outcome: str, cfg: MakerShadowConfig) -> QuotePlan:
    token_id = str(snapshot.get("token_id") or "")
    best_bid = _float(snapshot.get("best_bid"))
    best_ask = _float(snapshot.get("best_ask"))
    spread_pct = _float(snapshot.get("spread_pct"))
    bid_depth = _float(snapshot.get("bid_depth_usdc")) or 0.0
    ask_depth = _float(snapshot.get("ask_depth_usdc")) or 0.0
    mid = _float(snapshot.get("mid"))
    if mid is None and best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2.0
    features = {
        "token_id": token_id,
        "outcome": outcome,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_pct": spread_pct,
        "bid_depth_usdc": bid_depth,
        "ask_depth_usdc": ask_depth,
        "quote_size_usdc": cfg.quote_size_usdc,
    }
    if not token_id or best_bid is None or best_ask is None or best_bid <= 0 or best_ask >= 1 or best_bid >= best_ask:
        return QuotePlan(token_id, outcome, False, "invalid_book", None, None, 0.0, 0.0, features)
    if mid is None or mid < cfg.min_mid_price or mid > cfg.max_mid_price:
        return QuotePlan(token_id, outcome, False, "mid_outside_safe_band", None, None, 0.0, 0.0, features)
    if bid_depth < cfg.min_bid_depth_usdc:
        return QuotePlan(token_id, outcome, False, "exit_bid_depth_too_low", None, None, 0.0, 0.0, features)
    if ask_depth < cfg.min_ask_depth_usdc:
        return QuotePlan(token_id, outcome, False, "ask_depth_too_low", None, None, 0.0, 0.0, features)
    if spread_pct is not None and spread_pct > cfg.max_spread_pct:
        return QuotePlan(token_id, outcome, False, "spread_pct_too_wide", None, None, 0.0, 0.0, features)

    maker_bid = round(best_bid + cfg.tick_size, 4)
    maker_ask = round(best_ask - cfg.tick_size, 4)
    profit = round(maker_ask - maker_bid, 4)
    features.update({"maker_bid": maker_bid, "maker_ask": maker_ask, "profit_cents": profit})
    if maker_bid <= 0 or maker_ask >= 1 or maker_bid >= maker_ask:
        return QuotePlan(token_id, outcome, False, "no_quote_room_after_tick", maker_bid, maker_ask, profit, 0.0, features)
    if profit < cfg.min_profit_cents:
        return QuotePlan(token_id, outcome, False, "profit_below_min", maker_bid, maker_ask, profit, 0.0, features)

    profit_score = min(1.0, profit / max(cfg.target_profit_cents, 1e-9))
    depth_score = min(1.0, min(bid_depth, ask_depth) / max(cfg.min_bid_depth_usdc, cfg.min_ask_depth_usdc, 1e-9))
    mid_score = max(0.0, 1.0 - abs(mid - 0.5) * 2.0)
    spread_score = 1.0
    if spread_pct is not None and cfg.max_spread_pct > 0:
        spread_score = max(0.0, 1.0 - spread_pct / cfg.max_spread_pct)
    score = round(0.40 * profit_score + 0.25 * depth_score + 0.20 * mid_score + 0.15 * spread_score, 4)
    features["maker_shadow_score"] = score
    return QuotePlan(token_id, outcome, True, "shadow_quote_candidate", maker_bid, maker_ask, profit, score, features)


class Crypto5mMarketMakerShadow:
    def __init__(
        self,
        *,
        cfg: Optional[MakerShadowConfig] = None,
        trade_log: Optional[TradeLog] = None,
        db_path: Optional[str] = None,
        crypto_tape_client: Optional[CryptoExchangeTapeClient] = None,
    ):
        self.cfg = cfg or MakerShadowConfig.from_env()
        self.trade_log = trade_log or TradeLog(db_path=db_path)
        self.crypto_tape = crypto_tape_client or CryptoExchangeTapeClient()
        self._processed: set[str] = set()

    def run_once(self) -> dict:
        stats = {
            "candidates": 0,
            "markets": 0,
            "quotes": 0,
            "approved": 0,
            "rejected": 0,
            "skipped": 0,
        }
        rows = self.trade_log.list_market_universe(
            horizon="5m",
            limit=self.cfg.universe_limit,
        )
        now = time.time()
        for row in rows:
            stats["candidates"] += 1
            route_agent = str(row.get("route_agent") or "")
            asset = str(row.get("asset") or "").lower()
            if route_agent not in self.cfg.route_agents or asset not in self.cfg.assets:
                stats["skipped"] += 1
                continue
            if self.cfg.require_eligible and not row.get("eligible"):
                stats["skipped"] += 1
                continue
            if not row.get("accepting_orders"):
                stats["skipped"] += 1
                continue
            seconds_to_expiry = int(row.get("period_ts") or 0) + 300 - int(now)
            if seconds_to_expiry < self.cfg.min_seconds_to_expiry or seconds_to_expiry > self.cfg.max_seconds_to_expiry:
                stats["skipped"] += 1
                continue
            stats["markets"] += 1
            plans = self._evaluate_market(row, seconds_to_expiry=seconds_to_expiry)
            for plan in plans:
                stats["quotes"] += 1
                if plan.approved:
                    stats["approved"] += 1
                else:
                    stats["rejected"] += 1
                self._record(row, plan, seconds_to_expiry=seconds_to_expiry)
        if stats["markets"] == 0:
            self._record_cycle_reject("no_candidate_in_time_window", stats)
        self._heartbeat()
        return stats

    def _evaluate_market(self, row: dict, *, seconds_to_expiry: int) -> list[QuotePlan]:
        up_token = str(row.get("up_token") or "")
        down_token = str(row.get("down_token") or "")
        up = self.trade_log.latest_orderbook_snapshot(
            up_token,
            max_age_seconds=self.cfg.max_orderbook_age_sec,
        ) if up_token else None
        down = self.trade_log.latest_orderbook_snapshot(
            down_token,
            max_age_seconds=self.cfg.max_orderbook_age_sec,
        ) if down_token else None
        if up is None or down is None:
            return [
                QuotePlan(up_token or down_token, "pair", False, "missing_fresh_pair_book", None, None, 0.0, 0.0, {
                    "up_token": up_token,
                    "down_token": down_token,
                    "has_up_book": up is not None,
                    "has_down_book": down is not None,
                    "seconds_to_expiry": seconds_to_expiry,
                })
            ]
        pair_ask_sum = (_float(up.get("best_ask")) or 1.0) + (_float(down.get("best_ask")) or 1.0)
        pair_features = {
            "slug": row.get("slug"),
            "asset": row.get("asset"),
            "period_ts": row.get("period_ts"),
            "seconds_to_expiry": seconds_to_expiry,
            "pair_ask_sum": round(pair_ask_sum, 4),
        }
        if pair_ask_sum > self.cfg.max_pair_ask_sum:
            return [
                QuotePlan(str(row.get("up_token") or ""), "up", False, "pair_ask_sum_too_high", None, None, 0.0, 0.0, pair_features),
                QuotePlan(str(row.get("down_token") or ""), "down", False, "pair_ask_sum_too_high", None, None, 0.0, 0.0, pair_features),
            ]
        directional = self._directional_context(row)
        pair_features.update(directional)
        plans = [
            evaluate_quote({**dict(up), **{"token_id": up_token}}, outcome="up", cfg=self.cfg),
            evaluate_quote({**dict(down), **{"token_id": down_token}}, outcome="down", cfg=self.cfg),
        ]
        plans = [self._apply_directional_edge(plan, directional) for plan in plans]
        return [
            QuotePlan(
                p.token_id,
                p.outcome,
                p.approved,
                p.reason,
                p.maker_bid,
                p.maker_ask,
                p.profit_cents,
                p.score,
                {**pair_features, **p.features},
            )
            for p in plans
        ]

    def _directional_context(self, row: dict) -> dict:
        if not self.cfg.external_tape_enabled:
            return {"directional_status": "disabled"}
        asset = str(row.get("asset") or "").lower()
        question = str(row.get("question") or f"{asset} up or down").strip()
        try:
            signal = self.crypto_tape.analyze_question(question or asset)
        except Exception as exc:
            return {"directional_status": f"error:{type(exc).__name__}"}
        context = {
            "directional_status": "ok",
            "external_source": "crypto_exchange_tape",
            "external_direction": signal.direction,
            "external_probability": signal.probability,
            "external_confidence": signal.confidence,
            "external_reason": signal.reason,
            "external_features": signal.features,
        }
        if signal.direction == "bullish":
            context["fair_up_probability"] = float(signal.probability)
            context["fair_down_probability"] = 1.0 - float(signal.probability)
        elif signal.direction == "bearish":
            context["fair_up_probability"] = 1.0 - float(signal.probability)
            context["fair_down_probability"] = float(signal.probability)
        else:
            context["fair_up_probability"] = 0.5
            context["fair_down_probability"] = 0.5
        context["external_signal_strong"] = (
            signal.direction in {"bullish", "bearish"}
            and float(signal.confidence) >= self.cfg.external_tape_min_confidence
        )
        return context

    def _apply_directional_edge(self, plan: QuotePlan, directional: dict) -> QuotePlan:
        if not plan.approved:
            return plan
        if directional.get("directional_status") != "ok":
            return plan
        fair_key = "fair_up_probability" if plan.outcome == "up" else "fair_down_probability"
        fair_prob = _float(directional.get(fair_key))
        maker_bid = _float(plan.maker_bid)
        if fair_prob is None or maker_bid is None:
            return plan
        directional_edge = round(fair_prob - maker_bid, 4)
        features = {
            **plan.features,
            "fair_probability": round(fair_prob, 4),
            "directional_edge_pct": directional_edge,
            "directional_edge_required_pct": self.cfg.min_directional_edge_pct,
            "external_signal_strong": bool(directional.get("external_signal_strong")),
        }
        if directional.get("external_signal_strong"):
            if directional_edge < self.cfg.min_directional_edge_pct:
                return QuotePlan(
                    plan.token_id,
                    plan.outcome,
                    False,
                    "directional_edge_too_low",
                    plan.maker_bid,
                    plan.maker_ask,
                    plan.profit_cents,
                    min(plan.score, 0.49),
                    features,
                )
            edge_boost = min(0.12, max(0.0, directional_edge) * 1.5)
            return QuotePlan(
                plan.token_id,
                plan.outcome,
                True,
                "shadow_quote_candidate_with_directional_edge",
                plan.maker_bid,
                plan.maker_ask,
                plan.profit_cents,
                round(min(1.0, plan.score + edge_boost), 4),
                features,
            )
        if not self.cfg.quote_both_when_neutral and directional_edge < self.cfg.min_directional_edge_pct:
            return QuotePlan(
                plan.token_id,
                plan.outcome,
                False,
                "neutral_directional_edge_too_low",
                plan.maker_bid,
                plan.maker_ask,
                plan.profit_cents,
                min(plan.score, 0.49),
                features,
            )
        return QuotePlan(
            plan.token_id,
            plan.outcome,
            plan.approved,
            plan.reason,
            plan.maker_bid,
            plan.maker_ask,
            plan.profit_cents,
            plan.score,
            features,
        )

    def _record(self, row: dict, plan: QuotePlan, *, seconds_to_expiry: int) -> None:
        key = f"{row.get('slug')}:{plan.token_id}:{plan.reason}:{plan.maker_bid}:{plan.maker_ask}:{seconds_to_expiry // 10}"
        if key in self._processed:
            return
        self._processed.add(key)
        market_id = str(row.get("market_id") or row.get("slug") or "")
        approved = bool(plan.approved)
        features = {
            "question": row.get("question"),
            "slug": row.get("slug"),
            "asset": row.get("asset"),
            "horizon": row.get("horizon"),
            "shadow_only": True,
            "seconds_to_expiry": seconds_to_expiry,
            **plan.features,
        }
        decision_id = self.trade_log.insert_brain_decision(
            agent="crypto_5m_market_maker_shadow",
            strategy="spread_capture_shadow",
            decision_type="maker_quote_shadow",
            market_id=market_id,
            token_id=plan.token_id,
            approved=approved,
            reason=plan.reason,
            score=plan.score,
            market_type="crypto_5m_maker",
            asset=str(row.get("asset") or ""),
            features=features,
            action=f"QUOTE_{plan.outcome.upper()}",
            signal_source="crypto_5m_market_maker_shadow",
        )
        self.trade_log.insert_decision_journal(
            decision_id=decision_id,
            agent="crypto_5m_market_maker_shadow",
            strategy="spread_capture_shadow",
            market_id=market_id,
            token_id=plan.token_id,
            action=f"QUOTE_{plan.outcome.upper()}",
            decision="SHADOW_QUOTE" if approved else "REJECT",
            reason=plan.reason,
            signal_source="crypto_5m_market_maker_shadow",
            market_price=plan.maker_bid,
            live_entry_price=plan.maker_ask,
            internal_probability=None,
            raw_ev=plan.profit_cents,
            net_ev=plan.profit_cents,
            score=plan.score,
            mode="maker_shadow",
            features=features,
        )

    def _record_cycle_reject(self, reason: str, stats: dict) -> None:
        key = f"cycle:{reason}:{int(time.time()) // 60}"
        if key in self._processed:
            return
        self._processed.add(key)
        features = {
            "shadow_only": True,
            "route_agents": list(self.cfg.route_agents),
            "assets": list(self.cfg.assets),
            "min_seconds_to_expiry": self.cfg.min_seconds_to_expiry,
            "max_seconds_to_expiry": self.cfg.max_seconds_to_expiry,
            "max_orderbook_age_sec": self.cfg.max_orderbook_age_sec,
            **stats,
        }
        decision_id = self.trade_log.insert_brain_decision(
            agent="crypto_5m_market_maker_shadow",
            strategy="spread_capture_shadow",
            decision_type="maker_quote_shadow_cycle",
            market_id="crypto_5m_market_maker_shadow",
            token_id=None,
            approved=False,
            reason=reason,
            score=0.0,
            market_type="crypto_5m_maker",
            asset="crypto",
            features=features,
            action="NO_QUOTE",
            signal_source="crypto_5m_market_maker_shadow",
        )
        self.trade_log.insert_decision_journal(
            decision_id=decision_id,
            agent="crypto_5m_market_maker_shadow",
            strategy="spread_capture_shadow",
            market_id="crypto_5m_market_maker_shadow",
            token_id=None,
            action="NO_QUOTE",
            decision="REJECT",
            reason=reason,
            signal_source="crypto_5m_market_maker_shadow",
            score=0.0,
            mode="maker_shadow",
            features=features,
        )

    def _heartbeat(self) -> None:
        try:
            path = Path(self.cfg.heartbeat_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        except Exception:
            logger.debug("crypto_5m_mm_shadow heartbeat failed", exc_info=True)


class Crypto5mMarketMakerShadowDaemon:
    def __init__(self, db_path: Optional[str] = None):
        self.engine = Crypto5mMarketMakerShadow(db_path=db_path)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            signal.signal(signal.SIGTERM, lambda *_: self.stop())
            signal.signal(signal.SIGINT, lambda *_: self.stop())
        except (ValueError, OSError):
            pass
        while not self._stop.is_set():
            try:
                stats = self.engine.run_once()
                if stats["quotes"]:
                    logger.info("crypto_5m_mm_shadow cycle: %s", stats)
            except Exception:
                logger.exception("crypto_5m_mm_shadow cycle failed")
            self._stop.wait(self.engine.cfg.poll_seconds)


def _float(value) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    values = tuple(x.strip().lower() for x in raw.split(",") if x.strip())
    return values or default


def main() -> int:
    parser = argparse.ArgumentParser(description="Shadow maker probe for crypto 5m markets")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.once:
        engine = Crypto5mMarketMakerShadow(db_path=args.db)
        print(json.dumps(engine.run_once(), indent=2, sort_keys=True))
        return 0
    Crypto5mMarketMakerShadowDaemon(db_path=args.db).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
