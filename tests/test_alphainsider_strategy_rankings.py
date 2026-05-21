from __future__ import annotations

import unittest

from agents.application.alphainsider_strategy_rankings import (
    AlphaInsiderStrategy,
    summarize_rankings,
)


class AlphaInsiderStrategyRankingTests(unittest.TestCase):
    def test_strategy_parses_timeframes_and_family(self):
        strategy = AlphaInsiderStrategy.from_api(
            {
                "strategy_id": "s1",
                "name": "VWAP Mean Reversion Strategy",
                "type": "stock",
                "user_id": "u1",
                "subscriber_count": "12",
                "timeframes": [
                    {
                        "timeframe": "month",
                        "rank_performance": "2",
                        "rank_top": "3",
                        "max_drawdown": "0.05",
                        "past_value": "1.42",
                    }
                ],
            }
        )

        self.assertEqual(strategy.family(), "vwap_mean_reversion")
        row = strategy.as_dict("month")
        self.assertAlmostEqual(row["return_pct"], 0.42)
        self.assertEqual(row["rank_performance"], 2)
        self.assertGreater(row["quality_score"], 0.0)

    def test_summarize_groups_by_family(self):
        strategies = [
            AlphaInsiderStrategy.from_api(
                {
                    "strategy_id": "a",
                    "name": "MACD Trend",
                    "type": "cryptocurrency",
                    "timeframes": [{"timeframe": "year", "past_value": "2.0", "max_drawdown": "0.1"}],
                }
            ),
            AlphaInsiderStrategy.from_api(
                {
                    "strategy_id": "b",
                    "name": "Supply and Demand",
                    "type": "stock",
                    "timeframes": [{"timeframe": "year", "past_value": "1.4", "max_drawdown": "0.05"}],
                }
            ),
        ]

        summary = summarize_rankings(strategies, "year")

        self.assertEqual(summary["count"], 2)
        self.assertIn("trend_momentum", summary["by_family"])
        self.assertIn("supply_demand", summary["by_family"])


if __name__ == "__main__":
    unittest.main()
