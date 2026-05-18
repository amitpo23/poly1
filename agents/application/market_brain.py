"""Market-aware veto/scoring layer for strategy agents.

The brain does not place orders. It classifies a market, checks whether the
current evidence is strong enough for a strategy to act, and returns an
auditable decision that callers can log.
"""
from __future__ import annotations

import os
import re
import time
import json
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


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


@dataclass(frozen=True)
class BrainConfig:
    enabled: bool = True
    strict_unknown_markets: bool = False
    scalper_min_seconds_to_expiry: int = 90
    scalper_max_entry_price: float = 0.55
    scalper_max_pair_ask_sum: float = 1.04
    scalper_min_edge_score: float = 0.35
    exit_take_profit_pct: float = 0.05
    exit_trailing_stop_pct: float = 0.02
    exit_stop_loss_pct: float = 0.07
    exit_max_hold_seconds: int = 1800
    smart_exit_enabled: bool = True
    smart_exit_min_profit_pct: float = 0.05
    smart_exit_momentum_window: str = "60s"
    smart_exit_min_momentum_pct: float = 0.001
    smart_exit_peak_drawdown_hold_pct: float = 0.006
    smart_exit_min_seconds_to_expiry: int = 75
    crypto_signal_min_samples: int = 2
    # General binary market entry gates (sports, elections, any non-crypto).
    general_max_spread_pct: float = 0.15
    general_min_hours_to_close: float = 0.5
    general_max_hours_to_close: float = 168.0
    general_min_score: float = 0.30

    @classmethod
    def from_env(cls) -> "BrainConfig":
        return cls(
            enabled=_env_bool("MARKET_BRAIN_ENABLED", True),
            strict_unknown_markets=_env_bool("MARKET_BRAIN_STRICT_UNKNOWN", False),
            scalper_min_seconds_to_expiry=_env_int(
                "MARKET_BRAIN_SCALPER_MIN_SECONDS_TO_EXPIRY", 90
            ),
            scalper_max_entry_price=_env_float(
                "MARKET_BRAIN_SCALPER_MAX_ENTRY_PRICE", 0.55
            ),
            scalper_max_pair_ask_sum=_env_float(
                "MARKET_BRAIN_SCALPER_MAX_PAIR_ASK_SUM", 1.04
            ),
            scalper_min_edge_score=_env_float(
                "MARKET_BRAIN_SCALPER_MIN_EDGE_SCORE", 0.35
            ),
            exit_take_profit_pct=_env_float("MARKET_BRAIN_EXIT_TAKE_PROFIT_PCT", 0.05),
            exit_trailing_stop_pct=_env_float("MARKET_BRAIN_EXIT_TRAILING_STOP_PCT", 0.02),
            exit_stop_loss_pct=_env_float("MARKET_BRAIN_EXIT_STOP_LOSS_PCT", 0.07),
            exit_max_hold_seconds=_env_int("MARKET_BRAIN_EXIT_MAX_HOLD_SECONDS", 1800),
            smart_exit_enabled=_env_bool("MARKET_BRAIN_SMART_EXIT_ENABLED", True),
            smart_exit_min_profit_pct=_env_float(
                "MARKET_BRAIN_SMART_EXIT_MIN_PROFIT_PCT", 0.05
            ),
            smart_exit_momentum_window=os.getenv(
                "MARKET_BRAIN_SMART_EXIT_MOMENTUM_WINDOW", "60s"
            ),
            smart_exit_min_momentum_pct=_env_float(
                "MARKET_BRAIN_SMART_EXIT_MIN_MOMENTUM_PCT", 0.001
            ),
            smart_exit_peak_drawdown_hold_pct=_env_float(
                "MARKET_BRAIN_SMART_EXIT_PEAK_DRAWDOWN_HOLD_PCT", 0.006
            ),
            smart_exit_min_seconds_to_expiry=_env_int(
                "MARKET_BRAIN_SMART_EXIT_MIN_SECONDS_TO_EXPIRY", 75
            ),
            crypto_signal_min_samples=_env_int("MARKET_BRAIN_CRYPTO_MIN_SAMPLES", 2),
            general_max_spread_pct=_env_float("MARKET_BRAIN_GENERAL_MAX_SPREAD_PCT", 0.15),
            general_min_hours_to_close=_env_float(
                "MARKET_BRAIN_GENERAL_MIN_HOURS_TO_CLOSE", 0.5
            ),
            general_max_hours_to_close=_env_float(
                "MARKET_BRAIN_GENERAL_MAX_HOURS_TO_CLOSE", 168.0
            ),
            general_min_score=_env_float("MARKET_BRAIN_GENERAL_MIN_SCORE", 0.30),
        )


