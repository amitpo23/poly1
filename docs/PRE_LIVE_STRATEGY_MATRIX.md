# Pre-Live Strategy Matrix

Updated: 2026-05-21

This document is the operator-facing map for strategy QA before any live
trading.  The canonical machine-readable source is
`agents/application/strategy_catalog.py`.

## Rule

No strategy receives live capital just because a signal exists.  A strategy
must have:

- a catalog entry,
- a backtest or explicit `no_backtest_harness` blocker,
- 30/60/90 day evidence where possible,
- shadow markouts for 1/3/5/15 minute horizons,
- a live eligibility verdict from `scripts/pre_live_strategy_matrix.py`.

## Families Covered

- Trend following: BTC daily momentum, crypto 5m directional.
- Mean reversion: BTC daily fade, climax-volume reversal feature.
- Market making: crypto 5m market-maker shadow.
- Market microstructure: scalper spread-edge.
- Statistical arbitrage: cross-venue arb guard.
- Volatility relative value: equity/options fair value.
- News/sentiment/event driven: external conviction providers.
- Event-driven relative value: sports cheap-hold market sweep candidate.
- Machine learning: offline RL reward lab.

## Current Live Policy

Default state is **shadow or research only**.  A strategy is live-eligible only
if every required window passes its gate and no hard blocker remains.  Known
hard blockers include:

- recent negative backtest,
- insufficient split-window sample size,
- missing 30/60/90 harness,
- missing shadow markouts,
- unresolved lookahead-bias audit,
- external provider scorecard below gate.

## Commands

Print the catalog:

```bash
python scripts/strategy_catalog.py --json
```

Build a matrix from an existing backtest output directory:

```bash
python scripts/pre_live_strategy_matrix.py \
  --input-dir data/backtests/<run-id> \
  --out data/backtests/<run-id>/strategy_matrix.json
```

Run the normal shadow scorecard after a paper window:

```bash
python scripts/update_shadow_markouts.py \
  --db data/trade_log.db \
  --horizons 1,3,5,15 \
  --limit 1000

python scripts/strategy_scorecard.py \
  --db data/trade_log.db \
  --out data/strategy_scorecard.json
```

## Important Interpretation Notes

`$100/day` is treated as daily maximum budget, not `$100/trade`.  A strategy
that creates many entries in a day must be normalized to the daily budget;
otherwise the test rewards overtrading with fake capital.

Market-sweep results, especially sports cheap-hold, are research candidates
until they pass a lookahead-bias audit.  They may be real, but they are not
live-approved by raw PnL alone.

RL/TensorTrade-style work is offline only.  It may rank or veto future actions
after validation, but it must not directly submit live orders.
