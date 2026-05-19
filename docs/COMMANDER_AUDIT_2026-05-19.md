# Commander Audit - 2026-05-19

Purpose: verify that the fast, brain-gated trading principles are reflected in
code, docs, local tests, and the live SSH server.

## Local Code Audit

Passes:

- Canonical policy exists in `agents/application/trading_policy.py`.
- Stop-loss default is `3%`.
- Fast profit-taking starts at `5%` when momentum does not justify holding.
- Hard profit cap is `25%`.
- Max hold default is `6h` as a hard ceiling only; the strategy preference is
  fastest profitable exit unless the brain records strong hold evidence.
- Trader/scanner cadence defaults to `60s`.
- Position-manager cadence defaults to `60s`, including minute-by-minute
  brain/LLM exit revalidation.
- Telegram dashboard cadence defaults to `1h`.
- Trading rate cap is `100` trades/hour.
- Agent allocation cap is `50%` of wallet capital.
- Live preflight requires `POLY1_REQUIRE_BRAIN_APPROVAL=true` and
  `MARKET_BRAIN_ENABLED=true`.
- `btc_5min` is now represented in `deploy/runtime_policy.json`.
- `scripts/trading_stability_preflight.py` checks `btc_5min_open`,
  `EXECUTE_BTC_5MIN`, and `BTC_5MIN_RESERVE_USDC`.
- `tests/test_trading_policy_contract.py` locks these values so drift is caught.

Local verification:

```text
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
Ran 451 tests in 10.288s
OK
```

Compile verification:

```text
.venv/bin/python -m py_compile $(rg --files agents scripts deploy tests -g '*.py')
OK
```

Local blockers:

- `scripts/trading_stability_preflight.py --mode freeze` blocks because
  `settlement_reconciliation` contains 2 `active_unmanaged` rows.
- Local runtime is correctly in `freeze` with `data/HALT` present.
- Local goal loop is still open; `trader` has negative PnL in the 24h window.

The unmanaged local tokens requiring operator review:

| token prefix | market | action | cost | recoverable |
| --- | --- | --- | ---: | ---: |
| `285576146480...` | `691547` | `halt_and_restore_exit_manager` | `$1.53` | `$1.64` |
| `107587773577...` | `610236` | `halt_and_restore_exit_manager` | `$1.51` | `$1.29` |

## SSH Server Audit

Server:

- Host: `trader@83.229.82.193`
- Hostname: `hermesagent01`
- Project path: `/srv/poly1`
- Git: `main...origin/main`, HEAD `5486615`
- Local code is ahead of the server and not deployed.

Container health:

- All listed docker compose services were `Up` and `healthy`.
- `docker compose config --quiet` passed.
- Recent critical log grep over the last 10 minutes returned no critical
  runtime errors.

Runtime:

- `runtime_control.json` inside the container reports `mode=freeze`.
- `data/HALT` exists.
- `allowed_live_agents=[]`.
- The running trader container process environment is safe:
  `EXECUTE=false`, reserves `0`, `TRADING_SUPERVISOR_ENFORCE_HALT=true`,
  and `RUNTIME_CONFIG_HASH=73767002dca5e58c`.

Server blockers:

- `trading_stability_preflight.py --mode freeze` inside the container blocks
  because `/app/deploy/.env.runtime` baked/mounted in the container is stale:
  it shows `EXECUTE="true"`, agent reserves `2`, supervisor enforcement false,
  and hash `58d77446bca093b4`.
- Host `deploy/.env.runtime` is cleaner, but the container file differs from
  host state. This is deployment drift. A recreate/build without fixing the
  image/file contract can revive stale trading settings.
- Server host has no `.venv`; host-side tests fail from missing dependencies.
- Container tests are not green on the deployed code/image: 314 tests ran with
  21 failures and 11 errors. Failures include runtime-freeze interference,
  older near_resolution test drift, older position_manager behavior, and quota
  fallback noise.
- Server goal loop is not profitable:
  - `trader`: 36 entries, 35 closed, PnL `-$2.8984`, win-rate `0.0%`
  - `near_resolution`: 5 entries, 1 closed, PnL `-$0.5926`, win-rate `0.0%`

## Verdict

Not ready for live entries.

The correct posture is still freeze/no new live entries until:

1. Local `active_unmanaged` settlement rows are resolved or explicitly waived.
2. The server is updated to the audited policy code.
3. The server image/container file drift is eliminated.
4. Server tests pass in the same environment that runs the bots.
5. `trading_stability_preflight.py --mode freeze` passes on the server.
6. A single-agent live probe is generated via `scripts/runtime_control.py` and
   passes `--mode live` preflight before starting that agent profile.
