"""Settlement reconciler for Polymarket positions.

Stop-loss handles live markets. This module handles the next failure class:
positions that are resolved, dust, unrecoverable, redeemable, or present in the
journal without matching exit-path evidence. It writes one durable
classification row per token so dashboards and the trading supervisor can make
control decisions from reality, not stale journal assumptions.
"""
from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agents.application.trade_log import TradeLog


logger = logging.getLogger(__name__)


ACTIVE_MANAGED = "active_managed"
ACTIVE_UNMANAGED = "active_unmanaged"
ACTIVE_RECOVERABLE = "active_recoverable"
DUST_UNRECOVERABLE = "dust_unrecoverable"
LOST_FINAL = "lost_final"
REDEEMABLE = "redeemable"
RESOLVED_WON_NO_BALANCE = "resolved_won_no_balance"
UNKNOWN = "unknown"
RECONCILE_ERROR = "reconcile_error"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _listish(value) -> list:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    try:
        return ast.literal_eval(str(value))
    except Exception:
        try:
            return json.loads(str(value))
        except Exception:
            return []


@dataclass
class SettlementReconcilerConfig:
    poll_seconds: int = 300
    heartbeat_path: str = "/app/data/settlement_reconciler_heartbeat"
    min_recoverable_usdc: float = 1.0
    gas_estimate_usdc: float = 0.05
    redeemable_shares_floor: float = 0.5
    on_chain_dust_floor: float = 0.5
    exit_evidence_grace_seconds: int = 240
    require_exit_evidence_for_active: bool = True
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "SettlementReconcilerConfig":
        maintain_poll = _env_int("MAINTAIN_POLL_SEC", 60)
        return cls(
            poll_seconds=_env_int("SETTLEMENT_RECONCILER_POLL_SEC", 300),
            heartbeat_path=os.getenv(
                "SETTLEMENT_RECONCILER_HEARTBEAT_PATH",
                "/app/data/settlement_reconciler_heartbeat",
            ),
            min_recoverable_usdc=_env_float("SETTLEMENT_MIN_RECOVERABLE_USDC", 1.0),
            gas_estimate_usdc=_env_float("SETTLEMENT_GAS_ESTIMATE_USDC", 0.05),
            redeemable_shares_floor=_env_float("SETTLEMENT_REDEEMABLE_SHARES_FLOOR", 0.5),
            on_chain_dust_floor=_env_float("SETTLEMENT_ON_CHAIN_DUST_FLOOR", 0.5),
            exit_evidence_grace_seconds=_env_int(
                "SETTLEMENT_EXIT_EVIDENCE_GRACE_SEC", max(240, maintain_poll * 8)
            ),
            require_exit_evidence_for_active=_env_bool(
                "SETTLEMENT_REQUIRE_EXIT_EVIDENCE", True
            ),
            enabled=_env_bool("SETTLEMENT_RECONCILER_ENABLED", True),
        )


@dataclass
class AggregatedSettlementPosition:
    token_id: str
    market_id: str
    side: str
    latest_open_trade_id: int
    opened_ts: str
    cost_basis_usdc: float
    journal_shares: float


