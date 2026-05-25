"""Tests for agents/application/probability_calibrator.py."""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.application.probability_calibrator import (
    CalibrationStat,
    _price_band,
    calibrate,
    lookup_winrate,
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
            ts TEXT, market_id TEXT, token_id TEXT,
            status TEXT, response_json TEXT
        );
        """
    )
    return str(db)


def _add_decision(conn, ts, market_id, token_id, action, signal_source,
                  entry_price=0.45, market_type="general_binary"):
    conn.execute(
        "INSERT INTO brain_decisions (ts, agent, market_id, token_id, action, "
        "approved, market_type, features_json, signal_source) "
        "VALUES (?, 'market_scanner', ?, ?, ?, 1, ?, ?, ?)",
        (
            ts.isoformat(),
            market_id,
            token_id,
            action,
            market_type,
            json.dumps({"selected_entry_price": entry_price}),
            signal_source,
        ),
    )


def _add_close(conn, ts, market_id, token_id, status, pnl=0.0):
    conn.execute(
        "INSERT INTO trades (ts, market_id, token_id, status, response_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (ts.isoformat(), market_id, token_id, status,
         json.dumps({"pnl_usdc_real": pnl})),
    )


class PriceBandTests(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(_price_band(0.35), "<0.40")
        self.assertEqual(_price_band(0.40), "0.40-0.49")
        self.assertEqual(_price_band(0.49), "0.40-0.49")
        self.assertEqual(_price_band(0.50), "0.50-0.54")
        self.assertEqual(_price_band(0.54), "0.50-0.54")
        self.assertEqual(_price_band(0.55), "0.55-0.64")
        self.assertEqual(_price_band(0.75), "0.75+")
        self.assertEqual(_price_band(0.0), "invalid")
        self.assertEqual(_price_band(None), "invalid")
        self.assertEqual(_price_band(1.5), "invalid")


class CalibrationStatTests(unittest.TestCase):
    def test_winrate_and_wilson(self):
        s = CalibrationStat(key="x", segment="y", wins=20, losses=10)
        self.assertAlmostEqual(s.winrate, 0.6667, places=4)
        # Wilson lower at 95% for 20/30 wins should be around 0.49
        self.assertLess(s.wilson_lower, s.winrate)
        self.assertGreater(s.wilson_lower, 0.45)

    def test_zero_samples(self):
        s = CalibrationStat(key="x", segment="y", wins=0, losses=0)
        self.assertIsNone(s.winrate)
        self.assertIsNone(s.wilson_lower)


class CalibrateEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _seed_db(Path(self.tmp.name))
        self.conn = sqlite3.connect(self.db)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_per_source_winrate_aggregates(self):
        # AlphaInsider: 2 wins, 1 loss
        for i, status in enumerate(("closed_take_profit", "closed_take_profit", "closed_stop_loss")):
            _add_decision(
                self.conn, self.now - timedelta(minutes=30 + i),
                f"M_A{i}", f"T_A{i}", "BUY", "alphainsider_proven",
                entry_price=0.45,
            )
            _add_close(
                self.conn, self.now - timedelta(minutes=10 + i),
                f"M_A{i}", f"T_A{i}", status,
            )
        # Manifold: 1 win, 2 losses
        for i, status in enumerate(("closed_take_profit", "closed_stop_loss", "closed_timeout")):
            _add_decision(
                self.conn, self.now - timedelta(minutes=30 + i),
                f"M_M{i}", f"T_M{i}", "SELL", "manifold:manifold",
                entry_price=0.75,
            )
            _add_close(
                self.conn, self.now - timedelta(minutes=10 + i),
                f"M_M{i}", f"T_M{i}", status,
            )
        self.conn.commit()

        result = calibrate(self.db, days=7)
        self.assertEqual(result["total_closes"], 6)
        self.assertEqual(result["matched"], 6)
        sources = {s["key"]: s for s in result["per_signal_source"]}
        self.assertEqual(sources["alphainsider_proven"]["wins"], 2)
        self.assertEqual(sources["alphainsider_proven"]["losses"], 1)
        self.assertAlmostEqual(sources["alphainsider_proven"]["winrate"], 2/3, places=2)
        self.assertEqual(sources["manifold:manifold"]["wins"], 1)
        self.assertEqual(sources["manifold:manifold"]["losses"], 2)

    def test_unmatched_close_counted(self):
        # Close with no matching decision (decision was outside window)
        _add_decision(
            self.conn, self.now - timedelta(hours=200),
            "M_OLD", "T_OLD", "BUY", "test_source",
        )
        _add_close(
            self.conn, self.now - timedelta(hours=10),
            "M_OLD", "T_OLD", "closed_take_profit",
        )
        self.conn.commit()
        result = calibrate(self.db, days=7, max_age_hours=48)
        self.assertEqual(result["total_closes"], 1)
        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["unmatched"], 1)

    def test_lookup_specific_to_broad_fallback(self):
        # 10 alphainsider BUY @ 0.45 closes: 6 wins, 4 losses
        for i in range(6):
            _add_decision(
                self.conn, self.now - timedelta(minutes=30 + i),
                f"M_W{i}", f"T_W{i}", "BUY", "alphainsider_proven",
                entry_price=0.45,
            )
            _add_close(
                self.conn, self.now - timedelta(minutes=10 + i),
                f"M_W{i}", f"T_W{i}", "closed_take_profit",
            )
        for i in range(4):
            _add_decision(
                self.conn, self.now - timedelta(minutes=40 + i),
                f"M_L{i}", f"T_L{i}", "BUY", "alphainsider_proven",
                entry_price=0.45,
            )
            _add_close(
                self.conn, self.now - timedelta(minutes=20 + i),
                f"M_L{i}", f"T_L{i}", "closed_stop_loss",
            )
        self.conn.commit()
        cal = calibrate(self.db, days=7)
        # Most-specific lookup: source|band
        stat = lookup_winrate(
            cal, signal_source="alphainsider_proven",
            price_band="0.40-0.49", action="BUY", min_samples=5,
        )
        self.assertIsNotNone(stat)
        self.assertEqual(stat.wins, 6)
        self.assertEqual(stat.losses, 4)
        self.assertAlmostEqual(stat.winrate, 0.6, places=2)

    def test_3way_segmentation_exposes_asymmetric_edge(self):
        """Added 2026-05-25 after empirical finding: per_source_band
        aggregates BUY+SELL and can hide a positive-EV BUY subset
        inside a negative aggregate. per_source_band_action exposes it.
        """
        # 7 alphainsider BUY @ 0.55 closes: 5 wins, 2 losses (clearly +EV)
        for i in range(5):
            _add_decision(
                self.conn, self.now - timedelta(minutes=30 + i),
                f"BW{i}", f"BWT{i}", "BUY", "alphainsider_proven",
                entry_price=0.55,
            )
            _add_close(
                self.conn, self.now - timedelta(minutes=10 + i),
                f"BW{i}", f"BWT{i}", "closed_take_profit",
            )
        for i in range(2):
            _add_decision(
                self.conn, self.now - timedelta(minutes=40 + i),
                f"BL{i}", f"BLT{i}", "BUY", "alphainsider_proven",
                entry_price=0.55,
            )
            _add_close(
                self.conn, self.now - timedelta(minutes=20 + i),
                f"BL{i}", f"BLT{i}", "closed_stop_loss",
            )
        # 5 alphainsider SELL @ 0.55 closes: 1 win, 4 losses (clearly -EV)
        _add_decision(
            self.conn, self.now - timedelta(minutes=50),
            "SW1", "SWT1", "SELL", "alphainsider_proven",
            entry_price=0.55,
        )
        _add_close(
            self.conn, self.now - timedelta(minutes=15),
            "SW1", "SWT1", "closed_take_profit",
        )
        for i in range(4):
            _add_decision(
                self.conn, self.now - timedelta(minutes=55 + i),
                f"SL{i}", f"SLT{i}", "SELL", "alphainsider_proven",
                entry_price=0.55,
            )
            _add_close(
                self.conn, self.now - timedelta(minutes=25 + i),
                f"SL{i}", f"SLT{i}", "closed_stop_loss",
            )
        self.conn.commit()
        cal = calibrate(self.db, days=7)
        # Aggregate per_source_band: 6 wins / 12 total = 50%
        agg = lookup_winrate(
            cal, signal_source="alphainsider_proven",
            price_band="0.55-0.64", min_samples=5,
            action=None,
        )
        self.assertIsNotNone(agg)
        self.assertEqual(agg.wins + agg.losses, 12)
        # 3-way lookup for BUY only: 5/7 = ~71%
        buy_stat = lookup_winrate(
            cal, signal_source="alphainsider_proven",
            price_band="0.55-0.64", action="BUY", min_samples=5,
        )
        self.assertIsNotNone(buy_stat)
        self.assertEqual(buy_stat.wins, 5)
        self.assertEqual(buy_stat.losses, 2)
        self.assertAlmostEqual(buy_stat.winrate, 5/7, places=2)
        # The 3-way must be DIFFERENT from the aggregate — that's the
        # whole point of the new dimension.
        self.assertNotEqual(buy_stat.winrate, agg.winrate)

    def test_per_source_band_action_in_json(self):
        """Verify calibrate() output includes the new top-level key."""
        for i in range(5):
            _add_decision(
                self.conn, self.now - timedelta(minutes=30 + i),
                f"X{i}", f"XT{i}", "BUY", "src1", entry_price=0.45,
            )
            _add_close(
                self.conn, self.now - timedelta(minutes=10 + i),
                f"X{i}", f"XT{i}", "closed_take_profit",
            )
        self.conn.commit()
        cal = calibrate(self.db, days=7)
        self.assertIn("per_source_band_action", cal)
        keys = [e["key"] for e in cal["per_source_band_action"]]
        self.assertIn("src1|0.40-0.49|BUY", keys)

    def test_lookup_returns_none_below_min_samples(self):
        _add_decision(
            self.conn, self.now - timedelta(minutes=30),
            "M1", "T1", "BUY", "rare_source", entry_price=0.45,
        )
        _add_close(
            self.conn, self.now - timedelta(minutes=10),
            "M1", "T1", "closed_take_profit",
        )
        self.conn.commit()
        cal = calibrate(self.db, days=7)
        stat = lookup_winrate(
            cal, signal_source="rare_source", min_samples=5,
        )
        self.assertIsNone(stat)  # only 1 sample, below threshold


if __name__ == "__main__":
    unittest.main()
