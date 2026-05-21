# Indicator Authority Policy

The trading system does not use flat averaging as the final decision model.
Each indicator is treated as an evidence source with an authority tier.

## Authority Tiers

### Solo Expert

A solo expert can lead a trade decision without waiting for consensus, but it
still must pass MetaBrain, EV/spread gates, RiskGate, duplicate checks, and an
exit plan.

Current solo-capable sources:

- `wallet`: proven Polymarket/on-chain wallet activity.
- `cross_market`: cross-venue pricing/fair-value divergence.
- `equity_fv`: options-derived fair value.
- `alpaca_market_data`: external stock/crypto market tape.
- `openbb_market_data`: external market data research signal.
- `crypto_exchange_tape`: Binance/OKX crypto tape.
- `alphainsider_strategy`: ranked AlphaInsider strategy evidence.

### Supporting Evidence

Evidence can improve or reduce confidence but cannot lead alone unless it earns
solo status. Examples: ordinary news, unproven LLM conviction, unproven strategy
rankings, low-sample provider output.

### Veto / Conflict

A reliable opposing source can block an otherwise good trade. This prevents one
strong-looking indicator from overriding a better proven counter-signal.

## Wallet Solo Rule

A wallet can lead without local history only when external proof is present:

- external win-rate >= `EXPERT_WALLET_EXTERNAL_MIN_WINRATE`
- external trades >= `EXPERT_WALLET_EXTERNAL_MIN_TRADES`
- profit >= `EXPERT_WALLET_EXTERNAL_MIN_PROFIT_USDC`

Default thresholds are 70% win-rate, 50 trades, and 100 USDC profit.

## AlphaInsider Solo Rule

AlphaInsider is a strategy-discovery/ranking source. It can be a decisive
indicator only after the signal is mapped to a relevant market and satisfies:

- probability >= `EXPERT_SOLO_MIN_PROB`
- confidence >= `EXPERT_EXTERNAL_SOLO_MIN_CONFIDENCE`
- return >= `EXPERT_ALPHAINSIDER_MIN_RETURN_PCT`
- max drawdown <= `EXPERT_ALPHAINSIDER_MAX_DRAWDOWN`
- rank <= `EXPERT_ALPHAINSIDER_MAX_RANK`
- fresh enough for `EXPERT_EXTERNAL_SOLO_MAX_AGE_SEC`

Default thresholds: 10% return, max 35% drawdown, top-25 rank.

## Non-Negotiable Rule

No indicator, even a solo expert, bypasses execution safety:

1. MetaBrain evidence route.
2. Raw EV / net EV after costs.
3. Orderbook freshness and spread/depth checks.
4. RiskGate and reserve checks.
5. Cross-agent duplicate/concentration checks.
6. Explicit exit plan.

This is how strong indicators become decisive without turning the system into
blind copy-trading.
