# Session 2026-05-12 - Profitable Loop Implementation

This session implemented the first engineering pass from
`docs/TRADING_POSTMORTEM_2026-05-12.md`.

Goal: stop letting live capital reach agents that cannot complete a disciplined
loop:

```text
edge -> EV -> liquidity -> exitable size -> entry -> smart exit -> learning
```

## Implemented in poly1

### 1. Position Profit Monitor

Added persistent `position_marks` in `TradeLog`.

Tracked per token:

- entry price
- current price
- max price
- min price
- MFE %
- MAE %
- peak drawdown %
- shares
- open/closed status

`PositionManager` now stores MFE/MAE in SQLite instead of relying only on the
in-memory `_max_price_by_token`. This means restart does not erase the peak
profit needed for trailing exits.

### 2. Exitable-Size Gate

Added `agents/application/execution_safety.py`.

Entry agents now block positions that are too small to exit safely after a
normal stop-loss move.

Applied to:

- `Trader`
- `BtcDailyEngine`
- `ScalperEngine`

New env vars:

| Env var | Default | Meaning |
| --- | ---: | --- |
| `MIN_EXITABLE_ENTRY_USDC` | `3.0` | Absolute minimum live entry size. |
| `MIN_EXITABLE_STOP_LOSS_PCT` | `0.07` | Stop-loss move assumed for exitability math. |
| `MIN_EXIT_NOTIONAL_USDC` | `1.0` | Minimum practical exit notional. |
| `MIN_EXITABLE_SAFETY_BUFFER` | `1.25` | Buffer above exchange/strategy minimum. |

### 3. Exit-Failure Escalation

`PositionManagerConfig.max_close_failures` default changed from 10 to 3.

Repeated FAK sell failures now escalate faster to a terminal handled state
instead of producing hundreds of `close_failed` rows.

### 4. Market Quarantine

Added `market_quarantine` table and helpers.

When a market hits the broken-market threshold (`404`, missing orderbook, no
asks, stale live ask), `Trader` now quarantines it. Recent quarantines block
re-entry before the LLM spends more analysis on that market.

### 5. OpportunityRouter Live Capital Gate

Added `live_route_allowed(...)` to `OpportunityRouter`.

`Trader` live mode now enforces:

- if `OPPORTUNITY_ROUTER_ENFORCE_LIVE=true` (default),
- and no fresh `live_probe` route exists,
- the trade is blocked with `skipped_gate`.

This deliberately makes missing router evidence a live-trading blocker.

### 6. AI/News Fallback

Added deterministic `heuristic_classify(...)` fallback in `news_signal.py`.

If the LLM classifier fails:

- strong heuristic evidence is written as `heuristic_signal`,
- not as `news_signal`,
- so dashboards/research can see it,
- but allocator logic does not treat it as live-grade intelligence.

### 7. Agent Promotion Ledger

Added `agent_promotion_ledger` table and `TradeLog.upsert_agent_promotion(...)`.

This gives us a durable place to record:

- research
- backtest
- paper
- live_probe
- live_scaled
- demoted

Promotion/demotion can now be written by `goal_status`, allocator, or future
router jobs without inventing a second state store.

## Implemented in swarm

Changed files in sister repo: `/Users/mymac/Desktop/poly/bot`.

### Swarm StateStore

Updated `StateStore.has_active_order_for_market(...)`:

- default behavior still treats `filled` as blocking,
- market maker can pass `include_filled=False`,
- so filled inventory does not freeze the market forever.

Added `fill_inventory_by_market(agent=...)`:

- reconstructs net YES/NO inventory from persisted fills,
- includes fees,
- supports restart recovery.

### Swarm Market Maker

`MarketMakerAgent` now:

- recovers inventory from `fills` on startup,
- uses pending/submitted rows as the active quote blocker,
- does not let historical `filled` rows freeze quote maintenance.

This fixes the observed behavior where market maker bought once, then kept
logging that a submitted/filled row existed and stopped managing the market.

## Verification

poly1:

```bash
.venv/bin/python -m py_compile \
  agents/application/execution_safety.py \
  agents/application/trade_log.py \
  agents/application/position_manager.py \
  agents/application/opportunity_router.py \
  agents/application/trade.py \
  agents/application/btc_daily.py \
  agents/application/news_signal.py \
  agents/application/scalper.py
```

```bash
.venv/bin/python -m unittest \
  tests.test_execution_safety \
  tests.test_position_manager \
  tests.test_opportunity_router \
  tests.test_news_signal.TestNewsSignalLogic \
  tests.test_news_signal.TestNewsSignalStorage \
  tests.test_trader.TestTradeLog \
  tests.test_trader.TestRiskGate -v
```

Result: 56 tests passed.

Full poly1 test run was not possible in this local venv because these
dependencies are missing:

- `pytest`
- `langchain_openai`
- `langchain_core`
- `web3`

swarm:

```bash
.venv/bin/python -m pytest tests/test_state_store.py tests/test_market_maker_agent.py -q
```

Result: 25 tests passed.

## Remaining Work

This is the first hardening pass, not the final trading brain.

Next useful work:

1. Wire `agent_promotion_ledger` into `goal_status.py` or allocator output.
2. Make `OpportunityRouter` consume live CLOB orderbook at the final decision
   point for every strategy, not only scout/research rows.
3. Add dashboard panels for `position_marks`, MFE/MAE, market quarantines, and
   agent promotion states.
4. Add a scheduled job that writes promotion/demotion state every 15 minutes.
5. Add controlled maker/taker fallback for failed exits instead of only terminal
   escalation.
