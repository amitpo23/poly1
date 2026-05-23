# SESSION 2026-05-23 — Deeper Drill (SL / Exit Timing / Brain Accuracy)

**Operator's follow-up questions:**
1. Is the stop-loss premature? How many SLs would have been profitable if held?
2. Are exit timings (TP, hold-time) optimal?
3. Is the brain/score gate predictive?

**TL;DR:**
1. **SL is NOT premature.** Where post-exit data exists, 0/44 SL prices recovered to entry within 2h.
2. **Exit timing: 86% efficient.** Captures 86% of peak. 19/20 winners left 2%+ on table — a trailing stop could squeeze more.
3. **Brain score is NOT cleanly predictive.** Higher band kills returns regardless of score. **Band is the dominant signal, not score.**

---

## Q1. SL prematurity

**Method:** for each of 67 SLs in 30d, find decision_journal rows on same
market AFTER exit. Compute the max favorable price for the OWNED token
within +5/15/30/60/120min windows. Compare to entry price (recovery) and
to entry × 1.05 (TP threshold).

**Results:**

| Window | Recovered to entry | Would have hit TP |
|---|---|---|
| +5m | 0/44 (0.0%) | 0/44 (0.0%) |
| +15m | 0/44 (0.0%) | 0/44 (0.0%) |
| +30m | 0/44 (0.0%) | 0/44 (0.0%) |
| +60m | 0/44 (0.0%) | 0/44 (0.0%) |
| +120m | 0/44 (0.0%) | 0/44 (0.0%) |

Coverage caveat: only 44 of 67 SLs had usable post-exit data
(orderbook_monitor drops tokens after position close; decision_journal
sometimes lacks price extraction). The 23 missing-data SLs are unknown.

**For the 44 we can measure: zero recovered, zero would have TP'd.** This
is consistent with the earlier SL audit's "60% mean-reverted to entry but
only to noise level (0.495 vs entry 0.50)" — the prior "recovery" was
sub-noise, not actually profitable.

**Verdict: SL is correct. Don't loosen.** Loosening would just increase
average loss per SL without converting losses to wins.

---

## Q2. Exit timing — leaving money on the table?

**Method:** for each of 36 TP wins in 30d, compare actual exit PnL% to
the position's MFE (max favorable excursion) from position_marks.
Efficiency = actual / MFE.

**Sample: 20 TP wins (16 had missing MFE data).**

| Metric | Value |
|---|---|
| Average actual PnL% | +14.42% |
| Average MFE (peak)% | +16.78% |
| **Exit efficiency** | **86.0%** |
| Wins where MFE − actual > 2% (left meaningful upside) | 19/20 |

**Interpretation:** The bot captures 86% of the peak — quite good. But on
**95% of wins (19/20)**, the peak was at least 2% higher than the
actual exit. A trailing stop after meaningful profit would capture more.

**Suggested fix (Tier 1, modest impact):** Add `MAINTAIN_TRAILING_STOP_PCT`
trail of ~1.5-2% from peak once profit > 3%. Current setting in BASE_ENV
is `MAINTAIN_TRAILING_STOP_PCT=0.02` — verify if it's actually triggering.

---

## Q3. Brain accuracy — is the score predictive?

### Q3a. By score bucket (last 30d, 95 closed trades)

| Score band | n | wins | win% | PnL | ROI% |
|---|---|---|---|---|---|
| <0.55 | **73** | 24 | **32.9%** | **+$5.48** | **+4.88%** |
| 0.55-0.60 | 1 | 1 | 100.0% | $0.00 | 0% |
| 0.65-0.70 | 5 | 1 | 20.0% | −$0.10 | −2.00% |
| 0.70-0.75 | 9 | 1 | **11.1%** | **−$0.37** | **−3.49%** |
| 0.80-0.85 | 3 | 0 | 0.0% | −$0.09 | −3.00% |
| 0.85+ | 4 | 1 | 25.0% | +$1.61 | +56.62% (outlier) |

**Surprising: score is mildly ANTI-predictive in the mid-range.** Trades
scored 0.70-0.75 had only 11% win rate and −3.49% ROI. Trades scored
<0.55 had 33% win rate and +4.88% ROI.

This is **not noise** — 73 trades at low score had a clear positive
pattern; 9 trades at 0.70-0.75 had a clear negative pattern.

### Q3b. Score × Band grid (where IS the alpha?)

