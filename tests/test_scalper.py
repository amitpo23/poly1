import unittest

from agents.application.scalper import ScalpPair, ScalperConfig


class TestScalpPair(unittest.TestCase):
    def setUp(self):
        self.cfg = ScalperConfig()  # all defaults
        self.pair = ScalpPair(slug="btc-updown-15m-100", period_ts=100,
                               up_token="u", down_token="d", cfg=self.cfg)

    def test_ineligible_when_ask_above_threshold(self):
        # threshold = 0.499, ask = 0.55 → ineligible
        self.pair.apply_tick("up", best_ask=0.55, now_ms=1000)
        self.assertIsNone(self.pair.temp_price_up)

    def test_temp_price_tracks_running_low(self):
        self.pair.apply_tick("up", best_ask=0.49, now_ms=1000)
        self.assertEqual(self.pair.temp_price_up, 0.49)
        self.pair.apply_tick("up", best_ask=0.47, now_ms=1100)
        self.assertEqual(self.pair.temp_price_up, 0.47)
        self.pair.apply_tick("up", best_ask=0.48, now_ms=1200)
        self.assertEqual(self.pair.temp_price_up, 0.47, "running low must not increase")

    def test_temp_price_resets_on_ineligibility(self):
        self.pair.apply_tick("up", best_ask=0.45, now_ms=1000)
        self.assertEqual(self.pair.temp_price_up, 0.45)
        self.pair.apply_tick("up", best_ask=0.51, now_ms=1100)  # > threshold
        self.assertIsNone(self.pair.temp_price_up,
                          "becoming ineligible must reset tracker")

    def test_per_side_independence(self):
        self.pair.apply_tick("up", best_ask=0.45, now_ms=1000)
        self.pair.apply_tick("down", best_ask=0.49, now_ms=1000)
        self.assertEqual(self.pair.temp_price_up, 0.45)
        self.assertEqual(self.pair.temp_price_down, 0.49)

    def test_no_signal_while_no_temp_price(self):
        self.pair.apply_tick("up", best_ask=0.55, now_ms=1000)
        sig = self.pair.evaluate_entry("up", best_ask=0.55, now_ms=1000)
        self.assertIsNone(sig)

    def test_no_signal_when_just_setting_low(self):
        self.pair.apply_tick("up", best_ask=0.45, now_ms=1000)
        sig = self.pair.evaluate_entry("up", best_ask=0.45, now_ms=1100)
        self.assertIsNone(sig, "must wait for reversal or deeper drop")

    def test_reversal_trigger_at_2c_bounce(self):
        self.pair.apply_tick("up", best_ask=0.45, now_ms=1000)
        sig = self.pair.evaluate_entry("up", best_ask=0.47, now_ms=1100)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["reason"], "reversal")
        self.assertAlmostEqual(sig["price"], 0.47)

    def test_reversal_trigger_below_2c_does_not_fire(self):
        self.pair.apply_tick("up", best_ask=0.45, now_ms=1000)
        sig = self.pair.evaluate_entry("up", best_ask=0.469, now_ms=1100)
        self.assertIsNone(sig)

    def test_depth_trigger_at_5pct_discount(self):
        self.pair.apply_tick("up", best_ask=0.40, now_ms=1000)
        sig = self.pair.evaluate_entry("up", best_ask=0.38, now_ms=1100)
        self.assertIsNotNone(sig)
        self.assertEqual(sig["reason"], "depth")

    def test_depth_trigger_above_threshold_resets(self):
        self.pair.apply_tick("up", best_ask=0.40, now_ms=1000)
        sig = self.pair.evaluate_entry("up", best_ask=0.55, now_ms=1100)
        self.assertIsNone(sig)


if __name__ == "__main__":
    unittest.main()
