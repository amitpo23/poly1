"""Wallet-Follow Agent — poly1 copy-trading strategy.

Reads fresh ``wallet_signals`` rows written by ``wallet_watcher`` and
enters positions that copy smart-wallet trades — subject to EV, entry-price,
liquidity, dedupe, and risk-gate checks.

EV calculation:
  bullish signal → buy YES token → EV = confidence × (1 − yes_price)
  bearish signal → buy NO token  → EV = confidence × yes_price
  confidence = min(1.0, wallet_profit_usdc / WALLET_FOLLOW_PROFIT_SCALE)
               clamped to [WALLET_FOLLOW_MIN_CONFIDENCE, 1.0]

Where ``wallet_profit_usdc`` is the smart wallet's reported 30d profit from
the leaderboard (or scouted metrics). A wallet with profit ≥ PROFIT_SCALE
gets confidence 1.0; a wallet with $0 profit gets the minimum.

Position lifecycle:
  read wallet_signals → confidence → EV gate → RiskGate.ok() → dedupe
  → execute_market_order → wallet_follow_open row
  → exits owned by position_manager (TP / SL / max_hold_hours)

Storage: standard ``trades`` table, status ``wallet_follow_open``.
Signal row: ``wallet_signals.status`` updated to 'acted' or 'skipped'.

Environment variables (all optional, see defaults below):
  WALLET_FOLLOW_MIN_CONFIDENCE   — min confidence score (default 0.50)
  WALLET_FOLLOW_PROFIT_SCALE     — profit $ for full confidence (default 1000)
  WALLET_FOLLOW_MIN_EV           — min expected value (default 0.03)
  WALLET_FOLLOW_MAX_ENTRY_PRICE  — max entry price for the token (default 0.70)
  WALLET_FOLLOW_MAX_DRIFT         — max yes_price drift vs wallet entry price (default 0.10)
  WALLET_FOLLOW_MIN_LIQUIDITY    — min $USDC volume (default 3000)
  WALLET_FOLLOW_MIN_TRADES       — min wallet trades in 30d (default 5)
  WALLET_FOLLOW_POSITION_SIZE_USDC — size per trade (default 2.5)
  WALLET_FOLLOW_RESERVE_USDC     — capital reserved for this agent (default 15)
  WALLET_FOLLOW_MAX_AGE_HOURS    — max signal age to act on (default 1)
  WALLET_FOLLOW_POLL_SEC         — loop cadence in seconds (default 60)
  WALLET_FOLLOW_MAX_OPEN         — max concurrent open positions (default 3)
  WALLET_FOLLOW_HEARTBEAT_PATH   — file path for heartbeat (default /app/data/wallet_follow_heartbeat)
  EXECUTE_WALLET_FOLLOW          — set "true" to live-trade (default false)
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from agents.application.trade_log import WALLET_FOLLOW_OPEN, TradeLog
from agents.application.tavily import tavily_headlines

logger = logging.getLogger(__name__)

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"


# ---------------------------------------------------------------------------
# Helpers
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


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class WalletFollowConfig:
    min_confidence: float = 0.50
    profit_scale: float = 1000.0
    min_ev: float = 0.03
    max_entry_price: float = 0.70
    max_drift: float = 0.10
    min_wallet_trades: int = 5
    min_liquidity: float = 3000.0
    position_size_usdc: float = 2.5
    reserve_usdc: float = 15.0
    max_age_hours: float = 1.0
    poll_sec: int = 60
    max_open: int = 3
    heartbeat_path: str = "/app/data/wallet_follow_heartbeat"

    @classmethod
    def from_env(cls) -> "WalletFollowConfig":
        return cls(
            min_confidence=_env_float("WALLET_FOLLOW_MIN_CONFIDENCE", 0.50),
            profit_scale=_env_float("WALLET_FOLLOW_PROFIT_SCALE", 1000.0),
            min_ev=_env_float("WALLET_FOLLOW_MIN_EV", 0.03),
            max_entry_price=_env_float("WALLET_FOLLOW_MAX_ENTRY_PRICE", 0.70),
            max_drift=_env_float("WALLET_FOLLOW_MAX_DRIFT", 0.10),
            min_wallet_trades=_env_int("WALLET_FOLLOW_MIN_TRADES", 5),
            min_liquidity=_env_float("WALLET_FOLLOW_MIN_LIQUIDITY", 3000.0),
            position_size_usdc=_env_float("WALLET_FOLLOW_POSITION_SIZE_USDC", 2.5),
            reserve_usdc=_env_float("WALLET_FOLLOW_RESERVE_USDC", 15.0),
            max_age_hours=_env_float("WALLET_FOLLOW_MAX_AGE_HOURS", 1.0),
            poll_sec=_env_int("WALLET_FOLLOW_POLL_SEC", 60),
            max_open=_env_int("WALLET_FOLLOW_MAX_OPEN", 3),
            heartbeat_path=os.getenv(
                "WALLET_FOLLOW_HEARTBEAT_PATH",
                "/app/data/wallet_follow_heartbeat",
            ),
        )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class WalletFollowEngine:
    def __init__(
        self,
        polymarket,
        trade_log: TradeLog,
        risk_gate,
        cfg: WalletFollowConfig,
        execute: bool = False,
    ):
        self.polymarket = polymarket
        self.trade_log = trade_log
        self.risk_gate = risk_gate
        self.cfg = cfg
        self.execute = execute

    # ---------------------------------------------------- read signals

    def _read_fresh_signals(self) -> list[dict]:
        """Return fresh wallet_signals rows not yet acted/skipped."""
        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=self.cfg.max_age_hours)
        cutoff_str = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            with self.trade_log._lock, self.trade_log._connect() as conn:
                rows = conn.execute(
                    "SELECT id, ts, wallet_address, wallet_profit_usdc, "
                    "wallet_trades_30d, market_id, market_question, direction, "
                    "token_id, yes_price, wallet_entry_price, wallet_size_usdc "
                    "FROM wallet_signals "
                    "WHERE status = 'fresh' AND ts >= ? "
                    "ORDER BY wallet_profit_usdc DESC",
                    (cutoff_str,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("wallet_follow: DB read failed: %s", exc)
            return []

    def _mark_signal(self, signal_id: int, status: str) -> None:
        try:
            with self.trade_log._lock, self.trade_log._connect() as conn:
                conn.execute(
                    "UPDATE wallet_signals SET status = ? WHERE id = ?",
                    (status, signal_id),
                )
        except Exception as exc:
            logger.warning("wallet_follow: mark signal %d failed: %s", signal_id, exc)

    # --------------------------------------------------- confidence calc

    def _confidence(self, wallet_profit_usdc: float) -> float:
        if self.cfg.profit_scale <= 0:
            return self.cfg.min_confidence
        raw = wallet_profit_usdc / self.cfg.profit_scale
        return min(1.0, max(self.cfg.min_confidence, raw))

    # --------------------------------------------------- Gamma lookup

    def _gamma_market(self, market_id: str) -> Optional[dict]:
        try:
            params = urllib.parse.urlencode({"id": market_id})
            url = f"{GAMMA_MARKETS_URL}?{params}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "poly1-wallet-follow/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict) and data:
                return data
        except Exception as exc:
            logger.warning("wallet_follow: Gamma lookup %s failed: %s", market_id, exc)
        return None

    # --------------------------------------------------- market doc

    @staticmethod
    def _make_market_doc(market_id: str, outcomes: list, tokens: list,
                         yes_price: float, no_price: float) -> dict:
        class _Doc:
            pass

        doc = _Doc()
        doc.dict = lambda: {  # noqa: E731
            "metadata": {
                "id": market_id,
                "outcomes": str(outcomes),
                "clob_token_ids": str(tokens),
                "outcome_prices": str([str(yes_price), str(no_price)]),
            }
        }
        return {"doc": doc, "market_id": market_id}

    # -------------------------------------------------------- main loop

    def maybe_enter_all(self) -> int:
        """Process fresh signals and enter qualifying trades. Returns entries made."""
        signals = self._read_fresh_signals()
        if not signals:
            return 0

        # Count existing wallet_follow open positions
        try:
            open_rows = self.trade_log.filled_positions_with_id()
            wf_open = sum(1 for r in open_rows if r.get("status") == WALLET_FOLLOW_OPEN)
        except Exception:
            wf_open = 0

        if wf_open >= self.cfg.max_open:
            logger.info(
                "wallet_follow: already %d/%d open; skipping cycle",
                wf_open, self.cfg.max_open,
            )
            return 0

        # Deduplicate signals: best (highest wallet_profit) signal per market
        seen: dict[str, dict] = {}
        for sig in signals:
            mid = sig.get("market_id", "")
            if not mid:
                continue
            profit = float(sig.get("wallet_profit_usdc") or 0.0)
            if mid not in seen or profit > float(seen[mid].get("wallet_profit_usdc") or 0.0):
                seen[mid] = sig

        entries_made = 0
        for market_id, sig in seen.items():
            if wf_open + entries_made >= self.cfg.max_open:
                break

            signal_id = sig["id"]

            # Dedupe against trades table
            if self.trade_log.has_active_trade_for_market(market_id):
                logger.debug("wallet_follow: dedupe skip %s", market_id)
                self._mark_signal(signal_id, "skipped")
                continue

            # Confidence from wallet profit
            wallet_profit = float(sig.get("wallet_profit_usdc") or 0.0)
            confidence = self._confidence(wallet_profit)

            if confidence < self.cfg.min_confidence:
                logger.debug("wallet_follow: low confidence %.2f skip %s", confidence, market_id)
                self._mark_signal(signal_id, "skipped")
                continue

            # Wallet quality filter: ignore lucky wallets with too few trades
            wallet_trades = int(sig.get("wallet_trades_30d") or 0)
            if wallet_trades < self.cfg.min_wallet_trades:
                logger.debug(
                    "wallet_follow: skip %s — wallet only %d trades in 30d (min=%d)",
                    market_id, wallet_trades, self.cfg.min_wallet_trades,
                )
                self._mark_signal(signal_id, "skipped")
                continue

            # Risk gate
            if self.risk_gate is not None and not self.risk_gate.ok():
                logger.info("wallet_follow: risk gate blocked: %s", self.risk_gate.reason())
                self._mark_signal(signal_id, "skipped")
                break

            # Fetch current market data from Gamma
            mkt = self._gamma_market(market_id)
            if mkt is None:
                self._mark_signal(signal_id, "skipped")
                continue

            if not mkt.get("active") or mkt.get("closed"):
                self._mark_signal(signal_id, "skipped")
                continue

            try:
                outcomes = json.loads(mkt.get("outcomes", "[]"))
                tokens = json.loads(mkt.get("clobTokenIds", "[]"))
            except (json.JSONDecodeError, TypeError):
                try:
                    import ast
                    outcomes = ast.literal_eval(mkt.get("outcomes", "[]"))
                    tokens = ast.literal_eval(mkt.get("clobTokenIds", "[]"))
                except Exception:
                    self._mark_signal(signal_id, "skipped")
                    continue

            if len(outcomes) != 2 or len(tokens) != 2:
                self._mark_signal(signal_id, "skipped")
                continue

            try:
                prices = json.loads(mkt.get("outcomePrices", '["0.5","0.5"]'))
                yes_price = float(prices[0])
                no_price = float(prices[1])
            except (json.JSONDecodeError, IndexError, TypeError, ValueError):
                yes_price = float(sig.get("yes_price") or 0.5)
                no_price = 1.0 - yes_price

            # Price-drift check: if the market has already moved > max_drift in the
            # wallet's direction since they entered, the trade is already priced in.
            wallet_entry_price = sig.get("wallet_entry_price")
            if wallet_entry_price is not None:
                try:
                    wep = float(wallet_entry_price)
                    drift = yes_price - wep
                    direction_raw = sig.get("direction", "")
                    if direction_raw == "bullish" and drift > self.cfg.max_drift:
                        logger.info(
                            "wallet_follow: skip %s — bullish already priced in "
                            "(entry=%.3f current=%.3f drift=+%.3f)",
                            market_id, wep, yes_price, drift,
                        )
                        self._mark_signal(signal_id, "skipped")
                        continue
                    if direction_raw == "bearish" and drift < -self.cfg.max_drift:
                        logger.info(
                            "wallet_follow: skip %s — bearish already priced in "
                            "(entry=%.3f current=%.3f drift=%.3f)",
                            market_id, wep, yes_price, drift,
                        )
                        self._mark_signal(signal_id, "skipped")
                        continue
                except (TypeError, ValueError):
                    pass

            # Liquidity filter
            liquidity = float(mkt.get("volumeClob") or mkt.get("volume24hr") or 0)
            if liquidity < self.cfg.min_liquidity:
                self._mark_signal(signal_id, "skipped")
                continue

            direction = sig["direction"]

            # EV and side determination
            if direction == "bullish":
                ev = confidence * (1.0 - yes_price)
                side = "BUY"
                entry_price = yes_price
                token_idx = 0
            elif direction == "bearish":
                ev = confidence * yes_price
                side = "SELL"
                entry_price = no_price
                token_idx = 1
            else:
                self._mark_signal(signal_id, "skipped")
                continue

            if ev < self.cfg.min_ev:
                logger.debug(
                    "wallet_follow: skip %s ev=%.3f < %.3f", market_id, ev, self.cfg.min_ev
                )
                self._mark_signal(signal_id, "skipped")
                continue

            if entry_price > self.cfg.max_entry_price:
                logger.debug(
                    "wallet_follow: skip %s entry_price=%.3f > %.3f",
                    market_id, entry_price, self.cfg.max_entry_price,
                )
                self._mark_signal(signal_id, "skipped")
                continue

            # Tavily external context — log fresh headlines before following
            # the whale so the reasoning is auditable in logs.
            # Fails open: missing key or network error → no skip.
            market_question = str(mkt.get("question", "")) if mkt else ""
            if market_question:
                tavily_ctx = tavily_headlines(market_question, max_results=3)
                if tavily_ctx:
                    logger.info(
                        "wallet_follow: Tavily context for %s: %s",
                        market_id, tavily_ctx[:200],
                    )

            from agents.utils.objects import TradeRecommendation

            recommendation = TradeRecommendation(
                price=yes_price,
                size_fraction=0.0,
                side=side,
                confidence=confidence,
                amount_usdc=self.cfg.position_size_usdc,
            )

            market_doc = self._make_market_doc(
                market_id, outcomes, tokens, yes_price, no_price
            )
            token_id_for_log = tokens[token_idx]

            cycle_id = self.trade_log.new_cycle_id()
            pending_id = self.trade_log.insert_pending(
                cycle_id=cycle_id,
                market_id=market_id,
                token_id=token_id_for_log,
                side=side,
                price=yes_price,
                size_usdc=self.cfg.position_size_usdc,
                confidence=confidence,
            )

            if not self.execute:
                self.trade_log.mark(
                    pending_id,
                    WALLET_FOLLOW_OPEN,
                    response={"shadow": True, "direction": direction, "ev": ev,
                              "wallet_profit": wallet_profit},
                    error=(
                        f"SHADOW: would enter {side} on {market_id} "
                        f"dir={direction} wallet_profit=${wallet_profit:.0f} "
                        f"confidence={confidence:.2f} ev={ev:.3f}"
                    ),
                )
                self._mark_signal(signal_id, "acted")
                logger.info(
                    "wallet_follow SHADOW: %s %s dir=%s conf=%.2f ev=%.3f",
                    side, market_id, direction, confidence, ev,
                )
                entries_made += 1
                continue

            # Live path
            try:
                response = self.polymarket.execute_market_order(
                    (market_doc["doc"], 0.0), recommendation
                )
            except Exception as exc:
                self.trade_log.mark(
                    pending_id, "failed",
                    error=f"execute_market_order raised: {exc}",
                )
                self._mark_signal(signal_id, "skipped")
                logger.warning("wallet_follow entry failed %s: %s", market_id, exc)
                continue

            if not response or response.get("status") not in ("matched", "filled"):
                self.trade_log.mark(
                    pending_id, "failed",
                    response=response, error="entry not matched",
                )
                self._mark_signal(signal_id, "skipped")
                continue

            self.trade_log.mark(pending_id, WALLET_FOLLOW_OPEN, response=response)
            self._mark_signal(signal_id, "acted")
            logger.info(
                "wallet_follow ENTRY: %s %s dir=%s conf=%.2f ev=%.3f",
                side, market_id, direction, confidence, ev,
            )
            entries_made += 1

        return entries_made


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class WalletFollowDaemon:
    """Long-running loop. SIGTERM-aware."""

    def __init__(self, db_path: Optional[str] = None, execute: Optional[bool] = None):
        self.cfg = WalletFollowConfig.from_env()
        self.execute = (
            execute if execute is not None
            else os.getenv("EXECUTE_WALLET_FOLLOW", "false").lower() == "true"
        )
        self.trade_log = TradeLog(db_path=db_path)
        self._stop = threading.Event()

    def _handle_sigterm(self, *_) -> None:
        logger.info("wallet_follow: SIGTERM received — stopping")
        self._stop.set()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

        from agents.application.risk_gate import RiskGate
        risk_gate = RiskGate(
            trade_log=self.trade_log,
            polymarket=None,
            wallet_follow_reserve_usdc=self.cfg.reserve_usdc,
        )

        polymarket = None
        if self.execute:
            from agents.polymarket.polymarket import Polymarket
            polymarket = Polymarket(live=True)

        engine = WalletFollowEngine(
            polymarket=polymarket,
            trade_log=self.trade_log,
            risk_gate=risk_gate,
            cfg=self.cfg,
            execute=self.execute,
        )

        logger.info(
            "wallet_follow: starting — execute=%s poll=%ds max_open=%d",
            self.execute, self.cfg.poll_sec, self.cfg.max_open,
        )

        while not self._stop.is_set():
            try:
                n = engine.maybe_enter_all()
                if n:
                    logger.info("wallet_follow: %d entries made", n)
            except Exception as exc:
                logger.exception("wallet_follow: unhandled error: %s", exc)

            # Heartbeat
            try:
                hb = self.cfg.heartbeat_path
                os.makedirs(os.path.dirname(hb) or ".", exist_ok=True)
                with open(hb, "w") as f:
                    f.write(datetime.now(timezone.utc).isoformat())
            except OSError:
                pass

            self._stop.wait(self.cfg.poll_sec)

        logger.info("wallet_follow: stopped cleanly")


if __name__ == "__main__":
    WalletFollowDaemon().run()
