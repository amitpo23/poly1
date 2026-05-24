# SESSION 2026-05-24 — Morning Live Test Postmortem

**Duration:** ~10 minutes live (frozen at 07:03 UTC)
**Budget:** $10 / position_size $2 / max_open 10 / agents=scanner_executor only
**Result:** **0 trades fired. $21.20 cash unchanged. Zero damage.**

---

## What happened

### Activity in 15-min window (last freeze→freeze)

| Agent | Approved | Rejected |
|---|---|---|
| **market_scanner** | **1,614** | — |
| **scanner_executor** | 0 | **1,614** |
| external_conviction_crypto_tape | 0 | 4,951 (provider noise) |
| opportunity_factory | 0 | 2,006 (gating its own candidates) |
| external_conviction_alpaca | 0 | 1,020 |
| Other external_conviction agents | 0 | thousands (provider noise) |

**scanner_executor processed ALL 1,614 market_scanner approvals → rejected ALL 1,614.**

### Why all 1,614 rejected

| Reason | Count | % |
|---|---|---|
| `market_recent_reject_quarantine` | 1,200 | 74% |
| `score_below_executor_min` | 265 | 16% |
| `taker_entry_below_stop_on_spread` | 41 | 3% |
| `raw_ev_below_council_min` | 30 | 2% |
| `probability_not_calibrated` | 22 | 1% |
| `net_ev_below_council_min` | 22 | 1% |
| `risk_gate_blocked` | 17 | 1% |
| Others (orderbook/quality/timing) | 17 | 1% |

The dominant blocker: **quarantine cascade** (1,200/1,614 = 74%). Same ~137
distinct markets being scanned repeatedly. After 5 fails per market in
5min, quarantined for 1h.

### What the brain was actually approving

| Side | Count | % |
|---|---|---|
| **SELL** | **1,425** | **88%** |
| BUY | 189 | 12% |

| Price band | Count | % |
|---|---|---|
| <0.20 | 2 | 0% |
| 0.20-0.30 | 25 | 2% |
| 0.30-0.40 | 1 | 0% |
| **0.40-0.49 (profit zone)** | **19** | **1%** |
| 0.49-0.50 | 95 | 6% |
| 0.50-0.60 | 182 | 11% |
| 0.60-0.70 | 360 | 22% |
| **0.70+** | **930** | **58%** |

