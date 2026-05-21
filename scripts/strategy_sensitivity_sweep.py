#!/usr/bin/env python3
"""Run parameter-sensitivity sweeps over shadow decision markouts.

This is a pre-live research tool.  It does not send orders and does not change
runtime control.  It asks: "If we had used different entry and exit thresholds
on the shadow decisions we already recorded, which settings would have looked
least bad or best?"
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional


HORIZONS = {
    1: "outcome_1m_json",
    3: "outcome_3m_json",
    5: "outcome_5m_json",
    15: "outcome_15m_json",
    60: "outcome_60m_json",
}


@dataclass(frozen=True)
class SweepConfig:
    min_score: float
    min_raw_ev: float
    min_net_ev: float
    max_entry_price: float
    horizon_min: int
    take_profit_pct: float
    stop_loss_pct: float
    max_samples: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SweepResult:
    rank_key: float
    config: SweepConfig
    candidates: int
    wins: int
    losses: int
    stopped: int
    take_profit: int
    avg_pnl_pct: Optional[float]
    total_pnl_per_100: Optional[float]
    winrate: Optional[float]
    agents: dict[str, int]
    strategies: dict[str, int]
    groups: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["config"] = self.config.to_dict()
        return payload


def load_rows(db_path: str, *, since: str = "", limit: int = 10000) -> list[dict[str, Any]]:
    where = "WHERE decision IN ('SHADOW_ENTER', 'SHADOW_QUOTE')"
    args: list[Any] = []
    if since:
        where += " AND ts >= ?"
        args.append(since)
    args.append(int(limit))
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_journal'"
        ).fetchone()
        if exists is None:
            return []
        rows = conn.execute(
            f"""
            SELECT id, ts, agent, strategy, decision, market_id, token_id,
                   live_entry_price, market_price, raw_ev, net_ev, score,
                   signal_source, features_json,
                   outcome_1m_json, outcome_3m_json, outcome_5m_json,
                   outcome_15m_json, outcome_60m_json
            FROM decision_journal
            {where}
            ORDER BY id DESC
            LIMIT ?
            """,
            args,
        ).fetchall()
    return [dict(row) for row in rows]


def default_configs(limit: int) -> list[SweepConfig]:
    scores = [0.50, 0.60, 0.70, 0.78, 0.85]
    raw_evs = [-0.02, 0.00, 0.02, 0.04]
    net_evs = [-0.02, 0.00, 0.02, 0.04, 0.06]
    prices = [0.45, 0.55, 0.65, 0.80, 0.95]
    horizons = [1, 3, 5, 15]
    take_profits = [0.015, 0.03, 0.05, 0.08, 0.10]
    stop_losses = [0.02, 0.03, 0.05, 0.07, 0.10]
    configs: list[SweepConfig] = []
    for score in scores:
        for raw_ev in raw_evs:
            for net_ev in net_evs:
                for price in prices:
                    for horizon in horizons:
                        for tp in take_profits:
                            for sl in stop_losses:
                                configs.append(
                                    SweepConfig(
                                        min_score=score,
                                        min_raw_ev=raw_ev,
                                        min_net_ev=net_ev,
                                        max_entry_price=price,
                                        horizon_min=horizon,
                                        take_profit_pct=tp,
                                        stop_loss_pct=sl,
                                        max_samples=0,
                                    )
                                )
                                if len(configs) >= limit:
                                    return configs
    return configs


def run_sweep(
    rows: list[dict[str, Any]],
    configs: list[SweepConfig],
    *,
    min_trades: int,
    group_by: str = "",
) -> dict[str, Any]:
    results = [_evaluate(rows, cfg, group_by=group_by) for cfg in configs]
    ranked = sorted(
        results,
        key=lambda r: (
            r.candidates >= min_trades,
            r.total_pnl_per_100 if r.total_pnl_per_100 is not None else -10**9,
            r.avg_pnl_pct if r.avg_pnl_pct is not None else -10**9,
            r.candidates,
        ),
        reverse=True,
    )
    viable = [r for r in ranked if r.candidates >= min_trades and (r.total_pnl_per_100 or 0.0) > 0]
    return {
        "rows_loaded": len(rows),
        "configs_tested": len(configs),
        "min_trades": min_trades,
        "group_by": group_by or None,
        "viable_count": len(viable),
        "top": [r.to_dict() for r in ranked[:25]],
        "best_viable": [r.to_dict() for r in viable[:10]],
        "top_groups": _top_groups(ranked[:25]),
    }


def _evaluate(rows: list[dict[str, Any]], cfg: SweepConfig, *, group_by: str = "") -> SweepResult:
    pnl_values: list[float] = []
    wins = losses = stopped = take_profit = 0
    agents: dict[str, int] = {}
    strategies: dict[str, int] = {}
    groups: dict[str, int] = {}
    for row in rows:
        if not _passes_entry(row, cfg):
            continue
        pnl = _pnl_for_horizon(row, cfg.horizon_min)
        if pnl is None:
            continue
        if pnl >= cfg.take_profit_pct:
            realized = cfg.take_profit_pct
            take_profit += 1
        elif pnl <= -cfg.stop_loss_pct:
            realized = -cfg.stop_loss_pct
            stopped += 1
        else:
            realized = pnl
        pnl_values.append(realized)
        if realized > 0:
            wins += 1
        elif realized < 0:
            losses += 1
        agent = str(row.get("agent") or "unknown")
        strategy = str(row.get("strategy") or "unknown")
        agents[agent] = agents.get(agent, 0) + 1
        strategies[strategy] = strategies.get(strategy, 0) + 1
        if group_by:
            group = _group_value(row, group_by)
            groups[group] = groups.get(group, 0) + 1
        if cfg.max_samples and len(pnl_values) >= cfg.max_samples:
            break
    total = len(pnl_values)
    avg = sum(pnl_values) / total if total else None
    total_per_100 = sum(pnl_values) * 100.0 if total else None
    winrate = wins / (wins + losses) if (wins + losses) else None
    rank_key = total_per_100 if total_per_100 is not None else -10**9
    return SweepResult(
        rank_key=rank_key,
        config=cfg,
        candidates=total,
        wins=wins,
        losses=losses,
        stopped=stopped,
        take_profit=take_profit,
        avg_pnl_pct=None if avg is None else round(avg, 6),
        total_pnl_per_100=None if total_per_100 is None else round(total_per_100, 4),
        winrate=None if winrate is None else round(winrate, 4),
        agents=dict(sorted(agents.items())),
        strategies=dict(sorted(strategies.items())),
        groups=dict(sorted(groups.items())),
    )


def _passes_entry(row: dict[str, Any], cfg: SweepConfig) -> bool:
    score = _float(row.get("score")) or 0.0
    raw_ev = _float(row.get("raw_ev"))
    net_ev = _float(row.get("net_ev"))
    entry = _float(row.get("live_entry_price")) or _float(row.get("market_price"))
    if score < cfg.min_score:
        return False
    if raw_ev is not None and raw_ev < cfg.min_raw_ev:
        return False
    if net_ev is not None and net_ev < cfg.min_net_ev:
        return False
    if entry is not None and entry > cfg.max_entry_price:
        return False
    return True


def _pnl_for_horizon(row: dict[str, Any], horizon: int) -> Optional[float]:
    payload = _json(row.get(HORIZONS[horizon]))
    for key in ("pnl_pct", "bid_markout_pct", "markout_pct", "return_pct", "price_change_pct"):
        value = _float(payload.get(key))
        if value is not None:
            return value
    return None


def _json(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _group_value(row: dict[str, Any], group_by: str) -> str:
    key = str(group_by or "").strip()
    if key in {"agent", "strategy", "signal_source"}:
        return str(row.get(key) or "unknown")
    features = _json(row.get("features_json"))
    if key in {"strategy_family", "family"}:
        return str(features.get("strategy_family") or features.get("alphainsider_family") or "unknown")
    if key in {"regime", "market_regime"}:
        return str(features.get("regime") or features.get("micro_regime") or "unknown")
    if key:
        return str(features.get(key) or row.get(key) or "unknown")
    return "all"


def _top_groups(results: list[SweepResult]) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for result in results:
        for group, count in result.groups.items():
            item = totals.setdefault(
                group,
                {
                    "group": group,
                    "appearances": 0,
                    "candidate_count": 0,
                    "best_total_pnl_per_100": None,
                    "best_winrate": None,
                },
            )
            item["appearances"] += 1
            item["candidate_count"] += count
            pnl = result.total_pnl_per_100
            if pnl is not None and (
                item["best_total_pnl_per_100"] is None
                or pnl > item["best_total_pnl_per_100"]
            ):
                item["best_total_pnl_per_100"] = pnl
                item["best_winrate"] = result.winrate
    return sorted(
        totals.values(),
        key=lambda x: (
            x["best_total_pnl_per_100"] if x["best_total_pnl_per_100"] is not None else -10**9,
            x["candidate_count"],
        ),
        reverse=True,
    )[:20]


def _float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="./data/trade_log.db")
    parser.add_argument("--since", default="")
    parser.add_argument("--rows-limit", type=int, default=10000)
    parser.add_argument("--configs", type=int, default=1000)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument(
        "--group-by",
        default="",
        choices=["", "agent", "strategy", "strategy_family", "regime", "signal_source"],
        help="Optional segment to summarize inside top configs.",
    )
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    rows = load_rows(args.db, since=args.since, limit=args.rows_limit)
    configs = default_configs(args.configs)
    payload = run_sweep(rows, configs, min_trades=args.min_trades, group_by=args.group_by)
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
