import os
import tempfile
import unittest
from unittest.mock import MagicMock

from agents.application.scalper import ScalperEngine, ScalperConfig, ScalpPair
from agents.application.scalper_pairs import ScalperPairsDAO, ScalperState
from agents.application.trade_log import TradeLog, SCALPER_LEG


class TestPlaceLeg(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = TradeLog(db_path=self.tmp.name)
        self.dao = ScalperPairsDAO(self.log)
        self.client = MagicMock()
        self.cfg = ScalperConfig()
        self.engine = ScalperEngine(client=self.client, log=self.log,
                                      dao=self.dao, cfg=self.cfg, execute=True)
        self.dao.create("s1", 100, "tok_up", "tok_dn")

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.tmp.name + suffix
            if os.path.exists(p):
                os.unlink(p)

    def test_place_leg_writes_to_dao_and_trades(self):
        self.client.execute_market_order = MagicMock(return_value={
            "status": "filled",
            "amount_usdc": 5.0,
            "order_avg_price_estimate": 0.45,
            "token_id": "tok_up",
            "order_id": "abc",
        })
        result = self.engine.place_leg(slug="s1", side="up",
                                          token="tok_up", usdc=5.0,
                                          intended_price=0.45)
        self.assertTrue(result["filled"])
        row = self.dao.get_by_slug("s1")
        self.assertGreater(row["qty_up"], 0)
        self.assertAlmostEqual(row["cost_up"], 5.0)
        self.assertEqual(row["attempts_up"], 1)
        recent = self.log.recent(limit=5)
        self.assertEqual(recent[0]["status"], SCALPER_LEG)
        self.assertEqual(recent[0]["market_id"], "s1")

    def test_place_leg_handles_failure(self):
        self.client.execute_market_order = MagicMock(side_effect=ValueError("FOK kill"))
        result = self.engine.place_leg(slug="s1", side="up",
                                          token="tok_up", usdc=5.0,
                                          intended_price=0.45)
        self.assertFalse(result["filled"])
        row = self.dao.get_by_slug("s1")
        self.assertEqual(row["qty_up"], 0)
        self.assertEqual(row["attempts_up"], 1)


class TestMarketDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = TradeLog(db_path=self.tmp.name)
        self.dao = ScalperPairsDAO(self.log)
        self.gamma = MagicMock()
        self.engine = ScalperEngine(client=MagicMock(), log=self.log,
                                      dao=self.dao, cfg=ScalperConfig(),
                                      gamma=self.gamma, execute=False)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.tmp.name + suffix
            if os.path.exists(p):
                os.unlink(p)

    def test_discover_filters_to_updown_15m_only(self):
        self.gamma.get_events_by_tag = MagicMock(return_value=[
            {"slug": "btc-updown-15m-100", "markets": [
                {"slug": "btc-updown-15m-100",
                 "clobTokenIds": "['tok_up', 'tok_dn']",
                 "outcomes": "['Up', 'Down']",
                 "endDate": "2026-05-05T12:15:00Z",
                 "acceptingOrders": True}]},
            {"slug": "trump-2028", "markets": [{"slug": "trump-2028"}]},
            {"slug": "eth-updown-5m-50", "markets": [{"slug": "eth-updown-5m-50"}]},
        ])
        slugs = self.engine.discover_markets()
        self.assertEqual([m["slug"] for m in slugs], ["btc-updown-15m-100"])

    def test_discover_skips_not_accepting_orders(self):
        self.gamma.get_events_by_tag = MagicMock(return_value=[
            {"slug": "btc-updown-15m-100", "markets": [
                {"slug": "btc-updown-15m-100", "acceptingOrders": False,
                 "clobTokenIds": "['tok_up', 'tok_dn']",
                 "outcomes": "['Up', 'Down']",
                 "endDate": "2026-05-05T12:15:00Z"}]},
        ])
        self.assertEqual(self.engine.discover_markets(), [])

    def test_discover_creates_pair_in_dao(self):
        self.gamma.get_events_by_tag = MagicMock(return_value=[
            {"slug": "btc-updown-15m-100", "markets": [
                {"slug": "btc-updown-15m-100", "acceptingOrders": True,
                 "clobTokenIds": "['tok_up', 'tok_dn']",
                 "outcomes": "['Up', 'Down']",
                 "endDate": "2026-05-05T12:15:00Z"}]},
        ])
        self.engine.discover_markets()
        self.assertIsNotNone(self.dao.get_by_slug("btc-updown-15m-100"))


class TestTickLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = TradeLog(db_path=self.tmp.name)
        self.dao = ScalperPairsDAO(self.log)
        self.client = MagicMock()
        self.cfg = ScalperConfig(leg_usdc_cap=5.0)
        self.engine = ScalperEngine(client=self.client, log=self.log,
                                      dao=self.dao, cfg=self.cfg, execute=True)
        self.dao.create("s1", 100, "tok_up", "tok_dn")
        self.engine.add_pair(ScalpPair(slug="s1", period_ts=100,
                                          up_token="tok_up", down_token="tok_dn",
                                          cfg=self.cfg))

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.tmp.name + suffix
            if os.path.exists(p):
                os.unlink(p)

    def test_tick_below_threshold_sets_temp_only(self):
        self.engine.tick(slug="s1", up_ask=0.45, down_ask=0.50, now_ms=1000)
        self.client.execute_market_order.assert_not_called()
        self.assertEqual(self.engine.pairs["s1"].temp_price_up, 0.45)

    def test_tick_reversal_fires_leg1(self):
        self.client.execute_market_order = MagicMock(return_value={
            "status": "filled", "amount_usdc": 5.0,
            "order_avg_price_estimate": 0.471, "token_id": "tok_up",
            "order_id": "x",
        })
        self.engine.tick(slug="s1", up_ask=0.45, down_ask=0.50, now_ms=1000)
        self.engine.tick(slug="s1", up_ask=0.471, down_ask=0.50, now_ms=1100)
        self.client.execute_market_order.assert_called_once()
        row = self.dao.get_by_slug("s1")
        self.assertEqual(row["state"], ScalperState.LEG1_FILLED)
        self.assertGreater(row["qty_up"], 0)

    def test_tick_no_repeat_after_max_attempts(self):
        self.client.execute_market_order = MagicMock(
            side_effect=ValueError("FOK kill"))
        for i in range(6):
            self.engine.tick(slug="s1", up_ask=0.45, down_ask=0.50, now_ms=1000+i*10)
            self.engine.tick(slug="s1", up_ask=0.471, down_ask=0.50, now_ms=1010+i*10)
        self.assertEqual(self.client.execute_market_order.call_count, 4)


if __name__ == "__main__":
    unittest.main()
