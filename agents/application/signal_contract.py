"""Canonical signal envelope for all poly1 entry evidence.

Every signal-producing agent should be able to express its opinion in this
shape.  The arbiter can then reason about anchors, vetoes, and EV without
guessing which agent used which private schema.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


VALID_DIRECTIONS = {"yes", "no", "up", "down", "bullish", "bearish", "neutral", "skip"}


@dataclass(frozen=True)
class SignalEnvelope:
    agent_id: str
    market_id: str
    direction: str
    confidence: float
    probability: Optional[float] = None
    time_horizon_sec: Optional[int] = None
    evidence: dict[str, Any] = field(default_factory=dict)
    anchor: bool = False
    veto: bool = False
    reason: str = ""
    signal_source: Optional[str] = None
    created_ts: float = field(default_factory=time.time)
    stale_after_sec: int = 300

    def __post_init__(self) -> None:
        direction = str(self.direction).lower()
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "confidence", _bounded(float(self.confidence), 0.0, 1.0))
        if self.probability is not None:
            object.__setattr__(self, "probability", _bounded(float(self.probability), 0.0, 1.0))
        if direction not in VALID_DIRECTIONS:
            raise ValueError(f"invalid signal direction: {self.direction}")
        if not self.agent_id:
            raise ValueError("agent_id is required")
        if not self.market_id:
            raise ValueError("market_id is required")

    @property
    def is_directional(self) -> bool:
        return self.direction not in {"neutral", "skip"}

    @property
    def age_sec(self) -> float:
        return max(0.0, time.time() - float(self.created_ts))

    @property
    def stale(self) -> bool:
        return self.age_sec > self.stale_after_sec

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SignalEnvelope":
        return cls(**payload)


@dataclass(frozen=True)
class TradeProposal:
    """Canonical executable proposal passed from router to executor.

    A signal can be useful without being executable.  A proposal is the stricter
    shape the final executor needs before it may touch money: market, token,
    side, price/probability, source, and an explicit strategy/exit hint.
    """

    source_decision_id: int
    source_agent: str
    source_strategy: str
    market_id: str
    token_id: str
    side: str
    entry_price: float
    probability: float
    confidence: float
    signal_source: str
    strategy_type: str
    expected_hold_sec: Optional[int] = None
    exit_policy: str = "position_manager"
    evidence: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0

    def __post_init__(self) -> None:
        side = str(self.side).upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError(f"invalid proposal side: {self.side}")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "entry_price", _bounded(float(self.entry_price), 0.0, 1.0))
        object.__setattr__(self, "probability", _bounded(float(self.probability), 0.0, 1.0))
        object.__setattr__(self, "confidence", _bounded(float(self.confidence), 0.0, 1.0))
        object.__setattr__(self, "score", _bounded(float(self.score), 0.0, 1.0))
        if not self.market_id:
            raise ValueError("market_id is required")
        if not self.token_id:
            raise ValueError("token_id is required")
        if not self.source_agent:
            raise ValueError("source_agent is required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_brain_decision(cls, row: dict[str, Any], features: dict[str, Any]) -> "TradeProposal":
        side = _normalize_proposal_side(features.get("selected_side") or row.get("action") or "")
        token_id = str(features.get("selected_token_id") or row.get("token_id") or "")
        entry_price = _first_float(
            features.get("selected_entry_price"),
            features.get("entry_price"),
            features.get("yes_price"),
            row.get("price"),
        )
        probability = _first_float(
            features.get("estimated_win_probability"),
            features.get("probability"),
            row.get("score"),
        )
        confidence = _first_float(features.get("confidence"), row.get("score"), probability)
        signal_source = str(row.get("signal_source") or features.get("signal_source") or row.get("agent") or "")
        strategy_type = infer_strategy_type(row=row, features=features)
        if entry_price is None:
            raise ValueError("missing_entry_price")
        if probability is None:
            raise ValueError("missing_probability")
        return cls(
            source_decision_id=int(row.get("id") or 0),
            source_agent=str(row.get("agent") or ""),
            source_strategy=str(row.get("strategy") or ""),
            market_id=str(row.get("market_id") or features.get("condition_id") or ""),
            token_id=token_id,
            side=side,
            entry_price=entry_price,
            probability=probability,
            confidence=confidence if confidence is not None else probability,
            signal_source=signal_source,
            strategy_type=strategy_type,
            expected_hold_sec=_optional_int(
                features.get("expected_hold_sec")
                or features.get("time_horizon_sec")
                or features.get("max_hold_seconds")
            ),
            exit_policy=str(features.get("exit_policy") or "position_manager"),
            evidence=dict(features),
            score=float(row.get("score") or confidence or probability),
        )


def infer_strategy_type(*, row: dict[str, Any], features: dict[str, Any]) -> str:
    explicit = features.get("strategy_type") or features.get("strategy_family")
    if explicit:
        return str(explicit)
    source = " ".join(
        str(x or "").lower()
        for x in (
            row.get("agent"),
            row.get("strategy"),
            row.get("signal_source"),
            features.get("signal_source"),
            features.get("evidence_route"),
            features.get("question"),
        )
    )
    if "near_resolution" in source or "resolution" in source:
        return "near_resolution"
    if "wallet" in source or "whale" in source:
        return "wallet_follow"
    if "btc_5min" in source or "crypto_tape" in source or "crypto" in source:
        return "crypto_momentum"
    if "manifold" in source or "divergence" in source:
        return "cross_market_divergence"
    if "public_news" in source or "gdelt" in source or "news" in source:
        return "event_news"
    if "technical" in source or "tradingview" in source or "alphainsider" in source:
        return "technical_indicator"
    return "general_binary"


def _bounded(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _normalize_proposal_side(value: Any) -> str:
    side = str(value or "").strip().upper()
    if side.startswith("SHADOW_BUY_NO"):
        return "SELL"
    if side.startswith("SHADOW_BUY_YES"):
        return "BUY"
    if side.startswith("SHADOW_SELL"):
        return "SELL"
    if side.startswith("BUY"):
        return "BUY"
    if side.startswith("SELL"):
        return "SELL"
    return side


def _first_float(*values: Any) -> Optional[float]:
    for value in values:
        try:
            if value is None or value == "":
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _optional_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None
