"""Tests for MetaBrain — the unified synthesizing layer."""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agents.application.meta_brain import (
    ConvictionJSONLReader,
    ConvictionSummary,
    EvidenceClaim,
    EvidenceRouter,
    MetaBrain,
    MetaDecision,
    ProbVelocityDetector,
    ReliabilityStats,
    SourceReliabilityAdvisor,
    VelocitySignal,
    WinRateAdvisor,
    WinRateStats,
    BreakingNewsReader,
    EquityFairValueSignal,
    AlpacaMarketDataReader,
    AlpacaMarketSignal,
    NewsSignal,
    WhaleSentimentSignal,
)
from agents.application.execution_quality import ExecutionQualityAdvisor
from agents.application.sizing import (
    binary_kelly_fraction,
    binary_raw_ev,
    kelly_size_usdc,
)
from agents.application.trade_log import TradeLog


# ---------------------------------------------------------------------------
# WinRateAdvisor
# ---------------------------------------------------------------------------

class TestWinRateAdvisor(unittest.TestCase):

    def _make_db(self, rows):
        """Create an in-memory sqlite DB with brain_decisions rows."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE brain_decisions (
                id INTEGER PRIMARY KEY, ts TEXT, agent TEXT, strategy TEXT,
                decision_type TEXT, market_id TEXT, token_id TEXT,
                approved INTEGER, reason TEXT, score REAL,
                market_type TEXT, asset TEXT, features_json TEXT,
                action TEXT, outcome_status TEXT, outcome_json TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE trades (
                id INTEGER PRIMARY KEY, ts TEXT, cycle_id TEXT, market_id TEXT,
                token_id TEXT, side TEXT, price REAL, size_usdc REAL,
                confidence REAL, status TEXT, response_json TEXT, error TEXT
            )"""
        )
        for r in rows:
            conn.execute(
                "INSERT INTO brain_decisions (ts, agent, strategy, decision_type, "
                "market_id, approved, reason, score, market_type, outcome_status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r["ts"], "test", "test", "entry", r.get("market_id", "m1"),
                 r.get("approved", 1), r.get("reason", "ok"),
                 r.get("score", 0.6), r.get("market_type", "general_binary"),
                 r.get("outcome_status")),
            )
        conn.commit()
        return conn

    def test_no_db_returns_none(self):
        adv = WinRateAdvisor()
        stats = adv.compute("/nonexistent/path.db")
        self.assertIsNone(stats.winrate)
        self.assertEqual(stats.source, "no_db")

    def test_winrate_from_brain_decisions(self):
        adv = WinRateAdvisor()
        rows = [
            {"ts": "2026-05-19T10:00:00", "approved": 1, "outcome_status": "closed_take_profit"},
            {"ts": "2026-05-19T11:00:00", "approved": 1, "outcome_status": "closed_take_profit"},
            {"ts": "2026-05-19T12:00:00", "approved": 1, "outcome_status": "closed_stop_loss"},
        ]
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = self._make_db(rows)
            # Patch the advisor to use our conn directly.
            stats = adv._from_brain_decisions(conn, "general_binary", 240)
            self.assertAlmostEqual(stats.winrate, 2 / 3)
            self.assertEqual(stats.wins, 2)
            self.assertEqual(stats.losses, 1)
        finally:
            os.unlink(db_path)

    def test_winrate_filters_by_market_type(self):
        adv = WinRateAdvisor()
        rows = [
            {
                "ts": "2026-05-19T10:00:00",
                "approved": 1,
                "market_type": "general_binary",
                "outcome_status": "closed_take_profit",
            },
            {
                "ts": "2026-05-19T11:00:00",
                "approved": 1,
                "market_type": "crypto_15m",
                "outcome_status": "closed_stop_loss",
            },
        ]
        conn = self._make_db(rows)
        stats = adv._from_brain_decisions(conn, "general_binary", 240)
        self.assertEqual(stats.wins, 1)
        self.assertEqual(stats.losses, 0)
        self.assertEqual(stats.total_with_outcome, 1)

    def test_all_pending_falls_back_to_trades(self):
        """If brain_decisions has no resolved outcomes, falls back to trades table."""
        adv = WinRateAdvisor()
        rows = [
            {"ts": "2026-05-19T10:00:00", "approved": 1, "outcome_status": None},
        ]
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = self._make_db(rows)
            conn.execute(
                "INSERT INTO trades (ts, cycle_id, market_id, token_id, side, "
                "price, size_usdc, confidence, status) VALUES (?,?,?,?,?,?,?,?,?)",
                ("2026-05-19T10:00:00", "c1", "m1", None, "BUY",
                 0.6, 10.0, 0.7, "closed_take_profit"),
            )
            conn.commit()
            stats = adv._from_trades_table(conn, 240)
            self.assertEqual(stats.wins, 1)
            self.assertEqual(stats.source, "trades")
        finally:
            os.unlink(db_path)

    def test_daily_journal_refines_intraday_winrate(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            log = TradeLog(db_path=db_path)
            log.insert_brain_decision(
                agent="scalper",
                strategy="test",
                decision_type="entry",
                market_id="m1",
                approved=True,
                reason="approved",
                score=0.8,
                market_type="general_binary",
            )
            with log._connect() as conn:
                conn.execute(
                    "UPDATE brain_decisions SET outcome_status='closed_stop_loss'"
                )
            adv = WinRateAdvisor(cache_ttl_sec=0)
            stats = adv.compute(db_path, market_type="general_binary", hours=24)
            self.assertEqual(stats.source, "brain_decisions+daily_journal")
            self.assertLess(stats.winrate, 0.52)
            self.assertEqual(stats.total_with_outcome, 1)
        finally:
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# SourceReliability + EvidenceRouter
# ---------------------------------------------------------------------------

class TestEvidenceRouter(unittest.TestCase):
    def test_solo_trigger_requires_reliable_track_record(self):
        router = EvidenceRouter()
        route = router.route([
            EvidenceClaim(
                source_id="wallet:abc",
                source_type="wallet",
                direction="yes",
                probability=0.72,
                confidence=0.9,
                reliability=ReliabilityStats(
                    source_id="wallet:abc",
                    winrate=0.72,
                    wins=72,
                    losses=28,
                    sample_size=100,
                    wilson_lower=0.62,
                    source="test",
                ),
            )
        ])
        self.assertEqual(route.mode, "solo")
        self.assertEqual(route.leader.source_id, "wallet:abc")

    def test_unproven_high_confidence_does_not_solo(self):
        router = EvidenceRouter()
        route = router.route([
            EvidenceClaim(
                source_id="news",
                source_type="news",
                direction="yes",
                probability=0.95,
                confidence=1.0,
                reliability=None,
            )
        ])
        self.assertEqual(route.mode, "consensus")

    def test_source_reliability_reads_signal_source(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            log = TradeLog(db_path=db_path)
            for idx in range(8):
                decision_id = log.insert_brain_decision(
                    agent="wallet_follow",
                    strategy="wallet_follow",
                    decision_type="entry",
                    market_id=f"m{idx}",
                    approved=True,
                    reason="ok",
                    score=0.7,
                    market_type="general_binary",
                    signal_source="wallet:abc",
                )
                log.update_brain_decision_outcome(decision_id, "closed_take_profit", {})
            for idx in range(2):
                decision_id = log.insert_brain_decision(
                    agent="wallet_follow",
                    strategy="wallet_follow",
                    decision_type="entry",
                    market_id=f"l{idx}",
                    approved=True,
                    reason="ok",
                    score=0.7,
                    market_type="general_binary",
                    signal_source="wallet:abc",
                )
                log.update_brain_decision_outcome(decision_id, "closed_stop_loss", {})
            stats = SourceReliabilityAdvisor(cache_ttl_sec=0).compute(
                db_path,
                "wallet:abc",
                hours=9999,
            )
            self.assertEqual(stats.sample_size, 10)
            self.assertAlmostEqual(stats.winrate, 0.8)
            self.assertGreater(stats.wilson_lower, 0.49)
        finally:
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# Sizing helpers
# ---------------------------------------------------------------------------

class TestKellySizing(unittest.TestCase):
    def test_binary_kelly_fraction(self):
        self.assertAlmostEqual(binary_kelly_fraction(0.60, 0.50), 0.20)
        self.assertEqual(binary_kelly_fraction(0.49, 0.50), 0.0)

    def test_raw_ev_is_price_normalized(self):
        self.assertAlmostEqual(binary_raw_ev(0.60, 0.50), 0.20)
        self.assertAlmostEqual(binary_raw_ev(0.33, 0.30), 0.10)

    @patch.dict(os.environ, {
        "KELLY_SIZING_ENABLED": "true",
        "KELLY_FRACTION_SCALE": "0.25",
        "MAX_AGENT_ALLOCATION_FRACTION": "0.50",
        "KELLY_MAX_POSITION_USDC": "100",
    })
    def test_kelly_size_uses_fractional_cap(self):
        sizing = kelly_size_usdc(
            balance_usdc=100.0,
            win_probability=0.60,
            entry_price=0.50,
            fallback_amount_usdc=2.5,
        )
        self.assertAlmostEqual(sizing.amount_usdc, 5.0)
        self.assertAlmostEqual(sizing.raw_fraction, 0.20)


# ---------------------------------------------------------------------------
# ConvictionJSONLReader
# ---------------------------------------------------------------------------

class TestConvictionJSONLReader(unittest.TestCase):

    def _write_jsonl(self, path, records):
        import json
        with open(path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def test_no_files_returns_no_direction(self):
        reader = ConvictionJSONLReader(conviction_paths=["/nonexistent.jsonl"])
        result = reader.query("mkt1", "Will something happen?")
        self.assertIsNone(result.direction)

    def test_matching_by_market_id(self):
        import json, tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(json.dumps({
                "market_id": "0xabc", "direction": "yes",
                "confidence": 0.72, "source": "manifold",
                "ts": "20260519120000",
            }) + "\n")
            path = f.name
        try:
            reader = ConvictionJSONLReader(conviction_paths=[path])
            result = reader.query("0xabc", "")
            self.assertEqual(result.direction, "yes")
            self.assertAlmostEqual(result.confidence, 0.72)
            self.assertIn("manifold", result.sources)
        finally:
            os.unlink(path)

    def test_skip_when_no_strong_direction(self):
        import json, tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(json.dumps({
                "market_id": "0xdef", "direction": "skip",
                "confidence": 0.0, "source": "heuristic",
                "ts": "20260519120000",
            }) + "\n")
            path = f.name
        try:
            reader = ConvictionJSONLReader(conviction_paths=[path])
            result = reader.query("0xdef", "")
            self.assertEqual(result.direction, "skip")
        finally:
            os.unlink(path)

    def test_age_seconds_uses_real_elapsed_seconds(self):
        import json
        import tempfile
        import time as _time
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            ts = _time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", _time.gmtime(_time.time() - 90)
            )
            f.write(json.dumps({
                "market_id": "0xage", "direction": "yes",
                "confidence": 0.7, "source": "manifold", "ts": ts,
            }) + "\n")
            path = f.name
        try:
            reader = ConvictionJSONLReader(conviction_paths=[path])
            result = reader.query("0xage", "")
            self.assertGreaterEqual(result.age_seconds, 60)
            self.assertLess(result.age_seconds, 180)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# ProbVelocityDetector
# ---------------------------------------------------------------------------
# BreakingNewsReader (unit — no network)
# ---------------------------------------------------------------------------

class TestBreakingNewsReader(unittest.TestCase):

    def _reader(self) -> BreakingNewsReader:
        return BreakingNewsReader(cache_ttl_sec=0)  # no caching in tests

    def test_no_feeds_returns_neutral(self):
        """With all feed URLs empty and no Twitter token, signal is neutral."""
        env = {
            "BREAKING_NEWS_YNET_URL": "",
            "BREAKING_NEWS_TRUMP_URL": "",
            "BREAKING_NEWS_EXTRA_URLS": "",
            "TWITTER_BEARER_TOKEN": "",
        }
        with patch.dict(os.environ, env):
            sig = self._reader().query("Will Trump win the election?")
        self.assertIsNone(sig.direction)
        self.assertEqual(sig.confidence, 0.0)
        self.assertEqual(sig.n_items, 0)

    def test_sentiment_bullish(self):
        """_sentiment() detects bullish words correctly."""
        r = self._reader()
        self.assertEqual(r._sentiment("Trump wins the presidency"), "bullish")

    def test_sentiment_bearish(self):
        r = self._reader()
        self.assertEqual(r._sentiment("market crash and defeat feared"), "bearish")

    def test_sentiment_neutral(self):
        r = self._reader()
        self.assertIsNone(r._sentiment("the sky is blue"))

    def test_news_component_wires_into_synthesize(self):
        """NewsSignal bullish with confidence=1.0 should appear in features."""
        from tests.test_meta_brain import _MockMarketBrain  # local import ok here
        mock_brain = _MockMarketBrain(approved=True, score=0.65)
        mb = MetaBrain(db_path="/nonexistent.db", conviction_paths=[], market_brain=mock_brain)
        # Inject a strong bullish news signal (bypass real network).
        mb.news_reader.query = lambda q: NewsSignal("bullish", 1.0, 5, ["ynet"], ["headline"])
        decision = mb.synthesize(market_id="m1", question="Will Trump win?")
        self.assertEqual(decision.features.get("news_direction"), "bullish")
        self.assertEqual(decision.features.get("news_confidence"), 1.0)


class TestProbVelocityDetector(unittest.TestCase):

    def test_rising_trend(self):
        det = ProbVelocityDetector(min_abs_velocity=0.01)
        import time as _time
        now = _time.time()
        det._in_memory["tok1"] = [
            (now - 1800, 0.50),
            (now - 1200, 0.54),
            (now - 600, 0.58),
            (now, 0.62),
        ]
        sig = det._from_samples(det._in_memory["tok1"])
        self.assertEqual(sig.direction, "rising")
        self.assertGreater(sig.pct_per_hour, 0)

    def test_stable_trend(self):
        det = ProbVelocityDetector(min_abs_velocity=0.05)
        import time as _time
        now = _time.time()
        det._in_memory["tok2"] = [
            (now - 3600, 0.50),
            (now, 0.51),
        ]
        sig = det._from_samples(det._in_memory["tok2"])
        self.assertEqual(sig.direction, "stable")

    def test_too_few_samples(self):
        det = ProbVelocityDetector()
        sig = det._from_samples([(0, 0.5)])
        self.assertIsNone(sig.direction)

    def test_db_velocity_uses_elapsed_time_not_digit_math(self):
        det = ProbVelocityDetector(min_abs_velocity=0.01, cache_ttl_sec=0)
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                """CREATE TABLE position_marks (
                    market_id TEXT,
                    token_id TEXT,
                    entry_price REAL,
                    current_price REAL,
                    first_seen_ts TEXT,
                    last_seen_ts TEXT
                )"""
            )
            conn.execute(
                "INSERT INTO position_marks VALUES (?,?,?,?,?,?)",
                (
                    "m1",
                    "tok1",
                    0.50,
                    0.55,
                    "2026-05-19T10:59:00Z",
                    "2026-05-19T11:01:00Z",
                ),
            )
            conn.commit()
            conn.close()
            sig = det._from_db(db_path, "m1", "tok1")
            self.assertEqual(sig.direction, "rising")
            self.assertAlmostEqual(sig.pct_per_hour, 3.0, places=1)
        finally:
            os.unlink(db_path)


# ---------------------------------------------------------------------------
# MetaBrain integration
# ---------------------------------------------------------------------------

class _MockBrainDecision:
    def __init__(self, approved, reason="ok", score=0.65, features=None):
        self.approved = approved
        self.reason = reason
        self.score = score
        self.features = features or {}


class _MockMarketBrain:
    def __init__(self, approved=True, score=0.65):
        self._approved = approved
        self._score = score

    def evaluate_general_entry(self, **kwargs):
        return _MockBrainDecision(self._approved, score=self._score)

    def evaluate_crypto_straddle_entry(self, **kwargs):
        return _MockBrainDecision(
            self._approved,
            reason="approved_crypto_straddle" if self._approved else "blocked",
            score=self._score,
            features={"brain_path": "crypto_straddle"},
        )


class TestMetaBrain(unittest.TestCase):

    def _make_meta_brain(self, approved=True, score=0.65, conviction_paths=None):
        mock_brain = _MockMarketBrain(approved=approved, score=score)
        mb = MetaBrain(
            db_path="/nonexistent.db",
            conviction_paths=conviction_paths or [],
            market_brain=mock_brain,
        )
        # Stub the news_reader so tests never make live HTTP requests.
        from agents.application.meta_brain import NewsSignal
        mb.news_reader.query = lambda question: NewsSignal(None, 0.0, 0, [], [])
        mb.equity_fv_reader.query = lambda **kwargs: EquityFairValueSignal(None, 0.5, 0.0, None, None, float("inf"))
        return mb

    def test_blocked_by_brain_gate(self):
        mb = self._make_meta_brain(approved=False, score=0.0)
        decision = mb.synthesize(
            market_id="m1",
            question="Will X happen?",
            spread_pct=0.03,
            hours_to_close=12.0,
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.entry_timing, "skip")

    @patch.dict(os.environ, {
        "META_BRAIN_WEIGHT_BRAIN": "1.0",
        "META_BRAIN_WEIGHT_WINRATE": "0.0",
        "META_BRAIN_WEIGHT_CONVICTION": "0.0",
        "META_BRAIN_WEIGHT_VELOCITY": "0.0",
        "META_BRAIN_WEIGHT_CROSS_MARKET": "0.0",
        "META_BRAIN_WEIGHT_WHALE": "0.0",
        "META_BRAIN_WEIGHT_LIQUIDITY": "0.0",
    })
    def test_approved_has_timing(self):
        mb = self._make_meta_brain(approved=True, score=0.70)
        decision = mb.synthesize(
            market_id="m1",
            question="Will something happen?",
            spread_pct=0.02,
            hours_to_close=18.0,
        )
        self.assertTrue(decision.approved)
        self.assertIn(decision.entry_timing, ("now", "wait", "skip"))

    def test_high_score_yields_approved_timing(self):
        """A high brain score without other corroborating signals yields 'wait' not 'skip'."""
        mb = self._make_meta_brain(approved=True, score=0.80)
        decision = mb.synthesize(
            market_id="m1",
            question="Will something happen?",
            spread_pct=0.02,
            hours_to_close=18.0,
        )
        self.assertTrue(decision.approved)
        # High score without other signals → "wait" (conservative; needs 2+ for "now").
        self.assertIn(decision.entry_timing, ("now", "wait"))

    @patch.dict(os.environ, {
        "META_BRAIN_WEIGHT_BRAIN": "1.0",
        "META_BRAIN_WEIGHT_WINRATE": "0.0",
        "META_BRAIN_WEIGHT_CONVICTION": "0.0",
        "META_BRAIN_WEIGHT_VELOCITY": "0.0",
        "META_BRAIN_WEIGHT_CROSS_MARKET": "0.0",
        "META_BRAIN_WEIGHT_WHALE": "0.0",
        "META_BRAIN_WEIGHT_LIQUIDITY": "0.0",
        "META_BRAIN_ANCHOR_THRESHOLD": "0.70",
        "META_BRAIN_MIN_WEIGHTED_SCORE": "0.50",
        "META_BRAIN_MIN_WEIGHTED_SCORE_ANCHOR": "0.40",
        "META_BRAIN_MIN_EDGE_PCT": "0.0",
        "META_BRAIN_MIN_RAW_EV": "0.0",
    })
    def test_unproven_single_high_signal_does_not_anchor(self):
        """A high score without measured reliability is not an expert anchor."""
        mb = self._make_meta_brain(approved=True, score=0.85)
        decision = mb.synthesize(
            market_id="m1",
            question="Strong anchor test?",
        )
        self.assertTrue(decision.approved)
        self.assertFalse(decision.features.get("has_anchor"))
        self.assertTrue(decision.features.get("legacy_anchor_candidate"))
        self.assertEqual(decision.features.get("best_signal"), "brain")

    @patch.dict(os.environ, {
        "META_BRAIN_ANCHOR_THRESHOLD": "0.70",
        "META_BRAIN_MIN_WEIGHTED_SCORE": "0.50",
    })
    def test_weak_signals_only_do_not_anchor(self):
        """If no single signal clears the anchor bar, has_anchor must be False."""
        mb = self._make_meta_brain(approved=True, score=0.60)
        decision = mb.synthesize(market_id="m1", question="Weak?")
        self.assertFalse(decision.features.get("has_anchor"))

    @patch.dict(os.environ, {
        "META_BRAIN_WINRATE_PRIOR": "0.50",
        "META_BRAIN_WEIGHT_BRAIN": "0.25",
        "META_BRAIN_WEIGHT_WINRATE": "0.15",
        "META_BRAIN_WEIGHT_CONVICTION": "0.0",
        "META_BRAIN_WEIGHT_VELOCITY": "0.0",
        "META_BRAIN_WEIGHT_CROSS_MARKET": "0.0",
        "META_BRAIN_WEIGHT_WHALE": "0.0",
        "META_BRAIN_WEIGHT_NEWS": "0.0",
        "META_BRAIN_WEIGHT_LIQUIDITY": "0.0",
    })
    def test_missing_winrate_prior_is_neutral_not_dilutive(self):
        mb = self._make_meta_brain(approved=True, score=0.60)
        decision = mb.synthesize(market_id="m1", question="No history yet?")
        self.assertAlmostEqual(decision.features["meta_score"], 0.60)
        self.assertAlmostEqual(
            decision.features["weighted_components"]["winrate"],
            0.50,
        )

    @patch.dict(os.environ, {
        "EXPERT_SOLO_MIN_PROB": "0.65",
        "EXPERT_SOLO_MIN_WINRATE": "0.65",
        "EXPERT_SOLO_MIN_WILSON": "0.55",
        "EXPERT_SOLO_MIN_SAMPLES": "30",
        "META_BRAIN_MIN_EDGE_PCT": "0.0",
        "META_BRAIN_MIN_RAW_EV": "0.0",
    })
    def test_reliable_wallet_can_lead_as_solo_expert(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            log = TradeLog(db_path=db_path)
            for idx in range(60):
                decision_id = log.insert_brain_decision(
                    agent="wallet_follow",
                    strategy="wallet_follow",
                    decision_type="entry",
                    market_id=f"w{idx}",
                    approved=True,
                    reason="ok",
                    score=0.8,
                    market_type="general_binary",
                    signal_source="wallet:abc",
                )
                log.update_brain_decision_outcome(decision_id, "closed_take_profit", {})
            for idx in range(20):
                decision_id = log.insert_brain_decision(
                    agent="wallet_follow",
                    strategy="wallet_follow",
                    decision_type="entry",
                    market_id=f"wl{idx}",
                    approved=True,
                    reason="ok",
                    score=0.8,
                    market_type="general_binary",
                    signal_source="wallet:abc",
                )
                log.update_brain_decision_outcome(decision_id, "closed_stop_loss", {})
            mb = self._make_meta_brain(approved=True, score=0.55)
            mb.db_path = db_path
            mb.source_reliability = SourceReliabilityAdvisor(cache_ttl_sec=0)
            mb.whale_reader.query = lambda market_id, db: WhaleSentimentSignal(
                direction="bullish",
                confidence=1.0,
                n_whales=1,
                total_size_usdc=1000.0,
                avg_profit_usdc=500.0,
                wallets=["abc"],
            )
            decision = mb.synthesize(
                market_id="m1",
                question="Wallet lead?",
                poly_prob=0.50,
            )
            self.assertTrue(decision.approved)
            self.assertEqual(decision.entry_timing, "now")
            self.assertEqual(decision.features["evidence_route"]["mode"], "solo")
            self.assertEqual(decision.features["evidence_route"]["leader"], "wallet:abc")
            self.assertEqual(decision.features["internal_prob_source"], "expert_solo:wallet:abc")
        finally:
            os.unlink(db_path)

    @patch.dict(os.environ, {
        "EXPERT_SOLO_MIN_PROB": "0.65",
        "EXPERT_WALLET_EXTERNAL_MIN_WINRATE": "0.70",
        "EXPERT_WALLET_EXTERNAL_MIN_TRADES": "50",
        "EXPERT_WALLET_EXTERNAL_MIN_PROFIT_USDC": "100",
        "META_BRAIN_MIN_EDGE_PCT": "0.0",
        "META_BRAIN_MIN_RAW_EV": "0.0",
    })
    def test_external_verified_wallet_can_lead_without_local_history(self):
        mb = self._make_meta_brain(approved=True, score=0.55)
        mb.whale_reader.query = lambda market_id, db: WhaleSentimentSignal(
            direction="bullish",
            confidence=1.0,
            n_whales=1,
            total_size_usdc=1000.0,
            avg_profit_usdc=500.0,
            wallets=["external"],
            best_wallet_winrate_external=0.72,
            best_wallet_trades_external=120,
            best_wallet_rank=3,
        )
        decision = mb.synthesize(
            market_id="m1",
            question="External wallet lead?",
            poly_prob=0.50,
        )
        self.assertTrue(decision.approved)
        self.assertEqual(decision.features["evidence_route"]["mode"], "solo")
        self.assertEqual(decision.features["evidence_route"]["leader"], "wallet:external")
        self.assertEqual(decision.features["internal_prob_source"], "expert_solo:wallet:external")

    @patch.dict(os.environ, {
        "META_BRAIN_WEIGHT_BRAIN": "0.0",
        "META_BRAIN_WEIGHT_WINRATE": "0.0",
        "META_BRAIN_WEIGHT_CONVICTION": "0.0",
        "META_BRAIN_WEIGHT_VELOCITY": "0.0",
        "META_BRAIN_WEIGHT_CROSS_MARKET": "0.0",
        "META_BRAIN_WEIGHT_EQUITY_FV": "1.0",
        "META_BRAIN_WEIGHT_WHALE": "0.0",
        "META_BRAIN_WEIGHT_NEWS": "0.0",
        "META_BRAIN_WEIGHT_LIQUIDITY": "0.0",
        "META_BRAIN_MIN_EDGE_PCT": "0.0",
        "META_BRAIN_MIN_RAW_EV": "0.0",
    })
    def test_equity_fair_value_component_can_drive_meta_score(self):
        mb = self._make_meta_brain(approved=True, score=0.55)
        mb.equity_fv_reader.query = lambda **kwargs: EquityFairValueSignal(
            direction="yes",
            probability=0.74,
            edge=0.18,
            selected_ticker="NVDA",
            selected_outcome="Nvidia",
            age_seconds=20.0,
        )

        decision = mb.synthesize(
            market_id="m1",
            question="Will Nvidia be the largest company by market cap?",
            poly_prob=0.50,
        )

        self.assertTrue(decision.approved)
        self.assertAlmostEqual(decision.features["weighted_components"]["equity_fv"], 0.74)
        self.assertEqual(decision.features["equity_fv_selected_ticker"], "NVDA")
        self.assertIn("equity_fv:NVDA", decision.signal_sources)

    @patch.dict(os.environ, {
        "META_BRAIN_WEIGHT_BRAIN": "0.0",
        "META_BRAIN_WEIGHT_WINRATE": "0.0",
        "META_BRAIN_WEIGHT_CONVICTION": "0.0",
        "META_BRAIN_WEIGHT_VELOCITY": "0.0",
        "META_BRAIN_WEIGHT_CROSS_MARKET": "0.0",
        "META_BRAIN_WEIGHT_EQUITY_FV": "0.0",
        "META_BRAIN_WEIGHT_ALPACA": "1.0",
        "META_BRAIN_WEIGHT_WHALE": "0.0",
        "META_BRAIN_WEIGHT_NEWS": "0.0",
        "META_BRAIN_WEIGHT_LIQUIDITY": "0.0",
        "META_BRAIN_MIN_EDGE_PCT": "0.0",
        "META_BRAIN_MIN_RAW_EV": "0.0",
    })
    def test_alpaca_component_can_drive_meta_score(self):
        mb = self._make_meta_brain(approved=True, score=0.55)
        mb.alpaca_reader.query = lambda question: AlpacaMarketSignal(
            direction="bullish",
            probability=0.68,
            confidence=0.68,
            symbol="BTC/USD",
            asset_class="crypto",
            reason="test alpaca momentum",
            features={"momentum_pct": 0.005},
        )

        decision = mb.synthesize(
            market_id="m1",
            question="Will Bitcoin go up in 5 minutes?",
            poly_prob=0.50,
        )

        self.assertTrue(decision.approved)
        self.assertAlmostEqual(decision.features["weighted_components"]["alpaca"], 0.68)
        self.assertEqual(decision.features["alpaca_symbol"], "BTC/USD")
        self.assertIn("alpaca:BTC/USD", decision.signal_sources)

    def test_summary_is_nonempty(self):
        mb = self._make_meta_brain(approved=True, score=0.65)
        decision = mb.synthesize(
            market_id="m2", question="Test?",
        )
        self.assertGreater(len(decision.summary), 10)

    def test_winrate_none_when_no_db(self):
        mb = self._make_meta_brain()
        decision = mb.synthesize(market_id="m1", question="Test?")
        self.assertIsNone(decision.winrate_estimate)

    def test_conviction_from_jsonl(self):
        import json, tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(json.dumps({
                "market_id": "0xabc", "direction": "yes",
                "confidence": 0.75, "source": "manifold",
                "ts": "20260519120000",
            }) + "\n")
            f.write(json.dumps({
                "market_id": "0xabc", "direction": "yes",
                "confidence": 0.70, "source": "kalshi",
                "ts": "20260519120100",
            }) + "\n")
            path = f.name
        try:
            mb = self._make_meta_brain(
                approved=True, score=0.65, conviction_paths=[path]
            )
            decision = mb.synthesize(market_id="0xabc", question="Test?")
            self.assertIn(decision.conviction_direction, ("yes", "no"))
            self.assertGreater(decision.conviction_confidence, 0)
        finally:
            os.unlink(path)

    def test_meta_decision_is_dataclass(self):
        mb = self._make_meta_brain(approved=True, score=0.60)
        decision = mb.synthesize(market_id="m1", question="Test?")
        self.assertIsInstance(decision, MetaDecision)
        self.assertIsInstance(decision.signal_sources, list)
        self.assertIsInstance(decision.features, dict)

    @patch.dict(os.environ, {
        "META_BRAIN_WEIGHT_BRAIN": "1.0",
        "META_BRAIN_WEIGHT_WINRATE": "0.0",
        "META_BRAIN_WEIGHT_CONVICTION": "0.0",
        "META_BRAIN_WEIGHT_VELOCITY": "0.0",
        "META_BRAIN_WEIGHT_CROSS_MARKET": "0.0",
        "META_BRAIN_WEIGHT_WHALE": "0.0",
        "META_BRAIN_WEIGHT_LIQUIDITY": "0.0",
        "META_BRAIN_MIN_EDGE_PCT": "0.02",
        "META_BRAIN_MIN_RAW_EV": "0.10",
    })
    def test_raw_ev_gate_rejects_expensive_thin_edge(self):
        mb = self._make_meta_brain(approved=True, score=0.84)
        decision = mb.synthesize(
            market_id="m1",
            question="Expensive edge?",
            poly_prob=0.80,
        )
        self.assertFalse(decision.approved)
        self.assertIn("raw_ev_too_low", decision.reason)
        self.assertAlmostEqual(decision.features["raw_ev"], 0.05, places=3)

    @patch.dict(os.environ, {
        "META_BRAIN_EXECUTION_QUALITY_ENABLED": "true",
        "EXECUTION_QUALITY_REQUIRE_FRESH": "true",
        "META_BRAIN_MIN_EDGE_PCT": "0.0",
        "META_BRAIN_MIN_RAW_EV": "0.0",
    })
    def test_execution_quality_blocks_when_token_book_missing(self):
        mb = self._make_meta_brain(approved=True, score=0.84)
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        self.addCleanup(lambda: os.path.exists(db_path) and os.unlink(db_path))
        mb.execution_quality = ExecutionQualityAdvisor(db_path=db_path)
        decision = mb.synthesize(
            market_id="m1",
            question="Needs executable book?",
            poly_prob=0.50,
            token_id="missing-token",
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "no_fresh_orderbook")

    @patch.dict(os.environ, {
        "META_BRAIN_CRYPTO_STRADDLE_MIN_SCORE": "0.65",
        "META_BRAIN_STRADDLE_TAVILY_ENABLED": "false",
        "META_BRAIN_STRADDLE_LLM_ENABLED": "false",
        "META_BRAIN_STRADDLE_WEIGHT_BRAIN": "1.0",
        "META_BRAIN_STRADDLE_WEIGHT_WINRATE": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_TAVILY": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_TRADINGVIEW": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_HERMES": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_CONVICTION": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_VELOCITY": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_LIQUIDITY": "0.0",
        "META_BRAIN_EXECUTION_QUALITY_ENABLED": "true",
        "EXECUTION_QUALITY_REQUIRE_FRESH": "true",
    })
    def test_crypto_straddle_execution_quality_blocks_missing_book(self):
        mb = self._make_meta_brain(approved=True, score=0.90, conviction_paths=[])
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        self.addCleanup(lambda: os.path.exists(db_path) and os.unlink(db_path))
        mb.execution_quality = ExecutionQualityAdvisor(db_path=db_path)
        decision = mb.synthesize_crypto_straddle(
            slug="btc-updown-5m-1779199200",
            question="Bitcoin Up or Down - May 19, 2:00PM UTC",
            asset="BTC",
            up_price=0.50,
            down_price=0.50,
            pair_ask_sum=1.0,
            seconds_to_expiry=240,
            token_id="missing-token",
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.reason, "no_fresh_orderbook")

    @patch.dict(os.environ, {
        "META_BRAIN_CRYPTO_STRADDLE_MIN_SCORE": "0.65",
        "META_BRAIN_STRADDLE_TAVILY_ENABLED": "false",
        "META_BRAIN_STRADDLE_LLM_ENABLED": "false",
        "META_BRAIN_STRADDLE_WEIGHT_BRAIN": "1.0",
        "META_BRAIN_STRADDLE_WEIGHT_WINRATE": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_TAVILY": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_TRADINGVIEW": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_HERMES": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_CONVICTION": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_VELOCITY": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_LIQUIDITY": "0.0",
        "META_BRAIN_EXECUTION_QUALITY_ENABLED": "false",
    })
    def test_crypto_straddle_uses_weighted_meta_score(self):
        mb = self._make_meta_brain(approved=True, score=0.90, conviction_paths=[])
        decision = mb.synthesize_crypto_straddle(
            slug="btc-updown-5m-1779199200",
            question="Bitcoin Up or Down - May 19, 2:00PM UTC",
            asset="BTC",
            up_price=0.51,
            down_price=0.52,
            pair_ask_sum=1.03,
            seconds_to_expiry=240,
            token_id="tok_up",
            liquidity_usdc=3.0,
        )
        self.assertIsInstance(decision, MetaDecision)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.entry_timing, "now")
        self.assertIn("weighted_components", decision.features)

    @patch.dict(os.environ, {
        "META_BRAIN_CRYPTO_STRADDLE_MIN_SCORE": "0.65",
        "META_BRAIN_STRADDLE_TAVILY_ENABLED": "false",
        "META_BRAIN_STRADDLE_LLM_ENABLED": "false",
        "META_BRAIN_STRADDLE_WEIGHT_BRAIN": "1.0",
        "META_BRAIN_STRADDLE_WEIGHT_WINRATE": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_TAVILY": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_TRADINGVIEW": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_HERMES": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_CONVICTION": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_VELOCITY": "0.0",
        "META_BRAIN_STRADDLE_WEIGHT_LIQUIDITY": "0.0",
    })
    def test_crypto_straddle_blocks_low_weighted_score(self):
        mb = self._make_meta_brain(approved=True, score=0.30, conviction_paths=[])
        decision = mb.synthesize_crypto_straddle(
            slug="btc-updown-5m-1779199200",
            question="Bitcoin Up or Down - May 19, 2:00PM UTC",
            asset="BTC",
            up_price=0.51,
            down_price=0.52,
            pair_ask_sum=1.03,
            seconds_to_expiry=240,
        )
        self.assertFalse(decision.approved)
        self.assertEqual(decision.entry_timing, "skip")


if __name__ == "__main__":
    unittest.main()
