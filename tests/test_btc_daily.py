"""Tests for the btc_daily agent's entry/exit decision logic.

Uses a fake feed and a mock Polymarket adapter — no live calls.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from agents.application.btc_daily import (
    BtcDailyConfig, BtcDailyEngine, CoinbasePriceFeed, OpenPosition,
    format_btc_daily_slug,
)
from agents.application.trade_log import TradeLog


import tempfile
from pathlib import Path
from datetime import datetime, timezone


class _FakeFeed:
    """Stand-in for CoinbasePriceFeed with hardcoded percent_change values."""
    def __init__(self):
        self.next_short = 0.0
        self.next_long = 0.0

    def percent_change(self, window_sec: int) -> float | None:
        # The engine asks twice per cycle:
        #   1) short window for trigger,
        #   2) longer window for trend filter.
        # Use simple heuristic: if window is in seconds and represents
        # less than ~10 minutes, treat as short; otherwise long.
        if window_sec <= 600:
            return self.next_short
        return self.next_long


class _TmpDB:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "trade_log.db")

    def tearDown(self):
        self._tmp.cleanup()


class TestSlugFormat(unittest.TestCase):
    def test_format_btc_daily_slug(self):
        d = datetime(2026, 5, 6, tzinfo=timezone.utc)
        self.assertEqual(format_btc_daily_slug(d), "bitcoin-up-or-down-on-may-6-2026")

    def test_format_no_leading_zero_on_day(self):
        d = datetime(2026, 1, 9, tzinfo=timezone.utc)
        self.assertEqual(format_btc_daily_slug(d), "bitcoin-up-or-down-on-january-9-2026")


class TestEntryTrigger(_TmpDB, unittest.TestCase):
    def _engine(self, *, execute=False, btc_short=0.0, btc_long=0.0,
                cooldown_sec=0):
        feed = _FakeFeed()
        feed.next_short = btc_short
        feed.next_long = btc_long
        cfg = BtcDailyConfig(
            trigger_pct=0.004, window_sec=180,
            cooldown_sec=cooldown_sec,
            position_size_usdc=3.0,
            min_candidate_price=0.30,
            skip_on_strong_trend=True, trend_window_minutes=30,
            trend_threshold_pct=0.008,
            poll_sec=1,
            heartbeat_path="/tmp/test_btc_daily_heartbeat",
        )
        log = TradeLog(self.db_path)
        polymarket = MagicMock()
        polymarket.execute_market_order.return_value = {
            "status": "matched",
            "order_avg_price_estimate": 0.50,
            "amount_usdc": 3.0,
            "token_id": "TOK_A",
            "outcome_traded": "Yes",
        }
        engine = BtcDailyEngine(
            polymarket=polymarket,
            trade_log=log,
            risk_gate=None,
            feed=feed,
            cfg=cfg,
            execute=execute,
        )
        # Stub _resolve_today_market to avoid a network call.
        engine._resolve_today_market = lambda: {
            "market_id": "M1",
            "token_ids": ["TOK_A", "TOK_B"],
            "outcomes": ["Yes", "No"],
            "doc": MagicMock(),
        }
        return engine, polymarket

    def test_no_entry_below_threshold(self):
        engine, _ = self._engine(btc_short=0.001)
        self.assertIsNone(engine.maybe_enter())

    def test_no_entry_when_position_already_open(self):
        engine, _ = self._engine(btc_short=0.01)
        engine.open_position = OpenPosition(
            market_id="M1", token_id="TOK_A", outcome="Yes",
            entry_price=0.5, entry_size_usdc=3.0, shares=6.0,
            opened_ts_ms=0, btc_move_at_entry=0.005, db_row_id=1,
        )
        self.assertIsNone(engine.maybe_enter())

    def test_strong_trend_blocks_aligned_move(self):
        # Both same sign + longer over threshold => skip
        engine, polymarket = self._engine(btc_short=0.005, btc_long=0.03)
        result = engine.maybe_enter()
        self.assertIsNone(result)
        polymarket.execute_market_order.assert_not_called()

    def test_strong_trend_does_not_block_counter_move(self):
        # Short pumps, long dumps => fade allowed (we bet against short)
        engine, polymarket = self._engine(
            btc_short=0.005, btc_long=-0.03, execute=True,
        )
        result = engine.maybe_enter()
        self.assertIsNotNone(result)
        polymarket.execute_market_order.assert_called_once()

    def test_pump_buys_no_dump_buys_yes(self):
        # BTC pumped → SELL (= buy NO)
        engine, polymarket = self._engine(btc_short=0.01, execute=True)
        engine.maybe_enter()
        rec = polymarket.execute_market_order.call_args[0][1]
        self.assertEqual(rec.side, "SELL")

        # Reset and try a dump
        engine, polymarket = self._engine(btc_short=-0.01, execute=True)
        engine.maybe_enter()
        rec = polymarket.execute_market_order.call_args[0][1]
        self.assertEqual(rec.side, "BUY")

    def test_shadow_mode_does_not_call_execute(self):
        engine, polymarket = self._engine(btc_short=0.01, execute=False)
        engine.maybe_enter()
        polymarket.execute_market_order.assert_not_called()

    def test_shadow_can_continue_when_risk_gate_blocks(self):
        with patch.dict("os.environ", {"SHADOW_IGNORE_RISK_GATE": "true"}):
            engine, polymarket = self._engine(btc_short=0.01, execute=False)
            gate = MagicMock()
            gate.ok.return_value = False
            gate.reason.return_value = "paper test block"
            engine.risk_gate = gate
            engine.maybe_enter()
        polymarket.execute_market_order.assert_not_called()
        rows = engine.trade_log.recent(limit=2)
        self.assertEqual(rows[0]["status"], "btc_daily_open")


# TestExitDecisions removed 2026-05-08: btc_daily no longer manages
# its own exits. position_manager owns the exit path; tests for it
# live in tests/test_position_manager.py.


if __name__ == "__main__":
    unittest.main()
