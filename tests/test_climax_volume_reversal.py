from __future__ import annotations

import unittest

from agents.application.alpaca_market_data import AlpacaMarketDataClient
from agents.application.climax_volume_reversal import detect_climax_volume_reversal
from agents.application.crypto_exchange_tape import CryptoExchangeTapeClient
from agents.application.openbb_market_data import OpenBBMarketDataClient


def _bearish_reversal_bars() -> list[dict]:
    bars = [
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.3, "volume": 100.0}
        for _ in range(20)
    ]
    bars.append({"open": 100.0, "high": 106.0, "low": 99.5, "close": 105.0, "volume": 360.0})
    bars.append({"open": 105.0, "high": 105.2, "low": 101.0, "close": 101.5, "volume": 130.0})
    return bars


def _bullish_reversal_bars() -> list[dict]:
    bars = [
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 99.8, "volume": 100.0}
        for _ in range(20)
    ]
    bars.append({"open": 100.0, "high": 100.5, "low": 94.0, "close": 95.0, "volume": 360.0})
    bars.append({"open": 95.0, "high": 99.0, "low": 94.8, "close": 98.5, "volume": 130.0})
    return bars


class ClimaxVolumeReversalTests(unittest.TestCase):
    def test_detects_bearish_fade_after_bullish_volume_climax(self):
        signal = detect_climax_volume_reversal(_bearish_reversal_bars())

        self.assertEqual(signal.direction, "bearish")
        self.assertGreaterEqual(signal.confidence, 0.58)
        self.assertGreaterEqual(signal.features["climax_volume_ratio"], 3.0)
        self.assertTrue(signal.features["climax_reversal_confirmed"])

    def test_detects_bullish_fade_after_bearish_volume_climax(self):
        signal = detect_climax_volume_reversal(_bullish_reversal_bars())

        self.assertEqual(signal.direction, "bullish")
        self.assertGreaterEqual(signal.probability, 0.58)

    def test_requires_confirmation_candle(self):
        bars = _bearish_reversal_bars()
        bars[-1] = {"open": 105.0, "high": 106.0, "low": 104.0, "close": 105.5, "volume": 130.0}

        signal = detect_climax_volume_reversal(bars)

        self.assertIsNone(signal.direction)
        self.assertIn("confirmation", signal.reason)

    def test_openbb_signal_exposes_climax_features(self):
        signal = OpenBBMarketDataClient().signal_from_bars(
            "NVDA", "stock", _bearish_reversal_bars()
        )

        self.assertEqual(signal.direction, "bearish")
        self.assertEqual(signal.features["climax_volume_reversal_direction"], "bearish")

    def test_alpaca_signal_exposes_climax_features(self):
        bars = [
            {"o": b["open"], "h": b["high"], "l": b["low"], "c": b["close"], "v": b["volume"]}
            for b in _bullish_reversal_bars()
        ]

        signal = AlpacaMarketDataClient().signal_from_bars("BTC/USD", "crypto", bars)

        self.assertEqual(signal.direction, "bullish")
        self.assertEqual(signal.features["climax_volume_reversal_direction"], "bullish")

    def test_crypto_tape_signal_exposes_climax_features(self):
        bars = [
            [0, b["open"], b["high"], b["low"], b["close"], b["volume"]]
            for b in _bearish_reversal_bars()
        ]

        signal = CryptoExchangeTapeClient().signal_from_data(
            "BTC",
            "BTCUSDT",
            bars,
            {"bidPrice": "101.40", "askPrice": "101.60"},
            None,
        )

        self.assertEqual(signal.direction, "bearish")
        self.assertEqual(signal.features["climax_volume_reversal_direction"], "bearish")


if __name__ == "__main__":
    unittest.main()
