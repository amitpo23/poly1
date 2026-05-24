"""Multi-source consensus router for entry decisions.

Architectural intent (per the operator's 2-source-agreement design):
when 2+ distinct entry agents approve the same market within a short
window, scanner_executor uses relaxed gates because the cross-source
confirmation is itself evidence of quality. Without consensus, the
strict gates apply.

This module is a pure read-only query against `brain_decisions`. It
does not write, schedule, or trigger anything itself. scanner_executor
calls `query()` per candidate market and uses the result to choose
between strict and relaxed gate thresholds.

Empirical caveat (2026-05-24 feasibility analysis): the current entry
agents have non-overlapping market scopes (market_scanner = general,
btc_5min = BTC 5min, scalper = its pairs, opportunity_factory = wallet/
insider, external_conviction_* = shadow on crypto news). So consensus
events will be RARE until the agent coverage is realigned. The router
is built first so the infrastructure is in place; coverage rebalancing
follows in subsequent work.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional


DEFAULT_WINDOW_SEC = 300  # 5 minutes
DEFAULT_MIN_AGENTS = 2
DEFAULT_EXCLUDED_AGENTS = (
    # scanner_executor is the consumer; its own approvals don't add
    # confirmation. position_manager evaluates EXITS, not entries.
    "scanner_executor",
    "position_manager",
    "position_manager_llm",
)


@dataclass(frozen=True)
class ConsensusResult:
    """The shape `scanner_executor` reads.

    `consensus`: True iff >= `min_agents` distinct non-excluded agents
    approved this market_id within the window.

    `agents`: sorted list of distinct agent names contributing to the
    count. Useful for logging and decision_journal features.

    `actions`: sorted list of distinct actions (BUY/SELL/etc.) seen
    across the contributing approvals. Single-element when all agents
    agree directionally; multi-element when sources disagree on side.

    `signal_sources`: distinct signal_source strings from the approvals
    (for downstream tagging).
    """

    consensus: bool
    agents: tuple[str, ...]
    actions: tuple[str, ...]
    signal_sources: tuple[str, ...]
    window_seconds: int
    min_agents_required: int

    def as_features(self) -> dict:
        return {
            "consensus": self.consensus,
            "consensus_agent_count": len(self.agents),
            "consensus_agents": list(self.agents),
            "consensus_actions": list(self.actions),
            "consensus_sources": list(self.signal_sources),
            "consensus_window_sec": self.window_seconds,
            "consensus_min_agents": self.min_agents_required,
            "consensus_directional_agreement": len(self.actions) == 1,
        }


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def query(
    db: "sqlite3.Connection | str",
    market_id: str,
    *,
    now: Optional[datetime] = None,
    window_seconds: Optional[int] = None,
    min_agents: Optional[int] = None,
    excluded_agents: Optional[tuple[str, ...]] = None,
) -> ConsensusResult:
    """Check whether multiple entry agents have approved the given
    market within the window ending at `now` (UTC).

    Reads `brain_decisions` rows with:
    - market_id matching
    - approved = 1
    - agent NOT IN excluded_agents (default: scanner_executor + position_manager*)
    - ts within [now - window, now]

    Returns a `ConsensusResult` (never None).

    `db` is either a `sqlite3.Connection` (caller-owned) or a path string.
    If a path is given, the function opens a short-lived read-only
    connection. Both forms work; the path form is the simplest for
    runtime callers that don't already hold a connection.
    """
    if window_seconds is None:
        window_seconds = _env_int(
            "CONSENSUS_WINDOW_SECONDS", DEFAULT_WINDOW_SEC
        )
    if min_agents is None:
        min_agents = _env_int(
            "CONSENSUS_MIN_AGENTS", DEFAULT_MIN_AGENTS
        )
    if excluded_agents is None:
        excluded_agents = DEFAULT_EXCLUDED_AGENTS

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    cutoff = (now - timedelta(seconds=window_seconds)).isoformat()
    upper = now.isoformat()

    placeholders = ",".join("?" for _ in excluded_agents)
    sql = (
        "SELECT DISTINCT agent, action, signal_source "
        "FROM brain_decisions "
        "WHERE market_id = ? "
        "  AND approved = 1 "
        f"  AND agent NOT IN ({placeholders}) "
        "  AND ts >= ? AND ts <= ?"
    )

    params: list = [str(market_id), *excluded_agents, cutoff, upper]
    opened_here = False
    if isinstance(db, str):
        try:
            db = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            opened_here = True
        except sqlite3.Error:
            return ConsensusResult(
                consensus=False,
                agents=tuple(),
                actions=tuple(),
                signal_sources=tuple(),
                window_seconds=window_seconds,
                min_agents_required=min_agents,
            )
    try:
        try:
            rows = db.execute(sql, params).fetchall()
        except sqlite3.Error:
            # A failed read should not bring down scanner_executor —
            # fall back to "no consensus" so the strict-gate path applies.
            return ConsensusResult(
                consensus=False,
                agents=tuple(),
                actions=tuple(),
                signal_sources=tuple(),
                window_seconds=window_seconds,
                min_agents_required=min_agents,
            )
    finally:
        if opened_here:
            db.close()

    agents = sorted({r[0] for r in rows if r and r[0]})
    actions = sorted({r[1] for r in rows if r and r[1]})
    signal_sources = sorted({r[2] for r in rows if r and r[2]})

    return ConsensusResult(
        consensus=len(agents) >= min_agents,
        agents=tuple(agents),
        actions=tuple(actions),
        signal_sources=tuple(signal_sources),
        window_seconds=window_seconds,
        min_agents_required=min_agents,
    )
