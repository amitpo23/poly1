import importlib.util
import sys
import unittest
from pathlib import Path


def _load_guard():
    path = Path(__file__).resolve().parents[1] / "scripts" / "live_equity_guard.py"
    spec = importlib.util.spec_from_file_location("live_equity_guard_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LiveEquityGuardTests(unittest.TestCase):
    def test_open_position_value_offsets_cash_drop(self):
        guard = _load_guard()
        positions = guard.compute_position_values(
            [{
                "id": 1,
                "market_id": "M1",
                "token_id": "TOK",
                "side": "BUY",
                "price": 0.50,
                "size_usdc": 1.0,
            }],
            lambda token_id: {"mid": 0.50},
        )
        snapshot = guard.compute_equity_snapshot(
            cash_usdc=99.0,
            baseline_usdc=100.0,
            drawdown_limit_usdc=0.75,
            positions=positions,
        )
        self.assertAlmostEqual(snapshot.open_mtm_usdc, 1.0)
        self.assertAlmostEqual(snapshot.equity_usdc, 100.0)
        self.assertFalse(snapshot.breached)

    def test_breaches_on_real_mtm_drawdown(self):
        guard = _load_guard()
        positions = guard.compute_position_values(
            [{
                "id": 1,
                "market_id": "M1",
                "token_id": "TOK",
                "side": "BUY",
                "price": 0.50,
                "size_usdc": 1.0,
            }],
            lambda token_id: {"mid": 0.05},
        )
        snapshot = guard.compute_equity_snapshot(
            cash_usdc=99.0,
            baseline_usdc=100.0,
            drawdown_limit_usdc=0.75,
            positions=positions,
        )
        self.assertAlmostEqual(snapshot.open_mtm_usdc, 0.1)
        self.assertAlmostEqual(snapshot.equity_usdc, 99.1)
        self.assertTrue(snapshot.breached)

    def test_midpoint_failure_falls_back_to_entry(self):
        guard = _load_guard()

        def fail(_token_id):
            raise RuntimeError("midpoint unavailable")

        positions = guard.compute_position_values(
            [{
                "id": 1,
                "market_id": "M1",
                "token_id": "TOK",
                "side": "BUY",
                "price": 0.25,
                "size_usdc": 2.0,
            }],
            fail,
        )
        self.assertAlmostEqual(positions[0].mtm_usdc, 2.0)
        self.assertEqual(positions[0].midpoint_source, "entry_fallback")


if __name__ == "__main__":
    unittest.main()
