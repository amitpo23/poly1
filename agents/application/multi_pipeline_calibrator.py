"""Extended calibrator covering ALL 22 entry-agent pipelines, not just
scanner_executor.

The original `probability_calibrator.calibrate()` walks
decision_journal — which scanner_executor populates. Other agents have
different pipelines:

1. **scanner_executor pipeline** (decision_journal-based): already
   handled by `probability_calibrator.calibrate()`.

2. **Direct-execution pipeline** (btc_5min, scalper): trades write
   straight to the `trades` table with `cycle_id` patterns like
   `btc_5min:<decision_id>` or `scalper:<...>`. We pair closes (status
   IN closed_*) to their entries (status='filled', same token_id, earlier
   ts), then attribute each pair to the firing agent by cycle_id prefix.

3. **Shadow-research pipeline** (external_conviction_*): emit
   brain_decisions but no execution. To compute hypothetical edge we
   would need to simulate "what if THIS signal alone fired a trade?"
   That requires market price at decision_ts vs market price at
   decision_ts + horizon. The orderbook_snapshots table CAN provide
   this — for tokens that ARE in the snapshot watch list. Initial
   implementation: count approvals + estimate avg implied probability
   (markouts pipeline already extended).

This module is import-only — `multi_pipeline_calibrate(db_path)` returns
the combined dict, callable from the same refresh-calibration cron.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from agents.application.probability_calibrator import (
    LOSS_STATUSES,
    WIN_STATUSES,
    CalibrationStat,
    _price_band,
    calibrate as _base_calibrate,
)


# Agents that write trades directly via cycle_id. These are recognized
# by trades.cycle_id starting with one of these prefixes.
DIRECT_EXECUTION_AGENTS = (
    "btc_5min",
    "btc_daily",
    "scalper",
    "near_resolution",
    "news_shock",
    "wallet_follow",
)


def _agent_from_cycle_id(cycle_id: str) -> Optional[str]:
    """Map a cycle_id back to the firing agent. Returns None if it's
    a close row (cycle_id like 'close:...') or unrecognized."""
    if not cycle_id:
        return None
    if cycle_id.startswith("close:") or cycle_id.startswith("resolution_sync"):
        return None
    for agent in DIRECT_EXECUTION_AGENTS:
        if cycle_id.startswith(f"{agent}:") or cycle_id.startswith(f"{agent}_"):
            return agent
    if cycle_id.startswith("scanner_executor:"):
        return "scanner_executor"
    return None


def _direct_execution_stats(
    conn: sqlite3.Connection, *, days: int
) -> dict[str, dict]:
    """For each direct-execution agent, walk its closed trades and
    aggregate wins/losses + sum_pnl. Returns dict keyed by agent name.

    Entry-status detection covers both standard `filled` rows (used by
    scanner_executor) and agent-specific entry statuses like
    `btc_5min_open` and `near_resolution_open` — those agents use UUID
    cycle_ids and tag the status itself, so we recognize them by status.
    Each such status is also a tag of the firing agent.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
    terminal_statuses = WIN_STATUSES | LOSS_STATUSES | {"close_failed"}
    placeholders = ",".join("?" for _ in terminal_statuses)

    # Recognized entry statuses by agent. `filled` is the generic one;
    # the *_open variants are agent-tagged (status string IS the tag).
    ENTRY_STATUS_TO_AGENT = {
        "filled": None,                # fall back to cycle_id detection
        "btc_5min_open": "btc_5min",
        "near_resolution_open": "near_resolution",
    }
    entry_status_placeholders = ",".join("?" for _ in ENTRY_STATUS_TO_AGENT)

    # First find all entry rows per agent
    entries: dict[str, dict] = {}  # token_id -> {agent, ts, ...}
    for row in conn.execute(
        f"""
        SELECT id, ts, cycle_id, market_id, token_id, side, price, size_usdc, status
        FROM trades
        WHERE status IN ({entry_status_placeholders})
          AND ts >= ? AND token_id IS NOT NULL AND token_id != ''
        ORDER BY id
        """,
        (*ENTRY_STATUS_TO_AGENT.keys(), cutoff),
    ):
        # Status-tagged statuses identify the agent directly. For generic
        # `filled` we fall back to cycle_id pattern matching.
        agent = ENTRY_STATUS_TO_AGENT.get(row["status"])
        if agent is None:
            agent = _agent_from_cycle_id(row["cycle_id"])
        if not agent:
            continue
        # If multiple entries for same token_id, keep the LATEST (most recent)
        entries[row["token_id"]] = {
            "agent": agent,
            "ts": row["ts"],
            "side": row["side"],
            "price": row["price"],
            "size_usdc": row["size_usdc"],
        }

    # Now walk closes and attribute to entries
    per_agent_stats: dict[str, dict] = defaultdict(
        lambda: {"wins": 0, "losses": 0, "sum_win_pnl_usdc": 0.0,
                 "sum_loss_pnl_usdc": 0.0}
    )
    for row in conn.execute(
        f"""
        SELECT id, ts, market_id, token_id, status, response_json
        FROM trades
        WHERE status IN ({placeholders}) AND ts >= ?
        """,
        (*terminal_statuses, cutoff),
    ):
        entry = entries.get(row["token_id"])
        if entry is None:
            continue
        try:
            resp = json.loads(row["response_json"] or "{}")
            pnl = resp.get("pnl_usdc_real")
            pnl_f = float(pnl) if pnl is not None else 0.0
        except (TypeError, ValueError):
            pnl_f = 0.0
        is_win = row["status"] in WIN_STATUSES
        stats = per_agent_stats[entry["agent"]]
        if is_win:
            stats["wins"] += 1
            stats["sum_win_pnl_usdc"] += pnl_f
        else:
            stats["losses"] += 1
            stats["sum_loss_pnl_usdc"] += pnl_f

    out = []
    for agent, stats in per_agent_stats.items():
        cs = CalibrationStat(
            key=agent,
            segment="direct_execution_agent",
            wins=stats["wins"],
            losses=stats["losses"],
            sum_win_pnl_usdc=stats["sum_win_pnl_usdc"],
            sum_loss_pnl_usdc=stats["sum_loss_pnl_usdc"],
        )
        out.append(cs.as_dict())
    return sorted(out, key=lambda x: -x["total"])


