"""Tests for agents/application/consensus_router.py"""
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.application.consensus_router import (
    DEFAULT_EXCLUDED_AGENTS,
    ConsensusResult,
    query,
)


def _setup_db() -> sqlite3.Connection:
    """In-memory DB with the minimum brain_decisions columns the router reads."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE brain_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            agent TEXT,
            market_id TEXT,
            approved INTEGER,
            action TEXT,
            signal_source TEXT
        );
        """
    )
    return conn


def _insert(conn, *, ts: datetime, agent: str, market_id: str,
            approved: int = 1, action: str = "BUY",
            signal_source: str = "test") -> None:
    conn.execute(
        "INSERT INTO brain_decisions(ts, agent, market_id, approved, action, signal_source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ts.isoformat(), agent, market_id, approved, action, signal_source),
    )


class ConsensusRouterTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
        self.db = _setup_db()

    def tearDown(self):
        self.db.close()

    def test_no_approvals_returns_no_consensus(self):
        r = query(self.db, "M1", now=self.now)
        self.assertFalse(r.consensus)
        self.assertEqual(r.agents, ())
        self.assertEqual(r.actions, ())

    def test_single_agent_no_consensus(self):
        _insert(self.db, ts=self.now - timedelta(seconds=10),
                agent="market_scanner", market_id="M1")
        r = query(self.db, "M1", now=self.now)
        self.assertFalse(r.consensus)
        self.assertEqual(r.agents, ("market_scanner",))

    def test_two_distinct_agents_within_window_yields_consensus(self):
        _insert(self.db, ts=self.now - timedelta(seconds=120),
                agent="market_scanner", market_id="M1", action="BUY")
        _insert(self.db, ts=self.now - timedelta(seconds=60),
                agent="opportunity_factory", market_id="M1", action="BUY")
        r = query(self.db, "M1", now=self.now)
        self.assertTrue(r.consensus)
        self.assertEqual(r.agents, ("market_scanner", "opportunity_factory"))
        self.assertEqual(r.actions, ("BUY",))
        self.assertTrue(r.as_features()["consensus_directional_agreement"])

    def test_same_agent_twice_does_not_count(self):
        _insert(self.db, ts=self.now - timedelta(seconds=120),
                agent="market_scanner", market_id="M1")
        _insert(self.db, ts=self.now - timedelta(seconds=60),
                agent="market_scanner", market_id="M1")
        r = query(self.db, "M1", now=self.now)
        self.assertFalse(r.consensus)
        self.assertEqual(r.agents, ("market_scanner",))

    def test_approval_outside_window_excluded(self):
        _insert(self.db, ts=self.now - timedelta(seconds=600),  # 10 min ago
                agent="market_scanner", market_id="M1")
        _insert(self.db, ts=self.now - timedelta(seconds=60),
                agent="opportunity_factory", market_id="M1")
        r = query(self.db, "M1", now=self.now, window_seconds=300)
        self.assertFalse(r.consensus)
        self.assertEqual(r.agents, ("opportunity_factory",))

    def test_excluded_agents_do_not_count(self):
        # scanner_executor is excluded by default — its own approvals
        # are not cross-source confirmation.
        _insert(self.db, ts=self.now - timedelta(seconds=120),
                agent="scanner_executor", market_id="M1")
        _insert(self.db, ts=self.now - timedelta(seconds=60),
                agent="market_scanner", market_id="M1")
        r = query(self.db, "M1", now=self.now)
        self.assertFalse(r.consensus)
        self.assertEqual(r.agents, ("market_scanner",))

    def test_rejected_decisions_do_not_count(self):
        _insert(self.db, ts=self.now - timedelta(seconds=120),
                agent="market_scanner", market_id="M1", approved=1)
        _insert(self.db, ts=self.now - timedelta(seconds=60),
                agent="opportunity_factory", market_id="M1", approved=0)
        r = query(self.db, "M1", now=self.now)
        self.assertFalse(r.consensus)

    def test_different_market_does_not_count(self):
        _insert(self.db, ts=self.now - timedelta(seconds=60),
                agent="market_scanner", market_id="M1")
        _insert(self.db, ts=self.now - timedelta(seconds=60),
                agent="opportunity_factory", market_id="M2")
        r = query(self.db, "M1", now=self.now)
        self.assertFalse(r.consensus)
        self.assertEqual(r.agents, ("market_scanner",))

    def test_directional_disagreement_still_yields_consensus_flag(self):
        """Two agents agreeing on the MARKET but on opposite SIDES is
        still 'consensus' for routing purposes. The features expose this
        disagreement so the caller can choose to discount it."""
        _insert(self.db, ts=self.now - timedelta(seconds=120),
                agent="market_scanner", market_id="M1", action="BUY")
        _insert(self.db, ts=self.now - timedelta(seconds=60),
                agent="opportunity_factory", market_id="M1", action="SELL")
        r = query(self.db, "M1", now=self.now)
        self.assertTrue(r.consensus)
        self.assertEqual(r.actions, ("BUY", "SELL"))
        self.assertFalse(r.as_features()["consensus_directional_agreement"])

    def test_min_agents_three_requires_three(self):
        _insert(self.db, ts=self.now - timedelta(seconds=120),
                agent="market_scanner", market_id="M1")
        _insert(self.db, ts=self.now - timedelta(seconds=60),
                agent="opportunity_factory", market_id="M1")
        r = query(self.db, "M1", now=self.now, min_agents=3)
        self.assertFalse(r.consensus)
        # Add the third agent
        _insert(self.db, ts=self.now - timedelta(seconds=30),
                agent="btc_5min", market_id="M1")
        r2 = query(self.db, "M1", now=self.now, min_agents=3)
        self.assertTrue(r2.consensus)

    def test_features_payload_contract(self):
        _insert(self.db, ts=self.now - timedelta(seconds=120),
                agent="market_scanner", market_id="M1", action="BUY",
                signal_source="meta_brain")
        _insert(self.db, ts=self.now - timedelta(seconds=60),
                agent="opportunity_factory", market_id="M1", action="BUY",
                signal_source="alphainsider")
        f = query(self.db, "M1", now=self.now).as_features()
        self.assertEqual(f["consensus"], True)
        self.assertEqual(f["consensus_agent_count"], 2)
        self.assertEqual(set(f["consensus_agents"]), {"market_scanner", "opportunity_factory"})
        self.assertEqual(set(f["consensus_sources"]), {"meta_brain", "alphainsider"})
        self.assertEqual(f["consensus_window_sec"], 300)
        self.assertTrue(f["consensus_directional_agreement"])

    def test_database_error_falls_back_to_no_consensus(self):
        """If brain_decisions table is missing or unreadable, return False
        (never crash scanner_executor)."""
        broken_db = sqlite3.connect(":memory:")  # no tables
        r = query(broken_db, "M1", now=self.now)
        self.assertFalse(r.consensus)
        self.assertEqual(r.agents, ())
        broken_db.close()


if __name__ == "__main__":
    unittest.main()
