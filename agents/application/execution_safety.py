"""Execution safety gates shared by live entry agents."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class ExitableSizeCheck:
    ok: bool
    min_entry_usdc: float
    worst_exit_notional_usdc: float
    reason: str


def exitable_size_check(
    *,
    amount_usdc: float,
    entry_price: float,
    stop_loss_pct: float | None = None,
    min_exit_notional_usdc: float | None = None,
    safety_buffer: float | None = None,
    min_entry_usdc: float | None = None,
) -> ExitableSizeCheck:
    """Return whether an entry can still be exited after a normal stop.

    For a fixed USDC entry, the worst-case exit notional at a stop is roughly
    `amount_usdc * (1 - stop_loss_pct)`. We also enforce an absolute minimum
    entry size because $1-$2 positions created dust/retry loops in live logs.
    """
    stop_loss_pct = (
        _env_float("MIN_EXITABLE_STOP_LOSS_PCT", 0.07)
        if stop_loss_pct is None else float(stop_loss_pct)
    )
    min_exit_notional_usdc = (
        _env_float("MIN_EXIT_NOTIONAL_USDC", 1.0)
        if min_exit_notional_usdc is None else float(min_exit_notional_usdc)
    )
    safety_buffer = (
        _env_float("MIN_EXITABLE_SAFETY_BUFFER", 1.25)
        if safety_buffer is None else float(safety_buffer)
    )
    min_entry_usdc = (
        _env_float("MIN_EXITABLE_ENTRY_USDC", 3.0)
        if min_entry_usdc is None else float(min_entry_usdc)
    )

    if amount_usdc <= 0:
        return ExitableSizeCheck(False, min_entry_usdc, 0.0, "entry_amount_must_be_positive")
    if entry_price <= 0 or entry_price >= 1:
        return ExitableSizeCheck(False, min_entry_usdc, 0.0, "entry_price_out_of_range")

    worst_exit_price = max(0.01, entry_price * (1.0 - max(0.0, stop_loss_pct)))
    shares = amount_usdc / entry_price
    worst_exit_notional = shares * worst_exit_price
    computed_floor = min_exit_notional_usdc * safety_buffer / max(
        0.01, 1.0 - max(0.0, stop_loss_pct)
    )
    required = max(min_entry_usdc, computed_floor)
    if amount_usdc < required or worst_exit_notional < min_exit_notional_usdc * safety_buffer:
        return ExitableSizeCheck(
            False,
            required,
            worst_exit_notional,
            (
                f"entry_not_exitable: amount=${amount_usdc:.4f} "
                f"< min_entry=${required:.4f}; worst_exit_notional="
                f"${worst_exit_notional:.4f}"
            ),
        )
    return ExitableSizeCheck(
        True,
        required,
        worst_exit_notional,
        "entry_exitable_after_stop",
    )
