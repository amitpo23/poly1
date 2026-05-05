import unittest
from unittest.mock import MagicMock, patch

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


if __name__ == "__main__":
    unittest.main()
