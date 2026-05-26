# poly1 Wallet Architecture — operational guide

Last updated: 2026-05-26 (Path B migration to social-login proxy)

## TL;DR

The bot uses **3 addresses** that work together. Confusion comes from
mixing them up — read this when in doubt.

| Address | What it is | Where it's set | Holds funds? |
|---|---|---|---|
| **EOA** (signer) | Derived from `POLYGON_WALLET_PRIVATE_KEY`. Signs orders. | `.env` private key | No (no USDC, no positions) |
| **FUNDER / DEPOSIT_WALLET** (proxy) | The Polymarket proxy. Holds USDC and CTF positions. | `POLYMARKET_DEPOSIT_WALLET` in `.env` | **Yes** (everything lives here) |
| **BUILDER_ADDRESS** | Fee attribution recipient. | `POLYMARKET_BUILDER_ADDRESS` in `.env` | No |

## Current state (2026-05-26)

```
EOA:             0x14a2E262fCE33BbeF4cb507Df0caEE343412c55d   (signer)
FUNDER:          0x84fa6ea1E274B73C81D61cFF28dc1F8e05136882   (NEW proxy, $83.08, Google-login)
DEPOSIT_WALLET:  0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb   (OLD proxy, 32 stuck positions, $4 USDC.e)
BUILDER_ADDRESS: 0x84fa6ea1E274B73C81D61cFF28dc1F8e05136882   (NEW)
```

## How `self.funder` resolves in code

`agents/polymarket/polymarket.py:175`:

```python
self.funder = self.deposit_wallet or legacy_funder
```

**`POLYMARKET_DEPOSIT_WALLET` overrides `POLYMARKET_FUNDER`.** If both
are set to different values, the bot silently uses DEPOSIT_WALLET.

## Recommended migration to the new proxy (Path B)

**Goal**: future deposits via Polymarket UI go straight to the bot's
funder. No manual `Withdraw` step ever again.

### Step 1 (manual, in Polymarket UI)

1. Sign in to Polymarket UI with Google for the new account
   `0x84fa6ea1...`.
2. Click your avatar → **APIs**.
3. Click **Create API key**. Polymarket returns three strings:
   - `api_key`
   - `secret`
   - `passphrase`
4. Send those three values to Claude (via the conversation, **not by
   committing them to git**).

### Step 2 (Claude — bot config update)

Add to `.env`:

```bash
POLYMARKET_CLOB_API_KEY="<api_key from step 1>"
POLYMARKET_CLOB_API_SECRET="<secret>"
POLYMARKET_CLOB_API_PASSPHRASE="<passphrase>"
POLYMARKET_DEPOSIT_WALLET="0x84fa6ea1E274B73C81D61cFF28dc1F8e05136882"
```

Keep `POLYMARKET_FUNDER` and `POLYMARKET_BUILDER_ADDRESS` already set to
`0x84fa6ea1...` — they match.

Restart bot containers:

```bash
docker compose up -d --force-recreate
```

Verify:

```bash
docker exec poly1-live-dashboard python3 -c "
from agents.polymarket.polymarket import Polymarket
p = Polymarket(live=True)
print('funder:', p.funder)
print('balance:', p.get_usdc_balance())
"
# expected: funder=0x84fa6ea1..., balance≈$83
```

### Step 3 (optional — clean up the old proxy)

The old proxy `0x16577fEc...` has 32 positions ($11 of which is current
value) and $4 in USDC.e. Most are stuck losing markets, but a few may
be redeemable on win.

To clean up:
- The user signs in to Polymarket UI with the *old* account login.
- Polymarket UI shows a "Claim" button on any resolved-winning markets.
- Withdraw remaining USDC.e to the new proxy or any wallet.

Not urgent — the old proxy is now disconnected from the bot's runtime,
so it costs nothing to leave the positions sitting there.

## Why Path B beats Path A (one-time withdraw)

| | Path A: withdraw each deposit | Path B: bot uses new proxy |
|---|---|---|
| Setup work | 0 | ~15 min one-time |
| Per-deposit hassle | manual `Withdraw` every time | none |
| Gas cost over 12 deposits | ~12 × Polygon withdraw fee | 0 |
| Risk of forgetting to withdraw | yes | no |
| Source-of-truth clarity | 2 wallets (UI vs bot) | 1 wallet |

## What CANNOT be automated

- **Creating the API key** — must happen in Polymarket UI by the
  account owner. Claude cannot do this.
- **Authorizing the bot's EOA as a signer** — implicit when the
  API key is created via UI under the right account; the API key
  triplet is the proof of authorization.

## If anything goes wrong

If `get_usdc_balance()` reports the wrong number after restart,
or if `derive_api_key()` fails:
- Verify all 4 new env vars are present.
- Verify the API key was created under the *correct* Polymarket
  account (matching `0x84fa6ea1...`).
- Verify `POLYMARKET_SIGNATURE_TYPE=3` is still set.
- Worst case: revert `POLYMARKET_DEPOSIT_WALLET` to the old value,
  restart, and we're back to the previous-working state.
