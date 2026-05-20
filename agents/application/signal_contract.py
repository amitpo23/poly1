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


def _bounded(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
