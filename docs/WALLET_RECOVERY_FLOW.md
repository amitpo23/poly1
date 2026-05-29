# WALLET_RECOVERY_FLOW.md — closing and redeeming Polymarket positions on the shared deposit wallet

**Audience:** any agent (Claude Code, Codex, future operator) who needs to close out positions on the bot's wallet, recover winnings from resolved markets, or sweep cash between the deposit wallet and legacy Privy proxy.

This is a re-usable runbook. It was first established on 2026-05-26 and confirmed working again on 2026-05-29 after the Kamatera→Hetzner migration.

---

## TL;DR

| Position state | Tool | Touches CLOB? | Geoblock applies? |
|---|---|---|---|
| Resolved + WON (full payout owed) | `move_ctf_to_proxy.py` → Polymarket UI **Redeem** → `setup_deposit_wallet.py` | No (Builder relayer) | No |
| Resolved + LOST (zero payout) | leave it (dust — gas > value) | n/a | n/a |
| Still active, want to exit | `bot.sell_shares(...)` with `OrderType.FAK` | **Yes** | **Yes** — must run from an allowed region |

The two wallets:

- **deposit_wallet** = `0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb` — V2 sig_type=3, where the bot trades and where positions live by default
- **legacy_proxy** = `0x84fa6ea1E274B73C81D61cFF28dc1F8e05136882` — old Privy proxy, **the only wallet the Polymarket web UI sees** with the currently saved Chrome session

Same EOA signer for both: `0x14a2E262fCE33BbeF4cb507Df0caEE343412c55d`. Same `POLYGON_WALLET_PRIVATE_KEY` (66 chars) in `/srv/poly1/.env`.

---

## Why the direct redeem script fails — and the working detour

`scripts/python/redeem_winnings.py` looks correct (calls `CTF.redeemPositions(...)` via Builder relayer), and it reports `STATE_MINED + status=0x1` on chain. **But the actual payouts are $0** every time when called *from the deposit wallet*.

Root cause (per `SESSION_2026_05_26_HANDOFF.md`): Polymarket's CTF uses EC-point compression in `getCollectionId`, so the positionId derived off-chain by the script doesn't match what `redeemPositions` derives on-chain. The on-chain call succeeds but matches no held tokens → 0 payout.

This was first seen on 2026-05-26 (operator note: "PayoutRedemption events with $0 payouts every time"). Confirmed again on 2026-05-29 — same script, same symptom: 5.3M gas burned per attempt, $0 actually moved.

