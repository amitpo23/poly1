from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agents.application.strategy_catalog import (
    STRATEGY_CATALOG,
    WindowResult,
    catalog_summary,
    evaluate_gate,
)
from scripts.pre_live_strategy_matrix import build_matrix


class StrategyCatalogTests(unittest.TestCase):
    def test_catalog_covers_core_algo_families(self):
        summary = catalog_summary()
        families = set(summary["families"])

        self.assertGreaterEqual(len(STRATEGY_CATALOG), 10)
        self.assertIn("trend_following", families)
        self.assertIn("mean_reversion", families)
        self.assertIn("market_making", families)
        self.assertIn("statistical_arbitrage", families)
        self.assertIn("volatility_relative_value", families)
        self.assertIn("news_sentiment_event_driven", families)
        self.assertIn("machine_learning", families)

    def test_gate_requires_all_windows_to_pass(self):
        verdict = evaluate_gate(
            "sports_cheap_hold",
            [
                WindowResult("30d", 55, 0.60, 12.0),
                WindowResult("60d", 55, 0.60, 12.0),
                WindowResult("90d", 20, 0.70, 30.0),
            ],
        )

        self.assertEqual(verdict.state, "shadow_or_research_only")
        self.assertIn("90d:insufficient_samples", verdict.blockers)
        self.assertIn("split_window_gate_failed", verdict.blockers)

    def test_matrix_reads_market_sweep_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for days, n in [(30, 60), (60, 61), (90, 62)]:
                (root / f"market_sweep_{days}d.json").write_text(
                    """
                    {
                      "markets_total": 150,
                      "matrix": {
                        "sports": {
                          "cheap_hold_0.40": {
                            "n": %d,
                            "wins": 40,
                            "losses": 20,
                            "paper_pnl_per_dollar": 0.20,
                            "win_rate": 0.60
                          }
                        }
                      }
                    }
                    """
                    % n
                )

            payload = build_matrix(str(root))
            row = next(item for item in payload["rows"] if item["strategy_id"] == "sports_cheap_hold")

            self.assertEqual(len(row["windows"]), 3)
            self.assertEqual(row["windows"][0]["pnl_per_100"], 20.0)
            self.assertIn("needs_lookahead_bias_audit", row["verdict"]["blockers"])


if __name__ == "__main__":
    unittest.main()
