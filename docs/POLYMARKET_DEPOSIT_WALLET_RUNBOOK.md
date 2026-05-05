# Polymarket Deposit Wallet Runbook

This repo is configured for Polymarket CLOB v2 deposit-wallet trading.

For the latest agent-to-agent handoff, read:

- `docs/AGENT_HANDOFF_2026-05-04.md`

For the current release checkpoint, read:

- `docs/RELEASE_NOTES_2026-05-04_CLOB_V2.md`

For overnight and multi-week operation, read:

- `docs/OVERNIGHT_AND_MULTI_WEEK_OPERATION.md`

## Current State

- Deposit-wallet migration completed on 2026-05-04.
- Legacy proxy/funder wallet:
  `0x84fa6ea1E274B73C81D61cFF28dc1F8e05136882`
- Deposit wallet:
  `0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb`
- Current bot mode:
  - `POLYMARKET_DEPOSIT_WALLET=0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb`
  - `POLYMARKET_SIGNATURE_TYPE=3`
- Current observed pUSD balance after smoke order and two live bot fills:
  `75.890783`
- Authenticated CLOB reads work; authenticated open orders returned `[]`.
- Deposit-wallet smoke order succeeded with `status=matched`.
- Two live bot orders succeeded and CLOB confirms both as `MATCHED`:
  - `0x98e9b20b82115e86bc7e5feabc2f3cd53c9d8de36bc70257abf2885c3699b495`
  - `0x9144b707d6faf7b7d7947014563520ec755fde2ac514840b4de04ef2ce7d3253`

## Current Live Guardrails

```env
EXECUTE="true"
MAX_POSITION_FRACTION="0.025"
STARTING_BALANCE_USDC="80.0"
MAX_TRADES_PER_HOUR="2"
```

Keep these conservative unless the user explicitly asks to increase risk.

## Security Note

The `.env` contents were printed during debugging. Treat these as compromised
and rotate before serious live trading:

- `OPENAI_API_KEY`
- `POLYGON_WALLET_PRIVATE_KEY`

Do not commit `.env`.

## Migration Credentials

Builder relayer credentials are required to re-run deployment/migration:

```env
BUILDER_API_KEY="..."
BUILDER_SECRET="..."
BUILDER_PASS_PHRASE="..."
POLYMARKET_RELAYER_URL="https://relayer-v2.polymarket.com/"
```

Builder attribution is separate:

```env
POLYMARKET_BUILDER_CODE="..."
POLYMARKET_BUILDER_ADDRESS="..."
```

## Migration Command

The migration has already completed. If it must be repeated for a new wallet,
run:

```bash
docker compose build trader
docker compose run --rm trader env EXECUTE=true python scripts/python/setup_deposit_wallet.py
```

The script will:

1. Derive the existing legacy proxy and deposit wallet.
2. Deploy the deposit wallet if it is not deployed.
3. Transfer pUSD from the legacy proxy to the deposit wallet.
4. Approve pUSD and CTF spenders from the deposit wallet.
5. Print the deposit-wallet env values to set.

## Verification

Run tests:

```bash
docker compose run --rm trader pytest -q
```

Run a dry inspection:

```bash
docker compose run --rm trader python scripts/python/manual_order_test.py
```

Run a single live smoke order:

```bash
docker compose run --rm trader env EXECUTE=true python scripts/python/manual_order_test.py
```

Start the daemon:

```bash
docker compose up -d trader
docker logs --tail 120 -f poly1
```

## Stop / Disable Live Trading

```bash
docker compose stop trader
```

Set:

```env
EXECUTE="false"
```

Then restart only when intentionally resuming.

## Relevant Files

- `agents/polymarket/polymarket.py`: CLOB client and signing-mode selection.
- `scripts/python/setup_deposit_wallet.py`: deposit-wallet migration automation.
- `scripts/python/manual_order_test.py`: single market smoke test.
- `scripts/python/env_status.py`: safe env key presence check.
- `docs/AGENT_HANDOFF_2026-05-04.md`: full current handoff for future agents.
- `deploy/CURRENT_STATUS.md`: current operational state.
- `deploy/PREFLIGHT.md`: production checklist.
- `SPEC.md`: architecture and env documentation.
