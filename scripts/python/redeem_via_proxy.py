"""Redeem CTF tokens held by the legacy_proxy via RelayerTxType.PROXY.

Approach: legacy_proxy now holds the resolved-winner CTF tokens
(moved by move_ctf_to_proxy.py). Submit CTF.redeemPositions through
the Polymarket relayer's PROXY tx type — this is the same mechanism
that successfully transferred pUSD during the initial migration. If
redeem works here, the pUSD payout lands in legacy_proxy and a
subsequent run of setup_deposit_wallet.py sweeps it back to
deposit_wallet.

Dry run by default; pass EXECUTE=true to broadcast.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

from dotenv import load_dotenv
from py_builder_relayer_client.client import RelayClient, RelayerTxType
from py_builder_relayer_client.models import Transaction
from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig
from web3 import Web3

from agents.polymarket.polymarket import Polymarket


PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

CTF_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
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

PUSD_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
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
            key=key, secret=secret, passphrase=passphrase,
        )
    )


def _wait(response):
    confirmed = response.wait()
    return confirmed.to_dict() if hasattr(confirmed, "to_dict") else confirmed


def main() -> int:
    load_dotenv()
    execute = os.getenv("EXECUTE", "false").lower() == "true"

    pm = Polymarket(live=False)
    web3 = pm.web3
    relayer_url = os.getenv(
        "POLYMARKET_RELAYER_URL", "https://relayer-v2.polymarket.com/"
    )
    builder_config = _builder_config()
    if not builder_config:
        print("FATAL: Builder relayer creds missing.")
        return 2

    proxy_relayer = RelayClient(
        relayer_url, 137, pm.private_key, builder_config,
        relay_tx_type=RelayerTxType.PROXY,
        rpc_url=os.getenv("POLYGON_RPC"),
    )
    legacy_proxy = proxy_relayer.get_expected_proxy_wallet()
    deposit_wallet = proxy_relayer.get_expected_deposit_wallet()
    print(f"legacy_proxy:   {legacy_proxy}")
    print(f"deposit_wallet: {deposit_wallet}")
    print()

    pusd = web3.eth.contract(address=Web3.to_checksum_address(PUSD), abi=PUSD_ABI)
    bal_before_legacy = pusd.functions.balanceOf(legacy_proxy).call()
    bal_before_deposit = pusd.functions.balanceOf(deposit_wallet).call()
    print(f"pUSD BEFORE  legacy_proxy:   ${bal_before_legacy / 1e6:.4f}")
    print(f"pUSD BEFORE  deposit_wallet: ${bal_before_deposit / 1e6:.4f}")
    print()

    # Fetch redeemable positions at legacy_proxy
    url = (
        f"https://data-api.polymarket.com/positions?"
        f"user={legacy_proxy}&sizeThreshold=0.01"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        positions = json.loads(resp.read())
    redeemable = [p for p in positions if p.get("redeemable")]
    print(f"redeemable positions at legacy_proxy: {len(redeemable)}")
    if not redeemable:
        print("Nothing to redeem.")
        return 0

    ctf = web3.eth.contract(address=Web3.to_checksum_address(CTF), abi=CTF_ABI)

    seen: set[str] = set()
    txs: list[Transaction] = []
    for p in redeemable:
        cid = (p.get("conditionId") or "").lower()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        data = ctf.encodeABI(
            fn_name="redeemPositions",
            args=[
                Web3.to_checksum_address(PUSD),
                b"\x00" * 32,
                bytes.fromhex(cid[2:]),
                [1, 2],
            ],
        )
        txs.append(Transaction(to=CTF, data=data, value="0"))
        title = (p.get("title") or "")[:55]
        val = p.get("currentValue", 0)
        print(f"  queued: {title:55s} | val=${val:.3f} | cid={cid[:20]}...")

    print()
    print(f"Total redemption calls queued: {len(txs)}")
    if not execute:
        print("DRY RUN — set EXECUTE=true to broadcast.")
        return 0

    response = proxy_relayer.execute(
        txs, metadata="redeem CTF winners at legacy_proxy"
    )
    print("Submitted. Waiting…")
    result = _wait(response)
    print(json.dumps(result, default=str, indent=2)[:1200])

    time.sleep(3)
    bal_after_legacy = pusd.functions.balanceOf(legacy_proxy).call()
    diff = (bal_after_legacy - bal_before_legacy) / 1e6
    print()
    print(f"pUSD AFTER  legacy_proxy: ${bal_after_legacy / 1e6:.4f}")
    print(f"NET REDEEMED into legacy_proxy: ${diff:.4f}")

    if diff > 0:
        print()
        print("Next step: run setup_deposit_wallet.py with EXECUTE=true to "
              "sweep redeemed pUSD from legacy_proxy → deposit_wallet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