@dataclass(frozen=True)
class MarketProfile:
    market_type: str
    asset: Optional[str] = None
    period_ts: Optional[int] = None
    horizon: Optional[str] = None


@dataclass(frozen=True)
class BrainDecision:
    approved: bool
    reason: str
    score: float = 0.0
    profile: MarketProfile = field(default_factory=lambda: MarketProfile("unknown"))
    features: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExitPosition:
    market_id: str
    token_id: str
    side: str
    entry_price: float
    current_price: float
    opened_ts_ms: int
    max_price_seen: Optional[float] = None
    shares: Optional[float] = None


@dataclass(frozen=True)
class CryptoSignal:
    asset: str
    price: Optional[float]
    changes: dict
    samples: int
    fresh: bool


class CryptoSignalFeed:
    """Tiny REST-backed crypto price feed for brain evidence.

    The feed is intentionally optional. If network calls fail, callers can still
    make deterministic orderbook decisions; the brain should not crash a bot
    because an external signal endpoint is briefly unavailable.
    """

    SYMBOLS = {
        "btc": "BTC-USD",
        "eth": "ETH-USD",
        "sol": "SOL-USD",
        "xrp": "XRP-USD",
    }

    def __init__(self, max_history_sec: int = 600, timeout_sec: int = 5):
        self.max_history_sec = max_history_sec
        self.timeout_sec = timeout_sec
        self._samples: dict[str, deque] = {
            asset: deque() for asset in self.SYMBOLS
        }

    def update(self, asset: str) -> CryptoSignal:
        asset = self._normalize_asset(asset)
        symbol = self.SYMBOLS.get(asset)
        if not symbol:
            return CryptoSignal(asset=asset, price=None, changes={}, samples=0, fresh=False)
        try:
            with urllib.request.urlopen(
                f"https://api.coinbase.com/v2/prices/{symbol}/spot",
                timeout=self.timeout_sec,
            ) as resp:
                payload = json.loads(resp.read())
            price = float(payload["data"]["amount"])
        except Exception:
            return self.snapshot(asset, fresh=False)

        now_ms = int(time.time() * 1000)
        q = self._samples[asset]
        q.append((now_ms, price))
        cutoff = now_ms - self.max_history_sec * 1000
        while q and q[0][0] < cutoff:
            q.popleft()
        return self.snapshot(asset, fresh=True)

    def snapshot(self, asset: str, fresh: bool = True) -> CryptoSignal:
        asset = self._normalize_asset(asset)
        q = self._samples.get(asset) or deque()
        if not q:
            return CryptoSignal(asset=asset, price=None, changes={}, samples=0, fresh=False)
        return CryptoSignal(
            asset=asset,
            price=q[-1][1],
            changes={
                "30s": self.percent_change(asset, 30),
                "60s": self.percent_change(asset, 60),
                "180s": self.percent_change(asset, 180),
            },
            samples=len(q),
            fresh=fresh,
        )

    def percent_change(self, asset: str, window_sec: int) -> Optional[float]:
        asset = self._normalize_asset(asset)
        q = self._samples.get(asset) or deque()
        if len(q) < 2:
            return None
        now_ms, latest = q[-1]
        cutoff = now_ms - window_sec * 1000
        oldest = None
        for sample in q:
            if sample[0] >= cutoff:
                oldest = sample
                break
        if oldest is None or oldest[1] <= 0:
            return None
        return (latest - oldest[1]) / oldest[1]

    @staticmethod
    def _normalize_asset(asset: str) -> str:
        asset = (asset or "").lower()
        return {
            "bitcoin": "btc",
            "ethereum": "eth",
            "solana": "sol",
        }.get(asset, asset)


