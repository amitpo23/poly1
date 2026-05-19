#!/usr/bin/env python3
"""Backtest external conviction JSONL signals against trade outcomes.

Read-only analysis:
  - loads data/external_convictions*.jsonl style files
  - keeps actionable BUY/SELL plans, including shadow BUY/SELL recommendations
  - matches each signal to the first terminal trade outcome for the same
    token_id, or market_id when token_id is unavailable
  - reports win-rate by source/provider and confidence bucket
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


WIN_STATUSES = {
    "closed_take_profit",
    "closed_partial_take_profit",
    "resolved_yes",
    "resolved_no",
}
LOSS_STATUSES = {"closed_stop_loss", "closed_timeout", "resolved_loss"}
TERMINAL_STATUSES = WIN_STATUSES | LOSS_STATUSES


@dataclass(frozen=True)
class ConvictionSignal:
    ts: datetime
    market_id: str
    token_id: Optional[str]
    action: str
    side: str
    source: str
    confidence: float
    entry_price: Optional[float]
    path: str


@dataclass(frozen=True)
class TradeOutcome:
    ts: datetime
    market_id: str
    token_id: Optional[str]
    status: str
    pnl_usdc: Optional[float]

    @property
    def is_win(self) -> bool:
        return self.status in WIN_STATUSES


@dataclass
class ProviderStats:
    source: str
    signals: int = 0
    matched: int = 0
    wins: int = 0
    losses: int = 0
    pnl_usdc: float = 0.0

    @property
    def winrate(self) -> Optional[float]:
        total = self.wins + self.losses
        return (self.wins / total) if total else None


def parse_ts(raw: object) -> Optional[datetime]:
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _float_or_none(raw: object) -> Optional[float]:
    try:
        if raw in (None, ""):
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def confidence_bucket(confidence: float) -> str:
    if confidence < 0.52:
        return "<0.52"
    if confidence < 0.58:
        return "0.52-0.58"
    if confidence < 0.65:
        return "0.58-0.65"
    return ">=0.65"


def iter_signals(paths: Iterable[Path], actionable_only: bool = True) -> list[ConvictionSignal]:
    signals: list[ConvictionSignal] = []
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                plan = row.get("plan") if isinstance(row.get("plan"), dict) else row
                verdict = row.get("verdict") if isinstance(row.get("verdict"), dict) else {}
                action = str(plan.get("action") or "").upper()
                is_actionable = (
                    action in {"BUY", "SELL"}
                    or action.startswith("SHADOW_BUY")
                    or action.startswith("SHADOW_SELL")
                )
                if actionable_only and not is_actionable:
                    continue
                market_id = str(plan.get("market_id") or "").strip()
                if not market_id:
                    continue
                ts = parse_ts(plan.get("ts") or row.get("ts"))
                if ts is None:
                    continue
                source = str(
                    plan.get("source")
                    or verdict.get("source")
                    or row.get("source")
                    or path.stem
                )
                signals.append(
                    ConvictionSignal(
                        ts=ts,
                        market_id=market_id,
                        token_id=str(plan.get("token_id")) if plan.get("token_id") else None,
                        action=action,
                        side=str(plan.get("side") or "").upper(),
                        source=source,
                        confidence=float(_float_or_none(plan.get("confidence")) or 0.0),
                        entry_price=_float_or_none(plan.get("entry_price")),
                        path=str(path),
                    )
                )
    return sorted(signals, key=lambda s: s.ts)


def _pnl_from_response(raw: Optional[str]) -> Optional[float]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    for key in ("pnl_usdc_real", "strategy_pnl_usdc", "pnl_usdc"):
        val = _float_or_none(data.get(key))
        if val is not None:
            return val
    return None


def load_outcomes(db_path: Path) -> list[TradeOutcome]:
    # immutable=1 keeps this read-only even when the live DB has WAL sidecars
    # owned by another process/container.
    uri = f"file:{db_path.resolve()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT ts, market_id, token_id, status, response_json
            FROM trades
            WHERE status IN (
              'closed_take_profit','closed_partial_take_profit',
              'closed_stop_loss','closed_timeout',
              'resolved_yes','resolved_no','resolved_loss'
            )
            ORDER BY ts ASC, id ASC
            """
        ).fetchall()
    outcomes: list[TradeOutcome] = []
    for row in rows:
        ts = parse_ts(row["ts"])
        if ts is None:
            continue
        outcomes.append(
            TradeOutcome(
                ts=ts,
                market_id=str(row["market_id"]),
                token_id=str(row["token_id"]) if row["token_id"] else None,
                status=str(row["status"]),
                pnl_usdc=_pnl_from_response(row["response_json"]),
            )
        )
    return outcomes


