from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agents.application.external_conviction import (
    AggregatorProvider,
    BullBearDebateProvider,
    CLOBWhaleProvider,
    CrossMarketProvider,
    DataAPIWhaleConsensusProvider,
    ExternalConvictionAgent,
    ExternalConvictionConfig,
    ExternalProvider,
    ExternalVerdict,
    HeuristicProvider,
    AlpacaMarketDataProvider,
    KalshiDivergenceProvider,
    ManifoldDivergenceProvider,
    MetaculusDivergenceProvider,
    NansenSmartMoneyProvider,
    PoliflyBrowserProvider,
    PoliflyEnhancedProvider,
    PublicNewsProvider,
    TradingViewOptionsProvider,
    WalletMasterProvider,
    build_shadow_plan,
    filter_candidates,
    market_from_gamma,
    provider_from_config,
)
from agents.application.alpaca_market_data import AlpacaMarketSignal
from agents.application.trade_log import TradeLog


class AllowBrain:
    def evaluate_general_entry(self, **_kwargs):
        class Decision:
            approved = True
            reason = "test_approved"
            score = 0.9
            features = {"test": True}
        return Decision()


def _raw_market(
    market_id="M1",
    question="Will Bitcoin go up today?",
    yes_price=0.40,
    volume=10000.0,
    liquidity=2000.0,
):
    return {
        "id": market_id,
        "question": question,
        "slug": f"slug-{market_id}",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["TOK_YES", "TOK_NO"]',
        "outcomePrices": json.dumps([str(yes_price), str(1.0 - yes_price)]),
        "volumeClob": volume,
        "liquidityClob": liquidity,
        "active": True,
        "closed": False,
        "endDate": "2026-05-18T00:00:00Z",
    }


class FakeGamma:
    def __init__(self, markets):
        self.markets = markets

    def get_current_markets(self, limit=100):
        return self.markets[:limit]


class FixedProvider:
    source = "fixed"

    def __init__(self, direction="yes", confidence=0.70):
        self.direction = direction
        self.confidence = confidence

    def analyze(self, market):
        return ExternalVerdict(
            direction=self.direction,
            confidence=self.confidence,
            source=self.source,
            reason="fixed test verdict",
            evidence={"test": True},
        )


class FakeRiskGate:
    def ok(self):
        return True

    def reason(self):
        return None


class FakePolymarket:
    def __init__(self):
        self.orders = []

    def execute_market_order(self, market, recommendation):
        self.orders.append((market, recommendation))
        return {
            "status": "filled",
            "token_id": "TOK_YES" if recommendation.side == "BUY" else "TOK_NO",
            "amount_usdc": recommendation.amount_usdc,
            "order_avg_price_estimate": recommendation.price,
            "outcome_traded": "Yes" if recommendation.side == "BUY" else "No",
        }


class TestMarketParsing(unittest.TestCase):
    def test_market_from_gamma_parses_prices_and_tokens(self):
        snap = market_from_gamma(_raw_market())
        self.assertIsNotNone(snap)
        self.assertEqual(snap.market_id, "M1")
        self.assertEqual(snap.yes_price, 0.40)
        self.assertEqual(snap.no_price, 0.60)
        self.assertEqual(snap.tokens[0], "TOK_YES")
        self.assertEqual(snap.category, "crypto")

    def test_filter_candidates_excludes_low_liquidity(self):
        cfg = ExternalConvictionConfig(
            min_volume_usdc=5000.0,
            min_liquidity_usdc=500.0,
            max_candidates=5,
        )
        markets = [
            _raw_market("OK", liquidity=1000.0),
            _raw_market("LOW", liquidity=10.0),
        ]
        out = filter_candidates(markets, cfg)
        self.assertEqual([m.market_id for m in out], ["OK"])

    def test_filter_candidates_excludes_extreme_prices(self):
        cfg = ExternalConvictionConfig(min_price=0.12, max_price=0.88)
        out = filter_candidates([_raw_market("EXTREME", yes_price=0.96)], cfg)
        self.assertEqual(out, [])


class TestPlanning(unittest.TestCase):
    def test_build_shadow_plan_approves_when_confident(self):
        market = market_from_gamma(_raw_market(yes_price=0.40))
        cfg = ExternalConvictionConfig(min_confidence=0.58)
        verdict = ExternalVerdict(
            direction="yes",
            confidence=0.70,
            source="test",
            reason="good setup",
            evidence={},
        )
        plan = build_shadow_plan(market, verdict, cfg)
        self.assertEqual(plan.action, "SHADOW_BUY_YES")
        self.assertEqual(plan.token_id, "TOK_YES")
        self.assertGreater(plan.take_profit, plan.entry_price)
        self.assertLess(plan.stop_loss, plan.entry_price)

    def test_build_shadow_plan_skips_low_confidence(self):
        market = market_from_gamma(_raw_market(yes_price=0.40))
        cfg = ExternalConvictionConfig(min_confidence=0.80)
        verdict = ExternalVerdict(
            direction="yes",
            confidence=0.70,
            source="test",
            reason="not enough",
            evidence={},
        )
        plan = build_shadow_plan(market, verdict, cfg)
        self.assertEqual(plan.action, "SKIP")
        self.assertEqual(plan.status, "shadow_skip")

    def test_heuristic_provider_is_conservative(self):
        market = market_from_gamma(_raw_market(question="Will Bitcoin go up today?"))
        verdict = HeuristicProvider().analyze(market)
        self.assertIn(verdict.direction, ("yes", "no", "skip"))
        self.assertLessEqual(verdict.confidence, 0.72)

    def test_alpaca_provider_maps_bullish_btc_to_yes_for_up_market(self):
        class FakeAlpaca:
            def analyze_question(self, question):
                return AlpacaMarketSignal(
                    direction="bullish",
                    probability=0.66,
                    confidence=0.66,
                    symbol="BTC/USD",
                    asset_class="crypto",
                    reason="fake alpaca momentum",
                    features={"momentum_pct": 0.004},
                )

        market = market_from_gamma(_raw_market(question="Will Bitcoin go up in 5 minutes?"))
        verdict = AlpacaMarketDataProvider(client=FakeAlpaca()).analyze(market)
        self.assertEqual(verdict.source, "alpaca_market_data")
        self.assertEqual(verdict.direction, "yes")
        self.assertEqual(verdict.evidence["symbol"], "BTC/USD")
        self.assertGreater(verdict.confidence, 0.60)


