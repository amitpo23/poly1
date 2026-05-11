import unittest

from agents.application.research_committee import (
    CommitteeConfig,
    MarketContext,
    ResearchCommittee,
)


class TestResearchCommittee(unittest.TestCase):
    def setUp(self):
        self.committee = ResearchCommittee(CommitteeConfig(enabled=True))

    def test_mean_reversion_is_never_live_approved(self):
        report = self.committee.review(MarketContext(
            market_slug="bitcoin-up-or-down-may-11",
            market_id="m1",
            strategy="mean_reversion",
            score=0.92,
            yes_price=0.51,
            no_price=0.49,
            spread_cents=2.0,
            volume_24h=150_000,
            liquidity=120_000,
            days_to_end=0.5,
            news_count=3,
        ))
        self.assertEqual(report.final_action, "reject_live_backtest_required")
        self.assertFalse(report.approved_for_live)
        self.assertTrue(report.approved_for_backtest)
        self.assertGreaterEqual(report.risk_score, 0.5)

    def test_market_maker_is_paper_only(self):
        report = self.committee.review(MarketContext(
            market_slug="test-market",
            strategy="market_maker",
            score=0.85,
            yes_price=0.50,
            no_price=0.50,
            spread_cents=6.0,
            volume_24h=100_000,
            liquidity=100_000,
            days_to_end=10,
            news_count=1,
        ))
        self.assertEqual(report.final_action, "paper_trade_only")
        self.assertFalse(report.approved_for_live)
        self.assertIn("spread_capture_score", report.features)

    def test_unknown_strategy_stays_research_only_when_weak(self):
        report = self.committee.review(MarketContext(
            market_slug="thin-market",
            strategy="new_strategy",
            score=0.25,
            yes_price=0.20,
            no_price=0.80,
            spread_cents=8.0,
            volume_24h=1_000,
            liquidity=5_000,
            days_to_end=20,
            news_count=0,
        ))
        self.assertEqual(report.final_action, "research_only")
        self.assertFalse(report.approved_for_live)
        self.assertLess(report.final_score, 0.62)


if __name__ == "__main__":
    unittest.main()
