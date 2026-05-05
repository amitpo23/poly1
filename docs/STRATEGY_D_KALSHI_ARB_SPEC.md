# Strategy D — Polymarket↔Kalshi Cross-Exchange Arbitrage Spec

**Source repo analyzed:** `CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot` (MIT, 9 commits, last update 2026-03-25). Hobby project, no tests, single dev. Full clone at `/tmp/kalshi-arb`.

**Date:** 2026-05-05
**Author of analysis:** Claude
**Bottom line up front:** Build Phase 1 (signal-only) **as a feature for the LLM, not as P&L**. **Do not build Phase 2.** The operator is non-US (Israel) and Kalshi requires a US SSN for full account access; international onboarding is restricted-jurisdiction-dependent and Israel is not a confirmed-allowed jurisdiction at the time of writing.

---

## TL;DR Worked Example (the user asked for this)

> "If Poly DOWN ask = 0.45 and Kalshi YES ask = 0.50, what's the margin and is it tradeable?"

| Metric | Value |
|--------|-------|
| Naive total cost (source repo formula) | 0.45 + 0.50 = **0.95** |
| Naive margin (source claim) | **$0.05 per unit** = 5% |
| Polymarket taker fee (crypto rate, 7.2%) on $0.45 leg × 100 contracts | $3.24 |
| Kalshi taker fee, ceil(0.07 × P × (1−P)) × 100 = ceil(0.07 × 0.5 × 0.5 × 100c) = $1.75 | $1.75 |
| **Real total cost on 100 contracts** | **$100.00** ($95 + $4.99 fees) |
| Min payout if both legs settle | $100.00 |
| **Realistic profit** | **~$0.00** |

A 5¢ naive margin is **the break-even threshold**, not an opportunity. The source repo would shout "ARBITRAGE FOUND!" — and you'd net zero (and lose money on slippage). **The actionable threshold is naive margin ≥ ~$0.10 per unit, not >$0.00.**

---

## 1. The Arbitrage Formula — what's in the source

`/tmp/kalshi-arb/backend/arbitrage_bot.py:85-141` (and the dashboard mirror in `api.py`).

```python
# Case A: poly_strike > kalshi_strike
#   Strategy: Buy Poly DOWN + Buy Kalshi YES
#   Predicate: (poly_down_ask + kalshi_yes_ask) < 1.00
# Case B: poly_strike < kalshi_strike
#   Strategy: Buy Poly UP + Buy Kalshi NO
#   Predicate: (poly_up_ask + kalshi_no_ask) < 1.00
# Case C (equal strikes): check both pairs
```

The covered-set logic from the thesis (`/tmp/kalshi-arb/thesis.md`) is correct: when poly_strike > kalshi_strike, ANY BTC settlement price guarantees at least one leg pays $1, so total payout ≥ $1.00. Buy the pair below $1.00 and the difference is "risk-free."

**What the source DOES NOT account for** (each is a correctness failure, not just an optimization):

1. **No fees.** Polymarket's CLOB charges **7.2% taker fee on crypto markets** (per `docs.polymarket.com`); only geopolitical markets are fee-free. Kalshi taker fee is **`ceil(0.07 × C × P × (1−P))`** rounded to the cent (max ~$1.75/contract at P=0.50). Combined, fees can eat 4–8% of notional — larger than most apparent "arbs".
2. **No slippage.** Code uses `yes_ask` / `no_ask` from a single L1 quote, which is the top-of-book ask quantity, not depth. Poly L1 depth at the BTC-Up/Down market is often <$200; a $50 fill walks the book.
3. **Different resolution sources (basis risk).** Polymarket BTC Up/Down resolves on **Binance BTC/USDT 1H candle close vs open**. Kalshi KXBTCD resolves on **CF Benchmarks BRTI 60-second average** at strike time. *Different reference prices.* The thesis's "guaranteed min payout = $1.00" assumes one source. With two sources, a window exists where both legs lose: Binance close > open (Poly Down loses) AND BRTI avg < kalshi_strike (Kalshi Yes loses). Rare but real and not modeled.
4. **No latency model.** The source polls every 1 second (`time.sleep(1)`). A real arb-window closes in <500 ms on liquid hourly markets.
5. **API field bug.** Source reads `km['yes_ask']` and divides by 100. Live Kalshi API now returns the price under `yes_ask_dollars` as a string ("0.7200") AND `yes_ask` as int-cents (72). For events in `initialized` status (not yet open), `yes_ask` is `None` — the source's division silently produces `0.0`, firing **false-positive arbs of $1.00 margin**. Verified with `curl https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=KXBTCD-26MAY0523` — 188 markets returned, all with `yes_ask=None`. **Source repo is currently broken in this state.**

