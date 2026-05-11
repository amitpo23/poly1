import sqlite3
import tempfile
import unittest
from pathlib import Path

from agents.application.capital_allocator import (
    CapitalAllocator,
    MarketIntelligenceSnapshot,
)
from agents.application.trade_log import TradeLog


class TestCapitalAllocator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.poly_db = str(self.root / "trade_log.db")
        self.swarm_db = str(self.root / "swarm.db")
        # Force exploration mode OFF for legacy tests — exploration is an
        # opt-in feature; tests that predate it assume the strict gating.
        # Tests that exercise exploration set the env explicitly.
        import os
        self._saved_exploration_env = os.environ.pop("ALLOCATOR_EXPLORATION_USDC", None)

    def tearDown(self):
        self.tmp.cleanup()
        import os
        if self._saved_exploration_env is not None:
            os.environ["ALLOCATOR_EXPLORATION_USDC"] = self._saved_exploration_env

    def test_scores_and_allocates_clean_active_agent(self):
        log = TradeLog(self.poly_db)
        cycle = log.new_cycle_id()
        log.insert_terminal(
            cycle,
            "btc-updown-15m-test",
            "scalper_leg",
            side="BUY",
            price=0.45,
            size_usdc=2.5,
            error="SHADOW: would have fired",
        )
        log.insert_brain_decision(
            agent="scalper",
            strategy="crypto_15m",
            decision_type="entry",
            market_id="btc-updown-15m-test",
            approved=True,
            reason="edge",
            score=0.7,
        )

        report = CapitalAllocator(
            poly_db=self.poly_db,
            swarm_db=self.swarm_db,
            total_budget_usdc=20.0,
        ).build_report()

        by_agent = {s.agent: s for s in report.agents}
        self.assertGreater(by_agent["scalper"].score, 0)
        self.assertTrue(by_agent["scalper"].live_allowed)
        self.assertGreater(by_agent["scalper"].recommended_usdc, 0)

    def test_stale_state_blocks_live_allocation(self):
        log = TradeLog(self.poly_db)
        with sqlite3.connect(self.poly_db) as conn:
            conn.execute(
                """
                INSERT INTO scalper_pairs
                (slug, period_ts, up_token, down_token, cost_up, cost_down,
                 attempts_up, attempts_down, state, opened_ts)
                VALUES ('s', 1, 'u', 'd', 2.5, 0, 1, 0, 'reconcile_needed', 1)
                """
            )
            conn.commit()

        report = CapitalAllocator(
            poly_db=self.poly_db,
            swarm_db=self.swarm_db,
            total_budget_usdc=20.0,
        ).build_report()
        by_agent = {s.agent: s for s in report.agents}
        self.assertFalse(by_agent["scalper"].live_allowed)
        self.assertEqual(by_agent["scalper"].recommended_usdc, 0)

    def test_reads_swarm_orders(self):
        with sqlite3.connect(self.swarm_db) as conn:
            conn.executescript(
                """
                CREATE TABLE pending_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    size_usd REAL NOT NULL,
                    price_cents REAL,
                    status TEXT NOT NULL,
                    order_id TEXT,
                    created_ms INTEGER NOT NULL,
                    updated_ms INTEGER NOT NULL,
                    note TEXT
                );
                INSERT INTO pending_orders
                (agent, market_id, side, outcome, size_usd, price_cents,
                 status, order_id, created_ms, updated_ms, note)
                VALUES
                ('market_maker', 'm1', 'BUY', 'YES', 5.0, 45.0,
                 'submitted', 'dry_1', 1893456000000, 1893456000000, 'dry');
                """
            )
            conn.commit()

        report = CapitalAllocator(
            poly_db=self.poly_db,
            swarm_db=self.swarm_db,
            total_budget_usdc=20.0,
            window_hours=24 * 3650,
        ).build_report()
        by_agent = {s.agent: s for s in report.agents}
        self.assertEqual(by_agent["swarm_market_maker"].entries, 1)
        self.assertEqual(by_agent["swarm_market_maker"].stale_state, 1)
        self.assertFalse(by_agent["swarm_market_maker"].live_allowed)

    def test_veto_only_agent_gets_no_live_budget(self):
        log = TradeLog(self.poly_db)
        cycle = log.new_cycle_id()
        for market_id in ("m1", "m2", "m3"):
            log.insert_terminal(
                cycle,
                market_id,
                "skipped_dedupe",
                error="already holds filled position on this market",
            )

        report = CapitalAllocator(
            poly_db=self.poly_db,
            swarm_db=self.swarm_db,
            total_budget_usdc=20.0,
        ).build_report()
        by_agent = {s.agent: s for s in report.agents}
        self.assertFalse(by_agent["trader"].live_allowed)
        self.assertEqual(by_agent["trader"].recommended_usdc, 0)

    def test_market_intelligence_boosts_crypto_agent_score(self):
        class _Intel:
            def snapshot(self, **kwargs):
                return MarketIntelligenceSnapshot(
                    crypto={"btc": {"price": 100.0, "fresh": True}},
                    gamma_crypto_markets=4,
                    gamma_avg_liquidity_usd=20_000.0,
                    gamma_avg_volume_24h_usd=8_000.0,
                    fresh_brain_approvals=2,
                )

        log = TradeLog(self.poly_db)
        cycle = log.new_cycle_id()
        log.insert_terminal(
            cycle,
            "btc-updown-15m-test",
            "scalper_leg",
            side="BUY",
            price=0.45,
            size_usdc=2.5,
        )

        report = CapitalAllocator(
            poly_db=self.poly_db,
            swarm_db=self.swarm_db,
            total_budget_usdc=20.0,
            market_intelligence=_Intel(),
        ).build_report()
        by_agent = {s.agent: s for s in report.agents}
        self.assertGreater(by_agent["scalper"].market_score, 0)
        self.assertIn("btc", report.market_intelligence["crypto"])

    def test_db_only_mode_disables_market_score(self):
        class _Intel:
            def snapshot(self, **kwargs):
                raise AssertionError("should not call market intelligence")

        log = TradeLog(self.poly_db)
        cycle = log.new_cycle_id()
        log.insert_terminal(
            cycle,
            "btc-updown-15m-test",
            "scalper_leg",
            side="BUY",
            price=0.45,
            size_usdc=2.5,
        )

        report = CapitalAllocator(
            poly_db=self.poly_db,
            swarm_db=self.swarm_db,
            total_budget_usdc=20.0,
            include_market_intelligence=False,
            market_intelligence=_Intel(),
        ).build_report()
        by_agent = {s.agent: s for s in report.agents}
        self.assertEqual(by_agent["scalper"].market_score, 0)


if __name__ == "__main__":
    unittest.main()
