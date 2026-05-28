"""Daily BTC up/down market edge tracker.

PURPOSE
-------
Records what-if shadow data for the daily Polymarket bitcoin-up-or-down
markets. Tracks the "buy DOWN at N hours before close" hypothesis we
identified from a 27-market backtest showing structural +5-9% EV.

USAGE
-----
Run once per day (e.g., 17:00 UTC, 1h after market close):

    python3 scripts/daily_btc_edge_tracker.py [--backfill 60]

The --backfill flag pulls N days back at first run; subsequent runs only
add the latest day. Stats accumulate in data/daily_btc_edge.csv.

NO MONEY IS PLACED. This is data collection only.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

UA = "poly1-edge-tracker/1.0"
GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB_HISTORY = "https://clob.polymarket.com/prices-history"
ANCHORS = [1, 2, 3, 4, 6, 8, 12, 18, 24, 36, 47]
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "daily_btc_edge.csv"
CSV_FIELDS = [
    "market_date", "slug", "outcome_up", "outcome_yes_final",
    *(f"yp_{h}h" for h in ANCHORS),
]


def fetch(url: str, timeout: float = 30.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def slug_for(d) -> str:
    return f"bitcoin-up-or-down-on-{d.strftime('%B').lower()}-{d.day}-{d.year}"


def load_existing() -> set:
    if not DATA_PATH.exists():
        return set()
    with DATA_PATH.open() as f:
        rdr = csv.DictReader(f)
        return {row["market_date"] for row in rdr}


def append_row(row: dict) -> None:
    write_header = not DATA_PATH.exists()
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DATA_PATH.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            w.writeheader()
        w.writerow(row)


def process_one_market(market_date) -> dict | None:
    """Returns a CSV row for this market, or None if not yet closed."""
    slug = slug_for(market_date)
    r = fetch(f"{GAMMA}?slug={slug}&closed=true")
    if not r:
        return None
    m = r[0]
    end_dt = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
    prices_final = json.loads(m.get("outcomePrices", "[]"))
    if not prices_final:
        return None
    yes_final = float(prices_final[0])
    outcome_up = yes_final > 0.5
    tokens = json.loads(m.get("clobTokenIds", "[]"))
    if not tokens:
        return None
    h = fetch(f"{CLOB_HISTORY}?market={tokens[0]}&interval=1m&fidelity=60")
    hist = h.get("history", [])
    if len(hist) < 6:
        return None

    by_anchor: dict[int, float] = {}
    for entry in hist:
        dt = datetime.fromtimestamp(entry["t"], timezone.utc)
        hrs_to_close = (end_dt - dt).total_seconds() / 3600
        for anchor in ANCHORS:
            if anchor in by_anchor:
                continue
            if abs(hrs_to_close - anchor) < 0.5:
                by_anchor[anchor] = float(entry["p"])

    row = {
        "market_date": market_date.isoformat(),
        "slug": slug,
        "outcome_up": int(outcome_up),
        "outcome_yes_final": yes_final,
    }
    for anchor in ANCHORS:
        row[f"yp_{anchor}h"] = by_anchor.get(anchor, "")
    return row


def compute_stats():
    if not DATA_PATH.exists():
        print("no data yet")
        return
    rows = []
    with DATA_PATH.open() as f:
        for r in csv.DictReader(f):
            rows.append(r)
    n = len(rows)
    if n == 0:
        print("no data yet")
        return
    n_up = sum(1 for r in rows if r["outcome_up"] == "1")
    print(f"\n=== running stats: n={n} markets ===")
    print(f"  base rate: {n_up}/{n} UP ({n_up / n * 100:.1f}%)")

    print(f"\n--- 1) DOWN bet by anchor (the 'late mispricing' signal) ---")
    print(f"{'anchor':<7} {'n':<4} {'avg_DOWN_price':<14} {'WR':<7} {'EV/$1':<10}")
    for anchor in ANCHORS:
        col = f"yp_{anchor}h"
        valid = [(float(r[col]), int(r["outcome_up"])) for r in rows if r.get(col, "") != ""]
        if not valid:
            continue
        wins = sum(1 for _yp, up in valid if up == 0)
        pnl = sum((1 - (1 - yp)) if up == 0 else -(1 - yp) for yp, up in valid)
        avg_down_price = sum(1 - yp for yp, _ in valid) / len(valid)
        print(f"  {anchor:2d}h    {len(valid):<4} {avg_down_price:14.3f} {wins / len(valid) * 100:5.1f}%  ${pnl / len(valid):+.4f}")

    print(f"\n--- 2) UP bet by anchor (Amit P2 'BUY UP late' direction) ---")
    print(f"{'anchor':<7} {'n':<4} {'avg_UP_price':<14} {'WR':<7} {'EV/$1':<10}")
    for anchor in ANCHORS:
        col = f"yp_{anchor}h"
        valid = [(float(r[col]), int(r["outcome_up"])) for r in rows if r.get(col, "") != ""]
        if not valid:
            continue
        wins = sum(1 for _yp, up in valid if up == 1)
        pnl = sum((1 - yp) if up == 1 else -yp for yp, up in valid)
        avg_up_price = sum(yp for yp, _ in valid) / len(valid)
        print(f"  {anchor:2d}h    {len(valid):<4} {avg_up_price:14.3f} {wins / len(valid) * 100:5.1f}%  ${pnl / len(valid):+.4f}")

    # Amit translation to daily (47h cycle):
    #   Phase 1 = SELL DOWN at t=0 (= bet DOWN at 47h-to-close = market open)
    #   Phase 2 = BUY  UP   at ~60% through cycle (= ~18h-to-close)
    print(f"\n--- 3) Amit strategy simulated on daily (P1 + P2 combined) ---")
    p1_valid = [(float(r["yp_47h"]), int(r["outcome_up"])) for r in rows if r.get("yp_47h", "") != ""]
    p2_valid = [(float(r["yp_18h"]), int(r["outcome_up"])) for r in rows if r.get("yp_18h", "") != ""]
    if p1_valid:
        p1_pnl = sum((1 - (1 - yp)) if up == 0 else -(1 - yp) for yp, up in p1_valid)
        p1_wins = sum(1 for _yp, up in p1_valid if up == 0)
        print(f"  P1 SELL DOWN @ open (47h):   n={len(p1_valid):<3} WR={p1_wins / len(p1_valid) * 100:5.1f}%  EV=${p1_pnl / len(p1_valid):+.4f}")
    if p2_valid:
        p2_pnl = sum((1 - yp) if up == 1 else -yp for yp, up in p2_valid)
        p2_wins = sum(1 for _yp, up in p2_valid if up == 1)
        print(f"  P2 BUY  UP   @ 18h-to-close: n={len(p2_valid):<3} WR={p2_wins / len(p2_valid) * 100:5.1f}%  EV=${p2_pnl / len(p2_valid):+.4f}")
    if p1_valid and p2_valid:
        # Combined per-market: bet $0.50 P1 + $0.50 P2 = $1 total exposure
        total_pnl = 0.5 * p1_pnl + 0.5 * p2_pnl
        n_combined = min(len(p1_valid), len(p2_valid))
        print(f"  COMBINED ($0.50 each leg):   n={n_combined:<3}            EV=${total_pnl / max(1, n_combined):+.4f}")

    # Filtered DOWN-at-3h: only trade if market still uncertain (0.15 < yp < 0.85)
    print(f"\n--- 4) DOWN @ 3h-to-close — FILTERED (only if 0.15 < YES < 0.85) ---")
    for lo_hi in [(0.15, 0.85), (0.20, 0.80), (0.30, 0.70), (0.40, 0.60)]:
        lo, hi = lo_hi
        valid = [(float(r["yp_3h"]), int(r["outcome_up"])) for r in rows
                 if r.get("yp_3h", "") != "" and lo < float(r["yp_3h"]) < hi]
        if not valid:
            print(f"  band [{lo:.2f}, {hi:.2f}]: n=0")
            continue
        wins = sum(1 for _yp, up in valid if up == 0)
        pnl = sum((1 - (1 - yp)) if up == 0 else -(1 - yp) for yp, up in valid)
        avg_down = sum(1 - yp for yp, _ in valid) / len(valid)
        avg_win_size = (sum((1 - (1 - yp)) for yp, up in valid if up == 0) / wins) if wins else 0
        avg_loss_size = (sum(-(1 - yp) for yp, up in valid if up == 1) / (len(valid) - wins)) if (len(valid) - wins) else 0
        print(f"  band [{lo:.2f}, {hi:.2f}]: n={len(valid):<3} WR={wins / len(valid) * 100:5.1f}% "
              f"avg_DOWN_entry={avg_down:.3f} EV=${pnl / len(valid):+.4f} "
              f"win=+${avg_win_size:.3f} loss={avg_loss_size:.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0,
                    help="Days back to backfill (default 0 = just process yesterday)")
    ap.add_argument("--stats-only", action="store_true",
                    help="Skip fetching; just print stats from existing CSV")
    args = ap.parse_args()

    if args.stats_only:
        compute_stats()
        return

    existing = load_existing()
    today = datetime.now(timezone.utc).date()
    days_back = max(1, args.backfill) if args.backfill else 1
    # Include today (just-closed market at 16:00 UTC) AND lookback days.
    days_to_process = [today - timedelta(days=i) for i in range(0, days_back + 1)]
    new_count = 0
    for d in days_to_process:
        if d.isoformat() in existing:
            continue
        try:
            row = process_one_market(d)
        except Exception as e:
            print(f"  {d.isoformat()}: error {e!r}", file=sys.stderr)
            continue
        if row is None:
            continue
        append_row(row)
        new_count += 1
        print(f"  + {d.isoformat()} outcome_up={row['outcome_up']} yp_3h={row.get('yp_3h')}")
        time.sleep(0.25)

    print(f"\nAdded {new_count} new markets")
    compute_stats()


if __name__ == "__main__":
    main()
