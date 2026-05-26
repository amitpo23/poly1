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

### Step 3 (optional — recover ~$16 from the old proxy)

**Migration completed 2026-05-26** with the new proxy live. The old
proxy `0x16577fEc...` was left in place with recoverable funds.

Audit (2026-05-26):

| Category | Count | Value |
|---|---|---|
| Redeemable (resolved wins) | 10 | $11.21 |
| Tradeable (still has bid) | 1 | $0.70 |
| Worthless (resolved against us) | 22 | $0.01 |
| USDC.e cash sitting in proxy | — | $4.19 |
| **Total recoverable** | | **~$16.10** |

**Two recovery paths:**

1. **Via Polymarket UI** (simpler if you can access the old account):
   - Log into Polymarket UI with whatever credentials originally
     created the old proxy. The 10 winning positions will show
     "Claim" buttons.
   - After claiming → click Withdraw → send all USDC to the new
     proxy `0x84fa6ea1...`.

2. **Programmatic** (uses the bot's EOA which signs for both proxies):
   - Temporarily set `POLYMARKET_DEPOSIT_WALLET=0x16577fEc...`.
   - Run a one-shot script that:
     a. Calls `redeemPositions(conditionId, indexSets)` on the CTF
        contract `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` for
        each of the 10 winning conditionIds. Their conditionIds are
        listed in the migration audit `data/old_proxy_audit_2026_05_26.json`.
     b. Transfers all USDC.e from old proxy to new proxy.
   - Restore `POLYMARKET_DEPOSIT_WALLET=0x84fa6ea1...` and restart.

Either path is deferrable — the funds aren't going anywhere.

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
