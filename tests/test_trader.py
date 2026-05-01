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
        pm = MagicMock()
        pm.get_usdc_balance.return_value = 80.0
        gate = self._gate(polymarket=pm, starting_balance_usdc=100.0,
                          max_daily_loss_pct=0.10)
        self.assertFalse(gate.ok())
        self.assertIn("drawdown", gate.reason())

    def test_passes_when_clean(self):
        pm = MagicMock()
        pm.get_usdc_balance.return_value = 100.0
        gate = self._gate(polymarket=pm)
        self.assertTrue(gate.ok())


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

    def test_buy_picks_yes_token_with_anchor_price(self):
        from agents.polymarket.polymarket import Polymarket

        pm = Polymarket.__new__(Polymarket)
        pm.client = MagicMock()
        pm.client.create_market_order.return_value = "signed"
        pm.client.post_order.return_value = {"orderID": "ord123", "status": "submitted"}

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
        self.assertEqual(result["order_price"], 0.55)
        self.assertEqual(result["side_recommended"], "BUY")
        # Verify MarketOrderArgs received the YES token's price (no inversion).
        args = pm.client.create_market_order.call_args[0][0]
        self.assertEqual(args.price, 0.55)
        self.assertEqual(args.token_id, "yes_tok")

    def test_sell_picks_no_token_and_inverts_price(self):
        from agents.polymarket.polymarket import Polymarket

        pm = Polymarket.__new__(Polymarket)
        pm.client = MagicMock()
        pm.client.create_market_order.return_value = "signed"
        pm.client.post_order.return_value = {"orderID": "ord456", "status": "submitted"}

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
        self.assertAlmostEqual(result["order_price"], 0.6)
        args = pm.client.create_market_order.call_args[0][0]
        self.assertAlmostEqual(args.price, 0.6)
        self.assertEqual(args.token_id, "no_tok")

    def test_rejects_non_binary_market(self):
        from agents.polymarket.polymarket import Polymarket

        pm = Polymarket.__new__(Polymarket)
        pm.client = MagicMock()

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


if __name__ == "__main__":
    unittest.main()
