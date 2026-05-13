# Agent Handoff - Runtime Control and Deployment Direction - 2026-05-13

Audience: future agents/operators entering the poly1 workspace.

## Current State

The trading system is in **stability freeze**.

This is intentional. Do not start live entry agents directly from `.env`,
Docker Compose profiles, or old shell commands.

Current safety posture:

- live entry agents are stopped;
- `data/HALT` exists;
- `deploy/.env.runtime` is generated and active;
- `data/runtime_control.json` is active;
- `RiskGate` checks the runtime control file before every entry decision;
- `position_manager`, `trading_supervisor`, and `settlement_reconciler` are the
  only live trading-stack services that should run during freeze.

Latest committed runtime stabilization work:

```text
531c03d chore: add runtime control freeze guard
```

## Why This Matters

A stale live container previously kept old environment variables after `.env`
was changed. That stale container opened a BTC daily trade even though the
operator believed live entries were frozen.

The fix is not "remember to recreate containers." The fix is a control plane
that makes stale containers fail closed.

## Source of Truth

Use this command surface only:

```bash
.venv/bin/python scripts/runtime_control.py status
.venv/bin/python scripts/runtime_control.py freeze --note "reason"
.venv/bin/python scripts/runtime_control.py live-probe --agent btc_daily --budget 5 --note "reason"
```

`runtime_control.py` writes:

- `deploy/.env.runtime` - generated Docker env overrides, no secrets;
- `data/runtime_control.json` - shared runtime control file read by `RiskGate`;
- `data/HALT` - physical brake in freeze mode.

Do not edit `deploy/.env.runtime` by hand.

Docker Compose loads:

1. `.env`
2. `deploy/.env.runtime`

The second file overrides the first. This lets private secrets stay local while
runtime mode remains explicit and reviewable.

## Freeze Rules

During freeze:

- `RUNTIME_MODE=freeze`
- all entry execute flags are false;
- all entry reserves are zero;
- `EXECUTE_MAINTAIN=true`;
- `TRADING_SUPERVISOR_ENFORCE_HALT=true`;
- `data/HALT` must exist;
- `allowed_live_agents=[]`;
- no entry-agent container should be running.

Expected validation:

```bash
.venv/bin/python scripts/trading_stability_preflight.py --mode freeze
```

Expected result:

```text
trading_stability_preflight[freeze]: ok
```

## Live Probe Rules

A live probe is not "turn trading on." It is a scoped experiment with exactly
one approved entry agent and a small budget.

Generate the live-probe profile without arming it:

```bash
.venv/bin/python scripts/runtime_control.py live-probe \
  --agent btc_daily \
  --budget 5 \
  --note "approved live probe"
```

Arm only after explicit human approval:

```bash
.venv/bin/python scripts/runtime_control.py live-probe \
  --agent btc_daily \
  --budget 5 \
  --note "approved live probe" \
  --arm
```

Then recreate the exact services needed for that probe and run:

```bash
.venv/bin/python scripts/trading_stability_preflight.py --mode live
```

If preflight is blocked, do not trade.

## Runtime Guard Contract

`RiskGate` blocks entries unless all are true:

- runtime mode is trade-enabled: `paper`, `live_probe`, or `live`;
- `RUNTIME_AGENT` is listed in `allowed_live_agents`;
- container `RUNTIME_CONFIG_HASH` matches `data/runtime_control.json`;
- regular risk checks also pass.

This protects against:

- stale containers;
- accidental `.env` edits;
- multiple entry agents being activated at once;
- an agent opening risk outside the approved probe.

## Deployment Recommendation

Do **not** use GitHub Actions as the trading engine.

GitHub Actions is good for:

- tests;
- lint/type checks;
- Docker image builds;
- preflight validation;
- deploying/restarting an external runtime;
- producing daily reports.

GitHub Actions is not appropriate for:

- 24/7 live trading loops;
- long-running position management;
- stop-loss enforcement;
- health-supervised daemon operation;
- anything that must react continuously without CI job timeouts.

Recommended architecture:

```text
GitHub
  -> CI: tests + build Docker image + preflight
  -> Deploy: push image/restart service

External runtime
  -> one Docker Compose stack or Azure Container Apps/VM
  -> mounted persistent data volume / managed DB
  -> runtime_control.py is the only trading-mode switch
  -> supervisor + reconciler + position manager always-on
  -> entry agents only started through approved live probes
```

The trading engine should be a self-contained Docker runtime, preferably on a
stable VPS or Azure environment, but only after the local runtime contract is
stable. Moving broken control logic to cloud only makes failures harder to see.

## Next Safe Step

Before migration:

1. Keep freeze active.
2. Run a 24h shadow/paper cycle.
3. Confirm `position_manager`, `trading_supervisor`, and
   `settlement_reconciler` stay healthy.
4. Run one approved live probe with one agent and a small budget.
5. Only then move the same Docker runtime to Azure/VPS.

The target is not "more infrastructure." The target is one reliable trading
control contract that behaves the same locally and remotely.
