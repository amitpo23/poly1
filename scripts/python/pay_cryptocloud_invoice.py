"""Pay the Polifly CryptoCloud invoice from the Polymarket deposit wallet.

Default mode is dry-run. Set CRYPTOCLOUD_EXECUTE=true only after reviewing
the printed invoice, quote, and call summary.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any

from dotenv import load_dotenv
from py_builder_relayer_client.client import RelayClient
from py_builder_relayer_client.models import DepositWalletCall, TransactionType
from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig
from web3 import Web3

from agents.polymarket.polymarket import Polymarket


INVOICE_UUID = os.getenv("CRYPTOCLOUD_INVOICE_UUID", "Y590PJQM")
CRYPTOCLOUD_API = "https://api.cryptocloud.plus"
LIFI_API = "https://li.quest/v1/quote"

USDC_E_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
POLYGON_CHAIN_ID = "137"
BASE_CHAIN_ID = "8453"

# Quote found to deliver slightly over 1.00 USDC on Base after bridge fees.
DEFAULT_FROM_AMOUNT = 1_015_000
MIN_BASE_USDC_TO_RECEIVE = 1_000_000

ERC20_ABI = [
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
        "name": "allowance",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


def _json_request(url: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "poly1/1.0",
        },
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


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


def _wait(response: Any) -> Any:
    confirmed = response.wait()
    if hasattr(confirmed, "to_dict"):
        return confirmed.to_dict()
    return confirmed


def _invoice() -> dict[str, Any]:
    info = _json_request(f"{CRYPTOCLOUD_API}/v2/invoice/checkout/info?invoice_uuid={INVOICE_UUID}")
    if info.get("status") == "success":
        result = info["result"]
        currency = result.get("currency") or {}
        if result.get("address") and currency.get("fullcode") == "USDC_BASE":
            return result

    payload = {
        "invoice_uuid": INVOICE_UUID,
        "currency_code": "USDC_BASE",
        "phone_number": "",
        "customer_invoice_email": os.getenv(
            "CRYPTOCLOUD_CUSTOMER_EMAIL", "amitporat1981@gmail.com"
        ),
    }
    resp = _json_request(f"{CRYPTOCLOUD_API}/v2/invoice/checkout/confirm", payload=payload)
    if resp.get("status") != "success":
        raise RuntimeError(f"CryptoCloud confirm failed: {resp}")
    return resp["result"]


def _quote(from_address: str, to_address: str, from_amount: int) -> dict[str, Any]:
    params = {
        "fromChain": POLYGON_CHAIN_ID,
        "toChain": BASE_CHAIN_ID,
        "fromToken": USDC_E_POLYGON,
        "toToken": USDC_BASE,
        "fromAddress": from_address,
        "toAddress": to_address,
        "fromAmount": str(from_amount),
        "slippage": os.getenv("CRYPTOCLOUD_LIFI_SLIPPAGE", "0.005"),
    }
    resp = _json_request(f"{LIFI_API}?{urllib.parse.urlencode(params)}")
    if "transactionRequest" not in resp:
        raise RuntimeError(f"LI.FI quote did not include a transaction: {resp}")
    return resp


def main() -> int:
    load_dotenv()
    execute = os.getenv("CRYPTOCLOUD_EXECUTE", "false").lower() == "true"
    from_amount = int(os.getenv("CRYPTOCLOUD_FROM_AMOUNT_USDC_E", DEFAULT_FROM_AMOUNT))

    pm = Polymarket(live=False)
    web3 = pm.web3
    builder_config = _builder_config()
    relayer_url = os.getenv("POLYMARKET_RELAYER_URL", "https://relayer-v2.polymarket.com/")

    relayer = RelayClient(
        relayer_url,
        137,
        pm.private_key,
        builder_config,
        rpc_url=os.getenv("POLYGON_RPC"),
    )
    owner = pm.get_address_for_private_key()
    deposit_wallet = Web3.to_checksum_address(
        os.getenv("POLYMARKET_DEPOSIT_WALLET") or relayer.get_expected_deposit_wallet()
    )

    invoice = _invoice()
    if invoice.get("invoice_status") not in {"waiting", "start"}:
        raise RuntimeError(f"Invoice is not payable: {invoice.get('invoice_status')}")
    currency = invoice.get("currency") or {}
    if currency.get("fullcode") != "USDC_BASE":
        raise RuntimeError(f"Unexpected invoice currency: {currency}")
    to_address = Web3.to_checksum_address(invoice["address"])

    quote = _quote(deposit_wallet, to_address, from_amount)
    tx = quote["transactionRequest"]
    to_amount = int(quote["estimate"]["toAmount"])
    to_amount_min = int(quote["estimate"]["toAmountMin"])
    if to_amount_min < MIN_BASE_USDC_TO_RECEIVE:
        raise RuntimeError(f"Quote delivers too little USDC on Base: {to_amount_min}")

    usdc = web3.eth.contract(address=Web3.to_checksum_address(USDC_E_POLYGON), abi=ERC20_ABI)
    spender = Web3.to_checksum_address(
        quote.get("estimate", {}).get("approvalAddress") or tx["to"]
    )
    balance = int(usdc.functions.balanceOf(deposit_wallet).call())
    allowance = int(usdc.functions.allowance(deposit_wallet, spender).call())
    if balance < from_amount:
        raise RuntimeError(f"Insufficient USDC.e: balance={balance}, need={from_amount}")

    summary = {
        "execute": execute,
        "invoice_uuid": invoice["uuid"],
        "invoice_status": invoice["invoice_status"],
        "invoice_expires": invoice["expiry_date"],
        "pay_to": to_address,
        "pay_currency": "USDC on Base",
        "invoice_amount_usdc": invoice["amount_to_pay"],
        "source_wallet": deposit_wallet,
        "source_amount_usdc_e": str(Decimal(from_amount) / Decimal(1_000_000)),
        "estimated_received_base_usdc": str(Decimal(to_amount) / Decimal(1_000_000)),
        "minimum_received_base_usdc": str(Decimal(to_amount_min) / Decimal(1_000_000)),
        "lifi_tool": quote.get("tool"),
        "lifi_spender": spender,
        "current_allowance_raw": allowance,
        "has_builder_relayer_creds": builder_config is not None,
    }
    print(json.dumps(summary, indent=2))

    if not builder_config:
        print("Missing builder relayer credentials; cannot submit.")
        return 2
    if not execute:
        print(
            "Dry mode only. Re-run with CRYPTOCLOUD_EXECUTE=true to submit "
            "approve+bridge batch."
        )
        return 0

    calls: list[DepositWalletCall] = []
    if allowance < from_amount:
        approve_data = usdc.encodeABI(fn_name="approve", args=[spender, from_amount])
        calls.append(DepositWalletCall(target=USDC_E_POLYGON, value="0", data=approve_data))
    calls.append(DepositWalletCall(target=tx["to"], value=str(int(tx.get("value", "0"), 16)), data=tx["data"]))

    nonce_payload = relayer.get_nonce(owner, TransactionType.WALLET.value)
    response = relayer.execute_deposit_wallet_batch(
        calls=calls,
        wallet_address=deposit_wallet,
        nonce=str(nonce_payload["nonce"]),
        deadline=str(int(time.time()) + 600),
    )
    print(json.dumps(_wait(response), default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
