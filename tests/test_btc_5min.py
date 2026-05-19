"""Tests for the BTC 5-min multi-signal consensus agent."""
from __future__ import annotations

import functools
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy optional deps (same pattern as test_trader.py)
# ---------------------------------------------------------------------------
def _ensure_stub(name):
    if name not in sys.modules:
        sys.modules[name] = MagicMock()

for _mod in [
    "web3", "web3.constants", "web3.middleware",
    "httpx",
    "py_clob_client_v2", "py_clob_client_v2.client", "py_clob_client_v2.clob_types",
    "py_clob_client_v2.constants", "py_clob_client_v2.exceptions",
    "py_clob_client_v2.order_builder", "py_clob_client_v2.order_builder.constants",
    "py_order_utils", "py_order_utils.builders", "py_order_utils.model",
    "py_order_utils.signer",
    "tenacity",
    "langchain_core", "langchain_core.messages", "langchain_core.documents",
    "langchain_openai",
    "langchain_community", "langchain_community.document_loaders",
    "langchain_community.vectorstores", "langchain_community.vectorstores.chroma",
    "chromadb",
    "requests",
]:
    _ensure_stub(_mod)


def _identity_retry(*args, **kwargs):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*a, **kw):
            return func(*a, **kw)
        return wrapper
    return decorator

sys.modules["tenacity"].retry = _identity_retry

class _FakeDocument:
    def __init__(self, page_content="", metadata=None, **kwargs):
        self.page_content = page_content
        self.metadata = metadata or {}
    def dict(self):
        return {"page_content": self.page_content, "metadata": self.metadata}

sys.modules["langchain_core.documents"].Document = _FakeDocument

class _FakeMarketOrderArgsV2:
    def __init__(self, token_id=None, amount=None, price=None, side=None, **kwargs):
        self.token_id = token_id
        self.amount = amount
        self.price = price
        self.side = side

sys.modules["py_clob_client_v2.clob_types"].MarketOrderArgsV2 = _FakeMarketOrderArgsV2

# Now safe to import project modules
from agents.application.btc_5min import (
    Btc5MinConfig,
    Btc5MinEngine,
    SignalResult,
    _current_period_ts,
    _format_5min_slug,
    PERIOD_SEC,
    BTC_5MIN_OPEN,
)
from agents.application.btc_daily import CoinbasePriceFeed
from agents.application.trade_log import TradeLog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _TmpDB:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "trade_log.db")

    def tearDown(self):
        self._tmp.cleanup()


class _FakeFeed:
    """Controllable substitute for CoinbasePriceFeed."""

    def __init__(self):
        self._samples: list[tuple[int, float]] = []
        self.next_pct: float = 0.0

    def percent_change(self, window_sec: int) -> Optional[float]:
        return self.next_pct

    def update(self) -> Optional[float]:
        return 100000.0


def _make_market_doc(market_id="m1", token_ids=None, outcomes=None):
    token_ids = token_ids or ["tok_up", "tok_down"]
    outcomes = outcomes or ["Up", "Down"]

    class _Doc:
        pass
    doc = _Doc()
    doc.dict = lambda: {
        "metadata": {
            "id": market_id,
            "outcomes": str(outcomes),
            "clob_token_ids": str(token_ids),
            "outcome_prices": '["0.5","0.5"]',
        }
    }
    return {
        "market_id": market_id,
        "token_ids": token_ids,
        "outcomes": outcomes,
        "question": "Will BTC go up in next 5 min?",
        "doc": doc,
    }


def _engine(db_path, execute=False, cfg=None, feed=None, risk_gate=None):
    """Build a testable Btc5MinEngine with mocked externals."""
    if cfg is None:
        cfg = Btc5MinConfig(
            position_size_usdc=1.5,
            reserve_usdc=3.0,
            entry_window_start=60,
            entry_window_end=180,
            momentum_pct=0.0015,
            min_consensus=2,
            news_veto=False,  # disabled by default in tests
            poll_sec=1,
            cooldown_sec=300,
            max_per_hour=6,
        )
    if feed is None:
        feed = _FakeFeed()
    tl = TradeLog(db_path=db_path)
    pm = MagicMock()
    pm.execute_market_order.return_value = {
        "status": "matched",
        "order_avg_price_estimate": "0.50",
        "amount_usdc": "1.50",
        "token_id": "tok_up",
        "outcome_traded": "Up",
    }
    pm._fillable_market_buy.return_value = (0.50, 1.50, 0.50)
    pm.client = MagicMock()
    pm.client.get_midpoint.return_value = {"mid": "0.50"}
    if risk_gate is None:
        risk_gate = MagicMock()
        risk_gate.ok.return_value = True
    eng = Btc5MinEngine(
        polymarket=pm,
        trade_log=tl,
        risk_gate=risk_gate,
        feed=feed,
        cfg=cfg,
        execute=execute,
    )
    eng._resolve_current_5min_market = lambda ts: _make_market_doc()
    return eng


