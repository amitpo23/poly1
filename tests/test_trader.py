import functools
import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy optional deps so tests run without the full Docker pip install.
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
]:
    _ensure_stub(_mod)

# tenacity.retry must be an identity decorator factory so that methods decorated
# with @retry(...) still call through to the real function.
def _identity_retry(*args, **kwargs):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*a, **kw):
            return func(*a, **kw)
        return wrapper
    return decorator

sys.modules["tenacity"].retry = _identity_retry

# langchain_core.documents.Document needs a real .dict() method.
class _FakeDocument:
    def __init__(self, page_content="", metadata=None, **kwargs):
        self.page_content = page_content
        self.metadata = metadata or {}
    def dict(self):
        return {"page_content": self.page_content, "metadata": self.metadata}

sys.modules["langchain_core.documents"].Document = _FakeDocument

# py_clob_client_v2.clob_types.MarketOrderArgsV2 is imported as MarketOrderArgs in
# polymarket.py. execute_market_order builds an instance and passes it as a positional
# arg to create_and_post_market_order — tests inspect .price, .amount, .token_id.
class _FakeMarketOrderArgsV2:
    def __init__(self, token_id=None, amount=None, price=None, side=None, **kwargs):
        self.token_id = token_id
        self.amount = amount
        self.price = price
        self.side = side

sys.modules["py_clob_client_v2.clob_types"].MarketOrderArgsV2 = _FakeMarketOrderArgsV2
# ---------------------------------------------------------------------------

from agents.application.risk_gate import RiskGate
from agents.application.trade_log import (
    FAILED,
    FILLED,
    PENDING,
    SKIPPED_DEDUPE,
    SKIPPED_DRY_RUN,
    SKIPPED_GATE,
    SUBMITTED,
    TradeLog,
)
from agents.utils.objects import TradeRecommendation


class TempDataMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.db_path = str(self.tmp_path / "trade_log.db")
        self.kill_path = str(self.tmp_path / "HALT")
        self.usage_path = str(self.tmp_path / "llm_usage.jsonl")
        self._saved_execute_env = {
            key: os.environ.get(key)
            for key in (
                "EXECUTE",
                "EXECUTE_SCALPER",
                "EXECUTE_BTC_DAILY",
                "EXECUTE_BTC_5MIN",
                "EXECUTE_NEAR_RESOLUTION",
                "EXECUTE_NEWS_SHOCK",
                "EXECUTE_WALLET_FOLLOW",
                "EXECUTE_EXTERNAL_CONVICTION",
                "EXECUTE_SCANNER_EXECUTOR",
                "EXECUTE_MAINTAIN",
                "MAINTAIN_HEARTBEAT_PATH",
                "POSITION_MANAGER_HEARTBEAT_PATH",
                "POSITION_MANAGER_ENTRY_MAX_HEARTBEAT_AGE_SEC",
            )
        }
        for key in self._saved_execute_env:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved_execute_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()


