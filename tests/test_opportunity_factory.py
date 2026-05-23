from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agents.application.opportunity_factory import (
    OpportunityFactory,
    OpportunityFactoryConfig,
)
from agents.application.trade_log import TradeLog


class FakeCryptoTape:
    def __init__(self, direction="bullish", probability=0.62, confidence=0.61):
        self.direction = direction
        self.probability = probability
        self.confidence = confidence
        self.reason = "fake_tape"
        self.features = {"source": "fake"}

    def analyze_question(self, _question):
        return self


class OpportunityFactoryTests(unittest.TestCase):
    def test_proven_wallet_signal_becomes_calibrated_scanner_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "trade_log.db")
            log = TradeLog(db_path=db_path)
            self._insert_wallet_signal(log)
            cfg = OpportunityFactoryConfig(
                db_path=db_path,
                data_dir=tmp,
                report_path=str(Path(tmp) / "report.json"),
                heartbeat_path=str(Path(tmp) / "heartbeat"),
                min_wallet_profit_usdc=1_000_000,
                min_wallet_winrate=0.70,
            )
            factory = OpportunityFactory(cfg=cfg, trade_log=log)
            factory._gamma_market = lambda _market_id: {
                "id": "123",
                "conditionId": "cond123",
                "question": "Will BTC go up?",
                "slug": "btc-updown-test",
                "outcomes": '["Up","Down"]',
                "outcomePrices": '["0.42","0.58"]',
                "clobTokenIds": '["up-token","down-token"]',
            }

            stats = factory.run_once()
            rows = log.recent_brain_decisions(limit=3)

        self.assertEqual(stats["wallet_candidates"], 1)
        row = rows[0]
        self.assertEqual(row["agent"], "market_scanner")
        self.assertEqual(row["strategy"], "scanner_trade_opportunity")
        self.assertEqual(row["approved"], 1)
        features = json.loads(row["features_json"])
        self.assertTrue(features["estimated_win_probability_calibrated"])
        self.assertEqual(features["estimated_win_probability_source"], "wallet_external_winrate")
        self.assertEqual(features["selected_token_id"], "up-token")
        self.assertEqual(row["signal_source"], "opportunity_factory,proven_wallet")

    def test_alphainsider_without_direction_writes_attention_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "trade_log.db")
            log = TradeLog(db_path=db_path)
            alpha_path = Path(tmp) / "alpha.json"
            universe_path = Path(tmp) / "universe.json"
            alpha_path.write_text(json.dumps({
                "timeframes": {
                    "month": {
                        "top": [{
                            "family": "trend_momentum",
                            "strategy_id": "s1",
                            "name": "Trend",
                            "return_pct": 0.50,
                            "max_drawdown": 0.10,
                            "rank_performance": 2,
                            "rank_top": 1,
                            "quality_score": 1.2,
                        }]
                    }
                }
            }))
            universe_path.write_text(json.dumps({
                "candidates": [{
                    "eligible": True,
                    "market_id": "m1",
                    "question": "Bitcoin Up or Down",
                    "slug": "btc-updown-5m-x",
                    "route_agent": "btc_5min",
                    "asset": "btc",
                    "horizon": "5m",
                    "liquidity_usdc": 10000,
                    "up_token": "up",
                }]
            }))
            cfg = OpportunityFactoryConfig(
                db_path=db_path,
                data_dir=tmp,
                alphainsider_path=str(alpha_path),
                market_universe_path=str(universe_path),
                report_path=str(Path(tmp) / "report.json"),
                heartbeat_path=str(Path(tmp) / "heartbeat"),
                enable_alphainsider_directional=False,
            )

            stats = OpportunityFactory(cfg=cfg, trade_log=log).run_once()
            rows = log.recent_brain_decisions(limit=3)

        self.assertEqual(stats["attention_decisions"], 1)
        self.assertEqual(rows[0]["agent"], "opportunity_factory")
        self.assertEqual(rows[0]["approved"], 0)
        self.assertEqual(rows[0]["reason"], "proven_indicator_without_market_direction")

    def test_alphainsider_with_crypto_tape_direction_becomes_scanner_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "trade_log.db")
            log = TradeLog(db_path=db_path)
            alpha_path = Path(tmp) / "alpha.json"
            universe_path = Path(tmp) / "universe.json"
            alpha_path.write_text(json.dumps({
                "timeframes": {
                    "month": {
                        "top": [{
                            "family": "trend_momentum",
                            "strategy_id": "s-alpha",
                            "name": "Trend",
                            "return_pct": 0.50,
                            "max_drawdown": 0.10,
                            "rank_performance": 2,
                            "rank_top": 1,
                            "quality_score": 1.2,
                        }]
                    }
                }
            }))
            universe_path.write_text(json.dumps({
                "candidates": [{
                    "eligible": True,
                    "market_id": "123",
                    "question": "Bitcoin Up or Down",
                    "slug": "btc-updown-5m-x",
                    "route_agent": "btc_5min",
                    "asset": "btc",
                    "horizon": "5m",
                    "liquidity_usdc": 10000,
                    "up_token": "up",
                    "down_token": "down",
                }]
            }))
            cfg = OpportunityFactoryConfig(
                db_path=db_path,
                data_dir=tmp,
                alphainsider_path=str(alpha_path),
                market_universe_path=str(universe_path),
                report_path=str(Path(tmp) / "report.json"),
                heartbeat_path=str(Path(tmp) / "heartbeat"),
            )
            factory = OpportunityFactory(
                cfg=cfg,
                trade_log=log,
                crypto_tape=FakeCryptoTape(direction="bullish", probability=0.62, confidence=0.61),
            )
            factory._gamma_market = lambda _market_id: {
                "id": "123",
                "conditionId": "cond123",
                "question": "Bitcoin Up or Down",
                "slug": "btc-updown-5m-x",
                "outcomes": '["Up","Down"]',
                "outcomePrices": '["0.50","0.50"]',
                "clobTokenIds": '["up-token","down-token"]',
            }

            stats = factory.run_once()
            rows = log.recent_brain_decisions(limit=3)

        self.assertEqual(stats["alphainsider_directional_candidates"], 1)
        self.assertEqual(stats["attention_decisions"], 0)
        self.assertEqual(rows[0]["agent"], "market_scanner")
        self.assertEqual(rows[0]["strategy"], "scanner_trade_opportunity")
        self.assertEqual(rows[0]["approved"], 1)
        self.assertEqual(rows[0]["token_id"], "up-token")
        features = json.loads(rows[0]["features_json"])
        self.assertFalse(features["estimated_win_probability_calibrated"])
        self.assertEqual(
            features["estimated_win_probability_source"],
            "alphainsider_proven_family_plus_crypto_tape",
        )
        self.assertEqual(features["meta_timing"], "now")
        self.assertEqual(features["selected_outcome"], "Up")
        self.assertEqual(rows[0]["signal_source"], "opportunity_factory,alphainsider_proven,crypto_tape")

    def _insert_wallet_signal(self, log: TradeLog) -> None:
        with log._lock, log._connect() as conn:
            conn.execute(
                """
                INSERT INTO wallet_signals (
                    ts, wallet_address, wallet_profit_usdc, wallet_trades_30d,
                    market_id, market_question, direction, token_id, yes_price,
                    wallet_entry_price, wallet_size_usdc, wallet_winrate_external,
                    wallet_total_trades_external, wallet_rank, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'fresh')
                """,
                (
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                    "0xabc",
                    1_500_000,
                    42,
                    "123",
                    "Will BTC go up?",
                    "yes",
                    "up-token",
                    0.42,
                    0.41,
                    5000,
                    0.74,
                    100,
                    5,
                ),
            )


if __name__ == "__main__":
    unittest.main()
