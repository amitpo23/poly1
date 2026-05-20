"""Strategy and agent scorecards from decision_journal rows."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class StrategyScore:
    agent: str
    strategy: str
    decisions: int
    approvals: int
    rejects: int
    avg_score: float
    markout_samples: int
    avg_markout_pct: Optional[float]
    promotion_state: str
    blockers: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_strategy_scorecard(db_path: str, *, min_decisions: int = 50) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        return {"strategies": [], "strategy_count": 0, "reason": "db_missing"}
    with sqlite3.connect(str(path), timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "decision_journal"):
            return {"strategies": [], "strategy_count": 0, "reason": "no_decision_journal_table"}
        rows = conn.execute(
            """
            SELECT agent, strategy, decision, score,
                   outcome_1m_json, outcome_3m_json, outcome_5m_json, outcome_15m_json
            FROM decision_journal
            ORDER BY id DESC
            LIMIT 5000
            """
        ).fetchall()
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (str(row["agent"]), str(row["strategy"]))
        grouped.setdefault(key, []).append(row)
    scores = [_score_group(agent, strategy, group, min_decisions=min_decisions) for (agent, strategy), group in grouped.items()]
    scores.sort(key=lambda item: (item.promotion_state != "promotable", -item.decisions, item.agent))
    return {
        "strategies": [score.to_dict() for score in scores],
        "strategy_count": len(scores),
        "min_decisions": min_decisions,
    }


def write_strategy_scorecard(db_path: str, out_path: str, *, min_decisions: int = 50) -> dict[str, Any]:
    payload = build_strategy_scorecard(db_path, min_decisions=min_decisions)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def _score_group(agent: str, strategy: str, rows: list[sqlite3.Row], *, min_decisions: int) -> StrategyScore:
    decisions = len(rows)
    approvals = sum(1 for row in rows if str(row["decision"]).upper() in {"SHADOW_ENTER", "LIVE_ENTER", "ENTER", "APPROVE", "SHADOW_QUOTE"})
    rejects = sum(1 for row in rows if str(row["decision"]).upper() == "REJECT")
    score_values = [_float(row["score"]) for row in rows if _float(row["score"]) is not None]
    avg_score = sum(score_values) / len(score_values) if score_values else 0.0
    markouts = []
    for row in rows:
        for col in ("outcome_1m_json", "outcome_3m_json", "outcome_5m_json", "outcome_15m_json"):
            value = _markout(row[col])
            if value is not None:
                markouts.append(value)
    avg_markout = sum(markouts) / len(markouts) if markouts else None
    blockers = []
    if decisions < min_decisions:
        blockers.append("insufficient_decisions")
    if approvals <= 0:
        blockers.append("no_approved_candidates")
    if avg_markout is None:
        blockers.append("missing_markouts")
    elif avg_markout <= 0:
        blockers.append("non_positive_markout")
    state = "promotable" if not blockers else "shadow_only"
    return StrategyScore(
        agent=agent,
        strategy=strategy,
        decisions=decisions,
        approvals=approvals,
        rejects=rejects,
        avg_score=round(avg_score, 4),
        markout_samples=len(markouts),
        avg_markout_pct=None if avg_markout is None else round(avg_markout, 4),
        promotion_state=state,
        blockers=blockers,
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _markout(raw: Any) -> Optional[float]:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    for key in ("markout_pct", "pnl_pct", "return_pct", "price_change_pct"):
        if key in payload:
            return _float(payload[key])
    if "entry_price" in payload and "mark_price" in payload:
        entry = _float(payload["entry_price"])
        mark = _float(payload["mark_price"])
        if entry and mark is not None:
            return (mark - entry) / entry
    return None
