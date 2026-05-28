"""Tests for the anti-gap SL override added to scanner_executor on
2026-05-24 after Trade 4237 lost 47% on an Ethereum 5-min Up/Down
market that gapped during the resolution window.

The fix: when a candidate looks like a 5-min crypto market (strategy
type, signal source, market cluster, or evidence horizon indicates),
scanner_executor writes a tighter per-position SL+TP into
response_json, so position_manager reads them and bounds exit damage.

2026-05-27 audit update: slow crypto_momentum / crypto_tape non-fast
markets now route to long_market_sl_pct_override (6%) instead of the
tight 3% fast-market SL — 3% was cutting profitable trades too early.
Fast 5-min markets still get crypto_momentum_sl_pct_override.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.application.scanner_executor import (
    ScannerExecutor,
    ScannerExecutorConfig,
)


def _make_engine() -> ScannerExecutor:
    cfg = ScannerExecutorConfig(
        crypto_momentum_sl_pct_override=0.03,
        crypto_momentum_tp_pct_override=0.08,
        long_market_sl_pct_override=0.03,
        long_market_tp_pct_override=0.08,
    )
    from unittest.mock import MagicMock
    eng = ScannerExecutor.__new__(ScannerExecutor)
    eng.cfg = cfg
    return eng


class PerPositionExitOverrideTests(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()

    def test_crypto_momentum_strategy_gets_override(self):
        # crypto_momentum without fast-market signals → long_market override
        result = self.engine._per_position_exit_overrides(
            row={"signal_source": "market_scanner"},
            features={"strategy_type": "crypto_momentum"},
        )
        self.assertEqual(result["sl_pct_override"], 0.03)
        self.assertEqual(result["tp_pct_override"], 0.08)

    def test_crypto_tape_signal_source_triggers_override(self):
        # crypto_tape → long_market override
        result = self.engine._per_position_exit_overrides(
            row={
                "signal_source": "opportunity_factory,alphainsider_proven,crypto_tape"
            },
            features={},
        )
        self.assertEqual(result["sl_pct_override"], 0.03)

    def test_updown_5m_market_cluster_triggers(self):
        result = self.engine._per_position_exit_overrides(
            row={},
            features={"market_cluster": "eth-updown-5m-1779635100"},
        )
        self.assertEqual(result["sl_pct_override"], 0.03)

    def test_evidence_horizon_5m_triggers(self):
        result = self.engine._per_position_exit_overrides(
            row={},
            features={"trade_proposal": {"evidence": {"horizon": "5m"}}},
        )
        self.assertEqual(result["sl_pct_override"], 0.03)

    def test_non_crypto_momentum_no_override(self):
        """Long-horizon political / event markets should return no override."""
        result = self.engine._per_position_exit_overrides(
            row={"signal_source": "meta_brain,manifold,manifold:manifold"},
            features={
                "strategy_type": "trend_following",
                "market_cluster": "us-iran-nuclear-deal-may-31",
            },
        )
        self.assertEqual(result, {})

    def test_env_overrides_default(self):
        import os
        os.environ["SCANNER_EXECUTOR_CRYPTO_MOMENTUM_SL_PCT"] = "0.025"
        os.environ["SCANNER_EXECUTOR_CRYPTO_MOMENTUM_TP_PCT"] = "0.10"
        try:
            cfg = ScannerExecutorConfig.from_env()
            self.assertAlmostEqual(cfg.crypto_momentum_sl_pct_override, 0.025)
            self.assertAlmostEqual(cfg.crypto_momentum_tp_pct_override, 0.10)
        finally:
            del os.environ["SCANNER_EXECUTOR_CRYPTO_MOMENTUM_SL_PCT"]
            del os.environ["SCANNER_EXECUTOR_CRYPTO_MOMENTUM_TP_PCT"]


if __name__ == "__main__":
    unittest.main()



class PerPositionExitOverrideTests(unittest.TestCase):
    def setUp(self):
        self.engine = _make_engine()

    def test_crypto_momentum_strategy_gets_override(self):
        result = self.engine._per_position_exit_overrides(
            row={"signal_source": "market_scanner"},
            features={"strategy_type": "crypto_momentum"},
        )
        self.assertEqual(result["sl_pct_override"], 0.03)
        self.assertEqual(result["tp_pct_override"], 0.08)

    def test_crypto_tape_signal_source_triggers_override(self):
        result = self.engine._per_position_exit_overrides(
            row={
                "signal_source": "opportunity_factory,alphainsider_proven,crypto_tape"
            },
            features={},
        )
        self.assertEqual(result["sl_pct_override"], 0.03)

    def test_updown_5m_market_cluster_triggers(self):
        result = self.engine._per_position_exit_overrides(
            row={},
            features={"market_cluster": "eth-updown-5m-1779635100"},
        )
        self.assertEqual(result["sl_pct_override"], 0.03)

    def test_evidence_horizon_5m_triggers(self):
        result = self.engine._per_position_exit_overrides(
            row={},
            features={"trade_proposal": {"evidence": {"horizon": "5m"}}},
        )
        self.assertEqual(result["sl_pct_override"], 0.03)

    def test_non_crypto_momentum_no_override(self):
        """Long-horizon political / event markets should keep the
        default global SL (0.06)."""
        result = self.engine._per_position_exit_overrides(
            row={"signal_source": "meta_brain,manifold,manifold:manifold"},
            features={
                "strategy_type": "trend_following",
                "market_cluster": "us-iran-nuclear-deal-may-31",
            },
        )
        self.assertEqual(result, {})

    def test_env_overrides_default(self):
        import os
        os.environ["SCANNER_EXECUTOR_CRYPTO_MOMENTUM_SL_PCT"] = "0.025"
        os.environ["SCANNER_EXECUTOR_CRYPTO_MOMENTUM_TP_PCT"] = "0.10"
        try:
            cfg = ScannerExecutorConfig.from_env()
            self.assertAlmostEqual(cfg.crypto_momentum_sl_pct_override, 0.025)
            self.assertAlmostEqual(cfg.crypto_momentum_tp_pct_override, 0.10)
        finally:
            del os.environ["SCANNER_EXECUTOR_CRYPTO_MOMENTUM_SL_PCT"]
            del os.environ["SCANNER_EXECUTOR_CRYPTO_MOMENTUM_TP_PCT"]


if __name__ == "__main__":
    unittest.main()
