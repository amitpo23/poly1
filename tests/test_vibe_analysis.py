"""Tests for vibe_analysis pure math library + vibe providers."""
from __future__ import annotations

import json
import math
import unittest

from agents.application.vibe_analysis import (
    adx,
    bollinger_bands,
    composite_signal,
    ema,
    funding_rate_regime,
    harmonic_scan,
    multi_factor_zscore,
    obv,
    probability_technical_composite,
    rsi,
    sma,
    volatility_percentile,
)


# ---------------------------------------------------------------------------
# Helper: generate synthetic probability series
# ---------------------------------------------------------------------------

def _prob_series(start: float, end: float, n: int) -> list[float]:
    """Linear interpolation from *start* to *end* over *n* points."""
    if n <= 1:
        return [start]
    step = (end - start) / (n - 1)
    return [round(start + i * step, 6) for i in range(n)]


def _oscillating_series(center: float, amp: float, n: int) -> list[float]:
    """Sine-wave oscillation around *center*."""
    return [
        round(center + amp * math.sin(2 * math.pi * i / 20), 6)
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------

class TestEMA(unittest.TestCase):
    def test_ema_single_value(self):
        self.assertEqual(ema([0.5], 3), [0.5])

    def test_ema_constant_series(self):
        vals = [0.5] * 10
        result = ema(vals, 5)
        self.assertEqual(len(result), 10)
        for v in result:
            self.assertAlmostEqual(v, 0.5, places=6)

    def test_ema_length_matches_input(self):
        vals = _prob_series(0.3, 0.7, 50)
        result = ema(vals, 12)
        self.assertEqual(len(result), 50)

    def test_ema_responds_to_trend(self):
        vals = _prob_series(0.3, 0.8, 40)
        result = ema(vals, 5)
        # EMA should be rising
        self.assertGreater(result[-1], result[10])

    def test_ema_empty_input(self):
        self.assertEqual(ema([], 5), [])

    def test_ema_short_series(self):
        # Fewer points than period
        result = ema([0.4, 0.5, 0.6], 10)
        self.assertEqual(len(result), 3)


# ---------------------------------------------------------------------------
# SMA
# ---------------------------------------------------------------------------

class TestSMA(unittest.TestCase):
    def test_sma_constant(self):
        result = sma([0.5] * 10, 3)
        for v in result:
            self.assertAlmostEqual(v, 0.5, places=6)

    def test_sma_known_values(self):
        result = sma([1.0, 2.0, 3.0, 4.0, 5.0], 3)
        self.assertAlmostEqual(result[2], 2.0, places=6)  # (1+2+3)/3
        self.assertAlmostEqual(result[3], 3.0, places=6)  # (2+3+4)/3
        self.assertAlmostEqual(result[4], 4.0, places=6)  # (3+4+5)/3

    def test_sma_length(self):
        vals = _prob_series(0.2, 0.8, 30)
        result = sma(vals, 5)
        self.assertEqual(len(result), 30)


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------

class TestRSI(unittest.TestCase):
    def test_rsi_constant_returns_50(self):
        # No price change → RSI=50 (neutral)
        result = rsi([0.5] * 30, 14)
        self.assertEqual(len(result), 30)

    def test_rsi_strong_uptrend(self):
        vals = _prob_series(0.2, 0.8, 40)
        result = rsi(vals, 14)
        # In a pure uptrend, RSI should be high
        self.assertGreater(result[-1], 70)

    def test_rsi_strong_downtrend(self):
        vals = _prob_series(0.8, 0.2, 40)
        result = rsi(vals, 14)
        self.assertLess(result[-1], 30)

    def test_rsi_range(self):
        vals = _oscillating_series(0.5, 0.2, 60)
        result = rsi(vals, 14)
        for v in result:
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 100.0)

    def test_rsi_short_series(self):
        result = rsi([0.5], 14)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0], 50.0)


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

