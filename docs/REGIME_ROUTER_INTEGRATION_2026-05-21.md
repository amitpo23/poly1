# Regime Router Integration - 2026-05-21

## Goal

Turn the quant-research ideas we reviewed into a small production-safe layer:
before a strategy receives capital, the system should know whether the current
market state fits that strategy family.

This is not a new predictor. It is a deterministic routing layer that connects:

- market microstructure features already produced by `market_microstructure.py`
- the canonical strategy families in `strategy_catalog.py`
- scanner executor decisions and journal rows

## What Was Added

### 1. `agents/application/regime_router.py`

The router maps a market regime to strategy-family suitability.

Current regimes:

- `trending`
- `mean_reverting`
- `stretched`
- `mixed`
- `unknown`

Current family mapping:

- `trend_following`
- `mean_reversion`
- `market_microstructure`
- `market_making`
- `event_driven_relative_value`
- `statistical_arbitrage`
- `volatility_relative_value`
- `news_sentiment_event_driven`
- `machine_learning`
- `other`

Examples:

- `trending` prefers momentum/event/news flow and can block mean-reversion when confidence is high.
- `mean_reverting` prefers VWAP/fade/spread/pairs logic and can block trend-following when confidence is high.
- `stretched` prefers reversal/volatility confirmation and blocks trend chasing.
- `unknown` does not block, but carries lower risk and higher edge requirement metadata.

### 2. Scanner Executor Metadata

`scanner_executor` now records regime/family metadata into both:

- `brain_decisions.features_json`
- `decision_journal.features_json`

Fields include:

- `strategy_family`
- `regime`
- `regime_confidence`
- `regime_preferred_families`
- `regime_allowed_families`
- `regime_blocked_families`
- `regime_risk_multiplier`
- `regime_edge_multiplier`
- `regime_family_preferred`
- `regime_family_allowed`
- `regime_reason`

This means every reject/approval can later be backtested by regime:

- Which families worked in trending markets?
- Which families failed in mean-reverting markets?
- Did unknown-regime trades underperform?
- Did AlphaInsider-proven families work only in specific regimes?

### 3. Optional Hard Gate

Default behavior is observability only.

To make regime routing a hard gate:

```bash
SCANNER_EXECUTOR_ENFORCE_REGIME_ROUTER=true
```

When enabled, a high-confidence regime mismatch is rejected with:

```text
strategy_family_blocked_by_regime
```

This default is intentionally conservative. It lets us gather evidence before
the router starts blocking live trades.

## Why This Helps

The system was already asking "is this trade good?".

This adds the missing question:

```text
Is this the right kind of strategy for this kind of market?
```

That matters because the same signal quality can behave differently by regime:

- momentum works better in trending markets
- mean reversion works better in range/negative-autocorrelation markets
- market making is attractive in stable spread/liquidity regimes
- volatility/fair-value models need event or volatility states

## Next Integration Steps

### Phase 1 - Evidence Collection

Run shadow/live probes with the router in metadata-only mode.

Then query by:

- `strategy_family`
- `regime`
- `regime_family_preferred`
- `signal_source`
- 1/3/5/15 minute markout
- realized trade PnL

### Phase 2 - Scorecard

Add a `strategy_regime_scorecard.json` artifact:

```json
{
  "family": "trend_following",
  "regime": "trending",
  "samples": 120,
  "winrate": 0.58,
  "avg_markout_pct": 0.014,
  "promotion_state": "promotable"
}
```

### Phase 3 - Allocator

Feed `regime_risk_multiplier` and the scorecard into the allocator.

Example:

- trend-following in trending market: normal size
- trend-following in mean-reverting market: 0 size or shadow-only
- mean-reversion in stretched market: allow, but require stronger exit plan

### Phase 4 - Event Probability Simulator

Use the Monte-Carlo repo idea only as a concept:

- simulate probability of crossing target before expiry
- estimate first-touch probability for 5m/15m crypto markets
- compare simulator probability against Polymarket price

This should be implemented dependency-free or with existing project libraries,
not by copying the educational repo.

## Current Safety Position

This change does not open live trading and does not loosen existing gates.
It adds auditable decision metadata and an optional hard gate behind an env var.
