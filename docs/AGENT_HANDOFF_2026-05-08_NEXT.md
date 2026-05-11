# Handoff for the next agent (last update 2026-05-09 morning)

> Originally written end-of-day 2026-05-08. Top section updated
> 2026-05-09 morning with new findings.

## Update — 2026-05-09 afternoon (exhaustive strategy sweep)

After morning's MR backtest, user asked if any strategy + market combo
hits 55% WR. Built 2 more harnesses (`backtest_scalper_sweep.py`,
`backtest_market_sweep.py`) and ran 30/30/30 day split test.

**Result:** no strategy passes 55% WR with stability. Market sweep
on 90-day window initially showed 2 passing cells (sports/other ×
no_bias_hold), but 30/30/30 split revealed it was a regime-specific
artifact:
- Sports / no_bias_hold: 64.0% / 57.5% / **44.9%** across windows
- Sports / cheap_hold_0.20: 30.0% / 33.3% / **15.0%** across windows
- Other / no_bias_hold: 58.6% / 57.9% / **37.5%** across windows

Recent 30-60 days favored underdogs / NO outcomes. Earlier 30 days
followed normal favorite-bias. Strategy was riding a regime, not
edge.

**CLOB price-history retention is ~90 days** (verified empirically:
1 of 10 markets aged 90-180d returned data). Can't validate longer
windows without paid feed.

**Conclusion identical to advisor's earlier prediction:** btc_daily
is the only strategy with stable backtest evidence. swarm stays
dryrun. No new flips.

See `docs/SESSION_2026-05-09_MARKET_SWEEP.md` for full details.

## Update — 2026-05-09 morning

Three concrete results from a multi-hour investigation today:

### A. swarm is in dryrun (env drift)

`.env` has `BOT_MODE="live"` but the running container reports
`mode=dryrun`. Container was never recreated after the env change.
**This means swarm has not actually been placing orders all this
time** — every "skip submitted order exists" log was double-blocked
(dedupe gate AND dryrun). Cleaning up `pending_orders` rows + restart
exposed slippage_guard as the next layer, but BOT_MODE is still
preventing real execution.

To flip live: `docker compose up -d --force-recreate polymarket-swarm`.
**But don't, until backtest justifies it (see C below).**

### B. Tier 1 alerting layer replaces 30-min cron toil

Built `scripts/python/state_watcher.py` — diff-driven alerts on poly1
trades, swarm fills, container health. Silent when nothing changed.
Cron now fires a short prompt ("שקט." on no-change). See
`docs/SESSION_2026-05-09_ALERTING_LAYER.md`.

### C. mean_reversion backtest fails 65% gate decisively

Built `scripts/python/backtest_mean_reversion.py` with explicit
spread-based slippage modeling (the lesson from yesterday's scalper
mistake). 30 days × 372-409 entries depending on spread:

| spread | 30d win rate | 30d paper PnL |
|---|---|---|
| **0¢** (ceiling, impossible) | **43.5%** | +$3.33 |
| 1¢ | 33.2% | -$11.49 |
| 2¢ (typical) | 26.9% | -$22.13 |
| 3¢ | 15.9% | -$35.46 |

**Even at zero slippage the WR ceiling is below 50%.** Fade-the-move
at 0.3%/180s on BTC daily is structurally a losing strategy. The
+$3.33 zero-slippage PnL comes purely from R/R asymmetry (TP=5¢ vs
SL=3¢) — eaten by realistic spreads. No tuning rescues this; the
strategy concept itself is wrong for this market type at this
timescale.

By user's principle (≥65% WR with realistic slippage): **swarm stays
in dryrun**. Don't flip BOT_MODE. See `docs/SESSION_2026-05-09_MR_BACKTEST.md`.

### D. Side cleanups (durable in DB)

- `pending_orders.id=240, 241` cleared (stale strait-of-hormuz fills
  from 5/6 that were blocking market_maker forever via
  `BLOCKING_STATUSES` containing 'filled'. The systemic bug — fills
  never transition to cleared on resolution — was deferred since
  swarm is in dryrun anyway).