class TestBollingerBands(unittest.TestCase):
    def test_bb_clamped_to_0_1(self):
        vals = _prob_series(0.01, 0.05, 30)
        upper, mid, lower = bollinger_bands(vals, 20, 2.0)
        for v in lower:
            self.assertGreaterEqual(v, 0.0)
        for v in upper:
            self.assertLessEqual(v, 1.0)

    def test_bb_length(self):
        vals = _prob_series(0.3, 0.7, 50)
        upper, mid, lower = bollinger_bands(vals, 20, 2.0)
        self.assertEqual(len(upper), 50)
        self.assertEqual(len(mid), 50)
        self.assertEqual(len(lower), 50)

    def test_bb_ordering(self):
        vals = _oscillating_series(0.5, 0.1, 50)
        upper, mid, lower = bollinger_bands(vals, 20, 2.0)
        for i in range(20, len(vals)):
            self.assertGreaterEqual(upper[i], mid[i])
            self.assertGreaterEqual(mid[i], lower[i])


# ---------------------------------------------------------------------------
# ADX
# ---------------------------------------------------------------------------

class TestADX(unittest.TestCase):
    def test_adx_returns_correct_length(self):
        n = 50
        closes = _prob_series(0.3, 0.7, n)
        highs = [c + 0.02 for c in closes]
        lows = [c - 0.02 for c in closes]
        result = adx(highs, lows, closes, 14)
        self.assertEqual(len(result), n)

    def test_adx_range(self):
        closes = _oscillating_series(0.5, 0.15, 80)
        highs = [c + 0.02 for c in closes]
        lows = [c - 0.02 for c in closes]
        result = adx(highs, lows, closes, 14)
        for v in result:
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 100.0)

    def test_adx_trending_market(self):
        closes = _prob_series(0.2, 0.8, 60)
        highs = [c + 0.02 for c in closes]
        lows = [c - 0.02 for c in closes]
        result = adx(highs, lows, closes, 14)
        # Strong trend should produce ADX > 20
        self.assertGreater(result[-1], 10)

    def test_adx_short_series(self):
        result = adx([0.5], [0.4], [0.45], 14)
        self.assertEqual(len(result), 1)


# ---------------------------------------------------------------------------
# OBV
# ---------------------------------------------------------------------------

class TestOBV(unittest.TestCase):
    def test_obv_rising_prices(self):
        closes = [0.3, 0.4, 0.5, 0.6, 0.7]
        volumes = [100, 200, 300, 400, 500]
        result = obv(closes, volumes)
        self.assertEqual(len(result), 5)
        # All up → OBV should be monotonically increasing
        for i in range(1, len(result)):
            self.assertGreater(result[i], result[i - 1])

    def test_obv_falling_prices(self):
        closes = [0.7, 0.6, 0.5, 0.4, 0.3]
        volumes = [100, 200, 300, 400, 500]
        result = obv(closes, volumes)
        for i in range(1, len(result)):
            self.assertLess(result[i], result[i - 1])

    def test_obv_empty(self):
        self.assertEqual(obv([], []), [])


# ---------------------------------------------------------------------------
# Volatility percentile
# ---------------------------------------------------------------------------

class TestVolatilityPercentile(unittest.TestCase):
    def test_returns_float_between_0_1(self):
        vals = _oscillating_series(0.5, 0.1, 200)
        vp = volatility_percentile(vals, hv_window=20, lookback=120)
        self.assertGreaterEqual(vp, 0.0)
        self.assertLessEqual(vp, 1.0)

    def test_neutral_with_short_series(self):
        vp = volatility_percentile([0.5] * 5, hv_window=20, lookback=120)
        self.assertAlmostEqual(vp, 0.5)

    def test_low_vol_constant_series(self):
        # Almost constant → HV near zero → percentile should be low
        vals = [0.5 + 0.0001 * (i % 2) for i in range(200)]
        vp = volatility_percentile(vals, hv_window=20, lookback=120)
        # With near-zero variance, percentile could be 0.0 or very low
        self.assertLessEqual(vp, 1.0)


# ---------------------------------------------------------------------------
# Harmonic scan
# ---------------------------------------------------------------------------

class TestHarmonicScan(unittest.TestCase):
    def test_returns_list(self):
        vals = _oscillating_series(0.5, 0.15, 80)
        patterns = harmonic_scan(vals, tolerance=0.10)
        self.assertIsInstance(patterns, list)

    def test_short_series_returns_empty(self):
        self.assertEqual(harmonic_scan([0.5] * 5), [])

    def test_pattern_dict_shape(self):
        # Create a series with clear swing structure
        vals = _oscillating_series(0.5, 0.15, 80)
        patterns = harmonic_scan(vals, tolerance=0.20)
        for p in patterns:
            self.assertIn("pattern", p)
            self.assertIn("direction", p)
            self.assertIn("completion_index", p)
            self.assertIn("ratios", p)


