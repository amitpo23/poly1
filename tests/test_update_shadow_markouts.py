import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agents.application.trade_log import TradeLog
from scripts.update_shadow_markouts import update_markouts


class UpdateShadowMarkoutsTests(unittest.TestCase):
    def test_marks_live_enter_rows_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "trade_log.db")
            log = TradeLog(db_path=db)
            journal_id = log.insert_decision_journal(
                agent="market_scanner",
                strategy="scanner_executor",
                market_id="m1",
                token_id="tok1",
                action="BUY",
                decision="ENTER",
                reason="live_entry",
                market_price=0.42,
                live_entry_price=0.44,
            )
            entry_ts = datetime.now(timezone.utc) - timedelta(minutes=7)
            target_ts = entry_ts + timedelta(minutes=5, seconds=20)
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE decision_journal SET ts = ? WHERE id = ?",
                    (entry_ts.isoformat(), journal_id),
                )
                conn.execute(
                    """
                    INSERT INTO orderbook_snapshots (
                        ts, token_id, market_id, source, best_bid, best_ask,
                        bid_depth_usdc, ask_depth_usdc, bid_levels, ask_levels
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target_ts.isoformat(),
                        "tok1",
                        "m1",
                        "test",
                        0.50,
                        0.52,
                        100,
                        120,
                        2,
                        2,
                    ),
                )

            result = update_markouts(
                db_path=db,
                horizons=[5],
                limit=10,
                max_lag_seconds=90,
                live_fallback=False,
            )

            self.assertEqual(result["updated"], 1)
            with sqlite3.connect(db) as conn:
                raw = conn.execute(
                    "SELECT outcome_5m_json FROM decision_journal WHERE id = ?",
                    (journal_id,),
                ).fetchone()[0]
            payload = json.loads(raw)
            self.assertEqual(payload["decision"], "ENTER")
            self.assertEqual(payload["model"], "taker_entry_exit_at_future_bid")
            self.assertAlmostEqual(payload["pnl_pct"], round((0.50 / 0.44) - 1.0, 6))

    def test_decision_filter_can_keep_rejects_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "trade_log.db")
            log = TradeLog(db_path=db)
            journal_id = log.insert_decision_journal(
                agent="market_scanner",
                strategy="scanner_executor",
                market_id="m1",
                token_id="tok1",
                action="BUY",
                decision="REJECT",
                reason="blocked",
                market_price=0.42,
                live_entry_price=0.44,
            )
            entry_ts = datetime.now(timezone.utc) - timedelta(minutes=7)
            target_ts = entry_ts + timedelta(minutes=5)
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "UPDATE decision_journal SET ts = ? WHERE id = ?",
                    (entry_ts.isoformat(), journal_id),
                )
                conn.execute(
                    """
                    INSERT INTO orderbook_snapshots (
                        ts, token_id, market_id, source, best_bid, best_ask,
                        bid_depth_usdc, ask_depth_usdc, bid_levels, ask_levels
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target_ts.isoformat(),
                        "tok1",
                        "m1",
                        "test",
                        0.50,
                        0.52,
                        100,
                        120,
                        2,
                        2,
                    ),
                )

            result = update_markouts(
                db_path=db,
                horizons=[5],
                limit=10,
                max_lag_seconds=90,
                live_fallback=False,
            )

            self.assertEqual(result["updated"], 0)
            with sqlite3.connect(db) as conn:
                raw = conn.execute(
                    "SELECT outcome_5m_json FROM decision_journal WHERE id = ?",
                    (journal_id,),
                ).fetchone()[0]
            self.assertIsNone(raw)


if __name__ == "__main__":
    unittest.main()
