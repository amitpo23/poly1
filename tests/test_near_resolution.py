"""Tests for the near_resolution agent's entry decision logic.

Uses stub Gamma/Tavily responses and a real in-memory TradeLog.
No live network calls.
"""
from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agents.application.near_resolution import (
    NearResolutionConfig,
    NearResolutionEngine,
)
from agents.application.trade_log import NEAR_RESOLUTION_OPEN, TradeLog


class _TmpDB:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "trade_log.db")

    def tearDown(self):
        self._tmp.cleanup()


def _default_cfg(**kwargs) -> NearResolutionConfig:
    base = NearResolutionConfig(
        min_hours=0.5,
        max_hours=36.0,
        max_entry_price=0.15,
        min_liquidity=1000.0,  # low for tests
        min_confidence=0.60,
        position_size_usdc=2.5,
        reserve_usdc=15.0,
        poll_sec=60,
        max_open=3,
        heartbeat_path="/tmp/test_nr_heartbeat",
    )
    for k, v in kwargs.items():
        setattr(base, k, v)
    return base


def _fake_candidate(
    market_id="MKT1",
    yes_price=0.10,
    no_price=0.90,
    cheap_side="yes",
    cheap_price=0.10,
    hours_left=5.0,
    straddle_viable=False,
    single_viable=True,
):
    from datetime import datetime, timezone, timedelta
    return {
        "market_id": market_id,
        "question": f"Will something happen on {market_id}?",
        "yes_price": yes_price,
        "no_price": no_price,
        "cheap_side": cheap_side,
        "cheap_price": cheap_price,
        "price_sum": yes_price + no_price,
        "straddle_viable": straddle_viable,
        "single_viable": single_viable,
        "hours_left": hours_left,
        "end_dt": datetime.now(timezone.utc) + timedelta(hours=hours_left),
        "outcomes": ["Yes", "No"],
        "tokens": ["TOK_YES", "TOK_NO"],
        "raw": {},
    }


class TestNearResolutionConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = NearResolutionConfig()
        self.assertEqual(cfg.max_entry_price, 0.65)
        self.assertEqual(cfg.direction_min_confidence, 0.52)
        self.assertEqual(cfg.poll_sec, 60)


class TestScanCandidatesFilter(_TmpDB, unittest.TestCase):
    """Test that scan_candidates correctly filters by price and hours."""

    def _engine(self, raw_markets):
        cfg = _default_cfg()
        log = TradeLog(self.db_path)
        pm = MagicMock()
        engine = NearResolutionEngine(
            polymarket=pm, trade_log=log, risk_gate=None, cfg=cfg, execute=False
        )
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.read.return_value = json.dumps(raw_markets).encode()
            mock_open.return_value = mock_resp
            return engine.scan_candidates()

    def _make_raw_market(self, market_id, hours_left, yes_price,
                         volume=5000.0, end_offset_hours=None):
        from datetime import datetime, timezone, timedelta
        end_dt = datetime.now(timezone.utc) + timedelta(hours=hours_left)
        return {
            "id": market_id,
            "question": f"Market {market_id}?",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["T1", "T2"]',
            "endDate": end_dt.isoformat(),
            "outcomePrices": json.dumps([str(yes_price), str(1.0 - yes_price)]),
            "volumeClob": volume,
            "active": True,
            "closed": False,
        }

    def test_cheap_yes_included(self):
        markets = [self._make_raw_market("M1", hours_left=5, yes_price=0.10)]
        candidates = self._engine(markets)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["cheap_side"], "yes")

    def test_cheap_no_included(self):
        markets = [self._make_raw_market("M2", hours_left=5, yes_price=0.88)]
        candidates = self._engine(markets)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["cheap_side"], "no")

    def test_expensive_both_excluded(self):
        markets = [self._make_raw_market("M3", hours_left=5, yes_price=0.50)]
        candidates = self._engine(markets)
        self.assertEqual(len(candidates), 0)

    def test_too_far_future_excluded(self):
        markets = [self._make_raw_market("M4", hours_left=100, yes_price=0.10)]
        candidates = self._engine(markets)
        self.assertEqual(len(candidates), 0)

    def test_low_liquidity_excluded(self):
        markets = [self._make_raw_market("M5", hours_left=5, yes_price=0.10, volume=10.0)]
        candidates = self._engine(markets)
        self.assertEqual(len(candidates), 0)


