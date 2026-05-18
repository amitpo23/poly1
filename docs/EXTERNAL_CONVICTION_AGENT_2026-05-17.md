# External Conviction Agent - 2026-05-17

## Purpose

Run a shadow-only research loop every three hours:

1. scan active Polymarket markets;
2. filter for liquidity, volume, and tradable price range;
3. ask an external analysis provider for a short-horizon opinion;
4. write a trade plan with entry, take profit, stop loss, and max hold;
5. measure the idea before any live execution is considered.

The agent never places orders.

## Files

- `agents/application/external_conviction.py`
- `scripts/python/external_conviction.py`
- `data/external_convictions.jsonl`
- `data/external_convictions_polifly.jsonl`
- `data/external_convictions_api.jsonl`
- `data/external_conviction_heartbeat`
- `data/external_conviction_polifly_heartbeat`
- `data/external_conviction_api_heartbeat`

Each run also writes `brain_decisions` rows with:

```text
agent=external_conviction
strategy=event_probability_scalping
decision_type=shadow_trade_plan
```

## Split Agents

Two dedicated shadow agents are available:

- `external_conviction_polifly`: option 1, Polifly/Browser path. It uses
  `EXTERNAL_CONVICTION_PROVIDER=polifly_browser` and writes
  `data/external_convictions_polifly.jsonl`. It needs Polifly Pro plus a local
  `POLIFLY_BROWSER_BRIDGE_URL` before it can produce real analyzer opinions.
- `external_conviction_api`: option 2, external API path. It uses
  `EXTERNAL_CONVICTION_PROVIDER=public_news` now and writes
  `data/external_convictions_api.jsonl`. This is real no-key public news/RSS
  evidence. It can later be switched to `http_json` with a configured
  `EXTERNAL_CONVICTION_API_URL` wrapping Kaito, Santiment, Glassnode,
  CryptoQuant, or another provider.

## Provider Modes

Default:

```text
EXTERNAL_CONVICTION_PROVIDER=heuristic
```

This is intentionally conservative and local. It is only a placeholder so the
loop, logging, and evaluation can be tested without paid APIs.

External API mode:

```text
EXTERNAL_CONVICTION_PROVIDER=http_json
EXTERNAL_CONVICTION_API_URL=https://...
EXTERNAL_CONVICTION_API_KEY=...
```

The endpoint receives:

```json
{"market": {"market_id": "...", "question": "...", "yes_price": 0.42}}
```

and should return:

```json
{
  "direction": "yes",
  "confidence": 0.64,
  "source": "kaito",
  "reason": "narrative spike plus liquid market",
  "evidence": {}
}
```

This adapter can wrap Kaito, Santiment, Glassnode, CryptoQuant, or a browser
automation service that uploads a screenshot to an external market analyzer.

Public news mode:

```text
EXTERNAL_CONVICTION_PROVIDER=public_news
```

This queries live public news/RSS results for each candidate market question and
uses headline density as short-horizon attention evidence. It is a real external
source, but it is not treated as an oracle.

Polifly browser mode:

```text
EXTERNAL_CONVICTION_PROVIDER=polifly_browser
POLIFLY_BROWSER_BRIDGE_URL=http://127.0.0.1:8787/analyze
POLIFLY_BROWSER_BRIDGE_API_KEY=...
```

This mode deliberately does not handle signup, subscription checkout, or
credentials. It calls an operator-started browser bridge after Polifly Pro is
active.

Tavily mode:

```text
EXTERNAL_CONVICTION_PROVIDER=tavily
TAVILY_API_KEY=...
```

This uses Tavily search results as external news/narrative evidence. It is not a
prediction oracle and should stay shadow-only until the resulting plans show
measurable edge.

## Run

One scan:

```bash
python3 -m agents.application.external_conviction --once
```

Docker daemon, every three hours:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker compose \
  --profile external_conviction up -d --build external-conviction
```

Dedicated option agents:

```bash
/Applications/Docker.app/Contents/Resources/bin/docker compose \
  --profile external_conviction up -d --build \
  external-conviction-polifly external-conviction-api
```

## Safety

- No `execute_market_order` call exists in this agent.
- No `EXECUTE_*` flag is used.
- Output is shadow-only.
- Live promotion requires a separate future approval step and a green freeze/live
  preflight.
