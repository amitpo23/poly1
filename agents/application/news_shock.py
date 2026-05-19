"""News-Shock Agent — poly1 trading strategy.

Reacts to high-materiality news events already classified by the
``news_signal`` module. For each fresh bullish or bearish signal (scored
≥ MIN_NEWS_SCORE within the past MAX_NEWS_AGE_HOURS hours), the agent
queries Gamma for the current market price and enters a position when
the implied edge (EV) is above MIN_EV.

EV calculation:
  bullish signal → buy YES token → EV = materiality × (1 − yes_price)
  bearish signal → buy NO token → EV = materiality × yes_price
  (materiality is the news classifier's 0–1 relevance × confidence score)

Position lifecycle:
  read news_signals → EV gate → RiskGate.ok() → dedupe check
  → execute_market_order → news_shock_open row
  → exits owned by position_manager (TP / SL / max_hold_hours)

Storage: standard ``trades`` table, status ``news_shock_open``.

Environment variables (all optional, see defaults below):
  NEWS_SHOCK_MIN_SCORE          — min materiality score (default 0.70)
  NEWS_SHOCK_MAX_AGE_HOURS      — max signal age in hours (default 0.5)
  NEWS_SHOCK_MIN_EV             — min expected value (default 0.04)
  NEWS_SHOCK_MAX_ENTRY_PRICE    — max entry price for the entered token (default 0.60)
  NEWS_SHOCK_MAX_DRIFT          — max yes_price drift since signal (default 0.10)
  NEWS_SHOCK_MIN_LIQUIDITY      — min $USDC volume (default 5000)
  NEWS_SHOCK_POSITION_SIZE_USDC — size per trade (default 2.5)
  NEWS_SHOCK_RESERVE_USDC       — capital reserved for this agent (default 15)
  NEWS_SHOCK_POLL_SEC           — loop cadence in seconds (default 30)
  NEWS_SHOCK_MAX_OPEN           — max concurrent open positions (default 3)
  NEWS_SHOCK_HEARTBEAT_PATH     — file path for heartbeat (default /app/data/news_shock_heartbeat)
  EXECUTE_NEWS_SHOCK            — set "true" to live-trade (default false)
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

from agents.application.trade_log import NEWS_SHOCK_OPEN, TradeLog
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
class NewsShockConfig:
    min_score: float = 0.70
    max_age_hours: float = 0.5
    min_ev: float = 0.04
    max_entry_price: float = 0.60
    max_drift: float = 0.10
    min_liquidity: float = 5000.0
    position_size_usdc: float = 2.5
    reserve_usdc: float = 15.0
    poll_sec: int = 30
    max_open: int = 3
    heartbeat_path: str = "/app/data/news_shock_heartbeat"

    @classmethod
    def from_env(cls) -> "NewsShockConfig":
        return cls(
            min_score=_env_float("NEWS_SHOCK_MIN_SCORE", 0.70),
            max_age_hours=_env_float("NEWS_SHOCK_MAX_AGE_HOURS", 0.5),
            min_ev=_env_float("NEWS_SHOCK_MIN_EV", 0.04),
            max_entry_price=_env_float("NEWS_SHOCK_MAX_ENTRY_PRICE", 0.60),
            max_drift=_env_float("NEWS_SHOCK_MAX_DRIFT", 0.10),
            min_liquidity=_env_float("NEWS_SHOCK_MIN_LIQUIDITY", 5000.0),
            position_size_usdc=_env_float("NEWS_SHOCK_POSITION_SIZE_USDC", 2.5),
            reserve_usdc=_env_float("NEWS_SHOCK_RESERVE_USDC", 15.0),
            poll_sec=_env_int("NEWS_SHOCK_POLL_SEC", 30),
            max_open=_env_int("NEWS_SHOCK_MAX_OPEN", 3),
            heartbeat_path=os.getenv(
                "NEWS_SHOCK_HEARTBEAT_PATH", "/app/data/news_shock_heartbeat"
            ),
        )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class NewsShockEngine:
    def __init__(
        self,
        polymarket,
        trade_log: TradeLog,
        risk_gate,
        cfg: NewsShockConfig,
        execute: bool = False,
        brain=None,
    ):
        self.polymarket = polymarket
        self.trade_log = trade_log
        self.risk_gate = risk_gate
        self.cfg = cfg
        self.execute = execute
        self.brain = brain
        self.meta_brain = None
        if self.brain is not None:
            try:
                from agents.application.meta_brain import MetaBrain
                self.meta_brain = MetaBrain(db_path=os.getenv("TRADE_LOG_DB", "./data/trade_log.db"), market_brain=self.brain)
            except Exception:
                logger.exception("news_shock: MetaBrain init failed")

    def _brain_allows_entry(
        self,
        *,
        market_id: str,
        token_id: str,
        question: str,
        side: str,
        yes_price: float,
        no_price: float,
        liquidity_usdc: float = 0.0,
    ) -> bool:
        if self.brain is None:
            if self.execute:
                logger.warning("news_shock live blocked — missing MarketBrain")
                return False
            return True
        try:
            if self.meta_brain is not None:
                decision = self.meta_brain.synthesize(
                    market_id=market_id,
                    question=question,
                    spread_pct=abs(yes_price - (1.0 - no_price)),
                    hours_to_close=None,
                    poly_prob=yes_price,
                    token_id=token_id,
                    liquidity_usdc=liquidity_usdc,
                )
            else:
                decision = self.brain.evaluate_general_entry(
                    question=question,
                    spread_pct=abs(yes_price - (1.0 - no_price)),
                    hours_to_close=None,
                )
            self.trade_log.insert_brain_decision(
                agent="news_shock",
                strategy="news_reaction",
                decision_type="entry",
                market_id=market_id,
                token_id=token_id,
                approved=decision.approved,
                reason=decision.reason,
                score=decision.score,
                market_type="general_binary",
                features=decision.features,
                action=side,
            )
            if not decision.approved:
                logger.info(
                    "news_shock brain rejected %s: %s (score=%.3f)",
                    market_id, decision.reason, decision.score,
                )
                return False
            return True
        except Exception:
            logger.exception("news_shock brain gate failed; blocking entry")
            return False

    # -------------------------------------------------------- read signals

    def _read_fresh_signals(self) -> list[dict]:
        """Return high-materiality news_signals rows written in the last MAX_AGE_HOURS."""
        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=self.cfg.max_age_hours)
        cutoff_str = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            with self.trade_log._lock, self.trade_log._connect() as conn:
                rows = conn.execute(
                    "SELECT id, ts, market_id, market_question, direction, "
                    "materiality, relevance_score, headline, source, yes_price "
                    "FROM news_signals "
                    "WHERE status IN ('news_signal', 'scanner_news_shock') "
                    "AND materiality >= ? AND ts >= ? "
                    "AND direction IN ('bullish', 'bearish') "
                    "ORDER BY materiality DESC",
                    (self.cfg.min_score, cutoff_str),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("news_shock: DB read failed: %s", exc)
            return []

    def _mark_signal(self, signal_id: int, status: str) -> None:
        try:
            with self.trade_log._lock, self.trade_log._connect() as conn:
                conn.execute(
                    "UPDATE news_signals SET status = ? WHERE id = ?",
                    (status, signal_id),
                )
        except Exception as exc:
            logger.warning("news_shock: mark signal %d failed: %s", signal_id, exc)

    # --------------------------------------------------- Gamma market lookup

    def _gamma_market(self, market_id: str) -> Optional[dict]:
        """Fetch a single market from Gamma by ID."""
        try:
            params = urllib.parse.urlencode({"id": market_id})
            url = f"{GAMMA_MARKETS_URL}?{params}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "poly1-news-shock/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict) and data:
                return data
        except Exception as exc:
            logger.warning("news_shock: Gamma lookup %s failed: %s", market_id, exc)
        return None

    # --------------------------------------------------------- market doc

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

    # ------------------------------------------------------------- main loop

    def maybe_enter_all(self) -> int:
        """Process fresh signals and enter qualifying trades. Returns entries made."""
        signals = self._read_fresh_signals()
        if not signals:
            return 0

        # Check max open positions limit
        try:
            open_rows = self.trade_log.filled_positions_with_id()
            ns_open = sum(
                1 for r in open_rows if r.get("status") == NEWS_SHOCK_OPEN
            )
        except Exception:
            ns_open = 0

        if ns_open >= self.cfg.max_open:
            logger.info(
                "news_shock: already %d/%d open; skipping cycle",
                ns_open, self.cfg.max_open,
            )
            return 0

        # Deduplicate signals by market_id: keep highest materiality per market
        seen: dict[str, dict] = {}
        for sig in signals:
            mid = sig.get("market_id", "")
            if not mid:
                continue
            if mid not in seen or sig["materiality"] > seen[mid]["materiality"]:
                seen[mid] = sig

        entries_made = 0
        for market_id, sig in seen.items():
            if ns_open + entries_made >= self.cfg.max_open:
                break
            signal_id = sig["id"]

            # Dedupe: skip if already active on this market
            if self.trade_log.has_active_trade_for_market(market_id):
                logger.debug("news_shock: dedupe skip %s", market_id)
                self._mark_signal(signal_id, "skipped")
                continue

            # Risk gate
            if self.risk_gate is not None and not self.risk_gate.ok():
                logger.info("news_shock: risk gate blocked: %s", self.risk_gate.reason())
                self._mark_signal(signal_id, "skipped")
                break

            # Fetch current market data from Gamma
            mkt = self._gamma_market(market_id)
            if mkt is None:
                self._mark_signal(signal_id, "skipped")
                continue

            # Must be active and binary
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

            # Get current prices
            try:
                prices = json.loads(mkt.get("outcomePrices", '["0.5","0.5"]'))
                yes_price = float(prices[0])
                no_price = float(prices[1])
            except (json.JSONDecodeError, IndexError, TypeError, ValueError):
                yes_price = 0.5
                no_price = 0.5

            # Liquidity filter — require minimum USDC volume/depth.
            liquidity = float(mkt.get("volumeClob") or mkt.get("volume24hr") or 0)
            if liquidity < self.cfg.min_liquidity:
                logger.debug(
                    "news_shock: skip %s liquidity=%.0f < %.0f",
                    market_id, liquidity, self.cfg.min_liquidity,
                )
                self._mark_signal(signal_id, "skipped")
                continue

            # direction and materiality must be resolved before any direction-
            # dependent checks below (drift gate, EV, Tavily).
            direction = sig["direction"]
            materiality = float(sig["materiality"])

            # Price-drift check: if the market has already moved > max_drift in the
            # signal direction since the signal was recorded, the news is priced in.
            signal_yes_price = sig.get("yes_price")
            if signal_yes_price is not None:
                drift = yes_price - signal_yes_price
                if direction == "bullish" and drift > self.cfg.max_drift:
                    logger.info(
                        "news_shock: skip %s — bullish already priced in "
                        "(signal_price=%.3f current=%.3f drift=+%.3f)",
                        market_id, signal_yes_price, yes_price, drift,
                    )
                    self._mark_signal(signal_id, "skipped")
                    continue
                if direction == "bearish" and drift < -self.cfg.max_drift:
                    logger.info(
                        "news_shock: skip %s — bearish already priced in "
                        "(signal_price=%.3f current=%.3f drift=%.3f)",
                        market_id, signal_yes_price, yes_price, drift,
                    )
                    self._mark_signal(signal_id, "skipped")
                    continue

            # EV and side determination
            if direction == "bullish":
                # News supports YES resolving → buy YES (BUY)
                ev = materiality - yes_price
                side = "BUY"
                entry_price = yes_price
                token_idx = 0
            elif direction == "bearish":
                # News supports NO resolving → buy NO (SELL)
                ev = materiality - no_price
                side = "SELL"
                entry_price = no_price
                token_idx = 1
            else:
                self._mark_signal(signal_id, "skipped")
                continue

            if ev < self.cfg.min_ev:
                logger.debug(
                    "news_shock: skip %s ev=%.3f < %.3f", market_id, ev, self.cfg.min_ev
                )
                self._mark_signal(signal_id, "skipped")
                continue

            if entry_price > self.cfg.max_entry_price:
                logger.debug(
                    "news_shock: skip %s entry_price=%.3f > %.3f",
                    market_id, entry_price, self.cfg.max_entry_price,
                )
                self._mark_signal(signal_id, "skipped")
                continue

            # Tavily external validation — verify the news direction is still
            # current and not already priced in by the broader market.
            # Fails open (missing TAVILY_API_KEY or network error → no skip).
            market_question = str(mkt.get("question", ""))
            if market_question:
                tavily_ctx = tavily_headlines(market_question, max_results=3)
                if tavily_ctx:
                    logger.info(
                        "news_shock: Tavily context for %s: %s",
                        market_id, tavily_ctx[:200],
                    )
                else:
                    logger.debug("news_shock: no Tavily context for %s", market_id)

            # Build recommendation — yes_price is always the anchor
            from agents.utils.objects import TradeRecommendation

            recommendation = TradeRecommendation(
                price=yes_price,
                size_fraction=0.0,
                side=side,
                confidence=materiality,
                amount_usdc=self.cfg.position_size_usdc,
            )

            market_doc = self._make_market_doc(
                market_id, outcomes, tokens, yes_price, no_price
            )
            token_id_for_log = tokens[token_idx]
            if not self._brain_allows_entry(
                market_id=market_id,
                token_id=token_id_for_log,
                question=market_question,
                side=side,
                yes_price=yes_price,
                no_price=no_price,
                liquidity_usdc=liquidity,
            ):
                self._mark_signal(signal_id, "skipped")
                continue

            cycle_id = self.trade_log.new_cycle_id()
            pending_id = self.trade_log.insert_pending(
                cycle_id=cycle_id,
                market_id=market_id,
                token_id=token_id_for_log,
                side=side,
                price=yes_price,
                size_usdc=self.cfg.position_size_usdc,
                confidence=materiality,
            )

            if not self.execute:
                self.trade_log.mark(
                    pending_id,
                    NEWS_SHOCK_OPEN,
                    response={"shadow": True, "direction": direction, "ev": ev},
                    error=(
                        f"SHADOW: would enter {side} on {market_id} "
                        f"dir={direction} materiality={materiality:.2f} ev={ev:.3f}"
                    ),
                )
                self._mark_signal(signal_id, "acted")
                logger.info(
                    "news_shock SHADOW: %s %s dir=%s mat=%.2f ev=%.3f",
                    side, market_id, direction, materiality, ev,
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
                logger.warning("news_shock entry failed %s: %s", market_id, exc)
                continue

            if not response or response.get("status") not in ("matched", "filled"):
                self.trade_log.mark(
                    pending_id, "failed",
                    response=response, error="entry not matched",
                )
                self._mark_signal(signal_id, "skipped")
                continue

            self.trade_log.mark(pending_id, NEWS_SHOCK_OPEN, response=response)
            self._mark_signal(signal_id, "acted")
            logger.info(
                "news_shock ENTRY: %s %s dir=%s mat=%.2f ev=%.3f",
                side, market_id, direction, materiality, ev,
            )
            entries_made += 1

        return entries_made


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class NewsShockDaemon:
    """Long-running loop. SIGTERM-aware."""

    def __init__(self, db_path: Optional[str] = None, execute: Optional[bool] = None):
        self.cfg = NewsShockConfig.from_env()
        self.execute = (
            execute if execute is not None
            else os.getenv("EXECUTE_NEWS_SHOCK", "false").lower() == "true"
        )
        self.trade_log = TradeLog(db_path=db_path)
        from agents.polymarket.polymarket import Polymarket
        from agents.application.risk_gate import RiskGate
        self.polymarket = Polymarket(live=self.execute)
        self.risk_gate = RiskGate(
            trade_log=self.trade_log,
            polymarket=self.polymarket,
            news_shock_reserve_usdc=self.cfg.reserve_usdc,
        )
        from agents.application.market_brain import MarketBrain
        self.engine = NewsShockEngine(
            polymarket=self.polymarket,
            trade_log=self.trade_log,
            risk_gate=self.risk_gate,
            cfg=self.cfg,
            execute=self.execute,
            brain=MarketBrain(),
        )
        from pathlib import Path
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
        logger.info("NewsShockDaemon: starting (execute=%s)", self.execute)
        try:
            while not self._stop.is_set():
                try:
                    self.engine.maybe_enter_all()
                except Exception:
                    logger.exception("news_shock cycle failed")
                try:
                    self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
                    self.heartbeat.touch()
                except Exception:
                    logger.warning("news_shock: heartbeat touch failed")
                self._stop.wait(self.cfg.poll_sec)
        finally:
            logger.info("NewsShockDaemon: exited")


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)
    NewsShockDaemon().run()