class TestMaybeEnterAll(_TmpDB, unittest.TestCase):
    def _engine(self, cfg=None, execute=False):
        cfg = cfg or _default_cfg()
        log = TradeLog(self.db_path)
        pm = MagicMock()
        pm.execute_market_order.return_value = {
            "status": "matched",
            "order_avg_price_estimate": 0.10,
            "amount_usdc": 2.5,
            "token_id": "TOK_YES",
        }
        rg = MagicMock()
        rg.ok.return_value = True
        rg.reason.return_value = ""
        engine = NearResolutionEngine(
            polymarket=pm, trade_log=log, risk_gate=rg, cfg=cfg, execute=execute
        )
        engine.meta_brain = None
        return engine, log

    def test_shadow_writes_nr_open(self):
        engine, log = self._engine()
        engine.scan_candidates = lambda: [_fake_candidate()]
        engine._llm_direction = lambda q, yp, np, ed: {"direction": "yes", "confidence": 0.80, "reasoning": "test"}
        n = engine.maybe_enter_all()
        self.assertEqual(n, 1)
        rows = log.recent(limit=5)
        self.assertEqual(rows[0]["status"], NEAR_RESOLUTION_OPEN)

    def test_llm_direction_sends_json_system_instruction(self):
        engine, _ = self._engine()

        class FakeLLM:
            def __init__(self):
                self.messages = None

            def invoke(self, messages):
                self.messages = messages
                return type(
                    "Response",
                    (),
                    {"content": '{"direction":"yes","confidence":0.8,"reasoning":"ok"}'},
                )()

        fake = FakeLLM()
        engine._llm = fake
        engine._init_llm = lambda: None
        engine._prompter = type(
            "Prompter",
            (),
            {"binary_market_direction": lambda *args, **kwargs: "Return JSON for this market."},
        )()

        messages_mod = types.ModuleType("langchain_core.messages")

        class TestMessage:
            def __init__(self, content):
                self.content = content

        messages_mod.HumanMessage = TestMessage
        messages_mod.SystemMessage = TestMessage
        langchain_core_mod = types.ModuleType("langchain_core")
        with patch.dict(
            sys.modules,
            {
                "langchain_core": langchain_core_mod,
                "langchain_core.messages": messages_mod,
            },
        ):
            result = engine._llm_direction(
                "Will BTC be up?",
                0.45,
                0.55,
                "2026-05-22T00:00:00Z",
            )

        self.assertEqual(result["direction"], "yes")
        self.assertIsNotNone(fake.messages)
        self.assertIn("json", fake.messages[0].content.lower())

    def test_low_confidence_skipped(self):
        engine, log = self._engine()
        engine.scan_candidates = lambda: [_fake_candidate()]
        engine._llm_direction = lambda q, yp, np, ed: {"direction": "yes", "confidence": 0.30, "reasoning": "test"}  # below threshold
        n = engine.maybe_enter_all()
        self.assertEqual(n, 0)

    def test_risk_gate_blocked(self):
        engine, log = self._engine()
        engine.risk_gate.ok.return_value = False
        engine.scan_candidates = lambda: [_fake_candidate()]
        engine._llm_direction = lambda q, yp, np, ed: {"direction": "yes", "confidence": 0.90, "reasoning": "test"}
        n = engine.maybe_enter_all()
        self.assertEqual(n, 0)

    def test_dedupe_skips_active_market(self):
        engine, log = self._engine()
        # Pre-populate an active trade for MKT1
        log.insert_pending(
            cycle_id="c1", market_id="MKT1", token_id="TOK_YES",
            side="BUY", price=0.10, size_usdc=2.5, confidence=0.8,
        )
        engine.scan_candidates = lambda: [_fake_candidate(market_id="MKT1")]
        engine._llm_direction = lambda q, yp, np, ed: {"direction": "yes", "confidence": 0.90, "reasoning": "test"}
        n = engine.maybe_enter_all()
        self.assertEqual(n, 0)

    def test_max_open_respected(self):
        engine, log = self._engine(cfg=_default_cfg(max_open=1))
        # Already one open position
        pid = log.insert_pending(
            cycle_id="c0", market_id="MKTX", token_id="TX",
            side="BUY", price=0.10, size_usdc=2.5, confidence=0.8,
        )
        log.mark(pid, NEAR_RESOLUTION_OPEN)
        engine.scan_candidates = lambda: [
            _fake_candidate(market_id="MKT1"),
            _fake_candidate(market_id="MKT2"),
        ]
        engine._llm_direction = lambda q, yp, np, ed: {"direction": "yes", "confidence": 0.90, "reasoning": "test"}
        n = engine.maybe_enter_all()
        self.assertEqual(n, 0)

    def test_buy_side_for_cheap_yes(self):
        engine, log = self._engine()
        engine.scan_candidates = lambda: [_fake_candidate(cheap_side="yes")]
        engine._llm_direction = lambda q, yp, np, ed: {"direction": "yes", "confidence": 0.80, "reasoning": "test"}
        engine.maybe_enter_all()
        rows = log.recent(limit=5)
        self.assertEqual(rows[0]["side"], "BUY")

    def test_sell_side_for_cheap_no(self):
        engine, log = self._engine()
        engine.scan_candidates = lambda: [
            _fake_candidate(cheap_side="no", yes_price=0.88, no_price=0.12)
        ]
        engine._llm_direction = lambda q, yp, np, ed: {"direction": "no", "confidence": 0.80, "reasoning": "test"}
        engine.maybe_enter_all()
        rows = log.recent(limit=5)
        self.assertEqual(rows[0]["side"], "SELL")

    def test_live_execute_calls_polymarket(self):
        engine, log = self._engine(execute=True)
        engine.scan_candidates = lambda: [_fake_candidate()]
        engine._llm_direction = lambda q, yp, np, ed: {"direction": "yes", "confidence": 0.80, "reasoning": "test"}
        engine.meta_brain = MagicMock()
        engine.meta_brain.synthesize.return_value = type(
            "Decision",
            (),
            {
                "approved": True,
                "reason": "test_approved",
                "score": 0.9,
                "features": {"test": True},
                "entry_timing": "now",
            },
        )()
        engine.maybe_enter_all()
        engine.polymarket.execute_market_order.assert_called_once()
        rows = log.recent(limit=5)
        self.assertEqual(rows[0]["status"], NEAR_RESOLUTION_OPEN)


if __name__ == "__main__":
    unittest.main()
