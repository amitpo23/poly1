# External Provider Keys - 2026-05-17

## What can run without a new key

- Polymarket Gamma/Data public market discovery: no API key needed.
- Polymarket public CLOB reads such as orderbook/prices: no API key needed.
- Public news/RSS search for candidate market questions: no API key needed.

These are market-data/research inputs only. Public news can provide narrative
attention evidence, but it does not replace a paid prediction or sentiment
source.

## What needs operator-owned credentials

Do not use leaked, shared, or random internet API keys. Provider keys are account
secrets and may carry billing, quota, or trading implications.

### Option 1: Polifly Browser Analyzer

Status: account is logged in, Analyzer redirects to payment.

Needed:

- Activate Polifly Pro manually.
- Start a local browser bridge and set:

```text
POLIFLY_BROWSER_BRIDGE_URL=http://127.0.0.1:8787/analyze
POLIFLY_BROWSER_BRIDGE_API_KEY=<optional local bridge token>
```

Agent:

```text
external_conviction_polifly
```

### Option 2: External API Wrapper

Needed:

- A local or hosted endpoint that accepts a Polymarket market snapshot and
  returns `direction`, `confidence`, `source`, `reason`, and `evidence`.
- Set:

```text
EXTERNAL_CONVICTION_API_URL=https://...
EXTERNAL_CONVICTION_API_KEY=<optional bearer token>
```

Agent:

```text
external_conviction_api
```

Supported wrappers can be built around Kaito, Santiment/Sanbase, Glassnode,
CryptoQuant, or another research provider.

## Provider Notes

- Glassnode API access requires eligible paid API access/add-on before a key can
  be generated in Glassnode Studio.
- Santiment/Sanbase uses GraphQL and supports `Authorization: Apikey <key>`;
  some data requires a paid subscription.
- Kaito advertises API availability for crypto narrative/search data; access is
  account/provider controlled.
- TradingView is useful via webhook alerts, but it is alert-driven, not a
  prediction API. Webhooks require configuring an alert URL in the user account.

## Current Safe State

- Both external agents are running and healthy.
- Both are shadow-only.
- Both currently write `SKIP` until a real provider is connected.
- Runtime remains in `freeze` with `data/HALT` present.
