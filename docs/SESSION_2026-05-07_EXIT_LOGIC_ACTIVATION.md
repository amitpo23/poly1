# Session log — 2026-05-07: exit-logic activation + first SL closes

> Sister to `docs/POSTMORTEM_2026-05-06.md`. This file is the action log
> for the morning that followed the postmortem: what got built, what
> broke, what got fixed, and the on-chain results.

## Context coming into this session

End of 2026-05-06:
- Cash: $0.72 on the deposit wallet
- 4 active poly1 positions (cost basis ~$19.49, MTM ~$18.43)
- 5 resolved-against-us tokens (~$12.50 already lost)
- `maintain_positions()` was a stub
- User instruction: "פוזיציה שירדה מעל 10 אחוז אנחנו יוצאים ופוזיציה שעלתה
  עלתה 10 אחוז אנחנו יוצאים" — symmetric 10% TP/SL, activate immediately

## What got built

### `agents/application/position_manager.py` (new, ~400 lines)

- `PositionManagerConfig` — dataclass with `take_profit_pct`, `stop_loss_pct`,
  `max_hold_hours`, `poll_sec`, `execute` (default 0.10/0.10/720h/60s/false)
- `AggregatedPosition` — sums multiple fills on the same token into a single
  weighted-avg-entry record, including the holding-side price (BUY=price,
  SELL=1-price)
- `PositionManager.check_and_close_positions()` — main entry; loops through
  open aggregated positions, fetches current mid via CLOB midpoint,
  evaluates against TP/SL/max_hold thresholds, calls `_close_position()`
- `PositionManager._on_chain_shares()` — reads the deposit-wallet's CTF
  balance for a given token via `get_balance_allowance(BalanceAllowanceParams(
  asset_type=AssetType.CONDITIONAL, token_id=...))`. Used to clamp the sell
  size to ≤ on-chain reality (journal-based share counts can drift due to
  fees taken at fill, slippage on entry).
- `PositionManagerDaemon` — runs the cycle every `MAINTAIN_POLL_SEC`.

### Schema additions to `trade_log.py`

- `filled_positions_with_id()` — returns `(id, ts, market_id, token_id, side,
  price, size_usdc)` rows for status='filled'
- `has_close_attempt_for_token(token_id)` — returns True if there's a
  successful close row for this token (closed_take_profit / closed_stop_loss
  / closed_timeout). Critically: **does NOT include `close_failed`** so
  retries are allowed on the next cycle.
- New status enums: `CLOSED_TAKE_PROFIT`, `CLOSED_STOP_LOSS`,
  `CLOSED_TIMEOUT`, `CLOSE_FAILED`. None added to `ACTIVE_STATUSES` (correct;
  they're terminal).

### `polymarket.py` — new method

```python
def sell_shares(self, token_id, shares, limit_price, order_type=None) -> dict:
    if order_type is None:
        order_type = OrderType.GTC
    return self.client.create_and_post_order(
        OrderArgs(price=limit_price, size=shares, side="SELL", token_id=token_id),
        order_type=order_type,
    )
```

### `trade.py` — `maintain_positions()` is no longer a stub

It now delegates to `PositionManager` so callers of
`Trader.maintain_positions()` get the same behavior without a separate
container. The canonical home of the logic is the daemon (run as the
`positions` profile in compose) — the inline call exists for tests and
single-shot CLI invocations.

### `docker-compose.yml` — new service

Added `position_manager` under `profiles: ["positions"]`. Uses the same
image as `trader`, runs `python agents/application/position_manager.py`
with `EXECUTE_MAINTAIN`, `MAINTAIN_TAKE_PROFIT_PCT`, `MAINTAIN_STOP_LOSS_PCT`,
`MAINTAIN_MAX_HOLD_HOURS`, `MAINTAIN_POLL_SEC` env vars.

### `.env`

```
EXECUTE_MAINTAIN="true"
MAINTAIN_TAKE_PROFIT_PCT="0.10"
MAINTAIN_STOP_LOSS_PCT="0.10"
MAINTAIN_MAX_HOLD_HOURS="720"
MAINTAIN_POLL_SEC="60"
```

### Tests — `tests/test_position_manager.py`

13 tests across `TestAggregation` / `TestEvaluation` / `TestClosing` /
`TestEdgeCases`. All passed at activation.

## Two bugs hit during activation, both fixed

### Bug 1: `setup_deposit_wallet.py` missed NEG_RISK_ADAPTER

First sell attempts on Arsenal NO returned:
> allowance is not enough -> spender: 0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296

That's the NegRisk Adapter. The original setup script set
`CTF.setApprovalForAll` only on `EXCHANGE` and `NEG_RISK_EXCHANGE`, omitting
`NEG_RISK_ADAPTER`. Sport / political binary markets route SELL orders
through the adapter, so they were rejected at the on-chain allowance check.

Fix: added the adapter to the loop in `scripts/python/setup_deposit_wallet.py`
and re-ran with `EXECUTE=true`. Tx
`0xdebf62acc8ee220f310b3e58f4ea0ea4f1947179d3f131c15f4660cfaee8474a`,
state STATE_MINED.

### Bug 2: `_on_chain_shares()` returned None silently

After fix #1, the Arsenal NO sell went through (21.48 shares × $0.20 →
+$4.32 cash). The Man City YES sell still failed:
> not enough balance / allowance: balance: 21093982, order amount: 21490000

