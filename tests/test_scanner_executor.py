from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from agents.application.scanner_executor import ScannerExecutor, ScannerExecutorConfig
from agents.application.trade_log import FILLED, TradeLog


def _features(**overrides):
    data = {
        "question": "Will BTC be up in 5 minutes?",
        "condition_id": "0xabc",
        "gamma_market_id": "123",
        "slug": "btc-updown-5m-123",
        "outcomes": ["Up", "Down"],
        "outcome_prices": [0.50, 0.50],
        "clob_token_ids": ["tok_up", "tok_down"],
        "selected_side": "BUY",
        "selected_token_id": "tok_up",
        "selected_outcome": "Up",
        "selected_entry_price": 0.50,
        "estimated_win_probability": 0.58,
        "scanner_raw_ev": 0.16,
        "meta_timing": "now",
    }
    data.update(overrides)
    return data


class ScannerExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "trade_log.db")
        self.log = TradeLog(db_path=self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _engine(self, *, execute=True, price=0.50, fillable=1.0):
        pm = MagicMock()
        pm._fillable_market_buy.return_value = (price, fillable, price)
        pm.execute_market_order.return_value = {
            "status": "matched",
            "order_avg_price_estimate": price,
            "amount_usdc": fillable,
        }
        pm.get_usdc_balance.return_value = 27.0
        risk = MagicMock()
        risk.reason.return_value = None
        cfg = ScannerExecutorConfig(
            poll_seconds=1,
            max_decision_age_seconds=180,
            position_size_usdc=1.0,
            min_score=0.55,
            min_raw_ev=0.04,
            require_timing_now=True,
        )
        return ScannerExecutor(
            cfg=cfg,
            trade_log=self.log,
            polymarket=pm,
            risk_gate=risk,
            execute=execute,
        ), pm

    def test_executes_fresh_scanner_approval_when_ev_and_risk_pass(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(),
            action="BUY",
        )
        engine, pm = self._engine(execute=True)

        stats = engine.run_once()

        self.assertEqual(stats["executed"], 1)
        pm.execute_market_order.assert_called_once()
        rows = self.log.filled_positions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["token_id"], "tok_up")
        decisions = self.log.recent_brain_decisions(limit=2)
        self.assertEqual(decisions[0]["agent"], "scanner_executor")
        self.assertEqual(decisions[0]["approved"], 1)

    def test_rejects_missing_execution_metadata(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(selected_token_id=""),
            action="BUY",
        )
        engine, pm = self._engine(execute=True)

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        self.assertEqual(self.log.filled_positions(), [])
        self.assertEqual(self.log.recent_brain_decisions(limit=1)[0]["reason"], "missing_execution_metadata")

    def test_rejects_negative_live_ev_even_if_scanner_score_is_high(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.900",
            score=0.90,
            market_type="general_binary",
            features=_features(estimated_win_probability=0.54),
            action="BUY",
        )
        engine, pm = self._engine(execute=True, price=0.55)

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        self.assertEqual(self.log.recent_brain_decisions(limit=1)[0]["reason"], "raw_ev_below_executor_min")

    def test_shadow_mode_does_not_call_exchange(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(),
            action="BUY",
        )
        engine, pm = self._engine(execute=False)

        stats = engine.run_once()

        self.assertEqual(stats["shadow"], 1)
        pm.execute_market_order.assert_not_called()
        rows = self.log.filled_positions_with_id()
        self.assertEqual(rows, [])
        raw_rows = self.log.count_recent(FILLED, hours=1)
        self.assertEqual(raw_rows, 1)


if __name__ == "__main__":
    unittest.main()
