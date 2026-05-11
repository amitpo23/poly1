# Activation Log — 2026-05-06 Evening

Context: after the morning's wallet-unification migration, the user
flagged that bots were not actually trading despite the live state.
Root-cause review across docs led to two structural problems:

1. **Existing agents tuned conservatively** — by design (per
   `OVERNIGHT_AND_MULTI_WEEK_OPERATION.md`). Triggers rarely fire.
2. **Scalper had a silent bug** — 0 pairs in 9+ hours of shadow.

This evening's session addressed both, plus built a new dedicated
short-term agent. All changes documented below in execution order.

---

## 1. Scalper bug fixed (BLOCKER)

**Symptom:** `scalper_pairs` had 0 rows ever. `trades` had 0
`scalper_leg` rows ever. The scalper appeared healthy but was silently
not finding markets.

**Root cause:** `agents/application/scalper.py:287` called
`gamma.get_events_by_tag(tag_id=21)` with the default `limit=50`.
Polymarket sorts events by `endDate` ascending, so the first 50 were
all stale 5-minute markets. The 21 active 15-minute markets were
beyond the cutoff. Verified empirically: `limit=50` → 0 of 21 active
15m markets seen; `limit=200` → all 21 visible, all `acceptingOrders=True`.

**Fix:** explicit `limit=200` in the scalper's call.

**Verification:** within seconds of restart, `scalper_pairs` populated
with 21 rows (BTC/ETH/SOL/HYPE/XRP/DOGE/BNB × current + next + next-next
period). State `tracking` for current periods, `expired` for stale.

**Second bug found while flipping live:**
`agents/application/scalper.py:203` had `if self.execute and _FAK_TYPE
== "FAK":` — but `OrderType.FAK` from `py_clob_client_v2` is a StrEnum
whose value IS `"FAK"`, so the equality always held even when V2 was
properly installed. Live boot raised `RuntimeError("py_clob_client_v2
not installed")` despite the package being present.

**Fix:** introduced an explicit `_V2_INSTALLED: bool` sentinel set in
the import-try block and checked instead of the value-equality.

After both fixes: `ScalperDaemon: starting (execute=True)` — Stage 1
LIVE.

---

## 2. Loosened thresholds for activity

User explicitly accepted reduced "edge fidelity" in exchange for more
trades. Changes in `~/Desktop/poly/bot/config.py`:

| Param | Before | After | Reason |
|---|---|---|---|
| `MarketMakingConfig.target_spread_cents` | 1.0 | **0.5** | The 1.0¢ filter was rejecting every market (spreads exactly equal to target). |
| `MeanReversionConfig.btc_move_pct_trigger` | 0.010 | **0.004** | Original 1% threshold produced zero entries; 0.4% should produce multiple events per day. |
| `MeanReversionConfig.cooldown_seconds` | 480 | **180** | Faster re-entry after exits. |
| `MeanReversionConfig.take_profit_cents` | 4.0 | **2.0** | Quicker exit on small reversals. |
| `MeanReversionConfig.stop_loss_cents` | 4.0 | **3.0** | Slightly tighter loss cap. |

Swarm rebuilt and restarted live with the new tuning.

---

## 3. Scalper Stage 1 flip

`.env` change in poly1:
```
EXECUTE_SCALPER="false"  →  EXECUTE_SCALPER="true"
SCALP_LEG_USDC="2.5"  (unchanged — Stage 1 spec value)
```

After the two scalper bug fixes above, the daemon boots cleanly with
`execute=True`. Watching for first leg fills via:
```bash
sqlite3 data/trade_log.db "SELECT * FROM trades WHERE status='scalper_leg' ORDER BY id DESC LIMIT 10"
```

---

## 4. ANTHROPIC_API_KEY status

**User has `OPENAI_API_KEY`, NOT `ANTHROPIC_API_KEY`.** The swarm's
`ai_decision` agent uses `core.ai_advisor.AIAdvisor` which is
Anthropic-only (242-line module that imports `anthropic` SDK directly,
calls `anthropic.AsyncAnthropic`, parses Claude-style JSON).