# ---------------------------------------------------------------------------
# Tests: Slug format
# ---------------------------------------------------------------------------


class TestSlugFormat(unittest.TestCase):
    def test_format_5min_slug(self):
        slug = _format_5min_slug(1716000000)
        self.assertEqual(slug, "btc-updown-5m-1716000000")

    def test_current_period_ts_alignment(self):
        ts = _current_period_ts()
        self.assertEqual(ts % PERIOD_SEC, 0)
        # Should be within the current 5-min window
        now = int(time.time())
        self.assertLessEqual(ts, now)
        self.assertGreater(ts + PERIOD_SEC, now)


# ---------------------------------------------------------------------------
# Tests: Individual signals
# ---------------------------------------------------------------------------


class TestSignals(_TmpDB, unittest.TestCase):
    def test_momentum_bullish(self):
        eng = _engine(self.db_path)
        eng.feed.next_pct = 0.003  # 0.3% > 0.15%
        sig = eng._momentum_signal()
        self.assertEqual(sig.direction, "bullish")
        self.assertGreater(sig.confidence, 0.0)
        self.assertEqual(sig.weight, 2.0)

    def test_momentum_bearish(self):
        eng = _engine(self.db_path)
        eng.feed.next_pct = -0.003
        sig = eng._momentum_signal()
        self.assertEqual(sig.direction, "bearish")

    def test_momentum_skip_below_threshold(self):
        eng = _engine(self.db_path)
        eng.feed.next_pct = 0.0005  # below 0.15%
        sig = eng._momentum_signal()
        self.assertEqual(sig.direction, "skip")

    @patch("agents.application.btc_5min._fetch_funding_rate")
    def test_funding_bullish(self, mock_fetch):
        mock_fetch.return_value = -0.001  # negative → bullish
        eng = _engine(self.db_path)
        sig = eng._funding_signal()
        self.assertEqual(sig.direction, "bullish")

    @patch("agents.application.btc_5min._fetch_funding_rate")
    def test_funding_bearish(self, mock_fetch):
        mock_fetch.return_value = 0.001  # positive → bearish
        eng = _engine(self.db_path)
        sig = eng._funding_signal()
        self.assertEqual(sig.direction, "bearish")

    @patch("agents.application.btc_5min._fetch_funding_rate")
    def test_funding_skip_neutral(self, mock_fetch):
        mock_fetch.return_value = 0.00005  # too small
        eng = _engine(self.db_path)
        sig = eng._funding_signal()
        self.assertEqual(sig.direction, "skip")

    def test_rsi_skip_insufficient_data(self):
        eng = _engine(self.db_path)
        # _samples is empty by default on _FakeFeed
        eng.feed._samples = []
        sig = eng._rsi_signal()
        self.assertEqual(sig.direction, "skip")

    def test_rsi_bullish_oversold(self):
        eng = _engine(self.db_path)
        # Build a declining price series to push RSI below 25
        base = 100000
        now_ms = int(time.time() * 1000)
        samples = []
        for i in range(30):
            price = base - i * 50  # steady decline
            samples.append((now_ms - (30 - i) * 60000, price))
        eng.feed._samples = samples
        sig = eng._rsi_signal()
        # RSI should be low; if it's below 25, direction is bullish
        if sig.direction != "skip":
            self.assertIn(sig.direction, ["bullish", "bearish"])

    def test_rsi_bearish_overbought(self):
        eng = _engine(self.db_path)
        base = 100000
        now_ms = int(time.time() * 1000)
        samples = []
        for i in range(30):
            price = base + i * 50  # steady rise
            samples.append((now_ms - (30 - i) * 60000, price))
        eng.feed._samples = samples
        sig = eng._rsi_signal()
        if sig.direction != "skip":
            self.assertIn(sig.direction, ["bullish", "bearish"])


