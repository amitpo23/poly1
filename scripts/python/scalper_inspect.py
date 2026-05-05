"""CLI: list recent scalper pairs and a P&L summary."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agents.application.trade_log import TradeLog, SCALPER_LEG
from agents.application.scalper_pairs import ScalperPairsDAO


def main() -> None:
    ap = argparse.ArgumentParser(description="Inspect scalper pairs and leg spend")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--db", default=None, help="Path to trade_log.db")
    args = ap.parse_args()

    tl = TradeLog(db_path=args.db) if args.db else TradeLog()
    dao = ScalperPairsDAO(tl)

    rows = dao.list_recent(limit=args.limit)
    print(f"--- last {len(rows)} scalper pairs ---")
    for r in rows:
        net_cost = r["cost_up"] + r["cost_down"]
        sum_avg = "n/a"
        if r["qty_up"] > 0 and r["qty_down"] > 0:
            avg_up = r["cost_up"] / r["qty_up"]
            avg_dn = r["cost_down"] / r["qty_down"]
            sum_avg = f"{avg_up + avg_dn:.4f}"
        print(f"  {r['slug']:<40s} state={r['state']:<14s} "
              f"qty=({r['qty_up']:.2f},{r['qty_down']:.2f}) "
              f"cost=${net_cost:.2f} sum_avg={sum_avg}")

    legs = tl.recent(limit=args.limit * 4)
    scalper_legs = [l for l in legs if l["status"] == SCALPER_LEG]
    spent = sum(l["size_usdc"] or 0 for l in scalper_legs)
    print(f"\n--- {len(scalper_legs)} scalper legs (last {args.limit * 4} rows) ---")
    print(f"Total spent: ${spent:.2f}")


if __name__ == "__main__":
    main()
