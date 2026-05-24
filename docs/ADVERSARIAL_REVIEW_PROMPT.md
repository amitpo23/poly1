# Adversarial Review Prompt — poly1 session 2026-05-24

You are reviewing a marathon session that built a Bayesian probability
engine + 5 infrastructure fixes for a real-money Polymarket trading bot.
The operator has $20.50 of real capital at stake. The bot is currently
frozen. **Your job is to find bugs, weaknesses, and counterfactuals
before the next live arming.** Be hostile to the work. Confirm nothing
on faith.

---

## 1. The cardinal question

The bot lost **−$0.803 today**. After Round 10 lost **−$0.475** on a
single gap, infrastructure was added to prevent recurrence. Three
subsequent rounds (R11/R12/R13) fired **zero trades** — the Bayesian
gate rejected every candidate.

**Two possible truths:**
- (A) The gate is correctly disciplined; the data genuinely shows no
  edge, and 0-trade rounds are the right outcome.
- (B) The gate is mis-calibrated — it's blocking trades that would
  have been profitable, and we built a sophisticated rejection
  machine.

**Which is it? Build evidence either way.**

---

## 2. Specific work to scrutinize

All changes pushed to `main` between commits `f0db42c` (prior session)
and `f4caa2f` (end of session). Commits to focus on:

```
43a81db  fix: anti-gap SL override for crypto_momentum (scanner_executor)
46f0855  feat: btc_5min skip-reason INFO logging
da44fec  feat: orderbook_monitor watches recent SHADOW_ENTER tokens
9bcccdc  feat: hash-stale self-heal marker + health_check.py
8dc56ce  docs: SPEC.md §§24-26
f4caa2f  fix: btc_5min skip dedupe resets on period change
```

Plus the in-flight infrastructure from earlier the same day (Bayesian
engine, multi-pipeline calibrator, dust terminator, phantom sweep, P1
auto-recreate-on-arm) — see `docs/SESSION_2026-05-24_BAYESIAN_ENGINE_HANDOFF.md`.

### 2.1 Anti-gap protection (commit 43a81db)

**File:** `agents/application/scanner_executor.py` —
`_per_position_exit_overrides()` method.

**Question 1**: The detection uses 4 OR-d heuristics. Test each
in isolation. What if a market matches 1 heuristic but isn't actually
a 5-min binary? E.g., a 1-hour BTC market with `signal_source` containing
`crypto_tape` — does it get the wrong SL?

**Question 2**: SL=0.03 + TP=0.08 = RR of 2.67. The original Trade 4237
loss was 47% on a 1-second gap. SL=3% with FAK exit on illiquid book —
does the SL actually fire at 3%, or does slippage cascade like Trade 4237?
Inspect `position_manager._close_via_exit_executor` to see whether the
SL trigger price is the executable bid or just the last mid.

**Question 3**: response_json mutation across shadow + live paths —
does the override propagate correctly through both? Try
`grep -n "response_json\[.sl_pct_override.\]" agents/application/scanner_executor.py`
to find all write sites.

### 2.2 btc_5min logging dedupe (commits 46f0855 + f4caa2f)

**File:** `agents/application/btc_5min.py` — `_log_skip()` method and
all 6 call sites in `maybe_enter()`.

**Question 1**: The dedupe key is `(reason, period_ts)`. What about
`elapsed` in `timing_too_early elapsed=1.1s` vs `elapsed=3.0s`? Each
has different `reason` strings → all log. Are we spamming logs?
Count: how many INFO lines does btc_5min emit per minute now?

**Question 2**: The fix in f4caa2f was triggered by realizing dedupe
hid the in-window state. But are there OTHER skip paths in `maybe_enter()`
that still don't call `_log_skip` at all? Inspect lines 458-540.

**Question 3**: The first `bootstrapped N price samples` line gets
logged 5x per asset on every recreate. Is there value in this verbosity,
or is it noise?

### 2.3 Shadow watchlist (commit da44fec)

**File:** `agents/application/orderbook_monitor.py` — `_tokens()` method.
**File:** `agents/application/trade_log.py` — `recent_shadow_decision_tokens()`.

**Question 1**: `token_limit=60` (default) or `80` (env override).
Universe + filled positions may already saturate. Shadow tokens are
appended AFTER. Test: if universe gives 60 tokens, do shadow tokens
get truncated entirely?

