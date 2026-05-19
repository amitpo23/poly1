# API Health - 2026-05-19

Live checks were run on `trader@83.229.82.193:/srv/poly1` after rebuilding the
Docker image and recreating the poly1 services.

## Model Policy

- Primary OpenAI model is now configured as `gpt-4o`.
- Anthropic fallback model is now configured as `claude-sonnet-4-5-20250929`.
- Model defaults are centralized in `agents/application/llm_config.py`.
- LLM callers that previously defaulted to mixed/older models now read the same
  canonical defaults.

## Health Probe

Run:

```bash
scripts/check_api_health.py
```

The probe redacts secrets and checks:

- required/optional key presence
- Tavily search
- OpenAI chat
- Anthropic messages
- Polymarket Gamma public API
- Polymarket CLOB public API
- Polifly bridge health when configured

## Latest Server Result

- `TAVILY_API_KEY`: configured
- Tavily search: PASS
- `ANTHROPIC_API_KEY`: configured
- Anthropic Sonnet: PASS
- Polymarket Gamma: PASS
- Polymarket CLOB public API: PASS
- Builder relayer credentials: configured
- Wallet private key: configured
- OpenAI `gpt-4o`: FAIL with HTTP `429`
- Optional providers not configured: NewsAPI, Nansen, Wallet Master, Polifly
  bridge key
- Optional persisted CLOB L2 credentials are not configured; the Polymarket
  adapter may still derive credentials from the wallet when needed.

## Operational Meaning

The system has working Tavily/news enrichment, Anthropic LLM reasoning, and
Polymarket market connectivity. OpenAI is not currently healthy on the live
server because the provider returns `429`; this requires fixing the OpenAI
account/key/quota/billing outside the codebase. Existing code falls back to
Anthropic on OpenAI quota/rate-limit errors where LLM fallback is implemented.

The system remained in `freeze` during this check.

## Brain / Signal Connectivity Follow-Up

After reviewing live-capable entry agents, entry logic was tightened so brain
gate failures are fail-closed rather than fail-open:

- Main trader: MetaBrain/MarketBrain failure blocks entry.
- Scalper: missing MarketBrain blocks live entry.
- BTC daily: missing or failed MarketBrain blocks live entry and journals the
  brain decision.
- BTC 5m: missing or failed MarketBrain blocks live entry and journals the
  brain decision.
- News shock: now receives MarketBrain and journals entry decisions.
- Wallet follow: now receives MarketBrain and journals entry decisions.
- External conviction API: live mode initializes MarketBrain and blocks if the
  brain is missing or fails.
- Near resolution already requires LLM direction evidence before entry.

Test contract added: live entry brain failures must not regress to
`fail-open`.
