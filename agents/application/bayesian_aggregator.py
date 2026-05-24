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

    Fields:
    - p_win_calibrated: best-fit P(win) from history (point estimate)
    - p_win_conservative: Wilson 95% lower bound (use for thresholding)
    - implied_p_win: market's implied win probability for our side
    - edge: p_win_conservative - implied_p_win
    - actionable: True iff edge >= min_edge AND sample_size >= min_samples
    - reason: short string explaining the decision
    - source_segment: the segment we drew the estimate from
    - sample_size: n behind the estimate
    """

    p_win_calibrated: Optional[float]
    p_win_conservative: Optional[float]
    implied_p_win: float
    edge: Optional[float]
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
            actionable=False,
            reason=f"no_segment_above_min_samples_{min_samples}",
            source_segment="fallback_prior",
            sample_size=0,
        )

    p_cal = stat.winrate or 0.0
    p_cons = stat.wilson_lower or 0.0
    p_use = p_cons if use_wilson else p_cal
    edge = p_use - implied

    actionable = edge >= min_edge
    if actionable:
        reason = f"edge_{round(edge,3)}_at_n{stat.total}"
    else:
        reason = f"edge_below_min_{round(edge,3)}<{min_edge}"

    return EdgeResult(
        p_win_calibrated=round(p_cal, 4),
        p_win_conservative=round(p_cons, 4),
        implied_p_win=round(implied, 4),
        edge=round(edge, 4),
        actionable=actionable,
        reason=reason,
        source_segment=f"{stat.segment}:{stat.key}",
        sample_size=stat.total,
    )
