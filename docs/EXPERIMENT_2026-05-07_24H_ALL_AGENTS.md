# 24h All-Agent Trading Experiment - 2026-05-07

Goal: produce comparable 24-hour results from every active trading strategy
without losing attribution between agents.

## Start

- Start time: `2026-05-07 11:12 UTC`
- Wallet balance at preflight: `62.922603` USDC
- CLOB open orders at preflight: `0`
- Dashboard: `http://localhost:8050`
- Grafana: `http://localhost:3000`

## Capital Posture

Only the scalper is allowed to risk live capital during the initial run:

- `scalper`: live, capped by `SCALPER_RESERVE_USDC=20.0`
- `position_manager`: live exit-only
- `trader`: shadow mode, `EXECUTE=false`
- `btc_daily`: shadow mode, `EXECUTE_BTC_DAILY=false`
- `swarm`: dryrun mode via explicit `BOT_MODE=dryrun` override

`SHADOW_IGNORE_RISK_GATE=true` is enabled so shadow-only agents keep producing
paper decisions even while the live risk gate blocks entries. This flag is
ignored by live paths; it does not authorize real orders.

## Active Agents

`poly1`

- Main `trader`: broad market RAG/LLM scanner, shadow only.
- `btc_daily`: BTC daily up/down mean-reversion/fade, shadow only.
- `scalper`: crypto 15m reversal scalper, live small.
- `position_manager`: TP/trailing/SL/timeout exit manager, live exit-only.

`swarm`

- `market_maker`: dryrun.
- `mean_reversion`: dryrun.
- `nothing_happens`: dryrun.
- `ai_decision`: dryrun, currently skip-only if no Anthropic key.
- `arbitrage`: observational only, `$0` allocation.

## First Checkpoint

At `2026-05-07 11:13 UTC`:

- `trader` completed a shadow cycle.
- `trader` evaluated 297 tradeable events, mapped 11 markets, filtered to 4,
  then skipped 3 top markets because local journal says existing filled
  positions already exist.
- `btc_daily` started in shadow and continued past risk-gate block; no BTC
  trigger had fired yet.
- `swarm market_maker` created one dryrun submitted order for Alphabet.
- `swarm ai_decision` skipped because no `ANTHROPIC_API_KEY` is configured in
  the swarm environment.
- CLOB open orders remained `0`.

## Reporting

Use the read-only scoreboard:

```bash
python3 scripts/python/strategy_report.py --hours 24 --limit 50
```

Use the read-only capital allocator:

```bash
python3 scripts/python/capital_allocator.py --hours 24 --budget 20
```

An hourly/periodic local report can be appended to:

```text
data/strategy_report_24h.md
data/capital_allocator_24h.md
```

Evaluation after 24h should answer:

1. Which agents produced decisions?
2. Which agents produced simulated or live entries?
3. Which agents were blocked, and why?
4. Did live scalper entries close by TP/trailing/SL/timeout?
5. Did any strategy create avoidable noise, stale state, or duplicate exposure?

## CapitalAllocator

`CapitalAllocator` is intentionally advisory-only in this experiment. It reads
local historical data from:

- `data/trade_log.db`
- `data/scalper_pairs`
- `data/brain_decisions`
- `~/Desktop/poly/bot/data/swarm.db`

It scores each agent using:

- decisions and entries;
- exit evidence;
- realized/paper PnL where available;
- stale local state;
- errors;
- veto-only behavior.
- live market context:
  - Coinbase spot prices for BTC/ETH/SOL/XRP;
  - Gamma crypto-market liquidity and 24h volume;
  - fresh `brain_decisions` approvals;
  - fresh `news_signals` counts.

It refuses to allocate live budget when an agent has stale state, recent errors,
only veto/dedupe decisions, or no recent signal. `position_manager` is treated
as exit-only and receives no entry budget.

Important: market intelligence can increase an agent's score, but it does not
override operational blockers. For example, crypto market context currently
boosts `scalper`/`btc_daily` scores, but `scalper` still receives `$0` extra
allocation while `reconcile_needed` rows exist.

## Rules During The Run

- Do not enable `trader` live before reviewing its shadow outputs.
- Do not enable `btc_daily` live before at least one full shadow entry/exit
  path is observed.
- Do not enable swarm live while it has stale local `submitted` orders.
- Keep CLOB open orders target at `0` unless deliberately testing market-maker
  live quoting.
- Stop entry agents immediately if any unexpected live order appears.