- swarm restarted to refresh stale SQLite snapshot. This also
  silently fixed `mean_reversion`'s stale `may-8` slug (the bot only
  resolves once on `on_start`; restart re-resolved to `may-9`).

### Posture going into the rest of the day

| component | status | notes |
|---|---|---|
| btc_daily | LIVE, ~12 hrs no fills | feed FINE (404s yesterday were 2-min outage); skipping correctly because today's market is too directional (mid 0.235 < floor 0.30) |
| scalper | SHADOW | disabled 2026-05-08 evening after slippage-corrected backtest |
| swarm | DRYRUN | confirmed no path to live without backtest evidence |
| poly1 main / trader | $0 alloc | unchanged from 5/8 |
| cash | $54.2629 stable | unchanged 12+ hours |

## Read order (don't skip)

1. `deploy/CURRENT_STATUS.md` — top of file has today's summary
2. `docs/SESSION_2026-05-09_MR_BACKTEST.md` — backtest harness,
   spread sensitivity, decision
3. `docs/SESSION_2026-05-09_ALERTING_LAYER.md` — state_watcher.py,
   cron replacement
4. `docs/SESSION_2026-05-08_INFRA_FIXES.md` — yesterday's morning
   foundation
5. `docs/SESSION_2026-05-07_ALLOCATOR_AUTO_SYNC.md` — allocator_sync
   daemon, dust-bug fix
6. `docs/MARKET_BRAIN_STRATEGY_2026-05-07.md` — brain layer design

## Current live posture

- **$20 budget** split by `CapitalAllocator` across 3 trading systems:
  - `btc_daily` $12.50 (proven track, score=4.04)
  - `scalper` $1.50 (exploration mode)
  - `swarm` $6.00 across 4 sub-agents at $1.50 each (mean_reversion,
    market_maker, nothing_happens, ai_decision; arbitrage stub
    excluded)
- `position_manager` runs exit-only with 60s cycle, includes
  resolution_sync at the start of each cycle.
- `allocator_sync` daemon runs on the host (NOT in Docker), polls
  every 5 min, applies `.env` changes to both repos, restarts
  affected containers.
- The `trader` (poly1 main) is still at `$0` — see "Top open task".

## Top open task — DONE

Task `#36` (trader unblock) was completed 2026-05-08 evening. The
illiquid-market `ValueError("no asks available")` now writes
`SKIPPED_GATE` (veto, 0.06 penalty) instead of `FAILED` (0.45 penalty +
hard block). Code: `agents/application/trade.py:310-318`. Test:
`tests/test_trader.py::TestTraderTopN::test_illiquid_market_writes_skipped_gate_not_failed`.

The trader's existing `errors=1` will roll off the 24-hour allocator
window automatically; on the cycle after that, `live_allowed=True` and
exploration mode will allocate `$1.50` to it.

## Top open task — observe btc_daily, the only proven horse

Final state of the day after late-evening backtest correction (see
"Critical correction" below):
- `btc_daily` $12.50 LIVE ($3/trade) — only proven strategy (60.7% win
  rate / 30d backtest / +$0.61 paper). The "winning horse" we wait on.
- `scalper` SHADOW — `EXECUTE_SCALPER="false"`. Defunded after
  slippage-aware backtest showed every threshold negative.
- `swarm` ~$6 partially live — market_maker stale order id=244 cleared;
  ai_decision/nothing_happens/mean_reversion still gated by their own
  filters but unblocked at the infra layer.
- Cash on-chain: ~$54.26.

**What to do tomorrow morning:**
1. `docker exec poly1-position-manager python /app/scripts/python/capital_allocator.py --hours 24`
2. Look at `btc_daily` `realized_pnl_usdc` and trade count.
3. Track toward 30+ live trades before considering position-size scale-up.
4. If any agent past `defund_floor=-2.0`, allocator already auto-defunded.

