"""Move CTF tokens from deposit_wallet to legacy_proxy.

Strategy: After moving the resolved-winner positions to the legacy
Privy proxy (0x84fa6ea1...), redemption can be attempted from a
different path — either Polymarket UI's "Claim" button (since the UI
shows the legacy_proxy's holdings) or via RelayerTxType.PROXY which is
known to work for pUSD transfers from that proxy.

Defaults to DRY RUN; set EXECUTE=true to broadcast.
By default moves only the WINNERS (currentValue > $0.05). Pass
ALL=true to also move the losers (just for accounting cleanup).
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

from dotenv import load_dotenv
from py_builder_relayer_client.client import RelayClient
from py_builder_relayer_client.models import DepositWalletCall, TransactionType
from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig
from web3 import Web3

from agents.polymarket.polymarket import Polymarket


CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

CTF_ABI = [
    {
        "name": "safeTransferFrom",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "from", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "id", "type": "uint256"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
        ],
        "outputs": [],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


def _builder_config() -> BuilderConfig | None:
    key = os.getenv("BUILDER_API_KEY")
    secret = os.getenv("BUILDER_SECRET")
    passphrase = os.getenv("BUILDER_PASS_PHRASE")
    if not (key and secret and passphrase):
        return None
    return BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=key, secret=secret, passphrase=passphrase
        )
    )


def _wait(response):
    confirmed = response.wait()
    return confirmed.to_dict() if hasattr(confirmed, "to_dict") else confirmed


def main() -> int:
    load_dotenv()
    execute = os.getenv("EXECUTE", "false").lower() == "true"
    move_all = os.getenv("ALL", "false").lower() == "true"

    pm = Polymarket(live=False)
    web3 = pm.web3
    relayer_url = os.getenv(
        "POLYMARKET_RELAYER_URL", "https://relayer-v2.polymarket.com/"
    )
    builder_config = _builder_config()
    if not builder_config:
        print("FATAL: missing Builder relayer creds.")
        return 2

    relayer = RelayClient(
        relayer_url, 137, pm.private_key, builder_config,
        rpc_url=os.getenv("POLYGON_RPC"),
    )
    owner = pm.get_address_for_private_key()
    deposit_wallet = relayer.get_expected_deposit_wallet()
    legacy_proxy = relayer.get_expected_proxy_wallet()
    print(f"owner EOA:      {owner}")
    print(f"deposit_wallet: {deposit_wallet}  (CTF tokens are here)")
    print(f"legacy_proxy:   {legacy_proxy}    (CTF tokens going here)")
    print()

    url = (
        f"https://data-api.polymarket.com/positions?"
        f"user={deposit_wallet}&sizeThreshold=0.01"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        positions = json.loads(resp.read())

    if move_all:
        targets = [p for p in positions if p.get("size", 0) > 0]
    else:
        targets = [p for p in positions if p.get("redeemable") and p.get("currentValue", 0) > 0.05]

    print(f"Will move {len(targets)} CTF position(s) — "
          f"{'ALL with size>0' if move_all else 'WINNERS only'}")
    print()

    ctf = web3.eth.contract(address=Web3.to_checksum_address(CTF), abi=CTF_ABI)
    calls: list[DepositWalletCall] = []
    expected_value = 0.0
    for p in targets:
        asset_str = p.get("asset", "")
        size = p.get("size", 0)
        if not asset_str or size <= 0:
            continue
        position_id = int(asset_str) if asset_str.isdigit() else int(asset_str, 16)
        # Always use the EXACT on-chain balance (Data API's `size` is rounded)
        on_chain = ctf.functions.balanceOf(
            Web3.to_checksum_address(deposit_wallet), position_id
        ).call()
        raw_amount = on_chain
        title = (p.get("title") or "")[:42]
        val = p.get("currentValue", 0)
        print(f"  {title:42s} | val=${val:5.2f} | raw_amount={raw_amount}")
        if raw_amount <= 0:
            continue
        data = ctf.encodeABI(
            fn_name="safeTransferFrom",
            args=[
                Web3.to_checksum_address(deposit_wallet),
                Web3.to_checksum_address(legacy_proxy),
                position_id,
                raw_amount,
                b"",
            ],
        )
        calls.append(DepositWalletCall(target=CTF, value="0", data=data))
        expected_value += val

    print()
    print(f"Built {len(calls)} CTF.safeTransferFrom call(s)")
    print(f"Estimated total value to recover: ${expected_value:.3f}")
    print()
    if not execute:
        print("DRY RUN — set EXECUTE=true to broadcast.")
        return 0

    nonce_payload = relayer.get_nonce(owner, TransactionType.WALLET.value)
    response = relayer.execute_deposit_wallet_batch(
        calls=calls,
        wallet_address=deposit_wallet,
        nonce=str(nonce_payload["nonce"]),
        deadline=str(int(time.time()) + 900),
    )
    print("Submitted. Waiting…")
    result = _wait(response)
    print(json.dumps(result, default=str, indent=2)[:1200])

    time.sleep(3)
    # Verify
    print()
    print("=== Verifying balances after transfer ===")
    for p in targets[:3]:
        asset_str = p.get("asset", "")
        position_id = int(asset_str) if asset_str.isdigit() else int(asset_str, 16)
        bal_old = ctf.functions.balanceOf(
            Web3.to_checksum_address(deposit_wallet), position_id
        ).call()
        bal_new = ctf.functions.balanceOf(
            Web3.to_checksum_address(legacy_proxy), position_id
        ).call()
        title = (p.get("title") or "")[:42]
        print(f"  {title:42s} | deposit={bal_old/1e6:.4f} | legacy={bal_new/1e6:.4f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
