"""BTC Daily Up/Down — fast in/out agent for poly1.

Targets the daily ``bitcoin-up-or-down-on-{date}`` Polymarket binary:
when BTC moves sharply over a short window, the market overshoots in
the same direction. We bet against the move and exit on take-profit,
stop-loss, or timeout.

Design: minimal, self-contained, no LLM. Reuses poly1's Polymarket
adapter + TradeLog + RiskGate. Mirrors swarm's ``mean_reversion_agent``
in spirit but lives entirely inside poly1 (single deposit wallet, single
risk-gate ledger).

Position lifecycle:
  trigger → execute_market_order (BUY YES or NO) → poll midpoint every N sec
  → exit on TP / SL / max_hold / end-of-day → write closed row.

Storage: new ``btc_daily_positions`` table in ``trade_log.db``.
A normal ``trades`` row is still written for each leg with a new
status ``btc_daily_open`` (audit trail consistent with scalper's
``scalper_leg``).
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
import urllib.request
import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from agents.application.trade_log import TradeLog
from agents.application.execution_safety import exitable_size_check


logger = logging.getLogger(__name__)


BTC_DAILY_OPEN = "btc_daily_open"
# Note: there is no `btc_daily_closed` status — exits are owned by
# position_manager and write `closed_take_profit/stop_loss/timeout`.
# The string is kept in capital_allocator's status filter for legacy
# row tolerance (no live row has ever written it).


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


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
class BtcDailyConfig:
    """Env-driven config. All values overridable via .env."""
    # Entry trigger: |btc_pct_change_over_window| >= trigger_pct
    trigger_pct: float = 0.004                  # 0.4%
    window_sec: int = 180                       # 3 min
    cooldown_sec: int = 180                     # min seconds between entries
    # Sizing
    position_size_usdc: float = 3.0
    # NOTE on exits: btc_daily does NOT manage its own exits. Open
    # positions are picked up by `position_manager` (which sees them
    # via the `btc_daily_open` journal status) and closed by it using
    # `MAINTAIN_TAKE_PROFIT_PCT` / `MAINTAIN_STOP_LOSS_PCT` /
    # `MAINTAIN_TRAILING_STOP_PCT` / `MAINTAIN_MAX_HOLD_HOURS`. The
    # legacy `take_profit_pct` / `stop_loss_pct` / `sell_slippage` /
    # `max_hold_minutes` / `eod_close_minute` config and the
    # `maybe_exit` / `_close_position` methods that consumed them
    # were removed 2026-05-08 after we verified zero `btc_daily_close`
    # rows had ever been written — position_manager always won the
    # race. See `agents/application/position_manager.py` for the
    # single source of truth on btc_daily exit behaviour.
    # Skip-entry if the candidate token has already decayed past this floor.
    # Below 0.30 means the market has effectively chosen a side and
    # fading is no longer a mean-reversion bet — it's catching a falling knife.
    min_candidate_price: float = 0.30
    # Skip-entry if the candidate token is already above this ceiling.
    # Above 0.65 means the market has strongly priced the outcome; a mean-
    # reversion entry would require buying an overpriced token and face
    # slippage rejection (root cause of the 2026-05-13 stop-loss cascade).
    max_entry_price: float = 0.65
    # Skip if longer-window trend agrees with the short move (don't fight
    # a trend day). 0.8% catches real trend days; 2.5% almost never fired.
    skip_on_strong_trend: bool = True
    trend_window_minutes: int = 30
    trend_threshold_pct: float = 0.008          # 0.8%
    # Slippage kill-switch: give up on a market after this many consecutive
    # slippage failures. Prevents tight-loop retries when the market has moved
    # far from our anchor (e.g. ask=0.77 vs recommended=0.50).
    max_slippage_skips: int = 3
    # Capital reserve — keeps this slice of wallet off-limits to other agents
    reserve_usdc: float = 6.0
    # Polling
    poll_sec: int = 5                            # entry-loop cadence
    # Heartbeat
    heartbeat_path: str = "/app/data/btc_daily_heartbeat"

    @classmethod
    def from_env(cls) -> "BtcDailyConfig":
        return cls(
            trigger_pct=_env_float("BTC_DAILY_TRIGGER_PCT", 0.004),
            window_sec=_env_int("BTC_DAILY_WINDOW_SEC", 180),
            cooldown_sec=_env_int("BTC_DAILY_COOLDOWN_SEC", 180),
            position_size_usdc=_env_float("BTC_DAILY_POSITION_SIZE_USDC", 3.0),
            min_candidate_price=_env_float("BTC_DAILY_MIN_CANDIDATE_PRICE", 0.30),
            max_entry_price=_env_float("BTC_DAILY_MAX_ENTRY_PRICE", 0.65),
            skip_on_strong_trend=os.getenv(
                "BTC_DAILY_SKIP_TREND", "true"
            ).lower() == "true",
            trend_window_minutes=_env_int("BTC_DAILY_TREND_WINDOW_MIN", 30),
            trend_threshold_pct=_env_float("BTC_DAILY_TREND_THRESHOLD_PCT", 0.008),
            max_slippage_skips=_env_int("BTC_DAILY_MAX_SLIPPAGE_SKIPS", 3),
            poll_sec=_env_int("BTC_DAILY_POLL_SEC", 5),
            heartbeat_path=os.getenv(
                "BTC_DAILY_HEARTBEAT_PATH", "/app/data/btc_daily_heartbeat"
            ),
            reserve_usdc=_env_float("BTC_DAILY_RESERVE_USDC", 6.0),
        )


# ---------------------------------------------------------------------------
# BTC price feed — Coinbase public REST. Lightweight, no WebSocket dep.
# ---------------------------------------------------------------------------


class CoinbasePriceFeed:
    """Polls Coinbase spot price; keeps a small ring buffer for window % change."""
    URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"

    def __init__(self, max_history_sec: int = 1800):
        self.max_history_sec = max_history_sec
        self._samples: list[tuple[int, float]] = []  # (ts_ms, price)

    def update(self) -> Optional[float]:
        try:
            with urllib.request.urlopen(self.URL, timeout=5) as resp:
                data = json.loads(resp.read())
            price = float(data["data"]["amount"])
        except Exception as exc:
            logger.warning("btc_daily feed: fetch failed: %s", exc)
            return None
        ts_ms = int(time.time() * 1000)
        self._samples.append((ts_ms, price))
        cutoff = ts_ms - self.max_history_sec * 1000
        self._samples = [(t, p) for t, p in self._samples if t >= cutoff]
        return price

    def percent_change(self, window_sec: int) -> Optional[float]:
        """Return (last_price - oldest_in_window) / oldest_in_window, or None."""
        if len(self._samples) < 2:
            return None
        last_ts, last_px = self._samples[-1]
        target_ts = last_ts - window_sec * 1000
        # Find the oldest sample at or before target_ts.
        oldest = None
        for t, p in self._samples:
            if t <= target_ts:
                oldest = (t, p)
            else:
                break
        if oldest is None:
            # Not enough history yet; use the very first sample we have.
            if (last_ts - self._samples[0][0]) < window_sec * 500:
                return None  # less than half the window — too noisy
            oldest = self._samples[0]
        if oldest[1] <= 0:
            return None
        return (last_px - oldest[1]) / oldest[1]


# ---------------------------------------------------------------------------
# Slug resolution
# ---------------------------------------------------------------------------


def format_btc_daily_slug(when: Optional[datetime] = None) -> str:
    """Polymarket's daily BTC slug. Format verified against Gamma:
    ``bitcoin-up-or-down-on-{full-month}-{day-no-leading-zero}-{year}``.
    """
    if when is None:
        when = datetime.now(timezone.utc)
    month = when.strftime("%B").lower()
    return f"bitcoin-up-or-down-on-{month}-{when.day}-{when.year}"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass
class OpenPosition:
    """In-memory tracker for one open position."""
    market_id: str            # Polymarket market id (numeric str)
    token_id: str             # ERC-1155 token id (uint256 as str)
    outcome: str              # 'Yes' or 'No' — what we're long
    entry_price: float        # 0..1 of the token we bought
    entry_size_usdc: float    # USDC paid at entry
    shares: float             # entry_size_usdc / entry_price (approx)
    opened_ts_ms: int
    btc_move_at_entry: float
    db_row_id: int            # row id of the open audit row in `trades`


class BtcDailyEngine:
    """Stateful engine. One open position max at a time (single-shot day)."""

    def __init__(
        self,
        polymarket,                   # agents.polymarket.polymarket.Polymarket
        trade_log: TradeLog,
        risk_gate,                    # RiskGate (for ok() check)
        feed: CoinbasePriceFeed,
        cfg: BtcDailyConfig,
        execute: bool = False,
    ):
        self.polymarket = polymarket
        self.trade_log = trade_log
        self.risk_gate = risk_gate
        self.feed = feed
        self.cfg = cfg
        self.execute = execute
        self.shadow_ignore_risk_gate = (
            os.getenv("SHADOW_IGNORE_RISK_GATE", "false").lower() == "true"
        )
        self.open_position: Optional[OpenPosition] = None
        self.last_entry_ms: int = 0
        self._market_cache: dict[str, dict] = {}  # slug → market metadata
        # Consecutive slippage failures per market_id — reset on success.
        # When >= cfg.max_slippage_skips we stop retrying for this daemon run.
        self._slippage_fails: dict[str, int] = {}

    # ------------------------------------------------------------------ entry

    def maybe_enter(self) -> Optional[OpenPosition]:
        """Check trigger; place an order if conditions met. Returns the
        new OpenPosition, or None if no action taken.
        """
        if self.open_position is not None:
            return None
        now_ms = int(time.time() * 1000)
        if now_ms - self.last_entry_ms < self.cfg.cooldown_sec * 1000:
            return None
        if self.risk_gate is not None and not self.risk_gate.ok():
            if self.execute or not self.shadow_ignore_risk_gate:
                return None
            logger.warning(
                "btc_daily: risk gate blocked but shadow evaluation continues: %s",
                self.risk_gate.reason(),
            )

        btc_move = self.feed.percent_change(self.cfg.window_sec)
        if btc_move is None or abs(btc_move) < self.cfg.trigger_pct:
            return None

        if self.cfg.skip_on_strong_trend:
            longer = self.feed.percent_change(self.cfg.trend_window_minutes * 60)
            if longer is not None and abs(longer) > self.cfg.trend_threshold_pct:
                # Don't fight a trend.
                if (btc_move > 0) == (longer > 0):
                    logger.info(
                        "btc_daily: skip — strong trend %s%.2f%% in %dm",
                        "+" if longer > 0 else "", longer * 100,
                        self.cfg.trend_window_minutes,
                    )
                    return None

        # Fade the move: BTC pumped → buy NO; BTC dumped → buy YES.
        # In poly1 convention, our LLM uses "BUY ⇒ token_ids[0] (Yes)" and
        # "SELL ⇒ token_ids[1] (No)". For symmetry with the existing
        # executor we reuse the same TradeRecommendation shape.
        side = "SELL" if btc_move > 0 else "BUY"  # SELL ≡ buy NO

        market_doc = self._resolve_today_market()
        if market_doc is None:
            return None

        # Fix #2: don't average down. Block re-entry if we already have a
        # filled (live or recent paper-closed) position on this market. The
        # in-memory `open_position` guard above only catches positions held
        # in the current daemon process; the journal check survives restarts
        # and prevents the 14-entries-on-one-token disaster from 2026-05-07.
        market_id = market_doc.get("market_id", "")
        if self._slippage_fails.get(market_id, 0) >= self.cfg.max_slippage_skips:
            logger.info(
                "btc_daily: skip — %d consecutive slippage failures on %s, "
                "market has moved; giving up until daemon restart",
                self._slippage_fails[market_id], market_id,
            )
            return None
        if self.trade_log.has_filled_position_for_market(market_id):
            logger.info(
                "btc_daily: skip — already holds filled position on market %s",
                market_id,
            )
            return None

        safety = exitable_size_check(
            amount_usdc=self.cfg.position_size_usdc,
            entry_price=0.50,
        )
        if not safety.ok:
            self.trade_log.insert_terminal(
                cycle_id=self.trade_log.new_cycle_id(),
                market_id=market_id,
                status="skipped_gate",
                side=side,
                price=0.50,
                size_usdc=self.cfg.position_size_usdc,
                confidence=0.7,
                error=f"btc_daily_{safety.reason}",
            )
            logger.info("btc_daily: skip — %s", safety.reason)
            return None

        # Fix #3: skip if the candidate token has already decayed past the
        # mean-reversion floor, OR is already above the ceiling.
        # Floor: YES at $0.05 → fading "BTC dumped" is catching a falling knife.
        # Ceiling: NO at $0.73 → market has decided; we'd face slippage cap.
        # The fetched candidate_mid also becomes the anchor price (Fix #4).
        candidate_mid = 0.5  # default if pre-check is unavailable
        token_ids = market_doc.get("token_ids", [])
        if len(token_ids) >= 2:
            try:
                candidate_token = token_ids[0] if side == "BUY" else token_ids[1]
                mid_resp = self.polymarket.client.get_midpoint(candidate_token)
                candidate_mid = float(
                    mid_resp.get("mid", 0.5) if isinstance(mid_resp, dict)
                    else mid_resp
                )
            except Exception as exc:
                logger.warning("btc_daily mid pre-check failed: %s", exc)
                candidate_mid = 0.5  # fail-open
            if candidate_mid < self.cfg.min_candidate_price:
                logger.info(
                    "btc_daily: skip — candidate %s mid=%.4f < floor %.2f "
                    "(market already chose direction)",
                    side, candidate_mid, self.cfg.min_candidate_price,
                )
                return None
            if candidate_mid > self.cfg.max_entry_price:
                logger.info(
                    "btc_daily: skip — candidate %s mid=%.4f > ceiling %.2f "
                    "(token overpriced; slippage would reject)",
                    side, candidate_mid, self.cfg.max_entry_price,
                )
                return None

        from agents.utils.objects import TradeRecommendation
        # Fix #4: use the actual candidate midpoint as the anchor price.
        # Using a hardcoded 0.5 caused all 4 live slippage rejections in
        # 2026-05-13 — the token's ask was 0.73-0.99 while our anchor was 0.50,
        # triggering the slippage cap on every attempt.
        recommendation = TradeRecommendation(
            price=candidate_mid,
            size_fraction=0.0,
            side=side,
            confidence=0.7,
            amount_usdc=self.cfg.position_size_usdc,
        )
        cycle_id = self.trade_log.new_cycle_id()
        # market_id and token_ids already resolved above (Fix #2/#3 pre-checks)
        # Pre-record pending row for crash recovery.
        token_id_for_log = token_ids[0] if side == "BUY" else (token_ids[1] if len(token_ids) > 1 else "")
        pending_id = self.trade_log.insert_pending(
            cycle_id=cycle_id,
            market_id=market_id,
            token_id=token_id_for_log,
            side=side,
            price=recommendation.price,
            size_usdc=self.cfg.position_size_usdc,
            confidence=recommendation.confidence,
        )

        if not self.execute:
            # Shadow path — log a synthetic fill, do NOT touch CLOB.
            self.trade_log.mark(
                pending_id, BTC_DAILY_OPEN,
                response={"shadow": True, "side": side, "btc_move": btc_move},
                error=f"SHADOW: would have entered {side} on btc_move={btc_move:+.4f}",
            )
            logger.info(
                "btc_daily SHADOW: %s on btc_move=%+.4f", side, btc_move,
            )
            self.last_entry_ms = now_ms
            return None

        # Live path — call the existing executor.
        try:
            response = self.polymarket.execute_market_order(
                (market_doc["doc"], 0.0), recommendation,
            )
        except Exception as exc:
            # Apply cooldown even on failure so we don't hammer the CLOB
            # every 5 seconds on repeated slippage errors.
            self.last_entry_ms = now_ms
            self.trade_log.mark(
                pending_id, "failed", error=f"execute_market_order raised: {exc}"
            )
            exc_msg = str(exc)
            logger.warning("btc_daily entry failed: %s", exc)
            # Track consecutive slippage failures for the kill-switch.
            if "max slippage" in exc_msg or "exceeds recommended price" in exc_msg:
                self._slippage_fails[market_id] = (
                    self._slippage_fails.get(market_id, 0) + 1
                )
                logger.info(
                    "btc_daily: slippage fail #%d on %s (max=%d)",
                    self._slippage_fails[market_id], market_id,
                    self.cfg.max_slippage_skips,
                )
            # If the market has no orderbook (resolved/delisted), evict cache
            # so the next cycle re-fetches and detects the closed state.
            if "status_code=404" in exc_msg or "No orderbook" in exc_msg:
                slug = format_btc_daily_slug()
                self._market_cache.pop(slug, None)
                logger.info(
                    "btc_daily: evicted stale cache for %s (no orderbook)", slug
                )
            return None

        if not response or not response.get("status") in ("matched", "filled"):
            self.trade_log.mark(
                pending_id, "failed",
                response=response, error="entry not matched",
            )
            return None

        entry_price = float(response.get("order_avg_price_estimate", 0.5))
        entry_size_usdc = float(response.get("amount_usdc", self.cfg.position_size_usdc))
        response = {
            **response,
            "actual_entry_price": entry_price,
            "price_accounting": "actual_token_fill_price",
        }
        self.trade_log.mark(
            pending_id,
            BTC_DAILY_OPEN,
            response=response,
            price=entry_price,
            size_usdc=entry_size_usdc,
        )
        # Successful entry — reset the slippage failure counter for this market.
        self._slippage_fails.pop(market_id, None)
        shares = entry_size_usdc / max(entry_price, 0.01)
        token_id = response.get("token_id", token_id_for_log)

        pos = OpenPosition(
            market_id=market_id,
            token_id=token_id,
            outcome=response.get("outcome_traded", "Yes" if side == "BUY" else "No"),
            entry_price=entry_price,
            entry_size_usdc=entry_size_usdc,
            shares=shares,
            opened_ts_ms=now_ms,
            btc_move_at_entry=btc_move,
            db_row_id=pending_id,
        )
        self.open_position = pos
        self.last_entry_ms = now_ms
        logger.info(
            "btc_daily ENTRY: %s on %s @ %.3f shares=%.2f (btc_move=%+.4f)",
            side, market_id, entry_price, shares, btc_move,
        )
        return pos

    # ------------------------------------------------------------------ exit
    #
    # btc_daily DOES NOT manage its own exits. After 2026-05-08 we
    # verified zero `btc_daily_close` rows had ever been written —
    # `position_manager` (running every 60 s) always beat the legacy
    # `maybe_exit` poll, since position_manager's MAINTAIN_TAKE_PROFIT_PCT
    # is lower than the engine's local take_profit_pct. The legacy
    # `maybe_exit` / `_is_after_eod` / `_close_position` methods were
    # removed to eliminate config drift (two thresholds claiming to
    # govern the same exit). See `position_manager.check_and_close_positions`
    # for the live path.

    # -------------------------------------------------------------- helpers

    def _resolve_today_market(self) -> Optional[dict]:
        slug = format_btc_daily_slug()
        if slug in self._market_cache:
            return self._market_cache[slug]
        try:
            import requests
            r = requests.get(
                f"https://gamma-api.polymarket.com/markets?slug={slug}",
                timeout=10,
            )
            data = r.json()
            if not data:
                logger.warning("btc_daily: no market for slug %s", slug)
                return None
            m = data[0]
            # Skip closed/resolved markets — no orderbook exists.
            if not m.get("active", True) or m.get("closed", False):
                logger.info(
                    "btc_daily: market %s is closed/inactive — skipping",
                    m.get("id", slug),
                )
                return None
            import ast
            tokens = ast.literal_eval(m["clobTokenIds"])
            outcomes = ast.literal_eval(m["outcomes"])
            # Build a TradeRecommendation-compatible market doc. The
            # Polymarket adapter expects ``(doc_with_metadata_dict, score)``.
            class _Doc:
                pass
            doc = _Doc()
            doc.dict = lambda: {  # noqa: E731  — callable to mimic langchain
                "metadata": {
                    "id": m["id"],
                    "outcomes": str(outcomes),
                    "clob_token_ids": str(tokens),
                    "outcome_prices": m.get("outcomePrices", '["0.5","0.5"]'),
                }
            }
            entry = {
                "market_id": str(m["id"]),
                "token_ids": tokens,
                "outcomes": outcomes,
                "doc": doc,
            }
            self._market_cache[slug] = entry
            return entry
        except Exception as exc:
            logger.warning("btc_daily resolve %s failed: %s", slug, exc)
            return None


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class BtcDailyDaemon:
    """Long-running loop. SIGTERM-aware. One process per replica."""

    def __init__(self, db_path: Optional[str] = None, execute: Optional[bool] = None):
        self.cfg = BtcDailyConfig.from_env()
        self.execute = (
            execute if execute is not None
            else os.getenv("EXECUTE_BTC_DAILY", "false").lower() == "true"
        )
        self.tl = TradeLog(db_path=db_path)
        self.feed = CoinbasePriceFeed()
        # Lazy import: poly1's Polymarket / RiskGate are heavy.
        from agents.polymarket.polymarket import Polymarket
        from agents.application.risk_gate import RiskGate
        self.polymarket = Polymarket(live=self.execute)
        self.risk_gate = RiskGate(
            trade_log=self.tl,
            polymarket=self.polymarket,
            btc_daily_reserve_usdc=self.cfg.reserve_usdc,
        )
        self.engine = BtcDailyEngine(
            polymarket=self.polymarket,
            trade_log=self.tl,
            risk_gate=self.risk_gate,
            feed=self.feed,
            cfg=self.cfg,
            execute=self.execute,
        )
        self.heartbeat = Path(self.cfg.heartbeat_path)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            signal.signal(signal.SIGTERM, lambda *_: self.stop())
            signal.signal(signal.SIGINT, lambda *_: self.stop())
        except (ValueError, OSError):
            pass
        logger.info("BtcDailyDaemon: starting (execute=%s)", self.execute)
        try:
            while not self._stop.is_set():
                try:
                    self.feed.update()
                except Exception:
                    logger.exception("btc_daily feed update failed")
                try:
                    self.engine.maybe_enter()
                except Exception:
                    logger.exception("btc_daily entry check failed")
                # Exits are owned by position_manager (see comment near
                # the maybe_exit deletion above). No exit poll here.
                try:
                    self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
                    self.heartbeat.touch()
                except Exception:
                    logger.warning("btc_daily heartbeat touch failed")
                self._stop.wait(self.cfg.poll_sec)
        finally:
            logger.info("BtcDailyDaemon: exited")


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)
    BtcDailyDaemon().run()
