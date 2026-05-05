# Trading Log And Journal

Last updated: 2026-05-05 08:56 Israel time

This document explains where the bot records trades, decisions, and runtime
events.

## Current Morning Status

- Bot container: `poly1`
- Last observed container state: healthy
- Current authenticated CLOB balance: `70.185044`
- Open orders: `[]`
- Total filled/matched bot trades: `5`
- Active capital recorded in the local journal: `9.489509275`
- Current live trading state: risk gate is blocking new entries because cash
  balance drawdown is above `MAX_DAILY_LOSS_PCT=10%`.

Important nuance: the drawdown gate uses cash balance versus
`STARTING_BALANCE_USDC=80.0`. It does not mark positions to market. Some of the
cash reduction is capital deployed into positions, not necessarily realized
loss.

## Structured Trade Journal

Primary journal:

```text
data/trade_log.db
```

This is a SQLite database. It records:

- every pending trade attempt
- filled/matched trades
- failed trades
- skips caused by confidence gates
- skips caused by duplicate-market protection
- exchange response JSON
- order IDs and transaction hashes when available

Schema:

```sql
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT,
    side TEXT,
    price REAL,
    size_usdc REAL,
    confidence REAL,
    status TEXT NOT NULL,
    response_json TEXT,
    error TEXT
);
```

Useful query:

```bash
sqlite3 data/trade_log.db "select id,ts,market_id,side,price,size_usdc,confidence,status,json_extract(response_json,'$.status'),json_extract(response_json,'$.order_id'),error from trades order by id desc limit 30"
```

Filled-only query:

```bash
sqlite3 data/trade_log.db "select id,ts,market_id,side,price,size_usdc,confidence,status,json_extract(response_json,'$.status'),json_extract(response_json,'$.order_id'),json_extract(response_json,'$.raw.transactionsHashes[0]') from trades where status='filled' order by id"
```

Status counts:

```bash
sqlite3 data/trade_log.db "select status,count(*) from trades group by status order by status"
```

Current counts:

```text
failed|11
filled|5
skipped_dedupe|42
skipped_dry_run|6
skipped_gate|14
```

## Filled Bot Trades

```text
id 24
ts 2026-05-04T18:26:23.597544+00:00
market 566188
side BUY
price 0.38
size_usdc 1.996437875
confidence 0.65
status filled / matched
order 0x98e9b20b82115e86bc7e5feabc2f3cd53c9d8de36bc70257abf2885c3699b495
tx 0xe5b321d2d81b7b06495b67d950c8a17136c16097a5ef787a1c2d6b72f99139df
```

```text
id 25
ts 2026-05-04T18:26:26.713947+00:00
market 566228
side BUY
price 0.997
size_usdc 1.94576265
confidence 0.65
status filled / matched
order 0x9144b707d6faf7b7d7947014563520ec755fde2ac514840b4de04ef2ce7d3253
tx 0x9a491a4a9015bc475fd306fd19d3c997dc529f66a3d03246cee3d51e0aa5ef1e
```

```text
id 35
ts 2026-05-04T21:01:53.837284+00:00
market 566187
side SELL
price 0.565
size_usdc 1.897269575
confidence 0.75
status filled / matched
order 0xd8d8c88f0a9fcaee07af6a4eb6c418fb4101a79c75769633e3e01f4752f9a98d
tx 0xb2e255a8689e0ebba2a29a24ef5ba5f4e3b34e283272d9a87dd3d071c505d039
```

```text
id 40
ts 2026-05-04T21:32:21.958997+00:00
market 653788
side BUY
price 0.11
size_usdc 1.848829075
confidence 0.75
status filled / matched
order 0xf810656bc6c0292541c35018bf34bc584ac32b88da22887dc46c0d2be6aae816
tx 0xc71b4e09ab20ac72ca89cccb932aa1d85723ec7d0315e36453157e32852cd2bb
```

```text
id 77
ts 2026-05-05T04:10:13.629675+00:00
market 653788
side BUY
price 0.11
size_usdc 1.8012101
confidence 0.75
status filled / matched
order 0x54fdf93cb074a73dc34e3edc5e8a289619133d850ca769f2f409359b94db2315
tx 0xaa0cf16d7787ef93dd3095dc0de04657dba30504e32820b99af6fcb728c0b161
```

## Runtime Logs

Docker logs:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker logs --tail 260 poly1
```

Local runtime log file:

```text
data/logs/poly1.log
```

Current log file size:

```text
data/logs/poly1.log ~752 KB
```

Use runtime logs for:

- daemon cycle timing
- risk gate decisions
- market filtering
- order submission details
- CLOB errors
- repeated skip reasons

## LLM Usage Log

Token/cost usage is recorded here:

```text
data/llm_usage.jsonl
```

Use this when reviewing AI cost and model-call volume.

## Local Monitor

One-shot JSON monitor:

```bash
python3 scripts/python/monitor.py --once --json
```

Watch mode:

```bash
python3 scripts/python/monitor.py --watch
```

Web monitor, if not already running:

```bash
python3 scripts/python/monitor_web.py --port 7777
```

Dashboard URL:

```text
http://127.0.0.1:7777/
```

If port `7777` is already occupied, use another local port. During the
2026-05-05 swarm recovery, the dashboard was started on:

```text
http://127.0.0.1:7778/
```

The monitor now reads the sister swarm DB with an immutable read-only SQLite
snapshot and surfaces:

- stale/offline swarm alerts
- swarm table counts and pending-order status
- recent order ledger rows
- recent NothingHappens decisions
- recent swarm log lines

Swarm health can be checked from the dashboard or directly:

```bash
curl -fsS http://127.0.0.1:7778/healthz
```

## Stop / Resume

Stop live daemon:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker compose stop trader
```

Start live daemon:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker compose up -d trader
```

Optional kill switch:

```bash
touch data/HALT
```

Remove kill switch:

```bash
rm data/HALT
```
