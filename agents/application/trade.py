import ast
import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from agents.application.executor import Executor as Agent
from agents.application.risk_gate import RiskGate
from agents.application.trade_log import (
    FAILED,
    FILLED,
    SKIPPED_DEDUPE,
    SKIPPED_DRY_RUN,
    SKIPPED_GATE,
    SUBMITTED,
    TradeLog,
)
from agents.polymarket.gamma import GammaMarketClient as Gamma
from agents.polymarket.polymarket import Polymarket


logger = logging.getLogger(__name__)


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
        self.shadow_ignore_risk_gate = (
            os.getenv("SHADOW_IGNORE_RISK_GATE", "false").lower() == "true"
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

        filtered_events = self.agent.filter_events_with_rag(events)
        logger.info("cycle %s: %d events after RAG filter", cycle_id, len(filtered_events))

        markets = self.agent.map_filtered_events_to_markets(filtered_events)
        logger.info("cycle %s: %d markets mapped", cycle_id, len(markets))

        filtered_markets = self.agent.filter_markets(markets)
        logger.info(
            "cycle %s: %d markets after filter", cycle_id, len(filtered_markets)
        )

        ranked = self._rank_markets(filtered_markets)[: self.top_n]
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
        # Chroma similarity_search_with_score returns LOWER score = MORE relevant
        # (cosine distance), so we sort ASCENDING by score. Within equal score,
        # prefer wider spread (more market-making edge) by sorting ASCENDING on
        # negative spread (i.e. larger spread comes first).
        def key(item):
            doc = item[0].dict()
            metadata = doc.get("metadata", {})
            spread = metadata.get("spread")
            try:
                spread_val = float(spread) if spread is not None else 0.0
            except (TypeError, ValueError):
                spread_val = 0.0
            chroma_score = float(item[1]) if len(item) > 1 else 0.0
            return (chroma_score, -spread_val)

        return sorted(filtered_markets, key=key)

    def _market_id(self, market) -> str:
        metadata = market[0].dict().get("metadata", {})
        for k in ("id", "market_id", "questionID", "question_id"):
            if k in metadata and metadata[k] is not None:
                return str(metadata[k])
        return str(metadata.get("question", ""))[:64]

    def _evaluate_market(self, cycle_id: str, market) -> bool:
        market_id = self._market_id(market)

        # First gate: do we already hold a filled position on this market?
        # Without exit logic (`maintain_positions` is a stub), reopening is
        # "averaging down" — observed 2026-05-06 when the LLM doubled
        # exposure on 566187/566188 from 0.38 → 0.205. Block regardless
        # of dedupe age until exit logic exists to close positions
        # before reopening.
        if self.trade_log.has_filled_position_for_market(market_id):
            logger.info(
                "cycle %s: market %s skipped (already holds filled position)",
                cycle_id, market_id,
            )
            self.trade_log.insert_terminal(
                cycle_id, market_id, SKIPPED_DEDUPE,
                error="already holds filled position on this market",
            )
            return False

        if self.trade_log.has_active_trade_for_market(market_id, hours=6):
            logger.info(
                "cycle %s: market %s skipped (recent active trade)", cycle_id, market_id
            )
            self.trade_log.insert_terminal(
                cycle_id, market_id, SKIPPED_DEDUPE,
                error="active trade in last 6h",
            )
            return False

        try:
            best_trade = self.agent.source_best_trade(market)
            recommendation = self.agent.parse_trade_recommendation(best_trade)
        except Exception as e:
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
        try:
            metadata = market[0].dict()["metadata"]
            token_ids = ast.literal_eval(metadata["clob_token_ids"])
            if len(token_ids) == 2:
                token_id = (
                    token_ids[0] if recommendation.side.upper() == "BUY" else token_ids[1]
                )
        except Exception:
            token_id = None

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
            # Please refer to TOS before enabling live trading: polymarket.com/tos
            result = self.polymarket.execute_market_order(market, recommendation)
        except ValueError as e:
            if "no asks available" in str(e):
                # Thin orderbook — not a code error, just a market condition.
                # Write SKIPPED_GATE (veto) so the allocator doesn't penalise
                # the trader as if it crashed. The market can be retried next
                # cycle once liquidity returns.
                logger.warning(
                    "cycle %s: market %s illiquid (no asks) — skipping", cycle_id, market_id
                )
                self.trade_log.mark(trade_id, SKIPPED_GATE, error=f"illiquid: {e}")
                return False
            logger.exception(
                "cycle %s: market %s execute_market_order failed", cycle_id, market_id
            )
            self.trade_log.mark(trade_id, FAILED, error=str(e))
            return False
        except Exception as e:
            logger.exception(
                "cycle %s: market %s execute_market_order failed", cycle_id, market_id
            )
            self.trade_log.mark(trade_id, FAILED, error=str(e))
            return False

        terminal = FILLED if result.get("status") in ("filled", "matched") else SUBMITTED
        self.trade_log.mark(trade_id, terminal, response=result)
        logger.info("cycle %s: market %s TRADED %s", cycle_id, market_id, result)
        return True

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
