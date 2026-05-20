# Pre-Live QA Report — 2026-05-20

Status: **NO LIVE TRADING OPENED**

This document captures the pre-live quality pass after the Tavily budget
lockdown, TradingView snapshot formalization, Alpaca signal hookup, and
MetaBrain calibrated-probability enforcement.

## Executive Summary

The system is operationally stable in freeze mode and ready for another
controlled paper/shadow probe. It is not yet recommended for real live trading
until the two operator-owned gaps are completed:

1. OpenAI quota/API access restored, or a conscious decision is made to run
   with Anthropic/Hermes as the only LLM decision path.
2. TradingView options snapshot supplied from the ES options chain, so the
   TradingView macro signal is real rather than neutral.

Current server state:

- Server source of truth: `/srv/poly1`
- Server HEAD: `23f914d fix: probe Hermes health endpoint correctly`
- Runtime mode: `freeze`
- HALT file: present
- Entry agents: frozen
- Exit manager: enabled
- Open live positions: `0`
- Portfolio equity: `27.302273 USDC`
- Local full test suite: `564 tests OK`

## QA Commands Run

Local:

```bash
.venv/bin/python -m unittest discover -s tests
```

Server:

```bash
ssh trader@83.229.82.193 'cd /srv/poly1 && git log -1 --oneline'
ssh trader@83.229.82.193 'cd /srv/poly1 && cat data/runtime_control.json'
ssh trader@83.229.82.193 'cd /srv/poly1 && ls -l data/HALT'
ssh trader@83.229.82.193 'cd /srv/poly1 && docker compose exec -T trader python scripts/trading_stability_preflight.py --mode freeze'
ssh trader@83.229.82.193 'cd /srv/poly1 && docker compose exec -T trader python scripts/check_api_health.py'
ssh trader@83.229.82.193 'cd /srv/poly1 && docker compose exec -T trader python scripts/live_equity_guard.py --drawdown-limit 0.75 --json'
ssh trader@83.229.82.193 'cd /srv/poly1 && docker compose exec -T trader python scripts/update_shadow_markouts.py'
```

## Stability Preflight

Result: **PASS**

Important checks:

- Entry agents frozen: OK
- `EXECUTE_MAINTAIN=true`: OK
- Supervisor enforces HALT: OK
- Brain approval required: OK
- Max trades per hour capped at `100`: OK
- Max allocation per agent capped at `50%`: OK
- Exit revalidation: `MAINTAIN_POLL_SEC=10`,
  `MAINTAIN_LLM_EXIT_INTERVAL_SEC=60`: OK
- Runtime config hash matches: OK
- HALT present: OK
- Disk: `73.5%` used, below `85%` threshold
- DB exists and backup freshness OK
- Open positions accounted: `open=0`
- Settlement critical rows: none

## API and Integration Health

| Integration | Status | Meaning |
| --- | --- | --- |
| Polymarket Gamma | PASS | Market discovery API works. |
| Polymarket CLOB | PASS | Orderbook/read API works. |
| Polygon wallet key | PASS | Wallet secret exists in server env. |
| Builder keys | PASS | Builder credentials exist. |
| Alpaca market data | PASS | Returned `20` BTC/USD crypto bars. |
| Anthropic | PASS | LLM fallback is available. |
| Hermes forecast | PASS | Local Hermes bridge responds on `/healthz`. |
| LLM decision path | PASS | OpenAI is unavailable, but Anthropic/Hermes fallback exists. |
| Tavily | WARN | Disabled intentionally. Budget guard active. |
| TradingView options | WARN | Page reachable, snapshot missing. |
| OpenAI | FAIL | `HTTP 429` quota/billing limit. |
| NewsAPI | WARN | Not configured. Optional. |
| Nansen | WARN | Not configured. Optional. |
| Wallet Master | WARN | Not configured. Optional. |
| Polifly bridge | WARN | Not configured. Optional. |

The only hard blocker for the intended full-brain live mode is OpenAI quota.
TradingView is not a blocker for basic operation, but it is a blocker for the
specific options-chain macro signal the user asked to include.

## Docker and Services

All key services were up and healthy at the QA pass:

- `poly1`
- `poly1-market-scanner`
- `poly1-scanner-executor`
- `poly1-market-universe`
- `poly1-orderbook-monitor`
- `poly1-position-manager`
- `poly1-trading-supervisor`
- `poly1-settlement-reconciler`
- `poly1-telegram-reporter`
- `poly1-hermes-forecast`
- `poly1-external-conviction-alpaca`
- `poly1-external-conviction-tradingview`
- `poly1-external-conviction-crypto-tape`
- `poly1-external-conviction-technical`
- `poly1-external-conviction-whale`
- `poly1-wallet-follow`
- `poly1-wallet-watcher`
- `poly1-news-signal`
- `poly1-news-shock`
- `poly1-btc-5min`
- `poly1-scalper`
- `poly1-crypto-5m-market-maker-shadow`

Noted but non-blocking: several containers log a `tini` subreaper warning.
The containers are healthy, but this is worth cleaning later in compose for
process hygiene.

## Current Decision Flow Observed

The scanner is active in freeze/shadow context:

- Fetches roughly `300` markets per cycle.
- Filters roughly `19-22` candidates.
- Scores candidates.
- Often finds `2` brain-approved opportunities.
- Dispatches scanner trade opportunities and near-resolution hints.