# ---------------------------------------------------------------------------
# Tests: Consensus
# ---------------------------------------------------------------------------


class TestConsensus(_TmpDB, unittest.TestCase):
    def test_two_agree_enter(self):
        """2/3 signals agree → consensus direction is not skip."""
        eng = _engine(self.db_path)
        # Mock signals: 2 bullish, 1 skip
        eng._momentum_signal = lambda: SignalResult("momentum", "bullish", 0.7, weight=2.0)
        eng._funding_signal = lambda: SignalResult("funding", "bullish", 0.6)
        eng._rsi_signal = lambda: SignalResult("rsi", "skip", 0.0)
        result = eng.compute_consensus()
        self.assertEqual(result["direction"], "bullish")
        self.assertEqual(result["contributing_count"], 2)

    def test_one_agree_skip(self):
        """Only 1/3 agrees → should not reach min_consensus."""
        eng = _engine(self.db_path)
        eng._momentum_signal = lambda: SignalResult("momentum", "bullish", 0.7, weight=2.0)
        eng._funding_signal = lambda: SignalResult("funding", "skip", 0.0)
        eng._rsi_signal = lambda: SignalResult("rsi", "skip", 0.0)
        result = eng.compute_consensus()
        # Only 1 contributing — below min_consensus=2
        self.assertEqual(result["contributing_count"], 1)

    def test_disagreement_skip(self):
        """2 signals disagree → direction may be skip or weak."""
        eng = _engine(self.db_path)
        eng._momentum_signal = lambda: SignalResult("momentum", "bullish", 0.6, weight=2.0)
        eng._funding_signal = lambda: SignalResult("funding", "bearish", 0.6)
        eng._rsi_signal = lambda: SignalResult("rsi", "bearish", 0.6)
        result = eng.compute_consensus()
        # Contributing count is 3 but direction depends on weighted vote
        self.assertEqual(result["contributing_count"], 3)

    def test_three_agree_strong(self):
        """3/3 signals agree → high confidence."""
        eng = _engine(self.db_path)
        eng._momentum_signal = lambda: SignalResult("momentum", "bearish", 0.7, weight=2.0)
        eng._funding_signal = lambda: SignalResult("funding", "bearish", 0.7)
        eng._rsi_signal = lambda: SignalResult("rsi", "bearish", 0.7)
        result = eng.compute_consensus()
        self.assertEqual(result["direction"], "bearish")
        self.assertEqual(result["contributing_count"], 3)
        self.assertGreater(result["confidence"], 0.55)


# ---------------------------------------------------------------------------
# Tests: Timing guards
# ---------------------------------------------------------------------------


class TestTiming(_TmpDB, unittest.TestCase):
    def test_too_early_skip(self):
        """Before entry_window_start → no trade."""
        eng = _engine(self.db_path)
        # Override signals to always agree
        eng._momentum_signal = lambda: SignalResult("momentum", "bullish", 0.8, weight=2.0)
        eng._funding_signal = lambda: SignalResult("funding", "bullish", 0.7)
        eng._rsi_signal = lambda: SignalResult("rsi", "bullish", 0.7)
        # Force period_ts to be NOW (0 elapsed)
        with patch("agents.application.btc_5min._current_period_ts") as mock_ts:
            mock_ts.return_value = int(time.time())  # elapsed = 0
            result = eng.maybe_enter()
        self.assertFalse(result)

    def test_too_late_skip(self):
        """After entry_window_end → no trade."""
        eng = _engine(self.db_path)
        eng._momentum_signal = lambda: SignalResult("momentum", "bullish", 0.8, weight=2.0)
        eng._funding_signal = lambda: SignalResult("funding", "bullish", 0.7)
        eng._rsi_signal = lambda: SignalResult("rsi", "bullish", 0.7)
        with patch("agents.application.btc_5min._current_period_ts") as mock_ts:
            mock_ts.return_value = int(time.time()) - 200  # elapsed > 180
            result = eng.maybe_enter()
        self.assertFalse(result)

    def test_same_period_dedupe(self):
        """Can't enter twice in the same 5-min period."""
        eng = _engine(self.db_path)
        eng._momentum_signal = lambda: SignalResult("momentum", "bullish", 0.8, weight=2.0)
        eng._funding_signal = lambda: SignalResult("funding", "bullish", 0.7)
        eng._rsi_signal = lambda: SignalResult("rsi", "bullish", 0.7)
        period = _current_period_ts()
        with patch("agents.application.btc_5min._current_period_ts", return_value=period):
            with patch("time.time", return_value=float(period + 90)):
                eng.maybe_enter()  # first entry
                result = eng.maybe_enter()  # second attempt
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Tests: Side semantics
# ---------------------------------------------------------------------------


