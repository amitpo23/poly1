from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.python.backtest_external_convictions import (
    build_stats,
    iter_signals,
    load_outcomes,
)


class TestBacktestExternalConvictions(unittest.TestCase):
    def test_provider_stats_match_jsonl_signal_to_trade_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "external_convictions_test.jsonl"
            jsonl.write_text(
                json.dumps({
                    "plan": {
                        "ts": "2026-05-19T10:00:00+00:00",
                        "market_id": "M1",
                        "token_id": "TOK1",
                        "action": "BUY",
                        "side": "YES",
                        "confidence": 0.61,
                        "entry_price": 0.50,
                        "source": "provider_a",
                    }
                }) + "\n",
                encoding="utf-8",
            )
            db_path = root / "trade_log.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """CREATE TABLE trades (
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
                    )"""
                )
                conn.execute(
                    "INSERT INTO trades (ts, cycle_id, market_id, token_id, side, "
                    "price, size_usdc, confidence, status, response_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        "2026-05-19T10:05:00+00:00",
                        "c1",
                        "M1",
                        "TOK1",
                        "SELL",
                        0.55,
                        5.5,
                        0.7,
                        "closed_take_profit",
                        json.dumps({"pnl_usdc_real": 0.5}),
                    ),
                )

            signals = iter_signals([jsonl])
            outcomes = load_outcomes(db_path)
            providers, buckets = build_stats(signals, outcomes, max_age_hours=1)

            self.assertEqual(len(signals), 1)
            self.assertEqual(providers[0].source, "provider_a")
            self.assertEqual(providers[0].matched, 1)
            self.assertEqual(providers[0].wins, 1)
            self.assertAlmostEqual(providers[0].winrate, 1.0)
            self.assertEqual(buckets[0].source, "provider_a|0.58-0.65")

    def test_skips_are_not_actionable_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "external_convictions_test.jsonl"
            jsonl.write_text(
                json.dumps({
                    "plan": {
                        "ts": "2026-05-19T10:00:00+00:00",
                        "market_id": "M1",
                        "action": "SKIP",
                        "source": "provider_a",
                    }
                }) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(iter_signals([jsonl]), [])

    def test_shadow_buy_is_actionable_for_backtest(self):
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "external_convictions_test.jsonl"
            jsonl.write_text(
                json.dumps({
                    "plan": {
                        "ts": "2026-05-19T10:00:00+00:00",
                        "market_id": "M1",
                        "action": "SHADOW_BUY_YES",
                        "source": "provider_a",
                    }
                }) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(len(iter_signals([jsonl])), 1)

    def test_resolved_no_counts_as_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jsonl = root / "external_convictions_test.jsonl"
            jsonl.write_text(
                json.dumps({
                    "plan": {
                        "ts": "2026-05-19T10:00:00+00:00",
                        "market_id": "M1",
                        "token_id": "TOK_NO",
                        "action": "BUY",
                        "side": "NO",
                        "confidence": 0.66,
                        "source": "provider_a",
                    }
                }) + "\n",
                encoding="utf-8",
            )
            db_path = root / "trade_log.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """CREATE TABLE trades (
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
                    )"""
                )
                conn.execute(
                    "INSERT INTO trades (ts, cycle_id, market_id, token_id, side, "
                    "price, size_usdc, confidence, status) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        "2026-05-19T10:15:00+00:00",
                        "c1",
                        "M1",
                        "TOK_NO",
                        "RESOLUTION",
                        1.0,
                        4.0,
                        None,
                        "resolved_no",
                    ),
                )

            providers, _ = build_stats(
                iter_signals([jsonl]),
                load_outcomes(db_path),
                max_age_hours=1,
            )
            self.assertEqual(providers[0].wins, 1)
            self.assertEqual(providers[0].losses, 0)


if __name__ == "__main__":
    unittest.main()
