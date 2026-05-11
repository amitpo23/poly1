import json
import os
import tempfile
import unittest

from agents.application.trade_log import TradeLog


class TestBrainJournal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = TradeLog(db_path=self.tmp.name)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.tmp.name + suffix
            if os.path.exists(p):
                os.unlink(p)

    def test_insert_and_read_brain_decision(self):
        row_id = self.log.insert_brain_decision(
            agent="scalper",
            strategy="crypto_15m",
            decision_type="entry",
            market_id="eth-updown-15m-1770000000",
            token_id="tok",
            approved=True,
            reason="approved",
            score=0.61,
            market_type="crypto_15m",
            asset="eth",
            features={"candidate_price": 0.47, "signal_reason": "reversal"},
            action="BUY_UP",
        )
        self.assertGreater(row_id, 0)
        row = self.log.recent_brain_decisions()[0]
        self.assertEqual(row["agent"], "scalper")
        self.assertEqual(row["approved"], 1)
        self.assertEqual(row["reason"], "approved")
        self.assertEqual(row["asset"], "eth")
        self.assertEqual(json.loads(row["features_json"])["candidate_price"], 0.47)

    def test_update_brain_decision_outcome(self):
        row_id = self.log.insert_brain_decision(
            agent="scalper",
            strategy="crypto_15m",
            decision_type="entry",
            market_id="m1",
            approved=False,
            reason="pair_ask_sum_too_expensive",
            score=0.0,
        )
        self.log.update_brain_decision_outcome(
            row_id,
            outcome_status="would_have_missed",
            outcome={"mfe_pct": 0.0},
        )
        row = self.log.recent_brain_decisions()[0]
        self.assertEqual(row["outcome_status"], "would_have_missed")
        self.assertEqual(json.loads(row["outcome_json"])["mfe_pct"], 0.0)

    def test_insert_and_read_decision_reflection(self):
        row_id = self.log.insert_brain_decision(
            agent="research_committee",
            strategy="market_maker",
            decision_type="research",
            market_id="m2",
            approved=False,
            reason="paper_trade_only",
            score=0.52,
        )
        reflection_id = self.log.insert_decision_reflection(
            decision_id=row_id,
            agent="research_committee",
            strategy="market_maker",
            market_id="m2",
            lesson_type="execution_risk",
            lesson="Wide spreads need paper execution evidence before live capital.",
            outcome_status="paper_only",
            metrics={"risk_score": 0.58},
        )
        self.assertGreater(reflection_id, 0)
        row = self.log.recent_decision_reflections()[0]
        self.assertEqual(row["agent"], "research_committee")
        self.assertEqual(row["lesson_type"], "execution_risk")
        self.assertEqual(json.loads(row["metrics_json"])["risk_score"], 0.58)


if __name__ == "__main__":
    unittest.main()
