import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.performance_tearsheet import build_report, format_markdown


class TestPerformanceTearsheet(unittest.TestCase):
    def test_build_report_groups_pnl_and_markouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "trade_log.db"
            with sqlite3.connect(db) as conn:
                conn.executescript(
                    """
                    CREATE TABLE trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        cycle_id TEXT NOT NULL,
                        market_id TEXT NOT NULL,
                        token_id TEXT,
                        side TEXT,
                        price REAL,
                        size_usdc REAL,
                        confidence REAL,
                        status TEXT NOT NULL,
                        response_json TEXT,
                        error TEXT
                    );
                    CREATE TABLE decision_journal (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        decision_id INTEGER,
                        agent TEXT NOT NULL,
                        strategy TEXT NOT NULL,
                        market_id TEXT NOT NULL,
                        token_id TEXT,
                        action TEXT,
                        decision TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        signal_source TEXT,
                        market_price REAL,
                        live_entry_price REAL,
                        internal_probability REAL,
                        raw_ev REAL,
                        net_ev REAL,
                        score REAL,
                        mode TEXT,
                        features_json TEXT,
                        outcome_5m_json TEXT,
                        outcome_15m_json TEXT,
                        outcome_60m_json TEXT,
                        outcome_status TEXT,
                        outcome_1m_json TEXT,
                        outcome_3m_json TEXT
                    );
                    CREATE TABLE brain_decisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        agent TEXT NOT NULL,
                        strategy TEXT NOT NULL,
                        decision_type TEXT NOT NULL,
                        market_id TEXT NOT NULL,
                        token_id TEXT,
                        approved INTEGER NOT NULL,
                        reason TEXT NOT NULL,
                        score REAL NOT NULL,
                        market_type TEXT,
                        asset TEXT,
                        features_json TEXT,
                        action TEXT,
                        outcome_status TEXT,
                        outcome_json TEXT,
                        signal_source TEXT
                    );
                    """
                )
                conn.execute(
                    """
                    INSERT INTO trades
                    (ts, cycle_id, market_id, token_id, side, price, size_usdc, confidence, status, response_json, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2099-01-01T00:00:00+00:00",
                        "scanner_executor",
                        "m1",
                        "t1",
                        "BUY",
                        0.45,
                        2.0,
                        0.7,
                        "closed_take_profit",
                        json.dumps({"pnl_usdc_real": 0.2, "agent": "scanner_executor"}),
                        "",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO trades
                    (ts, cycle_id, market_id, token_id, side, price, size_usdc, confidence, status, response_json, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2099-01-01T00:01:00+00:00",
                        "scanner_executor",
                        "m2",
                        "t2",
                        "SELL",
                        0.62,
                        2.0,
                        0.7,
                        "closed_stop_loss",
                        json.dumps({"pnl_usdc_real": -0.1, "agent": "scanner_executor"}),
                        "",
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO decision_journal
                    (ts, agent, strategy, market_id, decision, reason, signal_source, market_price,
                     live_entry_price, internal_probability, raw_ev, net_ev, score, mode,
                     outcome_1m_json, outcome_3m_json, outcome_5m_json, outcome_15m_json,
                     outcome_60m_json, outcome_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2099-01-01T00:00:00+00:00",
                        "scanner_executor",
                        "proof",
                        "m1",
                        "LIVE_ENTER",
                        "approved",
                        "alpha",
                        0.44,
                        0.45,
                        0.6,
                        0.3,
                        0.2,
                        0.8,
                        "live",
                        json.dumps({"pnl_pct": 0.02}),
                        None,
                        json.dumps({"pnl_pct": 0.04}),
                        None,
                        None,
                        None,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO brain_decisions
                    (ts, agent, strategy, decision_type, market_id, token_id, approved, reason,
                     score, market_type, asset, features_json, action, outcome_status, outcome_json, signal_source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2099-01-01T00:00:00+00:00",
                        "scanner_executor",
                        "proof",
                        "entry",
                        "m1",
                        "t1",
                        1,
                        "approved",
                        0.8,
                        "crypto",
                        "BTC",
                        "{}",
                        "BUY",
                        None,
                        None,
                        "alpha",
                    ),
                )

            report = build_report(str(db), hours=24 * 365 * 100)
            self.assertTrue(report["ok"])
            self.assertEqual(report["summary"]["trades"], 2)
            self.assertAlmostEqual(report["summary"]["live_stats"]["pnl_usdc"], 0.1)
            self.assertEqual(report["summary"]["live_stats"]["wins"], 1)
            self.assertEqual(report["summary"]["live_stats"]["losses"], 1)
            self.assertEqual(report["summary"]["live_stats"]["max_loss_streak"], 1)
            self.assertEqual(report["summary"]["live_stats"]["expected_shortfall_95_usdc"], -0.1)
            self.assertEqual(report["decision_groups"]["by_signal_source"][0]["key"], "alpha")
            self.assertAlmostEqual(report["decision_groups"]["by_signal_source"][0]["avg_markout_pct"], 0.03)
            self.assertAlmostEqual(report["decision_groups"]["by_signal_source"][0]["avg_price_edge"], 0.15)
            self.assertAlmostEqual(report["decision_groups"]["by_signal_source"][0]["avg_spread_proxy"], 0.01)
            self.assertIn("Performance tear sheet", format_markdown(report))


if __name__ == "__main__":
    unittest.main()
