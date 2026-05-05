import os
import tempfile
import unittest

from agents.application.trade_log import TradeLog
from agents.application.scalper_pairs import ScalperPairsDAO, ScalperState


class TestScalperPairsDAO(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = TradeLog(db_path=self.tmp.name)
        self.dao = ScalperPairsDAO(self.log)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.tmp.name + suffix
            if os.path.exists(p):
                os.unlink(p)

    def test_create_pair_inserts_tracking_row(self):
        self.dao.create("btc-updown-15m-100", 100, "tok_up", "tok_dn")
        row = self.dao.get_by_slug("btc-updown-15m-100")
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], ScalperState.TRACKING)
        self.assertEqual(row["qty_up"], 0.0)
        self.assertEqual(row["attempts_up"], 0)

    def test_create_is_idempotent(self):
        self.dao.create("s1", 1, "u", "d")
        self.dao.create("s1", 1, "u", "d")  # second call no-ops
        rows = self.dao.list_open()
        self.assertEqual(len(rows), 1)

    def test_record_fill_updates_qty_and_cost(self):
        self.dao.create("s1", 1, "u", "d")
        self.dao.record_fill("s1", "up", qty=5.0, cost_usdc=2.50)
        row = self.dao.get_by_slug("s1")
        self.assertEqual(row["qty_up"], 5.0)
        self.assertEqual(row["cost_up"], 2.50)
        self.assertEqual(row["attempts_up"], 1)

    def test_set_state_transitions(self):
        self.dao.create("s1", 1, "u", "d")
        self.dao.set_state("s1", ScalperState.LEG1_FILLED)
        self.assertEqual(self.dao.get_by_slug("s1")["state"], "leg1_filled")
        self.dao.set_state("s1", ScalperState.BOTH_FILLED)
        self.assertEqual(self.dao.get_by_slug("s1")["state"], "both_filled")

    def test_list_open_excludes_terminal_states(self):
        self.dao.create("s1", 1, "u", "d")
        self.dao.create("s2", 1, "u", "d")
        self.dao.set_state("s2", ScalperState.REDEEMED)
        open_rows = self.dao.list_open()
        slugs = [r["slug"] for r in open_rows]
        self.assertIn("s1", slugs)
        self.assertNotIn("s2", slugs)

    def test_record_fill_accumulates(self):
        self.dao.create("s1", 1, "u", "d")
        self.dao.record_fill("s1", "up", qty=5.0, cost_usdc=2.50)
        self.dao.record_fill("s1", "up", qty=3.0, cost_usdc=1.50)
        row = self.dao.get_by_slug("s1")
        self.assertAlmostEqual(row["qty_up"], 8.0)
        self.assertAlmostEqual(row["cost_up"], 4.00)
        self.assertEqual(row["attempts_up"], 2)

    def test_set_state_terminal_sets_closed_ts(self):
        self.dao.create("s1", 1, "u", "d")
        self.dao.set_state("s1", ScalperState.REDEEMED)
        row = self.dao.get_by_slug("s1")
        self.assertIsNotNone(row["closed_ts"])
        self.assertGreater(row["closed_ts"], 0)

    def test_list_recent_returns_pairs_newest_first(self):
        self.dao.create("s1", 1, "u", "d")
        self.dao.create("s2", 2, "u", "d")
        rows = self.dao.list_recent(limit=10)
        self.assertGreaterEqual(len(rows), 2)
        slugs = [r["slug"] for r in rows]
        self.assertIn("s1", slugs)
        self.assertIn("s2", slugs)


if __name__ == "__main__":
    unittest.main()
