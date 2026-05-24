"""Resolution-sync: detect resolved markets and record actual P&L.

Background
----------

Without this module the journal lies. After a Polymarket market resolves
(daily/weekly markets close, sport events finish), the on-chain CTF
balance for the *winning* token stays nonzero (eventually redeemed to
USDC) while the *losing* token goes to dust. But nothing in poly1 wrote
a "resolved" row to the journal, so:

  - `Trader.has_filled_position_for_market` still treats those tokens
    as open positions and blocks reentry forever.
  - `CapitalAllocator` scores agents on `decisions/entries/exits/errors/
    stale_state` but never on realized P&L, because closed_* rows only
    cover SL/TP/timeout — not market resolution.
  - User-facing reports compute MTM as `journal_shares × current_mid`,
    which is meaningless after resolution (mid → 0 for losers, no
    orderbook).

This module reconciles by checking on-chain CTF balance against journal
"open" positions every cycle, classifying resolutions, and writing
RESOLVED_YES / RESOLVED_NO / RESOLVED_LOSS rows with the realized P&L.

What "resolved" means here
--------------------------

A token-position is treated as resolved when ALL three are true:

  1. The journal has at least one FILLED / BTC_DAILY_OPEN / SCALPER_LEG
     row on this token (i.e., we did buy something).
  2. There's no successful close row yet (so position_manager's idempotency
     guard hasn't already terminated it).
  3. The on-chain CTF balance is < `dust_shares_floor` (default 0.5).

If those hold, we ask Gamma whether the underlying market resolved YES or
NO, derive whether the token we held was the winning side, and record:

  - `RESOLVED_YES`  → we were on the winning side; payout = shares × $1
  - `RESOLVED_NO`   → we were on the winning side of a NO-side bet
  - `RESOLVED_LOSS` → we were on the losing side; payout = 0

`size_usdc` on the resolution row stores the dollar payout, NOT the
original cost basis. Allocator's P&L computation then does:

    realized_pnl = sum(payout for resolved rows on this agent's tokens)
                 - sum(cost for filled rows on the same tokens)

Idempotency is handled by `has_close_attempt_for_token` — once we write
a RESOLVED_* row, subsequent cycles skip the token.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from agents.application.trade_log import (
    BTC_DAILY_OPEN,
    FILLED,
    NEAR_RESOLUTION_OPEN,
    NEWS_SHOCK_OPEN,
    RESOLVED_LOSS,
    RESOLVED_NO,
    RESOLVED_YES,
    SCALPER_LEG,
    WALLET_FOLLOW_OPEN,
    TradeLog,
)


logger = logging.getLogger(__name__)


@dataclass
class ResolutionConfig:
    dust_shares_floor: float = 0.5  # below this on-chain balance, treat as resolved
    enabled: bool = True
    # Also scan swarm.db.fills and write to swarm.db.pnl_events so the
    # allocator's _read_swarm path picks up realized PnL on swarm-side
    # positions. Without this, swarm sub-agents can't be defunded.
    swarm_db_path: str = ""
    swarm_sync_enabled: bool = True
    # Dust terminator: when a position is on-chain dust AND gamma hasn't
    # produced a resolution AND the position is older than the threshold,
    # auto-write resolved_loss to terminate the journal. Without this,
    # position_manager re-evaluates the same dust positions forever and
    # the calibrator counts them as still-open exposure.
    dust_terminator_enabled: bool = True
    dust_terminator_age_hours: int = 24

    @classmethod
    def from_env(cls) -> "ResolutionConfig":
        return cls(
            dust_shares_floor=float(os.getenv("RESOLUTION_DUST_FLOOR", "0.5")),
            enabled=os.getenv("RESOLUTION_SYNC_ENABLED", "true").lower() == "true",
            swarm_db_path=os.getenv(
                "RESOLUTION_SWARM_DB_PATH",
                "/app/swarm/data/swarm.db",
            ),
            swarm_sync_enabled=os.getenv(
                "RESOLUTION_SWARM_SYNC_ENABLED", "true"
            ).lower() == "true",
            dust_terminator_enabled=os.getenv(
                "RESOLUTION_DUST_TERMINATOR_ENABLED", "true"
            ).lower() == "true",
            dust_terminator_age_hours=int(
                os.getenv("RESOLUTION_DUST_TERMINATOR_AGE_HOURS", "24")
            ),
        )


class ResolutionSync:
    """Compares journal-claimed open positions to on-chain CTF balance,
    writes RESOLVED_* rows for tokens whose markets have resolved.

    Designed to be invoked once per position_manager cycle (60s by default).
    Cheap when there are no new resolutions; queries Gamma + CTF balance
    only for tokens that look stranded.
    """

    OPEN_STATUSES = (FILLED, BTC_DAILY_OPEN, SCALPER_LEG, NEAR_RESOLUTION_OPEN, NEWS_SHOCK_OPEN, WALLET_FOLLOW_OPEN)

    def __init__(
        self,
        polymarket,
        trade_log: TradeLog,
        cfg: Optional[ResolutionConfig] = None,
    ):
        self.polymarket = polymarket
        self.trade_log = trade_log
        self.cfg = cfg or ResolutionConfig.from_env()

    def run_once(self) -> dict:
        """Walk all open journal positions; record RESOLVED_* for any whose
        on-chain balance went to dust. Returns counts by outcome."""
        result = {
            "checked": 0,
            "resolved_yes": 0,
            "resolved_no": 0,
            "resolved_loss": 0,
            "still_held": 0,
            "dust_market_open": 0,
            "errors": 0,
            "swarm_pnl_events_written": 0,
        }
        if not self.cfg.enabled:
            return result

        for token_id in self._tokens_needing_check():
            result["checked"] += 1
            try:
                outcome, why = self._classify_token_v2(token_id)
            except Exception:
                logger.exception("resolution_sync: classify failed for %s", token_id[:18])
                result["errors"] += 1
                continue

            if outcome is None:
                result[why] = result.get(why, 0) + 1
                continue

            self._record_resolution(token_id, outcome)
            result[outcome["status_key"]] += 1

        # Phase 2: scan swarm fills, write pnl_events for resolved markets.
        if self.cfg.swarm_sync_enabled and self.cfg.swarm_db_path:
            try:
                wrote = self._sync_swarm_resolutions()
                result["swarm_pnl_events_written"] = wrote
            except Exception:
                logger.exception("resolution_sync: swarm sync failed")
                result["errors"] += 1

        if result["checked"] > 0 or result["swarm_pnl_events_written"] > 0:
            logger.info("resolution_sync: %s", result)
        return result

    def _classify_token_v2(self, token_id: str) -> tuple:
        """Like _classify_token but also returns a diagnostic string."""
        on_chain = self._on_chain_shares(token_id)
        if on_chain is None:
            return None, "errors"
        if on_chain >= self.cfg.dust_shares_floor:
            return None, "still_held"
        outcome = self._gamma_resolution(token_id)
        if outcome is not None:
            return outcome, "ok"
        # Dust on-chain AND gamma has nothing to say. Without the
        # terminator the journal grows a stuck "dust_market_open" row
        # for every old position whose market hasn't resolved yet —
        # position_manager wastes a cycle on each one forever, and the
        # calibrator counts them as live exposure.
        if (
            self.cfg.dust_terminator_enabled
            and self._is_older_than_dust_threshold(token_id)
        ):
            return self._synthesize_dust_loss_outcome(token_id), "dust_terminated"
        return None, "dust_market_open"

    def _is_older_than_dust_threshold(self, token_id: str) -> bool:
        """Return True if the EARLIEST open row for this token is older
        than `dust_terminator_age_hours`. Older positions are deemed
        abandoned — markets typically resolve within a few days even
        when gamma doesn't surface it in our queries."""
        with self.trade_log._lock, self.trade_log._connect() as conn:
            row = conn.execute(
                "SELECT MIN(ts) AS first_ts FROM trades "
                "WHERE token_id = ?",
                (token_id,),
            ).fetchone()
        first_ts = row["first_ts"] if row else None
        if not first_ts:
            return False
        try:
            dt = datetime.fromisoformat(str(first_ts).replace("Z", "+00:00"))
        except ValueError:
            return False
        age_hours = (
            datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        ).total_seconds() / 3600
        return age_hours >= self.cfg.dust_terminator_age_hours

    def _synthesize_dust_loss_outcome(self, token_id: str) -> dict:
        """Build a resolved_loss outcome for dust terminator path. We
        treat the position as a total write-off because we hold no
        useful shares and the market hasn't produced a payout."""
        return {
            "status_key": "resolved_loss",
            "market_id": "",
            "payout_per_share": 0.0,
            "outcome_label": "dust_terminated",
        }

    def _tokens_needing_check(self) -> list[str]:
        """Return distinct token_ids that have OPEN_STATUSES rows but no
        terminal close/resolved row."""
        with self.trade_log._lock, self.trade_log._connect() as conn:
            placeholders = ",".join(["?"] * len(self.OPEN_STATUSES))
            sql = (
                f"SELECT token_id, MAX(id) AS latest_open_id FROM trades "
                f"WHERE status IN ({placeholders}) AND token_id IS NOT NULL "
                f"AND token_id != '' "
                f"GROUP BY token_id"
            )
            rows = conn.execute(sql, self.OPEN_STATUSES).fetchall()
        candidates = [(r["token_id"], int(r["latest_open_id"])) for r in rows]
        # Filter out tokens already terminally resolved/closed after their
        # latest open row. Old terminal rows must not suppress a re-entry.
        return [
            token_id for token_id, latest_open_id in candidates
            if not self.trade_log.has_close_attempt_for_token(
                token_id, after_id=latest_open_id
            )
        ]

    def _classify_token(self, token_id: str) -> Optional[dict]:
        """If on-chain balance is dust, query Gamma for resolution and
        return outcome dict. If still held, return None."""
        on_chain = self._on_chain_shares(token_id)
        if on_chain is None:
            # RPC failure — leave alone, try next cycle.
            return None
        if on_chain >= self.cfg.dust_shares_floor:
            return None  # still held, market not resolved against us yet

        # Token is at dust on-chain. Two cases:
        #   - Market resolved AGAINST our side (token is the loser → 0 payout)
        #   - Market resolved FOR our side (we redeemed → cash arrived, balance is 0)
        # We can't easily tell which from CTF balance alone (both are 0 after
        # redemption). Best-effort: ask Gamma for resolution and compare to
        # the side we bought.
        outcome = self._gamma_resolution(token_id)
        if outcome is None:
            # Market not yet resolved on Gamma side or fetch failed — skip.
            return None
        return outcome

    def _on_chain_shares(self, token_id: str) -> Optional[float]:
        try:
            from py_clob_client_v2.clob_types import (
                AssetType,
                BalanceAllowanceParams,
            )
            resp = self.polymarket.client.get_balance_allowance(
                params=BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=str(token_id),
                )
            )
            if isinstance(resp, dict):
                return float(resp.get("balance", 0)) / 1_000_000
            bal_raw = getattr(resp, "balance", None)
            if bal_raw is not None:
                return float(bal_raw) / 1_000_000
        except Exception as exc:
            logger.warning("resolution_sync: balance fetch failed for %s: %s",
                           token_id[:18], exc)
        return None

    def _gamma_resolution(self, token_id: str) -> Optional[dict]:
        """Look up the market via Gamma and determine if the token we
        bought was YES, NO, or undetermined. Returns dict with keys
        `status_key`, `payout_per_share`, `market_id`, `outcome_label`."""
        # First find the market that contains this token.
        market_doc = self._find_market_for_token(token_id)
        if market_doc is None:
            return None

        # Gamma reports resolution as `umaResolutionStatus` and outcomePrices.
        if not market_doc.get("closed", False):
            return None  # still trading

        outcome_prices_raw = market_doc.get("outcomePrices") or "[]"
        outcomes_raw = market_doc.get("outcomes") or "[]"
        clob_token_ids_raw = market_doc.get("clobTokenIds") or "[]"
        try:
            import ast
            outcome_prices = ast.literal_eval(outcome_prices_raw)
            outcomes = ast.literal_eval(outcomes_raw)
            clob_token_ids = ast.literal_eval(clob_token_ids_raw)
        except Exception:
            return None
        if len(outcome_prices) < 2 or len(clob_token_ids) < 2:
            return None

        # Find which side (index 0 = YES, index 1 = NO) this token was.
        try:
            our_index = clob_token_ids.index(str(token_id))
        except ValueError:
            return None

        try:
            our_outcome_price = float(outcome_prices[our_index])
        except (ValueError, TypeError):
            return None

        outcome_label = outcomes[our_index] if our_index < len(outcomes) else "?"

        # outcomePrices = [yes_payout, no_payout] each 0 or 1
        if our_outcome_price >= 0.99:
            # We were on the winning side.
            status_key = "resolved_yes" if our_index == 0 else "resolved_no"
            return {
                "status_key": status_key,
                "status": RESOLVED_YES if our_index == 0 else RESOLVED_NO,
                "payout_per_share": 1.0,
                "market_id": market_doc.get("conditionId") or market_doc.get("id", ""),
                "outcome_label": outcome_label,
            }
        else:
            # We were on the losing side.
            return {
                "status_key": "resolved_loss",
                "status": RESOLVED_LOSS,
                "payout_per_share": 0.0,
                "market_id": market_doc.get("conditionId") or market_doc.get("id", ""),
                "outcome_label": outcome_label,
            }

    def _find_market_for_token(self, token_id: str) -> Optional[dict]:
        """Query Gamma for the market containing this token_id.

        Gamma's /markets endpoint accepts `clob_token_ids=<tid>` to find the
        owning market. Returns the market dict or None.
        """
        try:
            import urllib.parse
            import urllib.request
            import json
            params = urllib.parse.urlencode({"clob_token_ids": str(token_id)})
            url = f"https://gamma-api.polymarket.com/markets?{params}"
            # Gamma returns 403 to default urllib User-Agent — set a browser UA.
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 poly1-resolution-sync"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict) and data.get("data"):
                arr = data["data"]
                if arr:
                    return arr[0]
        except Exception as exc:
            logger.debug("resolution_sync: gamma lookup failed for %s: %s",
                         token_id[:18], exc)
        return None

    def _sync_swarm_resolutions(self) -> int:
        """Scan swarm.db.fills for tokens whose markets have resolved on
        Gamma; write a pnl_event row per (agent, market) so the allocator's
        _read_swarm path picks up realized PnL.

        Idempotency: we only write a pnl_event if there's no existing
        pnl_event for that (agent, market_id) pair already.
        """
        import os.path
        if not os.path.exists(self.cfg.swarm_db_path):
            return 0
        wrote = 0
        try:
            con = sqlite3.connect(self.cfg.swarm_db_path)
            con.row_factory = sqlite3.Row
            # Distinct (agent, market_id) where we have fills
            cursor = con.execute(
                "SELECT DISTINCT agent, market_id FROM fills"
            )
            agent_markets = list(cursor.fetchall())
        except Exception as exc:
            logger.warning("resolution_sync: swarm fills read failed: %s", exc)
            return 0

        for row in agent_markets:
            agent = row["agent"]
            market_id = row["market_id"]
            if not agent or not market_id:
                continue
            # Already have a pnl_event for this (agent, market)?
            existing = con.execute(
                "SELECT 1 FROM pnl_events WHERE agent=? AND market_id=? LIMIT 1",
                (agent, market_id),
            ).fetchone()
            if existing:
                continue

            # Look up Gamma resolution by condition_id (swarm uses
            # market_id == conditionId).
            market_doc = self._gamma_market_by_condition(market_id)
            if market_doc is None or not market_doc.get("closed", False):
                continue

            # Determine which side (YES/NO) won and which side this agent
            # bought, sum fills, compute payout.
            outcome_prices_raw = market_doc.get("outcomePrices") or "[]"
            try:
                import ast
                outcome_prices = [float(p) for p in ast.literal_eval(outcome_prices_raw)]
            except Exception:
                continue
            if len(outcome_prices) < 2:
                continue
            yes_won = outcome_prices[0] >= 0.99

            # Sum agent's fills on this market
            fills = con.execute(
                "SELECT side, outcome, price, size, fee FROM fills "
                "WHERE agent=? AND market_id=?",
                (agent, market_id),
            ).fetchall()
            total_cost = 0.0
            total_payout = 0.0
            for f in fills:
                side = (f["side"] or "").upper()
                outcome_label = (f["outcome"] or "").upper()
                price_cents = float(f["price"] or 0)
                size_shares = float(f["size"] or 0)
                fee = float(f["fee"] or 0)
                # Cost basis: shares × price/100
                cost = size_shares * (price_cents / 100.0) + fee
                total_cost += cost
                # Did this fill win? Side BUY + outcome YES wins if YES won.
                # Side BUY + outcome NO wins if YES lost. (Swarm sells are
                # rare; ignore SELL fills for now.)
                if side != "BUY":
                    continue
                won = (outcome_label == "YES" and yes_won) or (
                    outcome_label == "NO" and not yes_won
                )
                if won:
                    total_payout += size_shares  # $1/share

            pnl = total_payout - total_cost
            ts_ms = int(__import__("time").time() * 1000)
            con.execute(
                "INSERT INTO pnl_events (agent, market_id, pnl, reason, ts_ms) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent, market_id, pnl, "resolution_sync_auto", ts_ms),
            )
            con.commit()
            wrote += 1
            logger.info(
                "resolution_sync: swarm %s on %s — payout=$%.2f cost=$%.2f pnl=$%+.2f",
                agent, market_id[:18], total_payout, total_cost, pnl,
            )
        con.close()
        return wrote

    def _gamma_market_by_condition(self, condition_id: str) -> Optional[dict]:
        """Look up market by conditionId via Gamma."""
        try:
            import urllib.parse
            import urllib.request
            import json
            params = urllib.parse.urlencode({"condition_ids": str(condition_id)})
            url = f"https://gamma-api.polymarket.com/markets?{params}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 poly1-resolution-sync"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if isinstance(data, list) and data:
                return data[0]
        except Exception as exc:
            logger.debug("resolution_sync: gamma condition lookup failed: %s", exc)
        return None

    def _record_resolution(self, token_id: str, outcome: dict) -> None:
        """Compute realized P&L per filled row and write a single resolution
        row aggregating everything. Payout is in size_usdc; cost basis stays
        in the original FILLED rows so the allocator can join them later."""
        # Sum shares we bought on this token.
        with self.trade_log._lock, self.trade_log._connect() as conn:
            sql = (
                "SELECT side, price, size_usdc, cycle_id, market_id "
                "FROM trades WHERE token_id = ? "
                "AND status IN (?, ?, ?, ?, ?) "
                "AND (error IS NULL OR error NOT LIKE 'SHADOW%')"
            )
            rows = conn.execute(
                sql, (str(token_id), FILLED, BTC_DAILY_OPEN, SCALPER_LEG, NEAR_RESOLUTION_OPEN, NEWS_SHOCK_OPEN)
            ).fetchall()

        total_shares = 0.0
        total_cost = 0.0
        cycle_id = "resolution_sync"
        market_id = outcome.get("market_id", "")
        for side, price, size_usdc, c, m in rows:
            try:
                p = float(price or 0)
                s = float(size_usdc or 0)
            except (TypeError, ValueError):
                continue
            if p <= 0 or s <= 0:
                continue
            # Convert LLM-side semantics: BUY at price; SELL means buying NO at 1-price.
            holding_price = p if (side or "").upper() == "BUY" else max(0.001, 1.0 - p)
            shares = s / holding_price
            total_shares += shares
            total_cost += s
            if c:
                cycle_id = str(c)
            if m and not market_id:
                market_id = str(m)

        payout = total_shares * outcome["payout_per_share"]
        pnl = payout - total_cost

        self.trade_log.insert_terminal(
            cycle_id=cycle_id,
            market_id=market_id,
            status=outcome["status"],
            token_id=token_id,
            side="RESOLUTION",
            price=outcome["payout_per_share"],
            size_usdc=payout,
            confidence=None,
            response={
                "outcome_label": outcome.get("outcome_label"),
                "shares_held": round(total_shares, 4),
                "total_cost_usdc": round(total_cost, 4),
                "payout_usdc": round(payout, 4),
                "realized_pnl_usdc": round(pnl, 4),
            },
        )
        logger.info(
            "resolution_sync: token=%s outcome=%s shares=%.2f cost=$%.2f payout=$%.2f pnl=$%+.2f",
            token_id[:18], outcome["status"], total_shares, total_cost, payout, pnl,
        )

        # Stage-0 calibration loop: annotate brain_decisions so WinRateAdvisor
        # can compute real win-rate from actual outcomes rather than falling
        # back to the trades table.  This is write-only — no trading decision
        # reads this path; it only enriches the journal for measurement.
        if market_id:
            self._annotate_brain_decisions(
                market_id=market_id,
                outcome_status=outcome["status_key"],
                outcome_context={
                    "resolution_outcome": outcome["status_key"],
                    "payout_per_share": outcome["payout_per_share"],
                    "shares_held": round(total_shares, 4),
                    "total_cost_usdc": round(total_cost, 4),
                    "payout_usdc": round(payout, 4),
                    "realized_pnl_usdc": round(pnl, 4),
                    "outcome_label": outcome.get("outcome_label"),
                },
            )

    def _annotate_brain_decisions(
        self,
        market_id: str,
        outcome_status: str,
        outcome_context: dict,
    ) -> None:
        """Annotate all unannotated brain_decisions rows for *market_id* with
        their resolution outcome.

        Only updates rows where outcome_status IS NULL (idempotent — calling
        this twice for the same market is safe and a no-op the second time).

        This method is write-only and does not affect any trading decision.
        Its sole purpose is to fill the calibration loop so that WinRateAdvisor
        can compute empirical win-rates from brain_decisions rather than
        falling back to the coarser trades table.
        """
        try:
            with self.trade_log._lock, self.trade_log._connect() as conn:
                rows = conn.execute(
                    "SELECT id FROM brain_decisions "
                    "WHERE market_id = ? AND outcome_status IS NULL",
                    (str(market_id),),
                ).fetchall()
            for row in rows:
                self.trade_log.update_brain_decision_outcome(
                    decision_id=int(row["id"]),
                    outcome_status=outcome_status,
                    outcome=outcome_context,
                )
            if rows:
                logger.info(
                    "resolution_sync: annotated %d brain_decision(s) market=%s outcome=%s",
                    len(rows), market_id[:24], outcome_status,
                )
        except Exception:
            logger.exception(
                "resolution_sync: brain_decisions annotation failed for market=%s",
                market_id[:24],
            )
