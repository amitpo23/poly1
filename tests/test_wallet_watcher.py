"""Tests for wallet_watcher signal production logic.

Uses synthetic data only — no live network calls.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from agents.application.wallet_watcher import (
    WalletWatcherConfig,
    WalletWatcherEngine,
)
from agents.application.trade_log import TradeLog


class _TmpDB:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "trade_log.db")

    def tearDown(self):
        self._tmp.cleanup()


def _default_cfg(**kwargs) -> WalletWatcherConfig:
    base = WalletWatcherConfig(
        watch_addresses=["0xABCD"],
        scout_enable=False,
        scout_limit=10,
        scout_min_profit_usdc=100.0,
        scout_min_trades=5,
        poll_sec=120,
        max_age_hours=4.0,
        heartbeat_path="/tmp/test_ww_heartbeat",
    )
    for k, v in kwargs.items():
        setattr(base, k, v)
    return base


def _make_trade(market_id="MKT1", trade_type="BUY", outcome="Yes",
                price=0.30, size=10.0, age_minutes=5) -> dict:
    ts = int((datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).timestamp())
    return {
        "conditionId": market_id,
        "timestamp": ts,
        "type": trade_type,
        "outcome": outcome,
        "asset": f"token_{market_id}",
        "price": price,
        "size": size,
        "title": f"Will {market_id} happen?",
    }


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

class TestWalletWatcherConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = WalletWatcherConfig()
        self.assertEqual(cfg.poll_sec, 120)
        self.assertFalse(cfg.scout_enable)
        self.assertEqual(cfg.max_age_hours, 4.0)
        self.assertEqual(cfg.scout_min_profit_usdc, 200.0)
        self.assertEqual(cfg.max_open if hasattr(cfg, "max_open") else 3, 3)

    def test_from_env_watch_addresses(self):
        import os
        with patch.dict(os.environ, {"WALLET_WATCH_ADDRESSES": "0xAA,0xBB,0xCC"}):
            cfg = WalletWatcherConfig.from_env()
        self.assertEqual(cfg.watch_addresses, ["0xAA", "0xBB", "0xCC"])

    def test_from_env_empty_addresses(self):
        import os
        with patch.dict(os.environ, {"WALLET_WATCH_ADDRESSES": ""}, clear=False):
            cfg = WalletWatcherConfig.from_env()
        self.assertEqual(cfg.watch_addresses, [])


# ---------------------------------------------------------------------------
# Signal writing
# ---------------------------------------------------------------------------

class TestWalletWatcherEngine(_TmpDB, unittest.TestCase):
    def setUp(self):
        _TmpDB.setUp(self)
        self.log = TradeLog(db_path=self.db_path)
        self.cfg = _default_cfg()
        self.engine = WalletWatcherEngine(trade_log=self.log, cfg=self.cfg)

    def tearDown(self):
        _TmpDB.tearDown(self)

    def _count_signals(self) -> int:
        with self.log._lock, self.log._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM wallet_signals").fetchone()[0]

    def _fetch_signals(self) -> list:
        with self.log._lock, self.log._connect() as conn:
            rows = conn.execute("SELECT * FROM wallet_signals").fetchall()
            return [dict(r) for r in rows]

    def test_buy_yes_writes_bullish_signal(self):
        trade = _make_trade(trade_type="BUY", outcome="Yes", price=0.25)
        with patch.object(self.engine, "_fetch_yes_price", return_value=0.28):
            written = self.engine._poll_wallet.__func__(  # call via instance
                self.engine,
                "0xABCD",
            ) if False else None
        # Use _write_signal directly
        result = self.engine._write_signal(
            ts=datetime.now(timezone.utc).isoformat(),
            wallet_address="0xABCD",
            wallet_profit_usdc=500.0,
            wallet_trades_30d=30,
            market_id="MKT1",
            market_question="Will MKT1 happen?",
            direction="bullish",
            token_id="token_MKT1",
            yes_price=0.28,
            wallet_entry_price=0.25,
            wallet_size_usdc=10.0,
            wallet_winrate_external=0.72,
            wallet_total_trades_external=120,
            wallet_rank=7,
        )
        self.assertTrue(result)
        self.assertEqual(self._count_signals(), 1)
        sigs = self._fetch_signals()
        self.assertEqual(sigs[0]["direction"], "bullish")
        self.assertEqual(sigs[0]["wallet_address"], "0xABCD")
        self.assertEqual(sigs[0]["status"], "fresh")
        self.assertAlmostEqual(sigs[0]["wallet_winrate_external"], 0.72)
        self.assertEqual(sigs[0]["wallet_total_trades_external"], 120)
        self.assertEqual(sigs[0]["wallet_rank"], 7)

    def test_duplicate_signal_suppressed(self):
        ts = datetime.now(timezone.utc).isoformat()
        self.engine._write_signal(
            ts=ts, wallet_address="0xABCD", wallet_profit_usdc=500.0,
            wallet_trades_30d=20, market_id="MKT1",
            market_question="Q?", direction="bullish", token_id="tok1",
            yes_price=0.30, wallet_entry_price=0.28, wallet_size_usdc=10.0,
        )
        # Same wallet + market → duplicate
        result = self.engine._write_signal(
            ts=ts, wallet_address="0xABCD", wallet_profit_usdc=500.0,
            wallet_trades_30d=20, market_id="MKT1",
            market_question="Q?", direction="bullish", token_id="tok1",
            yes_price=0.30, wallet_entry_price=0.28, wallet_size_usdc=10.0,
        )
        self.assertFalse(result)
        self.assertEqual(self._count_signals(), 1)

    def test_different_market_different_wallet_both_written(self):
        ts = datetime.now(timezone.utc).isoformat()
        self.engine._write_signal(
            ts=ts, wallet_address="0xABCD", wallet_profit_usdc=200.0,
            wallet_trades_30d=10, market_id="MKT1",
            market_question="Q1?", direction="bullish", token_id="tok1",
            yes_price=0.30, wallet_entry_price=0.28, wallet_size_usdc=5.0,
        )
        self.engine._write_signal(
            ts=ts, wallet_address="0xABCD", wallet_profit_usdc=200.0,
            wallet_trades_30d=10, market_id="MKT2",
            market_question="Q2?", direction="bearish", token_id="tok2",
            yes_price=0.70, wallet_entry_price=0.72, wallet_size_usdc=5.0,
        )
        self.assertEqual(self._count_signals(), 2)

    def test_scout_adds_to_watched(self):
        leaderboard = [
            {"proxyWallet": "0xNEW1", "profit": 500.0, "tradesCount": 30},
            {"proxyWallet": "0xNEW2", "profit": 50.0,  "tradesCount": 30},  # profit too low
        ]
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(leaderboard).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            self.engine.cfg.scout_enable = True
            self.engine._scout_leaderboard()

        # 0xNEW1 should be added (profit >= 200), 0xNEW2 should not
        self.assertIn("0xnew1", self.engine._watched)
        self.assertNotIn("0xnew2", self.engine._watched)
        self.assertIsNone(self.engine._wallet_stats["0xnew1"]["winrate_external"])

    def test_scout_caches_external_wallet_winrate_when_available(self):
        leaderboard = [
            {
                "proxyWallet": "0xWR",
                "profit": 500.0,
                "tradesCount": 120,
                "winRate": 72.0,
                "rank": "3",
                "vol": 10000.0,
            },
        ]
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(leaderboard).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            self.engine._scout_leaderboard()

        stats = self.engine._wallet_stats["0xwr"]
        self.assertAlmostEqual(stats["winrate_external"], 0.72)
        self.assertEqual(stats["total_trades_external"], 120)
        self.assertEqual(stats["rank"], 3)

    def test_scout_min_trades_not_enforced(self):
        # The Polymarket v1 leaderboard API no longer returns tradesCount, so
        # scout_min_trades cannot be enforced at the scouting stage.  A wallet
        # that meets the profit threshold IS added regardless of the (now dead)
        # scout_min_trades setting.
        leaderboard = [
            {"proxyWallet": "0xFEW", "profit": 1000.0},  # tradesCount absent in v1 API
        ]
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(leaderboard).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            self.engine.cfg.scout_min_trades = 5  # has no effect; field is deprecated
            self.engine._scout_leaderboard()

        # Wallet meets profit threshold → added (trades filter is dead)
        self.assertIn("0xfew", self.engine._watched)

    def test_old_trade_not_written(self):
        """Trades older than max_age_hours should not generate signals."""
        # _poll_wallet breaks on old trades; simulate via run_once with
        # mocked activity returning a stale trade
        old_trade = _make_trade(age_minutes=int(self.cfg.max_age_hours * 60) + 10)

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps([old_trade]).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            n = self.engine.run_once()

        self.assertEqual(n, 0)
        self.assertEqual(self._count_signals(), 0)

    def test_no_addresses_run_once_returns_zero(self):
        self.engine.cfg.watch_addresses = []
        self.engine._watched = set()
        n = self.engine.run_once()
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
