"""Market microstructure features for external signal providers.

The functions here are intentionally dependency-free.  They turn a short bar
series into features that the MetaBrain can log, shadow-test, and later use as
gates: VWAP deviation, mean-reversion z-score, volatility, and a simple regime
classification.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Optional


@dataclass(frozen=True)
class MicrostructureSnapshot:
    symbol: str
    asset_class: str
    bar_count: int
    last_close: float
    vwap: Optional[float]
    vwap_deviation_pct: Optional[float]
    mean_reversion_zscore: Optional[float]
    return_autocorr: Optional[float]
    volatility_pct: Optional[float]
    regime: str
    regime_confidence: float
    features: dict = field(default_factory=dict)


def feature_snapshot_from_bars(
    symbol: str,
    asset_class: str,
    bars: list[dict],
    *,
    z_window: int = 20,
    regime_window: int = 30,
) -> MicrostructureSnapshot:
    closes = [_bar_float(b, "close", "c") for b in bars]
    closes = [x for x in closes if x > 0]
    last = closes[-1] if closes else 0.0
    vwap = compute_vwap(bars)
    deviation = ((last - vwap) / vwap) if last > 0 and vwap and vwap > 0 else None
    zscore = mean_reversion_zscore(closes, window=z_window)
    autocorr = return_autocorrelation(closes, window=regime_window)
    volatility = realized_volatility_pct(closes, window=regime_window)
    regime, confidence = classify_regime(closes, autocorr=autocorr, zscore=zscore, volatility_pct=volatility)

    return MicrostructureSnapshot(
        symbol=symbol,
        asset_class=asset_class,
        bar_count=len(bars),
        last_close=last,
        vwap=vwap,
        vwap_deviation_pct=deviation,
        mean_reversion_zscore=zscore,
        return_autocorr=autocorr,
        volatility_pct=volatility,
        regime=regime,
        regime_confidence=confidence,
        features={
            "micro_bar_count": len(bars),
            "micro_last_close": _round(last),
            "micro_vwap": _round(vwap),
            "micro_vwap_deviation_pct": _round(deviation),
            "micro_mean_reversion_zscore": _round(zscore),
            "micro_return_autocorr": _round(autocorr),
            "micro_volatility_pct": _round(volatility),
            "micro_regime": regime,
            "micro_regime_confidence": round(confidence, 4),
        },
    )


def compute_vwap(bars: list[dict]) -> Optional[float]:
    total_value = 0.0
    total_volume = 0.0
    for bar in bars:
        close = _bar_float(bar, "close", "c")
        if close <= 0:
            continue
        high = _bar_float(bar, "high", "h") or close
        low = _bar_float(bar, "low", "l") or close
        volume = _bar_float(bar, "volume", "v")
        if volume <= 0:
            volume = 1.0
        typical = (high + low + close) / 3.0
        total_value += typical * volume
        total_volume += volume
    if total_volume <= 0:
        return None
    return total_value / total_volume


def mean_reversion_zscore(closes: Iterable[float], *, window: int = 20) -> Optional[float]:
    values = [float(x) for x in closes if float(x) > 0]
    if len(values) < max(3, window):
        return None
    sample = values[-window:]
    mean = sum(sample) / len(sample)
    variance = sum((x - mean) ** 2 for x in sample) / len(sample)
    std = math.sqrt(variance)
    if std <= 0:
        return 0.0
    return (sample[-1] - mean) / std


def return_autocorrelation(closes: Iterable[float], *, window: int = 30) -> Optional[float]:
    values = [float(x) for x in closes if float(x) > 0]
    returns = []
    for prev, cur in zip(values, values[1:]):
        if prev > 0:
            returns.append((cur - prev) / prev)
    if len(returns) < 4:
        return None
    sample = returns[-window:]
    xs = sample[:-1]
    ys = sample[1:]
    return _correlation(xs, ys)


def realized_volatility_pct(closes: Iterable[float], *, window: int = 30) -> Optional[float]:
    values = [float(x) for x in closes if float(x) > 0]
    returns = []
    for prev, cur in zip(values, values[1:]):
        if prev > 0:
            returns.append((cur - prev) / prev)
    if len(returns) < 3:
        return None
    sample = returns[-window:]
    mean = sum(sample) / len(sample)
    variance = sum((x - mean) ** 2 for x in sample) / len(sample)
    return math.sqrt(variance)


def classify_regime(
    closes: Iterable[float],
    *,
    autocorr: Optional[float] = None,
    zscore: Optional[float] = None,
    volatility_pct: Optional[float] = None,
) -> tuple[str, float]:
    values = [float(x) for x in closes if float(x) > 0]
    if len(values) < 8:
        return "unknown", 0.0
    if autocorr is None:
        autocorr = return_autocorrelation(values)
    if autocorr is None:
        return "unknown", 0.0

    abs_ac = abs(autocorr)
    confidence = min(0.85, 0.45 + abs_ac)
    if autocorr <= -0.18:
        return "mean_reverting", round(confidence, 4)
    if autocorr >= 0.18:
        return "trending", round(confidence, 4)
    if zscore is not None and abs(zscore) >= 2.0 and (volatility_pct or 0.0) > 0:
        return "stretched", 0.55
    return "mixed", round(max(0.35, confidence - 0.1), 4)


def _correlation(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return 0.0
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return cov / math.sqrt(vx * vy)


def _bar_float(bar: dict, *names: str) -> float:
    for name in names:
        try:
            return float(bar.get(name) or 0.0)
        except (TypeError, ValueError):
            continue
    return 0.0


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), digits)
