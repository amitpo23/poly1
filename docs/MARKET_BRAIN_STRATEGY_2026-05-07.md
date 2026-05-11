# MarketBrain Strategy Layer — 2026-05-07

## Why

Yesterday's trading showed that the system can find temporary edge, but the
agents were too isolated:

- some agents only watched price/orderbook;
- the news classifier was dry-run analytics only;
- no shared component could veto a trade because the market type, external
  context, liquidity, or timing was wrong;
- exits were not consistently attached before entries.

The first fix is a small deterministic `MarketBrain` layer. It does **not**
place orders. It classifies a market and returns `approved/veto + reason + score`
to the strategy agent before the agent can enter.

## Current Implementation

File: `agents/application/market_brain.py`

Current integration:

- `ScalperDaemon` constructs a `MarketBrain`.
- `ScalperEngine.tick()` calls the brain before first-leg and second-leg entries.
- Every scalper brain approval/veto is written to `brain_decisions` in
  `trade_log.db` for later calibration.
- Existing tests remain unaffected unless a test explicitly injects a brain.

Current scalper checks:

- classify `btc|eth|sol|xrp-updown-15m-<timestamp>` as `crypto_15m`;
- veto if too close to expiry;
- veto if candidate entry price is too high;
- veto if `up_ask + down_ask` is too expensive;
- score cheap/reversal/depth entries and veto weak edge scores;
- allow unknown slugs by default unless `MARKET_BRAIN_STRICT_UNKNOWN=true`.
- optionally attach `CryptoSignalFeed`, which stores Coinbase spot samples for
  BTC/ETH/SOL/XRP and exposes 30s/60s/180s changes as evidence features.
- expose `evaluate_exit()` for take-profit, trailing-stop, stop-loss, timeout,
  and hold decisions.
- `PositionManager` now uses `evaluate_exit()` and `ExitExecutor.sell_fak()`.
  A close is only recorded when the CLOB response is `matched` or `filled`.
  Responses such as `live`, `delayed`, `unmatched`, `rejected`, or exceptions
  are written as `close_failed` and can be retried.
- `ScalperEngine` now also routes one-leg exits through `evaluate_exit()`.
  Static expiry protection remains local and has priority, but TP/SL/trailing/
  timeout decisions are journaled as exit brain decisions.

Smart exit behavior:

- stop-loss, trailing-stop-after-profit, timeout, and expiry-risk exits are
  never overridden;
- when a crypto 15m position reaches the configured take-profit, the brain may
  return `hold_profit_with_momentum` instead of selling immediately;
- that hold is only allowed when Coinbase momentum supports the held side,
  the position is still near its peak, and there is enough time before expiry;
- every sell/hold exit decision is written to `brain_decisions` with action
  `SELL` or `HOLD_<SIDE>`.

Current journal table:

- `brain_decisions`
- fields include agent, strategy, decision_type, market_id, token_id, approved,
  reason, score, market_type, asset, features_json, action, and optional outcome.

## Environment

```env
MARKET_BRAIN_ENABLED="true"
MARKET_BRAIN_STRICT_UNKNOWN="true"
MARKET_BRAIN_SCALPER_MIN_SECONDS_TO_EXPIRY="90"
MARKET_BRAIN_SCALPER_MAX_ENTRY_PRICE="0.55"
MARKET_BRAIN_SCALPER_MAX_PAIR_ASK_SUM="1.04"
MARKET_BRAIN_SCALPER_MIN_EDGE_SCORE="0.35"
MARKET_BRAIN_EXIT_TAKE_PROFIT_PCT="0.05"
MARKET_BRAIN_EXIT_TRAILING_STOP_PCT="0.02"
MARKET_BRAIN_EXIT_STOP_LOSS_PCT="0.07"
MARKET_BRAIN_EXIT_MAX_HOLD_SECONDS="1800"
MARKET_BRAIN_SMART_EXIT_ENABLED="true"
MARKET_BRAIN_SMART_EXIT_MIN_PROFIT_PCT="0.05"
MARKET_BRAIN_SMART_EXIT_MOMENTUM_WINDOW="60s"
MARKET_BRAIN_SMART_EXIT_MIN_MOMENTUM_PCT="0.001"
MARKET_BRAIN_SMART_EXIT_PEAK_DRAWDOWN_HOLD_PCT="0.006"
MARKET_BRAIN_SMART_EXIT_MIN_SECONDS_TO_EXPIRY="75"
MARKET_BRAIN_CRYPTO_MIN_SAMPLES="2"
```

## What This Is Not Yet