def match_outcome(
    signal: ConvictionSignal,
    outcomes: list[TradeOutcome],
    max_age_hours: float,
) -> Optional[TradeOutcome]:
    latest = signal.ts + timedelta(hours=max_age_hours)
    for outcome in outcomes:
        if outcome.ts < signal.ts or outcome.ts > latest:
            continue
        if signal.token_id and outcome.token_id == signal.token_id:
            return outcome
        if not signal.token_id and outcome.market_id == signal.market_id:
            return outcome
        if signal.market_id == outcome.market_id and signal.token_id == outcome.token_id:
            return outcome
    return None


def build_stats(
    signals: list[ConvictionSignal],
    outcomes: list[TradeOutcome],
    max_age_hours: float,
) -> tuple[list[ProviderStats], list[ProviderStats]]:
    by_source: dict[str, ProviderStats] = defaultdict(lambda: ProviderStats(""))
    by_bucket: dict[str, ProviderStats] = defaultdict(lambda: ProviderStats(""))
    for signal in signals:
        source_key = signal.source
        bucket_key = f"{signal.source}|{confidence_bucket(signal.confidence)}"
        for stats, key in ((by_source[source_key], source_key), (by_bucket[bucket_key], bucket_key)):
            stats.source = key
            stats.signals += 1
        outcome = match_outcome(signal, outcomes, max_age_hours=max_age_hours)
        if outcome is None:
            continue
        for stats in (by_source[source_key], by_bucket[bucket_key]):
            stats.matched += 1
            if outcome.is_win:
                stats.wins += 1
            else:
                stats.losses += 1
            if outcome.pnl_usdc is not None:
                stats.pnl_usdc += outcome.pnl_usdc
    sort_key = lambda s: (s.matched, s.winrate or -1, s.signals)
    return (
        sorted(by_source.values(), key=sort_key, reverse=True),
        sorted(by_bucket.values(), key=sort_key, reverse=True),
    )


def _stats_dict(stats: ProviderStats) -> dict:
    return {
        "source": stats.source,
        "signals": stats.signals,
        "matched": stats.matched,
        "wins": stats.wins,
        "losses": stats.losses,
        "winrate": None if stats.winrate is None else round(stats.winrate, 4),
        "pnl_usdc": round(stats.pnl_usdc, 6),
    }


def print_markdown(provider_stats: list[ProviderStats], bucket_stats: list[ProviderStats]) -> None:
    def line(stats: ProviderStats) -> str:
        wr = "n/a" if stats.winrate is None else f"{stats.winrate:.1%}"
        return (
            f"- `{stats.source}`: signals={stats.signals}, matched={stats.matched}, "
            f"wins={stats.wins}, losses={stats.losses}, winrate={wr}, pnl={stats.pnl_usdc:.4f}"
        )

    print("Provider backtest")
    for stats in provider_stats:
        print(line(stats))
    print("\nConfidence buckets")
    for stats in bucket_stats:
        print(line(stats))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/trade_log.db")
    parser.add_argument("--glob", default="data/external_convictions*.jsonl")
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--include-skips", action="store_true")
    args = parser.parse_args()

    paths = sorted(Path(".").glob(args.glob))
    signals = iter_signals(paths, actionable_only=not args.include_skips)
    outcomes = load_outcomes(Path(args.db))
    provider_stats, bucket_stats = build_stats(signals, outcomes, args.max_age_hours)
    if args.json:
        print(json.dumps({
            "paths": [str(p) for p in paths],
            "signals": len(signals),
            "outcomes": len(outcomes),
            "max_age_hours": args.max_age_hours,
            "providers": [_stats_dict(s) for s in provider_stats],
            "confidence_buckets": [_stats_dict(s) for s in bucket_stats],
        }, indent=2, sort_keys=True))
    else:
        print_markdown(provider_stats, bucket_stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
