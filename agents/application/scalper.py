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
    exit_take_profit_pct: float = 0.05
    exit_trailing_stop_pct: float = 0.02
    exit_stop_loss_pct: float = 0.07
    exit_min_seconds_to_expiry: int = 45

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
            exit_take_profit_pct=_env_float("SCALP_EXIT_TAKE_PROFIT_PCT", 0.05),
            exit_trailing_stop_pct=_env_float("SCALP_EXIT_TRAILING_STOP_PCT", 0.02),
            exit_stop_loss_pct=_env_float("SCALP_EXIT_STOP_LOSS_PCT", 0.07),
            exit_min_seconds_to_expiry=_env_int("SCALP_EXIT_MIN_SECONDS_TO_EXPIRY", 45),
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

from agents.application.exit_executor import ExitExecutor  # noqa: E402
from agents.application.execution_safety import exitable_size_check  # noqa: E402
from agents.application.trade_log import (  # noqa: E402
    SCALPER_EXIT,
    SCALPER_LEG,
    SKIPPED_GATE,
    TradeLog,
)
from agents.application.scalper_pairs import ScalperPairsDAO, ScalperState  # noqa: E402
from agents.application.market_brain import CryptoSignalFeed, ExitPosition, MarketBrain  # noqa: E402
from agents.utils.objects import TradeRecommendation  # noqa: E402

try:
    from py_clob_client_v2.clob_types import OrderType as _ClobOrderType
    _FAK_TYPE = _ClobOrderType.FAK
    _V2_INSTALLED = True
