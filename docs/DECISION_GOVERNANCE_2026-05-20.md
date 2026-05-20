# Decision Governance - 2026-05-20

This document defines the post-audit architecture for turning many agents into
one controlled trading system.

## Principle

No live entry is allowed because "many agents averaged to a decent score".
The system uses an investment-committee model:

1. Signal agents produce structured `SignalEnvelope` evidence.
2. `MetaArbiter` decides by anchor, veto, or consensus.
3. `RiskGate` sizes and blocks based on capital and runtime state.
4. `scanner_executor` executes only after live orderbook and EV re-checks.
5. `position_manager` owns exits.
6. `strategy_scorecard` learns which agents deserve promotion.

Missing or silent agents do not dilute a strong signal. A missing answer is
ignored, not counted as `0.5`.

## New Files

- `config/agent_registry.json` - single source of truth for agent roles,
  modes, required inputs, anchor rules, veto rules, and promotion criteria.
- `agents/application/agent_registry.py` - loader and validator.
- `agents/application/signal_contract.py` - canonical signal envelope.
- `agents/application/meta_arbiter.py` - deterministic anchor/veto/consensus
  arbiter.
- `agents/application/strategy_scorecard.py` - scorecard from
  `decision_journal` markouts.
- `scripts/validate_agent_registry.py` - morning QA registry validation.
- `scripts/strategy_scorecard.py` - writes `data/strategy_scorecard.json`.

## Anchor Rule

An agent can lead a trade by itself only when:

- it is registered as anchor-capable,
- the signal sets `anchor=true`,
- confidence clears the agent-specific threshold,
- special quality requirements pass, such as wallet win-rate/profit,
- net EV after round-trip costs is positive,
- an exit plan exists.

Examples of valid anchor candidates:

- whale wallet with proven 30-day win-rate and large realized profit,
- Alpaca/TradingView/options fair-value signal with measurable dislocation,
- crypto exchange tape on 5m markets when fair probability materially exceeds
  maker bid,
- fast news shock with clear market mapping and low latency.

## Veto Rule

Any strong veto blocks the entry:

- stale signal,
- unregistered agent,
- explicit agent veto,
- missing exit plan,
- negative EV,
- risk gate block,
- stale runtime or HALT.

## Consensus Rule

If no anchor exists, a trade can still pass through consensus:

- at least two active directional signals agree,
- their confidence-weighted probability beats price,
- net EV clears the configured threshold,
- no veto exists.

## Promotion Rule

Shadow-only strategies remain shadow until they prove:

- enough decisions,
- enough actual candidates,
- positive markout after spread/fees,
- no single-market concentration,
- no repeated same-market entry loop,
- clean preflight,
- Telegram buy/sell/hourly PnL reporting works,
- position manager and supervisor are running.

## Morning QA Commands

```bash
python scripts/validate_agent_registry.py --json
python scripts/strategy_scorecard.py --db ./data/trade_log.db --out ./data/strategy_scorecard.json
python scripts/update_shadow_markouts.py --horizons 1,3,5,15 --limit 500
python scripts/trading_stability_preflight.py --mode freeze
```

## Integration Plan

The new infrastructure is deliberately additive tonight. Tomorrow's live probe
should still start controlled. The next code step is to adapt each signal
producer to emit `SignalEnvelope` rows in addition to its current legacy JSON.
Once enough agents emit envelopes, `MetaBrain.synthesize()` can delegate its
final anchor/veto choice to `MetaArbiter`.
