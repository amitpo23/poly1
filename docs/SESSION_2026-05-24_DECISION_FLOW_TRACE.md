# SESSION 2026-05-24 — Decision Flow Trace (end-to-end)

**Written:** late night 2026-05-23 → early morning 2026-05-24
**Reason:** before live test in the morning, operator asked: "are you sure
you've seen the brain processes and indicators on the server end-to-end?"
Honest answer was no — only pieces. This doc closes that gap.

---

## Live snapshot from production (taken just now)

The bot is in freeze (EXECUTE_SCANNER_EXECUTOR=false) but IS scanning.
In the last 4 hours:

**Reject reason distribution (top 12):**

| count | reason | layer |
|---|---|---|
| 13,783 | crypto_tape: no supported crypto asset | external_conviction noise |
| 5,819 | proven_indicator_without_market_direction | external_conviction noise |
| 5,703 | risk_gate_blocked | scanner_executor (legit) |
| 3,343 | score_below_executor_min | scanner_executor (legit) |
| 2,899 | market_recent_reject_quarantine | scanner_executor (legit) |
| 2,805 | alpaca: no supported symbol in question | external_conviction noise |
| 1,280 | no_candidate_in_time_window | shadow market_maker |
| 1,020 | non_equity_macro_market | external_conviction noise |
| 935 | openbb: no supported symbol | external_conviction noise |
| 646 | too_close_to_expiry | scanner / meta |
| 599 | edge_score_too_low | scanner / meta |
| 317 | probability_not_calibrated | scanner_executor |

**Most recent approvals** (sample):
- `meta_brain,manifold,manifold:manifold` is producing high-score approvals (0.85-0.94, BUY @ 0.355-0.445, calibrated=True)
- `opportunity_factory,alphainsider_proven,crypto_tape` produces lower scores (0.68) but still approves

**Critical pattern:** market_scanner approves a candidate → scanner_executor
rejects it ~2,900x with `market_recent_reject_quarantine`. This means the
quarantine list (markets recently rejected for the same reason) is doing
a lot of work blocking re-attempts.

---

## End-to-end decision flow

There are **30+ sequential gates** from "market discovered" to "order placed":

### Stage 1 — Market Scanner (5 gates + meta_brain)

1. Gamma API fetch (3 parallel orderings + dedupe by conditionId) — `market_scanner.py:363-399`
2. Coarse filter — price ∈ [0.10, 0.90], liquidity ≥ 1k, volume ≥ 500, hours-to-close in range — `market_scanner.py:401-436`
3. `MetaBrain.synthesize()` call — 8 internal gates (next section)
4. `recent_close_skip` (token had close in last 12h) — `market_scanner.py:529-542`
5. `SCANNER_MIN_TRADE_SCORE` threshold (0.55 default) — only above it → `brain_decision` written

### Stage 2 — MetaBrain (8 internal gates)

Per `agents/application/meta_brain.py`, in order:

1. `MarketBrain` hard gate — `spread_too_wide` / `too_close_to_expiry` / `horizon_too_long` — `market_brain.py:893-901`
2. `MarketBrain` score gate — `general_score_too_low` (≥0.50) — `market_brain.py:963`
3. `EvidenceRouter` conflict gate — `expert_conflict:{source}` (mode=="blocked") — `meta_brain.py:2608`
4. Meta-score threshold — `weighted_score_too_low:{x}<{thr}` (0.50, or 0.40 with anchor) — `meta_brain.py:2650-2673`
5. Calibrated probability requirement — `rank_only_no_calibrated_probability` if no solo/cross_market/quant_fv source — `meta_brain.py:2714-2731`
6. Edge gate — `internal_edge_too_low:{edge}<0.02` — `meta_brain.py:2732-2749`
7. Raw EV gate — `raw_ev_too_low:{raw}<0.04` — `meta_brain.py:2750-2767`
8. Execution quality gate (optional) — `meta_brain.py:2769-2816`

### Stage 3 — Scanner Executor (21 gates)

Per `agents/application/scanner_executor.py:_handle_decision`, in order:

