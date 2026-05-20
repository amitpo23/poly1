# 2026-05-20 Context Handoff

This is the short-term context window for the next agent/session. Read this
before touching live trading.

## Current State

- Local repo: `/Users/mymac/coding/poly1`
- Live server: `trader@83.229.82.193:/srv/poly1`
- Server is the only live source of truth.
- Runtime mode: `freeze`
- HALT: present
- Open positions from today's scanner proofs: none
- Latest deployed commit:
  `f916f3c feat: harden scanner signal execution quality`
- Tests after scanner-quality upgrade:
  `python3 -m unittest discover -s tests` → `538 tests OK`
- Server freeze preflight after deployment: OK
- Current server cash/equity after the final negative micro proof:
  `27.403582 USDC`

Do not copy local `data/`, local `.env`, or local `deploy/.env.runtime` to the
server. Use `scripts/runtime_control.py` only.

## What We Proved Today

The earlier live problem was not one single bug; it was a chain of blockers.
Today we proved the chain can now run end to end:

1. `market_scanner` writes approved `brain_decisions`.
2. `scanner_executor` consumes fresh approved scanner decisions.
3. It checks score, timing, duplicate positions, orderbook, executable EV,
   `RiskGate`, and runtime scope.
4. It opens live `$1` positions.
5. `position_manager` tracks open positions every cycle.
6. LLM/brain exit decisions can close positions.
7. Auto-freeze returns the system to `freeze`.

The proof opened three micro trades and all three closed. PnL was slightly
negative, but the infrastructure path worked.

A follow-up proof with `--scanner-wait-min-score 0.76` opened five MIBR entries
at `$1` each. The CLOB initially returned `delayed`, but `get_order` later
showed all entries were `MATCHED`. Position manager sold `8.32` shares at
`0.65`; after manual reconciliation, no open positions remained and equity was
`27.55036 USDC`, about `+0.375 USDC` versus the `27.175115` baseline.

A later 15-minute controlled proof opened two `$1` scanner-executor positions.
Both closed by stop-loss and the run ended around `27.403582 USDC`, about
`-0.1468 USDC` from the `27.55036` baseline. Infrastructure still behaved
correctly; the failure mode was signal quality and repetitive market selection.

## Key Fixes From The Proof

### Scanner Quality Upgrade

Implemented after the negative micro proof:

- `market_scanner` fetches multiple Gamma market orderings via
  `SCANNER_FETCH_ORDERS` and dedupes the result.
- `SCANNER_TARGET_TRADE_DECISIONS` lets a cycle aim for a small number of
  diverse trade approvals instead of flooding the journal with the same market
  cluster.
- Recent `closed_stop_loss` / `resolved_loss` rows apply a soft score penalty
  before a market is routed again.
- `scanner_executor` revalidates live executable price against the scanner
  entry price and rejects excessive drift.
- `scanner_executor` requires raw EV plus net EV after
  `SCANNER_EXECUTOR_ROUND_TRIP_COST_PCT`, so spread/slippage/exit friction is
  part of the gate.
- Scanner and executor decisions preserve `signal_source` in `brain_decisions`
  for later provider trust and outcome feedback.

### Equity Guard

Cash-only drawdown was false-positive unsafe because open positions reduce cash
while still having mark-to-market value. The live guard now uses:

```text
portfolio equity = USDC cash + open position MTM
```

Relevant commits:

- `baaf738 feat: add portfolio equity live guard`
- `91d9166 fix: use position marks in equity guard fallback`
- `db10ab6 feat: record equity baseline in runtime control`

Use:

```bash
docker compose exec -T trader python scripts/live_equity_guard.py --drawdown-limit 0.75 --json
```

### Scanner Executor Wait Probe

Scanner decisions around `0.792` were blocked because:

- wait override threshold was `0.79`
- general executor score threshold was still `0.80`

Now `runtime_control.py live-hour --scanner-allow-wait --scanner-wait-min-score
0.79` aligns both thresholds.

Relevant commits:

- `1292ea4 feat: allow controlled scanner wait probes`
- `603cf42 fix: align scanner wait probe score gate`

### Micro Exit Dust

The `$1.00` exit notional floor blocked `$1` test exits when real notional was
around `$0.96-$0.99`. Runtime defaults now set:

