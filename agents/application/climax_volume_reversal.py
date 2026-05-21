"""Climax-volume reversal detector.

Read-only signal logic for short-horizon exhaustion fades.  The detector looks
for a high-volume candle followed by an opposite confirmation candle.  It does
not place orders; callers decide whether the signal is only shadow evidence or
eligible for MetaBrain routing.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


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


@dataclass
class ClimaxVolumeReversalSignal:
    direction: Optional[str]  # "bullish" | "bearish" | None
    probability: float
    confidence: float
    reason: str
    features: dict = field(default_factory=dict)


def detect_climax_volume_reversal(
    bars: list[dict],
    *,
    volume_window: Optional[int] = None,
    volume_multiple: Optional[float] = None,
    range_multiple: Optional[float] = None,
    min_body_fraction: Optional[float] = None,
) -> ClimaxVolumeReversalSignal:
    """Return a bounded exhaustion-fade signal from OHLCV bars.

    The penultimate candle is the climax candidate; the final candle must close
    in the opposite direction to confirm the fade.  This mirrors the public
    strategy sketch without importing any execution behavior.
    """

    window = volume_window or _env_int("CLIMAX_VOLUME_WINDOW", 20)
    vol_mult = volume_multiple or _env_float("CLIMAX_VOLUME_MULTIPLE", 3.0)
    rng_mult = range_multiple or _env_float("CLIMAX_RANGE_MULTIPLE", 1.2)
    body_min = min_body_fraction or _env_float("CLIMAX_MIN_BODY_FRACTION", 0.35)
    if len(bars) < window + 2:
        return ClimaxVolumeReversalSignal(
            None,
            0.5,
            0.0,
            f"climax_volume_reversal: insufficient bars {len(bars)}<{window + 2}",
            {"bar_count": len(bars), "climax_window": window},
        )

    history = bars[-(window + 2):-2]
    climax = bars[-2]
    confirm = bars[-1]

    hist_volumes = [_bar_float(b, "volume", "v") for b in history]
    hist_ranges = [_bar_range(b) for b in history]
    avg_volume = sum(hist_volumes) / len(hist_volumes) if hist_volumes else 0.0
    avg_range = sum(hist_ranges) / len(hist_ranges) if hist_ranges else 0.0
    climax_open = _bar_float(climax, "open", "o")
    climax_close = _bar_float(climax, "close", "c")
    confirm_open = _bar_float(confirm, "open", "o")
    confirm_close = _bar_float(confirm, "close", "c")
    climax_volume = _bar_float(climax, "volume", "v")
    climax_range = _bar_range(climax)
    body_fraction = (
        abs(climax_close - climax_open) / climax_range
        if climax_range > 0
        else 0.0
    )
    volume_ratio = climax_volume / avg_volume if avg_volume > 0 else 0.0
    range_ratio = climax_range / avg_range if avg_range > 0 else 0.0

    features = {
        "climax_bar_count": len(bars),
        "climax_window": window,
        "climax_volume_ratio": round(volume_ratio, 4),
        "climax_range_ratio": round(range_ratio, 4),
        "climax_body_fraction": round(body_fraction, 4),
        "climax_open": round(climax_open, 8),
        "climax_close": round(climax_close, 8),
        "confirmation_open": round(confirm_open, 8),
        "confirmation_close": round(confirm_close, 8),
    }

    if volume_ratio < vol_mult:
        return ClimaxVolumeReversalSignal(
            None,
            0.5,
            0.0,
            f"climax_volume_reversal: volume_ratio={volume_ratio:.2f}<{vol_mult:.2f}",
            features,
        )
    if range_ratio < rng_mult:
        return ClimaxVolumeReversalSignal(
            None,
            0.5,
            0.0,
            f"climax_volume_reversal: range_ratio={range_ratio:.2f}<{rng_mult:.2f}",
            features,
        )
    if body_fraction < body_min:
        return ClimaxVolumeReversalSignal(
            None,
            0.5,
            0.0,
            f"climax_volume_reversal: body_fraction={body_fraction:.2f}<{body_min:.2f}",
            features,
        )

    climax_up = climax_close > climax_open
    climax_down = climax_close < climax_open
    confirm_up = confirm_close > confirm_open
    confirm_down = confirm_close < confirm_open
    if climax_up and confirm_down and confirm_close < climax_close:
        direction = "bearish"
    elif climax_down and confirm_up and confirm_close > climax_close:
        direction = "bullish"
    else:
        return ClimaxVolumeReversalSignal(
            None,
            0.5,
            0.0,
            "climax_volume_reversal: no opposite confirmation candle",
            features,
        )

    volume_strength = min(1.0, (volume_ratio - vol_mult) / max(vol_mult, 1e-6))
    range_strength = min(1.0, (range_ratio - rng_mult) / max(rng_mult, 1e-6))
    body_strength = min(1.0, max(0.0, body_fraction - body_min) / max(1.0 - body_min, 1e-6))
    confidence = min(0.86, 0.58 + 0.14 * volume_strength + 0.08 * range_strength + 0.06 * body_strength)
    probability = min(0.86, 0.5 + max(0.0, confidence - 0.5))
    features.update(
        {
            "climax_direction": "up" if climax_up else "down",
            "confirmation_direction": "up" if confirm_up else "down",
            "climax_reversal_confirmed": True,
        }
    )
    return ClimaxVolumeReversalSignal(
        direction,
        round(probability, 4),
        round(confidence, 4),
        (
            f"climax_volume_reversal: {direction} fade confirmed "
            f"vol_ratio={volume_ratio:.2f} range_ratio={range_ratio:.2f}"
        ),
        features,
    )


def _bar_float(bar: dict, *names: str) -> float:
    for name in names:
        try:
            return float(bar.get(name) or 0.0)
        except (TypeError, ValueError):
            continue
    return 0.0


def _bar_range(bar: dict) -> float:
    high = _bar_float(bar, "high", "h")
    low = _bar_float(bar, "low", "l")
    return max(0.0, high - low)
