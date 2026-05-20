"""Agent registry loader and validator.

The registry is the commander's roll-call: one source of truth for which
agents exist, what they are allowed to do, and what evidence they must provide
before a live promotion.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[2] / "config" / "agent_registry.json"


@dataclass(frozen=True)
class AgentSpec:
    agent_id: str
    display_name: str
    role: str
    mode: str
    places_orders: bool
    markets: list[str]
    owner_service: str
    required_inputs: list[str]
    signal_schema: str
    anchor_rules: dict[str, Any] = field(default_factory=dict)
    veto_rules: list[str] = field(default_factory=list)
    promotion_criteria: dict[str, Any] = field(default_factory=dict)
    requires_brain_approval: bool = True
    requires_positive_ev: bool = True

    @classmethod
    def from_dict(cls, payload: dict[str, Any], defaults: Optional[dict[str, Any]] = None) -> "AgentSpec":
        defaults = defaults or {}
        missing = [
            key for key in (
                "agent_id",
                "display_name",
                "role",
                "mode",
                "places_orders",
                "markets",
                "owner_service",
                "required_inputs",
                "signal_schema",
            )
            if key not in payload
        ]
        if missing:
            raise ValueError(f"agent spec missing required fields: {','.join(missing)}")
        return cls(
            agent_id=str(payload["agent_id"]),
            display_name=str(payload["display_name"]),
            role=str(payload["role"]),
            mode=str(payload["mode"]),
            places_orders=bool(payload["places_orders"]),
            markets=list(payload.get("markets") or []),
            owner_service=str(payload["owner_service"]),
            required_inputs=list(payload.get("required_inputs") or []),
            signal_schema=str(payload["signal_schema"]),
            anchor_rules=dict(payload.get("anchor_rules") or {}),
            veto_rules=list(payload.get("veto_rules") or []),
            promotion_criteria=dict(payload.get("promotion_criteria") or {}),
            requires_brain_approval=bool(payload.get("requires_brain_approval", defaults.get("requires_brain_approval", True))),
            requires_positive_ev=bool(payload.get("requires_positive_ev", defaults.get("requires_positive_ev", True))),
        )

    @property
    def can_anchor(self) -> bool:
        return bool(self.anchor_rules.get("allowed"))

    @property
    def live_capable(self) -> bool:
        return self.mode in {"live_candidate", "live"} and self.places_orders


@dataclass(frozen=True)
class AgentRegistry:
    version: int
    updated_at: str
    defaults: dict[str, Any]
    agents: dict[str, AgentSpec]

    @classmethod
    def load(cls, path: str | Path = DEFAULT_REGISTRY_PATH) -> "AgentRegistry":
        payload = json.loads(Path(path).read_text())
        defaults = dict(payload.get("defaults") or {})
        agents = {}
        for raw in payload.get("agents") or []:
            spec = AgentSpec.from_dict(raw, defaults)
            if spec.agent_id in agents:
                raise ValueError(f"duplicate agent_id in registry: {spec.agent_id}")
            agents[spec.agent_id] = spec
        registry = cls(
            version=int(payload.get("version", 0)),
            updated_at=str(payload.get("updated_at", "")),
            defaults=defaults,
            agents=agents,
        )
        registry.validate()
        return registry

    def get(self, agent_id: str) -> Optional[AgentSpec]:
        return self.agents.get(agent_id)

    def require(self, agent_id: str) -> AgentSpec:
        spec = self.get(agent_id)
        if spec is None:
            raise KeyError(f"agent not registered: {agent_id}")
        return spec

    def validate(self) -> None:
        if self.version <= 0:
            raise ValueError("agent registry version must be positive")
        if not self.agents:
            raise ValueError("agent registry must contain at least one agent")
        allowed_modes = {"disabled", "shadow", "live_candidate", "live"}
        for spec in self.agents.values():
            if spec.mode not in allowed_modes:
                raise ValueError(f"{spec.agent_id}: invalid mode {spec.mode}")
            if not spec.role:
                raise ValueError(f"{spec.agent_id}: role is required")
            if not spec.required_inputs:
                raise ValueError(f"{spec.agent_id}: required_inputs must not be empty")
            if spec.places_orders and not spec.requires_brain_approval and spec.agent_id != "position_manager":
                raise ValueError(f"{spec.agent_id}: money-touching entries must require brain approval")
            if spec.mode == "live" and spec.places_orders:
                criteria = spec.promotion_criteria
                if not criteria:
                    raise ValueError(f"{spec.agent_id}: live order agents need promotion criteria")

    def summary(self) -> dict[str, Any]:
        by_mode: dict[str, int] = {}
        by_role: dict[str, int] = {}
        for spec in self.agents.values():
            by_mode[spec.mode] = by_mode.get(spec.mode, 0) + 1
            by_role[spec.role] = by_role.get(spec.role, 0) + 1
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "agent_count": len(self.agents),
            "by_mode": by_mode,
            "by_role": by_role,
            "live_capable_agents": sorted(spec.agent_id for spec in self.agents.values() if spec.live_capable),
            "anchor_capable_agents": sorted(spec.agent_id for spec in self.agents.values() if spec.can_anchor),
        }


def load_agent_registry(path: str | Path = DEFAULT_REGISTRY_PATH) -> AgentRegistry:
    return AgentRegistry.load(path)
