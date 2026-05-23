# SESSION 2026-05-23 — Loss Root-Cause Analysis

---

## ⚠️ CORRECTION 2026-05-23 LATE NIGHT

The "TL;DR" claim that **PnL is positive +$6.54 / +4.89% ROI** is WRONG.
A formula bug (using `exit_price/entry_price-1` for SELL trades) inflated
results. Corrected via `response_json.pnl_usdc_real`:

| Metric | Original (BUGGY) | **Corrected** |
|---|---|---|
| Win rate | 29.5% | **22.1%** |
| Loss rate | 70.5% | **77.9%** |
| Total PnL 30d | +$6.54 | **−$1.92** |
| ROI | +4.89% | **−1.41%** |

**The bot lost money in net over 30 days.** See
`docs/SESSION_2026-05-23_DEEPER_DRILL.md` §"CORRECTION" for details.

What survives: the qualitative findings about execution failures
(64.5% of terminal events), the OpenAI bug fix (51 failures → 0 after
deploy), the favorite-longshot pattern in 0.50+ bands.

What's overturned: the asymmetric-payoff VC-style framing. The bot's
realized PnL was negative, not positive-with-noisy-distribution.

---

# ORIGINAL ANALYSIS (preserved for audit — contains buggy PnL formula)

**Question (operator):** *למה 72 או מעל 70 אחוז מהטריידים שלנו היו הפסדים?*

**TL;DR:** ה־70% loss rate הוא **לא** ה־problem. הוא תוצר של asymmetric payoff
distribution (הרבה הפסדים קטנים + מעט ניצחונות גדולים). ה־**PnL נטו חיובי**
(+$6.54 ב־30 ימים, ROI +4.89%). ה־problem האמיתי הוא **execution failures**
(53.7% מכל ה־terminal events) ו־**catastrophic zones** (0.80+ band, sports
markets).

---

## 1. Headline numbers

**Per-trade decomposition (last 30 days, 95 closed trades with entry+exit join):**

| Metric | Value |
|---|---|
| Closed trades | 95 |
| Wins (PnL > 0) | 28 (29.5%) |
| Losses (PnL ≤ 0) | 67 (**70.5%**) ← the user's number |
| Sum PnL | **+$6.54** |
| Capital deployed | $133.56 |
| ROI | **+4.89%** (positive!) |
| Mean per-trade PnL% | +3.52% |
| Median per-trade PnL% | −2.20% |
| Best | +497.84% |
| Worst | −82.71% |

The MEAN is positive (+3.52%) while the MEDIAN is negative (−2.20%) — classic
right-skewed payoff. The few large wins (+30% to +497%) pull the mean up while
the many small losses (−3% to −10%) dominate the count.

**This is a VC-style payoff distribution, not a "70% of the time we lose
money in net" situation.**

---

## 2. PnL histogram

| Bucket | Count |
|---|---|
| < −30% | 8 |
| −30..−10% | 6 |
| −10..−3% | 25 |
| −3..0% | 28 |
| 0..3% | 6 |
| 3..10% | 9 |
| 10..30% | 5 |
| > +30% | **8** |

The 8 trades with > +30% gain include one at +497% (an extreme longshot
hit) and several large recoveries. These 8 trades alone generated more
gross profit than the 14 trades with > −10% loss destroyed.

---

## 3. Where the losses concentrate

### 3a. By entry-price band

| Band | n | wins | win% | PnL | ROI% |
|---|---|---|---|---|---|
| <0.20 | 19 | 4 | 21.1% | +$8.77 | **+30.97%** ✅ |
| 0.20-0.30 | 3 | 0 | 0.0% | −$0.28 | −2.86% |
| 0.30-0.40 | 11 | 4 | 36.4% | +$4.51 | **+33.20%** ✅ |
| **0.40-0.50** | **17** | **9** | **52.9%** | **+$2.09** | **+8.98%** ✅ |
| 0.50-0.60 | 34 | 9 | 26.5% | +$0.05 | +0.15% (break-even) |
| 0.60-0.70 | 7 | 2 | 28.6% | −$2.89 | **−17.20%** ❌ |
| **0.80+** | **4** | **0** | **0.0%** | **−$5.72** | **−80.66%** 🔴 |

**Pattern: the bot's edge is in the LOW bands (longshots) and 0.40-0.50.
The high bands (0.60+) are systematic losses.** Entries at 0.80+ are
catastrophic — paying near-certainty price for the losing side.

This is the empirical favorite-longshot bias on Polymarket.

### 3b. By signal_source

| Source | n | wins | win% | ROI% |
|---|---|---|---|---|
| **unknown** | 76 | 22 | 28.9% | +6.09% |
| opportunity_factory,alphainsider_proven,crypto_tap | 16 | 5 | 31.2% | **−1.82%** |
| meta_brain,manifold,manifold:manifold | 3 | 1 | 33.3% | −0.11% |

