#!/usr/bin/env python3
"""Auto-sync allocator recommendations to .env files + restart containers.

Runs in a loop. Each cycle:
  1. Builds the CapitalAllocator report (read-only, queries DBs + market intel).
  2. Maps per-agent recommendations to env vars in poly1/.env and swarm/.env.
  3. If anything changed, writes the new values and restarts affected containers.

The allocator decides; this script enforces. The operator does not need to
approve each redistribution — that was the design rule from 2026-05-07.

Total budget is capped at SYNC_BUDGET_USDC (default $20). The script never
allocates beyond that cap regardless of allocator output.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.application.capital_allocator import CapitalAllocator  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("allocator_sync")

POLY1_ENV = os.getenv("POLY1_ENV_PATH", "/app/host/poly1.env")
SWARM_ENV = os.getenv("SWARM_ENV_PATH", "/app/host/swarm.env")
POLY1_DB = os.getenv("POLY1_DB_PATH", "/app/data/trade_log.db")
SWARM_DB = os.getenv("SWARM_DB_PATH", "/app/swarm/data/swarm.db")

CYCLE_SEC = int(os.getenv("ALLOC_SYNC_CYCLE_SEC", "300"))
BUDGET = float(os.getenv("ALLOC_SYNC_BUDGET_USDC", "20.0"))
WINDOW_HOURS = float(os.getenv("ALLOC_SYNC_WINDOW_HOURS", "24.0"))
# Minimum USDC change that justifies writing env + restarting a container.
# Prevents thrashing when allocator shifts by $0.01 every cycle. Set to 0
# to apply every change regardless of size.
MIN_DELTA_USDC = float(os.getenv("ALLOC_SYNC_MIN_DELTA_USDC", "0.50"))

# When true, write env files and restart containers. When false, log only.
ENFORCE = os.getenv("ALLOC_SYNC_ENFORCE", "true").lower() == "true"


def update_env_var(path: str, key: str, value: str) -> bool:
    """Set KEY=value in a .env file. Adds the line if missing. Returns True if changed."""
    p = Path(path)
    if not p.exists():
        logger.warning("env file missing: %s", path)
        return False
    content = p.read_text()
    pattern = re.compile(rf'^{re.escape(key)}=.*$', re.MULTILINE)
    new_line = f'{key}="{value}"'
    if pattern.search(content):
        new_content, _ = pattern.subn(new_line, content)
    else:
        new_content = content.rstrip() + "\n" + new_line + "\n"
    if new_content == content:
        return False
    p.write_text(new_content)
    return True


def restart_container(name: str, compose_file: str | None = None) -> bool:
    """Restart a docker container by service name. Returns True if successful."""
    cmd = ["docker"]
    if compose_file:
        cmd += ["compose", "-f", compose_file, "restart", name]
    else:
        cmd += ["restart", name]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            logger.info("restarted container %s", name)
            return True
        logger.warning("restart %s failed: rc=%s err=%s",
                       name, result.returncode, result.stderr.strip()[:200])
        return False
    except Exception as exc:
        logger.warning("restart %s exception: %s", name, exc)
        return False


def derive_env_targets(report) -> Dict[str, Dict[str, str]]:
    """Map allocator recommendations → env var values for both .env files."""
    by_agent = {a.agent: a.recommended_usdc for a in report.agents}

    btc_daily = round(by_agent.get("btc_daily", 0.0), 2)
    scalper = round(by_agent.get("scalper", 0.0), 2)
    swarm_total = round(sum(
        v for k, v in by_agent.items() if k.startswith("swarm_")
    ), 2)

    poly1 = {
        "BTC_DAILY_RESERVE_USDC": f"{btc_daily}",
        "EXECUTE_BTC_DAILY": "true" if btc_daily > 0 else "false",
        "SCALPER_RESERVE_USDC": f"{scalper}",
        "EXECUTE_SCALPER": "true" if scalper > 0 else "false",
        "SWARM_RESERVE_USDC": f"{swarm_total}",
    }
    swarm = {
        "TOTAL_CAPITAL": f"{swarm_total}",
        "BOT_MODE": "live" if swarm_total > 0 else "dryrun",
    }
    return {"poly1": poly1, "swarm": swarm, "_summary": {
        "btc_daily": btc_daily,
        "scalper": scalper,
        "swarm_total": swarm_total,
    }}


def cycle_once() -> dict:
    allocator = CapitalAllocator(
        poly_db=POLY1_DB,
        swarm_db=SWARM_DB,
        total_budget_usdc=BUDGET,
        window_hours=WINDOW_HOURS,
    )
    report = allocator.build_report()
    targets = derive_env_targets(report)
    summary = targets["_summary"]
    logger.info(
        "allocator: btc_daily=$%.2f scalper=$%.2f swarm=$%.2f (budget $%.2f)",
        summary["btc_daily"], summary["scalper"], summary["swarm_total"], BUDGET,
    )

    if not ENFORCE:
        logger.info("ENFORCE=false; skipping writes/restarts")
        return summary

    # Anti-churn: skip writes/restarts when the total budget shift is tiny.
    prev_summary = getattr(cycle_once, "_last_summary", {})
    max_delta = max(
        abs(summary.get(k, 0.0) - prev_summary.get(k, 0.0))
        for k in ("btc_daily", "scalper", "swarm_total")
    ) if prev_summary else float("inf")
    cycle_once._last_summary = dict(summary)
    if max_delta < MIN_DELTA_USDC:
        logger.debug(
            "allocator: max delta $%.2f < threshold $%.2f — skipping writes",
            max_delta, MIN_DELTA_USDC,
        )
        return summary

    poly1_changed = False
    for k, v in targets["poly1"].items():
        if update_env_var(POLY1_ENV, k, v):
            poly1_changed = True
            logger.info("poly1.env: set %s=%s", k, v)

    swarm_changed = False
    for k, v in targets["swarm"].items():
        if update_env_var(SWARM_ENV, k, v):
            swarm_changed = True
            logger.info("swarm.env: set %s=%s", k, v)

    if poly1_changed:
        for svc in ("poly1-scalper", "poly1-btc-daily"):
            restart_container(svc)

    if swarm_changed:
        # Swarm has its own compose; restart by container name only.
        restart_container("polymarket-swarm")

    return summary


def main() -> int:
    logger.info(
        "allocator_sync starting: budget=$%.2f cycle=%ds enforce=%s",
        BUDGET, CYCLE_SEC, ENFORCE,
    )
    while True:
        try:
            cycle_once()
        except Exception:
            logger.exception("cycle failed")
        time.sleep(CYCLE_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
