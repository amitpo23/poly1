from __future__ import annotations

import unittest

from agents.application.arb_quality import (
    ArbQualityConfig,
    BinaryVenueQuote,
    evaluate_binary_cross_venue_arb,
)


class ArbQualityTests(unittest.TestCase):
    def test_accepts_cross_venue_binary_arb_after_costs(self):
        poly = BinaryVenueQuote("polymarket", yes_ask=0.42, no_ask=0.60, yes_depth_usdc=50, no_depth_usdc=50, rules_key="btc_5m")
        kalshi = BinaryVenueQuote("kalshi", yes_ask=0.48, no_ask=0.52, yes_depth_usdc=50, no_depth_usdc=50, rules_key="btc_5m")

        result = evaluate_binary_cross_venue_arb(poly, kalshi, cfg=ArbQualityConfig(round_trip_cost=0.02, min_net_profit=0.015))

        self.assertTrue(result.candidate)
        self.assertEqual(result.reason, "arb_candidate")
        self.assertEqual(result.yes_venue, "polymarket")
        self.assertEqual(result.no_venue, "kalshi")
        self.assertAlmostEqual(result.net_profit, 0.04)

    def test_blocks_rule_mismatch_even_when_prices_look_good(self):
        poly = BinaryVenueQuote("polymarket", yes_ask=0.42, no_ask=0.60, yes_depth_usdc=50, no_depth_usdc=50, rules_key="btc_et")
        other = BinaryVenueQuote("kalshi", yes_ask=0.48, no_ask=0.52, yes_depth_usdc=50, no_depth_usdc=50, rules_key="btc_utc")

        result = evaluate_binary_cross_venue_arb(poly, other)

        self.assertFalse(result.candidate)
        self.assertIn("rule_mismatch", result.blockers)

    def test_blocks_negative_net_profit(self):
        poly = BinaryVenueQuote("polymarket", yes_ask=0.50, no_ask=0.51, yes_depth_usdc=50, no_depth_usdc=50, rules_key="x")
        other = BinaryVenueQuote("kalshi", yes_ask=0.51, no_ask=0.50, yes_depth_usdc=50, no_depth_usdc=50, rules_key="x")

        result = evaluate_binary_cross_venue_arb(poly, other)

        self.assertFalse(result.candidate)
        self.assertIn("net_profit_below_min", result.blockers)

    def test_blocks_thin_depth(self):
        poly = BinaryVenueQuote("polymarket", yes_ask=0.42, no_ask=0.60, yes_depth_usdc=2, no_depth_usdc=2, rules_key="x")
        other = BinaryVenueQuote("kalshi", yes_ask=0.48, no_ask=0.52, yes_depth_usdc=2, no_depth_usdc=2, rules_key="x")

        result = evaluate_binary_cross_venue_arb(poly, other, cfg=ArbQualityConfig(min_depth_usdc=10))

        self.assertFalse(result.candidate)
        self.assertIn("insufficient_depth", result.blockers)


if __name__ == "__main__":
    unittest.main()
