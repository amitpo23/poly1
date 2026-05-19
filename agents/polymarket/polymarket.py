# core polymarket api
# https://github.com/Polymarket/py-clob-client/tree/main/examples

import logging
import os
import pdb
import time
import ast
import requests

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

MAX_MARKET_ORDER_SLIPPAGE = float(os.getenv("POLYMARKET_MAX_SLIPPAGE", "0.03"))
MIN_MARKET_ORDER_USDC = float(os.getenv("POLYMARKET_MIN_ORDER_USDC", "1.0"))
MIN_ENTRY_PRICE = float(os.getenv("MIN_ENTRY_PRICE", "0.10"))
MIN_BID_DEPTH_USDC = float(os.getenv("MIN_BID_DEPTH_USDC", "20.0"))
MAX_ENTRY_SPREAD_PCT = float(os.getenv("MAX_ENTRY_SPREAD_PCT", "0.05"))

from web3 import Web3
from web3.constants import MAX_INT
from web3.middleware import geth_poa_middleware

import httpx
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, BuilderConfig
from py_clob_client_v2.constants import AMOY, POLYGON
from py_clob_client_v2.exceptions import PolyApiException
from py_order_utils.builders import OrderBuilder
from py_order_utils.model import OrderData
from py_order_utils.signer import Signer
from py_clob_client_v2.clob_types import (
    OrderArgs,
    MarketOrderArgsV2 as MarketOrderArgs,
    OrderType,
    OrderBookSummary,
)
from py_clob_client_v2.order_builder.constants import BUY
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from agents.utils.objects import SimpleMarket, SimpleEvent, TradeRecommendation

load_dotenv()


