"""Tests for resolution_sync dust terminator (P3).

The dust terminator auto-marks dust-on-chain positions whose markets
haven't been resolved by Gamma as resolved_loss when older than the
threshold. Without this they accumulate forever in
position_manager's "dust_market_open" bucket and skew the calibrator.
"""
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.application.resolution_sync import ResolutionConfig, ResolutionSync
from agents.application.trade_log import FILLED, TradeLog


def _seed_filled(log: TradeLog, token_id: str, *, age_hours: float) -> None:
    """Insert a FILLED row at the given age."""
    ts = (
        datetime.now(timezone.utc) - timedelta(hours=age_hours)
    ).isoformat()
    with log._lock, log._connect() as conn:
        conn.execute(
            "INSERT INTO trades (ts, cycle_id, market_id, token_id, side, "
            "price, size_usdc, status, confidence) VALUES (?, ?, ?, ?, ?, ?, "
            "?, ?, ?)",
            (ts, "test", "M1", token_id, "BUY", 0.5, 1.0, FILLED, 0.5),
        )


class DustTerminatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "trade_log.db")
        self.log = TradeLog(db_path=self.db_path)
        # Mock polymarket — _on_chain_shares returns 0 (dust)
        self.poly = MagicMock()
        self.cfg = ResolutionConfig(
            dust_shares_floor=0.5,
            dust_terminator_enabled=True,
            dust_terminator_age_hours=24,
            swarm_sync_enabled=False,
        )
        self.rs = ResolutionSync(
            polymarket=self.poly,
            trade_log=self.log,
            cfg=self.cfg,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _patch_on_chain_zero(self):
        self.rs._on_chain_shares = lambda token_id: 0.0
        self.rs._gamma_resolution = lambda token_id: None  # gamma silent

    def test_dust_under_24h_left_alone(self):
        _seed_filled(self.log, "T_YOUNG", age_hours=10)
        self._patch_on_chain_zero()
        outcome, why = self.rs._classify_token_v2("T_YOUNG")
        self.assertIsNone(outcome)
        self.assertEqual(why, "dust_market_open")

    def test_dust_over_24h_terminated(self):
        _seed_filled(self.log, "T_OLD", age_hours=72)
        self._patch_on_chain_zero()
        outcome, why = self.rs._classify_token_v2("T_OLD")
        self.assertIsNotNone(outcome)
        self.assertEqual(why, "dust_terminated")
        self.assertEqual(outcome["status_key"], "resolved_loss")

    def test_still_held_skips_dust_terminator(self):
        _seed_filled(self.log, "T_HELD", age_hours=72)
        self.rs._on_chain_shares = lambda token_id: 10.0  # above floor
        outcome, why = self.rs._classify_token_v2("T_HELD")
        self.assertIsNone(outcome)
        self.assertEqual(why, "still_held")

    def test_terminator_disabled_skips(self):
        cfg = ResolutionConfig(
            dust_shares_floor=0.5,
            dust_terminator_enabled=False,
            dust_terminator_age_hours=24,
            swarm_sync_enabled=False,
        )
        rs = ResolutionSync(polymarket=self.poly, trade_log=self.log, cfg=cfg)
        rs._on_chain_shares = lambda token_id: 0.0
        rs._gamma_resolution = lambda token_id: None
        _seed_filled(self.log, "T_OLD", age_hours=72)
        outcome, why = rs._classify_token_v2("T_OLD")
        self.assertIsNone(outcome)
        self.assertEqual(why, "dust_market_open")

    def test_terminator_runs_full_pipeline(self):
        """End-to-end: dust + age > 24h → resolved_loss row written."""
        _seed_filled(self.log, "T_OLD", age_hours=48)
        self._patch_on_chain_zero()
        # Mock _record_resolution to capture call
        captured = {}
        def fake_record(token_id, outcome):
            captured["token_id"] = token_id
            captured["status_key"] = outcome["status_key"]
        self.rs._record_resolution = fake_record
        result = self.rs.run_once()
        self.assertEqual(result["resolved_loss"], 1)
        self.assertEqual(captured.get("token_id"), "T_OLD")
        self.assertEqual(captured.get("status_key"), "resolved_loss")


if __name__ == "__main__":
    unittest.main()