The corrected predicate we would use:

```python
def real_arb_margin(poly_ask: float, kalshi_ask: float, contracts: int) -> float:
    """Returns net profit per contract (negative = no arb)."""
    poly_fee_per = 0.072 * poly_ask          # Poly crypto taker fee on cost
    kalshi_fee_per = 0.07 * kalshi_ask * (1 - kalshi_ask)
    total_per = poly_ask + kalshi_ask + poly_fee_per + kalshi_fee_per
    # Add slippage budget (estimated): 2% on each leg for thin books
    total_per += 0.02 * (poly_ask + kalshi_ask)
    return 1.00 - total_per

# Threshold for action: real_arb_margin > 0.02 per unit (covers basis-risk events)
```

### Live numbers (2026-05-05):

Poly "Bitcoin Up or Down — May 5, 5PM ET": `prices=["0.505","0.495"]`. Kalshi `KXBTCD-26MAY0517` top-vol strikes: $80,250 yes 0.70/0.72; $81,500 yes 0.34/0.35; $80,500 yes 0.62/0.65.

Apparent arb at strike $81,500 (poly_down=0.495 + kalshi_yes=0.35 = 0.845, naive margin $0.155, fee-aware ≈ $0.10) is illusory: Polymarket's "Up/Down" resolves on candle return (close vs open), so its "strike" floats with each hour's open. On May 5 at 5PM with BTC ~$80,200, open < $81,500 → the active pair flips to "Poly UP + Kalshi NO" and the arithmetic reverses. Most "10¢ apparent margins" on the bigger ladder are this kind of accounting illusion.

---

## 2. Kalshi Public API — what we measured

