"""Alpaca market-data signal adapter.

This module is read-only: it fetches external market data and turns it into a
bounded signal for MetaBrain/external_conviction. It never places orders.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from agents.application.climax_volume_reversal import detect_climax_volume_reversal


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass
class AlpacaMarketSignal:
    direction: Optional[str]       # "bullish" | "bearish" | None
    probability: float             # estimated probability for directional move
    confidence: float              # 0-1 signal confidence
    symbol: Optional[str]
    asset_class: Optional[str]     # "crypto" | "stock" | None
    reason: str
    features: dict = field(default_factory=dict)


class AlpacaMarketDataClient:
    """Fetch recent Alpaca bars and produce a simple momentum/volume signal."""

    CRYPTO_SYMBOLS = {
        "bitcoin": "BTC/USD",
        "btc": "BTC/USD",
        "ethereum": "ETH/USD",
        "eth": "ETH/USD",
        "solana": "SOL/USD",
        "sol": "SOL/USD",
        "xrp": "XRP/USD",
        "doge": "DOGE/USD",
        "dogecoin": "DOGE/USD",
    }
    STOCK_SYMBOLS = {
        "nvidia": "NVDA",
        "nvda": "NVDA",
        "microsoft": "MSFT",
        "msft": "MSFT",
        "apple": "AAPL",
        "aapl": "AAPL",
        "google": "GOOGL",
        "alphabet": "GOOGL",
        "googl": "GOOGL",
        "amazon": "AMZN",
        "amzn": "AMZN",
        "tesla": "TSLA",
        "tsla": "TSLA",
        "meta": "META",
        "facebook": "META",
        "s&p": "SPY",
        "spx": "SPY",
        "spy": "SPY",
        "nasdaq": "QQQ",
        "qqq": "QQQ",
    }

    def __init__(
        self,
        *,
        cache_ttl_sec: Optional[int] = None,
        timeout_sec: Optional[int] = None,
    ) -> None:
        self.crypto_bars_url = os.getenv(
            "ALPACA_CRYPTO_BARS_URL",
            "https://data.alpaca.markets/v1beta3/crypto/us/bars",
        )
        self.stock_bars_url = os.getenv(
            "ALPACA_STOCK_BARS_URL",
            "https://data.alpaca.markets/v2/stocks/bars",
        )
        self.cache_ttl_sec = (
            cache_ttl_sec
            if cache_ttl_sec is not None
            else _env_int("ALPACA_MARKET_DATA_CACHE_SEC", 60)
        )
        self.timeout_sec = (
            timeout_sec
            if timeout_sec is not None
            else _env_int("ALPACA_MARKET_DATA_TIMEOUT_SEC", 5)
        )
        self._cache: dict = {}

    def analyze_question(self, question: str) -> AlpacaMarketSignal:
        symbol, asset_class = self.infer_symbol(question)
        if not symbol:
            return AlpacaMarketSignal(
                None, 0.5, 0.0, None, None, "alpaca: no supported symbol in question"
            )
        bars = self.fetch_bars(symbol, asset_class)
        return self.signal_from_bars(symbol, asset_class, bars)

    def infer_symbol(self, question: str) -> tuple[Optional[str], Optional[str]]:
        text = str(question or "").lower()
        for key, symbol in self.CRYPTO_SYMBOLS.items():
            if key in text:
                return symbol, "crypto"
        for key, symbol in self.STOCK_SYMBOLS.items():
            if key in text:
                return symbol, "stock"
        return None, None

    def fetch_bars(self, symbol: str, asset_class: str) -> list[dict]:
        key = (asset_class, symbol)
        now = time.time()
        if key in self._cache:
            ts, bars = self._cache[key]
            if now - ts < self.cache_ttl_sec:
                return bars

        limit = _env_int("ALPACA_MARKET_DATA_BAR_LIMIT", 20)
        timeframe = os.getenv("ALPACA_MARKET_DATA_TIMEFRAME", "1Min")
        if asset_class == "crypto":
            params = {
                "symbols": symbol,
                "timeframe": timeframe,
                "limit": str(limit),
                "sort": "asc",
            }
            url = self.crypto_bars_url + "?" + urllib.parse.urlencode(params)
        else:
            params = {
                "symbols": symbol,
                "timeframe": timeframe,
                "limit": str(limit),
                "sort": "asc",
                "feed": os.getenv("ALPACA_STOCK_FEED", "iex"),
            }
            url = self.stock_bars_url + "?" + urllib.parse.urlencode(params)

        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
            body = json.loads(resp.read())
        bars_by_symbol = body.get("bars") if isinstance(body, dict) else {}
        bars = bars_by_symbol.get(symbol) if isinstance(bars_by_symbol, dict) else None
        if not isinstance(bars, list):
            bars = []
        self._cache[key] = (now, bars)
        return bars

    def _headers(self) -> dict:
        headers = {"User-Agent": "poly1-alpaca-market-data/1.0"}
        key = (
            os.getenv("ALPACA_API_KEY_ID", "").strip()
            or os.getenv("APCA_API_KEY_ID", "").strip()
        )
        secret = (
            os.getenv("ALPACA_API_SECRET_KEY", "").strip()
            or os.getenv("APCA_API_SECRET_KEY", "").strip()
        )
        if key and secret:
            headers["APCA-API-KEY-ID"] = key
            headers["APCA-API-SECRET-KEY"] = secret
        return headers

    def signal_from_bars(
        self,
        symbol: str,
        asset_class: str,
        bars: list[dict],
    ) -> AlpacaMarketSignal:
        min_bars = _env_int("ALPACA_MARKET_DATA_MIN_BARS", 5)
        if len(bars) < min_bars:
            return AlpacaMarketSignal(
                None,
                0.5,
                0.0,
                symbol,
                asset_class,
                f"alpaca: insufficient bars {len(bars)}<{min_bars}",
                {"bar_count": len(bars)},
            )
        closes = [self._bar_float(b, "c") for b in bars]
        highs = [self._bar_float(b, "h") for b in bars]
        lows = [self._bar_float(b, "l") for b in bars]
        volumes = [self._bar_float(b, "v") for b in bars]
        climax_bars = [
            {
                "open": self._bar_float(b, "o"),
                "high": self._bar_float(b, "h"),
                "low": self._bar_float(b, "l"),
                "close": self._bar_float(b, "c"),
                "volume": self._bar_float(b, "v"),
            }
            for b in bars
        ]
        closes = [c for c in closes if c > 0]
        if len(closes) < min_bars:
            return AlpacaMarketSignal(
                None, 0.5, 0.0, symbol, asset_class, "alpaca: invalid close bars"
            )
        start = closes[0]
        last = closes[-1]
        momentum = (last - start) / start if start else 0.0
        low = min([x for x in lows if x > 0] or closes)
        high = max([x for x in highs if x > 0] or closes)
        range_pct = (high - low) / start if start else 0.0
        recent_vol = sum(volumes[-3:]) / max(1, min(3, len(volumes)))
        prior = volumes[:-3] or volumes
        prior_vol = sum(prior) / max(1, len(prior))
        vol_ratio = recent_vol / prior_vol if prior_vol > 0 else 1.0

        threshold = _env_float(
            "ALPACA_MARKET_DATA_MOMENTUM_THRESHOLD",
            0.0015 if asset_class == "crypto" else 0.0025,
        )
        if momentum >= threshold:
            direction = "bullish"
        elif momentum <= -threshold:
            direction = "bearish"
        else:
            direction = None

        strength = min(1.0, abs(momentum) / max(threshold, 1e-6))
        vol_boost = min(0.12, max(0.0, vol_ratio - 1.0) * 0.06)
        range_boost = min(0.08, range_pct * 4.0)
        confidence = min(0.78, 0.48 + 0.14 * strength + vol_boost + range_boost)
        if direction is None:
            confidence = min(confidence, 0.52)
        climax = detect_climax_volume_reversal(climax_bars)
        if climax.direction:
            direction = climax.direction
            confidence = max(confidence, climax.confidence)
            probability = climax.probability
        else:
            probability = 0.5 if direction is None else min(0.78, 0.5 + (confidence - 0.5))
        return AlpacaMarketSignal(
            direction,
            round(probability, 4),
            round(confidence, 4),
            symbol,
            asset_class,
            (
                f"alpaca {asset_class} bars: momentum={momentum:+.3%}, "
                f"range={range_pct:.3%}, vol_ratio={vol_ratio:.2f}"
            ),
            {
                "bar_count": len(bars),
                "start_close": round(start, 6),
                "last_close": round(last, 6),
                "momentum_pct": round(momentum, 6),
                "range_pct": round(range_pct, 6),
                "volume_ratio": round(vol_ratio, 4),
                "timeframe": os.getenv("ALPACA_MARKET_DATA_TIMEFRAME", "1Min"),
                "climax_volume_reversal_direction": climax.direction,
                "climax_volume_reversal_probability": climax.probability,
                "climax_volume_reversal_confidence": climax.confidence,
                "climax_volume_reversal_reason": climax.reason,
                **climax.features,
            },
        )

    @staticmethod
    def _bar_float(bar: dict, key: str) -> float:
        try:
            return float(bar.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0


def question_aligned_direction(question: str, market_direction: str) -> str:
    """Map bullish/bearish underlying move into Polymarket YES/NO direction."""
    text = str(question or "").lower()
    asks_up = any(
        k in text
        for k in ("above", "higher", "up", "increase", "green", "gain", "rise", "rally")
    )
    asks_down = any(
        k in text
        for k in ("below", "lower", "down", "decrease", "red", "drop", "fall")
    )
    if market_direction == "bullish":
        return "yes" if asks_up or not asks_down else "no"
    if market_direction == "bearish":
        return "yes" if asks_down and not asks_up else "no"
    return "skip"


def alpaca_enabled_for_metabrain() -> bool:
    return _env_bool("META_BRAIN_ALPACA_ENABLED", True)
