# Session log — 2026-05-17: forensic audit of 5/10–5/16

## Why this audit exists

User asked: *"מה מצב המסחר?"* + *"תעשה בדיקה מלאה ותתעד כל מה שקרה
בימים האחרונים"*. Cash dropped from $54.26 (2026-05-10 end of day)
to $41.21 (2026-05-17 morning) = **-$13.05 / -24%**, well past the
8% drawdown threshold the user originally set for manual action.

## Headline numbers

| metric | value |
|---|---|
| Cash 2026-05-10 EOD | $54.26 |
| Cash 2026-05-17 morning | **$41.21** |
| Drawdown | **-$13.05 / -24%** ⚠️ |
| btc_daily live trades (5/8–5/13) | 9 closed (3 TP / 6 SL) |
| Realized WR | **33%** (vs 60.7% backtest claim) |
| Realized PnL identifiable | -$4.18 |
| Unexplained delta | -$8.87 (likely unrealized on stale positions + gas) |

## Closed trades with explicit PnL (in order)

| Date | Status | Entry | Exit | cash PnL | strategy PnL |
|---|---|---|---|---|---|
| 5/08 10:19 | TP | 0.50 | 0.5145 | +$0.12 | +$0.087 |
| 5/08 13:15 | SL | 0.50 | 0.4165 | +$0.41 (dust) | -$0.68 |
| 5/08 15:26 | SL | 0.50 | 0.2891 | -$0.10 | -$2.04 |
| 5/11 19:39 | filled@0.14 → 5/12 resolved_loss | 0.14 | $0 | -$1.37 | -$1.37 |
| 5/12 01:27 | TP | 0.50 | 0.5145 | +$0.12 | +$0.087 |
| 5/12 01:35 | SL | 0.50 | 0.3871 | -$0.66 | -$0.68 |
| 5/12 06:27 | filled@0.145 → closed_dust 33s later | 0.145 | 0.142 | -$0.05 | -$0.05 |
| 5/13 00:26 | SL | 0.50 | 0.3283 | -$1.02 | -$1.03 |
| 5/13 13:36 | SL | 0.50 | 0.3038 | -$1.20 | -$1.18 |

**Total realized cash PnL: ~-$3.75**
**Total realized strategy PnL: ~-$6.91**

Cash and strategy differ because of dust monetization (some "losing"
strategy trades produced positive cash via leftover shares from
prior closes — see the 5/8 13:15 row).

## Where the other -$8.87 went

Most likely sources, in order of probability:

1. **Stale open positions from 5/4–5/6** (never closed):
   - Token `16952...` @ 0.997 × $5.10 cost basis (2 fills)
   - Token `11575...` @ 0.11 × $3.65 cost basis (2 fills)
   - Combined: $8.75 cost basis on tokens that almost certainly
     resolved against us by now. On-chain CTF balance check needed
     to confirm.

2. **Gas fees + slippage on 552 close attempts** that ended in
   `resolved_loss` for one position (token `41879...` on 5/12).
   Probably small but accumulated.

3. **230 `closed_dust` rows on 5/12** — position_manager hammered
   tiny positions repeatedly, hitting `min_exit_notional_usdc=1.0`
   guard. Each dust event = tiny residual loss as price ticked
   down.

## New events I didn't know about

### `trading_supervisor` container (new, Up 3 days)

The supervisor fired 4 `supervisor_halt` events on 5/12 at
16:11–16:14 UTC, all with reason `"critical exit-path guard
tripped"`. Some new defensive layer was added that detected
abnormal state and halted trading. Worth understanding what it
guards before relying on it.

### `settlement-reconciler`, `wallet-watcher`, `news-signal`

Three additional containers added (3-4 days uptime). `news-signal`
is currently UNHEALTHY. Architecture has expanded since 5/10
without my involvement — someone (or another agent session) shipped
these.

### `poly1` / `poly1-scalper` / `polymarket-swarm` — STILL DOWN

These 3 containers have been missing since 2026-05-15 morning. The
state_watcher has been alerting `🚨 container missing` continuously
for 48+ hours. Live trader path is offline; scalper SHADOW path is
offline; swarm DRYRUN is offline.

### `swarm.db` corruption

`DatabaseError: database disk image is malformed`. Patched
state_watcher to be resilient (try/except around `_gather_swarm`),
but the DB itself is still corrupt and needs sqlite repair or
restore from backup.

## Per-day breakdown

```
                fill   open   tp   sl   dust   close_fail  failed
2026-05-10         0      0    0    0    0        0           221
2026-05-11         1      2    0    2    8        218          57
2026-05-12         1      3    1    1  230        334          22
2026-05-13         0      2    0    2    0          0           2
2026-05-14         0      0    0    0    0          0           0
2026-05-15         0      0    0    0    0          0           3
2026-05-16         0      0    0    0    0          0           2
```

5/12 was the bad day: 334 close_failed retries, 230 closed_dust
events. Position_manager spent the day fighting an unclosable
position. By 5/13 things normalized but the cash was already gone.

## Win-rate evolution

| period | TP | SL | total | WR |
|---|---|---|---|---|
| Backtest claim | — | — | 28 | 60.7% |
| 5/8 (3 trades) | 1 | 2 | 3 | 33% |
| 5/12-13 (5 trades) | 1 | 4 | 5 | 20% |
| **Combined 8 trades** | **2** | **6** | **8** | **25%** |

(Plus 1 `resolved_loss` and 1 `closed_dust` that close to break-even.)

**The backtest WR claim of 60.7% is not holding up in live trading.**
Live is 25%, which is even worse than the +33% strategy_pnl 33% rate
we already worried about on 5/10. The cash-WR vs strategy-WR
distinction is no longer the relevant question — **both are bad**.

## Decision implications

By the user's own original principle (defund if drawdown crosses 8%),
btc_daily should already be defunded:

- Current drawdown: -24% (3× the 8% threshold)
- WR: 25% on 8 trades (clearly below 60.7% claim)
- Even if some -$8.87 unexplained is recoverable, baseline tells
  us strategy isn't working

## Immediate actions (recommended, not done)

1. **HALT btc_daily** — touch `/Users/mymac/coding/poly1/data/HALT`
   to engage the kill-switch.
2. **Restart missing containers** — `docker compose up -d poly1
   poly1-scalper polymarket-swarm` (or however the user prefers).
3. **Run on-chain CTF balance check** on tokens `16952...` and
   `11575...` to confirm/deny the unrealized loss theory.
4. **Repair `swarm.db`** or replace from backup.
5. **Investigate `trading_supervisor` supervisor_halt** — what
   triggered it? Is it still tripping?

## What I did NOT do (deliberately)

- Did NOT touch any container (start/stop/restart) without explicit user
  permission — the system is in an unhealthy state and a wrong
  action could lose more money.
- Did NOT defund or halt btc_daily — that's a strategic decision the
  user should make explicitly with knowledge of the data above.
- Did NOT close any open position manually — they're stale but
  selling at current price might lock in losses worse than waiting
  for resolution.

## Files modified during the audit

Only state_watcher.py was edited today, to add `_error` field on
the swarm read so the watcher doesn't crash on `database disk image
is malformed`. No commit yet — fold into next commit when user
approves a path forward.
