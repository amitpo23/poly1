"""Tests for agents/application/multi_pipeline_calibrator.py."""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.application.multi_pipeline_calibrator import (
    _agent_from_cycle_id,
    _direct_execution_stats,
    _shadow_research_stats,
    multi_pipeline_calibrate,
)


def _seed_db(tmp_path: Path) -> str:
    db = tmp_path / "trade_log.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE brain_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, agent TEXT, market_id TEXT, token_id TEXT,
            action TEXT, approved INTEGER, market_type TEXT,
            features_json TEXT, signal_source TEXT,
            decision_type TEXT DEFAULT 'entry'
        );
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, cycle_id TEXT, market_id TEXT, token_id TEXT,
            side TEXT, price REAL, size_usdc REAL,
            status TEXT, response_json TEXT
        );
        """
    )
    return str(db)


def _add_trade(conn, ts, cycle_id, market_id, token_id, status,
               side="BUY", price=0.5, size=1.0, pnl=0.0):
    conn.execute(
        "INSERT INTO trades (ts, cycle_id, market_id, token_id, side, price, "
        "size_usdc, status, response_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts.isoformat(), cycle_id, market_id, token_id, side, price, size,
         status, json.dumps({"pnl_usdc_real": pnl})),
    )


class AgentFromCycleIdTests(unittest.TestCase):
    def test_recognized_prefixes(self):
        self.assertEqual(_agent_from_cycle_id("btc_5min:1234"), "btc_5min")
        self.assertEqual(_agent_from_cycle_id("scalper:abc"), "scalper")
        self.assertEqual(_agent_from_cycle_id("btc_daily:5"), "btc_daily")
        self.assertEqual(_agent_from_cycle_id("scanner_executor:99"), "scanner_executor")

    def test_close_rows_return_none(self):
        self.assertIsNone(_agent_from_cycle_id("close:abc123"))
        self.assertIsNone(_agent_from_cycle_id("resolution_sync"))

    def test_unrecognized_returns_none(self):
        self.assertIsNone(_agent_from_cycle_id(""))
        self.assertIsNone(_agent_from_cycle_id(None))
        self.assertIsNone(_agent_from_cycle_id("random_string"))


class DirectExecutionStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _seed_db(Path(self.tmp.name))
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_btc5min_winrate_and_pnl(self):
        # 2 wins, 1 loss for btc_5min
        for i, (status, pnl) in enumerate([
            ("filled", 0), ("closed_take_profit", 0.05),
            ("filled", 0), ("closed_take_profit", 0.04),
            ("filled", 0), ("closed_stop_loss", -0.06),
        ]):
            _add_trade(
                self.conn,
                self.now - timedelta(minutes=30 + i),
                f"btc_5min:{i}" if status == "filled" else f"close:{i//2}",
                f"M_{i//2}",
                f"T_{i//2}",
                status, pnl=pnl,
            )
        self.conn.commit()
        stats = _direct_execution_stats(self.conn, days=7)
        btc = next(s for s in stats if s["key"] == "btc_5min")
        self.assertEqual(btc["wins"], 2)
        self.assertEqual(btc["losses"], 1)
        self.assertAlmostEqual(btc["sum_win_pnl_usdc"], 0.09, places=4)
        self.assertAlmostEqual(btc["sum_loss_pnl_usdc"], -0.06, places=4)

    def test_unrecognized_cycle_id_skipped(self):
        _add_trade(
            self.conn, self.now - timedelta(minutes=30),
            "random_unrecognized_cycle", "M1", "T1", "filled",
        )
        _add_trade(
            self.conn, self.now - timedelta(minutes=10),
            "close:abc", "M1", "T1", "closed_stop_loss", pnl=-0.05,
        )
        self.conn.commit()
        stats = _direct_execution_stats(self.conn, days=7)
        # Should be empty — no recognized agent
        self.assertEqual(stats, [])

    def test_multiple_agents_separated(self):
        _add_trade(self.conn, self.now - timedelta(minutes=30),
                    "btc_5min:1", "M_BTC", "T_BTC", "filled")
        _add_trade(self.conn, self.now - timedelta(minutes=10),
                    "close:1", "M_BTC", "T_BTC", "closed_take_profit", pnl=0.05)
        _add_trade(self.conn, self.now - timedelta(minutes=30),
                    "scalper:1", "M_SC", "T_SC", "filled")
        _add_trade(self.conn, self.now - timedelta(minutes=10),
                    "close:2", "M_SC", "T_SC", "closed_stop_loss", pnl=-0.05)
        self.conn.commit()
        stats = _direct_execution_stats(self.conn, days=7)
        agents = {s["key"] for s in stats}
        self.assertEqual(agents, {"btc_5min", "scalper"})


class ShadowResearchStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _seed_db(Path(self.tmp.name))
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_aggregates_per_agent(self):
        for i in range(5):
            self.conn.execute(
                "INSERT INTO brain_decisions (ts, agent, market_id, token_id, "
                "approved) VALUES (?, ?, ?, ?, 1)",
                (
                    (self.now - timedelta(minutes=30 + i)).isoformat(),
                    "external_conviction_alpaca",
                    f"M_{i}",
                    f"T_{i}",
                ),
            )
        self.conn.commit()
        stats = _shadow_research_stats(self.conn, days=7)
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["key"], "external_conviction_alpaca")
        self.assertEqual(stats[0]["approvals"], 5)


class MultiPipelineCalibrateIntegrationTests(unittest.TestCase):
    def test_combined_output_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _seed_db(Path(tmp))
            now = datetime.now(timezone.utc)
            conn = sqlite3.connect(db)
            # Direct execution agent: btc_5min with 1 close
            _add_trade(conn, now - timedelta(minutes=30),
                        "btc_5min:1", "M1", "T1", "filled")
            _add_trade(conn, now - timedelta(minutes=10),
                        "close:1", "M1", "T1", "closed_take_profit", pnl=0.05)
            # Shadow research: external_conviction approval
            conn.execute(
                "INSERT INTO brain_decisions (ts, agent, market_id, token_id, "
                "approved, action) VALUES (?, ?, ?, ?, 1, 'BUY')",
                (now.isoformat(), "external_conviction_alpaca", "M2", "T2"),
            )
            conn.commit()
            conn.close()

            result = multi_pipeline_calibrate(db, days=7, max_age_hours=24)
            self.assertIn("per_direct_execution_agent", result)
            self.assertIn("shadow_research_visibility", result)
            # btc_5min should appear in direct execution
            btc_agents = {a["key"] for a in result["per_direct_execution_agent"]}
            self.assertIn("btc_5min", btc_agents)
            # external_conviction should appear in shadow research
            shadow_agents = {a["key"] for a in result["shadow_research_visibility"]}
            self.assertIn("external_conviction_alpaca", shadow_agents)


if __name__ == "__main__":
    unittest.main()
