# Agent Handoff: Dashboard, Swarm, and $5 Sizing

Date: 2026-05-05

This handoff records the live operational state after connecting the dashboards,
the local swarm bot, and the $5-per-trade sizing pass.

## Current Runtime

Docker services verified running:

- `poly1` - healthy
- `poly1-scalper` - healthy
- `poly1-grafana` - running on `http://localhost:3000`
- `poly1-dashboard-1` - healthy on `http://localhost:8050`
- `polymarket-swarm` - healthy, separate project under `~/Desktop/poly/bot`

Dashboards:

- Grafana unified dashboard: `http://localhost:3000`
- Streamlit dashboard: `http://localhost:8050`
- Temporary Cloudflare Grafana URL:
  `https://certified-comparisons-efficiently-sites.trycloudflare.com`

The Cloudflare URL is a quick tunnel. It only works while the `cloudflared`
process is still running. Do not assume it is permanent.

## Trading State

`poly1` main trader:

- `EXECUTE=true`
- `STARTING_BALANCE_USDC=80.0`
- `MAX_POSITION_FRACTION=0.0625`
- This targets about `$5` per trade.
- It is live-capable but currently blocked by the risk gate.
- Latest observed block:
  `drawdown 12.27% above max_daily_loss_pct 10.00%`

`poly1-scalper`:

- `EXECUTE_SCALPER=false`
- `SCALP_LEG_USDC=5.0`
- It is running in shadow mode only.
- It should not place real orders in this state.

`polymarket-swarm`:

- Running in dry-run mode.
- Logs show `[DRY] BUY` / `[DRY] SELL`.
- Latest observed allocation: `4` open simulated positions, `$20.00` at risk,
  which is consistent with about `$5` per position.
- Do not switch swarm live without a separate preflight review.

## What Changed

The local `.env` sizing block was updated:

```env
EXECUTE="true"
MAX_POSITION_FRACTION="0.0625"
STARTING_BALANCE_USDC="80.0"
MAX_TRADES_PER_HOUR="2"
EXECUTE_SCALPER="false"
SCALP_LEG_USDC="5.0"
```

After the change, `poly1` and `poly1-scalper` were recreated with Docker
Compose. Logs confirmed:

- `poly1`: `max_position_fraction=0.0625`
- `poly1-scalper`: `ScalperDaemon: starting (execute=False)`

## Important Safety Notes

- Do not bypass the risk gate casually. It is currently blocking because the
  configured daily loss threshold is already exceeded.
- The cash-balance drawdown check may count deployed capital as drawdown because
  it does not mark open positions to market. Review positions and wallet value
  before changing `MAX_DAILY_LOSS_PCT`.
- Do not enable scalper or swarm live at the same time as another new live
  strategy. Bring up one live strategy at a time and watch fills, slippage,
  failed orders, and P&L.
- `.env` contains live secrets. Do not print or paste it into chat/logs.

## Useful Commands

Check containers:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

Tail main trader:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker logs --tail 120 poly1
```

Tail scalper:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker logs --tail 120 poly1-scalper
```

Tail swarm:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker logs --tail 120 polymarket-swarm
```

Restart trader and scalper after env changes:

```bash
PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH" \
/Applications/Docker.app/Contents/Resources/bin/docker compose \
  --profile scalper up -d --force-recreate trader scalper
```