class TestTradeLog(TempDataMixin, unittest.TestCase):
    def test_idempotency_dedupes_recent_market(self):
        tl = TradeLog(self.db_path)
        cycle = tl.new_cycle_id()
        tl.insert_pending(
            cycle_id=cycle, market_id="42", token_id="t0",
            side="BUY", price=0.5, size_usdc=2.0, confidence=0.7,
        )
        self.assertTrue(tl.has_active_trade_for_market("42", hours=6))

        tl2 = TradeLog(self.db_path)
        self.assertTrue(tl2.has_active_trade_for_market("42", hours=6))

    def test_init_does_not_recover_stranded_pendings_by_default(self):
        from agents.application.trade_log import PENDING

        tl = TradeLog(self.db_path)
        cycle = tl.new_cycle_id()
        tl.insert_pending(
            cycle_id=cycle, market_id="init-no-recover", token_id="tok",
            side="BUY", price=0.5, size_usdc=2.0, confidence=0.7,
        )

        TradeLog(self.db_path)
        rows = tl.recent(limit=1)
        self.assertEqual(rows[0]["status"], PENDING)

    def test_pending_marked_may_have_fired_on_recovery(self):
        from agents.application.trade_log import MAY_HAVE_FIRED

        tl = TradeLog(self.db_path)
        cycle = tl.new_cycle_id()
        tl.insert_pending(
            cycle_id=cycle, market_id="9", token_id="t1",
            side="SELL", price=0.5, size_usdc=1.0, confidence=0.9,
        )
        # recover with very small "older than" so it sweeps the just-inserted row
        recovered = tl.recover_stranded_pendings(older_than_minutes=-1)
        self.assertGreaterEqual(recovered, 1)
        # Stranded rows must keep blocking the same market to avoid double-fill.
        self.assertTrue(tl.has_active_trade_for_market("9", hours=24))
        rows = tl.recent(limit=5)
        self.assertEqual(rows[0]["status"], MAY_HAVE_FIRED)

    def test_may_have_fired_blocks_beyond_dedupe_window(self):
        """MAY_HAVE_FIRED must block re-trading regardless of age — operator
        verifies on-chain and clears the row manually. A time-bounded check
        would re-open a double-fill window after the dedupe window expires."""
        import sqlite3
        from datetime import datetime, timedelta, timezone
        from agents.application.trade_log import MAY_HAVE_FIRED

        tl = TradeLog(self.db_path)
        # Backdate a MAY_HAVE_FIRED row to 7 days ago — well past any reasonable
        # dedupe window. It must still block re-trading.
        ancient = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO trades (ts, cycle_id, market_id, status, error) "
                "VALUES (?, ?, ?, ?, ?)",
                (ancient, "old-cycle", "777", MAY_HAVE_FIRED, "ancient stranded"),
            )
            conn.commit()
        self.assertTrue(tl.has_active_trade_for_market("777", hours=6))
        self.assertTrue(tl.has_active_trade_for_market("777", hours=1))

    def test_closed_filled_row_is_not_active_trade(self):
        tl = TradeLog(self.db_path)
        trade_id = tl.insert_pending(
            cycle_id="c-closed",
            market_id="42",
            token_id="tok-closed",
            side="BUY",
            price=0.50,
            size_usdc=1.0,
            confidence=0.8,
        )
        tl.mark(trade_id, "filled")
        tl.insert_terminal(
            cycle_id="c-close",
            market_id="42",
            token_id="tok-closed",
            side="BUY",
            price=0.49,
            size_usdc=0.98,
            confidence=0.8,
            status="closed_stop_loss",
        )

        self.assertFalse(
            tl.has_active_trade_for_market("42", hours=6, token_id="tok-closed")
        )
        self.assertTrue(
            tl.has_recent_close_for_market("42", hours=12, token_id="tok-closed")
        )

    def test_unclosed_filled_row_blocks_beyond_dedupe_window(self):
        import sqlite3
        from datetime import datetime, timedelta, timezone

        tl = TradeLog(self.db_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO trades (ts, cycle_id, market_id, token_id, side, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (old_ts, "old-fill", "42", "tok-old", "BUY", "filled"),
            )
            conn.commit()

        self.assertTrue(
            tl.has_active_trade_for_market("42", hours=1, token_id="tok-old")
        )

    def test_scalper_pairs_table_exists(self):
        log = TradeLog(db_path=self.db_path)
        with log._connect() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='scalper_pairs'"
            ).fetchone()
        self.assertIsNotNone(row, "scalper_pairs table must be created on init")

    def test_wal_mode_enabled(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        try:
            log = TradeLog(db_path=path)
            with log._connect() as conn:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
        finally:
            os.unlink(path)

    def test_scalper_leg_status_constant(self):
        from agents.application.trade_log import SCALPER_LEG, ACTIVE_STATUSES
        self.assertEqual(SCALPER_LEG, "scalper_leg")
        # Must NOT be in ACTIVE_STATUSES — scalper has its own dedupe
        self.assertNotIn(SCALPER_LEG, ACTIVE_STATUSES)

    def test_counts_recent_hard_failures_for_market(self):
        log = TradeLog(db_path=self.db_path)
        cycle = log.new_cycle_id()
        log.insert_terminal(
            cycle_id=cycle,
            market_id="bad-market",
            status=FAILED,
            error="execute_market_order raised: PolyApiException[status_code=404]",
        )
        log.insert_terminal(
            cycle_id=cycle,
            market_id="bad-market",
            status=FAILED,
            error="execute_market_order raised: live ask price 0.7400 exceeds recommended price",
        )
        self.assertEqual(
            log.count_recent_failures_for_market(
                "bad-market",
                error_like=["%status_code=404%", "%live ask price%"],
            ),
            2,
        )

    def test_position_marks_track_mfe_mae_across_updates(self):
        log = TradeLog(db_path=self.db_path)
        first = log.upsert_position_mark(
            token_id="tok",
            market_id="m1",
            entry_price=0.50,
            current_price=0.55,
            shares=10,
        )
        second = log.upsert_position_mark(
            token_id="tok",
            market_id="m1",
            entry_price=0.50,
            current_price=0.52,
            shares=10,
        )
        self.assertAlmostEqual(first["mfe_pct"], 0.10)
        self.assertAlmostEqual(second["max_price"], 0.55)
        self.assertGreater(second["peak_drawdown_pct"], 0)

    def test_market_quarantine_blocks_recent_bad_market(self):
        log = TradeLog(db_path=self.db_path)
        self.assertFalse(log.is_market_quarantined("bad"))
        log.quarantine_market("bad", "404")
        self.assertTrue(log.is_market_quarantined("bad"))

    def test_agent_promotion_ledger_upsert(self):
        log = TradeLog(db_path=self.db_path)
        log.upsert_agent_promotion(
            agent="scalper",
            state="paper",
            reason="negative_live_probe",
            score=0.1,
            sample_size=5,
        )
        with log._connect() as conn:
            row = conn.execute(
                "SELECT state, sample_size FROM agent_promotion_ledger WHERE agent='scalper'"
            ).fetchone()
        self.assertEqual(row["state"], "paper")
        self.assertEqual(row["sample_size"], 5)

    def test_open_positions_start_after_latest_terminal_row(self):
        log = TradeLog(db_path=self.db_path)
        log.insert_terminal(
            cycle_id="old-open",
            market_id="M1",
            token_id="TOK",
            side="BUY",
            price=0.50,
            size_usdc=5.0,
            confidence=0.8,
            status=FILLED,
        )
        log.insert_terminal(
            cycle_id="old-close",
            market_id="M1",
            token_id="TOK",
            side="SELL",
            price=0.50,
            size_usdc=5.0,
            status="closed_take_profit",
        )
        new_id = log.insert_terminal(
            cycle_id="new-open",
            market_id="M1",
            token_id="TOK",
            side="BUY",
            price=0.25,
            size_usdc=3.0,
            confidence=0.8,
            status=FILLED,
        )

        open_with_id = log.filled_positions_with_id()
        self.assertEqual([r["id"] for r in open_with_id], [new_id])
        self.assertEqual(len(log.filled_positions()), 1)
        self.assertAlmostEqual(log.filled_positions()[0]["size_usdc"], 3.0)

    def test_close_attempt_idempotency_can_be_scoped_after_entry(self):
        log = TradeLog(db_path=self.db_path)
        old_id = log.insert_terminal(
            cycle_id="old-open",
            market_id="M1",
            token_id="TOK",
            side="BUY",
            price=0.50,
            size_usdc=5.0,
            status=FILLED,
        )
        log.insert_terminal(
            cycle_id="old-close",
            market_id="M1",
            token_id="TOK",
            side="SELL",
            price=0.51,
            size_usdc=5.1,
            status="closed_take_profit",
        )
        new_id = log.insert_terminal(
            cycle_id="new-open",
            market_id="M1",
            token_id="TOK",
            side="BUY",
            price=0.40,
            size_usdc=4.0,
            status=FILLED,
        )

        self.assertTrue(log.has_close_attempt_for_token("TOK", after_id=old_id))
        self.assertFalse(log.has_close_attempt_for_token("TOK", after_id=new_id))


class TestEntryGuards(unittest.TestCase):
    """Fixes 1-3: _fillable_market_buy rejects penny tokens, thin bids, wide spreads."""

    def setUp(self):
        # Import Polymarket with stubs already in place from module top.
        from agents.polymarket.polymarket import Polymarket
        self.pm = Polymarket(live=False)

    def _make_book(self, asks, bids):
        """Return a dict matching what get_order_book returns."""
        return {
            "asks": [{"price": p, "size": s} for p, s in asks],
            "bids": [{"price": p, "size": s} for p, s in bids],
            "tick_size": "0.01",
        }

    @patch("agents.polymarket.polymarket.MIN_ENTRY_PRICE", 0.10)
    def test_rejects_penny_token(self):
        book = self._make_book(
            asks=[(0.05, 1000)],
            bids=[(0.04, 1000)],
        )
        self.pm.client = MagicMock()
        self.pm.client.get_order_book.return_value = book
        with self.assertRaises(ValueError) as ctx:
            self.pm._fillable_market_buy("tok1", 5.0)
        self.assertIn("below MIN_ENTRY_PRICE", str(ctx.exception))

    @patch("agents.polymarket.polymarket.MIN_ENTRY_PRICE", 0.01)
    @patch("agents.polymarket.polymarket.MIN_BID_DEPTH_USDC", 20.0)
    def test_rejects_thin_bid_depth(self):
        book = self._make_book(
            asks=[(0.50, 100)],
            bids=[(0.48, 5)],  # 5 * 0.48 = $2.40 < $20
        )
        self.pm.client = MagicMock()
        self.pm.client.get_order_book.return_value = book
        with self.assertRaises(ValueError) as ctx:
            self.pm._fillable_market_buy("tok2", 5.0)
        self.assertIn("insufficient bid depth", str(ctx.exception))

    @patch("agents.polymarket.polymarket.MIN_ENTRY_PRICE", 0.01)
    @patch("agents.polymarket.polymarket.MIN_BID_DEPTH_USDC", 0.0)
    @patch("agents.polymarket.polymarket.MAX_ENTRY_SPREAD_PCT", 0.05)
    def test_rejects_wide_spread(self):
        # Spread = (0.60 - 0.40) / 0.60 = 33% > 5%
        book = self._make_book(
            asks=[(0.60, 100)],
            bids=[(0.40, 500)],
        )
        self.pm.client = MagicMock()
        self.pm.client.get_order_book.return_value = book
        with self.assertRaises(ValueError) as ctx:
            self.pm._fillable_market_buy("tok3", 5.0)
        self.assertIn("spread too wide", str(ctx.exception))

    @patch("agents.polymarket.polymarket.MIN_ENTRY_PRICE", 0.01)
    @patch("agents.polymarket.polymarket.MIN_BID_DEPTH_USDC", 0.0)
    @patch("agents.polymarket.polymarket.MAX_ENTRY_SPREAD_PCT", 1.0)
    def test_passes_good_book(self):
        book = self._make_book(
            asks=[(0.50, 100)],
            bids=[(0.49, 500)],
        )
        self.pm.client = MagicMock()
        self.pm.client.get_order_book.return_value = book
        limit_price, fillable, avg = self.pm._fillable_market_buy("tok4", 5.0)
        self.assertGreater(fillable, 0)
        self.assertGreater(limit_price, 0)


class TestTokenIdDedupe(TempDataMixin, unittest.TestCase):
    """Fix 5: cross-agent dedupe matches on token_id."""

    def test_filled_position_found_by_token_id(self):
        log = TradeLog(self.db_path)
        # External conviction writes a fill with hex market_id but correct token_id
        log.insert_terminal(
            cycle_id="c1",
            market_id="0x7976abcdef",
            token_id="SHARED_TOKEN",
            side="BUY",
            price=0.50,
            size_usdc=3.0,
            status=FILLED,
        )
        # Trader queries with numeric market_id but same token_id → should find it
        self.assertTrue(
            log.has_filled_position_for_market("566187", token_id="SHARED_TOKEN")
        )
        # Without token_id, numeric market_id alone should NOT find the hex row
        self.assertFalse(
            log.has_filled_position_for_market("566187")
        )

    def test_active_trade_found_by_token_id(self):
        log = TradeLog(self.db_path)
        log.insert_pending(
            cycle_id="c2",
            market_id="0xabc123",
            token_id="SHARED_TOKEN_2",
            side="BUY",
            price=0.40,
            size_usdc=2.0,
            confidence=0.8,
        )
        self.assertTrue(
            log.has_active_trade_for_market("999999", hours=6, token_id="SHARED_TOKEN_2")
        )
        self.assertFalse(
            log.has_active_trade_for_market("999999", hours=6)
        )

    def test_backward_compatible_without_token_id(self):
        log = TradeLog(self.db_path)
        log.insert_pending(
            cycle_id="c3",
            market_id="42",
            token_id="tok",
            side="BUY",
            price=0.50,
            size_usdc=2.0,
            confidence=0.7,
        )
        # Old-style call without token_id still works
        self.assertTrue(log.has_active_trade_for_market("42", hours=6))


class TestRiskGate(TempDataMixin, unittest.TestCase):
    def _gate(self, **kwargs):
        tl = TradeLog(self.db_path)
        defaults = dict(
            trade_log=tl,
            polymarket=None,
            starting_balance_usdc=100.0,
            max_daily_loss_pct=0.10,
            max_trades_per_hour=4,
            min_usdc_floor=10.0,
            max_daily_token_usd=5.0,
            kill_switch_file=self.kill_path,
            llm_usage_file=self.usage_path,
        )
        defaults.update(kwargs)
        return RiskGate(**defaults)

    def test_kill_switch_file_blocks(self):
        Path(self.kill_path).write_text("halt")
        gate = self._gate()
        self.assertFalse(gate.ok())
        self.assertIn("kill switch", gate.reason())

    def test_balance_floor_blocks(self):
        pm = MagicMock()
        pm.get_usdc_balance.return_value = 5.0
        gate = self._gate(polymarket=pm)
        self.assertFalse(gate.ok())
        self.assertIn("below floor", gate.reason())

    def test_daily_loss_blocks(self):
        """Drawdown gate blocks when journal-based loss exceeds the daily
        loss limit. Cash level on-chain is intentionally NOT used: under
        the shared-wallet model another bot can spend pUSD without
        being a poly1 loss."""
        log = TradeLog(db_path=self.db_path)
        # Spent $30, positions are now worth $0 (resolved against us / mtm crashed).
        self._insert_filled(log, "M1", "TOK_X", "BUY", 0.50, 30.0)
        # No midpoints registered → mtm fallback to entry — but we want a
        # real loss, so register a midpoint of 0 explicitly.
        pm = MagicMock()
        pm.get_usdc_balance = MagicMock(return_value=80.0)
        client = MagicMock()
        client.get_midpoint = MagicMock(return_value={"mid": 0.0})
        pm.client = client
        gate = RiskGate(
            trade_log=log,
            polymarket=pm,
            starting_balance_usdc=100.0,
            max_daily_loss_pct=0.10,
            max_trades_per_hour=100,
            min_usdc_floor=10.0,
            kill_switch_file=self.kill_path,
            llm_usage_file=self.usage_path,
        )
        # portfolio = 100 - 30 + 0 = 70 → drawdown 30% > 10%
        self.assertFalse(gate.ok())
        self.assertIn("drawdown", gate.reason())

    def test_passes_when_clean(self):
        pm = MagicMock()
        pm.get_usdc_balance.return_value = 100.0
        gate = self._gate(polymarket=pm)
        self.assertTrue(gate.ok())

    def test_runtime_control_freeze_blocks(self):
        control_path = self.tmp_path / "runtime_control.json"
        control_path.write_text(json.dumps({
            "mode": "freeze",
            "allowed_live_agents": [],
            "config_hash": "freeze-hash",
        }))
        pm = MagicMock()
        pm.get_usdc_balance.return_value = 100.0
        gate = self._gate(polymarket=pm, runtime_control_file=str(control_path))
        self.assertFalse(gate.ok())
        self.assertIn("mode=freeze", gate.reason())

    def test_is_freeze_only_block_true_when_only_freeze(self):
        control_path = self.tmp_path / "runtime_control.json"
        control_path.write_text(json.dumps({
            "mode": "freeze",
            "allowed_live_agents": [],
            "config_hash": "freeze-hash",
        }))
        pm = MagicMock()
        pm.get_usdc_balance.return_value = 100.0  # healthy balance
        gate = self._gate(polymarket=pm, runtime_control_file=str(control_path))
        self.assertTrue(gate.is_freeze_only_block())

    def test_is_freeze_only_block_false_when_balance_also_blocks(self):
        """Freeze + balance below floor: must NOT route to shadow path."""
        control_path = self.tmp_path / "runtime_control.json"
        control_path.write_text(json.dumps({
            "mode": "freeze",
            "allowed_live_agents": [],
            "config_hash": "freeze-hash",
        }))
        pm = MagicMock()
        pm.get_usdc_balance.return_value = 5.0  # below min_usdc_floor=10.0
        gate = self._gate(polymarket=pm, runtime_control_file=str(control_path))
        self.assertFalse(gate.is_freeze_only_block())

    def test_is_freeze_only_block_false_when_emergency_kill_switch(self):
        """An operator/supervisor HALT (without the freeze marker) must
        veto even shadow logging — it's an emergency stop, not a mode."""
        control_path = self.tmp_path / "runtime_control.json"
        control_path.write_text(json.dumps({
            "mode": "freeze",
            "allowed_live_agents": [],
            "config_hash": "freeze-hash",
        }))
        Path(self.kill_path).write_text("operator emergency halt")
        pm = MagicMock()
        pm.get_usdc_balance.return_value = 100.0
        gate = self._gate(polymarket=pm, runtime_control_file=str(control_path))
        self.assertFalse(gate.is_freeze_only_block())

    def test_is_freeze_only_block_true_when_freeze_paired_halt(self):
        """The HALT file written by `runtime_control.py freeze` contains
        the FREEZE_HALT_MARKER, so it should NOT block shadow logging."""
        control_path = self.tmp_path / "runtime_control.json"
        control_path.write_text(json.dumps({
            "mode": "freeze",
            "allowed_live_agents": [],
            "config_hash": "freeze-hash",
        }))
        Path(self.kill_path).write_text(
            "HALT set by runtime_control.py freeze: no live entry agents..."
        )
        pm = MagicMock()
        pm.get_usdc_balance.return_value = 100.0
        gate = self._gate(polymarket=pm, runtime_control_file=str(control_path))
        self.assertTrue(gate.is_freeze_only_block())

    def test_is_freeze_only_block_false_when_no_runtime_file(self):
        pm = MagicMock()
        pm.get_usdc_balance.return_value = 100.0
        gate = self._gate(polymarket=pm)
        self.assertFalse(gate.is_freeze_only_block())

    def test_is_freeze_only_block_false_when_live_mode(self):
        control_path = self.tmp_path / "runtime_control.json"
        control_path.write_text(json.dumps({
            "mode": "live",
            "allowed_live_agents": ["scanner_executor"],
            "config_hash": "live-hash",
        }))
        pm = MagicMock()
        pm.get_usdc_balance.return_value = 100.0
        gate = self._gate(polymarket=pm, runtime_control_file=str(control_path))
        self.assertFalse(gate.is_freeze_only_block())

    def test_reason_skip_runtime_bypasses_freeze_check(self):
        control_path = self.tmp_path / "runtime_control.json"
        control_path.write_text(json.dumps({
            "mode": "freeze",
            "allowed_live_agents": [],
            "config_hash": "freeze-hash",
        }))
        pm = MagicMock()
        pm.get_usdc_balance.return_value = 100.0
        gate = self._gate(polymarket=pm, runtime_control_file=str(control_path))
        # Normal reason() returns the freeze block
        self.assertIn("mode=freeze", gate.reason())
        # skip_runtime=True bypasses runtime check; no other gate fires
        self.assertIsNone(gate.reason(skip_runtime=True))

    def test_runtime_control_hash_mismatch_blocks(self):
        control_path = self.tmp_path / "runtime_control.json"
        control_path.write_text(json.dumps({
            "mode": "live_probe",
            "allowed_live_agents": ["btc_daily"],
            "config_hash": "expected-hash",
        }))
        pm = MagicMock()
        pm.get_usdc_balance.return_value = 100.0
        with patch.dict(os.environ, {
            "RUNTIME_AGENT": "btc_daily",
            "RUNTIME_CONFIG_HASH": "stale-hash",
        }, clear=False):
            gate = self._gate(polymarket=pm, runtime_control_file=str(control_path))
            self.assertFalse(gate.ok())
            self.assertIn("hash mismatch", gate.reason())

    def test_runtime_control_allows_approved_agent_hash(self):
        control_path = self.tmp_path / "runtime_control.json"
        control_path.write_text(json.dumps({
            "mode": "live_probe",
            "allowed_live_agents": ["btc_daily"],
            "config_hash": "expected-hash",
        }))
        pm = MagicMock()
        pm.get_usdc_balance.return_value = 100.0
        with patch.dict(os.environ, {
            "RUNTIME_AGENT": "btc_daily",
            "RUNTIME_CONFIG_HASH": "expected-hash",
        }, clear=False):
            gate = self._gate(polymarket=pm, runtime_control_file=str(control_path))
            self.assertTrue(gate.ok(), msg=f"unexpected block: {gate.reason()}")

    def test_available_for_trader_subtracts_scalper_reserve(self):
        log = TradeLog(db_path=self.db_path)
        poly = MagicMock()
        poly.get_usdc_balance = MagicMock(return_value=80.0)
        gate = RiskGate(trade_log=log, polymarket=poly,
                         starting_balance_usdc=80.0,
                         scalper_reserve_usdc=20.0,
                         swarm_reserve_usdc=0.0,
                         btc_daily_reserve_usdc=0.0,
                         near_resolution_reserve_usdc=0.0,
                         news_shock_reserve_usdc=0.0,
                         wallet_follow_reserve_usdc=0.0)
        self.assertEqual(gate.available_for_trader(), 60.0)

    def test_agent_allocation_cap_blocks_above_half_wallet(self):
        log = TradeLog(db_path=self.db_path)
        poly = MagicMock()
        poly.get_usdc_balance = MagicMock(return_value=80.0)
        gate = RiskGate(
            trade_log=log,
            polymarket=poly,
            starting_balance_usdc=80.0,
            scalper_reserve_usdc=41.0,
            max_agent_allocation_fraction=0.50,
            min_usdc_floor=0.0,
            kill_switch_file=self.kill_path,
            llm_usage_file=self.usage_path,
        )
        self.assertFalse(gate.ok())
        self.assertIn("above 50%", gate.reason())

    def test_scalper_reserve_setter_updates_reserves_dict(self):
        log = TradeLog(db_path=self.db_path)
        gate = RiskGate(trade_log=log, polymarket=None,
                         starting_balance_usdc=80.0,
                         scalper_reserve_usdc=20.0)
        gate.scalper_reserve = 12.5
        self.assertEqual(gate.reserves["scalper"], 12.5)
        self.assertEqual(gate.scalper_reserve, 12.5)

    def test_available_for_trader_zero_reserve_default(self):
        log = TradeLog(db_path=self.db_path)
        poly = MagicMock()
        poly.get_usdc_balance = MagicMock(return_value=80.0)
        gate = RiskGate(trade_log=log, polymarket=poly,
                         starting_balance_usdc=80.0,
                         scalper_reserve_usdc=0.0,
                         swarm_reserve_usdc=0.0,
                         btc_daily_reserve_usdc=0.0,
                         near_resolution_reserve_usdc=0.0,
                         news_shock_reserve_usdc=0.0,
                         wallet_follow_reserve_usdc=0.0)  # ignore env
        self.assertEqual(gate.available_for_trader(), 80.0)

    def test_min_floor_uses_available_after_reserve(self):
        """If reserve makes available drop below min_usdc_floor, gate blocks."""
        log = TradeLog(db_path=self.db_path)
        poly = MagicMock()
        poly.get_usdc_balance = MagicMock(return_value=25.0)
        gate = RiskGate(trade_log=log, polymarket=poly,
                         starting_balance_usdc=80.0,
                         scalper_reserve_usdc=20.0,
                         min_usdc_floor=10.0)
        # available = 25 - 20 = 5 < 10 → block
        self.assertIsNotNone(gate.reason())

    def test_live_entry_requires_execute_maintain(self):
        log = TradeLog(db_path=self.db_path)
        poly = MagicMock()
        poly.get_usdc_balance = MagicMock(return_value=80.0)
        with patch.dict(os.environ, {
            "EXECUTE_BTC_DAILY": "true",
            "EXECUTE_MAINTAIN": "false",
        }, clear=False):
            gate = RiskGate(
                trade_log=log,
                polymarket=poly,
                starting_balance_usdc=80.0,
                min_usdc_floor=0.0,
                kill_switch_file=self.kill_path,
                llm_usage_file=self.usage_path,
            )
            self.assertIn("EXECUTE_MAINTAIN", gate.reason())

    def test_live_entry_requires_fresh_position_manager_heartbeat(self):
        log = TradeLog(db_path=self.db_path)
        poly = MagicMock()
        poly.get_usdc_balance = MagicMock(return_value=80.0)
        hb = self.tmp_path / "position_manager_heartbeat"
        with patch.dict(os.environ, {
            "EXECUTE_BTC_DAILY": "true",
            "EXECUTE_MAINTAIN": "true",
            "MAINTAIN_HEARTBEAT_PATH": str(hb),
            "POSITION_MANAGER_ENTRY_MAX_HEARTBEAT_AGE_SEC": "180",
        }, clear=False):
            gate = RiskGate(
                trade_log=log,
                polymarket=poly,
                starting_balance_usdc=80.0,
                min_usdc_floor=0.0,
                kill_switch_file=self.kill_path,
                llm_usage_file=self.usage_path,
            )
            self.assertIn("heartbeat missing", gate.reason())

            hb.touch()
            self.assertIsNone(gate.reason())

    def test_position_manager_guard_covers_all_entry_execute_flags(self):
        """C-4 defense-in-depth: every flag in ENTRY_EXECUTE_FLAGS must
        trigger the position_manager guard when set true without
        EXECUTE_MAINTAIN. If a new entry agent is added but its flag is
        forgotten from ENTRY_EXECUTE_FLAGS, this test will not catch it
        (that's a list-completeness invariant). What this DOES verify is
        that every flag CURRENTLY in the list correctly triggers the
        guard — so refactoring the guard can't accidentally skip one."""
        from agents.application.risk_gate import ENTRY_EXECUTE_FLAGS

        for flag in ENTRY_EXECUTE_FLAGS:
            with self.subTest(flag=flag):
                log = TradeLog(db_path=self.db_path)
                poly = MagicMock()
                poly.get_usdc_balance = MagicMock(return_value=80.0)
                # Clear all entry flags first, then set only the one under test
                clear_env = {f: "false" for f in ENTRY_EXECUTE_FLAGS}
                clear_env[flag] = "true"
                clear_env["EXECUTE_MAINTAIN"] = "false"
                with patch.dict(os.environ, clear_env, clear=False):
                    gate = RiskGate(
                        trade_log=log,
                        polymarket=poly,
                        starting_balance_usdc=80.0,
                        min_usdc_floor=0.0,
                        kill_switch_file=self.kill_path,
                        llm_usage_file=self.usage_path,
                    )
                    reason = gate.reason()
                    self.assertIsNotNone(
                        reason,
                        f"Guard should block when {flag}=true and EXECUTE_MAINTAIN=false",
                    )
                    self.assertIn("EXECUTE_MAINTAIN", reason, f"flag={flag}")

    def _insert_filled(self, log, market_id, token_id, side, price, size_usdc):
        log.insert_terminal(
            cycle_id="t-cycle",
            market_id=market_id,
            token_id=token_id,
            side=side,
            price=price,
            size_usdc=size_usdc,
            confidence=0.9,
            status=FILLED,
        )

    def _poly_with_midpoints(self, balance, midpoints):
        poly = MagicMock()
        poly.get_usdc_balance = MagicMock(return_value=balance)
        client = MagicMock()
        def get_mid(token_id):
            if token_id in midpoints:
                return {"mid": midpoints[token_id]}
            raise RuntimeError("unknown token")
        client.get_midpoint = MagicMock(side_effect=get_mid)
        poly.client = client
        return poly

    def test_drawdown_uses_portfolio_value_not_cash(self):
        """Cash-only drawdown reads deployed capital as loss; portfolio
        drawdown (cash + MTM) must not block when positions are flat."""
        log = TradeLog(db_path=self.db_path)
        # Deployed $9.49 split across 4 positions, all flat at entry price.
        self._insert_filled(log, "566188", "TOK_A", "BUY",  0.38,  1.996)
        self._insert_filled(log, "566228", "TOK_B", "BUY",  0.997, 1.946)
        self._insert_filled(log, "566187", "TOK_C", "SELL", 0.565, 1.897)
        self._insert_filled(log, "653788", "TOK_D", "BUY",  0.11,  3.650)
        # Midpoints exactly at entry → MTM == cost, portfolio == starting.
        poly = self._poly_with_midpoints(
            balance=70.51,  # 80 - 9.49 deployed
            midpoints={"TOK_A": 0.38, "TOK_B": 0.997, "TOK_C": 0.435, "TOK_D": 0.11},
        )
        gate = self._gate(polymarket=poly, starting_balance_usdc=80.0,
                          max_daily_loss_pct=0.10,
                          max_trades_per_hour=100)  # isolate drawdown check
        self.assertTrue(gate.ok(), msg=f"unexpected block: {gate.reason()}")

    def test_drawdown_blocks_on_real_mtm_loss(self):
        """If positions are actually losing enough to push portfolio below
        starting * (1 - max_daily_loss_pct), the gate must still block."""
        log = TradeLog(db_path=self.db_path)
        # Deployed $30 across two positions; both lost most of their value.
        self._insert_filled(log, "M1", "TOK_X", "BUY", 0.50, 20.0)  # cost $20
        self._insert_filled(log, "M2", "TOK_Y", "BUY", 0.40, 10.0)  # cost $10
        # Journal-based portfolio = starting - cost + mtm
        # = 100 - 30 + (40 * 0.10 + 25 * 0.05) = 100 - 30 + 5.25 = 75.25
        # drawdown = (100 - 75.25) / 100 = 24.75% > 10%
        poly = self._poly_with_midpoints(
            balance=70.0,  # cash is irrelevant under journal-based accounting
            midpoints={"TOK_X": 0.10, "TOK_Y": 0.05},
        )
        gate = self._gate(polymarket=poly, starting_balance_usdc=100.0,
                          max_daily_loss_pct=0.10,
                          max_trades_per_hour=100)  # isolate drawdown check
        self.assertFalse(gate.ok())
        self.assertIn("drawdown", gate.reason())

    def test_mtm_falls_back_to_entry_when_midpoint_fails(self):
        """If midpoint lookup raises, fall back to entry price for that
        position (treat it as flat) — don't crash, don't spuriously block."""
        log = TradeLog(db_path=self.db_path)
        self._insert_filled(log, "M1", "TOK_X", "BUY", 0.50, 20.0)
        # No midpoints registered → get_midpoint raises for every token.
        poly = self._poly_with_midpoints(balance=80.0, midpoints={})
        gate = self._gate(polymarket=poly, starting_balance_usdc=100.0,
                          max_daily_loss_pct=0.10)
        # Fallback MTM = cost = 20. Portfolio = 80 + 20 = 100. No drawdown.
        self.assertTrue(gate.ok(), msg=f"unexpected block: {gate.reason()}")
        self.assertAlmostEqual(gate.position_mtm_usd(), 20.0, places=4)

    def test_sell_position_mtm_uses_actual_no_entry_price(self):
        """SELL recommendations are encoded as BUYs of the NO token at
        order_price=(1-YES_recommendation). scanner_executor.py:723 stores
        TradeLog.price as the outcomes[0]-anchored value (the YES recommendation
        equivalent), NOT the actual NO token entry price. risk_gate must
        invert it with 1-price to get the real share-cost basis.

        Setup: SELL with YES anchor 0.40 → NO actual entry = 0.60. NO mid
        flat at 0.60 → portfolio at entry. Shares = 20/0.60 = 33.33;
        mtm = 33.33 * 0.60 = 20.0.
        """
        log = TradeLog(db_path=self.db_path)
        self._insert_filled(log, "M1", "NO_TOKEN", "SELL", 0.40, 20.0)
        poly = self._poly_with_midpoints(balance=60.0, midpoints={"NO_TOKEN": 0.60})
        gate = self._gate(polymarket=poly, starting_balance_usdc=80.0,
                          max_daily_loss_pct=0.10,
                          max_trades_per_hour=100)
        self.assertAlmostEqual(gate.position_mtm_usd(), 20.0, places=4)
        self.assertTrue(gate.ok(), msg=f"unexpected block: {gate.reason()}")

    def test_sell_position_mtm_reflects_no_price_move(self):
        """When the NO token's mid moves AWAY from entry, MTM must reflect
        the loss/gain in NO terms, not YES terms.

        Setup: SELL with YES anchor 0.40 → NO entry 0.60. NO mid drops to
        0.40 (the original YES anchor value) → real loss. Shares = 33.33;
        mtm = 33.33 * 0.40 = 13.33; loss = 6.67 ($-33%).

        Pre-fix bug: shares were computed as 20/0.40 = 50, mtm = 50*0.40
        = 20.0 → bug hid the loss entirely. This test now codifies the
        correct behaviour."""
        log = TradeLog(db_path=self.db_path)
        self._insert_filled(log, "M1", "NO_TOKEN", "SELL", 0.40, 20.0)
        poly = self._poly_with_midpoints(balance=60.0, midpoints={"NO_TOKEN": 0.40})
        gate = self._gate(polymarket=poly, starting_balance_usdc=80.0,
                          max_daily_loss_pct=0.50,  # allow drawdown to surface
                          max_trades_per_hour=100)
        # Real: 20 USDC / 0.60 NO_entry = 33.33 shares; 33.33 * 0.40 mid = 13.33
        self.assertAlmostEqual(gate.position_mtm_usd(), 13.333333, places=4)

    def test_sell_position_mtm_extreme_price_inversion(self):
        """Extreme SELL (YES anchor near 0): correct inversion is critical
        because the share count diverges sharply.

        SELL @ YES anchor 0.17 (NO entry 0.83). Pre-fix: shares = 20/0.17
        = 117.6, mtm at NO mid 0.83 = 97.65 → 5x overstatement.
        Post-fix: shares = 20/0.83 = 24.1; mtm = 24.1 * 0.83 = 20.0
        (flat at entry).
        """
        log = TradeLog(db_path=self.db_path)
        self._insert_filled(log, "M1", "NO_TOKEN", "SELL", 0.17, 20.0)
        poly = self._poly_with_midpoints(balance=60.0, midpoints={"NO_TOKEN": 0.83})
        gate = self._gate(polymarket=poly, starting_balance_usdc=80.0,
                          max_daily_loss_pct=0.10,
                          max_trades_per_hour=100)
        self.assertAlmostEqual(gate.position_mtm_usd(), 20.0, places=4)


class TestPolymarketDryRun(unittest.TestCase):
    def test_polymarket_live_false_no_private_key(self):
        old = os.environ.pop("POLYGON_WALLET_PRIVATE_KEY", None)
        try:
            from agents.polymarket.polymarket import Polymarket

            pm = Polymarket(live=False)
            self.assertIsNone(pm.client)
            self.assertIsNone(pm.credentials)
        finally:
            if old is not None:
                os.environ["POLYGON_WALLET_PRIVATE_KEY"] = old


class TestExecuteMarketOrderSideMapping(unittest.TestCase):
    def _build_market_doc(self, token_ids, outcomes):
        doc = MagicMock()
        doc.dict.return_value = {
            "metadata": {
                "clob_token_ids": str(token_ids),
                "outcomes": str(outcomes),
            }
        }
        return [doc]

    def _book(self, asks, bids=None):
        if bids is None:
            # Default: healthy bid book that passes entry guards.
            best_ask = asks[0][0] if asks else 0.50
            bids = [(best_ask - 0.01, 500)]
        return {
            "asks": [{"price": str(price), "size": str(size)} for price, size in asks],
            "bids": [{"price": str(price), "size": str(size)} for price, size in bids],
            "tick_size": "0.01",
        }

    def test_buy_picks_yes_token_with_anchor_price(self):
        from agents.polymarket.polymarket import Polymarket

        pm = Polymarket.__new__(Polymarket)
        pm.client = MagicMock()
        pm.client.get_order_book.return_value = self._book([(0.55, 100)])
        pm.client.create_and_post_market_order.return_value = {
            "orderID": "ord123",
            "status": "submitted",
        }

        rec = TradeRecommendation(
            price=0.55, size_fraction=0.1, side="BUY",
            confidence=0.7, amount_usdc=5.0,
        )
        market = self._build_market_doc(["yes_tok", "no_tok"], ["YES", "NO"])

        result = pm.execute_market_order(market, rec)

        self.assertEqual(result["token_id"], "yes_tok")
        self.assertEqual(result["outcome_traded"], "YES")
        self.assertEqual(result["amount_usdc"], 5.0)
        self.assertEqual(result["price_recommended"], 0.55)
        self.assertEqual(result["order_price_model"], 0.55)
        self.assertEqual(result["order_price"], 0.56)
        self.assertEqual(result["side_recommended"], "BUY")
        # Verify MarketOrderArgs received the live-book price plus one tick.
        args = pm.client.create_and_post_market_order.call_args[0][0]
        self.assertEqual(args.price, 0.56)
        self.assertEqual(args.token_id, "yes_tok")

    def test_sell_picks_no_token_and_inverts_price(self):
        from agents.polymarket.polymarket import Polymarket

        pm = Polymarket.__new__(Polymarket)
        pm.client = MagicMock()
        pm.client.get_order_book.return_value = self._book([(0.6, 100)])
        pm.client.create_and_post_market_order.return_value = {
            "orderID": "ord456",
            "status": "submitted",
        }

        # LLM thinks YES is worth 0.4 (so NO is worth 0.6) → recommends SELL at 0.4.
        rec = TradeRecommendation(
            price=0.4, size_fraction=0.1, side="SELL",
            confidence=0.8, amount_usdc=3.0,
        )
        market = self._build_market_doc(["yes_tok", "no_tok"], ["YES", "NO"])

        result = pm.execute_market_order(market, rec)

        self.assertEqual(result["token_id"], "no_tok")
        self.assertEqual(result["outcome_traded"], "NO")
        # SELL at price=0.4 (anchored to YES) = BUY of NO at price 0.6.
        self.assertAlmostEqual(result["order_price_model"], 0.6)
        self.assertAlmostEqual(result["order_price"], 0.61)
        args = pm.client.create_and_post_market_order.call_args[0][0]
        self.assertAlmostEqual(args.price, 0.61)
        self.assertEqual(args.token_id, "no_tok")

    def test_rejects_live_price_above_slippage(self):
        from agents.polymarket.polymarket import Polymarket

        pm = Polymarket.__new__(Polymarket)
        pm.client = MagicMock()
        pm.client.get_order_book.return_value = self._book([(0.7, 100)])

        rec = TradeRecommendation(
            price=0.55, size_fraction=0.1, side="BUY",
            confidence=0.7, amount_usdc=5.0,
        )
        market = self._build_market_doc(["yes_tok", "no_tok"], ["YES", "NO"])

        with self.assertRaises(ValueError):
            pm.execute_market_order(market, rec)
        pm.client.create_and_post_market_order.assert_not_called()

    def test_reduces_amount_to_available_liquidity(self):
        from agents.polymarket.polymarket import Polymarket

        pm = Polymarket.__new__(Polymarket)
        pm.client = MagicMock()
        pm.client.get_order_book.return_value = self._book([(0.55, 2)])
        pm.client.create_and_post_market_order.return_value = {
            "orderID": "ord789",
            "status": "submitted",
        }

        rec = TradeRecommendation(
            price=0.55, size_fraction=0.1, side="BUY",
            confidence=0.7, amount_usdc=5.0,
        )
        market = self._build_market_doc(["yes_tok", "no_tok"], ["YES", "NO"])

        result = pm.execute_market_order(market, rec)

        self.assertAlmostEqual(result["amount_usdc"], 1.1)
        args = pm.client.create_and_post_market_order.call_args[0][0]
        self.assertAlmostEqual(args.amount, 1.1)

    def test_rejects_non_binary_market(self):
        from agents.polymarket.polymarket import Polymarket

        pm = Polymarket.__new__(Polymarket)
        pm.client = MagicMock()
        pm.client.get_order_book.return_value = self._book([(0.5, 100)])

        rec = TradeRecommendation(
            price=0.5, size_fraction=0.1, side="BUY",
            confidence=0.5, amount_usdc=2.0,
        )
        market = self._build_market_doc(["a", "b", "c"], ["X", "Y", "Z"])

        with self.assertRaises(ValueError):
            pm.execute_market_order(market, rec)

    def test_rejects_zero_amount(self):
        from agents.polymarket.polymarket import Polymarket

        pm = Polymarket.__new__(Polymarket)
        pm.client = MagicMock()

        rec = TradeRecommendation(
            price=0.5, size_fraction=0.1, side="BUY",
            confidence=0.5, amount_usdc=0.0,
        )
        market = self._build_market_doc(["yes", "no"], ["YES", "NO"])

        with self.assertRaises(ValueError):
            pm.execute_market_order(market, rec)


class TestTraderTopN(TempDataMixin, unittest.TestCase):
    def _make_market(self, market_id, spread):
        doc = MagicMock()
        doc.dict.return_value = {
            "metadata": {
                "id": market_id,
                "spread": spread,
                "clob_token_ids": "['yes_t', 'no_t']",
                "outcomes": "['YES', 'NO']",
            }
        }
        return (doc, 0.5)

    def test_top_n_iteration_respects_min_confidence(self):
        from agents.application.trade import Trader

        with patch("agents.application.trade.Polymarket") as PMock, \
                patch("agents.application.trade.Agent") as AgentMock, \
                patch("agents.application.trade.Gamma"):
            pm = PMock.return_value
            pm.get_all_tradeable_events.return_value = []
            # Use a large balance so size_fraction * available_for_trader() exceeds
            # MIN_EXITABLE_ENTRY_USDC (default $3) regardless of reserve env vars.
            pm.get_usdc_balance.return_value = 1000.0

            agent = AgentMock.return_value
            agent.filter_events_with_rag.return_value = []
            agent.map_filtered_events_to_markets.return_value = []
            agent.filter_markets.return_value = [
                self._make_market(1, 0.05),
                self._make_market(2, 0.10),
                self._make_market(3, 0.02),
            ]
            agent.source_best_trade.side_effect = [
                "stub1", "stub2", "stub3",
            ]
            agent.parse_trade_recommendation.side_effect = [
                TradeRecommendation(price=0.6, size_fraction=0.1, side="BUY", confidence=0.4),
                TradeRecommendation(price=0.5, size_fraction=0.05, side="BUY", confidence=0.9),
                TradeRecommendation(price=0.7, size_fraction=0.05, side="SELL", confidence=0.85),
            ]

            tl = TradeLog(self.db_path)
            os.environ.pop("STARTING_BALANCE_USDC", None)
            gate = RiskGate(
                trade_log=tl, polymarket=pm,
                starting_balance_usdc=0.0,  # disable drawdown gate
                max_daily_loss_pct=0.99,
                max_trades_per_hour=99,
                min_usdc_floor=0.0,
                max_daily_token_usd=999.0,
                kill_switch_file=self.kill_path,
                llm_usage_file=self.usage_path,
            )
            trader = Trader(
                dry_run=True,
                top_n=3,
                max_trades_per_cycle=5,
                min_confidence=0.7,
                max_position_fraction=0.1,
                trade_log=tl,
                risk_gate=gate,
            )
            trader.meta_brain = None

            trader.one_best_trade_sweep()

        # 3 evaluated; 1 skipped_gate (low confidence), 2 skipped_dry_run.
        recent = tl.recent(limit=10)
        statuses = [r["status"] for r in recent]
        self.assertEqual(statuses.count(SKIPPED_GATE), 1)
        self.assertEqual(statuses.count(SKIPPED_DRY_RUN), 2)

    def test_shadow_can_continue_when_risk_gate_blocks(self):
        from agents.application.trade import Trader

        with patch.dict(os.environ, {"SHADOW_IGNORE_RISK_GATE": "true"}), \
                patch("agents.application.trade.Polymarket") as PMock, \
                patch("agents.application.trade.Agent") as AgentMock, \
                patch("agents.application.trade.Gamma"):
            pm = PMock.return_value
            pm.get_all_tradeable_events.return_value = []
            pm.get_usdc_balance.return_value = 50.0

            agent = AgentMock.return_value
            agent.filter_events_with_rag.return_value = []
            agent.map_filtered_events_to_markets.return_value = []
            agent.filter_markets.return_value = [self._make_market(22, 0.05)]
            agent.source_best_trade.return_value = "stub"
            agent.parse_trade_recommendation.return_value = TradeRecommendation(
                price=0.5, size_fraction=0.05, side="BUY", confidence=0.9,
            )

            tl = TradeLog(self.db_path)
            gate = MagicMock()
            gate.ok.return_value = False
            gate.reason.return_value = "paper test block"
            # Large enough so size_fraction * available ($0.05 * 200 = $10) exceeds
            # MIN_EXITABLE_ENTRY_USDC (default $3) — the exitable-size gate must pass.
            gate.available_for_trader.return_value = 200.0

            trader = Trader(
                dry_run=True,
                top_n=1,
                max_trades_per_cycle=1,
                min_confidence=0.7,
                max_position_fraction=0.1,
                trade_log=tl,
                risk_gate=gate,
            )
            trader.meta_brain = None

            trader.one_best_trade_sweep()

        recent = tl.recent(limit=5)
        self.assertEqual(recent[0]["status"], SKIPPED_DRY_RUN)

    def test_illiquid_market_writes_skipped_gate_not_failed(self):
        """execute_market_order raising ValueError('no asks available') must
        write SKIPPED_GATE (veto), not FAILED (error that blocks the trader
        for 24 h in the allocator)."""
        from agents.application.trade import Trader

        with patch("agents.application.trade.Polymarket") as PMock, \
                patch("agents.application.trade.Agent") as AgentMock, \
                patch("agents.application.trade.Gamma"):
            pm = PMock.return_value
            pm.get_all_tradeable_events.return_value = []
            pm.get_usdc_balance.return_value = 50.0
            pm.execute_market_order.side_effect = ValueError(
                "no asks available for token_id=abc123"
            )

            agent = AgentMock.return_value
            agent.filter_events_with_rag.return_value = []
            agent.map_filtered_events_to_markets.return_value = []
            agent.filter_markets.return_value = [self._make_market(99, 0.05)]
            agent.source_best_trade.return_value = "stub"
            agent.parse_trade_recommendation.return_value = TradeRecommendation(
                price=0.5, size_fraction=0.05, side="BUY", confidence=0.9,
            )

            tl = TradeLog(self.db_path)
            gate = RiskGate(
                trade_log=tl, polymarket=pm,
                starting_balance_usdc=0.0,
                max_daily_loss_pct=0.99,
                max_trades_per_hour=99,
                min_usdc_floor=0.0,
                max_daily_token_usd=999.0,
                kill_switch_file=self.kill_path,
                llm_usage_file=self.usage_path,
            )
            trader = Trader(
                dry_run=False,  # live path so execute_market_order is called
                top_n=1,
                max_trades_per_cycle=1,
                min_confidence=0.7,
                max_position_fraction=0.1,
                trade_log=tl,
                risk_gate=gate,
            )

            trader.one_best_trade_sweep()

        recent = tl.recent(limit=5)
        statuses = [r["status"] for r in recent]
        self.assertIn(SKIPPED_GATE, statuses, "illiquid market must write SKIPPED_GATE")
        self.assertNotIn(FAILED, statuses, "illiquid market must NOT write FAILED")

    def test_live_price_slippage_writes_skipped_gate_not_failed(self):
        from agents.application.trade import Trader

        with patch("agents.application.trade.Polymarket") as PMock, \
                patch("agents.application.trade.Agent") as AgentMock, \
                patch("agents.application.trade.Gamma"):
            pm = PMock.return_value
            pm.get_all_tradeable_events.return_value = []
            pm.get_usdc_balance.return_value = 50.0
            pm.execute_market_order.side_effect = ValueError(
                "live ask price 0.7400 exceeds recommended price 0.5000"
            )

            agent = AgentMock.return_value
            agent.filter_events_with_rag.return_value = []
            agent.map_filtered_events_to_markets.return_value = []
            agent.filter_markets.return_value = [self._make_market(100, 0.05)]
            agent.source_best_trade.return_value = "stub"
            agent.parse_trade_recommendation.return_value = TradeRecommendation(
                price=0.5, size_fraction=0.05, side="BUY", confidence=0.9,
            )

            tl = TradeLog(self.db_path)
            gate = RiskGate(
                trade_log=tl, polymarket=pm,
                starting_balance_usdc=0.0,
                max_daily_loss_pct=0.99,
                max_trades_per_hour=99,
                min_usdc_floor=0.0,
                max_daily_token_usd=999.0,
                kill_switch_file=self.kill_path,
                llm_usage_file=self.usage_path,
            )
            trader = Trader(
                dry_run=False,
                top_n=1,
                max_trades_per_cycle=1,
                min_confidence=0.7,
                max_position_fraction=0.1,
                trade_log=tl,
                risk_gate=gate,
            )
            trader.meta_brain = None

            trader.one_best_trade_sweep()

        recent = tl.recent(limit=5)
        statuses = [r["status"] for r in recent]
        self.assertIn(SKIPPED_GATE, statuses)
        self.assertNotIn(FAILED, statuses)

    def test_broken_market_failure_threshold_skips_before_llm(self):
        from agents.application.trade import Trader

        with patch("agents.application.trade.Polymarket") as PMock, \
                patch("agents.application.trade.Agent") as AgentMock, \
                patch("agents.application.trade.Gamma"):
            pm = PMock.return_value
            pm.get_all_tradeable_events.return_value = []
            pm.get_usdc_balance.return_value = 50.0

            agent = AgentMock.return_value
            agent.filter_events_with_rag.return_value = []
            agent.map_filtered_events_to_markets.return_value = []
            agent.filter_markets.return_value = [self._make_market(101, 0.05)]

            tl = TradeLog(self.db_path)
            for _ in range(3):
                tl.insert_terminal(
                    cycle_id=tl.new_cycle_id(),
                    market_id="101",
                    status=FAILED,
                    error="execute_market_order raised: PolyApiException[status_code=404]",
                )
            gate = RiskGate(
                trade_log=tl, polymarket=pm,
                starting_balance_usdc=0.0,
                max_daily_loss_pct=0.99,
                max_trades_per_hour=99,
                min_usdc_floor=0.0,
                max_daily_token_usd=999.0,
                kill_switch_file=self.kill_path,
                llm_usage_file=self.usage_path,
            )
            trader = Trader(
                dry_run=False,
                top_n=1,
                max_trades_per_cycle=1,
                min_confidence=0.7,
                max_position_fraction=0.1,
                trade_log=tl,
                risk_gate=gate,
            )
            trader.meta_brain = None

            trader.one_best_trade_sweep()

        agent.source_best_trade.assert_not_called()
        recent = tl.recent(limit=1)[0]
        self.assertEqual(recent["status"], SKIPPED_GATE)
        self.assertIn("broken_market_blacklist", recent["error"])

    def test_ai_quota_failure_in_event_filter_skips_cycle_not_crash(self):
        from agents.application.trade import Trader

        with patch("agents.application.trade.Polymarket") as PMock, \
                patch("agents.application.trade.Agent") as AgentMock, \
                patch("agents.application.trade.Gamma"):
            pm = PMock.return_value
            pm.get_all_tradeable_events.return_value = [MagicMock()]
            pm.get_usdc_balance.return_value = 50.0

            agent = AgentMock.return_value
            agent.filter_events_with_rag.side_effect = RuntimeError(
                "Error code: 429 - insufficient_quota"
            )

            tl = TradeLog(self.db_path)
            gate = RiskGate(
                trade_log=tl, polymarket=pm,
                starting_balance_usdc=0.0,
                max_daily_loss_pct=0.99,
                max_trades_per_hour=99,
                min_usdc_floor=0.0,
                max_daily_token_usd=999.0,
                kill_switch_file=self.kill_path,
                llm_usage_file=self.usage_path,
            )
            trader = Trader(
                dry_run=False,
                top_n=1,
                max_trades_per_cycle=1,
                min_confidence=0.7,
                max_position_fraction=0.1,
                trade_log=tl,
                risk_gate=gate,
            )
            trader.meta_brain = None

            trader.one_best_trade_sweep()

        recent = tl.recent(limit=1)[0]
        self.assertEqual(recent["status"], SKIPPED_GATE)
        self.assertEqual(recent["market_id"], "__cycle__")
        self.assertIn("ai_filter_unavailable", recent["error"])
        agent.map_filtered_events_to_markets.assert_not_called()

    def test_ai_quota_failure_in_trade_analysis_skips_market_not_failed(self):
        from agents.application.trade import Trader

        with patch("agents.application.trade.Polymarket") as PMock, \
                patch("agents.application.trade.Agent") as AgentMock, \
                patch("agents.application.trade.Gamma"):
            pm = PMock.return_value
            pm.get_all_tradeable_events.return_value = []
            pm.get_usdc_balance.return_value = 50.0

            agent = AgentMock.return_value
            agent.filter_events_with_rag.return_value = []
            agent.map_filtered_events_to_markets.return_value = []
            agent.filter_markets.return_value = [self._make_market(202, 0.05)]
            agent.source_best_trade.side_effect = RuntimeError(
                "Error code: 429 - insufficient_quota"
            )

            tl = TradeLog(self.db_path)
            gate = RiskGate(
                trade_log=tl, polymarket=pm,
                starting_balance_usdc=0.0,
                max_daily_loss_pct=0.99,
                max_trades_per_hour=99,
                min_usdc_floor=0.0,
                max_daily_token_usd=999.0,
                kill_switch_file=self.kill_path,
                llm_usage_file=self.usage_path,
            )
            trader = Trader(
                dry_run=False,
                top_n=1,
                max_trades_per_cycle=1,
                min_confidence=0.7,
                max_position_fraction=0.1,
                trade_log=tl,
                risk_gate=gate,
            )
            trader.meta_brain = None

            trader.one_best_trade_sweep()

        recent = tl.recent(limit=1)[0]
        self.assertEqual(recent["status"], SKIPPED_GATE)
        self.assertIn("ai_analysis_unavailable", recent["error"])


class TestReentryCooldown(TempDataMixin, unittest.TestCase):
    """Fix 1: has_recent_close_for_market blocks re-entry within window."""

    def test_blocks_within_window(self):
        tl = TradeLog(self.db_path)
        tl.insert_terminal("c1", "M1", "closed_timeout", token_id="TOK1")
        self.assertTrue(tl.has_recent_close_for_market("M1", hours=12))

    def test_allows_after_window(self):
        tl = TradeLog(self.db_path)
        # Insert a close row far in the past by manipulating ts directly
        with tl._connect() as conn:
            conn.execute(
                "INSERT INTO trades (ts, cycle_id, market_id, token_id, status) "
                "VALUES (?, ?, ?, ?, ?)",
                ("2025-01-01T00:00:00+00:00", "c1", "M1", "TOK1", "closed_timeout"),
            )
        self.assertFalse(tl.has_recent_close_for_market("M1", hours=12))

    def test_cross_agent_token_id_match(self):
        tl = TradeLog(self.db_path)
        # Close on different market_id but same token_id
        tl.insert_terminal("c1", "OTHER_MKT", "closed_stop_loss", token_id="TOK1")
        self.assertTrue(tl.has_recent_close_for_market("M1", hours=12, token_id="TOK1"))

    def test_filled_is_not_a_close(self):
        tl = TradeLog(self.db_path)
        tl.insert_terminal("c1", "M1", FILLED, token_id="TOK1")
        self.assertFalse(tl.has_recent_close_for_market("M1", hours=12))


class TestConcentrationLimit(TempDataMixin, unittest.TestCase):
    """Fix 2: count_recent_fills_for_market blocks at limit."""

    def test_counts_fills(self):
        tl = TradeLog(self.db_path)
        tl.insert_terminal("c1", "M1", FILLED, token_id="TOK1")
        tl.insert_terminal("c2", "M1", FILLED, token_id="TOK1")
        tl.insert_terminal("c3", "M1", FILLED, token_id="TOK1")
        self.assertEqual(tl.count_recent_fills_for_market("M1", hours=24), 3)

    def test_blocks_at_limit(self):
        tl = TradeLog(self.db_path)
        for i in range(3):
            tl.insert_terminal(f"c{i}", "M1", FILLED, token_id="TOK1")
        self.assertTrue(tl.count_recent_fills_for_market("M1", hours=24) >= 3)

    def test_cross_agent_token_match(self):
        tl = TradeLog(self.db_path)
        tl.insert_terminal("c1", "OTHER", FILLED, token_id="TOK1")
        self.assertEqual(
            tl.count_recent_fills_for_market("M1", hours=24, token_id="TOK1"), 1,
        )

    def test_old_fills_excluded(self):
        tl = TradeLog(self.db_path)
        with tl._connect() as conn:
            conn.execute(
                "INSERT INTO trades (ts, cycle_id, market_id, token_id, status) "
                "VALUES (?, ?, ?, ?, ?)",
                ("2025-01-01T00:00:00+00:00", "c1", "M1", "TOK1", "filled"),
            )
        self.assertEqual(tl.count_recent_fills_for_market("M1", hours=24), 0)


class TestPreExitBidDepth(TempDataMixin, unittest.TestCase):
    """Fix 3: pre-exit bid depth check in position_manager."""

    def _make_manager(self, bid_depth, execute=False, min_depth=5.0):
        from agents.application.position_manager import (
            PositionManager,
            PositionManagerConfig,
            AggregatedPosition,
        )
        import time as _time

        class FakePolymarket:
            def __init__(self, depth):
                self._depth = depth
            def bid_depth_usdc(self, token_id):
                return self._depth

        class FakeExitExecutor:
            def limit_price_from_mid(self, mid):
                return mid * 0.98
            def sell_fak(self, token_id, shares, mid):
                return None

        poly = FakePolymarket(bid_depth)
        tl = TradeLog(self.db_path)
        cfg = PositionManagerConfig(
            execute=execute,
            min_exit_bid_depth_usdc=min_depth,
        )
        mgr = PositionManager(
            polymarket=poly, trade_log=tl, cfg=cfg,
            exit_executor=FakeExitExecutor(),
        )
        pos = AggregatedPosition(
            token_id="TOK1",
            market_id="M1",
            side="BUY",
            total_cost_usdc=5.0,
            total_shares=10.0,
            avg_entry_price=0.50,
            earliest_ts=_time.time() - 7200,
        )
        return mgr, pos

    def test_timeout_deferred_on_empty_bids(self):
        mgr, pos = self._make_manager(bid_depth=0.0, execute=True)
        result = mgr._close_position(pos, "timeout", 0.50)
        self.assertEqual(result, "deferred")

    def test_stop_loss_not_deferred(self):
        mgr, pos = self._make_manager(bid_depth=0.0, execute=False)
        # stop_loss should NOT be deferred even with zero depth
        # In shadow mode it will return True (logged shadow close)
        result = mgr._close_position(pos, "stop_loss", 0.50)
        self.assertTrue(result)

    def test_take_profit_deferred_on_low_bids(self):
        mgr, pos = self._make_manager(bid_depth=2.0, execute=True)
        result = mgr._close_position(pos, "take_profit", 0.50)
        self.assertEqual(result, "deferred")

    def test_tp_proceeds_when_bids_sufficient(self):
        mgr, pos = self._make_manager(bid_depth=10.0, execute=False)
        result = mgr._close_position(pos, "take_profit", 0.50)
        self.assertTrue(result)

    def test_bid_depth_error_is_fail_open(self):
        """If bid_depth_usdc raises, the exit should proceed (fail-open)."""
        from agents.application.position_manager import (
            PositionManager,
            PositionManagerConfig,
            AggregatedPosition,
        )
        import time as _time

        class BrokenPolymarket:
            def bid_depth_usdc(self, token_id):
                raise RuntimeError("API down")

        class FakeExitExecutor:
            def limit_price_from_mid(self, mid):
                return mid * 0.98
            def sell_fak(self, token_id, shares, mid):
                return None

        tl = TradeLog(self.db_path)
        mgr = PositionManager(
            polymarket=BrokenPolymarket(),
            trade_log=tl,
            cfg=PositionManagerConfig(execute=False, min_exit_bid_depth_usdc=5.0),
            exit_executor=FakeExitExecutor(),
        )
        pos = AggregatedPosition(
            token_id="TOK1", market_id="M1", side="BUY",
            total_cost_usdc=5.0, total_shares=10.0,
            avg_entry_price=0.50,
            earliest_ts=_time.time() - 7200,
        )
        # Should proceed despite the exception (fail-open) — shadow mode returns True
        result = mgr._close_position(pos, "timeout", 0.50)
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
