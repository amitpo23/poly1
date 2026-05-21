# poly1 — Specification (v0.8.0-meta-brain)

## 1. Purpose

Autonomous Polymarket trading bot. An LLM pipeline scores binary prediction
markets, and a daemon executes market orders within configurable risk limits.
Targeted use case: $50–$200 capital running 24/7 on a single VPS.

Active operating goal:

- `/goal`: make every approved agent evidence-profitable before scalable
  capital allocation. See `docs/GOAL_PROFITABLE_AGENT_LOOP.md`.
- Track the loop with `python scripts/python/goal_status.py --hours 24`.
- The loop remains open until every approved agent is `profitable` or
  intentionally disabled/unfunded.

## 2. High-level architecture

```
                   ┌─────────────────────────┐
                   │  TraderDaemon (cron.py) │
                   │  while not stopping:    │
                   │    Trader.sweep()       │
                   │    heartbeat + ping     │
                   │    sleep(poll_seconds)  │
                   └────────────┬────────────┘
                                │
                                ▼
                   ┌─────────────────────────┐
                   │  Trader.sweep (trade.py)│
                   │  1. RiskGate.ok()       │
                   │  2. fetch events        │
                   │  3. RAG filter events   │
                   │  4. map → markets       │
                   │  5. RAG filter markets  │
                   │  6. rank, take top_n    │
                   │  7. for each market:    │
                   │     RiskGate.ok()       │
                   │     dedupe check        │
                   │     LLM forecast+trade  │
                   │     min_confidence gate │
                   │     execute or skip     │
                   │     log to TradeLog     │
                   └────────────┬────────────┘
                                │
              ┌─────────────────┼──────────────────┐
              ▼                 ▼                  ▼
       ┌────────────┐   ┌────────────────┐   ┌──────────────┐
       │ RiskGate   │   │ Polymarket     │   │ TradeLog     │
       │ env-driven │   │ CLOB market    │   │ SQLite       │
       │ gates      │   │ orders + reads │   │ ledger       │
       └────────────┘   └────────────────┘   └──────────────┘
                                │
                                ▼
                  Polygon RPC + Polymarket CLOB API
```

## 3. Modules

### Core infrastructure

| Module | Responsibility |
|---|---|
| `agents/application/cron.py` `TraderDaemon` | Long-running loop, SIGTERM-aware, heartbeat + Healthchecks ping |
| `agents/application/trade.py` `Trader` | Per-cycle sweep, top-N ranking, integration of all gates |
| `agents/application/trade_log.py` `TradeLog` | SQLite ledger (idempotency, dedupe, recovery) |
| `agents/application/risk_gate.py` `RiskGate` | Pre-trade kill switch, drawdown, rate limit, token-cost cap |
| `agents/application/executor.py` `Executor` | LLM pipeline (filter → forecast → trade rec), token-cost tracking |
| `agents/application/prompts.py` `Prompter` | Versioned prompts; defines BUY/SELL semantics |
| `agents/application/trade_recommendation.py` | Parses LLM JSON/legacy output → `TradeRecommendation` |
| `agents/application/execution_safety.py` | Exitable-size gate; shared by all live entry agents |
| `agents/application/meta_brain.py` `MetaBrain` | Single synthesizing layer: wraps MarketBrain + WinRateAdvisor + ConvictionJSONLReader + ProbVelocityDetector → unified `MetaDecision` with `entry_timing` (now/wait/skip) |
| `agents/application/meta_brain.py` `WinRateAdvisor` | Rolling win-rate from `brain_decisions` → `trades` SQLite (7-day window, 5-min cache) |
| `agents/application/meta_brain.py` `ConvictionJSONLReader` | Reads external_conviction JSONL output per market_id / question keywords; aggregates direction+confidence; 2-min cache |
| `agents/application/meta_brain.py` `ProbVelocityDetector` | Probability velocity from in-memory samples + `position_marks` table; classifies rising/falling/stable |
| `agents/application/market_brain.py` `MarketBrain` | Pre-LLM veto/scoring layer; spread, horizon, Tavily context, cross-market signal gates |
| `agents/application/market_brain.py` `CrossMarketSignalFeed` | Queries Kalshi + Metaculus + Manifold in parallel for consensus probability; integrated into `evaluate_general_entry` score |
| `agents/application/market_brain.py` `CoinGeckoFeed` | CoinGecko free-tier crypto price+24h-change feed (10-min cache) |
| `agents/application/market_brain.py` `CryptoSignalFeed` | Live crypto price feed: Binance primary, Coinbase fallback |
| `agents/polymarket/polymarket.py` `Polymarket` | CLOB client wrapper, side→token mapping, balance reads |
| `agents/polymarket/gamma.py` `GammaMarketClient` | Gamma REST reads (events, markets) |
| `agents/connectors/chroma.py` `PolymarketRAG` | Local Chroma vector store for RAG filtering |
| `agents/application/tavily.py` | Shared stdlib-only Tavily search helper (`tavily_headlines`, `tavily_confidence`) |
| `agents/utils/objects.py` | Pydantic data classes (`TradeRecommendation`, `SimpleMarket`, …) |
| `agents/utils/logging_setup.py` | JSON formatter, RotatingFileHandler |
| `agents/utils/notify.py` | Telegram (non-blocking) + Healthchecks ping |
| `deploy/run.py` | Container entrypoint, env validation, daemon start |

### Trading agents (entry)

| Module | Strategy | Profile |
|---|---|---|
| `agents/application/trade.py` `Trader` | LLM psychological-bias exploitation on general binary markets | (default) |
| `agents/application/btc_daily.py` `BtcDailyAgent` | Mean-revert BTC 24h markets after >3% crowd overreaction | `btc_daily` |
| `agents/application/near_resolution.py` `NearResolutionEngine` | Resolution-bias exploitation in markets closing in 0.5h–24h | `near_resolution` |
| `agents/application/news_shock.py` `NewsShockEngine` | News-driven entry before crowd re-prices on material news | `news_shock` |
| `agents/application/wallet_follow.py` `WalletFollowEngine` | Copy-trading — mirror proven whale wallets | `wallet` |
| `agents/application/scalper.py` `ScalperEngine` | Math-spread arb on crypto 15-min UP/DOWN pairs | `scalper` |
| `agents/application/external_conviction.py` `ExternalConvictionAgent` | Multi-source conviction aggregator (11+ providers) | `external_conviction` |
| `agents/application/btc_5min.py` `Btc5MinEngine` | Multi-signal consensus on BTC 5-min up/down markets | `btc_5min` |

### Control & support agents (no entry)

| Module | Responsibility | Profile |
|---|---|---|
| `agents/application/market_scanner.py` `MarketScanner` | Proactive 5-min opportunity finder; routes to entry agents via DB | `scanner` |
| `agents/application/position_manager.py` `PositionManager` | Exit logic for all open positions (TP/SL/timeout) | `positions` |
| `agents/application/trading_supervisor.py` `TradingSupervisor` | Safety control-plane; enforces HALT on exit-path failures | `supervisor` |
| `agents/application/settlement_reconciler.py` | On-chain reconciliation; resolves P&L and detects stuck positions | `settlement` |
| `agents/application/news_signal.py` | Dry-run news classification (LLM → news_signals DB rows) | `news_shock` |
| `agents/application/wallet_watcher.py` | Polls whale wallets; writes wallet_signals DB rows | `wallet` |
| `agents/application/capital_allocator.py` | Read-only allocation scoring across agents | `allocator` |
| `agents/application/scalper_pairs.py` `ScalperPairsDAO` | `scalper_pairs` table CRUD for scalper | `scalper` |

## 4. Data flow per cycle