Journal said 21.49 shares, on-chain only had 21.09. The `_on_chain_shares()`
clamp was supposed to catch this, but the "position_manager clamp:" log line
never appeared — meaning the function was returning None.

Cause: the SDK call was passing a plain dict:
```python
params = {"asset_type": "CONDITIONAL", "token_id": str(token_id)}
resp = self.polymarket.client.get_balance_allowance(params)
```
The v2 SDK requires `BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL,
token_id=...)`. The dict was rejected silently.

Fix:
```python
from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
resp = self.polymarket.client.get_balance_allowance(
    params=BalanceAllowanceParams(
        asset_type=AssetType.CONDITIONAL,
        token_id=str(token_id),
    )
)
```

Plus the failure path now logs at `WARNING` (was `DEBUG`) so future
silent failures surface immediately.

After rebuild + recreate of `position_manager`:
```
position_manager clamp: token=781404584321743978 journal=21.4982 on_chain=21.0940 → selling 21.0729
position_manager CLOSE [stop_loss]: token=781404584321743978 entry=0.2478 mid=0.2050 shares=21.07 status=matched
```

## On-chain outcomes

| event | time | result |
|---|---|---|
| Arsenal NO SL | 2026-05-07 04:19 UTC | 21.48 sh × $0.2009 → +$4.32 cash |
| Man City YES SL | 2026-05-07 04:25 UTC | 21.07 sh × $0.2009 → +$4.23 cash |

Cash trajectory: **$0.7236 → $4.9146 → $9.0274**.

Two positions remained open after the cycle, both inside the 10% threshold:
- `169521534992867124...` BUY @ $0.997 (entry == mid, 0% delta)
- `115755033510457721...` BUY @ $0.105 vs entry $0.110 (-4.5%, well inside SL)

Final portfolio at end of session: **$17.62** (cash $9.03 + MTM $8.59).

## Profit-window forensic — was there ever a moment of profit?

Cross-bot scan of all 10 real positions (poly1 main: 8 fills → 4 tokens;
scalper: 4 pairs with cost > 0 → 5 leg-positions; swarm market_maker: 2
fills → 1 token; btc_daily: 0 real fills, all shadow):

**4 of 10 had ≥5% profit windows that were missed:**

| position | entry | peak | gain | when |
|---|---|---|---|---|
| poly1 Man City YES | 0.380 | 0.445 | +17.1% | 05-04 19:50 (90 min after entry) |
| scalper XRP-UP 17:15 | 0.451 | 0.510 | +13.1% | 05-06 17:10 (5 min before resolve) |
| scalper SOL-UP 17:15 | 0.490 | 0.525 | +7.1% | 05-06 17:10 |
| scalper ETH-UP 17:15 | 0.490 | 0.520 | +6.1% | 05-06 17:10 |

