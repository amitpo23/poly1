"""Tests for the orderbook_monitor shadow-decision watchlist extension
added 2026-05-24.

After Round 12, calibration audit revealed: 14 SHADOW_ENTER tokens,
only 2 had any orderbook_snapshots, 0 had complete markouts. The
monitor was polling its curated universe + open positions but not
the markets we'd written shadow decisions on. Without snapshots,
update_shadow_markouts.py can't compute synthetic PnL → the
Bayesian calibration sample stays starved.

This commit:
1. Adds TradeLog.recent_shadow_decision_tokens(max_age_hours)
2. Wires it into OrderbookMonitor._tokens() as a third watchlist source

These tests verify the wiring without spinning up a real CLOB
fetcher.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.application.trade_log import TradeLog


def _seed(db_path: str, rows: list[dict]) -> None:
    """Write decision_journal rows directly (TradeLog API isn't exposed
    for raw inserts, but the schema is created on connection)."""
    TradeLog(db_path=db_path)  # creates schema
    with sqlite3.connect(db_path) as conn:
        for r in rows:
            conn.execute(
                "INSERT INTO decision_journal "
                "(ts, agent, strategy, market_id, token_id, decision, reason, score) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r["ts"], r.get("agent", "test"), "s", r.get("market_id", "M"),
                    r.get("token_id"), r["decision"], r.get("reason", ""), 0.5,
                ),
            )
        conn.commit()


class RecentShadowDecisionTokensTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmpdir.name, "test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_returns_shadow_enter_tokens(self):
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(hours=2)).isoformat()
        _seed(self.db, [
            {"ts": recent, "token_id": "T1", "decision": "SHADOW_ENTER"},
            {"ts": recent, "token_id": "T2", "decision": "SHADOW_QUOTE"},
        ])
        tl = TradeLog(db_path=self.db)
        result = tl.recent_shadow_decision_tokens(max_age_hours=24)
        tokens = {r["token_id"] for r in result}
        self.assertEqual(tokens, {"T1", "T2"})

    def test_excludes_old_entries(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        _seed(self.db, [
            {"ts": old, "token_id": "OLD", "decision": "SHADOW_ENTER"},
        ])
        tl = TradeLog(db_path=self.db)
        result = tl.recent_shadow_decision_tokens(max_age_hours=24)
        self.assertEqual(result, [])

    def test_excludes_reject_decisions(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _seed(self.db, [
            {"ts": recent, "token_id": "REJ", "decision": "REJECT"},
            {"ts": recent, "token_id": "ENT", "decision": "ENTER"},
        ])
        tl = TradeLog(db_path=self.db)
        result = tl.recent_shadow_decision_tokens(max_age_hours=24)
        # Only SHADOW_* decisions count.
        self.assertEqual(result, [])

    def test_excludes_fully_marked_out(self):
        """If all 4 markout columns are populated, no need to keep polling."""
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _seed(self.db, [
            {"ts": recent, "token_id": "FULL", "decision": "SHADOW_ENTER"},
        ])
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "UPDATE decision_journal SET "
                "outcome_1m_json='{}', outcome_3m_json='{}', "
                "outcome_5m_json='{}', outcome_15m_json='{}' "
                "WHERE token_id='FULL'"
            )
            conn.commit()
        tl = TradeLog(db_path=self.db)
        result = tl.recent_shadow_decision_tokens(max_age_hours=24)
        self.assertEqual(result, [])

    def test_dedups_by_token_id(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _seed(self.db, [
            {"ts": recent, "token_id": "DUP", "decision": "SHADOW_ENTER"},
            {"ts": recent, "token_id": "DUP", "decision": "SHADOW_ENTER"},
            {"ts": recent, "token_id": "DUP", "decision": "SHADOW_QUOTE"},
        ])
        tl = TradeLog(db_path=self.db)
        result = tl.recent_shadow_decision_tokens(max_age_hours=24)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["token_id"], "DUP")


class MonitorWatchlistTests(unittest.TestCase):
    """Verify OrderbookMonitor._tokens() includes shadow-decision tokens."""

    def test_tokens_method_calls_shadow_lookup(self):
        from agents.application.orderbook_monitor import (
            OrderbookMonitorDaemon,
            OrderbookMonitorConfig,
        )
        mon = OrderbookMonitorDaemon.__new__(OrderbookMonitorDaemon)
        mon.cfg = OrderbookMonitorConfig(
            token_limit=10, shadow_lookback_hours=24, watch_5min_crypto=False
        )
        mon.trade_log = MagicMock()
        mon.trade_log.market_universe_tokens.return_value = []
        mon.trade_log.filled_positions_with_id.return_value = []
        mon.trade_log.recent_shadow_decision_tokens.return_value = [
            {"market_id": "M1", "token_id": "SHADOW1"},
            {"market_id": "M2", "token_id": "SHADOW2"},
        ]
        result = mon._tokens()
        tokens = [r["token_id"] for r in result]
        self.assertIn("SHADOW1", tokens)
        self.assertIn("SHADOW2", tokens)
        # Each came tagged with the shadow_research outcome marker.
        outcomes = {r.get("outcome") for r in result}
        self.assertIn("shadow_research", outcomes)

    def test_dedup_across_sources(self):
        from agents.application.orderbook_monitor import (
            OrderbookMonitorDaemon,
            OrderbookMonitorConfig,
        )
        mon = OrderbookMonitorDaemon.__new__(OrderbookMonitorDaemon)
        mon.cfg = OrderbookMonitorConfig(token_limit=10, watch_5min_crypto=False)
        mon.trade_log = MagicMock()
        mon.trade_log.market_universe_tokens.return_value = [
            {"market_id": "M1", "token_id": "T1"},
        ]
        mon.trade_log.filled_positions_with_id.return_value = []
        # Same token also showing up as shadow — should not duplicate.
        mon.trade_log.recent_shadow_decision_tokens.return_value = [
            {"market_id": "M1", "token_id": "T1"},
        ]
        result = mon._tokens()
        tokens = [r["token_id"] for r in result]
        self.assertEqual(tokens.count("T1"), 1)

    def test_handles_attribute_error_gracefully(self):
        """If TradeLog is older and missing the new method, monitor
        should keep working with just the universe + filled sources."""
        from agents.application.orderbook_monitor import (
            OrderbookMonitorDaemon,
            OrderbookMonitorConfig,
        )
        mon = OrderbookMonitorDaemon.__new__(OrderbookMonitorDaemon)
        mon.cfg = OrderbookMonitorConfig(token_limit=10, watch_5min_crypto=False)
        mon.trade_log = MagicMock()
        mon.trade_log.market_universe_tokens.return_value = [
            {"market_id": "M1", "token_id": "UNI"},
        ]
        mon.trade_log.filled_positions_with_id.return_value = []
        # Simulate missing method on older TradeLog.
        mon.trade_log.recent_shadow_decision_tokens.side_effect = AttributeError()
        result = mon._tokens()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["token_id"], "UNI")


if __name__ == "__main__":
    unittest.main()
