# Agent Handoff: Polymarket Live Trading

Date: 2026-05-04

This document records the current working state for future agents. It is meant
to survive context loss and prevent repeated rediscovery.

## Executive Summary

The Polymarket bot is live and working against CLOB v2 using deposit-wallet
mode. The migration from legacy proxy flow to deposit-wallet flow is complete.
The bot has executed real live orders and Polymarket CLOB confirms those orders
as matched.

Release checkpoint:

- `docs/RELEASE_NOTES_2026-05-04_CLOB_V2.md`

Overnight and multi-week operating plan:

- `docs/OVERNIGHT_AND_MULTI_WEEK_OPERATION.md`

Trading log and journal:

- `docs/TRADING_LOG_AND_JOURNAL.md`

Current mode is conservative live trading:

```env
EXECUTE="true"
MAX_POSITION_FRACTION="0.025"
STARTING_BALANCE_USDC="80.0"
MAX_TRADES_PER_HOUR="2"
POLYMARKET_SIGNATURE_TYPE="3"
```

Do not increase size or loosen rate limits without an explicit user request.

Morning update, 2026-05-05:

- Container `poly1` is healthy.
- Authenticated CLOB balance is `70.185044`.
- Authenticated open orders are `[]`.
- Total live bot fills recorded locally: `5`.
- Risk gate is blocking new entries because the cash-balance drawdown check is
  above `MAX_DAILY_LOSS_PCT=10%`.
- This is a conservative pause, not a crash. The drawdown check is cash-based
  and does not mark open positions to market.

Swarm sister-bot update, 2026-05-05 10:20 IDT:

- Sister project path: `~/Desktop/poly/bot`.
- Symptom: swarm appeared down; monitor showed stale logs from
  `2026-05-02 20:25:00` IDT and no active `main.py start` process.
- Immediate cause: the prior foreground dry-run session had been stopped
  cleanly; later Docker startup was crash-looping on invalid config.
- Crash-loop cause: stale Docker image plus `.env` had `TOTAL_CAPITAL=40`,
  while configured strategy sizes required a larger bankroll. Config
  validation refused to boot.
- Fix applied:
  - rebuilt `polymarket-swarm:latest` from current swarm source
  - set `~/Desktop/poly/bot/.env` `TOTAL_CAPITAL=100`
  - restarted with Docker Compose in explicit `BOT_MODE=dryrun`
- Current status after fix:
  - container `polymarket-swarm` is `Up` and Docker health is `healthy`
  - restart policy is `unless-stopped`
  - restart count is `0`
  - mode is dry-run only; no real orders are being placed
- Verification:
  - `docker compose ps` in `~/Desktop/poly/bot` shows
    `polymarket-swarm` healthy
  - `scripts/python/monitor.py --once` shows fresh swarm log age
  - dashboard health at `http://127.0.0.1:7778/healthz` returns OK

Do not switch the swarm to live without a separate preflight review. The swarm
fix was operational only: persistent dry-run service, not live-trading approval.

## Live Addresses

- Legacy proxy/funder:
  `0x84fa6ea1E274B73C81D61cFF28dc1F8e05136882`
- Deposit wallet:
  `0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb`
- Active maker address on matched CLOB orders:
  `0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb`

## What Was Changed

### CLOB v2 / deposit-wallet support

Main file:

- `agents/polymarket/polymarket.py`

Implemented:

- Migrated from `py_clob_client` to `py_clob_client_v2`.
- Added deposit-wallet mode via:
  - `POLYMARKET_DEPOSIT_WALLET`
  - `POLYMARKET_SIGNATURE_TYPE=3`
- Added funder selection:
  - deposit wallet first
  - legacy proxy second
  - EOA fallback
- Added builder attribution support:
  - `POLYMARKET_BUILDER_CODE`
  - `POLYMARKET_BUILDER_ADDRESS`
- Switched deposit-wallet collateral to pUSD:
  `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`
- Switched deposit-wallet exchange addresses to the post-migration CLOB v2
  addresses.
- Balance lookup now reads the funder/deposit wallet balance in deposit-wallet
  mode.

### Order execution hardening

Main file:

- `agents/polymarket/polymarket.py`

Implemented:

- Added live orderbook-aware market buy pricing.
- Before FOK submission, the bot walks current asks and sets the FOK limit
  price to the worst required ask plus one tick.
- Added slippage cap:
  - `POLYMARKET_MAX_SLIPPAGE`, default `0.03`
- Added minimum market order amount:
  - `POLYMARKET_MIN_ORDER_USDC`, default `1.0`
- If orderbook liquidity is thin, the bot reduces order size rather than
  submitting a doomed FOK order.
- If live ask price is too far above the model price, the bot refuses to trade.

This fixed the prior CLOB error:

```text
order couldn't be fully filled. FOK orders are fully filled or killed.
```

### Trade journal status mapping

Main file:

- `agents/application/trade.py`

Implemented:

- CLOB status `matched` is now treated as local terminal status `filled`.
- Existing matched bot rows in `data/trade_log.db` were normalized from
  `submitted` to `filled`.

### Deposit-wallet tooling

Added:

- `scripts/python/setup_deposit_wallet.py`
- `scripts/python/env_status.py`

`setup_deposit_wallet.py` handles deposit-wallet deployment, pUSD transfer, and
approval setup using the builder relayer credentials.