The **unknown source** (76/95 = 80%) doesn't link cleanly to a brain_decisions
row. Could be: brain_decisions row inserted with empty signal_source, or
trades from direct execute paths bypassing the decision journal. Worth
digging into in Tier 1.

The `opportunity_factory_alphainsider_proven_crypto_tap` path has 31% win
rate but **negative ROI** — the wins are too small to offset the losses.
This is the path my Tier 0b fix (commit `31d1297` C-2 wallet) addresses
indirectly (by removing the hardcoded `calibrated=True` that lowered the
score floor).

### 3c. By hold duration

| Bucket | n | wins | win% | ROI% |
|---|---|---|---|---|
| <1min | 25 | 8 | 32.0% | +4.05% |
| **1-5min** | **15** | **3** | **20.0%** | **−3.23%** ⚠️ (adverse selection signature) |
| 5-15min | 23 | 9 | 39.1% | +1.59% |
| **15-60min** | **19** | **7** | **36.8%** | **+18.77%** ✅ (best ROI) |
| 1-3h | 11 | 1 | 9.1% | −1.98% |
| >3h | 2 | 0 | 0.0% | −2.27% |

**Sweet spot: 15-60min holds.** Worst: 1-5min (likely adverse selection —
price moves against immediately). Beyond 1h: degrades because crypto 5min
markets resolve before then.

### 3d. By market type

| Type | n | wins | win% | ROI% |
|---|---|---|---|---|
| **crypto_btc** | 21 | 10 | 47.6% | **+3.74%** ✅ |
| crypto_eth | 6 | 2 | 33.3% | −2.97% |
| **sports** | 3 | 0 | **0.0%** | −2.48% 🔴 |
| **politics** | 1 | 0 | **0.0%** | −2.20% 🔴 |
| **general** | 4 | 0 | **0.0%** | −2.29% 🔴 |
| crypto_doge | 1 | 0 | 0.0% | −9.26% |
| general_binary | 8 | 1 | 12.5% | +1.14% |

**Crypto_btc is the bot's strongest category. Sports/politics/general have
ZERO wins.** The bot is trading against informed sharps in those categories.

---

## 4. Execution failure decomposition (the BIGGEST issue)

Of 377 terminal events in last 30 days:

| Status | Count | % |
|---|---|---|
| failed | **89** | **23.6%** |
| exit_deferred | **68** | **18.0%** |
| supervisor_halt | 44 | 11.7% |
| close_failed | 42 | 11.1% |
| **Total execution failures** | **243** | **64.5%** |
| closed_stop_loss | 67 | 17.8% |
| closed_take_profit | 36 | 9.5% |
| closed_timeout | 21 | 5.6% |
| resolved_loss | 10 | 2.7% |

**More than 64% of terminal events are execution failures, not clean
wins/losses.** Of these:

### 4a. `failed` reasons (top 5)

| n | reason |
|---|---|
| **51** | `llm_or_parse: Error 400 - 'messages' must contain 'json'` ← **fixed in commit `adbb108` tonight** |
| 4 | `Trade size_fraction must be > 0 and at most 1. Got 0.0` |
| 2 | `execute_market_order: PolyApiException 400 — invalid amount for marketable order` |
| 13 | `straddle execute: spread too wide` (correct rejection) |
| 2 | `live ask price exceeds recommended` (correct slippage guard) |

**51/89 failures (57%) are the OpenAI bug already fixed tonight.** After
deploy, the failed bucket should drop dramatically.

### 4b. `close_failed` + `exit_deferred` (110 combined)

Almost all are `PolyApiException`:
- `the orderbook XXX` — orderbook not available for token
- `no orders found to match with FAK order. FAK requires immediate match`

**This is structural Polymarket liquidity gap on small/closing markets.**
Bot can't always exit cleanly. Position remains until orderbook returns or
position_manager retries.

### 4c. `supervisor_halt` (44)

All `critical exit-path guard tripped` — trading_supervisor's safety check
fired when exits were blocked. **Working as designed** (prevents
entry-without-exit pile-up).

---

## 5. Concentration

13 markets with > 1 trade out of 82 distinct markets. **Top 5 LOSS markets
were all crypto 5min markets where bot entered twice and lost both**
(reentry on losing setups). Top 5 WIN markets include one trade at $+9.27
(the +497% longshot).

---

## 6. Fee impact

| Metric | Value |
|---|---|
| Total capital deployed | $133.56 |
| Total realized PnL | +$6.54 |
| Estimated total fees (c=0.07 dynamic) | $3.74 |
| Gross PnL before fees | +$10.27 |

