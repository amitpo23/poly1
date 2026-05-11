# Session 2026-05-11 - Agent Trading Stabilization

## Operator Goal

Prepare the system for a disciplined next live-trading window without
forcing weak strategies into production. The target is not "more trades
at any cost"; the target is more qualified opportunities, fewer retry
storms, cleaner agent telemetry, and a safer path from research to live
probe.

## What Changed

### 1. Swarm dry-run order blocking fixed

Repository: `/Users/mymac/Desktop/poly/bot`

Problem:
- `market_maker` wrote synthetic dry-run orders as `submitted`.
- Those rows used `order_id` values like `dry_1`.
- Because they were never real CLOB orders, they could not be reconciled,
  but they still blocked future attempts on the same market.

Fix:
- Added `StateStore.clear_stale_dryrun_submitted_orders`.
- `has_active_order_for_market` now auto-clears stale `dry_%` submitted
  rows before deciding whether a market is blocked.
- Default dry-run submitted TTL is 30 seconds via
  `SWARM_DRYRUN_SUBMITTED_TTL_SECONDS`.
- Cleared existing stale dry-run rows.

Status:
- Swarm is running in `BOT_MODE=dryrun`.
- DB integrity is `ok`.
- `market_maker` now generates dry-run attempts again instead of staying
  permanently blocked.

### 2. Swarm SQLite durability improved

Problem:
- The swarm DB hit `database disk image is malformed` / `disk I/O error`
  during pending-order updates. This had already happened before and was
  likely aggravated by concurrent readers and Docker bind-mounted SQLite
  WAL files.

Fix:
- Added SQLite `timeout=30`, `PRAGMA busy_timeout=30000`, and
  `PRAGMA synchronous=NORMAL`.
- Changed the Docker healthcheck to open SQLite read-only.
- Rebuilt the active DB with `VACUUM INTO`, preserving timestamped backups.

Status:
- Current swarm DB integrity check: `ok`.
- No live orders were affected; swarm is still dry-run.

### 3. Swarm dry-run market data made realistic enough for testing

Problem:
- The dry-run mock orderbook generated a new random book on every call.
- `market_maker` fetched a book, then immediately fetched a fresh book for
  slippage protection. Because both were random, most dry-run orders were
  rejected as fake 20-30 cent price drift.

Fix:
- `_mock_orderbook` now caches the immediate book and then applies only a
  small random walk after one second.

Status:
- Dry-run slippage behavior is now measurable instead of chaotic.

### 4. Poly1 broken-market retry suppression added

Repository: `/Users/mymac/coding/poly1`

Problem:
- Main trader kept retrying markets that repeatedly failed with hard
  execution problems such as 404s, empty books, no asks, or live-price
  slippage beyond the recommended price.
- These were recorded as `failed`, polluting strategy quality metrics and
  causing retry noise.

Fix:
- Added `TradeLog.count_recent_failures_for_market`.
- Trader now pre-skips markets with repeated hard execution failures.
- Execution-gate errors such as `no asks available` and
  `live ask price ... exceeds recommended price` are recorded as
  `skipped_gate`, not strategy failures.

Status:
- Built into the `poly1:local` image.
- `poly1` main trader was recreated with the new image.
- Main trader remains `EXECUTE=false`, so this is a safe shadow fix.

### 5. OpportunityRouter added

Files:
- `agents/application/opportunity_router.py`
- `scripts/python/opportunity_router.py`
- `tests/test_opportunity_router.py`

Purpose:
- Convert scout/research rows into one of four routes:
  `live_probe`, `backtest`, `paper`, or `reject`.
- Prevent capital from being allocated to a strategy just because it
  produced an interesting idea.
- Require live committee approval, positive EV, enough liquidity, limited
  spread, and low risk before any live probe.

Current output:
- Latest scout item routes to `backtest`, not live:
  `bitcoin-up-or-down-on-may-11-2026`, strategy `mean_reversion`,
  score `0.086`, risk `0.640`.

### 6. Dashboard Router / No-Trade visibility added

Files:
- `scripts/python/dashboard.py`
- `scripts/python/db.py`

Added a `Router` tab to the Streamlit dashboard:
- Route counts: `live_probe`, `backtest`, `paper`, `reject`.
- Latest routed opportunities with score, risk, EV, market, and reasons.
- 24h "why no trade" summary for failed, gate-blocked, and deduped rows.
- 24h brain veto table grouped by agent, decision type, strategy, and
  reason.

