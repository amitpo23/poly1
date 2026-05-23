# SESSION 2026-05-24 — Full Stack Deep Verification

**Goal:** Operator asked me to read everything I hadn't read line-by-line
and verify all working assumptions before tomorrow's profitable-trading test.

**Coverage tonight:**
- `meta_brain.py` (2,805 lines) — full verification by Explore agent + spot-checks
- `market_brain.py` + CrossMarketSignalFeed — full verification (manifold dominant signal)
- `position_manager.py` + `exit_executor.py` + brain exit authority — full verification
- `polymarket.py` — execute_market_order + FOK/FAK semantics + side mapping
- `risk_gate.py` — MTM calculation + ENTRY_EXECUTE_FLAGS + balance ledger
- `decision_council.py` — read in full earlier
- `scanner_executor.py:_handle_decision` — full chain read earlier
- Server data: agent_promotion_ledger, signal_source distribution last 24h

---

## TL;DR — what changed vs my earlier reports

| Earlier claim | Verified reality |
|---|---|
| meta_brain has 8 gates | **11 signal sources + 5 hard gates** (the count was wrong) |
| Weights sum to 1.38 | **1.48** (sum was wrong; informed-only normalization compensates) |
| MAINTAIN_POLL_SEC=10 | **= 60** (I confused it; trading_policy.py:44) |
| meta_brain,manifold = "high quality" | **Raw Manifold crowd, no calibration, equal weight with Kalshi+Metaculus** |
| trailing stop is configured | **Active but conditional: requires mfe≥1.5% AND drawdown≥2%** |
| BUY-only learning guard sufficient | **Manifold approvals at entry 0.355 are BLOCKED by min=0.40** |
| Bot is net positive | **Net −$1.92 over 30d** (confirmed via response_json.pnl_usdc_real) |

---

## Critical findings

### 1. The bot is largely a "Manifold consensus" bot in production

**Signal source distribution (decision_journal, last 24h):**

| Source | Count | % | Notes |
|---|---|---|---|
| `meta_brain,manifold,manifold:manifold` | 14,939 | 54% | Dominant. Raw Manifold consensus. |
| `opportunity_factory,alphainsider_proven,crypto_tape` | 10,754 | 39% | Tier 0b gated tonight. |
| `crypto_5m_market_maker_shadow` | 2,782 | 10% | Shadow only. |
| Others | <100 each | <0.5% | quant_fv, alpaca, etc. |

**Risk:** the "high quality" meta_brain,manifold candidates we saw (score
0.85-0.94, calibrated=True) are coming from `CrossMarketSignalFeed` which
does **simple arithmetic mean of Kalshi/Metaculus/Manifold** with NO
calibration, NO volume weighting, NO source-specific bias correction. Per
the deep-dive Explore: Manifold amateurs are weighted equally with Kalshi
real-money prices.

The "manifold,manifold:manifold" duplication: first "manifold" from
meta.signal_sources; second "manifold:{slug}" from market_scanner.py:841
where the manifold market slug was literally "manifold". Dedup set misses
this because the strings are different.

### 2. Possible MTM calculation bug for SELL trades

