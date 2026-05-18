"""Pure-stdlib technical analysis for probability series.

Ported from Vibe-Trading's 75-skill framework, adapted for Polymarket's
0-1 probability domain. All functions operate on plain ``list[float]`` —
no numpy, no pandas, no poly1 imports.

Key probability-domain adaptations:
  - Bollinger Bands clamped to [0, 1].
  - HV uses linear returns (not log returns) to avoid log(0) at boundaries.
  - ADX accepts synthetic highs/lows via a spread proxy (default ±0.02).
  - Resolution skip: callers should skip when hours_to_close < 24 or
    price extreme (<0.10 or >0.90) — see TechnicalSignalProvider.
"""
from __future__ import annotations

import math
from typing import Optional


# ---------------------------------------------------------------------------
# Moving averages
# ---------------------------------------------------------------------------

def ema(prices: list[float], period: int) -> list[float]:
    """Exponential moving average. Returns list same length as *prices*;
    values before *period* are SMA-seeded then EMA from there."""
    if not prices or period < 1:
        return []
    k = 2.0 / (period + 1)
    result: list[float] = []
    if len(prices) < period:
        s = sum(prices) / len(prices)
        result.append(s)
        for p in prices[1:]:
            s = p * k + s * (1.0 - k)
            result.append(s)
        return result
    s = sum(prices[:period]) / period
    for i in range(period):
        result.append(s)
    for i in range(period, len(prices)):
        s = prices[i] * k + s * (1.0 - k)
        result.append(s)
    return result


def sma(prices: list[float], period: int) -> list[float]:
    """Simple moving average. First *period-1* values use available data."""
    if not prices or period < 1:
        return []
    result: list[float] = []
    running = 0.0
    for i, p in enumerate(prices):
        running += p
        if i >= period:
            running -= prices[i - period]
        window = min(i + 1, period)
        result.append(running / window)
    return result


# ---------------------------------------------------------------------------
# RSI (Wilder smoothing)
# ---------------------------------------------------------------------------

def rsi(prices: list[float], period: int = 14) -> list[float]:
    """Relative Strength Index with Wilder smoothing. Returns 0-100 scale."""
    if len(prices) < 2:
        return [50.0] * len(prices)
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(0.0, d) for d in deltas]
    losses = [max(0.0, -d) for d in deltas]

    if len(deltas) < period:
        avg_gain = sum(gains) / max(len(gains), 1)
        avg_loss = sum(losses) / max(len(losses), 1)
        if avg_loss == 0:
            return [100.0] * len(prices)
        rs = avg_gain / avg_loss
        val = 100.0 - 100.0 / (1.0 + rs)
        return [50.0] + [val] * len(deltas)

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    result = [50.0] * (period + 1)  # +1 because deltas start at index 1
    if avg_loss == 0:
        result[-1] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[-1] = 100.0 - 100.0 / (1.0 + rs)

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(100.0 - 100.0 / (1.0 + rs))

    return result


# ---------------------------------------------------------------------------
# Bollinger Bands (clamped to [0, 1] for probability series)
# ---------------------------------------------------------------------------