`env_status.py` checks required env key presence without printing secret values.

### Requirements

Updated:

- `py_clob_client_v2==1.0.1rc1`
- `py-builder-relayer-client==0.0.2rc1`

## Verified Working Evidence

### Tests

Latest verification:

```text
25 passed
```

### Current account state

Authenticated CLOB/account check:

```json
{
  "balance": 70.185044,
  "open_orders": []
}
```

### Smoke order

- Status: `matched`
- Order:
  `0x3c97624b9fa44cc77fb7661c038af530ab62f33e65d5688394ec3998df00127e`
- Tx:
  `0x0bad489f3ad313b0ca811478f03e45028a484d27bfa8fd8b8373df87ac695651`

### Bot live order 1

- Market: `566188`
- Local status: `filled`
- CLOB status: `MATCHED`
- Side: `BUY`
- Outcome: `Yes`
- Order:
  `0x98e9b20b82115e86bc7e5feabc2f3cd53c9d8de36bc70257abf2885c3699b495`
- Tx:
  `0xe5b321d2d81b7b06495b67d950c8a17136c16097a5ef787a1c2d6b72f99139df`
- Maker:
  `0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb`

### Bot live order 2

- Market: `566228`
- Local status: `filled`
- CLOB status: `MATCHED`
- Side: `BUY`
- Outcome: `Yes`
- Order:
  `0x9144b707d6faf7b7d7947014563520ec755fde2ac514840b4de04ef2ce7d3253`
- Tx:
  `0x9a491a4a9015bc475fd306fd19d3c997dc529f66a3d03246cee3d51e0aa5ef1e`
- Maker:
  `0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb`

### Bot live order 3

- Market: `566187`
- Local status: `filled`
- CLOB status: `MATCHED`
- Side: `SELL`
- Order:
  `0xd8d8c88f0a9fcaee07af6a4eb6c418fb4101a79c75769633e3e01f4752f9a98d`
- Tx:
  `0xb2e255a8689e0ebba2a29a24ef5ba5f4e3b34e283272d9a87dd3d071c505d039`

### Bot live order 4

- Market: `653788`
- Local status: `filled`
- CLOB status: `MATCHED`
- Side: `BUY`
- Order:
  `0xf810656bc6c0292541c35018bf34bc584ac32b88da22887dc46c0d2be6aae816`
- Tx:
  `0xc71b4e09ab20ac72ca89cccb932aa1d85723ec7d0315e36453157e32852cd2bb`

### Bot live order 5

- Market: `653788`
- Local status: `filled`
- CLOB status: `MATCHED`
- Side: `BUY`
- Order:
  `0x54fdf93cb074a73dc34e3edc5e8a289619133d850ca769f2f409359b94db2315`
- Tx:
  `0xaa0cf16d7787ef93dd3095dc0de04657dba30504e32820b99af6fcb728c0b161`

## Current Runtime State

- Docker service: `trader`
- Container name: `poly1`
- Last observed status: healthy
- Open orders: none
- The daemon is expected to sleep between cycles.
- The risk gate is currently blocking new entries because the cash-balance
  drawdown check is above `MAX_DAILY_LOSS_PCT=10%`.
- Keep the pause in place until a mark-to-market/position review is done.

## Commands For Future Agents

Use the Docker binary directly on this machine:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker compose ps
```

Tail recent logs without attaching forever:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker logs --tail 160 poly1
```

Check balance and open orders:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker compose run --rm trader python -c "import json; from agents.polymarket.polymarket import Polymarket; p=Polymarket(live=True); print(json.dumps({'balance': p.get_usdc_balance(), 'open_orders': p.client.get_open_orders()}, default=str, indent=2))"
```

Check recent local trade journal:

```bash
sqlite3 data/trade_log.db "select id,ts,market_id,side,price,size_usdc,confidence,status,json_extract(response_json,'$.status'),json_extract(response_json,'$.order_id'),error from trades order by id desc limit 12"
```

Run tests:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker compose run --rm trader pytest -q
```

Rebuild trader image:

```bash
/bin/zsh -lc 'PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH" /Applications/Docker.app/Contents/Resources/bin/docker compose build trader'
```

Start live daemon:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker compose up -d trader
```

Stop live daemon:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker compose stop trader
```

Safe env presence check:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker compose run --rm trader python scripts/python/env_status.py
```

## Safety Notes

- Do not print `.env`.
- Do not commit `.env`.
- Earlier debugging printed secret values in terminal output. Treat at least
  these as compromised before larger trading:
  - `OPENAI_API_KEY`
  - `POLYGON_WALLET_PRIVATE_KEY`
- Current live settings are intentionally conservative.
- Do not manually reset or delete `data/trade_log.db`; it is used by the risk
  gate.
- Failed rows before the final patch are historical and expected:
  - `maker address not allowed` happened before deposit-wallet flow was fixed.
  - FOK kill rows happened before orderbook-aware pricing was added.

## Primary References

- Current status:
  `deploy/CURRENT_STATUS.md`
- Deposit-wallet runbook:
  `docs/POLYMARKET_DEPOSIT_WALLET_RUNBOOK.md`
- Main CLOB adapter:
  `agents/polymarket/polymarket.py`
- Trading orchestration:
  `agents/application/trade.py`
- Risk gate:
  `agents/application/risk_gate.py`
- Tests:
  `tests/test_trader.py`
