"""Offline reward dataset builder for shadow trading decisions.

This module deliberately does not train or execute a live model.  It converts
the evidence we already collect (decision_journal markouts, brain decisions and
orderbook-derived features) into JSONL rows that can later feed TensorTrade,
FinRL, or a smaller custom policy learner.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


ACTION_MAP = {
    "SHADOW_ENTER": "enter",
    "LIVE_ENTER": "enter",
    "ENTER": "enter",
    "APPROVE": "enter",
    "SHADOW_QUOTE": "quote",
    "REJECT": "skip",
    "SKIP": "skip",
}


@dataclass(frozen=True)
class RewardConfig:
    preferred_horizon_minutes: int = 5
    round_trip_cost_pct: float = 0.04
    spread_free_pct: float = 0.02
    thin_depth_usdc: float = 20.0
    thin_depth_penalty: float = 0.015
    reentry_cooldown_hours: float = 12.0
    reentry_penalty: float = 0.025
    slow_hold_penalty_per_minute: float = 0.0005
    take_profit_bonus: float = 0.02
    stop_loss_penalty: float = 0.03


@dataclass(frozen=True)
class RLDatasetRow:
    journal_id: int
    ts: str
    agent: str
    strategy: str
    market_id: str
    token_id: Optional[str]
    action: str
    reward: float
    reward_components: dict[str, Any]
    observation: dict[str, Any]
    target: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_reward_dataset(
    db_path: str,
    *,
    limit: int = 5000,
    cfg: Optional[RewardConfig] = None,
) -> dict[str, Any]:
    """Build an offline reward dataset from decision_journal rows."""

    cfg = cfg or RewardConfig()
    path = Path(db_path)
    if not path.exists():
        return {"rows": [], "summary": {"reason": "db_missing", "row_count": 0}}
    with sqlite3.connect(str(path), timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "decision_journal"):
            return {"rows": [], "summary": {"reason": "no_decision_journal_table", "row_count": 0}}
        rows = conn.execute(
            """
            SELECT dj.*, bd.market_type AS brain_market_type, bd.asset AS brain_asset,
                   bd.features_json AS brain_features_json
            FROM decision_journal dj
            LEFT JOIN brain_decisions bd ON bd.id = dj.decision_id
            ORDER BY dj.id ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        prepared = [_row_to_dataset(conn, dict(row), cfg) for row in rows]
    dataset_rows = [row for row in prepared if row is not None]
    return {
        "rows": [row.to_dict() for row in dataset_rows],
        "summary": summarize_dataset(dataset_rows),
        "config": asdict(cfg),
    }


