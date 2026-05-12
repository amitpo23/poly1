# poly1 — Specification (v0.2.0-prod-prep)

## 1. Purpose

Autonomous Polymarket trading bot. An LLM pipeline scores binary prediction
markets, and a daemon executes market orders within configurable risk limits.
Targeted use case: $50–$200 capital running 24/7 on a single VPS.

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

| Module | Responsibility |
|---|---|
| `agents/application/cron.py` `TraderDaemon` | Long-running loop, SIGTERM-aware, heartbeat + Healthchecks ping |
| `agents/application/trade.py` `Trader` | Per-cycle sweep, top-N ranking, integration of all gates |
| `agents/application/trade_log.py` `TradeLog` | SQLite ledger (idempotency, dedupe, recovery) |
| `agents/application/risk_gate.py` `RiskGate` | Pre-trade kill switch, drawdown, rate limit, token-cost cap |
| `agents/application/executor.py` `Executor` | LLM pipeline (filter → forecast → trade rec), token-cost tracking |
| `agents/application/prompts.py` `Prompter` | Versioned prompts; defines BUY/SELL semantics |
| `agents/application/trade_recommendation.py` | Parses LLM JSON/legacy output → `TradeRecommendation` |
| `agents/polymarket/polymarket.py` `Polymarket` | CLOB client wrapper, side→token mapping, balance reads |
| `agents/polymarket/gamma.py` `GammaMarketClient` | Gamma REST reads (events, markets) |
| `agents/connectors/chroma.py` `PolymarketRAG` | Local Chroma vector store for RAG filtering |
| `agents/utils/objects.py` | Pydantic data classes (`TradeRecommendation`, `SimpleMarket`, …) |
| `agents/utils/logging_setup.py` | JSON formatter, RotatingFileHandler |
| `agents/utils/notify.py` | Telegram (non-blocking) + Healthchecks ping |
| `deploy/run.py` | Container entrypoint, env validation, daemon start |

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
| `POLYGON_RPC` | `https://polygon.drpc.org` | Polygon RPC endpoint. Override with paid Alchemy/Infura key for production. |
| `MAINTAIN_TAKE_PROFIT_PCT` | `0.05` | Position manager: exit when position gains this fraction above entry price. |
| `MAINTAIN_STOP_LOSS_PCT` | `0.03` | Position manager: exit when position drops this fraction below entry price. Tightened 2026-05-12 from 0.07 to reduce per-trade loss. |
| `MAINTAIN_MAX_HOLD_HOURS` | `24` | Position manager: force-close after this many hours regardless of P&L. |
| `BTC_DAILY_MAX_SLIPPAGE_SKIPS` | `3` | btc_daily: give up on a market after N consecutive slippage failures in one daemon run. Reset on successful entry. Prevents the 58-attempt tight-loop seen on market 2214715 (2026-05-11). |

### Persistence

| Var | Default |
|---|---|
| `TRADE_LOG_DB` | `./data/trade_log.db` |
| `LLM_USAGE_FILE` | `./data/llm_usage.jsonl` |
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

## 10. Failure modes & responses

| Failure | Behavior |
|---|---|
| Network timeout on CLOB `post_order` | Tenacity retries on `httpx.TimeoutException`, `httpx.NetworkError`, `requests.Timeout`, `requests.ConnectionError`. Other errors do **not** retry — duplicates are worse than missed fills. |
| LLM produces unparseable trade | `_evaluate_market` logs `FAILED`, sweep continues to next market |
| Polymarket gamma API down | `get_all_tradeable_events` raises; sweep fails; `TraderDaemon` catches and proceeds to next cycle |
| Wallet private key missing in dry-run | `Polymarket(live=False)` allowed, but `get_usdc_balance` still requires key — sweep fails with explicit error |
| OpenAI rate-limited | Bubbles up; cycle fails; `RiskGate.ok` next cycle should still pass and retry |
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

## 13. Out of scope (v0.2.0)

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
