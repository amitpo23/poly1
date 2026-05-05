import os
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from agents.application.scalper import ScalperDaemon, ScalperConfig
from agents.application.trade_log import TradeLog
from agents.application.scalper_pairs import ScalperPairsDAO


class TestScalperDaemon(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.heartbeat = tempfile.NamedTemporaryFile(suffix="-hb", delete=False)
        self.heartbeat.close()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.tmp.name + suffix
            if os.path.exists(p):
                os.unlink(p)
        if os.path.exists(self.heartbeat.name):
            os.unlink(self.heartbeat.name)

    @patch("agents.application.scalper.Polymarket")
    @patch("agents.application.scalper.GammaMarketClient")
    def test_stop_signal_breaks_loop(self, gamma_mock, poly_mock):
        gamma_mock.return_value.get_events_by_tag = MagicMock(return_value=[])
        poly_mock.return_value.get_usdc_balance = MagicMock(return_value=80.0)
        daemon = ScalperDaemon(heartbeat_path=self.heartbeat.name,
                                 db_path=self.tmp.name,
                                 poll_ms=100, discover_every_sec=10)

        t = threading.Thread(target=daemon.run, daemon=True)
        t.start()
        time.sleep(0.3)
        daemon.stop()
        t.join(timeout=2)
        self.assertFalse(t.is_alive())


if __name__ == "__main__":
    unittest.main()
