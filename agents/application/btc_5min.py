"""BTC 5-Min Up/Down — multi-signal consensus agent for poly1.

Targets the 5-minute ``btc-updown-5m-{unix_ts}`` Polymarket binary:
every 5 minutes a new market opens, auto-resolved by Chainlink at the
end of the window.  Price is always ~0.50/0.50.

Strategy: independent signals (momentum, funding rate, RSI) estimate the
true short-window probability for Up or Down.  Entry is allowed only when the
internal brain probability clears the minimum threshold and exceeds the live
Polymarket entry price by a configured edge.

Unlike btc_daily this agent has **no exit management** — 5-min markets
auto-resolve, so open positions simply expire.  Position lifecycle:
  signal consensus → execute_market_order (BUY Up or SELL → buy Down)
  → auto-resolve after 5 min.

Side semantics (same convention as btc_daily / scalper):
  outcomes = ["Up", "Down"]
  BUY  → token_ids[0] (Up)  — when signals say bullish
  SELL → token_ids[1] (Down) — when signals say bearish
"""
from __future__ import annotations

import logging
import os
import signal as _signal
import threading
import time
import json
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agents.application.trade_log import TradeLog, BTC_5MIN_OPEN
from agents.application.btc_daily import CoinbasePriceFeed
from agents.application.vibe_analysis import rsi, composite_signal, funding_rate_regime
from agents.application.tavily import tavily_headlines
from agents.application.execution_safety import exitable_size_check
from agents.application.trading_policy import MAX_TRADES_PER_HOUR
from agents.utils.notify import notify_trade, _safe_balance


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


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