Validation:
- Dashboard container rebuilt and restarted.
- Health endpoint returned `ok`.
- In-container dashboard data check returned:
  `routes=1`, `blockers=5`, `vetoes=4`.

### 7. EV-first router policy implemented

The router now follows the trading discipline agreed in the session:

- Do not loosen filters to force more trades.
- Treat many agents as research-only unless they pass evidence gates.
- Route capital by expected value, not raw win rate.
- Require explicit/proven edge before live probes.

EV formula:

```text
expected_value = estimated_true_probability
  - entry_price
  - slippage
  - error_margin
```

New fields on each route:
- `estimated_true_probability`
- `entry_price`
- `expected_value`
- `slippage`
- `error_margin`
- `liquidity_usd`
- `spread_cents`
- `catalyst_score`
- `historical_edge`

Live guard:
- A live probe is blocked if probability is only a router/model estimate.
- A live probe is blocked without positive historical/paper/live-probe edge.
- A live probe is blocked unless EV clears the live threshold.

The current routed opportunity remains `backtest`, not live:

```text
bitcoin-up-or-down-on-may-11-2026
strategy: mean_reversion
route: backtest
entry: 0.600
estimated probability: 0.600
EV: -0.025
reasons:
- risk_above_live_limit
- probability_is_model_estimate
- missing_historical_edge
- non_positive_ev
- requires_backtest_before_capital
```

## Validation

Poly1 container tests:

```text
python -m unittest tests.test_trader tests.test_opportunity_router \
  tests.test_research_committee tests.test_brain_journal -v

Ran 40 tests - OK
```

Swarm validation:
- `python3 -m py_compile core/client.py core/state_store.py` passed.
- `python3 -m py_compile scripts/python/db.py scripts/python/dashboard.py`
  passed.
- Manual StateStore dry-run TTL check passed.
- Container mock orderbook check passed:
  immediate book is stable; post-1s drift is tiny.
- Swarm DB integrity check from inside container: `ok`.
- Dashboard health check: `http://127.0.0.1:8050/_stcore/health` returned
  `ok`.

## Current Trading Status

Poly1:
- `EXECUTE=false` for main trader.
- `EXECUTE_BTC_DAILY=true`.
- `EXECUTE_SCALPER=true`, but current backtest evidence says scalper is
  not profitable under realistic spread/slippage. Treat it as high risk.
- `EXECUTE_MAINTAIN=true`.
- Dashboard and Grafana are running.

Swarm:
- Running in `BOT_MODE=dryrun`.
- Active agents: `market_maker`, `mean_reversion`, `nothing_happens`,
  `ai_decision`, `arbitrage`.
- `arbitrage` is observational only.
- `market_maker` is active in dry-run but still not live-approved.

## Decisions

Do not flip swarm to live yet.

Reasons:
- `market_maker` is now operational in dry-run, but not proven profitable.
- `mean_reversion` remains backtest-only and has failed prior realistic
  spread tests.
- `ai_decision` is mostly returning `SKIP`.
- `nothing_happens` has open dry-run state but no fresh live-approved
  opportunity today.
- `OpportunityRouter` did not produce a `live_probe` route.

## Next 10 Tasks

1. Convert swarm `market_maker` dry-run orders into paper fills with
   simulated exits, so the dashboard can calculate paper PnL instead of
   only showing submitted attempts.
2. Add a read-only reconciliation view for swarm pending orders, including
   dry-run auto-clears and live orders needing operator review.
3. Fix poly1 Chroma local DB permission warnings inside the trader
   container.
4. Add a scout-to-router-to-backtest pipeline command that takes today's
   candidates and automatically produces a short evidence report.
5. Keep btc_daily live, but cap size until at least 30 live closed trades
   confirm the backtest edge.
6. Move scalper back to paper-only unless a fresh backtest with real
   spread/slippage clears the win-rate and PnL gate.
7. Add a daily review job that compares temporary profit opportunities
   above +5% against actual exits, so missed-profit behavior is measured.
8. Only allow live capital allocation after `OpportunityRouter` emits
   `live_probe` and the exit agent has a matching exit thesis.