This is not yet a full real-time intelligence system. It does not yet consume:

- RSS/news materiality as a live trading veto;
- sports score/time feeds;
- official macro calendars;
- cross-market/Kalshi pricing.

Those should feed into the same `MarketBrain` rather than being embedded
separately inside every agent.

## Next Priority

1. Before any live restart, run the live preflight:
   - wallet position count from data API / CTF balances;
   - CLOB open orders must be zero;
   - approvals verified;
   - `EXECUTE_MAINTAIN=false` shadow cycle confirms decisions;
   - then flip only `position_manager` live before entry agents.

2. Use crypto external-price evidence inside `MarketBrain`:
   - Coinbase public feed per asset is available;
   - 30s/60s/180s move is captured;
   - next step is to calibrate how much it should influence approval/veto;
   - volatility regime;
   - Polymarket move vs underlying move.

3. Promote `news_signal` from analytics to veto-only:
   - never auto-buy from RSS alone;
   - allow it to block `nothing_happens` and LLM entries when fresh material news
     contradicts the thesis.

4. Dashboard:
   - show latest brain approvals/vetoes;
   - show veto reason counts by agent;
   - show MFE after approved entries.

## Operating Rule

An agent may only risk live capital when both are true:

1. `MarketBrain` approves the entry.
2. A working exit monitor is already attached.

If either condition is false, the agent can only run in shadow mode.

## 2026-05-07 Live Guardrail Update

The first guarded live restart uses strict unknown-market rejection. This matters
because Gamma currently returns additional crypto markets such as DOGE, BNB, and
HYPE alongside BTC/ETH/SOL/XRP. Until each asset has an explicit brain profile
and external signal support, unknown assets must be vetoed rather than traded.

Current live allocation posture:

- Total live scalper reserve: `$20.00`.
- Per-leg entry size: `$2.50`.
- Maximum scalper attempts: `4` per hour.
- Take profit: `+5%`.
- Trailing stop after profit: `2%`.
- Stop loss: `-7%`.
- Resting open orders target: `0`.

At the 2026-05-07 11:05 UTC checkpoint, the scalper was healthy and polling live
books, but it had not opened a new live position yet. CLOB open orders were `0`
and wallet USDC balance was `62.922603`.

At the 2026-05-07 16:00 UTC checkpoint, smart exit was connected to both
`position_manager` and `scalper`. Focused verification passed:

```text
python3 -m unittest tests.test_scalper_engine tests.test_market_brain tests.test_capital_allocator -v
Ran 39 tests in 2.093s — OK
```

Pre-restart live checks:

- deposit wallet balance: `$62.137265`;
- CLOB open orders: `0`;
- latest live-cycle scalper pairs: tracking only, no active `LEG1_FILLED`
  exposure. Older `RECONCILE_NEEDED` journal rows remain for operator review.

`scalper` and `position_manager` were rebuilt and restarted with the new image.

The older `RECONCILE_NEEDED` rows were then reconciled:

- `sol-updown-15m-1778087700`;
- `xrp-updown-15m-1778087700`;
- `sol-updown-15m-1778088600`.

Gamma reported all three as resolved with `Up=0`, `Down=1`. The wallet still
held Up CTF balances, which are losing tokens. They were journaled as
`scalper_reconciled_lost` and moved to terminal `expired` state. DB backup:
`data/trade_log.before_reconcile_2026-05-07.db`.

## 2026-05-07 24h Live Allocation Update

The approved live experiment budget is `$20` total, split across approved
agents only:

- `scalper`: `$14` reserve, live, `$2.50` legs;
- `btc_daily`: `$6` reserve, live, `$3.00` positions;
- `position_manager`: live exit-only;
- `swarm`: `$0` reserve until live-clean;
- `trader`: shadow only.

`position_manager` now treats real `btc_daily_open` rows as managed open
positions and ignores old shadow rows. This gives `btc_daily` a real exit path
through the shared FAK SELL executor.

Runtime check after restart:

- `btc_daily` started with `execute=True`;
- `RiskGate.reason()` returned `None`;
- total reserves were exactly `$20`;
- wallet balance was `$61.736726`;
- CLOB open orders were `0`.

## Live Restart Sequence

1. Keep entry agents stopped.
2. Start dashboard and reconcile wallet state.
3. Run `position_manager` with `EXECUTE_MAINTAIN=false` for one cycle.
4. If decisions look correct, flip `EXECUTE_MAINTAIN=true` and run only the
   exit manager.
5. Start one entry agent in shadow.
6. Start one entry agent live with small size only after exits are observed.
