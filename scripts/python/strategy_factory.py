#!/usr/bin/env python3
"""Strategy Factory — poly1 management CLI.

Lists all trading strategies in this repo, shows their live/shadow status,
and lets the operator enable or disable strategies quickly without touching
docker-compose.yml manually.

Usage:
  python scripts/python/strategy_factory.py list
  python scripts/python/strategy_factory.py status
  python scripts/python/strategy_factory.py enable near-resolution
  python scripts/python/strategy_factory.py disable news-shock
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------

# Each strategy entry describes how the strategy is identified in the system.
STRATEGIES = [
    {
        "name": "trader",
        "description": "Main LLM trader — sweeps Gamma for opportunities every cycle",
        "execute_env": "EXECUTE",
        "module": "agents.application.executor",
        "daemon_class": None,
        "heartbeat": "data/heartbeat",
        "docker_service": "trader",
        "profile": None,  # always active (no profile guard)
    },
    {
        "name": "btc-daily",
        "description": "BTC Up/Down mean-reversion agent (no LLM, 3-min candles)",
        "execute_env": "EXECUTE_BTC_DAILY",
        "module": "agents.application.btc_daily",
        "daemon_class": "BtcDailyDaemon",
        "heartbeat": "data/btc_daily_heartbeat",
        "docker_service": "btc-daily",
        "profile": "btc_daily",
    },
    {
        "name": "near-resolution",
        "description": "Near-resolution agent — targets cheap tokens <36h from close",
        "execute_env": "EXECUTE_NEAR_RESOLUTION",
        "module": "agents.application.near_resolution",
        "daemon_class": "NearResolutionDaemon",
        "heartbeat": "data/near_resolution_heartbeat",
        "docker_service": "near-resolution",
        "profile": "near_resolution",
    },
    {
        "name": "news-shock",
        "description": "News-shock agent — enters on high-materiality news signals",
        "execute_env": "EXECUTE_NEWS_SHOCK",
        "module": "agents.application.news_shock",
        "daemon_class": "NewsShockDaemon",
        "heartbeat": "data/news_shock_heartbeat",
        "docker_service": "news-shock",
        "profile": "news_shock",
    },
    {
        "name": "scalper",
        "description": "Scalper — delta-neutral pair trades on tight spreads",
        "execute_env": "EXECUTE_SCALPER",
        "module": "agents.application.scalper",
        "daemon_class": "ScalperDaemon",
        "heartbeat": "data/scalper_heartbeat",
        "docker_service": "scalper",
        "profile": "scalper",
    },
]

ENV_FILE = Path(os.getenv("ENV_FILE", ".env"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        return env
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _write_env_key(key: str, value: str) -> None:
    """Update or append a key in the .env file."""
    if not ENV_FILE.exists():
        ENV_FILE.write_text(f"{key}={value}\n")
        return
    lines = ENV_FILE.read_text().splitlines(keepends=True)
    new_lines = []
    found = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}\n")
    ENV_FILE.write_text("".join(new_lines))


def _heartbeat_age_minutes(path: str) -> Optional[float]:
    import time
    p = Path(path)
    if not p.exists():
        return None
    age_sec = time.time() - p.stat().st_mtime
    return age_sec / 60.0


def _strategy_by_name(name: str) -> Optional[dict]:
    for s in STRATEGIES:
        if s["name"] == name:
            return s
    return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list() -> None:
    """List all registered strategies."""
    print(f"{'Name':<20} {'Execute env':<32} {'Docker service':<20} Description")
    print("-" * 110)
    for s in STRATEGIES:
        print(
            f"{s['name']:<20} {s['execute_env']:<32} {s['docker_service']:<20} {s['description']}"
        )


def cmd_status() -> None:
    """Show live/shadow status for all strategies."""
    env = _read_env()
    print(f"\n{'Strategy':<20} {'Mode':<10} {'Heartbeat':<18} Description")
    print("-" * 90)
    for s in STRATEGIES:
        val = env.get(s["execute_env"], "false").lower()
        mode = "LIVE" if val == "true" else "shadow"
        hb = _heartbeat_age_minutes(s["heartbeat"])
        hb_str = f"{hb:.0f}m ago" if hb is not None else "no heartbeat"
        print(f"{s['name']:<20} {mode:<10} {hb_str:<18} {s['description']}")
    print()


def cmd_enable(name: str) -> None:
    """Set EXECUTE_<STRATEGY>=true in .env."""
    strat = _strategy_by_name(name)
    if strat is None:
        print(f"ERROR: unknown strategy '{name}'. Run 'list' to see options.")
        sys.exit(1)
    _write_env_key(strat["execute_env"], "true")
    print(f"Enabled live trading for '{name}' ({strat['execute_env']}=true in {ENV_FILE})")
    print("Restart the container to apply: docker compose restart", strat["docker_service"])


def cmd_disable(name: str) -> None:
    """Set EXECUTE_<STRATEGY>=false in .env."""
    strat = _strategy_by_name(name)
    if strat is None:
        print(f"ERROR: unknown strategy '{name}'. Run 'list' to see options.")
        sys.exit(1)
    _write_env_key(strat["execute_env"], "false")
    print(f"Disabled live trading for '{name}' ({strat['execute_env']}=false in {ENV_FILE})")
    print("Restart the container to apply: docker compose restart", strat["docker_service"])


def cmd_run(name: str) -> None:
    """Import and run the daemon for a named strategy (foreground, blocking)."""
    strat = _strategy_by_name(name)
    if strat is None:
        print(f"ERROR: unknown strategy '{name}'. Run 'list' to see options.")
        sys.exit(1)
    if strat["daemon_class"] is None:
        print(f"ERROR: '{name}' has no daemon class (use docker compose for the trader).")
        sys.exit(1)
    import importlib
    mod = importlib.import_module(strat["module"])
    daemon_cls = getattr(mod, strat["daemon_class"])
    print(f"Starting {strat['daemon_class']} for strategy '{name}' …")
    daemon_cls().run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


COMMANDS = {
    "list": (cmd_list, 0),
    "status": (cmd_status, 0),
    "enable": (cmd_enable, 1),
    "disable": (cmd_disable, 1),
    "run": (cmd_run, 1),
}

HELP = """\
strategy_factory.py — poly1 strategy management

Commands:
  list                  List all registered strategies
  status                Show live/shadow mode and heartbeat age
  enable  <name>        Set EXECUTE_<strategy>=true in .env
  disable <name>        Set EXECUTE_<strategy>=false in .env
  run     <name>        Run strategy daemon in foreground
"""


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP)
        return
    cmd = args[0]
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        print(HELP)
        sys.exit(1)
    fn, n_args = COMMANDS[cmd]
    if n_args == 0:
        fn()
    elif n_args == 1:
        if len(args) < 2:
            print(f"ERROR: '{cmd}' requires an argument (strategy name).")
            sys.exit(1)
        fn(args[1])


if __name__ == "__main__":
    main()
