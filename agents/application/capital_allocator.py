"""Read-only capital allocation brain for trading agents.

The allocator scores each strategy from local journals, then recommends a
conservative budget split. It deliberately does not mutate .env, Docker, or
live trading state; operators can review the recommendation before applying it.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


DEFAULT_POLY_DB = "./data/trade_log.db"
DEFAULT_SWARM_DB = os.path.expanduser("~/Desktop/poly/bot/data/swarm.db")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _connect_ro(path: str) -> Optional[sqlite3.Connection]:
    db_path = Path(path).expanduser().resolve()
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


@dataclass
class AgentScore:
    agent: str
    source: str
    decisions: int = 0
    entries: int = 0
    exits: int = 0
    errors: int = 0
    vetoes: int = 0
    stale_state: int = 0
    deployed_usdc: float = 0.0
    realized_pnl_usdc: float = 0.0
    paper_pnl_usdc: float = 0.0
    wins: int = 0
    losses: int = 0
    market_score: float = 0.0
    score: float = 0.0
    recommended_usdc: float = 0.0
    live_allowed: bool = False
    reasons: list[str] = field(default_factory=list)

    @property
    def win_rate(self) -> float | None:
        decided = self.wins + self.losses
        return (self.wins / decided) if decided else None

    def as_dict(self) -> dict:
        wr = self.win_rate
        return {
            "agent": self.agent,
            "source": self.source,
            "decisions": self.decisions,
            "entries": self.entries,
            "exits": self.exits,
            "errors": self.errors,
            "vetoes": self.vetoes,
            "stale_state": self.stale_state,
            "deployed_usdc": round(self.deployed_usdc, 4),
            "realized_pnl_usdc": round(self.realized_pnl_usdc, 4),
            "paper_pnl_usdc": round(self.paper_pnl_usdc, 4),
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(wr, 4) if wr is not None else None,
            "market_score": round(self.market_score, 4),
            "score": round(self.score, 4),
            "recommended_usdc": round(self.recommended_usdc, 4),
            "live_allowed": self.live_allowed,
            "reasons": list(self.reasons),
        }


@dataclass
class AllocationReport:
    generated_at: str
    window_hours: float
    total_budget_usdc: float
    agents: list[AgentScore]
    warnings: list[str] = field(default_factory=list)
    market_intelligence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "window_hours": self.window_hours,
            "total_budget_usdc": round(self.total_budget_usdc, 4),
            "agents": [a.as_dict() for a in self.agents],
            "warnings": list(self.warnings),
            "market_intelligence": dict(self.market_intelligence),
        }


@dataclass
class MarketIntelligenceSnapshot:
    crypto: dict[str, dict] = field(default_factory=dict)
    gamma_crypto_markets: int = 0
    gamma_avg_liquidity_usd: float = 0.0
    gamma_avg_volume_24h_usd: float = 0.0
    fresh_news_signals: int = 0
    fresh_brain_approvals: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "crypto": self.crypto,
            "gamma_crypto_markets": self.gamma_crypto_markets,
            "gamma_avg_liquidity_usd": round(self.gamma_avg_liquidity_usd, 4),
            "gamma_avg_volume_24h_usd": round(self.gamma_avg_volume_24h_usd, 4),
            "fresh_news_signals": self.fresh_news_signals,
            "fresh_brain_approvals": self.fresh_brain_approvals,
            "warnings": list(self.warnings),
        }


class MarketIntelligence:
    """Read-only live market context for the allocator.

    The class intentionally uses public HTTP endpoints and short timeouts. If a
    feed fails, allocation still proceeds from local DB evidence.
    """

    COINBASE_SYMBOLS = {
        "btc": "BTC-USD",
        "eth": "ETH-USD",
        "sol": "SOL-USD",
        "xrp": "XRP-USD",
    }

    def __init__(self, timeout_sec: float = 4.0):
        self.timeout_sec = timeout_sec

    def snapshot(
        self,
        *,
        poly_conn: Optional[sqlite3.Connection] = None,
        since_iso: Optional[str] = None,
    ) -> MarketIntelligenceSnapshot:
        snap = MarketIntelligenceSnapshot()
        self._load_crypto_prices(snap)
        self._load_gamma_crypto_liquidity(snap)
        self._load_local_signal_counts(snap, poly_conn, since_iso)
        return snap

    def _load_crypto_prices(self, snap: MarketIntelligenceSnapshot) -> None:
        for asset, symbol in self.COINBASE_SYMBOLS.items():
            try:
                url = f"https://api.coinbase.com/v2/prices/{symbol}/spot"
                payload = self._json_get(url)
                price = float(payload["data"]["amount"])
                snap.crypto[asset] = {
                    "price": price,
                    "fresh": True,
                }
            except Exception as exc:
                snap.crypto[asset] = {"price": None, "fresh": False}
                snap.warnings.append(f"coinbase_{asset}_failed:{exc}")

    def _load_gamma_crypto_liquidity(self, snap: MarketIntelligenceSnapshot) -> None:
        try:
            params = urllib.parse.urlencode({
                "tag_id": "21",
                "active": "true",
                "closed": "false",
                "limit": "100",
                "order": "endDate",
                "ascending": "true",
            })
            events = self._json_get(
                f"https://gamma-api.polymarket.com/events?{params}"
            )
            liquidities: list[float] = []
            volumes: list[float] = []
            for event in events if isinstance(events, list) else []:
                for market in event.get("markets") or []:
                    slug = str(market.get("slug") or event.get("slug") or "")
                    if "updown-15m" not in slug and "bitcoin-up-or-down" not in slug:
                        continue
                    liquidity = _safe_float(
                        market.get("liquidity")
                        or market.get("liquidityNum")
                        or market.get("liquidityClob")
                    )
                    volume = _safe_float(
                        market.get("volume24hr")
                        or market.get("volume24hrClob")
                        or market.get("volume")
                    )
                    if liquidity is not None:
                        liquidities.append(liquidity)
                    if volume is not None:
                        volumes.append(volume)
            snap.gamma_crypto_markets = len(liquidities) or len(volumes)
            snap.gamma_avg_liquidity_usd = (
                sum(liquidities) / len(liquidities) if liquidities else 0.0
            )
            snap.gamma_avg_volume_24h_usd = (
                sum(volumes) / len(volumes) if volumes else 0.0
            )
        except Exception as exc:
            snap.warnings.append(f"gamma_crypto_failed:{exc}")

    def _load_local_signal_counts(
        self,
        snap: MarketIntelligenceSnapshot,
        conn: Optional[sqlite3.Connection],
        since_iso: Optional[str],
    ) -> None:
        if conn is None or not since_iso:
            return
        try:
            if _table_exists(conn, "news_signals"):
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM news_signals WHERE ts >= ?",
                    (since_iso,),
                ).fetchone()
                snap.fresh_news_signals = int(row["n"] or 0) if row else 0
            if _table_exists(conn, "brain_decisions"):
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS n
                    FROM brain_decisions
                    WHERE ts >= ? AND approved = 1
                    """,
                    (since_iso,),
                ).fetchone()
                snap.fresh_brain_approvals = int(row["n"] or 0) if row else 0
        except sqlite3.Error as exc:
            snap.warnings.append(f"local_signal_count_failed:{exc}")

    def _json_get(self, url: str) -> object:
        req = urllib.request.Request(url, headers={"User-Agent": "poly1-capital-allocator/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
            return json.loads(resp.read())


def _safe_float(value) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class CapitalAllocator:
    """Score agents from DB history and recommend budget splits."""

    def __init__(
        self,
        poly_db: str = DEFAULT_POLY_DB,
        swarm_db: str = DEFAULT_SWARM_DB,
        total_budget_usdc: float = 20.0,
        window_hours: float = 24.0,
        min_allocation_usdc: float = 0.0,
        max_allocation_usdc: Optional[float] = None,
        include_market_intelligence: bool = True,
        market_intelligence: Optional[MarketIntelligence] = None,
    ):
        self.poly_db = poly_db
        self.swarm_db = swarm_db
        self.total_budget_usdc = float(total_budget_usdc)
        self.window_hours = float(window_hours)
        self.min_allocation_usdc = float(min_allocation_usdc)
        self.max_allocation_usdc = (
            float(max_allocation_usdc)
            if max_allocation_usdc is not None
            else self.total_budget_usdc
        )
        self.include_market_intelligence = include_market_intelligence
        self.market_intelligence = market_intelligence or MarketIntelligence()

    def build_report(self) -> AllocationReport:
        since = _utc_now() - timedelta(hours=self.window_hours)
        since_iso = since.isoformat()
        since_ms = int(since.timestamp() * 1000)
        warnings: list[str] = []
        intelligence = MarketIntelligenceSnapshot()
        scores: dict[str, AgentScore] = {
            "trader": AgentScore("trader", "poly1"),
            "btc_daily": AgentScore("btc_daily", "poly1"),
            "scalper": AgentScore("scalper", "poly1"),
            "position_manager": AgentScore("position_manager", "poly1"),
            "swarm_market_maker": AgentScore("swarm_market_maker", "swarm"),
            "swarm_mean_reversion": AgentScore("swarm_mean_reversion", "swarm"),
            "swarm_nothing_happens": AgentScore("swarm_nothing_happens", "swarm"),
            "swarm_ai_decision": AgentScore("swarm_ai_decision", "swarm"),
            "swarm_arbitrage": AgentScore("swarm_arbitrage", "swarm"),
        }

        poly = _connect_ro(self.poly_db)
        if poly is None:
            warnings.append(f"missing poly db: {self.poly_db}")
        else:
            try:
                if self.include_market_intelligence:
                    intelligence = self.market_intelligence.snapshot(
                        poly_conn=poly,
                        since_iso=since_iso,
                    )
                self._read_poly(poly, since_iso, scores)
            finally:
                poly.close()

        swarm = _connect_ro(self.swarm_db)
        if swarm is None:
            warnings.append(f"missing swarm db: {self.swarm_db}")
        else:
            try:
                self._read_swarm(swarm, since_ms, scores)
            finally:
                swarm.close()

        for score in scores.values():
            self._apply_market_intelligence(score, intelligence)
            self._score_agent(score)
        self._allocate(scores)

        ordered = sorted(
            scores.values(),
            key=lambda s: (s.recommended_usdc, s.score, s.entries, s.decisions),
            reverse=True,
        )
        return AllocationReport(
            generated_at=_utc_now().isoformat(),
            window_hours=self.window_hours,
            total_budget_usdc=self.total_budget_usdc,
            agents=ordered,
            warnings=warnings + intelligence.warnings,
            market_intelligence=intelligence.as_dict(),
        )

    def _read_poly(
        self,
        conn: sqlite3.Connection,
        since_iso: str,
        scores: dict[str, AgentScore],
    ) -> None:
        if _table_exists(conn, "trades"):
            rows = conn.execute(
                """
                SELECT status, market_id, side, price, size_usdc, error, response_json
                FROM trades
                WHERE ts >= ?
                """,
                (since_iso,),
            ).fetchall()
            for row in rows:
                status = str(row["status"] or "")
                agent = self._poly_agent_for_status(status)
                s = scores[agent]
                size = float(row["size_usdc"] or 0.0)
                if status in {"skipped_dry_run", "skipped_dedupe", "skipped_gate"}:
                    s.decisions += 1
                    if status in {"skipped_gate", "skipped_dedupe"}:
                        s.vetoes += 1
                elif status in {"filled", "submitted", "btc_daily_open", "scalper_leg", "near_resolution_open", "news_shock_open"}:
                    s.entries += 1
                    s.deployed_usdc += size
                    if "SHADOW" in str(row["error"] or ""):
                        s.decisions += 1
                elif status.startswith("closed_") or status in {"btc_daily_closed", "scalper_exit"}:
                    s.exits += 1
                    if status != "closed_dust":
                        s.deployed_usdc += size
                    # Real PnL when the close actually returned proceeds
                    # (size_usdc on closed_* rows is the proceeds, on
                    # `closed_*` from position_manager's live path; or paper
                    # on shadow rows). Shadow rows are skipped via error flag.
                    if "SHADOW" not in str(row["error"] or ""):
                        pnl = self._real_pnl_from_response(row["response_json"])
                        s.realized_pnl_usdc += pnl
                        if pnl > 0:
                            s.wins += 1
                        elif pnl < 0:
                            s.losses += 1
                    else:
                        s.paper_pnl_usdc += self._paper_pnl_from_response(row["response_json"])
                elif status in {"resolved_yes", "resolved_no", "resolved_loss"}:
                    # Resolution-sync wrote this row. size_usdc is the payout.
                    # Realized pnl per token is (payout - cost). The cost was
                    # already added to deployed_usdc when the FILLED row was
                    # processed; here we just credit realized PnL.
                    s.exits += 1
                    realized = self._realized_pnl_from_response(row["response_json"])
                    s.realized_pnl_usdc += realized
                    if realized > 0:
                        s.wins += 1
                    elif realized < 0:
                        s.losses += 1
                elif status in {"failed", "close_failed", "scalper_exit_failed"}:
                    s.errors += 1
                    s.deployed_usdc += size

        if _table_exists(conn, "brain_decisions"):
            rows = conn.execute(
                """
                SELECT agent, approved, reason
                FROM brain_decisions
                WHERE ts >= ?
                """,
                (since_iso,),
            ).fetchall()
            for row in rows:
                agent = str(row["agent"] or "")
                if agent not in scores:
                    continue
                s = scores[agent]
                s.decisions += 1
                if int(row["approved"] or 0) == 0:
                    s.vetoes += 1

        if _table_exists(conn, "scalper_pairs"):
            rows = conn.execute(
                """
                SELECT state, cost_up, cost_down, attempts_up, attempts_down
                FROM scalper_pairs
                """
            ).fetchall()
            s = scores["scalper"]
            for row in rows:
                state = str(row["state"] or "")
                attempts = int(row["attempts_up"] or 0) + int(row["attempts_down"] or 0)
                cost = float(row["cost_up"] or 0.0) + float(row["cost_down"] or 0.0)
                if attempts:
                    s.decisions += attempts
                if cost:
                    s.deployed_usdc += cost
                if state in {"reconcile_needed", "leg1_filled"}:
                    s.stale_state += 1

    def _read_swarm(
        self,
        conn: sqlite3.Connection,
        since_ms: int,
        scores: dict[str, AgentScore],
    ) -> None:
        if _table_exists(conn, "pending_orders"):
            rows = conn.execute(
                """
                SELECT agent, status, size_usd, note
                FROM pending_orders
                WHERE created_ms >= ?
                """,
                (since_ms,),
            ).fetchall()
            for row in rows:
                s = scores[self._swarm_agent_name(str(row["agent"] or ""))]
                status = str(row["status"] or "")
                size = float(row["size_usd"] or 0.0)
                s.decisions += 1
                if status in {"pending", "submitted", "filled"}:
                    s.entries += 1
                    s.deployed_usdc += size
                if status == "failed":
                    s.errors += 1
                if status == "submitted":
                    s.stale_state += 1

        if _table_exists(conn, "fills"):
            rows = conn.execute(
                """
                SELECT agent, price, size, fee
                FROM fills
                WHERE ts_ms >= ?
                """,
                (since_ms,),
            ).fetchall()
            for row in rows:
                s = scores[self._swarm_agent_name(str(row["agent"] or ""))]
                s.entries += 1
                price = float(row["price"] or 0.0)
                size = float(row["size"] or 0.0)
                s.deployed_usdc += (price / 100.0) * size

        if _table_exists(conn, "pnl_events"):
            rows = conn.execute(
                """
                SELECT agent, pnl
                FROM pnl_events
                WHERE ts_ms >= ?
                """,
                (since_ms,),
            ).fetchall()
            for row in rows:
                s = scores[self._swarm_agent_name(str(row["agent"] or ""))]
                s.exits += 1
                pnl = float(row["pnl"] or 0.0)
                s.realized_pnl_usdc += pnl
                if pnl > 0:
                    s.wins += 1
                elif pnl < 0:
                    s.losses += 1

        if _table_exists(conn, "nh_journal"):
            rows = conn.execute(
                """
                SELECT unrealized_pnl
                FROM nh_journal
                WHERE opened_at_ms >= ?
                """,
                (since_ms,),
            ).fetchall()
            s = scores["swarm_nothing_happens"]
            for row in rows:
                if row["unrealized_pnl"] is not None:
                    s.paper_pnl_usdc += float(row["unrealized_pnl"] or 0.0)

    def _score_agent(self, s: AgentScore) -> None:
        activity = math.log1p(s.decisions + s.entries)
        exit_credit = 0.4 * math.log1p(s.exits)
        # Realized PnL dominates paper PnL — paper is from speculative
        # `pnl_usdc_paper` shadow records and journal arithmetic, realized
        # is what actually moved cash. Cap at ±2.0 so a single big winner
        # doesn't paper-over a string of losses (or vice versa).
        # Asymmetric: losses penalize harder than wins reward, since the
        # cost of running a losing strategy live compounds with every cycle.
        realized_credit = max(-2.0, min(1.0, s.realized_pnl_usdc / 5.0))
        if s.realized_pnl_usdc < -1.0:
            # Hard defund: persistent realized loss → cap allowed.
            realized_credit -= 1.0
        paper_credit = max(-0.5, min(0.5, s.paper_pnl_usdc / 5.0))
        pnl_credit = realized_credit + paper_credit
        error_penalty = 0.45 * s.errors
        stale_penalty = 0.55 * s.stale_state
        veto_penalty = 0.06 * s.vetoes
        s.score = max(
            0.0,
            activity
            + exit_credit
            + pnl_credit
            + s.market_score
            - error_penalty
            - stale_penalty
            - veto_penalty,
        )

        if s.entries == 0 and s.decisions == 0:
            s.reasons.append("no_recent_signal")
        if s.errors:
            s.reasons.append(f"errors={s.errors}")
        if s.stale_state:
            s.reasons.append(f"stale_state={s.stale_state}")
        if s.vetoes and s.entries == 0:
            s.reasons.append(f"veto_only={s.vetoes}")
        if s.realized_pnl_usdc > 0.05:
            s.reasons.append(f"realized_pnl=+${s.realized_pnl_usdc:.2f}")
        elif s.realized_pnl_usdc < -0.05:
            s.reasons.append(f"realized_pnl=-${abs(s.realized_pnl_usdc):.2f}")
        if s.paper_pnl_usdc > 0:
            s.reasons.append("positive_paper_pnl")
        if s.exits:
            s.reasons.append("exit_path_observed")
        if s.market_score > 0:
            s.reasons.append(f"market_context=+{s.market_score:.2f}")

        entry_strategy = s.agent != "position_manager"
        # B2-mini (2026-05-08): exclude swarm_arbitrage from being a
        # tradable entry strategy. The agent is a stub that only logs
        # `agent.arb_candidate` events; it never places orders. Giving
        # it an exploration_floor allocation would waste $1.50 of the
        # $20 budget on a no-op. Building a real arbitrage trader is
        # tracked separately and gated on D3 (backtest harness).
        if s.agent == "swarm_arbitrage":
            entry_strategy = False
            s.reasons.append("stub_no_orders")
        has_constructive_signal = s.entries > 0 or (
            s.decisions >= 3 and s.vetoes == 0
        )
        # Hard defund: an agent that's bled more than $2 of realized PnL
        # in the window doesn't get live capital regardless of activity.
        # The threshold is intentionally low for the $20 experiment — a
        # single $2 loss is 10% of total budget. Tune via env if needed.
        defund_floor = float(os.getenv("ALLOCATOR_DEFUND_FLOOR_USDC", "-2.0"))
        bleeding = s.realized_pnl_usdc < defund_floor
        if bleeding:
            s.reasons.append(f"defund_bleeding=${s.realized_pnl_usdc:.2f}")

        # Exploration mode: when enabled, every entry-strategy agent that
        # isn't actively bleeding or erroring gets a minimum allocation
        # (`ALLOCATOR_EXPLORATION_USDC`, default $0). Lets the operator
        # parallelize learning across all agents instead of waiting for
        # `has_constructive_signal` to flip from data the agent can't
        # produce while at $0. Set env to ~$1.50 to give each agent a
        # toehold; allocator's score math still drives the proportional
        # split above the floor.
        exploration_floor = float(os.getenv("ALLOCATOR_EXPLORATION_USDC", "0.0"))
        exploration_eligible = (
            exploration_floor > 0
            and entry_strategy
            and s.errors == 0
            and s.stale_state == 0
            and not bleeding
        )

        s.live_allowed = (
            entry_strategy
            and (has_constructive_signal or exploration_eligible)
            and s.score >= 0
            and s.errors == 0
            and s.stale_state == 0
            and not bleeding
        )
        # Stash exploration eligibility so `_allocate` can apply the floor.
        s._exploration_only = exploration_eligible and not has_constructive_signal
        if exploration_eligible and not has_constructive_signal:
            s.reasons.append("exploration_mode")
        if not entry_strategy:
            s.reasons.append("exit_only_no_entry_budget")
        if not s.live_allowed and "no_live_until_clean" not in s.reasons:
            s.reasons.append("no_live_until_clean")

    def _apply_market_intelligence(
        self,
        s: AgentScore,
        intelligence: MarketIntelligenceSnapshot,
    ) -> None:
        if not self.include_market_intelligence:
            return
        if s.agent in {"scalper", "btc_daily", "swarm_mean_reversion"}:
            fresh_crypto = sum(
                1 for data in intelligence.crypto.values() if data.get("fresh")
            )
            if fresh_crypto:
                s.market_score += min(0.35, 0.07 * fresh_crypto)
            if intelligence.gamma_avg_liquidity_usd >= 10_000:
                s.market_score += 0.20
            if intelligence.gamma_avg_volume_24h_usd >= 5_000:
                s.market_score += 0.15
        if s.agent in {"trader", "swarm_ai_decision", "swarm_nothing_happens"}:
            if intelligence.fresh_news_signals:
                s.market_score += min(0.30, 0.05 * intelligence.fresh_news_signals)
        if s.agent == "scalper" and intelligence.fresh_brain_approvals:
            s.market_score += min(0.25, 0.05 * intelligence.fresh_brain_approvals)

    def _allocate(self, scores: dict[str, AgentScore]) -> None:
        eligible = [s for s in scores.values() if s.live_allowed and s.score >= 0]
        if not eligible:
            return

        exploration_floor = float(os.getenv("ALLOCATOR_EXPLORATION_USDC", "0.0"))

        # Phase 1: pin the exploration floor for every eligible entry agent.
        # Every agent in eligible gets `exploration_floor` first; the rest is
        # distributed proportionally to score among agents with
        # `has_constructive_signal` (or score > 0 if floor==0).
        if exploration_floor > 0:
            floor_total = exploration_floor * len(eligible)
            if floor_total > self.total_budget_usdc:
                # Budget too tight — split it evenly, no proportional share.
                per_agent = self.total_budget_usdc / len(eligible)
                for s in eligible:
                    s.recommended_usdc = round(per_agent, 4)
                return
            for s in eligible:
                s.recommended_usdc = exploration_floor
            remaining_pool = self.total_budget_usdc - floor_total
        else:
            remaining_pool = self.total_budget_usdc

        # Phase 2: distribute the rest proportionally to score, but only
        # among agents that aren't `_exploration_only` (i.e., they have
        # actually demonstrated activity). If everyone is exploration-only,
        # the floor was already applied and there's no proportional share.
        proven = [s for s in eligible
                  if not getattr(s, "_exploration_only", False) and s.score > 0]
        if not proven or remaining_pool <= 0:
            return
        total_score = sum(s.score for s in proven)
        for s in proven:
            extra = remaining_pool * (s.score / total_score)
            raw = s.recommended_usdc + extra
            s.recommended_usdc = min(self.max_allocation_usdc, max(self.min_allocation_usdc, raw))

        spent = sum(s.recommended_usdc for s in eligible)
        if spent > self.total_budget_usdc + 0.0001:
            scale = self.total_budget_usdc / spent
            for s in eligible:
                s.recommended_usdc *= scale

    @staticmethod
    def _poly_agent_for_status(status: str) -> str:
        if status.startswith("btc_daily"):
            return "btc_daily"
        if status.startswith("scalper"):
            return "scalper"
        if status.startswith("near_resolution"):
            return "near_resolution"
        if status.startswith("news_shock"):
            return "news_shock"
        if status.startswith("closed_") or status == "close_failed":
            return "position_manager"
        return "trader"

    @staticmethod
    def _swarm_agent_name(agent: str) -> str:
        mapping = {
            "market_maker": "swarm_market_maker",
            "mean_reversion": "swarm_mean_reversion",
            "nothing_happens": "swarm_nothing_happens",
            "ai_decision": "swarm_ai_decision",
            "arbitrage": "swarm_arbitrage",
        }
        return mapping.get(agent, f"swarm_{agent}" if agent else "swarm_unknown")

    @staticmethod
    def _paper_pnl_from_response(response_json: Optional[str]) -> float:
        if not response_json:
            return 0.0
        try:
            payload = json.loads(response_json)
        except (TypeError, ValueError):
            return 0.0
        for key in ("pnl_usdc_paper", "pnl_usdc", "pnl"):
            if key in payload:
                try:
                    return float(payload[key])
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    @staticmethod
    def _real_pnl_from_response(response_json: Optional[str]) -> float:
        """Extract realized PnL from a closed_* row's response_json.

        Live close rows from position_manager / btc_daily / exit_executor
        store realized PnL under `pnl_usdc_real`. Falls back to legacy
        `pnl_usdc_paper` only if the response is from an old shadow row,
        which the caller should already have filtered.
        """
        if not response_json:
            return 0.0
        try:
            payload = json.loads(response_json)
        except (TypeError, ValueError):
            return 0.0
        for key in ("pnl_usdc_real", "realized_pnl_usdc", "pnl_usdc"):
            if key in payload:
                try:
                    return float(payload[key])
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    @staticmethod
    def _realized_pnl_from_response(response_json: Optional[str]) -> float:
        """Extract realized PnL from a resolution row (resolved_yes / no /
        loss). resolution_sync writes `realized_pnl_usdc` directly."""
        if not response_json:
            return 0.0
        try:
            payload = json.loads(response_json)
        except (TypeError, ValueError):
            return 0.0
        if "realized_pnl_usdc" in payload:
            try:
                return float(payload["realized_pnl_usdc"])
            except (TypeError, ValueError):
                return 0.0
        return 0.0
