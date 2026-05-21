from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from agents.application.market_scanner import MarketScanner, ScannerConfig
from agents.application.trade_log import TradeLog


class _FakeMetaBrain:
    def synthesize(self, **kwargs):
        return SimpleNamespace(
            approved=True,
            reason="ok",
            score=0.82,
            entry_timing="now",
            summary="fake",
            winrate_estimate=None,
            winrate_sample_size=0,
            conviction_direction="",
            velocity_direction="",
            signal_sources=["cross_market"],
            cross_market_divergence=0.18,
            features={
                "evidence_route": {
                    "mode": "solo",
                    "direction": "yes",
                    "probability": 0.8,
                    "leader": "cross_market",
                    "reason": "expert_solo:cross_market",
                    "claims": [],
                    "conflicts": [],
                },
                "internal_probability": 0.8,
                "internal_probability_calibrated": True,
                "internal_prob_source": "expert_solo:cross_market",
            },
        )


class MarketScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "trade_log.db")
        self.log = TradeLog(db_path=self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scanner_persists_calibrated_probability_metadata_for_executor(self):
        scanner = MarketScanner(
            cfg=ScannerConfig(
                market_limit=1,
                max_candidates=1,
                target_trade_decisions=1,
                min_liquidity_usdc=10.0,
                min_volume_usdc=10.0,
                min_trade_score=0.55,
                manifold_enabled=False,
            ),
            trade_log=self.log,
            meta_brain=_FakeMetaBrain(),
        )
        close_time = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
        scanner._fetch_markets = lambda: [
            {
                "id": "gamma-1",
                "conditionId": "0xscan",
                "question": "Will test market resolve yes?",
                "slug": "test-market",
                "active": True,
                "closed": False,
                "outcomePrices": json.dumps(["0.50", "0.50"]),
                "outcomes": json.dumps(["Yes", "No"]),
                "clobTokenIds": json.dumps(["tok_yes", "tok_no"]),
                "liquidityClob": 10000,
                "volume24hr": 10000,
                "endDate": close_time,
            }
        ]

        result = scanner.scan_once()

        self.assertEqual(result["dispatched_trade"], 1)
        row = self.log.recent_brain_decisions(limit=1)[0]
        features = json.loads(row["features_json"])
        self.assertEqual(row["agent"], "market_scanner")
        self.assertEqual(features["estimated_win_probability"], 0.8)
        self.assertTrue(features["estimated_win_probability_calibrated"])
        self.assertEqual(
            features["estimated_win_probability_source"],
            "expert_solo:cross_market",
        )


if __name__ == "__main__":
    unittest.main()