class TestAgent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_once_writes_jsonl_and_brain_decision(self):
        cfg = ExternalConvictionConfig(
            min_volume_usdc=1000.0,
            min_liquidity_usdc=100.0,
            max_candidates=1,
            output_path=str(self.root / "external_convictions.jsonl"),
            heartbeat_path=str(self.root / "external_conviction_heartbeat"),
            min_confidence=0.58,
        )
        log = TradeLog(str(self.root / "trade_log.db"))
        agent = ExternalConvictionAgent(
            cfg=cfg,
            gamma=FakeGamma([_raw_market()]),
            provider=FixedProvider(direction="yes", confidence=0.70),
            trade_log=log,
        )
        written = agent.collect_once()
        self.assertEqual(written, 1)
        lines = (self.root / "external_convictions.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["plan"]["action"], "SHADOW_BUY_YES")
        self.assertTrue((self.root / "external_conviction_heartbeat").exists())
        decisions = log.recent_brain_decisions(limit=5)
        self.assertEqual(decisions[0]["agent"], "external_conviction")
        self.assertEqual(decisions[0]["approved"], 1)

    def test_collect_once_uses_configured_agent_identity(self):
        cfg = ExternalConvictionConfig(
            min_volume_usdc=1000.0,
            min_liquidity_usdc=100.0,
            max_candidates=1,
            output_path=str(self.root / "external_convictions_api.jsonl"),
            heartbeat_path=str(self.root / "external_conviction_api_heartbeat"),
            agent_name="external_conviction_api",
            strategy_name="api_event_probability_scalping",
            min_confidence=0.58,
        )
        log = TradeLog(str(self.root / "trade_log.db"))
        agent = ExternalConvictionAgent(
            cfg=cfg,
            gamma=FakeGamma([_raw_market()]),
            provider=FixedProvider(direction="yes", confidence=0.70),
            trade_log=log,
        )
        agent.collect_once()
        decisions = log.recent_brain_decisions(limit=5)
        self.assertEqual(decisions[0]["agent"], "external_conviction_api")
        self.assertEqual(decisions[0]["strategy"], "api_event_probability_scalping")

    def test_collect_once_executes_live_candidate_when_enabled(self):
        cfg = ExternalConvictionConfig(
            min_volume_usdc=1000.0,
            min_liquidity_usdc=100.0,
            max_candidates=1,
            output_path=str(self.root / "external_convictions_live.jsonl"),
            heartbeat_path=str(self.root / "external_conviction_live_heartbeat"),
            agent_name="external_conviction_api",
            strategy_name="public_news_event_probability_scalping",
            position_size_usdc=3.0,
            execute=True,
        )
        log = TradeLog(str(self.root / "trade_log.db"))
        polymarket = FakePolymarket()
        agent = ExternalConvictionAgent(
            cfg=cfg,
            gamma=FakeGamma([_raw_market()]),
            provider=FixedProvider(direction="yes", confidence=0.74),
            trade_log=log,
            polymarket=polymarket,
            risk_gate=FakeRiskGate(),
            brain=AllowBrain(),
        )
        agent.meta_brain = None
        written = agent.collect_once()
        self.assertEqual(written, 1)
        self.assertEqual(len(polymarket.orders), 1)
        positions = log.filled_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["market_id"], "M1")
        self.assertEqual(positions[0]["size_usdc"], 3.0)

    def test_live_execution_sell_side(self):
        cfg = ExternalConvictionConfig(
            min_volume_usdc=1000.0,
            min_liquidity_usdc=100.0,
            max_candidates=1,
            output_path=str(self.root / "ec_sell.jsonl"),
            heartbeat_path=str(self.root / "hb_sell"),
            position_size_usdc=3.0,
            execute=True,
            min_confidence=0.58,
        )
        log = TradeLog(str(self.root / "trade_log.db"))
        polymarket = FakePolymarket()
        agent = ExternalConvictionAgent(
            cfg=cfg,
            gamma=FakeGamma([_raw_market(yes_price=0.70)]),
            provider=FixedProvider(direction="no", confidence=0.72),
            trade_log=log,
            polymarket=polymarket,
            risk_gate=FakeRiskGate(),
            brain=AllowBrain(),
        )
        agent.meta_brain = None
        agent.collect_once()
        self.assertEqual(len(polymarket.orders), 1)
        _, rec = polymarket.orders[0]
        self.assertEqual(rec.side, "SELL")
        # recommendation.price is anchored to yes_price per prompt contract
        self.assertAlmostEqual(rec.price, 0.70, places=2)

    def test_live_blocked_by_risk_gate(self):
        class BlockingRiskGate:
            def ok(self):
                return False
            def reason(self):
                return "daily loss exceeded"

        cfg = ExternalConvictionConfig(
            min_volume_usdc=1000.0,
            min_liquidity_usdc=100.0,
            max_candidates=1,
            output_path=str(self.root / "ec_blocked.jsonl"),
            heartbeat_path=str(self.root / "hb_blocked"),
            position_size_usdc=3.0,
            execute=True,
            min_confidence=0.58,
        )
        log = TradeLog(str(self.root / "trade_log.db"))
        polymarket = FakePolymarket()
        agent = ExternalConvictionAgent(
            cfg=cfg,
            gamma=FakeGamma([_raw_market()]),
            provider=FixedProvider(direction="yes", confidence=0.74),
            trade_log=log,
            polymarket=polymarket,
            risk_gate=BlockingRiskGate(),
        )
        agent.collect_once()
        self.assertEqual(len(polymarket.orders), 0)
        self.assertEqual(len(log.filled_positions()), 0)

    def test_live_blocked_by_max_open_positions(self):
        cfg = ExternalConvictionConfig(
            min_volume_usdc=1000.0,
            min_liquidity_usdc=100.0,
            max_candidates=2,
            output_path=str(self.root / "ec_maxpos.jsonl"),
            heartbeat_path=str(self.root / "hb_maxpos"),
            position_size_usdc=3.0,
            execute=True,
            max_open_positions=1,
            min_confidence=0.58,
        )
        log = TradeLog(str(self.root / "trade_log.db"))
        polymarket = FakePolymarket()
        agent = ExternalConvictionAgent(
            cfg=cfg,
            gamma=FakeGamma([
                _raw_market("M1", yes_price=0.40),
                _raw_market("M2", question="Will ETH go up?", yes_price=0.35),
            ]),
            provider=FixedProvider(direction="yes", confidence=0.74),
            trade_log=log,
            polymarket=polymarket,
            risk_gate=FakeRiskGate(),
            brain=AllowBrain(),
        )
        agent.meta_brain = None
        agent.collect_once()
        # First market fills, second is blocked by max_open_positions
        self.assertEqual(len(polymarket.orders), 1)
        positions = log.filled_positions()
        self.assertEqual(len(positions), 1)

    def test_live_handles_execute_exception(self):
        class FailingPolymarket:
            def __init__(self):
                self.orders = []
            def execute_market_order(self, market, recommendation):
                raise ConnectionError("CLOB timeout")

        cfg = ExternalConvictionConfig(
            min_volume_usdc=1000.0,
            min_liquidity_usdc=100.0,
            max_candidates=1,
            output_path=str(self.root / "ec_fail.jsonl"),
            heartbeat_path=str(self.root / "hb_fail"),
            position_size_usdc=3.0,
            execute=True,
            min_confidence=0.58,
        )
        log = TradeLog(str(self.root / "trade_log.db"))
        agent = ExternalConvictionAgent(
            cfg=cfg,
            gamma=FakeGamma([_raw_market()]),
            provider=FixedProvider(direction="yes", confidence=0.74),
            trade_log=log,
            polymarket=FailingPolymarket(),
            risk_gate=FakeRiskGate(),
        )
        agent.collect_once()
        # Order failed but no crash, no filled positions
        self.assertEqual(len(log.filled_positions()), 0)


