"""Deploy/fund/approve a Polymarket deposit wallet.

This script requires Builder relayer credentials:
    BUILDER_API_KEY, BUILDER_SECRET, BUILDER_PASS_PHRASE

It performs no action unless EXECUTE=true is set for this script invocation.
Dry mode prints the derived addresses and missing prerequisites.
"""

from __future__ import annotations

import json
import os
import time

from dotenv import load_dotenv
from py_builder_relayer_client.client import RelayClient, RelayerTxType
from py_builder_relayer_client.models import DepositWalletCall, Transaction, TransactionType
from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig
from web3 import Web3

from agents.polymarket.polymarket import Polymarket


PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE = "0xe2222d279d744050d28e00520010520000310F59"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
MAX_UINT = 2**256 - 1

ERC20_ABI = [
    {
        "name": "transfer",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

ERC1155_ABI = [
    {
        "name": "setApprovalForAll",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"},
        ],
        "outputs": [],
    }
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
            key=key,
            secret=secret,
            passphrase=passphrase,
        )
    )


def _wait(response):
    confirmed = response.wait()
    if hasattr(confirmed, "to_dict"):
        return confirmed.to_dict()
    return confirmed


def main() -> int:
    load_dotenv()
    execute = os.getenv("EXECUTE", "false").lower() == "true"
    pm = Polymarket(live=False)
    web3 = pm.web3
    relayer_url = os.getenv("POLYMARKET_RELAYER_URL", "https://relayer-v2.polymarket.com/")
    builder_config = _builder_config()

    relayer = RelayClient(
        relayer_url,
        137,
        pm.private_key,
        builder_config,
        rpc_url=os.getenv("POLYGON_RPC"),
    )
    owner = pm.get_address_for_private_key()
    legacy_proxy = relayer.get_expected_proxy_wallet()
    deposit_wallet = relayer.get_expected_deposit_wallet()

    pusd = web3.eth.contract(address=Web3.to_checksum_address(PUSD), abi=ERC20_ABI)
    legacy_balance = pusd.functions.balanceOf(legacy_proxy).call()
    deposit_balance = pusd.functions.balanceOf(deposit_wallet).call()

    status = {
        "execute": execute,
        "owner": owner,
        "legacy_proxy": legacy_proxy,
        "deposit_wallet": deposit_wallet,
        "legacy_proxy_pusd": legacy_balance / 1e6,
        "deposit_wallet_pusd": deposit_balance / 1e6,
        "has_builder_relayer_creds": builder_config is not None,
    }
    print(json.dumps(status, indent=2))

    if not builder_config:
        print("Missing builder relayer credentials; cannot deploy/fund/approve.")
        return 2
    if not execute:
        print("Dry mode only. Re-run with EXECUTE=true to submit relayer transactions.")
        return 0

    deployed = relayer.get_deployed(deposit_wallet)
    if not deployed:
        print("Deploying deposit wallet...")
        print(json.dumps(_wait(relayer.deploy_deposit_wallet()), default=str, indent=2))

    if legacy_balance > 0:
        print("Transferring pUSD from legacy proxy to deposit wallet...")
        transfer_data = pusd.encodeABI(
            fn_name="transfer",
            args=[Web3.to_checksum_address(deposit_wallet), int(legacy_balance)],
        )
        proxy_relayer = RelayClient(
            relayer_url,
            137,
            pm.private_key,
            builder_config,
            relay_tx_type=RelayerTxType.PROXY,
            rpc_url=os.getenv("POLYGON_RPC"),
        )
        print(
            json.dumps(
                _wait(
                    proxy_relayer.execute(
                        [Transaction(to=PUSD, data=transfer_data, value="0")],
                        metadata="migrate pUSD to deposit wallet",
                    )
                ),
                default=str,
                indent=2,
            )
        )

    print("Approving deposit-wallet spenders...")
    ctf = web3.eth.contract(address=Web3.to_checksum_address(CTF), abi=ERC1155_ABI)
    calls = []
    for spender in (EXCHANGE, NEG_RISK_EXCHANGE, NEG_RISK_ADAPTER, CTF):
        calls.append(
            DepositWalletCall(
                target=PUSD,
                value="0",
                data=pusd.encodeABI(
                    fn_name="approve",
                    args=[Web3.to_checksum_address(spender), MAX_UINT],
                ),
            )
        )
    for operator in (EXCHANGE, NEG_RISK_EXCHANGE):
        calls.append(
            DepositWalletCall(
                target=CTF,
                value="0",
                data=ctf.encodeABI(
                    fn_name="setApprovalForAll",
                    args=[Web3.to_checksum_address(operator), True],
                ),
            )
        )

    nonce_payload = relayer.get_nonce(owner, TransactionType.WALLET.value)
    response = relayer.execute_deposit_wallet_batch(
        calls=calls,
        wallet_address=deposit_wallet,
        nonce=str(nonce_payload["nonce"]),
        deadline=str(int(time.time()) + 600),
    )
    print(json.dumps(_wait(response), default=str, indent=2))

    print(
        "Set POLYMARKET_DEPOSIT_WALLET=%s and POLYMARKET_SIGNATURE_TYPE=3"
        % deposit_wallet
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
