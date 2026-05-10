#!/usr/bin/env python3
"""CLI for the read-only CapitalAllocator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.application.capital_allocator import CapitalAllocator  # noqa: E402


def _print_text(report: dict) -> None:
    print("# Capital allocation recommendation")
    print(f"generated_at={report['generated_at']}")
    print(f"window_hours={report['window_hours']}")
    print(f"total_budget_usdc=${report['total_budget_usdc']:.2f}")
    if report["warnings"]:
        print("\n## warnings")
        for warning in report["warnings"]:
            print(f"- {warning}")
    intel = report.get("market_intelligence") or {}
    if intel:
        print("\n## market intelligence")
        crypto = intel.get("crypto") or {}
        fresh_crypto = [k for k, v in crypto.items() if v.get("fresh")]
        print(
            f"- crypto_fresh={','.join(fresh_crypto) or 'none'} "
            f"gamma_crypto_markets={intel.get('gamma_crypto_markets', 0)} "
            f"avg_liquidity=${float(intel.get('gamma_avg_liquidity_usd') or 0):.0f} "
            f"avg_volume24=${float(intel.get('gamma_avg_volume_24h_usd') or 0):.0f} "
            f"news_signals={intel.get('fresh_news_signals', 0)} "
            f"brain_approvals={intel.get('fresh_brain_approvals', 0)}"
        )
    print("\n## agents")
    for agent in report["agents"]:
        reasons = ", ".join(agent["reasons"]) or "clean"
        live = "yes" if agent["live_allowed"] else "no"
        wr = agent.get("win_rate")
        wr_str = f"{wr*100:.1f}%" if wr is not None else "n/a"
        wl = f"{agent.get('wins', 0)}/{agent.get('losses', 0)}"
        print(
            f"- {agent['agent']}: recommend=${agent['recommended_usdc']:.2f} "
            f"score={agent['score']:.3f} live_allowed={live} "
            f"decisions={agent['decisions']} entries={agent['entries']} "
            f"exits={agent['exits']} errors={agent['errors']} "
            f"stale={agent['stale_state']} pnl=${agent['realized_pnl_usdc'] + agent['paper_pnl_usdc']:.2f} "
            f"W/L={wl} WR={wr_str} "
            f"market={agent.get('market_score', 0):.2f} "
            f"reasons=[{reasons}]"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--budget", type=float, default=20.0)
    parser.add_argument("--poly-db", default="./data/trade_log.db")
    parser.add_argument("--swarm-db", default="~/Desktop/poly/bot/data/swarm.db")
    parser.add_argument("--min-allocation", type=float, default=0.0)
    parser.add_argument("--max-allocation", type=float)
    parser.add_argument(
        "--no-market-intel",
        action="store_true",
        help="disable live market intelligence feeds; DB-only scoring",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args()

    allocator = CapitalAllocator(
        poly_db=args.poly_db,
        swarm_db=args.swarm_db,
        total_budget_usdc=args.budget,
        window_hours=args.hours,
        min_allocation_usdc=args.min_allocation,
        max_allocation_usdc=args.max_allocation,
        include_market_intelligence=not args.no_market_intel,
    )
    report = allocator.build_report().as_dict()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
