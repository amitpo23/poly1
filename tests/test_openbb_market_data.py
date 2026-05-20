from __future__ import annotations

import unittest

from agents.application.openbb_market_data import OpenBBMarketDataClient


class OpenBBMarketDataTests(unittest.TestCase):
    def test_infer_symbol_for_equity_and_macro_proxy(self):
        client = OpenBBMarketDataClient()

        self.assertEqual(client.infer_symbol("Will Nvidia close higher?"), ("NVDA", "stock"))
        self.assertEqual(client.infer_symbol("Will the S&P 500 go up?"), ("SPY", "etf"))
        self.assertEqual(client.infer_symbol("Will crude oil rise?"), ("USO", "commodity_etf"))

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


if __name__ == "__main__":
    unittest.main()
