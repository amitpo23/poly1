#!/usr/bin/env python3
"""state_watcher.py — diff-driven alerting; silent when nothing changed.

Replaces 30-min "no change" cron reports with state-change alerts.
Reads current state from poly1 + swarm DBs and `docker ps`, compares
to the last snapshot, prints alerts only on material change.

Output is empty when nothing changed → cron caller can stay silent.

What triggers an alert:
- 🚨 container went unhealthy / disappeared
- 🚨 new RECONCILE_NEEDED scalper pair
- 🚨 new MAY_HAVE_FIRED trade
- ⚡ new fill on any agent (poly1 or swarm)
- ⚡ new btc_daily_open / scalper_leg / closed_take_profit / closed_stop_loss
- ℹ️ swarm submitted-open count rose

What is silent:
- Repeated `failed` rows (network/404 noise)
- skipped_dry_run / skipped_gate / close_failed
- feed 404 warnings
- Same state as last run

Out of scope (MVP):
- On-chain balance (run separately if needed; web3 import keeps script
  stdlib-only and fast)
- LLM-driven anomaly detection
- Auto-fix
- Drawdown thresholds (need balance history; future)

Usage:
    python3 scripts/python/state_watcher.py

First run seeds the snapshot and is silent. Subsequent runs print
alerts only when something changed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

POLY1_DB = Path("/Users/mymac/coding/poly1/data/trade_log.db")
SWARM_DB = Path("/Users/mymac/Desktop/poly/bot/data/swarm.db")
SCOUT_DB = Path("/Users/mymac/coding/poly1/data/scout.db")
SNAPSHOT_FILE = Path("/Users/mymac/coding/poly1/data/.state_watcher_snapshot.json")

# Statuses that should alert when count goes up.
POLY1_NOTABLE_STATUSES = {
    "btc_daily_open",
    "scalper_leg",
    "scalper_exit",
    "closed_take_profit",
    "closed_stop_loss",
    "filled",
}

# Statuses to ignore (high-frequency noise).
POLY1_IGNORE = {
    "failed",
    "skipped_dry_run",
    "skipped_gate",
    "close_failed",
}

EXPECTED_CONTAINERS = [
    "poly1",
    "poly1-scalper",
    "poly1-position-manager",
    "poly1-btc-daily",
    "polymarket-swarm",
]

DOCKER_PATH_EXTRA = ":/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin"


def _read_snapshot() -> dict[str, Any]:
    if not SNAPSHOT_FILE.exists():
        return {}
    try:
        return json.loads(SNAPSHOT_FILE.read_text())
    except Exception:
        return {}


def _write_snapshot(s: dict[str, Any]) -> None:
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(json.dumps(s, indent=2, sort_keys=True))


def _gather_poly1() -> dict[str, Any]:
    out: dict[str, Any] = {
        "max_trade_id": 0,
        "by_status": {},
        "reconcile_needed": 0,
        "may_have_fired": 0,
    }
    if not POLY1_DB.exists():
        return out
    # Plain connection (no mode=ro). The bot keeps a -wal file open,
    # which conflicts with SQLite's read-only journal-mode handshake.
    # We never call commit(), so this is read-only in practice.
    with sqlite3.connect(str(POLY1_DB)) as c:
        out["max_trade_id"] = c.execute("SELECT COALESCE(MAX(id),0) FROM trades").fetchone()[0]
        for status, n in c.execute("SELECT status, COUNT(*) FROM trades GROUP BY status"):
            out["by_status"][status] = n
        out["reconcile_needed"] = c.execute(
            "SELECT COUNT(*) FROM scalper_pairs WHERE state='RECONCILE_NEEDED'"
        ).fetchone()[0]
        out["may_have_fired"] = c.execute(
            "SELECT COUNT(*) FROM trades WHERE status='MAY_HAVE_FIRED'"
        ).fetchone()[0]
    return out


def _gather_swarm() -> dict[str, Any]:
    out: dict[str, Any] = {
        "max_fill_id": 0,
        "max_pending_id": 0,
        "submitted_open": 0,
    }
    if not SWARM_DB.exists():
        return out
    with sqlite3.connect(str(SWARM_DB)) as c:
        out["max_fill_id"] = c.execute("SELECT COALESCE(MAX(id),0) FROM fills").fetchone()[0]
        out["max_pending_id"] = c.execute("SELECT COALESCE(MAX(id),0) FROM pending_orders").fetchone()[0]
        out["submitted_open"] = c.execute(
            "SELECT COUNT(*) FROM pending_orders WHERE status='submitted'"
        ).fetchone()[0]
    return out


def _gather_scout() -> dict[str, Any]:
    """Scout opportunities table state.

    The scout cron writes to scout.db hourly; new rows there represent
    new market candidates worth a human look. We track max_id so the
    watcher alerts when fresh opportunities surface.
    """
    out: dict[str, Any] = {"max_id": 0, "top_candidate": None}
    if not SCOUT_DB.exists():
        return out
    try:
        with sqlite3.connect(str(SCOUT_DB)) as c:
            out["max_id"] = c.execute(
                "SELECT COALESCE(MAX(id),0) FROM scout_opportunities"
            ).fetchone()[0]
            row = c.execute(
                "SELECT strategy_match, market_slug, score, no_price, top_news_headline "
                "FROM scout_opportunities ORDER BY score DESC, id DESC LIMIT 1"
            ).fetchone()
            if row:
                out["top_candidate"] = {
                    "strategy": row[0], "slug": row[1], "score": row[2],
                    "no_price": row[3], "news": row[4],
                }
    except sqlite3.Error:
        pass
    return out


def _gather_containers() -> dict[str, str]:
    env = {**os.environ, "PATH": os.environ.get("PATH", "") + DOCKER_PATH_EXTRA}
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}|{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except Exception as exc:
        return {"_error": f"subprocess: {exc}"}
    if result.returncode != 0:
        return {"_error": result.stderr.strip()[:200]}
    out: dict[str, str] = {}
    for line in result.stdout.strip().split("\n"):
        if "|" not in line:
            continue
        name, status = line.split("|", 1)
        if name not in EXPECTED_CONTAINERS:
            continue
        out[name] = "healthy" if "healthy" in status.lower() else status.strip()
    return out


def _diff(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    alerts: list[str] = []

    old_c = old.get("containers", {})
    new_c = new.get("containers", {})
    if "_error" in new_c:
        alerts.append(f"🚨 docker query failed: {new_c['_error']}")
    else:
        for name in EXPECTED_CONTAINERS:
            new_h = new_c.get(name)
            old_h = old_c.get(name)
            if new_h is None and old_h is not None:
                alerts.append(f"🚨 container DOWN: {name} (was {old_h})")
            elif new_h is None:
                alerts.append(f"🚨 container missing: {name}")
            elif new_h != "healthy" and new_h != old_h:
                alerts.append(f"🚨 container unhealthy: {name} → {new_h}")

    op = old.get("poly1", {})
    np = new.get("poly1", {})
    if np.get("reconcile_needed", 0) > op.get("reconcile_needed", 0):
        alerts.append(f"🚨 NEW RECONCILE_NEEDED: total {np['reconcile_needed']}")
    if np.get("may_have_fired", 0) > op.get("may_have_fired", 0):
        alerts.append(f"🚨 NEW MAY_HAVE_FIRED: total {np['may_have_fired']}")
    if np.get("max_trade_id", 0) > op.get("max_trade_id", 0):
        for status, count in np.get("by_status", {}).items():
            old_count = op.get("by_status", {}).get(status, 0)
            delta = count - old_count
            if delta <= 0 or status in POLY1_IGNORE:
                continue
            if status in POLY1_NOTABLE_STATUSES:
                alerts.append(f"⚡ poly1 +{delta} {status} (total {count})")

    os_ = old.get("swarm", {})
    ns = new.get("swarm", {})
    new_fills = ns.get("max_fill_id", 0) - os_.get("max_fill_id", 0)
    if new_fills > 0:
        alerts.append(f"⚡ swarm +{new_fills} fill(s) (max id {ns['max_fill_id']})")
    if ns.get("submitted_open", 0) > os_.get("submitted_open", 0):
        alerts.append(f"ℹ️  swarm submitted-open rose to {ns['submitted_open']}")

    osc = old.get("scout", {})
    nsc = new.get("scout", {})
    new_opportunities = nsc.get("max_id", 0) - osc.get("max_id", 0)
    if new_opportunities > 0:
        top = nsc.get("top_candidate") or {}
        if top:
            news_str = f" news={top.get('news')[:50]!r}" if top.get("news") else ""
            alerts.append(
                f"🔎 scout: +{new_opportunities} opportunity(ies). "
                f"top: {top.get('strategy')} on {top.get('slug')[:50]} "
                f"(NO={top.get('no_price'):.3f} score={top.get('score')}){news_str}"
            )
        else:
            alerts.append(f"🔎 scout: +{new_opportunities} opportunity(ies)")

    return alerts


def main() -> int:
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new = {
        "ts": now_iso,
        "poly1": _gather_poly1(),
        "swarm": _gather_swarm(),
        "scout": _gather_scout(),
        "containers": _gather_containers(),
    }
    old = _read_snapshot()
    alerts = _diff(old, new) if old else []

    if alerts:
        print(f"=== state_watcher {now_iso} ===")
        for a in alerts:
            print(a)

    _write_snapshot(new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
