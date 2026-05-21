# Strategy Sweeper - 2026-05-21

## Purpose

This is the lightweight version of the Freqtrade lesson: do not add a second
trading framework, but do add a disciplined parameter-sweep layer.

The sweeper answers:

- Which entry thresholds would have worked on our shadow decisions?
- Which TP/SL/horizon combinations would have worked?
- Which agent, strategy family, regime, or signal source is responsible for the result?

It never sends orders and never changes runtime control.

## Command

```bash
python scripts/strategy_sensitivity_sweep.py \
  --db data/trade_log.db \
  --configs 1000 \
  --min-trades 20 \
  --group-by strategy_family \
  --out data/reports/strategy_sensitivity_family_latest.json
```

Useful `--group-by` values:

- `agent`
- `strategy`
- `strategy_family`
- `regime`
- `signal_source`

## What It Sweeps

The default grid tests combinations of:

- `min_score`
- `min_raw_ev`
- `min_net_ev`
- `max_entry_price`
- markout horizon: 1, 3, 5, 15 minutes
- take-profit percent
- stop-loss percent

Default limit is 1000 configurations.

## Output

The output includes:

- `top`: best configs, even when sample size is too small
- `best_viable`: configs with enough samples and positive normalized PnL
- `top_groups`: which segment showed up in the best configs

For example, after the RegimeRouter change, `top_groups` can tell us:

- `trend_following` worked only in `trending`
- `mean_reversion` failed in `trending`
- `scanner_executor` found opportunities but a specific source underperformed

## Current Limitation

The sweeper depends on matured shadow markouts in `decision_journal`:

- `outcome_1m_json`
- `outcome_3m_json`
- `outcome_5m_json`
- `outcome_15m_json`
- `outcome_60m_json`

If those fields are empty, it will correctly return no viable configs. That is
a data-readiness issue, not a sweeper failure.

## Live Rule

No strategy should receive more live allocation just because a single sweep
looked good. Use the sweeper as one input together with:

- 30/60/90 day backtest splits
- shadow markout sample size
- provider scorecard
- regime/family compatibility
- live slippage and spread checks
