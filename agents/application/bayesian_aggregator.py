"""Bayesian probability aggregator for trade candidates.

The operator's design intent: for any opportunity we evaluate, compute
P(win | all signals + history) and compare it to the market's implied
probability. Only trade when our calibrated probability exceeds the
implied probability by a configurable margin AND the sample size is
large enough to trust the estimate.

This module is the synthesis layer that was missing in the bot. Day 1
populated outcome_status. Day 2 produced per-segment winrate stats.
Day 3 (this module) combines them into a single decision-time call:

    edge = compute_edge(candidate, calibration, market_price)
    if edge.actionable: TRADE
    else: SKIP

The aggregation uses a conservative Wilson-lower-bound approach rather
than raw point estimate, so a 60% winrate at n=3 doesn't override a
30% winrate at n=50.

Caller (scanner_executor in Day 4) decides what to do with the result.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from agents.application.probability_calibrator import (
    CalibrationStat,
    _price_band,
    lookup_winrate,
)


@dataclass(frozen=True)
class EdgeResult:
    """The output of edge computation for a single candidate.

    Two decision modes:
    - winrate-based: edge = wilson_lower(p_win) - implied_p_win; actionable
      when edge >= min_edge. Conservative — rejects when sample is small
      OR magnitude is bad. May miss profitable asymmetric strategies.
    - EV-based: ev = p_win × avg_win + p_loss × avg_loss (in USDC per $1
      position); actionable when ev >= min_ev_usdc. Captures asymmetric
      reward/risk (small winrate × big wins can still be +EV).

    The aggregator runs BOTH and is actionable when EITHER passes.
    """

    p_win_calibrated: Optional[float]
    p_win_conservative: Optional[float]
    implied_p_win: float
    edge: Optional[float]
    expected_value_usdc: Optional[float]
    avg_win_usdc: Optional[float]
    avg_loss_usdc: Optional[float]
    actionable: bool
    reason: str
    source_segment: str
    sample_size: int

    def as_features(self) -> dict:
        return {
            "bayesian_p_win_calibrated": self.p_win_calibrated,
            "bayesian_p_win_conservative": self.p_win_conservative,
            "bayesian_implied_p_win": self.implied_p_win,
            "bayesian_edge": self.edge,
            "bayesian_expected_value_usdc": self.expected_value_usdc,
            "bayesian_avg_win_usdc": self.avg_win_usdc,
            "bayesian_avg_loss_usdc": self.avg_loss_usdc,
            "bayesian_actionable": self.actionable,
            "bayesian_reason": self.reason,
            "bayesian_segment": self.source_segment,
            "bayesian_sample_size": self.sample_size,
        }


def _implied_p_win_for_side(action: str, entry_price: float) -> float:
    """Market's implied probability of OUR side winning.

    Polymarket-anchored: entry_price is the YES (outcomes[0]) price.
    - BUY = bet YES → we win if outcomes[0] resolves YES → implied = entry_price
    - SELL = bet NO → we win if outcomes[0] resolves NO → implied = 1 - entry_price

    This is the price we paid for our token, expressed as probability.
    A profitable trade exists when our calibrated P(win) > implied_p_win.
    """
    if entry_price is None or entry_price <= 0 or entry_price >= 1:
        return 0.5
    if action == "BUY":
        return float(entry_price)
    if action == "SELL":
        return 1.0 - float(entry_price)
    return 0.5


def compute_edge(
    candidate: dict,
    calibration: dict,
    *,
    min_edge: float = 0.05,
    min_samples: int = 5,
    use_wilson: bool = True,
    fallback_global_prior: float = 0.25,
    min_ev_usdc: float = 0.01,
    require_both_modes: bool = False,
) -> EdgeResult:
    """Compute calibrated edge for a candidate.

    `candidate` dict expects these keys:
    - signal_source: str
    - action: 'BUY' or 'SELL'
    - entry_price: float in (0, 1)
    - market_type: optional, used as fallback segment

    `calibration` is the output dict from probability_calibrator.calibrate().

    Returns an `EdgeResult`. Actionable means: the bot should fire this
    trade because our calibrated probability beats market-implied by at
    least `min_edge` AND we have at least `min_samples` historical
    observations in the segment.

    `fallback_global_prior` is used when the lookup returns no segment
    above min_samples. Default 0.25 = the bot's actual aggregate
    winrate, which is conservative.
    """
    signal_source = str(candidate.get("signal_source") or "")
    action = str(candidate.get("action") or "")
    entry_price = candidate.get("entry_price")
    market_type = candidate.get("market_type")
    try:
        entry_price = float(entry_price)
    except (TypeError, ValueError):
        return EdgeResult(
            p_win_calibrated=None,
            p_win_conservative=None,
            implied_p_win=0.5,
            edge=None,
            expected_value_usdc=None,
            avg_win_usdc=None,
            avg_loss_usdc=None,
            actionable=False,
            reason="invalid_entry_price",
            source_segment="none",
            sample_size=0,
        )
    implied = _implied_p_win_for_side(action, entry_price)
    band = _price_band(entry_price)

    # Look up the most specific calibrated segment we have data for.
    stat: Optional[CalibrationStat] = lookup_winrate(
        calibration,
        signal_source=signal_source or None,
        market_type=market_type,
        action=action or None,
        price_band=band,
        min_samples=min_samples,
    )

    if stat is None:
        # No segment with enough samples. Fall back to global prior.
        return EdgeResult(
            p_win_calibrated=fallback_global_prior,
            p_win_conservative=fallback_global_prior,
            implied_p_win=implied,
            edge=fallback_global_prior - implied,
            expected_value_usdc=None,
            avg_win_usdc=None,
            avg_loss_usdc=None,
            actionable=False,
            reason=f"no_segment_above_min_samples_{min_samples}",
            source_segment="fallback_prior",
            sample_size=0,
        )

    p_cal = stat.winrate or 0.0
    p_cons = stat.wilson_lower or 0.0
    p_use = p_cons if use_wilson else p_cal
    edge = p_use - implied

    # RR-aware: expected value per $1 position using calibrated p_win
    # (point estimate, NOT wilson — for EV we use best estimate of mean).
    ev_per_trade = stat.expected_value_per_trade()
    avg_win = stat.avg_win_usdc
    avg_loss = stat.avg_loss_usdc

    edge_actionable = edge >= min_edge
    ev_actionable = ev_per_trade is not None and ev_per_trade >= min_ev_usdc

    if require_both_modes:
        actionable = edge_actionable and ev_actionable
    else:
        # Default: either mode can approve. Lets asymmetric-RR strategies
        # fire even when winrate alone is below threshold.
        actionable = edge_actionable or ev_actionable

    if actionable:
        which = "edge" if edge_actionable else "ev"
        reason = (
            f"{which}_actionable_edge={round(edge,3)}_"
            f"ev=${round(ev_per_trade,4) if ev_per_trade is not None else 'na'}"
            f"_n{stat.total}"
        )
    else:
        reason = (
            f"both_below_min_edge={round(edge,3)}<{min_edge}_"
            f"ev=${round(ev_per_trade,4) if ev_per_trade is not None else 'na'}<{min_ev_usdc}"
        )

    return EdgeResult(
        p_win_calibrated=round(p_cal, 4),
        p_win_conservative=round(p_cons, 4),
        implied_p_win=round(implied, 4),
        edge=round(edge, 4),
        expected_value_usdc=(
            round(ev_per_trade, 4) if ev_per_trade is not None else None
        ),
        avg_win_usdc=round(avg_win, 4) if avg_win is not None else None,
        avg_loss_usdc=round(avg_loss, 4) if avg_loss is not None else None,
        actionable=actionable,
        reason=reason,
        source_segment=f"{stat.segment}:{stat.key}",
        sample_size=stat.total,
    )