**Strategic posture:** *"אני מעדיף ללכת על סוס מנצח אני רק מחכה למצוא
אותו"*. btc_daily is the only horse that passed an honest backtest.
Don't add new agents; don't tune thresholds; don't build harnesses for
swarm sub-agents (LLM/news not replayable from price-history anyway).
Wait for evidence.

## Critical correction — scalper backtest was wrong

**What I claimed in the morning:** raising
`MARKET_BRAIN_SCALPER_MIN_EDGE_SCORE` 0.35 → 0.60 produced 65.4% win
rate / +$2.10 paper PnL on n=46 entries. "First data-driven config
change in the project."

**What was actually true:** the backtest exited at `mid` with zero
slippage. Live `agents/application/exit_executor.py:38-43` applies
`mid * (1 - 0.02)` = 2% slippage on every FAK SELL. Re-run with
`--slippage 0.02` (now a CLI flag in `backtest_scalper.py`):

| `min_edge_score` | Entries | Win Rate | Paper PnL |
|---|---|---|---|
| 0.35 | 110 | 27.4% | **-$5.92** |
| 0.50 | 73 | 21.7% | **-$4.50** |
| 0.60 | 47 | 23.3% | **-$1.57** |

Strategy edge (5-7%) is smaller than round-trip slippage (4%) on
current Polymarket 15m spreads. Scalper is structurally unprofitable
at the current spread regime.

Action taken: `EXECUTE_SCALPER="false"` in `.env`. The 65% claim is
retracted in `docs/SESSION_2026-05-08_BACKTEST_HARNESSES.md`.

**Lesson for any future backtest: model fees + slippage explicitly.**
A passing paper PnL without execution costs is paper math, not edge.

**What NOT to do:** re-enable scalper without honest slippage-modeled
backtest showing positive edge across 7+ days of varied market data
(current local DB has only ~2 days of `scalper_pairs`). Don't tune
`MARKET_BRAIN_SCALPER_MIN_EDGE_SCORE` — formula's mathematical ceiling
in current markets is 0.30, anything above that vetoes 100%. Don't
build backtests for swarm sub-agents (LLM/news/orderbook depth not
replayable from price-history).

## Hard rules (unchanged)

1. **$20 hard cap** on the experiment. Wallet balance is ~$55 cash;
   the rest (~$35) stays untouched.
2. **Don't bypass the allocator.** Exploration mode is part of its
   logic, not an override of it. Manual `.env` edits will be
   overwritten on the next 5-min cycle.
3. **Don't ship code that has TODO-paths active in live execution.**
   Yesterday btc_daily ran with `# TODO[btc_daily]: place a real sell`
   in production — that's how 14 entries went to $0 with no exits.
4. **Cross-check journal MTM against on-chain CTF balance** before
   reporting portfolio state. Journal lies after market resolution.

## Where each layer lives

| Concern | Module | Notes |
|---|---|---|
| Strategy execution | `agents/application/{btc_daily,scalper,trade}.py`, `~/Desktop/poly/bot/agents/*` | per-agent |
| Decision approval/veto | `agents/application/market_brain.py` | classify+score per agent type |
| Real exit (FAK SELL) | `agents/application/exit_executor.py` | shared by position_manager + btc_daily |
| Open-position management | `agents/application/position_manager.py` | TP/SL/trailing/timeout |
| Resolution detection | `agents/application/resolution_sync.py` | NEW today; covers BOTH poly1 and swarm-side resolutions |
| Capital allocation | `agents/application/capital_allocator.py` | scoring + budget split + exploration mode |
| Allocation enforcement | `scripts/python/allocator_sync.py` | host daemon, 5-min cycle |
| Risk gating | `agents/application/risk_gate.py` | reserves + floor + drawdown |
| Trade journal | `agents/application/trade_log.py` + `data/trade_log.db` | source of truth for poly1 |
| Swarm journal | `~/Desktop/poly/bot/data/swarm.db` | source of truth for swarm; resolution_sync writes pnl_events into this DB |

## Quick health-check commands

