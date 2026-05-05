import logging
import os
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class ScalperConfig:
    threshold: float = 0.499
    reversal_delta: float = 0.020
    depth_buy_discount: float = 0.05
    second_side_buffer: float = 0.01
    second_side_time_ms: int = 200
    dynamic_threshold_boost: float = 0.04
    max_sum_avg: float = 0.98
    max_buys_per_side: int = 4
    shares_per_side: float = 5.0
    leg_usdc_cap: float = 5.0
    poll_ms: int = 250
    discover_every_sec: int = 60

    @classmethod
    def from_env(cls) -> "ScalperConfig":
        return cls(
            threshold=_env_float("SCALP_ENTRY_THRESHOLD", 0.499),
            reversal_delta=_env_float("SCALP_REVERSAL_DELTA", 0.020),
            depth_buy_discount=_env_float("SCALP_DEPTH_DISCOUNT", 0.05),
            second_side_buffer=_env_float("SCALP_SECOND_BUFFER", 0.01),
            second_side_time_ms=_env_int("SCALP_SECOND_TIME_MS", 200),
            dynamic_threshold_boost=_env_float("SCALP_DYNAMIC_BOOST", 0.04),
            max_sum_avg=_env_float("SCALP_MAX_SUM_AVG", 0.98),
            max_buys_per_side=_env_int("SCALP_MAX_BUYS_PER_SIDE", 4),
            leg_usdc_cap=_env_float("SCALP_LEG_USDC", 5.0),
            poll_ms=_env_int("SCALP_POLL_MS", 250),
            discover_every_sec=_env_int("SCALP_DISCOVER_EVERY_SEC", 60),
        )


@dataclass
class ScalpPair:
    slug: str
    period_ts: int
    up_token: str
    down_token: str
    cfg: ScalperConfig
    temp_price_up: Optional[float] = None
    temp_price_down: Optional[float] = None
    last_update_up_ms: int = 0
    last_update_down_ms: int = 0
    below_dyn_since_up_ms: Optional[int] = None
    below_dyn_since_down_ms: Optional[int] = None
    dynamic_threshold_up: Optional[float] = None
    dynamic_threshold_down: Optional[float] = None

    def apply_tick(self, side: str, best_ask: float, now_ms: int) -> None:
        if side == "up":
            attr = "temp_price_up"
            ts_attr = "last_update_up_ms"
        elif side == "down":
            attr = "temp_price_down"
            ts_attr = "last_update_down_ms"
        else:
            raise ValueError(f"side must be 'up' or 'down', got {side}")

        if best_ask > self.cfg.threshold:
            setattr(self, attr, None)
            return
        cur = getattr(self, attr)
        if cur is None or best_ask < cur:
            setattr(self, attr, best_ask)
        setattr(self, ts_attr, now_ms)

    def evaluate_entry(
        self, side: str, best_ask: float, now_ms: int
    ) -> Optional[dict]:
        """Return {'reason': str, 'price': float} or None.

        Call AFTER apply_tick for the same (side, best_ask, now_ms).
        """
        if best_ask > self.cfg.threshold:
            return None
        if side not in ("up", "down"):
            raise ValueError(f"side must be 'up' or 'down', got {side}")
        temp_attr = "temp_price_up" if side == "up" else "temp_price_down"
        temp = getattr(self, temp_attr)
        if temp is None:
            return None
        if best_ask <= temp * (1.0 - self.cfg.depth_buy_discount):
            return {"reason": "depth", "price": best_ask}
        if best_ask >= temp + self.cfg.reversal_delta:
            return {"reason": "reversal", "price": best_ask}
        return None

    def check_profit_gate(
        self, side: str, price: float, qty_other: float, cost_other: float
    ) -> bool:
        """Return True if existing_avg_other + price <= max_sum_avg."""
        if qty_other <= 0:
            other_avg = 0.0
        else:
            other_avg = cost_other / qty_other
        return (other_avg + price) <= self.cfg.max_sum_avg

    def evaluate_second_leg(
        self, side: str, best_ask: float, now_ms: int
    ) -> Optional[dict]:
        """Return second-leg fire signal or None.

        Caller must have set dynamic_threshold_<side> after leg 1 fill.
        """
        if side == "up":
            dyn = self.dynamic_threshold_up
            timer_attr = "below_dyn_since_up_ms"
        elif side == "down":
            dyn = self.dynamic_threshold_down
            timer_attr = "below_dyn_since_down_ms"
        else:
            raise ValueError(f"side must be 'up' or 'down', got {side}")

        if dyn is None:
            return None
        if best_ask > dyn:
            setattr(self, timer_attr, None)
            return None
        # ask <= dyn from here on
        if best_ask <= dyn - self.cfg.second_side_buffer:
            return {"reason": "dyn_threshold_immediate", "price": best_ask}
        cur = getattr(self, timer_attr)
        if cur is None:
            setattr(self, timer_attr, now_ms)
            return None
        if (now_ms - cur) >= self.cfg.second_side_time_ms:
            return {"reason": "dyn_threshold_continuous", "price": best_ask}
        return None
