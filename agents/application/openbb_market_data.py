"""OpenBB market-data signal adapter.

OpenBB is used as a read-only research provider.  The dependency is optional:
if the `openbb` package is not installed, this adapter fails closed with a
skip-style signal instead of blocking the trading system.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

from agents.application.market_microstructure import feature_snapshot_from_bars


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
class OpenBBMarketSignal:
    direction: Optional[str]
    probability: float
    confidence: float
    symbol: Optional[str]
    asset_class: Optional[str]
    reason: str
    features: dict = field(default_factory=dict)


class OpenBBMarketDataClient:
    """Fetch recent OpenBB bars and produce a bounded directional signal."""

    SYMBOLS = {
        "nvidia": ("NVDA", "stock"),
        "nvda": ("NVDA", "stock"),
        "microsoft": ("MSFT", "stock"),
        "msft": ("MSFT", "stock"),
        "apple": ("AAPL", "stock"),
        "aapl": ("AAPL", "stock"),
        "google": ("GOOGL", "stock"),
        "alphabet": ("GOOGL", "stock"),
        "googl": ("GOOGL", "stock"),
        "amazon": ("AMZN", "stock"),
        "amzn": ("AMZN", "stock"),
        "tesla": ("TSLA", "stock"),
        "tsla": ("TSLA", "stock"),
        "meta": ("META", "stock"),
        "s&p": ("SPY", "etf"),
        "spx": ("SPY", "etf"),
        "spy": ("SPY", "etf"),
        "nasdaq": ("QQQ", "etf"),
        "qqq": ("QQQ", "etf"),
        "oil": ("USO", "commodity_etf"),
        "crude": ("USO", "commodity_etf"),
        "gold": ("GLD", "commodity_etf"),
        "silver": ("SLV", "commodity_etf"),
        "bitcoin": ("BTC-USD", "crypto"),
        "btc": ("BTC-USD", "crypto"),
        "ethereum": ("ETH-USD", "crypto"),
        "eth": ("ETH-USD", "crypto"),
    }

    def __init__(self, *, cache_ttl_sec: Optional[int] = None):
        self.cache_ttl_sec = cache_ttl_sec if cache_ttl_sec is not None else _env_int("OPENBB_MARKET_DATA_CACHE_SEC", 300)
        self.provider = os.getenv("OPENBB_PROVIDER", "yfinance")
        self._cache: dict = {}

    def analyze_question(self, question: str) -> OpenBBMarketSignal:
        symbol, asset_class = self.infer_symbol(question)
        if not symbol:
            return OpenBBMarketSignal(None, 0.5, 0.0, None, None, "openbb: no supported symbol in question")
        bars = self.fetch_bars(symbol)
        return self.signal_from_bars(symbol, asset_class, bars)

    def infer_symbol(self, question: str) -> tuple[Optional[str], Optional[str]]:
        text = str(question or "").lower()
        for key, value in self.SYMBOLS.items():
            if _keyword_matches(text, key):
                return value
        return None, None

    def fetch_bars(self, symbol: str) -> list[dict]:
        now = time.time()
        if symbol in self._cache:
            ts, bars = self._cache[symbol]
            if now - ts < self.cache_ttl_sec:
                return bars
        try:
            from openbb import obb  # type: ignore
        except Exception as exc:
            if self.provider.lower() == "yfinance":
                bars = self._fetch_yahoo_chart_bars(symbol)
                self._cache[symbol] = (now, bars)
                return bars
            raise RuntimeError("openbb package not installed") from exc

        limit = _env_int("OPENBB_MARKET_DATA_BAR_LIMIT", 60)
        try:
            result = obb.equity.price.historical(symbol=symbol, provider=self.provider)
            frame = result.to_df()
        except Exception:
            if symbol.endswith("-USD"):
                result = obb.crypto.price.historical(symbol=symbol, provider=self.provider)
                frame = result.to_df()
            else:
                raise
        if frame is None or len(frame) == 0:
            bars = []
        else:
            frame = frame.tail(limit)
            bars = [
                {
                    "close": float(row.get("close", 0.0) or 0.0),
                    "high": float(row.get("high", 0.0) or 0.0),
                    "low": float(row.get("low", 0.0) or 0.0),
                    "volume": float(row.get("volume", 0.0) or 0.0),
                }
                for _, row in frame.iterrows()
            ]
        self._cache[symbol] = (now, bars)
        return bars

    def _fetch_yahoo_chart_bars(self, symbol: str) -> list[dict]:
        """Lightweight yfinance-compatible fallback without adding OpenBB deps."""

        limit = _env_int("OPENBB_MARKET_DATA_BAR_LIMIT", 60)
        interval = os.getenv("OPENBB_YAHOO_INTERVAL", "5m")
        range_param = os.getenv("OPENBB_YAHOO_RANGE", "5d")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        response = requests.get(
            url,
            params={"range": range_param, "interval": interval},
            timeout=_env_float("OPENBB_MARKET_DATA_TIMEOUT_SEC", 5.0),
            headers={"User-Agent": "poly1-openbb-signal/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
        results = (((payload or {}).get("chart") or {}).get("result") or [])
        if not results:
            return []
        result = results[0]
        timestamps = result.get("timestamp") or []
        quote = (((result.get("indicators") or {}).get("quote") or [{}])[0]) or {}
        closes = quote.get("close") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        volumes = quote.get("volume") or []
        bars = []
        for idx, _ts in enumerate(timestamps[-limit:]):
            src_idx = len(timestamps) - len(timestamps[-limit:]) + idx
            close = _seq_float(closes, src_idx)
            if close <= 0:
                continue
            bars.append(
                {
                    "close": close,
                    "high": _seq_float(highs, src_idx),
                    "low": _seq_float(lows, src_idx),
                    "volume": _seq_float(volumes, src_idx),
                    "_dependency": "yahoo_chart",
                    "_provider": "yfinance",
                }
            )
        return bars

    def signal_from_bars(
        self,
        symbol: str,
        asset_class: str,
        bars: list[dict],
    ) -> OpenBBMarketSignal:
        min_bars = _env_int("OPENBB_MARKET_DATA_MIN_BARS", 10)
        if len(bars) < min_bars:
            return OpenBBMarketSignal(
                None,
                0.5,
                0.0,
                symbol,
                asset_class,
                f"openbb: insufficient bars {len(bars)}<{min_bars}",
                {"bar_count": len(bars)},
            )
        closes = [_bar_float(b, "close", "c") for b in bars]
        highs = [_bar_float(b, "high", "h") for b in bars]
        lows = [_bar_float(b, "low", "l") for b in bars]
        volumes = [_bar_float(b, "volume", "v") for b in bars]
        closes = [c for c in closes if c > 0]
        if len(closes) < min_bars:
            return OpenBBMarketSignal(None, 0.5, 0.0, symbol, asset_class, "openbb: invalid close bars")

        short_window = min(5, len(closes))
        long_window = min(20, len(closes))
        short_ma = sum(closes[-short_window:]) / short_window
        long_ma = sum(closes[-long_window:]) / long_window
        start = closes[-long_window]
        last = closes[-1]
        momentum = (last - start) / start if start else 0.0
        ma_edge = (short_ma - long_ma) / long_ma if long_ma else 0.0
        low = min([x for x in lows if x > 0] or closes)
        high = max([x for x in highs if x > 0] or closes)
        range_pct = (high - low) / start if start else 0.0
        recent_vol = sum(volumes[-short_window:]) / max(1, short_window)
        prior = volumes[:-short_window] or volumes
        prior_vol = sum(prior) / max(1, len(prior))
        vol_ratio = recent_vol / prior_vol if prior_vol > 0 else 1.0
        micro = feature_snapshot_from_bars(symbol, asset_class, bars)

        threshold = _env_float("OPENBB_MARKET_DATA_MOMENTUM_THRESHOLD", 0.01)
        composite = 0.65 * momentum + 0.35 * ma_edge
        if composite >= threshold:
            direction = "bullish"
        elif composite <= -threshold:
            direction = "bearish"
        else:
            direction = None
        strength = min(1.0, abs(composite) / max(threshold, 1e-6))
        vol_boost = min(0.08, max(0.0, vol_ratio - 1.0) * 0.04)
        range_boost = min(0.06, range_pct * 1.2)
        regime_boost = _microstructure_confidence_adjustment(direction, micro)
        confidence = min(0.76, 0.48 + 0.16 * strength + vol_boost + range_boost)
        confidence = max(0.0, min(0.78, confidence + regime_boost))
        if direction is None:
            confidence = min(confidence, 0.52)
        probability = 0.5 if direction is None else min(0.76, 0.5 + (confidence - 0.5))
        return OpenBBMarketSignal(
            direction,
            round(probability, 4),
            round(confidence, 4),
            symbol,
            asset_class,
            (
                f"openbb {asset_class} bars: composite={composite:+.3%}, "
                f"momentum={momentum:+.3%}, ma_edge={ma_edge:+.3%}, vol_ratio={vol_ratio:.2f}, "
                f"regime={micro.regime}"
            ),
            {
                "bar_count": len(bars),
                "start_close": round(start, 6),
                "last_close": round(last, 6),
                "momentum_pct": round(momentum, 6),
                "ma_edge_pct": round(ma_edge, 6),
                "composite_pct": round(composite, 6),
                "range_pct": round(range_pct, 6),
                "volume_ratio": round(vol_ratio, 4),
                "provider": self.provider,
                "dependency": str(bars[-1].get("_dependency") or "openbb"),
                "microstructure_adjustment": round(regime_boost, 4),
                **micro.features,
            },
        )


def openbb_enabled_for_metabrain() -> bool:
    return _env_bool("META_BRAIN_OPENBB_ENABLED", True)


def _bar_float(bar: dict, *names: str) -> float:
    for name in names:
        try:
            return float(bar.get(name) or 0.0)
        except (TypeError, ValueError):
            continue
    return 0.0


def _seq_float(values: list, idx: int) -> float:
    try:
        raw = values[idx]
        return float(raw or 0.0)
    except (IndexError, TypeError, ValueError):
        return 0.0


def _keyword_matches(text: str, keyword: str) -> bool:
    key = keyword.lower()
    if key == "s&p":
        return "s&p" in text or "s & p" in text
    if re.search(r"[a-z0-9]", key):
        return re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text) is not None
    return key in text


def _microstructure_confidence_adjustment(direction: Optional[str], micro) -> float:
    if direction is None:
        return 0.0
    adjustment = 0.0
    deviation = micro.vwap_deviation_pct or 0.0
    zscore = micro.mean_reversion_zscore or 0.0
    if micro.regime == "trending":
        if direction == "bullish" and deviation > 0:
            adjustment += 0.025
        elif direction == "bearish" and deviation < 0:
            adjustment += 0.025
    elif micro.regime in {"mean_reverting", "stretched"} and abs(zscore) >= 1.5:
        if direction == "bullish" and zscore > 0:
            adjustment -= 0.035
        elif direction == "bearish" and zscore < 0:
            adjustment -= 0.035
    return adjustment
