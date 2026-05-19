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


# ---------------------------------------------------------------------------
# Stage-0 calibration loop: _annotate_brain_decisions
# ---------------------------------------------------------------------------

class TestAnnotateBrainDecisions(unittest.TestCase):
    """Verify that resolution_sync._annotate_brain_decisions fills
    outcome_status on brain_decisions rows (Stage 0 of the calibration loop).
    These tests use no network and no polymarket client.
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = TradeLog(db_path=self.tmp.name)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.tmp.name + suffix
            if os.path.exists(p):
                os.unlink(p)

    def _make_rs(self):
        from unittest.mock import MagicMock
        from agents.application.resolution_sync import ResolutionSync, ResolutionConfig
        return ResolutionSync(
            polymarket=MagicMock(),
            trade_log=self.log,
            cfg=ResolutionConfig(enabled=True),
        )

    def _insert_decision(self, market_id: str, score: float = 0.70) -> int:
        return self.log.insert_brain_decision(
            agent="trader", strategy="general_binary",
            decision_type="entry", market_id=market_id,
            approved=True, reason="approved", score=score,
            market_type="general_binary",
        )

    def test_resolved_yes_annotates_decision(self):
        d_id = self._insert_decision("mkt1")
        self._make_rs()._annotate_brain_decisions(
            "mkt1", "resolved_yes", {"pnl": 2.5}
        )
        row = self.log.recent_brain_decisions()[0]
        self.assertEqual(row["outcome_status"], "resolved_yes")
        self.assertEqual(json.loads(row["outcome_json"])["pnl"], 2.5)

    def test_annotation_is_idempotent(self):
        """Second call with different outcome must not overwrite the first."""
        self._insert_decision("mkt2")
        rs = self._make_rs()
        rs._annotate_brain_decisions("mkt2", "resolved_yes", {"pnl": 1.0})
        rs._annotate_brain_decisions("mkt2", "resolved_loss", {"pnl": -1.0})
        row = self.log.recent_brain_decisions()[0]
        self.assertEqual(row["outcome_status"], "resolved_yes")  # unchanged

    def test_annotates_all_unannotated_rows_for_market(self):
        """Multiple decisions for the same market are all annotated in one call."""
        for score in (0.80, 0.75, 0.65):
            self._insert_decision("mkt3", score=score)
        self._make_rs()._annotate_brain_decisions("mkt3", "resolved_loss", {"pnl": -2.0})
        rows = [r for r in self.log.recent_brain_decisions() if r["market_id"] == "mkt3"]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["outcome_status"] == "resolved_loss" for r in rows))

    def test_unrelated_market_not_touched(self):
        self._insert_decision("mkt_a")
        self._insert_decision("mkt_b")
        self._make_rs()._annotate_brain_decisions("mkt_a", "resolved_yes", {})
        rows = self.log.recent_brain_decisions()
        by_market = {r["market_id"]: r["outcome_status"] for r in rows}
        self.assertEqual(by_market["mkt_a"], "resolved_yes")
        self.assertIsNone(by_market["mkt_b"])


# ---------------------------------------------------------------------------
# WinRateAdvisor outcome-classification correctness
# ---------------------------------------------------------------------------

class TestWinRateAdvisorOutcomes(unittest.TestCase):
    """resolved_no = held NO token that won → payout=1 → this is a WIN."""

    def test_resolved_no_classified_as_win(self):
        from agents.application.meta_brain import WinRateAdvisor
        self.assertIn("resolved_no", WinRateAdvisor.WIN_OUTCOMES)
        self.assertNotIn("resolved_no", WinRateAdvisor.LOSS_OUTCOMES)

    def test_resolved_loss_classified_as_loss(self):
        from agents.application.meta_brain import WinRateAdvisor
        self.assertIn("resolved_loss", WinRateAdvisor.LOSS_OUTCOMES)
        self.assertNotIn("resolved_loss", WinRateAdvisor.WIN_OUTCOMES)

    def test_winrate_computed_from_annotated_decisions(self):
        """End-to-end: annotate brain_decisions → WinRateAdvisor reads correct rate."""
        from unittest.mock import patch
        from agents.application.meta_brain import WinRateAdvisor
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            log = TradeLog(db_path=tmp.name)
            # 3 wins (yes + yes + no-side-win), 2 losses
            for score, outcome in [
                (0.80, "resolved_yes"),
                (0.75, "resolved_yes"),
                (0.70, "resolved_no"),    # bought NO, resolved NO → WIN
                (0.65, "resolved_loss"),
                (0.60, "resolved_loss"),
            ]:
                d_id = log.insert_brain_decision(
                    agent="trader", strategy="general_binary",
                    decision_type="entry", market_id=f"mkt_{score}",
                    approved=True, reason="approved", score=score,
                    market_type="general_binary",
                )
                log.update_brain_decision_outcome(d_id, outcome, {})

            advisor = WinRateAdvisor(cache_ttl_sec=0)
            # Disable daily-journal blending so the result comes purely from
            # brain_decisions and we can assert the exact ratio.
            with patch.dict(os.environ, {"META_BRAIN_DAILY_JOURNAL_ENABLED": "false"}):
                stats = advisor.compute(tmp.name, market_type="general_binary", hours=9999)
            self.assertEqual(stats.wins, 3)
            self.assertEqual(stats.losses, 2)
            self.assertAlmostEqual(stats.winrate, 3 / 5, places=5)
            self.assertEqual(stats.source, "brain_decisions")
        finally:
            for suffix in ("", "-wal", "-shm"):
                p = tmp.name + suffix
                if os.path.exists(p):
                    os.unlink(p)


if __name__ == "__main__":
    unittest.main()

