from __future__ import annotations

import unittest

from agents.application.market_microstructure import (
    classify_regime,
    compute_vwap,
    feature_snapshot_from_bars,
    mean_reversion_zscore,
    return_autocorrelation,
)


class MarketMicrostructureTests(unittest.TestCase):
    def test_compute_vwap_uses_volume_weighted_typical_price(self):
        bars = [
            {"high": 11, "low": 9, "close": 10, "volume": 10},
            {"high": 22, "low": 18, "close": 20, "volume": 30},
        ]

        self.assertAlmostEqual(compute_vwap(bars), 17.5)

    def test_snapshot_exposes_vwap_zscore_and_regime_features(self):
        bars = [
            {"close": 100 + ((-1) ** i) * 2, "high": 103, "low": 97, "volume": 1000}
            for i in range(40)
        ]

        snapshot = feature_snapshot_from_bars("BTC-USD", "crypto", bars)

        self.assertEqual(snapshot.symbol, "BTC-USD")
        self.assertIsNotNone(snapshot.vwap)
        self.assertIsNotNone(snapshot.mean_reversion_zscore)
        self.assertIn(snapshot.regime, {"mean_reverting", "mixed", "stretched", "trending", "unknown"})
        self.assertIn("micro_vwap_deviation_pct", snapshot.features)

    def test_return_autocorrelation_detects_mean_reverting_sequence(self):
        closes = [100, 102, 100, 102, 100, 102, 100, 102, 100, 102]

        autocorr = return_autocorrelation(closes)
        regime, confidence = classify_regime(closes, autocorr=autocorr)

        self.assertLess(autocorr, -0.5)
        self.assertEqual(regime, "mean_reverting")
        self.assertGreater(confidence, 0.6)

    def test_zscore_is_none_until_window_exists(self):
        self.assertIsNone(mean_reversion_zscore([100, 101, 102], window=10))


if __name__ == "__main__":
    unittest.main()