class TestPoliflyProvider(unittest.TestCase):
    def test_polifly_provider_skips_without_browser_bridge(self):
        market = market_from_gamma(_raw_market())
        verdict = PoliflyBrowserProvider(url="").analyze(market)
        self.assertEqual(verdict.direction, "skip")
        self.assertEqual(verdict.source, "polifly_browser")
        self.assertIn("POLIFLY_BROWSER_BRIDGE_URL", verdict.reason)
        self.assertTrue(verdict.evidence["access_required"])


class FakePublicNewsProvider(PublicNewsProvider):
    def _fetch_items(self, query):
        return [
            {
                "title": "Bitcoin latest odds rise after confirmed ETF flows",
                "description": "Forecast report says crypto markets lead today",
                "link": "https://example.test/1",
                "pubDate": "Sun, 17 May 2026 10:00:00 GMT",
            },
            {
                "title": "Polymarket traders watch Bitcoin market",
                "description": "Latest report highlights odds and volume",
                "link": "https://example.test/2",
                "pubDate": "Sun, 17 May 2026 10:05:00 GMT",
            },
            {
                "title": "Crypto forecast remains active",
                "description": "Market attention surges",
                "link": "https://example.test/3",
                "pubDate": "Sun, 17 May 2026 10:10:00 GMT",
            },
        ]


class TestPublicNewsProvider(unittest.TestCase):
    def test_public_news_uses_real_provider_shape(self):
        market = market_from_gamma(_raw_market(question="Will Bitcoin go up today?"))
        verdict = FakePublicNewsProvider().analyze(market)
        self.assertEqual(verdict.source, "public_news")
        self.assertEqual(verdict.direction, "yes")
        self.assertGreaterEqual(verdict.confidence, 0.58)
        self.assertEqual(verdict.evidence["result_count"], 3)


class TestSkipHelper(unittest.TestCase):
    def test_skip_helper_returns_skip_verdict(self):
        provider = HeuristicProvider()
        verdict = provider._skip("test reason", {"key": "val"})
        self.assertEqual(verdict.direction, "skip")
        self.assertEqual(verdict.confidence, 0.0)
        self.assertEqual(verdict.source, "heuristic")
        self.assertEqual(verdict.reason, "test reason")
        self.assertEqual(verdict.evidence, {"key": "val"})

    def test_skip_helper_default_evidence(self):
        provider = HeuristicProvider()
        verdict = provider._skip("no evidence")
        self.assertEqual(verdict.evidence, {})


class FakeCLOBWhaleProvider(CLOBWhaleProvider):
    def __init__(self, trades):
        self._fake_trades = trades

    def analyze(self, market):
        # Monkey-patch the fetch: override just the HTTP call part
        import agents.application.external_conviction as mod
        original_urlopen = mod.urllib.request.urlopen

        class FakeResp:
            def __init__(self, data):
                self._data = json.dumps(data).encode()
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=20):
            return FakeResp(self._fake_trades)

        mod.urllib.request.urlopen = fake_urlopen
        try:
            return super().analyze(market)
        finally:
            mod.urllib.request.urlopen = original_urlopen


