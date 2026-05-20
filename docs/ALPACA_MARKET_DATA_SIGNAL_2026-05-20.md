# Alpaca Market Data Signal

Status: read-only signal layer. It does not place orders. On the same date we
also added `crypto_exchange_tape`, a faster Binance/OKX signal for crypto
Up/Down markets.

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

## Crypto Exchange Tape Add-On

`crypto_exchange_tape` reads:

- Binance spot 1m klines
- Binance top-of-book bid/ask
- OKX perpetual funding rate

It supports BTC, ETH, SOL, XRP, DOGE, and BNB question matching. It is weighted
inside MetaBrain through `META_BRAIN_WEIGHT_CRYPTO_TAPE=0.12` and also writes
shadow rows through `external-conviction-crypto-tape`.

This is currently the most relevant public signal for fast crypto 5-minute
markets because it is closer to the actual resolution/reference venue than
general news or LLM reasoning.
