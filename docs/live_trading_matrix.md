# Live Trading Matrix

Updated: 2026-05-20

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

Current server runtime is **freeze**. The matrix below describes the intended
live roles after pre-live checks pass; it is not an indication that live entry
is currently open.

| Layer | Service | Live Entry | Matrix Decision | Reason |
| --- | --- | --- | --- | --- |
| Brain | meta_brain | No direct orders | Required | Central weighted approval layer. |
| Entry | btc_5min | Frozen now | Probe candidate | Fast crypto 5m/straddle path, MetaBrain weighted gate, Gamma/CLOB healthy. |
| Entry | scalper | Frozen now | Probe candidate | 15m crypto scalper, MarketBrain plus MetaBrain gate, Gamma/CLOB healthy. |
| Entry | btc_daily | Frozen now | Cautious only | Mean reversion with brain gate; Tavily is non-blocking but OpenAI is degraded. |
| Entry | near_resolution | Frozen now | Cautious only | MetaBrain gate and strict liquidity/risk checks. |
| Entry | wallet_follow | Frozen now | Signal-dependent | Depends on wallet signals and MetaBrain proof. |
| Entry | trader | Frozen now | Hold until OpenAI fixed or Anthropic-only policy approved | Main LLM trader can use Anthropic fallback, but OpenAI is currently 429. |
| Entry | news_shock | Frozen now | Disabled | Tavily/news stack is intentionally budget-limited; no news-driven live entries. |
| Entry | external_conviction_api | Frozen now | Disabled for live | External evidence collected; API entry held until signal quality is revalidated. |
| Signal | external-conviction-* | No direct runtime entry except API variant | Signal only | Feeds conviction JSONL into MetaBrain. |
| Signal | external-conviction-alpaca | No direct orders | Signal only | Alpaca market-data bars feed MetaBrain as external tape evidence. |
| Signal | external-conviction-openbb | No direct orders | Shadow only | Optional OpenBB market-data provider for equities, macro proxies, commodities, and crypto. Fails closed if dependency/data is unavailable. |
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

- Tavily: intentionally disabled by runtime; daily budget guard is active.
- OpenAI: unavailable, quota error `HTTP 429 insufficient quota`.
- Anthropic: available and used as LLM fallback.
- Hermes: available and healthy.
- Alpaca: available; `BTC/USD` market data bars verified.
- Gamma API: available.
- Polymarket CLOB read API: available.
- TradingView options page: reachable; local parsed snapshot is currently missing, so MetaBrain treats it as neutral.

See [PRE_LIVE_QA_REPORT_2026-05-20.md](PRE_LIVE_QA_REPORT_2026-05-20.md)
for the full pre-live QA record.