**6 had no meaningful profit window:**
- poly1 Arsenal NO: declined monotonically 0.435 → 0.205 (-52.9%)
- poly1 169521: max 0.9975 vs 0.997 entry (+0.05%)
- poly1 115755: flat at entry 0.110
- scalper ETH-DN: +4.2% peak (just below 5%)
- scalper SOL-UP 17:30: -1.0% (lost immediately)
- swarm Hormuz YES: +1.7% peak

### What this tells us

1. **Man City is the single biggest miss.** A 17% window in 90 minutes is
   exactly what 10% TP/SL is designed to catch. With the now-active
   mechanism, that exit would have locked +$0.20 instead of -$0.62.

2. **The 3 scalper UP-legs (XRP/SOL/ETH at 17:15) all peaked 5 minutes
   before market resolution.** This is by design — the scalper rides to
   resolution rather than exiting mid-cycle. The real bug was that all 3
   ended in `RECONCILE_NEEDED` (leg-2 never bought) → unhedged. That bug
   is fixed (`Fix A` in the scalper: skip `RECONCILE_NEEDED`/`EXPIRED`
   markets in `discover_markets`, plus `limit=200` instead of default
   `limit=50`).

3. **Arsenal NO is the failure mode the no-averaging-down guard was
   built for.** The bot bought at NO=0.435 on 05-04, watched it slide
   to 0.205 over 36 hours, then bought MORE at 0.205 on 05-06. The
   `has_filled_position_for_market` guard now blocks this.

4. **Most "trading" today was theoretical.** Beyond the 10 real
   positions:
   - swarm: 226 failed market_maker orders on Hormuz (0x348cd9...) —
     would not have been profitable even if they'd filled (mid hovered
     59.5-60.5).
   - btc_daily: 80 shadow attempts (`EXECUTE_BTC_DAILY=false`) —
     no money committed.
   - swarm mean_reversion / news_hunter / ai_decision / arbitrage:
     0 real fills, all dry/empty.
   - news_hunter `nh_journal` had 1 entry (Massie KY-04 NO @ 0.235)
     but `filled: false` and `order_id="dry_3"` → shadow only.

## Files modified

| path | type of change |
|---|---|
| `agents/application/position_manager.py` | **new**, ~400 lines |
| `tests/test_position_manager.py` | **new**, 13 tests |
| `agents/application/trade_log.py` | added `filled_positions_with_id()`, `has_close_attempt_for_token()`, new status enums |
| `agents/application/trade.py` | `maintain_positions()` now delegates to `PositionManager` |
| `agents/polymarket/polymarket.py` | added `sell_shares()` |
| `scripts/python/setup_deposit_wallet.py` | added `NEG_RISK_ADAPTER` to setApprovalForAll loop |
| `docker-compose.yml` | added `position_manager` service under `profiles: ["positions"]` |
| `.env` | `EXECUTE_MAINTAIN=true`, TP/SL=0.10/0.10, max_hold=720h, poll=60s |

## Verification

After the session, the daemon ran 30+ cycles in shadow without errors:
```
position_manager cycle: {'evaluated': 4, 'closed_tp': 0, 'closed_sl': 0,
'closed_timeout': 0, 'errors': 0, 'skipped_already_closed': 2}
```

The 2 closed positions show `skipped_already_closed: 2` (idempotency
working as designed). The 2 still-open positions are evaluated each
cycle but not exited because they're inside the 10% threshold.

## Open follow-ups (deferred, not done this session)

- MTM panel on the unified dashboard (currently no per-position P&L
  visualization)
- Prompt update for the LLM (filter 5%-95% prices, max 30-day
  resolution) to prevent re-entry into Arsenal-class one-way slides
- Global market lock to prevent two agents trading the same market
  simultaneously
- Consecutive-loss circuit breaker (after the day's pattern of 4 losing
  positions in a row, this would have halted earlier)

## Cross-bot policy note

Per `~/Desktop/poly/OPERATIONS.md`, both bots remain independent. The
swarm bot runs on its own wallet/container and was not modified in
this session except for the read-only forensic scan. No code was
propagated between repos.
