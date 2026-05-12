from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agents.application.settlement_reconciler import (
    ACTIVE_MANAGED,
    ACTIVE_UNMANAGED,
    DUST_UNRECOVERABLE,
    LOST_FINAL,
    REDEEMABLE,
    SettlementReconciler,
    SettlementReconcilerConfig,
    UNKNOWN,
)
from agents.application.trade_log import BTC_DAILY_OPEN, FILLED, TradeLog
from agents.application.trading_supervisor import (
    TradingSupervisor,
    TradingSupervisorConfig,
)


class _FakeClient:
    def __init__(self, bids=None, asks=None):
        self.bids = bids if bids is not None else [(0.25, 100)]
        self.asks = asks if asks is not None else [(0.30, 100)]

    def get_order_book(self, token_id):
        return {"bids": self.bids, "asks": self.asks}


class _FakePoly:
    def __init__(self, market, bids=None, asks=None):
        self._market = market
        self.client = _FakeClient(bids=bids, asks=asks)

    def gamma_market_for_token(self, token_id):
        return self._market


class _Reconciler(SettlementReconciler):
    def __init__(self, *args, on_chain=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._test_on_chain = on_chain

    def _on_chain_shares(self, token_id):
        return self._test_on_chain


class TestSettlementReconciler(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = str(self.root / "trade_log.db")
        self.log = TradeLog(self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def _cfg(self, **overrides):
        defaults = dict(
            poll_seconds=300,
            heartbeat_path=str(self.root / "settlement_hb"),
            min_recoverable_usdc=1.0,
            gas_estimate_usdc=0.05,
            redeemable_shares_floor=0.5,
            on_chain_dust_floor=0.5,
            exit_evidence_grace_seconds=240,
            require_exit_evidence_for_active=True,
            enabled=True,
        )
        defaults.update(overrides)
        return SettlementReconcilerConfig(**defaults)

    def _insert_open(
        self,
        token_id="TOK_YES",
        price=0.50,
        cost=5.0,
        seconds_ago=600,
        status=FILLED,
    ):
        trade_id = self.log.insert_terminal(
            cycle_id="entry",
            market_id="M1",
            token_id=token_id,
            side="BUY",
            price=price,
            size_usdc=cost,
            confidence=0.8,
            status=status,
        )
        ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
        with self.log._connect() as conn:
            conn.execute("UPDATE trades SET ts = ? WHERE id = ?", (ts, trade_id))
        return trade_id, ts

    def _market(self, closed=False, prices=None):
        return {
            "id": "M1",
            "question": "Will test resolve?",
            "active": not closed,
            "closed": closed,
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["TOK_YES", "TOK_NO"]',
            "outcomePrices": str(prices if prices is not None else [0.5, 0.5]),
        }

    def _insert_exit_evidence(self, token_id="TOK_YES", seconds_ago=30):
        ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
        self.log.upsert_position_mark(
            token_id=token_id,
            market_id="M1",
            entry_price=0.50,
            current_price=0.55,
            shares=10,
        )
        with self.log._connect() as conn:
            conn.execute(
                "UPDATE position_marks SET first_seen_ts=?, last_seen_ts=? WHERE token_id=?",
                (ts, ts, token_id),
            )
            conn.execute(
                """
                INSERT INTO brain_decisions
                    (ts, agent, strategy, decision_type, market_id, token_id,
                     approved, reason, score, action)
                VALUES (?, 'position_manager', 'position_exit', 'exit',
                        'M1', ?, 0, 'hold', 0.0, 'HOLD')
                """,
                (ts, token_id),
            )

    def test_active_unmanaged_when_recoverable_but_no_exit_evidence(self):
        self._insert_open()
        rec = _Reconciler(
            self.log,
            self._cfg(),
            polymarket=_FakePoly(self._market(), bids=[(0.20, 100)]),
            on_chain=10.0,
        )
        result = rec.run_once()
        self.assertEqual(result["counts"], {ACTIVE_UNMANAGED: 1})
        row = self.log.latest_settlement_reconciliations()[0]
        self.assertEqual(row["status"], ACTIVE_UNMANAGED)
        self.assertEqual(row["action"], "halt_and_restore_exit_manager")

    def test_active_managed_with_fresh_exit_evidence(self):
        self._insert_open()
        self._insert_exit_evidence()
        rec = _Reconciler(
            self.log,
            self._cfg(),
            polymarket=_FakePoly(self._market(), bids=[(0.20, 100)]),
            on_chain=10.0,
        )
        result = rec.run_once()
        self.assertEqual(result["counts"], {ACTIVE_MANAGED: 1})

    def test_dust_unrecoverable_when_bid_value_too_small(self):
        self._insert_open(cost=1.0)
        rec = _Reconciler(
            self.log,
            self._cfg(min_recoverable_usdc=1.0),
            polymarket=_FakePoly(self._market(), bids=[(0.01, 100)]),
            on_chain=2.0,
        )
        result = rec.run_once()
        self.assertEqual(result["counts"], {DUST_UNRECOVERABLE: 1})

    def test_missing_market_data_is_unknown_not_dust(self):
        self._insert_open(cost=1.0)
        rec = _Reconciler(
            self.log,
            self._cfg(min_recoverable_usdc=1.0),
            polymarket=None,
            on_chain=None,
        )
        result = rec.run_once()
        self.assertEqual(result["counts"], {UNKNOWN: 1})

    def test_closed_winner_with_balance_is_redeemable(self):
        self._insert_open()
        rec = _Reconciler(
            self.log,
            self._cfg(),
            polymarket=_FakePoly(self._market(closed=True, prices=[1.0, 0.0])),
            on_chain=10.0,
        )
        result = rec.run_once()
        self.assertEqual(result["counts"], {REDEEMABLE: 1})
        row = self.log.latest_settlement_reconciliations()[0]
        self.assertAlmostEqual(row["redeemable_usdc"], 10.0)

    def test_closed_loser_is_lost_final(self):
        self._insert_open(token_id="TOK_NO")
        rec = _Reconciler(
            self.log,
            self._cfg(),
            polymarket=_FakePoly(self._market(closed=True, prices=[1.0, 0.0])),
            on_chain=10.0,
        )
        result = rec.run_once()
        self.assertEqual(result["counts"], {LOST_FINAL: 1})

    def test_old_terminal_row_does_not_hide_new_open_position(self):
        self._insert_open(token_id="TOK_YES", seconds_ago=1200)
        self.log.insert_terminal(
            cycle_id="old-close",
            market_id="M1",
            token_id="TOK_YES",
            side="SELL",
            price=0.50,
            size_usdc=0.5,
            status="closed_dust",
        )
        new_id, _ = self._insert_open(
            token_id="TOK_YES", status=BTC_DAILY_OPEN, seconds_ago=600
        )
        rec = _Reconciler(
            self.log,
            self._cfg(),
            polymarket=_FakePoly(self._market(), bids=[(0.20, 100)]),
            on_chain=10.0,
        )
        rec.run_once()
        row = self.log.latest_settlement_reconciliations()[0]
        self.assertEqual(row["latest_open_trade_id"], new_id)
        self.assertEqual(row["status"], ACTIVE_UNMANAGED)


class TestSupervisorSettlementIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.log = TradeLog(str(self.root / "trade_log.db"))
        self.pm_hb = self.root / "position_manager_heartbeat"
        self.pm_hb.touch()

    def tearDown(self):
        self._tmp.cleanup()

    def test_supervisor_halts_on_redeemable_reconciliation(self):
        self.log.upsert_settlement_reconciliation(
            token_id="TOK",
            market_id="M1",
            status=REDEEMABLE,
            action="redeem",
            redeemable_usdc=10.0,
        )
        cfg = TradingSupervisorConfig(
            poll_seconds=60,
            heartbeat_path=str(self.root / "supervisor_hb"),
            state_path=str(self.root / "supervisor_state.json"),
            position_manager_heartbeat_path=str(self.pm_hb),
            kill_switch_file=str(self.root / "HALT"),
            stale_heartbeat_seconds=180,
            evaluation_grace_seconds=180,
            min_position_age_seconds=45,
            close_failed_window_minutes=15,
            close_failed_threshold=5,
            settlement_max_age_minutes=15,
            enforce_halt=True,
        )
        result = TradingSupervisor(self.log, cfg).run_once()
        codes = {i["code"] for i in result["issues"]}
        self.assertEqual(result["status"], "critical")
        self.assertIn("settlement_reconciliation_requires_action", codes)
        self.assertTrue((self.root / "HALT").exists())


if __name__ == "__main__":
    unittest.main()