**Question 2**: The lookback window is 24h. But orderbook_snapshots
only go back ~24h too. So a SHADOW_ENTER from 23h ago needs the future
snapshot at 23h+5min = 23h55m, which doesn't exist yet. Is the watchlist
adding tokens whose markout window has already passed?

**Question 3**: The `is_market_universe_eligible` filter for the
universe path — does it allow shadow tokens to get polled at the same
priority, or are universe tokens always preferred?

### 2.4 Hash-stale self-heal (commit 9bcccdc)

**File:** `agents/application/risk_gate.py` — `_signal_hash_mismatch()`.
**File:** `scripts/health_check.py`.

**Question 1**: The marker file is written on EVERY mismatched call
to `runtime_control_reason()`. That can be 100+ times per minute.
Is there I/O overhead? Should there be rate-limiting?

**Question 2**: The marker filename uses `agent_slug` from
`os.getenv("RUNTIME_AGENT")`. What if RUNTIME_AGENT is unset? Falls
back to `"unknown"`. Could two containers race-write `unknown_HASH_STALE`?

**Question 3**: `health_check.py` exits 1 on any marker. But once
markers exist, every cron run sends an alert. Should `--clear` be
automatic? Should there be a "last alerted" debounce?

### 2.5 Bayesian engine (earlier in session)

**File:** `agents/application/probability_calibrator.py`,
`agents/application/bayesian_aggregator.py`,
`agents/application/multi_pipeline_calibrator.py`.

**Question 1**: Wilson lower bound at 95%. For n=46 with wr=22%
(alphainsider × 0.50-0.54), Wilson_lower=0.12. That's extremely
conservative. Is 95% the right confidence level? What if we used
50% (effectively the point estimate)? How many of today's rejected
candidates would have fired?

**Question 2**: The `per_source_band` segments cross signal_source × price_band.
But: what about side (BUY/SELL)? Currently `per_action` is separate.
A BUY at alphainsider × 0.50 may be very different from a SELL at the
same band. Should we cross 3-way: source × band × action?

**Question 3**: 89 matched closes out of 165 total. Why are 76 (46%)
unmatched? Inspect `_agent_from_cycle_id()` mapping. Is there a
cycle_id pattern we're not handling?

**Question 4**: The gate only applies to scanner_executor. btc_5min
fires directly. So btc_5min's EV=−$0.045 is ignored by the gate.
Should it be? If we're confident in Bayesian segmentation, btc_5min
should also be filtered.

---

## 3. Specific data investigations

### 3.1 The 13,332 unused signals

`external_conviction_crypto_tape` writes **13,332 brain_decisions/hour**
but **0 BUY/SELL approvals**. That's 222/min, ~3.7/sec.

```sql
sqlite3 data/trade_log.db "SELECT agent, action, approved, COUNT(*) 
  FROM brain_decisions 
  WHERE ts > datetime('now', '-1 hour') 
  GROUP BY agent, action, approved 
  ORDER BY 4 DESC LIMIT 30"
```

**Is this agent producing useful signal or just CPU exhaust?** If it's
making predictions on every market it sees, what's the prediction?
Look at the `features_json` column — does it contain
direction/confidence data that we could feed into the Bayesian engine
even though `action` is SKIP?

### 3.2 The "consensus_router" mystery

Per the prior memory `consensus_router_2026_05_24.md`, a 2-source-agreement
infrastructure was built but `consensus_enabled=False`. **Why was it
disabled?** Read the commit (`43c674e`) and decide if it's still right
to leave it off.

### 3.3 Calibration sample distribution

```python
import json
d = json.load(open("data/probability_calibration.json"))
for entry in d.get("per_source_band", []):
    if entry.get("n", 0) >= 3:
        print(entry["key"], "n=", entry["n"], "ev=", entry.get("expected_value_per_trade"))
```

Only ~8 segments have n≥3. The Bayesian gate would benefit from more
granular segmentation. **Should we collapse to fewer, larger buckets?**
(e.g., merge 0.50-0.54 + 0.55-0.59 into 0.50-0.59.) Would that produce
actionable wilson_lower values for more candidates?

### 3.4 The btc_5min historical loss

btc_5min has n=46 closed trades with EV=−$0.045. **Inspect those 46
trades.** Where are the wins concentrated? Where are the losses?