except ImportError:
    _FAK_TYPE = "FAK"
    _V2_INSTALLED = False

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
        brain: MarketBrain = None,
        exit_executor: ExitExecutor = None,
        execute: bool = False,
        max_legs_per_hour: int = None,
    ):
        self.client = client
        self.log = log
        self.dao = dao
        self.cfg = cfg
        self.gamma = gamma
        self.brain = brain
        self.exit_executor = exit_executor or ExitExecutor(client)
        self.execute = execute
        if self.execute and not _V2_INSTALLED and _FAK_TYPE == "FAK":
            raise RuntimeError(
                "py_clob_client_v2 not installed; cannot run with execute=True"
            )
        self.max_legs_per_hour = (
            max_legs_per_hour if max_legs_per_hour is not None
            else _env_int("MAX_SCALP_TRADES_PER_HOUR", 60)
        )
        self.pairs: dict = {}
        self._exit_peak_by_slug_side: dict[tuple[str, str], float] = {}

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
        safety = exitable_size_check(amount_usdc=usdc, entry_price=intended_price)
        if not safety.ok:
            self.log.insert_terminal(
                cycle_id=f"scalp_gate:{slug}:{side}",
                market_id=slug,
                status=SKIPPED_GATE,
                token_id=token,
                side="BUY",
                price=intended_price,
                size_usdc=usdc,
                error=f"scalper_{safety.reason}",
            )
            return {"filled": False, "error": safety.reason}
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
        # limit=200 is needed because Polymarket sorts events by endDate
        # ascending; default limit=50 returns only stale 5m markets and
        # cuts off before the live 15m markets. Verified empirically
        # 2026-05-06: limit=50 → 0 of 21 active 15m markets seen.
        events = self.gamma.get_events_by_tag(tag_id=21, limit=200)
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

    def reap_expired(self, now_ts: int) -> int:
        n = 0
        for row in self.dao.list_open():
            if not row["period_ts"] or row["period_ts"] >= now_ts:
                continue
            slug = row["slug"]
            state = row["state"]
            if state == ScalperState.RECONCILE_NEEDED:
                # Operator must clear this manually — do not overwrite
                continue
            if state == ScalperState.LEG1_FILLED:
                # On-chain position exists; flag for operator review
                self.dao.set_state(slug, ScalperState.RECONCILE_NEEDED,
                                   error="period_expired_while_leg1_filled")
                logger.warning(
                    "scalper: %s expired with LEG1_FILLED — moved to RECONCILE_NEEDED",
                    slug,
                )
            else:
                # TRACKING, BOTH_FILLED: safe to expire
                self.dao.set_state(slug, ScalperState.EXPIRED)
            self.pairs.pop(slug, None)
            n += 1
        return n

    def reconcile_at_startup(self) -> int:
        """Find any LEG1_FILLED rows from prior process, mark RECONCILE_NEEDED.
        Operator must verify on-chain before clearing."""
        flipped = 0
        for row in self.dao.list_open():
            if row["state"] == ScalperState.LEG1_FILLED:
                self.dao.set_state(row["slug"],
                                     ScalperState.RECONCILE_NEEDED,
                                     error="restart_found_leg1_filled")
                flipped += 1
        if flipped:
            logger.warning(
                "scalper: %d pair(s) flipped to RECONCILE_NEEDED — "
                "verify on-chain positions before resuming", flipped
            )
        return flipped

    def tick(
        self,
        slug: str,
        up_ask: float,
        down_ask: float,
        now_ms: int,
        up_bid: float = None,
        down_bid: float = None,
    ) -> None:
        """One scheduling tick per slug. Apply ticks → maybe fire legs."""
        pair = self.pairs.get(slug)
        if pair is None:
            return
        row = self.dao.get_by_slug(slug)
        if row is None:
            return
        if row["state"] in (ScalperState.BOTH_FILLED, ScalperState.EXPIRED,
                              ScalperState.REDEEMED, ScalperState.SHADOW,
                              ScalperState.EXITED, ScalperState.RECONCILE_NEEDED):
            return

        if row["state"] == ScalperState.LEG1_FILLED and self._maybe_exit_leg1(
            slug=slug,
            row=row,
            pair=pair,
            up_bid=up_bid,
            down_bid=down_bid,
            now_ms=now_ms,
        ):
            return

        if not self._has_rate_capacity():
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
                if not self._brain_allows_entry(
                    slug=slug,
                    side=side,
                    up_ask=up_ask,
                    down_ask=down_ask,
                    signal=sig,
                    now_ms=now_ms,
                    period_ts=pair.period_ts,
                ):
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
            if not self._brain_allows_entry(
                slug=slug,
                side=second,
                up_ask=up_ask,
                down_ask=down_ask,
                signal=sig,
                now_ms=now_ms,
                period_ts=pair.period_ts,
            ):
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

    def _brain_allows_entry(
        self,
        *,
        slug: str,
        side: str,
        up_ask: float,
        down_ask: float,
        signal: dict,
        now_ms: int,
        period_ts: int,
    ) -> bool:
        if self.brain is None:
            return True
        decision = self.brain.evaluate_scalper_entry(
            slug=slug,
            side=side,
            up_ask=up_ask,
            down_ask=down_ask,
            candidate_price=float(signal["price"]),
            signal_reason=str(signal.get("reason", "")),
            now_ms=now_ms,
            period_ts=period_ts,
        )
        self._record_brain_decision(slug, side, signal, decision)
        if not decision.approved:
            logger.info(
                "scalper brain veto slug=%s side=%s reason=%s score=%.3f features=%s",
                slug,
                side,
                decision.reason,
                decision.score,
                decision.features,
            )
            return False
        logger.debug(
            "scalper brain approved slug=%s side=%s score=%.3f reason=%s",
            slug,
            side,
            decision.score,
            decision.reason,
        )
        return True

    def _record_brain_decision(self, slug: str, side: str, signal: dict, decision) -> None:
        try:
            token = None
            pair = self.pairs.get(slug)
            if pair is not None:
                token = pair.up_token if side == "up" else pair.down_token
            self.log.insert_brain_decision(
                agent="scalper",
                strategy=decision.profile.market_type or "unknown",
                decision_type="entry",
                market_id=slug,
                token_id=token,
                approved=decision.approved,
                reason=decision.reason,
                score=decision.score,
                market_type=decision.profile.market_type,
                asset=decision.profile.asset,
                features=decision.features,
                action=f"BUY_{side.upper()}:{signal.get('reason', '')}",
            )
        except Exception:
            logger.exception("scalper brain decision journal write failed")

    def _maybe_exit_leg1(
        self,
        *,
        slug: str,
        row: dict,
        pair: ScalpPair,
        up_bid: float,
        down_bid: float,
        now_ms: int,
    ) -> bool:
        held_side = None
        if float(row.get("qty_up") or 0) > 0 and float(row.get("qty_down") or 0) <= 0:
            held_side = "up"
        elif float(row.get("qty_down") or 0) > 0 and float(row.get("qty_up") or 0) <= 0:
            held_side = "down"
        if held_side is None:
            return False

        qty = float(row[f"qty_{held_side}"] or 0)
        cost = float(row[f"cost_{held_side}"] or 0)
        if qty <= 0 or cost <= 0:
            return False

        bid = up_bid if held_side == "up" else down_bid
        if bid is None or bid <= 0:
            return False

        entry = cost / qty
        key = (slug, held_side)
        peak = max(self._exit_peak_by_slug_side.get(key, entry), bid)
        self._exit_peak_by_slug_side[key] = peak
        pnl_pct = (bid - entry) / entry if entry > 0 else 0.0
        drawdown_from_peak = (peak - bid) / peak if peak > 0 else 0.0
        seconds_to_expiry = pair.period_ts - int(now_ms / 1000) if pair.period_ts else None

        token = pair.up_token if held_side == "up" else pair.down_token
        reason = None
        if seconds_to_expiry is not None and seconds_to_expiry <= self.cfg.exit_min_seconds_to_expiry:
            reason = "expiry_exit"
        elif self.brain is not None:
            opened_ts = float(row.get("opened_ts") or int(now_ms / 1000))
            decision = self.brain.evaluate_exit(
                ExitPosition(
                    market_id=slug,
                    token_id=token,
                    side=held_side,
                    entry_price=entry,
                    current_price=bid,
                    opened_ts_ms=int(opened_ts * 1000),
                    max_price_seen=peak,
                    shares=qty,
                ),
                now_ms=now_ms,
            )
            self._record_brain_exit_decision(
                slug=slug,
                token=token,
                held_side=held_side,
                decision=decision,
            )
            if not decision.approved:
                if decision.reason == "hold_profit_with_momentum":
                    logger.info(
                        "scalper smart exit hold slug=%s side=%s pnl=%.4f reason=%s features=%s",
                        slug,
                        held_side,
                        pnl_pct,
                        decision.reason,
                        decision.features,
                    )
                return False
            reason = {
                "trailing_stop_after_profit": "trailing_stop",
                "timeout": "timeout",
            }.get(decision.reason, decision.reason)
        else:
            if pnl_pct >= self.cfg.exit_take_profit_pct:
                reason = "take_profit"
            elif (
                peak >= entry * (1.0 + self.cfg.exit_take_profit_pct)
                and drawdown_from_peak >= self.cfg.exit_trailing_stop_pct
            ):
                reason = "trailing_stop"
            elif pnl_pct <= -self.cfg.exit_stop_loss_pct:
                reason = "stop_loss"
        if reason is None:
            return False

        cycle_id = f"scalp_exit:{slug}:{held_side}:{str(uuid.uuid4())[:8]}"

        if not self.execute:
            self.log.insert_terminal(
                cycle_id=cycle_id,
                market_id=slug,
                status=SCALPER_EXIT,
                token_id=token,
                side="SELL",
                price=bid,
                size_usdc=qty * bid,
                response={
                    "shadow": True,
                    "reason": reason,
                    "entry": entry,
                    "bid": bid,
                    "qty": qty,
                    "pnl_pct": pnl_pct,
                },
                error=f"SHADOW scalper exit {reason}",
            )
            return True

        result = self.exit_executor.sell_fak(token_id=token, shares=qty, mid=bid)
        limit_price = self.exit_executor.limit_price_from_mid(bid)
        if result.closed:
            self.log.insert_terminal(
                cycle_id=cycle_id,
                market_id=slug,
                status=SCALPER_EXIT,
                token_id=token,
                side="SELL",
                price=limit_price,
                size_usdc=qty * limit_price,
                response={
                    "reason": reason,
                    "entry": entry,
                    "bid": bid,
                    "qty": qty,
                    "pnl_pct": pnl_pct,
                    "raw": result.response,
                },
            )
            self.dao.set_state(slug, ScalperState.EXITED)
            return True

        self.log.insert_terminal(
            cycle_id=cycle_id,
            market_id=slug,
            status="scalper_exit_failed",
            token_id=token,
            side="SELL",
            price=limit_price,
            size_usdc=0,
            response=result.response,
            error=result.error or f"sell not matched: {result.status}",
        )
        return False

    def _record_brain_exit_decision(
        self,
        *,
        slug: str,
        token: str,
        held_side: str,
        decision,
    ) -> None:
        try:
            self.log.insert_brain_decision(
                agent="scalper",
                strategy=decision.profile.market_type or "unknown",
                decision_type="exit",
                market_id=slug,
                token_id=token,
                approved=decision.approved,
                reason=decision.reason,
                score=decision.score,
                market_type=decision.profile.market_type,
                asset=decision.profile.asset,
                features=decision.features,
                action="SELL" if decision.approved else f"HOLD_{held_side.upper()}",
            )
        except Exception:
            logger.exception("scalper brain exit decision journal write failed")


