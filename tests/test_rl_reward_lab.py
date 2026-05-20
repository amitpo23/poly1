from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agents.application.rl_reward_lab import (
    RewardConfig,
    build_reward_dataset,
    write_reward_dataset,
)


class RLRewardLabTests(unittest.TestCase):
    def _db(self, tmp: str) -> Path:
        db = Path(tmp) / "trade_log.db"
        with sqlite3.connect(db) as conn:
            conn.execute(
                """
                CREATE TABLE brain_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_type TEXT,
                    asset TEXT,
                    features_json TEXT
                )
                """
            )
            conn.execute(
                """
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
                    outcome_1m_json TEXT,
                    outcome_3m_json TEXT,
                    outcome_5m_json TEXT,
                    outcome_15m_json TEXT,
                    outcome_60m_json TEXT,
                    outcome_status TEXT
                )
                """
            )
        return db

    def test_build_reward_dataset_penalizes_reentry_and_costs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "INSERT INTO brain_decisions (market_type, asset, features_json) VALUES (?, ?, ?)",
                    ("crypto_5m", "BTC", json.dumps({"crypto_tape_symbol": "BTCUSDT"})),
                )
                base = {
                    "question": "Will Bitcoin go up?",
                    "spread_pct": 0.03,
                    "bid_depth_usdc": 10,
                    "ask_depth_usdc": 40,
                }
                for idx in range(2):
                    conn.execute(
                        """
                        INSERT INTO decision_journal (
                            ts, decision_id, agent, strategy, market_id, token_id,
                            decision, reason, signal_source, market_price,
                            live_entry_price, internal_probability, raw_ev, net_ev,
                            score, mode, features_json, outcome_5m_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"2026-05-21T00:0{idx}:00+00:00",
                            1,
                            "scanner_executor",
                            "scanner_executor",
                            "m1",
                            "tok",
                            "SHADOW_ENTER",
                            "shadow",
                            "meta_brain",
                            0.50,
                            0.50,
                            0.60,
                            0.10,
                            0.06,
                            0.80,
                            "shadow",
                            json.dumps(base),
                            json.dumps({"minutes": 5, "pnl_pct": 0.08, "hit_take_profit_5pct": True}),
                        ),
                    )

            payload = build_reward_dataset(
                str(db),
                cfg=RewardConfig(round_trip_cost_pct=0.04, thin_depth_penalty=0.01),
            )

            self.assertEqual(payload["summary"]["row_count"], 2)
            first, second = payload["rows"]
            self.assertEqual(first["action"], "enter")
            self.assertGreater(first["reward"], 0)
            self.assertLess(second["reward"], first["reward"])
            self.assertEqual(second["observation"]["recent_same_market_entries"], 1)
            self.assertLess(second["reward_components"]["reentry_penalty"], 0)

    def test_write_reward_dataset_outputs_jsonl_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            with sqlite3.connect(db) as conn:
                conn.execute(
                    """
                    INSERT INTO decision_journal (
                        ts, agent, strategy, market_id, token_id, decision, reason,
                        market_price, live_entry_price, score, outcome_1m_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2026-05-21T00:00:00+00:00",
                        "crypto_5m_market_maker_shadow",
                        "maker",
                        "m1",
                        "tok",
                        "SHADOW_QUOTE",
                        "quote",
                        0.49,
                        0.51,
                        0.7,
                        json.dumps({"minutes": 1, "bid_markout_pct": 0.03}),
                    ),
                )
            out = Path(tmp) / "dataset.jsonl"
            summary = Path(tmp) / "summary.json"

            payload = write_reward_dataset(str(db), str(out), summary_path=str(summary))

            self.assertTrue(out.exists())
            self.assertTrue(summary.exists())
            self.assertEqual(len(out.read_text().strip().splitlines()), 1)
            self.assertEqual(payload["summary"]["by_action"]["quote"], 1)


if __name__ == "__main__":
    unittest.main()