class SettlementReconciler:
    def __init__(
        self,
        trade_log: TradeLog,
        cfg: Optional[SettlementReconcilerConfig] = None,
        polymarket=None,
    ):
        self.trade_log = trade_log
        self.cfg = cfg or SettlementReconcilerConfig.from_env()
        self.polymarket = polymarket

    def run_once(self) -> dict:
        if not self.cfg.enabled:
            return {"status": "disabled", "checked": 0, "counts": {}}
        positions = self._aggregate_open_positions()
        stale_cleared = self.trade_log.clear_stale_active_settlement_rows(
            {p.token_id for p in positions}
        )
        counts: dict[str, int] = {}
        rows: list[dict] = []
        for pos in positions:
            try:
                row = self._reconcile_position(pos)
            except Exception as exc:
                logger.exception("settlement_reconciler failed for %s", pos.token_id[:18])
                row = self._write_row(
                    pos,
                    status=RECONCILE_ERROR,
                    action="investigate",
                    details={"error": f"{type(exc).__name__}: {exc}"},
                )
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            rows.append(row)
        self._heartbeat()
        result = {
            "status": "ok",
            "checked": len(positions),
            "counts": counts,
            "stale_cleared": stale_cleared,
            "rows": rows,
        }
        logger.info("settlement_reconciler: %s", {
            "checked": result["checked"],
            "counts": counts,
            "stale_cleared": stale_cleared,
        })
        return result

    def _aggregate_open_positions(self) -> list[AggregatedSettlementPosition]:
        by_token: dict[str, AggregatedSettlementPosition] = {}
        for row in self.trade_log.filled_positions_with_id():
            token_id = str(row.get("token_id") or "")
            if not token_id:
                continue
            try:
                cost = float(row.get("size_usdc") or 0)
                price = float(row.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if cost <= 0 or price <= 0:
                continue
            side = str(row.get("side") or "BUY").upper()
            entry = price if side == "BUY" else max(0.0001, 1.0 - price)
            shares = cost / entry
            trade_id = int(row.get("id") or 0)
            existing = by_token.get(token_id)
            if existing is None:
                by_token[token_id] = AggregatedSettlementPosition(
                    token_id=token_id,
                    market_id=str(row.get("market_id") or ""),
                    side=side,
                    latest_open_trade_id=trade_id,
                    opened_ts=str(row.get("ts") or ""),
                    cost_basis_usdc=cost,
                    journal_shares=shares,
                )
                continue
            existing.cost_basis_usdc += cost
            existing.journal_shares += shares
            if trade_id > existing.latest_open_trade_id:
                existing.latest_open_trade_id = trade_id
                existing.opened_ts = str(row.get("ts") or existing.opened_ts)
                existing.market_id = str(row.get("market_id") or existing.market_id)
        return list(by_token.values())

    def _reconcile_position(self, pos: AggregatedSettlementPosition) -> dict:
        on_chain = self._on_chain_shares(pos.token_id)
        market = self._gamma_market_for_token(pos.token_id)
        bid, ask, book_error = self._best_bid_ask(pos.token_id)
        shares_for_recovery = (
            min(pos.journal_shares, on_chain)
            if on_chain is not None and on_chain >= 0
            else pos.journal_shares
        )
        recoverable = max(0.0, shares_for_recovery * (bid or 0.0))

        base_details = {
            "book_error": book_error,
            "market_closed": bool(market.get("closed")) if market else None,
            "market_active": market.get("active") if market else None,
            "question": market.get("question") if market else None,
        }

        if market and market.get("closed"):
            payout_per_share, outcome_label = self._payout_for_token(market, pos.token_id)
            if payout_per_share is None:
                return self._write_row(
                    pos,
                    status=UNKNOWN,
                    action="investigate_resolution",
                    on_chain_shares=on_chain,
                    best_bid=bid,
                    best_ask=ask,
                    recoverable_usdc=recoverable,
                    details={**base_details, "reason": "closed_market_unparsed_resolution"},
                )
            redeemable = (on_chain or 0.0) * payout_per_share
            if payout_per_share >= 0.99:
                if (on_chain or 0.0) >= self.cfg.redeemable_shares_floor:
                    return self._write_row(
                        pos,
                        status=REDEEMABLE,
                        action="redeem",
                        on_chain_shares=on_chain,
                        best_bid=bid,
                        best_ask=ask,
                        recoverable_usdc=recoverable,
                        redeemable_usdc=redeemable,
                        details={**base_details, "outcome_label": outcome_label},
                    )
                return self._write_row(
                    pos,
                    status=RESOLVED_WON_NO_BALANCE,
                    action="verify_redeemed",
                    on_chain_shares=on_chain,
                    best_bid=bid,
                    best_ask=ask,
                    recoverable_usdc=0.0,
                    redeemable_usdc=0.0,
                    details={**base_details, "outcome_label": outcome_label},
                )
            return self._write_row(
                pos,
                status=LOST_FINAL,
                action="mark_lost",
                on_chain_shares=on_chain,
                best_bid=bid,
                best_ask=ask,
                recoverable_usdc=0.0,
                redeemable_usdc=0.0,
                    details={**base_details, "outcome_label": outcome_label},
                )

        if market is None and bid is None:
            return self._write_row(
                pos,
                status=UNKNOWN,
                action="investigate_market_data",
                on_chain_shares=on_chain,
                best_bid=bid,
                best_ask=ask,
                recoverable_usdc=recoverable,
                details={**base_details, "reason": "no_gamma_market_or_orderbook"},
            )

        if recoverable < max(self.cfg.min_recoverable_usdc, self.cfg.gas_estimate_usdc):
            return self._write_row(
                pos,
                status=DUST_UNRECOVERABLE,
                action="do_not_sell_below_recovery_threshold",
                on_chain_shares=on_chain,
                best_bid=bid,
                best_ask=ask,
                recoverable_usdc=recoverable,
                details=base_details,
            )

        if not self._has_fresh_exit_evidence(pos):
            return self._write_row(
                pos,
                status=ACTIVE_UNMANAGED,
                action="halt_and_restore_exit_manager",
                on_chain_shares=on_chain,
                best_bid=bid,
                best_ask=ask,
                recoverable_usdc=recoverable,
                details=base_details,
            )

        return self._write_row(
            pos,
            status=ACTIVE_MANAGED if self.cfg.require_exit_evidence_for_active else ACTIVE_RECOVERABLE,
            action="monitor",
            on_chain_shares=on_chain,
            best_bid=bid,
            best_ask=ask,
            recoverable_usdc=recoverable,
            details=base_details,
        )

    def _write_row(
        self,
        pos: AggregatedSettlementPosition,
        *,
        status: str,
        action: str,
        on_chain_shares: Optional[float] = None,
        best_bid: Optional[float] = None,
        best_ask: Optional[float] = None,
        recoverable_usdc: Optional[float] = None,
        redeemable_usdc: Optional[float] = None,
        details: Optional[dict] = None,
    ) -> dict:
        self.trade_log.upsert_settlement_reconciliation(
            token_id=pos.token_id,
            market_id=pos.market_id,
            status=status,
            action=action,
            latest_open_trade_id=pos.latest_open_trade_id,
            cost_basis_usdc=pos.cost_basis_usdc,
            journal_shares=pos.journal_shares,
            on_chain_shares=on_chain_shares,
            best_bid=best_bid,
            best_ask=best_ask,
            recoverable_usdc=recoverable_usdc,
            redeemable_usdc=redeemable_usdc,
            gas_estimate_usdc=self.cfg.gas_estimate_usdc,
            details=details,
        )
        return {
            "token_id": pos.token_id,
            "market_id": pos.market_id,
            "status": status,
            "action": action,
            "latest_open_trade_id": pos.latest_open_trade_id,
            "cost_basis_usdc": round(pos.cost_basis_usdc, 6),
            "journal_shares": round(pos.journal_shares, 6),
            "on_chain_shares": on_chain_shares,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "recoverable_usdc": recoverable_usdc,
            "redeemable_usdc": redeemable_usdc,
        }

    def _has_fresh_exit_evidence(self, pos: AggregatedSettlementPosition) -> bool:
        if not self.cfg.require_exit_evidence_for_active:
            return True
        opened = _parse_ts(pos.opened_ts)
        if opened is None:
            return False
        cutoff = datetime.now(timezone.utc).timestamp() - self.cfg.exit_evidence_grace_seconds
        with self.trade_log._lock, self.trade_log._connect() as conn:
            mark = conn.execute(
                "SELECT last_seen_ts FROM position_marks WHERE token_id=?",
                (pos.token_id,),
            ).fetchone()
            decision = conn.execute(
                """
                SELECT ts FROM brain_decisions
                WHERE agent='position_manager'
                  AND decision_type='exit'
                  AND token_id=?
                  AND ts >= ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (pos.token_id, opened.isoformat()),
            ).fetchone()
        mark_ts = _parse_ts(mark["last_seen_ts"]) if mark else None
        decision_ts = _parse_ts(decision["ts"]) if decision else None
        return (
            mark_ts is not None
            and decision_ts is not None
            and mark_ts.timestamp() >= cutoff
            and decision_ts.timestamp() >= cutoff
        )

    def _on_chain_shares(self, token_id: str) -> Optional[float]:
        if self.polymarket is None or getattr(self.polymarket, "client", None) is None:
            return None
        try:
            from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
            resp = self.polymarket.client.get_balance_allowance(
                params=BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=str(token_id),
                )
            )
            if isinstance(resp, dict):
                return float(resp.get("balance", 0)) / 1_000_000
            bal_raw = getattr(resp, "balance", None)
            if bal_raw is not None:
                return float(bal_raw) / 1_000_000
        except Exception as exc:
            logger.warning("settlement_reconciler balance fetch failed for %s: %s", token_id[:18], exc)
        return None

    def _best_bid_ask(self, token_id: str) -> tuple[Optional[float], Optional[float], Optional[str]]:
        if self.polymarket is None or getattr(self.polymarket, "client", None) is None:
            return None, None, "polymarket_client_unavailable"
        try:
            book = self.polymarket.client.get_order_book(str(token_id))
            bids = self._book_entries(book, "bids")
            asks = self._book_entries(book, "asks")
            best_bid = max((self._entry_price_size(b)[0] for b in bids), default=None)
            best_ask = min((self._entry_price_size(a)[0] for a in asks), default=None)
            return best_bid, best_ask, None
        except Exception as exc:
            return None, None, f"{type(exc).__name__}: {exc}"

    def _book_entries(self, book, side: str) -> list:
        entries = getattr(book, side, None)
        if entries is None and isinstance(book, dict):
            entries = book.get(side, [])
        return entries or []

    def _entry_price_size(self, entry) -> tuple[float, float]:
        if hasattr(entry, "price"):
            return float(entry.price), float(entry.size)
        if isinstance(entry, (tuple, list)):
            return float(entry[0]), float(entry[1])
        return float(entry["price"]), float(entry["size"])

    def _gamma_market_for_token(self, token_id: str) -> Optional[dict]:
        if self.polymarket is not None:
            fn = getattr(self.polymarket, "gamma_market_for_token", None)
            if callable(fn):
                return fn(token_id)
        try:
            import urllib.parse
            import urllib.request
            params = urllib.parse.urlencode({"clob_token_ids": str(token_id)})
            url = f"https://gamma-api.polymarket.com/markets?{params}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 poly1-settlement-reconciler"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if isinstance(data, list) and data:
                return data[0]
            if isinstance(data, dict) and data.get("data"):
                return data["data"][0] if data["data"] else None
        except Exception as exc:
            logger.debug("settlement_reconciler gamma lookup failed for %s: %s", token_id[:18], exc)
        return None

    def _payout_for_token(self, market: dict, token_id: str) -> tuple[Optional[float], Optional[str]]:
        token_ids = [str(t) for t in _listish(market.get("clobTokenIds") or market.get("clob_token_ids"))]
        outcome_prices = [float(p) for p in _listish(market.get("outcomePrices") or market.get("outcome_prices"))]
        outcomes = _listish(market.get("outcomes"))
        if not token_ids or len(outcome_prices) < len(token_ids):
            return None, None
        try:
            idx = token_ids.index(str(token_id))
        except ValueError:
            return None, None
        label = str(outcomes[idx]) if idx < len(outcomes) else None
        return float(outcome_prices[idx]), label

    def _heartbeat(self) -> None:
        path = Path(self.cfg.heartbeat_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


class SettlementReconcilerDaemon:
    def __init__(self, db_path: Optional[str] = None):
        self.cfg = SettlementReconcilerConfig.from_env()
        self.trade_log = TradeLog(db_path=db_path)
        from agents.polymarket.polymarket import Polymarket
        self.polymarket = Polymarket(live=True)
        self.engine = SettlementReconciler(
            trade_log=self.trade_log,
            cfg=self.cfg,
            polymarket=self.polymarket,
        )
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            signal.signal(signal.SIGTERM, lambda *_: self.stop())
            signal.signal(signal.SIGINT, lambda *_: self.stop())
        except (ValueError, OSError):
            pass
        logger.info(
            "SettlementReconcilerDaemon: starting poll=%ss",
            self.cfg.poll_seconds,
        )
        try:
            while not self._stop.is_set():
                try:
                    self.engine.run_once()
                except Exception:
                    logger.exception("settlement_reconciler cycle failed")
                self._stop.wait(self.cfg.poll_seconds)
        finally:
            logger.info("SettlementReconcilerDaemon: exited")


def main() -> int:
    parser = argparse.ArgumentParser(description="poly1 settlement reconciler")
    parser.add_argument("--once", action="store_true", help="run one check and exit")
    parser.add_argument("--json", action="store_true", help="print JSON in --once mode")
    parser.add_argument("--no-live", action="store_true", help="do not create Polymarket client")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    if args.once:
        polymarket = None
        if not args.no_live:
            from agents.polymarket.polymarket import Polymarket
            polymarket = Polymarket(live=True)
        result = SettlementReconciler(TradeLog(), polymarket=polymarket).run_once()
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    SettlementReconcilerDaemon().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
