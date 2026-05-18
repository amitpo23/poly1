"""BTC 5-Min Up/Down — multi-signal consensus agent for poly1.

Targets the 5-minute ``btc-updown-5m-{unix_ts}`` Polymarket binary:
every 5 minutes a new market opens, auto-resolved by Chainlink at the
end of the window.  Price is always ~0.50/0.50.

Strategy: three independent signals (momentum, funding rate, RSI) must
achieve ≥2/3 consensus to enter.  A Tavily news veto prevents entry
during high-impact BTC events.

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
    max_per_hour: int = 6
    heartbeat_path: str = "/app/data/btc_5min_heartbeat"

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
            max_per_hour=_env_int("BTC_5MIN_MAX_PER_HOUR", 6),
            heartbeat_path=os.getenv(
                "BTC_5MIN_HEARTBEAT_PATH", "/app/data/btc_5min_heartbeat"
            ),
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


def _format_5min_slug(period_ts: int) -> str:
    """Polymarket slug for a 5-min BTC up/down market."""
    return f"btc-updown-5m-{period_ts}"


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
    ):
        self.polymarket = polymarket
        self.trade_log = trade_log
        self.risk_gate = risk_gate
        self.feed = feed
        self.cfg = cfg
        self.execute = execute
        self.shadow_ignore_risk_gate = (
            os.getenv("SHADOW_IGNORE_RISK_GATE", "false").lower() == "true"
        )
        self._market_cache: dict[str, dict] = {}
        self._last_entered_period: int = 0
        self._hour_trades: list[float] = []  # timestamps of trades in last hour

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

        if confidence < 0.55:
            logger.debug("btc_5min: skip — confidence %.3f < 0.55", confidence)
            return False

        # News veto
        if self._news_veto():
            return False

        # Side mapping: bullish → BUY (Up), bearish → SELL (Down)
        side = "BUY" if direction == "bullish" else "SELL"

        # Resolve market
        market_doc = self._resolve_current_5min_market(period_ts)
        if market_doc is None:
            logger.info("btc_5min: no market found for period %d", period_ts)
            return False

        market_id = market_doc["market_id"]
        token_ids = market_doc.get("token_ids", [])

        # Dedupe: check if we already have a position on this market
        if self.trade_log.has_filled_position_for_market(market_id):
            logger.info("btc_5min: skip — already holds position on %s", market_id)
            return False

        # Exitable size check
        safety = exitable_size_check(
            amount_usdc=self.cfg.position_size_usdc,
            entry_price=0.50,
        )
        if not safety.ok:
            logger.info("btc_5min: skip — %s", safety.reason)
            return False

        from agents.utils.objects import TradeRecommendation
        recommendation = TradeRecommendation(
            price=0.50,
            size_fraction=0.0,
            side=side,
            confidence=confidence,
            amount_usdc=self.cfg.position_size_usdc,
        )
        cycle_id = self.trade_log.new_cycle_id()
        token_id_for_log = (
            token_ids[0] if side == "BUY"
            else (token_ids[1] if len(token_ids) > 1 else "")
        )
        pending_id = self.trade_log.insert_pending(
            cycle_id=cycle_id,
            market_id=market_id,
            token_id=token_id_for_log,
            side=side,
            price=0.50,
            size_usdc=self.cfg.position_size_usdc,
            confidence=confidence,
        )

        self._last_entered_period = period_ts
        self._hour_trades.append(time.time())

        if not self.execute:
            self.trade_log.mark(
                pending_id, BTC_5MIN_OPEN,
                response={"shadow": True, "side": side,
                           "consensus": consensus.get("agreement", 0),
                           "contributing": contributing},
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
            response=response,
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

    # ------------------------------------------------------------ market resolution

    def _resolve_current_5min_market(self, period_ts: int) -> Optional[dict]:
        """Resolve the current 5-min market from Gamma API."""
        slug = _format_5min_slug(period_ts)
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
        self.feed = CoinbasePriceFeed()
        from agents.polymarket.polymarket import Polymarket
        from agents.application.risk_gate import RiskGate
        self.polymarket = Polymarket(live=self.execute)
        self.risk_gate = RiskGate(
            trade_log=self.tl,
            polymarket=self.polymarket,
            btc_5min_reserve_usdc=self.cfg.reserve_usdc,
        )
        self.engine = Btc5MinEngine(
            polymarket=self.polymarket,
            trade_log=self.tl,
            risk_gate=self.risk_gate,
            feed=self.feed,
            cfg=self.cfg,
            execute=self.execute,
        )
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
        logger.info("Btc5MinDaemon: starting (execute=%s)", self.execute)
        try:
            while not self._stop.is_set():
                try:
                    self.feed.update()
                except Exception:
                    logger.exception("btc_5min feed update failed")
                try:
                    self.engine.maybe_enter()
                except Exception:
                    logger.exception("btc_5min entry check failed")
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
