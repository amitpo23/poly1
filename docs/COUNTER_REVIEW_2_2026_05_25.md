# Counter-Review of Reviewer #2 — 2026-05-25 evening

Reviewer #2 produced 3 critical + 6 concerns + 4 winrate ops.
Empirical verification finds: 1 valid critical, 3 valid concerns,
1 incidental win — and an unintended GENUINE smoking gun in the data
that neither reviewer flagged.

## 🔴 Reviewer #2 critical claims — verified

### #1: "Bayesian blocks all because avg_loss=None → EV=None" ❌ WRONG
**Reviewer's claim**: `resolved_loss` rows with NULL pnl propagate to
None values across calibration, breaking EV mode.

**Counter-evidence**: `data/probability_calibration.json` per_source_band
has REAL numbers in every segment with n≥3:
```
alphainsider × 0.50-0.54: avg_loss=-$0.068, ev=-$0.010 (not None)
alphainsider × 0.40-0.49: avg_loss=-$0.124, ev=-$0.008
BUY action: avg_loss=-$0.052, ev=+$0.015 (POSITIVE!)
btc_5min: avg_loss=-$0.157, ev=-$0.044
```

**Why the reviewer thought EV was None**: probably read the calibrator
code without running it. The resolved_loss rows that have NULL pnl
are also UNMATCHED to brain_decisions (verified: 5/5 sampled
resolved_loss have no_signal=1), so they don't enter the calibration
sample at all. Excluded, not poisoned.

**Reviewer #2 also claimed**: `BAYESIAN_MIN_EDGE=0.05`. Actual on
server: `SCANNER_EXECUTOR_BAYESIAN_MIN_EDGE=0.0`. They worked from
code defaults, not deployed env.

### #2: "resolved_loss=NULL pnl breaks calibration" ⚠️ TRUE BUT IMPACTLESS
**Verified**: 11 resolved_loss rows have NULL pnl_usdc_real.

**Impact**: zero. They're not in the calibration sample. Should be
fixed for hygiene (data quality, MTM accuracy) but does NOT cause
the 0-trade outcomes.

**Action**: still worth fixing — write `pnl_usdc_real = -position_size`
when resolution_sync writes resolved_loss. Low priority.

### #3: "76/165 closes unmatched — calibration blind" ⚠️ TRUE BUT STRUCTURAL
**Verified**: 165 closed trades, 89 matched to brain_decisions.

**Why**: btc_5min, scalper, near_resolution write entries directly
without going through brain_decisions. Their closes are unmatched
by design. The `per_direct_execution_agent` table already captures
btc_5min separately (n=46 shown in JSON). So not "blind" — separated.

Reviewer #2's recommendation to broaden `_find_originating_decision`
would conflate signals from different decision-making models.

## 🟡 Reviewer #2 concerns — verified

### #4: Wilson 95% CI too conservative ❌ TRUE MATH, IRRELEVANT IMPACT
Reviewer claimed: switch from 95% to 80% would unlock trades.

**Wilson math verified**:
- n=77, p=0.26, z=1.96 (95%) → wilson_lower=0.175 ✓ matches JSON
- n=77, p=0.26, z=1.28 (80%) → wilson_lower=0.201
- n=54, p=0.31, z=1.28 (80%) → wilson_lower=0.236

Switching to 80% only increases wilson_lower by ~3 percentage points.
For an alphainsider candidate at price 0.52: edge with 95% CI = -0.345.
With 80% CI = -0.32. **Still deeply negative.** Switching CI level
would NOT unlock trades.

The root issue isn't conservativeness — it's that the bot's empirical
WR is genuinely below the market-implied probability across the
prices it operates at.

### #5: Quarantine cascade ⚠️ TRUE — same bug as Reviewer #1
After 5 rejects on same market, 1-hour quarantine kicks in. This
amplifies the gate's strictness. Same finding both reviewers caught.

**Action for next session**: reduce quarantine effect — if reject
reason is `bayesian_edge_below_threshold`, exempt from quarantine
(it's a calibration result, not a market-quality issue).

### #6: btc_5min new SL=0.03/TP=0.08 untested ✅ TRUE
Valid concern. Worth a small live test once other issues addressed.

### #7: external_conviction 0 approvals ✅ TRUE — both reviewers caught
13K decisions/h, 0 approvals. Worth investigating.

### #8: phantom positions count ⚠️ STALE DATA
Reviewer #2 says "11 + 13 dust". Reviewer #1 said "55". Current
verified: **55 filled-no-close**.

Reviewer #2 used the narrow sweep query (counts only dust + recent);
Reviewer #1 (and I) used the broad query (all filled without
subsequent close). 55 is right.

### #9: btc_5min bypasses Bayesian gate ✅ VALID architectural point
btc_5min direct_execution path doesn't go through the gate. EV=-$0.044
on n=46 historical → it'd be rejected. But it bypasses.

This was Reviewer #1's 2.5 Q4 also.

**Action**: defer until btc_5min new SL is tested. If new SL flips
btc_5min to +EV, bypass becomes irrelevant.

## 💡 Reviewer #2 winrate opportunities — verified

### W-A: Lower bayesian_min_edge from 0.05 to 0.01 ❌ ALREADY 0.0
Reviewer worked from defaults. Actual: `BAYESIAN_MIN_EDGE=0.0`.
Edge mode is already maximally permissive. Cannot be lowered.

