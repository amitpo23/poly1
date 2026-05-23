#!/usr/bin/env python3
"""Read-only performance tear sheet for live, shadow, and brain decisions.

This is intentionally lightweight: no pyfolio dependency, no pandas, and no
runtime side effects. It turns the SQLite journal into the handful of metrics
we need before deciding which agents deserve live budget.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


DEFAULT_DB = "data/trade_log.db"
CLOSED_STATUSES = {
    "closed_take_profit",
    "closed_stop_loss",
    "closed_timeout",
    "closed_dust",
    "closed_manual",
    "resolved_yes",
    "resolved_no",
    "resolved_loss",
}
WIN_STATUSES = {"closed_take_profit", "resolved_yes", "resolved_no"}


@dataclass(frozen=True)
class ReturnStats:
    samples: int
    wins: int
    losses: int
    winrate: Optional[float]
    pnl_usdc: float
    avg_pnl_usdc: Optional[float]
    profit_factor: Optional[float]
    max_drawdown_usdc: float
    recovery_trades: Optional[int]
    max_loss_streak: int
    downside_volatility_usdc: Optional[float]
    expected_shortfall_95_usdc: Optional[float]


@dataclass(frozen=True)
class TradeGroup:
    key: str
    entries: int
    closed: int
    notional_usdc: float
    stats: ReturnStats


@dataclass(frozen=True)
class DecisionGroup:
    key: str
    decisions: int
    approvals: int
    rejects: int
    approval_rate: Optional[float]
    avg_score: Optional[float]
    avg_net_ev: Optional[float]
    avg_price_edge: Optional[float]
    avg_spread_proxy: Optional[float]
    markout_samples: int
    avg_markout_pct: Optional[float]
    top_reject_reasons: list[dict[str, Any]]


def build_report(db_path: str, *, hours: float = 24.0, limit_reasons: int = 8) -> dict[str, Any]:
    path = Path(db_path)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    since_iso = since.isoformat()
    if not path.exists():
        return {"ok": False, "reason": "db_missing", "db": str(path), "since_utc": since_iso}

    with sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        trades = _fetch_rows(conn, "trades", since_iso)
        decisions = _fetch_rows(conn, "decision_journal", since_iso) if _table_exists(conn, "decision_journal") else []
        brain = _fetch_rows(conn, "brain_decisions", since_iso) if _table_exists(conn, "brain_decisions") else []

    trade_groups = {
        "by_agent": _trade_groups(trades, _trade_agent),
        "by_side": _trade_groups(trades, lambda row: str(row["side"] or "unknown")),
        "by_price_band": _trade_groups(trades, _price_band),
    }
    decision_groups = {
        "by_agent": _decision_groups(decisions, lambda row: str(row["agent"] or "unknown"), limit_reasons),
        "by_strategy": _decision_groups(decisions, lambda row: str(row["strategy"] or "unknown"), limit_reasons),
        "by_signal_source": _decision_groups(decisions, lambda row: str(row["signal_source"] or "unknown"), limit_reasons),
        "by_price_band": _decision_groups(decisions, _decision_price_band, limit_reasons),
    }

    return {
        "ok": True,
        "db": str(path),
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "window_hours": hours,
        "since_utc": since_iso,
        "summary": {
            "trades": len(trades),
            "closed_trades": sum(1 for row in trades if str(row["status"]).lower() in CLOSED_STATUSES),
            "decisions": len(decisions),
            "brain_decisions": len(brain),
            "live_stats": asdict(_return_stats([_trade_pnl(row) for row in trades if _trade_pnl(row) is not None])),
            "top_trade_statuses": _counter_rows((str(row["status"] or "unknown") for row in trades), limit_reasons),
            "top_brain_reasons": _counter_rows((str(row["reason"] or "unknown") for row in brain), limit_reasons),
        },
        "trade_groups": {
            name: [asdict(group) for group in groups]
            for name, groups in trade_groups.items()
        },
        "decision_groups": {
            name: [asdict(group) for group in groups]
            for name, groups in decision_groups.items()
        },
        "interpretation": _interpret(trade_groups, decision_groups),
    }


def format_markdown(report: dict[str, Any]) -> str:
    if not report.get("ok"):
        return f"# Performance tear sheet\n\nmissing: {report.get('reason')}\n"
    lines = [
        "# Performance tear sheet",
        f"window_hours={report['window_hours']}",
        f"generated_utc={report['generated_utc']}",
        "",
        "## Summary",
    ]
    summary = report["summary"]
    stats = summary["live_stats"]
    lines.extend(
        [
            f"- trades: {summary['trades']} closed={summary['closed_trades']}",
            f"- decisions: {summary['decisions']} brain_decisions={summary['brain_decisions']}",
            f"- pnl_usdc: {stats['pnl_usdc']:.6f} winrate={_fmt_pct(stats['winrate'])} max_drawdown_usdc={stats['max_drawdown_usdc']:.6f}",
            f"- profit_factor: {_fmt_num(stats['profit_factor'])} avg_pnl_usdc={_fmt_num(stats['avg_pnl_usdc'])}",
            f"- max_loss_streak: {stats['max_loss_streak']} downside_volatility_usdc={_fmt_num(stats['downside_volatility_usdc'])} expected_shortfall_95_usdc={_fmt_num(stats['expected_shortfall_95_usdc'])}",
            "",
        ]
    )
    for section, key in (
        ("Trades By Agent", "by_agent"),
        ("Trades By Side", "by_side"),
        ("Trades By Price Band", "by_price_band"),
    ):
        lines.append(f"## {section}")
        for row in report["trade_groups"][key][:10]:
            s = row["stats"]
            lines.append(
                f"- {row['key']}: entries={row['entries']} closed={row['closed']} "
                f"pnl={s['pnl_usdc']:.6f} winrate={_fmt_pct(s['winrate'])} "
                f"dd={s['max_drawdown_usdc']:.6f} loss_streak={s['max_loss_streak']}"
            )
        lines.append("")
    for section, key in (
        ("Decisions By Agent", "by_agent"),
        ("Decisions By Signal", "by_signal_source"),
        ("Decisions By Price Band", "by_price_band"),
    ):
        lines.append(f"## {section}")
        for row in report["decision_groups"][key][:12]:
            lines.append(
                f"- {row['key']}: decisions={row['decisions']} approvals={row['approvals']} "
                f"approval_rate={_fmt_pct(row['approval_rate'])} avg_ev={_fmt_num(row['avg_net_ev'])} "
                f"edge={_fmt_num(row['avg_price_edge'])} spread={_fmt_num(row['avg_spread_proxy'])} "
                f"markout={_fmt_pct(row['avg_markout_pct'])}"
            )
        lines.append("")
    lines.append("## Interpretation")
    for item in report["interpretation"]:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def _fetch_rows(conn: sqlite3.Connection, table: str, since_iso: str) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT * FROM {table} WHERE ts >= ? ORDER BY ts ASC, id ASC", (since_iso,)).fetchall()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _trade_groups(rows: list[sqlite3.Row], key_fn: Any) -> list[TradeGroup]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[key_fn(row)].append(row)
    out = []
    for key, group in grouped.items():
        pnls = [_trade_pnl(row) for row in group]
        pnls = [pnl for pnl in pnls if pnl is not None]
        out.append(
            TradeGroup(
                key=key,
                entries=len(group),
                closed=sum(1 for row in group if str(row["status"]).lower() in CLOSED_STATUSES),
                notional_usdc=round(sum(_float(row["size_usdc"]) or 0.0 for row in group), 6),
                stats=_return_stats(pnls),
            )
        )
    out.sort(key=lambda row: (row.stats.pnl_usdc, row.closed, row.entries), reverse=True)
    return out


def _decision_groups(rows: list[sqlite3.Row], key_fn: Any, limit_reasons: int) -> list[DecisionGroup]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[key_fn(row)].append(row)
    out = []
    for key, group in grouped.items():
        approvals = sum(1 for row in group if str(row["decision"]).upper() not in {"REJECT", "SKIP", "VETO"})
        rejects = len(group) - approvals
        scores = [_float(row["score"]) for row in group if _float(row["score"]) is not None]
        evs = [_float(row["net_ev"]) for row in group if _float(row["net_ev"]) is not None]
        edges = [_price_edge(row) for row in group if _price_edge(row) is not None]
        spreads = [_spread_proxy(row) for row in group if _spread_proxy(row) is not None]
        markouts = []
        for row in group:
            for col in ("outcome_1m_json", "outcome_3m_json", "outcome_5m_json", "outcome_15m_json", "outcome_60m_json"):
                value = _markout(row[col])
                if value is not None:
                    markouts.append(value)
        out.append(
            DecisionGroup(
                key=key,
                decisions=len(group),
                approvals=approvals,
                rejects=rejects,
                approval_rate=approvals / len(group) if group else None,
                avg_score=_avg(scores),
                avg_net_ev=_avg(evs),
                avg_price_edge=_avg(edges),
                avg_spread_proxy=_avg(spreads),
                markout_samples=len(markouts),
                avg_markout_pct=_avg(markouts),
                top_reject_reasons=_counter_rows((str(row["reason"] or "unknown") for row in group), limit_reasons),
            )
        )
    out.sort(key=lambda row: (row.avg_markout_pct is not None, row.avg_markout_pct or -999, row.approvals), reverse=True)
    return out


def _return_stats(pnls: list[float]) -> ReturnStats:
    wins = sum(1 for pnl in pnls if pnl > 0)
    loss_count = sum(1 for pnl in pnls if pnl < 0)
    gross_win = sum(pnl for pnl in pnls if pnl > 0)
    gross_loss = -sum(pnl for pnl in pnls if pnl < 0)
    losses = [pnl for pnl in pnls if pnl < 0]
    curve = []
    total = 0.0
    for pnl in pnls:
        total += pnl
        curve.append(total)
    max_dd, recovery = _drawdown(curve)
    return ReturnStats(
        samples=len(pnls),
        wins=wins,
        losses=loss_count,
        winrate=wins / len(pnls) if pnls else None,
        pnl_usdc=round(sum(pnls), 6),
        avg_pnl_usdc=round(sum(pnls) / len(pnls), 6) if pnls else None,
        profit_factor=round(gross_win / gross_loss, 6) if gross_loss > 0 else (math.inf if gross_win > 0 else None),
        max_drawdown_usdc=round(max_dd, 6),
        recovery_trades=recovery,
        max_loss_streak=_max_loss_streak(pnls),
        downside_volatility_usdc=_downside_volatility(pnls),
        expected_shortfall_95_usdc=_expected_shortfall(losses, tail=0.05),
    )


def _drawdown(curve: list[float]) -> tuple[float, Optional[int]]:
    peak = 0.0
    max_dd = 0.0
    trough_index: Optional[int] = None
    recovery: Optional[int] = None
    for idx, value in enumerate(curve):
        if value >= peak:
            if trough_index is not None and recovery is None:
                recovery = idx - trough_index
            peak = value
        dd = peak - value
        if dd > max_dd:
            max_dd = dd
            trough_index = idx
            recovery = None
    return max_dd, recovery


def _max_loss_streak(pnls: list[float]) -> int:
    best = 0
    current = 0
    for pnl in pnls:
        if pnl < 0:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _downside_volatility(pnls: list[float]) -> Optional[float]:
    negative = [pnl for pnl in pnls if pnl < 0]
    if len(negative) < 2:
        return None
    mean = sum(negative) / len(negative)
    variance = sum((pnl - mean) ** 2 for pnl in negative) / (len(negative) - 1)
    return round(math.sqrt(variance), 6)


def _expected_shortfall(losses: list[float], *, tail: float) -> Optional[float]:
    if not losses:
        return None
    ordered = sorted(losses)
    count = max(1, math.ceil(len(ordered) * tail))
    return round(sum(ordered[:count]) / count, 6)


def _trade_agent(row: sqlite3.Row) -> str:
    payload = _json(row["response_json"])
    return str(payload.get("agent") or payload.get("source") or row["cycle_id"] or "unknown")


def _price_band(row: sqlite3.Row) -> str:
    price = _float(row["price"])
    if price is None:
        return "unknown"
    low = math.floor(price * 10) / 10
    high = low + 0.1
    return f"{low:.1f}-{high:.1f}"


def _decision_price_band(row: sqlite3.Row) -> str:
    price = _float(row["live_entry_price"]) or _float(row["market_price"])
    if price is None:
        return "unknown"
    low = math.floor(price * 10) / 10
    high = low + 0.1
    return f"{low:.1f}-{high:.1f}"


def _price_edge(row: sqlite3.Row) -> Optional[float]:
    probability = _float(row["internal_probability"])
    entry = _float(row["live_entry_price"]) or _float(row["market_price"])
    if probability is None or entry is None:
        return None
    return probability - entry


def _spread_proxy(row: sqlite3.Row) -> Optional[float]:
    market = _float(row["market_price"])
    entry = _float(row["live_entry_price"])
    if market is None or entry is None:
        return None
    return abs(entry - market)


def _trade_pnl(row: sqlite3.Row) -> Optional[float]:
    status = str(row["status"] or "").lower()
    payload = _json(row["response_json"])
    for key in ("pnl_usdc_real", "realized_pnl_usdc", "strategy_pnl_usdc", "pnl_usdc"):
        value = _float(payload.get(key))
        if value is not None:
            return value
    if status == "resolved_loss":
        return -abs(_float(row["size_usdc"]) or 0.0)
    if status in WIN_STATUSES:
        price = _float(row["price"])
        size = _float(row["size_usdc"])
        if price and size:
            shares = size / price
            return shares - size
    return None


def _markout(raw: Any) -> Optional[float]:
    payload = _json(raw)
    for key in ("markout_pct", "pnl_pct", "return_pct", "bid_markout_pct", "price_change_pct"):
        value = _float(payload.get(key))
        if value is not None:
            return value
    return None


def _interpret(trade_groups: dict[str, list[TradeGroup]], decision_groups: dict[str, list[DecisionGroup]]) -> list[str]:
    notes = []
    side_groups = {row.key: row for row in trade_groups.get("by_side", [])}
    if "BUY" in side_groups and "SELL" in side_groups:
        buy = side_groups["BUY"].stats.pnl_usdc
        sell = side_groups["SELL"].stats.pnl_usdc
        if buy > sell:
            notes.append("BUY/YES side outperformed SELL/NO in this window.")
        elif sell > buy:
            notes.append("SELL/NO side outperformed BUY/YES in this window.")
    price_groups = trade_groups.get("by_price_band", [])
    if price_groups:
        best = price_groups[0]
        notes.append(f"Best realized price band: {best.key} with pnl_usdc={best.stats.pnl_usdc:.6f}.")
    signal_groups = [row for row in decision_groups.get("by_signal_source", []) if row.markout_samples]
    if signal_groups:
        best_signal = signal_groups[0]
        notes.append(
            f"Best measured shadow signal: {best_signal.key} "
            f"avg_markout={_fmt_pct(best_signal.avg_markout_pct)} samples={best_signal.markout_samples}."
        )
    if not notes:
        notes.append("Not enough realized PnL or markout samples in this window for a strong conclusion.")
    return notes


def _json(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _counter_rows(values: Iterable[str], limit: int) -> list[dict[str, Any]]:
    return [{"key": key, "count": count} for key, count in Counter(values).most_common(limit)]


def _avg(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 6) if values else None


def _float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2%}"


def _fmt_num(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    if value == math.inf:
        return "inf"
    return f"{value:.6f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--json", action="store_true", help="Print JSON instead of markdown")
    parser.add_argument("--out", default="", help="Optional output path")
    args = parser.parse_args()

    report = build_report(args.db, hours=args.hours)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n" if args.json else format_markdown(report)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
