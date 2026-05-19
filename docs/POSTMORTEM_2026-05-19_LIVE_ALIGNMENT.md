# Postmortem 2026-05-19 — Live Alignment Before Resuming

## Verdict

Do not resume live trading until the system passes freeze/live preflight and the
operator explicitly switches from `freeze` to a bounded live probe.

## What Happened

- Live trading ran on the server while the server was behind local source.
- The server was at `39eaaa3`; local source had advanced through MetaBrain,
  expert routing, spread EV, Kelly sizing, attribution, and wallet proof fixes.
- BTC 5m activity increased after lowering thresholds, but the resulting
  quality was negative: 16 take-profits vs 22 stop-losses.
- Realized TP/SL result for the day was approximately `-2.41 USDC` real PnL.
- Several old `settlement_reconciliation` rows stayed active after their
  positions had already closed, blocking preflight.

## Root Causes

- Code drift: server containers were not running the latest committed logic.
- Calibration gap: server schema lacked `brain_decisions.signal_source` and
  wallet external proof fields, so provider-level learning could not run.
- BTC 5m overtrading: `0.52` confidence, one-signal consensus, zero cooldown,
  and straddle mode created too many weak entries.
- Stale settlement state: `active_unmanaged` rows were not cleared when no
  open journal position remained.

## Fixes Applied

- Server synced to latest `main`.
- Docker image rebuilt and all live services restarted under `freeze`.
- Migration fixed so `signal_source` index is created only after the column
  exists.
- Settlement reconciler now clears stale active rows when no open journal
  position remains.
- Runtime control defaults were made safer:
  - BTC 5m normal confidence: `0.60`
  - BTC 5m consensus: `2`
  - BTC 5m cooldown: `300s`
  - BTC 5m straddle: disabled by default
  - External conviction / near-resolution min confidence: `0.65`
  - `52%` is reserved for proven solo experts, not ordinary consensus.

## Backtest Notes

Server backtest over the latest 24h external conviction JSONL files showed:

- `public_news`: 89 matched losses, 0 wins, about `-2.57 USDC`
- `aggregator`: 42 matched losses, 0 wins, about `-1.86 USDC`
- `manifold_divergence`: 13 matched losses, 0 wins, about `-0.72 USDC`

These providers should remain evidence inputs only. They should not receive
solo authority until they produce measured positive reliability.

## Current Readiness Rule

Before any live probe:

1. `runtime_control.json` must be `freeze` until the operator starts a probe.
2. `data/HALT` must exist before the probe and be removed only by
   `runtime_control.py live-probe/live-hour`.
3. `scripts/trading_stability_preflight.py --mode freeze` must pass.
4. A live preflight must pass after switching to a bounded live probe.
5. Start with limited exposure and watch Telegram trade alerts plus hourly PnL.
