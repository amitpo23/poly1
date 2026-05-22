import os
import unittest
from unittest.mock import patch

from agents.application.quant_price_fair_value import QuantPriceFairValueReader


class TestQuantPriceFairValueReader(unittest.TestCase):
    def test_above_threshold_returns_yes_probability(self):
        reader = QuantPriceFairValueReader()
        sig = reader.query(
            question="Will Bitcoin be above $100,000 today?",
            hours_to_close=1.0,
            market_price=0.40,
            tape_features={
                "last_price": 101000.0,
                "realized_volatility_annual": 0.55,
            },
        )
        self.assertEqual(sig.asset, "BTC")
        self.assertEqual(sig.direction, "yes")
        self.assertGreater(sig.probability, 0.40)
        self.assertGreater(sig.confidence, 0.55)

    def test_below_threshold_understands_yes_means_below(self):
        reader = QuantPriceFairValueReader()
        sig = reader.query(
            question="Will BTC be below $100,000 in one hour?",
            hours_to_close=1.0,
            market_price=0.40,
            tape_features={
                "last_price": 99000.0,
                "realized_volatility_annual": 0.55,
            },
        )
        self.assertEqual(sig.direction, "yes")
        self.assertGreater(sig.probability, 0.40)

    def test_low_dollar_price_targets_are_supported(self):
        reader = QuantPriceFairValueReader()
        sig = reader.query(
            question="Will XRP be above $2.50 today?",
            hours_to_close=2.0,
            market_price=0.35,
            tape_features={
                "last_price": 2.70,
                "realized_volatility_annual": 0.80,
            },
        )
        self.assertEqual(sig.asset, "XRP")
        self.assertEqual(sig.target_price, 2.5)
        self.assertEqual(sig.direction, "yes")

    def test_non_price_question_is_neutral(self):
        reader = QuantPriceFairValueReader()
        sig = reader.query(
            question="Will the Fed cut rates?",
            hours_to_close=24.0,
            market_price=0.50,
            tape_features={"last_price": 100000.0},
        )
        self.assertIsNone(sig.direction)
        self.assertEqual(sig.probability, 0.5)

    @patch.dict(os.environ, {"QUANT_FV_VOL_PRIOR_WEIGHT": "1.0"})
    def test_prior_vol_can_dominate_realized_vol(self):
        reader = QuantPriceFairValueReader()
        vol = reader._posterior_annual_vol(
            "BTC",
            {"realized_volatility_annual": 2.0},
        )
        self.assertAlmostEqual(vol, float(os.getenv("QUANT_FV_DEFAULT_ANNUAL_VOL", 0.60)))


if __name__ == "__main__":
    unittest.main()