### W-B: BUY-only mode + block SELL ⚠️ MOSTLY BUT NOT IN SUGGESTED BAND
Reviewer's suggested band (`BUY + price<0.40`) — verified:
```
SELECT side, AVG(pnl) FROM trades WHERE side='BUY' AND price<0.40 7d
→ n=45, avg_pnl=-$0.139
```

**Negative.** Reviewer's specific band is WRONG.

**But broader BUY split is partially right**:
```
entry_side × band 30d:
  BUY × 0.40-0.49: n=22, avg_pnl=+$0.040 ✅
  BUY × 0.55-0.64: n=7,  avg_pnl=+$0.287 ✅✅
  BUY × <0.40:     n=38, avg_pnl=-$0.106 ❌
```

So BUY-only doesn't unlock — the LOW-price BUYs are losers.
But **BUY × 0.40-0.49 and BUY × 0.55-0.64** are positive. The
reviewer pointed in roughly the right direction but reversed it
(low-price BUYs are the losers, not the winners).

### W-C: Collapse price bands to 2 ❌ HIDES the +EV segments
Reviewer suggested merging 0.40-0.49 + 0.50-0.54 + 0.55-0.64 + 0.65+
into "price≥0.50".

But the data shows different signs across these bands. Collapsing
hides the +EV (BUY × 0.55-0.64) inside a larger -EV aggregate.
**Worse, not better.**

### W-D: btc_5min live test ✅ same as Reviewer #1's call

## 🔥 THE REAL FINDING — NEITHER REVIEWER FLAGGED

When I cross BOTH dimensions (entry_side × price_band) directly from
the trades table, positive EV segments emerge:

| segment | n | avg_pnl |
|---|---:|---:|
| BUY × 0.40-0.49 | 22 | **+$0.040** |
| BUY × 0.55-0.64 | 7 | **+$0.287** |
| SELL × 0.55-0.64 | 3 | +$0.207 |

But the Bayesian gate uses `per_source_band` (which AGGREGATES BUY
and SELL together):
```
alphainsider × 0.40-0.49: n=26, ev=-$0.008 ← AGGREGATE
  ↑ hidden inside:
  BUY × 0.40-0.49: n=22, ev=+$0.040
  SELL × 0.40-0.49: n=11, ev=-$0.10
```

When a BUY candidate at price 0.45 arrives, the gate looks up
`alphainsider × 0.40-0.49` → gets the BUY+SELL aggregate (-EV) →
rejects. **The BUY-side positive signal is invisible.**

This was Reviewer #1's 2.5 Q2 hypothesis (3-way segmentation). They
suggested it but I underweighted the recommendation. **It's now
verified empirically: there IS hidden positive EV that 3-way
segmentation would expose.**

**Action**: implement `per_source_band_action` segmentation in
`probability_calibrator.py`. Add the key format
`source|band|action` to calibration JSON. Update `lookup_winrate`
hierarchy to try this most-specific lookup FIRST.

Expected impact: BUY candidates at alphainsider × 0.40-0.49 would
look up segment n=22, ev=+$0.040, wilson_lower(0.31, 22, 1.28) ≈
0.21. Edge at price 0.45 = 0.21 - 0.45 = -0.24 (still rejected by
edge mode). But EV mode: +$0.040 ≥ $0.005 min_ev_usdc → **EV
APPROVES**.

**Estimated PnL impact**: 22 trades × $0.040 = +$0.88 historical.
Projected to 30 days at current cadence: maybe +$1-2/month from
unlocking this segment.

## VERDICT — Reviewer #2

| Category | Claimed | Validated | Wrong | Partial |
|---|---:|---:|---:|---:|
| 🔴 Critical | 3 | 0 | 1 (#1) | 2 (#2,#3 — true but impactless) |
| 🟡 Concerns | 6 | 4 (#5,#6,#7,#9) | 1 (#8 stale) | 1 (#4 true math, irrelevant impact) |
| 💡 Winrate | 4 | 0 | 2 (W-A,W-C) | 2 (W-B partial, W-D same as #1) |

Reviewer #2 was more confident than data supports, especially on the
"EV=None" claim that drove their priority. They didn't run the actual
calibration to verify before declaring it broken.

**Strongest contributions**: pointing to BUY/SELL asymmetry (even if
their specific band recommendation was wrong) + reinforcing
external_conviction investigation priority.

**Key blind spot**: didn't compute the 2D cross-tab that would have
exposed the real hidden +EV segments.

## Action items from BOTH counter-reviews

**Implement (highest leverage, low risk)**:
1. **3-way `per_source_band_action` segmentation** (NEW from this
   review) — unlocks the BUY-side positive segments
2. **`pnl_usdc_real` backfill for resolved_loss** (Reviewer #2 #2)
3. **Convert `require_universe_top` + `market_doc=None` to
   `_log_skip`** (Reviewer #1 C4)
4. **On-chain reconciliation of 55 phantoms** (both reviewers)
5. **Quarantine exempt for bayesian rejects** (Reviewer #2 #5)

**Investigate (next session)**:
- external_conviction 0-approval root cause (both reviewers)
- C2 horizon path verification (Reviewer #1)
- btc_5min new SL/TP small live test (both)

**Reject**:
- W3 (hour-of-day filter) — single-day overfit (Reviewer #1)
- W-A (lower min_edge) — already 0.0 (Reviewer #2)
- W-C (collapse bands) — hides positive segments (Reviewer #2)
- C1 (FAK fallback) — anti-gap SL already covers it (Reviewer #1)
- #4 lower Wilson CI — math doesn't help (Reviewer #2)
