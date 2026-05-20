#!/usr/bin/env python3
"""Runtime control-plane writer for poly1.

This is the single command surface for changing trading mode. It writes:

- deploy/.env.runtime: Docker env overrides, no secrets.
- data/runtime_control.json: volume-shared control file read by RiskGate.
- data/HALT: physical brake in freeze mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "deploy/runtime_policy.json"
ENV_RUNTIME_PATH = ROOT / "deploy/.env.runtime"
CONTROL_PATH = ROOT / "data/runtime_control.json"
HALT_PATH = ROOT / "data/HALT"


BASE_ENV = {
    "RUNTIME_CONTROL_PATH": "/app/data/runtime_control.json",
    "POLY1_REQUIRE_BRAIN_APPROVAL": "true",
    "POLY1_SOFT_STOP_LOSS_PCT": "0.03",
    "POLY1_STOP_LOSS_PCT": "0.06",
    "POLY1_PROFIT_TAKE_ALLOWED_PCT": "0.015",
    "POLY1_FAST_TAKE_PROFIT_PCT": "0.04",
    "POLY1_PREFERRED_TAKE_PROFIT_HIGH_PCT": "0.08",
    "POLY1_TAKE_PROFIT_CAP_PCT": "0.25",
    "POLY1_MAX_HOLD_SECONDS": "21600",
    "MARKET_BRAIN_ENABLED": "true",
    "MAX_TRADES_PER_HOUR": "100",
    "MAX_OPEN_POSITIONS": "10",
    "MAX_SCALP_TRADES_PER_HOUR": "100",
    "BTC_5MIN_MAX_PER_HOUR": "100",
    "BTC_5MIN_ASSETS": "btc,eth,sol,xrp,doge",
    "BTC_5MIN_REQUIRE_UNIVERSE_TOP": "false",
    "BTC_5MIN_MIN_UNIVERSE_WINRATE": "0.52",
    "BTC_5MIN_ENTRY_WINDOW_START": "20",
    "BTC_5MIN_ENTRY_WINDOW_END": "180",
    "BTC_5MIN_POLL_SEC": "1",
    "BTC_5MIN_COOLDOWN_SEC": "300",
    "BTC_5MIN_MOMENTUM_PCT": "0.0002",
    "BTC_5MIN_MIN_CONFIDENCE": "0.60",
    "BTC_5MIN_MIN_LIVE_ENTRY_PRICE": "0.40",
    "BTC_5MIN_MAX_LIVE_ENTRY_PRICE": "0.86",
    "BTC_5MIN_MIN_EDGE_PCT": "0.04",
    "BTC_5MIN_MIN_CONSENSUS": "2",
    "BTC_5MIN_NEWS_VETO": "true",
    "BTC_5MIN_MAX_HOLD_SECONDS": "120",
    "BTC_5MIN_TAKE_PROFIT_PCT": "0.05",
    "BTC_5MIN_STRADDLE_ENABLED": "false",
    "BTC_5MIN_STRADDLE_LEG_USDC": "1.50",
    "BTC_5MIN_STRADDLE_MAX_PAIR_ASK_SUM": "1.02",
    "BTC_5MIN_STRADDLE_TAKE_PROFIT_PCT": "0.03",
    "BTC_5MIN_STRADDLE_MAX_HOLD_SECONDS": "210",
    "BTC_5MIN_STRADDLE_MIN_SECONDS_TO_EXPIRY": "45",
    "BTC_5MIN_STRADDLE_MAX_ENTRY_SPREAD_PCT": "0.08",
    "BTC_5MIN_STRADDLE_MIN_ENTRY_PRICE": "0.05",
    "BTC_5MIN_STRADDLE_MIN_BID_DEPTH_USDC": "20.0",
    "MAX_ENTRY_SPREAD_PCT": "0.08",
    "MIN_BID_DEPTH_USDC": "20.0",
    "SCALP_EXIT_TAKE_PROFIT_PCT": "0.05",
    "SCALP_EXIT_TRAILING_STOP_PCT": "0.02",
    "SCALP_EXIT_STOP_LOSS_PCT": "0.03",
    "NEAR_RESOLUTION_MIN_CONFIDENCE": "0.65",
    "NEAR_RESOLUTION_DIRECTION_MIN_CONFIDENCE": "0.65",
    "MARKET_UNIVERSE_ASSETS": "btc,eth,sol,xrp,doge,bnb",
    "MARKET_UNIVERSE_HORIZONS": "5m,15m",
    "MARKET_UNIVERSE_PERIODS_AHEAD": "4",
    "MARKET_UNIVERSE_MIN_LIQUIDITY_USDC": "1500",
    "MARKET_UNIVERSE_MIN_WINRATE": "0.52",
    "MARKET_UNIVERSE_TOP_N": "10",
    "MARKET_UNIVERSE_POLL_SEC": "1",
    "MARKET_UNIVERSE_TRENDS_ENABLED": "true",
    "MARKET_UNIVERSE_TREND_EVERY_SEC": "180",
    "MARKET_UNIVERSE_TREND_LIMIT": "100",
    "MARKET_UNIVERSE_TREND_MIN_LIQUIDITY_USDC": "5000",
    "MARKET_UNIVERSE_TREND_MIN_VOLUME_24H_USDC": "1000",
    "MARKET_UNIVERSE_TREND_MAX_HOURS_TO_CLOSE": "24",
    "MARKET_UNIVERSE_TREND_TRADE_ENABLED": "false",
    "MARKET_UNIVERSE_WRITE_SCALPER_PAIRS": "true",
    "MARKET_BRAIN_GENERAL_MIN_SCORE": "0.52",
    "MARKET_BRAIN_SCALPER_MIN_EDGE_SCORE": "0.52",
    "MARKET_BRAIN_CRYPTO_STRADDLE_MIN_ENTRY_PRICE": "0.05",
    "MARKET_BRAIN_CRYPTO_STRADDLE_MAX_ENTRY_PRICE": "0.98",
    "MARKET_BRAIN_CRYPTO_STRADDLE_MAX_PAIR_ASK_SUM": "1.04",
    "MARKET_BRAIN_EXIT_TAKE_PROFIT_PCT": "0.25",
    "MARKET_BRAIN_EXIT_SOFT_STOP_LOSS_PCT": "0.03",
    "MARKET_BRAIN_EXIT_STOP_LOSS_PCT": "0.06",
    "MARKET_BRAIN_EXIT_TRAILING_STOP_PCT": "0.02",
    "MARKET_BRAIN_SMART_EXIT_ENABLED": "true",
    "MARKET_BRAIN_SMART_EXIT_MIN_PROFIT_PCT": "0.015",
    "MARKET_BRAIN_PREFERRED_TAKE_PROFIT_PCT": "0.04",
    "SCALPER_REQUIRE_UNIVERSE_TOP": "true",
    "SCALPER_MIN_UNIVERSE_WINRATE": "0.52",
    "EXTERNAL_CONVICTION_MIN_CONFIDENCE": "0.65",
    "EXTERNAL_CONVICTION_PROVIDER": "aggregator",
    "EXTERNAL_CONVICTION_ALLOW_WEAK_PROVIDERS": "false",
    "EXTERNAL_CONVICTION_AGGREGATOR_PROVIDERS": "manifold,metaculus,kalshi,technical_signal,clob_whale",
    "PROVIDER_SCORECARD_PATH": "/app/data/provider_scorecard.json",
    "PROVIDER_SCORECARD_MIN_MATCHED": "10",
    "PROVIDER_SCORECARD_MIN_WINRATE": "0.55",
    "SCALP_DISCOVER_EVERY_SEC": "1",
    "MAX_AGENT_ALLOCATION_FRACTION": "0.50",
    "META_BRAIN_MIN_WEIGHTED_SCORE": "0.50",
    "META_BRAIN_MIN_EDGE_PCT": "0.02",
    "META_BRAIN_MIN_RAW_EV": "0.04",
    "META_BRAIN_EXECUTION_QUALITY_ENABLED": "true",
    "META_BRAIN_EXECUTION_QUALITY_FAIL_CLOSED": "false",
    "META_BRAIN_EXECUTION_QUALITY_USDC": "3.0",
    "EXECUTION_QUALITY_REQUIRE_FRESH": "true",
    "EXECUTION_QUALITY_MAX_AGE_SEC": "10",
    "EXECUTION_QUALITY_MAX_SPREAD_PCT": "0.08",
    "EXECUTION_QUALITY_MIN_BID_DEPTH_USDC": "20",
    "EXECUTION_QUALITY_MAX_AVG_SLIPPAGE_PCT": "0.025",
    "EXECUTION_QUALITY_MIN_SCORE": "0.65",
    "EXECUTION_QUALITY_FEE_BUFFER_PCT": "0.01",
    "EXECUTION_QUALITY_MIN_NET_EV": "0.02",
    "ORDERBOOK_MONITOR_POLL_SEC": "1",
    "ORDERBOOK_MONITOR_TOKEN_LIMIT": "80",
    "ORDERBOOK_MONITOR_PRUNE_MINUTES": "180",
    "ORDERBOOK_MONITOR_STALE_MARKET_GRACE_SEC": "300",
    "SCANNER_EXECUTOR_POLL_SEC": "2",
    "SCANNER_EXECUTOR_MAX_DECISION_AGE_SEC": "180",
    "SCANNER_EXECUTOR_BATCH_LIMIT": "50",
    "SCANNER_EXECUTOR_POSITION_SIZE_USDC": "1.0",
    "SCANNER_EXECUTOR_MIN_SCORE": "0.80",
    "SCANNER_EXECUTOR_MIN_RAW_EV": "0.04",
    "SCANNER_EXECUTOR_MIN_NET_EV": "0.03",
    "SCANNER_EXECUTOR_ROUND_TRIP_COST_PCT": "0.04",
    "DECISION_COUNCIL_MIN_NET_EV": "0.04",
    "DECISION_COUNCIL_EXPERT_MIN_NET_EV": "0.025",
    "DECISION_COUNCIL_THIN_MIN_NET_EV": "0.06",
    "DECISION_COUNCIL_MIN_PROBABILITY": "0.52",
    "DECISION_COUNCIL_EXPERT_MIN_PROBABILITY": "0.50",
    "DECISION_COUNCIL_THIN_LIQUIDITY_USDC": "5000",
    "SCANNER_EXECUTOR_MAX_ENTRY_DRIFT_PCT": "0.04",
    "SCANNER_EXECUTOR_REQUIRE_TIMING_NOW": "true",
    "SCANNER_EXECUTOR_ALLOW_WAIT_WITH_HIGH_SCORE": "false",
    "SCANNER_EXECUTOR_WAIT_OVERRIDE_MIN_SCORE": "0.79",
    "SCANNER_EXECUTOR_MAX_OPEN": "4",
    "SCANNER_EXECUTOR_REENTRY_COOLDOWN_HOURS": "12",
    "PREFLIGHT_MAX_DISK_USED_PCT": "85",
    "PREFLIGHT_REQUIRE_DB_BACKUP": "true",
    "PREFLIGHT_MAX_BACKUP_AGE_HOURS": "30",
    "META_BRAIN_WINRATE_PRIOR": "0.50",
    "META_BRAIN_ANCHOR_THRESHOLD": "0.70",
    "META_BRAIN_MIN_WEIGHTED_SCORE_ANCHOR": "0.40",
    "META_BRAIN_WEIGHT_NEWS": "0.10",
    "EXPERT_RELIABILITY_HOURS": "720",
    "EXPERT_SOLO_MIN_PROB": "0.65",
    "EXPERT_SOLO_MIN_WINRATE": "0.65",
    "EXPERT_SOLO_MIN_WILSON": "0.58",
    "EXPERT_SOLO_MIN_SAMPLES": "30",
    "EXPERT_SOLO_MAX_AGE_SEC": "3600",
    "EXPERT_WALLET_EXTERNAL_MIN_WINRATE": "0.70",
    "EXPERT_WALLET_EXTERNAL_MIN_TRADES": "50",
    "EXPERT_WALLET_EXTERNAL_MIN_PROFIT_USDC": "100",
    "EXPERT_CONFLICT_MIN_PROB": "0.62",
    "EXPERT_CONFLICT_MIN_WINRATE": "0.58",
    "EXPERT_CONFLICT_MIN_SAMPLES": "15",
    "META_BRAIN_MIN_WINRATE_SAMPLES": "5",
    "META_BRAIN_CRYPTO_STRADDLE_MIN_SCORE": "0.52",
    "TAVILY_ENABLED": "false",
    "TAVILY_API_KEY": "",
    "TAVILY_DAILY_LIMIT": "50",
    "TAVILY_CACHE_TTL_SEC": "3600",
    "META_BRAIN_STRADDLE_TAVILY_ENABLED": "false",
    "META_BRAIN_STRADDLE_LLM_ENABLED": "true",
    "HERMES_FORECAST_URL": "http://hermes-forecast:8097/forecast",
    "HERMES_TIMEOUT_SEC": "8",
    "HERMES_ANTHROPIC_TIMEOUT_SEC": "12",
    "META_BRAIN_STRADDLE_WEIGHT_BRAIN": "0.30",
    "META_BRAIN_STRADDLE_WEIGHT_WINRATE": "0.25",
    "META_BRAIN_STRADDLE_WEIGHT_TAVILY": "0.00",
    "META_BRAIN_STRADDLE_WEIGHT_TRADINGVIEW": "0.10",
    "META_BRAIN_STRADDLE_WEIGHT_HERMES": "0.15",
    "META_BRAIN_STRADDLE_WEIGHT_CONVICTION": "0.05",
    "META_BRAIN_STRADDLE_WEIGHT_VELOCITY": "0.03",
    "META_BRAIN_STRADDLE_WEIGHT_LIQUIDITY": "0.02",
    "TRADINGVIEW_OPTIONS_CHAIN_URL": "https://www.tradingview.com/options/chain/?symbol=CME_MINI%3AES1%21",
    "TRADINGVIEW_OPTIONS_SNAPSHOT_PATH": "/app/data/tradingview_options_es1_snapshot.json",
    "TRADINGVIEW_OPTIONS_MAX_AGE_SEC": "900",
    "KELLY_SIZING_ENABLED": "true",
    "KELLY_FRACTION_SCALE": "0.25",
    "KELLY_MIN_POSITION_USDC": "0",
    "MAINTAIN_TAKE_PROFIT_PCT": "0.25",
    "MAINTAIN_SOFT_STOP_LOSS_PCT": "0.03",
    "MAINTAIN_STOP_LOSS_PCT": "0.06",
    "MAINTAIN_PROFIT_TAKE_ALLOWED_PCT": "0.015",
    "MAINTAIN_PREFERRED_TAKE_PROFIT_PCT": "0.04",
    "MAINTAIN_PREFERRED_TAKE_PROFIT_HIGH_PCT": "0.08",
    "MAINTAIN_IMMEDIATE_REVIEW_MOVE_PCT": "0.02",
    "MAINTAIN_TRAILING_STOP_PCT": "0.02",
    "MAINTAIN_MAX_HOLD_HOURS": "6",
    "MAINTAIN_POLL_SEC": "10",
    "MAINTAIN_LLM_EXIT_INTERVAL_SEC": "60",
    "MAINTAIN_PARTIAL_TAKE_PROFIT_ENABLED": "true",
    "MAINTAIN_PARTIAL_TAKE_PROFIT_PCT": "0.10",
    "MAINTAIN_PARTIAL_TAKE_PROFIT_FRACTION": "0.50",
    "MAINTAIN_PARTIAL_TAKE_PROFIT_MIN_POSITION_USDC": "500.0",
    "MAINTAIN_MIN_EXIT_NOTIONAL_USDC": "0.50",
    "MAINTAIN_MIN_TAKE_PROFIT_NET_PCT": "0.015",
    "MAINTAIN_MIN_TAKE_PROFIT_USDC": "0.01",
    "TELEGRAM_REPORT_SECONDS": "3600",
    "TELEGRAM_DIRECT_NOTIFICATIONS": "false",
    "TELEGRAM_TRADE_ALERTS": "true",
    "TELEGRAM_REPORT_SEND_ON_START": "false",
    "TELEGRAM_CRITICAL_MIN_INTERVAL_SEC": "900",
    "ALLOC_SYNC_ENFORCE": "false",
    "ALLOCATOR_EXPLORATION_USDC": "0",
    "WALLET_SCOUT_ENABLE": "false",
    "SWARM_RESERVE_USDC": "0",
    "SCALPER_RESERVE_USDC": "0",
    "BTC_DAILY_RESERVE_USDC": "0",
    "NEAR_RESOLUTION_RESERVE_USDC": "0",
    "NEWS_SHOCK_RESERVE_USDC": "0",
    "WALLET_FOLLOW_RESERVE_USDC": "0",
    "EXTERNAL_CONVICTION_RESERVE_USDC": "0",
    "BTC_5MIN_RESERVE_USDC": "0",
    "SCANNER_EXECUTOR_RESERVE_USDC": "0",
    "EXECUTE": "false",
    "EXECUTE_SCALPER": "false",
    "EXECUTE_BTC_DAILY": "false",
    "EXECUTE_BTC_5MIN": "false",
    "EXECUTE_NEAR_RESOLUTION": "false",
    "EXECUTE_NEWS_SHOCK": "false",
    "EXECUTE_WALLET_FOLLOW": "false",
    "EXECUTE_EXTERNAL_CONVICTION": "false",
    "EXECUTE_SCANNER_EXECUTOR": "false",
    "EXECUTE_MAINTAIN": "true",
    "TRADING_SUPERVISOR_ENFORCE_HALT": "true",
    "TRADING_SUPERVISOR_EVAL_GRACE_SEC": "180",
    "TRADING_SUPERVISOR_STALE_HEARTBEAT_SEC": "180",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_policy() -> dict:
    return json.loads(POLICY_PATH.read_text())


def _hash_payload(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _write_env(env: dict[str, str]) -> None:
    lines = [
        "# Generated by scripts/runtime_control.py. No secrets here.",
        "# Do not edit by hand; use runtime_control.py freeze/live-probe.",
        "",
    ]
    for key in sorted(env):
        lines.append(f'{key}="{env[key]}"')
    ENV_RUNTIME_PATH.write_text("\n".join(lines) + "\n")


def _write_control(control: dict) -> None:
    CONTROL_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTROL_PATH.write_text(json.dumps(control, indent=2, sort_keys=True) + "\n")


def _set_halt(message: str) -> None:
    HALT_PATH.parent.mkdir(parents=True, exist_ok=True)
    HALT_PATH.write_text(message.rstrip() + "\n")


def freeze(args: argparse.Namespace) -> int:
    env = dict(BASE_ENV)
    control_base = {
        "mode": "freeze",
        "allowed_live_agents": [],
        "requires_halt": True,
    }
    config_hash = _hash_payload({"env": env, "control": control_base})
    env["RUNTIME_MODE"] = "freeze"
    env["RUNTIME_CONFIG_HASH"] = config_hash
    control = {
        **control_base,
        "config_hash": config_hash,
        "updated_at": _utc_now(),
        "updated_by": "scripts/runtime_control.py freeze",
        "note": args.note or "stability freeze; live entries disabled",
    }
    _write_env(env)
    _write_control(control)
    _set_halt(
        "HALT set by runtime_control.py freeze: no live entry agents until "
        "runtime_control.py live-probe and preflight --mode live pass."
    )
    print(f"runtime mode set: freeze hash={config_hash}")
    print(f"wrote {ENV_RUNTIME_PATH}")
    print(f"wrote {CONTROL_PATH}")
    print(f"wrote {HALT_PATH}")
    return 0


def live_probe(args: argparse.Namespace) -> int:
    policy = _load_policy()
    agents = policy["entry_agents"]
    if args.agent not in agents:
        valid = ", ".join(sorted(agents))
        raise SystemExit(f"unknown agent {args.agent!r}; valid: {valid}")

    budget = float(args.budget)
    if budget <= 0:
        raise SystemExit("--budget must be positive")

    env = dict(BASE_ENV)
    env["RUNTIME_MODE"] = "live_probe"
    env["EXECUTE"] = "true"
    spec = agents[args.agent]
    env[spec["execute_flag"]] = "true"
    if spec.get("reserve_flag"):
        env[spec["reserve_flag"]] = str(budget)

    control_base = {
        "mode": "live_probe",
        "allowed_live_agents": [args.agent],
        "budget_usdc": budget,
        "requires_halt": False,
    }
    config_hash = _hash_payload({"env": env, "control": control_base})
    env["RUNTIME_CONFIG_HASH"] = config_hash
    control = {
        **control_base,
        "config_hash": config_hash,
        "updated_at": _utc_now(),
        "updated_by": "scripts/runtime_control.py live-probe",
        "note": args.note or f"live probe for {args.agent}",
    }
    _write_env(env)
    _write_control(control)
    if args.arm:
        HALT_PATH.unlink(missing_ok=True)
    print(f"runtime mode set: live_probe agent={args.agent} budget={budget} hash={config_hash}")
    print(f"wrote {ENV_RUNTIME_PATH}")
    print(f"wrote {CONTROL_PATH}")
    if args.arm:
        print(f"removed {HALT_PATH}")
    else:
        print(f"left {HALT_PATH} unchanged; pass --arm only after approval")
    return 0


def shadow_probe(args: argparse.Namespace) -> int:
    policy = _load_policy()
    agents = policy["entry_agents"]
    if args.agent not in agents:
        valid = ", ".join(sorted(agents))
        raise SystemExit(f"unknown agent {args.agent!r}; valid: {valid}")
    duration_minutes = int(args.minutes)
    if duration_minutes <= 0 or duration_minutes > 180:
        raise SystemExit("--minutes must be between 1 and 180")

    env = dict(BASE_ENV)
    env["RUNTIME_MODE"] = "paper"
    env["EXECUTE"] = "false"
    spec = agents[args.agent]
    env[spec["execute_flag"]] = "false"
    if spec.get("reserve_flag"):
        env[spec["reserve_flag"]] = "0"
    if args.scanner_allow_wait:
        env["SCANNER_EXECUTOR_ALLOW_WAIT_WITH_HIGH_SCORE"] = "true"
        env["SCANNER_EXECUTOR_WAIT_OVERRIDE_MIN_SCORE"] = args.scanner_wait_min_score
        env["SCANNER_EXECUTOR_MIN_SCORE"] = args.scanner_wait_min_score
    if args.position_size_usdc:
        env["SCANNER_EXECUTOR_POSITION_SIZE_USDC"] = args.position_size_usdc

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
    control_base = {
        "mode": "paper",
        "allowed_live_agents": [args.agent],
        "budget_usdc": 0.0,
        "expires_at": expires_at.isoformat(),
        "requires_halt": False,
        "shadow_only": True,
    }
    config_hash = _hash_payload({"env": env, "control": control_base})
    env["RUNTIME_CONFIG_HASH"] = config_hash
    control = {
        **control_base,
        "config_hash": config_hash,
        "updated_at": _utc_now(),
        "updated_by": "scripts/runtime_control.py shadow-probe",
        "note": args.note or f"shadow probe for {args.agent}; no live entries",
    }
    _write_env(env)
    _write_control(control)
    if args.arm:
        HALT_PATH.unlink(missing_ok=True)
    print(
        f"runtime mode set: paper shadow agent={args.agent} "
        f"minutes={duration_minutes} hash={config_hash}"
    )
    print(f"expires_at={expires_at.isoformat()}")
    print(f"wrote {ENV_RUNTIME_PATH}")
    print(f"wrote {CONTROL_PATH}")
    if args.arm:
        print(f"removed {HALT_PATH}; execute flags remain false")
    else:
        print(f"left {HALT_PATH} unchanged; pass --arm to let RiskGate allow shadow")
    return 0


def live_hour(args: argparse.Namespace) -> int:
    policy = _load_policy()
    all_agents = policy["entry_agents"]
    requested = [
        item.strip()
        for item in (args.agents.split(",") if args.agents else all_agents.keys())
        if item.strip()
    ]
    unknown = [agent for agent in requested if agent not in all_agents]
    if unknown:
        valid = ", ".join(sorted(all_agents))
        raise SystemExit(f"unknown agents {unknown!r}; valid: {valid}")

    duration_minutes = int(args.minutes)
    if duration_minutes <= 0 or duration_minutes > 60:
        raise SystemExit("--minutes must be between 1 and 60")
    budget = float(args.budget)
    if budget <= 0:
        raise SystemExit("--budget must be positive")
    wallet_balance = float(args.wallet_balance)
    if wallet_balance <= 0:
        raise SystemExit("--wallet-balance must be positive")
    equity_balance = (
        float(args.equity_balance)
        if args.equity_balance is not None
        else wallet_balance
    )
    if equity_balance <= 0:
        raise SystemExit("--equity-balance must be positive")

    env = dict(BASE_ENV)
    env["RUNTIME_MODE"] = "live"
    env["EXECUTE"] = "true"
    env["MAX_OPEN_POSITIONS"] = str(int(args.max_open))
    env["STARTING_BALANCE_USDC"] = f"{wallet_balance:.4f}"
    # RiskGate subtracts strategy reserves before comparing to MIN_USDC_FLOOR.
    # To allow exactly `budget` of real spending while reserves sum to `budget`,
    # the post-reserve floor must be wallet - 2*budget.
    env["MIN_USDC_FLOOR"] = f"{max(0.0, wallet_balance - (2 * budget)):.4f}"
    env["MAX_POSITION_FRACTION"] = args.max_position_fraction
    env["MAX_DAILY_TOKEN_USD"] = args.max_daily_token_usd
    if args.scanner_allow_wait:
        env["SCANNER_EXECUTOR_ALLOW_WAIT_WITH_HIGH_SCORE"] = "true"
        env["SCANNER_EXECUTOR_WAIT_OVERRIDE_MIN_SCORE"] = args.scanner_wait_min_score
        # The executor applies the general score gate before the timing override
        # can help. Keep both thresholds aligned for controlled wait probes.
        env["SCANNER_EXECUTOR_MIN_SCORE"] = args.scanner_wait_min_score
    env["BTC_DAILY_POSITION_SIZE_USDC"] = args.position_size_usdc
    env["BTC_5MIN_POSITION_SIZE_USDC"] = args.position_size_usdc
    env["BTC_5MIN_STRADDLE_LEG_USDC"] = args.position_size_usdc
    env["NEAR_RESOLUTION_POSITION_SIZE_USDC"] = args.position_size_usdc
    env["NEWS_SHOCK_POSITION_SIZE_USDC"] = args.position_size_usdc
    env["WALLET_FOLLOW_POSITION_SIZE_USDC"] = args.position_size_usdc
    env["EXTERNAL_CONVICTION_POSITION_SIZE_USDC"] = args.position_size_usdc
    env["SCANNER_EXECUTOR_POSITION_SIZE_USDC"] = args.position_size_usdc
    env["EXTERNAL_CONVICTION_MAX_OPEN_POSITIONS"] = str(int(args.max_open))
    env["NEAR_RESOLUTION_MAX_OPEN"] = str(int(args.max_open))
    env["NEWS_SHOCK_MAX_OPEN"] = str(int(args.max_open))
    env["WALLET_FOLLOW_MAX_OPEN"] = str(int(args.max_open))

    reserve_agents = [agent for agent in requested if all_agents[agent].get("reserve_flag")]
    reserve_each = budget / len(reserve_agents) if reserve_agents else 0.0
    for agent in requested:
        spec = all_agents[agent]
        env[spec["execute_flag"]] = "true"
        if spec.get("reserve_flag"):
            env[spec["reserve_flag"]] = f"{reserve_each:.4f}"

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
    control_base = {
        "mode": "live",
        "allowed_live_agents": requested,
        "budget_usdc": budget,
        "wallet_balance_at_start_usdc": wallet_balance,
        "equity_at_start_usdc": equity_balance,
        "max_open_positions": int(args.max_open),
        "expires_at": expires_at.isoformat(),
        "requires_halt": False,
    }
    config_hash = _hash_payload({"env": env, "control": control_base})
    env["RUNTIME_CONFIG_HASH"] = config_hash
    control = {
        **control_base,
        "config_hash": config_hash,
        "updated_at": _utc_now(),
        "updated_by": "scripts/runtime_control.py live-hour",
        "note": args.note or f"one-hour live test budget ${budget:.2f}",
    }
    _write_env(env)
    _write_control(control)
    if args.arm:
        HALT_PATH.unlink(missing_ok=True)
    print(
        "runtime mode set: live "
        f"agents={','.join(requested)} budget={budget:.2f} "
        f"minutes={duration_minutes} hash={config_hash}"
    )
    print(f"expires_at={expires_at.isoformat()}")
    print(f"reserve_each={reserve_each:.4f}")
    print(f"wrote {ENV_RUNTIME_PATH}")
    print(f"wrote {CONTROL_PATH}")
    if args.arm:
        print(f"removed {HALT_PATH}")
    else:
        print(f"left {HALT_PATH} unchanged; pass --arm only after approval")
    return 0


def status(_: argparse.Namespace) -> int:
    print(f"env_runtime_exists={ENV_RUNTIME_PATH.exists()} path={ENV_RUNTIME_PATH}")
    print(f"control_exists={CONTROL_PATH.exists()} path={CONTROL_PATH}")
    print(f"halt_exists={HALT_PATH.exists()} path={HALT_PATH}")
    if CONTROL_PATH.exists():
        print(CONTROL_PATH.read_text().rstrip())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_freeze = sub.add_parser("freeze")
    p_freeze.add_argument("--note", default="")
    p_freeze.set_defaults(func=freeze)

    p_live = sub.add_parser("live-probe")
    p_live.add_argument("--agent", required=True)
    p_live.add_argument("--budget", type=float, default=5.0)
    p_live.add_argument("--note", default="")
    p_live.add_argument("--arm", action="store_true", help="remove HALT after writing live control")
    p_live.set_defaults(func=live_probe)

    p_shadow = sub.add_parser("shadow-probe")
    p_shadow.add_argument("--agent", required=True)
    p_shadow.add_argument("--minutes", type=int, default=30)
    p_shadow.add_argument("--position-size-usdc", default="1.00")
    p_shadow.add_argument("--scanner-allow-wait", action="store_true")
    p_shadow.add_argument("--scanner-wait-min-score", default="0.79")
    p_shadow.add_argument("--note", default="")
    p_shadow.add_argument("--arm", action="store_true", help="remove HALT; execute flags stay false")
    p_shadow.set_defaults(func=shadow_probe)

    p_live_hour = sub.add_parser("live-hour")
    p_live_hour.add_argument("--budget", type=float, required=True)
    p_live_hour.add_argument("--wallet-balance", type=float, required=True)
    p_live_hour.add_argument("--equity-balance", type=float, default=None)
    p_live_hour.add_argument("--minutes", type=int, default=60)
    p_live_hour.add_argument("--max-open", type=int, default=100)
    p_live_hour.add_argument("--agents", default="")
    p_live_hour.add_argument("--max-position-fraction", default="0.03")
    p_live_hour.add_argument("--max-daily-token-usd", default="10.0")
    p_live_hour.add_argument("--position-size-usdc", default="1.50")
    p_live_hour.add_argument("--scanner-allow-wait", action="store_true")
    p_live_hour.add_argument("--scanner-wait-min-score", default="0.79")
    p_live_hour.add_argument("--note", default="")
    p_live_hour.add_argument("--arm", action="store_true", help="remove HALT after writing live control")
    p_live_hour.set_defaults(func=live_hour)

    p_status = sub.add_parser("status")
    p_status.set_defaults(func=status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