class Polymarket:
    def __init__(self, live: bool = True) -> None:
        self.live = live
        self.gamma_url = "https://gamma-api.polymarket.com"
        self.gamma_markets_endpoint = self.gamma_url + "/markets"
        self.gamma_events_endpoint = self.gamma_url + "/events"

        self.clob_url = "https://clob.polymarket.com"
        self.clob_auth_endpoint = self.clob_url + "/auth/api-key"

        self.chain_id = 137  # POLYGON
        self.private_key = os.getenv("POLYGON_WALLET_PRIVATE_KEY")
        # Default RPC: free public node. polygon-rpc.com used to be free but
        # has moved to authenticated tenants only; drpc.org is currently open.
        # Override via env if you have a paid Alchemy/Infura/QuickNode key.
        self.polygon_rpc = os.getenv("POLYGON_RPC", "https://polygon.drpc.org")
        self.w3 = Web3(Web3.HTTPProvider(self.polygon_rpc))

        # Deposit-wallet mode: CLOB v2 now requires new API users to trade
        # through a deposit wallet (signature_type=3). Set
        # POLYMARKET_DEPOSIT_WALLET once it has been deployed/funded.
        deposit_wallet_env = os.getenv("POLYMARKET_DEPOSIT_WALLET", "").strip()
        self.deposit_wallet = (
            Web3.to_checksum_address(deposit_wallet_env)
            if deposit_wallet_env
            else None
        )

        # POLY_PROXY mode: set POLYMARKET_FUNDER to the proxy address shown
        # on polymarket.com/settings (Privy/Magic embedded wallets, e.g. Google
        # login). When set, the bot signs orders with the EOA derived from
        # POLYGON_WALLET_PRIVATE_KEY but uses the proxy as the order funder
        # and balance source. Unset -> classic EOA-only mode (MetaMask).
        funder_env = os.getenv("POLYMARKET_FUNDER", "").strip()
        legacy_funder = Web3.to_checksum_address(funder_env) if funder_env else None
        self.funder = self.deposit_wallet or legacy_funder
        signature_type_env = os.getenv("POLYMARKET_SIGNATURE_TYPE", "").strip()
        if signature_type_env:
            self.signature_type = int(signature_type_env)
        elif self.deposit_wallet:
            self.signature_type = 3
        elif self.funder:
            self.signature_type = 1
        else:
            self.signature_type = None

        # CLOB v2 requires every order to carry a non-zero builder_code attribution.
        # Create one in polymarket.com/settings?tab=builder. Without it the new
        # exchange returns "maker address not allowed, please use the deposit
        # wallet flow" even when funder/allowances/balance are all correct.
        self.builder_code = os.getenv("POLYMARKET_BUILDER_CODE", "").strip() or None
        builder_addr_env = os.getenv("POLYMARKET_BUILDER_ADDRESS", "").strip()
        if builder_addr_env:
            self.builder_address = Web3.to_checksum_address(builder_addr_env)
        else:
            self.builder_address = self.funder or ""
            if self.builder_code and self.funder:
                logger.warning(
                    "POLYMARKET_BUILDER_ADDRESS not set; falling back to funder=%s. "
                    "Set POLYMARKET_BUILDER_ADDRESS explicitly for builder attribution.",
                    self.funder,
                )

        if self.funder:
            # Post-pUSD migration addresses (proxy/Privy users trade against
            # these). pUSD is a 6-decimal ERC-20 wrapping USDC.e.
            self.exchange_address = "0xE111180000d2663C0091e4f400237545B87B996B"
            self.neg_risk_exchange_address = "0xe2222d279d744050d28e00520010520000310F59"
        else:
            # Legacy EOA addresses (kept for backward compat with MetaMask wallets).
            self.exchange_address = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
            self.neg_risk_exchange_address = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

        self.erc20_approve = """[{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"owner","type":"address"},{"indexed":true,"internalType":"address","name":"spender","type":"address"},{"indexed":false,"internalType":"uint256","name":"value","type":"uint256"}],"name":"Approval","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"authorizer","type":"address"},{"indexed":true,"internalType":"bytes32","name":"nonce","type":"bytes32"}],"name":"AuthorizationCanceled","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"authorizer","type":"address"},{"indexed":true,"internalType":"bytes32","name":"nonce","type":"bytes32"}],"name":"AuthorizationUsed","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"account","type":"address"}],"name":"Blacklisted","type":"event"},{"anonymous":false,"inputs":[{"indexed":false,"internalType":"address","name":"userAddress","type":"address"},{"indexed":false,"internalType":"address payable","name":"relayerAddress","type":"address"},{"indexed":false,"internalType":"bytes","name":"functionSignature","type":"bytes"}],"name":"MetaTransactionExecuted","type":"event"},{"anonymous":false,"inputs":[],"name":"Pause","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"newRescuer","type":"address"}],"name":"RescuerChanged","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"bytes32","name":"role","type":"bytes32"},{"indexed":true,"internalType":"bytes32","name":"previousAdminRole","type":"bytes32"},{"indexed":true,"internalType":"bytes32","name":"newAdminRole","type":"bytes32"}],"name":"RoleAdminChanged","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"bytes32","name":"role","type":"bytes32"},{"indexed":true,"internalType":"address","name":"account","type":"address"},{"indexed":true,"internalType":"address","name":"sender","type":"address"}],"name":"RoleGranted","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"bytes32","name":"role","type":"bytes32"},{"indexed":true,"internalType":"address","name":"account","type":"address"},{"indexed":true,"internalType":"address","name":"sender","type":"address"}],"name":"RoleRevoked","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"from","type":"address"},{"indexed":true,"internalType":"address","name":"to","type":"address"},{"indexed":false,"internalType":"uint256","name":"value","type":"uint256"}],"name":"Transfer","type":"event"},{"anonymous":false,"inputs":[{"indexed":true,"internalType":"address","name":"account","type":"address"}],"name":"UnBlacklisted","type":"event"},{"anonymous":false,"inputs":[],"name":"Unpause","type":"event"},{"inputs":[],"name":"APPROVE_WITH_AUTHORIZATION_TYPEHASH","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"BLACKLISTER_ROLE","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"CANCEL_AUTHORIZATION_TYPEHASH","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"DECREASE_ALLOWANCE_WITH_AUTHORIZATION_TYPEHASH","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"DEFAULT_ADMIN_ROLE","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"DEPOSITOR_ROLE","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"DOMAIN_SEPARATOR","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"EIP712_VERSION","outputs":[{"internalType":"string","name":"","type":"string"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"INCREASE_ALLOWANCE_WITH_AUTHORIZATION_TYPEHASH","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"META_TRANSACTION_TYPEHASH","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"PAUSER_ROLE","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"PERMIT_TYPEHASH","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"RESCUER_ROLE","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"TRANSFER_WITH_AUTHORIZATION_TYPEHASH","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"WITHDRAW_WITH_AUTHORIZATION_TYPEHASH","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"owner","type":"address"},{"internalType":"address","name":"spender","type":"address"}],"name":"allowance","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"approve","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"owner","type":"address"},{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"value","type":"uint256"},{"internalType":"uint256","name":"validAfter","type":"uint256"},{"internalType":"uint256","name":"validBefore","type":"uint256"},{"internalType":"bytes32","name":"nonce","type":"bytes32"},{"internalType":"uint8","name":"v","type":"uint8"},{"internalType":"bytes32","name":"r","type":"bytes32"},{"internalType":"bytes32","name":"s","type":"bytes32"}],"name":"approveWithAuthorization","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"authorizer","type":"address"},{"internalType":"bytes32","name":"nonce","type":"bytes32"}],"name":"authorizationState","outputs":[{"internalType":"enum GasAbstraction.AuthorizationState","name":"","type":"uint8"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"blacklist","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[],"name":"blacklisters","outputs":[{"internalType":"address[]","name":"","type":"address[]"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"authorizer","type":"address"},{"internalType":"bytes32","name":"nonce","type":"bytes32"},{"internalType":"uint8","name":"v","type":"uint8"},{"internalType":"bytes32","name":"r","type":"bytes32"},{"internalType":"bytes32","name":"s","type":"bytes32"}],"name":"cancelAuthorization","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[],"name":"decimals","outputs":[{"internalType":"uint8","name":"","type":"uint8"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"subtractedValue","type":"uint256"}],"name":"decreaseAllowance","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"owner","type":"address"},{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"decrement","type":"uint256"},{"internalType":"uint256","name":"validAfter","type":"uint256"},{"internalType":"uint256","name":"validBefore","type":"uint256"},{"internalType":"bytes32","name":"nonce","type":"bytes32"},{"internalType":"uint8","name":"v","type":"uint8"},{"internalType":"bytes32","name":"r","type":"bytes32"},{"internalType":"bytes32","name":"s","type":"bytes32"}],"name":"decreaseAllowanceWithAuthorization","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"user","type":"address"},{"internalType":"bytes","name":"depositData","type":"bytes"}],"name":"deposit","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"userAddress","type":"address"},{"internalType":"bytes","name":"functionSignature","type":"bytes"},{"internalType":"bytes32","name":"sigR","type":"bytes32"},{"internalType":"bytes32","name":"sigS","type":"bytes32"},{"internalType":"uint8","name":"sigV","type":"uint8"}],"name":"executeMetaTransaction","outputs":[{"internalType":"bytes","name":"","type":"bytes"}],"stateMutability":"payable","type":"function"},{"inputs":[{"internalType":"bytes32","name":"role","type":"bytes32"}],"name":"getRoleAdmin","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"bytes32","name":"role","type":"bytes32"},{"internalType":"uint256","name":"index","type":"uint256"}],"name":"getRoleMember","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"bytes32","name":"role","type":"bytes32"}],"name":"getRoleMemberCount","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"bytes32","name":"role","type":"bytes32"},{"internalType":"address","name":"account","type":"address"}],"name":"grantRole","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"bytes32","name":"role","type":"bytes32"},{"internalType":"address","name":"account","type":"address"}],"name":"hasRole","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"addedValue","type":"uint256"}],"name":"increaseAllowance","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"owner","type":"address"},{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"increment","type":"uint256"},{"internalType":"uint256","name":"validAfter","type":"uint256"},{"internalType":"uint256","name":"validBefore","type":"uint256"},{"internalType":"bytes32","name":"nonce","type":"bytes32"},{"internalType":"uint8","name":"v","type":"uint8"},{"internalType":"bytes32","name":"r","type":"bytes32"},{"internalType":"bytes32","name":"s","type":"bytes32"}],"name":"increaseAllowanceWithAuthorization","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"string","name":"newName","type":"string"},{"internalType":"string","name":"newSymbol","type":"string"},{"internalType":"uint8","name":"newDecimals","type":"uint8"},{"internalType":"address","name":"childChainManager","type":"address"}],"name":"initialize","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[],"name":"initialized","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"isBlacklisted","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"name","outputs":[{"internalType":"string","name":"","type":"string"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"owner","type":"address"}],"name":"nonces","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"pause","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[],"name":"paused","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"pausers","outputs":[{"internalType":"address[]","name":"","type":"address[]"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"owner","type":"address"},{"internalType":"address","name":"spender","type":"address"},{"internalType":"uint256","name":"value","type":"uint256"},{"internalType":"uint256","name":"deadline","type":"uint256"},{"internalType":"uint8","name":"v","type":"uint8"},{"internalType":"bytes32","name":"r","type":"bytes32"},{"internalType":"bytes32","name":"s","type":"bytes32"}],"name":"permit","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"bytes32","name":"role","type":"bytes32"},{"internalType":"address","name":"account","type":"address"}],"name":"renounceRole","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"contract IERC20","name":"tokenContract","type":"address"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"rescueERC20","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[],"name":"rescuers","outputs":[{"internalType":"address[]","name":"","type":"address[]"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"bytes32","name":"role","type":"bytes32"},{"internalType":"address","name":"account","type":"address"}],"name":"revokeRole","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[],"name":"symbol","outputs":[{"internalType":"string","name":"","type":"string"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"totalSupply","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"transfer","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"sender","type":"address"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"transferFrom","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"from","type":"address"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"value","type":"uint256"},{"internalType":"uint256","name":"validAfter","type":"uint256"},{"internalType":"uint256","name":"validBefore","type":"uint256"},{"internalType":"bytes32","name":"nonce","type":"bytes32"},{"internalType":"uint8","name":"v","type":"uint8"},{"internalType":"bytes32","name":"r","type":"bytes32"},{"internalType":"bytes32","name":"s","type":"bytes32"}],"name":"transferWithAuthorization","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"unBlacklist","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[],"name":"unpause","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"string","name":"newName","type":"string"},{"internalType":"string","name":"newSymbol","type":"string"}],"name":"updateMetadata","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"withdraw","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"owner","type":"address"},{"internalType":"uint256","name":"value","type":"uint256"},{"internalType":"uint256","name":"validAfter","type":"uint256"},{"internalType":"uint256","name":"validBefore","type":"uint256"},{"internalType":"bytes32","name":"nonce","type":"bytes32"},{"internalType":"uint8","name":"v","type":"uint8"},{"internalType":"bytes32","name":"r","type":"bytes32"},{"internalType":"bytes32","name":"s","type":"bytes32"}],"name":"withdrawWithAuthorization","outputs":[],"stateMutability":"nonpayable","type":"function"}]"""
        self.erc1155_set_approval = """[{"inputs": [{ "internalType": "address", "name": "operator", "type": "address" },{ "internalType": "bool", "name": "approved", "type": "bool" }],"name": "setApprovalForAll","outputs": [],"stateMutability": "nonpayable","type": "function"}]"""

        if self.funder:
            # pUSD ERC-20 (6 decimals) — collateral token on the new CLOB.
            self.collateral_address = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
        else:
            # USDC.e — legacy collateral for EOA users.
            self.collateral_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        self.usdc_address = self.collateral_address  # alias, retained for callers
        self.ctf_address = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

        self.web3 = Web3(Web3.HTTPProvider(self.polygon_rpc))
        self.web3.middleware_onion.inject(geth_poa_middleware, layer=0)

        self.usdc = self.web3.eth.contract(
            address=self.collateral_address, abi=self.erc20_approve
        )
        self.ctf = self.web3.eth.contract(
            address=self.ctf_address, abi=self.erc1155_set_approval
        )

        if self.live:
            self._init_api_keys()
            self._init_approvals(False)
        else:
            self.client = None
            self.credentials = None

    def _init_api_keys(self) -> None:
        builder_config = (
            BuilderConfig(
                builder_address=self.builder_address,
                builder_code=self.builder_code,
            )
            if self.builder_code
            else None
        )

        signature_type = self.signature_type
        funder = self.funder if self.funder else None

        env_creds = self._api_creds_from_env()
        if env_creds:
            self.credentials = env_creds
        else:
            # L1 client: signs the auth request with the private key to derive
            # existing L2 API credentials. Derive first to avoid a noisy
            # "Could not create api key" response on accounts that already
            # have a CLOB API key.
            l1_client = ClobClient(
                self.clob_url,
                key=self.private_key,
                chain_id=self.chain_id,
                signature_type=signature_type,
                funder=funder,
                builder_config=builder_config,
            )
            try:
                self.credentials = l1_client.derive_api_key()
            except PolyApiException:
                self.credentials = l1_client.create_api_key()

        # L2 client: authenticated for order posting, cancellations, and
        # account methods. This matches the v2 SDK examples and avoids relying
        # on mutating client mode after construction.
        self.client = ClobClient(
            self.clob_url,
            key=self.private_key,
            chain_id=self.chain_id,
            creds=self.credentials,
            signature_type=signature_type,
            funder=funder,
            builder_config=builder_config,
        )
        # Patch session timeout: py_clob_client uses requests with no default timeout.
        try:
            session = getattr(self.client, "session", None)
            if session is not None:
                original_request = session.request

                def _request_with_timeout(method, url, **kwargs):
                    kwargs.setdefault("timeout", 20)
                    return original_request(method, url, **kwargs)

                session.request = _request_with_timeout
        except Exception:
            pass

    def _api_creds_from_env(self) -> ApiCreds:
        api_key = os.getenv("POLYMARKET_CLOB_API_KEY", "").strip()
        api_secret = os.getenv("POLYMARKET_CLOB_API_SECRET", "").strip()
        api_passphrase = os.getenv("POLYMARKET_CLOB_API_PASSPHRASE", "").strip()
        if api_key and api_secret and api_passphrase:
            return ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )
        return None

    def _init_approvals(self, run: bool = False) -> None:
        if not run:
            return
        if self.funder:
            # POLY_PROXY: Polymarket auto-sets allowances on proxy deployment;
            # the EOA cannot approve on behalf of the proxy anyway.
            return

        priv_key = self.private_key
        pub_key = self.get_address_for_private_key()
        chain_id = self.chain_id
        web3 = self.web3
        nonce = web3.eth.get_transaction_count(pub_key)
        usdc = self.usdc
        ctf = self.ctf

        # CTF Exchange
        raw_usdc_approve_txn = usdc.functions.approve(
            "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E", int(MAX_INT, 0)
        ).build_transaction({"chainId": chain_id, "from": pub_key, "nonce": nonce})
        signed_usdc_approve_tx = web3.eth.account.sign_transaction(
            raw_usdc_approve_txn, private_key=priv_key
        )
        send_usdc_approve_tx = web3.eth.send_raw_transaction(
            signed_usdc_approve_tx.raw_transaction
        )
        usdc_approve_tx_receipt = web3.eth.wait_for_transaction_receipt(
            send_usdc_approve_tx, 600
        )
        print(usdc_approve_tx_receipt)

        nonce = web3.eth.get_transaction_count(pub_key)

        raw_ctf_approval_txn = ctf.functions.setApprovalForAll(
            "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E", True
        ).build_transaction({"chainId": chain_id, "from": pub_key, "nonce": nonce})
        signed_ctf_approval_tx = web3.eth.account.sign_transaction(
            raw_ctf_approval_txn, private_key=priv_key
        )
        send_ctf_approval_tx = web3.eth.send_raw_transaction(
            signed_ctf_approval_tx.raw_transaction
        )
        ctf_approval_tx_receipt = web3.eth.wait_for_transaction_receipt(
            send_ctf_approval_tx, 600
        )
        print(ctf_approval_tx_receipt)

        nonce = web3.eth.get_transaction_count(pub_key)

        # Neg Risk CTF Exchange
        raw_usdc_approve_txn = usdc.functions.approve(
            "0xC5d563A36AE78145C45a50134d48A1215220f80a", int(MAX_INT, 0)
        ).build_transaction({"chainId": chain_id, "from": pub_key, "nonce": nonce})
        signed_usdc_approve_tx = web3.eth.account.sign_transaction(
            raw_usdc_approve_txn, private_key=priv_key
        )
        send_usdc_approve_tx = web3.eth.send_raw_transaction(
            signed_usdc_approve_tx.raw_transaction
        )
        usdc_approve_tx_receipt = web3.eth.wait_for_transaction_receipt(
            send_usdc_approve_tx, 600
        )
        print(usdc_approve_tx_receipt)

        nonce = web3.eth.get_transaction_count(pub_key)

        raw_ctf_approval_txn = ctf.functions.setApprovalForAll(
            "0xC5d563A36AE78145C45a50134d48A1215220f80a", True
        ).build_transaction({"chainId": chain_id, "from": pub_key, "nonce": nonce})
        signed_ctf_approval_tx = web3.eth.account.sign_transaction(
            raw_ctf_approval_txn, private_key=priv_key
        )
        send_ctf_approval_tx = web3.eth.send_raw_transaction(
            signed_ctf_approval_tx.raw_transaction
        )
        ctf_approval_tx_receipt = web3.eth.wait_for_transaction_receipt(
            send_ctf_approval_tx, 600
        )
        print(ctf_approval_tx_receipt)

        nonce = web3.eth.get_transaction_count(pub_key)

        # Neg Risk Adapter
        raw_usdc_approve_txn = usdc.functions.approve(
            "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296", int(MAX_INT, 0)
        ).build_transaction({"chainId": chain_id, "from": pub_key, "nonce": nonce})
        signed_usdc_approve_tx = web3.eth.account.sign_transaction(
            raw_usdc_approve_txn, private_key=priv_key
        )
        send_usdc_approve_tx = web3.eth.send_raw_transaction(
            signed_usdc_approve_tx.raw_transaction
        )
        usdc_approve_tx_receipt = web3.eth.wait_for_transaction_receipt(
            send_usdc_approve_tx, 600
        )
        print(usdc_approve_tx_receipt)

        nonce = web3.eth.get_transaction_count(pub_key)

        raw_ctf_approval_txn = ctf.functions.setApprovalForAll(
            "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296", True
        ).build_transaction({"chainId": chain_id, "from": pub_key, "nonce": nonce})
        signed_ctf_approval_tx = web3.eth.account.sign_transaction(
            raw_ctf_approval_txn, private_key=priv_key
        )
        send_ctf_approval_tx = web3.eth.send_raw_transaction(
            signed_ctf_approval_tx.raw_transaction
        )
        ctf_approval_tx_receipt = web3.eth.wait_for_transaction_receipt(
            send_ctf_approval_tx, 600
        )
        print(ctf_approval_tx_receipt)

    def get_all_markets(self) -> "list[SimpleMarket]":
        markets = []
        res = httpx.get(self.gamma_markets_endpoint)
        if res.status_code == 200:
            for market in res.json():
                try:
                    market_data = self.map_api_to_market(market)
                    markets.append(SimpleMarket(**market_data))
                except Exception as e:
                    print(e)
                    pass
        return markets

    def filter_markets_for_trading(self, markets: "list[SimpleMarket]"):
        tradeable_markets = []
        for market in markets:
            if market.active:
                tradeable_markets.append(market)
        return tradeable_markets

    def get_market(self, token_id: str) -> SimpleMarket:
        params = {"clob_token_ids": token_id}
        res = httpx.get(self.gamma_markets_endpoint, params=params)
        if res.status_code == 200:
            data = res.json()
            market = data[0]
            return self.map_api_to_market(market, token_id)

    def map_api_to_market(self, market, token_id: str = "") -> SimpleMarket:
        # Some Gamma responses post-migration omit fields like outcomePrices
        # or endDate for markets in odd states. Use .get with safe defaults
        # rather than crash the cycle on a single bad market.
        market = {
            "id": int(market["id"]),
            "question": market.get("question", ""),
            "end": market.get("endDate") or market.get("endDateIso", ""),
            "description": market.get("description", ""),
            "active": market.get("active", False),
            "funded": market.get("funded", False),
            "rewardsMinSize": float(market.get("rewardsMinSize") or 0),
            "rewardsMaxSpread": float(market.get("rewardsMaxSpread") or 0),
            "spread": float(market.get("spread") or 0),
            "outcomes": str(market.get("outcomes", "[]")),
            "outcome_prices": str(market.get("outcomePrices", "[]")),
            "clob_token_ids": str(market.get("clobTokenIds", "[]")),
        }
        if token_id:
            market["clob_token_ids"] = token_id
        return market

    def get_all_events(self) -> "list[SimpleEvent]":
        """Fetch active+open events from gamma. Paginates up to GAMMA_EVENT_LIMIT."""
        max_total = int(os.getenv("GAMMA_EVENT_LIMIT", "200"))
        page_size = 100
        events = []
        offset = 0
        while len(events) < max_total:
            try:
                res = httpx.get(
                    self.gamma_events_endpoint,
                    params={
                        "limit": page_size,
                        "offset": offset,
                        "active": "true",
                        "closed": "false",
                        "archived": "false",
                    },
                    timeout=15,
                )
            except httpx.HTTPError as e:
                logger.warning("gamma events fetch failed: %s", e)
                break
            if res.status_code != 200:
                logger.warning("gamma events bad status: %s", res.status_code)
                break
            batch = res.json()
            if not batch:
                break
            for event in batch:
                try:
                    event_data = self.map_api_to_event(event)
                    events.append(SimpleEvent(**event_data))
                except Exception as e:
                    logger.debug("skip event %s: %s", event.get("id"), e)
            if len(batch) < page_size:
                break
            offset += page_size
        logger.info("get_all_events: fetched %d events", len(events))
        return events

    def map_api_to_event(self, event) -> SimpleEvent:
        description = event["description"] if "description" in event.keys() else ""
        return {
            "id": int(event["id"]),
            "ticker": event["ticker"],
            "slug": event["slug"],
            "title": event["title"],
            "description": description,
            "active": event["active"],
            "closed": event["closed"],
            "archived": event["archived"],
            "new": event["new"],
            "featured": event["featured"],
            "restricted": event["restricted"],
            "end": event["endDate"],
            "markets": ",".join(str(x["id"]) for x in event["markets"]),
        }

    def filter_events_for_trading(
        self, events: "list[SimpleEvent]"
    ) -> "list[SimpleEvent]":
        # NOTE: `restricted` on the gamma API marks events that Polymarket
        # restricts from US-based UI users; it does NOT mean the CLOB rejects
        # orders. As of 2026-05, ALL active+open events are flagged restricted,
        # so excluding them here would idle the bot forever. We filter by
        # active/closed/archived only and let the wallet's jurisdiction decide.
        # Override with EXCLUDE_RESTRICTED=true if needed.
        exclude_restricted = os.getenv("EXCLUDE_RESTRICTED", "false").lower() == "true"
        tradeable_events = []
        for event in events:
            if not (event.active and not event.archived and not event.closed):
                continue
            if exclude_restricted and event.restricted:
                continue
            tradeable_events.append(event)
        return tradeable_events

    def get_all_tradeable_events(self) -> "list[SimpleEvent]":
        all_events = self.get_all_events()
        return self.filter_events_for_trading(all_events)

    def get_sampling_simplified_markets(self) -> "list[SimpleEvent]":
        markets = []
        raw_sampling_simplified_markets = self.client.get_sampling_simplified_markets()
        for raw_market in raw_sampling_simplified_markets["data"]:
            token_one_id = raw_market["tokens"][0]["token_id"]
            market = self.get_market(token_one_id)
            markets.append(market)
        return markets

    def get_orderbook(self, token_id: str) -> OrderBookSummary:
        return self.client.get_order_book(token_id)

    def get_orderbook_price(self, token_id: str) -> float:
        return float(self.client.get_price(token_id))

    def get_address_for_private_key(self):
        account = self.w3.eth.account.from_key(str(self.private_key))
        return account.address

    def build_order(
        self,
        market_token: str,
        amount: float,
        nonce: str = str(round(time.time())),  # for cancellations
        side: str = "BUY",
        expiration: str = "0",  # timestamp after which order expires
    ):
        signer = Signer(self.private_key)
        builder = OrderBuilder(self.exchange_address, self.chain_id, signer)

        buy = side == "BUY"
        side = 0 if buy else 1
        maker_amount = amount if buy else 0
        taker_amount = amount if not buy else 0
        order_data = OrderData(
            maker=self.get_address_for_private_key(),
            tokenId=market_token,
            makerAmount=maker_amount,
            takerAmount=taker_amount,
            feeRateBps="1",
            nonce=nonce,
            side=side,
            expiration=expiration,
        )
        order = builder.build_signed_order(order_data)
        return order

    def execute_order(self, price, size, side, token_id) -> str:
        return self.client.create_and_post_order(
            OrderArgs(price=price, size=size, side=side, token_id=token_id)
        )

    def sell_shares(
        self,
        token_id: str,
        shares: float,
        limit_price: float,
        order_type=None,
    ) -> dict:
        """Place a SELL order to liquidate held shares.

        Used by `position_manager` to exit a position via take-profit /
        stop-loss / timeout. `limit_price` is the floor — Polymarket will
        execute at this or better. For an aggressive exit (don't sit on
        bid), use FAK order_type with a price near the current best bid.

        Returns the raw response dict from CLOB. Status `matched` =
        filled (some/all). Status `delayed` / `unmatched` = order
        sitting in the book.
        """
        if order_type is None:
            order_type = OrderType.GTC
        return self.client.create_and_post_order(
            OrderArgs(
                price=limit_price,
                size=shares,
                side="SELL",
                token_id=token_id,
            ),
            order_type=order_type,
        )

    def _book_entries(self, book, side: str) -> list:
        entries = getattr(book, side, None)
        if entries is None and isinstance(book, dict):
            entries = book.get(side, [])
        return entries or []

    def _entry_price_size(self, entry) -> tuple[float, float]:
        if hasattr(entry, "price"):
            return float(entry.price), float(entry.size)
        return float(entry["price"]), float(entry["size"])

    def bid_depth_usdc(self, token_id: str) -> float:
        """Return total bid-side depth in USDC for *token_id*.

        Used by position_manager to check exit liquidity before selling.
        Returns 0.0 on any error (fail-open: caller decides whether to
        proceed or defer).
        """
        try:
            book = self.client.get_order_book(token_id)
            bids = self._book_entries(book, "bids")
            total = 0.0
            for b in bids:
                p, s = self._entry_price_size(b)
                total += p * s
            return total
        except Exception as exc:
            logger.warning("bid_depth_usdc failed for %s: %s", token_id[:18], exc)
            return 0.0

    def _fillable_market_buy(self, token_id: str, amount_usdc: float) -> tuple[float, float, float]:
        """Return (limit_price, fillable_usdc, avg_price) for a FOK market buy.

        CLOB market BUY amount is a USDC budget. To avoid FOK kills caused by
        stale model prices, walk the live ask book and pick the worst ask needed
        to fill the target amount. If liquidity is thin, fill only the available
        amount above the configured minimum.
        """
        book = self.client.get_order_book(token_id)
        asks = sorted(
            (self._entry_price_size(a) for a in self._book_entries(book, "asks")),
            key=lambda item: item[0],
        )
        if not asks:
            raise ValueError(f"no asks available for token_id={token_id}")

        best_ask = asks[0][0]
        if best_ask < MIN_ENTRY_PRICE:
            raise ValueError(
                f"below MIN_ENTRY_PRICE: best_ask={best_ask:.4f} < {MIN_ENTRY_PRICE}"
            )

        # Fix 2 — bid-side depth check: ensure there is exit liquidity.
        bids = sorted(
            (self._entry_price_size(b) for b in self._book_entries(book, "bids")),
            key=lambda item: item[0],
            reverse=True,
        )
        total_bid_usdc = sum(p * s for p, s in bids)
        if not bids or total_bid_usdc < MIN_BID_DEPTH_USDC:
            raise ValueError(
                f"insufficient bid depth: total_bid_usdc={total_bid_usdc:.2f}"
                f" < {MIN_BID_DEPTH_USDC}"
            )

        # Fix 3 — spread check: best_ask vs best_bid.
        best_bid = bids[0][0]
        if best_ask > 0:
            spread_pct = (best_ask - best_bid) / best_ask
            if spread_pct > MAX_ENTRY_SPREAD_PCT:
                raise ValueError(
                    f"spread too wide: {spread_pct:.4f} > {MAX_ENTRY_SPREAD_PCT}"
                )

        remaining = amount_usdc
        spend = 0.0
        tokens = 0.0
        worst_price = None
        for price, size_tokens in asks:
            if price <= 0:
                continue
            level_cost = price * size_tokens
            take_cost = min(remaining, level_cost)
            if take_cost <= 0:
                continue
            spend += take_cost
            tokens += take_cost / price
            worst_price = price
            remaining -= take_cost
            if remaining <= 1e-9:
                break

        if spend < MIN_MARKET_ORDER_USDC:
            raise ValueError(
                f"insufficient ask liquidity for token_id={token_id}: "
                f"fillable_usdc={spend:.4f} < min={MIN_MARKET_ORDER_USDC:.4f}"
            )

        tick_size = 0.01
        if isinstance(book, dict) and book.get("tick_size"):
            tick_size = float(book["tick_size"])
        elif hasattr(book, "tick_size") and book.tick_size:
            tick_size = float(book.tick_size)
        limit_price = min(1.0 - tick_size, (worst_price or asks[0][0]) + tick_size)
        avg_price = spend / tokens if tokens else limit_price
        return limit_price, min(amount_usdc, spend), avg_price

    def execute_market_order(
        self, market, recommendation: TradeRecommendation, order_type=None
    ) -> dict:
        if order_type is None:
            order_type = OrderType.FOK

        if self.client is None:
            raise RuntimeError(
                "Polymarket initialized in read-only mode (live=False); "
                "cannot execute orders."
            )

        metadata = market[0].dict()["metadata"]
        token_ids = ast.literal_eval(metadata["clob_token_ids"])
        outcomes = ast.literal_eval(metadata["outcomes"])
        outcome_prices_raw = metadata.get("outcome_prices")

        if len(outcomes) != 2 or len(token_ids) != 2:
            raise ValueError(
                f"execute_market_order supports binary markets only; "
                f"got outcomes={outcomes} token_ids={token_ids}"
            )

        # CLOB market orders are always BUY of a specific token.
        # Convention (must match the LLM prompt at prompts.py:one_best_trade):
        #   outcomes[0] is the "primary" outcome and the LLM anchors `price` to it.
        #   side=BUY  => buy outcomes[0] at recommendation.price
        #   side=SELL => bet against outcomes[0] = buy outcomes[1] at (1 - recommendation.price)
        side = recommendation.side.upper()
        if side == "BUY":
            token_id = token_ids[0]
            order_price = recommendation.price
        elif side == "SELL":
            token_id = token_ids[1]
            order_price = 1.0 - recommendation.price
        else:
            raise ValueError(f"side must be BUY or SELL, got {side}")

        if not 0.0 < order_price < 1.0:
            raise ValueError(
                f"order_price out of (0,1): {order_price} from side={side} "
                f"recommendation.price={recommendation.price}"
            )

        # Sanity check: warn if recommendation.price is on the wrong side of 0.5
        # for what `side` claims (e.g. side=BUY but price=0.2 implies the LLM
        # actually thought outcomes[0] was unlikely).
        try:
            outcome_prices = [float(p) for p in ast.literal_eval(outcome_prices_raw)]
            if len(outcome_prices) == 2:
                anchored_to_first = abs(recommendation.price - outcome_prices[0]) <= abs(
                    recommendation.price - outcome_prices[1]
                )
                if not anchored_to_first:
                    logger.warning(
                        "execute_market_order: price=%s appears anchored to "
                        "outcomes[1] (prices=%s) — LLM may have meant the opposite "
                        "side. Proceeding with side=%s.",
                        recommendation.price, outcome_prices, side,
                    )
        except Exception:
            pass

        amount_usdc = recommendation.amount_usdc
        if amount_usdc is None or amount_usdc <= 0:
            raise ValueError(
                f"recommendation.amount_usdc must be set and > 0, got {amount_usdc}"
            )

        live_price, fillable_usdc, avg_price = self._fillable_market_buy(
            token_id, amount_usdc
        )
        if live_price > order_price + MAX_MARKET_ORDER_SLIPPAGE:
            raise ValueError(
                f"live ask price {live_price:.4f} exceeds recommended price "
                f"{order_price:.4f} by more than max slippage "
                f"{MAX_MARKET_ORDER_SLIPPAGE:.4f}; avg_price={avg_price:.4f}"
            )
        if fillable_usdc < amount_usdc:
            logger.info(
                "execute_market_order: reducing amount from %.4f to %.4f due to "
                "available ask liquidity",
                amount_usdc,
                fillable_usdc,
            )
            amount_usdc = fillable_usdc

        # CLOB market orders are BUYs of a specific token (SELL semantics are
        # already encoded above by selecting the other token). py_clob_client
        # 0.34+ requires `side` explicitly.
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usdc,
            price=live_price,
            side=BUY,
        )

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(min=2, max=20),
            retry=retry_if_exception_type(
                (httpx.TimeoutException, httpx.NetworkError, requests.Timeout, requests.ConnectionError)
            ),
            reraise=True,
        )
        def _post():
            return self.client.create_and_post_market_order(
                order_args,
                order_type=order_type,
            )

        resp = _post()

        order_id = None
        status = "unknown"
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("order_id")
            status = resp.get("status", "unknown")

        return {
            "token_id": token_id,
            "outcome_traded": outcomes[token_ids.index(token_id)],
            "amount_usdc": amount_usdc,
            "price_recommended": recommendation.price,
            "order_price": live_price,
            "order_price_model": order_price,
            "order_avg_price_estimate": avg_price,
            "side_recommended": side,
            "order_id": order_id,
            "status": status,
            "raw": resp,
        }

    def get_usdc_balance(self) -> float:
        # In POLY_PROXY mode the EOA holds nothing; the proxy holds pUSD.
        if self.funder:
            holder = self.funder
        elif self.private_key:
            holder = self.get_address_for_private_key()
        else:
            raise RuntimeError(
                "Need POLYMARKET_FUNDER (proxy mode) or POLYGON_WALLET_PRIVATE_KEY (EOA mode) to read balance."
            )

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(min=1, max=10),
            reraise=True,
        )
        def _read():
            return self.usdc.functions.balanceOf(holder).call()

        # Both USDC.e and pUSD are 6 decimals.
        return float(_read() / 1e6)


