import unittest
import tempfile
import sqlite3
from pathlib import Path

from agents.application.opportunity_router import (
    OpportunityRouter,
    RouterConfig,
    live_route_allowed,
)


class TestOpportunityRouter(unittest.TestCase):
    def setUp(self):
        self.router = OpportunityRouter(RouterConfig())

    def test_live_probe_requires_committee_live_approval_and_low_risk(self):
        route = self.router.evaluate_row({
            "market_slug": "m",
            "strategy_match": "news_shock",
            "final_score": 0.82,
            "risk_score": 0.20,
            "approved_for_live": 1,
            "approved_for_backtest": 1,
            "features_json": (
                '{"estimated_true_probability": 0.68, "entry_price": 0.60, '
                '"historical_edge": 0.04, "error_margin": 0.02, "slippage": 0.01}'
            ),
            "liquidity": 100_000,
            "spread_cents": 2.0,
        })
        self.assertEqual(route.route, "live_probe")
        self.assertAlmostEqual(route.expected_value, 0.05)

    def test_committee_backtest_row_stays_backtest_not_live(self):
        route = self.router.evaluate_row({
            "market_slug": "btc-daily",
            "strategy_match": "mean_reversion",
            "final_score": 0.86,
            "risk_score": 0.64,
            "approved_for_live": 0,
            "approved_for_backtest": 1,
        })
        self.assertEqual(route.route, "backtest")
        self.assertIn("risk_above_live_limit:0.640", route.reasons)

    def test_negative_ev_blocks_live_probe(self):
        route = self.router.evaluate_row({
            "market_slug": "m",
            "strategy_match": "news_shock",
            "final_score": 0.90,
            "risk_score": 0.10,
            "approved_for_live": 1,
            "approved_for_backtest": 1,
            "features_json": (
                '{"estimated_true_probability": 0.55, "entry_price": 0.57, '
                '"historical_edge": 0.04}'
            ),
        })
        self.assertEqual(route.route, "backtest")
        self.assertIn("non_positive_ev:-0.060", route.reasons)

    def test_model_estimated_probability_cannot_go_live(self):
        route = self.router.evaluate_row({
            "market_slug": "m",
            "strategy_match": "market_maker",
            "final_score": 0.95,
            "risk_score": 0.10,
            "approved_for_live": 1,
            "approved_for_backtest": 1,
            "yes_price": 0.50,
            "liquidity": 100_000,
            "spread_cents": 2.0,
            "confidence": 1.0,
            "features_json": '{"historical_edge": 0.08}',
        })
        self.assertNotEqual(route.route, "live_probe")
        self.assertTrue(
            any(r.startswith("probability_is_model_estimate") for r in route.reasons)
        )

    def test_live_route_allowed_requires_fresh_live_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "scout.db")
            self.assertFalse(
                live_route_allowed(db_path=db_path, market_slug="m", strategy="trader").allowed
            )
            with sqlite3.connect(db_path) as conn:
                conn.executescript(OpportunityRouter.ROUTE_SCHEMA)
                conn.execute(
                    """
                    INSERT INTO opportunity_routes
                        (created_ts, market_slug, market_id, strategy_match,
                         route, score, risk_score, expected_value, slippage,
                         error_margin, catalyst_score, reasons_json)
                    VALUES (datetime('now'), 'm', '1', 'trader', 'live_probe',
                            0.9, 0.1, 0.05, 0.01, 0.02, 0.0, '[]')
                    """
                )
            self.assertTrue(
                live_route_allowed(db_path=db_path, market_slug="m", strategy="trader").allowed
            )


if __name__ == "__main__":
    unittest.main()
