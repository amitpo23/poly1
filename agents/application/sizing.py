"""Position sizing helpers for live entry agents."""
from __future__ import annotations

import os
from dataclasses import dataclass
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


@dataclass(frozen=True)
class KellySizing:
    amount_usdc: float
    fraction: float
    raw_fraction: float
    edge: float
    raw_ev: float
    enabled: bool
    reason: str

    def features(self) -> dict:
        return {
            "kelly_enabled": self.enabled,
            "kelly_amount_usdc": round(self.amount_usdc, 6),
            "kelly_fraction": round(self.fraction, 6),
            "kelly_raw_fraction": round(self.raw_fraction, 6),
            "edge": round(self.edge, 6),
            "raw_ev": round(self.raw_ev, 6),
            "sizing_reason": self.reason,
        }


def binary_raw_ev(win_probability: float, entry_price: float) -> float:
    price = max(float(entry_price), 1e-9)
    return (float(win_probability) - price) / price


def binary_kelly_fraction(win_probability: float, entry_price: float) -> float:
    p = max(0.0, min(1.0, float(win_probability)))
    c = max(1e-9, min(0.999999, float(entry_price)))
    edge = p - c
    if edge <= 0:
        return 0.0
    return max(0.0, min(1.0, edge / max(1.0 - c, 1e-9)))


def robust_kelly_fraction(
    win_probability: float,
    entry_price: float,
    *,
    probability_variance: float = 0.0,
    penalty_lambda: Optional[float] = None,
) -> float:
    """Kelly fraction dampened for probability-estimation uncertainty.

    Formula: f_hat = f* / (1 + lambda * Var(f*)).  The helper is opt-in and
    leaves the existing sizing path unchanged unless callers provide uncertainty.
    """
    raw = binary_kelly_fraction(win_probability, entry_price)
    variance = max(0.0, float(probability_variance or 0.0))
    if variance <= 0:
        return raw
    lam = (
        penalty_lambda
        if penalty_lambda is not None
        else _env_float("ROBUST_KELLY_VARIANCE_LAMBDA", 25.0)
    )
    lam = max(0.0, float(lam))
    return max(0.0, min(1.0, raw / (1.0 + lam * variance)))


def kelly_size_usdc(
    *,
    balance_usdc: Optional[float],
    win_probability: float,
    entry_price: float,
    fallback_amount_usdc: float,
    max_fraction: Optional[float] = None,
    probability_variance: float = 0.0,
) -> KellySizing:
    fallback = max(0.0, float(fallback_amount_usdc))
    p = max(0.0, min(1.0, float(win_probability)))
    c = max(1e-9, min(0.999999, float(entry_price)))
    edge = p - c
    raw_ev = binary_raw_ev(p, c)
    enabled = _env_bool("KELLY_SIZING_ENABLED", True)
    if not enabled:
        return KellySizing(fallback, 0.0, 0.0, edge, raw_ev, False, "disabled")

    if balance_usdc is not None and not isinstance(balance_usdc, (int, float, str)):
        balance = 0.0
    else:
        try:
            balance = float(balance_usdc) if balance_usdc is not None else 0.0
        except (TypeError, ValueError):
            balance = 0.0
    if balance <= 0:
        return KellySizing(fallback, 0.0, 0.0, edge, raw_ev, True, "missing_balance")

    raw_fraction = binary_kelly_fraction(p, c)
    adjusted_fraction = robust_kelly_fraction(
        p,
        c,
        probability_variance=probability_variance,
    )
    scale = max(0.0, _env_float("KELLY_FRACTION_SCALE", 0.25))
    operator_cap = (
        max_fraction
        if max_fraction is not None
        else _env_float("MAX_AGENT_ALLOCATION_FRACTION", 0.50)
    )
    cap_fraction = max(0.0, min(1.0, float(operator_cap)))
    fraction = min(cap_fraction, adjusted_fraction * scale)
    amount = balance * fraction

    max_position_usdc = _env_float("KELLY_MAX_POSITION_USDC", fallback)
    if max_position_usdc > 0:
        amount = min(amount, max_position_usdc)
    min_position_usdc = _env_float("KELLY_MIN_POSITION_USDC", 0.0)
    if amount < min_position_usdc:
        amount = 0.0

    return KellySizing(
        amount_usdc=round(max(0.0, amount), 6),
        fraction=fraction,
        raw_fraction=raw_fraction,
        edge=edge,
        raw_ev=raw_ev,
        enabled=True,
        reason="robust_kelly" if probability_variance > 0 else "kelly",
    )
