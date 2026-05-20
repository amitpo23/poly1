from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agents.application.equity_options_fair_value import (
    EquityOptionsFairValueAgent,
    evaluate_largest_company_market,
    largest_market_cap_probabilities,
)
from agents.application.trade_log import TradeLog


class EquityOptionsFairValueTests(unittest.TestCase):
    def test_largest_market_cap_probability_prefers_clear_leader(self):
        probs = largest_market_cap_probabilities(
            prices={"NVDA": 300.0, "MSFT": 200.0, "AAPL": 150.0},
            shares_outstanding={"NVDA": 10.0, "MSFT": 8.0, "AAPL": 7.0},
            annual_vols={"NVDA": 0.10, "MSFT": 0.10, "AAPL": 0.10},
            days_to_expiry=7,
            simulations=5_000,
            seed=1,
        )

        self.assertGreater(probs["NVDA"], 0.95)
        self.assertLess(probs["AAPL"], 0.05)

    def test_evaluates_largest_company_market_edge(self):
        market = {
            "id": "m1",
            "question": "Largest company at end of June?",
            "outcomes": '["Nvidia", "Microsoft", "Apple"]',
            "outcomePrices": '["0.40", "0.50", "0.10"]',
            "endDate": "2026-06-30T00:00:00Z",
        }

        result = evaluate_largest_company_market(
            market=market,
            prices={"NVDA": 300.0, "MSFT": 200.0, "AAPL": 150.0},
            now=datetime(2026, 6, 1, tzinfo=timezone.utc),
            simulations=5_000,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.selected_ticker, "NVDA")
        self.assertGreater(result.edge, 0.0)

    def test_agent_records_shadow_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = TradeLog(db_path=str(Path(tmp) / "trade_log.db"))
            agent = EquityOptionsFairValueAgent(trade_log=log)
            market = {
                "id": "m1",
                "conditionId": "0xabc",
                "question": "Largest company at end of June?",
                "outcomes": ["Nvidia", "Microsoft", "Apple"],
                "outcomePrices": ["0.40", "0.50", "0.10"],
                "endDate": "2026-06-30T00:00:00Z",
            }
            result = evaluate_largest_company_market(
                market=market,
                prices={"NVDA": 300.0, "MSFT": 200.0, "AAPL": 150.0},
                now=datetime(2026, 6, 1, tzinfo=timezone.utc),
                simulations=5_000,
            )

            agent._record(market, result)

            row = log.recent_brain_decisions(limit=1)[0]
            self.assertEqual(row["agent"], "equity_options_fair_value")
            self.assertEqual(row["signal_source"], "equity_options_fair_value")
            self.assertIn('"selected_ticker": "NVDA"', row["features_json"])


if __name__ == "__main__":
    unittest.main()