def test():
    host = "https://clob.polymarket.com"
    key = os.getenv("POLYGON_WALLET_PRIVATE_KEY")
    print(key)
    chain_id = POLYGON

    # Create CLOB client and get/set API credentials
    client = ClobClient(host, key=key, chain_id=chain_id)
    client.set_api_creds(client.create_or_derive_api_creds())

    creds = ApiCreds(
        api_key=os.getenv("CLOB_API_KEY"),
        api_secret=os.getenv("CLOB_SECRET"),
        api_passphrase=os.getenv("CLOB_PASS_PHRASE"),
    )
    chain_id = AMOY
    client = ClobClient(host, key=key, chain_id=chain_id, creds=creds)

    print(client.get_markets())
    print(client.get_simplified_markets())
    print(client.get_sampling_markets())
    print(client.get_sampling_simplified_markets())
    print(client.get_market("condition_id"))

    print("Done!")


def gamma():
    url = "https://gamma-com"
    markets_url = url + "/markets"
    res = httpx.get(markets_url)
    code = res.status_code
    if code == 200:
        markets: list[SimpleMarket] = []
        data = res.json()
        for market in data:
            try:
                market_data = {
                    "id": int(market["id"]),
                    "question": market["question"],
                    # "start": market['startDate'],
                    "end": market["endDate"],
                    "description": market["description"],
                    "active": market["active"],
                    "deployed": market["deployed"],
                    "funded": market["funded"],
                    # "orderMinSize": float(market['orderMinSize']) if market['orderMinSize'] else 0,
                    # "orderPriceMinTickSize": float(market['orderPriceMinTickSize']),
                    "rewardsMinSize": float(market["rewardsMinSize"]),
                    "rewardsMaxSpread": float(market["rewardsMaxSpread"]),
                    "volume": float(market["volume"]),
                    "spread": float(market["spread"]),
                    "outcome_a": str(market["outcomes"][0]),
                    "outcome_b": str(market["outcomes"][1]),
                    "outcome_a_price": str(market["outcomePrices"][0]),
                    "outcome_b_price": str(market["outcomePrices"][1]),
                }
                markets.append(SimpleMarket(**market_data))
            except Exception as err:
                print(f"error {err} for market {id}")
        pdb.set_trace()
    else:
        raise Exception()


