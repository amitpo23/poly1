# Session log — 2026-05-10: Scout system design + Phase A (WR infrastructure)

## Context

60+ hours since last fill (2026-05-08 15:26 UTC). User frustration
at the silence is real and legitimate. We've done exhaustive backtesting
and found that btc_daily is the only proven strategy; the 6 other
agents either fail the 55% WR + stability gate or have no backtest
at all.

User asked for two things this session:
1. **Per-agent WR computation** based on existing data, with auto-
   activation when WR passes a threshold.
2. **A central agent that hourly scans Gamma + Tavily across all
   strategies** and dispatches tasks when it finds opportunities.

These map to two different risk levels:
- WR computation = safe; `CapitalAllocator` already does this
- Auto-activation = the *exact* pattern that bit us yesterday with
  scalper (0.35→0.60 → 2 hours live → -$1.65)

Per advisor's repeated warnings (3× this session), an auto-activator
running hourly would replicate yesterday's scalper mistake at 24×
the rate. The fix isn't to add more inputs (Gamma + Tavily) —
adding inputs to a flawed decision pattern doesn't fix the pattern.

## Phase A — completed today (committed `ab041d1`)

Added `wins`/`losses`/`win_rate` to `AgentScore`. Wins/losses
counted on closed_* and resolved_* rows by sign of `pnl_usdc_real`.

Discovered an existing bug while implementing: `closed_*` rows from
position_manager **didn't write `pnl_usdc_real` to response_json**.
3 historical close rows from 2026-05-08 had no PnL data.

Fixed in same commit by augmenting `position_manager._close_position`:
- `pnl_usdc_real` = `actual_proceeds - pos.total_cost_usdc` (cash PnL)
- `strategy_pnl_usdc` = `shares_to_sell * (sell_price - pos.avg_entry_price)` (matched-shares only)
- `cost_basis_usdc`, `actual_proceeds_usdc` for traceability

Backfilled the 3 historical rows manually via SQL `json_set()`.

**Surprising finding from backfill:** btc_daily yesterday closed
3 trades. Cash-PnL was +$0.44 over the 3. **Strategy-PnL was -$2.63.**
The +$0.44 cash came purely from **dust monetization** — leftover
shares from earlier closes were sold at the new sell price, even
though only 6 shares were paid for per open.

This means the allocator (which scores cash-PnL) sees a winning
strategy. A human evaluating signal quality (strategy-PnL) sees a
losing one. The 60.7% backtest WR for btc_daily may not reflect
real strategy edge; the dust effect inflates apparent performance.

3 trades is too few to draw conclusions. Wait for 30+ live trades
before scaling btc_daily up. If strategy-PnL stays negative, defund.

## Decision: build a Scout, NOT an auto-activator

### What the Scout does (additive, safe)

