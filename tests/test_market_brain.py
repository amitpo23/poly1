import unittest

from agents.application.market_brain import (
    BrainConfig,
    CryptoSignalFeed,
    ExitPosition,
    MarketBrain,
)


class _StaticCryptoFeed:
    def __init__(self, changes):
        self.changes = changes

    def update(self, asset):
        from agents.application.market_brain import CryptoSignal
        return CryptoSignal(
            asset=asset,
            price=100.0,
            changes=self.changes,
            samples=3,
            fresh=True,
        )


class TestMarketBrain(unittest.TestCase):
    def setUp(self):
        self.brain = MarketBrain(BrainConfig(
            enabled=True,
            scalper_min_seconds_to_expiry=90,
            scalper_max_entry_price=0.55,
            scalper_max_pair_ask_sum=1.04,
            scalper_min_edge_score=0.35,
            exit_take_profit_pct=0.05,
            exit_trailing_stop_pct=0.02,
            exit_stop_loss_pct=0.07,
            exit_max_hold_seconds=1800,
            smart_exit_enabled=True,
            smart_exit_min_profit_pct=0.05,
            smart_exit_min_momentum_pct=0.001,
            smart_exit_peak_drawdown_hold_pct=0.006,
            smart_exit_min_seconds_to_expiry=75,
        ))

    def test_classifies_crypto_15m_slug(self):
        profile = self.brain.classify("eth-updown-15m-1770000000")
        self.assertEqual(profile.market_type, "crypto_15m")
        self.assertEqual(profile.asset, "eth")
        self.assertEqual(profile.period_ts, 1770000000)

    def test_approves_good_scalper_reversal(self):
        decision = self.brain.evaluate_scalper_entry(
            slug="eth-updown-15m-1770000000",
            side="up",
            up_ask=0.471,
            down_ask=0.50,
            candidate_price=0.471,
            signal_reason="reversal",
            now_ms=(1770000000 - 300) * 1000,
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.reason, "approved")
        self.assertGreaterEqual(decision.score, 0.35)

    def test_vetoes_too_close_to_expiry(self):
        decision = self.brain.evaluate_scalper_entry(
            slug="sol-updown-15m-1770000000",
            side="down",
            up_ask=0.45,
            down_ask=0.50,
            candidate_price=0.45,
            signal_reason="depth",
            now_ms=(1770000000 - 30) * 1000,
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "too_close_to_expiry")

    def test_vetoes_expensive_pair(self):
        decision = self.brain.evaluate_scalper_entry(
            slug="btc-updown-15m-1770000000",
            side="up",
            up_ask=0.52,
            down_ask=0.53,
            candidate_price=0.52,
            signal_reason="reversal",
            now_ms=(1770000000 - 300) * 1000,
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "pair_ask_sum_too_expensive")

    def test_unknown_market_allowed_when_not_strict(self):
        decision = self.brain.evaluate_scalper_entry(
            slug="custom-market",
            side="up",
            up_ask=0.45,
            down_ask=0.50,
            candidate_price=0.45,
            signal_reason="depth",
            now_ms=1000,
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.reason, "unknown_market_allowed_non_strict")

    def test_exit_take_profit(self):
        decision = self.brain.evaluate_exit(ExitPosition(
            market_id="eth-updown-15m-1770000000",
            token_id="tok",
            side="up",
            entry_price=0.50,
            current_price=0.53,
            max_price_seen=0.53,
            opened_ts_ms=1000,
        ), now_ms=10_000)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.reason, "take_profit")

    def test_exit_trailing_stop_after_profit(self):
        decision = self.brain.evaluate_exit(ExitPosition(
            market_id="eth-updown-15m-1770000000",
            token_id="tok",
            side="up",
            entry_price=0.50,
            current_price=0.535,
            max_price_seen=0.55,
            opened_ts_ms=1000,
        ), now_ms=10_000)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.reason, "trailing_stop_after_profit")

    def test_exit_stop_loss(self):
        decision = self.brain.evaluate_exit(ExitPosition(
            market_id="eth-updown-15m-1770000000",
            token_id="tok",
            side="up",
            entry_price=0.50,
            current_price=0.46,
            opened_ts_ms=1000,
        ), now_ms=10_000)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.reason, "stop_loss")

    def test_smart_exit_holds_profit_when_momentum_supports_side(self):
        brain = MarketBrain(
            self.brain.cfg,
            crypto_feed=_StaticCryptoFeed({"60s": 0.002}),
        )
        decision = brain.evaluate_exit(ExitPosition(
            market_id="eth-updown-15m-1770000000",
            token_id="tok",
            side="up",
            entry_price=0.50,
            current_price=0.53,
            max_price_seen=0.531,
            opened_ts_ms=1000,
        ), now_ms=(1770000000 - 300) * 1000)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "hold_profit_with_momentum")
        self.assertTrue(decision.features["smart_exit_supports_side"])

    def test_smart_exit_does_not_hold_when_momentum_disagrees(self):
        brain = MarketBrain(
            self.brain.cfg,
            crypto_feed=_StaticCryptoFeed({"60s": -0.002}),
        )
        decision = brain.evaluate_exit(ExitPosition(
            market_id="eth-updown-15m-1770000000",
            token_id="tok",
            side="up",
            entry_price=0.50,
            current_price=0.53,
            max_price_seen=0.531,
            opened_ts_ms=1000,
        ), now_ms=(1770000000 - 300) * 1000)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.reason, "take_profit")


