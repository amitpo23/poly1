# Overnight And Multi-Week Operation Plan

Date: 2026-05-04

This is the intended operating mode after confirming Polymarket CLOB v2 live
trading works.

## Current Decision

Let the bot trade live for the next several weeks with conservative limits. Do
not increase size, trade frequency, or model freedom without explicit approval.

The goal for this phase is controlled data collection and operational learning:

- verify the live trading loop remains stable over many cycles
- collect model recommendations and execution outcomes
- review fills, skips, failures, balance, and open orders
- tune strategy only after there is enough evidence

## 2026-05-05 Morning Update

The multi-week run should pause for review before further live entries.

Observed:

- container `poly1` is healthy
- authenticated CLOB balance is `70.185044`
- authenticated open orders are `[]`
- local journal records `5` filled/matched bot trades
- risk gate is blocking new entries because cash-balance drawdown is above
  `MAX_DAILY_LOSS_PCT=10%`

This is not a crash. It is the configured safety gate doing its job. The next
engineering step should be mark-to-market position tracking/reporting before
loosening this gate.

## Current Runtime Mode

```env
EXECUTE="true"
MAX_POSITION_FRACTION="0.025"
STARTING_BALANCE_USDC="80.0"
MAX_TRADES_PER_HOUR="2"
MIN_CONFIDENCE="0.60"
```

Observed daemon behavior:

- wakes roughly every 30 minutes
- scans the configured top markets
- trades only when confidence and risk gates pass
- checks the live orderbook before posting
- posts FOK orders, so orders should fill immediately or cancel
- leaves no stale open orders under normal conditions

## What Learning Means Right Now

The current system does not automatically retrain itself or rewrite strategy
logic. It learns operationally by accumulating records in:

- `data/trade_log.db`
- container logs
- Polymarket/CLOB order state

Future agents should treat this phase as measurement first, then tuning. Do not
assume the bot is self-improving unless a real feedback/retraining loop is
implemented later.

## Daily Check

Run:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker compose ps
```

```bash
/Applications/Docker.app/Contents/Resources/bin/docker logs --tail 160 poly1
```

```bash
/Applications/Docker.app/Contents/Resources/bin/docker compose run --rm trader python -c "import json; from agents.polymarket.polymarket import Polymarket; p=Polymarket(live=True); print(json.dumps({'balance': p.get_usdc_balance(), 'open_orders': p.client.get_open_orders()}, default=str, indent=2))"
```

```bash
sqlite3 data/trade_log.db "select id,ts,market_id,side,price,size_usdc,confidence,status,json_extract(response_json,'$.status'),json_extract(response_json,'$.order_id'),error from trades order by id desc limit 20"
```

Expected healthy state:

- container is healthy
- no repeated new errors
- open orders are `[]`
- successful CLOB orders show `matched` / `MATCHED`
- local matched orders are marked `filled`

## Weekly Review

Once per week, review:

- number of filled trades
- number of skipped confidence/risk gate decisions
- number and type of failures
- balance movement
- whether the bot is repeatedly trading the same market family
- whether slippage guard is blocking many otherwise good signals
- whether `MAX_TRADES_PER_HOUR=2` is too restrictive or appropriate

Only after review should risk knobs be changed.

## Risk Knobs

Conservative defaults to keep:

- `MAX_POSITION_FRACTION=0.025`
- `MAX_TRADES_PER_HOUR=2`
- `MIN_CONFIDENCE=0.60`
- `POLYMARKET_MAX_SLIPPAGE=0.03`
- `POLYMARKET_MIN_ORDER_USDC=1.0`

Potential future adjustments, only with explicit approval:

- increase `MAX_TRADES_PER_HOUR`
- increase `MAX_POSITION_FRACTION`
- lower or raise `MIN_CONFIDENCE`
- alter market selection / `TOP_N`
- add an automated post-trade analytics loop

## Stop Conditions

Stop the daemon immediately if any of these appear:

- repeated CLOB auth failures
- non-empty open orders that do not clear
- repeated unexpected failed order submissions
- balance read failures
- drawdown exceeds intended tolerance
- `MAY_HAVE_FIRED` appears in the trade log
- `.env` or private keys are printed again

Stop command:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker compose stop trader
```

Optional kill switch:

```bash
touch data/HALT
```

Resume after investigation:

```bash
rm data/HALT
/Applications/Docker.app/Contents/Resources/bin/docker compose up -d trader
```

## Security Requirement Before Scaling

Earlier debugging printed secret values in terminal output. Before increasing
capital, size, or unattended aggressiveness, rotate at least:

- `OPENAI_API_KEY`
- `POLYGON_WALLET_PRIVATE_KEY`

Do not print `.env`, and do not commit `.env`.

## Related Docs

- `docs/AGENT_HANDOFF_2026-05-04.md`
- `docs/RELEASE_NOTES_2026-05-04_CLOB_V2.md`
- `docs/POLYMARKET_DEPOSIT_WALLET_RUNBOOK.md`
- `deploy/CURRENT_STATUS.md`
