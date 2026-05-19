import json
import tempfile
import unittest
from pathlib import Path

from agents.application.market_universe import (
    UniverseConfig,
    apply_focus_policy,
    candidate_from_market,
    candidate_from_trend_market,
    discover,
    discover_trends,
    format_slug,
    persist,
    route_agent_for,
    score_candidate,
    refine_winrate_with_daily_journal,
)
from agents.application.trade_log import TradeLog


class MarketUniverseTests(unittest.TestCase):
    def test_format_slug_uses_asset_horizon_and_period(self):
        self.assertEqual(format_slug("eth", "5m", 1770000000), "eth-updown-5m-1770000000")
        self.assertEqual(format_slug("DOGE", "15m", 1770000900), "doge-updown-15m-1770000900")

    def test_candidate_filters_illiquid_or_closed_markets(self):
        market = {
            "id": "1",
            "slug": "btc-updown-5m-1770000000",
            "active": True,
            "closed": False,
            "acceptingOrders": True,
            "liquidity": "1499",
            "clobTokenIds": '["up","down"]',
            "outcomes": '["Up","Down"]',
            "outcomePrices": '["0.51","0.49"]',
        }
        self.assertIsNone(
            candidate_from_market(
                market,
                asset="btc",
                horizon="5m",
                period_ts=1770000000,
                min_liquidity_usdc=1500,
            )
        )
        market["liquidity"] = "2500"
        candidate = candidate_from_market(
            market,
            asset="btc",
            horizon="5m",
            period_ts=1770000000,
            min_liquidity_usdc=1500,
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.route_agent, "btc_5min")
        self.assertEqual(candidate.up_token, "up")
        self.assertEqual(candidate.down_token, "down")

    def test_score_prefers_liquid_balanced_short_horizon_markets(self):
        strong = score_candidate(
            liquidity_usdc=12000,
            volume_usdc=8000,
            yes_price=0.51,
            no_price=0.49,
            horizon="5m",
        )
        weak = score_candidate(
            liquidity_usdc=1500,
            volume_usdc=100,
            yes_price=0.88,
            no_price=0.12,
            horizon="15m",
        )
        self.assertGreater(strong, weak)

    def test_bnb_5m_is_not_routed_to_btc_5min_without_price_feed(self):
        self.assertEqual(route_agent_for("bnb", "5m"), "unassigned_5min")
        self.assertEqual(route_agent_for("bnb", "15m"), "scalper")
        self.assertEqual(route_agent_for("doge", "5m"), "btc_5min")

    def test_discover_fetches_configured_periods(self):
        calls = []

        def fake_fetch(slug):
            calls.append(slug)
            return {
                "id": slug,
                "slug": slug,
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "liquidity": "5000",
                "volume": "1000",
                "clobTokenIds": '["up","down"]',
                "outcomes": '["Up","Down"]',
                "outcomePrices": '["0.52","0.48"]',
            }

        import agents.application.market_universe as module

        old = module._fetch_gamma_market
        module._fetch_gamma_market = fake_fetch
        try:
            config = UniverseConfig(
                assets=("btc", "eth"),
                horizons=("5m",),
                periods_ahead=2,
                min_liquidity_usdc=1500,
            )
            candidates = discover(config, now_ts=1770000123, include_trends=False)
        finally:
            module._fetch_gamma_market = old
        self.assertEqual(len(candidates), 4)
        self.assertIn("btc-updown-5m-1770000000", calls)
        self.assertIn("eth-updown-5m-1770000300", calls)

    def test_persist_writes_db_json_and_scalper_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "trade_log.db")
            out = str(Path(tmp) / "market_universe.json")
            tl = TradeLog(db_path=db)
            candidate = candidate_from_market(
                {
                    "id": "m1",
                    "slug": "btc-updown-15m-1770000000",
                    "active": True,
                    "closed": False,
                    "acceptingOrders": True,
                    "liquidity": "9000",
                    "volume": "3000",
                    "clobTokenIds": '["up","down"]',
                    "outcomes": '["Up","Down"]',
                    "outcomePrices": '["0.5","0.5"]',
                },
                asset="btc",
                horizon="15m",
                period_ts=1770000000,
                min_liquidity_usdc=1500,
            )
            candidate = apply_focus_policy(
                [candidate],
                min_winrate=0.65,
                top_n=10,
            )[0]
            persist(tl, [candidate], output_path=out, write_scalper_pairs=True)
            self.assertTrue(Path(out).exists())
            payload = json.loads(Path(out).read_text())
            self.assertEqual(payload["count"], 1)
            self.assertEqual(tl.list_market_universe(route_agent="scalper")[0]["slug"], candidate.slug)
            with tl._connect() as conn:
                row = conn.execute(
                    "SELECT slug FROM scalper_pairs WHERE slug = ?", (candidate.slug,)
                ).fetchone()
            self.assertIsNotNone(row)

    def test_focus_policy_marks_only_top_eligible_markets(self):
        candidates = []
        for idx in range(12):
            candidate = candidate_from_market(
                {
                    "id": f"m{idx}",
                    "slug": f"btc-updown-15m-{1770000000 + idx * 900}",
                    "active": True,
                    "closed": False,
                    "acceptingOrders": True,
                    "liquidity": str(20000 - idx * 100),
                    "volume": "5000",
                    "clobTokenIds": '["up","down"]',
                    "outcomes": '["Up","Down"]',
                    "outcomePrices": '["0.5","0.5"]',
                },
                asset="btc",
                horizon="15m",
                period_ts=1770000000 + idx * 900,
                min_liquidity_usdc=1500,
            )
            candidates.append(candidate)
        focused = apply_focus_policy(candidates, min_winrate=0.65, top_n=10)
        eligible = [c for c in focused if c.eligible]
        self.assertEqual(len(eligible), 10)
        self.assertEqual([c.top_rank for c in eligible], list(range(1, 11)))

    def test_trend_market_is_watch_only_not_live_eligible(self):
        candidate = candidate_from_trend_market(
            {
                "id": "trend1",
                "slug": "will-bitcoin-be-above-90000-today",
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "liquidity": "80000",
                "volume": "200000",
                "volume24hr": "50000",
                "endDate": "2026-02-08T18:00:00Z",
                "clobTokenIds": '["yes","no"]',
                "outcomes": '["Yes","No"]',
                "outcomePrices": '["0.52","0.48"]',
            },
            min_liquidity_usdc=5000,
            min_volume_24h_usdc=1000,
            max_hours_to_close=24,
            now_ts=1770555600,
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.route_agent, "trend_watch")
        focused = apply_focus_policy([candidate], min_winrate=0.1, top_n=10)
        self.assertFalse(focused[0].eligible)
        self.assertIsNone(focused[0].top_rank)

    def test_trend_market_can_route_to_scalper_when_explicitly_enabled(self):
        candidate = candidate_from_trend_market(
            {
                "id": "trend1",
                "slug": "will-bitcoin-be-above-90000-today",
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "liquidity": "80000",
                "volume": "200000",
                "volume24hr": "50000",
                "endDate": "2026-02-08T18:00:00Z",
                "clobTokenIds": '["yes","no"]',
                "outcomes": '["Yes","No"]',
                "outcomePrices": '["0.52","0.48"]',
            },
            min_liquidity_usdc=5000,
            min_volume_24h_usdc=1000,
            max_hours_to_close=24,
            trade_enabled=True,
            now_ts=1770555600,
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.route_agent, "scalper")
        self.assertGreater(candidate.period_ts, 0)
        focused = apply_focus_policy([candidate], min_winrate=0.1, top_n=10)
        self.assertTrue(focused[0].eligible)

    def test_daily_journal_refines_candidate_winrate(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = TradeLog(db_path=str(Path(tmp) / "trade_log.db"))
            candidate = candidate_from_market(
                {
                    "id": "m1",
                    "slug": "btc-updown-15m-1770000000",
                    "active": True,
                    "closed": False,
                    "acceptingOrders": True,
                    "liquidity": "9000",
                    "volume": "3000",
                    "clobTokenIds": '["up","down"]',
                    "outcomes": '["Up","Down"]',
                    "outcomePrices": '["0.5","0.5"]',
                },
                asset="btc",
                horizon="15m",
                period_ts=1770000000,
                min_liquidity_usdc=1500,
            )
            self.assertIsNotNone(candidate)
            log.insert_brain_decision(
                agent="scalper",
                strategy="test",
                decision_type="entry",
                market_id=candidate.slug,
                approved=True,
                reason="approved",
                score=0.9,
                market_type="crypto_15m",
            )
            with log._connect() as conn:
                conn.execute("UPDATE brain_decisions SET outcome_status='closed_stop_loss'")
            refined = refine_winrate_with_daily_journal(candidate, log, weight=0.5)
            self.assertLess(refined.winrate_estimate, candidate.winrate_estimate)
            self.assertEqual(refined.details_json["daily_journal"]["losses"], 1)

    def test_discover_trends_dedupes_and_filters(self):
        calls = []

        def fake_fetch(params):
            calls.append(params["order"])
            return [
                {
                    "id": "trend1",
                    "slug": "same-trend",
                    "active": True,
                    "closed": False,
                    "acceptingOrders": True,
                    "liquidity": "10000",
                    "volume": "30000",
                    "volume24hr": "2000",
                    "endDate": "2026-02-08T18:00:00Z",
                    "clobTokenIds": '["yes","no"]',
                    "outcomes": '["Yes","No"]',
                    "outcomePrices": '["0.5","0.5"]',
                },
                {
                    "id": "trend2",
                    "slug": "too-far-away",
                    "active": True,
                    "closed": False,
                    "acceptingOrders": True,
                    "liquidity": "10000",
                    "volume": "30000",
                    "volume24hr": "2000",
                    "endDate": "2026-02-12T18:00:00Z",
                    "clobTokenIds": '["yes","no"]',
                    "outcomes": '["Yes","No"]',
                    "outcomePrices": '["0.5","0.5"]',
                },
            ]

        import agents.application.market_universe as module

        old = module._fetch_gamma_markets
        module._fetch_gamma_markets = fake_fetch
        try:
            config = UniverseConfig(
                trend_limit=20,
                trend_min_liquidity_usdc=5000,
                trend_min_volume_24h_usdc=1000,
                trend_max_hours_to_close=24,
            )
            candidates = discover_trends(config, now_ts=1770555600)
        finally:
            module._fetch_gamma_markets = old
        self.assertEqual(calls, ["volume24hr", "volume", "liquidity"])
        self.assertEqual([c.slug for c in candidates], ["same-trend"])


if __name__ == "__main__":
    unittest.main()
