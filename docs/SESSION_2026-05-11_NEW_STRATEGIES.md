# Session 2026-05-11 — New Trading Strategies: near_resolution, news_shock, wallet_follow

## Overview

Two working sessions in sequence. Combined result: **3 new trading agents**,
**2 signal-producer modules**, **complete infra wiring**, **76 tests passing**,
**3 git commits**.

---

## Commit 1 — `0c3ef39`

`feat: add near_resolution + news_shock agents + strategy_factory`

### Phase B — `agents/application/near_resolution.py`

New agent that finds binary markets closing within MIN–MAX hours where
one side is priced ≤ MAX_ENTRY_PRICE (cheap token). Uses Tavily news
search to get a confidence score before entering.

**Entry logic:**
```
cheap_side = "yes" if yes_price < no_price else "no"
confidence = _tavily_confidence(question, cheap_side)   # 0–1 heuristic
if confidence >= MIN_CONFIDENCE and RiskGate.ok() and not dedupe:
    BUY  (cheap_side=yes) → token_ids[0], price=yes_price
    SELL (cheap_side=no)  → token_ids[1], price=1-yes_price
    status = near_resolution_open
```

**Key env vars:**
```
EXECUTE_NEAR_RESOLUTION=false
NEAR_RESOLUTION_MIN_HOURS=0.5
NEAR_RESOLUTION_MAX_HOURS=36
NEAR_RESOLUTION_MAX_ENTRY_PRICE=0.15
NEAR_RESOLUTION_MIN_LIQUIDITY=3000
NEAR_RESOLUTION_MIN_CONFIDENCE=0.65
NEAR_RESOLUTION_POSITION_SIZE_USDC=2.5
NEAR_RESOLUTION_RESERVE_USDC=15
NEAR_RESOLUTION_MAX_OPEN=3
NEAR_RESOLUTION_POLL_SEC=60
```

**Tavily search:** POST to `https://api.tavily.com/search`, topic=news.
Score = fraction of results that contain the cheap outcome's keyword.
Confidence clamped [0.0, 1.0]. Requires `TAVILY_API_KEY`.

---

### Phase C — `agents/application/news_shock.py`

Reacts to high-materiality signals already written by the `news_signal`
classifier to the `news_signals` table.

**Entry logic:**
```
bullish → BUY YES  → EV = materiality × (1 − yes_price)
bearish → SELL NO  → EV = materiality × yes_price
enter if EV >= MIN_EV and entry_price <= MAX_ENTRY_PRICE
status = news_shock_open
```

**Key env vars:**
```
EXECUTE_NEWS_SHOCK=false
NEWS_SHOCK_MIN_SCORE=0.70
NEWS_SHOCK_MAX_AGE_HOURS=2.0
NEWS_SHOCK_MIN_EV=0.04
NEWS_SHOCK_MAX_ENTRY_PRICE=0.60
NEWS_SHOCK_MIN_LIQUIDITY=5000
NEWS_SHOCK_POSITION_SIZE_USDC=2.5
NEWS_SHOCK_RESERVE_USDC=15
NEWS_SHOCK_MAX_OPEN=3
NEWS_SHOCK_POLL_SEC=30
```

---

### Phase D (infra)

| File | Change |
|------|--------|
| `trade_log.py` | `NEAR_RESOLUTION_OPEN`, `NEWS_SHOCK_OPEN` constants; `filled_positions_with_id()` expanded |
| `risk_gate.py` | `near_resolution_reserve_usdc` + `news_shock_reserve_usdc` params; entries in `self.reserves` |
| `resolution_sync.py` | `OPEN_STATUSES` expanded to 5; SQL IN clause to 5 placeholders |
| `capital_allocator.py` | Status set + `_poly_agent_for_status()` routing for both new statuses |

---

### Phase E — `scripts/python/strategy_factory.py`

CLI for operator management of strategies:

```bash
python scripts/python/strategy_factory.py list
python scripts/python/strategy_factory.py status
python scripts/python/strategy_factory.py enable near-resolution
python scripts/python/strategy_factory.py disable news-shock
python scripts/python/strategy_factory.py run btc-daily
```

Registry: `trader`, `btc-daily`, `near-resolution`, `news-shock`, `scalper`.
`enable/disable` writes `EXECUTE_*` key to `.env`.
`run` dynamically imports daemon class and calls `.run()`.

