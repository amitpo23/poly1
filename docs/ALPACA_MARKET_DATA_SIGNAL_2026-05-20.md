# Alpaca Market Data Signal

Status: read-only signal layer. It does not place orders.

## Purpose

Alpaca is now another tool for the brain. It gives MetaBrain an external tape
signal from recent 1-minute bars, mainly for BTC/crypto fast markets and
equity/index markets that mention supported tickers.

## Flow

1. `AlpacaMarketDataClient` fetches recent bars from Alpaca Market Data.
2. It computes a bounded momentum/volume signal:
   - direction: `bullish`, `bearish`, or neutral
   - probability/confidence
   - supporting features: momentum, range, volume ratio, bar count
3. `MetaBrain` reads the signal directly via `AlpacaMarketDataReader`.
4. `external-conviction-alpaca` also writes shadow evidence to
   `data/external_convictions_alpaca.jsonl`, so provider quality can be
   backtested like the other conviction providers.

## Safety Rules

- No Alpaca component can bypass MetaBrain approval, EV gate, orderbook
  execution-quality checks, RiskGate, or journal dedupe.
- Missing Alpaca data is neutral (`0.5`) and is excluded by informed-only
  weighting.
- Alpaca can become a solo expert only after it earns empirical reliability via
  `brain_decisions.signal_source` / provider scorecard, same as every other
  provider.

## Configuration

Important env vars:

- `META_BRAIN_ALPACA_ENABLED=true`
- `META_BRAIN_WEIGHT_ALPACA=0.08`
- `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` optional for authenticated data
- `ALPACA_MARKET_DATA_TIMEFRAME=1Min`
- `ALPACA_MARKET_DATA_BAR_LIMIT=20`
- `ALPACA_MARKET_DATA_CACHE_SEC=60`
- `ALPACA_MARKET_DATA_MOMENTUM_THRESHOLD=0.0015`

Docker service:

- `external-conviction-alpaca`
- profile: `external_conviction`
- output: `/app/data/external_convictions_alpaca.jsonl`

## First QA

Run in shadow only:

```bash
python -m unittest tests.test_external_conviction tests.test_meta_brain -v
docker compose --profile external_conviction up -d external-conviction-alpaca
```

Then inspect:

```bash
tail -n 20 data/external_convictions_alpaca.jsonl
sqlite3 data/trade_log.db "select agent, approved, reason, score from brain_decisions where agent like '%alpaca%' order by id desc limit 20;"
```
