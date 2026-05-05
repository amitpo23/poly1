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


# ---------------------------------------------------------------------------
# Optional heavy deps — fall back to lightweight sentinels for stdlib test runs.
# ---------------------------------------------------------------------------
import uuid  # noqa: E402

from agents.application.trade_log import SCALPER_LEG, TradeLog  # noqa: E402
from agents.application.scalper_pairs import ScalperPairsDAO, ScalperState  # noqa: E402
from agents.utils.objects import TradeRecommendation  # noqa: E402

try:
    from py_clob_client_v2.clob_types import OrderType as _ClobOrderType
    _FAK_TYPE = _ClobOrderType.FAK
except ImportError:
    _FAK_TYPE = "FAK"

try:
    from langchain_core.documents import Document as _Document
except ImportError:
    from collections import namedtuple as _nt
    _Document = _nt("_FakeDoc", ["page_content", "metadata"])


class ScalperEngine:
    """Top-level scalper engine. Owns the running pairs and the I/O loop."""

    SLUG_FILTER = "-updown-15m-"

    def __init__(
        self,
        client,
        log: TradeLog,
        dao: ScalperPairsDAO,
        cfg: ScalperConfig,
        gamma=None,
        execute: bool = False,
        max_legs_per_hour: int = None,
    ):
        self.client = client
        self.log = log
        self.dao = dao
        self.cfg = cfg
        self.gamma = gamma
        self.execute = execute
        if self.execute and _FAK_TYPE == "FAK":
            raise RuntimeError(
                "py_clob_client_v2 not installed; cannot run with execute=True"
            )
        self.max_legs_per_hour = (
            max_legs_per_hour if max_legs_per_hour is not None
            else _env_int("MAX_SCALP_TRADES_PER_HOUR", 60)
        )
        self.pairs: dict = {}

    def add_pair(self, pair: ScalpPair) -> None:
        self.pairs[pair.slug] = pair

    def place_leg(
        self,
        slug: str,
        side: str,
        token: str,
        usdc: float,
        intended_price: float,
    ) -> dict:
        """Attempt one FAK leg. Always increments attempts; only increments qty/cost on success."""
        if not self.execute:
            # Shadow mode: log hypothetical leg, don't call CLOB
            cycle_id = f"scalp:{slug}:{side}"
            self.log.insert_terminal(
                cycle_id=cycle_id, market_id=slug, status=SCALPER_LEG,
                token_id=token, side="BUY",
                price=intended_price, size_usdc=usdc,
                error=f"SHADOW: would have fired at {intended_price:.4f}",
            )
            self.dao.record_fill(slug, side, qty=0.0, cost_usdc=0.0,
                                 fill_price=intended_price)
            return {"filled": False, "shadow": True}

        cycle_id = f"scalp:{slug}:{side}:{str(uuid.uuid4())[:8]}"
        rec = TradeRecommendation(
            price=intended_price if side == "up" else (1.0 - intended_price),
            size_fraction=0.0,
            side="BUY" if side == "up" else "SELL",
            confidence=None,
            amount_usdc=usdc,
        )
        # Read canonical tokens from DAO for the metadata
        pair_row = self.dao.get_by_slug(slug)
        up_tok = pair_row["up_token"] if pair_row else token
        dn_tok = pair_row["down_token"] if pair_row else token
        market = (_Document(page_content="", metadata={
            "outcomes": "['Up', 'Down']",
            "clob_token_ids": f"['{up_tok}', '{dn_tok}']",
            "outcome_prices": f"['{intended_price}', '{1.0 - intended_price}']",
            "id": slug,
        }), 0.0)

        order_exc = None
        response = None
        try:
            response = self.client.execute_market_order(
                market, rec, order_type=_FAK_TYPE,
            )
        except Exception as e:
            order_exc = e

        filled_usdc = float(response.get("amount_usdc", 0.0)) if response else 0.0
        avg_price = float(response.get("order_avg_price_estimate", intended_price)) if response else intended_price
        qty = filled_usdc / avg_price if avg_price > 0 else 0.0
        self.dao.record_fill(slug, side, qty=qty, cost_usdc=filled_usdc,
                             fill_price=avg_price if qty > 0 else None)
        self.log.insert_terminal(
            cycle_id=cycle_id, market_id=slug, status=SCALPER_LEG,
            token_id=token, side=rec.side if not order_exc else ("BUY" if side == "up" else "SELL"),
            price=avg_price, size_usdc=filled_usdc,
            response=response if not order_exc else None,
            error=str(order_exc) if order_exc else None,
        )
        if order_exc:
            return {"filled": False, "error": str(order_exc)}
        return {"filled": qty > 0, "qty": qty, "avg_price": avg_price}

    def discover_markets(self) -> list:
        """Scan gamma for *-updown-15m-* events. Create scalper_pairs rows for new ones."""
        import ast
        if self.gamma is None:
            raise RuntimeError("gamma client not provided")
        events = self.gamma.get_events_by_tag(tag_id=21)
        out = []
        for ev in events:
            for m in ev.get("markets", []):
                slug = m.get("slug", "")
                if self.SLUG_FILTER not in slug:
                    continue
                if not m.get("acceptingOrders", False):
                    continue
                try:
                    tokens = ast.literal_eval(m["clobTokenIds"])
                    outcomes = ast.literal_eval(m["outcomes"])
                except Exception as e:
                    logger.warning("scalper: bad market metadata %s: %s", slug, e)
                    continue
                if len(tokens) != 2 or len(outcomes) != 2:
                    continue
                period_ts = self._parse_period_ts(slug, m.get("endDate"))
                self.dao.create(slug=slug, period_ts=period_ts,
                                up_token=tokens[0], down_token=tokens[1])
                out.append({
                    "slug": slug,
                    "up_token": tokens[0],
                    "down_token": tokens[1],
                    "period_ts": period_ts,
                })
        return out

    @staticmethod
    def _parse_period_ts(slug: str, end_date) -> int:
        suffix = slug.rsplit("-", 1)[-1]
        if suffix.isdigit():
            return int(suffix)
        if end_date:
            from datetime import datetime
            try:
                return int(datetime.fromisoformat(
                    end_date.replace("Z", "+00:00")).timestamp())
            except Exception:
                pass
        return 0

    def _has_rate_capacity(self) -> bool:
        recent = self.log.count_recent(SCALPER_LEG, hours=1)
        return recent < self.max_legs_per_hour

    def tick(self, slug: str, up_ask: float, down_ask: float, now_ms: int) -> None:
        """One scheduling tick per slug. Apply ticks → maybe fire legs."""
        if not self._has_rate_capacity():
            return
        pair = self.pairs.get(slug)
        if pair is None:
            return
        row = self.dao.get_by_slug(slug)
        if row is None:
            return
        if row["state"] in (ScalperState.BOTH_FILLED, ScalperState.EXPIRED,
                              ScalperState.REDEEMED, ScalperState.SHADOW,
                              ScalperState.RECONCILE_NEEDED):
            return

        pair.apply_tick("up", up_ask, now_ms)
        pair.apply_tick("down", down_ask, now_ms)

        if row["state"] == ScalperState.TRACKING:
            for side, ask in (("up", up_ask), ("down", down_ask)):
                row = self.dao.get_by_slug(slug)  # re-read after potential write
                if row is None:
                    return
                attempts = row[f"attempts_{side}"]
                if attempts >= self.cfg.max_buys_per_side:
                    continue
                sig = pair.evaluate_entry(side, ask, now_ms)
                if sig is None:
                    continue
                if not pair.check_profit_gate(side, sig["price"],
                                                qty_other=0, cost_other=0):
                    continue
                token = pair.up_token if side == "up" else pair.down_token
                result = self.place_leg(slug=slug, side=side, token=token,
                                          usdc=self.cfg.leg_usdc_cap,
                                          intended_price=sig["price"])
                if result.get("filled"):
                    self.dao.set_state(slug, ScalperState.LEG1_FILLED)
                    other = "down" if side == "up" else "up"
                    setattr(pair, f"dynamic_threshold_{other}",
                            1.0 - result["avg_price"] + self.cfg.dynamic_threshold_boost)
                    return

        elif row["state"] == ScalperState.LEG1_FILLED:
            second = "up" if row["qty_up"] == 0 else "down"
            ask = up_ask if second == "up" else down_ask
            row = self.dao.get_by_slug(slug)
            if row is None:
                return
            attempts = row[f"attempts_{second}"]
            if attempts >= self.cfg.max_buys_per_side:
                return
            sig = pair.evaluate_second_leg(second, ask, now_ms)
            if sig is None:
                return
            qty_other = row["qty_up" if second == "down" else "qty_down"]
            cost_other = row["cost_up" if second == "down" else "cost_down"]
            if not pair.check_profit_gate(second, sig["price"],
                                             qty_other=qty_other,
                                             cost_other=cost_other):
                return
            token = pair.up_token if second == "up" else pair.down_token
            result = self.place_leg(slug=slug, side=second, token=token,
                                      usdc=self.cfg.leg_usdc_cap,
                                      intended_price=sig["price"])
            if result.get("filled"):
                self.dao.set_state(slug, ScalperState.BOTH_FILLED)
