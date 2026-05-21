"""Small research harness inspired by DeerFlow.

This is not a general-purpose agent runtime.  It is a deterministic contract
for poly1 research work: which skills exist, which tasks may use them, what
guardrails apply, and how queued strategy ideas become backtest/shadow plans.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


ALLOWED_MODES = {"flash", "standard", "pro", "ultra"}
ALLOWED_GUARDRAILS = {
    "no_live_trading",
    "shadow_only",
    "read_only",
    "no_secret_export",
    "requires_backtest",
    "requires_markout",
    "requires_rule_mapping",
    "requires_human_approval",
    "requires_bias_audit",
    "requires_walk_forward",
    "offline_only",
}


@dataclass(frozen=True)
class ResearchSkill:
    skill_id: str
    purpose: str
    inputs: list[str]
    outputs: list[str]
    guardrails: list[str] = field(default_factory=list)
    max_parallel_tasks: int = 1

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ResearchSkill":
        required = ("skill_id", "purpose", "inputs", "outputs")
        missing = [key for key in required if key not in payload]
        if missing:
            raise ValueError(f"research skill missing fields: {','.join(missing)}")
        return cls(
            skill_id=str(payload["skill_id"]),
            purpose=str(payload["purpose"]),
            inputs=list(payload.get("inputs") or []),
            outputs=list(payload.get("outputs") or []),
            guardrails=list(payload.get("guardrails") or []),
            max_parallel_tasks=int(payload.get("max_parallel_tasks") or 1),
        )


@dataclass(frozen=True)
class HarnessConfig:
    version: int
    updated_at: str
    max_parallel_tasks: int
    default_mode: str
    skills: dict[str, ResearchSkill]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "HarnessConfig":
        skills = {}
        for raw in payload.get("skills") or []:
            skill = ResearchSkill.from_dict(raw)
            if skill.skill_id in skills:
                raise ValueError(f"duplicate research skill: {skill.skill_id}")
            skills[skill.skill_id] = skill
        cfg = cls(
            version=int(payload.get("version") or 0),
            updated_at=str(payload.get("updated_at") or ""),
            max_parallel_tasks=int(payload.get("max_parallel_tasks") or 1),
            default_mode=str(payload.get("default_mode") or "standard"),
            skills=skills,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.version <= 0:
            raise ValueError("research harness version must be positive")
        if self.default_mode not in ALLOWED_MODES:
            raise ValueError(f"invalid default_mode: {self.default_mode}")
        if self.max_parallel_tasks < 1 or self.max_parallel_tasks > 3:
            raise ValueError("max_parallel_tasks must be between 1 and 3")
        if not self.skills:
            raise ValueError("research harness must define at least one skill")
        for skill in self.skills.values():
            if not skill.inputs:
                raise ValueError(f"{skill.skill_id}: inputs must not be empty")
            if not skill.outputs:
                raise ValueError(f"{skill.skill_id}: outputs must not be empty")
            if skill.max_parallel_tasks < 1 or skill.max_parallel_tasks > self.max_parallel_tasks:
                raise ValueError(f"{skill.skill_id}: max_parallel_tasks exceeds harness limit")
            unknown = sorted(set(skill.guardrails) - ALLOWED_GUARDRAILS)
            if unknown:
                raise ValueError(f"{skill.skill_id}: unknown guardrails: {','.join(unknown)}")


@dataclass(frozen=True)
class ResearchRunPlan:
    item_id: str
    mode: str
    skill_ids: list[str]
    objective: str
    guardrails: list[str]
    required_outputs: list[str]
    blocked: bool = False
    block_reason: Optional[str] = None


def build_run_plans(queue: dict[str, Any], cfg: HarnessConfig) -> list[ResearchRunPlan]:
    plans: list[ResearchRunPlan] = []
    for item in queue.get("items") or []:
        item_id = str(item.get("id") or "")
        status = str(item.get("status") or "")
        objective = str(item.get("expected_value_hypothesis") or "")
        skill_ids = _skills_for_item(item)
        missing = [skill_id for skill_id in skill_ids if skill_id not in cfg.skills]
        if status == "deferred":
            plans.append(
                ResearchRunPlan(
                    item_id=item_id,
                    mode="standard",
                    skill_ids=skill_ids,
                    objective=objective,
                    guardrails=["no_live_trading", "requires_human_approval"],
                    required_outputs=list(item.get("required_evidence") or []),
                    blocked=True,
                    block_reason="deferred_until_reproducible",
                )
            )
            continue
        if missing:
            plans.append(
                ResearchRunPlan(
                    item_id=item_id,
                    mode="standard",
                    skill_ids=skill_ids,
                    objective=objective,
                    guardrails=["no_live_trading"],
                    required_outputs=list(item.get("required_evidence") or []),
                    blocked=True,
                    block_reason=f"missing_skills:{','.join(missing)}",
                )
            )
            continue
        mode = "pro" if status.startswith("implemented") else cfg.default_mode
        guardrails = sorted({guard for skill_id in skill_ids for guard in cfg.skills[skill_id].guardrails})
        outputs = sorted({out for skill_id in skill_ids for out in cfg.skills[skill_id].outputs})
        plans.append(
            ResearchRunPlan(
                item_id=item_id,
                mode=mode,
                skill_ids=skill_ids,
                objective=objective,
                guardrails=guardrails,
                required_outputs=outputs,
            )
        )
    return plans


def summarize_harness(cfg: HarnessConfig, plans: list[ResearchRunPlan]) -> dict[str, Any]:
    blocked = [plan for plan in plans if plan.blocked]
    return {
        "version": cfg.version,
        "updated_at": cfg.updated_at,
        "skill_count": len(cfg.skills),
        "max_parallel_tasks": cfg.max_parallel_tasks,
        "plan_count": len(plans),
        "blocked_count": len(blocked),
        "ready_plan_ids": [plan.item_id for plan in plans if not plan.blocked],
        "blocked_plan_ids": [plan.item_id for plan in blocked],
    }


def _skills_for_item(item: dict[str, Any]) -> list[str]:
    item_id = str(item.get("id") or "")
    if item_id in {"vwap_microstructure_signal", "mean_reversion_regime_filter"}:
        return ["market_microstructure_review", "shadow_markout_backtest"]
    if item_id == "sports_cheap_hold_sweep":
        return ["market_sweep_bias_audit", "shadow_markout_backtest"]
    if item_id == "crypto_orderflow_footprint":
        return ["orderflow_footprint_review", "shadow_markout_backtest"]
    if item_id == "volatility_relative_value":
        return ["volatility_relative_value_review", "shadow_markout_backtest"]
    if item_id == "rl_reward_policy_lab":
        return ["reward_policy_lab", "shadow_markout_backtest"]
    if item_id == "oddpool_style_cross_venue_arb":
        return ["cross_venue_arb_review", "shadow_markout_backtest"]
    if item_id == "ssrn_research_ingestion":
        return ["paper_review", "research_to_shadow_spec"]
    if item_id == "latent_regime_chaos_score":
        return ["paper_review", "research_to_shadow_spec", "shadow_markout_backtest"]
    return ["research_to_shadow_spec"]
