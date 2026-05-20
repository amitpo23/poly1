# Shadow QA Markout Plan - 2026-05-20

## Purpose

Before returning capital to live trading, fast strategies must prove that they
can beat spread, slippage, and repeat-entry failure in shadow mode.

## Implemented Controls

- `scanner_executor` no longer treats 5-minute crypto shadow probes as taker
  fills. Fast markets now prefer `SHADOW_QUOTE` maker-style evidence.
- Taker entries are rejected when the bid/ask spread alone would put the
  position at or beyond the configured stop threshold.
- Shadow duplicate protection blocks repeated probes on the same market/token
  for a cooldown window.
- `crypto_5m_market_maker_shadow` scans a wider current/next 5-minute universe
  across BTC, ETH, SOL, XRP, DOGE, and BNB.
- The market-maker probe now has a directional mispricing layer: when
  Binance/OKX crypto tape is strong enough, it estimates fair UP/DOWN
  probability and only approves maker bids whose `fair_probability - maker_bid`
  clears `CRYPTO_5M_MM_SHADOW_MIN_DIRECTIONAL_EDGE_PCT`.
- If tape is neutral or weak, it can still collect pure spread-capture evidence
  under `CRYPTO_5M_MM_SHADOW_QUOTE_BOTH_WHEN_NEUTRAL=true`; those decisions are
  scored lower and must prove themselves through markouts.
- When the market-maker shadow agent finds no actionable market, it records an
  auditable reject row instead of staying silent.
- `scripts/update_shadow_markouts.py` annotates shadow decisions after
  1/3/5/15/60 minutes using stored orderbook snapshots.

## Strategy: Crypto 5m Mispricing Maker

This is the system's version of the "trade faster than a human can click"
Polymarket 5-minute bot:

1. `market_universe` finds current and next crypto 5m Up/Down markets.
2. `orderbook-monitor` keeps fresh CLOB books for both UP and DOWN tokens.
3. `crypto_exchange_tape` reads external BTC/ETH/SOL/XRP/DOGE/BNB tape.
4. `crypto_5m_market_maker_shadow` computes:
   - maker bid and maker ask inside the current spread,
   - potential spread-capture cents,
   - fair UP/DOWN probability from tape when the tape is strong,
   - directional edge as `fair_probability - maker_bid`,
   - book depth and quote quality.
5. The strategy remains shadow-only until markouts show that quotes would have
   positive expected value after spread and adverse selection.

This is deliberately not a blind "quote both sides forever" strategy. Strong
external tape can veto one side and approve only the side with measurable
mispricing.

## Promotion Standard

Do not promote a fast strategy back to live until a shadow window has:

- at least 20 shadow decisions,
- no concentration in only one or two markets,
- positive aggregate markout after spread,
- positive aggregate directional-edge markout when `external_signal_strong=true`,
- no repeated same-market loop,
- clear reason distribution for rejects,
- green `trading_stability_preflight.py --mode freeze` before and after.

## Useful Commands

```bash
python scripts/update_shadow_markouts.py --db data/trade_log.db --horizons 1,3,5,15 --limit 500
python scripts/analyze_shadow_probe.py --db data/trade_log.db --since <ISO_TS> --limit 100
python scripts/trading_stability_preflight.py --mode freeze
```