```env
MAINTAIN_MIN_EXIT_NOTIONAL_USDC=0.50
```

Relevant commit:

- `60fb845 fix: lower exit notional for micro probes`

### Executable Take Profit

The LoL trade showed why midpoint PnL is not enough. It was marked
`closed_take_profit` although actual executable close PnL was negative after
sell slippage/orderbook pricing.

Now take-profit is only a take-profit if the executable sell price clears:

```env
MAINTAIN_MIN_TAKE_PROFIT_NET_PCT=0.015
MAINTAIN_MIN_TAKE_PROFIT_USDC=0.01
```

LLM EXIT with midpoint profit but executable loss is closed as risk/stop-loss,
not `closed_take_profit`.

Relevant commit:

- `181734c fix: require executable profit for take-profit exits`

### Delayed CLOB Reconciliation

The final proof exposed a money-safety bug: FOK/FAK orders can return
`status=delayed` and then become `MATCHED` moments later. The old code marked
those rows failed immediately, which created a real but unmanaged position.

Now both entry and exit order paths briefly call `get_order(order_id)` before
deciding that a delayed order failed.

Relevant commit:

- `1c0434c fix: reconcile delayed clob orders before marking failure`

## Live Proof Result

Trades opened:

- Spurs vs Thunder, BUY at about `0.33`, `$1`
- Aston Villa win, BUY at about `0.59`, `$1`
- LoL Nongshim Red Force, BUY at about `0.32`, `$1`

Closures:

- Aston Villa closed as `closed_stop_loss`, small loss.
- LoL closed as `closed_take_profit` before the executable-profit fix, but
  actual PnL was negative. This is the bug fixed in `181734c`.
- Spurs closed as `closed_stop_loss`, small loss.

Approximate outcome:

- Close-row PnL: roughly `-0.08` to `-0.10 USDC`
- Equity comparison including friction: roughly `-0.13` to `-0.15 USDC`

Do not treat this run as signal-quality success. Treat it as infrastructure
success plus a signal/exit-pricing lesson.

## Current Risks

- OpenAI returns HTTP `429` in live position-manager logs. Anthropic fallback is
  working, but OpenAI quota/billing should be fixed before relying on OpenAI.
- Scanner market quality was weak in the last proof; the local scanner-quality
  upgrade needs deployment and a fresh controlled proof before increasing size.
- Position-manager now has executable-profit protection, but this needs a fresh
  proof run before increasing size.
- Some old journal rows from previous experiments still appear in broad
  `filled` queries. Use preflight/open-position accounting, not naive status
  queries, to judge whether the current system has unmanaged positions.

## Next Live Run Recommendation

Stay conservative.

1. Confirm freeze:

```bash
ssh trader@83.229.82.193 'cd /srv/poly1 && python3 scripts/runtime_control.py status && python3 scripts/trading_stability_preflight.py --mode freeze'
```

2. Start a short proof only if explicitly requested:

```bash
python3 scripts/runtime_control.py live-hour \
  --budget 5 \
  --wallet-balance <cash> \
  --equity-balance <equity> \
  --minutes 15 \
  --max-open 4 \
  --agents scanner_executor \
  --position-size-usdc 1.00 \
  --max-daily-token-usd 1.0 \
  --scanner-allow-wait \
  --scanner-wait-min-score 0.79 \
  --arm \
  --note "scanner executable-tp proof"
```

3. Recreate services with:

```bash
docker compose --profile scanner --profile positions --profile supervisor \
  --profile settlement --profile monitoring up -d scanner-executor \
  market_scanner trader position_manager trading-supervisor \
  settlement-reconciler telegram-reporter
```

4. Ensure auto-freeze is created for the new `expires_at`.

5. Monitor with equity guard, not cash:

```bash
docker compose exec -T trader python scripts/live_equity_guard.py --drawdown-limit 0.75 --json
```

## What Not To Do

- Do not run live from local.
- Do not enable many agents at once for the next proof.
- Do not raise position size until executable take-profit has been observed in
  a new run.
- Do not mark midpoint-only gains as success.
- Do not ignore OpenAI 429; fallback works, but it is still a live dependency
  gap.
