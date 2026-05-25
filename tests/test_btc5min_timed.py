"""Tests for the time-based DOWN/UP strategy added 2026-05-25.

Operator-requested strategy: no signal, just time-based entry at:
  Phase 1: t=0:01 of each 5-min period → BUY DOWN, TP+5%/SL-20%
  Phase 2: t=3:00 of each 5-min period → BUY UP, TP+5%/SL-20%

Backtest showed -EV but operator wants empirical validation. Defaults:
EXECUTE_BTC5MIN_TIMED=false, position=$0.20, max 10 trades/day, auto-halt
after 3 consecutive losses.
"""
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.application.btc5min_timed import (
    Btc5MinTimedConfig,
    Btc5MinTimedEngine,
    CycleState,
    DailyState,
    _current_period_ts,
    _format_slug,
)


def _make_engine(execute=False) -> Btc5MinTimedEngine:
    """Construct engine bypassing daemon's network init."""
    cfg = Btc5MinTimedConfig(execute=execute, position_usdc=0.20)
    risk_gate = MagicMock()
    risk_gate.ok.return_value = True
    return Btc5MinTimedEngine(
        polymarket=MagicMock(),
        trade_log=MagicMock(),
        risk_gate=risk_gate,
        cfg=cfg,
    )


class ConfigTests(unittest.TestCase):
    def test_defaults_safe(self):
        """Defaults must be conservative — small size, no live execution."""
        cfg = Btc5MinTimedConfig()
        self.assertFalse(cfg.execute)  # NOT live by default
        self.assertEqual(cfg.position_usdc, 0.20)
        self.assertEqual(cfg.max_trades_per_day, 10)
        self.assertEqual(cfg.halt_after_losses, 3)

    def test_phase1_parameters(self):
        cfg = Btc5MinTimedConfig()
        self.assertEqual(cfg.phase1_entry_offset_sec, 1)
        self.assertAlmostEqual(cfg.phase1_tp_pct, 0.05)
        self.assertAlmostEqual(cfg.phase1_sl_pct, 0.20)

    def test_phase2_parameters(self):
        cfg = Btc5MinTimedConfig()
        self.assertEqual(cfg.phase2_entry_offset_sec, 180)
        self.assertAlmostEqual(cfg.phase2_tp_pct, 0.05)
        self.assertAlmostEqual(cfg.phase2_sl_pct, 0.20)

    def test_from_env_overrides(self):
        os.environ["EXECUTE_BTC5MIN_TIMED"] = "true"
        os.environ["BTC5MIN_TIMED_POSITION_USDC"] = "0.50"
        os.environ["BTC5MIN_TIMED_PHASE1_TP_PCT"] = "0.08"
        try:
            cfg = Btc5MinTimedConfig.from_env()
            self.assertTrue(cfg.execute)
            self.assertAlmostEqual(cfg.position_usdc, 0.50)
            self.assertAlmostEqual(cfg.phase1_tp_pct, 0.08)
        finally:
            for k in ["EXECUTE_BTC5MIN_TIMED", "BTC5MIN_TIMED_POSITION_USDC",
                      "BTC5MIN_TIMED_PHASE1_TP_PCT"]:
                os.environ.pop(k, None)


class PeriodTimingTests(unittest.TestCase):
    def test_period_ts_aligned_to_5min(self):
        ts = _current_period_ts()
        self.assertEqual(ts % 300, 0)

    def test_slug_format(self):
        self.assertEqual(_format_slug(1779696900, "btc"), "btc-updown-5m-1779696900")
        self.assertEqual(_format_slug(1779696900, "ETH"), "eth-updown-5m-1779696900")


class MaybeEnterPhaseTimingTests(unittest.TestCase):
    """Verify entry only triggers at the correct timing offsets."""

    def setUp(self):
        self.engine = _make_engine()
        # Force phase1/phase2 not-fired
        self.engine._cycle = CycleState(period_ts=_current_period_ts())

    def _patch_elapsed(self, elapsed_sec: float):
        """Force time.time() to return a specific elapsed into current period."""
        period = _current_period_ts()
        import agents.application.btc5min_timed as mod
        mod.time.time = lambda: period + elapsed_sec

    def tearDown(self):
        import agents.application.btc5min_timed as mod
        mod.time.time = time.time

    def test_phase1_fires_at_offset_1sec(self):
        self._patch_elapsed(1.0)
        self.assertEqual(self.engine.maybe_enter(), "phase1")

    def test_phase1_does_not_fire_at_5sec(self):
        self._patch_elapsed(5.0)
        self.assertNotEqual(self.engine.maybe_enter(), "phase1")

    def test_phase2_fires_at_180sec(self):
        self._patch_elapsed(180.0)
        self.assertEqual(self.engine.maybe_enter(), "phase2")

    def test_no_entry_after_270sec(self):
        self._patch_elapsed(280.0)
        result = self.engine.maybe_enter()
        self.assertIsNone(result)

    def test_phase1_dedup_within_period(self):
        """Once phase1 fired this period, don't refire."""
        self._patch_elapsed(1.0)
        self.assertEqual(self.engine.maybe_enter(), "phase1")
        self.engine._cycle.phase1_fired = True  # simulate fire success
        self._patch_elapsed(1.5)
        self.assertNotEqual(self.engine.maybe_enter(), "phase1")


class RiskGateBlockTests(unittest.TestCase):
    def test_risk_gate_blocks_entry(self):
        engine = _make_engine()
        engine.risk_gate.ok.return_value = False
        engine._cycle = CycleState(period_ts=_current_period_ts())
        import agents.application.btc5min_timed as mod
        period = _current_period_ts()
        mod.time.time = lambda: period + 1.0
        try:
            self.assertIsNone(engine.maybe_enter())
        finally:
            mod.time.time = time.time


class DailyLimitTests(unittest.TestCase):
    def test_trades_per_day_cap(self):
        engine = _make_engine()
        engine._daily = DailyState(date_key=engine._today_key(), trades_today=10)
        # max_trades_per_day=10 → at-cap should block
        engine._cycle = CycleState(period_ts=_current_period_ts())
        import agents.application.btc5min_timed as mod
        period = _current_period_ts()
        mod.time.time = lambda: period + 1.0
        try:
            self.assertIsNone(engine.maybe_enter())
        finally:
            mod.time.time = time.time

    def test_auto_halt_after_losses(self):
        engine = _make_engine()
        engine._daily = DailyState(
            date_key=engine._today_key(),
            consecutive_losses=3,
            auto_halted=True,
        )
        engine._cycle = CycleState(period_ts=_current_period_ts())
        import agents.application.btc5min_timed as mod
        period = _current_period_ts()
        mod.time.time = lambda: period + 1.0
        try:
            self.assertIsNone(engine.maybe_enter())
        finally:
            mod.time.time = time.time


class FireDryrunTests(unittest.TestCase):
    def test_dryrun_phase1_marks_fired(self):
        engine = _make_engine(execute=False)
        result = engine.fire("phase1")
        self.assertTrue(result)
        self.assertTrue(engine._cycle.phase1_fired)
        self.assertFalse(engine._cycle.phase2_fired)

    def test_dryrun_phase2_marks_fired(self):
        engine = _make_engine(execute=False)
        result = engine.fire("phase2")
        self.assertTrue(result)
        self.assertTrue(engine._cycle.phase2_fired)

    def test_live_mode_unimplemented(self):
        """Until live path is reviewed, live mode must NOT silently trade."""
        engine = _make_engine(execute=True)
        result = engine.fire("phase1")
        # Returns False because live execution path is not implemented
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
