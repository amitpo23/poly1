import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from agents.application.trade_log import TradeLog, SUBMITTED, FILLED
from agents.application.trading_policy import (
    MAX_AGENT_ALLOCATION_FRACTION,
    MAX_TRADES_PER_HOUR,
)


logger = logging.getLogger(__name__)


ENTRY_EXECUTE_FLAGS = (
    "EXECUTE",
    "EXECUTE_SCALPER",
    "EXECUTE_BTC_DAILY",
    "EXECUTE_BTC_5MIN",
    "EXECUTE_NEAR_RESOLUTION",
    "EXECUTE_NEWS_SHOCK",
    "EXECUTE_WALLET_FOLLOW",
    "EXECUTE_EXTERNAL_CONVICTION",
    "EXECUTE_SCANNER_EXECUTOR",
)


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


def _env_true(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


class RiskGate:
    """Pre-trade risk checks. Read once at construction; balance fetched on each call."""

    def __init__(
        self,
        trade_log: TradeLog,
        polymarket=None,
        starting_balance_usdc: Optional[float] = None,
        max_daily_loss_pct: Optional[float] = None,
        max_trades_per_hour: Optional[int] = None,
        max_open_positions: Optional[int] = None,
        min_usdc_floor: Optional[float] = None,
        max_daily_token_usd: Optional[float] = None,
        max_agent_allocation_fraction: Optional[float] = None,
        kill_switch_file: Optional[str] = None,
        llm_usage_file: Optional[str] = None,
        scalper_reserve_usdc: Optional[float] = None,
        swarm_reserve_usdc: Optional[float] = None,
        btc_daily_reserve_usdc: Optional[float] = None,
        near_resolution_reserve_usdc: Optional[float] = None,
        news_shock_reserve_usdc: Optional[float] = None,
        wallet_follow_reserve_usdc: Optional[float] = None,
        external_conviction_reserve_usdc: Optional[float] = None,
        btc_5min_reserve_usdc: Optional[float] = None,
        scanner_executor_reserve_usdc: Optional[float] = None,
        runtime_control_file: Optional[str] = None,
    ):
        self.trade_log = trade_log
        self.polymarket = polymarket
        self.starting_balance = (
            starting_balance_usdc
            if starting_balance_usdc is not None
            else _env_float("STARTING_BALANCE_USDC", 0.0)
        )
        if (
            starting_balance_usdc is None
            and self.starting_balance <= 0
            and any(_env_true(flag) for flag in ENTRY_EXECUTE_FLAGS)
        ):
            raise RuntimeError(
                "STARTING_BALANCE_USDC must be > 0 when any live entry EXECUTE flag is true"
            )
        self.max_daily_loss_pct = (
            max_daily_loss_pct
            if max_daily_loss_pct is not None
            else _env_float("MAX_DAILY_LOSS_PCT", 0.10)
        )
        self.max_trades_per_hour = (
            max_trades_per_hour
            if max_trades_per_hour is not None
            else _env_int("MAX_TRADES_PER_HOUR", MAX_TRADES_PER_HOUR)
        )
        self.max_agent_allocation_fraction = (
            max_agent_allocation_fraction
            if max_agent_allocation_fraction is not None
            else _env_float(
                "MAX_AGENT_ALLOCATION_FRACTION",
                MAX_AGENT_ALLOCATION_FRACTION,
            )
        )
        self.max_open_positions = (
            max_open_positions
            if max_open_positions is not None
            else _env_int("MAX_OPEN_POSITIONS", 10)
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
        self.runtime_control_file = Path(
            runtime_control_file
            or os.getenv("RUNTIME_CONTROL_PATH", "/app/data/runtime_control.json")
        )
        # Capital reservation ledger. Each strategy that draws from the
        # shared deposit-wallet pUSD pool gets a slice via a *_RESERVE_USDC
        # env var. `available_for_trader()` returns balance - sum(reserves)
        # so each strategy's effective bankroll is `slice` and they can't
        # accidentally overdraw each other. Adding a new strategy = one
        # env var + one entry here. See ~/Desktop/poly/CAPITAL_ALLOCATION.md.
        self.reserves = {
            "scalper": (
                scalper_reserve_usdc if scalper_reserve_usdc is not None
                else _env_float("SCALPER_RESERVE_USDC", 0.0)
            ),
            "swarm": (
                swarm_reserve_usdc if swarm_reserve_usdc is not None
                else _env_float("SWARM_RESERVE_USDC", 0.0)
            ),
            "btc_daily": (
                btc_daily_reserve_usdc if btc_daily_reserve_usdc is not None
                else _env_float("BTC_DAILY_RESERVE_USDC", 0.0)
            ),
            "near_resolution": (
                near_resolution_reserve_usdc if near_resolution_reserve_usdc is not None
                else _env_float("NEAR_RESOLUTION_RESERVE_USDC", 0.0)
            ),
            "news_shock": (
                news_shock_reserve_usdc if news_shock_reserve_usdc is not None
                else _env_float("NEWS_SHOCK_RESERVE_USDC", 0.0)
            ),
            "wallet_follow": (
                wallet_follow_reserve_usdc if wallet_follow_reserve_usdc is not None
                else _env_float("WALLET_FOLLOW_RESERVE_USDC", 0.0)
            ),
            "external_conviction": (
                external_conviction_reserve_usdc
                if external_conviction_reserve_usdc is not None
                else _env_float("EXTERNAL_CONVICTION_RESERVE_USDC", 0.0)
            ),
            "btc_5min": (
                btc_5min_reserve_usdc
                if btc_5min_reserve_usdc is not None
                else _env_float("BTC_5MIN_RESERVE_USDC", 0.0)
            ),
            "scanner_executor": (
                scanner_executor_reserve_usdc
                if scanner_executor_reserve_usdc is not None
                else _env_float("SCANNER_EXECUTOR_RESERVE_USDC", 0.0)
            ),
        }
        self._mtm_cache_value: Optional[float] = None
        self._mtm_cache_ts: float = 0.0
        self._mtm_cache_ttl: float = _env_float("MTM_CACHE_TTL_SEC", 60.0)

    def position_mtm_usd(self) -> float:
        """Mark-to-market value of all currently filled positions.

        Sums each filled position's shares * current midpoint. If midpoint
        lookup fails for a token, falls back to the entry price for that
        position (treats it as flat). Result is cached for `MTM_CACHE_TTL_SEC`.
        Returns 0.0 if no polymarket adapter or no client.
        """
        if self.polymarket is None or getattr(self.polymarket, "client", None) is None:
            return 0.0
        now = time.time()
        if (
            self._mtm_cache_value is not None
            and (now - self._mtm_cache_ts) < self._mtm_cache_ttl
        ):
            return self._mtm_cache_value
        positions = self.trade_log.filled_positions()
        total = 0.0
        for pos in positions:
            token_id = pos.get("token_id")
            side = pos.get("side")
            price = pos.get("price")
            size_usdc = pos.get("size_usdc")
            if not token_id or not side or price is None or not size_usdc:
                continue
            # `token_id` is already the exact CLOB outcome token we bought.
            # A recommendation with side=SELL means "bet against outcomes[0]",
            # implemented by buying outcomes[1] at order_price=(1-model_price).
            # TradeLog.price stores that actual token entry price, so MTM must
            # value shares against `price` for both BUY and SELL rows.
            entry_px = float(price)
            if entry_px <= 0:
                continue
            shares = size_usdc / entry_px
            try:
                mid_resp = self.polymarket.client.get_midpoint(token_id)
                if isinstance(mid_resp, dict):
                    mid = float(mid_resp.get("mid", entry_px))
                else:
                    mid = float(mid_resp)
            except Exception as e:
                logger.warning(
                    "risk_gate mtm: midpoint lookup failed for %s: %s; using entry",
                    token_id, e,
                )
                mid = entry_px
            total += shares * mid
        self._mtm_cache_value = total
        self._mtm_cache_ts = now
        return total

    def portfolio_value_usdc(self) -> float:
        """Poly1's slice of the shared wallet, computed from its own journal.

        Returns ``starting_balance - deployed_cost + position_mtm``.
        Equivalent to ``starting - (cost - mtm)`` = starting minus realized
        + unrealized loss on poly1's own positions.

        Why journal-based instead of on-chain cash:
        the deposit wallet is shared with the swarm bot. When swarm spends
        pUSD on its own fills, the on-chain cash balance drops without
        poly1 having lost anything. A cash-based portfolio calc would
        treat the swarm's normal trading activity as a poly1 drawdown
        and halt poly1 within minutes of swarm going live.

        Falls back to cash + mtm only when starting_balance is unset
        (legacy single-bot config).
        """
        if self.starting_balance <= 0:
            # Legacy fallback — single-bot semantics where cash IS poly1's bankroll.
            if self.polymarket is None:
                return 0.0
            try:
                cash = self.polymarket.get_usdc_balance()
            except Exception:
                return 0.0
            return cash + self.position_mtm_usd()

        deployed_cost = sum(
            float(p.get("size_usdc") or 0)
            for p in self.trade_log.filled_positions()
        )
        return self.starting_balance - deployed_cost + self.position_mtm_usd()

    @property
    def scalper_reserve(self) -> float:
        """Backwards-compat alias for callers that read this attribute directly."""
        return self.reserves.get("scalper", 0.0)

    @scalper_reserve.setter
    def scalper_reserve(self, value: float) -> None:
        """Backwards-compat setter for older tests/callers mutating the field."""
        self.reserves["scalper"] = float(value)

    @property
    def total_reserves(self) -> float:
        """Sum of all strategy reserves carved out of the shared pUSD pool."""
        return sum(self.reserves.values())

    def available_for_trader(self) -> float:
        if self.polymarket is None:
            return 0.0
        bal = self.polymarket.get_usdc_balance()
        available = bal - self.total_reserves
        if available <= 0:
            reserves_breakdown = ", ".join(
                f"{name}={reserve:.2f}" for name, reserve in self.reserves.items() if reserve > 0
            ) or "none"
            logger.warning(
                "available_for_trader <= 0: balance=%.4f total_reserves=%.4f reserves=[%s]",
                bal,
                self.total_reserves,
                reserves_breakdown,
            )
        return max(0.0, available)

    def reason(self) -> Optional[str]:
        """Return None if all gates pass, else a short string describing the first failure."""
        runtime_reason = self.runtime_control_reason()
        if runtime_reason:
            return runtime_reason

        if self.kill_switch_file.exists():
            return f"kill switch file present: {self.kill_switch_file}"

        pm_reason = self.position_manager_guard_reason()
        if pm_reason:
            return pm_reason

        if self.polymarket is not None:
            try:
                bal = self.polymarket.get_usdc_balance()
            except Exception as e:
                return f"balance read failed: {e}"
            available = max(0.0, bal - self.total_reserves)
            if available < self.min_usdc_floor:
                reserves_breakdown = ", ".join(
                    f"{k}={v:.2f}" for k, v in self.reserves.items() if v > 0
                ) or "none"
                return (
                    f"available {available:.4f} (after reserves [{reserves_breakdown}]) "
                    f"below floor {self.min_usdc_floor}"
                )
            if self.max_agent_allocation_fraction > 0:
                max_agent_reserve = bal * self.max_agent_allocation_fraction
                oversized = {
                    name: reserve
                    for name, reserve in self.reserves.items()
                    if reserve > max_agent_reserve
                }
                if oversized:
                    detail = ", ".join(
                        f"{name}={reserve:.2f}" for name, reserve in oversized.items()
                    )
                    return (
                        f"agent allocation above {self.max_agent_allocation_fraction:.0%} "
                        f"of wallet ${bal:.2f}: {detail}"
                    )
            if self.starting_balance > 0:
                # Drawdown is based on portfolio value (cash + MTM of open
                # positions), not cash alone. Otherwise capital deployed to
                # open positions reads as drawdown until they resolve.
                portfolio = self.portfolio_value_usdc()
                drawdown = (self.starting_balance - portfolio) / self.starting_balance
                if drawdown > self.max_daily_loss_pct:
                    return (
                        f"drawdown {drawdown:.2%} (portfolio ${portfolio:.2f} "
                        f"vs starting ${self.starting_balance:.2f}) above "
                        f"max_daily_loss_pct {self.max_daily_loss_pct:.2%}"
                    )

        recent = self.trade_log.count_recent((SUBMITTED, FILLED), hours=1)
        if recent >= self.max_trades_per_hour:
            return (
                f"submitted trades in last hour {recent} >= "
                f"max_trades_per_hour {self.max_trades_per_hour}"
            )

        open_positions = len(self.trade_log.filled_positions())
        if open_positions >= self.max_open_positions:
            return (
                f"open positions {open_positions} >= "
                f"max_open_positions {self.max_open_positions}"
            )

        if self.daily_token_usd() >= self.max_daily_token_usd:
            return (
                f"daily LLM cost ${self.daily_token_usd():.2f} >= "
                f"max ${self.max_daily_token_usd:.2f}"
            )

        return None

    def runtime_control_reason(self) -> Optional[str]:
        """Block entry when the runtime control-plane disallows this process.

        The control file lives on the shared data volume, so it protects against
        containers that were started with stale environment variables. A stale
        entry container will still read the latest control file before placing a
        trade.
        """
        if not self.runtime_control_file.exists():
            return None

        try:
            control = json.loads(self.runtime_control_file.read_text())
        except Exception as exc:
            return f"runtime control unreadable: {exc}"

        mode = str(control.get("mode") or "").strip()
        if mode == "freeze":
            return "runtime control mode=freeze blocks live entries"

        if mode not in {"paper", "live_probe", "live"}:
            return f"runtime control mode {mode!r} is not trade-enabled"

        expires_at = str(control.get("expires_at") or "").strip()
        if expires_at:
            try:
                expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) >= expires_dt:
                    return f"runtime control expired at {expires_at}"
            except ValueError:
                return f"runtime control expires_at invalid: {expires_at!r}"

        agent = os.getenv("RUNTIME_AGENT", "").strip()
        allowed = [str(a).strip() for a in control.get("allowed_live_agents") or []]
        if not agent:
            return "runtime control missing RUNTIME_AGENT"
        if agent not in allowed:
            return f"runtime control blocks agent {agent!r}; allowed={allowed}"

        expected_hash = str(control.get("config_hash") or "").strip()
        actual_hash = os.getenv("RUNTIME_CONFIG_HASH", "").strip()
        if expected_hash and actual_hash != expected_hash:
            return (
                "runtime config hash mismatch: "
                f"container={actual_hash or '<unset>'} control={expected_hash}"
            )

        return None

    def position_manager_guard_reason(self) -> Optional[str]:
        """Block live entries when the exit manager is disabled or stale."""
        if not any(_env_true(flag) for flag in ENTRY_EXECUTE_FLAGS):
            return None
        if not _env_true("EXECUTE_MAINTAIN"):
            return "EXECUTE_MAINTAIN must be true before live entries are allowed"
        heartbeat_path = Path(
            os.getenv(
                "MAINTAIN_HEARTBEAT_PATH",
                os.getenv("POSITION_MANAGER_HEARTBEAT_PATH", "./data/position_manager_heartbeat"),
            )
        )
        max_age = _env_float("POSITION_MANAGER_ENTRY_MAX_HEARTBEAT_AGE_SEC", 180.0)
        try:
            age = time.time() - heartbeat_path.stat().st_mtime
        except FileNotFoundError:
            return f"position_manager heartbeat missing: {heartbeat_path}"
        except OSError as exc:
            return f"position_manager heartbeat unreadable: {exc}"
        if age > max_age:
            return (
                f"position_manager heartbeat stale: age={age:.1f}s "
                f"max={max_age:.1f}s path={heartbeat_path}"
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