1. `TraderDaemon` writes `data/heartbeat`, calls `Trader.one_best_trade_sweep`.
2. `RiskGate.ok()` — kill switch, balance floor, drawdown, rate limit, token cost.
3. `pre_trade_logic` — refresh Chroma every 24h (not per cycle).
4. `Polymarket.get_all_tradeable_events` (gamma REST).
5. `Executor.filter_events_with_rag` (LLM-anchored Chroma retrieval).
6. `Executor.map_filtered_events_to_markets` (gamma per-market REST).
7. `Executor.filter_markets` (Chroma).
8. `Trader._rank_markets` (chroma score asc, then -spread asc).
9. For each market in `top_n`:
   - `RiskGate.ok()` (re-check between markets).
   - `TradeLog.has_active_trade_for_market` — 6h dedupe.
   - `Executor.source_best_trade` → 2 LLM calls (superforecaster + one_best_trade).
   - `Executor.parse_trade_recommendation` → `TradeRecommendation`.
   - If `min_confidence > 0` and `confidence` missing or below: `SKIPPED_GATE`.
   - Compute `amount_usdc = min(size_fraction, max_position_fraction) * balance`.
   - If dry-run: `SKIPPED_DRY_RUN` row, continue.
   - Else: `insert_pending` → `Polymarket.execute_market_order` → mark `SUBMITTED`/`FILLED`/`FAILED`.

## 5. Side & token semantics (critical)

Convention encoded jointly in `prompts.py:one_best_trade` and
`polymarket.py:execute_market_order`:

- `outcomes[0]` is the "primary" outcome (typically YES). The LLM anchors
  `price` to this outcome.
- `side="BUY"` → buy `token_ids[0]` at `recommendation.price`.
- `side="SELL"` → buy `token_ids[1]` at `1.0 - recommendation.price`
  (CLOB has no SELL primitive for market orders; sell of YES = buy of NO).
- A sanity warning logs if `recommendation.price` is closer to
  `outcome_prices[1]` than `outcome_prices[0]`, which suggests the LLM
  anchored to the wrong outcome.

Non-binary markets (`len(outcomes) != 2`) raise `ValueError` and the trade
is logged as `FAILED`.

## 6. Persistence

### `data/trade_log.db` — SQLite

```sql
CREATE TABLE trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,                      -- ISO-8601 UTC
  cycle_id TEXT NOT NULL,                -- one UUID per sweep
  market_id TEXT NOT NULL,
  token_id TEXT,                         -- which CLOB token was/would be traded
  side TEXT,                             -- BUY|SELL (LLM-recommended)
  price REAL,                            -- recommendation.price (anchored to outcomes[0])
  size_usdc REAL,                        -- usdc amount sent to MarketOrderArgs
  confidence REAL,
  status TEXT NOT NULL,                  -- see Status enum
  response_json TEXT,                    -- raw exchange response, JSON-serialized
  error TEXT
);
CREATE INDEX idx_market_status_ts ON trades(market_id, status, ts);
CREATE INDEX idx_status_ts ON trades(status, ts);
```

#### Status enum

| Status | Meaning |
|---|---|
| `pending` | Inserted before submit; window during which a crash could leave the row stranded. |
| `submitted` | `post_order` returned. May or may not be filled. |
| `filled` | Exchange confirmed fill. |
| `failed` | `execute_market_order` raised; no order on exchange. |
| `may_have_fired` | Stranded `pending` recovered on startup. **Manual on-chain check required before re-trading the market.** |
| `skipped_dedupe` | Same market is currently blocked by an active row (see below). |
| `skipped_gate` | Confidence or other guard failed. |
| `skipped_dry_run` | `EXECUTE=false`; the row records what would have been sent. |
| `btc_5min_open` | BTC 5-min agent entry fill; auto-resolves after 5 min. |

**Dedupe rules (`has_active_trade_for_market`):**
- `TIME_BOUNDED_ACTIVE_STATUSES = (pending, submitted, filled)` — block
  re-trading the same market only if a row exists **within the dedupe window**
  (default 6h).
- `UNBOUNDED_BLOCKING_STATUSES = (may_have_fired,)` — block **forever**,
  regardless of age. The order may have actually executed on-chain even
  though we never recorded the response; the operator must verify on-chain
  and either delete the row or insert a manual reconciliation record before
  the bot will re-trade that market.
- `ACTIVE_STATUSES = TIME_BOUNDED_ACTIVE_STATUSES + UNBOUNDED_BLOCKING_STATUSES`
  is the union, kept for callers that just need "is this market blocked."

**Re-entry after exit (`has_filled_position_for_market`):**  
Returns `True` only if a `filled` row exists with `id > MAX(id)` of any
terminal close row (`closed_*`, `resolved_*`) for that market. A
terminal row written after the last fill means the position has been
exited and the market is open for re-entry. This prevents the old
behaviour where stale fills from shadow-mode sessions blocked markets
indefinitely after position_manager had already closed them on-chain.
(Fixed 2026-05-12; previously any historical fill blocked forever.)

**Open-position aggregation (`filled_positions*`):**
Position-manager and risk-gate open rows are scoped to rows after the latest
terminal close/resolution row for that `token_id`. Re-entering the same token
after a prior `closed_dust`, `closed_take_profit`, `closed_stop_loss`,
`closed_timeout`, or `resolved_*` row must produce a fresh managed position.
Old terminal rows must not suppress new entries, and old pre-close fills must
not inflate MTM/deployed-capital accounting.

### `data/llm_usage.jsonl`

One JSON object per LLM call; consumed by `RiskGate.daily_token_usd`:

```json
{"ts": "...", "tag": "one_best_trade", "model": "gpt-4o-mini",
 "prompt_tokens": 1234, "completion_tokens": 56, "est_cost_usd": 0.001234}
```

### `data/heartbeat`

Empty file; mtime is updated each cycle and during sleep. Docker
`HEALTHCHECK` reads mtime and fails if older than 600 seconds.

### `data/HALT`

Operator-controlled kill switch. Path configured by `KILL_SWITCH_FILE`.
While the file exists, `RiskGate.ok()` returns `False` and no trades are
attempted. `touch /srv/poly1/data/HALT` to halt; `rm` to resume.

### Runtime control plane

Trading mode is controlled through `scripts/runtime_control.py`, not by manual
`.env` edits. The script writes `deploy/.env.runtime` and
`data/runtime_control.json`.

Docker Compose loads `.env` first and `deploy/.env.runtime` second, so the
generated runtime profile overrides local defaults without exposing secrets.
`RiskGate` reads `data/runtime_control.json` on every entry check and blocks
unless:

- mode is trade-enabled (`paper`, `live_probe`, or `live`);
- `RUNTIME_AGENT` is listed in `allowed_live_agents`;
- container `RUNTIME_CONFIG_HASH` matches the current control file hash.

Freeze mode must be entered with:

```bash
.venv/bin/python scripts/runtime_control.py freeze
```

A live probe must be generated for exactly one approved entry agent:

```bash
.venv/bin/python scripts/runtime_control.py live-probe --agent btc_daily --budget 5
```

Pass `--arm` only after human approval; it removes `data/HALT`.

### Live stabilization preflight

Before enabling any live entry agent, run:

```bash
.venv/bin/python scripts/trading_stability_preflight.py --mode live
```

The preflight is dependency-light and checks `.env`,
`deploy/.env.runtime`, `data/runtime_control.json`, and `data/trade_log.db`
for the minimum live-readiness contract:

- entry agents and allocator enforcement are frozen during stabilization;
- runtime control mode and config hash match the intended mode;
- `position_manager` is live exit-only;
- `trading_supervisor` can enforce `HALT`;
- no HALT file is already present;
- open journal positions have `position_marks` and `position_manager` exit
  `brain_decisions`;
- settlement reconciliation has no critical action rows.

Any `blocked` result is a no-trade condition. See
`docs/LIVE_STABILIZATION_RUNBOOK_2026-05-12.md`.

During stabilization freeze, `data/HALT` should exist and the expected check is:

```bash
.venv/bin/python scripts/trading_stability_preflight.py --mode freeze
```

### `data/logs/poly1.log`

JSON-formatted application log, rotated at 10 MB × 5 files.

### `local_db_events/`, `local_db_markets/`

Chroma persistent stores. Refreshed once per 24 h or on `--refresh-dbs`.

