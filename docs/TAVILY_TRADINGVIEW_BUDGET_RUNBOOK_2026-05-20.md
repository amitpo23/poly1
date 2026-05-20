# Tavily and TradingView Budget Runbook — 2026-05-20

## Tavily policy

Tavily is no longer treated as a high-volume enrichment source. It is disabled
by default and, even when enabled, is guarded by a small daily budget.

Runtime defaults:

- `TAVILY_ENABLED=false`
- `TAVILY_DAILY_LIMIT=5`
- `TAVILY_CACHE_TTL_SEC=21600`
- `TAVILY_MIN_QUERY_INTERVAL_SEC=900`
- `TAVILY_MAX_RESULTS=2`
- `TAVILY_CRITICAL_ONLY=true`

The central helper `agents.application.tavily.tavily_headlines()` now enforces:

- cache before any network call
- daily call budget
- minimum spacing between live Tavily calls
- max result cap
- critical-keyword gate

Direct Tavily call sites were routed through the helper where they previously
bypassed the budget.

## When to enable Tavily

Enable only for news-sensitive probes where a breaking event can materially
move a market before Polymarket reprices:

- war / attack / missile / Iran / Israel
- Fed / FOMC / rate / CPI / inflation
- oil / crude / gas
- hack / exploit / SEC / ETF
- major earnings or legal event

Do not enable Tavily for broad market scanning. RSS, Polymarket APIs, Alpaca,
crypto tape, and scorecard history should carry the baseline workload.

## TradingView options snapshot

TradingView options chain is a browser-rendered page, not a stable JSON API.
The system therefore reads a local snapshot file and fails closed if it is
missing or stale.

Snapshot path:

```bash
/app/data/tradingview_options_es1_snapshot.json
```

Expected fields:

```json
{
  "ts": "2026-05-20T12:00:00+00:00",
  "symbol": "CME_MINI:ES1!",
  "put_call_ratio": 0.82,
  "put_volume": 8200,
  "call_volume": 10000,
  "source": "manual_tradingview_options_chain"
}
```

Write a snapshot:

```bash
python scripts/write_tradingview_options_snapshot.py \
  --path data/tradingview_options_es1_snapshot.json \
  --put-call-ratio 0.82
```

Or with raw volumes:

```bash
python scripts/write_tradingview_options_snapshot.py \
  --path data/tradingview_options_es1_snapshot.json \
  --put-volume 8200 \
  --call-volume 10000
```

Freshness is controlled by `TRADINGVIEW_OPTIONS_MAX_AGE_SEC` (default 900s).
If the snapshot is missing, stale, or has no put/call signal, TradingView is
neutral and cannot approve a trade by itself.
