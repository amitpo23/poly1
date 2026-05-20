from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from agents.application.crypto_5m_market_maker_shadow import (
    Crypto5mMarketMakerShadow,
    MakerShadowConfig,
    evaluate_quote,
)
from agents.application.trade_log import TradeLog


@dataclass
class FakeTapeSignal:
    direction: str | None = None
    probability: float = 0.5
    confidence: float = 0.0
    asset: str = "BTC"
    symbol: str = "BTCUSDT"
    reason: str = "fake"
    features: dict = field(default_factory=dict)


class FakeTapeClient:
    def __init__(self, signal: FakeTapeSignal):
        self.signal = signal

    def analyze_question(self, question: str) -> FakeTapeSignal:
        return self.signal


class Crypto5mMarketMakerShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "trade_log.db")
        self.log = TradeLog(db_path=self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _cfg(self):
        return MakerShadowConfig(
            universe_limit=10,
            max_orderbook_age_sec=30,
            min_bid_depth_usdc=10,
            min_ask_depth_usdc=10,
            min_seconds_to_expiry=30,
            max_seconds_to_expiry=260,
            target_profit_cents=0.02,
            min_profit_cents=0.01,
            external_tape_enabled=False,
        )

    def _seed_market(self, *, up_bid=0.47, up_ask=0.52, down_bid=0.47, down_ask=0.52):
        period_ts = int(time.time()) - 180
        self.log.upsert_market_universe({
            "slug": f"btc-updown-5m-{period_ts}",
            "horizon": "5m",
            "asset": "btc",
            "period_ts": period_ts,
            "market_id": "m1",
            "question": "Bitcoin Up or Down?",
            "liquidity_usdc": 5000,
            "volume_usdc": 5000,
            "yes_price": 0.50,
            "no_price": 0.50,
            "up_token": "tok_up",
            "down_token": "tok_down",
            "accepting_orders": True,
            "route_agent": "btc_5min",
            "score": 0.9,
            "winrate_estimate": 0.55,
            "eligible": True,
            "top_rank": 1,
        })
        for token, bid, ask in (("tok_up", up_bid, up_ask), ("tok_down", down_bid, down_ask)):
            self.log.upsert_orderbook_snapshot({
                "token_id": token,
                "market_id": "m1",
                "source": "test",
                "best_bid": bid,
                "best_ask": ask,
                "mid": (bid + ask) / 2,
                "spread_pct": (ask - bid) / ((bid + ask) / 2),
                "bid_depth_usdc": 30.0,
                "ask_depth_usdc": 30.0,
                "bid_levels": 3,
                "ask_levels": 3,
            })

    def test_evaluate_quote_accepts_wide_deep_mid_book(self):
        plan = evaluate_quote(
            {
                "token_id": "tok_up",
                "best_bid": 0.47,
                "best_ask": 0.52,
                "mid": 0.495,
                "spread_pct": 0.096,
                "bid_depth_usdc": 30.0,
                "ask_depth_usdc": 30.0,
            },
            outcome="up",
            cfg=self._cfg(),
        )

        self.assertTrue(plan.approved)
        self.assertEqual(plan.reason, "shadow_quote_candidate")
        self.assertAlmostEqual(plan.maker_bid, 0.48)
        self.assertAlmostEqual(plan.maker_ask, 0.51)
        self.assertGreater(plan.score, 0.5)

    def test_evaluate_quote_rejects_thin_exit_depth(self):
        plan = evaluate_quote(
            {
                "token_id": "tok_up",
                "best_bid": 0.47,
                "best_ask": 0.52,
                "mid": 0.495,
                "spread_pct": 0.096,
                "bid_depth_usdc": 2.0,
                "ask_depth_usdc": 30.0,
            },
            outcome="up",
            cfg=self._cfg(),
        )

        self.assertFalse(plan.approved)
        self.assertEqual(plan.reason, "exit_bid_depth_too_low")

    def test_run_once_records_shadow_quotes(self):
        self._seed_market()
        engine = Crypto5mMarketMakerShadow(cfg=self._cfg(), trade_log=self.log)

        stats = engine.run_once()

        self.assertEqual(stats["markets"], 1)
        self.assertEqual(stats["approved"], 2)
        rows = self.log.recent_decision_journal(limit=5)
        self.assertEqual([r["decision"] for r in rows[:2]], ["SHADOW_QUOTE", "SHADOW_QUOTE"])
        brain = self.log.recent_brain_decisions(limit=1)[0]
        self.assertEqual(brain["agent"], "crypto_5m_market_maker_shadow")

    def test_strong_bullish_tape_quotes_up_and_rejects_down(self):
        self._seed_market(up_bid=0.47, up_ask=0.52, down_bid=0.47, down_ask=0.52)
        cfg = MakerShadowConfig(
            universe_limit=10,
            max_orderbook_age_sec=30,
            min_bid_depth_usdc=10,
            min_ask_depth_usdc=10,
            min_seconds_to_expiry=30,
            max_seconds_to_expiry=260,
            min_profit_cents=0.01,
            external_tape_enabled=True,
            external_tape_min_confidence=0.58,
            min_directional_edge_pct=0.015,
        )
        engine = Crypto5mMarketMakerShadow(
            cfg=cfg,
            trade_log=self.log,
            crypto_tape_client=FakeTapeClient(FakeTapeSignal(
                direction="bullish",
                probability=0.62,
                confidence=0.66,
                reason="fake bullish tape",
            )),
        )

        stats = engine.run_once()

        self.assertEqual(stats["approved"], 1)
        self.assertEqual(stats["rejected"], 1)
        rows = self.log.recent_decision_journal(limit=2)
        decisions = {(r["action"], r["decision"], r["reason"]) for r in rows}
        self.assertIn(("QUOTE_UP", "SHADOW_QUOTE", "shadow_quote_candidate_with_directional_edge"), decisions)
        self.assertIn(("QUOTE_DOWN", "REJECT", "directional_edge_too_low"), decisions)

    def test_run_once_records_no_market_cycle_reject(self):
        engine = Crypto5mMarketMakerShadow(cfg=self._cfg(), trade_log=self.log)

        stats = engine.run_once()

        self.assertEqual(stats["markets"], 0)
        row = self.log.recent_decision_journal(limit=1)[0]
        self.assertEqual(row["decision"], "REJECT")
        self.assertEqual(row["reason"], "no_candidate_in_time_window")


if __name__ == "__main__":
    unittest.main()