The scanner executor is correctly rejecting entries that lack calibrated
probability:

```text
reason=probability_not_calibrated
```

This is expected and desirable after the MetaBrain fix. It means the system no
longer treats a high rank score as a true probability. A market can look
interesting, but it cannot become a live entry unless the probability is
calibrated by a trusted source such as cross-market/equity FV/Alpaca/crypto
tape/wallet proof.

## Orderbook and Universe Health

Orderbook monitor:

- Actively updates orderbooks every few seconds.
- Latest observed cycles updated `16-20` tokens.
- Recent `errors=0`.
- `orderbook_latest` contained `1677` rows.
- Latest orderbook timestamp observed around `2026-05-20T16:30:50Z`.

Market universe:

- Actively persists candidates.
- Recent cycles persisted roughly `44-60` candidates.

This layer is working and should be available for the next controlled probe.

## Shadow and Markout Status

`scripts/update_shadow_markouts.py` ran successfully.

Output:

- 1m eligible: `0`
- 3m eligible: `0`
- 5m eligible: `0`
- 15m eligible: `0`
- updated: `0`
- missing snapshot: `0`
- live fallback: `0`

Interpretation: there were no matured shadow decisions needing markout update
at that instant. This is not an error.

Recent shadow behavior:

- Recent `scanner_executor` shadow fills existed before the freeze window.
- Current scanner_executor decisions are rejected as
  `probability_not_calibrated`.
- `crypto_5m_market_maker_shadow` often reports
  `no_candidate_in_time_window`, meaning no valid maker quote candidate in its
  strict time/market window.

## Journal and Position Accounting

Preflight and equity guard both report no open live positions.

Equity guard:

```json
{
  "cash_usdc": 27.302273,
  "equity_usdc": 27.302273,
  "open_mtm_usdc": 0,
  "open_positions": [],
  "breached": false
}
```

Important nuance:

The `trades` table still contains historical rows with statuses such as
`btc_5min_open` from 2026-05-19. Preflight correctly excludes these when a
terminal row exists after the open row for the same token, and the equity guard
shows no live exposure.

Conclusion:

- No current unmanaged live position was found.
- There is historical journal noise that can confuse simple status-count
  reports.
- Before long-running live sessions, a reporting cleanup/migration should mark
  old superseded open rows as archived or expose a canonical `is_active`
  view.

## Tavily Policy

Tavily is intentionally disabled in runtime:

- `TAVILY_ENABLED=false`
- `TAVILY_DAILY_LIMIT=5`
- `TAVILY_CACHE_TTL_SEC=21600`
- `TAVILY_MIN_QUERY_INTERVAL_SEC=900`
- `TAVILY_MAX_RESULTS=2`
- `TAVILY_CRITICAL_ONLY=true`

Health checks do not spend Tavily calls unless
`TAVILY_HEALTH_REAL_CALL=true` is explicitly set.

This protects against a repeat of the 3000-query day.

## TradingView Requirements

TradingView options page is reachable, but the system needs a structured
snapshot before the signal can affect MetaBrain.

Snapshot path on server:

```bash
/app/data/tradingview_options_es1_snapshot.json
```

Write command:

```bash
python scripts/write_tradingview_options_snapshot.py \
  --path data/tradingview_options_es1_snapshot.json \
  --put-call-ratio 0.82
```

Or:

```bash
python scripts/write_tradingview_options_snapshot.py \
  --path data/tradingview_options_es1_snapshot.json \
  --put-volume 8200 \
  --call-volume 10000
```

Without this, TradingView remains neutral and cannot approve a trade.

## OpenAI Requirements

OpenAI is currently failing with `HTTP 429`.

Impact:

- Some LLM paths may skip or fall back.
- Anthropic and Hermes are available, so the brain is not fully blind.
- Full intended live mode still expects OpenAI quota restored or an explicit
  policy decision to run Anthropic/Hermes-only.

## Go / No-Go

### Allowed Now

- Continue frozen monitoring.
- Run another shadow QA/probe.
- Run paper-mode multi-agent tests.
- Generate TradingView snapshot once values are supplied.
- Re-run API health after OpenAI quota is restored.

### Not Recommended Yet

- Full live trading with all agents.
- News-driven live entries relying on Tavily.
- TradingView-led entries without a fresh snapshot.
- OpenAI-primary live mode while quota remains `429`.

### Recommended Next Step

After OpenAI quota and TradingView snapshot are supplied:

1. Run `scripts/check_api_health.py` again.
2. Run `scripts/trading_stability_preflight.py --mode freeze`.
3. Start a **shadow-only 20-30 minute probe** across the relevant agents.
4. Update markouts at 1m/3m/5m/15m.
5. If calibrated-probability entries appear and markouts are acceptable, open a
   small controlled live probe with `scanner_executor` only.

## Current Conclusion

The system is much cleaner than yesterday:

- One server source of truth.
- Runtime freeze is enforced.
- No unmanaged live positions.
- Orderbook and market-universe infrastructure are alive.
- Tavily is budget-protected.
- Alpaca and Hermes are active.
- MetaBrain refuses uncalibrated pseudo-probabilities.

The remaining work before live is not code hygiene; it is signal completeness:
OpenAI quota plus TradingView snapshot.
