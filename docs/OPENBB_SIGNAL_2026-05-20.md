# OpenBB Market-Data Signal - 2026-05-20

## Purpose

OpenBB is integrated as a read-only research provider, not as an execution
engine.  It gives MetaBrain another external market-data lens for equity-linked,
macro, commodity, and crypto markets.

## Files

- `agents/application/openbb_market_data.py`
- `agents/application/external_conviction.py` provider:
  `openbb_market_data`
- `docker-compose.yml` service:
  `external-conviction-openbb`
- `config/agent_registry.json` agent:
  `external_conviction_openbb`

## Safety

- The provider is shadow-only.
- It never places orders.
- If the optional `openbb` package is missing and `OPENBB_PROVIDER=yfinance`,
  the adapter falls back to Yahoo Finance's chart endpoint via `requests`.  If
  both data paths fail, it returns a skip verdict and logs the reason.
- Promotion requires a positive provider scorecard and positive markouts.

## Runtime

The default compose service polls every 15 minutes:

```bash
docker compose --profile external_conviction up -d external-conviction-openbb
```

Config:

- `OPENBB_PROVIDER=yfinance`
- `OPENBB_MARKET_DATA_BAR_LIMIT=60`
- `OPENBB_MARKET_DATA_MIN_BARS=10`
- `OPENBB_MARKET_DATA_CACHE_SEC=300`
- `OPENBB_MARKET_DATA_TIMEOUT_SEC=5`
- `OPENBB_MARKET_DATA_MOMENTUM_THRESHOLD=0.01`
- `OPENBB_YAHOO_INTERVAL=5m`
- `OPENBB_YAHOO_RANGE=5d`

## Signal Logic

The adapter maps market questions to broad symbols:

- `NVDA`, `MSFT`, `AAPL`, `GOOGL`, `AMZN`, `TSLA`, `META`
- `SPY` for S&P/SPX questions
- `QQQ` for Nasdaq questions
- `USO`, `GLD`, `SLV` for crude/gold/silver proxies
- `BTC-USD`, `ETH-USD` for crypto

It computes:

- short moving average,
- long moving average,
- recent momentum,
- volume ratio,
- range expansion.

The output is bounded to a conservative probability/confidence so it cannot
overpower the system before it proves value in shadow.

## Dependency Policy

Do not install the full `openbb` package in the live image casually.  It pulls a
large dependency set and may upgrade infrastructure packages.  The current
production-safe path is:

1. Use OpenBB when it is already available.
2. Otherwise use the `yfinance`/Yahoo chart fallback.
3. Keep the provider shadow-only until scorecards and markouts prove value.

Install full OpenBB only in a dedicated dependency-upgrade pass:

```bash
pip install openbb
```
