"""Tests for the btc_5min skip-reason logging added 2026-05-24.

Round 11 + Round 12 ran for 20-30 min each with btc_5min armed and
produced ZERO log lines except bootstrap. Every return-False path in
maybe_enter() returned silently. This made it impossible to diagnose
why btc_5min wasn't firing. The fix: a _log_skip(reason) helper that
emits INFO once per state-change, so the daemon shows what's blocking
it without spamming 150 lines/min across 5 assets.
"""
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.application.btc_5min import Btc5MinConfig, Btc5MinEngine


def _make_engine(asset: str = "btc") -> Btc5MinEngine:
    """Build an engine bypassing __init__ side effects (network bootstrap)."""
    eng = Btc5MinEngine.__new__(Btc5MinEngine)
    eng.cfg = Btc5MinConfig()
    eng.asset = asset
    eng._last_skip_reason = ""
    return eng


class LogSkipTests(unittest.TestCase):
    def test_first_skip_logs_info(self):
        eng = _make_engine()
        with self.assertLogs("agents.application.btc_5min", level="INFO") as cm:
            eng._log_skip("timing_too_early elapsed=5s")
        self.assertTrue(
            any("timing_too_early" in m for m in cm.output),
            f"Expected timing_too_early in {cm.output}",
        )

    def test_same_reason_logs_only_once(self):
        eng = _make_engine()
        # First call logs.
        with self.assertLogs("agents.application.btc_5min", level="INFO") as cm:
            eng._log_skip("cooldown_active")
        self.assertEqual(len(cm.output), 1)
        # Second call with same reason — assertLogs requires AT LEAST one
        # record, so we patch logger directly.
        logger = logging.getLogger("agents.application.btc_5min")
        handler = MagicMock()
        logger.addHandler(handler)
        try:
            eng._log_skip("cooldown_active")
        finally:
            logger.removeHandler(handler)
        # No new emit since reason didn't change.
        self.assertEqual(handler.handle.call_count, 0)

    def test_reason_change_logs_again(self):
        eng = _make_engine()
        with self.assertLogs("agents.application.btc_5min", level="INFO") as cm:
            eng._log_skip("timing_too_early")
            eng._log_skip("timing_too_late")  # different reason — logs again
        self.assertEqual(len(cm.output), 2)
        self.assertIn("timing_too_early", cm.output[0])
        self.assertIn("timing_too_late", cm.output[1])

    def test_asset_appears_in_log(self):
        eng = _make_engine(asset="eth")
        with self.assertLogs("agents.application.btc_5min", level="INFO") as cm:
            eng._log_skip("risk_gate_blocked")
        self.assertTrue(
            any("[eth]" in m for m in cm.output),
            f"Expected '[eth]' tag in {cm.output}",
        )


class HasMethodTests(unittest.TestCase):
    """Sanity: the engine actually exposes the new method."""

    def test_engine_has_log_skip(self):
        eng = _make_engine()
        self.assertTrue(hasattr(eng, "_log_skip"))
        self.assertTrue(callable(eng._log_skip))

    def test_engine_tracks_last_skip_reason(self):
        eng = _make_engine()
        self.assertEqual(eng._last_skip_reason, "")
        eng._log_skip("foo")
        self.assertEqual(eng._last_skip_reason, "foo")


if __name__ == "__main__":
    unittest.main()
