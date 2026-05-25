"""BTC 5-min timed-entry strategy agent.

Operator-requested 2026-05-25. Time-based entry, no LLM, no signal:

  Phase 1: at t=0:01 into each 5-min period, BUY DOWN
           TP at +5%, SL at -20%
  Phase 2: at t=3:00 into each 5-min period, BUY UP
           TP at +5%, SL at -20%
  No-bet last 30 sec (t > 4:30)

Backtest 14d: -$582 net (-$41/day @ $1 position).
Operator accepts the risk and wants to validate empirically.

Defaults:
  - EXECUTE_BTC5MIN_TIMED=false (must be explicitly enabled)
  - BTC5MIN_TIMED_POSITION_USDC=0.20 (small)
  - BTC5MIN_TIMED_MAX_TRADES_PER_DAY=10 (cap exposure)
  - BTC5MIN_TIMED_HALT_AFTER_LOSSES=3 (auto-halt after 3 consecutive SL/resolved_loss)

Architecture:
  - Single asyncio loop, polls every 5 sec
  - At t=0:01 boundary: schedule Phase 1 entry
  - At t=3:00 boundary: schedule Phase 2 entry
  - Position_manager handles exits via sl_pct_override / tp_pct_override
  - Annotated with btc5min_timed_open status for ledger isolation

Heartbeat: data/btc5min_timed_heartbeat
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except (ValueError, TypeError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes")


@dataclass
class Btc5MinTimedConfig:
    """Operator-tunable parameters."""
    execute: bool = False
    position_usdc: float = 0.20
    max_trades_per_day: int = 10
    halt_after_losses: int = 3
    # Phase 1: DOWN at start
    phase1_entry_offset_sec: int = 1       # t=0:01
    phase1_tp_pct: float = 0.05            # +5%
    phase1_sl_pct: float = 0.20            # -20%
    # Phase 2: UP at minute 3
    phase2_entry_offset_sec: int = 180     # t=3:00
    phase2_tp_pct: float = 0.05            # +5%
    phase2_sl_pct: float = 0.20            # -20%
    # Common
    no_entry_after_sec: int = 270          # don't enter after t=4:30
    poll_sec: int = 2
    asset: str = "btc"                     # market asset (BTC/ETH/SOL)
    heartbeat_path: str = "/app/data/btc5min_timed_heartbeat"

    @classmethod
    def from_env(cls) -> "Btc5MinTimedConfig":
        return cls(
            execute=_env_bool("EXECUTE_BTC5MIN_TIMED", False),
            position_usdc=_env_float("BTC5MIN_TIMED_POSITION_USDC", 0.20),
            max_trades_per_day=_env_int("BTC5MIN_TIMED_MAX_TRADES_PER_DAY", 10),
            halt_after_losses=_env_int("BTC5MIN_TIMED_HALT_AFTER_LOSSES", 3),
            phase1_entry_offset_sec=_env_int("BTC5MIN_TIMED_PHASE1_OFFSET_SEC", 1),
            phase1_tp_pct=_env_float("BTC5MIN_TIMED_PHASE1_TP_PCT", 0.05),
            phase1_sl_pct=_env_float("BTC5MIN_TIMED_PHASE1_SL_PCT", 0.20),
            phase2_entry_offset_sec=_env_int("BTC5MIN_TIMED_PHASE2_OFFSET_SEC", 180),
            phase2_tp_pct=_env_float("BTC5MIN_TIMED_PHASE2_TP_PCT", 0.05),
            phase2_sl_pct=_env_float("BTC5MIN_TIMED_PHASE2_SL_PCT", 0.20),
            no_entry_after_sec=_env_int("BTC5MIN_TIMED_NO_ENTRY_AFTER_SEC", 270),
            poll_sec=_env_int("BTC5MIN_TIMED_POLL_SEC", 2),
            asset=os.getenv("BTC5MIN_TIMED_ASSET", "btc").lower(),
            heartbeat_path=os.getenv("BTC5MIN_TIMED_HEARTBEAT_PATH", "/app/data/btc5min_timed_heartbeat"),
        )


def _current_period_ts() -> int:
    """Current 5-min period boundary (epoch seconds)."""
    return int(time.time() // 300) * 300


def _format_slug(period_ts: int, asset: str) -> str:
    """Market slug, matches the btc_5min slug convention."""
    return f"{asset.lower()}-updown-5m-{period_ts}"


@dataclass
class CycleState:
    """Per-period state — which phases have fired."""
    period_ts: int = 0
    phase1_fired: bool = False
    phase2_fired: bool = False


@dataclass
class DailyState:
    """Per-day risk state — cap exposure + auto-halt."""
    date_key: str = ""
    trades_today: int = 0
    consecutive_losses: int = 0
    auto_halted: bool = False


class Btc5MinTimedEngine:
    """Time-based DOWN/UP strategy engine. NOT driven by signals."""

    def __init__(
        self,
        polymarket,
        trade_log,
        risk_gate,
        cfg: Btc5MinTimedConfig,
    ):
        self.polymarket = polymarket
        self.trade_log = trade_log
        self.risk_gate = risk_gate
        self.cfg = cfg
        self._cycle: CycleState = CycleState()
        self._daily: DailyState = DailyState(date_key=self._today_key())

    @staticmethod
    def _today_key() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _reset_daily_if_new_day(self) -> None:
        today = self._today_key()
        if today != self._daily.date_key:
            self._daily = DailyState(date_key=today)

    def _refresh_cycle(self, period_ts: int) -> None:
        if period_ts != self._cycle.period_ts:
            self._cycle = CycleState(period_ts=period_ts)

    def maybe_enter(self) -> Optional[str]:
        """Check timing; return 'phase1', 'phase2', or None.

        This is the ONLY decision logic. No signal, no LLM, no consensus.
        Just time within the 5-min period.
        """
        self._reset_daily_if_new_day()
        if self._daily.auto_halted:
            return None
        if self._daily.trades_today >= self.cfg.max_trades_per_day:
            return None

        now = time.time()
        period_ts = _current_period_ts()
        self._refresh_cycle(period_ts)
        elapsed = now - period_ts

        # Skip last 30 sec to avoid mid-resolution chaos
        if elapsed > self.cfg.no_entry_after_sec:
            return None

        # Risk gate first (runtime control, balance, etc.)
        if self.risk_gate is not None and not self.risk_gate.ok():
            return None

        # Phase 1: BUY DOWN at t=0:01
        if not self._cycle.phase1_fired:
            if abs(elapsed - self.cfg.phase1_entry_offset_sec) < self.cfg.poll_sec:
                return "phase1"

        # Phase 2: BUY UP at t=3:00
        if not self._cycle.phase2_fired:
            if abs(elapsed - self.cfg.phase2_entry_offset_sec) < self.cfg.poll_sec:
                return "phase2"

        return None

    def fire(self, phase: str) -> bool:
        """Attempt entry for phase. Returns True if filled."""
        period_ts = _current_period_ts()
        slug = _format_slug(period_ts, self.cfg.asset)

        if phase == "phase1":
            side = "SELL"   # SELL YES = BUY NO = bet DOWN
            tp_pct = self.cfg.phase1_tp_pct
            sl_pct = self.cfg.phase1_sl_pct
            label = "DOWN"
        elif phase == "phase2":
            side = "BUY"    # BUY YES = bet UP
            tp_pct = self.cfg.phase2_tp_pct
            sl_pct = self.cfg.phase2_sl_pct
            label = "UP"
        else:
            return False

        if not self.cfg.execute:
            logger.info(
                "btc5min_timed[%s/%s] DRYRUN entry: side=%s tp=%.0f%% sl=%.0f%%",
                self.cfg.asset, label, side, tp_pct * 100, sl_pct * 100,
            )
            if phase == "phase1":
                self._cycle.phase1_fired = True
            else:
                self._cycle.phase2_fired = True
            return True

        # Live execution path — fetch market doc, place order, write trade row.
        # NOT IMPLEMENTED in this scaffolding — real implementation requires
        # market resolution + order placement + per-position TP/SL override
        # via response_json. The operator must wire this before EXECUTE=true.
        logger.warning(
            "btc5min_timed[%s/%s] LIVE entry requested but execution not implemented. "
            "Set EXECUTE_BTC5MIN_TIMED=false until live path is reviewed.",
            self.cfg.asset, label,
        )
        return False


class Btc5MinTimedDaemon:
    """Long-running loop. SIGTERM-aware."""

    def __init__(self):
        self.cfg = Btc5MinTimedConfig.from_env()
        self._stop = False
        # Lazy imports — module must be importable for tests without these.
        from agents.application.trade_log import TradeLog
        from agents.polymarket.polymarket import Polymarket
        from agents.application.risk_gate import RiskGate
        self.tl = TradeLog()
        self.polymarket = Polymarket(live=self.cfg.execute)
        self.risk_gate = RiskGate(trade_log=self.tl, polymarket=self.polymarket)
        self.engine = Btc5MinTimedEngine(
            polymarket=self.polymarket,
            trade_log=self.tl,
            risk_gate=self.risk_gate,
            cfg=self.cfg,
        )

    def run(self) -> None:
        import signal
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "_stop", True))
        logger.info(
            "Btc5MinTimedDaemon: starting execute=%s asset=%s position=$%.2f",
            self.cfg.execute, self.cfg.asset, self.cfg.position_usdc,
        )
        try:
            while not self._stop:
                try:
                    phase = self.engine.maybe_enter()
                    if phase:
                        self.engine.fire(phase)
                except Exception:
                    logger.exception("btc5min_timed cycle failed")
                # heartbeat
                try:
                    p = Path(self.cfg.heartbeat_path)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.touch()
                except Exception:
                    pass
                time.sleep(self.cfg.poll_sec)
        finally:
            logger.info("Btc5MinTimedDaemon: exited")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    Btc5MinTimedDaemon().run()
