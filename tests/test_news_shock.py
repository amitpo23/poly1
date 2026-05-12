"""Tests for the news_shock agent's entry decision logic.

Uses a stub TradeLog with synthetic news_signals rows.
No live network calls.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from agents.application.news_shock import (
    NewsShockConfig,
    NewsShockEngine,
)
from agents.application.trade_log import NEWS_SHOCK_OPEN, TradeLog


class _TmpDB:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "trade_log.db")

    def tearDown(self):
        self._tmp.cleanup()


def _default_cfg(**kwargs) -> NewsShockConfig:
    base = NewsShockConfig(
        min_score=0.70,
        max_age_hours=2.0,
        min_ev=0.04,
        max_entry_price=0.60,
        min_liquidity=1000.0,  # low for tests
        position_size_usdc=2.5,
        reserve_usdc=15.0,
        poll_sec=30,
        max_open=3,
        heartbeat_path="/tmp/test_ns_heartbeat",
    )
    for k, v in kwargs.items():
        setattr(base, k, v)
    return base


def _insert_signal(log: TradeLog, market_id: str, direction: str,
                   materiality: float, age_minutes: int = 5) -> None:
    """Helper to write a synthetic news_signals row."""
    ts = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes))
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%S")
    with log._lock, log._connect() as conn:
        conn.execute(
            "INSERT INTO news_signals "
            "(ts, headline, source, url, market_id, market_question, "
            "direction, materiality, relevance_score, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                ts_str,
                f"Test headline for {market_id}",
                "test",
                "",
                market_id,
                f"Will {market_id} happen?",
                direction,
                materiality,
                0.8,
                "news_signal",
            ),
        )


def _fake_gamma_market(market_id="MKT1", yes_price=0.30, active=True, closed=False,
                        volume=10000.0):
    return {
        "id": market_id,
        "question": f"Market {market_id}?",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["TOK_YES", "TOK_NO"]',
        "outcomePrices": json.dumps([str(yes_price), str(1.0 - yes_price)]),
        "active": active,
        "closed": closed,
        "volumeClob": volume,
    }


class TestNewsShockConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = NewsShockConfig()
        self.assertEqual(cfg.min_score, 0.70)
        self.assertEqual(cfg.min_ev, 0.04)
        self.assertEqual(cfg.poll_sec, 30)


class TestReadFreshSignals(_TmpDB, unittest.TestCase):
    def test_reads_high_materiality_signals(self):
        log = TradeLog(self.db_path)
        _insert_signal(log, "MKT1", "bullish", 0.80, age_minutes=30)
        _insert_signal(log, "MKT2", "bearish", 0.90, age_minutes=10)

        cfg = _default_cfg()
        engine = NewsShockEngine(
            polymarket=MagicMock(), trade_log=log, risk_gate=None, cfg=cfg
        )
        signals = engine._read_fresh_signals()
        self.assertEqual(len(signals), 2)

    def test_excludes_low_materiality(self):
        log = TradeLog(self.db_path)
        _insert_signal(log, "MKT1", "bullish", 0.30, age_minutes=5)

        cfg = _default_cfg()
        engine = NewsShockEngine(
            polymarket=MagicMock(), trade_log=log, risk_gate=None, cfg=cfg
        )
        signals = engine._read_fresh_signals()
        self.assertEqual(len(signals), 0)

    def test_excludes_old_signals(self):
        log = TradeLog(self.db_path)
        _insert_signal(log, "MKT1", "bullish", 0.90, age_minutes=300)  # 5 hours ago

        cfg = _default_cfg(max_age_hours=2.0)
        engine = NewsShockEngine(
            polymarket=MagicMock(), trade_log=log, risk_gate=None, cfg=cfg
        )
        signals = engine._read_fresh_signals()
        self.assertEqual(len(signals), 0)

    def test_excludes_consumed_signals(self):
        log = TradeLog(self.db_path)
        _insert_signal(log, "MKT1", "bullish", 0.90, age_minutes=5)
        with log._lock, log._connect() as conn:
            conn.execute(
                "UPDATE news_signals SET status = 'acted' WHERE market_id = ?",
                ("MKT1",),
            )

        cfg = _default_cfg()
        engine = NewsShockEngine(
            polymarket=MagicMock(), trade_log=log, risk_gate=None, cfg=cfg
        )
        signals = engine._read_fresh_signals()
        self.assertEqual(len(signals), 0)


class TestEVCalculation(_TmpDB, unittest.TestCase):
    def _engine_with_gamma(self, gamma_market):
        log = TradeLog(self.db_path)
        pm = MagicMock()
        pm.execute_market_order.return_value = {
            "status": "matched",
            "order_avg_price_estimate": 0.30,
            "amount_usdc": 2.5,
            "token_id": "TOK_YES",
        }
        rg = MagicMock()
        rg.ok.return_value = True
        rg.reason.return_value = ""
        cfg = _default_cfg()
        engine = NewsShockEngine(
            polymarket=pm, trade_log=log, risk_gate=rg, cfg=cfg, execute=False
        )
        engine._gamma_market = lambda mid: gamma_market
        return engine, log

    def test_bullish_signal_buys_yes(self):
        engine, log = self._engine_with_gamma(_fake_gamma_market(yes_price=0.30))
        _insert_signal(log, "MKT1", "bullish", 0.90)
        engine.maybe_enter_all()
        rows = log.recent(limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["side"], "BUY")
        self.assertEqual(rows[0]["status"], NEWS_SHOCK_OPEN)

    def test_bearish_signal_buys_no(self):
        engine, log = self._engine_with_gamma(_fake_gamma_market(yes_price=0.70))
        _insert_signal(log, "MKT1", "bearish", 0.90)
        engine.maybe_enter_all()
        rows = log.recent(limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["side"], "SELL")

    def test_low_ev_skipped(self):
        # yes_price=0.95 → bullish EV = 0.80 * (1 - 0.95) = 0.04 — exactly at threshold
        # Use yes_price=0.96 for EV=0.032 < 0.04
        engine, log = self._engine_with_gamma(_fake_gamma_market(yes_price=0.96))
        _insert_signal(log, "MKT1", "bullish", 0.80)
        n = engine.maybe_enter_all()
        self.assertEqual(n, 0)

    def test_high_entry_price_skipped(self):
        # max_entry_price=0.60; bearish on yes_price=0.20 → entry=no_price=0.80 > 0.60
        engine, log = self._engine_with_gamma(_fake_gamma_market(yes_price=0.20))
        _insert_signal(log, "MKT1", "bearish", 0.90)
        n = engine.maybe_enter_all()
        self.assertEqual(n, 0)

    def test_dedupe_skips_active_market(self):
        engine, log = self._engine_with_gamma(_fake_gamma_market(yes_price=0.30))
        log.insert_pending(
            cycle_id="c0", market_id="MKT1", token_id="TOK_YES",
            side="BUY", price=0.30, size_usdc=2.5, confidence=0.9,
        )
        _insert_signal(log, "MKT1", "bullish", 0.90)
        n = engine.maybe_enter_all()
        self.assertEqual(n, 0)

    def test_risk_gate_blocked(self):
        engine, log = self._engine_with_gamma(_fake_gamma_market(yes_price=0.30))
        engine.risk_gate.ok.return_value = False
        _insert_signal(log, "MKT1", "bullish", 0.90)
        n = engine.maybe_enter_all()
        self.assertEqual(n, 0)

    def test_closed_market_skipped(self):
        engine, log = self._engine_with_gamma(
            _fake_gamma_market(yes_price=0.30, active=False, closed=True)
        )
        _insert_signal(log, "MKT1", "bullish", 0.90)
        n = engine.maybe_enter_all()
        self.assertEqual(n, 0)

    def test_max_open_respected(self):
        engine, log = self._engine_with_gamma(_fake_gamma_market(yes_price=0.30))
        engine.cfg = _default_cfg(max_open=1)
        # Simulate 1 existing open NS position
        pid = log.insert_pending(
            cycle_id="c0", market_id="MKTX", token_id="TX",
            side="BUY", price=0.30, size_usdc=2.5, confidence=0.9,
        )
        log.mark(pid, NEWS_SHOCK_OPEN)
        _insert_signal(log, "MKT1", "bullish", 0.90)
        n = engine.maybe_enter_all()
        self.assertEqual(n, 0)

    def test_signal_marked_acted_on_entry(self):
        engine, log = self._engine_with_gamma(_fake_gamma_market(yes_price=0.30))
        _insert_signal(log, "MKT1", "bullish", 0.90)
        n = engine.maybe_enter_all()
        self.assertEqual(n, 1)
        with log._lock, log._connect() as conn:
            row = conn.execute(
                "SELECT status FROM news_signals WHERE market_id = ?",
                ("MKT1",),
            ).fetchone()
        self.assertEqual(row["status"], "acted")

    def test_signal_marked_skipped_on_dedupe(self):
        engine, log = self._engine_with_gamma(_fake_gamma_market(yes_price=0.30))
        log.insert_pending(
            cycle_id="c0", market_id="MKT1", token_id="TOK_YES",
            side="BUY", price=0.30, size_usdc=2.5, confidence=0.9,
        )
        _insert_signal(log, "MKT1", "bullish", 0.90)
        n = engine.maybe_enter_all()
        self.assertEqual(n, 0)
        with log._lock, log._connect() as conn:
            row = conn.execute(
                "SELECT status FROM news_signals WHERE market_id = ?",
                ("MKT1",),
            ).fetchone()
        self.assertEqual(row["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
