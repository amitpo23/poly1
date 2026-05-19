# Live Session Summary — 2026-05-19

## Trading Principles

- Polymarket percentage is treated as market entry price, not truth.
- The brain's internal probability is the decision source.
- Directional entries require:
  - internal brain probability at or above the live threshold, currently 52%;
  - positive edge versus Polymarket price;
  - configured minimum edge: 2 percentage points.
- Formula:

```text
edge = internal_brain_probability - polymarket_entry_price
```

## Entry Gates

- `BTC_5MIN_MIN_CONFIDENCE=0.52`
- `BTC_5MIN_MIN_EDGE_PCT=0.02`
- `META_BRAIN_MIN_EDGE_PCT=0.02`
- `META_BRAIN_CRYPTO_STRADDLE_MIN_SCORE=0.52`
- `MARKET_BRAIN_GENERAL_MIN_SCORE=0.52`
- `MARKET_BRAIN_SCALPER_MIN_EDGE_SCORE=0.52`
- `MARKET_UNIVERSE_MIN_WINRATE=0.52`
- `SCALPER_MIN_UNIVERSE_WINRATE=0.52`

## Exit Policy

- Stop loss: 3%.
- Fast take-profit starts at 5%.
- Hard profit cap: 25%.
- Position manager poll: every 10 seconds.
- LLM/brain exit recheck: every 60 seconds.
- BTC 5m max hold: 120 seconds.
- BTC 5m straddle max hold: 210 seconds.
- General max hold: 6 hours safety ceiling only.

## Telegram Policy

- Send immediate alert on every live buy/fill.
- Send immediate alert on every live sell/exit.
- Send hourly PnL/dashboard report.
- Do not send skipped gates or brain-only checks.
- New bot verified after `/start`; `sendMessage` succeeded.
- Details: `docs/TELEGRAM_REPORTING_POLICY_2026-05-19.md`.

## Integrations

- Hermes forecast service is available and used by MetaBrain/straddle logic.
- Anthropic is available as LLM fallback.
- OpenAI API is degraded due quota.
- Tavily is disabled in runtime due reported quota usage/limit.
- Gamma and Polymarket CLOB are active.

## Live Run Notes

- The 52% threshold did increase trade activity.
- Latest observed run since 15:07 UTC had 11 BTC 5m opened positions:
  - 3 closed take-profit.
  - 7 closed stop-loss.
  - 1 closed dust.
  - Estimated PnL from `position_marks`: about -1.83 USDC.
- Trading was halted by `trading_supervisor` due a settlement reconciliation
  guard on a recoverable/on-chain token that looked unmanaged.
- New live trading should remain blocked until the HALT/settlement issue is
  reconciled or explicitly waived.

## Source Of Truth

- Server: `trader@83.229.82.193:/srv/poly1`
- Runtime env: `/srv/poly1/deploy/.env.runtime`
- Secrets env: `/srv/poly1/.env`
- Runtime control: `/srv/poly1/data/runtime_control.json`
- HALT guard: `/srv/poly1/data/HALT`
- Trade ledger: `/srv/poly1/data/trade_log.db`
