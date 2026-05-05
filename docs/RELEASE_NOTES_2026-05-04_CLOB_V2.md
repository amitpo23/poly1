# Release Notes: 2026-05-04 CLOB v2 Live Trading

Release label: `polymarket-clob-v2-deposit-wallet-live-2026-05-04`

## Status

Live trading path is operational.

The bot is configured for Polymarket CLOB v2 deposit-wallet trading and has
successfully placed live matched orders through the deposit wallet.

## Key Outcomes

- Deposit-wallet migration completed.
- Deposit wallet deployed, funded, and approved.
- CLOB v2 SDK path is active.
- CLOB L2 authentication works.
- Builder attribution is configured.
- Live smoke order matched.
- Two live bot orders matched.
- Open orders are empty.
- Risk gate is active and correctly limiting trade frequency.

## Version Contents

Code changes included in this checkpoint:

- CLOB v2 SDK migration:
  - `py_clob_client_v2==1.0.1rc1`
  - `py-builder-relayer-client==0.0.2rc1`
- Deposit-wallet mode:
  - `POLYMARKET_DEPOSIT_WALLET`
  - `POLYMARKET_SIGNATURE_TYPE=3`
- pUSD collateral support for deposit-wallet accounts.
- Post-migration exchange address support.
- Builder config support.
- Orderbook-aware FOK market buy execution.
- Slippage guard through `POLYMARKET_MAX_SLIPPAGE`.
- Minimum live order guard through `POLYMARKET_MIN_ORDER_USDC`.
- `matched` to `filled` local status mapping.
- Deposit-wallet setup automation.
- Safe env-status check script.
- Tests updated for orderbook pricing and liquidity behavior.

## Verified Runtime

Last observed live account state:

```json
{
  "balance": 75.890783,
  "open_orders": []
}
```

Last test result:

```text
25 passed
```

Daemon:

```text
poly1: healthy
```

## Matched Orders

Smoke order:

- `0x3c97624b9fa44cc77fb7661c038af530ab62f33e65d5688394ec3998df00127e`

Bot orders:

- `0x98e9b20b82115e86bc7e5feabc2f3cd53c9d8de36bc70257abf2885c3699b495`
- `0x9144b707d6faf7b7d7947014563520ec755fde2ac514840b4de04ef2ce7d3253`

Both bot orders were confirmed by CLOB as `MATCHED`.

## Current Guardrails

```env
EXECUTE="true"
MAX_POSITION_FRACTION="0.025"
STARTING_BALANCE_USDC="80.0"
MAX_TRADES_PER_HOUR="2"
```

## Documentation Added Or Updated

- `docs/AGENT_HANDOFF_2026-05-04.md`
- `docs/RELEASE_NOTES_2026-05-04_CLOB_V2.md`
- `docs/POLYMARKET_DEPOSIT_WALLET_RUNBOOK.md`
- `deploy/CURRENT_STATUS.md`

## Important Caution

Earlier debugging printed secret values in terminal output. Rotate exposed keys
before increasing size or running unattended at higher risk.