def write_reward_dataset(
    db_path: str,
    out_path: str,
    *,
    summary_path: Optional[str] = None,
    limit: int = 5000,
    cfg: Optional[RewardConfig] = None,
) -> dict[str, Any]:
    payload = build_reward_dataset(db_path, limit=limit, cfg=cfg)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for row in payload["rows"]:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    if summary_path:
        summary = Path(summary_path)
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_text(
            json.dumps(
                {
                    "summary": payload["summary"],
                    "config": payload.get("config", {}),
                    "output": str(out),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    return payload


def summarize_dataset(rows: list[RLDatasetRow]) -> dict[str, Any]:
    by_agent: dict[str, dict[str, Any]] = {}
    by_action: dict[str, int] = {}
    rewards = [row.reward for row in rows]
    for row in rows:
        by_action[row.action] = by_action.get(row.action, 0) + 1
        item = by_agent.setdefault(
            row.agent,
            {"rows": 0, "avg_reward": 0.0, "positive": 0, "negative": 0},
        )
        item["rows"] += 1
        item["avg_reward"] += row.reward
        if row.reward > 0:
            item["positive"] += 1
        elif row.reward < 0:
            item["negative"] += 1
    for item in by_agent.values():
        if item["rows"]:
            item["avg_reward"] = round(item["avg_reward"] / item["rows"], 6)
    return {
        "row_count": len(rows),
        "avg_reward": round(sum(rewards) / len(rewards), 6) if rewards else None,
        "positive_rows": sum(1 for value in rewards if value > 0),
        "negative_rows": sum(1 for value in rewards if value < 0),
        "by_action": by_action,
        "by_agent": by_agent,
    }


def _row_to_dataset(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    cfg: RewardConfig,
) -> Optional[RLDatasetRow]:
    action = ACTION_MAP.get(str(row.get("decision") or "").upper(), "unknown")
    markout, markout_payload = _best_markout(row, cfg.preferred_horizon_minutes)
    if markout is None:
        return None
    features = _json(row.get("features_json"))
    brain_features = _json(row.get("brain_features_json"))
    reentries = _recent_same_market_count(conn, row, cfg)
    reward, components = _reward(row, features, markout, markout_payload, reentries, cfg)
    observation = _observation(row, features, brain_features, reentries)
    target = {
        "preferred_horizon_minutes": cfg.preferred_horizon_minutes,
        "markout": markout,
        "markout_payload": markout_payload,
    }
    return RLDatasetRow(
        journal_id=int(row["id"]),
        ts=str(row.get("ts") or ""),
        agent=str(row.get("agent") or ""),
        strategy=str(row.get("strategy") or ""),
        market_id=str(row.get("market_id") or ""),
        token_id=row.get("token_id"),
        action=action,
        reward=round(reward, 6),
        reward_components=components,
        observation=observation,
        target=target,
    )


def _reward(
    row: dict[str, Any],
    features: dict[str, Any],
    markout: float,
    markout_payload: dict[str, Any],
    reentries: int,
    cfg: RewardConfig,
) -> tuple[float, dict[str, Any]]:
    raw_ev = _float(row.get("raw_ev")) or 0.0
    net_ev = _float(row.get("net_ev")) or 0.0
    spread = _first_float(
        markout_payload.get("quoted_spread_pct"),
        features.get("spread_pct"),
        features.get("book_spread_pct"),
        features.get("execution_spread_pct"),
    )
    bid_depth = _first_float(markout_payload.get("bid_depth_usdc"), features.get("bid_depth_usdc"))
    ask_depth = _first_float(markout_payload.get("ask_depth_usdc"), features.get("ask_depth_usdc"))
    spread_penalty = max(0.0, (spread or 0.0) - cfg.spread_free_pct)
    depth_penalty = 0.0
    if bid_depth is not None and bid_depth < cfg.thin_depth_usdc:
        depth_penalty += cfg.thin_depth_penalty
    if ask_depth is not None and ask_depth < cfg.thin_depth_usdc:
        depth_penalty += cfg.thin_depth_penalty
    reentry_penalty = cfg.reentry_penalty if reentries > 0 else 0.0
    horizon = int(markout_payload.get("minutes") or cfg.preferred_horizon_minutes)
    slow_penalty = max(0, horizon - cfg.preferred_horizon_minutes) * cfg.slow_hold_penalty_per_minute
    tp_bonus = cfg.take_profit_bonus if markout_payload.get("hit_take_profit_5pct") else 0.0
    sl_penalty = cfg.stop_loss_penalty if markout_payload.get("hit_stop_3pct") else 0.0
    reward = (
        markout
        - cfg.round_trip_cost_pct
        - spread_penalty
        - depth_penalty
        - reentry_penalty
        - slow_penalty
        - sl_penalty
        + tp_bonus
        + min(0.02, max(-0.02, net_ev * 0.25))
        + min(0.01, max(-0.01, raw_ev * 0.10))
    )
    components = {
        "markout": round(markout, 6),
        "round_trip_cost_pct": -round(cfg.round_trip_cost_pct, 6),
        "spread_penalty": -round(spread_penalty, 6),
        "depth_penalty": -round(depth_penalty, 6),
        "reentry_penalty": -round(reentry_penalty, 6),
        "slow_hold_penalty": -round(slow_penalty, 6),
        "stop_loss_penalty": -round(sl_penalty, 6),
        "take_profit_bonus": round(tp_bonus, 6),
        "raw_ev_bonus": round(min(0.01, max(-0.01, raw_ev * 0.10)), 6),
        "net_ev_bonus": round(min(0.02, max(-0.02, net_ev * 0.25)), 6),
    }
    return reward, components


def _observation(
    row: dict[str, Any],
    features: dict[str, Any],
    brain_features: dict[str, Any],
    reentries: int,
) -> dict[str, Any]:
    return {
        "agent": row.get("agent"),
        "strategy": row.get("strategy"),
        "signal_source": row.get("signal_source"),
        "market_price": _float(row.get("market_price")),
        "live_entry_price": _float(row.get("live_entry_price")),
        "internal_probability": _float(row.get("internal_probability")),
        "raw_ev": _float(row.get("raw_ev")),
        "net_ev": _float(row.get("net_ev")),
        "score": _float(row.get("score")),
        "mode": row.get("mode"),
        "recent_same_market_entries": reentries,
        "brain_market_type": row.get("brain_market_type"),
        "brain_asset": row.get("brain_asset"),
        "features": _compact_features(features),
        "brain_features": _compact_features(brain_features),
    }


def _compact_features(features: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "question",
        "asset",
        "market_type",
        "book_quality",
        "spread_pct",
        "bid_depth_usdc",
        "ask_depth_usdc",
        "raw_ev",
        "net_ev",
        "internal_probability",
        "market_entry_price",
        "edge",
        "signal_sources",
        "openbb_symbol",
        "openbb_direction",
        "alpaca_symbol",
        "crypto_tape_symbol",
        "external_signal_strong",
    }
    return {key: features[key] for key in keep if key in features}


def _best_markout(row: dict[str, Any], preferred: int) -> tuple[Optional[float], dict[str, Any]]:
    candidates: list[tuple[int, dict[str, Any], float]] = []
    for minutes, column in (
        (1, "outcome_1m_json"),
        (3, "outcome_3m_json"),
        (5, "outcome_5m_json"),
        (15, "outcome_15m_json"),
        (60, "outcome_60m_json"),
    ):
        payload = _json(row.get(column))
        value = _markout_value(payload)
        if value is not None:
            candidates.append((abs(minutes - preferred), payload, value))
    if not candidates:
        return None, {}
    candidates.sort(key=lambda item: item[0])
    _distance, payload, value = candidates[0]
    return value, payload


def _markout_value(payload: dict[str, Any]) -> Optional[float]:
    for key in ("pnl_pct", "bid_markout_pct", "markout_pct", "return_pct", "price_change_pct"):
        if key in payload:
            return _float(payload[key])
    entry = _float(payload.get("entry_price"))
    mark = _float(payload.get("mark_price"))
    if entry and mark is not None:
        return (mark - entry) / entry
    return None


def _recent_same_market_count(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    cfg: RewardConfig,
) -> int:
    ts = _parse_ts(row.get("ts"))
    if ts is None:
        return 0
    cutoff = (ts - timedelta(hours=cfg.reentry_cooldown_hours)).isoformat()
    current_id = int(row.get("id") or 0)
    result = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM decision_journal
        WHERE market_id = ?
          AND id < ?
          AND ts >= ?
          AND decision IN ('SHADOW_ENTER', 'SHADOW_QUOTE', 'LIVE_ENTER', 'ENTER')
        """,
        (str(row.get("market_id") or ""), current_id, cutoff),
    ).fetchone()
    return int(result["c"] if result else 0)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _json(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_float(*values: Any) -> Optional[float]:
    for value in values:
        parsed = _float(value)
        if parsed is not None:
            return parsed
    return None


def _parse_ts(value: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None