**Decision:** leave `ai_decision` enabled (registered with $6 reserved
capital) but skipping every cycle with `reason=no ANTHROPIC_API_KEY
set`. Two paths to enable later:

- (a) operator provides Anthropic key, OR
- (b) rewrite `AIAdvisor` for OpenAI (~1-2h code change, separate task).

This is not a blocker for the rest of the activation.

---

## 5. New agent: `btc_daily` — fast in/out on BTC daily up/down

**File:** `agents/application/btc_daily.py` (~470 lines)
**Tests:** `tests/test_btc_daily.py` (12 tests, all passing)
**Container:** `poly1-btc-daily` under `profiles: ["btc_daily"]` in
`docker-compose.yml`

### Strategy

Targets the daily `bitcoin-up-or-down-on-{date}` Polymarket binary.
When BTC moves sharply over a short window, the market overshoots
in the same direction. Fade the move:
- BTC pumped → `SELL` (= buy NO)
- BTC dumped → `BUY` (= buy YES)

Built-in exits:
- **Take profit** at +`take_profit_cents` (default 2¢)
- **Stop loss** at -`stop_loss_cents` (default 3¢)
- **Timeout** after `max_hold_minutes` (default 30 min)
- **End-of-day** auto-close at 23:50 UTC (10 min before resolution)

### Trend filter

If a longer-window BTC move over `trend_window_minutes` (30 min) exceeds
`trend_threshold_pct` (2.5%) AND aligns with the short move, skip the
trade. Prevents fading a strong intraday trend.

### MVP boundary

Exit currently records the closing decision in the journal but does
**not** place an exit FOK order — that requires sell-side semantics
in `agents/polymarket/polymarket.py` which is currently `maintain_positions
= pass`. The agent's value at MVP is the disciplined entry decision
under risk-gate supervision; auto-flat is documented as a follow-up
(`docs/POLY1_EXIT_LOGIC_GAP.md`).

### Configuration (in `.env`)

```
EXECUTE_BTC_DAILY="false"      # shadow first
BTC_DAILY_RESERVE_USDC="6.0"
BTC_DAILY_POSITION_SIZE_USDC="3.0"
BTC_DAILY_TRIGGER_PCT="0.004"    # 0.4% in 3 min
BTC_DAILY_TAKE_PROFIT_CENTS="2.0"
BTC_DAILY_STOP_LOSS_CENTS="3.0"
```

### Risk gate integration

`RiskGate` constructor extended with `btc_daily_reserve_usdc`. The
`reserves` dict now has 3 keys: `scalper`, `swarm`, `btc_daily`.
`available_for_trader()` returns `cash - sum(reserves)`. Existing
tests adjusted to pass all three reserves explicitly to isolate from
env-driven values.

### Container status

```
poly1-btc-daily   Up   healthy   shadow mode (EXECUTE_BTC_DAILY=false)
```

Daemon logs `BtcDailyDaemon: starting (execute=False)`. Polls
Coinbase BTC price every 5 sec, evaluates entry trigger, records
shadow leg if conditions met. Heartbeat at `/app/data/btc_daily_heartbeat`.

---

## 6. Updated capital ledger

| Slice | Env var | $ | Notes |
|---|---|---|---|
| poly1 main | `STARTING_BALANCE_USDC` | 40 | journal-based drawdown |
| swarm | `SWARM_RESERVE_USDC` + `TOTAL_CAPITAL` | 20 | 4 funded agents × $5 |
| scalper | `SCALPER_RESERVE_USDC` | 0 | Stage 1 LIVE, $2.50/leg, eats from poly1 main slice |
| btc_daily | `BTC_DAILY_RESERVE_USDC` | 6 | shadow first, $3/position |
| **Total budgeted** | | **66** | over $63 actual pUSD by $3 — first to fill wins |

Note: scalper has `$0` formal reserve but uses up to 4 × $2.50 = $10
per active pair from poly1's slice when LIVE. This is intentional —
scalper trades are fast (15-min cycles) so the cash recycles. If
scalper holdings grow > $5, RiskGate's MTM accounting will reflect
the deployment correctly.

---

## 7. Test status