## 7. Configuration (env vars)

### Required

| Var | Purpose |
|---|---|
| `POLYGON_WALLET_PRIVATE_KEY` | EOA private key used to sign CLOB orders and (in EOA mode) hold collateral |
| `OPENAI_API_KEY` | LLM (ChatOpenAI) and embeddings (Chroma) |
| `POLYMARKET_FUNDER` | Proxy address from `polymarket.com/settings`. **Required for Privy/Magic accounts** (Google/email login) — switches the bot to POLY_PROXY signing mode (`signature_type=1`), uses pUSD as collateral (`0xC011…2DFB`), reads balance from the proxy, and skips `_init_approvals` (Polymarket auto-allows on proxy deployment). Leave blank for classic EOA wallets (MetaMask). |
| `POLYMARKET_DEPOSIT_WALLET` | Deposit wallet address for current CLOB v2 API trading. When set, the bot uses this as funder/balance holder and defaults to `signature_type=3`. |
| `POLYMARKET_SIGNATURE_TYPE` | Optional override. Use `3` for deposit wallet (`POLY_1271`), `1` for legacy Magic/proxy, or leave blank to infer from wallet env. |
| `POLYMARKET_CLOB_API_KEY` / `POLYMARKET_CLOB_API_SECRET` / `POLYMARKET_CLOB_API_PASSPHRASE` | Optional persisted CLOB L2 credentials. If blank, the bot derives credentials from `POLYGON_WALLET_PRIVATE_KEY` at startup. |
| `POLYMARKET_BUILDER_CODE` / `POLYMARKET_BUILDER_ADDRESS` | Optional builder attribution config for CLOB v2 orders. |
| `BUILDER_API_KEY` / `BUILDER_SECRET` / `BUILDER_PASS_PHRASE` | Builder relayer credentials needed by `scripts/python/setup_deposit_wallet.py` to deploy/fund/approve the deposit wallet. |

### Trader runtime

| Var | Default | Notes |
|---|---|---|
| `EXECUTE` | `false` | When `false`, daemon runs as dry-run; rows logged `skipped_dry_run` |
| `OPENAI_MODEL` | `gpt-3.5-turbo-16k` | Recommended: `gpt-4o-mini` for cost; `gpt-4o` for quality |
| `CYCLE_SECONDS` | `1800` | Time between sweeps |
| `CYCLE_JITTER_SECONDS` | `30` | Random additional wait |
| `TOP_N` | `3` | Markets evaluated per cycle |
| `MAX_TRADES_PER_CYCLE` | `2` | Cap regardless of confidence |
| `MAX_POSITION_FRACTION` | `0.05` | Cap on `size_fraction × balance` |
| `MIN_CONFIDENCE` | `0.60` | If > 0, missing confidence also blocks |

### Risk gates

| Var | Default | Notes |
|---|---|---|
| `STARTING_BALANCE_USDC` | `0` | If `> 0`, used for daily-loss check |
| `MAX_DAILY_LOSS_PCT` | `0.10` | Drawdown vs `STARTING_BALANCE_USDC` |
| `MAX_TRADES_PER_HOUR` | `4` | Counts SUBMITTED + FILLED |
| `MIN_USDC_FLOOR` | `10.0` | Halt if balance below |
| `MAX_DAILY_TOKEN_USD` | `5.0` | Halt if 24h LLM cost exceeds |
| `KILL_SWITCH_FILE` | `./data/HALT` | Operator override |
| `POLYMARKET_MAX_SLIPPAGE` | `0.03` | FOK market-buy slippage tolerance. Rejects if live ask exceeds model price by more than this fraction. |
| `POLYMARKET_MIN_ORDER_USDC` | `1.0` | Skip orders smaller than this USDC amount. |
| `MIN_ENTRY_PRICE` | `0.10` | Skip penny tokens with best ask below this price. Prevents high round-trip spread losses. |
| `MIN_BID_DEPTH_USDC` | `20.0` | Require at least this much bid-side USDC depth before entering. Ensures exit liquidity exists. |
| `MAX_ENTRY_SPREAD_PCT` | `0.05` | Reject market entry when bid-ask spread exceeds this fraction (5%). |
| `POLYGON_RPC` | `https://polygon.drpc.org` | Polygon RPC endpoint. Override with paid Alchemy/Infura key for production. |
| `MAINTAIN_TAKE_PROFIT_PCT` | `0.25` | Hard profit cap. The brain may exit earlier but may not hold past this cap. |
| `MAINTAIN_SOFT_STOP_LOSS_PCT` | `0.03` | Soft stop: force immediate brain review, not blind selling. |
| `MAINTAIN_STOP_LOSS_PCT` | `0.06` | Hard stop guardrail. |
| `MAINTAIN_MAX_HOLD_HOURS` | `6` | Safety ceiling. Brain may extend only within bounded extension controls. |
| `MAINTAIN_BRAIN_EXIT_AUTHORITY_ENABLED` | `true` | Allows the brain to hold through regular TP/timeout when confidence is strong. |
| `MAINTAIN_BRAIN_HOLD_OVERRIDE_CONFIDENCE` | `0.65` | Confidence needed to hold through regular profit-taking. |
| `MAINTAIN_BRAIN_EXTEND_HOLD_CONFIDENCE` | `0.75` | Confidence needed to extend an ordinary timeout. |
| `MAINTAIN_BRAIN_MAX_HOLD_EXTENSION_HOURS` | `1.0` | Maximum additional hold time beyond `MAINTAIN_MAX_HOLD_HOURS`. |
| `BTC_DAILY_MAX_SLIPPAGE_SKIPS` | `3` | btc_daily: give up on a market after N consecutive slippage failures in one daemon run. Reset on successful entry. Prevents the 58-attempt tight-loop seen on market 2214715 (2026-05-11). |
| `MIN_ENTRY_PRICE` | `0.10` | Skip penny tokens with best ask below this price. Prevents high round-trip spread losses. |
| `MIN_BID_DEPTH_USDC` | `20.0` | Require at least this much bid-side USDC depth before entering. Ensures exit liquidity exists. |
| `MAX_ENTRY_SPREAD_PCT` | `0.05` | Reject market entry when bid-ask spread exceeds this fraction (5%). |
| `MARKET_BRAIN_TIMEOUT_FLAT_GRACE_PCT` | `0.01` | If position P&L is within ±1% at timeout, grant grace period instead of force-selling at spread cost. |
| `MARKET_BRAIN_TIMEOUT_GRACE_SECONDS` | `3600` | Extra hold time (seconds) granted to flat positions at timeout. |
| `REENTRY_COOLDOWN_HOURS` | `12` | Block re-entry on a market for N hours after a terminal close (timeout/SL/TP/dust). Prevents buy→close→re-buy spread drain. |
| `MAX_FILLS_PER_MARKET_24H` | `3` | Max filled entries on one market in 24h across all agents. Prevents endless re-entry after dedupe window expires. |
| `MIN_EXIT_BID_DEPTH_USDC` | `5.0` | Pre-exit bid depth check: defer non-stop-loss exits when bid-side depth is below this. stop_loss always attempts. |

### Trading supervisor

`agents/application/trading_supervisor.py` is a control-plane safety daemon,
not a trading strategy. It checks that every open journal position is being
managed by `position_manager`. If the exit path goes stale while capital is
open, it writes `KILL_SWITCH_FILE` so entry agents stop opening new risk.

