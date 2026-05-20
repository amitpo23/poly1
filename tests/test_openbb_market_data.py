from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from agents.application.openbb_market_data import OpenBBMarketDataClient


class OpenBBMarketDataTests(unittest.TestCase):
    def test_infer_symbol_for_equity_and_macro_proxy(self):
        client = OpenBBMarketDataClient()

        self.assertEqual(client.infer_symbol("Will Nvidia close higher?"), ("NVDA", "stock"))
        self.assertEqual(client.infer_symbol("Will the S&P 500 go up?"), ("SPY", "etf"))
        self.assertEqual(client.infer_symbol("Will crude oil rise?"), ("USO", "commodity_etf"))
        self.assertEqual(client.infer_symbol("Will the Vegas Golden Knights win?"), (None, None))

    def test_signal_from_bars_detects_bullish_trend(self):
        client = OpenBBMarketDataClient()
        bars = [
            {"close": 100 + i, "high": 101 + i, "low": 99 + i, "volume": 1000 + i * 20}
            for i in range(30)
        ]

        signal = client.signal_from_bars("NVDA", "stock", bars)

        self.assertEqual(signal.direction, "bullish")
        self.assertGreater(signal.confidence, 0.60)
        self.assertEqual(signal.symbol, "NVDA")

    def test_signal_from_bars_insufficient_data_skips(self):
        client = OpenBBMarketDataClient()

        signal = client.signal_from_bars("NVDA", "stock", [{"close": 100}])

        self.assertIsNone(signal.direction)
        self.assertEqual(signal.confidence, 0.0)
        self.assertIn("insufficient", signal.reason)

    @patch("agents.application.openbb_market_data.requests.get")
    def test_fetch_bars_falls_back_to_yahoo_chart(self, mock_get):
        response = Mock()
        response.json.return_value = {
            "chart": {
                "result": [
                    {
                        "timestamp": list(range(12)),
                        "indicators": {
                            "quote": [
                                {
                                    "close": [100 + i for i in range(12)],
                                    "high": [101 + i for i in range(12)],
                                    "low": [99 + i for i in range(12)],
                                    "volume": [1000 + i for i in range(12)],
                                }
                            ]
                        },
                    }
                ]
            }
        }
        response.raise_for_status.return_value = None
        mock_get.return_value = response
        client = OpenBBMarketDataClient(cache_ttl_sec=0)

        bars = client.fetch_bars("NVDA")

        self.assertEqual(len(bars), 12)
        self.assertEqual(bars[-1]["close"], 111.0)
        self.assertEqual(bars[-1]["_dependency"], "yahoo_chart")


if __name__ == "__main__":
    unittest.main()
