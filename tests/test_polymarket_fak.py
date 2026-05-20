import functools
import sys
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy optional deps so tests run without the full Docker pip install.
# ---------------------------------------------------------------------------
def _ensure_stub(name):
    if name not in sys.modules:
        sys.modules[name] = MagicMock()

for _mod in [
    "web3", "web3.constants", "web3.middleware",
    "httpx",
    "py_clob_client_v2", "py_clob_client_v2.client", "py_clob_client_v2.clob_types",
    "py_clob_client_v2.constants", "py_clob_client_v2.exceptions",
    "py_clob_client_v2.order_builder", "py_clob_client_v2.order_builder.constants",
    "py_order_utils", "py_order_utils.builders", "py_order_utils.model",
    "py_order_utils.signer",
    "tenacity",
    "langchain_core", "langchain_core.messages", "langchain_core.documents",
    "langchain_openai",
    "langchain_community", "langchain_community.document_loaders",
    "langchain_community.vectorstores", "langchain_community.vectorstores.chroma",
    "chromadb",
]:
    _ensure_stub(_mod)

# tenacity.retry must be an identity decorator factory, otherwise the
# @retry(...) inside execute_market_order wraps _post with a MagicMock and
# create_and_post_market_order is never called.
def _identity_retry(*args, **kwargs):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*a, **kw):
            return func(*a, **kw)
        return wrapper
    return decorator

sys.modules["tenacity"].retry = _identity_retry

# langchain_core.documents.Document must be a real class because execute_market_order
# calls market[0].dict()["metadata"] on it.
class _FakeDocument:
    def __init__(self, page_content="", metadata=None, **kwargs):
        self.page_content = page_content
        self.metadata = metadata or {}
    def dict(self):
        return {"page_content": self.page_content, "metadata": self.metadata}

sys.modules["langchain_core.documents"].Document = _FakeDocument

# py_clob_client_v2.clob_types.MarketOrderArgsV2 is imported AS MarketOrderArgs in
# polymarket.py. execute_market_order builds an instance and passes it positionally to
# create_and_post_market_order. Tests in test_trader inspect .price/.amount/.token_id;
# we need the stub in place HERE so the name is bound correctly when polymarket.py is
# first imported (before test_trader.py even loads).
class _FakeMarketOrderArgsV2:
    def __init__(self, token_id=None, amount=None, price=None, side=None, **kwargs):
        self.token_id = token_id
        self.amount = amount
        self.price = price
        self.side = side

sys.modules["py_clob_client_v2.clob_types"].MarketOrderArgsV2 = _FakeMarketOrderArgsV2
# ---------------------------------------------------------------------------

from agents.polymarket.polymarket import Polymarket
from agents.utils.objects import TradeRecommendation


class TestExecuteMarketOrderFAK(unittest.TestCase):
    def _make_market(self):
        from langchain_core.documents import Document
        doc = Document(
            page_content="...",
            metadata={
                "outcomes": "['Up', 'Down']",
                "clob_token_ids": "['tok_up', 'tok_dn']",
                "outcome_prices": "['0.5', '0.5']",
                "id": "btc-updown-15m-100",
            },
        )
        return (doc, 0.0)

    @patch.object(Polymarket, "__init__", lambda self, **kw: None)
    def test_execute_market_order_passes_fak_when_requested(self):
        from py_clob_client_v2.clob_types import OrderType
        p = Polymarket()
        p.client = MagicMock()
        p._fillable_market_buy = MagicMock(return_value=(0.51, 5.0, 0.50))
        p.client.create_and_post_market_order = MagicMock(
            return_value={"status": "filled", "orderID": "abc"}
        )
        rec = TradeRecommendation(price=0.50, size_fraction=0.05, side="BUY",
                                   confidence=0.7, amount_usdc=5.0)
        p.execute_market_order(self._make_market(), rec, order_type=OrderType.FAK)
        call_kwargs = p.client.create_and_post_market_order.call_args.kwargs
        self.assertEqual(call_kwargs.get("order_type"), OrderType.FAK)

    @patch.object(Polymarket, "__init__", lambda self, **kw: None)
    def test_execute_market_order_defaults_to_fok(self):
        from py_clob_client_v2.clob_types import OrderType
        p = Polymarket()
        p.client = MagicMock()
        p._fillable_market_buy = MagicMock(return_value=(0.51, 5.0, 0.50))
        p.client.create_and_post_market_order = MagicMock(
            return_value={"status": "filled", "orderID": "abc"}
        )
        rec = TradeRecommendation(price=0.50, size_fraction=0.05, side="BUY",
                                   confidence=0.7, amount_usdc=5.0)
        p.execute_market_order(self._make_market(), rec)
        call_kwargs = p.client.create_and_post_market_order.call_args.kwargs
        self.assertEqual(call_kwargs.get("order_type"), OrderType.FOK)

    @patch.object(Polymarket, "__init__", lambda self, **kw: None)
    def test_execute_market_order_normalizes_object_response(self):
        class Resp:
            taker_order_id = "obj-order"
            status = "matched"

        p = Polymarket()
        p.client = MagicMock()
        p._fillable_market_buy = MagicMock(return_value=(0.51, 5.0, 0.50))
        p.client.create_and_post_market_order = MagicMock(return_value=Resp())
        rec = TradeRecommendation(price=0.50, size_fraction=0.05, side="BUY",
                                   confidence=0.7, amount_usdc=5.0)

        result = p.execute_market_order(self._make_market(), rec)

        self.assertEqual(result["order_id"], "obj-order")
        self.assertEqual(result["status"], "matched")


if __name__ == "__main__":
    unittest.main()