**Fees consume ~36% of gross PnL.** Not the killer, but not negligible —
worth tracking. A fee-aware EV gate (LIVE_AUDIT Idea #3) would help.

---

## 7. Side breakdown — correcting prior misreading

Earlier SL audit reported "100% SELL" — that referred to the **close-trade
side** (which is naturally SELL for BUY-entry exits, since you sell what
you bought).

**Actual entry-side distribution (last 30d, status=filled):**

| Entry side | n | % |
|---|---|---|
| BUY | 118 | 76% |
| SELL | 37 | 24% |

The bot is BUY-dominant. The SL audit's "100% SELL" was an artifact of
querying CLOSE rows.

---

## 8. So WHY 70% losses?

The 70% loss rate is **the structural cost of trading with asymmetric
payoffs in efficient binary markets.** Specifically:

1. **The bot enters at prices that imply ~30-40% true win rate.** A market
   priced at 0.40 implies the market thinks 40% chance of YES. If the bot
   has slight edge (say true probability is 45%), it'll win 45% of the
   time — which is still "losses dominate count."

2. **The bot's edge is in the TAILS (longshot trades and 0.40-0.50 band).**
   The 8 trades with > +30% gain are where the EV lives. The middle (0-3%
   profit/loss) is noise.

3. **The REAL loss drivers** (in order of fixable impact):
   - **Execution failures from OpenAI bug** — 51 trades, ALREADY FIXED tonight.
   - **0.80+ band entries** — 0/4 wins, −80.66% ROI. The bot is paying
     near-certainty price for the losing side. **Easy fix: hard cap entries < 0.79.**
   - **Sports/politics/general markets** — 0/8 wins combined. The bot
     trades against informed sharps. **Easy fix: filter to crypto-only
     until proven otherwise.**
   - **Exit liquidity gaps** — 110 close_failed/exit_deferred. Structural
     Polymarket issue. **Mitigation: avoid entering markets with thin
     opposite-side books.**

4. **The bot's WINS are concentrated where structure favors it:**
   - crypto_btc: 47.6% win, +3.74% ROI
   - 0.40-0.50 band: 52.9% win, +8.98% ROI
   - 15-60min hold: 36.8% win, +18.77% ROI

---

## 9. Recommended fixes (Tier 1 plan)

### 9a. Easy / config-only

| # | Fix | Mechanism |
|---|---|---|
| F1 | Hard-cap entry price at < 0.79 | `BTC_5MIN_MAX_LIVE_ENTRY_PRICE` already at 0.86 → tighten to 0.78 |
| F2 | Block sports/politics/general | `SCANNER_MARKET_CATEGORY_ALLOW=crypto` env (new) |
| F3 | Verify OpenAI failure drop post-deploy | grep `failed` rows past `adbb108` deploy ts |

### 9b. Modest code changes

| # | Fix | Effort |
|---|---|---|
| F4 | Pre-entry bid-depth check on **opposite side** (to ensure exit liquidity exists) | ~30 min |
| F5 | Fee-aware EV gate (subtract dynamic Polymarket fee from net EV calc) | ~45 min |
| F6 | Reduce `MAX_HOLD_SECONDS` for crypto 5min to 90min (currently 360min) | env var |

### 9c. Bigger (defer)

- Better signal_source provenance — 80% of trades had unknown source. Fix the brain_decisions linkage.
- The markouts pipeline structural redesign (still Tier 1 from earlier).
- C-2 calibration loop integration test.

---

## 10. The real KPI to track

Not "win rate" — instead:

**Per-band Expectancy: `(win_pct × avg_win_pnl) - (loss_pct × avg_loss_pnl)`**

If this is positive per-band, the bot is profitable in that band. Current
data:

| Band | Expectancy/trade |
|---|---|
| <0.20 | +$0.46 |
| 0.40-0.50 | +$0.12 |
| 0.50-0.60 | +$0.00 (break-even) |
| 0.60-0.70 | **−$0.41** |
| 0.80+ | **−$1.43** |

A live policy that ENFORCES "trade only positive-expectancy bands" would
move PnL substantially.

---

## 11. Answer to operator's question

> **The 70% loss rate isn't a bug — it's the geometry of betting on prediction
> markets with longshot edge. The bot's gross PnL is positive (+$6.54 over
> 30d) despite the loss-count majority.**
>
> **The REAL problem is execution friction: 64.5% of terminal events are
> execution failures (failed/deferred/halted), not actual win/loss outcomes.
> 57% of those failures (51 trades) were the OpenAI bug I fixed tonight in
> commit `adbb108` — that alone should significantly improve the operational
> picture.**
>
> **The fixable money leaks (in order):**
> 1. 0.80+ band entries — 4 trades, lost $5.72 (−80% ROI).
> 2. Sports/politics/general categories — 8 trades, 0 wins.
> 3. Exit liquidity gaps — 110 stuck exits (structural).
> 4. Fee impact — eating 36% of gross PnL.
>
> **The strengths to preserve:**
> - crypto_btc: 47.6% win, +3.74% ROI
> - 0.40-0.50 band: 52.9% win, +8.98% ROI (the learning guard now active!)
> - 15-60min hold sweet spot: +18.77% ROI

Carry-over to next session: F1-F6 above, plus the markouts redesign (Tier 1).