def main():
    # auth()
    # test()
    # gamma()
    print(Polymarket().get_all_events())


if __name__ == "__main__":
    load_dotenv()

    p = Polymarket()

    # k = p.get_api_key()
    # m = p.get_sampling_simplified_markets()

    # print(m)
    # m = p.get_market('11015470973684177829729219287262166995141465048508201953575582100565462316088')

    # t = m[0]['token_id']
    # o = p.get_orderbook(t)
    # pdb.set_trace()

    """
    
    (Pdb) pprint(o)
            OrderBookSummary(
                market='0x26ee82bee2493a302d21283cb578f7e2fff2dd15743854f53034d12420863b55', 
                asset_id='11015470973684177829729219287262166995141465048508201953575582100565462316088', 
                bids=[OrderSummary(price='0.01', size='600005'), OrderSummary(price='0.02', size='200000'), ...
                asks=[OrderSummary(price='0.99', size='100000'), OrderSummary(price='0.98', size='200000'), ...
            )
    
    """

    # https://polygon-rpc.com

    test_market_token_id = (
        "101669189743438912873361127612589311253202068943959811456820079057046819967115"
    )
    test_market_data = p.get_market(test_market_token_id)

    # test_size = 0.0001
    test_size = 1
    test_side = BUY
    test_price = float(ast.literal_eval(test_market_data["outcome_prices"])[0])

    # order = p.execute_order(
    #    test_price,
    #    test_size,
    #    test_side,
    #    test_market_token_id,
    # )

    # order = p.execute_market_order(test_price, test_market_token_id)

    balance = p.get_usdc_balance()
