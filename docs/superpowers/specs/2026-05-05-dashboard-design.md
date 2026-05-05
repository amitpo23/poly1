# Dashboard Design — poly1

**Date:** 2026-05-05  
**Status:** Approved  
**Phase 1:** Streamlit  
**Phase 2:** FastAPI + React (future)

---

## Goal

Replace the hand-rolled `monitor_web.py` (stdlib, port 7777) with a full-featured Streamlit dashboard that covers live monitoring, P&L analytics, capital usage, trade history, scalper state, LLM cost, and operational control — all in one place.

---

## Architecture

### Phase 1 — Streamlit (this spec)

```
Docker service: dashboard  (profile: dashboard)
  │
  └─ scripts/python/dashboard.py
       ├── reads data/trade_log.db  (SQLite, read-only)
       ├── reads data/llm_usage.jsonl
       ├── reads data/logs/poly1.log (tail)
       ├── reads data/heartbeat, data/scalper_heartbeat
       └── writes data/HALT  (control tab only)

Port: 8050  (existing monitor_web.py stays on 7777)
Auto-refresh: 30 s on Live tab
```

### Phase 2 — FastAPI + React (future spec)

- FastAPI backend exposes REST + WebSocket on same data
- React frontend replaces Streamlit UI
- Streamlit remains as analytics fallback

---

## 7 Tabs

### 🟢 Live

Real-time agent health — refreshes every 30 s.

| Widget | Source |
|--------|--------|
| Trader heartbeat (age in seconds) | `data/heartbeat` |
| Scalper heartbeat (age in seconds) | `data/scalper_heartbeat` |
| HALT status (active / clear) | `data/HALT` file presence |
| RiskGate last trigger reason | trade_log: last `gate` row |
| Current USDC balance estimate | trades: starting_balance − net spent |
| Drawdown % today | trades: filled rows today |
| Last 5 log lines | `data/logs/poly1.log` tail |

### 📈 P&L

Profit & loss over time.

| Widget | Source |
|--------|--------|
| Cumulative P&L line chart | trades: filled, cost_usdc, outcome |
| Daily P&L bar chart | trades: grouped by date |
| Win rate (filled trades) | trades: win/loss count |
| Avg profit per win / avg loss per loss | trades aggregation |
| Sharpe-like ratio (if ≥30 trades) | rolling daily P&L |

**Note:** Polymarket settled outcomes are not auto-fetched. P&L is estimated from fill prices; final settlement requires manual reconciliation or future outcome polling.

### 💰 Capital

Balance and position usage.

| Widget | Source |
|--------|--------|
| Starting balance vs current estimate | `.env` + trades |
| Capital at risk (open positions) | trades: filled, not redeemed |
| Scalper reserve vs deployed | scalper_pairs: cost_up + cost_down |
| Daily spend chart | trades: cost_usdc by date |
| MAX_POSITION_FRACTION gauge | env var |
| STARTING_BALANCE_USDC reference | env var |

### 📋 Trades

Full trade history with filters.

| Widget | Source |
|--------|--------|
| Status filter (all / filled / gate / failed / dry_run) | trades table |
| Date range selector | trades.ts |
| Market search (slug substring) | trades.market |
| Table: ts, market, side, price, qty, cost, status | trades |
| Expandable row: full JSON recommendation | trades.raw_response |

### 🔪 Scalper

Scalper pair state and performance.

| Widget | Source |
|--------|--------|
| Open pairs count | scalper_pairs: list_open |
| Pairs by state breakdown | scalper_pairs: state counts |
| Pairs table: slug, state, cost_up, cost_down, pnl_est | scalper_pairs |
| RECONCILE_NEEDED pairs highlighted red | scalper_pairs |
| Recently closed pairs (last 20) | scalper_pairs: order by closed_ts |
| Scalper P&L estimate (both_filled pairs) | cost_up + cost_down vs 1.0 payout |

### 🤖 LLM Cost

LLM token usage and cost tracking.

| Widget | Source |
|--------|--------|
| Total spend $ (all time) | llm_usage.jsonl |
| Daily spend chart | llm_usage: grouped by date |
| Per-tag breakdown (trader, scalper, …) | llm_usage.tag |
| Tokens: prompt vs completion | llm_usage |
| MAX_DAILY_TOKEN_USD gate reference | env var |
| Recent invocations table (last 20) | llm_usage |

### ⚙️ Control

Operational controls — requires confirmation before action.

| Control | Action |
|---------|--------|
| HALT button | Create `data/HALT` file |
| RESUME button | Delete `data/HALT` file |
| Current EXECUTE mode display | `.env` read-only (no write) |
| Log level display | `.env` read-only |
| Log viewer (last 200 lines, scrollable) | `data/logs/poly1.log` |
| Bot process status | `data/heartbeat` age |

**Safety:** HALT/RESUME buttons show a `st.warning` confirmation step before executing. Writing `.env` is out of scope — only the HALT file is writable.

---

## Data Access

All data access is **read-only** except:
- `data/HALT` — created/deleted by Control tab

SQLite access uses the same connection pattern as `TradeLog` but opens in read-only mode (`?mode=ro` URI). No writes to `trade_log.db` from the dashboard.

---

## Docker Service

```yaml
# docker-compose.yml addition
dashboard:
  build: .
  profiles: ["dashboard"]
  command: streamlit run scripts/python/dashboard.py --server.port 8050 --server.address 0.0.0.0
  ports:
    - "8050:8050"
  volumes:
    - ./data:/app/data:ro        # read-only mount
    - ./data/HALT:/app/data/HALT # writable exception for HALT file
  environment:
    - TRADE_LOG_DB=/app/data/trade_log.db
    - LLM_USAGE_FILE=/app/data/llm_usage.jsonl
    - LOG_DIR=/app/data/logs
    - STARTING_BALANCE_USDC=${STARTING_BALANCE_USDC:-50.0}
  restart: unless-stopped
```

**Note on volume mount:** A read-only mount with a writable exception for a single file requires two separate mount entries. Alternatively: mount `./data` read-write and limit writes in application code.

---

## Implementation Files

| File | Action |
|------|--------|
| `scripts/python/dashboard.py` | New — main Streamlit app (~500 lines) |
| `docker-compose.yml` | Add `dashboard` service |
| `requirements.txt` | Add `streamlit>=1.35` |
| `.env.example` | No changes needed — dashboard reads existing vars |

---

## Out of Scope (Phase 1)

- Settlement outcome polling (resolved market prices from Polymarket API)
- Writing to `.env` from dashboard
- User authentication / password protection
- Alert notifications (Telegram from dashboard)
- Historical backtest view

---

## Success Criteria

1. `docker compose --profile dashboard up dashboard` starts without error
2. All 7 tabs render with real data from `data/trade_log.db`
3. HALT button creates `data/HALT`; RESUME removes it; Trader respects it within one cycle
4. Auto-refresh on Live tab updates heartbeat age every 30 s
5. P&L chart shows correct cumulative values matching manual SQLite query
