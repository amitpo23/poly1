"""Fast public crypto exchange tape signal.

Read-only adapter for Binance/OKX public market data. It turns recent spot
bars, top-of-book spread, and perpetual funding into a bounded signal for
MetaBrain and external_conviction. It never places orders.
"""
from __future__ import annotations

import json
import math
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
class CryptoExchangeSignal:
    direction: Optional[str]       # "bullish" | "bearish" | None
    probability: float
    confidence: float
    asset: Optional[str]
    symbol: Optional[str]
    reason: str
    features: dict = field(default_factory=dict)


class CryptoExchangeTapeClient:
    """Fetch Binance/OKX public data and compute a short-horizon tape signal."""

    ASSETS = {
        "bitcoin": ("BTC", "BTCUSDT", "BTC-USDT-SWAP"),
        "btc": ("BTC", "BTCUSDT", "BTC-USDT-SWAP"),
        "ethereum": ("ETH", "ETHUSDT", "ETH-USDT-SWAP"),
        "eth": ("ETH", "ETHUSDT", "ETH-USDT-SWAP"),
        "solana": ("SOL", "SOLUSDT", "SOL-USDT-SWAP"),
        "sol": ("SOL", "SOLUSDT", "SOL-USDT-SWAP"),
        "xrp": ("XRP", "XRPUSDT", "XRP-USDT-SWAP"),
        "doge": ("DOGE", "DOGEUSDT", "DOGE-USDT-SWAP"),
        "dogecoin": ("DOGE", "DOGEUSDT", "DOGE-USDT-SWAP"),
        "bnb": ("BNB", "BNBUSDT", "BNB-USDT-SWAP"),
    }

    def __init__(
        self,
        *,
        cache_ttl_sec: Optional[int] = None,
        timeout_sec: Optional[int] = None,
    ) -> None:
        self.cache_ttl_sec = (
            cache_ttl_sec
            if cache_ttl_sec is not None
            else _env_int("CRYPTO_TAPE_CACHE_SEC", 15)
        )
        self.timeout_sec = (
            timeout_sec
            if timeout_sec is not None
            else _env_int("CRYPTO_TAPE_TIMEOUT_SEC", 4)
        )
        self.binance_klines_url = os.getenv(
            "BINANCE_KLINES_URL", "https://api.binance.com/api/v3/klines"
        )
        self.binance_book_url = os.getenv(
            "BINANCE_BOOK_TICKER_URL", "https://api.binance.com/api/v3/ticker/bookTicker"
        )
        self.okx_funding_url = os.getenv(
            "OKX_FUNDING_URL", "https://www.okx.com/api/v5/public/funding-rate"
        )
        self._cache: dict = {}

    def analyze_question(self, question: str) -> CryptoExchangeSignal:
        asset, symbol, okx_inst = self.infer_asset(question)
        if not symbol:
            return CryptoExchangeSignal(
                None, 0.5, 0.0, None, None, "crypto_tape: no supported crypto asset"
            )
        key = (symbol, okx_inst)
        now = time.time()
        if key in self._cache:
            ts, sig = self._cache[key]
            if now - ts < self.cache_ttl_sec:
                return sig
        try:
            bars = self.fetch_binance_klines(symbol)
            book = self.fetch_binance_book(symbol)
            funding = self.fetch_okx_funding(okx_inst)
            sig = self.signal_from_data(asset, symbol, bars, book, funding)
        except Exception as exc:
            sig = CryptoExchangeSignal(
                None,
                0.5,
                0.0,
                asset,
                symbol,
                f"crypto_tape: error:{type(exc).__name__}",
                {"error": str(exc)[:180]},
            )
        self._cache[key] = (now, sig)
        return sig

    def infer_asset(self, question: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
        text = str(question or "").lower()
        for key, row in self.ASSETS.items():
            if key in text:
                return row
        return None, None, None

    def fetch_binance_klines(self, symbol: str) -> list[list]:
        params = {
            "symbol": symbol,
            "interval": os.getenv("CRYPTO_TAPE_INTERVAL", "1m"),
            "limit": str(_env_int("CRYPTO_TAPE_BAR_LIMIT", 30)),
        }
        url = self.binance_klines_url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "poly1-crypto-tape/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
            body = json.loads(resp.read())
        return body if isinstance(body, list) else []

    def fetch_binance_book(self, symbol: str) -> dict:
        url = self.binance_book_url + "?" + urllib.parse.urlencode({"symbol": symbol})
        req = urllib.request.Request(url, headers={"User-Agent": "poly1-crypto-tape/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
            body = json.loads(resp.read())
        return body if isinstance(body, dict) else {}

    def fetch_okx_funding(self, okx_inst: Optional[str]) -> Optional[float]:
        if not okx_inst:
            return None
        try:
            url = self.okx_funding_url + "?" + urllib.parse.urlencode({"instId": okx_inst})
            req = urllib.request.Request(url, headers={"User-Agent": "poly1-crypto-tape/1.0"})
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                body = json.loads(resp.read())
            data = body.get("data", []) if isinstance(body, dict) else []
            if data:
                return float(data[0].get("fundingRate", 0.0))
        except Exception:
            return None
        return None

    def signal_from_data(
        self,
        asset: str,
        symbol: str,
        bars: list[list],
        book: dict,
        funding_rate: Optional[float],
    ) -> CryptoExchangeSignal:
        min_bars = _env_int("CRYPTO_TAPE_MIN_BARS", 8)
        if len(bars) < min_bars:
            return CryptoExchangeSignal(
                None,
                0.5,
                0.0,
                asset,
                symbol,
                f"crypto_tape: insufficient bars {len(bars)}<{min_bars}",
                {"bar_count": len(bars)},
            )
        closes = [self._float_idx(row, 4) for row in bars]
        opens = [self._float_idx(row, 1) for row in bars]
        highs = [self._float_idx(row, 2) for row in bars]
        lows = [self._float_idx(row, 3) for row in bars]
        vols = [self._float_idx(row, 5) for row in bars]
        climax_bars = [
            {
                "open": self._float_idx(row, 1),
                "high": self._float_idx(row, 2),
                "low": self._float_idx(row, 3),
                "close": self._float_idx(row, 4),
                "volume": self._float_idx(row, 5),
            }
            for row in bars
        ]
        closes = [c for c in closes if c > 0]
        if len(closes) < min_bars:
            return CryptoExchangeSignal(None, 0.5, 0.0, asset, symbol, "crypto_tape: invalid closes")

        start = closes[0]
        last = closes[-1]
        log_returns = [
            math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i] > 0 and closes[i - 1] > 0
        ]
        if len(log_returns) >= 2:
            mean_ret = sum(log_returns) / len(log_returns)
            variance = sum((r - mean_ret) ** 2 for r in log_returns) / (len(log_returns) - 1)
            realized_vol_annual = math.sqrt(max(0.0, variance)) * math.sqrt(365.0 * 24.0 * 60.0)
        else:
            realized_vol_annual = None
        short_start = closes[max(0, len(closes) - 6)]
        momentum = (last - start) / start if start else 0.0
        short_momentum = (last - short_start) / short_start if short_start else 0.0
        high = max([x for x in highs if x > 0] or closes)
        low = min([x for x in lows if x > 0] or closes)
        range_pct = (high - low) / start if start else 0.0
        recent_vol = sum(vols[-5:]) / max(1, min(5, len(vols)))
        prior = vols[:-5] or vols
        prior_vol = sum(prior) / max(1, len(prior))
        vol_ratio = recent_vol / prior_vol if prior_vol > 0 else 1.0

        bid = self._float(book.get("bidPrice"))
        ask = self._float(book.get("askPrice"))
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else last
        spread_pct = (ask - bid) / mid if mid > 0 and ask >= bid and bid > 0 else None

        threshold = _env_float("CRYPTO_TAPE_MOMENTUM_THRESHOLD", 0.0012)
        combined_momentum = (0.65 * short_momentum) + (0.35 * momentum)
        if combined_momentum >= threshold:
            direction = "bullish"
        elif combined_momentum <= -threshold:
            direction = "bearish"
        else:
            direction = None

        strength = min(1.0, abs(combined_momentum) / max(threshold, 1e-6))
        vol_boost = min(0.10, max(0.0, vol_ratio - 1.0) * 0.05)
        range_boost = min(0.08, range_pct * 3.0)
        funding_penalty = 0.0
        funding_note = "neutral"
        if funding_rate is not None:
            crowded = _env_float("CRYPTO_TAPE_CROWDED_FUNDING_ABS", 0.0005)
            if direction == "bullish" and funding_rate > crowded:
                funding_penalty = min(0.08, abs(funding_rate) * 80)
                funding_note = "crowded_long"
            elif direction == "bearish" and funding_rate < -crowded:
                funding_penalty = min(0.08, abs(funding_rate) * 80)
                funding_note = "crowded_short"
            elif direction == "bullish" and funding_rate < -crowded:
                vol_boost += min(0.05, abs(funding_rate) * 50)
                funding_note = "short_squeeze_support"
            elif direction == "bearish" and funding_rate > crowded:
                vol_boost += min(0.05, abs(funding_rate) * 50)
                funding_note = "long_unwind_support"

        confidence = min(0.82, 0.48 + 0.17 * strength + vol_boost + range_boost - funding_penalty)
        if spread_pct is not None and spread_pct > _env_float("CRYPTO_TAPE_MAX_SPREAD_PCT", 0.003):
            confidence = min(confidence, 0.55)
        if direction is None:
            confidence = min(confidence, 0.52)
        climax = detect_climax_volume_reversal(climax_bars)
        if climax.direction:
            direction = climax.direction
            confidence = max(confidence, climax.confidence)
            probability = climax.probability
        else:
            probability = 0.5 if direction is None else min(0.82, 0.5 + max(0.0, confidence - 0.5))
        return CryptoExchangeSignal(
            direction,
            round(probability, 4),
            round(max(0.0, confidence), 4),
            asset,
            symbol,
            (
                f"crypto_tape {symbol}: short={short_momentum:+.3%}, "
                f"window={momentum:+.3%}, vol_ratio={vol_ratio:.2f}, "
                f"funding={funding_rate if funding_rate is not None else 'na'}"
            ),
            {
                "bar_count": len(bars),
                "last_price": round(last, 6),
                "momentum_pct": round(momentum, 6),
                "short_momentum_pct": round(short_momentum, 6),
                "range_pct": round(range_pct, 6),
                "realized_volatility_annual": (
                    None if realized_vol_annual is None else round(realized_vol_annual, 6)
                ),
                "volume_ratio": round(vol_ratio, 4),
                "last_open": round(opens[-1], 6) if opens else None,
                "bid": round(bid, 6),
                "ask": round(ask, 6),
                "spread_pct": None if spread_pct is None else round(spread_pct, 6),
                "funding_rate": funding_rate,
                "funding_note": funding_note,
                "climax_volume_reversal_direction": climax.direction,
                "climax_volume_reversal_probability": climax.probability,
                "climax_volume_reversal_confidence": climax.confidence,
                "climax_volume_reversal_reason": climax.reason,
                **climax.features,
                "exchange_sources": ["binance_spot", "okx_funding"],
            },
        )

    @staticmethod
    def _float(value) -> float:
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _float_idx(cls, row: list, idx: int) -> float:
        try:
            return cls._float(row[idx])
        except (IndexError, TypeError):
            return 0.0


def crypto_tape_enabled_for_metabrain() -> bool:
    return _env_bool("META_BRAIN_CRYPTO_TAPE_ENABLED", True)
