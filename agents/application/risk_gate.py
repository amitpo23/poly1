import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from agents.application.trade_log import TradeLog, SUBMITTED, FILLED


logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("invalid float for %s=%r, using default %s", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("invalid int for %s=%r, using default %s", name, raw, default)
        return default


class RiskGate:
    """Pre-trade risk checks. Read once at construction; balance fetched on each call."""

    def __init__(
        self,
        trade_log: TradeLog,
        polymarket=None,
        starting_balance_usdc: Optional[float] = None,
        max_daily_loss_pct: Optional[float] = None,
        max_trades_per_hour: Optional[int] = None,
        min_usdc_floor: Optional[float] = None,
        max_daily_token_usd: Optional[float] = None,
        kill_switch_file: Optional[str] = None,
        llm_usage_file: Optional[str] = None,
        scalper_reserve_usdc: Optional[float] = None,   # NEW
    ):
        self.trade_log = trade_log
        self.polymarket = polymarket
        self.starting_balance = (
            starting_balance_usdc
            if starting_balance_usdc is not None
            else _env_float("STARTING_BALANCE_USDC", 0.0)
        )
        self.max_daily_loss_pct = (
            max_daily_loss_pct
            if max_daily_loss_pct is not None
            else _env_float("MAX_DAILY_LOSS_PCT", 0.10)
        )
        self.max_trades_per_hour = (
            max_trades_per_hour
            if max_trades_per_hour is not None
            else _env_int("MAX_TRADES_PER_HOUR", 4)
        )
        self.min_usdc_floor = (
            min_usdc_floor
            if min_usdc_floor is not None
            else _env_float("MIN_USDC_FLOOR", 10.0)
        )
        self.max_daily_token_usd = (
            max_daily_token_usd
            if max_daily_token_usd is not None
            else _env_float("MAX_DAILY_TOKEN_USD", 5.0)
        )
        self.kill_switch_file = Path(
            kill_switch_file or os.getenv("KILL_SWITCH_FILE", "./data/HALT")
        )
        self.llm_usage_file = Path(
            llm_usage_file or os.getenv("LLM_USAGE_FILE", "./data/llm_usage.jsonl")
        )
        self.scalper_reserve = (
            scalper_reserve_usdc if scalper_reserve_usdc is not None
            else _env_float("SCALPER_RESERVE_USDC", 0.0)
        )

    def available_for_trader(self) -> float:
        if self.polymarket is None:
            return 0.0
        bal = self.polymarket.get_usdc_balance()
        return max(0.0, bal - self.scalper_reserve)

    def reason(self) -> Optional[str]:
        """Return None if all gates pass, else a short string describing the first failure."""
        if self.kill_switch_file.exists():
            return f"kill switch file present: {self.kill_switch_file}"

        if self.polymarket is not None:
            try:
                bal = self.polymarket.get_usdc_balance()
            except Exception as e:
                return f"balance read failed: {e}"
            available = max(0.0, bal - self.scalper_reserve)
            if available < self.min_usdc_floor:
                return (
                    f"available {available:.4f} (after scalper reserve "
                    f"{self.scalper_reserve:.4f}) below floor {self.min_usdc_floor}"
                )
            if self.starting_balance > 0:  # keep drawdown check on raw bal
                drawdown = (self.starting_balance - bal) / self.starting_balance
                if drawdown > self.max_daily_loss_pct:
                    return (
                        f"drawdown {drawdown:.2%} above max_daily_loss_pct "
                        f"{self.max_daily_loss_pct:.2%}"
                    )

        recent = self.trade_log.count_recent((SUBMITTED, FILLED), hours=1)
        if recent >= self.max_trades_per_hour:
            return (
                f"submitted trades in last hour {recent} >= "
                f"max_trades_per_hour {self.max_trades_per_hour}"
            )

        if self.daily_token_usd() >= self.max_daily_token_usd:
            return (
                f"daily LLM cost ${self.daily_token_usd():.2f} >= "
                f"max ${self.max_daily_token_usd:.2f}"
            )

        return None

    def ok(self) -> bool:
        r = self.reason()
        if r:
            logger.warning("risk_gate: blocked - %s", r)
            return False
        return True

    def daily_token_usd(self) -> float:
        if not self.llm_usage_file.exists():
            return 0.0
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        total = 0.0
        try:
            with self.llm_usage_file.open("r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = rec.get("ts", "")
                    if ts < cutoff:
                        continue
                    total += float(rec.get("est_cost_usd", 0.0))
        except OSError as e:
            logger.warning("llm usage read failed: %s", e)
        return total
