import unittest

from agents.application.trade_recommendation import parse_trade_recommendation


class TestTradeRecommendationParsing(unittest.TestCase):
    def test_parse_json_trade_recommendation(self):
        recommendation = parse_trade_recommendation(
            '{"price": 0.42, "size_fraction": 0.08, "side": "BUY", "confidence": 0.7}'
        )

        self.assertEqual(recommendation.price, 0.42)
        self.assertEqual(recommendation.size_fraction, 0.08)
        self.assertEqual(recommendation.side, "BUY")
        self.assertEqual(recommendation.confidence, 0.7)

    def test_parse_legacy_field_trade_recommendation(self):
        recommendation = parse_trade_recommendation(
            """
            price:0.55,
            size:0.1,
            side:SELL,
            """
        )

        self.assertEqual(recommendation.price, 0.55)
        self.assertEqual(recommendation.size_fraction, 0.1)
        self.assertEqual(recommendation.side, "SELL")

    def test_rejects_out_of_range_size(self):
        with self.assertRaises(ValueError):
            parse_trade_recommendation(
                '{"price": 0.42, "size_fraction": 1.5, "side": "BUY"}'
            )

    def test_rejects_unknown_side(self):
        with self.assertRaises(ValueError):
            parse_trade_recommendation(
                '{"price": 0.42, "size_fraction": 0.1, "side": "HOLD"}'
            )


if __name__ == "__main__":
    unittest.main()