def _env_list(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    items: list[str] = []
    for item in raw.split(","):
        value = item.strip().lower()
        if value:
            items.append(value)
    return tuple(dict.fromkeys(items))


@dataclass
class Btc5MinConfig:
    """Env-driven config.  All values overridable via .env."""
    position_size_usdc: float = 1.5
    reserve_usdc: float = 3.0
    entry_window_start: int = 60       # seconds after period open
    entry_window_end: int = 180        # latest entry point
    momentum_pct: float = 0.0015       # 0.15% min move
    min_consensus: int = 2             # min agreeing signals
    news_veto: bool = True
    poll_sec: int = 3
    cooldown_sec: int = 300            # = one 5-min window
    max_hold_seconds: int = 120        # force exit before the 5m market resolves
    take_profit_pct: float = 0.05
    max_per_hour: int = MAX_TRADES_PER_HOUR
    heartbeat_path: str = "/app/data/btc_5min_heartbeat"
    assets: tuple[str, ...] = ("btc",)
    min_confidence: float = 0.55
    min_live_entry_price: float = 0.0
    max_live_entry_price: float = 0.90
    min_edge_pct: float = 0.02
    require_universe_top: bool = False
    min_universe_winrate: float = 0.52
    straddle_enabled: bool = False
    straddle_leg_usdc: float = 1.5
    straddle_max_pair_ask_sum: float = 1.04
    straddle_take_profit_pct: float = 0.03
    straddle_max_hold_seconds: int = 210
    straddle_min_seconds_to_expiry: int = 45
    straddle_max_entry_spread_pct: float = 0.30
    straddle_min_entry_price: float = 0.05
    straddle_min_bid_depth_usdc: float = 20.0

    @classmethod
    def from_env(cls) -> "Btc5MinConfig":
        return cls(
            position_size_usdc=_env_float("BTC_5MIN_POSITION_SIZE_USDC", 1.5),
            reserve_usdc=_env_float("BTC_5MIN_RESERVE_USDC", 3.0),
            entry_window_start=_env_int("BTC_5MIN_ENTRY_WINDOW_START", 60),
            entry_window_end=_env_int("BTC_5MIN_ENTRY_WINDOW_END", 180),
            momentum_pct=_env_float("BTC_5MIN_MOMENTUM_PCT", 0.0015),
            min_consensus=_env_int("BTC_5MIN_MIN_CONSENSUS", 2),
            news_veto=os.getenv("BTC_5MIN_NEWS_VETO", "true").lower() == "true",
            poll_sec=_env_int("BTC_5MIN_POLL_SEC", 3),
            cooldown_sec=_env_int("BTC_5MIN_COOLDOWN_SEC", 300),
            max_hold_seconds=_env_int("BTC_5MIN_MAX_HOLD_SECONDS", 120),
            take_profit_pct=_env_float("BTC_5MIN_TAKE_PROFIT_PCT", 0.05),
            max_per_hour=_env_int("BTC_5MIN_MAX_PER_HOUR", MAX_TRADES_PER_HOUR),
            heartbeat_path=os.getenv(
                "BTC_5MIN_HEARTBEAT_PATH", "/app/data/btc_5min_heartbeat"
            ),
            assets=_env_list("BTC_5MIN_ASSETS", "btc"),
            min_confidence=_env_float("BTC_5MIN_MIN_CONFIDENCE", 0.55),
            min_live_entry_price=_env_float("BTC_5MIN_MIN_LIVE_ENTRY_PRICE", 0.0),
            max_live_entry_price=_env_float("BTC_5MIN_MAX_LIVE_ENTRY_PRICE", 0.90),
            min_edge_pct=_env_float("BTC_5MIN_MIN_EDGE_PCT", 0.02),
            require_universe_top=os.getenv(
                "BTC_5MIN_REQUIRE_UNIVERSE_TOP", "false"
            ).lower() == "true",
            min_universe_winrate=_env_float("BTC_5MIN_MIN_UNIVERSE_WINRATE", 0.52),
            straddle_enabled=os.getenv(
                "BTC_5MIN_STRADDLE_ENABLED", "false"
            ).lower() == "true",
            straddle_leg_usdc=_env_float("BTC_5MIN_STRADDLE_LEG_USDC", 1.5),
            straddle_max_pair_ask_sum=_env_float("BTC_5MIN_STRADDLE_MAX_PAIR_ASK_SUM", 1.04),
            straddle_take_profit_pct=_env_float("BTC_5MIN_STRADDLE_TAKE_PROFIT_PCT", 0.03),
            straddle_max_hold_seconds=_env_int("BTC_5MIN_STRADDLE_MAX_HOLD_SECONDS", 210),
            straddle_min_seconds_to_expiry=_env_int("BTC_5MIN_STRADDLE_MIN_SECONDS_TO_EXPIRY", 45),
            straddle_max_entry_spread_pct=_env_float("BTC_5MIN_STRADDLE_MAX_ENTRY_SPREAD_PCT", 0.30),
            straddle_min_entry_price=_env_float("BTC_5MIN_STRADDLE_MIN_ENTRY_PRICE", 0.05),
            straddle_min_bid_depth_usdc=_env_float("BTC_5MIN_STRADDLE_MIN_BID_DEPTH_USDC", 20.0),
        )


# ---------------------------------------------------------------------------
# Signal dataclass
# ---------------------------------------------------------------------------

@dataclass
class SignalResult:
    """Output of a single signal computation."""
    name: str
    direction: str       # "bullish" | "bearish" | "skip"
    confidence: float    # 0.0 – 1.0
    weight: float = 1.0
    detail: str = ""


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

PERIOD_SEC = 300  # 5 minutes


def _current_period_ts() -> int:
    """Unix timestamp of the current 5-min window start (floored to 300s)."""
    now = int(time.time())
    return now - (now % PERIOD_SEC)


def _format_5min_slug(period_ts: int, asset: str = "btc") -> str:
    """Polymarket slug for a 5-min BTC up/down market."""
    return f"{asset.lower()}-updown-5m-{period_ts}"


ASSET_PRODUCTS = {
    "btc": "BTC-USD",
    "eth": "ETH-USD",
    "sol": "SOL-USD",
    "xrp": "XRP-USD",
    "doge": "DOGE-USD",
}


class AssetPriceFeed(CoinbasePriceFeed):
    def __init__(self, product: str, max_history_sec: int = 1800):
        super().__init__(max_history_sec=max_history_sec)
        self.product = product
        self.URL = f"https://api.coinbase.com/v2/prices/{product}/spot"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


# Funding rate helpers (OKX public endpoint, no auth required)
_FUNDING_CACHE: dict[str, tuple[float, float]] = {}  # "okx" → (ts, rate)
_FUNDING_CACHE_TTL = 120  # seconds


def _fetch_funding_rate() -> Optional[float]:
    """Fetch BTC perpetual funding rate from OKX (public, no auth).
    Returns the 8h funding rate as a float, or None on failure.
    """
    cache_key = "okx"
    cached = _FUNDING_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < _FUNDING_CACHE_TTL:
        return cached[1]
    try:
        url = "https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP"
        req = urllib.request.Request(url, headers={"User-Agent": "poly1-btc5min/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        rate = float(data["data"][0]["fundingRate"])
        _FUNDING_CACHE[cache_key] = (time.time(), rate)
        return rate
    except Exception as exc:
        logger.warning("btc_5min: funding rate fetch failed: %s", exc)
        return None


class Btc5MinEngine:
    """Multi-signal consensus engine for 5-min BTC up/down markets."""

    def __init__(
        self,
        polymarket,
        trade_log: TradeLog,
        risk_gate,
        feed: CoinbasePriceFeed,
        cfg: Btc5MinConfig,
        execute: bool = False,
        brain=None,
        meta_brain=None,
        asset: str = "btc",
    ):
        self.polymarket = polymarket
        self.trade_log = trade_log
        self.risk_gate = risk_gate
        self.feed = feed
        self.cfg = cfg
        self.execute = execute
        self.brain = brain
        self.meta_brain = meta_brain
        self.asset = asset.lower()
        self.shadow_ignore_risk_gate = (
            os.getenv("SHADOW_IGNORE_RISK_GATE", "false").lower() == "true"
        )
        self._market_cache: dict[str, dict] = {}
        self._last_entered_period: int = 0
        self._hour_trades: list[float] = []  # timestamps of trades in last hour
        self._bootstrap_feed_history()

    def _bootstrap_feed_history(self) -> None:
        """Seed 1m BTC candles so the daemon can trade immediately after restart."""
        if not hasattr(self.feed, "max_history_sec"):
            return
        if len(getattr(self.feed, "_samples", [])) >= 20:
            return
        try:
            url = (
                "https://api.exchange.coinbase.com/products/"
                f"{ASSET_PRODUCTS.get(self.asset, 'BTC-USD')}/candles"
                "?granularity=60"
            )
            req = urllib.request.Request(
                url, headers={"User-Agent": "poly1-btc5min/1.0"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                candles = json.loads(resp.read())
            samples = []
            for row in candles:
                if not isinstance(row, list) or len(row) < 5:
                    continue
                ts_sec = int(row[0])
                close = float(row[4])
                samples.append((ts_sec * 1000, close))
            samples.sort(key=lambda item: item[0])
            if samples:
                cutoff = int(time.time() * 1000) - self.feed.max_history_sec * 1000
                self.feed._samples = [(t, p) for t, p in samples if t >= cutoff]
                logger.info(
                    "btc_5min: bootstrapped %d 1m price samples",
                    len(self.feed._samples),
                )
        except Exception as exc:
            logger.warning("btc_5min: price bootstrap failed: %s", exc)

    # -------------------------------------------------------------- signals

    def _momentum_signal(self) -> SignalResult:
        """Late-entry momentum: check if BTC moved ≥threshold in recent window."""
        pct = self.feed.percent_change(120)  # 2 min lookback
        if pct is None:
            return SignalResult("momentum", "skip", 0.0, weight=2.0,
                                detail="no data")
        if abs(pct) < self.cfg.momentum_pct:
            return SignalResult("momentum", "skip", 0.0, weight=2.0,
                                detail=f"move {pct:+.4f} < threshold")
        direction = "bullish" if pct > 0 else "bearish"
        # Confidence scales with move size (0.15% → 0.55, 0.30% → 0.75)
        conf = min(0.90, 0.40 + abs(pct) / self.cfg.momentum_pct * 0.15)
        return SignalResult("momentum", direction, conf, weight=2.0,
                            detail=f"pct={pct:+.4f}")

    def _funding_signal(self) -> SignalResult:
        """Funding rate regime from OKX perpetual."""
        if self.asset != "btc":
            return SignalResult("funding", "skip", 0.0,
                                detail=f"not used for {self.asset}")
        rate = _fetch_funding_rate()
        if rate is None:
            return SignalResult("funding", "skip", 0.0,
                                detail="fetch failed")
        regime = funding_rate_regime(rate)
        sig = regime.get("signal", "skip")
        if sig == "skip":
            return SignalResult("funding", "skip", 0.0,
                                detail=f"regime={regime.get('regime')}")
        # Stronger deviation → higher confidence
        conf = min(0.80, 0.50 + abs(rate) * 500)
        return SignalResult("funding", sig, conf,
                            detail=f"rate={rate:.6f} regime={regime.get('regime')}")

    def _rsi_signal(self) -> SignalResult:
        """RSI on BTC price feed resampled to ~1-min closes."""
        samples = self.feed._samples
        if len(samples) < 20:
            return SignalResult("rsi", "skip", 0.0,
                                detail=f"only {len(samples)} samples")
        # Resample to ~60s candles: take last sample in each 60s bucket
        buckets: dict[int, float] = {}
        for ts_ms, px in samples:
            bucket = ts_ms // 60000
            buckets[bucket] = px
        closes = [buckets[k] for k in sorted(buckets)]
        if len(closes) < 15:
            return SignalResult("rsi", "skip", 0.0,
                                detail=f"only {len(closes)} 1m candles")
        rsi_values = rsi(closes, period=14)
        current_rsi = rsi_values[-1]
        if current_rsi < 25:
            direction = "bullish"
            conf = min(0.80, 0.50 + (25 - current_rsi) / 50)
        elif current_rsi > 75:
            direction = "bearish"
            conf = min(0.80, 0.50 + (current_rsi - 75) / 50)
        else:
            return SignalResult("rsi", "skip", 0.0,
                                detail=f"rsi={current_rsi:.1f} neutral")
        return SignalResult("rsi", direction, conf,
                            detail=f"rsi={current_rsi:.1f}")

    def _news_veto(self) -> bool:
        """Return True if news veto triggers (should SKIP the trade)."""
        if not self.cfg.news_veto:
            return False
        headlines = tavily_headlines("Bitcoin BTC breaking news", max_results=3)
        if not headlines:
            return False
        veto_keywords = ["hack", "exploit", "sec ", "etf", "ban", "crash",
                         "regulation", "lawsuit", "fraud"]
        lower = headlines.lower()
        for kw in veto_keywords:
            if kw in lower:
                logger.info("btc_5min: news veto triggered on keyword '%s'", kw)
                return True
        return False

    # ----------------------------------------------------------- consensus

    def compute_consensus(self) -> dict:
        """Run all signals through composite_signal()."""
        signals = [
            self._momentum_signal(),
            self._funding_signal(),
            self._rsi_signal(),
        ]
        signal_dicts = [
            {"direction": s.direction, "confidence": s.confidence,
             "weight": s.weight, "name": s.name, "detail": s.detail}
            for s in signals
        ]
        result = composite_signal(signal_dicts)
        result["raw_signals"] = signal_dicts
        return result

    # ------------------------------------------------------------- entry

    def maybe_enter(self) -> bool:
        """Check timing + consensus; place order if conditions met.
        Returns True if a trade was attempted (live or shadow)."""
        now = time.time()
        period_ts = _current_period_ts()

        # Same-period dedupe
        if period_ts == self._last_entered_period:
            return False

        # Timing guard: only enter between entry_window_start and entry_window_end
        elapsed = now - period_ts
        if elapsed < self.cfg.entry_window_start:
            return False
        if elapsed > self.cfg.entry_window_end:
            return False

        # Hourly cap
        cutoff = now - 3600
        self._hour_trades = [t for t in self._hour_trades if t > cutoff]
        if len(self._hour_trades) >= self.cfg.max_per_hour:
            return False

        # Cooldown
        if self._hour_trades and now - self._hour_trades[-1] < self.cfg.cooldown_sec:
            return False

        # Risk gate
        if self.risk_gate is not None and not self.risk_gate.ok():
            if self.execute or not self.shadow_ignore_risk_gate:
                return False
            logger.warning("btc_5min: risk gate blocked but shadow continues")

        slug = _format_5min_slug(period_ts, self.asset)
        if self.cfg.require_universe_top and not self.trade_log.is_market_universe_eligible(
            slug,
            min_winrate=self.cfg.min_universe_winrate,
            require_top_rank=True,
        ):
            logger.info(
                "btc_5min: skip — %s not in focused top universe >= %.2f",
                slug,
                self.cfg.min_universe_winrate,
            )
            return False

        market_doc = self._resolve_current_5min_market(period_ts)
        if market_doc is None:
            logger.info("btc_5min: no market found for period %d", period_ts)
            return False

        if self.cfg.straddle_enabled:
            attempted = self._maybe_enter_straddle(
                slug=slug,
                period_ts=period_ts,
                market_doc=market_doc,
                now=now,
            )
            if attempted:
                return True

        # Compute consensus
        consensus = self.compute_consensus()
        direction = consensus.get("direction", "skip")
        confidence = consensus.get("confidence", 0.0)
        contributing = consensus.get("contributing_count", 0)

        if direction == "skip" or contributing < self.cfg.min_consensus:
            logger.debug(
                "btc_5min: skip — direction=%s contributing=%d confidence=%.3f",
                direction, contributing, confidence,
            )
            return False

        if confidence < self.cfg.min_confidence:
            logger.debug(
                "btc_5min: skip — confidence %.3f < %.3f",
                confidence,
                self.cfg.min_confidence,
            )
            return False

        # News veto
        if self._news_veto():
            return False

        # Side mapping: bullish → BUY (Up), bearish → SELL (Down)
        side = "BUY" if direction == "bullish" else "SELL"

        market_id = market_doc["market_id"]
        token_ids = market_doc.get("token_ids", [])
        yes_price = float(market_doc.get("yes_price", 0.50))
        no_price = float(market_doc.get("no_price", 0.50))
        token_id_for_log = (
            token_ids[0] if side == "BUY"
            else (token_ids[1] if len(token_ids) > 1 else "")
        )
        if not token_id_for_log:
            logger.info("btc_5min: skip — missing token for side=%s", side)
            return False
        try:
            live_entry_price, fillable_usdc, avg_price = self.polymarket._fillable_market_buy(
                token_id_for_log,
                self.cfg.position_size_usdc,
            )
        except Exception as exc:
            logger.info("btc_5min: skip — live order book not executable: %s", exc)
            return False
        order_amount_usdc = min(self.cfg.position_size_usdc, fillable_usdc)
        entry_token_price = live_entry_price
        if entry_token_price < self.cfg.min_live_entry_price:
            logger.info(
                "btc_5min: skip — live entry %.4f below min %.4f",
                entry_token_price,
                self.cfg.min_live_entry_price,
            )
            return False
        if entry_token_price > self.cfg.max_live_entry_price:
            logger.info(
                "btc_5min: skip — live entry %.4f above max %.4f",
                entry_token_price,
                self.cfg.max_live_entry_price,
            )
            return False
        brain_probability = float(confidence)
        edge = brain_probability - entry_token_price
        if edge < self.cfg.min_edge_pct:
            self.trade_log.insert_brain_decision(
                agent="btc_5min",
                strategy="btc_5min_internal_probability_edge",
                decision_type="entry",
                market_id=slug,
                token_id=token_id_for_log,
                approved=False,
                reason=(
                    f"internal_edge_too_low edge={edge:.3f}<"
                    f"{self.cfg.min_edge_pct:.3f}"
                ),
                score=brain_probability,
                market_type="crypto_5min",
                asset=self.asset.upper(),
                features={
                    "side": side,
                    "brain_probability": round(brain_probability, 4),
                    "market_entry_price": round(entry_token_price, 4),
                    "edge": round(edge, 4),
                    "min_edge_pct": self.cfg.min_edge_pct,
                    "consensus": consensus,
                },
                action=side,
            )
            logger.info(
                "btc_5min: skip — internal edge %.3f < %.3f "
                "(brain_probability=%.3f market_price=%.3f side=%s)",
                edge,
                self.cfg.min_edge_pct,
                brain_probability,
                entry_token_price,
                side,
            )
            return False
        # execute_market_order anchors recommendation.price to outcomes[0]:
        # BUY buys outcomes[0] at price, SELL buys outcomes[1] at 1-price.
        order_anchor_price = (
            live_entry_price if side == "BUY" else 1.0 - live_entry_price
        )
        gamma_entry_price = yes_price if side == "BUY" else no_price
        if abs(entry_token_price - gamma_entry_price) > 0.03:
            logger.info(
                "btc_5min: Gamma/CLOB price gap side=%s gamma=%.4f live=%.4f avg=%.4f",
                side,
                gamma_entry_price,
                live_entry_price,
                avg_price,
            )

        # Brain gate: centralized quality check for crypto entries.
        if self.brain is None:
            if self.execute:
                logger.warning("btc_5min: live entry blocked — missing MarketBrain")
                return False
        else:
            try:
                decision = self.brain.evaluate_crypto_entry(
                    slug=slug,
                    candidate_price=entry_token_price,
                    side=side,
                )
                self.trade_log.insert_brain_decision(
                    agent="btc_5min",
                    strategy="btc_5min_consensus",
                    decision_type="entry",
                    market_id=slug,
                    approved=decision.approved,
                    reason=decision.reason,
                    score=decision.score,
                    market_type="crypto_5min",
                    asset=self.asset.upper(),
                    features={
                        **decision.features,
                        "brain_probability": round(brain_probability, 4),
                        "market_entry_price": round(entry_token_price, 4),
                        "edge": round(edge, 4),
                        "min_edge_pct": self.cfg.min_edge_pct,
                    },
                    action=side,
                )
                if not decision.approved:
                    logger.info(
                        "btc_5min: brain rejected — %s (score=%.3f)",
                        decision.reason, decision.score,
                    )
                    return False
            except Exception:
                logger.exception("btc_5min brain gate failed; blocking entry")
                return False

        # Dedupe: check if we already have a position on this market
        if self.trade_log.has_filled_position_for_market(market_id):
            logger.info("btc_5min: skip — already holds position on %s", market_id)
            return False

        # Exitable size check
        safety = exitable_size_check(
            amount_usdc=order_amount_usdc,
            entry_price=entry_token_price,
        )
        if not safety.ok:
            logger.info("btc_5min: skip — %s", safety.reason)
            return False

        from agents.utils.objects import TradeRecommendation
        recommendation = TradeRecommendation(
            price=order_anchor_price,
            size_fraction=0.0,
            side=side,
            confidence=confidence,
            amount_usdc=order_amount_usdc,
        )
        cycle_id = self.trade_log.new_cycle_id()
        pending_id = self.trade_log.insert_pending(
            cycle_id=cycle_id,
            market_id=market_id,
            token_id=token_id_for_log,
            side=side,
            price=order_anchor_price,
            size_usdc=order_amount_usdc,
            confidence=confidence,
        )

        self._last_entered_period = period_ts
        self._hour_trades.append(time.time())

        if not self.execute:
            self.trade_log.mark(
                pending_id, BTC_5MIN_OPEN,
                response={"shadow": True, "side": side,
                           "consensus": consensus.get("agreement", 0),
                           "contributing": contributing,
                           "tp_pct_override": self.cfg.take_profit_pct,
                           "max_hold_seconds": self.cfg.max_hold_seconds},
                error=f"SHADOW: {side} consensus={direction} conf={confidence:.3f}",
            )
            logger.info(
                "btc_5min SHADOW: %s direction=%s confidence=%.3f period=%d",
                side, direction, confidence, period_ts,
            )
            return True

        # Live path
        try:
            response = self.polymarket.execute_market_order(
                (market_doc["doc"], 0.0), recommendation,
            )
        except Exception as exc:
            self.trade_log.mark(
                pending_id, "failed",
                error=f"execute_market_order raised: {exc}",
            )
            logger.warning("btc_5min entry failed: %s", exc)
            return False

        if not response or response.get("status") not in ("matched", "filled"):
            self.trade_log.mark(
                pending_id, "failed",
                response=response, error="entry not matched",
            )
            return False

        entry_price = float(response.get("order_avg_price_estimate", 0.5))
        entry_size_usdc = float(
            response.get("amount_usdc", self.cfg.position_size_usdc)
        )
        self.trade_log.mark(
            pending_id, BTC_5MIN_OPEN,
            response={
                **(response or {}),
                "tp_pct_override": self.cfg.take_profit_pct,
                "max_hold_seconds": self.cfg.max_hold_seconds,
                "entry_mode": "btc_5min_fast_exit",
            },
            price=entry_price,
            size_usdc=entry_size_usdc,
        )
        logger.info(
            "btc_5min ENTRY: %s on %s @ %.3f (consensus=%s conf=%.3f)",
            side, market_id, entry_price, direction, confidence,
        )
        notify_trade(
            event="fill",
            agent="btc_5min",
            market_id=market_id,
            side=side,
            price=entry_price,
            size_usdc=entry_size_usdc,
            reason=f"consensus={direction} conf={confidence:.3f}",
            balance_usdc=_safe_balance(self.polymarket),
        )
        return True

    def _maybe_enter_straddle(
        self,
        *,
        slug: str,
        period_ts: int,
        market_doc: dict,
        now: float,
    ) -> bool:
        """Enter both Up and Down when the 5m book is cheap enough to scalp volatility."""
        seconds_to_expiry = (period_ts + PERIOD_SEC) - int(now)
        if seconds_to_expiry < self.cfg.straddle_min_seconds_to_expiry:
            logger.debug(
                "btc_5min straddle skip — %s seconds_to_expiry=%s < %s",
                slug,
                seconds_to_expiry,
                self.cfg.straddle_min_seconds_to_expiry,
            )
            return False

        market_id = market_doc["market_id"]
        token_ids = market_doc.get("token_ids", [])
        if len(token_ids) < 2:
            logger.info("btc_5min straddle skip — missing tokens for %s", slug)
            return False
        up_token, down_token = str(token_ids[0]), str(token_ids[1])
        if self.trade_log.has_filled_position_for_market(market_id, up_token) or self.trade_log.has_filled_position_for_market(market_id, down_token):
            logger.info("btc_5min straddle skip — already holds %s", market_id)
            return False

        leg_specs = [
            {"label": "up", "side": "BUY", "token": up_token},
            {"label": "down", "side": "SELL", "token": down_token},
        ]
        total_entry = 0.0
        for spec in leg_specs:
            try:
                live_price, fillable_usdc, avg_price = self.polymarket._fillable_market_buy(
                    spec["token"],
                    self.cfg.straddle_leg_usdc,
                    max_spread_pct=self.cfg.straddle_max_entry_spread_pct,
                    min_entry_price=self.cfg.straddle_min_entry_price,
                    min_bid_depth_usdc=self.cfg.straddle_min_bid_depth_usdc,
                )
            except Exception as exc:
                logger.info("btc_5min straddle skip — %s book not executable: %s", spec["label"], exc)
                return False
            amount_usdc = min(self.cfg.straddle_leg_usdc, fillable_usdc)
            if amount_usdc <= 0:
                return False
            safety = exitable_size_check(amount_usdc=amount_usdc, entry_price=live_price)
            if not safety.ok:
                logger.info("btc_5min straddle skip — %s %s", spec["label"], safety.reason)
                return False
            spec.update(
                live_price=float(live_price),
                amount_usdc=float(amount_usdc),
                avg_price=float(avg_price),
                anchor_price=float(live_price if spec["side"] == "BUY" else 1.0 - live_price),
            )
            total_entry += float(live_price)

        if total_entry > self.cfg.straddle_max_pair_ask_sum:
            logger.debug(
                "btc_5min straddle skip — pair ask sum %.4f > %.4f slug=%s",
                total_entry,
                self.cfg.straddle_max_pair_ask_sum,
                slug,
            )
            return False

        if self.brain is None and self.meta_brain is None and self.execute:
            logger.warning("btc_5min straddle blocked — missing MarketBrain/MetaBrain")
            return False
        if self.meta_brain is not None:
            decision = self.meta_brain.synthesize_crypto_straddle(
                slug=slug,
                question=market_doc.get("question") or slug,
                asset=self.asset.upper(),
                up_price=leg_specs[0]["live_price"],
                down_price=leg_specs[1]["live_price"],
                pair_ask_sum=total_entry,
                seconds_to_expiry=seconds_to_expiry,
                token_id=up_token,
                liquidity_usdc=sum(spec["amount_usdc"] for spec in leg_specs),
            )
            decision_reason = decision.reason
            decision_score = decision.score
            decision_features = decision.features
            decision_approved = decision.approved and decision.entry_timing == "now"
            if decision.approved and decision.entry_timing != "now":
                decision_reason = f"meta_timing_{decision.entry_timing}"
                decision_approved = False
        elif self.brain is not None:
            if hasattr(self.brain, "evaluate_crypto_straddle_entry"):
                decision = self.brain.evaluate_crypto_straddle_entry(
                    slug=slug,
                    up_price=leg_specs[0]["live_price"],
                    down_price=leg_specs[1]["live_price"],
                    pair_ask_sum=total_entry,
                    seconds_to_expiry=seconds_to_expiry,
                )
            else:
                decision = self.brain.evaluate_crypto_entry(
                    slug=slug,
                    candidate_price=0.50,
                    side="BUY",
                )
            decision_reason = decision.reason
            decision_score = decision.score
            decision_features = decision.features
            decision_approved = decision.approved
        else:
            decision_reason = "brain_missing_shadow"
            decision_score = 0.0
            decision_features = {}
            decision_approved = True

        for spec in leg_specs:
            features = {
                **(decision_features or {}),
                "pair_ask_sum": round(total_entry, 4),
                "seconds_to_expiry": seconds_to_expiry,
                "leg": spec["label"],
            }
            self.trade_log.insert_brain_decision(
                agent="btc_5min",
                strategy="btc_5min_straddle_scalp",
                decision_type="entry",
                market_id=slug,
                token_id=spec["token"],
                approved=decision_approved,
                reason=decision_reason,
                score=decision_score,
                market_type="crypto_5min_straddle",
                asset=self.asset.upper(),
                features=features,
                action=spec["side"],
            )
        if not decision_approved:
            logger.info(
                "btc_5min straddle meta rejected — %s score=%.3f pair_sum=%.4f",
                decision_reason,
                decision_score,
                total_entry,
            )
            return False

        from agents.utils.objects import TradeRecommendation
        cycle_id = self.trade_log.new_cycle_id()
        pending_ids: list[tuple[int, dict]] = []
        for spec in leg_specs:
            pending_id = self.trade_log.insert_pending(
                cycle_id=f"{cycle_id}:{spec['label']}",
                market_id=market_id,
                token_id=spec["token"],
                side=spec["side"],
                price=spec["anchor_price"],
                size_usdc=spec["amount_usdc"],
                confidence=1.0,
            )
            pending_ids.append((pending_id, spec))

        self._last_entered_period = period_ts
        self._hour_trades.append(time.time())

        if not self.execute:
            for pending_id, spec in pending_ids:
                self.trade_log.mark(
                    pending_id,
                    BTC_5MIN_OPEN,
                    response={
                        "shadow": True,
                        "entry_mode": "btc_5min_straddle_scalp",
                        "straddle_id": cycle_id,
                        "leg": spec["label"],
                        "pair_ask_sum": round(total_entry, 4),
                        "tp_pct_override": self.cfg.straddle_take_profit_pct,
                        "max_hold_seconds": self.cfg.straddle_max_hold_seconds,
                    },
                    error=f"SHADOW straddle {spec['label']} pair_sum={total_entry:.4f}",
                )
            logger.info("btc_5min STRADDLE SHADOW: %s pair_sum=%.4f", slug, total_entry)
            return True

        filled = 0
        for pending_id, spec in pending_ids:
            rec = TradeRecommendation(
                price=spec["anchor_price"],
                size_fraction=0.0,
                side=spec["side"],
                confidence=1.0,
                amount_usdc=spec["amount_usdc"],
            )
            try:
                response = self.polymarket.execute_market_order((market_doc["doc"], 0.0), rec)
            except Exception as exc:
                self.trade_log.mark(pending_id, "failed", error=f"straddle execute raised: {exc}")
                logger.warning("btc_5min straddle %s failed: %s", spec["label"], exc)
                continue
            if not response or response.get("status") not in ("matched", "filled"):
                self.trade_log.mark(
                    pending_id,
                    "failed",
                    response=response,
                    error="straddle entry not matched",
                )
                continue
            entry_price = float(response.get("order_avg_price_estimate", spec["live_price"]))
            entry_size_usdc = float(response.get("amount_usdc", spec["amount_usdc"]))
            self.trade_log.mark(
                pending_id,
                BTC_5MIN_OPEN,
                response={
                    **(response or {}),
                    "entry_mode": "btc_5min_straddle_scalp",
                    "straddle_id": cycle_id,
                    "leg": spec["label"],
                    "pair_ask_sum": round(total_entry, 4),
                    "tp_pct_override": self.cfg.straddle_take_profit_pct,
                    "max_hold_seconds": self.cfg.straddle_max_hold_seconds,
                },
                price=entry_price,
                size_usdc=entry_size_usdc,
            )
            filled += 1
            notify_trade(
                event="fill",
                agent="btc_5min_straddle",
                market_id=market_id,
                side=spec["side"],
                price=entry_price,
                size_usdc=entry_size_usdc,
                reason=f"{spec['label']} pair_sum={total_entry:.4f}",
                balance_usdc=_safe_balance(self.polymarket),
            )
        logger.info("btc_5min STRADDLE: %s filled=%d/2 pair_sum=%.4f", slug, filled, total_entry)
        return filled > 0

    # ------------------------------------------------------------ market resolution

    def _resolve_current_5min_market(self, period_ts: int) -> Optional[dict]:
        """Resolve the current 5-min market from Gamma API."""
        slug = _format_5min_slug(period_ts, self.asset)
        if slug in self._market_cache:
            return self._market_cache[slug]
        try:
            import requests
            r = requests.get(
                f"https://gamma-api.polymarket.com/markets?slug={slug}",
                timeout=10,
            )
            data = r.json()
            if not data:
                logger.debug("btc_5min: no market for slug %s", slug)
                return None
            m = data[0]
            if not m.get("active", True) or m.get("closed", False):
                logger.debug("btc_5min: market %s closed/inactive", slug)
                return None
            import ast
            tokens = ast.literal_eval(m["clobTokenIds"])
            outcomes = ast.literal_eval(m["outcomes"])

            class _Doc:
                pass
            doc = _Doc()
            doc.dict = lambda: {
                "metadata": {
                    "id": m["id"],
                    "outcomes": str(outcomes),
                    "clob_token_ids": str(tokens),
                    "outcome_prices": m.get("outcomePrices", '["0.5","0.5"]'),
                }
            }
            entry = {
                "market_id": str(m["id"]),
                "token_ids": tokens,
                "outcomes": outcomes,
                "yes_price": float(ast.literal_eval(m.get("outcomePrices", '["0.5","0.5"]'))[0]),
                "no_price": float(ast.literal_eval(m.get("outcomePrices", '["0.5","0.5"]'))[1]),
                "question": str(m.get("question", "")),
                "doc": doc,
            }
            self._market_cache[slug] = entry
            return entry
        except Exception as exc:
            logger.warning("btc_5min: resolve %s failed: %s", slug, exc)
            return None


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class Btc5MinDaemon:
    """Long-running loop. SIGTERM-aware."""

    def __init__(self, db_path: Optional[str] = None, execute: Optional[bool] = None):
        self.cfg = Btc5MinConfig.from_env()
        self.execute = (
            execute if execute is not None
            else os.getenv("EXECUTE_BTC_5MIN", "false").lower() == "true"
        )
        self.tl = TradeLog(db_path=db_path)
        assets = [asset for asset in self.cfg.assets if asset in ASSET_PRODUCTS]
        if not assets:
            assets = ["btc"]
        from agents.polymarket.polymarket import Polymarket
        from agents.application.risk_gate import RiskGate
        self.polymarket = Polymarket(live=self.execute)
        self.risk_gate = RiskGate(
            trade_log=self.tl,
            polymarket=self.polymarket,
            btc_5min_reserve_usdc=self.cfg.reserve_usdc,
        )
        from agents.application.market_brain import MarketBrain
        from agents.application.meta_brain import MetaBrain
        market_brain = MarketBrain()
        meta_brain = MetaBrain(db_path=db_path, market_brain=market_brain)
        self.feeds = {
            asset: AssetPriceFeed(ASSET_PRODUCTS[asset]) for asset in assets
        }
        self.engines = [
            Btc5MinEngine(
                polymarket=self.polymarket,
                trade_log=self.tl,
                risk_gate=self.risk_gate,
                feed=self.feeds[asset],
                cfg=self.cfg,
                execute=self.execute,
                brain=market_brain,
                meta_brain=meta_brain,
                asset=asset,
            )
            for asset in assets
        ]
        self.engine = self.engines[0]
        self.heartbeat = Path(self.cfg.heartbeat_path)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            _signal.signal(_signal.SIGTERM, lambda *_: self.stop())
            _signal.signal(_signal.SIGINT, lambda *_: self.stop())
        except (ValueError, OSError):
            pass
        logger.info(
            "Btc5MinDaemon: starting (execute=%s assets=%s)",
            self.execute,
            ",".join(sorted(self.feeds)),
        )
        try:
            while not self._stop.is_set():
                for asset, feed in self.feeds.items():
                    try:
                        feed.update()
                    except Exception:
                        logger.exception("btc_5min %s feed update failed", asset)
                for engine in self.engines:
                    try:
                        engine.maybe_enter()
                    except Exception:
                        logger.exception(
                            "btc_5min %s entry check failed", engine.asset
                        )
                try:
                    self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
                    self.heartbeat.touch()
                except Exception:
                    logger.warning("btc_5min heartbeat touch failed")
                self._stop.wait(self.cfg.poll_sec)
        finally:
            logger.info("Btc5MinDaemon: exited")


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)
    Btc5MinDaemon().run()
