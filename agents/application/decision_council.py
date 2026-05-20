"""Deterministic decision council for money-touching entry decisions.

The council is intentionally boring code: it turns a brain-approved candidate
into ENTER/WAIT/REJECT using live execution facts, source provenance, and
net-EV thresholds.  LLMs and scanners can suggest; this layer decides whether
the suggestion is still tradeable at the price we can actually fill.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from agents.application.sizing import binary_raw_ev


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class CouncilDecision:
    approved: bool
    action: str
    reason: str
    internal_probability: float
    entry_price: float
    raw_ev: float
    net_ev: float
    min_net_ev: float
    min_raw_ev: float
    mode: str
    features: dict = field(default_factory=dict)


class DecisionCouncil:
    """Final reviewer for execution candidates.

    Rules:
    - expert_solo can use a lower net-EV threshold, but still must beat costs.
    - thin or non-expert markets need a wider edge because exit friction matters.
    - unknown/missing live price or probability fails closed.
    """

    def __init__(
        self,
        *,
        min_raw_ev: float = 0.04,
        min_net_ev: float = 0.04,
        expert_min_net_ev: float = 0.025,
        thin_min_net_ev: float = 0.06,
        round_trip_cost_pct: float = 0.04,
        min_probability: float = 0.52,
        expert_min_probability: float = 0.50,
        thin_liquidity_usdc: float = 5_000.0,
    ):
        self.min_raw_ev = float(min_raw_ev)
        self.min_net_ev = float(min_net_ev)
        self.expert_min_net_ev = float(expert_min_net_ev)
        self.thin_min_net_ev = float(thin_min_net_ev)
        self.round_trip_cost_pct = max(0.0, float(round_trip_cost_pct))
        self.min_probability = float(min_probability)
        self.expert_min_probability = float(expert_min_probability)
        self.thin_liquidity_usdc = float(thin_liquidity_usdc)

    @classmethod
    def from_env(cls, *, min_raw_ev: float, min_net_ev: float, round_trip_cost_pct: float) -> "DecisionCouncil":
        return cls(
            min_raw_ev=min_raw_ev,
            min_net_ev=_env_float("DECISION_COUNCIL_MIN_NET_EV", min_net_ev),
            expert_min_net_ev=_env_float("DECISION_COUNCIL_EXPERT_MIN_NET_EV", 0.025),
            thin_min_net_ev=_env_float("DECISION_COUNCIL_THIN_MIN_NET_EV", 0.06),
            round_trip_cost_pct=round_trip_cost_pct,
            min_probability=_env_float("DECISION_COUNCIL_MIN_PROBABILITY", 0.52),
            expert_min_probability=_env_float("DECISION_COUNCIL_EXPERT_MIN_PROBABILITY", 0.50),
            thin_liquidity_usdc=_env_float("DECISION_COUNCIL_THIN_LIQUIDITY_USDC", 5_000.0),
        )

    def review_entry(
        self,
        *,
        features: dict,
        score: float,
        live_entry_price: float,
        avg_entry_price: Optional[float] = None,
        fillable_usdc: Optional[float] = None,
        signal_source: Optional[str] = None,
    ) -> CouncilDecision:
        entry_price = _bounded_price(avg_entry_price if avg_entry_price is not None else live_entry_price)
        if entry_price <= 0.0:
            return self._reject("invalid_entry_price", 0.0, 0.0, 0.0, self.min_net_ev, "invalid")

        route = features.get("evidence_route") if isinstance(features.get("evidence_route"), dict) else {}
        mode = str(route.get("mode") or "consensus")
        is_expert = mode == "solo"
        liquidity = _safe_float(features.get("liquidity_usdc"), None)
        thin = (
            (fillable_usdc is not None and float(fillable_usdc) < 1.0)
            or (liquidity is not None and liquidity < self.thin_liquidity_usdc)
        )

        internal_probability = _safe_float(
            features.get("internal_probability"),
            _safe_float(features.get("estimated_win_probability"), score),
        )
        internal_probability = max(0.0, min(1.0, internal_probability))
        raw_ev = binary_raw_ev(internal_probability, entry_price)
        net_ev = raw_ev - self.round_trip_cost_pct
        min_net_ev = self._effective_min_net_ev(is_expert=is_expert, thin=thin)
        min_prob = self.expert_min_probability if is_expert else self.min_probability

        council_features = {
            "decision_council_mode": mode,
            "decision_council_signal_source": signal_source,
            "decision_council_is_expert": is_expert,
            "decision_council_thin_market": thin,
            "decision_council_internal_probability": round(internal_probability, 4),
            "decision_council_entry_price": round(entry_price, 4),
            "decision_council_raw_ev": round(raw_ev, 4),
            "decision_council_net_ev": round(net_ev, 4),
            "decision_council_min_net_ev": min_net_ev,
            "decision_council_min_raw_ev": self.min_raw_ev,
            "decision_council_round_trip_cost_pct": self.round_trip_cost_pct,
            "decision_council_min_probability": min_prob,
            "evidence_route_reason": route.get("reason"),
            "evidence_route_leader": route.get("leader"),
        }

        if mode == "blocked":
            return CouncilDecision(
                False, "REJECT", "expert_conflict", internal_probability, entry_price,
                raw_ev, net_ev, min_net_ev, self.min_raw_ev, mode, council_features,
            )
        if internal_probability < min_prob:
            return CouncilDecision(
                False, "REJECT", "probability_below_council_min", internal_probability,
                entry_price, raw_ev, net_ev, min_net_ev, self.min_raw_ev, mode,
                council_features,
            )
        if raw_ev < self.min_raw_ev:
            return CouncilDecision(
                False, "REJECT", "raw_ev_below_council_min", internal_probability,
                entry_price, raw_ev, net_ev, min_net_ev, self.min_raw_ev, mode,
                council_features,
            )
        if net_ev < min_net_ev:
            return CouncilDecision(
                False, "REJECT", "net_ev_below_council_min", internal_probability,
                entry_price, raw_ev, net_ev, min_net_ev, self.min_raw_ev, mode,
                council_features,
            )

        return CouncilDecision(
            True, "ENTER", "council_approved", internal_probability, entry_price,
            raw_ev, net_ev, min_net_ev, self.min_raw_ev, mode, council_features,
        )

    def _effective_min_net_ev(self, *, is_expert: bool, thin: bool) -> float:
        if is_expert:
            return self.expert_min_net_ev
        if thin:
            return max(self.min_net_ev, self.thin_min_net_ev)
        return self.min_net_ev

    def _reject(
        self,
        reason: str,
        internal_probability: float,
        entry_price: float,
        raw_ev: float,
        min_net_ev: float,
        mode: str,
    ) -> CouncilDecision:
        return CouncilDecision(
            False,
            "REJECT",
            reason,
            internal_probability,
            entry_price,
            raw_ev,
            raw_ev - self.round_trip_cost_pct,
            min_net_ev,
            self.min_raw_ev,
            mode,
            {},
        )


def _safe_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bounded_price(value) -> float:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return 0.0
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return price