| Var | Default | Notes |
|---|---|---|
| `TRADING_SUPERVISOR_POLL_SEC` | `60` | Supervisor loop cadence. |
| `TRADING_SUPERVISOR_ENFORCE_HALT` | `true` | When true, critical exit-path failures write `KILL_SWITCH_FILE`. |
| `TRADING_SUPERVISOR_EVAL_GRACE_SEC` | `180` | Max age for position-manager `brain_decisions` / `position_marks` on open positions. |
| `TRADING_SUPERVISOR_STALE_HEARTBEAT_SEC` | `180` | Max acceptable age for `position_manager` heartbeat when open positions exist. |
| `TRADING_SUPERVISOR_MIN_POSITION_AGE_SEC` | `45` | Grace period after a new fill before requiring the first exit evaluation. |
| `TRADING_SUPERVISOR_CLOSE_FAILED_WINDOW_MIN` | `15` | Rolling window for close-failure storm detection. |
| `TRADING_SUPERVISOR_CLOSE_FAILED_THRESHOLD` | `5` | Critical if recent `close_failed` rows exceed this count. |
| `TRADING_SUPERVISOR_SETTLEMENT_MAX_AGE_MIN` | `15` | Max age for settlement rows that can trip supervisor control. |
| `TRADING_SUPERVISOR_HEARTBEAT_PATH` | `/app/data/trading_supervisor_heartbeat` | Supervisor healthcheck heartbeat. |
| `TRADING_SUPERVISOR_STATE_PATH` | `/app/data/trading_supervisor_status.json` | Latest supervisor state for dashboard/ops. |
| `TRADING_SUPERVISOR_POSITION_MANAGER_HEARTBEAT` | `/app/data/position_manager_heartbeat` | Heartbeat file the supervisor watches. |

### Settlement reconciler

`agents/application/settlement_reconciler.py` reconciles journal positions
against market state and, when available, on-chain CTF balances. It classifies
positions that are no longer a normal stop-loss problem: resolved losses,
redeemable winners, dust below recovery threshold, and active positions that
are not being managed by the exit loop.

It writes one row per token to `settlement_reconciliation` with:
`status`, `action`, cost basis, journal shares, on-chain shares, bid/ask,
recoverable value, redeemable value, gas estimate, and diagnostic JSON.

Important statuses:

| Status | Meaning | Supervisor action |
|---|---|---|
| `active_managed` | Market is live and exit evidence is fresh. | No halt. |
| `active_unmanaged` | Market is live/recoverable but exit evidence is missing or stale. | Critical halt. |
| `redeemable` | Market resolved in our favor and on-chain shares remain. | Critical halt until redeemed/handled. |
| `lost_final` | Market resolved against us. | Record loss, no sell retry. |
| `dust_unrecoverable` | Live bid exists but recoverable value is below threshold/gas. | No sell retry. |
| `resolved_won_no_balance` | Market won, but on-chain shares are already gone/dust. | Verify redeemed. |
| `reconcile_error` | Reconciler could not classify. | Critical halt. |

| Var | Default | Notes |
|---|---|---|
| `SETTLEMENT_RECONCILER_ENABLED` | `true` | Disable only for maintenance/testing. |
| `SETTLEMENT_RECONCILER_POLL_SEC` | `300` | Reconciliation cadence. |
| `SETTLEMENT_RECONCILER_HEARTBEAT_PATH` | `/app/data/settlement_reconciler_heartbeat` | Healthcheck heartbeat. |
| `SETTLEMENT_MIN_RECOVERABLE_USDC` | `1.0` | Below this, classify as dust/unrecoverable rather than trying to sell. |
| `SETTLEMENT_GAS_ESTIMATE_USDC` | `0.05` | Recovery economics threshold helper. |
| `SETTLEMENT_REDEEMABLE_SHARES_FLOOR` | `0.5` | Minimum winning shares before marking as redeemable. |
| `SETTLEMENT_ON_CHAIN_DUST_FLOOR` | `0.5` | Dust threshold for balances. |
| `SETTLEMENT_EXIT_EVIDENCE_GRACE_SEC` | `240` | Freshness window for `position_marks` and exit decisions. |
| `SETTLEMENT_REQUIRE_EXIT_EVIDENCE` | `true` | Require position-manager evidence before classifying live positions as managed. |

### Persistence

| Var | Default |
|---|---|
| `TRADE_LOG_DB` | `./data/trade_log.db` |
| `LLM_USAGE_FILE` | `./data/llm_usage.jsonl` |
| `EXTERNAL_CONVICTION_OUTPUT_PATH` | `./data/external_convictions.jsonl` |
| `LOG_DIR` | `./data/logs` |
| `LOG_LEVEL` | `INFO` |
| `HEARTBEAT_PATH` | `./data/heartbeat` |

### Notifications

| Var | Notes |
|---|---|
| `TG_BOT_TOKEN`, `TG_CHAT_ID` | If both set, lifecycle + trade events sent to Telegram (non-blocking) |
| `HEALTHCHECK_URL` | If set, GET'd at end of each successful cycle |

### Dashboard swarm mirror

| Var | Default | Notes |
|---|---|---|
| `SWARM_DB` | `~/Desktop/poly/bot/data/swarm.db` | Read-only path used by dashboard Swarm tab. |
| `SWARM_AGENT_ALLOCATIONS_JSON` | `{"market_maker":5,...}` | Dashboard-only per-agent budget map (USD) for Swarm tab summary. |

### Allocator-sync daemon

| Var | Default | Notes |
|---|---|---|
| `ALLOC_SYNC_CYCLE_SEC` | `300` | Daemon loop interval in seconds. |
| `ALLOC_SYNC_BUDGET_USDC` | `20.0` | Hard cap — daemon never allocates beyond this total. |
| `ALLOC_SYNC_WINDOW_HOURS` | `24.0` | Scoring lookback window passed to `CapitalAllocator`. |
| `ALLOC_SYNC_ENFORCE` | `true` | `false` = log-only dry run; no writes or restarts. |
| `ALLOC_SYNC_MIN_DELTA_USDC` | `0.50` | Min per-agent USDC shift to trigger env write + container restart. Prevents thrashing on tiny allocation changes. |
| `SWARM_ENV_PATH` | `/Users/mymac/Desktop/poly/bot/.env` | Host path to swarm `.env`; bind-mounted into the allocator-sync container. |

### Near-resolution agent

| Var | Default | Notes |
|---|---|---|
| `NEAR_RESOLUTION_MIN_HOURS` | `0.5` | Lower bound: markets closing in less than this are too close to act. |
| `NEAR_RESOLUTION_MAX_HOURS` | `336.0` | Two-week window; raised 2026-05-12 because active Polymarket binary markets were resolving >280h from scan time and the original 36h window filtered everything. |
| `NEAR_RESOLUTION_MAX_ENTRY_PRICE` | `0.15` | Buy only the cheap side when priced at or below this. |
| `NEAR_RESOLUTION_MIN_LIQUIDITY` | `500.0` | Lowered 2026-05-12; most viable near-resolution markets had <$3000 `volumeClob`. |
| `NEAR_RESOLUTION_MIN_CONFIDENCE` | `0.65` | Tavily news-search confidence threshold before entering. |
| `NEAR_RESOLUTION_POSITION_SIZE_USDC` | `2.5` | Per-trade size. |
| `NEAR_RESOLUTION_RESERVE_USDC` | `15.0` | Capital ring-fenced for this agent. |
| `NEAR_RESOLUTION_POLL_SEC` | `60` | Scan cadence. |
| `NEAR_RESOLUTION_MAX_OPEN` | `3` | Max concurrent open positions. |
| `NEAR_RESOLUTION_HEARTBEAT_PATH` | `/app/data/near_resolution_heartbeat` | Heartbeat file path. |
| `EXECUTE_NEAR_RESOLUTION` | `false` | Set `true` to live-trade. |

### External conviction agent

`agents/application/external_conviction.py` is a shadow-only research loop. It
scans active liquid Polymarket markets, asks an external analysis provider for a
short-horizon opinion, and writes a trade plan to
`data/external_convictions.jsonl` plus `brain_decisions`. It never places
orders; external tools are treated as opinions, not execution authority.

Provider modes:

- `heuristic` (default): local conservative placeholder for dry calibration.
- `public_news`: no-key public RSS/news search evidence for current narrative
  attention. This is live external data, but still not an oracle.
- `tavily`: uses `TAVILY_API_KEY` search results as external news/narrative
  evidence.
- `http_json`: POSTs a market snapshot to `EXTERNAL_CONVICTION_API_URL`, which
  can wrap Kaito, Santiment, Glassnode, CryptoQuant, or a browser automation
  analyzer.
