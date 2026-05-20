"""Deterministic MetaBrain arbiter.

This is intentionally not a weighted-average brain.  It implements the policy
we settled on: a proven strong indicator may lead on its own, missing agents do
not dilute the decision, strong vetoes stop the trade, and no entry is valid
without positive edge after costs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from agents.application.agent_registry import AgentRegistry, AgentSpec, load_agent_registry
from agents.application.signal_contract import SignalEnvelope
from agents.application.sizing import binary_raw_ev


@dataclass(frozen=True)
class ArbiterDecision:
    approved: bool
    decision: str
    reason: str
    primary_trigger: Optional[str]
    supporting_signals: list[str]
    vetoes: list[str]
    internal_probability: float
    entry_price: float
    raw_ev: float
    net_ev: float
    min_net_ev: float
    mode: str
    max_size_usdc: Optional[float] = None
    exit_plan: dict[str, Any] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArbiterConfig:
    min_anchor_confidence: float = 0.62
    min_consensus_confidence: float = 0.55
    min_net_ev: float = 0.025
    round_trip_cost_pct: float = 0.04
    stale_signal_blocks: bool = True
    min_supporting_signals: int = 2


class MetaArbiter:
    def __init__(
        self,
        *,
        registry: Optional[AgentRegistry] = None,
        cfg: Optional[ArbiterConfig] = None,
    ):
        self.registry = registry or load_agent_registry()
        self.cfg = cfg or ArbiterConfig()

    def decide(
        self,
        *,
        market_id: str,
        entry_price: float,
        signals: Iterable[SignalEnvelope],
        exit_plan: Optional[dict[str, Any]] = None,
        max_size_usdc: Optional[float] = None,
    ) -> ArbiterDecision:
        exit_plan = exit_plan or {}
        signals = [s for s in signals if s.market_id == market_id]
        if not signals:
            return self._reject("no_signals", None, [], [], 0.5, entry_price, "none", exit_plan)
        if not exit_plan:
            return self._reject("missing_exit_plan", None, [], [], 0.5, entry_price, "invalid", exit_plan)
        price = _price(entry_price)
        if price <= 0.0:
            return self._reject("invalid_entry_price", None, [], [], 0.5, entry_price, "invalid", exit_plan)

        vetoes = []
        active = []
        for signal in signals:
            spec = self.registry.get(signal.agent_id)
            if spec is None:
                vetoes.append(f"{signal.agent_id}:unregistered_agent")
                continue
            if signal.stale and self.cfg.stale_signal_blocks:
                vetoes.append(f"{signal.agent_id}:stale_signal")
                continue
            if signal.veto:
                vetoes.append(f"{signal.agent_id}:veto")
                continue
            if signal.direction in {"skip", "neutral"}:
                continue
            active.append((signal, spec))

        if vetoes:
            return self._reject("strong_veto", None, [], vetoes, 0.5, price, "blocked", exit_plan)
        if not active:
            return self._reject("no_active_directional_signals", None, [], [], 0.5, price, "none", exit_plan)

        anchors = [(s, spec) for s, spec in active if self._is_anchor(s, spec)]
        if anchors:
            leader, leader_spec = max(anchors, key=lambda item: item[0].confidence)
            probability = self._signal_probability(leader)
            raw_ev = binary_raw_ev(probability, price)
            net_ev = raw_ev - self.cfg.round_trip_cost_pct
            min_net_ev = self._min_net_ev(leader_spec)
            if net_ev < min_net_ev:
                return self._reject(
                    "anchor_net_ev_too_low",
                    leader.agent_id,
                    [s.agent_id for s, _ in active if s.agent_id != leader.agent_id],
                    [],
                    probability,
                    price,
                    "anchor",
                    exit_plan,
                    raw_ev=raw_ev,
                    net_ev=net_ev,
                    min_net_ev=min_net_ev,
                )
            return ArbiterDecision(
                approved=True,
                decision="approve",
                reason="anchor_approved",
                primary_trigger=leader.agent_id,
                supporting_signals=[s.agent_id for s, _ in active if s.agent_id != leader.agent_id],
                vetoes=[],
                internal_probability=probability,
                entry_price=price,
                raw_ev=raw_ev,
                net_ev=net_ev,
                min_net_ev=min_net_ev,
                mode="anchor",
                max_size_usdc=max_size_usdc,
                exit_plan=exit_plan,
                features=self._features(active, leader.agent_id),
            )

        groups: dict[str, list[SignalEnvelope]] = {}
        for signal, _spec in active:
            canonical = _canonical_direction(signal.direction)
            groups.setdefault(canonical, []).append(signal)
        direction, group = max(groups.items(), key=lambda item: (len(item[1]), sum(s.confidence for s in item[1])))
        if len(group) < self.cfg.min_supporting_signals:
            return self._reject("no_anchor_and_insufficient_consensus", None, [s.agent_id for s in group], [], 0.5, price, "consensus", exit_plan)
        probability = sum(self._signal_probability(s) * s.confidence for s in group) / max(0.0001, sum(s.confidence for s in group))
        raw_ev = binary_raw_ev(probability, price)
        net_ev = raw_ev - self.cfg.round_trip_cost_pct
        if net_ev < self.cfg.min_net_ev:
            return self._reject("consensus_net_ev_too_low", group[0].agent_id, [s.agent_id for s in group[1:]], [], probability, price, "consensus", exit_plan, raw_ev=raw_ev, net_ev=net_ev, min_net_ev=self.cfg.min_net_ev)
        return ArbiterDecision(
            approved=True,
            decision="approve",
            reason="consensus_approved",
            primary_trigger=group[0].agent_id,
            supporting_signals=[s.agent_id for s in group[1:]],
            vetoes=[],
            internal_probability=probability,
            entry_price=price,
            raw_ev=raw_ev,
            net_ev=net_ev,
            min_net_ev=self.cfg.min_net_ev,
            mode="consensus",
            max_size_usdc=max_size_usdc,
            exit_plan=exit_plan,
            features={**self._features(active, group[0].agent_id), "consensus_direction": direction},
        )

    def _is_anchor(self, signal: SignalEnvelope, spec: AgentSpec) -> bool:
        if not spec.can_anchor or not signal.anchor:
            return False
        min_conf = float(spec.anchor_rules.get("min_confidence", self.cfg.min_anchor_confidence))
        if signal.confidence < min_conf:
            return False
        min_wallet_winrate = spec.anchor_rules.get("min_wallet_winrate_30d")
        if min_wallet_winrate is not None:
            if float(signal.evidence.get("wallet_winrate_30d", 0.0)) < float(min_wallet_winrate):
                return False
        min_wallet_profit = spec.anchor_rules.get("min_wallet_profit_usdc")
        if min_wallet_profit is not None:
            if float(signal.evidence.get("wallet_profit_usdc", 0.0)) < float(min_wallet_profit):
                return False
        return True

    def _signal_probability(self, signal: SignalEnvelope) -> float:
        if signal.probability is not None:
            return signal.probability
        return max(0.5, min(0.99, 0.5 + (signal.confidence - 0.5)))

    def _min_net_ev(self, spec: AgentSpec) -> float:
        raw = spec.anchor_rules.get("min_edge_after_costs", self.cfg.min_net_ev)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return self.cfg.min_net_ev

    def _reject(
        self,
        reason: str,
        primary: Optional[str],
        supporting: list[str],
        vetoes: list[str],
        probability: float,
        price: float,
        mode: str,
        exit_plan: dict[str, Any],
        *,
        raw_ev: Optional[float] = None,
        net_ev: Optional[float] = None,
        min_net_ev: Optional[float] = None,
    ) -> ArbiterDecision:
        price = _price(price)
        raw = binary_raw_ev(probability, price) if raw_ev is None and price > 0 else (raw_ev or 0.0)
        net = raw - self.cfg.round_trip_cost_pct if net_ev is None else net_ev
        return ArbiterDecision(
            approved=False,
            decision="reject",
            reason=reason,
            primary_trigger=primary,
            supporting_signals=supporting,
            vetoes=vetoes,
            internal_probability=probability,
            entry_price=price,
            raw_ev=raw,
            net_ev=net,
            min_net_ev=self.cfg.min_net_ev if min_net_ev is None else min_net_ev,
            mode=mode,
            exit_plan=exit_plan,
            features={"vetoes": vetoes},
        )

    def _features(self, active: list[tuple[SignalEnvelope, AgentSpec]], leader: Optional[str]) -> dict[str, Any]:
        return {
            "arbiter": "meta_arbiter_v1",
            "primary_trigger": leader,
            "active_signal_count": len(active),
            "active_agents": [signal.agent_id for signal, _ in active],
            "missing_agents_ignored": True,
            "weighting_policy": "anchor_or_consensus_not_naive_average",
        }


def _price(value: float) -> float:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return 0.0
    if price <= 0.0 or price >= 1.0:
        return 0.0
    return price


def _canonical_direction(direction: str) -> str:
    direction = str(direction).lower()
    if direction in {"yes", "up", "bullish"}:
        return "yes"
    if direction in {"no", "down", "bearish"}:
        return "no"
    return direction
