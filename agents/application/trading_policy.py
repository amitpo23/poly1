"""Canonical trading policy shared by entry, exit, and ops agents.

This module is intentionally boring: one place for the rules that must not
drift between agents. Strategy modules can be creative about signals, but risk
limits and agent identities live here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


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


STOP_LOSS_PCT = 0.03
FAST_TAKE_PROFIT_PCT = 0.05
TAKE_PROFIT_CAP_PCT = 0.25
# Hard safety ceiling only. The strategy preference is to exit as quickly as
# the brain can justify, not to wait for this timeout.
MAX_HOLD_SECONDS = 6 * 3600
POSITION_POLL_SECONDS = 60
MARKET_SCAN_SECONDS = 60
TELEGRAM_REPORT_SECONDS = 3600
MAX_TRADES_PER_HOUR = 100
MAX_AGENT_ALLOCATION_FRACTION = 0.50
REQUIRE_BRAIN_APPROVAL = True


@dataclass(frozen=True)
class TradingPolicy:
    """Resolved policy values after environment overrides."""

    stop_loss_pct: float = STOP_LOSS_PCT
    fast_take_profit_pct: float = FAST_TAKE_PROFIT_PCT
    take_profit_cap_pct: float = TAKE_PROFIT_CAP_PCT
    max_hold_seconds: int = MAX_HOLD_SECONDS
    position_poll_seconds: int = POSITION_POLL_SECONDS
    market_scan_seconds: int = MARKET_SCAN_SECONDS
    telegram_report_seconds: int = TELEGRAM_REPORT_SECONDS
    max_trades_per_hour: int = MAX_TRADES_PER_HOUR
    max_agent_allocation_fraction: float = MAX_AGENT_ALLOCATION_FRACTION
    require_brain_approval: bool = REQUIRE_BRAIN_APPROVAL

    @classmethod
    def from_env(cls) -> "TradingPolicy":
        return cls(
            stop_loss_pct=_env_float("POLY1_STOP_LOSS_PCT", STOP_LOSS_PCT),
            fast_take_profit_pct=_env_float(
                "POLY1_FAST_TAKE_PROFIT_PCT", FAST_TAKE_PROFIT_PCT
            ),
            take_profit_cap_pct=_env_float(
                "POLY1_TAKE_PROFIT_CAP_PCT", TAKE_PROFIT_CAP_PCT
            ),
            max_hold_seconds=_env_int("POLY1_MAX_HOLD_SECONDS", MAX_HOLD_SECONDS),
            position_poll_seconds=_env_int(
                "POLY1_POSITION_POLL_SECONDS", POSITION_POLL_SECONDS
            ),
            market_scan_seconds=_env_int(
                "POLY1_MARKET_SCAN_SECONDS", MARKET_SCAN_SECONDS
            ),
            telegram_report_seconds=_env_int(
                "TELEGRAM_REPORT_SECONDS", TELEGRAM_REPORT_SECONDS
            ),
            max_trades_per_hour=_env_int(
                "MAX_TRADES_PER_HOUR", MAX_TRADES_PER_HOUR
            ),
            max_agent_allocation_fraction=_env_float(
                "MAX_AGENT_ALLOCATION_FRACTION", MAX_AGENT_ALLOCATION_FRACTION
            ),
            require_brain_approval=os.getenv(
                "POLY1_REQUIRE_BRAIN_APPROVAL", "true"
            ).lower() in {"1", "true", "yes", "on"},
        )


AGENT_MANIFEST: dict[str, dict[str, str]] = {
    "meta_brain": {
        "role": "final entry brain",
        "strategy": "Fuse MarketBrain, Gamma context, cross-market conviction, win-rate, and velocity before any main entry.",
        "places_orders": "no",
    },
    "trader": {
        "role": "main LLM trader",
        "strategy": "Psychological/crowd mispricing trades only after MetaBrain approval.",
        "places_orders": "yes",
    },
    "market_scanner": {
        "role": "opportunity router",
        "strategy": "Scan Gamma and external evidence, then write opportunities for entry agents.",
        "places_orders": "no",
    },
    "scanner_executor": {
        "role": "scanner execution bridge",
        "strategy": "Consume fresh market_scanner brain approvals only when execution metadata, live order book, EV, dedupe, and RiskGate all pass.",
        "places_orders": "yes",
    },
    "position_manager": {
        "role": "exit brain",
        "strategy": "Re-evaluate exits every minute; exit fast by default, hold only on strong brain/forecast evidence, enforce 3% stop and 25% hard cap.",
        "places_orders": "sell only",
    },
    "risk_gate": {
        "role": "capital guard",
        "strategy": "Block entries on drawdown, stale runtime mode, reserves, overtrading, open-position limits, and >50% wallet allocation to one agent.",
        "places_orders": "no",
    },
    "trading_supervisor": {
        "role": "ops watchdog",
        "strategy": "Detect stale heartbeats, stuck positions, close failures, and unsafe runtime drift.",
        "places_orders": "no",
    },
    "scalper": {
        "role": "15m crypto scalper",
        "strategy": "Enter only mathematically cheap UP/DOWN legs and exit quickly through MarketBrain.",
        "places_orders": "yes",
    },
    "btc_5min": {
        "role": "5m BTC momentum/reversal",
        "strategy": "Short-horizon BTC signal consensus with MarketBrain sanity gate.",
        "places_orders": "yes",
    },
    "btc_daily": {
        "role": "daily BTC mean reversion",
        "strategy": "Fade daily overreaction only when signal and brain agree; exits handled centrally.",
        "places_orders": "yes",
    },
    "near_resolution": {
        "role": "near-resolution event trader",
        "strategy": "Trade high-confidence markets close to resolution with strict liquidity and risk gates.",
        "places_orders": "yes",
    },
    "news_signal": {
        "role": "news ingestion",
        "strategy": "Find material market-moving headlines and journal signals.",
        "places_orders": "no",
    },
    "news_shock": {
        "role": "news reaction trader",
        "strategy": "Act on fresh material news before the market fully reprices.",
        "places_orders": "yes",
    },
    "wallet_watcher": {
        "role": "wallet intelligence",
        "strategy": "Track wallets and produce follow signals.",
        "places_orders": "no",
    },
    "wallet_follow": {
        "role": "wallet-follow trader",
        "strategy": "Mirror proven wallets only when EV, drift, and liquidity gates pass.",
        "places_orders": "yes",
    },
    "external_conviction": {
        "role": "external conviction family",
        "strategy": "Manifold, Metaculus, Kalshi, news, technical, whale, and aggregator evidence.",
        "places_orders": "signal; api variant can enter",
    },
    "settlement_reconciler": {
        "role": "settlement recovery",
        "strategy": "Detect resolved markets and reconcile/redeem recoverable positions.",
        "places_orders": "no",
    },
    "allocator_sync": {
        "role": "capital allocation sync",
        "strategy": "Keep per-agent reserves aligned with realized performance and runtime policy.",
        "places_orders": "no",
    },
    "telegram_reporter": {
        "role": "operator dashboard",
        "strategy": "Send concise hourly status with wallet, agents, positions, exits, errors, and commands.",
        "places_orders": "no",
    },
}
