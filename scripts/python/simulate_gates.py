"""Simulate the new data-driven gates on historical 7-day trades.

Apply:
  1. Disable Phase 1 (keep Phase 2 only)
  2. Trade only 11-17 UTC
  3. Spread < 0.02

Report: how many trades remain, predicted WR.
"""
from __future__ import annotations

import sys
sys.path.insert(0, "/srv/poly1/scripts/python")

from liquidity_analyzer import Analyzer  # type: ignore


def main() -> int:
    db = sys.argv[1] if len(sys.argv) > 1 else "/srv/poly1/data/trade_log.db"
    a = Analyzer(db, lookback_days=7)
    rows = a.build_dataset()

    closed = [r for r in rows if r["outcome"] in ("TP", "SL", "TIMEOUT")]
    print(f"Total closed trades (baseline): {len(closed)}")
    tp = sum(1 for r in closed if r["outcome"] == "TP")
    sl = sum(1 for r in closed if r["outcome"] == "SL")
    to = sum(1 for r in closed if r["outcome"] == "TIMEOUT")
    print(f"  TP={tp} SL={sl} TIMEOUT={to}  baseline WR = {tp/max(1,len(closed))*100:.1f}%")
    print()

    # Cumulative gating
    print("=== Cumulative gates applied ===")
    steps = [
        ("baseline",          lambda r: True),
        ("+ Phase 2 only",    lambda r: r["phase"] == "phase2"),
        ("+ hours 11-17 UTC", lambda r: r["phase"] == "phase2" and 11 <= r["hour_utc"] <= 17),
        ("+ spread < 2%",     lambda r: r["phase"] == "phase2"
                                       and 11 <= r["hour_utc"] <= 17
                                       and (r["snap_spread_pct"] is None
                                            or r["snap_spread_pct"] < 0.02)),
    ]
    for label, pred in steps:
        kept = [r for r in closed if pred(r)]
        kept_tp = sum(1 for r in kept if r["outcome"] == "TP")
        kept_sl = sum(1 for r in kept if r["outcome"] == "SL")
        kept_to = sum(1 for r in kept if r["outcome"] == "TIMEOUT")
        n = len(kept)
        wr = kept_tp / max(1, n)
        # Break-even at TP=5%/SL=20% = 0.20 / (0.05 + 0.20) = 0.80
        # With TP=7%/SL=10% break-even = 0.10 / (0.07 + 0.10) = 0.588
        # Use baseline TP=5%/SL=20% break-even since strategy is reverted to that.
        breakeven_5_20 = 0.80
        ev_per_trade = wr * 0.05 - (1 - wr - kept_to/max(1,n)) * 0.20
        print(f"  {label:25s} n={n:3d}  TP={kept_tp:3d}  SL={kept_sl:2d}  TO={kept_to:2d}  "
              f"WR={wr*100:>5.1f}%  break-even=80%  EV/trade ≈ ${ev_per_trade:+.3f}")

    print()
    print("=== Per-feature analysis on final filtered set ===")
    final = [r for r in closed if r["phase"] == "phase2"
             and 11 <= r["hour_utc"] <= 17
             and (r["snap_spread_pct"] is None or r["snap_spread_pct"] < 0.02)]
    # By hour
    print("\nWR by hour (filtered set):")
    by_hour = {}
    for r in final:
        h = r["hour_utc"]
        by_hour.setdefault(h, {"tp":0,"sl":0,"to":0})
        if r["outcome"] == "TP":
            by_hour[h]["tp"] += 1
        elif r["outcome"] == "SL":
            by_hour[h]["sl"] += 1
        else:
            by_hour[h]["to"] += 1
    for h in sorted(by_hour):
        b = by_hour[h]
        n = b["tp"]+b["sl"]+b["to"]
        print(f"  {h:>2d} UTC: n={n} TP={b['tp']} SL={b['sl']} TO={b['to']} WR={b['tp']/max(1,n)*100:.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
