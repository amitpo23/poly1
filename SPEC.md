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
| `skipped_dedupe` | Same market had an active row (PENDING/SUBMITTED/FILLED/MAY_HAVE_FIRED) within 6h. |
| `skipped_gate` | Confidence or other guard failed. |
| `skipped_dry_run` | `EXECUTE=false`; the row records what would have been sent. |

`ACTIVE_STATUSES = (pending, submitted, filled, may_have_fired)` —
the dedupe window blocks re-trading any market with such a row in the last
6 hours.

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
| `POLYGON_WALLET_PRIVATE_KEY` | Polygon EOA used for CLOB order signing and balance reads |
| `OPENAI_API_KEY` | LLM (ChatOpenAI) and embeddings (Chroma) |

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
