# Live Stabilization Runbook - 2026-05-12

Purpose: stabilize the local trading system before any Azure migration or
larger live-capital run. This runbook is the current handoff for agents and
operators.

## Current Mode

The system is in **Phase 1 stability freeze**.

`scripts/runtime_control.py freeze` is the source of truth for this phase. It
writes:

- `deploy/.env.runtime` - secret-free Docker env overrides
- `data/runtime_control.json` - shared control file read by `RiskGate`
- `data/HALT` - the physical brake

`data/HALT` must exist in this phase. `RiskGate` also reads
`data/runtime_control.json` on every entry check, so a stale container with old
environment variables is still blocked unless its `RUNTIME_CONFIG_HASH` matches
the current control file and its `RUNTIME_AGENT` is explicitly allowed.

Allowed:

- `position_manager` live exit-only (`EXECUTE_MAINTAIN=true`)
- `trading_supervisor` with halt enforcement
- `settlement_reconciler`
- dashboards and read-only signal producers
- paper/shadow analysis

Blocked:

- new live entries from trader/scalper/btc_daily/near_resolution/news_shock/wallet_follow
- allocator sync enforcement
- reserve allocation to entry agents
- cloud migration as a substitute for local stability

## Local Safety Defaults

As of 2026-05-19, the canonical live trading policy is defined in
`agents/application/trading_policy.py` and summarized in
`docs/AGENT_MATRIX_2026-05-19.md`:

- stop-loss `3%`
- fast profit-taking can start at `5%`
- hard profit cap `25%`
- max hold `6h`
- scanner/trader cadence `60s`
- position-manager cadence `15s`
- Telegram dashboard cadence `1h`

Do not edit `deploy/.env.runtime` by hand. Regenerate it with:

```bash
.venv/bin/python scripts/runtime_control.py freeze \
  --note "stability freeze before live probe"
```

Docker Compose loads `.env` first and `deploy/.env.runtime` second, so the
runtime file overrides private local defaults without committing secrets.

The generated freeze-mode runtime sets:

- `EXECUTE=false`
- `EXECUTE_SCALPER=false`
- `EXECUTE_BTC_DAILY=false`
- `EXECUTE_NEAR_RESOLUTION=false`
- `EXECUTE_NEWS_SHOCK=false`
- `EXECUTE_WALLET_FOLLOW=false`
- `ALLOC_SYNC_ENFORCE=false`
- entry-agent reserves set to `0`
- `ALLOCATOR_EXPLORATION_USDC=0`
- `TRADING_SUPERVISOR_ENFORCE_HALT=true`
- `TRADING_SUPERVISOR_EVAL_GRACE_SEC=180`
- `TRADING_SUPERVISOR_STALE_HEARTBEAT_SEC=180`

The tracked, secret-free reference is `deploy/env.stability.freeze`, but the
active runtime source is `deploy/.env.runtime` plus
`data/runtime_control.json`. Do not commit private `.env` files.

Do not re-enable live entries until the gates below pass.

## Preflight Command

Run before any live probe:

```bash
.venv/bin/python scripts/trading_stability_preflight.py --mode freeze
```

Expected freeze output:

```text
trading_stability_preflight[freeze]: ok
- OK entry_agents_frozen: live flags/reserves disabled
- OK exit_manager_live: EXECUTE_MAINTAIN=true
- OK supervisor_enforces_halt: TRADING_SUPERVISOR_ENFORCE_HALT=true
- OK halt_file_present: HALT present at ...
- OK trade_log_db_exists: ...
- OK open_positions_accounted: open=0
- OK settlement_requires_no_action: no critical settlement rows
```

If the script returns `blocked`, do not trade. Fix the listed issue first.

For a live probe, choose exactly one approved entry agent and generate the
runtime profile:

```bash
.venv/bin/python scripts/runtime_control.py live-probe \
  --agent btc_daily \
  --budget 5 \
  --note "approved live probe"
```

This writes the live-probe runtime but leaves `data/HALT` in place. After human
approval, arm the probe:

```bash
.venv/bin/python scripts/runtime_control.py live-probe \
  --agent btc_daily \
  --budget 5 \
  --note "approved live probe" \
  --arm
```

Then recreate containers from the approved env and run:

```bash
.venv/bin/python scripts/trading_stability_preflight.py --mode live
```

`--mode live` must pass before any entry-agent profile is started.

## Why This Exists

The BTC May 12 failure was not missing stop-loss logic. It was an ownership
failure: a re-entry on the same token was skipped because old terminal rows made
the position look already closed. The fix now checks terminal evidence after the
latest open trade id, and the supervisor/reconciler layers are responsible for
catching unmanaged positions.

The stabilization goal is to prove that every live position has:

- a fresh `position_mark`
- a fresh `position_manager` exit `brain_decision`
- no critical settlement row
- an active exit manager heartbeat
- a supervisor that can write `HALT`

## Live Probe Gate

A live probe is allowed only after all are true:

- preflight passes
- focused tests pass
- dependency environment is complete
- no open unmanaged positions
- no `redeemable`, `active_unmanaged`, or `reconcile_error` rows
- dashboard shows supervisor/reconciler/position_manager healthy
- operator chooses exactly one approved entry agent and one budget

Initial live probe limit:

- max capital: `$5-$10`
- max concurrent positions: `1-2`
- take profit: `+5%`
- trailing after profit: `2%` peak drawdown after `+5%`
- stop loss: `-3%`
- min exit notional: `$1`

## Next Implementation Work

1. Complete the Python environment so the full test suite runs locally.
2. Keep freeze mode on until the full exit/supervisor/reconciler path has one
   clean 24h paper/shadow run.
3. Generate a 24h shadow report: candidates, rejected/paper/live_probe routes,
   hypothetical exits, and missed-profit analysis.
4. Prepare DB migration design from SQLite to Postgres before Azure.
5. Move secrets to Key Vault only during the Azure phase.

## Dependency Gate Status

Local `.venv` currently uses Python 3.9.6. Installing `requirements.txt` from
public PyPI is still blocked because the Polymarket V2 package
`py_clob_client_v2==1.0.1rc1` is not available from the default index. Earlier
Docker verification notes show the package import working inside the trading
container, so full-suite verification should run in the production container or
against the private/package source that supplies the V2 client.

Do not treat local full-suite failures caused by missing `py_clob_client_v2` as
strategy failures. Do treat them as a deployment-readiness blocker until the
runtime source is explicit and reproducible.

## Runtime Rule

Changing `.env` is not a runtime change, and direct `.env` edits are not the
approved way to change trading mode. Use `scripts/runtime_control.py`, recreate
the affected services, then verify the live env inside the container:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker compose \
  --profile positions up -d --force-recreate \
  position_manager trading-supervisor settlement-reconciler

/Applications/Docker.app/Contents/Resources/bin/docker exec \
  poly1-trading-supervisor env | rg 'RUNTIME|EXECUTE|RESERVE|TRADING_SUPERVISOR|MAINTAIN'
```

No entry-agent container should be running during freeze.