```
poly1 full Docker suite:  103 tests, all passing  ✅
  - new tests/test_btc_daily.py: 12 tests
  - existing test_trader.py adjusted: 3 calls now pass btc_daily_reserve_usdc=0
swarm focused suite:      173 tests, 2 deselected (live)  ✅
```

---

## 8. Containers state at end of session

```
NAME                  STATUS                MODE
poly1                 Up 9+ hours, healthy  live (poly1 main, $5/trade)
poly1-scalper         Up healthy            LIVE (Stage 1, $2.50/leg)
poly1-btc-daily       Up healthy            shadow (EXECUTE_BTC_DAILY=false)
polymarket-swarm      Up healthy            live (loosened thresholds)
poly1-dashboard-1     Up healthy            Streamlit at :8050
poly1-grafana         Up                    Grafana at :3000
```

---

## 9. Outstanding follow-ups

- **btc_daily live flip**: after watching ≥4-8 hours of shadow with
  trigger logic firing on real BTC moves, flip `EXECUTE_BTC_DAILY=true`.
- **btc_daily auto-flat**: currently records exits in journal but
  doesn't place sell orders. When poly1's `maintain_positions` is
  built (`docs/POLY1_EXIT_LOGIC_GAP.md`), wire btc_daily's exit
  decisions to it.
- **AIAdvisor for OpenAI**: rewrite `~/Desktop/poly/bot/core/ai_advisor.py`
  to support OpenAI as an alternative to Anthropic. Or have the
  operator obtain an Anthropic key.
- **Scalper observation**: with 14+ tracking pairs at any given time
  in Stage 1, expect first live FAK fills within 1-2 periods (15
  min). Watch for `RECONCILE_NEEDED` rows — those require manual
  on-chain verification per `docs/SCALPER_PNL_RUNBOOK.md` Part C.
- **Dashboard**: `Wallet — Capital Allocation Ledger` panel in
  Grafana still hardcodes `swarm=$40` from earlier in the day; should
  be updated to reflect current `swarm=$20`, plus add a `btc_daily=$6`
  row.

---

## 10. What "active trading" looks like now

Compared to morning state (5 fills total, 0 swarm, 0 scalper):

| Source | Trigger conditions | Expected daily fills |
|---|---|---|
| poly1 main (LLM) | LLM `confidence >= 0.6` + risk gate ok + dedupe | 0-2 per cycle, 30-min cycles |
| scalper (Stage 1) | sum_avg < 0.98 on `*-updown-15m-*` | dozens per day (each period is 15 min, multiple assets) |
| swarm market_maker | spread >= 0.5¢ on configured markets | varies with liquidity |
| swarm mean_reversion | BTC > 0.4% in 3 min, no aligned 30-min trend | 5-15 per day |
| swarm nothing_happens | NO ≤ 30¢ on speculative markets | 1-3 per day |
| swarm ai_decision | requires Anthropic key | 0 (key missing) |
| btc_daily | shadow only — observation phase | 0 fills, shadow logs |

Activity is now structurally enabled. Whether trades produce P&L is the
next question — that's measured over days/weeks of journal data, not
a single afternoon.

---

## 11. Monitoring loop (cron-based, session-only)

After the activation push, the operator asked for a 30-min status loop.
Set up via `/loop` skill:

- **Schedule:** `13,43 * * * *` (every 30 minutes at :13 and :43; offset
  off the round marks per cron-skill best practice).
- **Job ID:** `d9f199dd`
- **Lifecycle:** session-only, expires when this Claude session
  closes. Auto-deletes after 7 days regardless. Cancel sooner via
  `CronDelete d9f199dd`.
- **Prompt:** "check status of all 4 agents — fills last 30 min,
  pairs tracked, errors, drawdown. Hebrew summary. Flag urgent
  human-action items at top (RECONCILE_NEEDED, MAY_HAVE_FIRED,
  drawdown >8%, container down)."

For durable cross-session monitoring, would need `/schedule` instead
(cloud-based, not implemented this session).

---

## 12. T+0 monitoring report (17:13 UTC)

First check after the cron was set up.

### 🚨 Concerns surfaced