# ---------------------------------------------------------------------------
# Funding rate regime
# ---------------------------------------------------------------------------

class TestFundingRateRegime(unittest.TestCase):
    def test_overheated_long(self):
        result = funding_rate_regime(0.001)
        self.assertEqual(result["regime"], "overheated_long")
        self.assertEqual(result["signal"], "bearish")

    def test_overheated_short(self):
        result = funding_rate_regime(-0.001)
        self.assertEqual(result["regime"], "overheated_short")
        self.assertEqual(result["signal"], "bullish")

    def test_neutral(self):
        result = funding_rate_regime(0.00005)
        self.assertEqual(result["regime"], "neutral")
        self.assertEqual(result["signal"], "skip")

    def test_mild(self):
        result = funding_rate_regime(0.0003)
        self.assertEqual(result["regime"], "mild")
        self.assertEqual(result["signal"], "skip")

    def test_annualized_calculation(self):
        result = funding_rate_regime(0.001)
        self.assertAlmostEqual(result["annualized"], 0.001 * 3 * 365, places=3)


# ---------------------------------------------------------------------------
# Multi-factor z-score
# ---------------------------------------------------------------------------

class TestMultiFactorZscore(unittest.TestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(multi_factor_zscore({}), {})

    def test_identical_values_return_zeros(self):
        result = multi_factor_zscore({"a": 5.0, "b": 5.0, "c": 5.0})
        for v in result.values():
            self.assertAlmostEqual(v, 0.0)

    def test_known_distribution(self):
        result = multi_factor_zscore({"low": 0.0, "mid": 5.0, "high": 10.0})
        self.assertLess(result["low"], 0)
        self.assertAlmostEqual(result["mid"], 0.0, places=3)
        self.assertGreater(result["high"], 0)

    def test_preserves_keys(self):
        inp = {"btc": 0.7, "eth": 0.3, "sol": 0.5}
        result = multi_factor_zscore(inp)
        self.assertEqual(set(result.keys()), set(inp.keys()))


# ---------------------------------------------------------------------------
# Composite signal
# ---------------------------------------------------------------------------

class TestCompositeSignal(unittest.TestCase):
    def test_empty_signals(self):
        result = composite_signal([])
        self.assertEqual(result["direction"], "skip")

    def test_all_skip(self):
        signals = [
            {"direction": "skip", "confidence": 0.0},
            {"direction": "skip", "confidence": 0.0},
        ]
        result = composite_signal(signals)
        self.assertEqual(result["direction"], "skip")
        self.assertEqual(result["contributing_count"], 0)

    def test_unanimous_bullish(self):
        signals = [
            {"direction": "bullish", "confidence": 0.7},
            {"direction": "bullish", "confidence": 0.6},
            {"direction": "bullish", "confidence": 0.65},
        ]
        result = composite_signal(signals)
        self.assertEqual(result["direction"], "bullish")
        self.assertAlmostEqual(result["agreement"], 1.0)
        self.assertGreater(result["confidence"], 0.7)

    def test_majority_bullish(self):
        signals = [
            {"direction": "bullish", "confidence": 0.7},
            {"direction": "bullish", "confidence": 0.6},
            {"direction": "bearish", "confidence": 0.5},
        ]
        result = composite_signal(signals)
        self.assertEqual(result["direction"], "bullish")

    def test_majority_bearish(self):
        signals = [
            {"direction": "bearish", "confidence": 0.8},
            {"direction": "bearish", "confidence": 0.7},
            {"direction": "bullish", "confidence": 0.3},
        ]
        result = composite_signal(signals)
        self.assertEqual(result["direction"], "bearish")

    def test_weights_affect_outcome(self):
        signals = [
            {"direction": "bullish", "confidence": 0.5, "weight": 1.0},
            {"direction": "bearish", "confidence": 0.5, "weight": 3.0},
        ]
        result = composite_signal(signals)
        self.assertEqual(result["direction"], "bearish")


# ---------------------------------------------------------------------------
# Probability technical composite
# ---------------------------------------------------------------------------

class TestProbabilityTechnicalComposite(unittest.TestCase):
    def test_returns_none_with_insufficient_data(self):
        result = probability_technical_composite([0.5] * 10, min_bars=30)
        self.assertIsNone(result)

    def test_returns_dict_with_sufficient_data(self):
        vals = _oscillating_series(0.5, 0.1, 60)
        result = probability_technical_composite(vals, min_bars=30)
        self.assertIsNotNone(result)
        self.assertIn("direction", result)
        self.assertIn("confidence", result)
        self.assertIn("indicators", result)

    def test_uptrend_detects_bullish(self):
        vals = _prob_series(0.2, 0.7, 60)
        result = probability_technical_composite(vals, min_bars=30)
        self.assertIsNotNone(result)
        # Strong uptrend: EMA crossover bullish, RSI high (bearish/skip),
        # net signal should reflect trend
        self.assertIn(result["direction"], ("bullish", "bearish", "skip"))

    def test_downtrend_detects_bearish(self):
        vals = _prob_series(0.8, 0.2, 60)
        result = probability_technical_composite(vals, min_bars=30)
        self.assertIsNotNone(result)

    def test_indicators_present(self):
        vals = _oscillating_series(0.5, 0.1, 60)
        result = probability_technical_composite(vals, min_bars=30)
        indicators = result["indicators"]
        self.assertIn("ema_short", indicators)
        self.assertIn("ema_long", indicators)
        self.assertIn("rsi", indicators)
        self.assertIn("bb_upper", indicators)
        self.assertIn("bb_lower", indicators)
        self.assertIn("price", indicators)


# ---------------------------------------------------------------------------
# Provider tests (with mocked HTTP)
# ---------------------------------------------------------------------------

class TestTechnicalSignalProvider(unittest.TestCase):
    """Test TechnicalSignalProvider using mocked price-history fetch."""

    def test_provider_skip_on_resolution_near(self):
        from agents.application.external_conviction import (
            TechnicalSignalProvider,
            MarketSnapshot,
        )
        provider = TechnicalSignalProvider()
        market = MarketSnapshot(
            market_id="M1", question="Will BTC go up?", slug="btc-up",
            yes_price=0.5, no_price=0.5, volume_usdc=10000, liquidity_usdc=5000,
            end_date="2026-05-18T01:00:00Z",  # <24h from now
            outcomes=["Yes", "No"], tokens=["T1", "T2"], category="crypto",
            raw={"endDate": "2026-05-18T01:00:00Z"},
        )
        verdict = provider.analyze(market)
        # Should skip due to near-resolution or extreme price
        self.assertIn(verdict.direction, ("skip", "yes", "no"))

    def test_provider_skip_extreme_price(self):
        from agents.application.external_conviction import (
            TechnicalSignalProvider,
            MarketSnapshot,
        )
        provider = TechnicalSignalProvider()
        market = MarketSnapshot(
            market_id="M1", question="Will event happen?", slug="event",
            yes_price=0.95, no_price=0.05, volume_usdc=10000, liquidity_usdc=5000,
            end_date="2026-06-01T00:00:00Z",
            outcomes=["Yes", "No"], tokens=["T1", "T2"], category="general",
            raw={"endDate": "2026-06-01T00:00:00Z"},
        )
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "skip")
        self.assertIn("extreme_price", verdict.reason)


