# AlphaInsider Strategy Rankings Integration

AlphaInsider is a read-only research source for discovering strategy families
that are showing forward-tested performance outside poly1. It is not a direct
execution source and must not bypass MetaBrain, EV gates, or shadow validation.

## What It Provides

- Strategy marketplace search via `searchStrategies`.
- Timeframes: `day`, `week`, `month`, `year`, `five_year`.
- Sorts: `performance`, `top`, `trending`, `popular`, `newest`.
- Useful fields: `rank_performance`, `rank_top`, `max_drawdown`,
  `past_value`, `subscriber_count`, `type`, `description`, `categories`.

## poly1 Usage

Run locally or on the server with an environment token:

```bash
ALPHAINSIDER_API_TOKEN=... \
python scripts/alphainsider_strategy_rankings.py \
  --timeframes month,year,five_year \
  --sort performance \
  --limit 50 \
  --out data/alphainsider_strategy_rankings.json
```

The output intentionally excludes the token. It groups strategies into families
such as `trend_momentum`, `vwap_mean_reversion`, `supply_demand`,
`market_making`, `volatility`, `machine_learning`, and `event_sentiment`.

## Trading Rule

AlphaInsider rankings are evidence, not permission to trade.

1. Pull rankings.
2. Identify high-quality strategy families with low drawdown and repeated
   strength across `month`, `year`, and `five_year`.
3. Map those families to poly1 strategy candidates.
4. Run poly1 shadow/backtest.
5. Only then allow MetaBrain to use them as a supporting or solo evidence
   source if our own reliability table confirms edge.

## Caveats

- Past and forward performance can decay.
- Some strategies are opaque and cannot be replicated without subscription or
  TradingView/PineScript details.
- AlphaInsider performance is mostly stocks/crypto; Polymarket requires a
  translation layer from asset behavior into event probability.
