# Counter-Review of Adversarial Review — 2026-05-25

The adversarial review (received 2026-05-25) produced 4 critical bugs,
5 concerns, 9 validated, 3 winrate ops. This document is the
empirical verification — what survived contact with the database
vs what collapsed.

## 🟢 Reviewer was RIGHT (validated empirically)

### Y1 — log spam defeats dedupe ✅
**Measurement**: `docker compose logs --since=60s btc_5min | grep -c "btc_5min\["` → **180 lines/60s**.

After `sed -E 's/elapsed=[0-9.]+s/N/g'` → **5 unique lines** (one per asset).

The elapsed-in-reason defeated the period_ts dedupe entirely. Each
iteration produced `elapsed=227.1s`, `elapsed=228.7s`, etc.,
distinct keys, every iteration logged.

**Fixed in commit 4d3cbe5**: `_normalize_reason_for_dedup()` regexes
numeric values out of the dedupe key before comparison. The full
reason with concrete elapsed is still emitted; only the dedupe key
collapses.

### C3 — 55 phantom positions, not 11 ✅
**Measurement**: `SELECT COUNT(*) FROM trades t1 WHERE status IN
('filled','btc_5min_open','near_resolution_open') AND NOT EXISTS
(...closes...)` → **55**.

The "11" I reported earlier was from
`sweep_stale_phantom_open.py --dry-run` which only counts dust
candidates (size_$1.00_above_dust). The broader query reveals 55.

**Action for next session**: build on-chain reconciliation pass —
for each of 55, query the CTF balance to determine if it's
(a) genuinely held (settle), (b) zeroed but never marked closed
(write resolved_loss), (c) tiny dust (closed_dust).

### C4 — `require_universe_top` + `market_doc=None` log without dedupe (partial)
Reviewer claimed these are **silent**. **They are not**:

- Line 499: `logger.info("btc_5min: skip — %s not in focused top universe...")` — DOES log
- Line 508: `logger.info("btc_5min: no market found for period %d", ...)` — DOES log

But they use `logger.info` directly, **not** `self._log_skip` →
**no dedupe** → these spam every iteration when triggered. So
the symptom (verbosity) is real, the diagnosis ("silent") is wrong.

**Action**: convert both to `_log_skip` calls for consistency.
Lower priority than Y1 fix.

## 🔴 Reviewer was WRONG (counter-evidence)

### W3 — hour-of-day winrate ❌ BUNK (single-day overfit)
Reviewer's claim: "btc_5min hour 10 UTC = 64% wr, restrict to UTC 9-13
for +$0.55/month."

**Measurement**: all 38 btc_5min trades that produced these stats
are from **a single date**: 2026-05-19.

```
hour|n  |tp|sl|net_pnl|date
10  |11 |7 |4 |+0.434 |all 2026-05-19
11  |13 |5 |8 |-0.819 |all 2026-05-19
14  |3  |1 |2 |-0.515 |all 2026-05-19
15  |11 |3 |7 |-1.153 |all 2026-05-19
```

This is **11 trades in one specific Sunday morning's BTC price
action**, not a time-of-day structural pattern. Implementing
`BTC_5MIN_ALLOWED_HOURS_UTC` based on this is exactly the kind of
overfit the Bayesian gate is built to reject.

**Action**: ignore W3. Hour-of-day filter would need 100+ days of
data across mixed regimes before being statistically meaningful.

### C1 — anti-gap SL=0.03 wouldn't help Trade 4237 ❌ WRONG
Reviewer's argument: even with SL=0.03, the FAK exit would have
hit empty book and produced the same $0.475 loss.

**Counter-evidence from `orderbook_snapshots`**:

| t (sec from entry) | bid | bid_depth_usdc |
|---:|---:|---:|
| Entry 15:05:07 (BUY @ 0.40) | 0.45 | $3,642 |
| **15:05:09 (+2s) — SL=0.03 would trigger** | **0.38** | **$3,005** |
| 15:05:13 (+6s) | 0.32 | $2,124 |
| 15:05:22 (+15s) | 0.24 | $1,431 |
| 15:05:33 (+26s) — actual SL fired | 0.20 | $1,069 |
| 15:05:35 (+28s) | 0.19 | — |