class TestCLOBWhaleProvider(unittest.TestCase):
    def test_whale_skip_when_few_big_trades(self):
        trades = [{"size": "100", "side": "BUY"}] * 5  # all < $5K
        provider = FakeCLOBWhaleProvider(trades)
        market = market_from_gamma(_raw_market())
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "skip")
        self.assertEqual(verdict.source, "clob_whale")

    def test_whale_yes_when_heavy_buying(self):
        trades = [
            {"size": "8000", "side": "BUY"},
            {"size": "7000", "side": "BUY"},
            {"size": "6000", "side": "SELL"},
        ]
        provider = FakeCLOBWhaleProvider(trades)
        market = market_from_gamma(_raw_market())
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "yes")
        self.assertGreater(verdict.confidence, 0.45)


class FakeManifoldProvider(ManifoldDivergenceProvider):
    def __init__(self, manifold_prob):
        self._prob = manifold_prob

    def analyze(self, market):
        import agents.application.external_conviction as mod
        original_urlopen = mod.urllib.request.urlopen

        class FakeResp:
            def __init__(self, data):
                self._data = json.dumps(data).encode()
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=20):
            return FakeResp([{"probability": self._prob, "slug": "test-market"}])

        mod.urllib.request.urlopen = fake_urlopen
        try:
            return super().analyze(market)
        finally:
            mod.urllib.request.urlopen = original_urlopen


class TestManifoldDivergenceProvider(unittest.TestCase):
    def test_signals_yes_when_manifold_higher(self):
        provider = FakeManifoldProvider(0.70)
        market = market_from_gamma(_raw_market(yes_price=0.40))
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "yes")
        self.assertEqual(verdict.source, "manifold_divergence")
        self.assertGreater(verdict.confidence, 0.5)

    def test_skip_when_divergence_small(self):
        provider = FakeManifoldProvider(0.42)
        market = market_from_gamma(_raw_market(yes_price=0.40))
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "skip")


class FakeMetaculusProvider(MetaculusDivergenceProvider):
    def __init__(self, mc_prob):
        self._prob = mc_prob

    def analyze(self, market):
        import agents.application.external_conviction as mod
        original_urlopen = mod.urllib.request.urlopen

        class FakeResp:
            def __init__(self, data):
                self._data = json.dumps(data).encode()
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=20):
            return FakeResp({
                "results": [{
                    "id": 123,
                    "community_prediction": {"full": {"q2": self._prob}},
                }]
            })

        mod.urllib.request.urlopen = fake_urlopen
        try:
            return super().analyze(market)
        finally:
            mod.urllib.request.urlopen = original_urlopen


class TestMetaculusDivergenceProvider(unittest.TestCase):
    def test_signals_no_when_metaculus_lower(self):
        provider = FakeMetaculusProvider(0.25)
        market = market_from_gamma(_raw_market(yes_price=0.50))
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "no")
        self.assertEqual(verdict.source, "metaculus_divergence")

    def test_skip_when_divergence_small(self):
        provider = FakeMetaculusProvider(0.48)
        market = market_from_gamma(_raw_market(yes_price=0.40))
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "skip")


class TestCrossMarketProvider(unittest.TestCase):
    def test_skip_when_no_all_markets(self):
        provider = CrossMarketProvider()
        market = market_from_gamma(_raw_market())
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "skip")
        self.assertIn("no all_markets", verdict.reason)

    def test_signals_yes_when_related_markets_high(self):
        provider = CrossMarketProvider()
        main = market_from_gamma(_raw_market(
            market_id="M1",
            question="Will Bitcoin reach 100K today?",
            yes_price=0.40,
        ))
        related = market_from_gamma(_raw_market(
            market_id="M2",
            question="Will Bitcoin reach 90K today?",
            yes_price=0.80,
        ))
        provider.all_markets = [main, related]
        verdict = provider.analyze(main)
        self.assertEqual(verdict.direction, "yes")
        self.assertEqual(verdict.source, "cross_market")


class FakeKalshiProvider(KalshiDivergenceProvider):
    def __init__(self, kalshi_yes_cents):
        self._yes_cents = kalshi_yes_cents

    def analyze(self, market):
        import agents.application.external_conviction as mod
        original_urlopen = mod.urllib.request.urlopen

        class FakeResp:
            def __init__(self, data):
                self._data = json.dumps(data).encode()
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        q_lower = market.question[:60].lower()

        def fake_urlopen(req, timeout=20):
            return FakeResp({
                "markets": [{
                    "title": market.question,
                    "yes_ask": self._yes_cents,
                    "ticker": "TEST-KALSHI",
                }]
            })

        mod.urllib.request.urlopen = fake_urlopen
        try:
            return super().analyze(market)
        finally:
            mod.urllib.request.urlopen = original_urlopen


class TestKalshiDivergenceProvider(unittest.TestCase):
    def test_signals_yes_when_kalshi_higher(self):
        provider = FakeKalshiProvider(70)  # 70 cents = 0.70
        market = market_from_gamma(_raw_market(yes_price=0.40))
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "yes")
        self.assertEqual(verdict.source, "kalshi_divergence")

    def test_skip_when_divergence_small(self):
        provider = FakeKalshiProvider(42)  # 42 cents = 0.42
        market = market_from_gamma(_raw_market(yes_price=0.40))
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "skip")