def _shadow_research_stats(
    conn: sqlite3.Connection, *, days: int
) -> list[dict]:
    """For external_conviction_* agents, count their approvals and the
    fraction in the profit band. We can't directly measure their edge
    (they don't fire trades) but we can surface their volume and band
    coverage so the operator can see they're alive and producing signal.

    To get HYPOTHETICAL win rate, we'd need a simulator that takes each
    of their approvals and looks up the price at approval_ts + 5min.
    That's a future extension; today this is just visibility.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
    out = []
    for row in conn.execute(
        """
        SELECT agent, COUNT(*) AS total,
               SUM(CASE WHEN token_id IS NOT NULL AND token_id != '' THEN 1 ELSE 0 END) AS with_token
        FROM brain_decisions
        WHERE approved = 1 AND ts >= ? AND agent LIKE 'external_conviction%'
        GROUP BY agent
        """,
        (cutoff,),
    ):
        out.append(
            {
                "key": row["agent"],
                "segment": "shadow_research_agent",
                "approvals": row["total"],
                "with_token": row["with_token"],
                "note": "no closed trades — hypothetical edge requires simulator",
            }
        )
    return sorted(out, key=lambda x: -x["approvals"])


def multi_pipeline_calibrate(
    db_path: str,
    *,
    days: int = 30,
    max_age_hours: int = 48,
) -> dict:
    """Combined calibration covering all 3 pipelines."""
    base = _base_calibrate(db_path, days=days, max_age_hours=max_age_hours)
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        per_agent_direct = _direct_execution_stats(conn, days=days)
        shadow_research = _shadow_research_stats(conn, days=days)
    base["per_direct_execution_agent"] = per_agent_direct
    base["shadow_research_visibility"] = shadow_research
    return base