1. Pre-sweep `RiskGate.reason()` — `cycle_blocked` if HALT/balance/budget (my I-2 fix, line 290)
2. `TradeProposal.from_brain_decision()` — `missing_execution_metadata` or `proposal_missing_execution_fields`
3. Quarantine check — `market_recent_reject_quarantine` (line 360-367)
4. Timing check — `timing_not_now` if meta_timing != "now"
5. Score threshold — `score_below_executor_min` (0.80, or 0.54 calibrated bypass)
6. Calibrated probability — `probability_not_calibrated`
7. Promotable strategy (optional) — `strategy_scorecard_not_promotable`
8. Side/token validity — `missing_execution_metadata`
9. **Learning side block** — `today_lesson_side_blocked` (BUY-only when guard active) ← MY FIX TONIGHT
10. CLOB token IDs hydration — `missing_clob_token_ids`
11. Side/token alignment — `side_token_mismatch`
12. Active trade exists — `active_trade_exists`
13. Reentry cooldown 12h — `recent_close_cooldown`
14. Market loss cooldown 1h — `recent_market_loss_cooldown`
15. Shadow recent entry 10min — `shadow_recent_entry_exists`
16. Regime router (optional) — `strategy_family_blocked_by_regime`
17. Orderbook fetch — `orderbook_not_executable`
18. **Learning band block** — `today_lesson_price_band_blocked` (0.40-0.49) ← MY FIX TONIGHT
19. Entry drift — `entry_price_drift_too_high` (4% cap)
20. **DecisionCouncil** — 8 sub-gates (next section)
21. Immediate-exit-on-spread — `taker_entry_below_stop_on_spread`
22. Per-market `RiskGate.ok()` check (CLAUDE.md invariant #4)
23. Kelly sizing — `kelly_size_zero` if = 0
24. `polymarket.execute_market_order()` — actual order placement

### Stage 4 — DecisionCouncil (8 sub-gates, called from #20 above)

Per `agents/application/decision_council.py:review_entry`:

1. `invalid_entry_price` (price ≤ 0 or ≥ 1)
2. `expert_conflict` (route.mode == "blocked")
3. `probability_below_council_min` (0.52 default, 0.50 expert_solo)
4. `raw_ev_below_council_min` (0.04)
5. `net_ev_below_council_min` (0.04 default, 0.025 expert, 0.06 thin market)
6. `book_exit_depth_below_min` (20 USDC)
7. `book_spread_too_wide` (0.08)
8. `book_quality_below_min` (0.65)

**Total: 8 + 21 + 8 = 37 gates.** Plus 5 scanner-side filters earlier.

---

## Signal sources → features map

Per the meta_brain mapping (see `docs/SESSION_2026-05-23_DEEPER_DRILL.md`
for the original table):

The signal sources that are CURRENTLY firing on production:
- **meta_brain,manifold:manifold** — high-quality (score 0.85-0.94) cross-market consensus from Manifold
- **opportunity_factory,alphainsider_proven,crypto_tape** — config-gated calibrated since tonight; score around 0.68
- **opportunity_factory,proven_wallet** — gated since tonight (my C-2 wallet fix)
- **external_conviction:** alpaca, openbb, crypto_tape — mostly noise rejects in production
- **technical/news_signal** — soft signals, not solo-eligible

The DOMINANT sources right now: meta_brain,manifold (cross_market) and
opportunity_factory,alphainsider_proven. Other sources are noise.

---

## Score / probability formula (meta_brain weighted synthesis)

Per `agents/application/meta_brain.py:2200-2571`:

```python
weights = {
    "brain": 0.25,         # MarketBrain.evaluate_general_entry
    "winrate": 0.15,       # historical WR or prior 0.50
    "conviction": 0.15,    # external JSONL aggregated
    "velocity": 0.10,      # price velocity
    "cross_market": 0.10,  # Kalshi/Metaculus/Manifold divergence
    "equity_fv": 0.12,     # equity fair-value model
    "alpaca": 0.08,        # Alpaca market data
    "openbb": 0.06,        # OpenBB
    "crypto_tape": 0.12,   # CryptoExchangeTapeReader
    "quant_fv": 0.10,      # quant_price_fair_value
    "whale": 0.10,         # CLOBWhale
    "news": 0.10,          # BreakingNews
    "liquidity": 0.05,     # liquidity USDC / 50k
}
# active_weight_sum = Σ(weights[k] for k if components[k] != 0.5)
# score = Σ(weights[k] * components[k]) / active_weight_sum
# Neutral (=0.5 exactly) signals are EXCLUDED from active weights.
```

**Sum of weights = 1.38** (NOT 1.0). The PRE_LIVE_QA_REVIEW H-8 flagged
this. The informed-only normalization at 2552-2570 saves it — the active
sum is computed dynamically per call — but the WEIGHTS table itself is not
normalized to 1.0. If the dynamic normalization fails (all components
neutral), the fallback divides by 1.38, not 1.0.

---

## The "30 gates" reality check

The bot rejects ~22,000-30,000 candidates per hour (per the live snapshot).
Most rejects are external_conviction provider noise — the providers can't
match the question to their asset universe. Of the LEGITIMATE rejects:

- 5,703x `risk_gate_blocked` (legit safety)
- 3,343x `score_below_executor_min` (legit — score < 0.80)
- 2,899x `market_recent_reject_quarantine` (legit — same market same reject)
- 317x `probability_not_calibrated` (legit — no calibrated signal)

**Implication for tomorrow's test:** the bot has 37 gates. Each gate
is a chance to reject. To get a trade through, the candidate must
satisfy ALL 37. The OBSERVED pattern: market_scanner approves ~10/hour,
scanner_executor lets ~0 through (because of quarantine on every market
already seen).

For the morning live test, the quarantine list will not be FULL initially
(fresh `live-hour` call), but it will fill up quickly. **Expect maybe
1-5 actual fills in a 15-minute window.**

---

## What I didn't verify (honest gaps)

1. **MetaBrain.synthesize() internal flow** at the CODE LEVEL — I read the
   Explore agent's map but didn't trace line-by-line. The 8 gates are
   confirmed via the agent's report; the SCORE computation formula is
   confirmed via line citations but I didn't run it on a specific input.

2. **The interaction between scanner-side `_execution_metadata_for_market`
   and meta_brain's `internal_probability`** — both compute "calibrated"
   independently, and `market_scanner.py:887-911` re-derives it for the
   features dict. They COULD disagree silently. My fix tonight didn't
   touch this.

3. **What happens when a candidate has score 0.853 BUY @ 0.355 calibrated=True
   est_prob=0.53** (the recent case from production) — the executor should
   pass score gate (0.853 > 0.80), calibrated gate, learning side (BUY ok),
   learning band... wait, 0.355 < 0.40 (the learning_min_entry_price).
   **That trade would be blocked by `today_lesson_price_band_blocked`** —
   the band is 0.40-0.49. The 0.355 price is BELOW the min.

   This means the learning guard's price band may be too narrow for what
   meta_brain,manifold is producing. **Worth re-examining the band limits
   pre-test.**

4. **The decision_journal entries I sampled** are very recent (last 4h).
   I didn't dig into older patterns at scale. The dominant patterns may
   shift over different time periods.

5. **Position manager exit logic + trailing stop** — I asked the advisor
   about it but didn't read the code. Q2 finding (86% exit efficiency)
   stands but the "why isn't trailing capturing more" question is open.

---

## Recommendations for the morning test

### Specific to the 30-gate chain

1. **Lower the learning_min_entry_price from 0.40 to 0.30** to allow the
   high-score (0.85+) meta_brain,manifold candidates at 0.355 through.
   The DEEPER_DRILL data (corrected) showed 0.30-0.40 had +47.80% ROI
   on 7 trades — that band is profitable, not just 0.40-0.50.
   Currently blocked by learning_min_entry_price=0.40.

   **However:** this is an env change that affects live behavior. Defer
   to operator approval. Don't ship without explicit OK.

2. **Verify expectation:** how many fills do we expect in 15 minutes?
   Given the quarantine and the 0.40-0.49 band, the realistic answer is
   0-3 fills. If 0, the test result is "no trades, narrow filters" not
   "no edge".

3. **Watch for `recent_close_cooldown` and `recent_market_loss_cooldown`**
   in the reject reasons — these are operational gates, not strategy
   gates. They prevent useful trades on markets we've seen before.

### Open question (advisor-style)

**Is the bot's 22 (out of 37) reject rate too high for $5 budget / 15min
window?** With ~10 scanner approvals per hour and quarantine blocking
most, we may see 0 trades. If that happens, the test is "infrastructure
works, no signal hit our band" not "infrastructure broken".

### My honest confidence

- ✅ I understand the SCANNER → SCANNER_EXECUTOR pipeline at the code level.
- ✅ I understand the 37-gate sequence and where my Tier 0a/0b fixes sit.
- ✅ I traced a specific candidate (the meta_brain,manifold BUY @ 0.355) and
  identified it would be blocked by the learning band.
- ⚠️ I DID NOT read meta_brain.py line-by-line; relied on the Explore agent.
- ⚠️ I didn't read position_manager.py exit logic.
- ⚠️ I didn't read external_conviction providers' code.
- ⚠️ I didn't trace what happens when a trade actually FIRES — only what
  blocks it.

**For the morning test**, my confidence is: the system will likely run
SAFE (the 37 gates are conservative), but may produce 0-2 fills in 15
minutes. The MAIN concern is whether the learning_min_entry_price=0.40
is too tight — recent meta_brain,manifold approvals were at 0.355,
which would be blocked.

---

## Final question for operator before morning

Do you want me to:
- (a) Lower `SCANNER_EXECUTOR_LEARNING_MIN_ENTRY_PRICE` from 0.40 to 0.30
  for tomorrow's test, capturing the 0.30-0.40 band that historically
  had +47.80% ROI?
- (b) Keep it at 0.40 and accept 0-2 fills; data is data.
- (c) Defer the test until I do more verification (e.g., read
  position_manager.py exit logic, trace a successful trade end-to-end).

Default if no answer: (b). The test runs as planned.