At t=+2s, the book had **$3,005 of depth at bid=0.38** — more
than enough to fill a $1 sell at 0.38 with no slippage. SL=0.03
(trigger 0.388) would have executed there → ~$0.05 loss.

The reviewer mistook the AFTERMATH state (at t=+26s, where bid_depth
was already collapsed to $1,069) for the moment SL=0.03 would have
fired (at t=+2s, where depth was 3× larger).

The actual cascade happened because **the global SL=0.06 waited
until trigger=0.376**. By the time price fell to 0.376, the book
was already collapsed and we got 0.20 fills.

**Conclusion**: SL=0.03 anti-gap protection **does** mitigate this
class of loss. ~89% reduction (from $0.475 to $0.05). The commit
43a81db is justified.

### C2 — `"crypto_tape" in signal_source` substring → false positives ❓ UNVERIFIED
Reviewer's concern is theoretically valid. The dominant signal
pattern is `opportunity_factory,alphainsider_proven,crypto_tape`
with 19,443 brain_decisions. If even 10% of these are long-horizon
markets, SL=0.03 would fire prematurely.

**Could not verify**: the response_json path the reviewer suggested
(`features.trade_proposal.evidence.horizon`) returned no data via
`json_extract`. The actual structure may differ. Without horizon
data, the scope of false-positive risk is unknown.

**Action for next session**: trace where horizon is stored
(scanner_executor.py `_per_position_exit_overrides` heuristic 4
references it but the path may be different at write time vs read
time). Once located, run:
```sql
SELECT horizon, COUNT(*) FROM ... WHERE signal_source LIKE '%crypto_tape%'
```
If >5% of crypto_tape trades have horizon != "5m", the AND fix
(reviewer's recommendation) becomes warranted.

## 🟡 Reviewer was PARTIALLY right (small impact)

### Y5 — 24h shadow lookback ⚠️ structural but not blocking now
**Measurement**: 2 SHADOW tokens in last 24h, both have snapshots.

Reviewer's structural argument is correct — tokens added to
watchlist retroactively can't have past snapshots that don't exist.
But the immediate impact today is small (2 tokens, both already
covered).

**Action**: defer until external_conviction shadow simulator is
built. The simulator would walk decision_journal SHADOW_ENTER rows
and look up snapshots that DO exist within the 24h window. That's
the right unblocker, not the lookback itself.

## Cumulative assessment of the review

| Category | Reviewer claims | Validated | Wrong | Partial |
|---|---:|---:|---:|---:|
| 🔴 Critical | 4 | 1 (C3) | 2 (C1, W3 in disguise) | 1 (C2, C4) |
| 🟡 Concern | 5 | 1 (Y1) | 0 | 4 |
| 🟢 Validated | 9 | not re-checked | — | — |
| 💡 Winrate | 3 | 0 | 1 (W3) | 2 (W1, W2 unverified) |

**The review was thorough but mixed.** Strongest contributions: Y1
(real bug, fixed), C3 (real undercount, action needed). Weakest:
W3 (overfit recommendation) and C1 (mis-read chronology that would
have led to abandoning a working fix).

**Lesson for future reviews**: when claiming "X wouldn't have worked"
about an exit strategy, verify the orderbook state at the time the
strategy WOULD have fired, not the time it actually fired. The 24
seconds between SL=0.03 trigger and SL=0.06 trigger is a full
cascade in 5-min binary markets.

## Action items from this counter-review

1. **Done now**: Y1 dedupe fix (commit 4d3cbe5)
2. **Next session high priority**:
   - C3: on-chain reconciliation of 55 phantoms
   - C2 verification: trace horizon path in response_json
3. **Next session lower priority**:
   - C4: convert require_universe_top + market_doc_None to `_log_skip`
4. **Reject (don't implement)**:
   - W3 (BTC_5MIN_ALLOWED_HOURS_UTC) — overfit on single-day data
   - C1 (FAK fallback) — anti-gap SL=0.03 already does ~89% of the work; FAK fallback adds complexity for marginal additional protection
