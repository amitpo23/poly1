"""Tests for scripts/backfill_brain_decisions_outcomes.py + TradeLog.annotate_brain_decisions_for_close."""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.application.trade_log import TradeLog
from scripts.backfill_brain_decisions_outcomes import backfill


def _seed_brain_decision(log: TradeLog, *, market_id: str, token_id: str,
                          ts: datetime, signal_source: str = "test_source") -> int:
    """Insert a brain_decision row with custom ts (bypasses log helper which
    uses now()). Returns the row id."""
    with log._lock, log._connect() as conn:
        cur = conn.execute(
            """INSERT INTO brain_decisions (
                ts, agent, strategy, decision_type, market_id, token_id, action,
                approved, reason, score, market_type, asset, features_json,
                signal_source
            ) VALUES (?, 'market_scanner', 'scanner_trade_opportunity',
                'entry', ?, ?, 'BUY', 1, 'test', 0.8, 'general_binary',
                NULL, '{}', ?)""",
            (ts.isoformat(), market_id, token_id, signal_source),
        )
        return int(cur.lastrowid)


def _seed_trade(log: TradeLog, *, market_id: str, token_id: str,
                ts: datetime, status: str, pnl: float = -0.05) -> None:
    with log._lock, log._connect() as conn:
        conn.execute(
            """INSERT INTO trades (
                ts, cycle_id, market_id, token_id, side, price, size_usdc,
                status, response_json
            ) VALUES (?, 'test_cycle', ?, ?, 'SELL', 0.5, 1.0, ?, ?)""",
            (
                ts.isoformat(),
                market_id,
                token_id,
                status,
                json.dumps({"pnl_usdc_real": pnl}),
            ),
        )


class AnnotateBrainDecisionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "trade_log.db")
        self.log = TradeLog(db_path=self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_annotate_matches_market_and_token(self):
        now = datetime.now(timezone.utc)
        dec_id = _seed_brain_decision(
            self.log, market_id="M1", token_id="T1", ts=now - timedelta(minutes=30)
        )
        n = self.log.annotate_brain_decisions_for_close(
            market_id="M1", token_id="T1",
            outcome_status="closed_take_profit", pnl_usdc=0.31,
        )
        self.assertEqual(n, 1)
        # Verify the row was actually updated
        with self.log._connect() as conn:
            row = conn.execute(
                "SELECT outcome_status, outcome_json FROM brain_decisions WHERE id = ?",
                (dec_id,),
            ).fetchone()
        self.assertEqual(row["outcome_status"], "closed_take_profit")
        payload = json.loads(row["outcome_json"])
        self.assertEqual(payload["pnl_usdc_real"], 0.31)

    def test_annotate_skips_already_annotated(self):
        now = datetime.now(timezone.utc)
        dec_id = _seed_brain_decision(
            self.log, market_id="M1", token_id="T1", ts=now - timedelta(minutes=30)
        )
        # First call annotates
        self.log.annotate_brain_decisions_for_close(
            market_id="M1", token_id="T1",
            outcome_status="closed_take_profit", pnl_usdc=0.31,
        )
        # Second call should skip (idempotent — outcome_status no longer NULL)
        n = self.log.annotate_brain_decisions_for_close(
            market_id="M1", token_id="T1",
            outcome_status="closed_stop_loss", pnl_usdc=-0.5,
        )
        self.assertEqual(n, 0)
        # Original annotation preserved
        with self.log._connect() as conn:
            row = conn.execute(
                "SELECT outcome_status FROM brain_decisions WHERE id = ?",
                (dec_id,),
            ).fetchone()
        self.assertEqual(row["outcome_status"], "closed_take_profit")

    def test_annotate_respects_max_age(self):
        now = datetime.now(timezone.utc)
        # Old decision — beyond max_age_hours
        _seed_brain_decision(
            self.log, market_id="M1", token_id="T1",
            ts=now - timedelta(hours=100),
        )
        n = self.log.annotate_brain_decisions_for_close(
            market_id="M1", token_id="T1",
            outcome_status="closed_take_profit", pnl_usdc=0.31,
            max_age_hours=72,
        )
        self.assertEqual(n, 0)

    def test_annotate_matches_market_only_when_token_id_none(self):
        now = datetime.now(timezone.utc)
        _seed_brain_decision(
            self.log, market_id="M1", token_id="T1", ts=now - timedelta(minutes=10)
        )
        _seed_brain_decision(
            self.log, market_id="M1", token_id="T2", ts=now - timedelta(minutes=10)
        )
        # With no token_id — both rows for M1 should match
        n = self.log.annotate_brain_decisions_for_close(
            market_id="M1", token_id=None,
            outcome_status="resolved_loss", pnl_usdc=-1.0,
        )
        self.assertEqual(n, 2)


class BackfillScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "trade_log.db")
        self.log = TradeLog(db_path=self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_backfill_annotates_brain_decisions(self):
        now = datetime.now(timezone.utc)
        _seed_brain_decision(
            self.log, market_id="M1", token_id="T1",
            ts=now - timedelta(minutes=30), signal_source="alphainsider",
        )
        _seed_trade(
            self.log, market_id="M1", token_id="T1",
            ts=now - timedelta(minutes=10), status="closed_take_profit", pnl=0.31,
        )
        result = backfill(
            self.db_path, days=7, max_match_age_hours=24, dry_run=False
        )
        self.assertEqual(result["scanned_closes"], 1)
        self.assertEqual(result["annotated_decisions"], 1)
        self.assertEqual(result["by_status"], {"closed_take_profit": 1})

    def test_backfill_dry_run_does_not_write(self):
        now = datetime.now(timezone.utc)
        dec_id = _seed_brain_decision(
            self.log, market_id="M1", token_id="T1",
            ts=now - timedelta(minutes=30),
        )
        _seed_trade(
            self.log, market_id="M1", token_id="T1",
            ts=now - timedelta(minutes=10), status="closed_stop_loss",
        )
        result = backfill(
            self.db_path, days=7, max_match_age_hours=24, dry_run=True
        )
        # Counter says 1 would be annotated...
        self.assertEqual(result["annotated_decisions"], 1)
        # ...but actually nothing was written
        with self.log._connect() as conn:
            row = conn.execute(
                "SELECT outcome_status FROM brain_decisions WHERE id = ?",
                (dec_id,),
            ).fetchone()
        self.assertIsNone(row["outcome_status"])

    def test_backfill_idempotent(self):
        now = datetime.now(timezone.utc)
        _seed_brain_decision(
            self.log, market_id="M1", token_id="T1", ts=now - timedelta(minutes=30)
        )
        _seed_trade(
            self.log, market_id="M1", token_id="T1",
            ts=now - timedelta(minutes=10), status="closed_take_profit",
        )
        r1 = backfill(self.db_path, days=7, max_match_age_hours=24, dry_run=False)
        r2 = backfill(self.db_path, days=7, max_match_age_hours=24, dry_run=False)
        self.assertEqual(r1["annotated_decisions"], 1)
        # Second run: 0 new annotations (already done)
        self.assertEqual(r2["annotated_decisions"], 0)


if __name__ == "__main__":
    unittest.main()