class TestTimeoutGrace(unittest.TestCase):
    """Fix 4: flat positions get a grace period before timeout sell."""

    def _brain(self, grace_pct=0.01, grace_sec=3600, max_hold=1800):
        return MarketBrain(BrainConfig(
            enabled=True,
            exit_take_profit_pct=0.05,
            exit_trailing_stop_pct=0.02,
            exit_stop_loss_pct=0.07,
            exit_max_hold_seconds=max_hold,
            exit_timeout_flat_grace_pct=grace_pct,
            exit_timeout_grace_seconds=grace_sec,
        ))

    def test_flat_at_timeout_gets_grace(self):
        brain = self._brain()
        # Position is 1801s old (past 1800s max_hold), pnl ~0% → grace
        decision = brain.evaluate_exit(ExitPosition(
            market_id="test-mkt",
            token_id="tok",
            side="up",
            entry_price=0.50,
            current_price=0.503,  # +0.6% < 1% grace threshold
            opened_ts_ms=0,
        ), now_ms=1801_000)
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "timeout_grace_flat")

    def test_grace_expired_forces_timeout(self):
        brain = self._brain()
        # Position is 5401s old (past max_hold + grace_seconds) → timeout
        decision = brain.evaluate_exit(ExitPosition(
            market_id="test-mkt",
            token_id="tok",
            side="up",
            entry_price=0.50,
            current_price=0.503,
            opened_ts_ms=0,
        ), now_ms=5401_000)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.reason, "timeout")

    def test_losing_at_timeout_no_grace(self):
        brain = self._brain()
        # Position is past timeout with pnl = -4% (beyond grace_pct) → timeout
        decision = brain.evaluate_exit(ExitPosition(
            market_id="test-mkt",
            token_id="tok",
            side="up",
            entry_price=0.50,
            current_price=0.48,  # -4% > 1% grace threshold
            opened_ts_ms=0,
        ), now_ms=1801_000)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.reason, "timeout")

    def test_stop_loss_fires_before_grace(self):
        brain = self._brain()
        # Position is past timeout but also past stop_loss (-8%) → stop_loss
        decision = brain.evaluate_exit(ExitPosition(
            market_id="test-mkt",
            token_id="tok",
            side="up",
            entry_price=0.50,
            current_price=0.46,  # -8% → stop_loss fires first
            opened_ts_ms=0,
        ), now_ms=1801_000)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.reason, "stop_loss")


class TestCryptoSignalFeed(unittest.TestCase):
    def test_percent_change_from_samples(self):
        feed = CryptoSignalFeed()
        feed._samples["eth"].append((1000, 100.0))
        feed._samples["eth"].append((31_000, 103.0))
        self.assertAlmostEqual(feed.percent_change("eth", 30), 0.03)
        snap = feed.snapshot("eth")
        self.assertEqual(snap.asset, "eth")
        self.assertEqual(snap.price, 103.0)
        self.assertEqual(snap.samples, 2)


if __name__ == "__main__":
    unittest.main()
