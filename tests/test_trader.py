import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agents.application.risk_gate import RiskGate
from agents.application.trade_log import (
    FAILED,
    FILLED,
    PENDING,
    SKIPPED_DEDUPE,
    SKIPPED_DRY_RUN,
    SKIPPED_GATE,
    SUBMITTED,
    TradeLog,
)
from agents.utils.objects import TradeRecommendation


class TempDataMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.db_path = str(self.tmp_path / "trade_log.db")
        self.kill_path = str(self.tmp_path / "HALT")
        self.usage_path = str(self.tmp_path / "llm_usage.jsonl")

    def tearDown(self):
        self._tmp.cleanup()


class TestTradeLog(TempDataMixin, unittest.TestCase):
    def test_idempotency_dedupes_recent_market(self):
        tl = TradeLog(self.db_path)
        cycle = tl.new_cycle_id()
        tl.insert_pending(
            cycle_id=cycle, market_id="42", token_id="t0",
            side="BUY", price=0.5, size_usdc=2.0, confidence=0.7,
        )
        self.assertTrue(tl.has_active_trade_for_market("42", hours=6))

        tl2 = TradeLog(self.db_path)
        self.assertTrue(tl2.has_active_trade_for_market("42", hours=6))

    def test_pending_marked_may_have_fired_on_recovery(self):
        from agents.application.trade_log import MAY_HAVE_FIRED

        tl = TradeLog(self.db_path)
        cycle = tl.new_cycle_id()
        tl.insert_pending(
            cycle_id=cycle, market_id="9", token_id="t1",
            side="SELL", price=0.5, size_usdc=1.0, confidence=0.9,
        )
        # recover with very small "older than" so it sweeps the just-inserted row
        recovered = tl.recover_stranded_pendings(older_than_minutes=-1)
        self.assertGreaterEqual(recovered, 1)
        # Stranded rows must keep blocking the same market to avoid double-fill.
        self.assertTrue(tl.has_active_trade_for_market("9", hours=24))
        rows = tl.recent(limit=5)
        self.assertEqual(rows[0]["status"], MAY_HAVE_FIRED)

    def test_may_have_fired_blocks_beyond_dedupe_window(self):
        """MAY_HAVE_FIRED must block re-trading regardless of age — operator
        verifies on-chain and clears the row manually. A time-bounded check
        would re-open a double-fill window after the dedupe window expires."""
        import sqlite3
        from datetime import datetime, timedelta, timezone
        from agents.application.trade_log import MAY_HAVE_FIRED

        tl = TradeLog(self.db_path)
        # Backdate a MAY_HAVE_FIRED row to 7 days ago — well past any reasonable
        # dedupe window. It must still block re-trading.
        ancient = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO trades (ts, cycle_id, market_id, status, error) "
                "VALUES (?, ?, ?, ?, ?)",
                (ancient, "old-cycle", "777", MAY_HAVE_FIRED, "ancient stranded"),
            )
            conn.commit()
        self.assertTrue(tl.has_active_trade_for_market("777", hours=6))
        self.assertTrue(tl.has_active_trade_for_market("777", hours=1))

    def test_scalper_pairs_table_exists(self):
        log = TradeLog(db_path=self.db_path)
        with log._connect() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='scalper_pairs'"
            ).fetchone()
        self.assertIsNotNone(row, "scalper_pairs table must be created on init")

    def test_wal_mode_enabled(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        try:
            log = TradeLog(db_path=path)
            with log._connect() as conn:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
        finally:
            os.unlink(path)

    def test_scalper_leg_status_constant(self):
        from agents.application.trade_log import SCALPER_LEG, ACTIVE_STATUSES
        self.assertEqual(SCALPER_LEG, "scalper_leg")
        # Must NOT be in ACTIVE_STATUSES — scalper has its own dedupe
        self.assertNotIn(SCALPER_LEG, ACTIVE_STATUSES)

    def test_counts_recent_hard_failures_for_market(self):
        log = TradeLog(db_path=self.db_path)
        cycle = log.new_cycle_id()
        log.insert_terminal(
            cycle_id=cycle,
            market_id="bad-market",
            status=FAILED,
            error="execute_market_order raised: PolyApiException[status_code=404]",
        )
        log.insert_terminal(
            cycle_id=cycle,
            market_id="bad-market",
            status=FAILED,
            error="execute_market_order raised: live ask price 0.7400 exceeds recommended price",
        )
        self.assertEqual(
            log.count_recent_failures_for_market(
                "bad-market",
                error_like=["%status_code=404%", "%live ask price%"],
            ),
            2,
        )

    def test_position_marks_track_mfe_mae_across_updates(self):
        log = TradeLog(db_path=self.db_path)
        first = log.upsert_position_mark(
            token_id="tok",
            market_id="m1",
            entry_price=0.50,
            current_price=0.55,
            shares=10,
        )
        second = log.upsert_position_mark(
            token_id="tok",
            market_id="m1",
            entry_price=0.50,
            current_price=0.52,
            shares=10,
        )
        self.assertAlmostEqual(first["mfe_pct"], 0.10)
        self.assertAlmostEqual(second["max_price"], 0.55)
        self.assertGreater(second["peak_drawdown_pct"], 0)

    def test_market_quarantine_blocks_recent_bad_market(self):
        log = TradeLog(db_path=self.db_path)
        self.assertFalse(log.is_market_quarantined("bad"))
        log.quarantine_market("bad", "404")
        self.assertTrue(log.is_market_quarantined("bad"))

    def test_agent_promotion_ledger_upsert(self):
        log = TradeLog(db_path=self.db_path)
        log.upsert_agent_promotion(
            agent="scalper",
            state="paper",
            reason="negative_live_probe",
            score=0.1,
            sample_size=5,
        )
        with log._connect() as conn:
            row = conn.execute(
                "SELECT state, sample_size FROM agent_promotion_ledger WHERE agent='scalper'"
            ).fetchone()
        self.assertEqual(row["state"], "paper")
        self.assertEqual(row["sample_size"], 5)

    def test_open_positions_start_after_latest_terminal_row(self):
        log = TradeLog(db_path=self.db_path)
        log.insert_terminal(
            cycle_id="old-open",
            market_id="M1",
            token_id="TOK",
            side="BUY",
            price=0.50,
            size_usdc=5.0,
            confidence=0.8,
            status=FILLED,
        )
        log.insert_terminal(
            cycle_id="old-close",
            market_id="M1",
            token_id="TOK",
            side="SELL",
            price=0.50,
            size_usdc=5.0,
            status="closed_dust",
        )
        new_id = log.insert_terminal(
            cycle_id="new-open",
            market_id="M1",
            token_id="TOK",
            side="BUY",
            price=0.25,
            size_usdc=3.0,
            confidence=0.8,
            status=FILLED,
        )

        open_with_id = log.filled_positions_with_id()
        self.assertEqual([r["id"] for r in open_with_id], [new_id])
        self.assertEqual(len(log.filled_positions()), 1)
        self.assertAlmostEqual(log.filled_positions()[0]["size_usdc"], 3.0)

    def test_close_attempt_idempotency_can_be_scoped_after_entry(self):
        log = TradeLog(db_path=self.db_path)
        old_id = log.insert_terminal(
            cycle_id="old-open",
            market_id="M1",
            token_id="TOK",
            side="BUY",
            price=0.50,
            size_usdc=5.0,
            status=FILLED,
        )
        log.insert_terminal(
            cycle_id="old-close",
            market_id="M1",
            token_id="TOK",
            side="SELL",
            price=0.51,
            size_usdc=5.1,
            status="closed_take_profit",
        )
        new_id = log.insert_terminal(
            cycle_id="new-open",
            market_id="M1",
            token_id="TOK",
            side="BUY",
            price=0.40,
            size_usdc=4.0,
            status=FILLED,
        )

        self.assertTrue(log.has_close_attempt_for_token("TOK", after_id=old_id))
        self.assertFalse(log.has_close_attempt_for_token("TOK", after_id=new_id))


class TestRiskGate(TempDataMixin, unittest.TestCase):
    def _gate(self, **kwargs):
        tl = TradeLog(self.db_path)
        defaults = dict(
            trade_log=tl,
            polymarket=None,
            starting_balance_usdc=100.0,
            max_daily_loss_pct=0.10,
            max_trades_per_hour=4,
            min_usdc_floor=10.0,
            max_daily_token_usd=5.0,
            kill_switch_file=self.kill_path,
            llm_usage_file=self.usage_path,
        )
        defaults.update(kwargs)
        return RiskGate(**defaults)

    def test_kill_switch_file_blocks(self):
        Path(self.kill_path).write_text("halt")
        gate = self._gate()
        self.assertFalse(gate.ok())
        self.assertIn("kill switch", gate.reason())

    def test_balance_floor_blocks(self):
        pm = MagicMock()
        pm.get_usdc_balance.return_value = 5.0
        gate = self._gate(polymarket=pm)
        self.assertFalse(gate.ok())
        self.assertIn("below floor", gate.reason())

    def test_daily_loss_blocks(self):
        """Drawdown gate blocks when journal-based loss exceeds the daily
        loss limit. Cash level on-chain is intentionally NOT used: under
        the shared-wallet model another bot can spend pUSD without
        being a poly1 loss."""
        log = TradeLog(db_path=self.db_path)
        # Spent $30, positions are now worth $0 (resolved against us / mtm crashed).
        self._insert_filled(log, "M1", "TOK_X", "BUY", 0.50, 30.0)
        # No midpoints registered → mtm fallback to entry — but we want a
        # real loss, so register a midpoint of 0 explicitly.
        pm = MagicMock()
        pm.get_usdc_balance = MagicMock(return_value=80.0)
        client = MagicMock()
        client.get_midpoint = MagicMock(return_value={"mid": 0.0})
        pm.client = client
        gate = RiskGate(
            trade_log=log,
            polymarket=pm,
            starting_balance_usdc=100.0,
            max_daily_loss_pct=0.10,
            max_trades_per_hour=100,
            min_usdc_floor=10.0,
            kill_switch_file=self.kill_path,
            llm_usage_file=self.usage_path,
        )
        # portfolio = 100 - 30 + 0 = 70 → drawdown 30% > 10%
        self.assertFalse(gate.ok())
        self.assertIn("drawdown", gate.reason())

    def test_passes_when_clean(self):
        pm = MagicMock()
        pm.get_usdc_balance.return_value = 100.0
        gate = self._gate(polymarket=pm)
        self.assertTrue(gate.ok())

    def test_available_for_trader_subtracts_scalper_reserve(self):
        log = TradeLog(db_path=self.db_path)
        poly = MagicMock()
        poly.get_usdc_balance = MagicMock(return_value=80.0)
        gate = RiskGate(trade_log=log, polymarket=poly,
                         starting_balance_usdc=80.0,
                         scalper_reserve_usdc=20.0,
                         swarm_reserve_usdc=0.0,
                         btc_daily_reserve_usdc=0.0,
                         near_resolution_reserve_usdc=0.0,
                         news_shock_reserve_usdc=0.0,
                         wallet_follow_reserve_usdc=0.0)
        self.assertEqual(gate.available_for_trader(), 60.0)

    def test_scalper_reserve_setter_updates_reserves_dict(self):
        log = TradeLog(db_path=self.db_path)
        gate = RiskGate(trade_log=log, polymarket=None,
                         starting_balance_usdc=80.0,
                         scalper_reserve_usdc=20.0)
        gate.scalper_reserve = 12.5
        self.assertEqual(gate.reserves["scalper"], 12.5)
        self.assertEqual(gate.scalper_reserve, 12.5)

    def test_available_for_trader_zero_reserve_default(self):
        log = TradeLog(db_path=self.db_path)
        poly = MagicMock()
        poly.get_usdc_balance = MagicMock(return_value=80.0)
        gate = RiskGate(trade_log=log, polymarket=poly,
                         starting_balance_usdc=80.0,
                         scalper_reserve_usdc=0.0,
                         swarm_reserve_usdc=0.0,
                         btc_daily_reserve_usdc=0.0,
                         near_resolution_reserve_usdc=0.0,
                         news_shock_reserve_usdc=0.0,
                         wallet_follow_reserve_usdc=0.0)  # ignore env
        self.assertEqual(gate.available_for_trader(), 80.0)

    def test_min_floor_uses_available_after_reserve(self):
        """If reserve makes available drop below min_usdc_floor, gate blocks."""
        log = TradeLog(db_path=self.db_path)
        poly = MagicMock()
        poly.get_usdc_balance = MagicMock(return_value=25.0)
        gate = RiskGate(trade_log=log, polymarket=poly,
                         starting_balance_usdc=80.0,
                         scalper_reserve_usdc=20.0,
                         min_usdc_floor=10.0)
        # available = 25 - 20 = 5 < 10 → block
        self.assertIsNotNone(gate.reason())

    def _insert_filled(self, log, market_id, token_id, side, price, size_usdc):
        log.insert_terminal(
            cycle_id="t-cycle",
            market_id=market_id,
            token_id=token_id,
            side=side,
            price=price,
            size_usdc=size_usdc,
            confidence=0.9,
            status=FILLED,
        )

    def _poly_with_midpoints(self, balance, midpoints):
        poly = MagicMock()
        poly.get_usdc_balance = MagicMock(return_value=balance)
        client = MagicMock()
        def get_mid(token_id):
            if token_id in midpoints:
                return {"mid": midpoints[token_id]}
            raise RuntimeError("unknown token")
        client.get_midpoint = MagicMock(side_effect=get_mid)
        poly.client = client
        return poly

    def test_drawdown_uses_portfolio_value_not_cash(self):
        """Cash-only drawdown reads deployed capital as loss; portfolio
        drawdown (cash + MTM) must not block when positions are flat."""
        log = TradeLog(db_path=self.db_path)
        # Deployed $9.49 split across 4 positions, all flat at entry price.
        self._insert_filled(log, "566188", "TOK_A", "BUY",  0.38,  1.996)
        self._insert_filled(log, "566228", "TOK_B", "BUY",  0.997, 1.946)
        self._insert_filled(log, "566187", "TOK_C", "SELL", 0.565, 1.897)
        self._insert_filled(log, "653788", "TOK_D", "BUY",  0.11,  3.650)
        # Midpoints exactly at entry → MTM == cost, portfolio == starting.
        poly = self._poly_with_midpoints(
            balance=70.51,  # 80 - 9.49 deployed
            midpoints={"TOK_A": 0.38, "TOK_B": 0.997, "TOK_C": 0.435, "TOK_D": 0.11},
        )
        gate = self._gate(polymarket=poly, starting_balance_usdc=80.0,
                          max_daily_loss_pct=0.10,
                          max_trades_per_hour=100)  # isolate drawdown check
        self.assertTrue(gate.ok(), msg=f"unexpected block: {gate.reason()}")

    def test_drawdown_blocks_on_real_mtm_loss(self):
        """If positions are actually losing enough to push portfolio below
        starting * (1 - max_daily_loss_pct), the gate must still block."""
        log = TradeLog(db_path=self.db_path)
        # Deployed $30 across two positions; both lost most of their value.
        self._insert_filled(log, "M1", "TOK_X", "BUY", 0.50, 20.0)  # cost $20
        self._insert_filled(log, "M2", "TOK_Y", "BUY", 0.40, 10.0)  # cost $10
        # Journal-based portfolio = starting - cost + mtm
        # = 100 - 30 + (40 * 0.10 + 25 * 0.05) = 100 - 30 + 5.25 = 75.25
        # drawdown = (100 - 75.25) / 100 = 24.75% > 10%
        poly = self._poly_with_midpoints(
            balance=70.0,  # cash is irrelevant under journal-based accounting
            midpoints={"TOK_X": 0.10, "TOK_Y": 0.05},
        )
        gate = self._gate(polymarket=poly, starting_balance_usdc=100.0,
                          max_daily_loss_pct=0.10,
                          max_trades_per_hour=100)  # isolate drawdown check
        self.assertFalse(gate.ok())
        self.assertIn("drawdown", gate.reason())

    def test_mtm_falls_back_to_entry_when_midpoint_fails(self):
        """If midpoint lookup raises, fall back to entry price for that
        position (treat it as flat) — don't crash, don't spuriously block."""
        log = TradeLog(db_path=self.db_path)
        self._insert_filled(log, "M1", "TOK_X", "BUY", 0.50, 20.0)
        # No midpoints registered → get_midpoint raises for every token.
        poly = self._poly_with_midpoints(balance=80.0, midpoints={})
        gate = self._gate(polymarket=poly, starting_balance_usdc=100.0,
                          max_daily_loss_pct=0.10)
        # Fallback MTM = cost = 20. Portfolio = 80 + 20 = 100. No drawdown.
        self.assertTrue(gate.ok(), msg=f"unexpected block: {gate.reason()}")
        self.assertAlmostEqual(gate.position_mtm_usd(), 20.0, places=4)

    def test_sell_position_mtm_uses_actual_token_entry_price(self):
        """SELL recommendations are encoded as BUYs of the opposite token.
        TradeLog.price is already the actual token entry price, so MTM must
        not invert it again with 1-price.
        """
        log = TradeLog(db_path=self.db_path)
        self._insert_filled(log, "M1", "NO_TOKEN", "SELL", 0.40, 20.0)
        poly = self._poly_with_midpoints(balance=60.0, midpoints={"NO_TOKEN": 0.40})
        gate = self._gate(polymarket=poly, starting_balance_usdc=80.0,
                          max_daily_loss_pct=0.10,
                          max_trades_per_hour=100)
        # shares = 20 / 0.40 = 50; mtm = 50 * 0.40 = 20; portfolio flat.
        self.assertAlmostEqual(gate.position_mtm_usd(), 20.0, places=4)
        self.assertTrue(gate.ok(), msg=f"unexpected block: {gate.reason()}")


class TestPolymarketDryRun(unittest.TestCase):
    def test_polymarket_live_false_no_private_key(self):
        old = os.environ.pop("POLYGON_WALLET_PRIVATE_KEY", None)
        try:
            from agents.polymarket.polymarket import Polymarket

            pm = Polymarket(live=False)
            self.assertIsNone(pm.client)
            self.assertIsNone(pm.credentials)
        finally:
            if old is not None:
                os.environ["POLYGON_WALLET_PRIVATE_KEY"] = old


class TestExecuteMarketOrderSideMapping(unittest.TestCase):
    def _build_market_doc(self, token_ids, outcomes):
        doc = MagicMock()
        doc.dict.return_value = {
            "metadata": {
                "clob_token_ids": str(token_ids),
                "outcomes": str(outcomes),
            }
        }
        return [doc]

    def _book(self, asks):
        return {
            "asks": [{"price": str(price), "size": str(size)} for price, size in asks],
            "tick_size": "0.01",
        }

    def test_buy_picks_yes_token_with_anchor_price(self):
        from agents.polymarket.polymarket import Polymarket

        pm = Polymarket.__new__(Polymarket)
        pm.client = MagicMock()
        pm.client.get_order_book.return_value = self._book([(0.55, 100)])
        pm.client.create_and_post_market_order.return_value = {
            "orderID": "ord123",
            "status": "submitted",
        }

        rec = TradeRecommendation(
            price=0.55, size_fraction=0.1, side="BUY",
            confidence=0.7, amount_usdc=5.0,
        )
        market = self._build_market_doc(["yes_tok", "no_tok"], ["YES", "NO"])

        result = pm.execute_market_order(market, rec)

        self.assertEqual(result["token_id"], "yes_tok")
        self.assertEqual(result["outcome_traded"], "YES")
        self.assertEqual(result["amount_usdc"], 5.0)
        self.assertEqual(result["price_recommended"], 0.55)
        self.assertEqual(result["order_price_model"], 0.55)
        self.assertEqual(result["order_price"], 0.56)
        self.assertEqual(result["side_recommended"], "BUY")
        # Verify MarketOrderArgs received the live-book price plus one tick.
        args = pm.client.create_and_post_market_order.call_args[0][0]
        self.assertEqual(args.price, 0.56)
        self.assertEqual(args.token_id, "yes_tok")

    def test_sell_picks_no_token_and_inverts_price(self):
        from agents.polymarket.polymarket import Polymarket

        pm = Polymarket.__new__(Polymarket)
        pm.client = MagicMock()
        pm.client.get_order_book.return_value = self._book([(0.6, 100)])
        pm.client.create_and_post_market_order.return_value = {
            "orderID": "ord456",
            "status": "submitted",
        }

        # LLM thinks YES is worth 0.4 (so NO is worth 0.6) → recommends SELL at 0.4.
        rec = TradeRecommendation(
            price=0.4, size_fraction=0.1, side="SELL",
            confidence=0.8, amount_usdc=3.0,
        )
        market = self._build_market_doc(["yes_tok", "no_tok"], ["YES", "NO"])

        result = pm.execute_market_order(market, rec)

        self.assertEqual(result["token_id"], "no_tok")
        self.assertEqual(result["outcome_traded"], "NO")
        # SELL at price=0.4 (anchored to YES) = BUY of NO at price 0.6.
        self.assertAlmostEqual(result["order_price_model"], 0.6)
        self.assertAlmostEqual(result["order_price"], 0.61)
        args = pm.client.create_and_post_market_order.call_args[0][0]
        self.assertAlmostEqual(args.price, 0.61)
        self.assertEqual(args.token_id, "no_tok")

    def test_rejects_live_price_above_slippage(self):
        from agents.polymarket.polymarket import Polymarket

        pm = Polymarket.__new__(Polymarket)
        pm.client = MagicMock()
        pm.client.get_order_book.return_value = self._book([(0.7, 100)])

        rec = TradeRecommendation(
            price=0.55, size_fraction=0.1, side="BUY",
            confidence=0.7, amount_usdc=5.0,
        )
        market = self._build_market_doc(["yes_tok", "no_tok"], ["YES", "NO"])

        with self.assertRaises(ValueError):
            pm.execute_market_order(market, rec)
        pm.client.create_and_post_market_order.assert_not_called()

    def test_reduces_amount_to_available_liquidity(self):
        from agents.polymarket.polymarket import Polymarket

        pm = Polymarket.__new__(Polymarket)
        pm.client = MagicMock()
        pm.client.get_order_book.return_value = self._book([(0.55, 2)])
        pm.client.create_and_post_market_order.return_value = {
            "orderID": "ord789",
            "status": "submitted",
        }

        rec = TradeRecommendation(
            price=0.55, size_fraction=0.1, side="BUY",
            confidence=0.7, amount_usdc=5.0,
        )
        market = self._build_market_doc(["yes_tok", "no_tok"], ["YES", "NO"])

        result = pm.execute_market_order(market, rec)

        self.assertAlmostEqual(result["amount_usdc"], 1.1)
        args = pm.client.create_and_post_market_order.call_args[0][0]
        self.assertAlmostEqual(args.amount, 1.1)

    def test_rejects_non_binary_market(self):
        from agents.polymarket.polymarket import Polymarket

        pm = Polymarket.__new__(Polymarket)
        pm.client = MagicMock()
        pm.client.get_order_book.return_value = self._book([(0.5, 100)])

        rec = TradeRecommendation(
            price=0.5, size_fraction=0.1, side="BUY",
            confidence=0.5, amount_usdc=2.0,
        )
        market = self._build_market_doc(["a", "b", "c"], ["X", "Y", "Z"])

        with self.assertRaises(ValueError):
            pm.execute_market_order(market, rec)

    def test_rejects_zero_amount(self):
        from agents.polymarket.polymarket import Polymarket

        pm = Polymarket.__new__(Polymarket)
        pm.client = MagicMock()

        rec = TradeRecommendation(
            price=0.5, size_fraction=0.1, side="BUY",
            confidence=0.5, amount_usdc=0.0,
        )
        market = self._build_market_doc(["yes", "no"], ["YES", "NO"])

        with self.assertRaises(ValueError):
            pm.execute_market_order(market, rec)


class TestTraderTopN(TempDataMixin, unittest.TestCase):
    def _make_market(self, market_id, spread):
        doc = MagicMock()
        doc.dict.return_value = {
            "metadata": {
                "id": market_id,
                "spread": spread,
                "clob_token_ids": "['yes_t', 'no_t']",
                "outcomes": "['YES', 'NO']",
            }
        }
        return (doc, 0.5)

    def test_top_n_iteration_respects_min_confidence(self):
        from agents.application.trade import Trader

        with patch("agents.application.trade.Polymarket") as PMock, \
                patch("agents.application.trade.Agent") as AgentMock, \
                patch("agents.application.trade.Gamma"):
            pm = PMock.return_value
            pm.get_all_tradeable_events.return_value = []
            pm.get_usdc_balance.return_value = 50.0

            agent = AgentMock.return_value
            agent.filter_events_with_rag.return_value = []
            agent.map_filtered_events_to_markets.return_value = []
            agent.filter_markets.return_value = [
                self._make_market(1, 0.05),
                self._make_market(2, 0.10),
                self._make_market(3, 0.02),
            ]
            agent.source_best_trade.side_effect = [
                "stub1", "stub2", "stub3",
            ]
            agent.parse_trade_recommendation.side_effect = [
                TradeRecommendation(price=0.6, size_fraction=0.1, side="BUY", confidence=0.4),
                TradeRecommendation(price=0.5, size_fraction=0.05, side="BUY", confidence=0.9),
                TradeRecommendation(price=0.7, size_fraction=0.05, side="SELL", confidence=0.85),
            ]

            tl = TradeLog(self.db_path)
            os.environ.pop("STARTING_BALANCE_USDC", None)
            gate = RiskGate(
                trade_log=tl, polymarket=pm,
                starting_balance_usdc=0.0,  # disable drawdown gate
                max_daily_loss_pct=0.99,
                max_trades_per_hour=99,
                min_usdc_floor=0.0,
                max_daily_token_usd=999.0,
                kill_switch_file=self.kill_path,
                llm_usage_file=self.usage_path,
            )
            trader = Trader(
                dry_run=True,
                top_n=3,
                max_trades_per_cycle=5,
                min_confidence=0.7,
                max_position_fraction=0.1,
                trade_log=tl,
                risk_gate=gate,
            )

            trader.one_best_trade_sweep()

        # 3 evaluated; 1 skipped_gate (low confidence), 2 skipped_dry_run.
        recent = tl.recent(limit=10)
        statuses = [r["status"] for r in recent]
        self.assertEqual(statuses.count(SKIPPED_GATE), 1)
        self.assertEqual(statuses.count(SKIPPED_DRY_RUN), 2)

    def test_shadow_can_continue_when_risk_gate_blocks(self):
        from agents.application.trade import Trader

        with patch.dict(os.environ, {"SHADOW_IGNORE_RISK_GATE": "true"}), \
                patch("agents.application.trade.Polymarket") as PMock, \
                patch("agents.application.trade.Agent") as AgentMock, \
                patch("agents.application.trade.Gamma"):
            pm = PMock.return_value
            pm.get_all_tradeable_events.return_value = []
            pm.get_usdc_balance.return_value = 50.0

            agent = AgentMock.return_value
            agent.filter_events_with_rag.return_value = []
            agent.map_filtered_events_to_markets.return_value = []
            agent.filter_markets.return_value = [self._make_market(22, 0.05)]
            agent.source_best_trade.return_value = "stub"
            agent.parse_trade_recommendation.return_value = TradeRecommendation(
                price=0.5, size_fraction=0.05, side="BUY", confidence=0.9,
            )

            tl = TradeLog(self.db_path)
            gate = MagicMock()
            gate.ok.return_value = False
            gate.reason.return_value = "paper test block"
            gate.available_for_trader.return_value = 20.0

            trader = Trader(
                dry_run=True,
                top_n=1,
                max_trades_per_cycle=1,
                min_confidence=0.7,
                max_position_fraction=0.1,
                trade_log=tl,
                risk_gate=gate,
            )

            trader.one_best_trade_sweep()

        recent = tl.recent(limit=5)
        self.assertEqual(recent[0]["status"], SKIPPED_DRY_RUN)

    def test_illiquid_market_writes_skipped_gate_not_failed(self):
        """execute_market_order raising ValueError('no asks available') must
        write SKIPPED_GATE (veto), not FAILED (error that blocks the trader
        for 24 h in the allocator)."""
        from agents.application.trade import Trader

        with patch("agents.application.trade.Polymarket") as PMock, \
                patch("agents.application.trade.Agent") as AgentMock, \
                patch("agents.application.trade.Gamma"):
            pm = PMock.return_value
            pm.get_all_tradeable_events.return_value = []
            pm.get_usdc_balance.return_value = 50.0
            pm.execute_market_order.side_effect = ValueError(
                "no asks available for token_id=abc123"
            )

            agent = AgentMock.return_value
            agent.filter_events_with_rag.return_value = []
            agent.map_filtered_events_to_markets.return_value = []
            agent.filter_markets.return_value = [self._make_market(99, 0.05)]
            agent.source_best_trade.return_value = "stub"
            agent.parse_trade_recommendation.return_value = TradeRecommendation(
                price=0.5, size_fraction=0.05, side="BUY", confidence=0.9,
            )

            tl = TradeLog(self.db_path)
            gate = RiskGate(
                trade_log=tl, polymarket=pm,
                starting_balance_usdc=0.0,
                max_daily_loss_pct=0.99,
                max_trades_per_hour=99,
                min_usdc_floor=0.0,
                max_daily_token_usd=999.0,
                kill_switch_file=self.kill_path,
                llm_usage_file=self.usage_path,
            )
            trader = Trader(
                dry_run=False,  # live path so execute_market_order is called
                top_n=1,
                max_trades_per_cycle=1,
                min_confidence=0.7,
                max_position_fraction=0.1,
                trade_log=tl,
                risk_gate=gate,
            )

            trader.one_best_trade_sweep()

        recent = tl.recent(limit=5)
        statuses = [r["status"] for r in recent]
        self.assertIn(SKIPPED_GATE, statuses, "illiquid market must write SKIPPED_GATE")
        self.assertNotIn(FAILED, statuses, "illiquid market must NOT write FAILED")

    def test_live_price_slippage_writes_skipped_gate_not_failed(self):
        from agents.application.trade import Trader

        with patch("agents.application.trade.Polymarket") as PMock, \
                patch("agents.application.trade.Agent") as AgentMock, \
                patch("agents.application.trade.Gamma"):
            pm = PMock.return_value
            pm.get_all_tradeable_events.return_value = []
            pm.get_usdc_balance.return_value = 50.0
            pm.execute_market_order.side_effect = ValueError(
                "live ask price 0.7400 exceeds recommended price 0.5000"
            )

            agent = AgentMock.return_value
            agent.filter_events_with_rag.return_value = []
            agent.map_filtered_events_to_markets.return_value = []
            agent.filter_markets.return_value = [self._make_market(100, 0.05)]
            agent.source_best_trade.return_value = "stub"
            agent.parse_trade_recommendation.return_value = TradeRecommendation(
                price=0.5, size_fraction=0.05, side="BUY", confidence=0.9,
            )

            tl = TradeLog(self.db_path)
            gate = RiskGate(
                trade_log=tl, polymarket=pm,
                starting_balance_usdc=0.0,
                max_daily_loss_pct=0.99,
                max_trades_per_hour=99,
                min_usdc_floor=0.0,
                max_daily_token_usd=999.0,
                kill_switch_file=self.kill_path,
                llm_usage_file=self.usage_path,
            )
            trader = Trader(
                dry_run=False,
                top_n=1,
                max_trades_per_cycle=1,
                min_confidence=0.7,
                max_position_fraction=0.1,
                trade_log=tl,
                risk_gate=gate,
            )

            trader.one_best_trade_sweep()

        recent = tl.recent(limit=5)
        statuses = [r["status"] for r in recent]
        self.assertIn(SKIPPED_GATE, statuses)
        self.assertNotIn(FAILED, statuses)

    def test_broken_market_failure_threshold_skips_before_llm(self):
        from agents.application.trade import Trader

        with patch("agents.application.trade.Polymarket") as PMock, \
                patch("agents.application.trade.Agent") as AgentMock, \
                patch("agents.application.trade.Gamma"):
            pm = PMock.return_value
            pm.get_all_tradeable_events.return_value = []
            pm.get_usdc_balance.return_value = 50.0

            agent = AgentMock.return_value
            agent.filter_events_with_rag.return_value = []
            agent.map_filtered_events_to_markets.return_value = []
            agent.filter_markets.return_value = [self._make_market(101, 0.05)]

            tl = TradeLog(self.db_path)
            for _ in range(3):
                tl.insert_terminal(
                    cycle_id=tl.new_cycle_id(),
                    market_id="101",
                    status=FAILED,
                    error="execute_market_order raised: PolyApiException[status_code=404]",
                )
            gate = RiskGate(
                trade_log=tl, polymarket=pm,
                starting_balance_usdc=0.0,
                max_daily_loss_pct=0.99,
                max_trades_per_hour=99,
                min_usdc_floor=0.0,
                max_daily_token_usd=999.0,
                kill_switch_file=self.kill_path,
                llm_usage_file=self.usage_path,
            )
            trader = Trader(
                dry_run=False,
                top_n=1,
                max_trades_per_cycle=1,
                min_confidence=0.7,
                max_position_fraction=0.1,
                trade_log=tl,
                risk_gate=gate,
            )

            trader.one_best_trade_sweep()

        agent.source_best_trade.assert_not_called()
        recent = tl.recent(limit=1)[0]
        self.assertEqual(recent["status"], SKIPPED_GATE)
        self.assertIn("broken_market_blacklist", recent["error"])

    def test_ai_quota_failure_in_event_filter_skips_cycle_not_crash(self):
        from agents.application.trade import Trader

        with patch("agents.application.trade.Polymarket") as PMock, \
                patch("agents.application.trade.Agent") as AgentMock, \
                patch("agents.application.trade.Gamma"):
            pm = PMock.return_value
            pm.get_all_tradeable_events.return_value = [MagicMock()]
            pm.get_usdc_balance.return_value = 50.0

            agent = AgentMock.return_value
            agent.filter_events_with_rag.side_effect = RuntimeError(
                "Error code: 429 - insufficient_quota"
            )

            tl = TradeLog(self.db_path)
            gate = RiskGate(
                trade_log=tl, polymarket=pm,
                starting_balance_usdc=0.0,
                max_daily_loss_pct=0.99,
                max_trades_per_hour=99,
                min_usdc_floor=0.0,
                max_daily_token_usd=999.0,
                kill_switch_file=self.kill_path,
                llm_usage_file=self.usage_path,
            )
            trader = Trader(
                dry_run=False,
                top_n=1,
                max_trades_per_cycle=1,
                min_confidence=0.7,
                max_position_fraction=0.1,
                trade_log=tl,
                risk_gate=gate,
            )

            trader.one_best_trade_sweep()

        recent = tl.recent(limit=1)[0]
        self.assertEqual(recent["status"], SKIPPED_GATE)
        self.assertEqual(recent["market_id"], "__cycle__")
        self.assertIn("ai_filter_unavailable", recent["error"])
        agent.map_filtered_events_to_markets.assert_not_called()

    def test_ai_quota_failure_in_trade_analysis_skips_market_not_failed(self):
        from agents.application.trade import Trader

        with patch("agents.application.trade.Polymarket") as PMock, \
                patch("agents.application.trade.Agent") as AgentMock, \
                patch("agents.application.trade.Gamma"):
            pm = PMock.return_value
            pm.get_all_tradeable_events.return_value = []
            pm.get_usdc_balance.return_value = 50.0

            agent = AgentMock.return_value
            agent.filter_events_with_rag.return_value = []
            agent.map_filtered_events_to_markets.return_value = []
            agent.filter_markets.return_value = [self._make_market(202, 0.05)]
            agent.source_best_trade.side_effect = RuntimeError(
                "Error code: 429 - insufficient_quota"
            )

            tl = TradeLog(self.db_path)
            gate = RiskGate(
                trade_log=tl, polymarket=pm,
                starting_balance_usdc=0.0,
                max_daily_loss_pct=0.99,
                max_trades_per_hour=99,
                min_usdc_floor=0.0,
                max_daily_token_usd=999.0,
                kill_switch_file=self.kill_path,
                llm_usage_file=self.usage_path,
            )
            trader = Trader(
                dry_run=False,
                top_n=1,
                max_trades_per_cycle=1,
                min_confidence=0.7,
                max_position_fraction=0.1,
                trade_log=tl,
                risk_gate=gate,
            )

            trader.one_best_trade_sweep()

        recent = tl.recent(limit=1)[0]
        self.assertEqual(recent["status"], SKIPPED_GATE)
        self.assertIn("ai_analysis_unavailable", recent["error"])


if __name__ == "__main__":
    unittest.main()