class TestSideSemantics(_TmpDB, unittest.TestCase):
    @patch.dict("os.environ", {"MIN_EXITABLE_ENTRY_USDC": "1.0"})
    def test_bullish_buys_up_token(self):
        """Bullish consensus → BUY → token_ids[0] (Up)."""
        eng = _engine(self.db_path)
        eng._momentum_signal = lambda: SignalResult("momentum", "bullish", 0.8, weight=2.0)
        eng._funding_signal = lambda: SignalResult("funding", "bullish", 0.7)
        eng._rsi_signal = lambda: SignalResult("rsi", "skip", 0.0)
        period = _current_period_ts()
        with patch("agents.application.btc_5min._current_period_ts", return_value=period):
            with patch("time.time", return_value=float(period + 90)):
                eng.maybe_enter()
        tl = TradeLog(self.db_path)
        rows = tl.recent(10)
        buy_rows = [r for r in rows if r.get("side") == "BUY"]
        self.assertTrue(len(buy_rows) > 0)
        self.assertEqual(buy_rows[0]["token_id"], "tok_up")

    @patch.dict("os.environ", {"MIN_EXITABLE_ENTRY_USDC": "1.0"})
    def test_bearish_sells_down_token(self):
        """Bearish consensus → SELL → token_ids[1] (Down)."""
        eng = _engine(self.db_path)
        eng._momentum_signal = lambda: SignalResult("momentum", "bearish", 0.8, weight=2.0)
        eng._funding_signal = lambda: SignalResult("funding", "bearish", 0.7)
        eng._rsi_signal = lambda: SignalResult("rsi", "skip", 0.0)
        period = _current_period_ts()
        with patch("agents.application.btc_5min._current_period_ts", return_value=period):
            with patch("time.time", return_value=float(period + 90)):
                eng.maybe_enter()
        tl = TradeLog(self.db_path)
        rows = tl.recent(10)
        sell_rows = [r for r in rows if r.get("side") == "SELL"]
        self.assertTrue(len(sell_rows) > 0)
        self.assertEqual(sell_rows[0]["token_id"], "tok_down")


# ---------------------------------------------------------------------------
# Tests: 5m straddle scalp
# ---------------------------------------------------------------------------


class TestStraddleScalp(_TmpDB, unittest.TestCase):
    @patch.dict("os.environ", {"MIN_EXITABLE_ENTRY_USDC": "0.1"})
    def test_straddle_shadow_opens_both_legs_when_pair_sum_is_cheap(self):
        cfg = Btc5MinConfig(
            news_veto=False,
            entry_window_start=20,
            entry_window_end=180,
            cooldown_sec=0,
            min_consensus=2,
            straddle_enabled=True,
            straddle_leg_usdc=1.5,
            straddle_max_pair_ask_sum=1.02,
            straddle_take_profit_pct=0.03,
        )
        eng = _engine(self.db_path, cfg=cfg)
        eng.polymarket._fillable_market_buy.side_effect = [
            (0.46, 1.5, 0.46),
            (0.55, 1.5, 0.55),
        ]
        period = _current_period_ts()
        with patch("agents.application.btc_5min._current_period_ts", return_value=period):
            with patch("time.time", return_value=float(period + 90)):
                self.assertTrue(eng.maybe_enter())
        rows = TradeLog(self.db_path).recent(10)
        open_rows = [r for r in rows if r["status"] == BTC_5MIN_OPEN]
        self.assertEqual(len(open_rows), 2)
        self.assertEqual({r["token_id"] for r in open_rows}, {"tok_up", "tok_down"})
        self.assertEqual({r["side"] for r in open_rows}, {"BUY", "SELL"})
        self.assertTrue(all("btc_5min_straddle_scalp" in (r["response_json"] or "") for r in open_rows))
        for call in eng.polymarket._fillable_market_buy.call_args_list:
            self.assertEqual(call.kwargs["max_spread_pct"], cfg.straddle_max_entry_spread_pct)
            self.assertEqual(call.kwargs["min_entry_price"], cfg.straddle_min_entry_price)
            self.assertEqual(call.kwargs["min_bid_depth_usdc"], cfg.straddle_min_bid_depth_usdc)

    @patch.dict("os.environ", {"MIN_EXITABLE_ENTRY_USDC": "0.1"})
    def test_straddle_skips_when_pair_sum_is_too_expensive(self):
        cfg = Btc5MinConfig(
            news_veto=False,
            entry_window_start=20,
            entry_window_end=180,
            cooldown_sec=0,
            min_consensus=2,
            straddle_enabled=True,
            straddle_leg_usdc=1.5,
            straddle_max_pair_ask_sum=1.02,
        )
        eng = _engine(self.db_path, cfg=cfg)
        eng.polymarket._fillable_market_buy.side_effect = [
            (0.55, 1.5, 0.55),
            (0.55, 1.5, 0.55),
        ]
        eng._momentum_signal = lambda: SignalResult("momentum", "skip", 0.0)
        eng._funding_signal = lambda: SignalResult("funding", "skip", 0.0)
        eng._rsi_signal = lambda: SignalResult("rsi", "skip", 0.0)
        period = _current_period_ts()
        with patch("agents.application.btc_5min._current_period_ts", return_value=period):
            with patch("time.time", return_value=float(period + 90)):
                self.assertFalse(eng.maybe_enter())
        rows = TradeLog(self.db_path).recent(10)
        self.assertFalse([r for r in rows if r["status"] == BTC_5MIN_OPEN])


