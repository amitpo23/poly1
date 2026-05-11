"""Near-Resolution Agent — poly1 trading strategy.

Targets binary Polymarket markets that close within MIN_HOURS–MAX_HOURS
and where one outcome is priced ≤ MAX_ENTRY_PRICE (≤ ~15 cents). In
near-resolution windows, cheap tokens often snap to 0 or 1; we take the
cheap side only when a fast Tavily news search gives at least
MIN_TAVILY_CONFIDENCE confidence that the cheap side has real probability.

Position lifecycle:
  scan Gamma → price/time filter → Tavily confidence → RiskGate.ok()
  → dedupe check → execute_market_order → near_resolution_open row
  → exits owned by position_manager (TP / SL / max_hold_hours)

Storage: standard ``trades`` table, status ``near_resolution_open``.

Environment variables (all optional, see defaults below):
  NEAR_RESOLUTION_MIN_HOURS       — min hours until market close (default 0.5)
  NEAR_RESOLUTION_MAX_HOURS       — max hours until market close (default 36)
  NEAR_RESOLUTION_MAX_ENTRY_PRICE — max price for cheap side (default 0.15)
  NEAR_RESOLUTION_MIN_LIQUIDITY   — min $USDC book depth (default 3000)
  NEAR_RESOLUTION_MIN_CONFIDENCE  — Tavily threshold 0-1 (default 0.65)
  NEAR_RESOLUTION_POSITION_SIZE_USDC — size per trade (default 2.5)
  NEAR_RESOLUTION_RESERVE_USDC    — capital reserved for this agent (default 15)
  NEAR_RESOLUTION_POLL_SEC        — loop cadence in seconds (default 60)
  NEAR_RESOLUTION_MAX_OPEN        — max concurrent open positions (default 3)
  NEAR_RESOLUTION_HEARTBEAT_PATH  — file path for heartbeat (default /app/data/near_resolution_heartbeat)
  EXECUTE_NEAR_RESOLUTION         — set "true" to live-trade (default false)
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
from datetime import datetime, timezone
from typing import Optional

from agents.application.trade_log import NEAR_RESOLUTION_OPEN, TradeLog

logger = logging.getLogger(__name__)

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
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
class NearResolutionConfig:
    min_hours: float = 0.5
    max_hours: float = 36.0
    max_entry_price: float = 0.15
    min_liquidity: float = 3000.0
    min_confidence: float = 0.65
    position_size_usdc: float = 2.5
    reserve_usdc: float = 15.0
    poll_sec: int = 60
    max_open: int = 3
    heartbeat_path: str = "/app/data/near_resolution_heartbeat"

    @classmethod
    def from_env(cls) -> "NearResolutionConfig":
        return cls(
            min_hours=_env_float("NEAR_RESOLUTION_MIN_HOURS", 0.5),
            max_hours=_env_float("NEAR_RESOLUTION_MAX_HOURS", 36.0),
            max_entry_price=_env_float("NEAR_RESOLUTION_MAX_ENTRY_PRICE", 0.15),
            min_liquidity=_env_float("NEAR_RESOLUTION_MIN_LIQUIDITY", 3000.0),
            min_confidence=_env_float("NEAR_RESOLUTION_MIN_CONFIDENCE", 0.65),
            position_size_usdc=_env_float("NEAR_RESOLUTION_POSITION_SIZE_USDC", 2.5),
            reserve_usdc=_env_float("NEAR_RESOLUTION_RESERVE_USDC", 15.0),
            poll_sec=_env_int("NEAR_RESOLUTION_POLL_SEC", 60),
            max_open=_env_int("NEAR_RESOLUTION_MAX_OPEN", 3),
            heartbeat_path=os.getenv(
                "NEAR_RESOLUTION_HEARTBEAT_PATH", "/app/data/near_resolution_heartbeat"
            ),
        )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class NearResolutionEngine:
    def __init__(
        self,
        polymarket,
        trade_log: TradeLog,
        risk_gate,
        cfg: NearResolutionConfig,
        execute: bool = False,
    ):
        self.polymarket = polymarket
        self.trade_log = trade_log
        self.risk_gate = risk_gate
        self.cfg = cfg
        self.execute = execute

    # ------------------------------------------------------------------ scan

    def scan_candidates(self) -> list[dict]:
        """Fetch open binary markets from Gamma and filter by time + price + liquidity."""
        try:
            params = urllib.parse.urlencode({
                "closed": "false",
                "active": "true",
                "order": "end_date_asc",
                "ascending": "true",
                "limit": 200,
            })
            url = f"{GAMMA_MARKETS_URL}?{params}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "poly1-near-resolution/1.0"}
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                markets = json.loads(resp.read())
        except Exception as exc:
            logger.warning("near_resolution: Gamma scan failed: %s", exc)
            return []

        now_utc = datetime.now(timezone.utc)
        candidates = []
        for m in markets:
            # Must be binary (exactly 2 outcomes)
            try:
                outcomes = json.loads(m.get("outcomes", "[]"))
                tokens = json.loads(m.get("clobTokenIds", "[]"))
            except (json.JSONDecodeError, TypeError):
                try:
                    import ast
                    outcomes = ast.literal_eval(m.get("outcomes", "[]"))
                    tokens = ast.literal_eval(m.get("clobTokenIds", "[]"))
                except Exception:
                    continue
            if len(outcomes) != 2 or len(tokens) != 2:
                continue

            # Must have a close time in [min_hours, max_hours]
            end_date_str = m.get("endDate") or m.get("end_date_iso") or ""
            if not end_date_str:
                continue
            try:
                # Gamma returns ISO 8601 strings; strip trailing 'Z' for fromisoformat
                end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                hours_left = (end_dt - now_utc).total_seconds() / 3600.0
            except (ValueError, TypeError):
                continue
            if not (self.cfg.min_hours <= hours_left <= self.cfg.max_hours):
                continue

            # Check pricing: YES price is outcomes[0] price
            try:
                prices = json.loads(m.get("outcomePrices", '["0.5","0.5"]'))
                yes_price = float(prices[0])
                no_price = float(prices[1])
            except (json.JSONDecodeError, IndexError, TypeError, ValueError):
                yes_price = 0.5
                no_price = 0.5

            # Identify which side is cheap
            if yes_price <= self.cfg.max_entry_price:
                cheap_side = "yes"
                cheap_price = yes_price
            elif no_price <= self.cfg.max_entry_price:
                cheap_side = "no"
                cheap_price = no_price
            else:
                continue

            # Liquidity filter — Gamma provides volumeClob or bestBid/bestAsk depth
            liquidity = float(m.get("volumeClob") or m.get("volume24hr") or 0)
            if liquidity < self.cfg.min_liquidity:
                continue

            candidates.append({
                "market_id": str(m.get("id", "")),
                "question": m.get("question", ""),
                "yes_price": yes_price,
                "no_price": no_price,
                "cheap_side": cheap_side,
                "cheap_price": cheap_price,
                "hours_left": hours_left,
                "end_dt": end_dt,
                "outcomes": outcomes,
                "tokens": tokens,
                "raw": m,
            })

        logger.info("near_resolution: %d candidates after filters", len(candidates))
        return candidates

    # ---------------------------------------------------------- Tavily check

    def _tavily_confidence(self, question: str, cheap_side: str) -> float:
        """Query Tavily for news about the market question.

        Returns a confidence score 0.0–1.0 that the cheap side has real
        probability. Heuristic: count results that do NOT strongly deny
        the cheap outcome. Returns 0.0 on API failure (conservative).
        """
        api_key = os.getenv("TAVILY_API_KEY", "").strip()
        if not api_key:
            logger.debug("near_resolution: TAVILY_API_KEY not set; skipping confidence")
            return 0.0

        # Build a search query that looks for evidence of the cheap side happening
        side_label = "Yes" if cheap_side == "yes" else "No"
        query = f"{question} {side_label} outcome"
        payload = json.dumps({
            "api_key": api_key,
            "query": query,
            "max_results": 5,
            "search_depth": "basic",
            "topic": "news",
        }).encode("utf-8")
        req = urllib.request.Request(
            TAVILY_SEARCH_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "poly1-near-resolution/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
        except Exception as exc:
            logger.warning("near_resolution: Tavily failed: %s", exc)
            return 0.0

        results = body.get("results") or []
        if not results:
            return 0.0

        # Heuristic: score = fraction of results that contain affirmative keywords
        # for the cheap side, as a proxy for news supporting it.
        affirm_keywords = {"yes", "possible", "likely", "could", "may", "chance", "will"}
        deny_keywords = {"no", "impossible", "never", "won't", "cannot", "ruled out", "zero"}

        affirm_count = 0
        deny_count = 0
        for r in results:
            text = ((r.get("title") or "") + " " + (r.get("content") or "")).lower()
            if any(k in text for k in affirm_keywords):
                affirm_count += 1
            if any(k in text for k in deny_keywords):
                deny_count += 1

        total = max(len(results), 1)
        # If the cheap side is "yes": we want affirm > deny
        # If the cheap side is "no": invert — we want deny > affirm (news denying YES)
        if cheap_side == "yes":
            raw_score = (affirm_count - deny_count + total) / (2 * total)
        else:
            raw_score = (deny_count - affirm_count + total) / (2 * total)

        score = max(0.0, min(1.0, raw_score))
        logger.debug(
            "near_resolution: Tavily confidence for '%s' side=%s: %.2f "
            "(affirm=%d deny=%d results=%d)",
            question[:60], cheap_side, score, affirm_count, deny_count, total,
        )
        return score

    # ----------------------------------------------------------- market doc

    @staticmethod
    def _make_market_doc(m: dict) -> dict:
        """Build the (doc, score) tuple expected by execute_market_order."""

        class _Doc:
            pass

        doc = _Doc()
        doc.dict = lambda: {  # noqa: E731
            "metadata": {
                "id": m["market_id"],
                "outcomes": str(m["outcomes"]),
                "clob_token_ids": str(m["tokens"]),
                "outcome_prices": str([str(m["yes_price"]), str(m["no_price"])]),
            }
        }
        return {"doc": doc, "market_id": m["market_id"]}

    # ------------------------------------------------------------- main loop

    def maybe_enter_all(self) -> int:
        """Scan candidates and enter qualifying trades. Returns entries made."""
        candidates = self.scan_candidates()
        if not candidates:
            return 0

        # Check max open positions limit
        try:
            open_rows = self.trade_log.filled_positions_with_id()
            nr_open = sum(
                1 for r in open_rows if r.get("status") == NEAR_RESOLUTION_OPEN
            )
        except Exception:
            nr_open = 0

        if nr_open >= self.cfg.max_open:
            logger.info(
                "near_resolution: already %d/%d open; skipping scan",
                nr_open, self.cfg.max_open,
            )
            return 0

        entries_made = 0
        for candidate in candidates:
            if nr_open + entries_made >= self.cfg.max_open:
                break

            market_id = candidate["market_id"]

            # Dedupe: skip if already active on this market
            if self.trade_log.has_active_trade_for_market(market_id):
                logger.debug("near_resolution: dedupe skip %s", market_id)
                continue

            # Risk gate
            if self.risk_gate is not None and not self.risk_gate.ok():
                logger.info("near_resolution: risk gate blocked: %s", self.risk_gate.reason())
                break

            # Tavily confidence
            confidence = self._tavily_confidence(
                candidate["question"], candidate["cheap_side"]
            )
            if confidence < self.cfg.min_confidence:
                logger.info(
                    "near_resolution: skip %s — confidence %.2f < %.2f",
                    market_id, confidence, self.cfg.min_confidence,
                )
                continue

            # In our convention: BUY → YES (token_ids[0]); SELL → NO (token_ids[1])
            # We always use yes_price as the anchor price for TradeRecommendation.
            cheap_side = candidate["cheap_side"]
            side = "BUY" if cheap_side == "yes" else "SELL"
            yes_price = candidate["yes_price"]

            from agents.utils.objects import TradeRecommendation

            recommendation = TradeRecommendation(
                price=yes_price,
                size_fraction=0.0,
                side=side,
                confidence=confidence,
                amount_usdc=self.cfg.position_size_usdc,
            )

            market_doc = self._make_market_doc(candidate)
            token_id_for_log = (
                candidate["tokens"][0] if side == "BUY"
                else candidate["tokens"][1]
            )

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
                    NEAR_RESOLUTION_OPEN,
                    response={"shadow": True, "side": side, "confidence": confidence},
                    error=(
                        f"SHADOW: would enter {side} on market {market_id} "
                        f"cheap_side={cheap_side} price={candidate['cheap_price']:.3f} "
                        f"hours_left={candidate['hours_left']:.1f} confidence={confidence:.2f}"
                    ),
                )
                logger.info(
                    "near_resolution SHADOW: %s %s cheap=%.3f hours=%.1f conf=%.2f",
                    side, market_id, candidate["cheap_price"],
                    candidate["hours_left"], confidence,
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
                logger.warning("near_resolution entry failed %s: %s", market_id, exc)
                continue

            if not response or response.get("status") not in ("matched", "filled"):
                self.trade_log.mark(
                    pending_id, "failed",
                    response=response, error="entry not matched",
                )
                continue

            self.trade_log.mark(pending_id, NEAR_RESOLUTION_OPEN, response=response)
            logger.info(
                "near_resolution ENTRY: %s %s cheap=%.3f hours=%.1f conf=%.2f",
                side, market_id, candidate["cheap_price"],
                candidate["hours_left"], confidence,
            )
            entries_made += 1

        return entries_made


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class NearResolutionDaemon:
    """Long-running loop. SIGTERM-aware."""

    def __init__(self, db_path: Optional[str] = None, execute: Optional[bool] = None):
        self.cfg = NearResolutionConfig.from_env()
        self.execute = (
            execute if execute is not None
            else os.getenv("EXECUTE_NEAR_RESOLUTION", "false").lower() == "true"
        )
        self.trade_log = TradeLog(db_path=db_path)
        from agents.polymarket.polymarket import Polymarket
        from agents.application.risk_gate import RiskGate
        self.polymarket = Polymarket(live=self.execute)
        self.risk_gate = RiskGate(
            trade_log=self.trade_log,
            polymarket=self.polymarket,
            near_resolution_reserve_usdc=self.cfg.reserve_usdc,
        )
        self.engine = NearResolutionEngine(
            polymarket=self.polymarket,
            trade_log=self.trade_log,
            risk_gate=self.risk_gate,
            cfg=self.cfg,
            execute=self.execute,
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
        logger.info("NearResolutionDaemon: starting (execute=%s)", self.execute)
        try:
            while not self._stop.is_set():
                try:
                    self.engine.maybe_enter_all()
                except Exception:
                    logger.exception("near_resolution cycle failed")
                try:
                    self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
                    self.heartbeat.touch()
                except Exception:
                    logger.warning("near_resolution: heartbeat touch failed")
                self._stop.wait(self.cfg.poll_sec)
        finally:
            logger.info("NearResolutionDaemon: exited")


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)
    NearResolutionDaemon().run()
