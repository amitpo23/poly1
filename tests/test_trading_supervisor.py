from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agents.application.trade_log import BTC_DAILY_OPEN, FILLED, TradeLog
from agents.application.trading_supervisor import (
    TradingSupervisor,
    TradingSupervisorConfig,
)


class TestTradingSupervisor(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = str(self.root / "trade_log.db")
        self.halt_path = str(self.root / "HALT")
        self.hb_path = self.root / "position_manager_heartbeat"
        self.supervisor_hb = str(self.root / "supervisor_heartbeat")
        self.state_path = str(self.root / "supervisor_status.json")
        self.log = TradeLog(self.db_path)
        self.hb_path.touch()

    def tearDown(self):
        self._tmp.cleanup()

    def _cfg(self, **overrides):
        defaults = dict(
            poll_seconds=60,
            heartbeat_path=self.supervisor_hb,
            state_path=self.state_path,
            position_manager_heartbeat_path=str(self.hb_path),
            kill_switch_file=self.halt_path,
            stale_heartbeat_seconds=180,
            evaluation_grace_seconds=180,
            min_position_age_seconds=45,
            close_failed_window_minutes=15,
            close_failed_threshold=5,
            enforce_halt=True,
        )
        defaults.update(overrides)
        return TradingSupervisorConfig(**defaults)

    def _insert_open(self, token_id="TOK", status=FILLED, seconds_ago=600):
        trade_id = self.log.insert_terminal(
            cycle_id="entry",
            market_id="M1",
            token_id=token_id,
            side="BUY",
            price=0.50,
            size_usdc=5.0,
            confidence=0.8,
            status=status,
        )
        ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
        with self.log._connect() as conn:
            conn.execute("UPDATE trades SET ts = ? WHERE id = ?", (ts, trade_id))
        return trade_id, ts

    def _insert_position_manager_evidence(self, token_id="TOK", seconds_ago=30):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
        self.log.upsert_position_mark(
            token_id=token_id,
            market_id="M1",
            entry_price=0.50,
            current_price=0.51,
            shares=10,
            status="open",
        )
        with self.log._connect() as conn:
            conn.execute(
                "UPDATE position_marks SET first_seen_ts=?, last_seen_ts=? WHERE token_id=?",
                (ts, ts, token_id),
            )
            conn.execute(
                """
                INSERT INTO brain_decisions
                    (ts, agent, strategy, decision_type, market_id, token_id,
                     approved, reason, score, action)
                VALUES (?, 'position_manager', 'position_exit', 'exit',
                        'M1', ?, 0, 'hold', 0.0, 'HOLD')
                """,
                (ts, token_id),
            )

    def test_no_open_positions_is_ok(self):
        result = TradingSupervisor(self.log, self._cfg()).run_once()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(Path(self.halt_path).exists())

    def test_recent_open_position_gets_grace_period(self):
        self._insert_open(seconds_ago=10)
        result = TradingSupervisor(self.log, self._cfg()).run_once()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(Path(self.halt_path).exists())

    def test_old_open_without_exit_evidence_halts(self):
        self._insert_open(seconds_ago=600)
        result = TradingSupervisor(self.log, self._cfg()).run_once()
        codes = {i["code"] for i in result["issues"]}
        self.assertEqual(result["status"], "critical")
        self.assertIn("open_position_without_position_mark", codes)
        self.assertIn("open_position_without_exit_decision", codes)
        self.assertTrue(Path(self.halt_path).exists())

    def test_open_position_with_fresh_exit_evidence_is_ok(self):
        self._insert_open(seconds_ago=600)
        self._insert_position_manager_evidence(seconds_ago=30)
        result = TradingSupervisor(self.log, self._cfg()).run_once()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(Path(self.halt_path).exists())

    def test_stale_position_manager_heartbeat_halts_when_positions_are_open(self):
        self._insert_open(seconds_ago=600)
        old = time.time() - 600
        self.hb_path.touch()
        import os
        os.utime(self.hb_path, (old, old))
        result = TradingSupervisor(self.log, self._cfg()).run_once()
        codes = {i["code"] for i in result["issues"]}
        self.assertIn("position_manager_heartbeat_stale", codes)
        self.assertTrue(Path(self.halt_path).exists())

    def test_old_terminal_row_does_not_hide_reentry(self):
        self._insert_open(token_id="TOK", seconds_ago=1200)
        self.log.insert_terminal(
            cycle_id="old-close",
            market_id="M1",
            token_id="TOK",
            side="SELL",
            price=0.50,
            size_usdc=0.5,
            status="closed_dust",
        )
        new_id, _ = self._insert_open(
            token_id="TOK", status=BTC_DAILY_OPEN, seconds_ago=600
        )

        result = TradingSupervisor(self.log, self._cfg()).run_once()
        issue_ids = {i.get("trade_id") for i in result["issues"]}
        self.assertEqual(result["status"], "critical")
        self.assertIn(new_id, issue_ids)


if __name__ == "__main__":
    unittest.main()