class TestDataAPIWhaleConsensusProvider(unittest.TestCase):
    def test_skip_when_leaderboard_fails(self):
        import agents.application.external_conviction as mod
        original_urlopen = mod.urllib.request.urlopen

        def fake_urlopen(req, timeout=20):
            raise Exception("network error")

        mod.urllib.request.urlopen = fake_urlopen
        try:
            provider = DataAPIWhaleConsensusProvider()
            market = market_from_gamma(_raw_market())
            verdict = provider.analyze(market)
            self.assertEqual(verdict.direction, "skip")
            self.assertIn("leaderboard error", verdict.reason)
        finally:
            mod.urllib.request.urlopen = original_urlopen

    def test_signals_yes_when_majority_yes(self):
        import agents.application.external_conviction as mod
        original_urlopen = mod.urllib.request.urlopen

        class FakeResp:
            def __init__(self, data):
                self._data = json.dumps(data).encode()
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        call_count = [0]

        def fake_urlopen(req, timeout=20):
            call_count[0] += 1
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "leaderboard" in url:
                return FakeResp([
                    {"address": "0xAAA"},
                    {"address": "0xBBB"},
                    {"address": "0xCCC"},
                ])
            # positions endpoint — return yes positions for all wallets
            return FakeResp([{"outcome": "Yes", "size": "100"}])

        mod.urllib.request.urlopen = fake_urlopen
        try:
            provider = DataAPIWhaleConsensusProvider()
            market = market_from_gamma(_raw_market())
            verdict = provider.analyze(market)
            self.assertEqual(verdict.direction, "yes")
            self.assertEqual(verdict.evidence["yes_votes"], 3)
            self.assertEqual(verdict.evidence["no_votes"], 0)
        finally:
            mod.urllib.request.urlopen = original_urlopen


class TestBullBearDebateProvider(unittest.TestCase):
    def test_skip_without_api_key(self):
        provider = BullBearDebateProvider(api_key="")
        market = market_from_gamma(_raw_market())
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "skip")
        self.assertIn("OPENAI_API_KEY", verdict.reason)

    def test_debate_with_fake_llm(self):
        import agents.application.external_conviction as mod
        original_urlopen = mod.urllib.request.urlopen
        call_count = [0]

        class FakeResp:
            def __init__(self, text):
                body = {"choices": [{"message": {"content": text}}]}
                self._data = json.dumps(body).encode()
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=30):
            call_count[0] += 1
            if call_count[0] <= 2:
                return FakeResp("Strong argument for this side.")
            return FakeResp('{"direction":"yes","confidence":0.72,"reason":"bull wins"}')

        mod.urllib.request.urlopen = fake_urlopen
        try:
            provider = BullBearDebateProvider(api_key="fake-key")
            market = market_from_gamma(_raw_market())
            verdict = provider.analyze(market)
            self.assertEqual(verdict.direction, "yes")
            self.assertAlmostEqual(verdict.confidence, 0.72, places=2)
            self.assertEqual(call_count[0], 3)
        finally:
            mod.urllib.request.urlopen = original_urlopen


class TestNansenSmartMoneyProvider(unittest.TestCase):
    def test_skip_without_api_key(self):
        provider = NansenSmartMoneyProvider(api_key="")
        market = market_from_gamma(_raw_market())
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "skip")
        self.assertIn("NANSEN_API_KEY", verdict.reason)

    def test_signals_yes_with_inflow(self):
        import agents.application.external_conviction as mod
        original_urlopen = mod.urllib.request.urlopen

        class FakeResp:
            def __init__(self, data):
                self._data = json.dumps(data).encode()
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=20):
            return FakeResp({
                "smart_money_inflow": 8000,
                "smart_money_outflow": 2000,
            })

        mod.urllib.request.urlopen = fake_urlopen
        try:
            provider = NansenSmartMoneyProvider(api_key="fake-key")
            market = market_from_gamma(_raw_market())
            verdict = provider.analyze(market)
            self.assertEqual(verdict.direction, "yes")
            self.assertGreater(verdict.confidence, 0.5)
        finally:
            mod.urllib.request.urlopen = original_urlopen


class TestWalletMasterProvider(unittest.TestCase):
    def test_skip_without_api_key(self):
        provider = WalletMasterProvider(api_key="")
        market = market_from_gamma(_raw_market())
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "skip")
        self.assertIn("WALLET_MASTER_API_KEY", verdict.reason)

    def test_returns_api_consensus(self):
        import agents.application.external_conviction as mod
        original_urlopen = mod.urllib.request.urlopen

        class FakeResp:
            def __init__(self, data):
                self._data = json.dumps(data).encode()
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def fake_urlopen(req, timeout=20):
            return FakeResp({
                "consensus": "no",
                "confidence": 0.68,
                "weighted_score": 0.35,
                "wallets_counted": 15,
            })

        mod.urllib.request.urlopen = fake_urlopen
        try:
            provider = WalletMasterProvider(api_key="fake-key")
            market = market_from_gamma(_raw_market())
            verdict = provider.analyze(market)
            self.assertEqual(verdict.direction, "no")
            self.assertAlmostEqual(verdict.confidence, 0.68, places=2)
        finally:
            mod.urllib.request.urlopen = original_urlopen


class TestPoliflyEnhancedProvider(unittest.TestCase):
    def test_falls_back_to_public_news(self):
        provider = PoliflyEnhancedProvider(polifly_url="", polifly_api_key="")
        # Replace the fallback with our fake
        provider._fallback = FakePublicNewsProvider()
        market = market_from_gamma(_raw_market(question="Will Bitcoin go up today?"))
        verdict = provider.analyze(market)
        self.assertEqual(verdict.source, "polifly_enhanced")
        self.assertIn("fallback", verdict.reason)
        self.assertTrue(verdict.evidence.get("fallback"))

    def test_returns_polifly_when_successful(self):
        provider = PoliflyEnhancedProvider(polifly_url="", polifly_api_key="")
        # Replace inner polifly with a provider that returns a real verdict
        provider._polifly = FixedProvider(direction="yes", confidence=0.70)
        provider._polifly.source = "polifly_browser"
        market = market_from_gamma(_raw_market())
        verdict = provider.analyze(market)
        self.assertEqual(verdict.source, "polifly_enhanced")
        self.assertEqual(verdict.direction, "yes")
        self.assertAlmostEqual(verdict.confidence, 0.70, places=2)

    def test_fallback_on_exception(self):
        provider = PoliflyEnhancedProvider(polifly_url="", polifly_api_key="")

        class RaisingProvider:
            source = "polifly_browser"
            def analyze(self, market):
                raise ConnectionError("bridge down")

        provider._polifly = RaisingProvider()
        provider._fallback = FakePublicNewsProvider()
        market = market_from_gamma(_raw_market(question="Will Bitcoin go up today?"))
        verdict = provider.analyze(market)
        self.assertEqual(verdict.source, "polifly_enhanced")
        self.assertIn("fallback", verdict.reason)
        self.assertTrue(verdict.evidence.get("fallback"))