1. **2 × `RECONCILE_NEEDED` scalper pairs:**
   - `sol-updown-15m-1778087700` — leg1 filled (5.10 SOL-Up shares paid $2.50), leg2 never filled, period closing 17:30 UTC.
   - `xrp-updown-15m-1778087700` — leg1 filled (5.79 XRP-Up shares paid $2.50), same pattern.
   - Trigger: `period_expired_while_leg1_filled` per `reap_expired()`.

2. **poly1 main risk_gate showing `available 0.0000` and `swarm=40.00`:**
   - Running container env: `SWARM_RESERVE_USDC=40.0` (stale).
   - `.env` file: `SWARM_RESERVE_USDC=20.0` (current).
   - Container started at `07:57Z`, `.env` change came after Restore Checkpoint mid-day. Container has NOT reloaded env.

3. **14 scalper exceptions** in last 30 min logs (sample: `PolyApiException(error_msg="Request exception!")` — orderbook fetch timeouts; non-fatal, fills still landed).

### 🟢 Good news

- **Scalper Stage 1 is firing.** Four `scalper_leg` rows at 17:15 UTC:
  - `eth-updown-15m-...` SELL $2.50 + BUY $2.50 (pair attempt)
  - `xrp-updown-15m-...` BUY $2.50
  - `sol-updown-15m-...` BUY $2.50
- **Pair state:** 11 tracking + 19 expired (total 30 distinct pairs over the day).
- **No `MAY_HAVE_FIRED` rows.** Idempotency contract holds.
- **No errors in poly1 main or btc_daily logs.**

---

## 13. Advisor consultation + on-chain verification

After T+0 report, operator asked to consult advisor before any
remediation. Advisor responses paraphrased:

1. **`RECONCILE_NEEDED`: do not auto-handle.** Per
   `docs/SCALPER_PNL_RUNBOOK.md` Part C, operator verifies on-chain,
   then I do the SQL UPDATE based on what they report. Two real risks
   to auto-handling: (a) re-trigger leg2, double-fill on-chain;
   (b) mark `expired` from journal alone, lose track of a real
   on-chain position that later resolves at $1/share with no audit
   trail.

2. **Trader env reload: verify before recreating.** Cheap check:
   `docker exec poly1 sh -c 'echo SWARM_RESERVE_USDC=$SWARM_RESERVE_USDC'`
   to confirm running vs file. Don't recreate based on a guess. Also,
   `available=0` may be legitimate if cash genuinely depleted.

3. **14 scalper exceptions: not urgent.** Fills landed = order path
   works. Tail exception text in next 30-min report; only act if
   blocking.

### Verification results

**Env hypothesis confirmed (running stale, file current):**

```
running poly1 process: SWARM_RESERVE_USDC=40.0
.env file:             SWARM_RESERVE_USDC=20.0
```

**But the deeper cause — wallet near-empty:**

```
on-chain pUSD cash: $3.31
projected available with reserves=20+6: max(0, 3.31 - 26) = $0.00
projected available with reserves=40:   max(0, 3.31 - 40) = $0.00
```

**Recreate would NOT unblock poly1.** Cash is genuinely depleted; the
env discrepancy is cosmetic. Where the $60 went: 4 scalper legs today
($10) + multiple poly1 main fills + 2 reconciled swarm market_maker
fills + open RECONCILE_NEEDED positions ($5 still on-chain).

**Decision: defer the recreate.** No action benefit; the fix is to
wait for cash to recycle as positions resolve.

### On-chain verification of RECONCILE_NEEDED (read-only)

Operator authorized direct on-chain check (CTF.balanceOf via web3).
This IS the verification the runbook asks for — Polymarket UI is
just one interface.

| slug | Up shares (we hold) | Down shares | Up price now | Status |
|---|---|---|---|---|
| `sol-updown-15m-1778087700` | **5.1020** | 0.0000 | $0.255 | open, end 17:30Z |
| `xrp-updown-15m-1778087700` | **5.7955** | 0.0000 | $0.220 | open, end 17:30Z |

**Findings:**
1. Positions are real and held by deposit wallet on-chain.
2. Markets close at 17:30 UTC (within minutes of T+0 report).
3. Up is being priced as the LOSING outcome (~22-26% probability).
4. Expected value if resolved at current pricing:
   - SOL: 5.10 × $0.26 ≈ $1.30 (loss of $1.20 from $2.50 cost)
   - XRP: 5.80 × $0.22 ≈ $1.27 (loss of $1.23 from $2.50 cost)
   - Combined expected residual: ~$2.57 from $5.00 deployed.

