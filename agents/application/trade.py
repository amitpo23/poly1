import ast
import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from agents.application.executor import Executor as Agent
from agents.application.market_brain import MarketBrain
from agents.application.risk_gate import RiskGate
from agents.application.tavily import tavily_headlines
from agents.utils.notify import notify_trade, _safe_balance
from agents.application.trade_log import (
    FAILED,
    FILLED,
    MAY_HAVE_FIRED,
    SKIPPED_DEDUPE,
    SKIPPED_DRY_RUN,
    SKIPPED_GATE,
    SUBMITTED,
    TradeLog,
)
from agents.application.execution_safety import exitable_size_check
from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.polymarket.polymarket import Polymarket


logger = logging.getLogger(__name__)


def _is_ai_quota_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "insufficient_quota" in text or "exceeded your current quota" in text


class Trader:
    def __init__(
        self,
        dry_run: bool = True,
        max_retries: int = 3,
        retry_delay_seconds: int = 5,
        max_position_fraction: float = 0.10,
        min_confidence: float = 0.0,
        top_n: int = 3,
        max_trades_per_cycle: int = 2,
        refresh_dbs_hours: int = 24,
        force_refresh_dbs: bool = False,
        trade_log: Optional[TradeLog] = None,
        risk_gate: Optional[RiskGate] = None,
    ):
        if not 0 < max_position_fraction <= 1:
            raise ValueError("max_position_fraction must be greater than 0 and at most 1.")
        if not 0 <= min_confidence <= 1:
            raise ValueError("min_confidence must be between 0 and 1.")
        if top_n < 1:
            raise ValueError("top_n must be >= 1.")
        if max_trades_per_cycle < 1:
            raise ValueError("max_trades_per_cycle must be >= 1.")

        self.dry_run = dry_run
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.max_position_fraction = max_position_fraction
        self.min_confidence = min_confidence
        self.top_n = top_n
        self.max_trades_per_cycle = max_trades_per_cycle
        self.refresh_dbs_interval = timedelta(hours=refresh_dbs_hours)
        self.force_refresh_dbs = force_refresh_dbs
        self.last_refresh: Optional[datetime] = None

        self.polymarket = Polymarket(live=not dry_run)
        self.gamma = Gamma()
        self.agent = Agent()
        self.trade_log = trade_log or TradeLog()
        self.risk_gate = risk_gate or RiskGate(
            trade_log=self.trade_log, polymarket=self.polymarket
        )
        self.brain = MarketBrain()
        # MetaBrain: single synthesizing layer (wraps brain + win-rate + velocity + conviction).
        try:
            from agents.application.meta_brain import MetaBrain
            self.meta_brain = MetaBrain(
                db_path=os.getenv("TRADE_LOG_PATH", "./data/poly1.db"),
                market_brain=self.brain,
            )
        except Exception:
            self.meta_brain = None
        self.shadow_ignore_risk_gate = (
            os.getenv("SHADOW_IGNORE_RISK_GATE", "false").lower() == "true"
        )
        self.broken_market_failure_threshold = int(
            os.getenv("TRADER_BROKEN_MARKET_FAILURE_THRESHOLD", "3")
        )
        self.broken_market_window_hours = int(
            os.getenv("TRADER_BROKEN_MARKET_WINDOW_HOURS", "6")
        )

    def pre_trade_logic(self) -> None:
        if self.force_refresh_dbs:
            logger.info("pre_trade_logic: forced Chroma refresh")
            self.clear_local_dbs()
            self.last_refresh = datetime.now(timezone.utc)
            return
        if self.last_refresh is None or (
            datetime.now(timezone.utc) - self.last_refresh > self.refresh_dbs_interval
        ):
            logger.info("pre_trade_logic: refreshing Chroma DBs")
            self.clear_local_dbs()
            self.last_refresh = datetime.now(timezone.utc)

    def clear_local_dbs(self) -> None:
        for d in ("local_db_events", "local_db_markets"):
            try:
                shutil.rmtree(d)
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.warning("clear_local_dbs: failed to remove %s: %s", d, e)

    def one_best_trade(self) -> None:
        """Backward-compatible single-cycle entrypoint."""
        for attempt in range(1, self.max_retries + 1):
            try:
                return self.one_best_trade_sweep()
            except Exception:
                logger.exception(
                    "trade cycle attempt %s/%s failed", attempt, self.max_retries
                )
                if attempt == self.max_retries:
                    raise
                time.sleep(self.retry_delay_seconds)

    def one_best_trade_sweep(self) -> None:
        cycle_id = self.trade_log.new_cycle_id()
        logger.info("cycle %s: starting sweep (dry_run=%s)", cycle_id, self.dry_run)

        if not self.risk_gate.ok():
            if self.dry_run and self.shadow_ignore_risk_gate:
                logger.warning(
                    "cycle %s: risk gate blocked but shadow evaluation continues: %s",
                    cycle_id,
                    self.risk_gate.reason(),
                )
            else:
                logger.warning("cycle %s: risk gate blocked entry", cycle_id)
                return
        self.pre_trade_logic()

        events = self.polymarket.get_all_tradeable_events()
        logger.info("cycle %s: %d tradeable events", cycle_id, len(events))

        try:
            filtered_events = self.agent.filter_events_with_rag(events)
        except Exception as exc:
            if _is_ai_quota_error(exc):
                logger.error(
                    "cycle %s: AI event filter unavailable due quota; skipping cycle",
                    cycle_id,
                )
                self.trade_log.insert_terminal(
                    cycle_id,
                    "__cycle__",
                    SKIPPED_GATE,
                    error=f"ai_filter_unavailable: {type(exc).__name__}: {exc}",
                )
                return
            raise
        logger.info("cycle %s: %d events after RAG filter", cycle_id, len(filtered_events))

        markets = self.agent.map_filtered_events_to_markets(filtered_events)
        logger.info("cycle %s: %d markets mapped", cycle_id, len(markets))

        try:
            filtered_markets = self.agent.filter_markets(markets)
        except Exception as exc:
            if _is_ai_quota_error(exc):
                logger.error(
                    "cycle %s: AI market filter unavailable due quota; skipping cycle",
                    cycle_id,
                )
                self.trade_log.insert_terminal(
                    cycle_id,
                    "__cycle__",
                    SKIPPED_GATE,
                    error=f"ai_filter_unavailable: {type(exc).__name__}: {exc}",
                )
                return
            raise
        logger.info(
            "cycle %s: %d markets after filter", cycle_id, len(filtered_markets)
        )

        ranked_all = self._rank_markets(filtered_markets)
        # Filter out markets we already hold BEFORE applying TOP_N cutoff,
        # so held positions don't consume evaluation slots.
        ranked = []
        for m in ranked_all:
            if len(ranked) >= self.top_n:
                break
            mid = self._market_id(m)
            if self.trade_log.has_filled_position_for_market(mid):
                continue
            ranked.append(m)
        logger.info("cycle %s: evaluating top %d ranked", cycle_id, len(ranked))

        placed = 0
        for market in ranked:
            if not self.risk_gate.ok() and not (
                self.dry_run and self.shadow_ignore_risk_gate
            ):
                logger.warning("cycle %s: risk gate flipped mid-sweep, stopping", cycle_id)
                break
            if self._evaluate_market(cycle_id, market):
                placed += 1
            if placed >= self.max_trades_per_cycle:
                logger.info(
                    "cycle %s: hit max_trades_per_cycle=%d",
                    cycle_id,
                    self.max_trades_per_cycle,
                )
                break

        logger.info("cycle %s: done, placed=%d", cycle_id, placed)

    def _rank_markets(self, filtered_markets) -> list:
        """Rank markets for evaluation.

        Sorting key (ascending = better):
        1. Chroma semantic score (lower distance = more relevant).
        2. Short-term bonus: subtract 0.05 per each full week remaining if
           the market resolves within 7 days — prefers quick-turnaround markets
           aligned with the psychological-bias exploitation strategy.
        3. Liquidity bonus: subtract 0.01 per $10k of liquidity (high-liquidity
           markets are easier to enter and exit cleanly).
        4. Wide spread (more market-making edge) as a final tiebreaker.
        """
        _now = datetime.now(timezone.utc)

        def key(item):
            doc = item[0].dict()
            metadata = doc.get("metadata", {})
            spread = metadata.get("spread")
            liquidity = metadata.get("liquidity")
            end_date_raw = metadata.get("end") or metadata.get("end_date") or ""
            try:
                spread_val = float(spread) if spread is not None else 0.0
            except (TypeError, ValueError):
                spread_val = 0.0
            try:
                liquidity_val = float(liquidity) if liquidity is not None else 0.0
            except (TypeError, ValueError):
                liquidity_val = 0.0
            chroma_score = float(item[1]) if len(item) > 1 else 0.0

            # Short-term bonus: prefer markets resolving in ≤7 days.
            short_term_bonus = 0.0
            if end_date_raw:
                try:
                    end_dt = datetime.fromisoformat(
                        str(end_date_raw).replace("Z", "+00:00")
                    )
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    days_left = (end_dt - _now).total_seconds() / 86400.0
                    if 0 < days_left <= 7:
                        # Stronger bonus for nearer resolution.
                        short_term_bonus = -0.10 * (1.0 - days_left / 7.0)
                except Exception:
                    pass

            # Liquidity bonus: up to -0.05 for markets with $50k+.
            liquidity_bonus = -min(0.05, liquidity_val / 1_000_000.0)

            return (
                chroma_score + short_term_bonus + liquidity_bonus,
                -spread_val,
            )

        return sorted(filtered_markets, key=key)

    def _market_id(self, market) -> str:
        metadata = market[0].dict().get("metadata", {})
        for k in ("id", "market_id", "questionID", "question_id"):
            if k in metadata and metadata[k] is not None:
                return str(metadata[k])
        return str(metadata.get("question", ""))[:64]

    def _evaluate_market(self, cycle_id: str, market) -> bool:
        market_id = self._market_id(market)

        hard_failures = self.trade_log.count_recent_failures_for_market(
            market_id,
            hours=self.broken_market_window_hours,
            error_like=[
                "%status_code=404%",
                "%No orderbook%",
                "%no asks available%",
                "%live ask price%",
            ],
        )
        if hard_failures >= self.broken_market_failure_threshold:
            self.trade_log.quarantine_market(
                market_id,
                reason=(
                    f"hard_execution_failures={hard_failures} "
                    f"in_{self.broken_market_window_hours}h"
                ),
            )
            logger.info(
                "cycle %s: market %s skipped (broken-market failure threshold: %d)",
                cycle_id,
                market_id,
                hard_failures,
            )
            self.trade_log.insert_terminal(
                cycle_id,
                market_id,
                SKIPPED_GATE,
                error=(
                    f"broken_market_blacklist: {hard_failures} hard execution "
                    f"failures in {self.broken_market_window_hours}h"
                ),
            )
            return False

        if self.trade_log.is_market_quarantined(market_id):
            logger.info("cycle %s: market %s skipped (quarantined)", cycle_id, market_id)
            self.trade_log.insert_terminal(
                cycle_id,
                market_id,
                SKIPPED_GATE,
                error="market_quarantined_recent_hard_failure",
            )
            return False

        # Extract primary token_id early so dedupe can cross-match agents
        # that use different market identifiers (numeric vs hex).
        _dedupe_token_id = None
        try:
            _meta = market[0].dict().get("metadata", {})
            _tids = ast.literal_eval(_meta["clob_token_ids"])
            if _tids:
                _dedupe_token_id = _tids[0]
        except Exception:
            pass

        # First gate: do we already hold a filled position on this market?
        # Without exit logic (`maintain_positions` is a stub), reopening is
        # "averaging down" — observed 2026-05-06 when the LLM doubled
        # exposure on 566187/566188 from 0.38 → 0.205. Block regardless
        # of dedupe age until exit logic exists to close positions
        # before reopening.
        if self.trade_log.has_filled_position_for_market(
            market_id, token_id=_dedupe_token_id,
        ):
            logger.info(
                "cycle %s: market %s skipped (already holds filled position)",
                cycle_id, market_id,
            )
            self.trade_log.insert_terminal(
                cycle_id, market_id, SKIPPED_DEDUPE,
                error="already holds filled position on this market",
            )
            return False

        if self.trade_log.has_active_trade_for_market(
            market_id, hours=6, token_id=_dedupe_token_id,
        ):
            logger.info(
                "cycle %s: market %s skipped (recent active trade)", cycle_id, market_id
            )
            self.trade_log.insert_terminal(
                cycle_id, market_id, SKIPPED_DEDUPE,
                error="active trade in last 6h",
            )
            return False

        # Fix 1: Post-close re-entry cooldown.
        reentry_cooldown_hours = int(os.getenv("REENTRY_COOLDOWN_HOURS", "12"))
        if self.trade_log.has_recent_close_for_market(
            market_id, hours=reentry_cooldown_hours, token_id=_dedupe_token_id,
        ):
            logger.info(
                "cycle %s: market %s skipped (re-entry cooldown %dh)",
                cycle_id, market_id, reentry_cooldown_hours,
            )
            self.trade_log.insert_terminal(
                cycle_id, market_id, SKIPPED_GATE,
                error=f"reentry_cooldown: closed within {reentry_cooldown_hours}h",
            )
            return False

        # Fix 2: Per-market concentration limit.
        max_fills_24h = int(os.getenv("MAX_FILLS_PER_MARKET_24H", "3"))
        recent_fills = self.trade_log.count_recent_fills_for_market(
            market_id, hours=24, token_id=_dedupe_token_id,
        )
        if recent_fills >= max_fills_24h:
            logger.info(
                "cycle %s: market %s skipped (concentration: %d fills >= max %d)",
                cycle_id, market_id, recent_fills, max_fills_24h,
            )
            self.trade_log.insert_terminal(
                cycle_id, market_id, SKIPPED_GATE,
                error=f"concentration_limit: {recent_fills} fills in 24h >= {max_fills_24h}",
            )
            return False

        # External conviction gate: if recent signals from external_conviction
        # agent disapprove this market, skip the LLM call entirely.
        if self._conviction_blocks_entry(cycle_id, market_id):
            return False

        # Pre-LLM brain gate: score market quality before spending LLM tokens.
        # Extracts spread_pct + hours_to_close from metadata; builds Tavily
        # context once here so the LLM and brain share the same news text.
        market_question = str(
            market[0].dict().get("metadata", {}).get("question", "")
        )
        news_context = self._build_news_context(market_id, question=market_question)
        if not self._brain_entry_gate(cycle_id, market_id, market, news_context):
            return False

        try:
            best_trade = self.agent.source_best_trade(market, news_context=news_context)
            recommendation = self.agent.parse_trade_recommendation(best_trade)
        except Exception as e:
            if _is_ai_quota_error(e):
                logger.error(
                    "cycle %s: market %s AI trade analysis unavailable due quota",
                    cycle_id,
                    market_id,
                )
                self.trade_log.insert_terminal(
                    cycle_id,
                    market_id,
                    SKIPPED_GATE,
                    error=f"ai_analysis_unavailable: {type(e).__name__}: {e}",
                )
                return False
            logger.exception("cycle %s: market %s LLM/parse failed", cycle_id, market_id)
            self.trade_log.insert_terminal(
                cycle_id, market_id, FAILED, error=f"llm_or_parse: {e}"
            )
            return False

        logger.info(
            "cycle %s: market %s recommendation side=%s price=%s size_fraction=%s confidence=%s",
            cycle_id,
            market_id,
            recommendation.side,
            recommendation.price,
            recommendation.size_fraction,
            recommendation.confidence,
        )

        # no_edge: superforecaster explicitly found no exploitable edge vs market price
        if recommendation.confidence == 0 and recommendation.size_fraction == 0:
            logger.info(
                "cycle %s: market %s skipped (superforecaster: no edge vs market price)",
                cycle_id, market_id,
            )
            self.trade_log.insert_terminal(
                cycle_id, market_id, SKIPPED_GATE,
                error="no_edge: LLM found no exploitable edge vs market price",
            )
            return False

        # If a min_confidence threshold is set, missing confidence (None) must
        # also fail the gate — otherwise a parser/LLM that omits the field
        # silently bypasses the check.
        confidence = recommendation.confidence
        if self.min_confidence > 0 and (
            confidence is None or confidence < self.min_confidence
        ):
            self.trade_log.insert_terminal(
                cycle_id, market_id, SKIPPED_GATE,
                token_id=None,
                side=recommendation.side,
                price=recommendation.price,
                size_usdc=None,
                confidence=confidence,
                error=(
                    f"confidence {confidence} < min {self.min_confidence}"
                    if confidence is not None
                    else f"confidence missing; min_confidence={self.min_confidence}"
                ),
            )
            return False

        size_fraction = min(recommendation.size_fraction, self.max_position_fraction)
        try:
            usdc_balance = self.risk_gate.available_for_trader()
        except Exception as e:
            logger.exception("cycle %s: balance read failed", cycle_id)
            self.trade_log.insert_terminal(
                cycle_id, market_id, FAILED, error=f"balance_read: {e}"
            )
            return False
        recommendation.amount_usdc = size_fraction * usdc_balance

        # Resolve which token_id will actually be traded so the log is accurate.
        token_id = None
        metadata = {}
        try:
            metadata = market[0].dict()["metadata"]
            token_ids = ast.literal_eval(metadata["clob_token_ids"])
            if len(token_ids) == 2:
                token_id = (
                    token_ids[0] if recommendation.side.upper() == "BUY" else token_ids[1]
                )
        except Exception:
            token_id = None

        safety = exitable_size_check(
            amount_usdc=float(recommendation.amount_usdc or 0.0),
            entry_price=float(recommendation.price or 0.0),
        )
        if not safety.ok:
            logger.info(
                "cycle %s: market %s skipped by exitable-size gate: %s",
                cycle_id,
                market_id,
                safety.reason,
            )
            self.trade_log.insert_terminal(
                cycle_id,
                market_id,
                SKIPPED_GATE,
                token_id=token_id,
                side=recommendation.side,
                price=recommendation.price,
                size_usdc=recommendation.amount_usdc,
                confidence=recommendation.confidence,
                error=safety.reason,
            )
            return False

        if (
            not self.dry_run
            and os.getenv("OPPORTUNITY_ROUTER_ENFORCE_LIVE", "true").lower()
            in {"1", "true", "yes", "on"}
        ):
            from agents.application.opportunity_router import live_route_allowed

            scout_db = os.getenv("SCOUT_DB", "./data/scout.db")
            slug = str(metadata.get("slug") or metadata.get("market_slug") or market_id)
            route = live_route_allowed(
                db_path=scout_db,
                market_slug=slug,
                strategy="trader",
            )
            if not route.allowed:
                self.trade_log.insert_terminal(
                    cycle_id,
                    market_id,
                    SKIPPED_GATE,
                    token_id=token_id,
                    side=recommendation.side,
                    price=recommendation.price,
                    size_usdc=recommendation.amount_usdc,
                    confidence=recommendation.confidence,
                    error=f"opportunity_router_block:{route.reason}",
                )
                return False

        if self.dry_run:
            logger.info(
                "cycle %s: market %s DRY_RUN side=%s price=%s amount=%.4f",
                cycle_id, market_id, recommendation.side, recommendation.price,
                recommendation.amount_usdc,
            )
            self.trade_log.insert_terminal(
                cycle_id, market_id, SKIPPED_DRY_RUN,
                token_id=token_id,
                side=recommendation.side,
                price=recommendation.price,
                size_usdc=recommendation.amount_usdc,
                confidence=recommendation.confidence,
            )
            return True

        trade_id = self.trade_log.insert_pending(
            cycle_id=cycle_id,
            market_id=market_id,
            token_id=token_id,
            side=recommendation.side,
            price=recommendation.price,
            size_usdc=recommendation.amount_usdc,
            confidence=recommendation.confidence,
        )

        try:
            self.trade_log.mark(
                trade_id,
                MAY_HAVE_FIRED,
                error="live order submission started; verify on-chain if process stops here",
            )
            # Please refer to TOS before enabling live trading: polymarket.com/tos
            result = self.polymarket.execute_market_order(market, recommendation)
        except ValueError as e:
            msg = str(e)
            if any(s in msg for s in (
                "no asks available",
                "live ask price",
                "below MIN_ENTRY_PRICE",
                "insufficient bid depth",
                "spread too wide",
            )):
                # Thin orderbook — not a code error, just a market condition.
                # Write SKIPPED_GATE (veto) so the allocator doesn't penalise
                # the trader as if it crashed. The market can be retried next
                # cycle once liquidity returns.
                logger.warning(
                    "cycle %s: market %s execution gate (%s) — skipping",
                    cycle_id,
                    market_id,
                    msg[:160],
                )
                self.trade_log.mark(trade_id, SKIPPED_GATE, error=f"execution_gate: {e}")
                return False
            logger.exception(
                "cycle %s: market %s execute_market_order failed", cycle_id, market_id
            )
            self.trade_log.mark(trade_id, FAILED, error=str(e))
            return False
        except Exception as e:
            msg = str(e)
            if "status_code=404" in msg or "No orderbook" in msg:
                logger.warning(
                    "cycle %s: market %s broken market (%s) — skipping",
                    cycle_id,
                    market_id,
                    msg[:160],
                )
                self.trade_log.mark(trade_id, SKIPPED_GATE, error=f"broken_market: {e}")
                return False
            logger.exception(
                "cycle %s: market %s execute_market_order failed", cycle_id, market_id
            )
            self.trade_log.mark(trade_id, FAILED, error=str(e))
            return False

        terminal = FILLED if result.get("status") in ("filled", "matched") else SUBMITTED
        self.trade_log.mark(trade_id, terminal, response=result)
        logger.info("cycle %s: market %s TRADED %s", cycle_id, market_id, result)
        if terminal == FILLED:
            notify_trade(
                event="fill",
                agent="trader",
                market_id=market_id,
                side=recommendation.side,
                price=recommendation.price,
                size_usdc=recommendation.amount_usdc,
                balance_usdc=_safe_balance(self.polymarket),
            )
        return True

    def _build_news_context(self, market_id: str, question: str = "") -> str:
        """Return a brief news summary from DB signals, with Tavily fallback.

        Priority:
        1. Recent DB news_signals (fast, free, always tried first).
        2. Tavily live search when DB has no signals and a question is provided.
           This enriches the LLM for markets that haven't triggered news_signal
           yet — e.g. sports, elections, or any quiet market where external
           probability data may contradict the crowd price.
        """
        try:
            rows = self.trade_log.market_news_signals(market_id, hours=48, limit=5)
            if rows:
                parts = []
                for r in rows:
                    direction = r.get("direction", "")
                    headline = r.get("headline", "")
                    mat = r.get("materiality", "")
                    if headline:
                        parts.append(f"[{direction.upper()}] {headline} (materiality={mat})")
                return "; ".join(parts)
        except Exception:
            logger.exception("_build_news_context DB query failed for %s (non-fatal)", market_id)

        # No DB signals — fall back to live Tavily search for fresh context.
        if question:
            tavily_ctx = tavily_headlines(question, max_results=4)
            if tavily_ctx:
                logger.debug(
                    "_build_news_context: Tavily enrichment for market %s", market_id
                )
                return "[EXTERNAL NEWS]\n" + tavily_ctx
        return ""

    def _conviction_blocks_entry(self, cycle_id: str, market_id: str) -> bool:
        """Return True and log if recent external_conviction signals disapprove.

        Only blocks when ALL recent signals disapprove (i.e. at least one
        disapproval and no approvals). If there are no signals, allow through —
        the absence of signal is not a veto.
        """
        try:
            decisions = self.trade_log.market_brain_decisions(market_id, hours=6)
        except Exception:
            logger.exception("conviction gate DB query failed for %s", market_id)
            return False
        if not decisions:
            return False
        n_approve = sum(1 for d in decisions if d.get("approved"))
        n_reject = sum(1 for d in decisions if not d.get("approved"))
        if n_reject > 0 and n_approve == 0:
            reason = decisions[0].get("reason", "")
            logger.info(
                "cycle %s: market %s skipped (conviction gate: %d disapproval(s),"
                " reason=%s)",
                cycle_id, market_id, n_reject, reason,
            )
            self.trade_log.insert_terminal(
                cycle_id, market_id, SKIPPED_GATE,
                error=f"conviction_gate: {n_reject} disapproval(s): {reason}",
            )
            return True
        return False

    def _brain_entry_gate(
        self,
        cycle_id: str,
        market_id: str,
        market,
        news_context: str,
    ) -> bool:
        """Return False (and log) if MetaBrain rejects this entry.

        Uses MetaBrain.synthesize() which aggregates: spread/horizon gate,
        cross-market signals (Kalshi/Metaculus/Manifold), win-rate from
        history, probability velocity, and external conviction JSONL.
        Falls back to brain.evaluate_general_entry() if MetaBrain unavailable.
        """
        try:
            metadata = market[0].dict().get("metadata", {})
            question = str(metadata.get("question", ""))

            # spread_pct: stored in metadata as a fraction (0–1) by Gamma.
            try:
                spread_pct = float(metadata["spread"]) if metadata.get("spread") else None
            except (TypeError, ValueError):
                spread_pct = None

            # hours_to_close: derived from end / end_date timestamp.
            hours_to_close = None
            end_raw = metadata.get("end") or metadata.get("end_date") or ""
            if end_raw:
                try:
                    from datetime import datetime, timezone
                    end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    hours_to_close = max(0.0, (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0)
                except Exception:
                    pass

            # Try prices[0] for poly_prob.
            poly_prob: Optional[float] = None
            try:
                prices = market[0].dict().get("tokens", [{}])
                poly_prob = float(prices[0].get("price", 0.5)) if prices else None
            except Exception:
                pass

            # MetaBrain synthesis (preferred).
            if self.meta_brain is not None:
                meta = self.meta_brain.synthesize(
                    market_id=market_id,
                    question=question,
                    spread_pct=spread_pct,
                    hours_to_close=hours_to_close,
                    poly_prob=poly_prob,
                    external_context=news_context,
                    token_id=None,
                )
                if not meta.approved:
                    logger.info(
                        "cycle %s: market %s skipped (meta_brain: %s score=%.3f timing=%s)",
                        cycle_id, market_id, meta.reason, meta.score, meta.entry_timing,
                    )
                    self.trade_log.insert_terminal(
                        cycle_id, market_id, SKIPPED_GATE,
                        error=f"meta_brain:{meta.reason} score={meta.score:.3f}",
                    )
                    return False
                logger.debug(
                    "cycle %s: market %s meta_brain approved — %s",
                    cycle_id, market_id, meta.summary,
                )
                return True

            # Fallback: legacy brain gate.
            decision = self.brain.evaluate_general_entry(
                question=question,
                spread_pct=spread_pct,
                hours_to_close=hours_to_close,
                external_context=news_context,
            )
            if not decision.approved:
                logger.info(
                    "cycle %s: market %s skipped (brain gate: %s score=%.3f)",
                    cycle_id, market_id, decision.reason, decision.score,
                )
                self.trade_log.insert_terminal(
                    cycle_id, market_id, SKIPPED_GATE,
                    error=f"brain_gate:{decision.reason} score={decision.score:.3f}",
                )
                return False
            logger.debug(
                "cycle %s: market %s brain gate approved reason=%s score=%.3f",
                cycle_id, market_id, decision.reason, decision.score,
            )
            return True
        except Exception:
            logger.exception("brain_entry_gate failed for %s; blocking entry", market_id)
            self.trade_log.insert_terminal(
                cycle_id, market_id, SKIPPED_GATE,
                error="brain_gate_failed",
            )
            return False

    def maintain_positions(self):
        """Inline position-management call. Delegates to PositionManager.

        The canonical home of exit logic is the dedicated daemon at
        `agents.application.position_manager` (run as its own container
        via `--profile positions`). This inline path exists so callers
        of `Trader.maintain_positions()` get the same behavior without
        spinning up a separate process; useful for tests or single-shot
        CLI invocations.
        """
        try:
            from agents.application.position_manager import PositionManager
            mgr = PositionManager(
                polymarket=self.polymarket,
                trade_log=self.trade_log,
            )
            result = mgr.check_and_close_positions()
            if result.get("evaluated"):
                logger.info("maintain_positions: %s", result)
        except Exception:
            logger.exception("maintain_positions failed")

    def incentive_farm(self):
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    t = Trader(dry_run=True)
    t.one_best_trade()