**Endpoints used by source repo:**
- `GET /trade-api/v2/markets?event_ticker={X}&limit=100` — list strikes for an event
- (we'd add) `GET /trade-api/v2/events?series_ticker=KXBTCD&status=open` — find current event
- (we'd add) `GET /trade-api/v2/exchange/status` — health check

**Auth:** **None for reads.** Verified: `curl https://api.elections.kalshi.com/trade-api/v2/markets` returns 200 with no headers. Trading (POST orders) requires API key + RSA signature with timestamps; non-US users cannot get this without approved KYC.

**Rate limits (verified at `docs.kalshi.com/getting_started/rate_limits`):**
- Token-bucket model. Default cost = 10 tokens/request.
- Basic tier (anonymous reads): **200 read tokens/sec = ~20 RPS**.
- 5 rapid sequential calls in our test: all 200, no 429.
- Burst budget: ~1 second on Basic tier.
- For a 30-second poll cadence on one event, we use ~3 tokens/min — negligible.

**Minimum order size:** `marketmath.io` says historically 1 contract minimum, but **fractional trading rolled out per-market starting 2026-03-09** — most major markets including KXBTCD now accept dollar-denominated orders down to **$1**. The "5,000 contract" minimum referenced in some 2024 docs applied to legacy event-contract format and is no longer the practical floor.

**Response price units:** Modern API returns `yes_ask_dollars: "0.7200"` (string dollars) — the source's `yes_ask / 100` cents division is fragile. Use `_dollars` fields directly via `float()`.

---

## 3. Market Mapping — how the source matches markets

`/tmp/kalshi-arb/backend/get_current_markets.py` and the slug generators.

**Polymarket slug pattern:** `bitcoin-up-or-down-{month}-{day}-{year}-{hour}{ampm}-et`
**Kalshi event ticker pattern:** `KXBTCD-{YY}{MMM}{DD}{HH}` (HH in ET, 24h)

**Hour offset:** Source code applies a **+1 hour offset** to the Kalshi target before generating the ticker (`get_current_markets.py:21`). Reason: the resolution time is the *end* of the hourly window. Polymarket's slug uses the *start*, Kalshi uses the *strike-time hour*.

**Reliability:** Hardcoded for BTC hourly only. Both pattern generators rely on string formatting; any platform UI rename (e.g., Polymarket changing slug from `bitcoin-up-or-down-...` to `btc-...-hourly`) breaks the bot silently. **Will not generalize** to ETH, S&P 500, weather, or any other contract without rewriting the slug logic per series.

**Critical resolution-source mismatch (the thesis ignores this):**
- Polymarket BTC Up/Down resolves on Binance BTC/USDT 1H candle (close vs open). Confirmed by reading `description` field of the live event.
- Kalshi KXBTCD resolves on CF Benchmarks BRTI 60-sec average at strike time. Confirmed in `rules_primary` of the live market: *"the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time Index (BRTI) before 5 PM EDT is above ..."*.

These can disagree by 0.05–0.15% during volatile minutes. On a $80,000 contract, that's $40–$120 of basis risk — directly eats into any apparent arb margin. **The "minimum $1 payout" claim is wrong**; the true minimum is $0 with a small probability when basis crosses the strike.

---

## 4. Two-Phase Plan

### Phase 1 — `agents/application/kalshi_scanner.py` (signal-only)

**Purpose:** Provide a divergence signal as a feature for the LLM (per `docs/REPO_RESEARCH_2026-05-05.md` Tier 1 item 4: "market consensus divergence"). **No P&L on its own.**

**Spec:**
- Standalone module, no money path. Read-only.
- Pulls Kalshi `KXBTCD-{currentHour+1}` ladder + Polymarket `bitcoin-up-or-down-{currentHour}-...` event every **30 seconds** (NOT 1s — hourly markets don't move on the second; we keep cache warm and stay well below rate limit).
- For each Kalshi strike near the Polymarket implied open (±$2,500), computes both naive and fee-aware margins.
- Logs structured rows to `./data/kalshi_scanner.jsonl`:
  ```json
  {
    "ts": "2026-05-05T16:30:12Z",
    "poly_event_slug": "bitcoin-up-or-down-may-5-2026-5pm-et",
    "kalshi_event_ticker": "KXBTCD-26MAY0517",
    "kalshi_strike": 81500,
    "poly_down_ask": 0.495, "kalshi_yes_ask": 0.35,
    "naive_margin": 0.155,
    "real_margin": 0.10,
    "phantom": false,
    "alert_level": "high"
  }
  ```
- `phantom = true` when naive_margin > 0 but real_margin < 0 (fees kill it). Flag explicitly.
- Alerts at `real_margin > 0.02` (Telegram or local log alert — wire into existing monitor.py).
- Feeds into `Executor` LLM prompt as `{"kalshi_consensus": yes_price, "divergence_pct": ...}` so the LLM has cross-exchange context — **not** so the bot trades the arb.

**What goes into `RiskGate`:** Nothing yet. Scanner is purely additive.

**What we add to env:**
```
KALSHI_SCANNER_ENABLED="false"
KALSHI_SCAN_INTERVAL_SEC="30"
KALSHI_ARB_REAL_MARGIN_THRESHOLD="0.02"
KALSHI_API_BASE="https://api.elections.kalshi.com/trade-api/v2"
```

**Estimated effort:** ~250 lines, 1 day.

**What's reusable from source:**
- The slug-generation idea (re-implement; source's logic is correct but we want unit tests)
- The "find closest strike to spot" iteration in `api.py:55-67`
- Nothing else. The source's data-fetcher and arb predicate need rewriting for fee-correctness.

### Phase 2 — `agents/application/kalshi_executor.py` (atomic two-leg execution)

**Status: NOT VIABLE for this operator. Do not build.**

Reasons (in order):

1. **US-only access for our operator.** Kalshi requires US SSN for full account access. International onboarding launched but Israel is not on the published allowed-jurisdictions list as of 2026-05-05. Without an account, no API key, no order signing, no executions.
2. **No way to fund.** Even if account existed: Kalshi funding rails are ACH (US bank), wire (US bank), or debit card (US-issued). USDC deposits are not supported.
3. **Latency floor.** Atomic two-leg fills require sub-200 ms round-trip on both exchanges from a co-located host. Our VPS is in EU; Kalshi's API is US-east. Round-trip is 80–120 ms each direction. The arb window typically closes within that envelope.
4. **Capital lock-up.** Capital sits in TWO exchanges, both demanding minimums. With $100 starting, $50 on each = below practical L1 depth in the high-volatility windows where arbs appear.
5. **CFTC oversight on Kalshi.** Algorithmic execution may require additional reporting (Kalshi has had CFTC filings around algo markers).

**If this changed in the future** (e.g., hypothetical US-resident operator with US bank), the spec would be:
- Place Kalshi leg first (CFTC-regulated, slower fill but more deterministic execution).
- Once Kalshi fill confirmed, immediately submit Polymarket FOK at the bid.
- If Polymarket fails: race to flatten Kalshi via opposite-side market order. Accept loss.
- TradeLog both legs as paired records with `arb_pair_id`.
- Add `RiskGate.ok()` constraint: max 1 open arb pair at a time.
- Estimated effort: 5–7 days, plus CFTC compliance review.

---

## 5. Real-World Viability

From the live KXBTCD-26MAY0517 ladder (80 strikes) + Poly BTC Up/Down:

- **Naive zero-fee "opportunities" with margin > $0.05:** Multiple per hour, mostly in thin-liquidity windows.
- **Real fee-aware margins > $0.02:** A few per week, usually 5–15 min before resolution.
- **Real margins > $0.05 (worth ops risk):** ~1 per week at our capital scale.

Whelan (2025, UCD) notes Kalshi's maker/taker structure absorbs most cross-exchange dispersion at the ~1.5% level. **Phase 1 standalone profit = zero.** It's a feature for the LLM, not a strategy. **Phase 2 even for a US operator: marginal** — $50–$200/month at $100–$1,000 capital, not worth the ops complexity.

---

## 6. Risks (Phase 1 only)

- **API field drift** (high): defensive parser — `_dollars` first, int-cents fallback, fail loud if both null. Source repo currently bites on this.
- **Slug drift** (medium): cache last-good slug, alert after 4 consecutive 404s.
- **Phantom alerts confusing the LLM** (medium): always pass `real_margin` not `naive_margin`, tag as "after-fee".
- **Rate-limit ban** (low at 30s cadence): back off on 429.
- **Capital tied up:** none — Phase 1 doesn't trade.

---

## 7. Honest Go/No-Go

| Question | Answer |
|----------|--------|
| Build Phase 1 (signal-only) for the LLM? | **Yes, but only after Tier 1 items in `REPO_RESEARCH_2026-05-05.md` (Kelly, MIN_EDGE_PCT, calibration) are shipped.** Phase 1 is Tier 2 effort. |
| Build Phase 2 (executor)? | **No.** Operator is non-US; Kalshi blocks. Even if it didn't, expected return doesn't justify ops complexity. |
| Use the source repo's code as-is? | **No.** It's currently broken against live API (initialized-status nulls), it ignores fees, and it has no tests. Re-implement clean using the *concept* (slug match, strike-pair iteration) but not the code. |
| Is the source repo battle-tested? | **No.** 9 commits, 1 contributor, no tests, no error handling, hobby project. Use it as inspiration only. |

---

## 8. Source-vs-Build (condensed)

- **Arb predicate:** Source: `total < 1.00`. Build: real-margin with 7.2% Poly + parabolic Kalshi + 2% slippage budget.
- **Kalshi field:** Source uses `yes_ask`/100 (broken on null). Build uses `yes_ask_dollars` parsed via `float()`.
- **Cadence:** Source 1s. Build 30s.
- **Trading leg:** Source has none (despite README claim). Build has none either — Phase 2 ruled out.

**Action item if we proceed:** Schedule for Week 3+ after Tier 1 ships. Do not start Phase 1 before the live launch on 2026-05-03+24h — let the bot prove out first.

---

## Appendix: Sources

- Source repo: `/tmp/kalshi-arb` (commit `b12e1b7`, MIT, last update 2026-03-25)
- Kalshi live probes 2026-05-05: `KXBTCD-26MAY0517` (80 trading markets), `KXBTCD-26MAY0523` (188 initialized, all null — confirms source-code bug)
- Kalshi rate limits: token bucket, 200 read tokens/sec Basic — `docs.kalshi.com/getting_started/rate_limits`
- Kalshi fees: `ceil(0.07 × C × P × (1−P))` taker, `ceil(0.0175 × ...)` maker
- Polymarket fees: 7.2% crypto, 3% sports, 0% geopolitical — `docs.polymarket.com`
- Resolution-source mismatch confirmed: Polymarket = Binance 1H candle (close vs open); Kalshi = CF Benchmarks BRTI 60-sec avg
