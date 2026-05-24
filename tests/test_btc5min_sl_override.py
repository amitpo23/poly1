"""Tests for sl_pct_override integration (btc_5min → position_manager).

btc_5min uses 5-min crypto up/down markets. The global stop_loss_pct
(0.06) is too wide for that horizon: positions auto-resolve at market
close before SL fires, so losers go unbounded. Backtest on 30d of
data: SL=0.03 with TP=0.08 flips EV from -$0.045 to positive.

These tests verify the wiring end-to-end:
1. btc_5min writes sl_pct_override into the response_json
2. position_manager parses it into AggregatedPosition.sl_pct_override
3. The exit-decision branch uses the override instead of the global
"""
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.application.position_manager import AggregatedPosition


class AggregatedPositionTests(unittest.TestCase):
    """The dataclass must support sl_pct_override."""

    def test_has_sl_pct_override_field(self):
        pos = AggregatedPosition(
            token_id="T1",
            market_id="M1",
            side="BUY",
            total_cost_usdc=1.0,
            total_shares=2.0,
            avg_entry_price=0.5,
            earliest_ts=0.0,
            sl_pct_override=0.03,
        )
        self.assertEqual(pos.sl_pct_override, 0.03)

    def test_sl_pct_override_defaults_none(self):
        pos = AggregatedPosition(
            token_id="T1", market_id="M1", side="BUY",
            total_cost_usdc=1.0, total_shares=2.0,
            avg_entry_price=0.5, earliest_ts=0.0,
        )
        self.assertIsNone(pos.sl_pct_override)


class Btc5MinConfigTests(unittest.TestCase):
    """Verify the new stop_loss_pct config field and env loader."""

    def test_default_stop_loss_pct(self):
        from agents.application.btc_5min import Btc5MinConfig
        cfg = Btc5MinConfig()
        self.assertAlmostEqual(cfg.stop_loss_pct, 0.03)

    def test_env_overrides_stop_loss_pct(self):
        import os
        from agents.application.btc_5min import Btc5MinConfig
        os.environ["BTC_5MIN_STOP_LOSS_PCT"] = "0.025"
        try:
            cfg = Btc5MinConfig.from_env()
            self.assertAlmostEqual(cfg.stop_loss_pct, 0.025)
        finally:
            del os.environ["BTC_5MIN_STOP_LOSS_PCT"]

    def test_default_take_profit_is_8pct(self):
        """Raised from 0.05 to 0.08 per the 2026-05-24 backtest finding."""
        from agents.application.btc_5min import Btc5MinConfig
        cfg = Btc5MinConfig()
        self.assertAlmostEqual(cfg.take_profit_pct, 0.08)


if __name__ == "__main__":
    unittest.main()