class TestAggregatorProvider(unittest.TestCase):
    def test_aggregator_weighted_consensus(self):
        p1 = FixedProvider(direction="yes", confidence=0.70)
        p2 = FixedProvider(direction="yes", confidence=0.65)
        p3 = FixedProvider(direction="no", confidence=0.60)
        aggregator = AggregatorProvider([p1, p2, p3])
        market = market_from_gamma(_raw_market())
        verdict = aggregator.analyze(market)
        self.assertEqual(verdict.direction, "yes")
        self.assertEqual(verdict.source, "aggregator")
        self.assertGreater(verdict.confidence, 0)

    def test_aggregator_skip_when_no_providers(self):
        aggregator = AggregatorProvider([])
        market = market_from_gamma(_raw_market())
        verdict = aggregator.analyze(market)
        self.assertEqual(verdict.direction, "skip")

    def test_aggregator_handles_all_skips(self):
        p1 = FixedProvider(direction="skip", confidence=0.0)
        p2 = FixedProvider(direction="skip", confidence=0.0)
        aggregator = AggregatorProvider([p1, p2])
        market = market_from_gamma(_raw_market())
        verdict = aggregator.analyze(market)
        self.assertEqual(verdict.direction, "skip")

    def test_aggregator_respects_custom_weights(self):
        p1 = FixedProvider(direction="yes", confidence=0.70)
        p1.source = "provider_a"
        p2 = FixedProvider(direction="no", confidence=0.65)
        p2.source = "provider_b"
        # Weight provider_b at 3x so NO wins despite lower confidence
        aggregator = AggregatorProvider(
            [p1, p2], weights={"provider_a": 1.0, "provider_b": 3.0}
        )
        market = market_from_gamma(_raw_market())
        verdict = aggregator.analyze(market)
        self.assertEqual(verdict.direction, "no")
        self.assertGreater(verdict.evidence["no_weight"], verdict.evidence["yes_weight"])


class TestProviderFactory(unittest.TestCase):
    def test_factory_creates_clob_whale(self):
        cfg = ExternalConvictionConfig(provider="clob_whale")
        prov = provider_from_config(cfg)
        self.assertIsInstance(prov, CLOBWhaleProvider)

    def test_factory_creates_manifold(self):
        cfg = ExternalConvictionConfig(provider="manifold")
        prov = provider_from_config(cfg)
        self.assertIsInstance(prov, ManifoldDivergenceProvider)

    def test_factory_creates_metaculus(self):
        cfg = ExternalConvictionConfig(provider="metaculus")
        prov = provider_from_config(cfg)
        self.assertIsInstance(prov, MetaculusDivergenceProvider)

    def test_factory_creates_cross_market(self):
        cfg = ExternalConvictionConfig(provider="cross_market")
        prov = provider_from_config(cfg)
        self.assertIsInstance(prov, CrossMarketProvider)

    def test_factory_creates_kalshi(self):
        cfg = ExternalConvictionConfig(provider="kalshi")
        prov = provider_from_config(cfg)
        self.assertIsInstance(prov, KalshiDivergenceProvider)

    def test_factory_creates_tradingview_options(self):
        cfg = ExternalConvictionConfig(provider="tradingview_options")
        prov = provider_from_config(cfg)
        self.assertIsInstance(prov, TradingViewOptionsProvider)

    def test_factory_creates_alpaca_market_data(self):
        cfg = ExternalConvictionConfig(provider="alpaca_market_data")
        prov = provider_from_config(cfg)
        self.assertIsInstance(prov, AlpacaMarketDataProvider)

    def test_factory_creates_whale_consensus(self):
        cfg = ExternalConvictionConfig(provider="whale_consensus")
        prov = provider_from_config(cfg)
        self.assertIsInstance(prov, DataAPIWhaleConsensusProvider)

    def test_factory_creates_debate(self):
        cfg = ExternalConvictionConfig(provider="debate")
        prov = provider_from_config(cfg)
        self.assertIsInstance(prov, BullBearDebateProvider)

    def test_factory_creates_nansen(self):
        cfg = ExternalConvictionConfig(provider="nansen")
        prov = provider_from_config(cfg)
        self.assertIsInstance(prov, NansenSmartMoneyProvider)

    def test_factory_creates_wallet_master(self):
        cfg = ExternalConvictionConfig(provider="wallet_master")
        prov = provider_from_config(cfg)
        self.assertIsInstance(prov, WalletMasterProvider)

    def test_factory_creates_polifly_enhanced(self):
        cfg = ExternalConvictionConfig(provider="polifly_enhanced")
        prov = provider_from_config(cfg)
        self.assertIsInstance(prov, PoliflyEnhancedProvider)

    def test_factory_creates_aggregator(self):
        import os
        old = os.environ.get("EXTERNAL_CONVICTION_AGGREGATOR_PROVIDERS")
        os.environ["EXTERNAL_CONVICTION_AGGREGATOR_PROVIDERS"] = "heuristic,public_news"
        try:
            cfg = ExternalConvictionConfig(provider="aggregator")
            prov = provider_from_config(cfg)
            self.assertIsInstance(prov, AggregatorProvider)
            self.assertEqual(len(prov.sub_providers), 2)
        finally:
            if old is not None:
                os.environ["EXTERNAL_CONVICTION_AGGREGATOR_PROVIDERS"] = old
            else:
                os.environ.pop("EXTERNAL_CONVICTION_AGGREGATOR_PROVIDERS", None)