# ---------------------------------------------------------------------------
# Daemon — long-running loop.  SIGTERM-aware.  One process per replica.
# ---------------------------------------------------------------------------
import signal  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

try:
    from agents.polymarket.polymarket import Polymarket as _Polymarket
    Polymarket = _Polymarket
except ImportError:
    Polymarket = None  # type: ignore

try:
    from agents.polymarket.gamma import GammaMarketClient as _GammaMarketClient
    GammaMarketClient = _GammaMarketClient
except ImportError:
    GammaMarketClient = None  # type: ignore


class ScalperDaemon:
    """Long-running loop. SIGTERM-aware. One process per replica.

    Cadence:
      - poll_ms          -> re-fetch order books for tracked tokens, run tick()
      - discover_every_sec -> re-scan gamma for new -updown-15m- markets
    """

    def __init__(
        self,
        heartbeat_path: str = None,
        db_path: str = None,
        poll_ms: int = None,
        discover_every_sec: int = None,
        execute: bool = None,
    ):
        self.heartbeat = Path(heartbeat_path or
                              os.getenv("SCALPER_HEARTBEAT_PATH",
                                        "/app/data/scalper_heartbeat"))
        self.cfg = ScalperConfig.from_env()
        if poll_ms is not None:
            self.cfg.poll_ms = poll_ms
        if discover_every_sec is not None:
            self.cfg.discover_every_sec = discover_every_sec
        self.execute = (
            execute if execute is not None
            else os.getenv("EXECUTE_SCALPER", "false").lower() == "true"
        )
        self.tl = TradeLog(db_path=db_path)
        self.dao = ScalperPairsDAO(self.tl)
        self.client = Polymarket(live=self.execute)
        self.gamma = GammaMarketClient()
        self.engine = ScalperEngine(
            client=self.client, log=self.tl, dao=self.dao,
            cfg=self.cfg, gamma=self.gamma,
            brain=MarketBrain(crypto_feed=CryptoSignalFeed()),
            execute=self.execute,
        )
        # Public client for order book reads — no credentials required.
        # ClobClient with only host+chain_id works for GET endpoints.
        # Recreated on HTTP/2 connection errors (see `_make_book_client`
        # and the tick-fail handler) — without recreation the SDK's
        # underlying httpx connection hits the server's max-streams cap
        # (~20k requests) and ConnectionTerminated errors keep firing
        # until the container restarts. Discovered 2026-05-08 when the
        # Brain approved 16 entries and 0 became fills.
        self._book_client = self._make_book_client()
        self._book_client_request_count = 0
        # Cap on requests before a proactive client refresh — well under
        # the typical HTTP/2 server-side max_streams (10k-20k).
        self._book_client_max_requests = int(
            os.getenv("SCALP_BOOK_CLIENT_MAX_REQUESTS", "5000")
        )
        self._stop = threading.Event()

    @staticmethod
    def _make_book_client():
        """Return a fresh ClobClient for public book reads, or None if the
        SDK isn't importable."""
        try:
            from py_clob_client_v2.client import ClobClient as _ClobClient
            return _ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
            )
        except (ImportError, Exception) as e:
            logger.warning("ScalperDaemon: ClobClient unavailable: %s", e)
            return None

    def _refresh_book_client_if_needed(self) -> None:
        """Periodic refresh — replaces the client before HTTP/2
        max_streams kicks in. Called every tick."""
        self._book_client_request_count += 1
        if self._book_client_request_count >= self._book_client_max_requests:
            logger.info(
                "ScalperDaemon: rotating _book_client after %d requests",
                self._book_client_request_count,
            )
            self._book_client = self._make_book_client()
            self._book_client_request_count = 0

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            signal.signal(signal.SIGTERM, lambda *_: self.stop())
            signal.signal(signal.SIGINT, lambda *_: self.stop())
        except (ValueError, OSError):
            pass  # not the main thread — SIGTERM won't reach us anyway
        logger.info("ScalperDaemon: starting (execute=%s)", self.execute)
        self.engine.reconcile_at_startup()
        last_discover = 0.0
        try:
            while not self._stop.is_set():
                now = time.time()
                if now - last_discover >= self.cfg.discover_every_sec:
                    try:
                        self.engine.discover_markets()
                    except Exception:
                        logger.exception("discover_markets failed")
                    try:
                        self.engine.reap_expired(now_ts=int(now))
                    except Exception:
                        logger.exception("reap_expired failed")
                    last_discover = now
                for row in self.dao.list_open():
                    slug = row["slug"]
                    # Skip pairs the engine can't act on. Saves two
                    # CLOB book fetches per cycle per pair, which is
                    # the bulk of the 6250+ "404 No orderbook" errors
                    # we observed: RECONCILE_NEEDED pairs are kept in
                    # list_open() until an operator clears them, but
                    # their markets have already resolved so the
                    # /book endpoint returns 404. tick() already
                    # short-circuits on these states; doing the same
                    # check here avoids the wasted fetches.
                    if row["state"] in (
                        ScalperState.BOTH_FILLED,
                        ScalperState.EXPIRED,
                        ScalperState.REDEEMED,
                        ScalperState.EXITED,
                        ScalperState.SHADOW,
                        ScalperState.RECONCILE_NEEDED,
                    ):
                        continue
                    if slug not in self.engine.pairs:
                        self.engine.add_pair(ScalpPair(
                            slug=slug, period_ts=row["period_ts"],
                            up_token=row["up_token"], down_token=row["down_token"],
                            cfg=self.cfg,
                        ))
                    try:
                        if self._book_client is None:
                            self._book_client = self._make_book_client()
                            if self._book_client is None:
                                continue
                        self._refresh_book_client_if_needed()
                        book_up = self._book_client.get_order_book(row["up_token"])
                        book_dn = self._book_client.get_order_book(row["down_token"])
                        ask_up = self._best_ask(book_up)
                        ask_dn = self._best_ask(book_dn)
                        bid_up = self._best_bid(book_up)
                        bid_dn = self._best_bid(book_dn)
                        if ask_up and ask_dn:
                            self.engine.tick(slug, up_ask=ask_up, down_ask=ask_dn,
                                             now_ms=int(now * 1000),
                                             up_bid=bid_up, down_bid=bid_dn)
                    except Exception as exc:
                        # HTTP/2 ConnectionTerminated / RemoteProtocolError /
                        # PolyApiException all indicate a dead client. Drop
                        # it; the next tick rebuilds. Cheap, idempotent.
                        msg = str(exc).lower()
                        if any(s in msg for s in (
                            "connectionterminated", "remoteprotocol",
                            "request exception", "max_streams",
                        )):
                            logger.warning(
                                "ScalperDaemon: connection error on %s — "
                                "dropping book client (will rebuild next tick): %s",
                                slug, type(exc).__name__,
                            )
                            self._book_client = None
                            self._book_client_request_count = 0
                        else:
                            logger.exception("tick failed for %s", slug)
                try:
                    self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
                    self.heartbeat.touch()
                except Exception:
                    logger.warning("ScalperDaemon: heartbeat touch failed")
                self._stop.wait(self.cfg.poll_ms / 1000.0)
        finally:
            logger.info("ScalperDaemon: exited")

    @staticmethod
    def _best_ask(book) -> Optional[float]:
        asks = (getattr(book, "asks", None) if not isinstance(book, dict)
                else book.get("asks", []))
        if not asks:
            return None
        prices = []
        for a in asks:
            if hasattr(a, "price"):
                prices.append(float(a.price))
            else:
                prices.append(float(a["price"]))
        return min(prices) if prices else None

    @staticmethod
    def _best_bid(book) -> Optional[float]:
        bids = (getattr(book, "bids", None) if not isinstance(book, dict)
                else book.get("bids", []))
        if not bids:
            return None
        prices = []
        for b in bids:
            if hasattr(b, "price"):
                prices.append(float(b.price))
            else:
                prices.append(float(b["price"]))
        return max(prices) if prices else None


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)
    ScalperDaemon().run()