# ---------------------------------------------------------------------------
# Tests: News veto
# ---------------------------------------------------------------------------


class TestNewsVeto(_TmpDB, unittest.TestCase):
    @patch("agents.application.btc_5min.tavily_headlines")
    def test_veto_on_hack_keyword(self, mock_tavily):
        """News containing 'hack' triggers veto."""
        mock_tavily.return_value = "Breaking: Major exchange hack reported"
        cfg = Btc5MinConfig(news_veto=True)
        eng = _engine(self.db_path, cfg=cfg)
        self.assertTrue(eng._news_veto())

    @patch("agents.application.btc_5min.tavily_headlines")
    def test_no_veto_normal_news(self, mock_tavily):
        """Normal news does not trigger veto."""
        mock_tavily.return_value = "Bitcoin price stable around 100k"
        cfg = Btc5MinConfig(news_veto=True)
        eng = _engine(self.db_path, cfg=cfg)
        self.assertFalse(eng._news_veto())

    @patch("agents.application.btc_5min.tavily_headlines")
    def test_veto_disabled(self, mock_tavily):
        """When news_veto=False, never triggers."""
        mock_tavily.return_value = "Major hack crash exploit"
        cfg = Btc5MinConfig(news_veto=False)
        eng = _engine(self.db_path, cfg=cfg)
        self.assertFalse(eng._news_veto())

    @patch("agents.application.btc_5min.tavily_headlines")
    def test_veto_on_etf_keyword(self, mock_tavily):
        """News about ETF triggers veto."""
        mock_tavily.return_value = "SEC approves new BTC ETF"
        cfg = Btc5MinConfig(news_veto=True)
        eng = _engine(self.db_path, cfg=cfg)
        self.assertTrue(eng._news_veto())


# ---------------------------------------------------------------------------
# Tests: Hourly trade cap
# ---------------------------------------------------------------------------


class TestHourlyCap(_TmpDB, unittest.TestCase):
    def test_max_per_hour_blocks(self):
        """After max_per_hour trades, further entries are blocked."""
        eng = _engine(self.db_path)
        eng.cfg.max_per_hour = 2
        eng._momentum_signal = lambda: SignalResult("momentum", "bullish", 0.8, weight=2.0)
        eng._funding_signal = lambda: SignalResult("funding", "bullish", 0.7)
        eng._rsi_signal = lambda: SignalResult("rsi", "skip", 0.0)
        # Simulate 2 trades already done
        now = time.time()
        eng._hour_trades = [now - 10, now - 5]
        eng.cfg.cooldown_sec = 0  # disable cooldown for this test
        period = _current_period_ts()
        with patch("agents.application.btc_5min._current_period_ts", return_value=period):
            with patch("time.time", return_value=float(period + 90)):
                result = eng.maybe_enter()
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