class TestTradingViewOptionsProvider(unittest.TestCase):
    def test_skips_without_snapshot(self):
        provider = TradingViewOptionsProvider(
            snapshot_path="/tmp/poly1_missing_tradingview_snapshot.json",
            max_age_sec=900,
        )
        provider._page_reachable = lambda: True
        market = market_from_gamma(
            _raw_market(
                "TV1",
                question="Will the S&P 500 close higher today?",
                yes_price=0.48,
            )
        )
        verdict = provider.analyze(market)
        self.assertEqual(verdict.direction, "skip")
        self.assertEqual(verdict.source, "tradingview_options")
        self.assertIn("snapshot_missing", verdict.reason)

    def test_snapshot_put_call_ratio_produces_macro_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tv.json"
            path.write_text(json.dumps({
                "ts": "2026-05-19T12:00:00Z",
                "symbol": "CME_MINI:ES1!",
                "put_call_ratio": 0.70,
                "call_volume": 1000,
                "put_volume": 700,
            }))
            provider = TradingViewOptionsProvider(
                snapshot_path=str(path),
                max_age_sec=10**9,
            )
            market = market_from_gamma(
                _raw_market(
                    "TV2",
                    question="Will the S&P 500 close higher today?",
                    yes_price=0.48,
                )
            )
            verdict = provider.analyze(market)
            self.assertEqual(verdict.direction, "yes")
            self.assertGreaterEqual(verdict.confidence, 0.55)
            self.assertEqual(verdict.evidence["put_call_ratio"], 0.7)


class TestCrossMarketInjection(unittest.TestCase):
    def test_collect_once_injects_all_markets_for_cross_market(self):
        cfg = ExternalConvictionConfig(
            min_volume_usdc=1000.0,
            min_liquidity_usdc=100.0,
            max_candidates=5,
            output_path=str(Path(tempfile.mkdtemp()) / "ec.jsonl"),
            heartbeat_path=str(Path(tempfile.mkdtemp()) / "hb"),
            provider="cross_market",
        )
        cross = CrossMarketProvider()
        log = TradeLog(str(Path(tempfile.mkdtemp()) / "tl.db"))
        agent = ExternalConvictionAgent(
            cfg=cfg,
            gamma=FakeGamma([_raw_market("A"), _raw_market("B")]),
            provider=cross,
            trade_log=log,
        )
        agent.collect_once()
        self.assertTrue(len(cross.all_markets) > 0)


class TestReentryCooldownLive(unittest.TestCase):
    """Fix 1: external_conviction blocked by recent close."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_blocked_by_recent_close(self):
        cfg = ExternalConvictionConfig(
            min_volume_usdc=1000.0,
            min_liquidity_usdc=100.0,
            max_candidates=1,
            output_path=str(self.root / "ec_cool.jsonl"),
            heartbeat_path=str(self.root / "hb_cool"),
            position_size_usdc=3.0,
            execute=True,
            min_confidence=0.58,
        )
        log = TradeLog(str(self.root / "trade_log.db"))
        polymarket = FakePolymarket()
        # Insert a recent close for the market
        log.insert_terminal("c_old", "M1", "closed_timeout", token_id="TOK_YES")
        agent = ExternalConvictionAgent(
            cfg=cfg,
            gamma=FakeGamma([_raw_market()]),
            provider=FixedProvider(direction="yes", confidence=0.74),
            trade_log=log,
            polymarket=polymarket,
            risk_gate=FakeRiskGate(),
        )
        agent.collect_once()
        # Should be blocked — no orders placed
        self.assertEqual(len(polymarket.orders), 0)


class TestConcentrationLive(unittest.TestCase):
    """Fix 2: external_conviction blocked at fill limit."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_blocked_at_concentration_limit(self):
        import os
        old_val = os.environ.get("MAX_FILLS_PER_MARKET_24H")
        os.environ["MAX_FILLS_PER_MARKET_24H"] = "2"
        try:
            cfg = ExternalConvictionConfig(
                min_volume_usdc=1000.0,
                min_liquidity_usdc=100.0,
                max_candidates=1,
                output_path=str(self.root / "ec_conc.jsonl"),
                heartbeat_path=str(self.root / "hb_conc"),
                position_size_usdc=3.0,
                execute=True,
                min_confidence=0.58,
            )
            log = TradeLog(str(self.root / "trade_log.db"))
            polymarket = FakePolymarket()
            # Insert 2 existing fills (at the limit)
            log.insert_terminal("c1", "M1", "filled", token_id="TOK_YES")
            # Need a close row so has_filled_position_for_market returns False
            log.insert_terminal("close1", "M1", "closed_take_profit", token_id="TOK_YES")
            log.insert_terminal("c2", "M1", "filled", token_id="TOK_YES")
            log.insert_terminal("close2", "M1", "closed_stop_loss", token_id="TOK_YES")
            agent = ExternalConvictionAgent(
                cfg=cfg,
                gamma=FakeGamma([_raw_market()]),
                provider=FixedProvider(direction="yes", confidence=0.74),
                trade_log=log,
                polymarket=polymarket,
                risk_gate=FakeRiskGate(),
                brain=AllowBrain(),
            )
            agent.meta_brain = None
            agent.collect_once()
            # Should be blocked by concentration limit (2 fills >= max 2)
            self.assertEqual(len(polymarket.orders), 0)
        finally:
            if old_val is not None:
                os.environ["MAX_FILLS_PER_MARKET_24H"] = old_val
            else:
                os.environ.pop("MAX_FILLS_PER_MARKET_24H", None)


