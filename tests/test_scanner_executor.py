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

    def _engine(
        self,
        *,
        execute=True,
        price=0.50,
        avg_price=None,
        fillable=1.0,
        allow_wait_with_high_score=False,
        wait_override_min_score=0.79,
        min_net_ev=0.03,
    ):
        pm = MagicMock()
        pm._fillable_market_buy.return_value = (
            price,
            fillable,
            price if avg_price is None else avg_price,
        )
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
            min_net_ev=min_net_ev,
            round_trip_cost_pct=0.04,
            max_entry_drift_pct=0.04,
            require_timing_now=True,
            allow_wait_with_high_score=allow_wait_with_high_score,
            wait_override_min_score=wait_override_min_score,
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
            features=_features(selected_entry_price=0.55, estimated_win_probability=0.54),
            action="BUY",
        )
        engine, pm = self._engine(execute=True, price=0.55)

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        self.assertEqual(self.log.recent_brain_decisions(limit=1)[0]["reason"], "raw_ev_below_council_min")

    def test_rejects_when_net_ev_does_not_cover_round_trip_cost(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.900",
            score=0.90,
            market_type="general_binary",
            features=_features(estimated_win_probability=0.53),
            action="BUY",
        )
        engine, pm = self._engine(execute=True, price=0.50)

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        row = self.log.recent_brain_decisions(limit=1)[0]
        self.assertEqual(row["reason"], "net_ev_below_council_min")
        self.assertIn('"net_ev": 0.02', row["features_json"])

    def test_decision_journal_records_rejects_and_live_enters(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.900",
            score=0.90,
            market_type="general_binary",
            features=_features(estimated_win_probability=0.53),
            action="BUY",
        )
        engine, _ = self._engine(execute=True, price=0.50)

        engine.run_once()

        journal = self.log.recent_decision_journal(limit=1)[0]
        self.assertEqual(journal["decision"], "REJECT")
        self.assertEqual(journal["reason"], "net_ev_below_council_min")
        self.assertAlmostEqual(journal["internal_probability"], 0.53)

        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xdef",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(
                condition_id="0xdef",
                selected_token_id="tok_up2",
                clob_token_ids=["tok_up2", "tok_down2"],
            ),
            action="BUY",
        )
        engine, _ = self._engine(execute=True)

        engine.run_once()

        journal = self.log.recent_decision_journal(limit=1)[0]
        self.assertEqual(journal["decision"], "ENTER")
        self.assertEqual(journal["reason"], "live_executed")
        self.assertGreater(journal["net_ev"], 0.0)

    def test_expert_solo_can_use_lower_net_ev_threshold(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.700",
            score=0.70,
            market_type="general_binary",
            features=_features(
                estimated_win_probability=0.535,
                evidence_route={
                    "mode": "solo",
                    "leader": "wallet:abc",
                    "reason": "expert_solo:wallet:abc",
                },
            ),
            action="BUY",
            signal_source="wallet:abc",
        )
        engine, pm = self._engine(execute=True, price=0.50)

        stats = engine.run_once()

        self.assertEqual(stats["executed"], 1)
        pm.execute_market_order.assert_called_once()
        journal = self.log.recent_decision_journal(limit=1)[0]
        self.assertEqual(journal["mode"], "solo")

    def test_rejects_when_live_entry_price_worsens_after_scan(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.900",
            score=0.90,
            market_type="general_binary",
            features=_features(selected_entry_price=0.50, estimated_win_probability=0.75),
            action="BUY",
        )
        engine, pm = self._engine(execute=True, price=0.52, avg_price=0.53)

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        self.assertEqual(self.log.recent_brain_decisions(limit=1)[0]["reason"], "entry_price_drift_too_high")

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

    def test_wait_timing_can_execute_as_controlled_probe_when_score_is_high(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.792",
            score=0.792,
            market_type="general_binary",
            features=_features(meta_timing="wait", estimated_win_probability=0.58),
            action="BUY",
        )
        engine, pm = self._engine(execute=True, allow_wait_with_high_score=True)

        stats = engine.run_once()

        self.assertEqual(stats["executed"], 1)
        pm.execute_market_order.assert_called_once()
        response = self.log.filled_positions_with_id()[0]["response_json"]
        self.assertIn('"timing_override": true', response)

    def test_executor_preserves_scanner_signal_source_for_feedback(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(signal_source="meta_brain,tavily"),
            action="BUY",
            signal_source="meta_brain,tavily",
        )
        engine, _ = self._engine(execute=True)

        stats = engine.run_once()

        self.assertEqual(stats["executed"], 1)
        row = self.log.recent_brain_decisions(limit=1)[0]
        self.assertEqual(row["signal_source"], "meta_brain,tavily")
        self.assertIn('"scanner_signal_source": "meta_brain,tavily"', row["features_json"])

    def test_wait_timing_still_rejects_when_override_score_is_low(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.780",
            score=0.78,
            market_type="general_binary",
            features=_features(meta_timing="wait", estimated_win_probability=0.58),
            action="BUY",
        )
        engine, pm = self._engine(execute=True, allow_wait_with_high_score=True)

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        self.assertEqual(self.log.recent_brain_decisions(limit=1)[0]["reason"], "timing_not_now")


if __name__ == "__main__":
    unittest.main()