`scout` cron — hourly:
- Scans Gamma `/markets` for each strategy's filter:
  - `nothing_happens`: cheap-NO, "Will X happen by Y" speculative
  - `market_maker`: spread ≥2¢, mid 25-75%, liq ≥$50k
  - `mean_reversion`: BTC/ETH/SOL daily up/down
  - (placeholders for future strategies #5/#6/#9 from the 10-strategies doc)
- Pulls Tavily news per candidate market (keyword search on slug)
- Scores by *replayable heuristics* — NOT predicted WR (that's the trap)
- Writes ranked list to `data/scout_opportunities` table
- `state_watcher` picks up new rows and alerts the user

### What the Scout does NOT do

- ❌ Activate any agent
- ❌ Modify any `.env`
- ❌ Change `BOT_MODE`
- ❌ Promote any candidate to live

### Workflow

```
scout (hourly) → opportunity surfaced → human reviews →
  → backtest_market_sweep on candidate → split-test passes ≥55% →
  → human flips BOT_MODE=live for that one strategy
```

The bottleneck stays at "human + backtest evidence", which is what
prevented yesterday's mistake from being a habit.

## Tiered plan (~6-8h focused work)

| step | what | time | status | deliverable |
|---|---|---|---|---|
| 1 | Wire Tavily into `news_signal.py` (additional source alongside RSS) | 1-2h | ✅ done | `fetch_tavily_items()` working; verified on "strait of hormuz iran" returned 8 relevant Iran/Hormuz news |
| 2 | Build `scout` cron — Gamma scan + Tavily lookup + scoring | 2-3h | ✅ done | `scripts/python/scout.py` writes to separate `data/scout.db` (avoids WAL contention with trade_log.db). First run found 3 candidates: btc_daily, US-Iran peace deal, Arsenal title. |
| 3 | `state_watcher` extension — alert on new opportunities | 30m | ✅ done | Watcher tracks `scout.scout_opportunities.max_id` + top candidate (highest score). Verified alert fires when new rows appear. |
| 4 | Build `backtest_nothing_happens.py` with news signal | 2-3h | next | harness with split test |
| 5 | If step 4 passes 55% WR with stability | 30m | pending | flip BOT_MODE=live for `nothing_happens` only |

### Cron jobs in this session

| id | cron | action |
|---|---|---|
| `1590c7bb` | `17,47 * * * *` | state_watcher (silent on no-change) |
| `131149fb` | `23 * * * *` | scout (writes new opportunities) |

Sequence per hour: scout writes at :23 → watcher picks up new rows at :47.

### Steps 1-3 verification commands

```bash
# Tavily fetcher direct
python3 -c "
import os
from pathlib import Path
for line in Path('/Users/mymac/coding/poly1/.env').read_text().splitlines():
    if line.strip() and not line.startswith('#') and '=' in line:
        k, _, v = line.partition('='); os.environ.setdefault(k.strip(), v.strip().strip('\"'))
import sys; sys.path.insert(0, '/Users/mymac/coding/poly1')
from agents.application.news_signal import fetch_tavily_items
for i in fetch_tavily_items('Bitcoin price today', limit=3):
    print(f'  - {i.headline[:80]}')
"

# Scout direct (writes to scout.db)
python3 /Users/mymac/coding/poly1/scripts/python/scout.py --db-path /Users/mymac/coding/poly1/data/scout.db

# Scout opportunities table
sqlite3 /Users/mymac/coding/poly1/data/scout.db \
  "SELECT id, strategy_match, score, market_slug, news_count, top_news_headline \
   FROM scout_opportunities ORDER BY score DESC LIMIT 10;"
```

Step 5 is a *result* of step 4, not a guaranteed outcome. If step 4
fails (likely scenario per `no_bias_hold` precedent), we accept it,
log to vault, and stay on btc_daily-only.

## What's deliberately NOT in scope today

- **Auto-activation logic** — the central piece the user asked for
  is replaced by surfacing-to-human + backtest gate. This is the
  guardrail. Don't build it.
- **Strategies 5/6/9 from the 10-strategy doc** (Resolution Drift,
  Correlated Pairs, Range-Bound). Out of scope today; if scout
  starts surfacing candidates that match these patterns, build the
  backtest first, then the agent. Days of work.
- **Whale tracking (#7)** — same: defer, but the scout could
  surface "wallet X just bought $5k of NO" as a heuristic later.

## Files to be added

- `agents/application/news_signal.py` — extend with Tavily fetcher
- `scripts/python/scout.py` — the new hourly cron
- `scripts/python/backtest_nothing_happens.py` — strategy backtest
- `data/scout_opportunities` (SQLite table) — opportunity queue
- `docs/SESSION_2026-05-10_SCOUT_PLAN.md` — this file
- `~/coding/poly vault/Decisions/2026-05-10_scout_not_auto_activator.md`

## Files modified in this session (committed `ab041d1`)

- `agents/application/capital_allocator.py` — `wins`/`losses`/`win_rate`
- `agents/application/position_manager.py` — `pnl_usdc_real` augmentation
- `scripts/python/capital_allocator.py` — text output shows W/L + WR

## Verification commands

```bash
# Verify A1+A2 (committed):
docker exec poly1-position-manager python /app/scripts/python/capital_allocator.py --hours 168
# → look for "W/L=2/1 WR=66.7%" on position_manager line

# Run backfill verification (3 historical rows have PnL):
sqlite3 ~/coding/poly1/data/trade_log.db "
  SELECT id, json_extract(response_json,'\$.pnl_usdc_real')
  FROM trades WHERE id IN (618, 642, 938);"
# Expected: 0.12 / 0.4146 / -0.096
```

## Final session results (added end-of-day 2026-05-10)

### Step 4 verdict — nothing_happens fails the gate (committed `62ca40e`)

The harness uses `data-api.polymarket.com/trades` (paginated). This was
the breakthrough that let us get history for political markets where
CLOB `/prices-history` returns 0 samples.

| Window | n | WR | PnL/$ |
|---|---|---|---|
| 0-30d | 34 | 32.4% | +$8.06 |
| 30-60d | 22 | 4.5% | -$3.89 |
| 60-90d | 40 | 12.5% | -$13.92 |

The 0-30d window matches the agent's docstring claim (33% WR with
positive EV from R/R asymmetry). But 30-60d and 60-90d are
catastrophic. **Regime-dependent strategy** — won't pass user's gate
of ≥55% WR with stability across 3×30d windows. Net 90-day PnL: -$9.75.

**Decision:** nothing_happens stays in dryrun.

### Bonus: strategy #5 (Resolution Drift) and #9 (Range-Bound) — also fail

Built `scripts/python/backtest_strategy_5_9.py` to test the two
unimplemented strategies from the 10-strategies design doc:

| Strategy | Window | n | WR | PnL/$ | Verdict |
|---|---|---|---|---|---|
| #5 Resolution Drift | 0-30d | 0 | n/a | $0 | filter too narrow — inconclusive |
| #5 | 30-60d | 0 | n/a | $0 | (same) |
| #5 | 60-90d | 1 | 100% | +$0.06 | n=1, noise |
| **#9 Range-Bound** | 0-30d | 1496 | **21.9%** | **-$39.84** | ❌ |
| **#9** | 30-60d | 2222 | **20.2%** | **-$64.12** | ❌ |
| **#9** | 60-90d | 4188 | **23.7%** | **-$96.54** | ❌ |

**Strategy #9 fails decisively:** 7906 trades, 22% WR, -$200 PnL.
Range-bound mean reversion at TP=0.50 / SL=0.40 with 2% slippage
needs ~50% WR to break even; observed is 22%. Not viable.

**Strategy #5 was inconclusive** — only n=1 across 300 markets. The
0.55-0.95 entry band is too narrow. Could be retried with looser
filter (0.52-0.98) but priors suggest still fails.

### Code review fixes (committed `62ca40e`)

3 correctness bugs caught by `feature-dev:code-reviewer` agent before
the commit landed:
- `scout._persist`: was using `conn.total_changes` (cumulative since
  conn open) → switched to `cur.rowcount` (per-statement). Without
  this, all post-first-insert calls reported True, inflating
  insertion counts.
- `scout._filter_mean_reversion`: missing the `days_to_end is None`
  guard the other filters had → could crash mid-loop on malformed
  Gamma data + the `reason` f-string had `cand.get('days_to_end',
  0):.1f` which fails on None (key present, value None). Added guard
  + made all numeric formatters use `(x or 0)`.
- `state_watcher`: top_candidate fields could be None per scout.db
  schema; crash in `_diff` would loop watcher every cron tick AND
  silence other alerts. Added isinstance + or-fallback.

### Combined verdict end of day

Three strategies tested today via the new data-api harness; **0/3
pass the gate**. btc_daily remains the only proven strategy.

The scout + state_watcher infrastructure is durable and useful even
without a winning second strategy — operator gets surfaced
opportunities instead of needing to manually check.

The Phase B price_snapshots table starts collecting from now on. In
~30 days we'll have our own time-series for the candidates the scout
found. That data could potentially be used to backtest with looser
slippage assumptions or different exit rules.

## Late-afternoon additions (commits `e328042` + `cf4f1fb`)

### Manual entry tool (`e328042`)

User asked for a way to take directional bets ("BTC up in 90d", "oil
down in 90d") at $2.50/trade with auto-exit at +20% TP. None of our
algorithmic strategies do directional momentum — all fade. Backtest
gates apply only to ALGORITHMIC agents; manual user-conviction
trades are a separate code path with explicit operator approval.

Built:
- `position_manager.AggregatedPosition.tp_pct_override` + `no_sl`
  fields. Populated from response_json on the originating filled
  row. When set, _evaluate_position uses simple TP-only rule and
  bypasses the brain's compound exit logic.
- `trade_log.filled_positions_with_id` extended to include
  response_json so the aggregator can read overrides.
- `scripts/python/manual_entry.py` — CLI:
  ```
  --slug X --side YES|NO --size-usdc 2.5 --tp-pct 0.20 [--no-sl] --execute
  ```
  Resolves Gamma slug → token_ids, fetches best_ask, places FOK BUY,
  writes filled row with overrides.

Verified dryrun on `will-wti-reach-110-in-may-2026-116-472` NO at
0.5950 → +20% TP exit at 0.7140.

### Momentum backtest (`cf4f1fb`)

User wanted a momentum agent (chase BTC trend) but agreed to
backtest first per discipline. Built `backtest_btc_momentum.py`:

| Window | n | WR | PnL/$ |
|---|---|---|---|
| 0-30d | 23 | 30.4% | -$1.20 |
| 30-60d | 21 | 33.3% | -$0.81 |
| 60-90d | 0 | n/a (Gamma gap) | $0 |

**Verdict:** 0/3 windows pass. Momentum on BTC daily fails decisively
(30-33% WR, both windows lose money after 2% slippage on TP/SL exits).
**No momentum agent built.** Harness stays in repo.

This brings today's strategy-test count to **4 candidates, 0 pass:**
nothing_happens, #5 Resolution Drift (inconclusive), #9 Range-Bound,
momentum. Plus yesterday's scalper sweep (0/19) and mean_reversion
(structurally broken).

## End-of-day summary commits

| commit | what |
|---|---|
| `ab041d1` | Phase A — wins/losses/WR + cash-PnL |
| `001f849` | Scout S1-S3 |
| `62ca40e` | Code review fixes + nothing_happens backtest |
| `73b4202` | Final docs + #5/#9 backtests |
| `438e3c8` | Handoff doc |
| `b2c7f96` | Date attribution fix |
| `e328042` | Manual entry + per-position TP |
| `cf4f1fb` | Momentum backtest (no agent built) |

## Open follow-ups

| # | Item | Notes |
|---|---|---|
| – | btc_daily strategy-PnL trend | Watch for 30+ live trades. Cash-WR vs strategy-WR divergence is a real concern. Last trade was 2026-05-08; 2 days idle since. |
| – | First manual_entry execution | User has $2.50/trade ready; pending operator command via CLI |
| – | Strategy #5 with looser filter | Quick re-run with (0.52-0.98) entry band; ~30min |
| – | Strategy #6 Correlated Pairs | Not built. Needs cross-market price data. ~3-4h harness build |
| – | Strategy #7 Whale Tracking | Not built. Needs Polygon RPC scraping. ~1-2 days |
| – | 53 unpushed commits + many untracked core files | The repo is in "lots of work, not committed" state. Cleanup is its own task |
| – | Vault sync to iCloud/Dropbox | Knowledge-store, not code; git not appropriate |