- `polifly_browser`: calls `POLIFLY_BROWSER_BRIDGE_URL`, a local bridge that
  drives a logged-in Polifly Analyzer session after Pro access is active.
- `clob_whale`: fetches 100 recent trades from `data-api.polymarket.com/trades`,
  filters >$5K whale trades, computes buy/sell directional consensus. No key.
- `manifold_divergence` (alias `manifold`): searches Manifold Markets for a
  matching question, signals if probability diverges >10% from Polymarket. No key.
- `metaculus_divergence` (alias `metaculus`): same divergence pattern using the
  Metaculus community prediction median. No key.
- `cross_market`: finds related Polymarket markets by keyword overlap, signals if
  related markets have moved >15% from 0.50. Uses already-fetched Gamma data.
- `kalshi_divergence` (alias `kalshi`): compares Polymarket price with Kalshi
  real-money yes-ask prices. Signals on >10% divergence. No key.
- `whale_consensus` (alias `data_api_whale`): polls top-20 leaderboard wallets
  via `data-api.polymarket.com/positions`, consensus vote on each market. No key.
- `bull_bear_debate` (alias `debate`): 3-call LLM debate (Bull → Bear → Judge)
  using raw OpenAI REST API with `gpt-4o-mini`. ~$0.01/scan. Requires
  `OPENAI_API_KEY`.
- `nansen_smart_money` (alias `nansen`): Nansen API smart-money Polygon CTF
  flows. Requires `NANSEN_API_KEY`; skips gracefully when missing.
- `wallet_master`: Wallet Master API win-rate-weighted whale consensus. Requires
  `WALLET_MASTER_API_KEY`; skips gracefully when missing.
- `polifly_enhanced`: extends `polifly_browser` with retry and `public_news`
  fallback if Polifly bridge is unavailable.
- `aggregator`: runs N sub-providers configured via
  `EXTERNAL_CONVICTION_AGGREGATOR_PROVIDERS` (comma-separated), computes weighted
  majority verdict. Default sub-providers: `manifold,metaculus,kalshi,technical_signal,clob_whale,gdelt,public_news,heuristic`.

| Var | Default | Notes |
|---|---|---|
| `EXTERNAL_CONVICTION_PROVIDER` | `heuristic` | `heuristic`, `public_news`, `tavily`, `http_json`, `polifly_browser`, `clob_whale`, `manifold`, `metaculus`, `cross_market`, `kalshi`, `whale_consensus`, `debate`, `nansen`, `wallet_master`, `polifly_enhanced`, `technical_signal`, `volatility_regime`, `crypto_derivatives`, `multi_factor_rank`, `gdelt`, or `aggregator`. |
| `EXTERNAL_CONVICTION_AGENT_NAME` | `external_conviction` | Brain-decision agent identity. |
| `EXTERNAL_CONVICTION_STRATEGY_NAME` | `event_probability_scalping` | Brain-decision strategy identity. |
| `EXTERNAL_CONVICTION_API_URL` | empty | Optional POST endpoint for external analysis. |
| `EXTERNAL_CONVICTION_API_KEY` | empty | Optional bearer token for `http_json`. |
| `POLIFLY_BROWSER_BRIDGE_URL` | empty | Optional Polifly browser bridge endpoint. |
| `POLIFLY_BROWSER_BRIDGE_API_KEY` | empty | Optional bearer token for the Polifly bridge. |
| `EXTERNAL_CONVICTION_POLL_SEC` | `10800` | Three-hour cadence. |
| `EXTERNAL_CONVICTION_MARKET_LIMIT` | `200` | Active markets fetched per scan. |
| `EXTERNAL_CONVICTION_MAX_CANDIDATES` | `12` | Max shadow plans per scan. |
| `EXTERNAL_CONVICTION_MIN_VOLUME_USDC` | `5000` | Minimum market volume proxy. |
| `EXTERNAL_CONVICTION_MIN_LIQUIDITY_USDC` | `500` | Minimum market liquidity proxy. |
| `EXTERNAL_CONVICTION_MIN_CONFIDENCE` | `0.58` | Minimum provider confidence for `shadow_candidate`. |
| `EXTERNAL_CONVICTION_TAKE_PROFIT_PCT` | `0.10` | Shadow take-profit target. |
| `EXTERNAL_CONVICTION_STOP_LOSS_PCT` | `0.07` | Shadow stop-loss target. |
| `EXTERNAL_CONVICTION_MAX_HOLD_MINUTES` | `60` | Shadow maximum holding window. |
| `EXTERNAL_CONVICTION_HEARTBEAT_PATH` | `/app/data/external_conviction_heartbeat` | Heartbeat file path. |
| `EXTERNAL_CONVICTION_POSITION_SIZE_USDC` | `3.0` | Per-trade size for live execution. |
| `EXTERNAL_CONVICTION_MAX_LIVE_TRADES_PER_CYCLE` | `1` | Max live entries per scan cycle. |
| `EXTERNAL_CONVICTION_MAX_OPEN_POSITIONS` | `1` | Max concurrent open positions. |
| `EXTERNAL_CONVICTION_RESERVE_USDC` | `0` | Capital ring-fenced for this agent's live trades. |
| `EXECUTE_EXTERNAL_CONVICTION` | `false` | Set `true` to enable live order execution. |
| `EXTERNAL_CONVICTION_AGGREGATOR_PROVIDERS` | `clob_whale,manifold,public_news` | Comma-separated sub-provider list for aggregator mode. |
| `EXTERNAL_CONVICTION_DEBATE_MODEL` | `gpt-4o-mini` | LLM model for `bull_bear_debate` provider. |
| `NANSEN_API_KEY` | empty | Nansen smart-money API key. Paid tier 3 provider; skips when missing. |
| `WALLET_MASTER_API_KEY` | empty | Wallet Master API key. Paid tier 3 provider; skips when missing. |
| `EXTERNAL_CONVICTION_VIBE_EMA_SHORT` | `12` | Short EMA period for technical_signal provider. |
| `EXTERNAL_CONVICTION_VIBE_EMA_LONG` | `26` | Long EMA period for technical_signal provider. |
| `EXTERNAL_CONVICTION_VIBE_RSI_PERIOD` | `14` | RSI period for technical_signal provider. |
| `EXTERNAL_CONVICTION_VIBE_RSI_OVERSOLD` | `30` | RSI oversold threshold. |
| `EXTERNAL_CONVICTION_VIBE_RSI_OVERBOUGHT` | `70` | RSI overbought threshold. |
| `EXTERNAL_CONVICTION_VIBE_BB_PERIOD` | `20` | Bollinger Band period. |
| `EXTERNAL_CONVICTION_VIBE_BB_STD` | `2.0` | Bollinger Band standard deviations. |
| `EXTERNAL_CONVICTION_VIBE_MIN_BARS` | `30` | Minimum price bars required for analysis. |
| `EXTERNAL_CONVICTION_VIBE_HV_WINDOW` | `20` | Rolling HV calculation window. |
| `EXTERNAL_CONVICTION_VIBE_HV_LOOKBACK` | `120` | HV percentile lookback bars. |
| `EXTERNAL_CONVICTION_VIBE_SPREAD_PROXY` | `0.02` | Synthetic H/L spread for ADX on probability series. |

Two shadow-only service variants are available under the `external_conviction`
compose profile:

- `external-conviction-polifly`: `agent=external_conviction_polifly`,
  `provider=polifly_browser`, output
  `data/external_convictions_polifly.jsonl`. It remains skip-only until Polifly
  Pro is active and a browser bridge URL is configured.
- `external-conviction-api`: `agent=external_conviction_api`,
  `provider=public_news`, output `data/external_convictions_api.jsonl`. It uses
  public no-key news/RSS search evidence now. It can later be switched to
  `http_json` when `EXTERNAL_CONVICTION_API_URL` is configured.
- `external-conviction-whale`: `agent=external_conviction_whale`,
  `provider=clob_whale`, output `data/external_convictions_whale.jsonl`. Tracks
  whale trades from Polymarket data API.
