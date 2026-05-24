#!/usr/bin/env python3
"""Per-agent hypothetical edge report from decision_journal markouts.

Complementary to performance_tearsheet.py (which measures real closed PnL).
This script measures HYPOTHETICAL edge: for each ENTER / SHADOW_ENTER decision,
what would the PnL have been if exited at the 5m / 15m markout?

Output: markdown report grouped by signal_source.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


HORIZONS = (1, 3, 5, 15, 60)
PROFIT_BAND_BUY = (0.40, 0.49)
PROFIT_BAND_SELL = (0.51, 0.60)


def _parse_outcome(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def _hypothetical_pnl(side: str, entry_price: float, best_bid: float) -> float | None:
    """PnL pct for entering at entry_price and exiting at best_bid.

    BUY: bought YES at entry_price; exit at best_bid for YES → (best_bid / entry_price) - 1.
    SELL: bought NO at (1 - entry_price); exit at best NO bid = (1 - best_ask of YES).
          Approximation here uses best_bid (YES) as the YES side; for NO position
          we estimate exit at (1 - best_bid). Real exit asks would differ marginally.
    """
    if entry_price is None or best_bid is None:
        return None
    try:
        ep = float(entry_price)
        bb = float(best_bid)
    except (TypeError, ValueError):
        return None
    if ep <= 0 or bb <= 0 or ep >= 1 or bb >= 1:
        return None
    if side == "BUY":
        return (bb / ep) - 1.0
    if side == "SELL":
        no_entry = 1.0 - ep
        no_exit = 1.0 - bb
        if no_entry <= 0 or no_exit <= 0:
            return None
        return (no_exit / no_entry) - 1.0
    return None


def _in_profit_band(side: str, price: float) -> bool:
    if side == "BUY":
        return PROFIT_BAND_BUY[0] <= price <= PROFIT_BAND_BUY[1]
    if side == "SELL":
        return PROFIT_BAND_SELL[0] <= price <= PROFIT_BAND_SELL[1]
    return False


def collect(db_path: str, days: int) -> dict:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    approvals_per_agent: dict[str, int] = defaultdict(int)
    for r in conn.execute(
        """
        SELECT agent, COUNT(*) AS n
        FROM brain_decisions
        WHERE approved=1 AND ts > ?
        GROUP BY agent
        """,
        (cutoff,),
    ):
        approvals_per_agent[r["agent"] or "unknown"] = int(r["n"])

    edge: dict[str, dict] = defaultdict(
        lambda: {
            "decisions": 0,
            "in_band": 0,
            "with_markout": 0,
            "wins": 0,
            "losses": 0,
            "sum_pnl_pct": 0.0,
            "sum_pnl_pct_in_band": 0.0,
            "in_band_with_markout": 0,
            "actions": defaultdict(int),
        }
    )

    rows = conn.execute(
        """
        SELECT id, ts, decision, agent, signal_source, action,
               live_entry_price, market_price,
               outcome_1m_json, outcome_3m_json, outcome_5m_json,
               outcome_15m_json, outcome_60m_json
        FROM decision_journal
        WHERE decision IN ('ENTER', 'SHADOW_ENTER')
          AND ts > ?
        """,
        (cutoff,),
    ).fetchall()

    for r in rows:
        source = r["signal_source"] or r["agent"] or "unknown"
        action = r["action"] or "?"
        entry = r["live_entry_price"] or r["market_price"]
        try:
            entry = float(entry) if entry is not None else None
        except (TypeError, ValueError):
            entry = None
        bucket = edge[source]
        bucket["decisions"] += 1
        bucket["actions"][action] += 1
        in_band = entry is not None and _in_profit_band(action, entry)
        if in_band:
            bucket["in_band"] += 1
        markout = _parse_outcome(r["outcome_5m_json"])
        best_bid = markout.get("best_bid") if markout else None
        pnl = _hypothetical_pnl(action, entry, best_bid) if entry and best_bid else None
        if pnl is not None:
            bucket["with_markout"] += 1
            bucket["sum_pnl_pct"] += pnl
            if pnl > 0.005:
                bucket["wins"] += 1
            elif pnl < -0.005:
                bucket["losses"] += 1
            if in_band:
                bucket["in_band_with_markout"] += 1
                bucket["sum_pnl_pct_in_band"] += pnl

    return {
        "approvals_per_agent": dict(approvals_per_agent),
        "edge_per_source": {k: {**v, "actions": dict(v["actions"])} for k, v in edge.items()},
        "days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def render_markdown(data: dict) -> str:
    days = data["days"]
    lines = [
        f"# Agent Edge Report — last {days} days",
        f"_Generated: {data['generated_at']}_",
        "",
        "## Approvals per agent (brain_decisions)",
        "",
        "| Agent | Approvals |",
        "|---|---:|",
    ]
    for agent, n in sorted(data["approvals_per_agent"].items(), key=lambda x: -x[1]):
        lines.append(f"| {agent} | {n} |")

    lines.extend(
        [
            "",
            "## Hypothetical 5-min edge per signal source",
            "",
            "Computed from `decision_journal` ENTER/SHADOW_ENTER rows with `outcome_5m_json`.",
            "PnL approximation: BUY → (best_bid / entry) − 1; SELL → ((1−best_bid) / (1−entry)) − 1.",
            "",
            "| Signal source | Decisions | In band | With markout | Wins | Losses | Avg PnL% | In-band PnL% |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    rows = sorted(
        data["edge_per_source"].items(),
        key=lambda x: -x[1]["decisions"],
    )
    for source, e in rows:
        avg = (100.0 * e["sum_pnl_pct"] / e["with_markout"]) if e["with_markout"] else None
        band_avg = (
            100.0 * e["sum_pnl_pct_in_band"] / e["in_band_with_markout"]
            if e["in_band_with_markout"]
            else None
        )
        avg_str = f"{avg:+.2f}%" if avg is not None else "—"
        band_avg_str = f"{band_avg:+.2f}%" if band_avg is not None else "—"
        lines.append(
            f"| {source[:60]} | {e['decisions']} | {e['in_band']} | "
            f"{e['with_markout']} | {e['wins']} | {e['losses']} | {avg_str} | {band_avg_str} |"
        )

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Only sources with **with_markout ≥ 30** carry statistical weight; smaller samples are noisy.",
            "- `in_band` = entry price in profit zone (BUY 0.40-0.49 or SELL 0.51-0.60).",
            "- 5m markout is a proxy for short-term edge, not a substitute for closed-trade PnL.",
            "- See `scripts/performance_tearsheet.py` for actual realised PnL.",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Per-agent hypothetical edge report")
    parser.add_argument("--db", default="data/trade_log.db")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--out", default=None, help="Markdown output path (default: stdout)")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of markdown")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"db not found: {args.db}", file=sys.stderr)
        return 2

    data = collect(args.db, args.days)
    text = json.dumps(data, indent=2, sort_keys=True) if args.json else render_markdown(data)

    if args.out:
        Path(args.out).write_text(text + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
