from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from agents.application.agent_registry import load_agent_registry
from agents.application.meta_arbiter import ArbiterConfig, MetaArbiter
from agents.application.signal_contract import SignalEnvelope
from agents.application.strategy_scorecard import build_strategy_scorecard


class AgentRegistryTests(unittest.TestCase):
    def test_registry_loads_and_has_command_roles(self):
        registry = load_agent_registry("config/agent_registry.json")
        summary = registry.summary()

        self.assertGreaterEqual(summary["agent_count"], 14)
        self.assertIn("scanner_executor", summary["live_capable_agents"])
        self.assertIn("crypto_5m_market_maker_shadow", summary["anchor_capable_agents"])
        self.assertIn("external_conviction_openbb", summary["anchor_capable_agents"])
        self.assertEqual(registry.require("risk_gate").role, "risk_manager")

    def test_money_touching_agents_require_brain_approval(self):
        registry = load_agent_registry("config/agent_registry.json")

        for spec in registry.agents.values():
            if spec.places_orders and spec.agent_id != "position_manager":
                self.assertTrue(spec.requires_brain_approval, spec.agent_id)


class SignalEnvelopeTests(unittest.TestCase):
    def test_invalid_direction_rejected(self):
        with self.assertRaises(ValueError):
            SignalEnvelope(
                agent_id="external_conviction_alpaca",
                market_id="m1",
                direction="sideways-ish",
                confidence=0.7,
            )

    def test_stale_detection(self):
        signal = SignalEnvelope(
            agent_id="external_conviction_alpaca",
            market_id="m1",
            direction="yes",
            confidence=0.7,
            created_ts=time.time() - 20,
            stale_after_sec=5,
        )
        self.assertTrue(signal.stale)


class MetaArbiterTests(unittest.TestCase):
    def _arbiter(self):
        return MetaArbiter(cfg=ArbiterConfig(round_trip_cost_pct=0.02, min_net_ev=0.02))

    def test_anchor_can_approve_without_average_dilution(self):
        arbiter = self._arbiter()
        signal = SignalEnvelope(
            agent_id="external_conviction_crypto_tape",
            market_id="btc-5m",
            direction="up",
            confidence=0.70,
            probability=0.62,
            anchor=True,
            reason="strong tape",
        )

        decision = arbiter.decide(
            market_id="btc-5m",
            entry_price=0.52,
            signals=[signal],
            exit_plan={"stop_loss_pct": 0.03, "take_profit_pct": 0.05},
            max_size_usdc=1.0,
        )

        self.assertTrue(decision.approved)
        self.assertEqual(decision.reason, "anchor_approved")
        self.assertEqual(decision.primary_trigger, "external_conviction_crypto_tape")
        self.assertEqual(decision.mode, "anchor")

    def test_anchor_blocks_when_ev_after_costs_is_negative(self):
        arbiter = self._arbiter()
        signal = SignalEnvelope(
            agent_id="external_conviction_crypto_tape",
            market_id="btc-5m",
            direction="up",
            confidence=0.80,
            probability=0.54,
            anchor=True,
        )

        decision = arbiter.decide(
            market_id="btc-5m",
            entry_price=0.53,
            signals=[signal],
            exit_plan={"stop_loss_pct": 0.03, "take_profit_pct": 0.05},
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "anchor_net_ev_too_low")

    def test_veto_blocks_even_with_good_signal(self):
        arbiter = self._arbiter()
        good = SignalEnvelope(
            agent_id="external_conviction_crypto_tape",
            market_id="btc-5m",
            direction="up",
            confidence=0.72,
            probability=0.64,
            anchor=True,
        )
        veto = SignalEnvelope(
            agent_id="external_conviction_alpaca",
            market_id="btc-5m",
            direction="down",
            confidence=0.90,
            veto=True,
            reason="conflicting external macro tape",
        )

        decision = arbiter.decide(
            market_id="btc-5m",
            entry_price=0.50,
            signals=[good, veto],
            exit_plan={"stop_loss_pct": 0.03, "take_profit_pct": 0.05},
        )

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "strong_veto")
        self.assertIn("external_conviction_alpaca:veto", decision.vetoes)

    def test_missing_exit_plan_blocks(self):
        arbiter = self._arbiter()
        signal = SignalEnvelope(
            agent_id="external_conviction_crypto_tape",
            market_id="btc-5m",
            direction="up",
            confidence=0.72,
            probability=0.64,
            anchor=True,
        )

        decision = arbiter.decide(market_id="btc-5m", entry_price=0.50, signals=[signal])

        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "missing_exit_plan")


class StrategyScorecardTests(unittest.TestCase):
    def test_scorecard_promotes_only_positive_markout_with_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "trade_log.db"
            with sqlite3.connect(db) as conn:
                conn.execute(
                    """
                    CREATE TABLE decision_journal (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        agent TEXT,
                        strategy TEXT,
                        decision TEXT,
                        score REAL,
                        outcome_1m_json TEXT,
                        outcome_3m_json TEXT,
                        outcome_5m_json TEXT,
                        outcome_15m_json TEXT
                    )
                    """
                )
                for _ in range(3):
                    conn.execute(
                        """
                        INSERT INTO decision_journal
                        (agent, strategy, decision, score, outcome_1m_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            "crypto_5m_market_maker_shadow",
                            "crypto_5m_market_maker_shadow",
                            "SHADOW_QUOTE",
                            0.7,
                            json.dumps({"markout_pct": 0.02}),
                        ),
                    )

            scorecard = build_strategy_scorecard(str(db), min_decisions=3)

            self.assertEqual(scorecard["strategy_count"], 1)
            strategy = scorecard["strategies"][0]
            self.assertEqual(strategy["promotion_state"], "promotable")
            self.assertEqual(strategy["approvals"], 3)
            self.assertGreater(strategy["avg_markout_pct"], 0)


if __name__ == "__main__":
    unittest.main()
