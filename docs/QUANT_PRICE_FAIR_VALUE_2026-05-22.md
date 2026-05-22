# Quant Price Fair Value Signal

Date: 2026-05-22

## Purpose

This is a small, scoped integration of the useful part of Heston-style digital pricing for Polymarket price-threshold markets.

It does not replace MetaBrain, RiskGate, DecisionCouncil, execution-quality checks, or position management. It only adds one more calibrated probability source when the market question contains:

- a supported asset, currently BTC/ETH/SOL/XRP/DOGE/BNB,
- a clear price threshold,
- a clear above/below direction,
- a current external tape price,
- a valid time horizon.

If any of those are missing, the signal returns neutral and is excluded by informed-only weighting.

## Model

The implementation uses:

- lognormal digital probability: `P(S_T > K)` or `P(S_T < K)`,
- realized volatility from crypto exchange 1-minute closes,
- Bayesian-style volatility shrinkage toward a configurable prior,
- edge vs the Polymarket entry price,
- confidence based on absolute edge.

This is intentionally not a full Heston calibration. Full Heston needs an options surface, volatility-of-volatility, correlation, and calibration stability. Without that data, it would be overfitting theater. The current version gives us the benefit we need: a bounded probability for fast price markets.

## MetaBrain Behavior

New source:

- `quant_price_fair_value`
- signal source IDs like `quant_fv:BTC`
- feature prefix: `quant_fv_*`

It can become `internal_prob_source="quant_price_fair_value"` only when enabled and confident enough. The normal EV gates still apply:

- `META_BRAIN_MIN_EDGE_PCT`
- `META_BRAIN_MIN_RAW_EV`
- execution-quality checks
- RiskGate

It cannot trade by itself outside the existing pipeline.

## Environment

Relevant env vars:

- `META_BRAIN_QUANT_FV_ENABLED`
- `META_BRAIN_WEIGHT_QUANT_FV`
- `META_BRAIN_QUANT_FV_CALIBRATED_ENABLED`
- `QUANT_FV_MIN_CONFIDENCE`
- `QUANT_FV_MIN_ABS_EDGE`
- `QUANT_FV_VOL_PRIOR_WEIGHT`
- `QUANT_FV_DEFAULT_ANNUAL_VOL`
- `QUANT_FV_DEFAULT_ANNUAL_VOL_BTC`
- `QUANT_FV_DEFAULT_ANNUAL_VOL_ETH`
- `QUANT_FV_MIN_HOURS_TO_CLOSE`
- `QUANT_FV_MAX_HOURS_TO_CLOSE`

## Sizing

`agents/application/sizing.py` now includes `robust_kelly_fraction`, which dampens Kelly by estimation variance:

`f_hat = f* / (1 + lambda * Var(f*))`

Existing callers are unchanged unless they pass `probability_variance`.

## Guardrails

- No external orders.
- No new network calls beyond the existing crypto tape reader.
- No effect on non-price markets.
- No effect when the price target or direction is ambiguous.
- No solo expert status unless future outcome history proves reliability through the existing EvidenceRouter.
