# Server Source Of Truth - 2026-05-19

This file is the operating contract for poly1 live trading.

## Canonical Runtime

- Live host: `trader@83.229.82.193`
- Live project path: `/srv/poly1`
- Runtime owner: server only.
- Local workspace: development, review, tests, and deployment staging only.

No local process may place trades, maintain exits, write the live journal, or send the
official Telegram dashboard. If local commands are used, they must run in dry mode or
target the server through SSH.

## One Boss

The single control plane is:

- `scripts/runtime_control.py`
- server `deploy/.env.runtime`
- server `data/runtime_control.json`
- server `data/HALT`

Do not hand-edit runtime flags in multiple places. Use `runtime_control.py freeze` or
`runtime_control.py live-probe` so the env file, runtime JSON, config hash, and HALT
brake stay synchronized.

Dashboards and monitors are read-only by default. They must not create or remove
`HALT` directly unless an explicit emergency override env flag is set. Normal
freeze/resume/probe operations go through `runtime_control.py`.

Current safe posture after the commander audit:

- `mode=freeze`
- `allowed_live_agents=[]`
- `data/HALT` present
- all entry agents disabled
- exit-only maintenance allowed for position protection

## One Trade Journal

The live trade journal is only:

- server `/srv/poly1/data/trade_log.db`

Do not sync local `data/` to the server. Do not copy server `data/` back over local
data unless the goal is an explicit read-only backup or analysis snapshot.

## One Wallet Runtime

Wallet balances, live credentials, and exchange-facing state belong to the server
environment only.

Never deploy these from the local machine:

- local `.env`
- local `data/`
- local `deploy/.env.runtime`
- secrets or wallet files

The checked-in `.env.example` documents defaults, but it is not a live runtime file.

## One Telegram Path

The official Telegram dashboard is:

- `poly1-telegram-reporter`
- interval: hourly (`TELEGRAM_REPORT_SECONDS=3600`)
- no startup message (`TELEGRAM_REPORT_SEND_ON_START=false`)
- no direct per-agent spam (`TELEGRAM_DIRECT_NOTIFICATIONS=false`)
- no trade-alert spam unless explicitly re-enabled (`TELEGRAM_TRADE_ALERTS=false`)

Old ad-hoc monitors are disabled and must not be restarted.

## No Brainless Trading

Every live entry must pass through the brain/risk gate before execution. The canonical
policy is in `agents/application/trading_policy.py`:

- stop loss: `3%`
- fast take-profit starts from `5%`
- hard take-profit cap: `25%`
- max trades per hour: `100`
- max allocation per agent: `50%` of wallet capital
- market scan cadence: `60s`
- open-position revalidation cadence: `60s`
- max hold: `6h` hard ceiling, not a target

The preferred strategy is to exit as quickly as the brain can justify with the best
available profit. Holding is allowed only when the brain has a strong reason to hold.

As of the 2026-05-19 follow-up, brain-gate failures are fail-closed for live
entries. If MarketBrain/MetaBrain is missing or throws during a live entry
decision, the trade is blocked.

## Drift Check

Use this from the local workspace to verify runtime code parity without touching
server data or secrets:

```bash
scripts/verify_server_source_of_truth.sh
```

The check compares code/config surfaces and intentionally excludes:

- `.env`
- `deploy/.env.runtime`
- `data/`
- caches and bytecode

It also checks that local poly1 containers are not running and that server
`runtime_control.json`, `data/HALT`, and `data/trade_log.db` exist.

## API Health Check

Use this on the server to verify external providers without printing secrets:

```bash
scripts/check_api_health.py
```

The probe covers Tavily, OpenAI, Anthropic, Polymarket Gamma, Polymarket CLOB,
and configured optional provider keys.
