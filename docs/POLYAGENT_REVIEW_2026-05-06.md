# PolyAgent Review

Date: 2026-05-06
Source: `https://github.com/omkute101/PolyAgent`

Read-only review only. The repo was cloned to `/private/tmp/polyagent-review`
for inspection. It was not run, installed into `poly1`, or connected to any
wallet/API keys.

## Summary

PolyAgent is a news-driven Polymarket pipeline:

- real-time Twitter / Telegram / RSS news ingestion
- keyword matching from news to active niche markets
- LLM classification as bullish / bearish / neutral
- materiality score instead of direct probability estimation
- edge detection with capped quarter-Kelly sizing
- SQLite logging for trades, news events, latency, and calibration

The most useful ideas for `poly1` are not its execution layer. Our execution
path is stronger because it already uses current CLOB v2/deposit-wallet support,
orderbook-aware pricing, slippage caps, and existing risk gates.

## Good Ideas To Borrow

1. Classification instead of probability estimation

PolyAgent asks whether breaking news makes a market more likely to resolve YES,
NO, or is irrelevant. This is cleaner than asking an LLM for a raw probability.
For `poly1`, use this as an additional signal alongside the existing prompt,
not as a replacement.

2. Materiality threshold

PolyAgent requires materiality above a threshold before producing a signal. This
could reduce weak news-driven trades in `poly1`.

3. News latency tracking

PolyAgent logs news latency, classification latency, and total latency. This is
useful for deciding whether a fast-news strategy is actually competitive.

4. Calibration table

PolyAgent records classification outcomes and later checks whether the
classification direction matched market movement/resolution. This is useful for
dashboard analytics and for disabling weak sources/categories.

5. Niche-market filter

PolyAgent intentionally targets lower-volume markets. We should not copy the
exact thresholds blindly, but the idea is useful as a configurable filter.

## Do Not Copy Directly

1. Live executor

PolyAgent uses an older-looking CLOB client path and posts GTC orders. Do not
copy this. Keep using `agents/polymarket/polymarket.py` in `poly1`.

2. Daily loss calculation

PolyAgent's `get_daily_pnl()` treats executed amount as spend/loss, not
mark-to-market P&L. This has the same weakness we already observed in our cash
drawdown gate. Do not adopt it as-is.

3. Simple keyword matching

The matcher is fast but shallow. It can be used as a first-pass filter, but not
as final relevance proof.

4. Quarter-Kelly implementation

The sizing is capped, but it derives bankroll from daily loss limit. For `poly1`,
keep explicit wallet/bucket sizing instead.

## Recommended Integration Path

Safe first steps:

- Add a `classification_direction` and `materiality` field to logged trade
  recommendations.
- Add a dry-run-only classifier experiment for news events.
- Add dashboard panels for classification accuracy and latency.
- Add source/category calibration before allowing the signal to affect live
  sizing.

Avoid initially:

- replacing the executor
- enabling any PolyAgent logic to place real orders
- letting classification bypass existing risk gates

## Current Decision

No code from PolyAgent should be merged directly right now. The best next
improvement is a read-only/dry-run "news classification signal" module that
writes analytics to the dashboard and trade log. After enough observations, it
can be considered as an input to `poly1` scoring.

## Implemented In `poly1`

Implemented on 2026-05-06 as a dry-run analytics feature only:

- `agents/application/news_signal.py`
  - RSS/NewsAPI headline ingestion
  - keyword-based first-pass market matching
  - LLM classification into `bullish` / `bearish` / `neutral`
  - materiality and latency capture
- `scripts/python/news_signal.py`
  - one-shot collector CLI
  - supports RSS fallback when `NEWSAPI_API_KEY` is not configured
  - supports manual `--headline` inputs
- `trade_log.db`
  - new `news_signals` table
- Streamlit dashboard
  - new "Dry-run news classification signals" section under the LLM tab

Safety constraints:

- No execution code is called.
- No order placement is possible from this path.
- Signals are analytics only and do not affect `poly1`, `scalper`, or `swarm`
  strategy decisions.
- The first collector run exposed overly broad matching, so matching now
  requires at least two non-generic keyword hits.

Verification:

```text
python3 -m unittest tests.test_news_signal.TestNewsSignalLogic \
  tests.test_news_signal.TestNewsSignalStorage tests.test_trader.TestTradeLog -v

docker compose run --rm trader python -m unittest \
  tests.test_news_signal tests.test_trader.TestTradeLog -v
```

Both focused suites passed. A small RSS collector run with stricter matching
inserted `0` rows, which is acceptable when no sufficiently relevant news/market
pair is found.
