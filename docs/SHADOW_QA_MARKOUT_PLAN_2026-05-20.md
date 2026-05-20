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
- When the market-maker shadow agent finds no actionable market, it records an
  auditable reject row instead of staying silent.
- `scripts/update_shadow_markouts.py` annotates shadow decisions after
  1/3/5/15/60 minutes using stored orderbook snapshots.

## Promotion Standard

Do not promote a fast strategy back to live until a shadow window has:

- at least 20 shadow decisions,
- no concentration in only one or two markets,
- positive aggregate markout after spread,
- no repeated same-market loop,
- clear reason distribution for rejects,
- green `trading_stability_preflight.py --mode freeze` before and after.

## Useful Commands

```bash
python scripts/update_shadow_markouts.py --db data/trade_log.db --horizons 1,3,5,15 --limit 500
python scripts/analyze_shadow_probe.py --db data/trade_log.db --since <ISO_TS> --limit 100
python scripts/trading_stability_preflight.py --mode freeze
```
