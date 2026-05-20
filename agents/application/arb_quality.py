"""Cross-venue binary arbitrage quality checks.

This module does not place orders.  It evaluates whether a proposed
prediction-market arbitrage is real after fees, stale-data checks, rule
compatibility, and minimum depth.  The intended use is shadow-first: Oddpool,
Kalshi, Polymarket, or internal scanners can feed quotes here before a signal
is allowed to reach MetaBrain as an anchor.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class BinaryVenueQuote:
    venue: str
    yes_ask: Optional[float] = None
    no_ask: Optional[float] = None
    yes_bid: Optional[float] = None
    no_bid: Optional[float] = None
    yes_depth_usdc: Optional[float] = None
    no_depth_usdc: Optional[float] = None
    rules_key: str = ""
    observed_ts: Optional[float] = None


@dataclass(frozen=True)
class ArbQualityConfig:
    min_net_profit: float = 0.015
    round_trip_cost: float = 0.02
    min_depth_usdc: float = 10.0
    max_quote_age_sec: float = 15.0


@dataclass(frozen=True)
class ArbQualityResult:
    candidate: bool
    reason: str
    net_profit: float = 0.0
    gross_cost: Optional[float] = None
    yes_venue: Optional[str] = None
    no_venue: Optional[str] = None
    blockers: list[str] = field(default_factory=list)
    features: dict = field(default_factory=dict)


def evaluate_binary_cross_venue_arb(
    primary: BinaryVenueQuote,
    secondary: BinaryVenueQuote,
    *,
    cfg: ArbQualityConfig | None = None,
    now: Optional[float] = None,
) -> ArbQualityResult:
    cfg = cfg or ArbQualityConfig()
    now = time.time() if now is None else now
    blockers = _quote_blockers(primary, secondary, cfg=cfg, now=now)

    yes_leg = min(
        [(primary.yes_ask, primary.venue, primary.yes_depth_usdc), (secondary.yes_ask, secondary.venue, secondary.yes_depth_usdc)],
        key=lambda item: _ask_sort_value(item[0]),
    )
    no_leg = min(
        [(primary.no_ask, primary.venue, primary.no_depth_usdc), (secondary.no_ask, secondary.venue, secondary.no_depth_usdc)],
        key=lambda item: _ask_sort_value(item[0]),
    )
    if yes_leg[0] is None or no_leg[0] is None:
        blockers.append("missing_prices")
        return _blocked("missing_prices", blockers)
    if float(yes_leg[2] or 0.0) < cfg.min_depth_usdc or float(no_leg[2] or 0.0) < cfg.min_depth_usdc:
        blockers.append("insufficient_depth")

    gross_cost = float(yes_leg[0]) + float(no_leg[0])
    net_profit = 1.0 - gross_cost - cfg.round_trip_cost
    if yes_leg[1] == no_leg[1]:
        blockers.append("same_venue_pair")
    if net_profit < cfg.min_net_profit:
        blockers.append("net_profit_below_min")
    if blockers:
        reason = blockers[0]
        return ArbQualityResult(
            False,
            reason,
            round(net_profit, 6),
            round(gross_cost, 6),
            yes_leg[1],
            no_leg[1],
            blockers,
            _features(primary, secondary, cfg, gross_cost, net_profit),
        )

    return ArbQualityResult(
        True,
        "arb_candidate",
        round(net_profit, 6),
        round(gross_cost, 6),
        yes_leg[1],
        no_leg[1],
        [],
        _features(primary, secondary, cfg, gross_cost, net_profit),
    )


def _quote_blockers(
    primary: BinaryVenueQuote,
    secondary: BinaryVenueQuote,
    *,
    cfg: ArbQualityConfig,
    now: float,
) -> list[str]:
    blockers: list[str] = []
    if primary.rules_key and secondary.rules_key and primary.rules_key != secondary.rules_key:
        blockers.append("rule_mismatch")
    for quote in (primary, secondary):
        if quote.observed_ts is not None and now - quote.observed_ts > cfg.max_quote_age_sec:
            blockers.append(f"stale_quote:{quote.venue}")
        if quote.yes_ask is not None and not _valid_price(quote.yes_ask):
            blockers.append(f"invalid_yes_ask:{quote.venue}")
        if quote.no_ask is not None and not _valid_price(quote.no_ask):
            blockers.append(f"invalid_no_ask:{quote.venue}")

    return blockers


def _features(
    primary: BinaryVenueQuote,
    secondary: BinaryVenueQuote,
    cfg: ArbQualityConfig,
    gross_cost: Optional[float],
    net_profit: float,
) -> dict:
    return {
        "primary_venue": primary.venue,
        "secondary_venue": secondary.venue,
        "primary_yes_ask": primary.yes_ask,
        "primary_no_ask": primary.no_ask,
        "secondary_yes_ask": secondary.yes_ask,
        "secondary_no_ask": secondary.no_ask,
        "gross_cost": round(gross_cost, 6) if gross_cost is not None else None,
        "round_trip_cost": cfg.round_trip_cost,
        "min_net_profit": cfg.min_net_profit,
        "net_profit": round(net_profit, 6),
    }


def _blocked(reason: str, blockers: list[str]) -> ArbQualityResult:
    return ArbQualityResult(False, reason, blockers=blockers)


def _ask_sort_value(value: Optional[float]) -> float:
    if value is None:
        return 999.0
    return float(value)


def _valid_price(value: float) -> bool:
    return 0.0 < float(value) < 1.0
