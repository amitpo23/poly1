#!/usr/bin/env python3
"""Build a unified pre-live strategy matrix from existing backtest artifacts.

The command can either read an existing backtest directory (`--input-dir`) or
only print the canonical run plan (`--plan-only`).  It normalizes each strategy
into the same concepts: samples, win-rate, PnL per $100 daily budget, blockers,
and live eligibility.  The runner deliberately does not enable live trading.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.application.strategy_catalog import (
    STRATEGY_CATALOG,
    WindowResult,
    catalog_summary,
    evaluate_gate,
)


@dataclass(frozen=True)
class MatrixRow:
    strategy_id: str
    family: str
    owner_agent: str
    maturity: str
    windows: list[dict[str, Any]]
    verdict: dict[str, Any]
    notes: list[str]


def build_matrix(input_dir: Optional[str] = None) -> dict[str, Any]:
    base = Path(input_dir) if input_dir else None
    rows: list[MatrixRow] = []
    for spec in STRATEGY_CATALOG:
        windows, notes = _load_windows(base, spec.strategy_id) if base else ([], ["not_run_this_pass"])
        verdict = evaluate_gate(spec.strategy_id, windows).to_dict()
        rows.append(
            MatrixRow(
                strategy_id=spec.strategy_id,
                family=spec.family,
                owner_agent=spec.owner_agent,
                maturity=spec.maturity,
                windows=[asdict(w) for w in windows],
                verdict=verdict,
                notes=notes,
            )
        )
    return {
        "catalog": {
            "strategy_count": len(STRATEGY_CATALOG),
            "families": catalog_summary()["families"],
        },
        "input_dir": str(base) if base else None,
        "rows": [asdict(row) for row in rows],
        "live_eligible": [row.strategy_id for row in rows if row.verdict["state"] == "live_eligible"],
        "shadow_only": [row.strategy_id for row in rows if row.verdict["state"] != "live_eligible"],
    }


def _load_windows(base: Optional[Path], strategy_id: str) -> tuple[list[WindowResult], list[str]]:
    if base is None or not base.exists():
        return [], ["input_dir_missing"]
    if strategy_id == "btc_daily_mean_reversion":
        return _load_mean_reversion(base), []
    if strategy_id == "btc_daily_momentum":
        return _load_btc_momentum(base), []
    if strategy_id == "scalper_spread_edge":
        return _load_scalper(base), ["history_limited_to_scalper_pairs"]
    if strategy_id == "sports_cheap_hold":
        return _load_market_sweep(base, category="sports", strategy="cheap_hold_0.40"), [
            "market_sweep_requires_bias_audit",
        ]
    if strategy_id == "external_conviction_providers":
        return _load_external_convictions(base), ["provider_rows_are_aggregated_not_daily_budget"]
    return [], ["no_parser_or_no_backtest_harness"]


def _load_mean_reversion(base: Path) -> list[WindowResult]:
    out: list[WindowResult] = []
    for days in (30, 60, 90):
        path = base / f"mean_reversion_{days}d.json"
        if not path.exists():
            continue
        payload = json.loads(path.read_text())
        day_rows = payload.get("days") or []
        active = 0
        entries = 0
        pnl_norm = 0.0
        for day in day_rows:
            e = int(day.get("entries") or 0)
            p = float(day.get("pnl") or 0.0)
            entries += e
            if e > 0:
                active += 1
                pnl_norm += p / e
        out.append(WindowResult(f"{days}d", entries, None, round(pnl_norm, 4)))
    return out


def _load_scalper(base: Path) -> list[WindowResult]:
    path = base / "scalper_30d.json"
    if not path.exists():
        return []
    payload = json.loads(path.read_text())
    windows = payload.get("windows") or []
    out: list[WindowResult] = []
    for row in windows:
        pnl = float(row.get("paper_pnl_usdc") or 0.0)
        position = float((payload.get("config") or {}).get("position_size") or 2.5)
        pnl_per_100 = (pnl / position) * 100.0 if position else None
        out.append(
            WindowResult(
                str(row.get("label") or "window"),
                int(row.get("entries") or row.get("pairs") or 0),
                _optional_float(row.get("win_rate")),
                None if pnl_per_100 is None else round(pnl_per_100, 4),
            )
        )
    return out


def _load_btc_momentum(base: Path) -> list[WindowResult]:
    path = base / "btc_momentum_split_30_60_90.txt"
    if not path.exists():
        return []
    out: list[WindowResult] = []
    for line in path.read_text().splitlines():
        parts = line.strip().split()
        if not parts or "-" not in parts[0] or "d" not in parts[0]:
            continue
        label = parts[0]
        try:
            if "=" in parts[1]:
                n = int(parts[1].split("=")[1])
                wins = int(parts[2].split("=")[1])
                losses = int(parts[3].split("=")[1])
                pnl = float(parts[5].split("=")[1].replace("$", ""))
            else:
                n = int(parts[1])
                wins = int(parts[2])
                losses = int(parts[3])
                pnl_text = parts[5].replace("$", "")
                if pnl_text == "" and len(parts) > 6:
                    pnl_text = parts[6].replace("$", "")
                pnl = float(pnl_text)
        except (IndexError, ValueError):
            continue
        decided = wins + losses
        wr = wins / decided if decided else None
        out.append(WindowResult(label, n, wr, round(pnl * 100.0, 4)))
    return out


def _load_market_sweep(base: Path, *, category: str, strategy: str) -> list[WindowResult]:
    out: list[WindowResult] = []
    for days in (30, 60, 90):
        path = base / f"market_sweep_{days}d.json"
        if not path.exists():
            continue
        payload = json.loads(path.read_text())
        row = ((payload.get("matrix") or {}).get(category) or {}).get(strategy)
        if not row:
            continue
        out.append(
            WindowResult(
                f"{days}d",
                int(row.get("n") or 0),
                _optional_float(row.get("win_rate")),
                round(float(row.get("paper_pnl_per_dollar") or 0.0) * 100.0, 4),
            )
        )
    return out


def _load_external_convictions(base: Path) -> list[WindowResult]:
    path = base / "external_convictions.json"
    if not path.exists():
        return []
    payload = json.loads(path.read_text())
    matched = 0
    wins = 0
    pnl = 0.0
    for row in payload.get("providers") or []:
        matched += int(row.get("matched") or 0)
        wins += int(row.get("wins") or 0)
        pnl += float(row.get("pnl_usdc") or 0.0)
    wr = wins / matched if matched else None
    return [WindowResult("provider_scorecard", matched, wr, round(pnl, 4))]


def _optional_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()

    payload = catalog_summary() if args.plan_only else build_matrix(args.input_dir or None)
    raw = json.dumps(payload, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(raw + "\n")
    print(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
