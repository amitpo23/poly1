# Live Trading Matrix

Updated: 2026-05-19

## Global Rules

- Every live entry must pass MarketBrain or MetaBrain before order placement.
- Entry threshold is 52% weighted probability/score across the live entry
  gates.
- Directional crypto entries require positive internal edge: the brain's
  estimated probability for the chosen side must exceed the live Polymarket
  entry price by at least 2 percentage points.
- General MetaBrain-gated entries use the same rule when a side price is
  available: internal weighted probability minus market entry price must be at
  least 2 percentage points.
- Position exits are managed centrally by position_manager.
- Stop loss: 3%.
- Fast take profit target: from 5%, with strategy-specific faster exits allowed.
- Hard take profit cap: 25%.
- Max hold is a safety ceiling only; preferred behavior is fast exit unless the brain has strong hold evidence.
- Max trades per hour: 100.
- Max allocation per agent: 50% of wallet cap through runtime control and RiskGate.
- Telegram sends every live buy/fill and every sell/exit immediately.
- Telegram sends a full PnL/dashboard report once per hour.

## Current Runtime Matrix

| Layer | Service | Live Entry | Matrix Decision | Reason |
| --- | --- | --- | --- | --- |
| Brain | meta_brain | No direct orders | Required | Central weighted approval layer. |
| Entry | btc_5min | Yes | Enabled | Fast crypto 5m/straddle path, MetaBrain weighted gate, Gamma/CLOB healthy. |
| Entry | scalper | Yes | Enabled | 15m crypto scalper, MarketBrain plus MetaBrain gate, Gamma/CLOB healthy. |
| Entry | btc_daily | Yes | Enabled small-size | Mean reversion with brain gate; Tavily context is non-blocking while quota is exhausted. |
| Entry | near_resolution | Yes | Enabled | MetaBrain gate and strict liquidity/risk checks. |
| Entry | wallet_follow | Yes | Enabled | MetaBrain gate; depends on wallet signals, not Tavily as hard requirement. |
| Entry | trader | Yes | Enabled cautious | Main LLM trader uses Anthropic fallback while OpenAI quota is exhausted. |
| Entry | news_shock | Yes | Disabled for this run | News/Tavily is quota-blocked; no news-driven live entries until restored. |
| Entry | external_conviction_api | Yes | Disabled for this run | External evidence still collected, but API entry is held back while Tavily/OpenAI are degraded. |
| Signal | external-conviction-* | No direct runtime entry except API variant | Signal only | Feeds conviction JSONL into MetaBrain. |
| Signal | external-conviction-alpaca | No direct orders | Signal only | Alpaca market-data bars feed MetaBrain as external tape evidence. |
| Signal | external-conviction-crypto-tape | No direct orders | Signal only | Binance/OKX fast crypto tape feeds MetaBrain for crypto Up/Down timing. |
| Signal | market_universe | No direct orders | Signal only | Maintains focused market list and scalper candidates. |
| Signal | market_scanner | No direct orders | Signal only | Opportunity router. |
| Signal | news_signal | No direct orders | Signal only | Anthropic fallback available; news entry remains disabled until Tavily restored. |
| Signal | wallet_watcher | No direct orders | Signal only | Produces wallet-follow signals. |
| Exit | position_manager | Sell only | Required | Enforces fast exit, stop loss, timeout, and LLM exit checks. |
| Ops | trading_supervisor | No orders | Required | Enforces HALT/critical issue detection. |
| Ops | settlement_reconciler | Redeem/reconcile | Required | Keeps resolved positions from being forgotten. |
| Ops | telegram_reporter | No orders | Required | Hourly dashboard and critical alerts only. |

## Degraded Integrations

- Tavily: unavailable, quota error `HTTP 433 pay-as-you-go limit`.
- OpenAI: unavailable, quota error `HTTP 429 insufficient quota`.
- Anthropic: available and used as LLM fallback.
- Gamma API: available.
- Polymarket CLOB read API: available.
- TradingView options page: reachable; local parsed snapshot is currently missing, so MetaBrain treats it as neutral.
