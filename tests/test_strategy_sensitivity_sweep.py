from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.strategy_sensitivity_sweep import (
    SweepConfig,
    default_configs,
    load_rows,
    run_sweep,
)


class StrategySensitivitySweepTests(unittest.TestCase):
    def test_default_grid_can_build_one_thousand_configs(self):
        configs = default_configs(1000)

        self.assertEqual(len(configs), 1000)
        self.assertEqual(configs[0].horizon_min, 1)

    def test_sweep_ranks_positive_markouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "trade_log.db"
            con = sqlite3.connect(db)
            con.execute(
                """
                CREATE TABLE decision_journal (
                    id INTEGER PRIMARY KEY,
                    ts TEXT,
                    agent TEXT,
                    strategy TEXT,
                    decision TEXT,
                    market_id TEXT,
                    token_id TEXT,
                    live_entry_price REAL,
                    market_price REAL,
                    raw_ev REAL,
                    net_ev REAL,
                    score REAL,
                    outcome_1m_json TEXT,
                    outcome_3m_json TEXT,
                    outcome_5m_json TEXT,
                    outcome_15m_json TEXT,
                    outcome_60m_json TEXT
                )
                """
            )
            for idx, pnl in enumerate([0.04, 0.03, -0.01, 0.02], start=1):
                con.execute(
                    """
                    INSERT INTO decision_journal (
                        id, ts, agent, strategy, decision, market_id, token_id,
                        live_entry_price, market_price, raw_ev, net_ev, score,
                        outcome_1m_json, outcome_3m_json, outcome_5m_json,
                        outcome_15m_json, outcome_60m_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        idx,
                        "2026-05-21T00:00:00+00:00",
                        "scanner_executor",
                        "execute_scanner_trade_opportunity",
                        "SHADOW_ENTER",
                        f"m{idx}",
                        f"t{idx}",
                        0.5,
                        0.5,
                        0.05,
                        0.04,
                        0.8,
                        json.dumps({"pnl_pct": pnl}),
                        None,
                        None,
                        None,
                        None,
                    ),
                )
            con.commit()
            con.close()

            rows = load_rows(str(db))
            payload = run_sweep(
                rows,
                [
                    SweepConfig(0.7, 0.0, 0.0, 0.8, 1, 0.05, 0.03, 0),
                    SweepConfig(0.9, 0.0, 0.0, 0.8, 1, 0.05, 0.03, 0),
                ],
                min_trades=2,
            )

        self.assertEqual(payload["configs_tested"], 2)
        self.assertEqual(payload["viable_count"], 1)
        self.assertEqual(payload["best_viable"][0]["candidates"], 4)


if __name__ == "__main__":
    unittest.main()