**81% of approvals are at price > 0.50** (out of bot's profitable zone).
**Only 1% in the profit zone 0.40-0.49.**

### Signal source distribution

| Source | Count | % |
|---|---|---|
| `meta_brain,manifold,manifold:manifold` | 1,312 | **81%** |
| `opportunity_factory,alphainsider_proven,crypto_tape` | 287 | 18% |
| `meta_brain,manifold,manifold:<other>` | 15 | 1% |

The brain is dominantly leaning on **Manifold consensus** (per Tier 0a +
Alpaca weight bump, the manifold path is now the top signal generator).

---

## Root cause analysis

### 1. Brain produces wrong-zone candidates

The brain is rewarding "obvious" outcomes (high probability = high score),
which on Polymarket means HIGH PRICES (favorites). The bot's edge,
empirically, is in the 0.40-0.49 BUY zone (underpriced near-50-50
outcomes). These are RARE in market_scanner's output.

Result: 81% of scanner approvals are at price > 0.50 → the bot's losing
zone per corrected 30-day PnL data.

### 2. Learning guard EXPIRED (bug)

`SCANNER_EXECUTOR_LEARNING_GUARD_TTL_HOURS=24`. Container uptime was
37 hours. `_learning_guard_active()` returns False after TTL expires.

The side+band protection (BUY only, 0.40-0.49) wasn't firing during
this test. The other gates (score, EV, quality) caught the trades, but
the learning guard's specific protection was OFF.

**Fix:** TTL should reset on container restart OR on every live-hour arm
event. Current implementation: started_ts set in __init__ → never resets
during container lifetime.

### 3. Quarantine cascade

74% of rejects were re-rejects of already-quarantined markets. The
quarantine threshold (5 rejects in 5 min → 1h quarantine) is correct
in spirit but the scan cadence (60s) means a market hits the threshold
fast. With 137 markets × ~12 scans/5min, many markets bounce 5+ times
in their first 5 minutes.

The quarantine helps avoid spam but produces a "shrinking universe"
effect: legitimate markets get blacklisted after early rejects.

### 4. The few profitable-zone candidates get filtered

Of 19 approvals in 0.40-0.49 BUY zone, all were rejected by spread/EV/
quality/risk gates. These are SUPPOSED to fire but the small sample
combined with strict EV thresholds means even valid candidates rarely
pass.

---

## What we learned (positive)

✅ **Infrastructure works end-to-end.** Scanner → meta_brain → scanner_executor
→ reject chain → audit trail → freeze. All wired correctly.

✅ **Zero damage.** $21.20 cash unchanged. No leaked positions. No stuck
trades. The bot can run live without losing money — IF zero fills is
acceptable.

✅ **Safety gates work.** 1,614 candidates rejected for legitimate reasons.
DecisionCouncil + score + quarantine + RiskGate all firing.

✅ **Freeze + HALT discipline holds.** `runtime_control.py freeze` halted
cleanly. No race conditions.

✅ **The Tier 0a/0b fixes are operational.** Learning guard env vars
present, calibrated bypass closed for wallet path, markouts running,
backups fresh.

---

## What we did NOT learn

❌ **Whether the bot can actually trade profitably.** Zero fills = zero
data on outcomes. Can't assess win rate.

❌ **Whether the learning guard works in practice.** Bug means it was
expired; protection didn't fire.

❌ **Whether the AlphaInsider re-enabled path generates good trades.** 287
approvals from opportunity_factory,alphainsider_proven,crypto_tape → all
rejected by downstream gates. Path is active; quality not measurable.

❌ **Whether Alpaca weight 0.20 changes anything.** Alpaca contributed to
the weighted consensus but the brain still primarily leans on Manifold.

---

## Next session plan — to get fills happening

To produce trades for the operator's profitable-trading goal, ONE of:

### Path A: Fix signal quality (right path, harder)

The brain is producing wrong-zone candidates. Need to:
1. Investigate WHY meta_brain ranks Manifold-dominant SELL @ 0.70+ above
   BUY @ 0.40-0.49.
2. Adjust the scoring formula to reward profit-zone candidates.
3. Possibly: add a meta_brain side-aware penalty for high-price entries.

**Effort:** 2-4 hours of code work + verification.

### Path B: Loosen executor gates (faster, riskier)

Relax executor thresholds to let more candidates through:
- Lower `SCANNER_EXECUTOR_MIN_SCORE` from 0.80 to 0.65
- Disable learning band check (allow 0.50+ entries)
- Lower `DECISION_COUNCIL_MIN_NET_EV` from 0.04 to 0.02

**Risk:** This recreates the May 12 / May 21 conditions (we'll trade
losing-zone candidates). Likely net negative.

### Path C: Fix the learning guard TTL bug first

Per "Root cause #2", learning guard expires after 24h container uptime.
This is a real bug — restart on each live-hour arm event would fix.

**Effort:** ~15 min code + test + deploy.

### Path D: Hunt different markets

The current scanner finds 137 markets per cycle, mostly crypto 5min.
The profit-zone candidates are rare in this universe. Possibilities:
- Increase market diversity (different SCANNER_MARKET_LIMIT)
- Different `SCANNER_TARGET_TRADE_DECISIONS`
- Lower `SCANNER_MIN_LIQUIDITY` (more candidates but less liquid)

**Effort:** Env tuning + observation.

---

## Recommended next session

**Combine C + A:**

1. **Fix learning guard TTL bug** (C) — restart on each `live-hour --arm`.
   Code change in `runtime_control.py` to send a signal, OR in
   `scanner_executor.py` to read the runtime_control updated_at and reset.
2. **Investigate brain's high-price bias** (A) — read meta_brain
   line-by-line for why SELL @ 0.70+ outranks BUY @ 0.40-0.49.
   The score formula has a bug or wrong intent.
3. **Then re-test** with a fixed guard and clearer expectations.

**Time estimate:** 1 session (~2-3 hours).

---

## Operator's directive carry-over

Per `agent_scope_directive.md`: next session(s) should also begin
verifying each entry agent's gate chain so the bot can run multi-agent
(not just scanner_executor only).

Combined with the Path A investigation above, this is the right work
to make the system both safe AND productive.
