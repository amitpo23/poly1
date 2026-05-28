"""Amit v3 — fade-the-spike strategy.

Operator-requested 2026-05-27. Continuous spike detection, bet opposite:

  From t=1 to t=260 of each 5-min period, monitor mid price.
  Whenever the price moves >= spike_threshold_pct (default 5%) over the
  last spike_window_sec (default 20s), enter on the OPPOSITE side:
    - Price spiked UP   → SELL YES (= BUY DOWN)
    - Price spiked DOWN → BUY YES (= BUY UP)
  Hold each position until t=270 (= 30s before resolution) or until
  TP/SL hits — same dynamic max_hold + Layer 1+2 as v2.

  Up to MAX_ENTRIES_PER_CYCLE per period (default 3), each entry must
  be in the OPPOSITE direction from the previous (no doubling up on
  the same fade).

Analyzer (7d data) — chosen 5%/20s gives:
  - 26% of cycles have >=1 spike
  - avg 1.82 spikes per cycle when triggered
  - up/down distribution 48%/52% (balanced)

Defaults:
  - EXECUTE_BTC5MIN_TIMED_V3=false (must be explicitly enabled)
  - BTC5MIN_TIMED_V3_POSITION_USDC=0.50
  - BTC5MIN_TIMED_V3_SPIKE_THRESHOLD_PCT=0.05
  - BTC5MIN_TIMED_V3_SPIKE_WINDOW_SEC=20
  - BTC5MIN_TIMED_V3_MAX_ENTRIES_PER_CYCLE=3
  - TP=7%, SL=10% (same as current v2)

Annotated with btc5min_timed_v3_open status for ledger isolation.
Heartbeat: data/btc5min_timed_v3_heartbeat
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agents.utils.ws_book_feed import WSBookFeed

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except (ValueError, TypeError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes")


@dataclass
class Btc5MinTimedV3Config:
    """Operator-tunable parameters."""
    execute: bool = False
    position_usdc: float = 0.20
    max_trades_per_day: int = 10
    halt_after_losses: int = 3
    # Phase 1: DOWN at start
    phase1_entry_offset_sec: float = 1.0   # t=0:01
    phase1_tp_pct: float = 0.05            # +5%
    phase1_sl_pct: float = 0.20            # -20%
    # Phase 2: UP at minute 3
    phase2_entry_offset_sec: float = 180.0 # t=3:00
    phase2_tp_pct: float = 0.05            # +5%
    phase2_sl_pct: float = 0.20            # -20%
    # Phase 2 momentum gate (2026-05-26)
    phase2_min_momentum_price: float = 0.55
    # v3 fade-the-spike (overrides phase1/phase2 entry logic)
    spike_threshold_pct: float = 0.05   # 5% move triggers entry
    spike_window_sec: float = 20.0      # window over which we measure change
    max_entries_per_cycle: int = 3      # allow up to 3 fades per 5min market
    entry_window_start_sec: float = 1.0   # earliest entry
    entry_window_end_sec: float = 260.0   # latest entry (= 40s before resolution)
    spike_cooldown_sec: float = 30.0      # min seconds between fires
    # Hours-of-day (UTC) — 0..23 = disabled, direct gates handle liquidity
    trade_hour_start_utc: int = 0
    trade_hour_end_utc: int = 23
    # Spread cap (fraction) — analyzer showed >2% spread removes net losers.
    max_spread_pct: float = 0.02
    # Legacy phase1_disabled flag (v3 doesn't use phases; kept for API compat)
    phase1_disabled: bool = True
    # Common
    no_entry_after_sec: int = 270          # don't enter after t=4:30
    poll_sec: float = 2.0
    fast_preload_enabled: bool = False
    fast_preload_seconds: float = 25.0
    fast_poll_sec: float = 0.05
    fast_boundary_window_sec: float = 2.0
    max_entry_attempts_per_phase: int = 1
    force_phase1_down_first_second: bool = False
    phase1_entry_window_sec: float = 1.0
    asset: str = "btc"                     # market asset (BTC/ETH/SOL)
    heartbeat_path: str = "/app/data/btc5min_timed_v3_heartbeat"

    @classmethod
    def from_env(cls) -> "Btc5MinTimedV3Config":
        return cls(
            execute=_env_bool("EXECUTE_BTC5MIN_TIMED_V3", False),
            position_usdc=_env_float("BTC5MIN_TIMED_V3_POSITION_USDC", 0.20),
            max_trades_per_day=_env_int("BTC5MIN_TIMED_V3_MAX_TRADES_PER_DAY", 10),
            halt_after_losses=_env_int("BTC5MIN_TIMED_V3_HALT_AFTER_LOSSES", 3),
            phase1_entry_offset_sec=_env_float("BTC5MIN_TIMED_V3_PHASE1_OFFSET_SEC", 1.0),
            phase1_tp_pct=_env_float("BTC5MIN_TIMED_V3_PHASE1_TP_PCT", 0.05),
            phase1_sl_pct=_env_float("BTC5MIN_TIMED_V3_PHASE1_SL_PCT", 0.20),
            phase2_entry_offset_sec=_env_float("BTC5MIN_TIMED_V3_PHASE2_OFFSET_SEC", 180.0),
            phase2_tp_pct=_env_float("BTC5MIN_TIMED_V3_PHASE2_TP_PCT", 0.05),
            phase2_sl_pct=_env_float("BTC5MIN_TIMED_V3_PHASE2_SL_PCT", 0.20),
            phase2_min_momentum_price=_env_float(
                "BTC5MIN_TIMED_V3_PHASE2_MIN_MOMENTUM_PRICE", 0.55
            ),
            phase1_disabled=_env_bool("BTC5MIN_TIMED_V3_PHASE1_DISABLED", True),
            spike_threshold_pct=_env_float("BTC5MIN_TIMED_V3_SPIKE_THRESHOLD_PCT", 0.05),
            spike_window_sec=_env_float("BTC5MIN_TIMED_V3_SPIKE_WINDOW_SEC", 20.0),
            max_entries_per_cycle=_env_int("BTC5MIN_TIMED_V3_MAX_ENTRIES_PER_CYCLE", 3),
            entry_window_start_sec=_env_float("BTC5MIN_TIMED_V3_ENTRY_START_SEC", 1.0),
            entry_window_end_sec=_env_float("BTC5MIN_TIMED_V3_ENTRY_END_SEC", 260.0),
            spike_cooldown_sec=_env_float("BTC5MIN_TIMED_V3_SPIKE_COOLDOWN_SEC", 30.0),
            trade_hour_start_utc=_env_int("BTC5MIN_TIMED_V3_HOUR_START_UTC", 0),
            trade_hour_end_utc=_env_int("BTC5MIN_TIMED_V3_HOUR_END_UTC", 23),
            max_spread_pct=_env_float("BTC5MIN_TIMED_V3_MAX_SPREAD_PCT", 0.02),
            no_entry_after_sec=_env_int("BTC5MIN_TIMED_V3_NO_ENTRY_AFTER_SEC", 270),
            poll_sec=_env_float("BTC5MIN_TIMED_V3_POLL_SEC", 2.0),
            fast_preload_enabled=_env_bool("BTC5MIN_TIMED_V3_FAST_PRELOAD_ENABLED", False),
            fast_preload_seconds=_env_float("BTC5MIN_TIMED_V3_FAST_PRELOAD_SECONDS", 25.0),
            fast_poll_sec=_env_float("BTC5MIN_TIMED_V3_FAST_POLL_SEC", 0.05),
            fast_boundary_window_sec=_env_float("BTC5MIN_TIMED_V3_FAST_BOUNDARY_WINDOW_SEC", 2.0),
            max_entry_attempts_per_phase=_env_int("BTC5MIN_TIMED_V3_MAX_ENTRY_ATTEMPTS_PER_PHASE", 1),
            force_phase1_down_first_second=_env_bool(
                "BTC5MIN_TIMED_V3_FORCE_PHASE1_DOWN_FIRST_SECOND", False
            ),
            phase1_entry_window_sec=_env_float("BTC5MIN_TIMED_V3_PHASE1_ENTRY_WINDOW_SEC", 1.0),
            asset=os.getenv("BTC5MIN_TIMED_V3_ASSET", "btc").lower(),
            heartbeat_path=os.getenv("BTC5MIN_TIMED_V3_HEARTBEAT_PATH", "/app/data/btc5min_timed_v3_heartbeat"),
        )


def _current_period_ts() -> int:
    """Current 5-min period boundary (epoch seconds)."""
    return int(time.time() // 300) * 300


def _format_slug(period_ts: int, asset: str) -> str:
    """Market slug, matches the btc_5min slug convention."""
    return f"{asset.lower()}-updown-5m-{period_ts}"


@dataclass
class CycleState:
    """Per-period state — spike tracking for v3 fade strategy."""
    period_ts: int = 0
    # Compatibility shims (rest of v2 code references these):
    phase1_fired: bool = False
    phase2_fired: bool = False
    phase1_attempts: int = 0
    phase2_attempts: int = 0
    # v3 spike state
    yes_token_id: str = ""
    # Recent (offset_sec, mid_price) samples of the YES token
    price_history: list = field(default_factory=list)
    entry_count: int = 0
    last_spike_direction: str = ""   # "up" or "down" — must alternate
    last_fire_offset: float = -999.0


@dataclass
class DailyState:
    """Per-day risk state — cap exposure + auto-halt."""
    date_key: str = ""
    trades_today: int = 0
    consecutive_losses: int = 0
    auto_halted: bool = False


class Btc5MinTimedV3Engine:
    """Time-based DOWN/UP strategy engine. NOT driven by signals."""

    def __init__(
        self,
        polymarket,
        trade_log,
        risk_gate,
        cfg: Btc5MinTimedV3Config,
    ):
        self.polymarket = polymarket
        self.trade_log = trade_log
        self.risk_gate = risk_gate
        self.cfg = cfg
        self._cycle: CycleState = CycleState()
        self._daily: DailyState = DailyState(date_key=self._today_key())
        self._market_cache: dict[int, dict] = {}
        # WebSocket book feed — sub-100ms real-time mid price tracking.
        # Started lazily on first cycle that resolves a YES token.
        self._ws_feed: Optional[WSBookFeed] = None

    @staticmethod
    def _today_key() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _reset_daily_if_new_day(self) -> None:
        today = self._today_key()
        if today != self._daily.date_key:
            self._daily = DailyState(date_key=today)

    def _refresh_cycle(self, period_ts: int) -> None:
        if period_ts != self._cycle.period_ts:
            self._cycle = CycleState(period_ts=period_ts)

    def preload_next_market(self) -> None:
        """Resolve and warm the next 5-minute market before the boundary."""
        if not self.cfg.fast_preload_enabled:
            return
        now = time.time()
        current = _current_period_ts()
        next_period = current + 300
        if next_period in self._market_cache:
            return
        if next_period - now > self.cfg.fast_preload_seconds:
            return
        slug = _format_slug(next_period, self.cfg.asset)
        market_doc = self._resolve_market(next_period, slug)
        if market_doc is None:
            return
        self._market_cache[next_period] = market_doc
        token_ids = market_doc.get("token_ids") or []
        if len(token_ids) >= 2:
            try:
                self.polymarket._fillable_market_buy(str(token_ids[1]), self.cfg.position_usdc)
            except Exception as exc:
                logger.info("btc5min_timed_v3 fast preload book warmup skipped: %s", exc)
        for key in list(self._market_cache):
            if key < current - 300:
                self._market_cache.pop(key, None)

    def maybe_enter(self) -> Optional[str]:
        """v3 fade-the-spike entry detector.

        On every poll, fetch the YES token's mid price, append to the
        per-cycle history, then compute the price change over the last
        spike_window_sec. If |change| >= spike_threshold_pct AND we've
        not yet hit max_entries_per_cycle AND the direction is OPPOSITE
        to the previous fire AND we're past spike_cooldown_sec since
        last fire, return:
          "spike_down" → BUY YES (price fell, fade by betting UP)
          "spike_up"   → SELL YES (price rose, fade by betting DOWN)
        """
        self._reset_daily_if_new_day()
        if self._daily.auto_halted:
            return None
        if self._daily.trades_today >= self.cfg.max_trades_per_day:
            return None

        now = time.time()
        period_ts = _current_period_ts()
        self._refresh_cycle(period_ts)
        elapsed = now - period_ts

        # Entry window guard
        if elapsed < self.cfg.entry_window_start_sec:
            return None
        if elapsed > self.cfg.entry_window_end_sec:
            return None
        if elapsed > self.cfg.no_entry_after_sec:
            return None

        # Hour-of-day gate (default 0-23 = all hours)
        hour_utc = datetime.now(timezone.utc).hour
        if not (self.cfg.trade_hour_start_utc <= hour_utc <= self.cfg.trade_hour_end_utc):
            return None

        # Risk gate
        if self.risk_gate is not None and not self.risk_gate.ok():
            return None

        # Per-cycle cap
        if self._cycle.entry_count >= self.cfg.max_entries_per_cycle:
            return None

        # Resolve YES token for this period (cached if possible)
        if not self._cycle.yes_token_id:
            slug = _format_slug(period_ts, self.cfg.asset)
            mdoc = self._resolve_market(period_ts, slug)
            if mdoc is None:
                return None
            tokens = mdoc.get("token_ids") or []
            if len(tokens) < 1:
                return None
            self._cycle.yes_token_id = str(tokens[0])  # outcome[0] = YES/UP
            # Subscribe to this token on the WS feed (sub-100ms updates)
            if self._ws_feed is None:
                self._ws_feed = WSBookFeed(
                    asset_ids=[self._cycle.yes_token_id],
                    history_window_sec=max(60.0, self.cfg.spike_window_sec + 10.0),
                )
                self._ws_feed.start()
            else:
                self._ws_feed.add_asset(self._cycle.yes_token_id)

        # Use WS feed history instead of HTTP polling. The feed maintains
        # a per-asset rolling buffer of (ts, mid) tuples updated by
        # push events at sub-100ms latency.
        wall_history = self._ws_feed.history(self._cycle.yes_token_id)
        if len(wall_history) < 2:
            return None

        # Translate wall-clock history → in-cycle (offset, mid) and limit
        # to the spike window.
        window_start = time.time() - self.cfg.spike_window_sec
        windowed = [(ts, p) for ts, p in wall_history if ts >= window_start]
        if len(windowed) < 2:
            return None

        base = windowed[0][1]
        mid = windowed[-1][1]
        if base <= 0:
            return None
        change = (mid - base) / base

        if abs(change) < self.cfg.spike_threshold_pct:
            return None

        # Cooldown gate
        if elapsed - self._cycle.last_fire_offset < self.cfg.spike_cooldown_sec:
            return None

        direction = "up" if change > 0 else "down"
        # Opposite-direction rule: don't fire same direction twice in a row
        if direction == self._cycle.last_spike_direction:
            return None

        logger.info(
            "btc5min_timed_v3[%s] SPIKE detected: %s %.4f → %.4f (%.2f%% over %.0fs)",
            self.cfg.asset, direction, base, mid, change * 100, self.cfg.spike_window_sec,
        )
        return "spike_up" if direction == "up" else "spike_down"

    def fire(self, phase: str) -> bool:
        """Attempt entry. v3: phase is 'spike_up' or 'spike_down'."""
        period_ts = _current_period_ts()
        slug = _format_slug(period_ts, self.cfg.asset)
        # v3: fade the spike. Same TP/SL for both directions (=phase2 cfg).
        if phase == "spike_up":
            # YES rose → fade by SELLING YES (= BUY DOWN/NO)
            side = "SELL"
            label = "DOWN"
            spike_direction = "up"
        elif phase == "spike_down":
            # YES fell → fade by BUYING YES (= BUY UP)
            side = "BUY"
            label = "UP"
            spike_direction = "down"
        elif phase == "phase1":
            # Backward-compat for legacy calls — disabled by default anyway
            side = "SELL"
            label = "DOWN"
            spike_direction = ""
            self._cycle.phase1_attempts += 1
        elif phase == "phase2":
            side = "BUY"
            label = "UP"
            spike_direction = ""
            self._cycle.phase2_attempts += 1
        else:
            return False
        tp_pct = self.cfg.phase2_tp_pct
        sl_pct = self.cfg.phase2_sl_pct

        # Resolve current 5-min market via Gamma
        market_doc = self._market_cache.get(period_ts)
        if market_doc is None:
            market_doc = self._resolve_market(period_ts, slug)
        if market_doc is None:
            logger.info("btc5min_timed_v3[%s/%s] skip: no market for %s",
                        self.cfg.asset, label, slug)
            return False
        token_ids = market_doc.get("token_ids", [])
        if len(token_ids) < 2:
            logger.info("btc5min_timed_v3[%s/%s] skip: missing tokens",
                        self.cfg.asset, label)
            return False
        # token_ids[0] = YES (UP); token_ids[1] = NO (DOWN)
        token_id = token_ids[0] if side == "BUY" else token_ids[1]

        # Bug B fix: update spike state for shadow alternation. Real fire path
        # updates these inside _fire_live AFTER order success; in shadow we
        # need them set now or alternation rule never triggers.
        if phase in ("spike_up", "spike_down"):
            now_offset = time.time() - period_ts
            self._cycle.entry_count += 1
            self._cycle.last_spike_direction = spike_direction
            self._cycle.last_fire_offset = now_offset

        if not self.cfg.execute:
            logger.info(
                "btc5min_timed_v3[%s/%s] DRYRUN: side=%s token=%s tp=%.0f%% sl=%.0f%%",
                self.cfg.asset, label, side, str(token_id)[:18],
                tp_pct * 100, sl_pct * 100,
            )
            if phase == "phase1":
                self._cycle.phase1_fired = True
            else:
                self._cycle.phase2_fired = True
            self._daily.trades_today += 1
            return True

        return self._fire_live(
            phase=phase, side=side, label=label, token_id=token_id,
            market_doc=market_doc, tp_pct=tp_pct, sl_pct=sl_pct,
            period_ts=period_ts,
        )

    def _resolve_market(self, period_ts: int, slug: str):
        """Fetch the current 5-min market from Gamma API."""
        try:
            import urllib.request
            import json as _json
            import ast
            req = urllib.request.Request(
                f"https://gamma-api.polymarket.com/markets?slug={slug}",
                headers={"User-Agent": "poly1-btc5min-timed/1.0"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = _json.loads(resp.read())
        except Exception as exc:
            logger.warning("btc5min_timed_v3: market resolve failed: %s", exc)
            return None
        if not data:
            return None
        m = data[0]
        if not m.get("active", True) or m.get("closed", False):
            return None
        try:
            tokens = ast.literal_eval(m.get("clobTokenIds") or "[]")
            outcomes = ast.literal_eval(m.get("outcomes") or '["Yes","No"]')
        except Exception:
            return None

        class _Doc:
            pass
        doc = _Doc()
        doc.dict = lambda: {
            "metadata": {
                "id": m["id"],
                "outcomes": str(outcomes),
                "clob_token_ids": str(tokens),
                "outcome_prices": m.get("outcomePrices", '["0.5","0.5"]'),
            }
        }
        return {
            "market_id": str(m["id"]),
            "token_ids": [str(t) for t in tokens],
            "outcomes": outcomes,
            "doc": doc,
        }

    def _fire_live(self, *, phase, side, label, token_id, market_doc,
                   tp_pct, sl_pct, period_ts):
        """Place a real Polymarket market order with TP/SL override."""
        from agents.application.trade_log import BTC5MIN_TIMED_V3_OPEN
        from agents.utils.objects import TradeRecommendation

        market_id = market_doc["market_id"]
        if self.trade_log.has_filled_position_for_market(market_id):
            logger.info("btc5min_timed_v3[%s/%s] skip: already holds %s",
                        self.cfg.asset, label, market_id)
            return False

        try:
            force_phase1 = (
                phase == "phase1" and self.cfg.force_phase1_down_first_second
            )
            live_price, fillable_usdc, _avg = (
                self.polymarket._fillable_market_buy(
                    token_id, self.cfg.position_usdc,
                    min_entry_price=0.0 if force_phase1 else None,
                    min_bid_depth_usdc=0.0 if force_phase1 else None,
                    max_spread_pct=1.0 if force_phase1 else None,
                )
            )
        except Exception as exc:
            logger.info("btc5min_timed_v3[%s/%s] skip: book err: %s",
                        self.cfg.asset, label, exc)
            return False
        if live_price <= 0 or live_price >= 1:
            logger.info("btc5min_timed_v3[%s/%s] skip: bad price %.4f",
                        self.cfg.asset, label, live_price)
            return False
        order_amount = min(self.cfg.position_usdc, fillable_usdc)
        if order_amount < 0.10:
            logger.info("btc5min_timed_v3[%s/%s] skip: tiny fillable $%.4f",
                        self.cfg.asset, label, order_amount)
            return False
        recommendation_price = live_price if side == "BUY" else 1.0 - live_price

        # Phase 2 momentum gate (2026-05-26): historical Phase 2 WR=19%.
        # Only enter UP if UP/YES token shows clear momentum at t=180.
        if phase == "phase2":
            min_mom = self.cfg.phase2_min_momentum_price
            if live_price < min_mom:
                logger.info(
                    "btc5min_timed_v3[%s/%s] phase2 SKIP (no momentum): "
                    "YES=%.4f < threshold=%.4f",
                    self.cfg.asset, label, live_price, min_mom,
                )
                self._cycle.phase2_fired = True
                return False

        # LAYER 1 GUARD (v2): pre-entry liquidity check.
        # Operator added 2026-05-25 to prevent stuck positions.
        # Verifies that IF this trade moves -20% adverse (SL trigger),
        # there will be enough bid depth on our holding side to FAK-exit.
        # Without this, the trade would enter a market guaranteed to stick.
        if not force_phase1:
            try:
                book = self.polymarket.client.get_order_book(str(token_id))
                bids = []
                asks = []
                if hasattr(book, "bids"):
                    bids = [(float(b.price), float(b.size)) for b in (book.bids or [])]
                    asks = [(float(a.price), float(a.size)) for a in (book.asks or [])]
                elif isinstance(book, dict):
                    bids = [(float(b["price"]), float(b["size"])) for b in (book.get("bids") or [])]
                    asks = [(float(a["price"]), float(a["size"])) for a in (book.get("asks") or [])]
                best_bid = bids[0][0] if bids else 0
                best_ask = asks[0][0] if asks else 1
                spread = best_ask - best_bid
                if spread > self.cfg.max_spread_pct:
                    logger.info(
                        "btc5min_timed_v3[%s/%s] LAYER1 skip: spread too wide %.4f > %.4f",
                        self.cfg.asset, label, spread, self.cfg.max_spread_pct,
                    )
                    return False
                # SL trigger price (for our token, after entry)
                our_token_entry = live_price
                sl_trigger_price = our_token_entry * 0.80
                # Bid depth AT or BELOW sl_trigger (where we'd need to sell)
                depth_at_sl = sum(s for p, s in bids if p >= sl_trigger_price * 0.5)
                if depth_at_sl < 2.0:
                    logger.info(
                        "btc5min_timed_v3[%s/%s] LAYER1 skip: insufficient exit depth "
                        "%.2f shares < 2.0 at SL zone (trigger=%.4f)",
                        self.cfg.asset, label, depth_at_sl, sl_trigger_price,
                    )
                    return False
            except Exception as exc:
                logger.warning(
                    "btc5min_timed_v3[%s/%s] LAYER1 check failed (proceeding): %s",
                    self.cfg.asset, label, exc,
                )

        recommendation = TradeRecommendation(
            price=recommendation_price,
            size_fraction=0.0,
            side=side,
            confidence=0.50,
            amount_usdc=order_amount,
        )
        cycle_id = f"btc5min_timed_v3:{phase}:{period_ts}"
        pending_id = self.trade_log.insert_pending(
            cycle_id=cycle_id, market_id=market_id, token_id=token_id,
            side=side, price=recommendation_price, size_usdc=order_amount,
            confidence=0.50,
        )
        try:
            response = self.polymarket.execute_market_order(
                (market_doc["doc"], 0.0),
                recommendation,
                min_entry_price=0.0 if force_phase1 else None,
                min_bid_depth_usdc=0.0 if force_phase1 else None,
                max_spread_pct=1.0 if force_phase1 else None,
                max_slippage=1.0 if force_phase1 else None,
            )
        except Exception as exc:
            self.trade_log.mark(pending_id, "failed",
                                error=f"execute_market_order raised: {exc}")
            logger.warning("btc5min_timed_v3[%s/%s] live entry failed: %s",
                           self.cfg.asset, label, exc)
            return False
        if not response or response.get("status") not in ("matched", "filled"):
            self.trade_log.mark(pending_id, "failed",
                                response=response, error="entry not matched")
            return False

        # Compute dynamic max_hold so we always close ≥30s before market
        # resolves. Cycle resolves at period_ts + 300; safe close target
        # is period_ts + 270. For Phase 1 (entry ~t=1s) the 120s cap wins;
        # for Phase 2 (entry t=180s) this caps at ~90s — exactly the
        # window operator specified.
        safe_close_deadline = period_ts + 270
        seconds_to_safe_close = safe_close_deadline - time.time()
        max_hold = max(15, min(120, int(seconds_to_safe_close)))

        response_data = dict(response) if isinstance(response, dict) else {}
        response_data.update({
            "phase": phase,
            "label": label,
            "side": side,
            "actual_entry_price": live_price,
            "tp_pct_override": tp_pct,
            "sl_pct_override": sl_pct,
            # Was hardcoded 120s — bug: Phase 2 entries at t=180s + 120s =
            # close at t=300s, exactly when market resolves. Operator
            # caught this on R31. Fix: dynamic cap to (resolution - 30s).
            "max_hold_seconds": max_hold,
        })

        # CRITICAL FIX (2026-05-25): place a resting LIMIT SELL at TP price
        # IMMEDIATELY after entry. This is the HFT-style approach — instead
        # of relying on position_manager polling to detect TP and firing a
        # FAK (which fails on illiquid binaries), the LIMIT sits in the
        # book and fills the moment any taker hits it.
        # Background: Round 22 lost ~$6 because PM's FAK exits never matched.
        # See agents/application/btc5min_timed_v3.py docstring + commit log.
        try:
            # We hold `token_id` either way; live_price is that token's entry.
            our_token_entry = live_price
            # Use ACTUAL filled shares from response, not estimated.
            # For BUY: takingAmount = shares; for SELL YES: makingAmount = NO shares.
            # Apply 3% safety margin to absorb settlement rounding /
            # micro-fees that cause "balance 2.08 vs order 2.12" rejections.
            raw = response.get("raw", {}) if isinstance(response, dict) else {}
            try:
                if side == "BUY":
                    actual_shares = float(raw.get("takingAmount") or 0)
                else:
                    actual_shares = float(raw.get("makingAmount") or 0)
            except (ValueError, TypeError):
                actual_shares = 0
            if actual_shares > 0:
                shares_held = actual_shares * 0.97  # 3% margin for fees/rounding
            else:
                shares_held = (order_amount / max(our_token_entry, 0.001)) * 0.97
            tp_limit_price = round(our_token_entry * (1 + tp_pct), 4)
            # Cap at $0.99 — Polymarket clamps anyway
            tp_limit_price = min(0.99, max(0.02, tp_limit_price))
            # Wait for entry shares to SETTLE on-chain before placing the
            # resting SELL. Without this, Polymarket rejects the LIMIT with
            # "not enough balance / allowance: balance: 0" because the CTF
            # token transfer from the trade hasn't completed yet.
            # 3 seconds is enough for builder-relayer settlement in practice.
            time.sleep(3.0)
            tp_resp = self.polymarket.place_resting_limit(
                token_id=token_id,
                size_shares=shares_held,
                limit_price=tp_limit_price,
                side="SELL",
            )
            if isinstance(tp_resp, dict):
                response_data["tp_resting_order_id"] = tp_resp.get("order_id") or tp_resp.get("orderID")
                response_data["tp_resting_price"] = tp_limit_price
                response_data["tp_resting_status"] = tp_resp.get("status")
                logger.info(
                    "btc5min_timed_v3[%s/%s] resting TP placed: shares=%.4f @ %.4f order_id=%s",
                    self.cfg.asset, label, shares_held, tp_limit_price,
                    response_data.get("tp_resting_order_id"),
                )
        except Exception as exc:
            logger.warning(
                "btc5min_timed_v3[%s/%s] resting TP placement FAILED: %s — entry stands, "
                "position_manager FAK fallback will handle exit",
                self.cfg.asset, label, exc,
            )
            response_data["tp_resting_error"] = str(exc)

        self.trade_log.mark(pending_id, BTC5MIN_TIMED_V3_OPEN, response=response_data)

        # v3: track spike fire state
        now_offset = time.time() - period_ts
        if phase in ("spike_up", "spike_down"):
            self._cycle.entry_count += 1
            self._cycle.last_spike_direction = spike_direction
            self._cycle.last_fire_offset = now_offset
            # Clear price history so next spike measures from a fresh baseline
            self._cycle.price_history = []
        elif phase == "phase1":
            self._cycle.phase1_fired = True
        elif phase == "phase2":
            self._cycle.phase2_fired = True
        self._daily.trades_today += 1

        logger.info(
            "btc5min_timed_v3[%s/%s] LIVE ENTERED side=%s price=%.4f size=$%.2f tp=%.0f%% sl=%.0f%%",
            self.cfg.asset, label, side, live_price, order_amount,
            tp_pct * 100, sl_pct * 100,
        )
        return True


class Btc5MinTimedV3Daemon:
    """Long-running loop. SIGTERM-aware."""

    def __init__(self):
        self.cfg = Btc5MinTimedV3Config.from_env()
        self._stop = False
        # Lazy imports — module must be importable for tests without these.
        from agents.application.trade_log import TradeLog
        from agents.polymarket.polymarket import Polymarket
        from agents.application.risk_gate import RiskGate
        self.tl = TradeLog()
        self.polymarket = Polymarket(live=self.cfg.execute)
        self.risk_gate = RiskGate(trade_log=self.tl, polymarket=self.polymarket)
        self.engine = Btc5MinTimedV3Engine(
            polymarket=self.polymarket,
            trade_log=self.tl,
            risk_gate=self.risk_gate,
            cfg=self.cfg,
        )

    def run(self) -> None:
        import signal
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "_stop", True))
        logger.info(
            "Btc5MinTimedV3Daemon: starting execute=%s asset=%s position=$%.2f",
            self.cfg.execute, self.cfg.asset, self.cfg.position_usdc,
        )
        last_limit_poll = 0.0
        last_sl_poll = 0.0
        LIMIT_POLL_INTERVAL = 15  # seconds
        SL_POLL_INTERVAL = 2  # seconds — FAST SL detection (v2 improvement)
        try:
            while not self._stop:
                try:
                    self.engine.preload_next_market()
                    phase = self.engine.maybe_enter()
                    if phase:
                        self.engine.fire(phase)
                except Exception:
                    logger.exception("btc5min_timed_v3 cycle failed")
                # V2 IMPROVEMENT (2026-05-25): tight SL poller every 2s.
                # Detects SL trigger faster than PM's 10s polling — fires
                # cascade FAK at multiple price levels to catch the
                # liquidity window before binary collapse.
                now = time.time()
                if now - last_sl_poll > SL_POLL_INTERVAL:
                    last_sl_poll = now
                    try:
                        self._check_stop_loss_cascade()
                    except Exception:
                        logger.exception("btc5min_timed_v3 SL-poll failed (non-fatal)")
                # Poll resting LIMIT orders for fills every ~15s. Without
                # this, MATCHED limits sit silent until something else
                # closes the position. Discovered in R25: 2 LIMITs hit
                # MATCHED but the trade_log still showed btc5min_timed_v3_open.
                if now - last_limit_poll > LIMIT_POLL_INTERVAL:
                    last_limit_poll = now
                    try:
                        self._reconcile_open_limits()
                    except Exception:
                        logger.exception("btc5min_timed_v3 limit-poll failed (non-fatal)")
                # heartbeat
                try:
                    p = Path(self.cfg.heartbeat_path)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.touch()
                except Exception:
                    pass
                elapsed = time.time() - _current_period_ts()
                near_boundary = (
                    elapsed <= self.cfg.fast_boundary_window_sec
                    or elapsed >= 300 - self.cfg.fast_preload_seconds
                )
                sleep_for = self.cfg.fast_poll_sec if (
                    self.cfg.fast_preload_enabled and near_boundary
                ) else self.cfg.poll_sec
                time.sleep(max(0.01, sleep_for))
        finally:
            logger.info("Btc5MinTimedV3Daemon: exited")

    def _reconcile_open_limits(self) -> None:
        """Scan btc5min_timed_v3_open positions; if their resting LIMIT TP
        is MATCHED on Polymarket, write a close row with the realized PnL.

        Bug discovered in R25: limits filled in the book but the local
        DB still showed btc5min_timed_v3_open → equity reporting wrong.
        """
        import json as _json
        import sqlite3 as _sql
        with _sql.connect(self.tl.db_path, timeout=5) as conn:
            conn.row_factory = _sql.Row
            rows = conn.execute(
                """
                SELECT t1.id, t1.market_id, t1.token_id, t1.side, t1.price,
                       t1.size_usdc, t1.response_json
                FROM trades t1
                WHERE t1.status = 'btc5min_timed_v3_open'
                  AND NOT EXISTS (
                    SELECT 1 FROM trades t2
                    WHERE t2.token_id = t1.token_id
                      AND t2.id > t1.id
                      AND (t2.status LIKE 'closed_%' OR t2.status LIKE 'resolved_%')
                  )
                """
            ).fetchall()
        for r in rows:
            try:
                resp = _json.loads(r["response_json"] or "{}")
            except Exception:
                continue
            tp_order_id = resp.get("tp_resting_order_id")
            if not tp_order_id:
                continue
            try:
                order = self.polymarket.client.get_order(tp_order_id)
            except Exception:
                continue
            if not isinstance(order, dict):
                continue
            if order.get("status", "").upper() != "MATCHED":
                continue
            size_matched = float(order.get("size_matched") or 0)
            exit_price = float(order.get("price") or 0)
            if size_matched <= 0 or exit_price <= 0:
                continue
            entry_price = float(r["price"])
            entry_side = resp.get("side", r["side"] or "BUY")
            our_token_entry = (
                entry_price if entry_side == "BUY"
                else max(0.01, 1.0 - entry_price)
            )
            proceeds = size_matched * exit_price
            cost_basis = size_matched * our_token_entry
            pnl = proceeds - cost_basis
            close_response = {
                "source": "btc5min_timed_v3_daemon_poll",
                "tp_resting_order_id": tp_order_id,
                "exit_price": exit_price,
                "shares_sold": size_matched,
                "actual_proceeds_usdc": round(proceeds, 4),
                "cost_basis_usdc": round(cost_basis, 4),
                "pnl_usdc_real": round(pnl, 4),
                "status": "matched",
            }
            cycle_id = f"close:{str(r['token_id'])[:12]}"
            self.tl.insert_terminal(
                cycle_id=cycle_id,
                market_id=str(r["market_id"]),
                status="closed_take_profit",
                token_id=str(r["token_id"]),
                side="SELL",
                price=exit_price,
                size_usdc=proceeds,
                confidence=None,
                response=close_response,
            )
            logger.info(
                "btc5min_timed_v3 reconciled LIMIT fill: id=%s shares=%.4f @ %.4f PnL=$%+.4f",
                r["id"], size_matched, exit_price, pnl,
            )

    def _check_stop_loss_cascade(self) -> None:
        """V2 IMPROVEMENT: fast SL detection + cascade FAK.

        Every 2 seconds, scans all v2 open positions. For each:
          - Query current CLOB book for our token
          - Compute current PnL on our holding
          - If PnL ≤ -SL_pct (e.g., -20%): fire FAK cascade:
              level 1: SELL @ trigger_price (current bid)
              level 2: SELL @ trigger * 0.7 (if level 1 fails)
              level 3: SELL @ trigger * 0.4 (catastrophe floor)
          - First match wins, others canceled.

        This is the v2 improvement over v1: instead of waiting for PM's
        10s polling + FAK that may fail, we react in <2s and try multiple
        price levels to catch the brief liquidity window.
        """
        import json as _json
        import sqlite3 as _sql
        with _sql.connect(self.tl.db_path, timeout=5) as conn:
            conn.row_factory = _sql.Row
            rows = conn.execute(
                """
                SELECT t1.id, t1.market_id, t1.token_id, t1.side, t1.price,
                       t1.size_usdc, t1.response_json
                FROM trades t1
                WHERE t1.status = 'btc5min_timed_v3_open'
                  AND NOT EXISTS (
                    SELECT 1 FROM trades t2
                    WHERE t2.token_id = t1.token_id
                      AND t2.id > t1.id
                      AND (t2.status LIKE 'closed_%' OR t2.status LIKE 'resolved_%')
                  )
                """
            ).fetchall()
        for r in rows:
            try:
                resp = _json.loads(r["response_json"] or "{}")
            except Exception:
                continue
            sl_pct = float(resp.get("sl_pct_override") or 0.20)
            entry_price = float(r["price"])
            entry_side = resp.get("side", r["side"] or "BUY")
            our_token_entry = (
                entry_price if entry_side == "BUY"
                else max(0.01, 1.0 - entry_price)
            )
            sl_trigger = our_token_entry * (1.0 - sl_pct)

            # Get current best bid for our token
            try:
                book = self.polymarket.client.get_order_book(str(r["token_id"]))
                # bids are sorted high-to-low; best bid is first
                best_bid = None
                if hasattr(book, "bids") and book.bids:
                    best_bid = float(book.bids[0].price)
                elif isinstance(book, dict):
                    bids = book.get("bids") or []
                    if bids:
                        best_bid = float(bids[0].get("price", 0))
            except Exception:
                continue
            if best_bid is None or best_bid <= 0:
                continue

            if best_bid > sl_trigger:
                continue  # not at SL yet

            # SL triggered — cascade FAK exit
            # Determine shares from response or estimate
            raw = resp.get("raw", {})
            try:
                if entry_side == "BUY":
                    shares_held = float(raw.get("takingAmount") or 0)
                else:
                    shares_held = float(raw.get("makingAmount") or 0)
            except (ValueError, TypeError):
                shares_held = 0
            if shares_held <= 0:
                shares_held = float(r["size_usdc"]) / max(our_token_entry, 0.001)
            shares_held *= 0.97  # safety margin

            # LAYER 2 (v2 expanded 2026-05-25): 5-level cascade catches
            # liquidity at progressively lower prices. Each level is FAK,
            # first match wins, others not attempted.
            cascade_prices = [
                round(best_bid, 4),                      # L1: best bid
                round(best_bid * 0.7, 4),                # L2: -30%
                round(best_bid * 0.5, 4),                # L3: -50% (NEW)
                round(max(0.02, best_bid * 0.3), 4),    # L4: -70% (NEW)
                round(max(0.02, best_bid * 0.1), 4),    # L5: floor
            ]
            # Dedupe (clamping may have collapsed L4/L5 to same value)
            cascade_prices = list(dict.fromkeys(cascade_prices))
            logger.warning(
                "btc5min_timed_v3[id=%s] SL TRIGGERED: bid=%.4f trigger=%.4f cascading FAK",
                r["id"], best_bid, sl_trigger,
            )
            from py_clob_client_v2.clob_types import OrderArgs, OrderType
            for level, price in enumerate(cascade_prices, 1):
                try:
                    with __import__("agents.polymarket.polymarket", fromlist=["live_order_lock"]).live_order_lock():
                        sell_resp = self.polymarket.client.create_and_post_order(
                            OrderArgs(
                                price=price, size=shares_held,
                                side="SELL", token_id=str(r["token_id"]),
                            ),
                            order_type=OrderType.FAK,
                        )
                    if isinstance(sell_resp, dict) and sell_resp.get("status") in ("matched", "filled"):
                        # Write close row
                        proceeds = shares_held * price
                        cost_basis = shares_held * our_token_entry
                        pnl = proceeds - cost_basis
                        close_response = {
                            "source": "btc5min_timed_v3_sl_cascade",
                            "cascade_level": level,
                            "exit_price": price,
                            "shares_sold": shares_held,
                            "actual_proceeds_usdc": round(proceeds, 4),
                            "cost_basis_usdc": round(cost_basis, 4),
                            "pnl_usdc_real": round(pnl, 4),
                            "status": "matched",
                        }
                        self.tl.insert_terminal(
                            cycle_id=f"close:{str(r['token_id'])[:12]}",
                            market_id=str(r["market_id"]),
                            status="closed_stop_loss",
                            token_id=str(r["token_id"]),
                            side="SELL",
                            price=price,
                            size_usdc=proceeds,
                            confidence=None,
                            response=close_response,
                        )
                        logger.warning(
                            "btc5min_timed_v3[id=%s] SL FAK cascade level %d matched @ %.4f PnL=$%+.4f",
                            r["id"], level, price, pnl,
                        )
                        break
                except Exception as exc:
                    logger.debug(
                        "btc5min_timed_v3 SL cascade level %d failed: %s",
                        level, exc,
                    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    Btc5MinTimedV3Daemon().run()
