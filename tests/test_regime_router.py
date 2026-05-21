from __future__ import annotations

import unittest

from agents.application.market_microstructure import MicrostructureSnapshot
from agents.application.regime_router import (
    family_from_signal,
    normalize_family,
    route_for_features,
    route_for_regime,
    route_for_snapshot,
)


class RegimeRouterTests(unittest.TestCase):
    def test_trending_regime_prefers_momentum_and_blocks_confident_fade(self):
        route = route_for_regime("trending", confidence=0.72)

        trend = route.verdict_for_family("trend_following")
        fade = route.verdict_for_family("mean_reversion")

        self.assertTrue(trend.allowed)
        self.assertTrue(trend.preferred)
        self.assertFalse(fade.allowed)
        self.assertEqual(fade.risk_multiplier, 0.0)

    def test_mean_reverting_regime_prefers_spread_and_fade(self):
        route = route_for_regime("mean_reverting", confidence=0.70)

        self.assertTrue(route.verdict_for_family("market_microstructure").preferred)
        self.assertTrue(route.verdict_for_family("mean_reversion").preferred)
        self.assertFalse(route.verdict_for_family("trend_following").allowed)

    def test_unknown_regime_is_conservative_but_not_a_hard_block(self):
        route = route_for_features({})
        verdict = route.verdict_for_family("trend_following")

        self.assertEqual(route.regime, "unknown")
        self.assertTrue(verdict.allowed)
        self.assertLess(verdict.risk_multiplier, 1.0)
        self.assertGreater(verdict.edge_multiplier, 1.0)

    def test_snapshot_routes_from_microstructure_features(self):
        snapshot = MicrostructureSnapshot(
            symbol="BTC/USD",
            asset_class="crypto",
            bar_count=40,
            last_close=100.0,
            vwap=99.0,
            vwap_deviation_pct=0.01,
            mean_reversion_zscore=0.8,
            return_autocorr=-0.3,
            volatility_pct=0.005,
            regime="mean_reverting",
            regime_confidence=0.75,
        )

        route = route_for_snapshot(snapshot)

        self.assertEqual(route.regime, "mean_reverting")
        self.assertTrue(route.verdict_for_family("market_making").preferred)

    def test_family_mapping_understands_catalog_and_external_names(self):
        self.assertEqual(family_from_signal(strategy_id="crypto_5m_directional"), "trend_following")
        self.assertEqual(normalize_family("trend_momentum"), "trend_following")
        self.assertEqual(
            family_from_signal(
                signal_source="opportunity_factory,alphainsider_proven,crypto_tape",
                features={"alphainsider_family": "vwap_mean_reversion"},
            ),
            "mean_reversion",
        )


if __name__ == "__main__":
    unittest.main()