- `external-conviction-divergence`: `agent=external_conviction_divergence`,
  `provider=manifold`, output `data/external_convictions_divergence.jsonl`.
  Cross-platform probability divergence detection.
- `external-conviction-debate`: `agent=external_conviction_debate`,
  `provider=debate`, output `data/external_convictions_debate.jsonl`. LLM
  bull/bear debate; requires `OPENAI_API_KEY`. 384MB / 0.50 CPU.
- `external-conviction-aggregator`: `agent=external_conviction_aggregator`,
  `provider=aggregator`, output `data/external_convictions_aggregator.jsonl`.
  Weighted consensus from multiple sub-providers. 512MB / 0.50 CPU.
- `external-conviction-technical`: `agent=external_conviction_technical`,
  `provider=technical_signal`, output `data/external_convictions_technical.jsonl`.
  EMA crossover + RSI + Bollinger Bands on CLOB probability history.
  Polls every 2h. Skips markets near resolution (<24h) or extreme prices.
  Available under both `external_conviction` and `vibe` compose profiles.
- `external-conviction-crypto-deriv`: `agent=external_conviction_crypto_deriv`,
  `provider=crypto_derivatives`, output `data/external_convictions_crypto_deriv.jsonl`.
  OKX/Binance funding rate regime classification. Crypto markets only.
  Available under both `external_conviction` and `vibe` compose profiles.

## 8. LLM prompt contract

`prompts.py:one_best_trade` requires the LLM to return JSON of the form:

```json
{"price": 0.5, "size_fraction": 0.1, "side": "BUY", "confidence": 0.62}
```

Semantic constraints stated explicitly to the LLM:
- `outcomes[0]` is the "primary" outcome.
- `side=BUY` → bet on the first outcome.
- `side=SELL` → bet against the first outcome.
- `price` is anchored to the first outcome's price even when `side=SELL`.
- `confidence` between 0 and 1.

`trade_recommendation.parse_trade_recommendation` accepts the JSON form and a
legacy `key:value` form, validates ranges, and rejects unknown sides.

## 9. Idempotency & crash recovery

The risky window: between `TradeLog.insert_pending` and `TradeLog.mark`
following the `post_order` HTTP call.

- On startup, `TradeLog.recover_stranded_pendings` flips any `pending` row
  older than `older_than_minutes` (default 10) to `may_have_fired`.
- `may_have_fired` is in `ACTIVE_STATUSES`, so the same market will be
  skipped via dedupe for `dedupe_hours` (default 6).
- The operator must verify on-chain whether the order actually executed
  before manually clearing the row.
- Position close idempotency is row-scoped: `PositionManager._already_closed`
  calls terminal-row helpers with `after_id=max(open journal row ids)`.
  A stale close from a previous position on the same `token_id` cannot block a
  new position from being evaluated or exited. Regression tests cover this
  because it caused a missed +20.97% BTC-daily exit window on 2026-05-12.

## 10. Failure modes & responses

| Failure | Behavior |
|---|---|
| Network timeout on CLOB `post_order` | Tenacity retries on `httpx.TimeoutException`, `httpx.NetworkError`, `requests.Timeout`, `requests.ConnectionError`. Other errors do **not** retry — duplicates are worse than missed fills. |
| LLM produces unparseable trade | `_evaluate_market` logs `FAILED`, sweep continues to next market |
| Polymarket gamma API down | `get_all_tradeable_events` raises; sweep fails; `TraderDaemon` catches and proceeds to next cycle |
| Wallet private key missing in dry-run | `Polymarket(live=False)` allowed, but `get_usdc_balance` still requires key — sweep fails with explicit error |
| OpenAI quota/rate limit during trader RAG or analysis | Writes `skipped_gate` with `ai_filter_unavailable` or `ai_analysis_unavailable`; the cycle/market is skipped without placing an order or crashing the daemon |
| OpenAI quota/rate limit during `news_signal` classification | Writes `news_signals.status='classifier_failed'` and enters a cooldown (`NEWS_SIGNAL_QUOTA_COOLDOWN_SEC`, default 3600) instead of creating actionable neutral `news_signal` rows |
| Allocator reads failed news classifications | `fresh_news_signals` ignores `classifier_failed`; only `status='news_signal'` counts as market-intelligence volume |
| Disk full | Heartbeat fails to write → Docker healthcheck reports unhealthy |
| `data/HALT` file present | `RiskGate.ok()` returns False; daemon keeps polling but never trades |

## 11. Deployment

Single-VPS, single-container. See `deploy/PREFLIGHT.md` for the full
pre-launch checklist and `deploy/vps-bootstrap.sh` for one-time host setup.

```
.   /srv/poly1
├── (git checkout)
├── .env                    # 600, never committed
└── data/                   # bind-mount; survives container rebuild
    ├── trade_log.db
    ├── llm_usage.jsonl
    ├── heartbeat
    ├── HALT                # if present, halts trading
    └── logs/poly1.log
```

Deploy: `./deploy/deploy.sh user@vps <git_ref>`.

Backup:
- VPS hourly: `sqlite3 trade_log.db ".backup trade_log.bak"`.
- Operator workstation every 30 min: `rsync -az` of `data/`.

## 12. Verification stages (smoke)

| Stage | Capital | Cmd | Pass criterion |
|---|---|---|---|
| 0 | $0 (local) | `docker compose run --rm trader python deploy/run.py` (one cycle) | trade_log.db has skipped_dry_run rows; no errors |
| 1 | $0 (VPS) | same on VPS | PREFLIGHT all green; one cycle clean |
| 1.5 | $0 (VPS, 24h) | `docker compose up -d` with EXECUTE=false | Markets vary; token_id matches side; ≥3 hypothetical trades |
| 2 | $5 live | single-shot with `EXECUTE=true` | Order submitted, balance changed, row recorded |
| 3 | $50, 24h | daemon | ≥4 attempted trades; PnL ≥ -5%; hit rate ≥ 50% on resolved |
| 4 | $200 | daemon | Only after stage 3 passes |

## 13. Out of scope (v0.8.0)

- Closing positions / `maintain_positions` (the bot only opens; positions
  resolve at market close).
- Non-binary markets.
- Backtesting / historical replay.
- Market making / passive limit orders.
- Multi-account or multi-wallet operation.
- Strategy plug-ins (single strategy: `one_best_trade` over top-N).
- Position size adaptive sizing (Kelly etc.).
- Anti-front-running / MEV protection.

## 14. Known risks

1. **Strategy unproven.** No backtest exists. Validation is in stages 1.5-3.
2. **No exit logic.** Capital is locked until each market resolves (days–weeks).
   Available USDC drops monotonically across the cycle until resolution.
3. **LLM cost.** All `llm.invoke` calls are tracked, but Chroma's OpenAI
   embeddings are billed separately and not tracked. The
   `MAX_DAILY_TOKEN_USD` gate undercounts by the embedding cost.
4. **CLOB rate limits.** Not documented publicly. With `top_n=3` and 30 min
   cycles the bot makes ≤6 LLM-driven API calls per hour, well below typical
   limits.
5. **Single point of failure.** Single VPS, single container. A host outage
   pauses trading until restored. Not high-availability.

## 15. Scalper (Strategy C — short-duration crypto Up/Down)

Independent module that runs in its own container alongside Trader.
Targets `*-updown-15m-*` markets via FAK market BUYs. No LLM. See
`docs/STRATEGY_C_SCALPING_SPEC.md` for the algorithm reference and
`docs/superpowers/plans/2026-05-05-scalper-strategy-c.md` for the build
log.

### Modules

| Module | Responsibility |
|---|---|
| `agents/application/scalper.py` | `ScalpPair`, `ScalperEngine`, `ScalperDaemon`, `__main__` |
| `agents/application/scalper_pairs.py` | `ScalperPairsDAO` — `scalper_pairs` CRUD |

### Storage

