from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from agents.application.execution_quality import ExecutionQualityAdvisor
from agents.application.orderbook_monitor import metrics_from_book
from agents.application.trade_log import TradeLog


class TestOrderbookExecutionQuality(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = TradeLog(db_path=self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_metrics_from_book_computes_spread_depth_and_slippage(self):
        row = metrics_from_book(
            token_id="TOK",
            market_id="M1",
            source="test",
            book={
                "bids": [{"price": "0.49", "size": "100"}],
                "asks": [
                    {"price": "0.51", "size": "2"},
                    {"price": "0.52", "size": "20"},
                ],
            },
        )
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["best_bid"], 0.49)
        self.assertAlmostEqual(row["best_ask"], 0.51)
        self.assertGreater(row["bid_depth_usdc"], 40)
        self.assertGreater(row["ask_depth_usdc"], 10)
        self.assertGreater(row["slippage_buy_3_pct"], 0)

    @patch.dict(os.environ, {
        "EXECUTION_QUALITY_REQUIRE_FRESH": "true",
        "EXECUTION_QUALITY_MIN_SCORE": "0.50",
    })
    def test_execution_quality_accepts_good_book(self):
        row = metrics_from_book(
            token_id="TOK",
            market_id="M1",
            source="test",
            book={
                "bids": [{"price": "0.50", "size": "100"}],
                "asks": [{"price": "0.51", "size": "100"}],
            },
        )
        self.log.upsert_orderbook_snapshot(row)
        quality = ExecutionQualityAdvisor(trade_log=self.log).evaluate(
            token_id="TOK",
            intended_usdc=3.0,
            internal_probability=0.62,
            entry_price=0.51,
        )
        self.assertTrue(quality.ok)
        self.assertGreaterEqual(quality.score, 0.5)
        self.assertEqual(quality.reason, "execution_quality_ok")
        self.assertGreater(quality.features["execution_net_ev"], 0.02)

    @patch.dict(os.environ, {
        "EXECUTION_QUALITY_REQUIRE_FRESH": "true",
        "EXECUTION_QUALITY_MAX_SPREAD_PCT": "0.05",
        "EXECUTION_QUALITY_MIN_SCORE": "0.50",
    })
    def test_execution_quality_blocks_wide_spread(self):
        row = metrics_from_book(
            token_id="TOK",
            market_id="M1",
            source="test",
            book={
                "bids": [{"price": "0.40", "size": "100"}],
                "asks": [{"price": "0.55", "size": "100"}],
            },
        )
        self.log.upsert_orderbook_snapshot(row)
        quality = ExecutionQualityAdvisor(trade_log=self.log).evaluate(
            token_id="TOK",
            intended_usdc=3.0,
        )
        self.assertFalse(quality.ok)
        self.assertIn("execution_quality_blocked", quality.reason)

    def test_execution_quality_blocks_negative_net_ev_after_friction(self):
        row = metrics_from_book(
            token_id="TOK",
            market_id="M1",
            source="test",
            book={
                "bids": [{"price": "0.49", "size": "100"}],
                "asks": [{"price": "0.51", "size": "100"}],
            },
        )
        self.log.upsert_orderbook_snapshot(row)
        quality = ExecutionQualityAdvisor(trade_log=self.log).evaluate(
            token_id="TOK",
            intended_usdc=3.0,
            internal_probability=0.52,
            entry_price=0.51,
            min_score=0.0,
            min_net_ev=0.02,
        )
        self.assertFalse(quality.ok)
        self.assertIn("net_ev", quality.reason)

    def test_execution_quality_blocks_missing_fresh_book(self):
        quality = ExecutionQualityAdvisor(trade_log=self.log).evaluate(
            token_id="MISSING",
            require_fresh=True,
        )
        self.assertFalse(quality.ok)
        self.assertEqual(quality.reason, "no_fresh_orderbook")

    def test_market_universe_tokens_prioritize_future_eligible_books(self):
        now = int(time.time())
        self.log.upsert_market_universe({
            "slug": "past-high-score",
            "horizon": "5m",
            "asset": "btc",
            "period_ts": now - 3600,
            "market_id": "past",
            "up_token": "PAST_UP",
            "down_token": "PAST_DOWN",
            "accepting_orders": True,
            "route_agent": "btc_5min",
            "score": 0.99,
            "eligible": True,
            "top_rank": 1,
        })
        self.log.upsert_market_universe({
            "slug": "future-active",
            "horizon": "5m",
            "asset": "btc",
            "period_ts": now + 300,
            "market_id": "future",
            "up_token": "FUTURE_UP",
            "down_token": "FUTURE_DOWN",
            "accepting_orders": True,
            "route_agent": "btc_5min",
            "score": 0.70,
            "eligible": True,
            "top_rank": 5,
        })
        self.log.upsert_market_universe({
            "slug": "future-ineligible",
            "horizon": "5m",
            "asset": "btc",
            "period_ts": now + 60,
            "market_id": "blocked",
            "up_token": "BLOCKED_UP",
            "down_token": "BLOCKED_DOWN",
            "accepting_orders": True,
            "route_agent": "btc_5min",
            "score": 1.0,
            "eligible": False,
            "top_rank": 1,
        })

        tokens = self.log.market_universe_tokens(limit=4, min_period_ts=now - 300)
        token_ids = [row["token_id"] for row in tokens]
        self.assertEqual(token_ids[:2], ["FUTURE_UP", "FUTURE_DOWN"])
        self.assertNotIn("BLOCKED_UP", token_ids)


if __name__ == "__main__":
    unittest.main()