The known-good workaround is to **move the CTF tokens off the deposit wallet onto the legacy Privy proxy first, then redeem through the Polymarket web UI** (which the UI knows how to do correctly because it's been the canonical path for years).

---

## The three-step recovery (resolved winners)

All commands run inside the trader Docker container on the server. Server is HALT + freeze throughout — this flow does NOT need the bot to be live.

### Step 1 — Move winner CTFs to legacy_proxy

```bash
ssh poly1
cd /srv/poly1
docker compose run --rm --no-deps trader python3 scripts/python/move_ctf_to_proxy.py
# DRY-RUN — prints the positions that would move + their values
# Confirm the list looks right, then:
docker compose run --rm --no-deps -e EXECUTE=true trader python3 scripts/python/move_ctf_to_proxy.py
```

What this does: calls `CTF.safeTransferFrom(deposit_wallet, legacy_proxy, tokenId, amount, "")` for each position where `currentValue > $0.05` (winners). Zero gas via Builder relayer. By default skips the dust losers; pass `ALL=true` to also relocate the zero-value tokens.

Verification (built into the script): prints `deposit=0` / `legacy=<amount>` for each token after the transfer. If both legs show, the move worked.

### Step 2 — Redeem on Polymarket UI as legacy_proxy

The Chrome browser on this Mac already has a logged-in Polymarket session on the legacy_proxy account. To use it:

1. Open `https://polymarket.com/portfolio` in Chrome (the existing session auto-logs in)
2. The top banner shows "You won $X.XX" with a master blue **Redeem** button on the right
3. Click it → confirmation modal listing each winning position → click the big blue **Redeem $X.XX** button at the bottom of the modal
4. A "Share your $X.XX winnings" celebration modal appears. Close it (×). The portfolio now shows "No positions found" for the previously-listed winners and Cash = the redeemed total

This step uses Polymarket's own redemption code path which handles the EC-point compression correctly. No gas, no transaction signed by us — Polymarket's UI submits via Privy's relayer.

Via the `mcp__claude-in-chrome__*` browser tool: `navigate` to `polymarket.com/portfolio`, `left_click` the master Redeem button, `left_click` the confirmation button. Verified working on 2026-05-29.

### Step 3 — Sweep pUSD legacy_proxy → deposit_wallet

```bash
docker compose run --rm --no-deps trader python3 scripts/python/setup_deposit_wallet.py
# DRY-RUN: should show "legacy_proxy_pusd: <amount>" — the redeemed cash sitting on legacy
docker compose run --rm --no-deps -e EXECUTE=true trader python3 scripts/python/setup_deposit_wallet.py
# Re-run with EXECUTE=true to transfer it back to deposit_wallet
```

The script is idempotent. It also does deposit-wallet bootstrapping (deploy + approvals) if not already done. On a second run with nothing to sweep, it's a no-op.

Verify by checking the deposit wallet's pUSD balance before/after:

```bash
docker compose run --rm --no-deps trader python3 -c "
from agents.polymarket.polymarket import Polymarket
p = Polymarket(live=False)
print(f'pUSD: \${p.get_usdc_balance():.4f}')
"
```

---

## Closing active positions (CLOB SELL)

For positions that are **still active** (the market hasn't resolved yet), you can't redeem — you have to SELL on the CLOB.

```python
from agents.polymarket.polymarket import Polymarket
from py_clob_client_v2.clob_types import OrderType

p = Polymarket(live=True)
resp = p.sell_shares(
    token_id="<the CLOB token id of the side we hold>",
    shares=<float, the position size>,
    limit_price=<float, the lowest price you'll accept — typically best_bid>,
    order_type=OrderType.FAK,  # Fill-And-Kill: fills what can fill at >= limit, cancels rest
)
print(resp)
# Successful: {'status': 'matched', 'takingAmount': '<usdc>', 'makingAmount': '<shares>', 'success': True, ...}
```

`token_id`, `outcome`, and `size` for every open position are available from the Polymarket data API: `https://data-api.polymarket.com/positions?user=<deposit_wallet>&sizeThreshold=0.5&limit=50`.

Get the current `best_bid` via the CLOB book endpoint: `https://clob.polymarket.com/book?token_id=<id>` → `bids[0].price`.

### CRITICAL: CLOB is geoblocked

The Polymarket CLOB rejects orders from blocked countries with HTTP 403. As of 2026-05-29 the canonical list is at `https://docs.polymarket.com/developers/CLOB/geoblock`:

- **Blocked (fully):** Australia, Belgium, Belarus, Burundi, Central African Republic, Congo, Cuba, **Germany (DE)**, Eritrea, Ethiopia, UK, Iran, Iraq, Italy, Lebanon, Libya, Myanmar, Nicaragua, **Netherlands (NL)**, North Korea, Russia, Somalia, Sudan, Syria, South Sudan, **USA**, Venezuela, Yemen, Zimbabwe
- **Close-only:** Poland (PL), Singapore (SG) — can SELL existing positions but not BUY new ones
- **Frontend-only restriction (API works):** Japan (JP)
- **Allowed:** every other country including Israel, Finland (FI — where this server now is), Sweden, Switzerland, France

**This is why the Hetzner server lives in Helsinki, not Falkenstein.** Provisioning in a blocked region will leave you unable to BUY/SELL — only the Builder-relayer paths (`redeemPositions`, `safeTransferFrom`, `approve`) keep working because those bypass CLOB.

If you ever need to migrate the server again because of geoblock changes:
- Provision in Finland / Sweden / Switzerland / Israel / any unlisted country
- Re-run the migrate-to-Hetzner pattern (see `migrate_to_hetzner.sh` + Hetzner Console)
- 30 minutes end-to-end on a CX22 → CPX22

### Why SELL needs to come from the right wallet

The bot's `Polymarket(live=True).sell_shares(...)` signs from the EOA configured via `POLYGON_WALLET_PRIVATE_KEY` and submits against the deposit_wallet (the `signature_type=3` proxy). It cannot SELL from the legacy_proxy unless reconfigured.

So:
- If you moved tokens to legacy_proxy in Step 1, then closed via the UI **before resolution** — you have to use Polymarket UI's SELL flow (not bot.sell_shares)
- If you didn't move the tokens, just SELL from the bot using `bot.sell_shares(...)` while the bot is on the deposit_wallet

The clean rule: **redeem flow uses legacy_proxy + UI. SELL flow uses deposit_wallet + bot.**

---

## Dust positions — don't bother

Positions with `currentValue == 0` are tokens of the losing side of resolved markets. They:

- Cannot be SOLD (no one will buy a zero-value token)
- Can technically be REDEEMED (returns $0) but the redeem call costs gas (even if relayed, it costs Polymarket's gas budget)
- Cleanest treatment: leave them on the wallet. They don't affect anything operationally.

If you want a tidy-zero portfolio for accounting reasons, run `move_ctf_to_proxy.py` with `ALL=true` to relocate them off the deposit_wallet onto legacy_proxy, then redeem-zero them in the UI. This is cosmetic, not functional.

---

## Verification checklist

After running all three steps for a redemption:

```bash
# 1. deposit_wallet pUSD should have increased by ~the redeemed amount (minus 0 — Builder relayer eats gas)
docker compose run --rm --no-deps trader python3 -c "
from agents.polymarket.polymarket import Polymarket
print(f'pUSD: \${Polymarket(live=False).get_usdc_balance():.4f}')
"

# 2. Polymarket positions should show 0 for the redeemed conditions
curl -s "https://data-api.polymarket.com/positions?user=0x16577fEc75797Cd59D01CB6e8518Df6a4B2c04Cb&sizeThreshold=0.5&limit=50" | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print(f'{len(d)} positions, total \${sum(float(p.get(\"currentValue\",0)or 0) for p in d):.2f}')"

# 3. Open-positions count + total MTM should match expectations
```

---

## When NOT to use this flow

- The bot is in active live trading and the position is being managed by `position_manager.py` — don't race the bot. Stop the position_manager profile first or wait for the trade lock to release.
- You are uncertain whether the position resolved — check `redeemable: true` in the data API response first. A position with `currentValue > 0` and `redeemable: false` is still active, not resolved.
- The wallet you're operating on is NOT the canonical deposit wallet — verify by `POLYMARKET_DEPOSIT_WALLET` in `/srv/poly1/.env` matches `0x16577fEc...`.

---

## History

- **2026-05-26** — first discovered that `redeem_winnings.py` on deposit_wallet pays $0; established the move-to-proxy-then-UI workflow. Commits `1607143`, `c661b60`.
- **2026-05-29** — replayed the exact same flow after migration to Hetzner; recovered $4.21 of resolved Bitcoin Down winners and $1.74 of active SELLs (after moving server to Helsinki to bypass German geoblock). Total $5.95 of $5.99 MTM recovered (99% — only slippage was the gap).
