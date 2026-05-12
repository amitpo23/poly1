#!/usr/bin/env python3
"""Read-only /goal loop status for profitable trading agents.

This script does not place trades or mutate allocation. It reports whether each
approved agent has enough realized evidence to be considered profitable, and it
can run in a watch loop until all agents meet the goal.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


DEFAULT_POLY_DB = "./data/trade_log.db"
DEFAULT_SWARM_DB = os.path.expanduser("~/Desktop/poly/bot/data/swarm.db")

MIN_CLOSED_TRADES = 20

POLY_AGENT_STATUSES = {
    "trader": {
        "entries": {"filled", "submitted"},
        "exits": {"closed_take_profit", "closed_stop_loss", "closed_timeout", "resolved_loss"},
        "paper": {"skipped_dry_run"},
        "blockers": {"failed", "skipped_gate", "skipped_dedupe"},
    },
    "btc_daily": {
        "entries": {"btc_daily_open"},
        "exits": {"closed_take_profit", "closed_stop_loss", "closed_timeout", "resolved_loss"},
        "paper": set(),
        "blockers": {"failed", "skipped_gate", "skipped_dedupe"},
    },
    "scalper": {
        "entries": {"scalper_leg"},
        "exits": {"scalper_exit", "scalper_reconciled_lost"},
        "paper": {"scalper_leg"},
        "blockers": {"failed", "skipped_gate"},
    },
    "near_resolution": {
        "entries": {"near_resolution_open"},
        "exits": {"closed_take_profit", "closed_stop_loss", "closed_timeout", "resolved_loss"},
        "paper": set(),
        "blockers": {"failed", "skipped_gate"},
    },
    "news_shock": {
        "entries": {"news_shock_open"},
        "exits": {"closed_take_profit", "closed_stop_loss", "closed_timeout", "resolved_loss"},
        "paper": set(),
        "blockers": {"failed", "skipped_gate"},
    },
    "wallet_follow": {
        "entries": {"wallet_follow_open"},
        "exits": {"closed_take_profit", "closed_stop_loss", "closed_timeout", "resolved_loss"},
        "paper": set(),
        "blockers": {"failed", "skipped_gate"},
    },
}

SWARM_AGENTS = {
    "swarm_market_maker": "market_maker",
    "swarm_mean_reversion": "mean_reversion",
    "swarm_nothing_happens": "nothing_happens",
    "swarm_ai_decision": "ai_decision",
}


@dataclass
class AgentGoalStatus:
    agent: str
    state: str
    profitable: bool
    entries: int = 0
    closed: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl_usdc: float = 0.0
    avg_pnl_usdc: Optional[float] = None
    win_rate: Optional[float] = None
    blockers: list[str] = None
    next_action: str = ""

    def __post_init__(self) -> None:
        if self.blockers is None:
            self.blockers = []


def _connect(path: str) -> Optional[sqlite3.Connection]:
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


def _json_pnl(row: sqlite3.Row) -> float:
    raw = row["response_json"] if "response_json" in row.keys() else None
    if not raw:
        return 0.0
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return 0.0
    for key in ("pnl_usdc_real", "strategy_pnl_usdc", "pnl"):
        if key in payload:
            try:
                return float(payload[key])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _poly_agent_status(
    conn: Optional[sqlite3.Connection],
    agent: str,
    since_iso: str,
) -> AgentGoalStatus:
    cfg = POLY_AGENT_STATUSES[agent]
    if conn is None or not _table_exists(conn, "trades"):
        return AgentGoalStatus(
            agent=agent,
            state="research",
            profitable=False,
            blockers=["missing_poly_db"],
            next_action="restore trade_log.db visibility",
        )

    rows = conn.execute(
        """
        SELECT status, token_id, response_json, error
        FROM trades
        WHERE ts >= ?
        """,
        (since_iso,),
    ).fetchall()

    entries = 0
    closed = 0
    wins = 0
    losses = 0
    pnl = 0.0
    blocker_counts: dict[str, int] = {}
    paper_count = 0
    entry_tokens: set[str] = set()

    for row in rows:
        status = row["status"]
        token_id = str(row["token_id"] or "")
        error = str(row["error"] or "")
        if status in cfg["entries"]:
            entries += 1
            if status in cfg["paper"] or error.startswith("SHADOW"):
                paper_count += 1
            elif token_id:
                entry_tokens.add(token_id)

    for row in rows:
        status = row["status"]
        token_id = str(row["token_id"] or "")
        error = str(row["error"] or "")
        if status in cfg["exits"] and token_id and token_id in entry_tokens:
            closed += 1
            row_pnl = _json_pnl(row)
            pnl += row_pnl
            if row_pnl > 0:
                wins += 1
            elif row_pnl < 0 or status in {"closed_stop_loss", "resolved_loss"}:
                losses += 1

        # Generic failed/skipped rows currently belong to the main trader.
        # Specialized agents mostly emit their skips to logs, not the shared
        # trades table. Do not smear trader blockers across every agent.
        if agent == "trader" and status in cfg["blockers"]:
            key = status
            if "ai_filter_unavailable" in error or "ai_analysis_unavailable" in error:
                key = "ai_unavailable"
            elif "confidence" in error:
                key = "confidence_gate"
            elif "active trade" in error or "already holds" in error:
                key = "dedupe"
            blocker_counts[key] = blocker_counts.get(key, 0) + 1

    if agent == "news_shock" and _table_exists(conn, "news_signals"):
        row = conn.execute(
            """
            SELECT status, COUNT(*) AS n
            FROM news_signals
            WHERE ts >= ?
            GROUP BY status
            """,
            (since_iso,),
        ).fetchall()
        counts = {str(r["status"]): int(r["n"] or 0) for r in row}
        if counts.get("classifier_failed"):
            blocker_counts["classifier_failed"] = counts["classifier_failed"]
        if not counts.get("news_signal"):
            blocker_counts["no_actionable_news"] = 1

    if agent == "wallet_follow" and _table_exists(conn, "wallet_signals"):
        row = conn.execute(
            """
            SELECT status, COUNT(*) AS n
            FROM wallet_signals
            WHERE ts >= ?
            GROUP BY status
            """,
            (since_iso,),
        ).fetchall()
        counts = {str(r["status"]): int(r["n"] or 0) for r in row}
        if not counts.get("fresh"):
            blocker_counts["no_fresh_wallet_signals"] = 1

    blockers = [f"{k}:{v}" for k, v in sorted(blocker_counts.items())]
    avg_pnl = (pnl / closed) if closed else None
    win_rate = (wins / (wins + losses)) if (wins + losses) else None
    profitable = closed >= MIN_CLOSED_TRADES and pnl > 0 and (avg_pnl or 0) > 0

    if profitable:
        state = "profitable"
        next_action = "eligible for cautious scale-up review"
    elif entries > 0 and closed < MIN_CLOSED_TRADES:
        state = "live_probe" if paper_count < entries else "paper"
        next_action = f"collect more closed samples ({closed}/{MIN_CLOSED_TRADES})"
    elif paper_count > 0:
        state = "paper"
        next_action = "validate paper EV before live_probe"
    else:
        state = "backtest" if blockers else "research"
        next_action = "fix blockers and produce EV-positive candidates"

    return AgentGoalStatus(
        agent=agent,
        state=state,
        profitable=profitable,
        entries=entries,
        closed=closed,
        wins=wins,
        losses=losses,
        realized_pnl_usdc=round(pnl, 4),
        avg_pnl_usdc=round(avg_pnl, 4) if avg_pnl is not None else None,
        win_rate=round(win_rate, 4) if win_rate is not None else None,
        blockers=blockers,
        next_action=next_action,
    )


def _swarm_agent_status(
    conn: Optional[sqlite3.Connection],
    public_name: str,
    swarm_agent: str,
    since_ms: int,
) -> AgentGoalStatus:
    if conn is None:
        return AgentGoalStatus(
            agent=public_name,
            state="research",
            profitable=False,
            blockers=["missing_swarm_db"],
            next_action="restore swarm DB visibility",
        )

    fills = 0
    stale = 0
    pnl = 0.0
    closed = 0
    wins = 0
    losses = 0

    if _table_exists(conn, "fills"):
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM fills
            WHERE ts_ms >= ? AND agent = ?
            """,
            (since_ms, swarm_agent),
        ).fetchone()
        fills = int(row["n"] or 0) if row else 0

    if _table_exists(conn, "pending_orders"):
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM pending_orders
            WHERE created_ms >= ? AND agent = ?
              AND status IN ('submitted', 'may_have_fired', 'failed')
            """,
            (since_ms, swarm_agent),
        ).fetchone()
        stale = int(row["n"] or 0) if row else 0

    if _table_exists(conn, "pnl_events"):
        rows = conn.execute(
            """
            SELECT pnl
            FROM pnl_events
            WHERE ts_ms >= ? AND agent = ?
            """,
            (since_ms, swarm_agent),
        ).fetchall()
        closed = len(rows)
        for row in rows:
            row_pnl = float(row["pnl"] or 0.0)
            pnl += row_pnl
            if row_pnl > 0:
                wins += 1
            elif row_pnl < 0:
                losses += 1

    avg_pnl = (pnl / closed) if closed else None
    win_rate = (wins / (wins + losses)) if (wins + losses) else None
    profitable = closed >= MIN_CLOSED_TRADES and pnl > 0 and (avg_pnl or 0) > 0
    blockers = []
    if stale:
        blockers.append(f"stale_or_failed_orders:{stale}")
    if fills and closed < MIN_CLOSED_TRADES:
        blockers.append(f"insufficient_closed_sample:{closed}/{MIN_CLOSED_TRADES}")

    if profitable:
        state = "profitable"
        next_action = "eligible for cautious scale-up review"
    elif fills:
        state = "live_probe"
        next_action = "add/verify exit and settlement PnL, then collect sample"
    elif stale:
        state = "paper"
        next_action = "clear stale order blocker before next probe"
    else:
        state = "research"
        next_action = "prove positive EV before capital"

    return AgentGoalStatus(
        agent=public_name,
        state=state,
        profitable=profitable,
        entries=fills,
        closed=closed,
        wins=wins,
        losses=losses,
        realized_pnl_usdc=round(pnl, 4),
        avg_pnl_usdc=round(avg_pnl, 4) if avg_pnl is not None else None,
        win_rate=round(win_rate, 4) if win_rate is not None else None,
        blockers=blockers,
        next_action=next_action,
    )


def build_report(poly_db: str, swarm_db: str, hours: float) -> list[AgentGoalStatus]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    since_iso = since.isoformat()
    since_ms = int(since.timestamp() * 1000)

    poly = _connect(poly_db)
    swarm = _connect(swarm_db)
    try:
        statuses = [
            _poly_agent_status(poly, agent, since_iso)
            for agent in POLY_AGENT_STATUSES.keys()
        ]
        statuses.extend(
            _swarm_agent_status(swarm, public_name, swarm_agent, since_ms)
            for public_name, swarm_agent in SWARM_AGENTS.items()
        )
        return statuses
    finally:
        if poly is not None:
            poly.close()
        if swarm is not None:
            swarm.close()


def print_report(statuses: list[AgentGoalStatus], hours: float) -> None:
    generated = datetime.now(timezone.utc).isoformat()
    print("# /goal profitable-agent loop")
    print(f"generated_utc={generated}")
    print(f"window_hours={hours}")
    print(f"goal=all_approved_agents_profitable_or_unfunded_until_proven")
    print("")
    for status in statuses:
        marker = "OK" if status.profitable else "LOOP"
        blockers = ", ".join(status.blockers) if status.blockers else "none"
        avg = "n/a" if status.avg_pnl_usdc is None else f"{status.avg_pnl_usdc:+.4f}"
        wr = "n/a" if status.win_rate is None else f"{status.win_rate:.1%}"
        print(
            f"{marker:4s} {status.agent:22s} state={status.state:11s} "
            f"entries={status.entries:3d} closed={status.closed:3d} "
            f"pnl=${status.realized_pnl_usdc:+.4f} avg={avg} win_rate={wr}"
        )
        print(f"     blockers={blockers}")
        print(f"     next={status.next_action}")
    print("")
    remaining = [s.agent for s in statuses if not s.profitable]
    if remaining:
        print("goal_status=open")
        print("remaining=" + ",".join(remaining))
    else:
        print("goal_status=complete")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--poly-db", default=DEFAULT_POLY_DB)
    parser.add_argument("--swarm-db", default=DEFAULT_SWARM_DB)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=900)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--require-all-profitable", action="store_true")
    args = parser.parse_args()

    while True:
        statuses = build_report(args.poly_db, args.swarm_db, args.hours)
        if args.json:
            print(json.dumps([asdict(s) for s in statuses], indent=2))
        else:
            print_report(statuses, args.hours)

        all_profitable = all(s.profitable for s in statuses)
        if not args.watch or all_profitable:
            if args.require_all_profitable and not all_profitable:
                return 2
            return 0
        time.sleep(max(1, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
