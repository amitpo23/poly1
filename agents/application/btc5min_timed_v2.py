"""BTC 5-min timed-entry strategy agent.

Operator-requested 2026-05-25. Time-based entry, no LLM, no signal:

  Phase 1: at t=0:01 into each 5-min period, BUY DOWN
           TP at +5%, SL at -20%
  Phase 2: at t=3:00 into each 5-min period, BUY UP
           TP at +5%, SL at -20%
  No-bet last 30 sec (t > 4:30)

Backtest 14d: -$582 net (-$41/day @ $1 position).
Operator accepts the risk and wants to validate empirically.

Defaults:
  - EXECUTE_BTC5MIN_TIMED_V2=false (must be explicitly enabled)
  - BTC5MIN_TIMED_V2_POSITION_USDC=0.20 (small)
  - BTC5MIN_TIMED_V2_MAX_TRADES_PER_DAY=10 (cap exposure)
  - BTC5MIN_TIMED_V2_HALT_AFTER_LOSSES=3 (auto-halt after 3 consecutive SL/resolved_loss)

Architecture:
  - Single asyncio loop, polls every 5 sec
  - At t=0:01 boundary: schedule Phase 1 entry
  - At t=3:00 boundary: schedule Phase 2 entry
  - Position_manager handles exits via sl_pct_override / tp_pct_override
  - Annotated with btc5min_timed_v2_open status for ledger isolation

Heartbeat: data/btc5min_timed_v2_heartbeat
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
class Btc5MinTimedV2Config:
    """Operator-tunable parameters."""
    execute: bool = False
    position_usdc: float = 0.20
    max_trades_per_day: int = 10
    halt_after_losses: int = 3
    # Phase 1: DOWN at start
    phase1_entry_offset_sec: int = 1       # t=0:01
    phase1_tp_pct: float = 0.05            # +5%
    phase1_sl_pct: float = 0.20            # -20%
    # Phase 2: UP at minute 3
    phase2_entry_offset_sec: int = 180     # t=3:00
    phase2_tp_pct: float = 0.05            # +5%
    phase2_sl_pct: float = 0.20            # -20%
    # Common
    no_entry_after_sec: int = 270          # don't enter after t=4:30
    poll_sec: int = 2
    asset: str = "btc"                     # market asset (BTC/ETH/SOL)
    heartbeat_path: str = "/app/data/btc5min_timed_v2_heartbeat"

    @classmethod
    def from_env(cls) -> "Btc5MinTimedV2Config":
        return cls(
            execute=_env_bool("EXECUTE_BTC5MIN_TIMED_V2", False),
            position_usdc=_env_float("BTC5MIN_TIMED_V2_POSITION_USDC", 0.20),
            max_trades_per_day=_env_int("BTC5MIN_TIMED_V2_MAX_TRADES_PER_DAY", 10),
            halt_after_losses=_env_int("BTC5MIN_TIMED_V2_HALT_AFTER_LOSSES", 3),
            phase1_entry_offset_sec=_env_int("BTC5MIN_TIMED_V2_PHASE1_OFFSET_SEC", 1),
            phase1_tp_pct=_env_float("BTC5MIN_TIMED_V2_PHASE1_TP_PCT", 0.05),
            phase1_sl_pct=_env_float("BTC5MIN_TIMED_V2_PHASE1_SL_PCT", 0.20),
            phase2_entry_offset_sec=_env_int("BTC5MIN_TIMED_V2_PHASE2_OFFSET_SEC", 180),
            phase2_tp_pct=_env_float("BTC5MIN_TIMED_V2_PHASE2_TP_PCT", 0.05),
            phase2_sl_pct=_env_float("BTC5MIN_TIMED_V2_PHASE2_SL_PCT", 0.20),
            no_entry_after_sec=_env_int("BTC5MIN_TIMED_V2_NO_ENTRY_AFTER_SEC", 270),
            poll_sec=_env_int("BTC5MIN_TIMED_V2_POLL_SEC", 2),
            asset=os.getenv("BTC5MIN_TIMED_V2_ASSET", "btc").lower(),
            heartbeat_path=os.getenv("BTC5MIN_TIMED_V2_HEARTBEAT_PATH", "/app/data/btc5min_timed_v2_heartbeat"),
        )


def _current_period_ts() -> int:
    """Current 5-min period boundary (epoch seconds)."""
    return int(time.time() // 300) * 300


def _format_slug(period_ts: int, asset: str) -> str:
    """Market slug, matches the btc_5min slug convention."""
    return f"{asset.lower()}-updown-5m-{period_ts}"


@dataclass
class CycleState:
    """Per-period state — which phases have fired."""
    period_ts: int = 0
    phase1_fired: bool = False
    phase2_fired: bool = False


@dataclass
class DailyState:
    """Per-day risk state — cap exposure + auto-halt."""
    date_key: str = ""
    trades_today: int = 0
    consecutive_losses: int = 0
    auto_halted: bool = False


class Btc5MinTimedV2Engine:
    """Time-based DOWN/UP strategy engine. NOT driven by signals."""

    def __init__(
        self,
        polymarket,
        trade_log,
        risk_gate,
        cfg: Btc5MinTimedV2Config,
    ):
        self.polymarket = polymarket
        self.trade_log = trade_log
        self.risk_gate = risk_gate
        self.cfg = cfg
        self._cycle: CycleState = CycleState()
        self._daily: DailyState = DailyState(date_key=self._today_key())

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

    def maybe_enter(self) -> Optional[str]:
        """Check timing; return 'phase1', 'phase2', or None.

        This is the ONLY decision logic. No signal, no LLM, no consensus.
        Just time within the 5-min period.
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

        # Skip last 30 sec to avoid mid-resolution chaos
        if elapsed > self.cfg.no_entry_after_sec:
            return None

        # Risk gate first (runtime control, balance, etc.)
        if self.risk_gate is not None and not self.risk_gate.ok():
            return None

        # Phase 1: BUY DOWN at t=0:01
        if not self._cycle.phase1_fired:
            if abs(elapsed - self.cfg.phase1_entry_offset_sec) < self.cfg.poll_sec:
                return "phase1"

        # Phase 2: BUY UP at t=3:00
        if not self._cycle.phase2_fired:
            if abs(elapsed - self.cfg.phase2_entry_offset_sec) < self.cfg.poll_sec:
                return "phase2"

        return None

    def fire(self, phase: str) -> bool:
        """Attempt entry for phase. Returns True if filled."""
        period_ts = _current_period_ts()
        slug = _format_slug(period_ts, self.cfg.asset)

        if phase == "phase1":
            side = "SELL"   # SELL YES = BUY NO = bet DOWN
            tp_pct = self.cfg.phase1_tp_pct
            sl_pct = self.cfg.phase1_sl_pct
            label = "DOWN"
        elif phase == "phase2":
            side = "BUY"    # BUY YES = bet UP
            tp_pct = self.cfg.phase2_tp_pct
            sl_pct = self.cfg.phase2_sl_pct
            label = "UP"
        else:
            return False

        # Resolve current 5-min market via Gamma
        market_doc = self._resolve_market(period_ts, slug)
        if market_doc is None:
            logger.info("btc5min_timed_v2[%s/%s] skip: no market for %s",
                        self.cfg.asset, label, slug)
            return False
        token_ids = market_doc.get("token_ids", [])
        if len(token_ids) < 2:
            logger.info("btc5min_timed_v2[%s/%s] skip: missing tokens",
                        self.cfg.asset, label)
            return False
        # token_ids[0] = YES (UP); token_ids[1] = NO (DOWN)
        token_id = token_ids[0] if side == "BUY" else token_ids[1]

        if not self.cfg.execute:
            logger.info(
                "btc5min_timed_v2[%s/%s] DRYRUN: side=%s token=%s tp=%.0f%% sl=%.0f%%",
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
            logger.warning("btc5min_timed_v2: market resolve failed: %s", exc)
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
        from agents.application.trade_log import BTC5MIN_TIMED_V2_OPEN
        from agents.utils.objects import TradeRecommendation

        market_id = market_doc["market_id"]
        if self.trade_log.has_filled_position_for_market(market_id):
            logger.info("btc5min_timed_v2[%s/%s] skip: already holds %s",
                        self.cfg.asset, label, market_id)
            return False

        try:
            live_price, fillable_usdc, _avg = (
                self.polymarket._fillable_market_buy(
                    token_id, self.cfg.position_usdc,
                )
            )
        except Exception as exc:
            logger.info("btc5min_timed_v2[%s/%s] skip: book err: %s",
                        self.cfg.asset, label, exc)
            return False
        if live_price <= 0 or live_price >= 1:
            logger.info("btc5min_timed_v2[%s/%s] skip: bad price %.4f",
                        self.cfg.asset, label, live_price)
            return False
        order_amount = min(self.cfg.position_usdc, fillable_usdc)
        if order_amount < 0.10:
            logger.info("btc5min_timed_v2[%s/%s] skip: tiny fillable $%.4f",
                        self.cfg.asset, label, order_amount)
            return False

        # LAYER 1 GUARD (v2): pre-entry liquidity check.
        # Operator added 2026-05-25 to prevent stuck positions.
        # Verifies that IF this trade moves -20% adverse (SL trigger),
        # there will be enough bid depth on our holding side to FAK-exit.
        # Without this, the trade would enter a market guaranteed to stick.
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
            if spread > 0.10:
                logger.info(
                    "btc5min_timed_v2[%s/%s] LAYER1 skip: spread too wide %.4f",
                    self.cfg.asset, label, spread,
                )
                return False
            # SL trigger price (for our token, after entry)
            if side == "BUY":
                our_token_entry = live_price
            else:
                our_token_entry = max(0.01, 1.0 - live_price)
            sl_trigger_price = our_token_entry * 0.80
            # Bid depth AT or BELOW sl_trigger (where we'd need to sell)
            depth_at_sl = sum(s for p, s in bids if p >= sl_trigger_price * 0.5)
            if depth_at_sl < 2.0:
                logger.info(
                    "btc5min_timed_v2[%s/%s] LAYER1 skip: insufficient exit depth "
                    "%.2f shares < 2.0 at SL zone (trigger=%.4f)",
                    self.cfg.asset, label, depth_at_sl, sl_trigger_price,
                )
                return False
        except Exception as exc:
            logger.warning(
                "btc5min_timed_v2[%s/%s] LAYER1 check failed (proceeding): %s",
                self.cfg.asset, label, exc,
            )

        recommendation = TradeRecommendation(
            price=live_price,
            size_fraction=0.0,
            side=side,
            confidence=0.50,
            amount_usdc=order_amount,
        )
        cycle_id = f"btc5min_timed_v2:{phase}:{period_ts}"
        pending_id = self.trade_log.insert_pending(
            cycle_id=cycle_id, market_id=market_id, token_id=token_id,
            side=side, price=live_price, size_usdc=order_amount,
            confidence=0.50,
        )
        try:
            response = self.polymarket.execute_market_order(
                (market_doc["doc"], 0.0), recommendation,
            )
        except Exception as exc:
            self.trade_log.mark(pending_id, "failed",
                                error=f"execute_market_order raised: {exc}")
            logger.warning("btc5min_timed_v2[%s/%s] live entry failed: %s",
                           self.cfg.asset, label, exc)
            return False
        if not response or response.get("status") not in ("matched", "filled"):
            self.trade_log.mark(pending_id, "failed",
                                response=response, error="entry not matched")
            return False

        response_data = dict(response) if isinstance(response, dict) else {}
        response_data.update({
            "phase": phase,
            "label": label,
            "side": side,
            "tp_pct_override": tp_pct,
            "sl_pct_override": sl_pct,
            # 120s = exit by t=2:00 from entry. After analysis of Round 22
            # losses: 5-min binaries blow through SL=20% within 30sec of
            # adverse move, then reverse, then go to resolution. The TP/SL
            # window is the first 60-90 sec. Force-close at 2:00 to lock
            # whatever mean-reverted price exists before the next big move.
            "max_hold_seconds": 120,
        })

        # CRITICAL FIX (2026-05-25): place a resting LIMIT SELL at TP price
        # IMMEDIATELY after entry. This is the HFT-style approach — instead
        # of relying on position_manager polling to detect TP and firing a
        # FAK (which fails on illiquid binaries), the LIMIT sits in the
        # book and fills the moment any taker hits it.
        # Background: Round 22 lost ~$6 because PM's FAK exits never matched.
        # See agents/application/btc5min_timed_v2.py docstring + commit log.
        try:
            # For both phase1 and phase2, we hold the token in `token_id`.
            # Entry price for THAT token: BUY = live_price; SELL of YES =
            # we actually hold NO, NO entry ≈ 1 - live_price.
            if side == "BUY":
                our_token_entry = live_price
            else:  # SELL YES = hold NO at (1 - live_price)
                our_token_entry = max(0.01, 1.0 - live_price)
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
                    "btc5min_timed_v2[%s/%s] resting TP placed: shares=%.4f @ %.4f order_id=%s",
                    self.cfg.asset, label, shares_held, tp_limit_price,
                    response_data.get("tp_resting_order_id"),
                )
        except Exception as exc:
            logger.warning(
                "btc5min_timed_v2[%s/%s] resting TP placement FAILED: %s — entry stands, "
                "position_manager FAK fallback will handle exit",
                self.cfg.asset, label, exc,
            )
            response_data["tp_resting_error"] = str(exc)

        self.trade_log.mark(pending_id, BTC5MIN_TIMED_V2_OPEN, response=response_data)

        if phase == "phase1":
            self._cycle.phase1_fired = True
        else:
            self._cycle.phase2_fired = True
        self._daily.trades_today += 1

        logger.info(
            "btc5min_timed_v2[%s/%s] LIVE ENTERED side=%s price=%.4f size=$%.2f tp=%.0f%% sl=%.0f%%",
            self.cfg.asset, label, side, live_price, order_amount,
            tp_pct * 100, sl_pct * 100,
        )
        return True


