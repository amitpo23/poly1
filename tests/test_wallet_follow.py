"""Tests for wallet_follow entry decision logic.

Uses synthetic wallet_signals rows and stub Polymarket/RiskGate.
No live network calls.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from agents.application.wallet_follow import (
    WalletFollowConfig,
    WalletFollowEngine,
)
from agents.application.trade_log import WALLET_FOLLOW_OPEN, TradeLog


class _AllowBrain:
    def evaluate_general_entry(self, **_kwargs):
        class Decision:
            approved = True
            reason = "test_approved"
            score = 0.9
            features = {"test": True}
        return Decision()


class _TmpDB:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "trade_log.db")

    def tearDown(self):
        self._tmp.cleanup()


def _default_cfg(**kwargs) -> WalletFollowConfig:
    base = WalletFollowConfig(
        min_confidence=0.50,
        profit_scale=1000.0,
        min_ev=0.03,
        max_entry_price=0.70,
        min_liquidity=1000.0,  # low for tests
        position_size_usdc=2.5,
        reserve_usdc=15.0,
        max_age_hours=4.0,
        poll_sec=60,
        max_open=3,
        heartbeat_path="/tmp/test_wf_heartbeat",
    )
    for k, v in kwargs.items():
        setattr(base, k, v)
    return base


def _insert_signal(log: TradeLog, market_id: str, direction: str,
                   wallet_profit: float = 500.0, age_minutes: int = 5,
                   yes_price: float = 0.30) -> int:
    ts = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes))
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S")
    with log._lock, log._connect() as conn:
        cur = conn.execute(
            "INSERT INTO wallet_signals "
            "(ts, wallet_address, wallet_profit_usdc, wallet_trades_30d, "
            "market_id, market_question, direction, token_id, "
            "yes_price, wallet_entry_price, wallet_size_usdc, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,'fresh')",
            (
                ts_str,
                "0xABCD",
                wallet_profit,
                20,
                market_id,
                f"Will {market_id} happen?",
                direction,
                f"token_{market_id}",
                yes_price,
                yes_price - 0.02,
                10.0,
            ),
        )
        return cur.lastrowid


def _fake_gamma_market(market_id="MKT1", yes_price=0.30, active=True,
                        closed=False, volume=10000.0) -> dict:
    return {
        "id": market_id,
        "question": f"Market {market_id}?",
        "active": active,
        "closed": closed,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([f"tok_yes_{market_id}", f"tok_no_{market_id}"]),
        "outcomePrices": json.dumps([str(yes_price), str(1.0 - yes_price)]),
        "volumeClob": volume,
    }


class _PassGate:
    def ok(self): return True
    def reason(self): return ""


class _FailGate:
    def ok(self): return False
    def reason(self): return "balance_too_low"


def _make_engine(log, cfg=None, execute=False, gate=None) -> WalletFollowEngine:
    return WalletFollowEngine(
        polymarket=None,
        trade_log=log,
        risk_gate=gate or _PassGate(),
        cfg=cfg or _default_cfg(),
        execute=execute,
    )


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

class TestWalletFollowConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = WalletFollowConfig()
        self.assertEqual(cfg.min_confidence, 0.50)
        self.assertEqual(cfg.profit_scale, 1000.0)
        self.assertEqual(cfg.min_ev, 0.03)
        self.assertEqual(cfg.max_entry_price, 0.70)
        self.assertEqual(cfg.max_open, 3)

    def test_from_env(self):
        import os
        with patch.dict(os.environ, {
            "WALLET_FOLLOW_MIN_CONFIDENCE": "0.60",
            "WALLET_FOLLOW_MIN_EV": "0.05",
        }):
            cfg = WalletFollowConfig.from_env()
        self.assertAlmostEqual(cfg.min_confidence, 0.60)
        self.assertAlmostEqual(cfg.min_ev, 0.05)


# ---------------------------------------------------------------------------
# Confidence calculation
# ---------------------------------------------------------------------------

class TestConfidence(_TmpDB, unittest.TestCase):
    def setUp(self):
        _TmpDB.setUp(self)
        self.log = TradeLog(db_path=self.db_path)
        self.cfg = _default_cfg()
        self.engine = _make_engine(self.log, self.cfg)

    def tearDown(self):
        _TmpDB.tearDown(self)

    def test_full_confidence_at_scale(self):
        # profit = scale → confidence = 1.0
        c = self.engine._confidence(1000.0)
        self.assertAlmostEqual(c, 1.0)

    def test_partial_confidence(self):
        c = self.engine._confidence(500.0)
        self.assertAlmostEqual(c, 0.5)

    def test_min_confidence_floor(self):
        # wallet with $0 profit → clamped to min_confidence
        c = self.engine._confidence(0.0)
        self.assertAlmostEqual(c, self.cfg.min_confidence)

    def test_confidence_exceeds_1_clamped(self):
        c = self.engine._confidence(5000.0)
        self.assertLessEqual(c, 1.0)


# ---------------------------------------------------------------------------
# Entry decisions — shadow mode
# ---------------------------------------------------------------------------

class TestWalletFollowEntry(_TmpDB, unittest.TestCase):
    def setUp(self):
        _TmpDB.setUp(self)
        self.log = TradeLog(db_path=self.db_path)
        self.cfg = _default_cfg()
        self.engine = _make_engine(self.log, self.cfg, execute=False)

    def tearDown(self):
        _TmpDB.tearDown(self)

    def _count_open(self) -> int:
        with self.log._lock, self.log._connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status = ?",
                (WALLET_FOLLOW_OPEN,),
            ).fetchone()[0]

    def test_bullish_signal_writes_buy_yes(self):
        _insert_signal(self.log, "MKT1", "bullish", wallet_profit=600.0, yes_price=0.30)
        with patch.object(self.engine, "_gamma_market",
                          return_value=_fake_gamma_market("MKT1", yes_price=0.30)):
            n = self.engine.maybe_enter_all()
        self.assertEqual(n, 1)
        with self.log._lock, self.log._connect() as conn:
            row = conn.execute(
                "SELECT side FROM trades WHERE status=?", (WALLET_FOLLOW_OPEN,)
            ).fetchone()
        self.assertEqual(row["side"], "BUY")

    def test_bearish_signal_writes_sell(self):
        _insert_signal(self.log, "MKT2", "bearish", wallet_profit=800.0, yes_price=0.65)
        with patch.object(self.engine, "_gamma_market",
                          return_value=_fake_gamma_market("MKT2", yes_price=0.65)):
            n = self.engine.maybe_enter_all()
        self.assertEqual(n, 1)
        with self.log._lock, self.log._connect() as conn:
            row = conn.execute(
                "SELECT side FROM trades WHERE status=?", (WALLET_FOLLOW_OPEN,)
            ).fetchone()
        self.assertEqual(row["side"], "SELL")

    def test_low_ev_skipped(self):
        # yes_price=0.95 → bullish EV = confidence × (1-0.95) = small
        cfg = _default_cfg(min_ev=0.10)
        engine = _make_engine(self.log, cfg, execute=False)
        _insert_signal(self.log, "MKT3", "bullish", wallet_profit=800.0, yes_price=0.95)
        with patch.object(engine, "_gamma_market",
                          return_value=_fake_gamma_market("MKT3", yes_price=0.95)):
            n = engine.maybe_enter_all()
        self.assertEqual(n, 0)

    def test_high_entry_price_skipped(self):
        # bearish at yes_price=0.15 → entry_price(NO)=0.85 > max 0.70
        cfg = _default_cfg(max_entry_price=0.70)
        engine = _make_engine(self.log, cfg, execute=False)
        _insert_signal(self.log, "MKT4", "bearish", wallet_profit=900.0, yes_price=0.15)
        with patch.object(engine, "_gamma_market",
                          return_value=_fake_gamma_market("MKT4", yes_price=0.15)):
            n = engine.maybe_enter_all()
        self.assertEqual(n, 0)

    def test_dedupe_skips_market_with_active_trade(self):
        _insert_signal(self.log, "MKT5", "bullish", wallet_profit=700.0, yes_price=0.28)
        # Simulate an active trade for MKT5
        with patch.object(self.log, "has_active_trade_for_market", return_value=True):
            with patch.object(self.engine, "_gamma_market",
                              return_value=_fake_gamma_market("MKT5")):
                n = self.engine.maybe_enter_all()
        self.assertEqual(n, 0)

    def test_risk_gate_blocks(self):
        _insert_signal(self.log, "MKT6", "bullish", wallet_profit=600.0, yes_price=0.30)
        engine = _make_engine(self.log, self.cfg, gate=_FailGate())
        with patch.object(engine, "_gamma_market",
                          return_value=_fake_gamma_market("MKT6")):
            n = engine.maybe_enter_all()
        self.assertEqual(n, 0)

    def test_closed_market_skipped(self):
        _insert_signal(self.log, "MKT7", "bullish", wallet_profit=600.0, yes_price=0.30)
        with patch.object(self.engine, "_gamma_market",
                          return_value=_fake_gamma_market("MKT7", closed=True)):
            n = self.engine.maybe_enter_all()
        self.assertEqual(n, 0)

    def test_max_open_respected(self):
        cfg = _default_cfg(max_open=1)
        engine = _make_engine(self.log, cfg, execute=False)
        _insert_signal(self.log, "MKT8", "bullish", wallet_profit=700.0, yes_price=0.30)
        _insert_signal(self.log, "MKT9", "bullish", wallet_profit=800.0, yes_price=0.35)
        with patch.object(engine, "_gamma_market", side_effect=[
            _fake_gamma_market("MKT8", yes_price=0.30),
            _fake_gamma_market("MKT9", yes_price=0.35),
        ]):
            n = engine.maybe_enter_all()
        self.assertEqual(n, 1)

    def test_signal_marked_acted_on_entry(self):
        _insert_signal(self.log, "MKT10", "bullish", wallet_profit=500.0, yes_price=0.25)
        with patch.object(self.engine, "_gamma_market",
                          return_value=_fake_gamma_market("MKT10", yes_price=0.25)):
            self.engine.maybe_enter_all()
        with self.log._lock, self.log._connect() as conn:
            row = conn.execute(
                "SELECT status FROM wallet_signals WHERE market_id='MKT10'"
            ).fetchone()
        self.assertEqual(row["status"], "acted")

    def test_no_signals_returns_zero(self):
        n = self.engine.maybe_enter_all()
        self.assertEqual(n, 0)


# ---------------------------------------------------------------------------
# Live path
# ---------------------------------------------------------------------------

class TestWalletFollowLive(_TmpDB, unittest.TestCase):
    def setUp(self):
        _TmpDB.setUp(self)
        self.log = TradeLog(db_path=self.db_path)
        self.cfg = _default_cfg()

    def tearDown(self):
        _TmpDB.tearDown(self)

    def test_live_execute_writes_wallet_follow_open(self):
        mock_poly = MagicMock()
        mock_poly.execute_market_order.return_value = {
            "status": "matched",
            "orderID": "ord123",
        }
        engine = WalletFollowEngine(
            polymarket=mock_poly,
            trade_log=self.log,
            risk_gate=_PassGate(),
            cfg=self.cfg,
            execute=True,
            brain=_AllowBrain(),
        )
        engine.meta_brain = None
        _insert_signal(self.log, "MKT11", "bullish", wallet_profit=700.0, yes_price=0.30)
        with patch.object(engine, "_gamma_market",
                          return_value=_fake_gamma_market("MKT11", yes_price=0.30)):
            n = engine.maybe_enter_all()
        self.assertEqual(n, 1)
        with self.log._lock, self.log._connect() as conn:
            row = conn.execute(
                "SELECT status FROM trades WHERE status=?",
                (WALLET_FOLLOW_OPEN,),
            ).fetchone()
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
