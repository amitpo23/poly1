"""Route strategy families by deterministic market regime evidence.

The router does not predict markets by itself.  It answers a narrower question:
given the market state we can observe now, which strategy families are suited
to lead, which should be sized down, and which should wait for a better setup?
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from agents.application.market_microstructure import MicrostructureSnapshot
from agents.application.strategy_catalog import strategy_family


ALL_FAMILIES: tuple[str, ...] = (
    "trend_following",
    "mean_reversion",
    "market_microstructure",
    "market_making",
    "event_driven_relative_value",
    "statistical_arbitrage",
    "volatility_relative_value",
    "news_sentiment_event_driven",
    "machine_learning",
    "other",
)

ALPHAINSIDER_FAMILY_MAP = {
    "trend_momentum": "trend_following",
    "vwap_mean_reversion": "mean_reversion",
    "market_making": "market_making",
    "volatility": "volatility_relative_value",
    "machine_learning": "machine_learning",
    "event_sentiment": "news_sentiment_event_driven",
    "supply_demand": "market_microstructure",
    "other": "other",
}


@dataclass(frozen=True)
class StrategyRegimeVerdict:
    family: str
    allowed: bool
    preferred: bool
    risk_multiplier: float
    edge_multiplier: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegimeRoute:
    regime: str
    confidence: float
    preferred_families: tuple[str, ...]
    allowed_families: tuple[str, ...]
    blocked_families: tuple[str, ...]
    risk_multiplier: float
    edge_multiplier: float
    reason: str

    def verdict_for_family(self, family: str) -> StrategyRegimeVerdict:
        fam = normalize_family(family)
        blocked = fam in self.blocked_families and self.confidence >= 0.65
        preferred = fam in self.preferred_families
        if blocked:
            return StrategyRegimeVerdict(
                family=fam,
                allowed=False,
                preferred=False,
                risk_multiplier=0.0,
                edge_multiplier=1.5,
                reason=f"{fam}_blocked_in_{self.regime}",
            )
        risk_multiplier = self.risk_multiplier
        edge_multiplier = self.edge_multiplier
        if preferred:
            risk_multiplier = min(1.25, risk_multiplier * 1.1)
            edge_multiplier = max(0.85, edge_multiplier * 0.9)
        elif fam not in self.allowed_families:
            risk_multiplier = min(risk_multiplier, 0.5)
            edge_multiplier = max(edge_multiplier, 1.25)
        return StrategyRegimeVerdict(
            family=fam,
            allowed=True,
            preferred=preferred,
            risk_multiplier=round(risk_multiplier, 4),
            edge_multiplier=round(edge_multiplier, 4),
            reason=f"{fam}_{'preferred' if preferred else 'allowed'}_in_{self.regime}",
        )

    def features_for_family(self, family: str) -> dict[str, Any]:
        verdict = self.verdict_for_family(family)
        return {
            "strategy_family": verdict.family,
            "regime": self.regime,
            "regime_confidence": round(self.confidence, 4),
            "regime_preferred_families": list(self.preferred_families),
            "regime_allowed_families": list(self.allowed_families),
            "regime_blocked_families": list(self.blocked_families),
            "regime_risk_multiplier": verdict.risk_multiplier,
            "regime_edge_multiplier": verdict.edge_multiplier,
            "regime_family_preferred": verdict.preferred,
            "regime_family_allowed": verdict.allowed,
            "regime_reason": verdict.reason,
            "regime_route_reason": self.reason,
        }


def route_for_snapshot(snapshot: Optional[MicrostructureSnapshot]) -> RegimeRoute:
    if snapshot is None:
        return route_for_regime("unknown", confidence=0.0)
    route = route_for_regime(
        snapshot.regime,
        confidence=snapshot.regime_confidence,
        volatility_pct=snapshot.volatility_pct,
    )
    return route


def route_for_features(features: dict[str, Any]) -> RegimeRoute:
    regime = str(
        features.get("micro_regime")
        or features.get("market_regime")
        or features.get("regime")
        or "unknown"
    )
    confidence = _float(
        features.get("micro_regime_confidence")
        or features.get("market_regime_confidence")
        or features.get("regime_confidence"),
        0.0,
    )
    volatility_pct = _float(
        features.get("micro_volatility_pct")
        or features.get("volatility_pct")
        or features.get("realized_volatility_pct"),
        None,
    )
    return route_for_regime(regime, confidence=confidence, volatility_pct=volatility_pct)


def route_for_regime(
    regime: str,
    *,
    confidence: float = 0.0,
    volatility_pct: Optional[float] = None,
) -> RegimeRoute:
    reg = normalize_regime(regime)
    conf = max(0.0, min(1.0, float(confidence or 0.0)))
    high_vol = volatility_pct is not None and float(volatility_pct) >= 0.012
    if reg == "trending":
        return RegimeRoute(
            regime=reg,
            confidence=conf,
            preferred_families=("trend_following", "event_driven_relative_value", "news_sentiment_event_driven"),
            allowed_families=(
                "trend_following",
                "event_driven_relative_value",
                "news_sentiment_event_driven",
                "statistical_arbitrage",
                "volatility_relative_value",
                "market_microstructure",
            ),
            blocked_families=("mean_reversion",) if conf >= 0.65 else (),
            risk_multiplier=0.8 if high_vol else 1.0,
            edge_multiplier=1.15 if high_vol else 1.0,
            reason="positive_autocorrelation_prefers_momentum_and_event_flow",
        )
    if reg == "mean_reverting":
        return RegimeRoute(
            regime=reg,
            confidence=conf,
            preferred_families=("mean_reversion", "market_microstructure", "market_making", "statistical_arbitrage"),
            allowed_families=(
                "mean_reversion",
                "market_microstructure",
                "market_making",
                "statistical_arbitrage",
                "volatility_relative_value",
            ),
            blocked_families=("trend_following",) if conf >= 0.65 else (),
            risk_multiplier=0.9 if high_vol else 1.0,
            edge_multiplier=1.1 if high_vol else 1.0,
            reason="negative_autocorrelation_prefers_fade_spread_and_pairs",
        )
    if reg == "stretched":
        return RegimeRoute(
            regime=reg,
            confidence=max(conf, 0.55),
            preferred_families=("mean_reversion", "market_microstructure", "volatility_relative_value"),
            allowed_families=("mean_reversion", "market_microstructure", "volatility_relative_value", "event_driven_relative_value"),
            blocked_families=("trend_following",),
            risk_multiplier=0.75,
            edge_multiplier=1.2,
            reason="extreme_zscore_requires_reversal_or_volatility_confirmation",
        )
    if reg == "mixed":
        return RegimeRoute(
            regime=reg,
            confidence=conf,
            preferred_families=("statistical_arbitrage", "market_microstructure"),
            allowed_families=ALL_FAMILIES,
            blocked_families=(),
            risk_multiplier=0.8,
            edge_multiplier=1.1,
            reason="mixed_regime_allows_small_size_until_clearer_signal",
        )
    return RegimeRoute(
        regime="unknown",
        confidence=conf,
        preferred_families=(),
        allowed_families=ALL_FAMILIES,
        blocked_families=(),
        risk_multiplier=0.7,
        edge_multiplier=1.2,
        reason="missing_regime_evidence_requires_conservative_size",
    )


def family_from_signal(
    *,
    strategy_id: Optional[str] = None,
    agent: Optional[str] = None,
    signal_source: Optional[str] = None,
    features: Optional[dict[str, Any]] = None,
) -> str:
    features = features or {}
    explicit = features.get("strategy_family") or features.get("family")
    if explicit:
        return normalize_family(str(explicit))
    alpha_family = features.get("alphainsider_family")
    if alpha_family:
        return normalize_family(str(alpha_family))
    if strategy_id:
        fam = strategy_family(str(strategy_id), default="")
        if fam:
            return fam
    text = " ".join(
        str(x or "").lower()
        for x in (
            strategy_id,
            agent,
            signal_source,
            features.get("estimated_win_probability_source"),
            features.get("route_agent"),
            features.get("evidence_route"),
        )
    )
    if "market_maker" in text or "quote" in text:
        return "market_making"
    if "scalper" in text or "vwap" in text or "supply_demand" in text:
        return "market_microstructure"
    if "mean_reversion" in text or "reversal" in text or "climax" in text:
        return "mean_reversion"
    if "trend" in text or "momentum" in text or "btc_5min" in text or "crypto_tape" in text:
        return "trend_following"
    if "wallet" in text or "whale" in text or "arb" in text:
        return "event_driven_relative_value"
    if "options" in text or "volatility" in text or "vix" in text:
        return "volatility_relative_value"
    if "news" in text or "tavily" in text or "sentiment" in text:
        return "news_sentiment_event_driven"
    if "ml" in text or "machine" in text or "rl_" in text:
        return "machine_learning"
    return "other"


def normalize_family(family: str) -> str:
    value = (family or "other").strip().lower().replace("-", "_").replace(" ", "_")
    value = ALPHAINSIDER_FAMILY_MAP.get(value, value)
    if value in ALL_FAMILIES:
        return value
    return "other"


def normalize_regime(regime: str) -> str:
    value = (regime or "unknown").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "trend": "trending",
        "momentum": "trending",
        "mean_reversion": "mean_reverting",
        "reverting": "mean_reverting",
        "range": "mean_reverting",
        "range_bound": "mean_reverting",
        "overextended": "stretched",
        "volatile": "stretched",
    }
    return aliases.get(value, value if value in {"trending", "mean_reverting", "stretched", "mixed"} else "unknown")


def _float(value: Any, default: Optional[float]) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default
