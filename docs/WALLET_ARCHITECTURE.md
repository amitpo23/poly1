# poly1 Wallet Architecture — operational guide

Last updated: 2026-05-26 (consolidated via `setup_deposit_wallet.py`)

## TL;DR

The bot uses **3 derived addresses** from a single EOA private key.
They have specific roles in Polymarket's CLOB v2 architecture.

| Address | Role | Holds funds? |
|---|---|---|
| **EOA** (signer) | Signs orders. Has no positions or pUSD itself. | No |
| **legacy_proxy** | Polymarket Privy proxy. Where the UI's "Deposit" button sends funds for accounts that log in via social (Google/email). | Sometimes — until consolidated to deposit_wallet |
| **deposit_wallet** | The proper CLOB v2 trading address. Holds pUSD and CTF positions. Bot trades from here. | **Yes** — everything lives here once consolidated |

`POLYMARKET_DEPOSIT_WALLET` in `.env` must always point to the
deposit_wallet, not the legacy_proxy. The bot's `Polymarket.funder` is
the deposit_wallet.

## How funds flow

1. User clicks "Deposit" in Polymarket UI → USDC lands in the
   **legacy_proxy** (because the UI is built on Privy).
2. Bot can't trade from legacy_proxy under CLOB v2 — it needs the
   deposit_wallet.
3. Run `scripts/python/setup_deposit_wallet.py` with `EXECUTE=true` to
   sweep pUSD from legacy_proxy → deposit_wallet (gasless via Builder
   relayer).

## Current state (2026-05-26, post-migration)

```
EOA:             0x14a2E262fCE33BbeF4cb507Df0caEE343412c55d   (signer, no balance)
legacy_proxy:    0x84fa6ea1E274B73C81D61cFF28dc1F8e05136882   (drained, $0)
deposit_wallet:  0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb   ($90.12 pUSD + 32 CTF positions)
```

The 32 CTF positions at deposit_wallet are the bot's actual trading
history; 10 of them are redeemable winnings worth $11.21 — see Step 3
below for recovery instructions.

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
