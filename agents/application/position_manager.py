"""Position manager — exit logic for poly1 main and approved live agent positions.

This is the implementation of the long-stubbed `Trader.maintain_positions`.
It periodically scans open journal positions (`filled` and `btc_daily_open`),
computes their MTM via Polymarket midpoint, and closes them on three triggers:

- **take_profit:** fast exit can start at +5%; hard cap at +25%
- **stop_loss:**   mid <= entry × (1 - stop_loss_pct)    (default 3%)
- **timeout:**     position age >= max_hold_hours        (default 6h hard cap)

The 6h value is not a target hold. Each cycle revalidates whether to exit now;
holding is allowed only when the brain/forecast evidence says the edge is still
strong enough to justify staying in the position.

Closing places a SELL LIMIT order via Polymarket V2 (the swarm pattern).
On success a new `closed_*` row is written to the journal preserving the
audit trail; the original `filled` row is left in place but won't trigger
re-evaluation because we also write a `closed_take_profit` /
`closed_stop_loss` / `closed_timeout` row that the dedupe gate will see.

Idempotency: each position is identified by `token_id`. Once a closing
order has been posted (regardless of whether it filled), we mark the
position with a `closed_*` row so subsequent cycles skip it.

Single-shot per token: even if two `filled` rows exist for the same
token (because of averaging-down before Fix B), they're treated as one
combined position with summed shares and weighted-average entry price.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from agents.application.exit_executor import ExitExecutor
from agents.application.market_brain import BrainConfig, ExitPosition, MarketBrain
from agents.application.tavily import tavily_headlines
from agents.application.trade_log import TradeLog, RESOLVED_LOSS
from agents.application.trading_policy import (
    MAX_HOLD_SECONDS,
    POSITION_POLL_SECONDS,
    STOP_LOSS_PCT,
    TAKE_PROFIT_CAP_PCT,
)
from agents.utils.notify import notify_trade, _safe_balance


logger = logging.getLogger(__name__)


# Status constants written to the trades table
CLOSED_TP = "closed_take_profit"
CLOSED_SL = "closed_stop_loss"
CLOSED_TIMEOUT = "closed_timeout"
CLOSED_DUST = "closed_dust"
CLOSE_FAILED = "close_failed"


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
class PositionManagerConfig:
    """Env-driven config for exit thresholds."""
    # Hard profit cap: the brain may exit earlier, but never holds past this.
    take_profit_pct: float = TAKE_PROFIT_CAP_PCT
    # Hard loss guard: no position is allowed to drift into "hope mode".
    stop_loss_pct: float = STOP_LOSS_PCT
    # Hard close after this many hours regardless of price (long-tail
    # positions on multi-week markets shouldn't sit indefinitely).
    max_hold_hours: int = int(MAX_HOLD_SECONDS / 3600)
    # Polling cadence — every minute is enough for poly1 main positions
    # which are typically multi-day or longer-resolution markets.
    poll_seconds: int = POSITION_POLL_SECONDS
    # Slippage allowance on the SELL side. We aim to sell at
    # `mid × (1 - slippage)` so the order is competitive at the bid.
    sell_slippage: float = 0.02
    min_exit_notional_usdc: float = 1.0
    # After this many consecutive close_failed rows for the same token,
    # escalate to resolved_loss (illiquid market, FAK never matches).
    max_close_failures: int = 3
    # Pre-exit bid depth: defer non-stop-loss exits when bid-side depth
    # is below this threshold. stop_loss always attempts regardless.
    min_exit_bid_depth_usdc: float = 5.0
    # Heartbeat path for healthcheck.
    heartbeat_path: str = "/app/data/position_manager_heartbeat"
    # When False, log decisions but don't actually post SELL orders.
    execute: bool = False

    @classmethod
    def from_env(cls) -> "PositionManagerConfig":
        return cls(
            take_profit_pct=_env_float("MAINTAIN_TAKE_PROFIT_PCT", TAKE_PROFIT_CAP_PCT),
            stop_loss_pct=_env_float("MAINTAIN_STOP_LOSS_PCT", STOP_LOSS_PCT),
            max_hold_hours=_env_int("MAINTAIN_MAX_HOLD_HOURS", int(MAX_HOLD_SECONDS / 3600)),
            poll_seconds=_env_int("MAINTAIN_POLL_SEC", POSITION_POLL_SECONDS),
            sell_slippage=_env_float("MAINTAIN_SELL_SLIPPAGE", 0.02),
            min_exit_notional_usdc=_env_float("MAINTAIN_MIN_EXIT_NOTIONAL_USDC", 1.0),
            max_close_failures=_env_int("MAINTAIN_MAX_CLOSE_FAILURES", 3),
            min_exit_bid_depth_usdc=_env_float("MIN_EXIT_BID_DEPTH_USDC", 5.0),
            heartbeat_path=os.getenv(
                "MAINTAIN_HEARTBEAT_PATH",
                "/app/data/position_manager_heartbeat",
            ),
            execute=os.getenv("EXECUTE_MAINTAIN", "false").lower() == "true",
        )


@dataclass
class AggregatedPosition:
    """One token_id rolled up across all its filled rows."""
    token_id: str
    market_id: str
    side: str  # 'BUY' or 'SELL' (semantic: what the LLM said)
    total_cost_usdc: float
    total_shares: float
    avg_entry_price: float  # the price at which we bought *this token*
    earliest_ts: float       # unix seconds — for max_hold check
    journal_row_ids: list[int] = field(default_factory=list)
    # Manual-entry overrides — encoded in response_json on the originating
    # filled row (`tp_pct_override`, `no_sl`). When present, position_manager
    # bypasses the brain's compound-exit logic and uses a simple TP-only
    # rule. None means "use global config / brain".
    tp_pct_override: Optional[float] = None
    no_sl: bool = False
    max_hold_seconds_override: Optional[int] = None
    # When set, this position is one leg of a straddle. After the first
    # leg exits at TP, the partner leg's stop-loss is removed so it can
    # run freely to its own TP (its cost is already covered by the TP exit).
    straddle_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class PositionManager:
    """Stateless engine — checks all open positions and decides what to close."""

    def __init__(
        self,
        polymarket,
        trade_log: TradeLog,
        cfg: Optional[PositionManagerConfig] = None,
        brain: Optional[MarketBrain] = None,
        exit_executor: Optional[ExitExecutor] = None,
    ):
        self.polymarket = polymarket
        self.trade_log = trade_log
        self.cfg = cfg or PositionManagerConfig.from_env()
        self.brain = brain or MarketBrain(BrainConfig(
            exit_take_profit_pct=self.cfg.take_profit_pct,
            exit_trailing_stop_pct=_env_float(
                "MAINTAIN_TRAILING_STOP_PCT", 0.02
            ),
            exit_stop_loss_pct=self.cfg.stop_loss_pct,
            exit_max_hold_seconds=self.cfg.max_hold_hours * 3600,
        ))
        self.exit_executor = exit_executor or ExitExecutor(
            polymarket=self.polymarket,
            sell_slippage=self.cfg.sell_slippage,
        )
        # Resolution-sync — detects markets that resolved naturally (we won
        # OR lost via on-chain settlement) and writes RESOLVED_* rows so
        # the journal stops claiming we still hold those positions.
        from agents.application.resolution_sync import ResolutionSync
        self.resolution_sync = ResolutionSync(
            polymarket=self.polymarket,
            trade_log=self.trade_log,
        )
        self._max_price_by_token: dict[str, float] = {}
        # Tracks when we last ran the LLM exit check per token (unix timestamp).
        self._last_llm_exit_check: dict[str, float] = {}
        self._llm_exit_interval: int = _env_int("MAINTAIN_LLM_EXIT_INTERVAL_SEC", 300)
        # Lazy-init: LLM client for exit evaluation (only built on first use).
        self._llm = None
        self._prompter = None

    # --------------------------------------------------------------- public

    def check_and_close_positions(self) -> dict:
        """Walk all open positions; close those matching exit criteria.

        Returns: dict with counts of {evaluated, closed_tp, closed_sl,
        closed_timeout, errors}.
        """
        result = {"evaluated": 0, "closed_tp": 0, "closed_sl": 0,
                  "closed_timeout": 0, "errors": 0, "deferred": 0,
                  "skipped_already_closed": 0}
        # Sync resolved markets first — this terminates phantom-open journal
        # rows so `_aggregate_open_positions` doesn't list tokens we no
        # longer hold (avoids the dust-skip → mid=0 → wasted cycle pattern).
        try:
            res = self.resolution_sync.run_once()
            if res.get("checked", 0) > 0:
                result["resolved"] = res
        except Exception:
            logger.exception("resolution_sync failed (non-fatal)")
        positions = self._aggregate_open_positions()
        for pos in positions:
            result["evaluated"] += 1
            if self._already_closed(pos):
                result["skipped_already_closed"] += 1
                continue
            try:
                reason, mid = self._evaluate_position(pos)
            except Exception:
                logger.exception("eval failed for token %s", pos.token_id[:18])
                result["errors"] += 1
                continue
            if reason is None:
                # Rule-based logic says hold — check LLM reasoning every N minutes.
                reason = self._llm_exit_check(pos, mid)
                if reason is None:
                    continue
            close_result = self._close_position(pos, reason, mid)
            if close_result == "deferred":
                result["deferred"] += 1
            elif close_result:
                if reason == "take_profit":
                    result["closed_tp"] += 1
                elif reason == "stop_loss":
                    result["closed_sl"] += 1
                else:
                    result["closed_timeout"] += 1
            else:
                result["errors"] += 1
        return result

    # --------------------------------------------------------- aggregation

    def _aggregate_open_positions(self) -> list[AggregatedPosition]:
        """Collapse open journal rows on the same token into one position."""
        import json as _json
        rows = self.trade_log.filled_positions_with_id()
        by_token: dict[str, AggregatedPosition] = {}
        for r in rows:
            tok = r.get("token_id")
            if not tok:
                continue
            cost = float(r.get("size_usdc") or 0)
            if cost <= 0:
                continue
            side = r.get("side") or "BUY"
            price = float(r.get("price") or 0.5)
            # Per-position overrides and execution metadata. Manual entries set
            # TP/no-SL fields; live CLOB entries include
            # order_avg_price_estimate / actual_entry_price. Those execution
            # prices are the actual token entry price and must not be inverted
            # for semantic SELL rows.
            tp_override: Optional[float] = None
            no_sl_flag = False
            max_hold_seconds_override: Optional[int] = None
            straddle_id_val: Optional[str] = None
            actual_entry_price: Optional[float] = None
            rj = r.get("response_json")
            if rj:
                try:
                    payload = _json.loads(rj) if isinstance(rj, str) else rj
                    if isinstance(payload, dict):
                        if payload.get("tp_pct_override") is not None:
                            tp_override = float(payload["tp_pct_override"])
                        if payload.get("no_sl"):
                            no_sl_flag = True
                        if payload.get("max_hold_seconds") is not None:
                            max_hold_seconds_override = int(payload["max_hold_seconds"])
                        if payload.get("straddle_id"):
                            straddle_id_val = str(payload["straddle_id"])
                        for key in ("actual_entry_price", "order_avg_price_estimate"):
                            if payload.get(key) is not None:
                                candidate = float(payload[key])
                                if candidate > 0:
                                    actual_entry_price = candidate
                                    break
                except (TypeError, ValueError, _json.JSONDecodeError):
                    pass
            # Convert the LLM's "SELL" semantic into the actual entry price
            # of the token we hold (token_ids[1] at 1 - price).
            entry = (
                actual_entry_price
                if actual_entry_price is not None
                else price if side == "BUY" else (1.0 - price)
            )
            if entry <= 0:
                continue
            shares = cost / entry
            ts_str = r.get("ts") or ""
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = time.time()
            existing = by_token.get(tok)
            if existing is None:
                by_token[tok] = AggregatedPosition(
                    token_id=tok,
                    market_id=str(r.get("market_id") or ""),
                    side=side,
                    total_cost_usdc=cost,
                    total_shares=shares,
                    avg_entry_price=entry,
                    earliest_ts=ts,
                    journal_row_ids=[r.get("id")] if r.get("id") else [],
                    tp_pct_override=tp_override,
                    no_sl=no_sl_flag,
                    max_hold_seconds_override=max_hold_seconds_override,
                    straddle_id=straddle_id_val,
                )
            else:
                # Weighted-average entry price across all fills on this token.
                new_cost = existing.total_cost_usdc + cost
                new_shares = existing.total_shares + shares
                existing.total_cost_usdc = new_cost
                existing.total_shares = new_shares
                existing.avg_entry_price = (
                    new_cost / new_shares if new_shares > 0 else entry
                )
                existing.earliest_ts = min(existing.earliest_ts, ts)
                if r.get("id"):
                    existing.journal_row_ids.append(r.get("id"))
                # Preserve any override from any fill on this token.
                if tp_override is not None and existing.tp_pct_override is None:
                    existing.tp_pct_override = tp_override
                if no_sl_flag:
                    existing.no_sl = True
                if (
                    max_hold_seconds_override is not None
                    and existing.max_hold_seconds_override is None
                ):
                    existing.max_hold_seconds_override = max_hold_seconds_override
                if straddle_id_val and not existing.straddle_id:
                    existing.straddle_id = straddle_id_val
        return list(by_token.values())

    # ----------------------------------------------------------- evaluate

    def _evaluate_position(self, pos: AggregatedPosition) -> tuple[Optional[str], float]:
        """Return (reason, current_mid). reason is None if no action."""
        mid_fetch_error: Optional[str] = None
        try:
            mid_resp = self.polymarket.client.get_midpoint(pos.token_id)
            if isinstance(mid_resp, dict):
                mid = float(mid_resp.get("mid", pos.avg_entry_price))
            else:
                mid = float(mid_resp)
            if mid <= 0:
                mid = pos.avg_entry_price
        except Exception as exc:
            logger.warning(
                "midpoint fetch failed for %s: %s", pos.token_id[:18], exc
            )
            mid = pos.avg_entry_price
            mid_fetch_error = str(exc)

        mark = self.trade_log.upsert_position_mark(
            token_id=pos.token_id,
            market_id=pos.market_id,
            entry_price=pos.avg_entry_price,
            current_price=mid,
            shares=pos.total_shares,
            status="open",
            notes={
                "cost_basis_usdc": round(pos.total_cost_usdc, 6),
                **({"midpoint_error": mid_fetch_error} if mid_fetch_error else {}),
            },
        )
        peak = float(mark.get("max_price") or mid)

        if mid_fetch_error:
            return (None, mid)

        # Per-position TP override (set by manual_entry.py): bypass the
        # brain's compound exit logic. This is a deliberately simple
        # "exit at +X% from entry" — no trailing stop, no momentum, no
        # max-hold. SL is honored unless explicitly disabled by `no_sl`.
        if pos.tp_pct_override is not None:
            gain_pct = (mid - pos.avg_entry_price) / max(pos.avg_entry_price, 1e-9)
            if gain_pct >= pos.tp_pct_override:
                return ("take_profit", mid)
            if not pos.no_sl and gain_pct <= -self.cfg.stop_loss_pct:
                return ("stop_loss", mid)
            if (
                pos.max_hold_seconds_override is not None
                and time.time() - pos.earliest_ts >= pos.max_hold_seconds_override
            ):
                return ("timeout", mid)
            return (None, mid)

        decision = self.brain.evaluate_exit(
            ExitPosition(
                market_id=pos.market_id,
                token_id=pos.token_id,
                side=pos.side,
                entry_price=pos.avg_entry_price,
                current_price=mid,
                opened_ts_ms=int(pos.earliest_ts * 1000),
                max_price_seen=peak,
                shares=pos.total_shares,
            ),
            now_ms=int(time.time() * 1000),
        )
        self._record_exit_brain_decision(pos, decision)
        if not decision.approved:
            return (None, mid)
        reason_map = {
            "take_profit": "take_profit",
            "take_profit_cap": "take_profit",
            "trailing_stop_after_profit": "take_profit",
            "stop_loss": "stop_loss",
            "timeout": "timeout",
        }
        return (reason_map.get(decision.reason), mid)

    # --------------------------------------------------------- LLM exit check

    def _get_llm(self):
        """Lazy-init the LLM client (same model as executor)."""
        if self._llm is None:
            from langchain_openai import ChatOpenAI
            from agents.application.llm_config import openai_model
            model = openai_model()
            self._llm = ChatOpenAI(model=model, temperature=0)
        return self._llm

    def _get_prompter(self):
        if self._prompter is None:
            from agents.application.prompts import Prompter
            self._prompter = Prompter()
        return self._prompter

    def _llm_exit_check(self, pos: AggregatedPosition, mid: float) -> Optional[str]:
        """Ask the LLM every N minutes whether to exit a position.

        Returns a close reason string ("stop_loss" / "take_profit") if the
        LLM recommends exit, or None to hold.  Never raises — errors are
        logged and treated as HOLD.
        """
        now = time.time()
        last = self._last_llm_exit_check.get(pos.token_id, 0.0)
        if now - last < self._llm_exit_interval:
            return None
        self._last_llm_exit_check[pos.token_id] = now

        try:
            hold_hours = (now - pos.earliest_ts) / 3600.0

            # Gather context from DB
            news_rows = self.trade_log.market_news_signals(pos.market_id, hours=24)
            news_context = "; ".join(
                f"{r['headline']} [{r['direction']}, materiality={r['materiality']}]"
                for r in news_rows
            ) if news_rows else ""

            conviction_rows = self.trade_log.market_brain_decisions(pos.market_id, hours=6)
            conviction_context = "; ".join(
                f"approved={r['approved']} score={r.get('score','?')} reason={r.get('reason','')}"
                for r in conviction_rows
            ) if conviction_rows else ""

            # Fetch market question from the journal if available
            question = pos.market_id  # fallback
            try:
                ns = self.trade_log.market_news_signals(pos.market_id, hours=168, limit=1)
                if ns and ns[0].get("market_question"):
                    question = ns[0]["market_question"]
            except Exception:
                pass

            # Live Tavily context — fresher than DB news_signals (which may
            # be hours old). Enriches the LLM exit decision with current news.
            tavily_ctx = ""
            if question and question != pos.market_id:
                tavily_ctx = tavily_headlines(question, max_results=3)
                if tavily_ctx:
                    logger.info(
                        "llm_exit_check: Tavily for %s: %s",
                        pos.token_id[:18], tavily_ctx[:150],
                    )

            prompt = self._get_prompter().should_exit_position(
                question=question,
                side=pos.side,
                entry_price=pos.avg_entry_price,
                current_price=mid,
                hold_hours=hold_hours,
                news_context=news_context,
                conviction_context=conviction_context,
                tavily_context=tavily_ctx,
            )

            llm = self._get_llm()
            raw = None
            try:
                response = llm.invoke(prompt)
                raw = response.content if hasattr(response, "content") else str(response)
            except Exception as _llm_exc:
                _is_quota = (
                    "insufficient_quota" in str(_llm_exc)
                    or "exceeded your current quota" in str(_llm_exc)
                    or "RateLimitError" in type(_llm_exc).__name__
                )
                _anth_key = os.getenv("ANTHROPIC_API_KEY")
                if _is_quota and _anth_key:
                    try:
                        import anthropic as _anth
                        from agents.application.llm_config import anthropic_model
                        _model = anthropic_model()
                        logger.warning("position_manager: OpenAI quota exhausted — fallback to %s", _model)
                        _c = _anth.Anthropic(api_key=_anth_key)
                        _content = prompt if isinstance(prompt, str) else prompt[0].content if hasattr(prompt[0], "content") else str(prompt[0])
                        _r = _c.messages.create(model=_model, max_tokens=1024, messages=[{"role": "user", "content": _content}])
                        raw = _r.content[0].text
                    except Exception as _anth_exc:
                        logger.warning("position_manager: Anthropic fallback failed: %s", _anth_exc)
                        return None
                else:
                    raise _llm_exc

            import json as _json
            import re as _re
            if raw is None:
                logger.warning("llm_exit_check: empty response for %s", pos.token_id[:18])
                return None
            if not isinstance(raw, str):
                raw = str(raw)
            m = _re.search(r"\{.*?\}", raw, _re.DOTALL)
            if not m:
                logger.warning(
                    "llm_exit_check: no JSON in response for %s",
                    pos.token_id[:18],
                )
                return None
            data = _json.loads(m.group())
            action = data.get("action", "HOLD").upper()
            reason_text = data.get("reason", "LLM exit")
            confidence = float(data.get("confidence", 0.5))

            logger.info(
                "llm_exit_check: token=%s action=%s confidence=%.2f reason=%s",
                pos.token_id[:18], action, confidence, reason_text,
            )

            self.trade_log.insert_brain_decision(
                agent="position_manager_llm",
                strategy="llm_exit_evaluation",
                decision_type="exit",
                market_id=pos.market_id,
                token_id=pos.token_id,
                approved=(action == "EXIT"),
                reason=reason_text,
                score=confidence,
                action=action,
            )

            if action == "EXIT":
                pnl_pct = (mid - pos.avg_entry_price) / max(pos.avg_entry_price, 1e-9)
                return "take_profit" if pnl_pct >= 0 else "stop_loss"

        except Exception:
            logger.exception(
                "llm_exit_check failed for token %s (non-fatal, holding)",
                pos.token_id[:18],
            )
        return None

    def _record_exit_brain_decision(self, pos: AggregatedPosition, decision) -> None:
        try:
            self.trade_log.insert_brain_decision(
                agent="position_manager",
                strategy=decision.profile.market_type or "position_exit",
                decision_type="exit",
                market_id=pos.market_id,
                token_id=pos.token_id,
                approved=decision.approved,
                reason=decision.reason,
                score=decision.score,
                market_type=decision.profile.market_type,
                asset=decision.profile.asset,
                features=decision.features,
                action="SELL" if decision.approved else "HOLD",
            )
        except Exception:
            logger.exception("position_manager brain decision journal write failed")

    # ----------------------------------------------------------- closing

    def _already_closed(self, pos: AggregatedPosition) -> bool:
        """Idempotency guard — has a close-attempt row for this token been
        recorded? Prevents double-close even across daemon restarts.

        Dust-close override: a prior terminal close row may represent a
        partial fill that left the bulk of the position on-chain. If the
        wallet still holds > 1 share of the token, treat as still open
        and retry the close. Discovered 2026-05-07 when a $0.0034
        timeout fill on token 115755 left 33.15 shares stranded but
        marked the position closed forever.

        Exception: resolved_loss marks a market that no longer has a CLOB
        orderbook (market resolved or delisted). The dust-override must NOT
        trigger for such tokens — retrying would always 404.
        """
        token_id = pos.token_id
        after_id = max((int(i) for i in pos.journal_row_ids if i), default=0)
        if not self.trade_log.has_close_attempt_for_token(token_id, after_id=after_id):
            return False
        # If the market resolved (no orderbook), skip on-chain dust check —
        # on-chain tokens are redeemable via CTF, not the CLOB.
        if self.trade_log.has_resolved_marker_for_token(token_id, after_id=after_id):
            return True
        # Dust close does not warrant retry — position was evaluated as
        # sub-minimum notional; retrying always reproduces closed_dust.
        if self.trade_log.has_dust_close_for_token(token_id, after_id=after_id):
            return True
        on_chain = self._on_chain_shares(token_id)
        if on_chain is None:
            return True
        if on_chain > 1.0:
            logger.info(
                "position_manager: token=%s journal=closed but on-chain=%.4f "
                "shares — dust close detected, retrying close",
                token_id[:18], on_chain,
            )
            return False
        return True

    def _on_chain_shares(self, token_id: str) -> Optional[float]:
        """Read the deposit-wallet's on-chain CTF balance for this token.
        Returns None if the SDK call fails.

        Necessary because journal-based share counts can drift from
        on-chain reality (fees taken at fill, slippage on entry, etc.).
        Selling more shares than we actually hold → 'not enough balance'
        rejection. Use min(journal_shares, on_chain_shares) at the sell.
        """
        try:
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            resp = self.polymarket.client.get_balance_allowance(
                params=BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=str(token_id),
                )
            )
            if isinstance(resp, dict):
                bal_raw = resp.get("balance", "0")
                return float(bal_raw) / 1_000_000  # 6-decimal CTF token
            # Some SDK versions return objects
            bal_raw = getattr(resp, "balance", None)
            if bal_raw is not None:
                return float(bal_raw) / 1_000_000
        except Exception as exc:
            logger.warning(
                "on-chain CTF balance fetch failed for %s: %s",
                token_id[:18], exc,
            )
        return None

    def _close_position(
        self, pos: AggregatedPosition, reason: str, mid: float
    ) -> bool:
        """Place a SELL order and record a closed row. Returns True on success."""
        # Pre-exit bid depth check: defer non-stop-loss exits when the
        # bid side is too thin.  stop_loss always attempts (capital
        # preservation > execution quality).
        if reason != "stop_loss" and self.cfg.min_exit_bid_depth_usdc > 0:
            try:
                depth = self.polymarket.bid_depth_usdc(pos.token_id)
                if depth < self.cfg.min_exit_bid_depth_usdc:
                    logger.info(
                        "position_manager defer [%s]: token=%s bid_depth=$%.2f "
                        "< min $%.2f — deferring to next cycle",
                        reason, pos.token_id[:18], depth,
                        self.cfg.min_exit_bid_depth_usdc,
                    )
                    return "deferred"
            except Exception:
                # Fail-open: if we can't read the book, proceed with the sell.
                logger.warning(
                    "position_manager bid_depth check failed for %s (proceeding)",
                    pos.token_id[:18],
                )
        sell_price = self.exit_executor.limit_price_from_mid(mid)
        # Clamp shares to actual on-chain balance — journal can over-count
        # by a few percent due to fees taken at fill time.
        shares_to_sell = pos.total_shares
        on_chain = self._on_chain_shares(pos.token_id)
        if on_chain is not None and on_chain < pos.total_shares:
            shares_to_sell = on_chain * 0.999  # tiny margin for rounding
            logger.info(
                "position_manager clamp: token=%s journal=%.4f on_chain=%.4f → selling %.4f",
                pos.token_id[:18], pos.total_shares, on_chain, shares_to_sell,
            )
        if shares_to_sell <= 0:
            logger.warning(
                "position_manager: token=%s has no on-chain balance, skipping",
                pos.token_id[:18],
            )
            return False
        if shares_to_sell * sell_price < self.cfg.min_exit_notional_usdc:
            self.trade_log.insert_terminal(
                cycle_id=f"dust:{pos.token_id[:12]}",
                market_id=pos.market_id,
                status=CLOSED_DUST,
                token_id=pos.token_id,
                side="SELL",
                price=sell_price,
                size_usdc=shares_to_sell * sell_price,
                error=(
                    f"dust notional below min_exit_notional_usdc="
                    f"{self.cfg.min_exit_notional_usdc:.4f}"
                ),
            )
            self.trade_log.mark_position_closed(pos.token_id, status=CLOSED_DUST)
            logger.info(
                "position_manager dust skip: token=%s notional=$%.4f",
                pos.token_id[:18], shares_to_sell * sell_price,
            )
            return True
        status_value = {
            "take_profit": CLOSED_TP,
            "stop_loss": CLOSED_SL,
            "timeout": CLOSED_TIMEOUT,
        }[reason]

        cycle_id = f"close:{pos.token_id[:12]}"

        if not self.cfg.execute:
            # Shadow mode — log decision, don't trade.
            self.trade_log.insert_terminal(
                cycle_id=cycle_id,
                market_id=pos.market_id,
                status=status_value,
                token_id=pos.token_id,
                side="SELL",
                price=sell_price,
                size_usdc=shares_to_sell * sell_price,
                error=(
                    f"SHADOW {reason}: would sell {shares_to_sell:.2f} shares "
                    f"@ {sell_price:.4f} (entry={pos.avg_entry_price:.4f}, "
                    f"mid={mid:.4f})"
                ),
            )
            logger.info(
                "position_manager SHADOW [%s]: token=%s entry=%.4f mid=%.4f "
                "shares=%.2f would_receive=$%.2f",
                reason, pos.token_id[:18], pos.avg_entry_price, mid,
                shares_to_sell, shares_to_sell * sell_price,
            )
            return True

        # Live path — post a SELL order.
        try:
            exit_result = self.exit_executor.sell_fak(
                token_id=pos.token_id,
                shares=shares_to_sell,
                mid=mid,
            )
        except Exception as exc:
            # ExitExecutor should catch its own exceptions; this keeps the
            # manager robust if a custom executor is injected in tests.
            exit_result = None
            logger.exception("exit executor failed for %s", pos.token_id[:18])
            err = f"exit executor exception: {type(exc).__name__}: {exc}"
        else:
            err = exit_result.error if exit_result else "exit executor returned None"

        if exit_result is not None and exit_result.closed:
            # Augment CLOB response with computed PnL fields. allocator
            # reads `pnl_usdc_real` from response_json to score wins/losses.
            #
            # Cash-PnL (`pnl_usdc_real`): actual USDC received minus full
            # position cost basis. Captures dust-monetization correctly —
            # when leftover shares from prior closes are sold at the new
            # price, that residual cash counts as profit (since its cost
            # was already booked at the original close).
            #
            # Strategy-PnL (`strategy_pnl_usdc`): matched-shares only,
            # using avg_entry_price. Reflects whether the strategy's
            # signal was right or wrong, independent of dust effects.
            #
            # Cash-PnL is what the allocator scores on (it's the actual
            # money number). Strategy-PnL is for human evaluation of
            # signal quality — a "stop_loss" close can be cash-positive
            # via dust while strategy-negative.
            taking_raw = (exit_result.response or {}).get("takingAmount", "")
            try:
                actual_proceeds = float(taking_raw) if taking_raw else (shares_to_sell * sell_price)
            except (TypeError, ValueError):
                actual_proceeds = shares_to_sell * sell_price
            pnl_usdc_real = actual_proceeds - pos.total_cost_usdc
            strategy_pnl_usdc = shares_to_sell * (sell_price - pos.avg_entry_price)

            response_with_pnl = dict(exit_result.response or {})
            response_with_pnl["pnl_usdc_real"] = round(pnl_usdc_real, 6)
            response_with_pnl["strategy_pnl_usdc"] = round(strategy_pnl_usdc, 6)
            response_with_pnl["cost_basis_usdc"] = round(pos.total_cost_usdc, 6)
            response_with_pnl["actual_proceeds_usdc"] = round(actual_proceeds, 6)

            self.trade_log.insert_terminal(
                cycle_id=cycle_id,
                market_id=pos.market_id,
                status=status_value,
                token_id=pos.token_id,
                side="SELL",
                price=sell_price,
                size_usdc=shares_to_sell * sell_price,
                response=response_with_pnl,
            )
            self.trade_log.mark_position_closed(pos.token_id, status=status_value)
            # Straddle TP: remove stop-loss on the partner leg so it can
            # run to its own TP — its cost is already covered by this exit.
            if reason == "take_profit" and pos.straddle_id:
                patched = self.trade_log.set_no_sl_for_straddle_partner(
                    pos.straddle_id, pos.token_id
                )
                if patched:
                    logger.info(
                        "position_manager: straddle TP on %s → no_sl set on "
                        "partner (straddle_id=%s)",
                        pos.token_id[:18], pos.straddle_id,
                    )
            logger.info(
                "position_manager CLOSE [%s]: token=%s entry=%.4f mid=%.4f "
                "shares=%.2f status=%s",
                reason, pos.token_id[:18], pos.avg_entry_price, mid,
                shares_to_sell, exit_result.status,
            )
            event_key = {
                "take_profit": "close_tp",
                "stop_loss": "close_sl",
                "timeout": "close_timeout",
            }.get(reason, "close_timeout")
            notify_trade(
                event=event_key,
                agent="position_mgr",
                market_id=pos.market_id,
                side="SELL",
                price=sell_price,
                size_usdc=shares_to_sell * sell_price,
                pnl_usdc=pnl_usdc_real,
                reason=reason,
                balance_usdc=_safe_balance(self.polymarket),
            )
            return True

        # Failed/non-matched sell — don't mark as closed; FAK should not leave a
        # live order resting. The next cycle can retry if the condition remains.
        clob_status = exit_result.status if exit_result is not None else "exception"
        response = exit_result.response if exit_result is not None else None
        err_str = err or f"sell not matched: {clob_status}"

        # 404 / no-orderbook means the market resolved or was delisted. The
        # CLOB will never accept a sell; on-chain tokens are redeemable via
        # the CTF contract, not via CLOB. Write RESOLVED_LOSS so that:
        # (a) has_close_attempt_for_token → True on next cycle, AND
        # (b) the dust-override in _already_closed does NOT re-trigger
        #     (RESOLVED_LOSS is a terminal marker even with leftover tokens).
        if "status_code=404" in err_str or "No orderbook" in err_str:
            logger.warning(
                "position_manager: token=%s market resolved/delisted (no orderbook)"
                " — marking resolved_loss to stop retries",
                pos.token_id[:18],
            )
            self.trade_log.insert_terminal(
                cycle_id=cycle_id,
                market_id=pos.market_id,
                status=RESOLVED_LOSS,
                token_id=pos.token_id,
                side="SELL",
                price=sell_price,
                size_usdc=0,
                response=response,
                error=err_str,
            )
            self.trade_log.mark_position_closed(pos.token_id, status=RESOLVED_LOSS)
            return True  # treat as "handled" so errors counter stays clean

        # Escalate to resolved_loss when FAK keeps bouncing for too many
        # cycles (e.g., illiquid market: 400 "no orders found to match").
        # On-chain tokens remain redeemable via CTF; further CLOB retries
        # are pointless and spam the journal.
        failed_count = self.trade_log.count_close_failed_for_token(pos.token_id)
        if failed_count >= self.cfg.max_close_failures:
            logger.error(
                "position_manager: token=%s already has %d close_failed rows — "
                "escalating to resolved_loss (illiquid/stuck). "
                "Redeem on-chain via CTF if needed. error=%s",
                pos.token_id[:18], failed_count, err_str,
            )
            self.trade_log.insert_terminal(
                cycle_id=cycle_id,
                market_id=pos.market_id,
                status=RESOLVED_LOSS,
                token_id=pos.token_id,
                side="SELL",
                price=sell_price,
                size_usdc=0,
                response=response,
                error=f"escalated after {failed_count} close_failed: {err_str}",
            )
            self.trade_log.mark_position_closed(pos.token_id, status=RESOLVED_LOSS)
            return True

        self.trade_log.insert_terminal(
            cycle_id=cycle_id,
            market_id=pos.market_id,
            status=CLOSE_FAILED,
            token_id=pos.token_id,
            side="SELL",
            price=sell_price,
            size_usdc=0,
            response=response,
            error=err_str,
        )
        return False


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class PositionManagerDaemon:
    """Long-running loop. SIGTERM-aware. One process per replica."""

    def __init__(self, db_path: Optional[str] = None):
        self.cfg = PositionManagerConfig.from_env()
        self.tl = TradeLog(db_path=db_path)
        # Lazy import — don't construct the heavy CLOB client unless needed.
        from agents.polymarket.polymarket import Polymarket
        # The manager needs to actually send sells, so live client is
        # required even in shadow mode (we still poll midpoints).
        self.polymarket = Polymarket(live=True)
        self.engine = PositionManager(
            polymarket=self.polymarket,
            trade_log=self.tl,
            cfg=self.cfg,
            brain=MarketBrain(),
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
        logger.info(
            "PositionManagerDaemon: starting tp=%.1f%% sl=%.1f%% max_hold=%dh execute=%s",
            self.cfg.take_profit_pct * 100,
            self.cfg.stop_loss_pct * 100,
            self.cfg.max_hold_hours,
            self.cfg.execute,
        )
        try:
            while not self._stop.is_set():
                try:
                    result = self.engine.check_and_close_positions()
                    if result["evaluated"] > 0:
                        logger.info("position_manager cycle: %s", result)
                except Exception:
                    logger.exception("position_manager cycle failed")
                try:
                    self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
                    self.heartbeat.touch()
                except Exception:
                    logger.warning("heartbeat touch failed")
                self._stop.wait(self.cfg.poll_seconds)
        finally:
            logger.info("PositionManagerDaemon: exited")


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)
    PositionManagerDaemon().run()
