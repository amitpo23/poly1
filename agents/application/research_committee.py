"""Deterministic research committee for market opportunities.

This module borrows the useful shape of multi-agent trading research
systems: separate bull, bear, risk, and portfolio-manager views. It is a
read-only advisor. It does not place orders, allocate capital, or change
live-trading gates.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class CommitteeConfig:
    enabled: bool = True
    min_watchlist_score: float = 0.62
    max_live_risk_score: float = 0.35

    @classmethod
    def from_env(cls) -> "CommitteeConfig":
        return cls(
            enabled=_env_bool("RESEARCH_COMMITTEE_ENABLED", True),
            min_watchlist_score=_env_float("RESEARCH_COMMITTEE_MIN_WATCHLIST_SCORE", 0.62),
            max_live_risk_score=_env_float("RESEARCH_COMMITTEE_MAX_LIVE_RISK_SCORE", 0.35),
        )


@dataclass(frozen=True)
class MarketContext:
    market_slug: str
    strategy: str
    score: float
    market_id: Optional[str] = None
    yes_price: Optional[float] = None
    no_price: Optional[float] = None
    spread_cents: Optional[float] = None
    volume_24h: Optional[float] = None
    liquidity: Optional[float] = None
    days_to_end: Optional[float] = None
    news_count: int = 0
    top_news_headline: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class RoleAssessment:
    role: str
    stance: str
    score: float
    confidence: float
    reasons: list[str]
    risks: list[str]


@dataclass(frozen=True)
class ResearchReport:
    created_ts: str
    market_slug: str
    market_id: Optional[str]
    strategy: str
    final_action: str
    final_score: float
    risk_score: float
    confidence: float
    approved_for_backtest: bool
    approved_for_live: bool
    assessments: list[RoleAssessment]
    features: dict
    conclusion: str

    def to_dict(self) -> dict:
        data = asdict(self)
        return data


class ResearchCommittee:
    """Read-only research advisor for candidate opportunities."""

    STRATEGY_NOTES = {
        "mean_reversion": {
            "status": "failed_gate",
            "risk": "Historical realistic-slippage sweeps did not pass the activation gate.",
        },
        "market_maker": {
            "status": "paper_only",
            "risk": "Execution path is not proven live and prior runs hit slippage/404 noise.",
        },
        "nothing_happens": {
            "status": "needs_split_backtest",
            "risk": "The idea is regime-dependent and needs split-window proof before live activation.",
        },
        "btc_daily": {
            "status": "live_probe",
            "risk": "Small live probe is allowed, but evidence is still too thin for larger sizing.",
        },
    }

    def __init__(self, cfg: Optional[CommitteeConfig] = None):
        self.cfg = cfg or CommitteeConfig.from_env()

    def review(self, ctx: MarketContext) -> ResearchReport:
        features = self._features(ctx)
        bull = self._bull_case(ctx, features)
        bear = self._bear_case(ctx, features)
        risk = self._risk_review(ctx, features)

        support = bull.score
        pressure = 0.55 * bear.score + 0.45 * risk.score
        final_score = round(_clamp(0.18 + support - pressure), 3)
        risk_score = round(risk.score, 3)
        confidence = round(_clamp(0.45 + 0.25 * features["data_depth"] + 0.20 * features["news_score"]), 3)

        note = self.STRATEGY_NOTES.get(ctx.strategy, {})
        status = note.get("status", "research_only")
        approved_for_live = False
        approved_for_backtest = final_score >= self.cfg.min_watchlist_score or status in {
            "failed_gate",
            "paper_only",
            "needs_split_backtest",
        }

        if status == "failed_gate":
            action = "reject_live_backtest_required"
            conclusion = "Reject live activation. Only a changed variant with fresh backtest evidence should proceed."
        elif status == "paper_only":
            action = "paper_trade_only"
            conclusion = "Paper-trade and execution-test first; do not deploy capital from this report."
        elif status == "needs_split_backtest":
            action = "watchlist_split_backtest"
            conclusion = "Add to research queue, then require split-window backtest before live use."
        elif final_score >= self.cfg.min_watchlist_score and risk_score <= self.cfg.max_live_risk_score:
            action = "watchlist_backtest"
            conclusion = "Strong enough for a backtest ticket, but still not live-approved by the committee."
        else:
            action = "research_only"
            conclusion = "Keep as research context; current evidence is not strong enough."

        manager = RoleAssessment(
            role="portfolio_manager",
            stance=action,
            score=final_score,
            confidence=confidence,
            reasons=[conclusion, "approved_for_live is hard-blocked in this read-only committee."],
            risks=[] if risk_score < 0.50 else ["risk_score_above_comfort_zone"],
        )
        return ResearchReport(
            created_ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            market_slug=ctx.market_slug,
            market_id=ctx.market_id,
            strategy=ctx.strategy,
            final_action=action,
            final_score=final_score,
            risk_score=risk_score,
            confidence=confidence,
            approved_for_backtest=approved_for_backtest,
            approved_for_live=approved_for_live,
            assessments=[bull, bear, risk, manager],
            features=features,
            conclusion=conclusion,
        )

    def _features(self, ctx: MarketContext) -> dict:
        volume = float(ctx.volume_24h or 0.0)
        liquidity = float(ctx.liquidity or 0.0)
        spread = ctx.spread_cents
        days = ctx.days_to_end
        news_count = int(ctx.news_count or 0)
        return {
            "strategy_score": round(_clamp(ctx.score), 3),
            "volume_score": round(_clamp(volume / 75_000.0), 3),
            "liquidity_score": round(_clamp(liquidity / 100_000.0), 3),
            "news_score": round(_clamp(news_count / 3.0), 3),
            "spread_capture_score": round(_clamp(((spread or 0.0) - 1.5) / 4.5), 3),
            "wide_spread_risk": bool(spread is not None and spread > 5.0),
            "short_expiry_risk": bool(days is not None and days < 1.0),
            "low_liquidity_risk": bool(liquidity < 25_000.0),
            "data_depth": round(_clamp(
                (1 if ctx.yes_price is not None else 0)
                + (1 if ctx.no_price is not None else 0)
                + (1 if spread is not None else 0)
                + (1 if days is not None else 0),
                0,
                4,
            ) / 4.0, 3),
            "strategy_status": self.STRATEGY_NOTES.get(ctx.strategy, {}).get("status", "unknown"),
        }

    def _bull_case(self, ctx: MarketContext, f: dict) -> RoleAssessment:
        score = round(_clamp(
            0.45 * f["strategy_score"]
            + 0.20 * f["volume_score"]
            + 0.20 * f["liquidity_score"]
            + 0.15 * f["news_score"]
        ), 3)
        reasons = [
            f"heuristic_score={f['strategy_score']}",
            f"volume_score={f['volume_score']}",
            f"liquidity_score={f['liquidity_score']}",
        ]
        if ctx.strategy == "market_maker":
            score = round(_clamp(score + 0.15 * f["spread_capture_score"]), 3)
            reasons.append(f"spread_capture_score={f['spread_capture_score']}")
        if ctx.news_count:
            reasons.append(f"news_items={ctx.news_count}")
        return RoleAssessment(
            role="bull_researcher",
            stance="opportunity",
            score=score,
            confidence=round(_clamp(0.45 + 0.35 * f["data_depth"]), 3),
            reasons=reasons,
            risks=[],
        )

    def _bear_case(self, ctx: MarketContext, f: dict) -> RoleAssessment:
        risks: list[str] = []
        pressure = 0.10
        note = self.STRATEGY_NOTES.get(ctx.strategy)
        if note and note["status"] in {"failed_gate", "paper_only"}:
            pressure += 0.42
            risks.append(note["risk"])
        if f["news_score"] == 0:
            pressure += 0.12
            risks.append("No fresh news context attached to this candidate.")
        if f["low_liquidity_risk"]:
            pressure += 0.16
            risks.append("Liquidity is below the committee comfort floor.")
        if f["strategy_score"] < 0.45:
            pressure += 0.10
            risks.append("Raw scout score is weak.")
        return RoleAssessment(
            role="bear_researcher",
            stance="skeptical",
            score=round(_clamp(pressure), 3),
            confidence=round(_clamp(0.45 + 0.25 * len(risks)), 3),
            reasons=["Look for why the apparent edge may be an artifact."],
            risks=risks,
        )

    def _risk_review(self, ctx: MarketContext, f: dict) -> RoleAssessment:
        risks: list[str] = []
        pressure = 0.12
        note = self.STRATEGY_NOTES.get(ctx.strategy)
        if note:
            risks.append(note["risk"])
            if note["status"] == "failed_gate":
                pressure += 0.38
            elif note["status"] == "paper_only":
                pressure += 0.30
            elif note["status"] == "needs_split_backtest":
                pressure += 0.20
        if f["wide_spread_risk"]:
            pressure += 0.16
            risks.append("Spread is wide enough to hide execution slippage.")
        if f["short_expiry_risk"]:
            pressure += 0.14
            risks.append("Time to resolution is short; exit optionality is limited.")
        if f["data_depth"] < 0.75:
            pressure += 0.10
            risks.append("Market data is incomplete.")
        return RoleAssessment(
            role="risk_manager",
            stance="guarded",
            score=round(_clamp(pressure), 3),
            confidence=round(_clamp(0.55 + 0.10 * len(risks)), 3),
            reasons=["Capital protection outranks novelty."],
            risks=risks,
        )
