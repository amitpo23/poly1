"""Comprehensive retro for May 1 2026 — given the actual paper-trade context.

Inputs:
- 30-day backtest results from /Users/mymac/Desktop/poly/bot/backtest/results_30day.json
- Open position from swarm.db (nothing_happens agent on WTI≥$90 May)
- 5-day dry-run plan from 03_יומן_מסחר_נייר.xlsx
- Live Polymarket prices for the markets in question (today)

The user's actual setup is the swarm bot ($150 across 3 sub-bots), not poly1.
This retro projects what $100 (proportionally) on May 1 would have produced
across the three strategies + the existing open NO position.
"""
import json
import requests
from datetime import datetime, timezone


# ---- 1. 30-day backtest baseline ----
with open("/Users/mymac/Desktop/poly/bot/backtest/results_30day.json") as f:
    bt = json.load(f)

print("=" * 72)
print("POLY1 RETRO — 1 May 2026, $100 capital, scaled from swarm $150 plan")
print("=" * 72)
print()
print("📊 30-day backtest baseline (run 30 Apr 2026, $50/sub-bot = $150 total):")
for name, b in bt["bots"].items():
    n_trades = b["trades"]
    pnl = b["pnl"]
    wr = b["win_rate"]
    avg = pnl / max(n_trades, 1)
    daily_avg = pnl / 30.0
    print(f"  {name:<14}  {n_trades:>3} trades  WR={wr:<4}  PnL=${pnl:>+8.2f}  "
          f"avg/trade=${avg:>+6.2f}  ~${daily_avg:>+5.2f}/day")
print(f"  {'TOTAL':<14}  {bt['total']['trades']:>3} trades  "
      f"PnL=${bt['total']['pnl']:>+8.2f}  monthly_ROI={bt['total']['monthly_roi_pct']}%")


# ---- 2. Open position from swarm.db ----
import sqlite3
con = sqlite3.connect("/Users/mymac/Desktop/poly/bot/data/swarm.db")
con.row_factory = sqlite3.Row
positions = []
for r in con.execute("SELECT * FROM agent_state WHERE agent='nothing_happens'"):
    payload = json.loads(r["payload"])
    for pos in payload.get("positions", {}).values():
        positions.append(pos)
con.close()

print()
print("📂 Open positions in swarm.db (nothing_happens agent):")
if not positions:
    print("  (none)")
for p in positions:
    print(f"  • {p['question']}")
    print(f"    slug={p['slug']}  size=${p['size_usd']}")
    print(f"    NO entry price=${p['no_entry_price']:.4f}  "
          f"end={p['end_date_iso'][:10]}  filled={p['filled']}")


# ---- 3. Live mark of those positions today ----
print()
print("📈 Mark-to-market today (live Polymarket prices):")
for p in positions:
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": p["slug"]},
            timeout=10,
        )
        if r.ok:
            data = r.json()
            m = data[0] if isinstance(data, list) and data else None
            if m:
                prices = m.get("outcomePrices")
                outcomes = m.get("outcomes")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                # We hold NO; what's NO price now?
                no_idx = outcomes.index("No") if "No" in outcomes else 1
                no_now = float(prices[no_idx])
                size_usd = p["size_usd"]
                shares = size_usd / max(p["no_entry_price"], 0.0001)
                value_now = shares * no_now
                pnl = value_now - size_usd
                print(f"  • {m.get('question', '')[:60]}")
                print(f"    NO entry=${p['no_entry_price']:.4f}  NO now=${no_now:.4f}  "
                      f"shares={shares:,.1f}")
                print(f"    Position value today: ${value_now:.2f}  (paper PnL: ${pnl:+.2f})")
    except (requests.RequestException, json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"  • {p['slug']}: lookup failed — {e}")


# ---- 4. What May 1 would produce, statistically, on $100 ----
print()
print("🎲 Statistical projection for May 1 on $100 (scaled from $150 plan):")
scale = 100.0 / 150.0
for name, b in bt["bots"].items():
    daily_pnl = (b["pnl"] / 30.0) * scale
    n_per_day = b["trades"] / 30.0
    print(f"  {name:<14}  expected: ${daily_pnl:>+6.2f}/day  "
          f"(scaled), ~{n_per_day:.1f} trades/day on average")

total_daily = (bt["total"]["pnl"] / 30.0) * scale
print(f"  {'TOTAL':<14}  expected: ${total_daily:>+6.2f}/day on $100 (avg over 30d)")


# ---- 5. Tail risk vs typical day (from xlsx plan) ----
print()
print("📉 Daily distribution from the xlsx plan (scaled to $100):")
scenarios = [
    ("routine day (~50%)", 3 * scale),
    ("medium day (~30%)", 6 * scale),
    ("good day (~15%)", 10 * scale),
    ("excellent (~5%)", 18 * scale),
    ("BAD day (~10%)", -12 * scale),
]
for name, pnl in scenarios:
    print(f"  {name:<22}  ${pnl:>+6.2f}")

# Expected value
ev = 0.50*3 + 0.30*6 + 0.15*10 + 0.05*18 - 0.10*12  # weighted on $150
ev_100 = ev * scale
print(f"  weighted EV:           ${ev_100:>+6.2f}/day on $100")


# ---- 6. Honest caveats ----
print()
print("=" * 72)
print("Honest caveats")
print("=" * 72)
print("• The swarm bot was last running 30 Apr 23:54. It was OFF on 1 May.")
print("  These numbers project what would have happened IF it had run today.")
print("• 30d backtest is heavily lifted by Bot3-WTI (+$281), masking that")
print("  Bot1+Bot2 lost money. If WTI vol drops, Bot3 alpha decays.")
print("• Bot1+Bot2 had only 7 combined trades in 30d — sample too small to trust.")
print("• The 'BAD day' scenario in the plan is -$12 on $150 (-8%). On $100")
print(f"  that's ~${12*scale:.0f}. Tail risk on a single day is real.")
print("• Today's open NO position on WTI≥$90 (entry $0.0065) is mark-to-market")
print("  positive only because NO≈$1; it locks $50 till market resolution (May 31).")
