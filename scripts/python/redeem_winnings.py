"""Redeem resolved-winner CTF positions held by the deposit wallet.

Built 2026-05-26 for the consolidation flow. After migrating pUSD from
legacy_proxy to deposit_wallet via setup_deposit_wallet.py, the wallet
still holds CTF tokens from resolved markets. This script burns those
tokens for their pUSD payouts (zero gas via the Builder relayer).

Reads the redeemable list from Polymarket's Data API. For each
resolved condition, submits CTF.redeemPositions([1, 2]) which catches
whichever outcome we hold — losing-side tokens contribute zero. The
script also attempts the NegRiskAdapter for the same condition in
case a market routed through neg-risk infrastructure.

Dry run by default; pass EXECUTE=true to broadcast.

    docker compose run --rm -e EXECUTE=true trader \
        python3 scripts/python/redeem_winnings.py
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

from dotenv import load_dotenv
from py_builder_relayer_client.client import RelayClient, RelayerTxType
from py_builder_relayer_client.models import DepositWalletCall, TransactionType
from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig
from web3 import Web3

from agents.polymarket.polymarket import Polymarket


PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

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
]

NEG_RISK_ADAPTER_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "conditionId", "type": "bytes32"},
            {"name": "amounts", "type": "uint256[]"},
        ],
        "outputs": [],
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
    key = os.getenv("BUILDER_API_KEY") or os.getenv("POLY_BUILDER_API_KEY")
    secret = os.getenv("BUILDER_SECRET") or os.getenv("POLY_BUILDER_SECRET")
    passphrase = (
        os.getenv("BUILDER_PASS_PHRASE")
        or os.getenv("BUILDER_PASSPHRASE")
        or os.getenv("POLY_BUILDER_PASSPHRASE")
    )
    if not (key and secret and passphrase):
        return None
    return BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=key, secret=secret, passphrase=passphrase,
        )
    )


def _wait(response):
    confirmed = response.wait()
    if hasattr(confirmed, "to_dict"):
        return confirmed.to_dict()
    return confirmed


def fetch_redeemable(wallet: str) -> list[dict]:
    """Pull redeemable positions from the public Data API."""
    url = (
        f"https://data-api.polymarket.com/positions?"
        f"user={wallet}&sizeThreshold=0.01&redeemable=true"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        positions = json.loads(resp.read())
    # Filter again locally because the API param may be ignored
    return [p for p in positions if p.get("redeemable")]


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
        print("FATAL: Builder relayer credentials missing.")
        return 2

    relayer = RelayClient(
        relayer_url, 137, pm.private_key, builder_config,
        rpc_url=os.getenv("POLYGON_RPC"),
    )
    owner = pm.get_address_for_private_key()
    deposit_wallet = relayer.get_expected_deposit_wallet()
    print(f"owner EOA:        {owner}")
    print(f"deposit_wallet:   {deposit_wallet}")
    print()

    redeemable = fetch_redeemable(deposit_wallet)
    if not redeemable:
        print("No redeemable positions found. Nothing to do.")
        return 0

    # Bucket by negRisk via Gamma API? — cheaper: try regular CTF first
    # and let NegRiskAdapter pick up the rest. Many crypto Up/Down
    # markets historically routed via regular CTF; some moved to
    # NegRisk infrastructure. Try both per condition; the second pass
    # will be a no-op for the contract that didn't recognize the
    # condition.
    pusd_contract = web3.eth.contract(
        address=Web3.to_checksum_address(PUSD), abi=PUSD_ABI
    )
    balance_before = pusd_contract.functions.balanceOf(deposit_wallet).call()
    print(f"pUSD balance BEFORE: ${balance_before / 1e6:.4f}")
    print()

    ctf = web3.eth.contract(address=Web3.to_checksum_address(CTF), abi=CTF_ABI)
    neg_risk = web3.eth.contract(
        address=Web3.to_checksum_address(NEG_RISK_ADAPTER),
        abi=NEG_RISK_ADAPTER_ABI,
    )

    print(f"Redeemable conditions: {len(redeemable)}")
    for i, pos in enumerate(redeemable, 1):
        title = (pos.get("title") or "")[:55]
        size = pos.get("size", 0)
        val = pos.get("currentValue", 0)
        out = pos.get("outcome", "?")
        cid = pos.get("conditionId", "")
        neg = pos.get("negativeRisk", False)
        marker = "[neg-risk]" if neg else "[regular ]"
        print(f"  {i:2d}. {marker} {title:55s} {out:5s} size={size:6.2f} val=${val:6.3f}")
        print(f"       conditionId: {cid}")

    if not execute:
        print()
        print("DRY RUN — re-run with EXECUTE=true to broadcast redemptions.")
        return 0

    # Build calls — group conditions by negRisk flag
    calls_regular: list[DepositWalletCall] = []
    calls_neg_risk: list[DepositWalletCall] = []
    seen_conditions: set[str] = set()

    for pos in redeemable:
        cid = pos.get("conditionId", "").lower()
        if not cid or cid in seen_conditions:
            continue
        seen_conditions.add(cid)
        neg = bool(pos.get("negativeRisk"))

        if neg:
            # Neg-risk redeemPositions(conditionId, amounts).
            # amounts is per outcome index (0/1 for binary).
            # Passing zeros lets the adapter compute from balances.
            data = neg_risk.encodeABI(
                fn_name="redeemPositions",
                args=[bytes.fromhex(cid[2:]), [0, 0]],
            )
            calls_neg_risk.append(
                DepositWalletCall(target=NEG_RISK_ADAPTER, value="0", data=data)
            )
        else:
            # Regular CTF redemption — passing both index sets catches
            # whichever side we hold (losing side burns to zero).
            data = ctf.encodeABI(
                fn_name="redeemPositions",
                args=[
                    Web3.to_checksum_address(PUSD),
                    b"\x00" * 32,
                    bytes.fromhex(cid[2:]),
                    [1, 2],
                ],
            )
            calls_regular.append(
                DepositWalletCall(target=CTF, value="0", data=data)
            )

    print()
    print(f"Will submit {len(calls_regular)} regular CTF redemption(s) and "
          f"{len(calls_neg_risk)} neg-risk redemption(s).")

    all_calls = calls_regular + calls_neg_risk
    nonce_payload = relayer.get_nonce(owner, TransactionType.WALLET.value)
    response = relayer.execute_deposit_wallet_batch(
        calls=all_calls,
        wallet_address=deposit_wallet,
        nonce=str(nonce_payload["nonce"]),
        deadline=str(int(time.time()) + 900),
    )
    print()
    print("Submitted batch. Waiting for confirmation…")
    result = _wait(response)
    print(json.dumps(result, default=str, indent=2)[:1500])

    # Re-check balance
    time.sleep(2)
    balance_after = pusd_contract.functions.balanceOf(deposit_wallet).call()
    diff = (balance_after - balance_before) / 1e6
    print()
    print(f"pUSD balance AFTER:  ${balance_after / 1e6:.4f}")
    print(f"NET REDEEMED:        ${diff:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