New `scalper_pairs` table in the existing `trade_log.db` (WAL mode).
Each FAK attempt also writes a `SCALPER_LEG` row to the existing
`trades` table for audit/PnL.

### Capital isolation

`SCALPER_RESERVE_USDC` reserves a fixed sub-balance for the scalper.
`SWARM_RESERVE_USDC` reserves a fixed sub-balance for the sister swarm
bot (which shares the same deposit wallet). Both reserves are tracked
in `RiskGate.reserves` dict. `RiskGate.available_for_trader()` returns
`balance - sum(reserves)`. The scalper itself reads the wallet balance
directly and refuses to enter new pairs when `balance < leg_cost × 2`.

### Operational stages

Stage 0 (shadow): `EXECUTE_SCALPER=false`, 2-3 days. Sanity check that
triggers fire and pair counts are non-trivial.

Stage 1 (live small): `EXECUTE_SCALPER=true`, leg=$2.50, **min 2 weeks**.
Abort if cumulative PnL < -$15 at any point.

Stage 2 (scale): leg=$5+ only after Stage 1 ends positive.

## 16. Profitable-agent safety loop (2026-05-12)

Live capital is gated by expected value and exitability, not by agent
activity. The current hardening layer adds:

| Component | Storage / module | Rule |
|---|---|---|
| Position profit monitor | `position_marks` in `trade_log.db` | Persist MFE/MAE and peak drawdown per token so restart does not erase trailing-exit context. |
| Exitable-size gate | `agents/application/execution_safety.py` | Block live entries that are too small to exit after a normal stop-loss move. |
| Market quarantine | `market_quarantine` in `trade_log.db` | Markets with repeated 404/no-orderbook/liquidity failures are blocked before another LLM/execution attempt. |
| Router live gate | `OpportunityRouter.live_route_allowed` | In live mode, `Trader` requires a fresh `live_probe` route when `OPPORTUNITY_ROUTER_ENFORCE_LIVE=true`. |
| Agent promotion ledger | `agent_promotion_ledger` in `trade_log.db` | Durable research/backtest/paper/live_probe/live_scaled/demoted state. |
| News fallback | `heuristic_signal` rows | LLM failure produces research-grade heuristic rows, not live-grade `news_signal` rows. |

Env vars:

| Env var | Default | Meaning |
|---|---:|---|
| `MIN_EXITABLE_ENTRY_USDC` | `3.0` | Absolute minimum entry size for live agents. |
| `MIN_EXITABLE_STOP_LOSS_PCT` | `0.07` | Stop-loss move assumed for exitability math. |
| `MIN_EXIT_NOTIONAL_USDC` | `1.0` | Minimum practical exit notional. |
| `MIN_EXITABLE_SAFETY_BUFFER` | `1.25` | Buffer above exchange/strategy minimum. |
| `MAINTAIN_MAX_CLOSE_FAILURES` | `3` | Close failures before terminal escalation. |
| `OPPORTUNITY_ROUTER_ENFORCE_LIVE` | `true` | Require fresh `live_probe` route for live `Trader` entries. |

## 17. Agent Registry — goals, data sources & error handling

Canonical goal definition for every active agent. The authoritative Python
registry is `AGENT_GOALS` in `agents/application/market_scanner.py`; this
table is the human-readable mirror.

| Agent | Goal | Win-rate target | Entry criterion | Key data sources | Error handling |
|---|---|---|---|---|---|
| **trader** | Exploit psychological crowd mispricing via LLM | >55%, hold <6h | Brain ≥0.30 + conviction gate + LLM conf ≥0.60 | Gamma, Chroma, Tavily, brain_decisions | LLM fail → `skipped_gate`; quota → 1h cooldown |
| **btc_daily** | Fade BTC 24h overreactions (mean reversion) | >55%, EOD | BTC drift >3%, price ≤0.65, no Tavily macro news | CoinGecko BTC price, Gamma, Tavily | Slippage counter; N=3 → skip cycle |
| **near_resolution** | Exploit last-mile anchoring bias in markets closing 0.5h–24h | >60%, hold <2h | hours_left ∈ [0.5, 24], conf ≥0.65, liq ≥$500 | Gamma, Tavily, scanner_near_resolution signals | Gamma fail → []; Tavily fails open |
| **news_shock** | Enter before crowd re-prices after material news (≤30 min window) | >50%, fast | signal age <30 min, materiality ≥0.5, drift <10% | DB news_signals + scanner_news_shock, Tavily, Gamma | Drift/liq fail → mark `skipped`; LLM quota → `classifier_failed` + 1h cooldown |
| **wallet_follow** | Mirror proven whale wallets | track whales >60% 30d | signal age <1h, drift <10%, liq ≥$3k, conf ≥0.50 | wallet_signals DB, Gamma, Tavily | Old signal → skip; high drift → skip |
| **scalper** | Math-spread arb on crypto 15-min UP/DOWN pairs | >55%, hold <10 min | pair_sum <1.04, entry ≤0.55, >90s to expiry | Gamma 15m markets, CLOB orderbook | FAK miss → re-queue; `RECONCILE_NEEDED` on ambiguous fill |
| **external_conviction** | 11-source cross-platform consensus aggregation | conf ≥0.58 + diverg >5% | confidence ≥0.58, volume ≥$5k, price ∈ [0.12, 0.88] | Manifold, Metaculus, Kalshi, CLOB whale, Tavily, LLM debate | Provider fail → skip verdict; daemon continues |
| **market_scanner** | 5-min proactive scan; route signals to entry agents via DB | N/A (discovery only) | Brain gate + Tavily ≥0.40 or Manifold div ≥0.07 | Gamma, Tavily, Manifold REST | Gamma fail → empty cycle; per-market fail → log + skip |
| **position_manager** | Exit all open positions (TP/SL/timeout) | N/A (exit only) | Open fills; exit on TP/SL/timeout | trade_log, Gamma live price, Tavily (LLM exit) | close_failed row; N=3 in 15 min → supervisor HALT |
| **trading_supervisor** | Detect exit-path failures; enforce HALT pre-entry | N/A (safety only) | Stale/missing position_manager evidence | trade_log, position_marks, settlement_reconciliation, heartbeat files | Critical → write HALT; warning → status JSON |
| **settlement_reconciler** | On-chain P&L truth; flag redeemable/stuck | N/A (reconcile) | Open fills + on-chain CTF | trade_log, Gamma resolution, CTF balance | `reconcile_error` → critical halt |

## 18. BTC Daily agent

**Goal:** Fade BTC 24h crowd overreaction. When BTC moves >3% intraday,
crowd herding over-extends the daily UP/DOWN binary price. The bot bets on
mean reversion.

### Data flow

```
BtcDailyDaemon → BtcDailyAgent.run_once()
  1. fetch_btc_price()     → CoinGecko /simple/price
  2. fetch_daily_market()  → Gamma ?q=bitcoin-up-or-down-on-{date}
  3. compute_drift()       → skip if |drift| < BTC_DAILY_THRESHOLD_PCT
  4. tavily_headlines()    → skip on fundamental macro news
  5. brain gate + RiskGate.ok() + dedupe
  6. execute or skipped_dry_run
```

### Entry / exit

- **Entry:** |drift| > threshold, entry price ≤ 0.65, no Tavily macro news, brain pass.
- **Exit:** `position_manager` (TP=15%, SL=5%, timeout=EOD).
- **Slippage guard:** `BTC_DAILY_MAX_SLIPPAGE_SKIPS` consecutive fails → skip rest of cycle.

### Error handling

| Error | Response |
|---|---|
| CoinGecko unavailable | `fetch_btc_price()` returns None; cycle skipped |
| Gamma market not found | `fetch_daily_market()` returns None; cycle skipped |
| Slippage skip | Counter incremented; abort after N skips |
| Tavily network error | Fails open; Tavily filter skipped |

Tests: `tests/test_trader.py` `TestBtcDaily*`. Env vars: `BTC_DAILY_*`.

## 19. News Shock agent

**Goal:** Enter Polymarket markets within the 30-minute window after a
material news headline, before the crowd fully re-prices.