**Decision tree (post-resolution):**
- If Up wins (currently unlikely): wallet receives $5.10 + $5.80 via
  Polymarket auto-redemption; mark both rows `leg2_filled` after
  confirming on-chain balance went to 0.
- If Down wins (currently likely): positions go to $0; balance stays
  0; mark both rows `expired`.
- If still pending past 18:00 UTC: anomaly, investigate.

**`RECONCILE_NEEDED` IS the correct local state until resolution.**
No SQL change today. The next 30-min cron firing will check post-17:30
balances and update accordingly with operator authorization.

---

## 14. Current concerns + open follow-ups

### Today

- **Wallet near-empty ($3.31).** poly1 main effectively blocked; new
  trades from any agent require capital recycling via:
  1. RECONCILE_NEEDED resolution (SOL+XRP at 17:30 UTC, ~$0-$10.90 returning)
  2. Older filled positions resolving over coming days
  3. Operator decision: top up the deposit wallet externally OR wait

- **Container env mismatch** (poly1 running with `SWARM_RESERVE=40`
  vs file=20). Cosmetic — doesn't affect anything until wallet has
  cash to deploy. Address with `--force-recreate` when convenient.

### This week

- Watch RECONCILE_NEEDED count. If it grows >3-4, the scalper is
  hitting its known leg2-failure mode regularly; investigate
  `evaluate_second_leg` thresholds.

- Watch btc_daily shadow logs. If 0 entries after 24h, BTC volatility
  is below 0.4%/3-min and the trigger is too tight; loosen further.

- Watch swarm mean_reversion entries. With trigger lowered to 0.4%,
  expect 5-15 entries/day on a typical BTC volatility day.

### Future

- See sections 9 + 14.x of this log for the durable list.
- Cloud-based monitoring (`/schedule`) for cross-session continuity.
- AIAdvisor port from Anthropic-only to OpenAI-compatible.

---

## 15. Cumulative session arc — what was achieved 2026-05-06

A long day. Three macro phases:

**Morning — Wallet unification migration**
- swarm migrated to V2 SDK + signature_type=3 + shared deposit wallet
- RiskGate refactored to journal-based portfolio_value + reserves dict
- 2 missing pUSD approvals set via builder relayer (gasless)
- Capital plan: poly1 $40 / swarm $40 / scalper $0 (then corrected to
  $40/$20/$0 after live fills reduced cash)

**Mid-day — Reconciliation + dashboard polish**
- 2 live swarm market_maker fills reconciled (`reconcile_orders.py`)
- Streamlit Swarm tab built with per-agent money summary
- Grafana panel for "Submitted Orders Needing Reconciliation"
- Trading review documented (3 fills today, $19.49 deployed total)

**Evening — Activation push**
- Scalper bug fixed (limit=50→200, V2_INSTALLED sentinel)
- Thresholds loosened (mm spread 0.5, mr trigger 0.4%, mr cooldown 180s, mr TP 2¢)
- Scalper Stage 1 LIVE flipped — first live FAK fills landed at 17:15
- New btc_daily agent built (470 LOC + 12 passing tests, container in shadow)
- 30-min monitoring loop scheduled
- T+0 status revealed wallet near-empty + 2 RECONCILE_NEEDED awaiting resolution

**Numbers:**
- 18 task list items completed across the session.
- ~15 commits + uncommitted local changes.
- 103 poly1 tests passing, 173/173 swarm focused tests passing.
- 4 live agents (poly1 main, scalper Stage 1, swarm 4/5 funded, btc_daily shadow).
- 1 deposit wallet, 4 capital slices, 1 unified ledger.

**Honest assessment:** the activation succeeded structurally. The
agents trade. Whether they make money is a question of days, not
hours. The wallet is currently near-empty pending position resolution
— not because anything is broken, but because the strategy choice
(small fast bets on volatile crypto) means money sits in positions
until they resolve.

End of 2026-05-06 evening session.