---

### Docker services added (commit 1)

```yaml
near-resolution:
  profiles: ["near_resolution"]
  command: python -m agents.application.near_resolution
  healthcheck: near_resolution_heartbeat < 180s

news-shock:
  profiles: ["news_shock"]
  command: python -m agents.application.news_shock
  healthcheck: news_shock_heartbeat < 120s
```

### Tests (commit 1)

| File | Count | Coverage |
|------|-------|---------|
| `tests/test_near_resolution.py` | 14 | scan filter, shadow/live entry, dedupe, risk gate, confidence gate, max_open, BUY/SELL sides |
| `tests/test_news_shock.py` | 12 | DB read (mat/age filters), EV calc, bullish→BUY, bearish→SELL, low EV skip, high price skip, dedupe, risk gate, closed market, max_open |

---

## Commit 2 — `08ddd17`

`feat: add wallet_watcher + wallet_follow agents (Strategy G)`

### `agents/application/wallet_watcher.py` — signal producer

Polls `data-api.polymarket.com/activity` for recent trades from a
configured list of proxy wallet addresses. Optionally auto-discovers
top traders from the public leaderboard.

**Two operating modes:**
1. Static: `WALLET_WATCH_ADDRESSES=0x1234...,0xABCD...`
2. Scout: `WALLET_SCOUT_ENABLE=true` — fetches leaderboard, qualifies by
   profit >= `WALLET_SCOUT_MIN_PROFIT_USDC` AND trades >= `WALLET_SCOUT_MIN_TRADES`.
   Scouted addresses are in-memory only; re-scouted on each daemon start.

**Signal format** (`wallet_signals` table):
```sql
wallet_address, wallet_profit_usdc, wallet_trades_30d,
market_id, market_question, direction (bullish/bearish),
token_id, yes_price, wallet_entry_price, wallet_size_usdc,
status ('fresh' → 'acted'/'skipped')
```

Deduplication: no second signal for the same (wallet, market) within
`WALLET_WATCHER_MAX_AGE_HOURS`.

**Key env vars:**
```
WALLET_WATCH_ADDRESSES=""
WALLET_SCOUT_ENABLE=false
WALLET_SCOUT_LIMIT=20
WALLET_SCOUT_MIN_PROFIT_USDC=200
WALLET_SCOUT_MIN_TRADES=15
WALLET_WATCHER_POLL_SEC=120
WALLET_WATCHER_MAX_AGE_HOURS=4.0
```

---

### `agents/application/wallet_follow.py` — entry agent (Strategy G)

Reads `wallet_signals` rows with `status='fresh'` and decides whether
to copy-enter the same position.

**Confidence model:**
```
confidence = min(1.0, max(MIN_CONFIDENCE, wallet_profit_usdc / PROFIT_SCALE))
```
A wallet with `$0` profit → `min_confidence` (0.50).
A wallet with `$1000` profit → confidence 1.0.

**EV and side:**
```
bullish → BUY YES  → EV = confidence × (1 − yes_price)
bearish → SELL NO  → EV = confidence × yes_price
enter if EV >= MIN_EV and entry_price <= MAX_ENTRY_PRICE
status = wallet_follow_open
```

Marks the `wallet_signals` row as `'acted'` on entry or `'skipped'`
on any gate rejection. Position exits owned by `position_manager`.

**Key env vars:**
```
EXECUTE_WALLET_FOLLOW=false
WALLET_FOLLOW_RESERVE_USDC=15.0
WALLET_FOLLOW_POSITION_SIZE_USDC=2.5
WALLET_FOLLOW_MIN_CONFIDENCE=0.50
WALLET_FOLLOW_PROFIT_SCALE=1000.0
WALLET_FOLLOW_MIN_EV=0.03
WALLET_FOLLOW_MAX_ENTRY_PRICE=0.70
WALLET_FOLLOW_MIN_LIQUIDITY=3000
WALLET_FOLLOW_MAX_AGE_HOURS=4.0
WALLET_FOLLOW_MAX_OPEN=3
WALLET_FOLLOW_POLL_SEC=60
```

---

### Infra updates (commit 2)