### Data flow

```
NewsShockDaemon → NewsShockEngine.run_once()
  1. _read_fresh_signals()  → news_signals WHERE status IN
                               ('news_signal','scanner_news_shock')
                               AND materiality >= min AND age < max_age_hours
                               AND direction IN ('bullish','bearish')
  2. for each signal:
     a. Gamma live price + liquidity check
     b. drift check      → skip if priced in
     c. EV gate          → ev = materiality × (1 - entry_price)
     d. Tavily context   → logged, fails open
     e. RiskGate + dedupe → execute or skipped_dry_run
     f. _mark_signal()   → 'consumed' / 'skipped' / 'entry_failed'
```

### Signal sources

| Source | Written by | DB status |
|---|---|---|
| LLM classifier | `news_signal.py` | `news_signal` |
| Scanner Tavily | `market_scanner.py` | `scanner_news_shock` |

### Error handling

| Error | Response |
|---|---|
| DB read failure | Returns []; daemon sleeps |
| Gamma not found | Signal marked `skipped` |
| Drift too high | Signal marked `skipped` |
| LLM quota | `classifier_failed` status + 1h cooldown |

Env vars: `NEWS_SHOCK_*`, `EXECUTE_NEWS_SHOCK`.

## 20. Wallet Follow agent

**Goal:** Mirror proven whale wallets within 1 hour of their entry.

### Data flow

```
WalletFollowDaemon → WalletFollowEngine.run_once()
  1. _read_fresh_signals()  → wallet_signals WHERE status='new' AND age < max_age_hours
  2. for each signal:
     a. Gamma live price + liquidity + drift check
     b. EV gate + RiskGate + dedupe
     c. execute or skipped_dry_run
     d. _mark_signal()
```

### Entry / exit

- **Min confidence:** ≥ `WALLET_FOLLOW_MIN_CONFIDENCE` (default 0.50).
- **Signal age cap:** ≤ `WALLET_FOLLOW_MAX_AGE_HOURS` (default 1.0h).
- **Drift cap:** ≤ `WALLET_FOLLOW_MAX_DRIFT` (default 10%).
- **Exit:** `position_manager` (TP=10%, SL=7%).

### Error handling

| Error | Response |
|---|---|
| Watcher API down | No signals written; follow agent idles |
| Signal too old | Marked `skipped` |
| High drift | Marked `skipped` |

Env vars: `WALLET_FOLLOW_*`, `WALLET_WATCHER_*`, `EXECUTE_WALLET_FOLLOW`.

## 21. Position Manager

**Goal:** Exit every open position at the correct time (TP/SL/timeout).

### Data flow

```
PositionManagerDaemon → PositionManager.run_once()
  1. filled_positions_with_id()  → all open fills, all entry agents
  2. for each position:
     a. Gamma current price
     b. TP review → close only if hard cap or brain does not justify hold
     c. SL check  → soft stop forces review; hard stop exits
     d. timeout   → close unless brain extends within bounded guardrail
     e. LLM/brain exit authority check + optional Tavily context
     f. write position_mark + brain_decision
  3. heartbeat
```

### Error handling

| Error | Response |
|---|---|
| Gamma price unavailable | Skip position this cycle |
| CLOB FAK sell has no immediate match | Write `exit_deferred`; keep position managed |
| CLOB sell fails for non-liquidity reason | Write `close_failed`; supervisor counts; N threshold in window → HALT |
| LLM exit fails | Fails open; hold decision unchanged |

Env vars: `MAINTAIN_*`.

## 22. Market Scanner

**Goal:** Proactive 5-minute opportunity discovery.  Routes approved signals
to entry agents via existing DB tables.  Never places orders.

### Signal routing

| Target | Rule | DB written |
|---|---|---|
| `trade` | brain score ≥ SCANNER_MIN_TRADE_SCORE | `brain_decisions` (approved=True) |
| `news_shock` | Tavily confidence ≥ SCANNER_NEWS_SHOCK_MATERIALITY | `news_signals` (scanner_news_shock) |
| `near_resolution` | hours ∈ [0.5, 24] AND conf ≥ threshold | `news_signals` (scanner_near_resolution) |

### AGENT_GOALS registry

```python
from agents.application.market_scanner import AGENT_GOALS
# or: python -m agents.application.market_scanner --goals
```

### Error handling

| Error | Response |
|---|---|
| Gamma fetch fails | Empty cycle; heartbeat still written |
| Single market error | Log + skip; scan continues |
| Tavily unavailable | No boost, no news_signal written |
| Manifold unavailable | No divergence boost |

### Smoke test

```bash
python -m agents.application.market_scanner --once --json
docker compose --profile scanner run --rm market_scanner \
  python -m agents.application.market_scanner --once --json
```

Env vars: `SCANNER_*`.

## 23. BTC 5-min agent

**Goal:** Multi-signal consensus trading on Polymarket's 5-minute BTC
up/down markets (`btc-updown-5m-{unix_ts}`). Every 5 minutes a new
market opens and auto-resolves via Chainlink. Price is always ~0.50/0.50.

### Data flow

```
Btc5MinDaemon → Btc5MinEngine.maybe_enter()
  1. CoinbasePriceFeed.update()  → spot price ring buffer
  2. _momentum_signal()          → pct_change over 2 min, threshold 0.15%
  3. _funding_signal()           → OKX BTC perpetual funding rate (public)
  4. _rsi_signal()               → RSI(14) on 1-min resampled price feed
  5. composite_signal()          → weighted majority vote (momentum w=2.0)
  6. _news_veto()                → Tavily headline keyword filter
  7. RiskGate.ok() + dedupe + exitable_size_check
  8. execute or shadow
```

### Entry / exit

- **Entry:** ≥2/3 signals agree, confidence > 0.55, within entry window
  (60–180s into the 5-min period), no news veto, hourly cap ≤ 6.
- **Exit:** None — 5-min markets auto-resolve by Chainlink.
- **Side:** bullish → BUY → token_ids[0] (Up); bearish → SELL → token_ids[1] (Down).

### Signals

| Signal | Source | Weight | Bullish | Bearish |
|---|---|---|---|---|
| Momentum | CoinbasePriceFeed 2-min %Δ | 2.0 | move > +0.15% | move < −0.15% |
| Funding | OKX perpetual 8h rate | 1.0 | rate < −0.0005 | rate > +0.0005 |
| RSI | RSI(14) on 1-min candles | 1.0 | RSI < 25 | RSI > 75 |

### Error handling

| Error | Response |
|---|---|
| Coinbase feed unavailable | `update()` returns None; cycle skipped |
| OKX funding timeout | Funding signal returns skip; other signals can still form consensus |
| Gamma market not found | `_resolve_current_5min_market()` returns None; cycle skipped |
| Tavily network error | Fails open; news veto skipped |

### Env vars

| Var | Default | Notes |
|---|---|---|
| `EXECUTE_BTC_5MIN` | `false` | Live trading flag |
| `BTC_5MIN_RESERVE_USDC` | `3.0` | Capital reserved in RiskGate |
| `BTC_5MIN_POSITION_SIZE_USDC` | `1.5` | Per-trade size |
| `BTC_5MIN_ENTRY_WINDOW_START` | `60` | Seconds after period open |
| `BTC_5MIN_ENTRY_WINDOW_END` | `180` | Latest entry point |
| `BTC_5MIN_MOMENTUM_PCT` | `0.0015` | 0.15% min BTC move |
| `BTC_5MIN_MIN_CONSENSUS` | `2` | Min agreeing signals |
| `BTC_5MIN_NEWS_VETO` | `true` | News veto enabled |
| `BTC_5MIN_POLL_SEC` | `3` | Loop cadence |
| `BTC_5MIN_COOLDOWN_SEC` | `300` | = one 5-min window |
| `BTC_5MIN_MAX_PER_HOUR` | `6` | Hard trade cap |

Tests: `tests/test_btc_5min.py`. Env vars: `BTC_5MIN_*`.