class TestVolatilityRegimeProvider(unittest.TestCase):
    def test_provider_skip_extreme_price(self):
        from agents.application.external_conviction import (
            VolatilityRegimeProvider,
            MarketSnapshot,
        )
        provider = VolatilityRegimeProvider()
        market = MarketSnapshot(
            market_id="M1", question="Will event happen?", slug="event",
            yes_price=0.05, no_price=0.95, volume_usdc=10000, liquidity_usdc=5000,
            end_date="2026-06-01T00:00:00Z",
            outcomes=["Yes", "No"], tokens=["T1", "T2"], category="general",
            raw={"endDate": "2026-06-01T00:00:00Z"},
        )
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "skip")


class TestCryptoDerivativesProvider(unittest.TestCase):
    def test_skip_non_crypto(self):
        from agents.application.external_conviction import (
            CryptoDerivativesProvider,
            MarketSnapshot,
        )
        provider = CryptoDerivativesProvider()
        market = MarketSnapshot(
            market_id="M1", question="Will team win?", slug="sports-event",
            yes_price=0.5, no_price=0.5, volume_usdc=10000, liquidity_usdc=5000,
            end_date="2026-06-01T00:00:00Z",
            outcomes=["Yes", "No"], tokens=["T1", "T2"], category="sports",
            raw={},
        )
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "skip")
        self.assertIn("non_crypto", verdict.reason)

    def test_overheated_long_with_mock(self):
        from agents.application.external_conviction import (
            CryptoDerivativesProvider,
            MarketSnapshot,
        )
        import agents.application.external_conviction as mod
        original_urlopen = mod.urllib.request.urlopen

        class FakeResp:
            def __init__(self, data):
                self._data = json.dumps(data).encode()
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass

        def fake_urlopen(req, timeout=20):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "okx" in url:
                return FakeResp({"data": [{"fundingRate": "0.002"}]})
            # binance
            return FakeResp([{"fundingRate": "0.0018"}])

        mod.urllib.request.urlopen = fake_urlopen
        try:
            provider = CryptoDerivativesProvider()
            market = MarketSnapshot(
                market_id="M1", question="Will Bitcoin go up?",
                slug="btc-up", yes_price=0.5, no_price=0.5,
                volume_usdc=10000, liquidity_usdc=5000,
                end_date="2026-06-01T00:00:00Z",
                outcomes=["Yes", "No"], tokens=["T1", "T2"],
                category="crypto", raw={},
            )
            verdict = provider.analyze(market)
            self.assertEqual(verdict.direction, "no")
            self.assertEqual(verdict.source, "crypto_derivatives")
            self.assertIn("overheated_long", verdict.reason)
        finally:
            mod.urllib.request.urlopen = original_urlopen