```sql
SELECT json_extract(response_json, '$.pnl_usdc_real') AS pnl,
       json_extract(response_json, '$.sl_pct_override'),
       json_extract(response_json, '$.tp_pct_override'),
       price, ts
FROM trades
WHERE status LIKE 'closed_%'
  AND cycle_id LIKE 'btc_5min%'
ORDER BY pnl DESC;
```

Do the wins cluster around specific market conditions (volatility,
time-of-day)? Could we add a regime filter?

---

## 4. Architectural challenges

### 4.1 Three pipelines, one gate

scanner_executor goes through Bayesian gate. btc_5min, scalper,
near_resolution bypass it (direct_execution). Shadow agents don't even
fire trades. **Is this asymmetry correct?** Make the case for unifying
all entry agents under the Bayesian gate, or argue why they should
remain independent.

### 4.2 The "no-edge" loop

Calibration produces calibration_json. Bayesian gate reads it. Gate
rejects most candidates. Few trades fire. → calibration sample stays
small. → gate stays conservative. → fewer trades.

**Is this a stable feedback loop or a death spiral?** What's the
unblocking mechanism? Shadow markouts populate the sample without
firing trades — but only for tokens the orderbook_monitor watches.
Is the watchlist self-limiting?

### 4.3 The 5-min binary problem

Trade 4237 lost 47% in 37 seconds. 5-min Up/Down markets resolve
binary — winner takes all. The bot's exit logic was unable to react.
**Should 5-min binary markets be EXCLUDED entirely, not just have
tighter SL?** Make the case for "no 5-min trades, period."

---

## 5. What's NOT done (verify or build)

From `MEMORY.md` and session notes:
- `external_conviction` 0-approval investigation
- Shadow research simulator for 7 external_conviction agents
- `health_check.py` not wired to cron
- btc_5min new SL=0.03/TP=0.08 has never been live-tested
- 11 phantom positions remain on-chain (each $1+ exposure)

**Pick the one most likely to materially change PnL in the next 7 days
and explain why.**

---

## 6. Where to read

- `SPEC.md` — current architecture (§§24-26 are new today)
- `docs/SESSION_2026-05-24_BAYESIAN_ENGINE_HANDOFF.md` — earlier session handoff
- `docs/POLY1_WORKING_DISCIPLINE.md` — operator's 7 working rules
- `~/Desktop/poly/OPERATIONS.md` — joint operations playbook
- `~/.claude/projects/-Users-mymac-coding-poly1/memory/MEMORY.md` — prior session memory chain
- `agents/application/CLAUDE.md` / project root `CLAUDE.md` — guardrails
- Server: `ssh trader@83.229.82.193`, working dir `/srv/poly1`
- Wallet on chain — check explicit balance via Polymarket API

Read commit messages with `git log --oneline -30` for the chronology.

---

## 7. How to challenge

When you find an issue:
1. **Construct a concrete failure scenario.** Not "this could break" —
   "if input X arrives at time Y with state Z, output is W which is
   wrong because A."
2. **Quantify the harm.** Lost dollars, missed trades, false rejects,
   data corruption.
3. **Propose the smallest fix.** Don't redesign; identify the one-line
   change.
4. **Note the test that would have caught it.** Was the existing test
   covering this case? If yes, why didn't it fail? If no, what test
   would have?

When you find a winrate opportunity:
1. **Cite the specific calibration data row** (segment, n, EV).
2. **Compute counterfactual:** "If we'd taken every BUY at price <0.40,
   we'd have X trades at avg PnL Y, total Z."
3. **Show what the bayesian gate currently outputs for that segment.**
4. **Propose the threshold change.** Be precise: env var, current value,
   proposed value, expected trade volume increase.

---

## 8. Tone & approach

- The operator built this codebase. Be respectful, but don't soften
  hard truths.
- "I disagree with X because Y" is welcome.
- "X is fine" is also welcome if you actually checked.
- Don't recommend changes you haven't verified solve the problem.
- The bot has real money. Anything that increases trading volume MUST
  be paired with risk evidence, not just "probably works."

End your review with a 5-bullet `## VERDICT` section:
- 🔴 Critical bugs found (must fix before next arm)
- 🟡 Concerns (should investigate)
- 🟢 Validated (checked, looks fine)
- 💡 Winrate opportunities (with quantification)
- 📋 Still uncertain (what to dig into next session)
