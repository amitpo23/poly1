# RL Reward Lab - 2026-05-21

## Purpose

The TensorTrade / reinforcement-learning idea is useful for research, not for
direct live execution.  The safe version for poly1 is an offline reward lab:
turn our existing shadow decisions and markouts into training examples, then
use that dataset to challenge MetaBrain and the entry agents.

This layer never places orders.

## What Was Added

- `agents/application/rl_reward_lab.py`
- `scripts/rl_reward_dataset.py`
- `tests/test_rl_reward_lab.py`
- `config/agent_registry.json` entry: `rl_reward_lab`

## Data Source

The lab reads:

- `decision_journal`
- `brain_decisions`
- markout columns:
  - `outcome_1m_json`
  - `outcome_3m_json`
  - `outcome_5m_json`
  - `outcome_15m_json`
  - `outcome_60m_json`

Run markout refresh first:

```bash
python scripts/update_shadow_markouts.py --db data/trade_log.db --horizons 1,3,5,15 --limit 500
```

Then export the RL dataset:

```bash
python scripts/rl_reward_dataset.py \
  --db data/trade_log.db \
  --out data/rl_reward_dataset.jsonl \
  --summary-out data/rl_reward_summary.json
```

## Reward Function

The reward starts from realized shadow markout and then adjusts for the costs
we actually care about:

- round-trip cost buffer,
- spread penalty,
- thin exit-depth penalty,
- repeated entry on the same market,
- slow hold penalty,
- stop-loss penalty,
- take-profit bonus,
- bounded raw/net EV bonus.

This is intentionally harsher than a naive PnL curve.  A policy that only looks
good before spread, liquidity, and repeat-entry costs is not useful for live.

## Output Row Shape

Each JSONL row contains:

- `observation`: compact state available at decision time,
- `action`: `enter`, `quote`, or `skip`,
- `reward`: cost-adjusted realized reward,
- `reward_components`: full reward breakdown,
- `target`: selected markout horizon and raw markout payload.

## Promotion Rule

RL output can become a MetaBrain advisor only after:

- at least 500 labeled rows,
- multiple market regimes represented,
- positive walk-forward reward,
- no improvement that disappears after spread/depth/reentry penalties,
- shadow-only agreement test against MetaBrain for at least one full session.

Until then, it is an offline critic: it tells us which agents and features look
predictive, not what to buy live.
