"""Tests for agents/application/bayesian_aggregator.py."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.application.bayesian_aggregator import (
    EdgeResult,
    _implied_p_win_for_side,
    compute_edge,
)


def _fake_calibration(per_source_band: dict = None,
                       per_signal_source: dict = None,
                       per_price_band: dict = None) -> dict:
    """Build a calibration dict in the format probability_calibrator emits.

    Each segment value is (wins, losses); the function constructs the
    CalibrationStat-like entries (wins, losses, total, winrate, wilson).
    """
    import math
    def _to_entries(segment_dict, segment_name):
        entries = []
        for key, (wins, losses) in (segment_dict or {}).items():
            n = wins + losses
            wr = wins / n if n else None
            wilson = None
            if n:
                z = 1.96
                p = wins / n
                denom = 1 + z*z/n
                center = (p + z*z/(2*n)) / denom
                half = (z/denom) * math.sqrt(p*(1-p)/n + z*z/(4*n*n))
                wilson = max(0.0, center - half)
            entries.append({
                "key": key, "segment": segment_name,
                "wins": wins, "losses": losses, "total": n,
                "winrate": round(wr, 4) if wr is not None else None,
                "wilson_lower": round(wilson, 4) if wilson is not None else None,
            })
        return sorted(entries, key=lambda x: -x["total"])
    return {
        "per_source_band": _to_entries(per_source_band, "source|band"),
        "per_signal_source": _to_entries(per_signal_source, "signal_source"),
        "per_price_band": _to_entries(per_price_band, "price_band"),
        "per_action": [],
        "per_market_type": [],
    }


class ImpliedProbabilityTests(unittest.TestCase):
    def test_buy_implied_eq_entry_price(self):
        self.assertAlmostEqual(_implied_p_win_for_side("BUY", 0.40), 0.40)
        self.assertAlmostEqual(_implied_p_win_for_side("BUY", 0.55), 0.55)

    def test_sell_implied_eq_one_minus_entry(self):
        self.assertAlmostEqual(_implied_p_win_for_side("SELL", 0.40), 0.60)
        self.assertAlmostEqual(_implied_p_win_for_side("SELL", 0.75), 0.25)

    def test_invalid_returns_neutral(self):
        self.assertEqual(_implied_p_win_for_side("BUY", 0.0), 0.5)
        self.assertEqual(_implied_p_win_for_side("BUY", 1.0), 0.5)
        self.assertEqual(_implied_p_win_for_side("WHAT", 0.5), 0.5)


class ComputeEdgeTests(unittest.TestCase):
    def test_actionable_when_calibrated_beats_implied_by_margin(self):
        # alphainsider|0.40-0.49 has 36% winrate at n=22 in real data;
        # Wilson lower ≈ 0.20. For BUY at 0.40, implied = 0.40.
        # Hand-craft a high-winrate segment so we DO get edge.
        # Want: wilson ≈ 0.55, implied 0.40, edge 0.15 → actionable.
        # 60 wins / 100 → wilson ~0.50, edge ~0.10, actionable at min_edge=0.05.
        cal = _fake_calibration(
            per_source_band={"alphainsider|0.40-0.49": (60, 40)},
        )
        candidate = {
            "signal_source": "alphainsider",
            "action": "BUY",
            "entry_price": 0.40,
        }
        result = compute_edge(candidate, cal, min_edge=0.05, min_samples=5)
        self.assertIsInstance(result, EdgeResult)
        self.assertEqual(result.sample_size, 100)
        self.assertTrue(result.actionable, f"expected actionable, got {result}")
        self.assertGreater(result.edge, 0.05)
        self.assertEqual(result.implied_p_win, 0.40)

    def test_not_actionable_when_edge_below_min(self):
        # 50/50 winrate → wilson ≈ 0.40; implied for BUY @ 0.40 = 0.40. Edge 0.0.
        cal = _fake_calibration(
            per_source_band={"src|0.40-0.49": (50, 50)},
        )
        result = compute_edge(
            {"signal_source": "src", "action": "BUY", "entry_price": 0.40},
            cal, min_edge=0.05, min_samples=5,
        )
        self.assertFalse(result.actionable)
        self.assertIn("below_min", result.reason)

    def test_falls_back_to_prior_when_segment_below_min_samples(self):
        cal = _fake_calibration(
            per_source_band={"rare|0.40-0.49": (1, 0)},  # n=1, below min
        )
        result = compute_edge(
            {"signal_source": "rare", "action": "BUY", "entry_price": 0.40},
            cal, min_edge=0.05, min_samples=5, fallback_global_prior=0.25,
        )
        self.assertEqual(result.p_win_calibrated, 0.25)
        self.assertEqual(result.sample_size, 0)
        self.assertFalse(result.actionable)
        self.assertIn("no_segment_above_min_samples", result.reason)

    def test_sell_implied_calculation(self):
        cal = _fake_calibration(
            per_source_band={"src|0.50-0.54": (70, 30)},  # 70% wr at n=100
        )
        # SELL @ 0.50 → implied = 0.50. If our wilson ≈ 0.61, edge ≈ 0.11.
        result = compute_edge(
            {"signal_source": "src", "action": "SELL", "entry_price": 0.50},
            cal, min_edge=0.05, min_samples=5,
        )
        self.assertEqual(result.implied_p_win, 0.50)
        self.assertGreater(result.edge, 0.05)
        self.assertTrue(result.actionable)

    def test_fallback_specificity_hierarchy(self):
        # No source|band match, but source has data — should use source segment
        cal = _fake_calibration(
            per_source_band={},
            per_signal_source={"alphainsider": (60, 40)},  # n=100
        )
        result = compute_edge(
            {"signal_source": "alphainsider", "action": "BUY", "entry_price": 0.40},
            cal, min_samples=5,
        )
        self.assertEqual(result.sample_size, 100)
        self.assertIn("signal_source:alphainsider", result.source_segment)

    def test_features_payload(self):
        cal = _fake_calibration(per_source_band={"x|0.40-0.49": (60, 40)})
        result = compute_edge(
            {"signal_source": "x", "action": "BUY", "entry_price": 0.40}, cal,
        )
        f = result.as_features()
        self.assertIn("bayesian_p_win_calibrated", f)
        self.assertIn("bayesian_edge", f)
        self.assertIn("bayesian_actionable", f)
        self.assertEqual(f["bayesian_sample_size"], 100)

    def test_invalid_price_returns_non_actionable(self):
        cal = _fake_calibration(per_source_band={"x|0.40-0.49": (60, 40)})
        result = compute_edge(
            {"signal_source": "x", "action": "BUY", "entry_price": "garbage"}, cal,
        )
        self.assertFalse(result.actionable)
        self.assertEqual(result.reason, "invalid_entry_price")


if __name__ == "__main__":
    unittest.main()