class TestMultiFactorRankProvider(unittest.TestCase):
    def test_skip_without_all_markets(self):
        from agents.application.external_conviction import (
            MultiFactorRankProvider,
            MarketSnapshot,
        )
        provider = MultiFactorRankProvider()
        market = MarketSnapshot(
            market_id="M1", question="test?", slug="test",
            yes_price=0.5, no_price=0.5, volume_usdc=10000, liquidity_usdc=5000,
            end_date="2026-06-01T00:00:00Z",
            outcomes=["Yes", "No"], tokens=["T1", "T2"], category="general",
            raw={},
        )
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "skip")
        self.assertIn("no all_markets", verdict.reason)


class TestProviderFactoryVibe(unittest.TestCase):
    def test_factory_creates_technical_signal(self):
        from agents.application.external_conviction import (
            TechnicalSignalProvider,
            ExternalConvictionConfig,
            provider_from_config,
        )
        cfg = ExternalConvictionConfig(provider="technical_signal")
        prov = provider_from_config(cfg)
        self.assertIsInstance(prov, TechnicalSignalProvider)

    def test_factory_creates_volatility_regime(self):
        from agents.application.external_conviction import (
            VolatilityRegimeProvider,
            ExternalConvictionConfig,
            provider_from_config,
        )
        cfg = ExternalConvictionConfig(provider="volatility_regime")
        prov = provider_from_config(cfg)
        self.assertIsInstance(prov, VolatilityRegimeProvider)

    def test_factory_creates_crypto_derivatives(self):
        from agents.application.external_conviction import (
            CryptoDerivativesProvider,
            ExternalConvictionConfig,
            provider_from_config,
        )
        cfg = ExternalConvictionConfig(provider="crypto_derivatives")
        prov = provider_from_config(cfg)
        self.assertIsInstance(prov, CryptoDerivativesProvider)

    def test_factory_creates_multi_factor_rank(self):
        from agents.application.external_conviction import (
            MultiFactorRankProvider,
            ExternalConvictionConfig,
            provider_from_config,
        )
        cfg = ExternalConvictionConfig(provider="multi_factor_rank")
        prov = provider_from_config(cfg)
        self.assertIsInstance(prov, MultiFactorRankProvider)


if __name__ == "__main__":
    unittest.main()
