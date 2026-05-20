#!/usr/bin/env python3
"""Portfolio-equity guard for controlled live probes.

Cash-only drawdown is too conservative for a live trading probe: buying a
position moves USDC from cash into CTF tokens, but that is deployed capital,
not a realized loss. This guard measures equity as:

    cash USDC + mark-to-market value of open positions

It can be used by heartbeats and QA checks before deciding whether to freeze.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Iterable, Any


DEFAULT_DB_PATH = Path("data/trade_log.db")
DEFAULT_CONTROL_PATH = Path("data/runtime_control.json")


@dataclass
class PositionValue:
    trade_id: int | None
    market_id: str
    token_id: str
    side: str
    entry_price: float
    size_usdc: float
    shares: float
    midpoint: float
    mtm_usdc: float
    midpoint_source: str


@dataclass
class EquitySnapshot:
    cash_usdc: float
    open_mtm_usdc: float
    equity_usdc: float
    baseline_usdc: float
    drawdown_usdc: float
    drawdown_limit_usdc: float
    breached: bool
    open_positions: list[PositionValue]


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _midpoint_from_response(resp: Any, fallback: float) -> tuple[float, str]:
    if isinstance(resp, dict):
        return (
            _float(resp.get("mid"), fallback),
            str(resp.get("_source") or "clob_midpoint"),
        )
    return _float(resp, fallback), "clob_midpoint"


def compute_position_values(
    rows: Iterable[dict[str, Any]],
    midpoint_for_token: Callable[[str], Any],
) -> list[PositionValue]:
    values: list[PositionValue] = []
    for row in rows:
        token_id = str(row.get("token_id") or "").strip()
        if not token_id:
            continue
        entry_price = _float(row.get("price"))
        size_usdc = _float(row.get("size_usdc"))
        if entry_price <= 0 or size_usdc <= 0:
            continue
        midpoint = entry_price
        midpoint_source = "entry_fallback"
        try:
            midpoint, midpoint_source = _midpoint_from_response(
                midpoint_for_token(token_id),
                entry_price,
            )
        except Exception:
            midpoint = entry_price
        if midpoint <= 0:
            midpoint = entry_price
            midpoint_source = "entry_fallback"
        shares = size_usdc / entry_price
        values.append(PositionValue(
            trade_id=int(row["id"]) if row.get("id") is not None else None,
            market_id=str(row.get("market_id") or ""),
            token_id=token_id,
            side=str(row.get("side") or ""),
            entry_price=entry_price,
            size_usdc=size_usdc,
            shares=shares,
            midpoint=midpoint,
            mtm_usdc=shares * midpoint,
            midpoint_source=midpoint_source,
        ))
    return values


def compute_equity_snapshot(
    *,
    cash_usdc: float,
    baseline_usdc: float,
    drawdown_limit_usdc: float,
    positions: list[PositionValue],
) -> EquitySnapshot:
    open_mtm = sum(p.mtm_usdc for p in positions)
    equity = cash_usdc + open_mtm
    drawdown = max(0.0, baseline_usdc - equity)
    return EquitySnapshot(
        cash_usdc=cash_usdc,
        open_mtm_usdc=open_mtm,
        equity_usdc=equity,
        baseline_usdc=baseline_usdc,
        drawdown_usdc=drawdown,
        drawdown_limit_usdc=drawdown_limit_usdc,
        breached=drawdown > drawdown_limit_usdc,
        open_positions=positions,
    )


def _baseline_from_control(path: Path) -> float:
    if not path.exists():
        return 0.0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0.0
    return _float(
        data.get("equity_at_start_usdc")
        or data.get("wallet_balance_at_start_usdc")
    )


def _latest_position_marks(db_path: Path) -> dict[str, float]:
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT token_id, current_price FROM position_marks WHERE token_id IS NOT NULL"
        ).fetchall()
        conn.close()
    except Exception:
        return {}
    out: dict[str, float] = {}
    for token_id, current_price in rows:
        price = _float(current_price)
        if token_id and price > 0:
            out[str(token_id)] = price
    return out


def live_snapshot(args: argparse.Namespace) -> EquitySnapshot:
    from agents.application.trade_log import TradeLog
    from agents.polymarket.polymarket import Polymarket

    db_path = Path(args.db)
    control_path = Path(args.control)
    poly = Polymarket(live=False)
    cash = _float(poly.get_usdc_balance())
    baseline = (
        float(args.baseline)
        if args.baseline is not None
        else _baseline_from_control(control_path)
    )
    rows = TradeLog(str(db_path)).filled_positions_with_id()
    position_marks = _latest_position_marks(db_path)

    def midpoint_for_token(token_id: str) -> Any:
        try:
            return poly.client.get_midpoint(token_id)
        except Exception:
            if token_id in position_marks:
                return {"mid": position_marks[token_id], "_source": "position_mark"}
            raise

    positions = compute_position_values(
        rows,
        midpoint_for_token,
    )
    return compute_equity_snapshot(
        cash_usdc=cash,
        baseline_usdc=baseline,
        drawdown_limit_usdc=float(args.drawdown_limit),
        positions=positions,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--control", default=str(DEFAULT_CONTROL_PATH))
    parser.add_argument("--baseline", type=float, default=None)
    parser.add_argument("--drawdown-limit", type=float, default=0.75)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    snapshot = live_snapshot(args)
    payload = asdict(snapshot)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "equity_guard: "
            f"cash={snapshot.cash_usdc:.6f} "
            f"open_mtm={snapshot.open_mtm_usdc:.6f} "
            f"equity={snapshot.equity_usdc:.6f} "
            f"baseline={snapshot.baseline_usdc:.6f} "
            f"drawdown={snapshot.drawdown_usdc:.6f} "
            f"limit={snapshot.drawdown_limit_usdc:.6f} "
            f"breached={str(snapshot.breached).lower()} "
            f"open={len(snapshot.open_positions)}"
        )
    return 2 if snapshot.breached else 0


if __name__ == "__main__":
    raise SystemExit(main())
