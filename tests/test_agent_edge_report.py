"""Tests for scripts/agent_edge_report.py — hypothetical edge computation."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.agent_edge_report import (
    _hypothetical_pnl,
    _in_profit_band,
    collect,
    render_markdown,
)


class TestHypotheticalPnL(unittest.TestCase):
    def test_buy_profit(self):
        # Entered at 0.40, exit at best_bid 0.50 → +25%
        self.assertAlmostEqual(_hypothetical_pnl("BUY", 0.40, 0.50), 0.25, places=4)

    def test_buy_loss(self):
        self.assertAlmostEqual(_hypothetical_pnl("BUY", 0.50, 0.40), -0.20, places=4)

    def test_sell_profit(self):
        # SELL @ 0.55 owns NO at 0.45; exit at YES best_bid 0.45 → NO exit 0.55 → +22.2%
        self.assertAlmostEqual(_hypothetical_pnl("SELL", 0.55, 0.45), 0.55 / 0.45 - 1, places=4)

    def test_sell_loss(self):
        # SELL @ 0.45 owns NO at 0.55; YES bid moves UP to 0.60 → NO worth 0.40 → −27.3%
        self.assertAlmostEqual(_hypothetical_pnl("SELL", 0.45, 0.60), 0.4 / 0.55 - 1, places=4)

    def test_invalid_inputs(self):
        self.assertIsNone(_hypothetical_pnl("BUY", None, 0.5))
        self.assertIsNone(_hypothetical_pnl("BUY", 0.5, None))
        self.assertIsNone(_hypothetical_pnl("BUY", -0.1, 0.5))
        self.assertIsNone(_hypothetical_pnl("BUY", 0.5, 1.5))
        self.assertIsNone(_hypothetical_pnl("UNKNOWN", 0.5, 0.5))


class TestProfitBand(unittest.TestCase):
    def test_buy_band(self):
        self.assertTrue(_in_profit_band("BUY", 0.40))
        self.assertTrue(_in_profit_band("BUY", 0.49))
        self.assertFalse(_in_profit_band("BUY", 0.39))
        self.assertFalse(_in_profit_band("BUY", 0.50))

    def test_sell_band(self):
        self.assertTrue(_in_profit_band("SELL", 0.51))
        self.assertTrue(_in_profit_band("SELL", 0.60))
        self.assertFalse(_in_profit_band("SELL", 0.50))
        self.assertFalse(_in_profit_band("SELL", 0.61))


class TestCollectFromFixture(unittest.TestCase):
    def _build_db(self, tmp_path):
        db_path = Path(tmp_path) / "trade_log.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """
            CREATE TABLE brain_decisions (
                id INTEGER PRIMARY KEY,
                ts TEXT, agent TEXT, approved INTEGER
            );
            CREATE TABLE decision_journal (
                id INTEGER PRIMARY KEY,
                ts TEXT,
                decision TEXT,
                agent TEXT,
                signal_source TEXT,
                action TEXT,
                live_entry_price REAL,
                market_price REAL,
                outcome_1m_json TEXT,
                outcome_3m_json TEXT,
                outcome_5m_json TEXT,
                outcome_15m_json TEXT,
                outcome_60m_json TEXT
            );
            """
        )
        now = "2026-05-24T00:00:00+00:00"
        conn.executemany(
            "INSERT INTO brain_decisions(ts, agent, approved) VALUES (?, ?, 1)",
            [(now, "market_scanner"), (now, "market_scanner"), (now, "scalper")],
        )
        conn.execute(
            "INSERT INTO decision_journal(ts, decision, agent, signal_source, action, "
            "live_entry_price, outcome_5m_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                now,
                "ENTER",
                "scanner_executor",
                "test_source_winner",
                "BUY",
                0.45,
                json.dumps({"best_bid": 0.55}),
            ),
        )
        conn.execute(
            "INSERT INTO decision_journal(ts, decision, agent, signal_source, action, "
            "live_entry_price, outcome_5m_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                now,
                "ENTER",
                "scanner_executor",
                "test_source_loser",
                "BUY",
                0.50,
                json.dumps({"best_bid": 0.45}),
            ),
        )
        conn.commit()
        conn.close()
        return str(db_path)

    def test_collect_and_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._build_db(tmp)
            data = collect(db, days=30)
            self.assertEqual(data["approvals_per_agent"]["market_scanner"], 2)
            self.assertEqual(data["approvals_per_agent"]["scalper"], 1)
            winner = data["edge_per_source"]["test_source_winner"]
            self.assertEqual(winner["wins"], 1)
            self.assertEqual(winner["in_band"], 1)
            self.assertAlmostEqual(winner["sum_pnl_pct"], 0.55 / 0.45 - 1, places=4)
            loser = data["edge_per_source"]["test_source_loser"]
            self.assertEqual(loser["losses"], 1)
            self.assertEqual(loser["in_band"], 0)
            md = render_markdown(data)
            self.assertIn("test_source_winner", md)
            self.assertIn("Agent Edge Report", md)


if __name__ == "__main__":
    unittest.main()
