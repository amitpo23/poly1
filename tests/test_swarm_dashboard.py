import importlib
import os
import sqlite3
import sys
import tempfile
import unittest


class TestSwarmDashboardDb(unittest.TestCase):
    def _import_db_with_swarm_db(self, path: str):
        old = os.environ.get("SWARM_DB")
        try:
            os.environ["SWARM_DB"] = path
            sys.modules.pop("scripts.python.db", None)
            return importlib.import_module("scripts.python.db")
        finally:
            if old is None:
                os.environ.pop("SWARM_DB", None)
            else:
                os.environ["SWARM_DB"] = old

    def test_partial_swarm_schema_degrades_gracefully(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(db_path) and os.unlink(db_path))

        con = sqlite3.connect(db_path)
        con.execute(
            "CREATE TABLE pending_orders ("
            "id INTEGER, agent TEXT, market_id TEXT, side TEXT, outcome TEXT, "
            "size_usd REAL, price_cents REAL, status TEXT, order_id TEXT, "
            "created_ms INTEGER, updated_ms INTEGER, note TEXT)"
        )
        con.execute(
            "INSERT INTO pending_orders VALUES "
            "(1, 'mm', 'm1', 'BUY', 'YES', 5.0, 55.5, 'submitted', 'o1', 0, 0, 'note')"
        )
        con.commit()
        con.close()

        db = self._import_db_with_swarm_db(db_path)
        self.assertTrue(db.swarm_db_present())
        self.assertEqual(len(db.swarm_pending_orders()), 1)
        self.assertEqual(db.swarm_agent_summary(), [])
        self.assertEqual(db.swarm_submitted_unreconciled(), [])


class TestSwarmMonitorWeb(unittest.TestCase):
    def test_price_cents_render_as_cents(self):
        sys.path.insert(0, os.path.join(os.getcwd(), "scripts/python"))
        try:
            import monitor_web
        finally:
            sys.path.pop(0)

        html = monitor_web._swarm_body(
            {
                "submitted_unreconciled": [
                    {
                        "updated_ms": 0,
                        "agent": "mm",
                        "side": "BUY",
                        "outcome": "YES",
                        "price_cents": 55.5,
                        "size_usd": 5.0,
                        "order_id": "abc123",
                        "note": "test",
                    }
                ],
                "recent_pending": [
                    {
                        "updated_ms": 0,
                        "agent": "mm",
                        "status": "submitted",
                        "side": "BUY",
                        "outcome": "YES",
                        "price_cents": 55.5,
                        "size_usd": 5.0,
                        "note": "test",
                    }
                ],
            }
        )

        self.assertIn("55.5", html)
        self.assertIn("\xa2", html)
        self.assertNotIn("55.5000", html)


if __name__ == "__main__":
    unittest.main()