def bollinger_bands(
    prices: list[float],
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[list[float], list[float], list[float]]:
    """Returns (upper, mid, lower) clamped to [0, 1]."""
    mid = sma(prices, period)
    upper: list[float] = []
    lower: list[float] = []
    for i in range(len(prices)):
        window_start = max(0, i - period + 1)
        window = prices[window_start: i + 1]
        m = mid[i]
        if len(window) < 2:
            upper.append(min(1.0, m))
            lower.append(max(0.0, m))
            continue
        variance = sum((x - m) ** 2 for x in window) / len(window)
        std = math.sqrt(variance)
        upper.append(min(1.0, m + num_std * std))
        lower.append(max(0.0, m - num_std * std))
    return upper, mid, lower


# ---------------------------------------------------------------------------
# ADX (Average Directional Index)
# ---------------------------------------------------------------------------

def adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[float]:
    """ADX 0-100 using Wilder's DI chain. For probability series without
    real H/L, callers should pass synthetic highs/lows via spread proxy."""
    n = len(closes)
    if n < 2:
        return [0.0] * n

    plus_dm: list[float] = []
    minus_dm: list[float] = []
    tr: list[float] = []

    for i in range(1, n):
        high_diff = highs[i] - highs[i - 1]
        low_diff = lows[i - 1] - lows[i]
        plus_dm.append(max(high_diff, 0.0) if high_diff > low_diff else 0.0)
        minus_dm.append(max(low_diff, 0.0) if low_diff > high_diff else 0.0)
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    if len(tr) < period:
        return [0.0] * n

    # Wilder smoothing for TR, +DM, -DM
    atr = sum(tr[:period])
    apdm = sum(plus_dm[:period])
    amdm = sum(minus_dm[:period])

    dx_values: list[float] = []

    for i in range(period - 1, len(tr)):
        if i == period - 1:
            pass  # initial sums already computed
        else:
            atr = atr - atr / period + tr[i]
            apdm = apdm - apdm / period + plus_dm[i]
            amdm = amdm - amdm / period + minus_dm[i]

        if atr == 0:
            dx_values.append(0.0)
            continue
        plus_di = 100.0 * apdm / atr
        minus_di = 100.0 * amdm / atr
        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_values.append(0.0)
        else:
            dx_values.append(100.0 * abs(plus_di - minus_di) / di_sum)

    if len(dx_values) < period:
        return [0.0] * n

    adx_val = sum(dx_values[:period]) / period
    result = [0.0] * (n - len(dx_values) + period - 1)
    result.append(adx_val)

    for i in range(period, len(dx_values)):
        adx_val = (adx_val * (period - 1) + dx_values[i]) / period
        result.append(adx_val)

    while len(result) < n:
        result.append(result[-1] if result else 0.0)
    return result[:n]


# ---------------------------------------------------------------------------
# OBV (On-Balance Volume)
# ---------------------------------------------------------------------------

def obv(closes: list[float], volumes: list[float]) -> list[float]:
    """Cumulative on-balance volume."""
    if not closes or not volumes:
        return []
    result = [0.0]
    for i in range(1, min(len(closes), len(volumes))):
        if closes[i] > closes[i - 1]:
            result.append(result[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            result.append(result[-1] - volumes[i])
        else:
            result.append(result[-1])
    return result


# ---------------------------------------------------------------------------
# Volatility percentile
# ---------------------------------------------------------------------------

def volatility_percentile(
    prices: list[float],
    hv_window: int = 20,
    lookback: int = 120,
) -> float:
    """Historical volatility percentile (0.0-1.0) in *lookback*-bar window.
    Uses linear returns (not log) to avoid log(0) at probability boundaries."""
    if len(prices) < hv_window + 2:
        return 0.5  # not enough data → neutral

    # Linear returns
    returns = [
        (prices[i] - prices[i - 1]) / max(prices[i - 1], 1e-9)
        for i in range(1, len(prices))
    ]

    # Rolling HV (std of returns over hv_window)
    hvs: list[float] = []
    for end in range(hv_window, len(returns) + 1):
        window = returns[end - hv_window: end]
        mean = sum(window) / hv_window
        var = sum((r - mean) ** 2 for r in window) / hv_window
        hvs.append(math.sqrt(var))

    if not hvs:
        return 0.5

    current_hv = hvs[-1]
    lookback_hvs = hvs[-lookback:] if len(hvs) >= lookback else hvs
    count_below = sum(1 for h in lookback_hvs if h <= current_hv)
    return count_below / len(lookback_hvs)


# ---------------------------------------------------------------------------
# Harmonic pattern scanner
# ---------------------------------------------------------------------------

def harmonic_scan(
    prices: list[float],
    tolerance: float = 0.05,
) -> list[dict]:
    """Scan for Gartley/Bat/Butterfly/Crab patterns in price series.

    Returns list of dicts with {pattern, direction, completion_index, ratios}.
    Uses swing-point detection on last 60 bars then checks Fibonacci ratios.
    """
    if len(prices) < 20:
        return []

    # Find swing points (local min/max over 5-bar window)
    swings: list[tuple[int, float, str]] = []
    window = 5
    for i in range(window, len(prices) - window):
        segment = prices[i - window: i + window + 1]
        if prices[i] == max(segment):
            swings.append((i, prices[i], "high"))
        elif prices[i] == min(segment):
            swings.append((i, prices[i], "low"))

    if len(swings) < 5:
        return []

    patterns: list[dict] = []
    # Check last 5 swing points for harmonic ratios
    HARMONIC_RATIOS = {
        "gartley": {"xb": 0.618, "ac": (0.382, 0.886), "bd": (1.272, 1.618), "xd": 0.786},
        "bat": {"xb": (0.382, 0.500), "ac": (0.382, 0.886), "bd": (1.618, 2.618), "xd": 0.886},
        "butterfly": {"xb": 0.786, "ac": (0.382, 0.886), "bd": (1.618, 2.618), "xd": (1.272, 1.618)},
        "crab": {"xb": (0.382, 0.618), "ac": (0.382, 0.886), "bd": (2.618, 3.618), "xd": 1.618},
    }

    for start in range(max(0, len(swings) - 8), len(swings) - 4):
        pts = swings[start: start + 5]
        x_val, a_val = pts[0][1], pts[1][1]
        b_val, c_val, d_val = pts[2][1], pts[3][1], pts[4][1]
        xa = abs(a_val - x_val)
        if xa < 1e-9:
            continue
        xb_ratio = abs(b_val - a_val) / xa
        ab = abs(b_val - a_val)
        if ab < 1e-9:
            continue
        ac_ratio = abs(c_val - b_val) / ab
        bc = abs(c_val - b_val)
        if bc < 1e-9:
            continue
        bd_ratio = abs(d_val - c_val) / bc
        xd_ratio = abs(d_val - x_val) / xa

        for name, ratios in HARMONIC_RATIOS.items():
            if _ratio_match(xb_ratio, ratios["xb"], tolerance) and \
               _ratio_match(ac_ratio, ratios["ac"], tolerance) and \
               _ratio_match(bd_ratio, ratios["bd"], tolerance) and \
               _ratio_match(xd_ratio, ratios["xd"], tolerance):
                direction = "bullish" if d_val < a_val else "bearish"
                patterns.append({
                    "pattern": name,
                    "direction": direction,
                    "completion_index": pts[4][0],
                    "ratios": {
                        "xb": round(xb_ratio, 3),
                        "ac": round(ac_ratio, 3),
                        "bd": round(bd_ratio, 3),
                        "xd": round(xd_ratio, 3),
                    },
                })

    return patterns


def _ratio_match(
    actual: float,
    target,
    tolerance: float,
) -> bool:
    """Check if *actual* is within *tolerance* of *target* (scalar or range tuple)."""
    if isinstance(target, (tuple, list)):
        lo, hi = target
        return (lo - tolerance) <= actual <= (hi + tolerance)
    return abs(actual - target) <= tolerance


# ---------------------------------------------------------------------------
# Funding rate regime (crypto derivatives)
# ---------------------------------------------------------------------------

def funding_rate_regime(rate_8h: float) -> dict:
    """Classify 8-hour funding rate into a trading regime.

    Returns {regime, signal, annualized}.
    """
    annualized = rate_8h * 3 * 365  # 3 funding periods per day
    if rate_8h > 0.0005:
        regime = "overheated_long"
        signal = "bearish"
    elif rate_8h < -0.0005:
        regime = "overheated_short"
        signal = "bullish"
    elif abs(rate_8h) < 0.0001:
        regime = "neutral"
        signal = "skip"
    else:
        regime = "mild"
        signal = "skip"
    return {
        "regime": regime,
        "signal": signal,
        "annualized": round(annualized, 4),
        "rate_8h": rate_8h,
    }


# ---------------------------------------------------------------------------
# Multi-factor z-score normalization
# ---------------------------------------------------------------------------

def multi_factor_zscore(factors: dict[str, float]) -> dict[str, float]:
    """Z-score normalize a dict of {name: raw_score} values.

    Returns {name: z_score}. If all values are identical, returns all zeros.
    """
    if not factors:
        return {}
    vals = list(factors.values())
    mean = sum(vals) / len(vals)
    variance = sum((v - mean) ** 2 for v in vals) / len(vals)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std < 1e-12:
        return {k: 0.0 for k in factors}
    return {k: round((v - mean) / std, 4) for k, v in factors.items()}


# ---------------------------------------------------------------------------
# Composite signal (weighted majority vote)
# ---------------------------------------------------------------------------

def composite_signal(signals: list[dict]) -> dict:
    """Weighted majority vote across multiple signal dicts.

    Each signal dict must have at least:
      - direction: "bullish" | "bearish" | "skip"
      - confidence: float 0-1
      - weight: float (optional, default 1.0)

    Returns {direction, confidence, agreement, contributing_count, signals}.
    """
    if not signals:
        return {"direction": "skip", "confidence": 0.0, "agreement": 0.0,
                "contributing_count": 0, "signals": []}

    bullish_weight = 0.0
    bearish_weight = 0.0
    total_weight = 0.0
    contributing = 0

    for s in signals:
        d = s.get("direction", "skip")
        c = float(s.get("confidence", 0))
        w = float(s.get("weight", 1.0))
        if d == "skip" or c <= 0:
            continue
        contributing += 1
        weighted = c * w
        total_weight += weighted
        if d == "bullish":
            bullish_weight += weighted
        elif d == "bearish":
            bearish_weight += weighted

    if contributing == 0 or total_weight == 0:
        return {"direction": "skip", "confidence": 0.0, "agreement": 0.0,
                "contributing_count": 0, "signals": signals}

    if bullish_weight > bearish_weight:
        direction = "bullish"
        agreement = bullish_weight / total_weight
    elif bearish_weight > bullish_weight:
        direction = "bearish"
        agreement = bearish_weight / total_weight
    else:
        direction = "skip"
        agreement = 0.5

    # Confidence scales with agreement: 3/3 → 0.85, 2/3 → 0.65, 1/3 → skip
    confidence = agreement * min(0.85, 0.40 + 0.15 * contributing)

    return {
        "direction": direction,
        "confidence": round(confidence, 4),
        "agreement": round(agreement, 4),
        "contributing_count": contributing,
        "signals": signals,
    }


# ---------------------------------------------------------------------------
# Convenience: probability-aware technical composite
# ---------------------------------------------------------------------------

def probability_technical_composite(
    prices: list[float],
    *,
    ema_short: int = 12,
    ema_long: int = 26,
    rsi_period: int = 14,
    rsi_oversold: float = 30.0,
    rsi_overbought: float = 70.0,
    bb_period: int = 20,
    bb_std: float = 2.0,
    min_bars: int = 30,
) -> Optional[dict]:
    """Run EMA crossover + RSI + BB on a probability series and return a
    composite signal dict suitable for ``composite_signal()``.

    Returns None if not enough data.
    """
    if len(prices) < min_bars:
        return None

    ema_s = ema(prices, ema_short)
    ema_l = ema(prices, ema_long)
    rsi_vals = rsi(prices, rsi_period)
    upper, mid, lower = bollinger_bands(prices, bb_period, bb_std)

    last_price = prices[-1]
    last_ema_s = ema_s[-1]
    last_ema_l = ema_l[-1]
    last_rsi = rsi_vals[-1]
    last_upper = upper[-1]
    last_lower = lower[-1]

    signals: list[dict] = []

    # 1. EMA crossover
    if last_ema_s > last_ema_l:
        signals.append({"name": "ema_crossover", "direction": "bullish",
                        "confidence": 0.6, "weight": 1.0})
    elif last_ema_s < last_ema_l:
        signals.append({"name": "ema_crossover", "direction": "bearish",
                        "confidence": 0.6, "weight": 1.0})
    else:
        signals.append({"name": "ema_crossover", "direction": "skip",
                        "confidence": 0.0, "weight": 1.0})

    # 2. RSI
    if last_rsi < rsi_oversold:
        signals.append({"name": "rsi", "direction": "bullish",
                        "confidence": 0.7, "weight": 1.0})
    elif last_rsi > rsi_overbought:
        signals.append({"name": "rsi", "direction": "bearish",
                        "confidence": 0.7, "weight": 1.0})
    else:
        signals.append({"name": "rsi", "direction": "skip",
                        "confidence": 0.0, "weight": 1.0})

    # 3. Bollinger Band position
    if last_price <= last_lower:
        signals.append({"name": "bb", "direction": "bullish",
                        "confidence": 0.65, "weight": 1.0})
    elif last_price >= last_upper:
        signals.append({"name": "bb", "direction": "bearish",
                        "confidence": 0.65, "weight": 1.0})
    else:
        signals.append({"name": "bb", "direction": "skip",
                        "confidence": 0.0, "weight": 1.0})

    composite = composite_signal(signals)
    composite["indicators"] = {
        "ema_short": round(last_ema_s, 6),
        "ema_long": round(last_ema_l, 6),
        "rsi": round(last_rsi, 2),
        "bb_upper": round(last_upper, 6),
        "bb_lower": round(last_lower, 6),
        "price": round(last_price, 6),
    }
    return composite