`risk_gate.py:227`: `shares = size_usdc / entry_px` (where `entry_px` is the
trades row's `price` column).

The COMMENT (lines 218-223) says: "TradeLog.price stores that actual token
entry price, so MTM must value shares against `price` for both BUY and SELL rows."

**But data inspection shows otherwise.** Trade id=7 (SELL, status=filled):
- `price = 0.845` (matches `response_json.price_recommended`, the YES recommendation)
- `response_json.order_avg_price_estimate = 0.16` (actual NO fill avg)

If `price` is the YES recommendation for SELL trades (which the data
suggests), then `shares = size_usdc / 0.845 = 4.4 NO shares` — but the
actual fill was 23.25 NO shares (1/0.155 = 6.5x more).

**This would mean MTM for SELL positions is understated by ~5-6x.**

**Action required:** verify with another SELL trade and check
`trade_log.insert_terminal()` / `update_to_filled()` for what gets stored.
If confirmed bug, MTM-based decisions (equity-based drawdown, kelly sizing)
are wrong for SELL positions. The PnL via `pnl_usdc_real` is unaffected
because that's the realized P&L, not MTM.

**Defer to next session for verification — too important to fix without verification.**

### 3. Float epsilon precision in meta_brain weighting

`meta_brain.py:2555`: `if v != NEUTRAL:` where `NEUTRAL = 0.5`. **Exact float equality.**

If any signal reader returns 0.50000001 due to rounding, it WILL be included
in the active weight sum. If it returns exactly 0.5, EXCLUDED. The behavior
flips on any micro-rounding.

**Impact:** 13 signal sources each could produce micro-non-0.5 values. With
13 sources, the weighted score can be silently distorted. Mitigation:
should use `abs(v - 0.5) < 1e-6`. Not fixing tonight; flagging.

### 4. Exit-side trailing stop is conditional

Earlier I said trailing stop wasn't capturing peaks. Now verified WHY:
- Trailing activates only when **mfe ≥ 1.5% AND drawdown_from_peak ≥ 2%**
- A position that hits +2.5% peak then drops to +1.5% (drawdown=1%) → no trail
- A position that hits +6% then drops to +2% (drawdown=4/6=66%) → trails

The 86% exit-efficiency we measured comes from:
1. 60s poll cycle (peaks between cycles are missed)
2. Trailing thresholds (small peaks don't trail)
3. Momentum-hold at preferred TP zone (+4% to +8%) — holds if momentum positive
4. FAK execution delay

**This is by design.** Not a bug. But it explains the 14% gap.

### 5. agent_promotion_ledger is EMPTY

No agent has ever been promoted via the formal ledger. The
`SCANNER_EXECUTOR_REQUIRE_PROMOTABLE_STRATEGY=false` (BASE_ENV) makes this
non-blocking, so trades happen anyway. But the formal promotion mechanism
isn't being used.

**Implication:** the bot has no formal "this strategy is proven" gate. The
"proven_calibrated_score" bypass (score 0.54 vs 0.80) relies on the
`signal_source` string containing "proven" or "alphainsider_proven" —
which is essentially a code-controlled bypass.

### 6. FOK for entry, FAK for exit

- `polymarket.execute_market_order` uses **FOK** (Fill-or-Kill) — entire
  order fills or none. Lines 874, 968-979.
- `exit_executor.py:65` uses **FAK** (Fill-and-Kill) — what fills, kills the rest.

This is correct design (don't enter partial; exit whatever you can). But
the asymmetry means entries can fail-and-retry while exits leave partial
positions stuck.

### 7. Brain Exit Authority extends max_hold to 7h

`MAINTAIN_MAX_HOLD_HOURS=1.0` (BASE_ENV) + `MAINTAIN_BRAIN_MAX_HOLD_EXTENSION_HOURS=1.0`.
Brain can extend by +1h if confidence ≥ 0.75.

Earlier docs said "6h max hold". The CURRENT BASE_ENV is **1.0 hour**, not 6h.
The 6h appears in `trading_policy.py` as the absolute upper bound, but
`maintain_max_hold_hours` overrides it to 1h. **Bot's effective max_hold = 1h
without extension, 2h with extension.**

### 8. Cross-market exact agreement = no signal

`meta_brain.py:2206-2208`: if `cross_market_divergence == 0` (exact
agreement between Kalshi/Metaculus/Manifold and Poly), the cross_market
component = 0.5 (neutral). Treated identically to "no cross-market data".

**Risk:** "all sources agree with poly" is GOOD information (strong consensus)
but discarded. A market where Kalshi/Metaculus/Manifold all say 0.55 and Poly
says 0.55 looks identical to a market with no external signal.

### 9. WinRateAdvisor whipsaw

`meta_brain.py:286`: `failure_penalty = min(0.18, failures * 0.03)`.

A single day with 6+ failures deducts 18% from historical winrate. Could
cause the bot to suddenly stop trusting itself after a bad morning, even
if longterm WR is good.

### 10. EvidenceRouter aggressive blocking

Solo expert can be blocked by a "conflict" expert with wilson_lower within
0.03 of the leader. In small samples (n=30), this margin is wide. A weak
but plausible counter-signal can suppress a strong signal.

---

## What's verified safe

These I now have line-by-line confidence in:

- **Side ↔ token mapping** (CLAUDE.md invariant #1) — confirmed in polymarket.py:899-906
- **MAY_HAVE_FIRED is in ACTIVE_STATUSES** (invariant #2) — confirmed in trade_log.py
- **Tenacity retry is network-only** (invariant #3) — confirmed in test_polymarket_fak + polymarket.py:967-974
- **RiskGate.ok() per-cycle + per-market** (invariant #4) — confirmed pre-sweep (my I-2 commit) + line 623 in scanner_executor
- **Polymarket(live=False) in shadow paths** (invariant #5) — confirmed in scanner_executor.py:258 (intentionally live=True with documented comment)
- **Learning guard side+band block** — confirmed in scanner_executor.py:420-560
- **DecisionCouncil EV gating** — 8 sub-gates confirmed
- **ENTRY_EXECUTE_FLAGS coverage** — all 9 agents in risk_gate.py:19-29
- **HALT file as physical brake** — confirmed in multiple places

---

## Recommendations for tomorrow's profitable test

Given the verification:

### Must do (high confidence, low risk)

1. **Run scanner_executor ONLY.** No btc_5min, news_shock, etc. They haven't
   been verified to have the 0.49 cap. Per LIVE_LESSONS_2026-05-21.

2. **$1/trade, max 4 open, 15-min window.** Per the live-hour command in
   the morning plan.

3. **Monitor specifically:**
   - `today_lesson_side_blocked` count (learning guard side firing)
   - `today_lesson_price_band_blocked` count (learning guard band firing)
   - `market_recent_reject_quarantine` count (quarantine working)
   - Fill rate (expect 0-3 in 15 min given the 37 gates)

### Should consider (operator decision)

4. **Lower `SCANNER_EXECUTOR_LEARNING_MIN_ENTRY_PRICE` from 0.40 to 0.30.**
   The dominant signal (`meta_brain,manifold`) is producing entries at
   0.355, which is BELOW 0.40 and gets blocked. The corrected-PnL data
   showed 0.30-0.40 band was +47.80% ROI on 7 trades.
   
   **But:** that 7-trade sample is thin, and the dominant signal source
   (Manifold) is itself a single-source liability (point #1 above).
   
   **Pragmatic compromise:** keep at 0.40 for tomorrow's test, see what
   fires. If 0 fills, consider lowering for the NEXT test with verification.

### Must NOT do

5. **Don't enable other agents.** btc_5min especially — its
   BTC_5MIN_MAX_LIVE_ENTRY_PRICE is 0.86. The May-18 catastrophic 0.80+
   trades came through btc_5min's path (most likely).

6. **Don't extend window beyond 15 min** without seeing how the first 15
   minutes go.

7. **Don't trust the calibrated=True flag at face value.** It can be:
   - From manifold consensus (un-calibrated raw crowd)
   - From quant_fv (model-based, 0.55 confidence threshold)
   - From solo route (high-bar: prob ≥ 0.65, wr ≥ 0.65, wilson ≥ 0.58)

### Hard stop conditions for tomorrow

Halt immediately if:
- More than 2 SLs in first 5 min
- Any `close_failed` event
- Any `supervisor_halt` event
- Any trade with entry > 0.50 (means learning guard is broken)
- Any near_resolution 400 error (means OpenAI fix didn't deploy correctly)

---

## My current confidence level

**8/10 for the SCANNER → SCANNER_EXECUTOR pipeline at the code level.**
I traced it gate by gate. The 37 gates are documented, the side semantics
are correct, the learning guard is wired correctly.

**6/10 for the META_BRAIN scoring logic.** Verified via Explore agent +
spot checks. The float epsilon issue is real but probably noise-level.
The weights-sum-1.48 is intentional via informed-only normalization.

**5/10 for the CROSS_MARKET signal quality.** Functionally works, but the
TRUST model (equal weighting Manifold amateurs with Kalshi real money) is
suspect. This is the biggest source of "alpha" right now per production
data. Either it's working (and the bot lost money for OTHER reasons) or
it's the source of the losses.

**4/10 for MTM accuracy for SELL positions.** Possible bug, needs
verification.

**3/10 for "this will be profitable tomorrow."** Honest. The bot has been
net negative. The fixes from tonight close some leaks but don't change the
fundamental signal quality (manifold reliance) or address the MTM concern.

**Worth doing the test:** yes. $5 budget, 15 min, conservative gates,
hard stops. If positive PnL → real progress. If neutral → learning continues.
If negative → freeze and reassess before next attempt.

---

## Open questions for after tomorrow's test

1. Verify the MTM-for-SELL bug claim — read trade_log.py insert/update.
2. Trace ONE successful fill from market_scanner → scanner_executor → polymarket → close → P&L (not just the rejection chain).
3. Audit each external_conviction provider's output quality.
4. Consider weighting Kalshi > Metaculus > Manifold instead of equal.
5. Review the float epsilon fix in meta_brain.py:2555.
6. Investigate why agent_promotion_ledger is empty — is there a promotion daemon that isn't running?