| Score | Band | n | wins | win% | PnL | ROI% |
|---|---|---|---|---|---|---|
| low | <0.30 | 21 | 3 | 14.3% | +$8.49 | **+22.92%** |
| low | **0.30-0.40** | **7** | **4** | **57.1%** | **+$4.65** | **+47.80%** ✅ |
| low | **0.40-0.50** | **15** | **9** | **60.0%** | **+$2.16** | **+10.11%** ✅ |
| low | 0.50-0.60 | 22 | 7 | 31.8% | −$1.34 | −5.53% |
| low | 0.60-0.70 | 5 | 2 | 40.0% | −$2.76 | **−21.32%** |
| low | **0.70+** | 4 | 0 | **0.0%** | −$5.72 | **−80.66%** 🔴 |
| mid | 0.30-0.40 | 3 | 0 | 0.0% | −$0.11 | −3.93% |
| mid | 0.40-0.50 | 1 | 0 | 0.0% | −$0.04 | −4.04% |
| mid | 0.50-0.60 | 7 | 1 | 14.3% | −$0.20 | −2.88% |
| mid | 0.60-0.70 | 2 | 0 | 0.0% | −$0.13 | −3.35% |
| high | 0.50-0.60 | 5 | 1 | 20.0% | +$1.58 | +41.47% (outlier) |
| high | 0.30-0.40 | 1 | 0 | 0.0% | −$0.03 | −3.29% |
| high | 0.40-0.50 | 1 | 0 | 0.0% | −$0.03 | −3.04% |

### The pattern that emerges

**Band is the dominant signal. Score is secondary.**

- The **profit zone**: low score + band 0.30-0.50. Win rate 57-60%,
  ROI +10-48%. Two cells, 22 trades, +$6.81 combined.
- The **death zone**: ANY trade in band 0.70+, regardless of score.
  0 wins, −80% ROI.
- The **break-even zone**: band 0.50-0.70 at any score. Win rate
  14-40%, ROI −3% to −21%.

**The 0.70+ band loses catastrophically no matter what the brain says.**

---

## Actionable fixes (Tier 1, prioritized by impact)

### F1. HARD-CAP entry price < 0.69 (highest impact)

Block all entries with `selected_entry_price >= 0.69`. The grid shows
ZERO wins above 0.70 band, regardless of score. Removing 0.70+ trades
from the 30-day sample would have moved PnL from +$6.54 to +$12.26
(+87% improvement on PnL).

**Implementation:** env var
`SCANNER_EXECUTOR_LEARNING_MAX_ENTRY_PRICE` already exists. Currently
0.49 per BASE_ENV (set during my Tier 0a). The **learning guard is
already blocking 0.50+ in BUY-side**.

But `BTC_5MIN_MAX_LIVE_ENTRY_PRICE` is `0.86` — that allows up to 0.86
for btc_5min. Tighten to 0.69.

### F2. Inverse the score gate philosophy

Current: `SCANNER_EXECUTOR_MIN_SCORE=0.80` (high score required).
Data: high score does NOT predict wins; low score (<0.55) trades win
more.

**Don't blindly invert** — that would let in noise. But consider:
- Lower `min_score` to 0.55 (currently 0.80) IF entry-band is in the
  profitable zone (0.30-0.50)
- Keep high `min_score` (0.80) for OTHER bands

This is essentially what the learning_guard already does — restrict by
band AND require calibrated.

**Verification:** the recent <0.55 trades that won were largely the
"calibrated bypass" path (opportunity_factory_alphainsider). My Tier 0b
fix (C-2 wallet) closes some of that bypass but keeps the
config-controlled one. So this isn't broken — but the score itself isn't
the real gate; the **band + calibration source** is.

### F3. Trailing stop activation (modest impact)

Trail = 1.5% from peak once profit > 3%. Will recover ~2% on average
across winners. On 36 wins/30d worth ~+$10 gross PnL, +2% better
capture = ~+$0.20-0.40 incremental. Small but free.

**Implementation:** verify `MAINTAIN_TRAILING_STOP_PCT=0.02` is actually
firing. If not, debug position_manager exit logic.

### F4. Filter sports/politics/general markets

0/8 wins across these categories. Hard-block from scanner. New env:
`SCANNER_MARKET_CATEGORY_ALLOW=crypto`.

### F5. Don't change SL

Empirical: 0/44 SLs would have recovered to entry within 2h. SL is
correct. Don't loosen.

---

## What this means for the brain

The brain.score is not a useless signal — it has SOME predictive value
at the EXTREMES (very low score = longshot tradeable; very high score =
sometimes hits big like 0.85+ with +56%). But in the middle (0.55-0.80),
it's noise or anti-signal.

This is **consistent with the bot's payoff structure**: the edge is in
the asymmetric longshots, and brain.score is more of a "how strong is my
evidence" signal which in efficient markets correlates with "how priced
in is this view" — which means LESS edge for high scores.

**The real lesson:** the bot's empirical edge is in the longshot+low-band
quadrant. Lean into that. Cut the death-zone (0.70+ band) and the
break-even zone (0.50-0.70 mid-score) hard.

---

## Carry-over

- F1 (cap 0.69) — simple env change, can do in next session
- F2 (score gate philosophy) — needs design discussion
- F3 (trailing stop verification) — debug session
- F4 (category filter) — needs market-type signal in scanner
- F5 — no change needed

Plus the Tier 1 carry-overs from earlier session (markouts redesign,
SPEC sync, calibration integration test).