class TestReentryCooldownAllowsAfterExpiry(unittest.TestCase):
    """Fix 1 positive case: old close does NOT block re-entry."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_allows_entry_when_close_is_old(self):
        import os
        old_val = os.environ.get("REENTRY_COOLDOWN_HOURS")
        os.environ["REENTRY_COOLDOWN_HOURS"] = "12"
        try:
            cfg = ExternalConvictionConfig(
                min_volume_usdc=1000.0,
                min_liquidity_usdc=100.0,
                max_candidates=1,
                output_path=str(self.root / "ec_allow.jsonl"),
                heartbeat_path=str(self.root / "hb_allow"),
                position_size_usdc=3.0,
                execute=True,
                min_confidence=0.58,
            )
            log = TradeLog(str(self.root / "trade_log.db"))
            polymarket = FakePolymarket()
            # Insert a close with an old timestamp (>12h ago)
            log._connect()  # ensure table exists
            with log._lock, log._connect() as conn:
                conn.execute(
                    "INSERT INTO trades (cycle_id, market_id, status, token_id, ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("c_old", "M1", "closed_timeout", "TOK_YES", "2025-01-01T00:00:00+00:00"),
                )
            agent = ExternalConvictionAgent(
                cfg=cfg,
                gamma=FakeGamma([_raw_market()]),
                provider=FixedProvider(direction="yes", confidence=0.74),
                trade_log=log,
                polymarket=polymarket,
                risk_gate=FakeRiskGate(),
                brain=AllowBrain(),
            )
            agent.meta_brain = None
            agent.collect_once()
            # The old close should NOT block — order should be placed
            self.assertGreater(len(polymarket.orders), 0)
        finally:
            if old_val is not None:
                os.environ["REENTRY_COOLDOWN_HOURS"] = old_val
            else:
                os.environ.pop("REENTRY_COOLDOWN_HOURS", None)


class TestConcentrationAllowsBelowLimit(unittest.TestCase):
    """Fix 2 positive case: fills below limit do NOT block re-entry."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_allows_entry_below_limit(self):
        """1 old fill (outside dedupe) + close → below limit 3, entry allowed."""
        import os
        old_fills = os.environ.get("MAX_FILLS_PER_MARKET_24H")
        old_cool = os.environ.get("REENTRY_COOLDOWN_HOURS")
        os.environ["MAX_FILLS_PER_MARKET_24H"] = "3"
        os.environ["REENTRY_COOLDOWN_HOURS"] = "0"  # disable cooldown for this test
        try:
            cfg = ExternalConvictionConfig(
                min_volume_usdc=1000.0,
                min_liquidity_usdc=100.0,
                max_candidates=1,
                output_path=str(self.root / "ec_below.jsonl"),
                heartbeat_path=str(self.root / "hb_below"),
                position_size_usdc=3.0,
                execute=True,
                min_confidence=0.58,
            )
            log = TradeLog(str(self.root / "trade_log.db"))
            polymarket = FakePolymarket()
            # Insert 1 old fill (outside the 6h active dedupe window)
            # so has_active_trade_for_market doesn't block, then a close.
            log._connect()  # ensure table exists
            with log._lock, log._connect() as conn:
                conn.execute(
                    "INSERT INTO trades (cycle_id, market_id, status, token_id, ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("c1", "M1", "filled", "TOK_YES", "2025-01-01T00:00:00+00:00"),
                )
                conn.execute(
                    "INSERT INTO trades (cycle_id, market_id, status, token_id, ts) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("close1", "M1", "closed_take_profit", "TOK_YES", "2025-01-01T01:00:00+00:00"),
                )
            agent = ExternalConvictionAgent(
                cfg=cfg,
                gamma=FakeGamma([_raw_market()]),
                provider=FixedProvider(direction="yes", confidence=0.74),
                trade_log=log,
                polymarket=polymarket,
                risk_gate=FakeRiskGate(),
                brain=AllowBrain(),
            )
            agent.meta_brain = None
            agent.collect_once()
            # 1 old fill < 3 limit — order should be placed
            self.assertGreater(len(polymarket.orders), 0)
        finally:
            for var, val in [("MAX_FILLS_PER_MARKET_24H", old_fills),
                             ("REENTRY_COOLDOWN_HOURS", old_cool)]:
                if val is not None:
                    os.environ[var] = val
                else:
                    os.environ.pop(var, None)


class TestInMemoryDuplicateGuard(unittest.TestCase):
    """Fix 6: same market_id should only get one live entry per cycle."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_duplicate_market_blocked_in_same_cycle(self):
        cfg = ExternalConvictionConfig(
            min_volume_usdc=1000.0,
            min_liquidity_usdc=100.0,
            max_candidates=5,
            max_live_trades_per_cycle=5,
            max_open_positions=5,
            output_path=str(self.root / "ec_dup.jsonl"),
            heartbeat_path=str(self.root / "hb_dup"),
            position_size_usdc=3.0,
            execute=True,
        )
        log = TradeLog(str(self.root / "trade_log.db"))
        polymarket = FakePolymarket()
        # Two identical markets in the candidate list
        markets = [_raw_market(), _raw_market()]
        agent = ExternalConvictionAgent(
            cfg=cfg,
            gamma=FakeGamma(markets),
            provider=FixedProvider(direction="yes", confidence=0.74),
            trade_log=log,
            polymarket=polymarket,
            risk_gate=FakeRiskGate(),
            brain=AllowBrain(),
        )
        agent.meta_brain = None
        agent.collect_once()
        # Only one order should have been placed despite two candidates
        self.assertEqual(len(polymarket.orders), 1)


if __name__ == "__main__":
    unittest.main()
