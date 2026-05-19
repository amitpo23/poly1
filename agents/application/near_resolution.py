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
  NEAR_RESOLUTION_MAX_HOURS       — max hours until market close (default 36; set 336 in .env since active markets close 280h+ away)
  NEAR_RESOLUTION_MAX_ENTRY_PRICE — max price for cheap side (default 0.15)
  NEAR_RESOLUTION_MIN_LIQUIDITY   — min $USDC book depth (default 3000)
  NEAR_RESOLUTION_MIN_CONFIDENCE  — Tavily threshold 0-1 (default 0.52)
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
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
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
    # Time window — wide: all active markets, not just near-expiry
    min_hours: float = 0.0
    max_hours: float = 720.0
    # Single-side entry: enter only if token price is in this range
    # (too cheap = near-resolved loser; too expensive = little upside)
    max_entry_price: float = 0.65
    min_entry_price: float = 0.10
    # Straddle: enter BOTH YES and NO when market is genuinely uncertain
    # and the price sum is low enough to guarantee mathematical edge.
    straddle_max_sum: float = 0.88    # YES_ask + NO_ask < this to straddle
    straddle_min_each: float = 0.30   # each leg must be ≥ this
    direction_min_confidence: float = 0.52  # LLM confidence < this → try straddle
    # Backward-compatible alias for older callers/tests and env naming.
    min_confidence: Optional[float] = field(default=None, repr=False)
    # Limit how many markets we run LLM on per cycle (API cost control)
    max_candidates_per_cycle: int = 8
    min_liquidity: float = 3000.0
    position_size_usdc: float = 2.5
    reserve_usdc: float = 15.0
    poll_sec: int = 60
    # Raised to 6: a straddle uses 2 slots, allow up to 3 simultaneous straddles
    max_open: int = 6
    heartbeat_path: str = "/app/data/near_resolution_heartbeat"

    def __post_init__(self) -> None:
        if self.min_confidence is not None:
            self.direction_min_confidence = float(self.min_confidence)

    @classmethod
    def from_env(cls) -> "NearResolutionConfig":
        direction_min_confidence = _env_float(
            "NEAR_RESOLUTION_DIRECTION_MIN_CONFIDENCE",
            _env_float("NEAR_RESOLUTION_MIN_CONFIDENCE", 0.52),
        )
        return cls(
            min_hours=_env_float("NEAR_RESOLUTION_MIN_HOURS", 0.0),
            max_hours=_env_float("NEAR_RESOLUTION_MAX_HOURS", 720.0),
            max_entry_price=_env_float("NEAR_RESOLUTION_MAX_ENTRY_PRICE", 0.65),
            min_entry_price=_env_float("NEAR_RESOLUTION_MIN_ENTRY_PRICE", 0.10),
            straddle_max_sum=_env_float("NEAR_RESOLUTION_STRADDLE_MAX_SUM", 0.88),
            straddle_min_each=_env_float("NEAR_RESOLUTION_STRADDLE_MIN_EACH", 0.30),
            direction_min_confidence=direction_min_confidence,
            max_candidates_per_cycle=_env_int(
                "NEAR_RESOLUTION_MAX_CANDIDATES_PER_CYCLE", 8
            ),
            min_liquidity=_env_float("NEAR_RESOLUTION_MIN_LIQUIDITY", 3000.0),
            position_size_usdc=_env_float("NEAR_RESOLUTION_POSITION_SIZE_USDC", 2.5),
            reserve_usdc=_env_float("NEAR_RESOLUTION_RESERVE_USDC", 15.0),
            poll_sec=_env_int("NEAR_RESOLUTION_POLL_SEC", 60),
            max_open=_env_int("NEAR_RESOLUTION_MAX_OPEN", 6),
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
        # Lazy-init LLM (same pattern as position_manager)
        self._llm = None
        self._prompter = None
        # LLM result cache: question → (result_dict, unix_ts)
        # Prevents re-analysing the same market every 60 s (5-min TTL).
        self._llm_cache: dict = {}
        try:
            from agents.application.market_brain import MarketBrain
            from agents.application.meta_brain import MetaBrain
            self.meta_brain = MetaBrain(
                db_path=os.getenv("TRADE_LOG_DB", "./data/trade_log.db"),
                market_brain=MarketBrain(),
            )
        except Exception:
            logger.exception("near_resolution: MetaBrain init failed")
            self.meta_brain = None

    # ------------------------------------------------------------------ scan

    def scan_candidates(self) -> list[dict]:
        """Fetch open binary markets from Gamma and filter by time + liquidity.

        Returns markets that are either:
        - Straddle-viable: YES+NO < straddle_max_sum AND both >= straddle_min_each
        - Single-side potential: one token is in [min_entry_price, max_entry_price]
        """
        try:
            params = urllib.parse.urlencode({
                "closed": "false",
                "active": "true",
                "order": "volume24hr",
                "ascending": "false",
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

            # Time filter
            end_date_str = m.get("endDate") or m.get("end_date_iso") or ""
            hours_left: Optional[float] = None
            end_dt: Optional[datetime] = None
            if end_date_str:
                try:
                    end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    hours_left = (end_dt - now_utc).total_seconds() / 3600.0
                    if not (self.cfg.min_hours <= hours_left <= self.cfg.max_hours):
                        continue
                except (ValueError, TypeError):
                    continue

            # Prices
            try:
                prices = json.loads(m.get("outcomePrices", '["0.5","0.5"]'))
                yes_price = float(prices[0])
                no_price = float(prices[1])
            except (json.JSONDecodeError, IndexError, TypeError, ValueError):
                yes_price = 0.5
                no_price = 0.5

            # Liquidity filter
            liquidity = float(m.get("volumeClob") or m.get("volume24hr") or 0)
            if liquidity < self.cfg.min_liquidity:
                continue

            price_sum = yes_price + no_price

            # Determine entry mode:
            # 1. Straddle-viable: both sides in sweet spot and sum is low
            straddle_viable = (
                price_sum < self.cfg.straddle_max_sum
                and yes_price >= self.cfg.straddle_min_each
                and no_price >= self.cfg.straddle_min_each
            )
            # 2. Single-side: at least one token is in the tradeable range
            yes_tradeable = self.cfg.min_entry_price <= yes_price <= self.cfg.max_entry_price
            no_tradeable = self.cfg.min_entry_price <= no_price <= self.cfg.max_entry_price
            single_viable = yes_tradeable or no_tradeable

            if not straddle_viable and not single_viable:
                continue

            cheap_side = "yes" if yes_price <= no_price else "no"
            cheap_price = yes_price if cheap_side == "yes" else no_price

            candidates.append({
                "market_id": str(m.get("id", "")),
                "question": m.get("question", ""),
                "yes_price": yes_price,
                "no_price": no_price,
                "cheap_side": cheap_side,
                "cheap_price": cheap_price,
                "price_sum": price_sum,
                "straddle_viable": straddle_viable,
                "single_viable": single_viable,
                "hours_left": hours_left,
                "end_dt": end_dt,
                "end_date_str": end_date_str,
                "outcomes": outcomes,
                "tokens": tokens,
                "raw": m,
            })

        # Sort: straddle-viable first (best mathematical edge), then by liquidity
        candidates.sort(key=lambda c: (0 if c["straddle_viable"] else 1, -float(c["raw"].get("volumeClob") or c["raw"].get("volume24hr") or 0)))
        logger.info(
            "near_resolution: %d candidates (straddle=%d single=%d)",
            len(candidates),
            sum(1 for c in candidates if c["straddle_viable"]),
            sum(1 for c in candidates if not c["straddle_viable"] and c["single_viable"]),
        )
        return candidates

    # ------------------------------------------------- scanner signal boost

    def _scanner_market_ids(self, max_age_hours: float = 1.0) -> set:
        """Return market IDs flagged by market_scanner in the last max_age_hours.

        The market_scanner writes news_signals with status='scanner_near_resolution'
        when it detects a near-resolution opportunity.  We use these to re-order
        scan_candidates() so scanner-flagged markets are evaluated first.
        """
        try:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
            ).isoformat()
            with self.trade_log._lock, self.trade_log._connect() as conn:
                rows = conn.execute(
                    "SELECT DISTINCT market_id FROM news_signals "
                    "WHERE status = 'scanner_near_resolution' AND ts >= ?",
                    (cutoff,),
                ).fetchall()
            return {r["market_id"] for r in rows}
        except Exception as exc:
            logger.debug("near_resolution: scanner_market_ids failed: %s", exc)
            return set()

    # ---------------------------------------------------------- LLM direction

    def _get_news_context(self, question: str) -> str:
        """Fetch Tavily news for the market question. Returns formatted string or ""."""
        api_key = os.getenv("TAVILY_API_KEY", "").strip()
        if not api_key:
            return ""
        payload = json.dumps({
            "api_key": api_key,
            "query": question,
            "max_results": 4,
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
            results = body.get("results") or []
            if not results:
                return ""
            lines = []
            for r in results[:4]:
                title = (r.get("title") or "").strip()
                if title:
                    lines.append(f"- {title}")
            return "\n".join(lines)
        except Exception as exc:
            logger.debug("near_resolution: Tavily failed: %s", exc)
            return ""

    def _init_llm(self):
        """Lazy-init LangChain LLM (same pattern as position_manager)."""
        if self._llm is not None:
            return
        try:
            from langchain_openai import ChatOpenAI
            from agents.application.prompts import Prompter
            from agents.application.llm_config import openai_model
            model = openai_model()
            self._llm = ChatOpenAI(
                model=model,
                temperature=0,
                model_kwargs={"response_format": {"type": "json_object"}},
            )
            self._prompter = Prompter()
            logger.debug("near_resolution: LLM initialised (%s)", model)
        except Exception as exc:
            logger.warning("near_resolution: LLM init failed: %s", exc)
            self._llm = None

    def _llm_direction(
        self, question: str, yes_price: float, no_price: float, end_date_str: str
    ) -> dict:
        """Ask LLM for market direction.

        Returns {"direction": "yes"|"no"|"uncertain", "confidence": float,
                 "reasoning": str}.
        Falls back to {"direction": "uncertain", "confidence": 0.5} on any error.
        """
        fallback = {"direction": "uncertain", "confidence": 0.5, "reasoning": "llm_unavailable"}
        # Cache check — skip LLM if we analysed this market recently.
        _now_ts = time.time()
        cached = self._llm_cache.get(question)
        if cached is not None:
            _cached_result, _cached_ts = cached
            if _now_ts - _cached_ts < 300:  # 5-minute TTL
                logger.debug("near_resolution: LLM cache hit for '%s'", question[:60])
                return _cached_result
        self._init_llm()
        if self._llm is None:
            return fallback

        news_context = self._get_news_context(question)

        try:
            prompt_text = self._prompter.binary_market_direction(
                question=question,
                yes_price=yes_price,
                no_price=no_price,
                end_date=end_date_str,
                news_context=news_context,
            )
            from langchain_core.messages import HumanMessage
            raw = None
            try:
                response = self._llm.invoke([HumanMessage(content=prompt_text)])
                raw = response.content if hasattr(response, "content") else str(response)
            except Exception as llm_exc:
                _is_quota = (
                    "insufficient_quota" in str(llm_exc)
                    or "exceeded your current quota" in str(llm_exc)
                    or "RateLimitError" in type(llm_exc).__name__
                )
                anthropic_key = os.getenv("ANTHROPIC_API_KEY")
                if _is_quota and anthropic_key:
                    try:
                        import anthropic as _anthropic_sdk
                        from agents.application.llm_config import anthropic_model
                        _model = anthropic_model()
                        logger.warning(
                            "near_resolution: OpenAI quota exhausted — falling back to %s",
                            _model,
                        )
                        _client = _anthropic_sdk.Anthropic(api_key=anthropic_key)
                        _resp = _client.messages.create(
                            model=_model,
                            max_tokens=1024,
                            messages=[{"role": "user", "content": prompt_text}],
                        )
                        raw = _resp.content[0].text
                    except Exception as anth_exc:
                        logger.warning("near_resolution: Anthropic fallback failed: %s", anth_exc)
                        return fallback
                else:
                    raise llm_exc
            import re as _re
            # Try direct JSON parse first; fall back to extracting JSON object
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                _m = _re.search(r"\{[^{}]*\}", raw or "", _re.DOTALL)
                if _m:
                    data = json.loads(_m.group())
                else:
                    logger.warning("near_resolution: no JSON in LLM response for '%s'", question[:60])
                    return fallback
            direction = str(data.get("direction", "uncertain")).lower()
            if direction not in ("yes", "no", "uncertain"):
                direction = "uncertain"
            confidence = float(data.get("confidence", 0.5))
            reasoning = str(data.get("reasoning", ""))
            logger.info(
                "near_resolution: LLM direction=%s conf=%.2f for '%s'",
                direction, confidence, question[:60],
            )
            result = {"direction": direction, "confidence": confidence, "reasoning": reasoning}
            self._llm_cache[question] = (result, time.time())
            return result
        except Exception as exc:
            logger.warning("near_resolution: LLM direction failed for '%s': %s", question[:60], exc)
            return fallback

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

    # ----------------------------------------------------- single-leg entry

    def _enter_one_leg(
        self,
        candidate: dict,
        side: str,
        price: float,
        confidence: float,
        extra_meta: Optional[dict] = None,
    ) -> bool:
        """Place a single-leg entry. Returns True on success (or shadow)."""
        from agents.utils.objects import TradeRecommendation

        market_id = candidate["market_id"]
        tokens = candidate["tokens"]
        token_id_for_log = tokens[0] if side == "BUY" else tokens[1]
        liquidity = float(
            candidate["raw"].get("liquidityClob")
            or candidate["raw"].get("liquidity")
            or candidate["raw"].get("volumeClob")
            or candidate["raw"].get("volume24hr")
            or 0
        )

        if self.meta_brain is None:
            if self.execute:
                logger.warning("near_resolution live blocked — missing MetaBrain")
                return False
        else:
            meta = self.meta_brain.synthesize(
                market_id=market_id,
                question=str(candidate.get("question") or ""),
                spread_pct=abs(candidate["yes_price"] - (1.0 - candidate["no_price"])),
                hours_to_close=candidate.get("hours_left"),
                poly_prob=price,
                token_id=token_id_for_log,
                liquidity_usdc=liquidity,
            )
            self.trade_log.insert_brain_decision(
                agent="near_resolution",
                strategy="near_resolution",
                decision_type="entry",
                market_id=market_id,
                token_id=token_id_for_log,
                approved=meta.approved,
                reason=meta.reason,
                score=meta.score,
                market_type="general_binary",
                features=meta.features,
                action=side,
            )
            if not meta.approved:
                logger.info(
                    "near_resolution meta_brain rejected %s: %s score=%.3f timing=%s",
                    market_id, meta.reason, meta.score, meta.entry_timing,
                )
                return False

        recommendation = TradeRecommendation(
            price=price,
            size_fraction=0.0,
            side=side,
            confidence=confidence,
            amount_usdc=self.cfg.position_size_usdc,
        )
        market_doc = self._make_market_doc(candidate)

        # Encode straddle metadata into response_json so position_manager can see it
        response_meta = extra_meta or {}

        cycle_id = self.trade_log.new_cycle_id()
        pending_id = self.trade_log.insert_pending(
            cycle_id=cycle_id,
            market_id=market_id,
            token_id=token_id_for_log,
            side=side,
            price=price,
            size_usdc=self.cfg.position_size_usdc,
            confidence=confidence,
        )

        if not self.execute:
            self.trade_log.mark(
                pending_id,
                NEAR_RESOLUTION_OPEN,
                response={**response_meta, "shadow": True, "side": side, "confidence": confidence},
                error=(
                    f"SHADOW: {side} market={market_id} price={price:.3f} "
                    f"conf={confidence:.2f} sum={candidate['price_sum']:.3f}"
                ),
            )
            return True

        try:
            response = self.polymarket.execute_market_order(
                (market_doc["doc"], 0.0), recommendation
            )
        except Exception as exc:
            self.trade_log.mark(pending_id, "failed", error=f"execute_market_order: {exc}")
            logger.warning("near_resolution entry failed %s %s: %s", side, market_id, exc)
            return False

        if not response or response.get("status") not in ("matched", "filled"):
            self.trade_log.mark(
                pending_id, "failed", response=response, error="entry not matched"
            )
            return False

        self.trade_log.mark(
            pending_id, NEAR_RESOLUTION_OPEN,
            response={**response_meta, **(response or {})},
        )
        return True

    # ------------------------------------------------------------- main loop

    def maybe_enter_all(self) -> int:
        """Scan candidates; enter single-side or straddle. Returns entries (legs) made."""
        candidates = self.scan_candidates()
        if not candidates:
            return 0

        # Boost markets the scanner pre-approved to front of the queue.
        scanner_ids = self._scanner_market_ids()
        if scanner_ids:
            logger.info(
                "near_resolution: scanner boost — %d flagged market(s): %s",
                len(scanner_ids),
                list(scanner_ids)[:5],
            )
            candidates.sort(
                key=lambda c: (
                    0 if c["market_id"] in scanner_ids else 1,
                    0 if c["straddle_viable"] else 1,
                    -float(c["raw"].get("volumeClob") or c["raw"].get("volume24hr") or 0),
                )
            )

        # Count open near_resolution legs
        try:
            open_rows = self.trade_log.filled_positions_with_id()
            nr_open = sum(1 for r in open_rows if r.get("status") == NEAR_RESOLUTION_OPEN)
        except Exception:
            nr_open = 0

        if nr_open >= self.cfg.max_open:
            logger.info(
                "near_resolution: already %d/%d open; skipping scan", nr_open, self.cfg.max_open
            )
            return 0

        entries_made = 0
        evaluated = 0

        for candidate in candidates:
            slots_remaining = self.cfg.max_open - (nr_open + entries_made)
            if slots_remaining <= 0:
                break
            if evaluated >= self.cfg.max_candidates_per_cycle:
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

            evaluated += 1
            yes_price = candidate["yes_price"]
            no_price = candidate["no_price"]
            question = candidate["question"]
            end_date_str = candidate.get("end_date_str", "")

            # ---- LLM direction analysis ----
            llm_result = self._llm_direction(question, yes_price, no_price, end_date_str)
            direction = llm_result["direction"]
            confidence = llm_result["confidence"]

            # ---- Decision: single-side or straddle ----
            if direction in ("yes", "no") and confidence >= self.cfg.direction_min_confidence:
                # Clear directional conviction → single-side entry
                if direction == "yes":
                    if not (self.cfg.min_entry_price <= yes_price <= self.cfg.max_entry_price):
                        logger.info(
                            "near_resolution: skip YES %s — price %.3f out of range",
                            market_id, yes_price,
                        )
                        continue
                    ok = self._enter_one_leg(candidate, "BUY", yes_price, confidence,
                                             extra_meta={"entry_mode": "single_yes", "reasoning": llm_result.get("reasoning", "")})
                else:
                    if not (self.cfg.min_entry_price <= no_price <= self.cfg.max_entry_price):
                        logger.info(
                            "near_resolution: skip NO %s — price %.3f out of range",
                            market_id, no_price,
                        )
                        continue
                    ok = self._enter_one_leg(candidate, "SELL", yes_price, confidence,
                                             extra_meta={"entry_mode": "single_no", "reasoning": llm_result.get("reasoning", "")})
                if ok:
                    entries_made += 1
                    logger.info(
                        "near_resolution SINGLE %s: %s price=%.3f conf=%.2f reason='%s'",
                        direction.upper(), market_id, yes_price if direction == "yes" else no_price,
                        confidence, llm_result.get("reasoning", "")[:60],
                    )

            elif candidate["straddle_viable"] and slots_remaining >= 2:
                # Uncertain direction + math edge → STRADDLE both sides
                import uuid as _uuid
                straddle_id = str(_uuid.uuid4())[:8]
                meta_yes = {"entry_mode": "straddle_yes", "straddle_id": straddle_id,
                            "reasoning": llm_result.get("reasoning", "")}
                meta_no = {"entry_mode": "straddle_no", "straddle_id": straddle_id,
                           "reasoning": llm_result.get("reasoning", "")}

                ok_yes = self._enter_one_leg(candidate, "BUY", yes_price, confidence,
                                             extra_meta=meta_yes)
                ok_no = self._enter_one_leg(candidate, "SELL", yes_price, confidence,
                                            extra_meta=meta_no)
                legs = (1 if ok_yes else 0) + (1 if ok_no else 0)
                entries_made += legs
                logger.info(
                    "near_resolution STRADDLE %s: YES=%.3f NO=%.3f sum=%.3f legs=%d id=%s",
                    market_id, yes_price, no_price, candidate["price_sum"], legs, straddle_id,
                )

            else:
                logger.debug(
                    "near_resolution: skip %s — direction=%s conf=%.2f straddle=%s slots=%d",
                    market_id, direction, confidence, candidate["straddle_viable"], slots_remaining,
                )

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