```bash
export PATH=$PATH:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin

# Containers
docker ps --format '{{.Names}} | {{.Status}}' | grep -E 'poly1|swarm|btc'

# Allocator daemon alive
ps aux | grep -v grep | grep allocator_sync.py

# Latest allocator decision
tail -5 data/logs/allocator_sync.log

# resolution_sync activity
docker logs --since 5m poly1-position-manager | grep resolution_sync | tail -3

# Real cash on-chain
docker exec poly1-position-manager python -c \
  "from agents.polymarket.polymarket import Polymarket; \
   print(f'\${Polymarket(live=True).get_usdc_balance():.4f}')"

# Full allocator report (read-only)
docker exec poly1-position-manager python /app/scripts/python/capital_allocator.py \
  --budget 20 --hours 24 --swarm-db /app/swarm/data/swarm.db
```

## Things that are intentionally NOT done

- **mean_reversion backtest harness** — agent hasn't fired live, build
  only if it becomes relevant. ~1-2h when needed.
- **Auto-tuning closed loop** — backtest is advisory, humans decide.
  Don't add a tuner that mutates `.env` automatically.
- **trader migration to Claude** with structured output. This is the
  cleanest fix for the current `errors=1` block but wasn't done in
  this session.
- **Dust reconciliation for long-stuck positions** — 125 tokens are
  dust on-chain with markets still open. No journal cleanup written.
- **Gamma query batching** — `_sync_swarm_resolutions` calls Gamma
  per (agent, market_id) pair each cycle. Idempotency keeps it bounded
  but a future optimization is to cache or batch.

## Things that ARE done (don't redo)

- **swarm P&L → allocator** — completed late-morning 2026-05-08. See
  `docs/SESSION_2026-05-08_INFRA_FIXES.md` section "swarm P&L →
  CapitalAllocator".
- **resolution_sync** for poly1-side — covers FILLED, BTC_DAILY_OPEN,
  SCALPER_LEG rows.
- **Dust idempotency in `_already_closed`** — fixed yesterday; the
  position_manager won't double-attempt a close on a token that's
  already been settled.
- **Backtest harnesses for btc_daily + scalper** — built evening
  2026-05-08. See `docs/SESSION_2026-05-08_BACKTEST_HARNESSES.md`.
  Re-run via:
  - `docker exec poly1-position-manager python /app/scripts/python/backtest_harness.py --days 30`
  - `docker exec poly1-position-manager python /app/scripts/python/backtest_scalper.py --hours 48 --max-pairs 200 --slippage 0.02`
- **`backtest_scalper.py` slippage support** — `--slippage` flag added
  late evening 2026-05-08 after live/backtest divergence revealed the
  exit-at-mid assumption was wrong. Default 0.02 matches live.
- **scalper disabled** — `EXECUTE_SCALPER="false"`. Slippage-aware
  backtest showed every threshold negative; structurally unprofitable
  at current 15m spreads. See "Critical correction" above.
- **stale market_maker order cleared** — `pending_orders.id=244`
  (status='submitted' from 2026-05-07 20:02 UTC) marked
  `cleared_stale_2026-05-08_22:30`. Had been blocking market_maker
  quoting for 23+ hours via `has_active_order_for_market()` gate.

## What can change while you're not watching

The allocator-sync daemon will redistribute the $20 if any agent's
score crosses thresholds. Likely overnight changes:

- `swarm sub-agents` start firing decisions → score rises → exploration
  share grows
- `btc_daily` realizes a loss > $2 → defund_floor triggers,
  `live_allowed=False`, its $12.50 redistributed
- `scalper` realizes any meaningful loss → score drops, exploration
  floor stays but proportional share shrinks

You'll see them in `allocator_sync.log` as `set X=Y` lines and
container restarts.

## End of handoff

Anything else, dig in the session logs. The work this session was
deliberately small in scope (2 infrastructure pieces + 1 mode flag),
deliberately self-critical (every change has an "unvalidated, watching"
disclaimer), and deliberately reversible (one env knob turns
exploration off).
