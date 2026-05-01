"""$100 simulation without LLM — what was AVAILABLE today + heuristic rank."""
import json
import sys
sys.path.insert(0, "/Users/mymac/coding/poly1")
import os
os.chdir("/Users/mymac/coding/poly1")
from dotenv import load_dotenv
load_dotenv("/Users/mymac/coding/poly1/.env")

from agents.polymarket.polymarket import Polymarket

pm = Polymarket(live=False)
events = pm.get_all_tradeable_events()
print(f"\nFetched {len(events)} active+open events from Polymarket gamma.\n")

# Walk events → markets → identify binary mid-priced (the realistic LLM candidates)
import requests
candidates = []
for ev in events:
    market_ids = (ev.markets or "").split(",")
    for mid in market_ids:
        if not mid:
            continue
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"id": mid},
                timeout=10,
            )
            data = r.json() if r.ok else []
            if not data:
                continue
            m = data[0] if isinstance(data, list) else data
            outcomes = m.get("outcomes")
            prices = m.get("outcomePrices")
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            if isinstance(prices, str):
                prices = json.loads(prices)
            if not (isinstance(outcomes, list) and len(outcomes) == 2):
                continue
            if not (isinstance(prices, list) and len(prices) == 2):
                continue
            try:
                p0 = float(prices[0])
                p1 = float(prices[1])
            except (TypeError, ValueError):
                continue
            if not (0.10 < p0 < 0.90):
                continue
            spread = m.get("spread")
            volume = m.get("volume") or 0
            liquidity = m.get("liquidity") or 0
            candidates.append({
                "question": m.get("question", "")[:90],
                "outcomes": outcomes,
                "prices": (p0, p1),
                "spread": spread,
                "volume": volume,
                "liquidity": liquidity,
                "endDate": m.get("endDate", "")[:10],
            })
        except (requests.RequestException, json.JSONDecodeError):
            continue
        if len(candidates) >= 30:
            break
    if len(candidates) >= 30:
        break

# Heuristic ranking (proxy for what a working LLM might prefer):
# - moderate certainty (price closer to 0.5 = more uncertainty = more potential edge)
# - higher volume
# - tighter spread
def edge_score(c):
    p0 = c["prices"][0]
    uncertainty = 1.0 - abs(p0 - 0.5) * 2  # 1.0 at p=0.5, 0.0 at extremes
    try:
        spread_pen = float(c["spread"] or 0.0) * 100
    except (TypeError, ValueError):
        spread_pen = 0.0
    try:
        vol = float(c["volume"]) if c["volume"] else 0.0
    except (TypeError, ValueError):
        vol = 0.0
    vol_boost = min(vol / 100000, 5)
    return uncertainty * 10 + vol_boost - spread_pen


candidates.sort(key=edge_score, reverse=True)
print(f"Found {len(candidates)} binary markets priced 10%-90% (LLM candidates):\n")
print(f"{'rank':<4}  {'price[0]':<8}  {'spread':<8}  {'volume':>12}  {'ends':<11}  question")
print("-" * 110)
for i, c in enumerate(candidates[:10], 1):
    p0 = c["prices"][0]
    try:
        sp = f"{float(c['spread']):.4f}" if c["spread"] is not None else "n/a"
    except (TypeError, ValueError):
        sp = "n/a"
    try:
        vnum = float(c["volume"]) if c["volume"] else 0.0
    except (TypeError, ValueError):
        vnum = 0.0
    vol = f"${vnum:>10,.0f}"
    print(f"{i:<4}  {p0:<8.3f}  {sp:<8}  {vol:>12}  {c['endDate']:<11}  {c['question']}")

print()
print("=" * 70)
print("Hypothetical $100 trades (top 3 by uncertainty + volume):")
print("=" * 70)
for i, c in enumerate(candidates[:3], 1):
    p0 = c["prices"][0]
    # If LLM agrees with market, it would skip (no edge). If LLM disagrees by ~10%
    # and confidence>=0.6, it would trade ~$5 (max_position_fraction=0.05 of $100).
    # We can't actually KNOW the LLM's view here.
    print(f"\n  Market {i}: {c['question']}")
    print(f"    Current YES price: {p0:.3f}  NO price: {c['prices'][1]:.3f}")
    print(f"    If LLM forecast > {p0+0.10:.2f}: would BUY YES at ~{p0:.3f}, size ~$5")
    print(f"    If LLM forecast < {p0-0.10:.2f}: would SELL YES (= BUY NO at ~{c['prices'][1]:.3f}), size ~$5")
    print(f"    If LLM forecast within ±10% of market: would skip (no edge)")