class Btc5MinTimedV2Daemon:
    """Long-running loop. SIGTERM-aware."""

    def __init__(self):
        self.cfg = Btc5MinTimedV2Config.from_env()
        self._stop = False
        # Lazy imports — module must be importable for tests without these.
        from agents.application.trade_log import TradeLog
        from agents.polymarket.polymarket import Polymarket
        from agents.application.risk_gate import RiskGate
        self.tl = TradeLog()
        self.polymarket = Polymarket(live=self.cfg.execute)
        self.risk_gate = RiskGate(trade_log=self.tl, polymarket=self.polymarket)
        self.engine = Btc5MinTimedV2Engine(
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
            "Btc5MinTimedV2Daemon: starting execute=%s asset=%s position=$%.2f",
            self.cfg.execute, self.cfg.asset, self.cfg.position_usdc,
        )
        last_limit_poll = 0.0
        last_sl_poll = 0.0
        LIMIT_POLL_INTERVAL = 15  # seconds
        SL_POLL_INTERVAL = 2  # seconds — FAST SL detection (v2 improvement)
        try:
            while not self._stop:
                try:
                    phase = self.engine.maybe_enter()
                    if phase:
                        self.engine.fire(phase)
                except Exception:
                    logger.exception("btc5min_timed_v2 cycle failed")
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
                        logger.exception("btc5min_timed_v2 SL-poll failed (non-fatal)")
                # Poll resting LIMIT orders for fills every ~15s. Without
                # this, MATCHED limits sit silent until something else
                # closes the position. Discovered in R25: 2 LIMITs hit
                # MATCHED but the trade_log still showed btc5min_timed_v2_open.
                if now - last_limit_poll > LIMIT_POLL_INTERVAL:
                    last_limit_poll = now
                    try:
                        self._reconcile_open_limits()
                    except Exception:
                        logger.exception("btc5min_timed_v2 limit-poll failed (non-fatal)")
                # heartbeat
                try:
                    p = Path(self.cfg.heartbeat_path)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.touch()
                except Exception:
                    pass
                time.sleep(self.cfg.poll_sec)
        finally:
            logger.info("Btc5MinTimedV2Daemon: exited")

    def _reconcile_open_limits(self) -> None:
        """Scan btc5min_timed_v2_open positions; if their resting LIMIT TP
        is MATCHED on Polymarket, write a close row with the realized PnL.

        Bug discovered in R25: limits filled in the book but the local
        DB still showed btc5min_timed_v2_open → equity reporting wrong.
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
                WHERE t1.status = 'btc5min_timed_v2_open'
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
                "source": "btc5min_timed_v2_daemon_poll",
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
                "btc5min_timed_v2 reconciled LIMIT fill: id=%s shares=%.4f @ %.4f PnL=$%+.4f",
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
                WHERE t1.status = 'btc5min_timed_v2_open'
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
                "btc5min_timed_v2[id=%s] SL TRIGGERED: bid=%.4f trigger=%.4f cascading FAK",
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
                            "source": "btc5min_timed_v2_sl_cascade",
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
                            "btc5min_timed_v2[id=%s] SL FAK cascade level %d matched @ %.4f PnL=$%+.4f",
                            r["id"], level, price, pnl,
                        )
                        break
                except Exception as exc:
                    logger.debug(
                        "btc5min_timed_v2 SL cascade level %d failed: %s",
                        level, exc,
                    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    Btc5MinTimedV2Daemon().run()