class MarketBrain:
    """Small deterministic first pass for market context.

    This is intentionally conservative and auditable. Richer inputs such as RSS,
    sports scores, and LLM/news evidence should feed this layer later, rather
    than each trading agent inventing its own private source of truth.
    """

    CRYPTO_15M_RE = re.compile(
        r"^(?P<asset>btc|bitcoin|eth|ethereum|sol|solana|xrp)-updown-15m-(?P<ts>\d+)$",
        re.IGNORECASE,
    )

    def __init__(
        self,
        cfg: Optional[BrainConfig] = None,
        crypto_feed: Optional[CryptoSignalFeed] = None,
    ):
        self.cfg = cfg or BrainConfig.from_env()
        self.crypto_feed = crypto_feed

    def classify(self, slug: str, period_ts: Optional[int] = None) -> MarketProfile:
        match = self.CRYPTO_15M_RE.match(slug or "")
        if match:
            asset = match.group("asset").lower()
            if asset == "bitcoin":
                asset = "btc"
            elif asset == "ethereum":
                asset = "eth"
            elif asset == "solana":
                asset = "sol"
            return MarketProfile(
                market_type="crypto_15m",
                asset=asset,
                period_ts=int(match.group("ts")),
                horizon="15m",
            )
        return MarketProfile(
            market_type="unknown",
            period_ts=period_ts,
        )

    def evaluate_scalper_entry(
        self,
        *,
        slug: str,
        side: str,
        up_ask: float,
        down_ask: float,
        candidate_price: float,
        signal_reason: str,
        now_ms: int,
        period_ts: Optional[int] = None,
    ) -> BrainDecision:
        profile = self.classify(slug, period_ts=period_ts)
        features = {
            "side": side,
            "up_ask": up_ask,
            "down_ask": down_ask,
            "candidate_price": candidate_price,
            "signal_reason": signal_reason,
            "pair_ask_sum": up_ask + down_ask,
        }

        if not self.cfg.enabled:
            return BrainDecision(True, "brain_disabled", 1.0, profile, features)

        if profile.market_type == "unknown":
            if self.cfg.strict_unknown_markets:
                return BrainDecision(False, "unknown_market_type", 0.0, profile, features)
            return BrainDecision(True, "unknown_market_allowed_non_strict", 0.5, profile, features)

        if profile.market_type != "crypto_15m":
            return BrainDecision(False, f"unsupported_market_type:{profile.market_type}", 0.0, profile, features)

        if self.crypto_feed is not None and profile.asset:
            signal = self.crypto_feed.update(profile.asset)
            features["crypto_price"] = signal.price
            features["crypto_changes"] = signal.changes
            features["crypto_samples"] = signal.samples
            features["crypto_fresh"] = signal.fresh

        expiry_ts = profile.period_ts or period_ts
        if expiry_ts:
            seconds_to_expiry = expiry_ts - int(now_ms / 1000)
            features["seconds_to_expiry"] = seconds_to_expiry
            if seconds_to_expiry < self.cfg.scalper_min_seconds_to_expiry:
                return BrainDecision(
                    False,
                    "too_close_to_expiry",
                    0.0,
                    profile,
                    features,
                )

        if candidate_price > self.cfg.scalper_max_entry_price:
            return BrainDecision(False, "candidate_price_too_high", 0.0, profile, features)

        pair_sum = up_ask + down_ask
        if pair_sum > self.cfg.scalper_max_pair_ask_sum:
            return BrainDecision(False, "pair_ask_sum_too_expensive", 0.0, profile, features)

        # Cheap entry is the base edge; reversal gets a small premium because
        # yesterday's missed winners came from fast bounces after a dislocation.
        discount_score = max(0.0, min(1.0, (0.50 - candidate_price) / 0.10))
        signal_bonus = 0.20 if signal_reason == "reversal" else 0.10
        pair_bonus = max(0.0, min(0.20, (1.00 - pair_sum) / 0.10))
        score = max(0.0, min(1.0, discount_score + signal_bonus + pair_bonus))
        features["discount_score"] = round(discount_score, 4)
        features["pair_bonus"] = round(pair_bonus, 4)

        if score < self.cfg.scalper_min_edge_score:
            return BrainDecision(False, "edge_score_too_low", score, profile, features)

        return BrainDecision(True, "approved", score, profile, features)

    def evaluate_exit(self, position: ExitPosition, now_ms: Optional[int] = None) -> BrainDecision:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        profile = self.classify(position.market_id)
        entry = float(position.entry_price)
        current = float(position.current_price)
        max_seen = float(position.max_price_seen or current)
        age_seconds = max(0.0, (now_ms - position.opened_ts_ms) / 1000.0)
        pnl_pct = ((current - entry) / entry) if entry > 0 else 0.0
        mfe_pct = ((max_seen - entry) / entry) if entry > 0 else 0.0
        drawdown_from_peak_pct = ((max_seen - current) / max_seen) if max_seen > 0 else 0.0
        features = {
            "entry_price": entry,
            "current_price": current,
            "max_price_seen": max_seen,
            "pnl_pct": pnl_pct,
            "mfe_pct": mfe_pct,
            "drawdown_from_peak_pct": drawdown_from_peak_pct,
            "age_seconds": age_seconds,
        }

        if self.crypto_feed is not None and profile.asset:
            signal = self.crypto_feed.update(profile.asset)
            features["crypto_price"] = signal.price
            features["crypto_changes"] = signal.changes
            features["crypto_samples"] = signal.samples
            features["crypto_fresh"] = signal.fresh

        seconds_to_expiry = None
        if profile.period_ts:
            seconds_to_expiry = profile.period_ts - int(now_ms / 1000)
            features["seconds_to_expiry"] = seconds_to_expiry

        if entry <= 0:
            return BrainDecision(False, "invalid_entry_price", 0.0, profile, features)

        if pnl_pct <= -self.cfg.exit_stop_loss_pct:
            return BrainDecision(True, "stop_loss", abs(pnl_pct), profile, features)

        if (
            mfe_pct >= self.cfg.exit_take_profit_pct
            and drawdown_from_peak_pct >= self.cfg.exit_trailing_stop_pct
        ):
            return BrainDecision(True, "trailing_stop_after_profit", mfe_pct, profile, features)

        if pnl_pct >= self.cfg.exit_take_profit_pct:
            if self._smart_exit_should_hold(
                position=position,
                profile=profile,
                features=features,
                pnl_pct=pnl_pct,
                drawdown_from_peak_pct=drawdown_from_peak_pct,
                seconds_to_expiry=seconds_to_expiry,
            ):
                return BrainDecision(
                    False,
                    "hold_profit_with_momentum",
                    pnl_pct,
                    profile,
                    features,
                )
            return BrainDecision(True, "take_profit", pnl_pct, profile, features)

        if age_seconds >= self.cfg.exit_max_hold_seconds:
            return BrainDecision(True, "timeout", pnl_pct, profile, features)

        return BrainDecision(False, "hold", pnl_pct, profile, features)

    def _smart_exit_should_hold(
        self,
        *,
        position: ExitPosition,
        profile: MarketProfile,
        features: dict,
        pnl_pct: float,
        drawdown_from_peak_pct: float,
        seconds_to_expiry: Optional[int],
    ) -> bool:
        """Return True when a profitable position deserves more room.

        This is intentionally a veto on immediate take-profit only. It never
        overrides stop-loss, trailing-stop, timeout, or expiry-risk exits.
        """
        if not self.cfg.smart_exit_enabled:
            return False
        if profile.market_type != "crypto_15m" or not profile.asset:
            return False
        if pnl_pct < self.cfg.smart_exit_min_profit_pct:
            return False
        if (
            seconds_to_expiry is not None
            and seconds_to_expiry < self.cfg.smart_exit_min_seconds_to_expiry
        ):
            features["smart_exit_block"] = "too_close_to_expiry"
            return False
        if drawdown_from_peak_pct > self.cfg.smart_exit_peak_drawdown_hold_pct:
            features["smart_exit_block"] = "peak_drawdown_too_large"
            return False

        changes = features.get("crypto_changes") or {}
        momentum = changes.get(self.cfg.smart_exit_momentum_window)
        if momentum is None:
            features["smart_exit_block"] = "missing_momentum"
            return False
        try:
            momentum = float(momentum)
        except (TypeError, ValueError):
            features["smart_exit_block"] = "bad_momentum"
            return False

        side = (position.side or "").lower()
        supports_side = (
            (side in {"up", "buy", "yes"} and momentum >= self.cfg.smart_exit_min_momentum_pct)
            or (side in {"down", "sell", "no"} and momentum <= -self.cfg.smart_exit_min_momentum_pct)
        )
        features["smart_exit_momentum"] = momentum
        features["smart_exit_supports_side"] = supports_side
        return supports_side

    def evaluate_general_entry(
        self,
        *,
        question: str,
        spread_pct: Optional[float] = None,
        hours_to_close: Optional[float] = None,
        external_context: str = "",
        vibe_signals: Optional[dict] = None,
    ) -> BrainDecision:
        """Pre-LLM gate for general binary markets (sports, elections, events).

        Quickly rejects markets with bad quality characteristics before spending
        LLM tokens. Returns BrainDecision; if approved=False, caller should skip.

        The score is informational — callers log it but don't use it for sizing.
        Entry is either allowed (score >= general_min_score) or blocked.
        """
        profile = MarketProfile(market_type="general_binary")
        features: dict = {
            "question_preview": (question or "")[:80],
            "spread_pct": spread_pct,
            "hours_to_close": hours_to_close,
            "has_external_context": bool(external_context),
        }

        if not self.cfg.enabled:
            return BrainDecision(True, "brain_disabled", 1.0, profile, features)

        # Hard reject: spread too wide (expensive to enter AND exit).
        if spread_pct is not None and spread_pct > self.cfg.general_max_spread_pct:
            return BrainDecision(False, "spread_too_wide", 0.0, profile, features)

        # Hard reject: resolution timing outside our operational window.
        if hours_to_close is not None:
            if hours_to_close < self.cfg.general_min_hours_to_close:
                return BrainDecision(False, "too_close_to_expiry", 0.0, profile, features)
            if hours_to_close > self.cfg.general_max_hours_to_close:
                return BrainDecision(False, "horizon_too_long", 0.0, profile, features)

        # Scoring: base 0.5, then additive adjustments.
        score = 0.5

        # Time horizon bonus: psychological-bias peak is 1h–48h before resolution.
        if hours_to_close is not None:
            if 1.0 <= hours_to_close <= 48.0:
                score += 0.15   # sweet spot for fast turnaround
            elif hours_to_close <= 1.0:
                score -= 0.10   # very close — rush entry less reliable
            elif hours_to_close > 72.0:
                score -= 0.10   # long horizon — bias reversion is slower

        # Spread quality bonus: tighter spread → cheaper round-trip.
        if spread_pct is not None:
            if spread_pct < 0.05:
                score += 0.10
            elif spread_pct > 0.10:
                score -= 0.10

        # External context bonus: Tavily found relevant discussion — this
        # market is actively being priced by humans, better signal quality.
        if external_context:
            score += 0.10
            features["external_context_preview"] = external_context[:100]

        # Vibe analysis bonus: technical indicators on probability series.
        if vibe_signals:
            composite_conf = float(vibe_signals.get("confidence", 0))
            if composite_conf > 0:
                score += min(0.15, composite_conf * 0.20)
                features["vibe_confidence"] = round(composite_conf, 4)
                features["vibe_direction"] = vibe_signals.get("direction", "")

        score = max(0.0, min(1.0, score))
        features["score"] = round(score, 4)

        if score < self.cfg.general_min_score:
            return BrainDecision(False, "general_score_too_low", score, profile, features)

        return BrainDecision(True, "approved_general_entry", score, profile, features)
