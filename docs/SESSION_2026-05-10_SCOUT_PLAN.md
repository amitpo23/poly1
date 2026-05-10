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

## Open follow-ups

| # | Item | Notes |
|---|---|---|
| – | Scout step 1-5 | This plan; estimated 6-8h |
| – | btc_daily strategy-PnL trend | Watch for 30+ live trades. Cash-WR vs strategy-WR divergence is a real concern |
| – | 53 unpushed commits + many untracked core files | The repo is in "lots of work, not committed" state. Cleanup is its own task |
| – | Vault sync to iCloud/Dropbox | Knowledge-store, not code; git not appropriate |
