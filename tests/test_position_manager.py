"""Tests for the position_manager — exit logic for poly1 main."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from agents.application.position_manager import (
    PositionManager, PositionManagerConfig, AggregatedPosition,
    CLOSED_TP, CLOSED_PARTIAL_TP, CLOSED_SL, CLOSED_TIMEOUT, CLOSED_DUST,
    CLOSE_FAILED,
)
from agents.application.trade_log import TradeLog, BTC_DAILY_OPEN, FILLED


class _TmpDB:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "trade_log.db")
        self.tl = TradeLog(self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def _insert_filled(self, market_id, token_id, side, price, cost):
        self.tl.insert_terminal(
            cycle_id="t-cycle",
            market_id=market_id,
            token_id=token_id,
            side=side,
            price=price,
            size_usdc=cost,
            confidence=0.7,
            status=FILLED,
        )

    def _insert_btc_daily_open(self, market_id, token_id, side, price, cost,
                               response=None):
        self.tl.insert_terminal(
            cycle_id="btc-cycle",
            market_id=market_id,
            token_id=token_id,
            side=side,
            price=price,
            size_usdc=cost,
            confidence=0.7,
            status=BTC_DAILY_OPEN,
            response=response,
        )

    def _config(self, **overrides):
        defaults = dict(
            take_profit_pct=0.25,
            stop_loss_pct=0.03,
            max_hold_hours=6,
            poll_seconds=15,
            sell_slippage=0.02,
            execute=False,
            partial_take_profit_enabled=False,
        )
        defaults.update(overrides)
        return PositionManagerConfig(**defaults)

    def _polymarket(self, midpoints):
        pm = MagicMock()
        client = MagicMock()
        # _on_chain_shares() calls get_balance_allowance(). Raise so it falls
        # back to journal-based share count (returns None) in all test contexts,
        # including discover mode where py_clob_client_v2 is stubbed.
        client.get_balance_allowance.side_effect = RuntimeError("no SDK in test env")
        def get_mid(token_id):
            if token_id in midpoints:
                return {"mid": midpoints[token_id]}
            raise RuntimeError(f"no midpoint for {token_id}")
        client.get_midpoint = MagicMock(side_effect=get_mid)
        pm.client = client
        pm.sell_shares = MagicMock(return_value={"status": "matched", "orderID": "0xabc"})
        pm.get_usdc_balance = MagicMock(return_value=100.0)
        return pm


class TestAggregation(_TmpDB, unittest.TestCase):
    def test_two_buys_same_token_collapse(self):
        # Same averaging-down scenario as 566188 yesterday
        self._insert_filled("M1", "TOK", "BUY", 0.40, 4.0)   # 10 shares
        self._insert_filled("M1", "TOK", "BUY", 0.20, 4.0)   # 20 shares
        pm = self._polymarket({"TOK": 0.30})
        mgr = PositionManager(polymarket=pm, trade_log=self.tl, cfg=self._config())
        positions = mgr._aggregate_open_positions()
        self.assertEqual(len(positions), 1)
        p = positions[0]
        self.assertEqual(p.token_id, "TOK")
        self.assertAlmostEqual(p.total_cost_usdc, 8.0, places=4)
        self.assertAlmostEqual(p.total_shares, 30.0, places=4)
        # weighted-avg entry = 8 / 30 = 0.2667
        self.assertAlmostEqual(p.avg_entry_price, 8.0 / 30.0, places=4)

    def test_sell_side_inverts_entry(self):
        # SELL @ 0.60 means we bought NO at 0.40
        self._insert_filled("M1", "TOK", "SELL", 0.60, 2.0)   # 5 shares of NO
        pm = self._polymarket({"TOK": 0.40})
        mgr = PositionManager(polymarket=pm, trade_log=self.tl, cfg=self._config())
        positions = mgr._aggregate_open_positions()
        self.assertEqual(len(positions), 1)
        self.assertAlmostEqual(positions[0].avg_entry_price, 0.40, places=4)
        self.assertAlmostEqual(positions[0].total_shares, 5.0, places=4)

    def test_btc_daily_open_is_managed_as_open_position(self):
        self._insert_btc_daily_open("BTC_DAILY", "TOK_BTC", "BUY", 0.50, 3.0)
        pm = self._polymarket({"TOK_BTC": 0.56})
        mgr = PositionManager(polymarket=pm, trade_log=self.tl, cfg=self._config())
        positions = mgr._aggregate_open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].token_id, "TOK_BTC")
        self.assertAlmostEqual(positions[0].total_cost_usdc, 3.0)

    def test_btc_daily_open_uses_actual_fill_price_from_response(self):
        self._insert_btc_daily_open(
            "BTC_DAILY",
            "TOK_BTC",
            "BUY",
            0.50,
            3.0,
            response={
                "order_avg_price_estimate": 0.33,
                "price_recommended": 0.50,
            },
        )
        pm = self._polymarket({"TOK_BTC": 0.31})
        mgr = PositionManager(polymarket=pm, trade_log=self.tl, cfg=self._config())
        positions = mgr._aggregate_open_positions()
        self.assertEqual(len(positions), 1)
        self.assertAlmostEqual(positions[0].avg_entry_price, 0.33, places=4)
        self.assertAlmostEqual(positions[0].total_shares, 3.0 / 0.33, places=4)

    def test_btc_daily_actual_fill_uses_real_entry_for_stop_loss(self):
        self._insert_btc_daily_open(
            "BTC_DAILY",
            "TOK_BTC",
            "BUY",
            0.50,
            3.0,
            response={
                "order_avg_price_estimate": 0.33,
                "price_recommended": 0.50,
            },
        )
        pm = self._polymarket({"TOK_BTC": 0.31})
        mgr = PositionManager(polymarket=pm, trade_log=self.tl, cfg=self._config())
        result = mgr.check_and_close_positions()
        self.assertEqual(result["evaluated"], 1)
        self.assertEqual(result["closed_sl"], 1)
        pm.sell_shares.assert_not_called()
        with self.tl._connect() as conn:
            mark = conn.execute(
                "SELECT entry_price, current_price, status FROM position_marks "
                "WHERE token_id='TOK_BTC'"
            ).fetchone()
        self.assertAlmostEqual(mark["entry_price"], 0.33, places=4)
        self.assertAlmostEqual(mark["current_price"], 0.31, places=4)
        self.assertEqual(mark["status"], "open")

    def test_shadow_btc_daily_open_is_not_managed(self):
        trade_id = self.tl.insert_pending(
            cycle_id="btc-shadow",
            market_id="BTC_DAILY",
            token_id="TOK_SHADOW",
            side="BUY",
            price=0.50,
            size_usdc=3.0,
            confidence=0.7,
        )
        self.tl.mark(
            trade_id,
            BTC_DAILY_OPEN,
            response={"shadow": True},
            error="SHADOW: would have entered BUY",
        )
        pm = self._polymarket({"TOK_SHADOW": 0.56})
        mgr = PositionManager(polymarket=pm, trade_log=self.tl, cfg=self._config())
        self.assertEqual(mgr._aggregate_open_positions(), [])


class TestEvaluation(_TmpDB, unittest.TestCase):
    def _make_position(self, token_id, entry_price, total_shares=10.0,
                      total_cost=None, age_seconds=0):
        if total_cost is None:
            total_cost = total_shares * entry_price
        return AggregatedPosition(
            token_id=token_id,
            market_id="M1",
            side="BUY",
            total_cost_usdc=total_cost,
            total_shares=total_shares,
            avg_entry_price=entry_price,
            earliest_ts=time.time() - age_seconds,
        )

    def test_take_profit_fires_at_5_pct_above_entry(self):
        pm = self._polymarket({"TOK_X": 0.525})  # 5% above 0.50
        mgr = PositionManager(polymarket=pm, trade_log=self.tl, cfg=self._config())
        pos = self._make_position("TOK_X", entry_price=0.50)
        reason, mid = mgr._evaluate_position(pos)
        self.assertEqual(reason, "take_profit")
        self.assertAlmostEqual(mid, 0.525)

    def test_stop_loss_fires_at_3_pct_below_entry(self):
        pm = self._polymarket({"TOK_X": 0.484})  # below 3% threshold of 0.50
        mgr = PositionManager(polymarket=pm, trade_log=self.tl, cfg=self._config())
        pos = self._make_position("TOK_X", entry_price=0.50)
        reason, mid = mgr._evaluate_position(pos)
        self.assertEqual(reason, "stop_loss")

    def test_within_band_holds(self):
        pm = self._polymarket({"TOK_X": 0.52})  # 4% above, less than fast TP
        mgr = PositionManager(polymarket=pm, trade_log=self.tl, cfg=self._config())
        pos = self._make_position("TOK_X", entry_price=0.50)
        reason, mid = mgr._evaluate_position(pos)
        self.assertIsNone(reason)

    def test_timeout_fires_after_max_hold(self):
        # Mid at entry, but age past max_hold
        pm = self._polymarket({"TOK_X": 0.50})
        mgr = PositionManager(polymarket=pm, trade_log=self.tl,
                              cfg=self._config(max_hold_hours=1))
        # 2 hours old
        pos = self._make_position("TOK_X", entry_price=0.50, age_seconds=7200)
        reason, mid = mgr._evaluate_position(pos)
        self.assertEqual(reason, "timeout")

    def test_take_profit_wins_over_timeout(self):
        # Both TP threshold met AND age > max_hold — take_profit fires (priority)
        pm = self._polymarket({"TOK_X": 0.60})  # 20% above
        mgr = PositionManager(polymarket=pm, trade_log=self.tl,
                              cfg=self._config(max_hold_hours=1))
        pos = self._make_position("TOK_X", entry_price=0.50, age_seconds=7200)
        reason, mid = mgr._evaluate_position(pos)
        self.assertEqual(reason, "take_profit")

    def test_profit_peak_survives_engine_recreation(self):
        pm = self._polymarket({"TOK_X": 0.56})
        mgr = PositionManager(polymarket=pm, trade_log=self.tl, cfg=self._config())
        pos = self._make_position("TOK_X", entry_price=0.50)
        mgr._evaluate_position(pos)

        pm2 = self._polymarket({"TOK_X": 0.54})
        mgr2 = PositionManager(polymarket=pm2, trade_log=self.tl, cfg=self._config())
        reason, _ = mgr2._evaluate_position(pos)
        self.assertEqual(reason, "take_profit")
        with self.tl._connect() as conn:
            row = conn.execute(
                "SELECT max_price, peak_drawdown_pct FROM position_marks WHERE token_id='TOK_X'"
            ).fetchone()
        self.assertAlmostEqual(row["max_price"], 0.56)
        self.assertGreater(row["peak_drawdown_pct"], 0.0)


class TestClosing(_TmpDB, unittest.TestCase):
    def test_shadow_mode_logs_decision_no_sell_call(self):
        self._insert_filled("M1", "TOK", "BUY", 0.50, 5.0)  # 10 shares
        pm = self._polymarket({"TOK": 0.55})  # +10%
        mgr = PositionManager(polymarket=pm, trade_log=self.tl,
                              cfg=self._config(execute=False))
        result = mgr.check_and_close_positions()
        self.assertEqual(result["closed_tp"], 1)
        pm.sell_shares.assert_not_called()  # shadow → no sell
        # closed row written
        rows = self.tl.recent(limit=10)
        statuses = [r["status"] for r in rows]
        self.assertIn(CLOSED_TP, statuses)

    def test_live_mode_calls_sell_shares(self):
        self._insert_filled("M1", "TOK", "BUY", 0.50, 5.0)
        pm = self._polymarket({"TOK": 0.55})
        mgr = PositionManager(polymarket=pm, trade_log=self.tl,
                              cfg=self._config(execute=True))
        result = mgr.check_and_close_positions()
        self.assertEqual(result["closed_tp"], 1)
        pm.sell_shares.assert_called_once()
        # check sell price = mid * (1 - slippage)
        call = pm.sell_shares.call_args
        self.assertAlmostEqual(call.kwargs["limit_price"], 0.55 * 0.98, places=4)
        self.assertIn("order_type", call.kwargs)

    def test_partial_take_profit_sells_half_and_keeps_position_open(self):
        self._insert_filled("M1", "TOK", "BUY", 0.50, 5.0)  # 10 shares
        pm = self._polymarket({"TOK": 0.56})  # +12%
        mgr = PositionManager(
            polymarket=pm,
            trade_log=self.tl,
            cfg=self._config(
                execute=True,
                partial_take_profit_enabled=True,
                partial_take_profit_pct=0.10,
                partial_take_profit_fraction=0.50,
            ),
        )
        result = mgr.check_and_close_positions()
        self.assertEqual(result["closed_tp"], 1)
        call = pm.sell_shares.call_args
        self.assertAlmostEqual(call.kwargs["shares"], 5.0, places=4)
        rows = self.tl.recent(limit=5)
        statuses = [r["status"] for r in rows]
        self.assertIn(CLOSED_PARTIAL_TP, statuses)
        positions = self.tl.filled_positions_with_id()
        self.assertEqual(len(positions), 1)

    def test_already_closed_position_is_not_aggregated(self):
        self._insert_filled("M1", "TOK", "BUY", 0.50, 5.0)
        # Pre-mark the token as already closed
        self.tl.insert_terminal(
            cycle_id="prior", market_id="M1", status=CLOSED_TP,
            token_id="TOK", side="SELL", price=0.55, size_usdc=5.5,
        )
        pm = self._polymarket({"TOK": 0.60})  # huge profit
        mgr = PositionManager(polymarket=pm, trade_log=self.tl,
                              cfg=self._config(execute=True))
        result = mgr.check_and_close_positions()
        self.assertEqual(result["evaluated"], 0)
        self.assertEqual(result["skipped_already_closed"], 0)
        self.assertEqual(result["closed_tp"], 0)
        pm.sell_shares.assert_not_called()

    def test_reentry_after_old_close_is_managed(self):
        self._insert_filled("M1", "TOK", "BUY", 0.50, 5.0)
        self.tl.insert_terminal(
            cycle_id="old-close", market_id="M1", status=CLOSED_DUST,
            token_id="TOK", side="SELL", price=0.50, size_usdc=0.5,
        )
        self._insert_filled("M1", "TOK", "BUY", 0.50, 5.0)

        pm = self._polymarket({"TOK": 0.55})
        mgr = PositionManager(polymarket=pm, trade_log=self.tl,
                              cfg=self._config(execute=True))
        result = mgr.check_and_close_positions()

        self.assertEqual(result["evaluated"], 1)
        self.assertEqual(result["skipped_already_closed"], 0)
        self.assertEqual(result["closed_tp"], 1)
        pm.sell_shares.assert_called_once()
        call = pm.sell_shares.call_args
        self.assertAlmostEqual(call.kwargs["shares"], 10.0, places=4)

    def test_failed_sell_writes_close_failed(self):
        self._insert_filled("M1", "TOK", "BUY", 0.50, 5.0)
        pm = self._polymarket({"TOK": 0.55})
        # Simulate rejected sell
        pm.sell_shares = MagicMock(return_value={"status": "rejected", "errorMsg": "no liquidity"})
        mgr = PositionManager(polymarket=pm, trade_log=self.tl,
                              cfg=self._config(execute=True))
        result = mgr.check_and_close_positions()
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["closed_tp"], 0)
        rows = self.tl.recent(limit=5)
        statuses = [r["status"] for r in rows]
        self.assertIn(CLOSE_FAILED, statuses)

    def test_dust_exit_is_marked_without_sell(self):
        self._insert_filled("M1", "TOK", "BUY", 0.50, 5.0)
        pm = self._polymarket({"TOK": 0.55})
        mgr = PositionManager(polymarket=pm, trade_log=self.tl,
                              cfg=self._config(execute=True, min_exit_notional_usdc=1.0))
        mgr._on_chain_shares = MagicMock(return_value=0.01)
        result = mgr.check_and_close_positions()
        self.assertEqual(result["closed_tp"], 1)
        pm.sell_shares.assert_not_called()
        rows = self.tl.recent(limit=5)
        self.assertIn("closed_dust", [r["status"] for r in rows])

    def test_live_order_status_live_is_not_marked_closed(self):
        self._insert_filled("M1", "TOK", "BUY", 0.50, 5.0)
        pm = self._polymarket({"TOK": 0.55})
        pm.sell_shares = MagicMock(return_value={"status": "live", "orderID": "resting"})
        mgr = PositionManager(polymarket=pm, trade_log=self.tl,
                              cfg=self._config(execute=True))
        result = mgr.check_and_close_positions()
        self.assertEqual(result["closed_tp"], 0)
        self.assertEqual(result["errors"], 1)
        rows = self.tl.recent(limit=5)
        statuses = [r["status"] for r in rows]
        self.assertIn(CLOSE_FAILED, statuses)
        self.assertNotIn(CLOSED_TP, statuses)


class TestEdgeCases(_TmpDB, unittest.TestCase):
    def test_zero_cost_position_skipped(self):
        self._insert_filled("M1", "TOK", "BUY", 0.50, 0)  # bug case
        pm = self._polymarket({"TOK": 0.55})
        mgr = PositionManager(polymarket=pm, trade_log=self.tl, cfg=self._config())
        result = mgr.check_and_close_positions()
        self.assertEqual(result["evaluated"], 0)

    def test_midpoint_fetch_failure_skips_not_fails(self):
        self._insert_filled("M1", "TOK_GOOD", "BUY", 0.50, 5.0)
        self._insert_filled("M1", "TOK_BAD", "BUY", 0.50, 5.0)
        pm = self._polymarket({"TOK_GOOD": 0.55})  # TOK_BAD will throw
        mgr = PositionManager(polymarket=pm, trade_log=self.tl, cfg=self._config())
        result = mgr.check_and_close_positions()
        # GOOD takes profit; BAD silently skipped (warn-but-continue —
        # don't let one bad token kill the whole cycle).
        self.assertEqual(result["evaluated"], 2)
        self.assertEqual(result["closed_tp"], 1)
        self.assertEqual(result["errors"], 0)


if __name__ == "__main__":
    unittest.main()