| File | Change |
|------|--------|
| `trade_log.py` | `wallet_signals` table + 4 indexes in SCHEMA; `WALLET_FOLLOW_OPEN` constant; `filled_positions_with_id()` includes `wallet_follow_open` |
| `resolution_sync.py` | `WALLET_FOLLOW_OPEN` imported + added to `OPEN_STATUSES` (now 6 entries) |
| `risk_gate.py` | `wallet_follow_reserve_usdc` constructor param + `'wallet_follow'` in `self.reserves` |
| `capital_allocator.py` | `wallet_follow_open` in status set + `_poly_agent_for_status()` branch |

---

### Docker services added (commit 2)

```yaml
wallet-watcher:
  profiles: ["wallet"]
  command: python -m agents.application.wallet_watcher
  healthcheck: wallet_watcher_heartbeat < 300s

wallet-follow:
  profiles: ["wallet"]
  command: python -m agents.application.wallet_follow
  healthcheck: wallet_follow_heartbeat < 180s
```

Both share the `wallet` profile — start/stop them together:
```bash
docker compose --profile wallet up -d
docker compose --profile wallet down
```

### Tests (commit 2)

| File | Count | Coverage |
|------|-------|---------|
| `tests/test_wallet_watcher.py` | 10 | config defaults, env parsing, signal write, duplicate suppression, scout filters (profit/trades), stale trade skip, no-addresses early exit |
| `tests/test_wallet_follow.py` | 17 | config defaults, confidence calc (full/partial/floor/clamp), bullish→BUY, bearish→SELL, low EV skip, high entry price skip, dedupe, risk gate, closed market, max_open, signal marked 'acted', no-signals zero, live execute |

---

## Test summary

| Commit | New tests | Cumulative |
|--------|-----------|------------|
| before session | — | 23 |
| `0c3ef39` | +26 | 49 |
| `08ddd17` | +27 | 76 |

All 76 pass locally via:
```bash
python3 -m unittest discover -s tests -v
```

---

## Architecture map — all strategies after this session

| Strategy | Module | Entry status | Trigger |
|----------|--------|-------------|---------|
| A — Main trader | `executor.py` | `filled` | LLM sweep |
| B — BTC daily | `btc_daily.py` | `btc_daily_open` | BTC price signal |
| C — Scalper | `scalper.py` | `scalper_leg` | CLOB spread |
| D — Kalshi arb | spec only (Strategy D) | — | pending |
| E — Near-resolution | `near_resolution.py` | `near_resolution_open` | Gamma time filter + Tavily |
| F — News shock | `news_shock.py` | `news_shock_open` | `news_signals` DB rows |
| G — Wallet follow | `wallet_follow.py` | `wallet_follow_open` | `wallet_signals` DB rows |

Signal producers: `news_signal.py` (→ F), `wallet_watcher.py` (→ G).
All exits owned by `position_manager.py`.

---

## Operator checklist before enabling Strategy G live

1. Find candidate wallets (start with manual research on polymarket.com):
   - Sort leaderboard by 30d profit
   - Look for wallets with 15+ trades and consistent win-rate
   - Copy their proxy wallet addresses

2. Set in `.env`:
   ```
   WALLET_WATCH_ADDRESSES="0x...,0x..."
   WALLET_WATCHER_POLL_SEC=120
   WALLET_FOLLOW_RESERVE_USDC=15
   EXECUTE_WALLET_FOLLOW=false   # shadow mode first
   ```

3. Run shadow mode for ≥24h:
   ```bash
   docker compose --profile wallet up -d
   docker compose run --rm trader python scripts/python/cli.py inspect-trades --limit 30
   ```
   Verify shadow entries look reasonable (direction, price, market type).

4. Only then flip `EXECUTE_WALLET_FOLLOW=true` with small size ($1–$2).

---

## Known limitations / future work

- `wallet_watcher` uses `data-api.polymarket.com/activity` which is an
  undocumented endpoint. If it changes structure, `_poll_wallet()` needs updating.
- The leaderboard API key / rate limit is not currently handled — add
  exponential backoff if scout fails repeatedly.
- Confidence is purely profit-based; a more robust model would also consider
  win-rate and average return per trade. The `wallet_stats` dict is already
  populated from the leaderboard — extend `_confidence()` when data is available.
- Strategy D (Kalshi arb) is still spec-only (`docs/STRATEGY_D_KALSHI_ARB_SPEC.md`).
