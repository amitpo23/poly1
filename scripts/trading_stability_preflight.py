#!/usr/bin/env python3
"""Stability checks for the poly1 trading stack.

This script is intentionally dependency-light: it uses stdlib only so it can
run before the application virtualenv is fully installed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


OPEN_STATUSES = (
    "filled",
    "btc_daily_open",
    "near_resolution_open",
    "news_shock_open",
    "wallet_follow_open",
)

TERMINAL_STATUSES = (
    "closed_take_profit",
    "closed_stop_loss",
    "closed_timeout",
    "closed_dust",
    "resolved_yes",
    "resolved_no",
    "resolved_loss",
)

ENTRY_EXECUTE_FLAGS = (
    "EXECUTE",
    "EXECUTE_SCALPER",
    "EXECUTE_BTC_DAILY",
    "EXECUTE_NEAR_RESOLUTION",
    "EXECUTE_NEWS_SHOCK",
    "EXECUTE_WALLET_FOLLOW",
)

ENTRY_RESERVE_FLAGS = (
    "SWARM_RESERVE_USDC",
    "SCALPER_RESERVE_USDC",
    "BTC_DAILY_RESERVE_USDC",
    "NEAR_RESOLUTION_RESERVE_USDC",
    "NEWS_SHOCK_RESERVE_USDC",
    "WALLET_FOLLOW_RESERVE_USDC",
    "ALLOCATOR_EXPLORATION_USDC",
)

CRITICAL_SETTLEMENT_STATUSES = {
    "active_unmanaged",
    "redeemable",
    "reconcile_error",
}


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _is_true(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _as_float(value: str | None) -> float:
    try:
        return float(str(value or "0").strip())
    except ValueError:
        return 0.0


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_freeze_config(env: dict[str, str], *, mode: str) -> list[CheckResult]:
    results: list[CheckResult] = []
    live_flags = [key for key in ENTRY_EXECUTE_FLAGS if _is_true(env.get(key))]
    live_reserves = [
        f"{key}={env.get(key)}"
        for key in ENTRY_RESERVE_FLAGS
        if _as_float(env.get(key)) > 0
    ]
    allocator_enforces = _is_true(env.get("ALLOC_SYNC_ENFORCE"))
    supervisor_enforces = _is_true(env.get("TRADING_SUPERVISOR_ENFORCE_HALT"))
    maintain_live = _is_true(env.get("EXECUTE_MAINTAIN"))

    if mode == "freeze":
        results.append(CheckResult(
            "entry_agents_frozen",
            not live_flags and not live_reserves and not allocator_enforces,
            "live flags/reserves disabled"
            if not live_flags and not live_reserves and not allocator_enforces
            else f"live_flags={live_flags} live_reserves={live_reserves} "
                 f"allocator_enforces={allocator_enforces}",
        ))
    else:
        approved_live_flags = [
            key for key in live_flags
            if key != "EXECUTE"
        ]
        results.append(CheckResult(
            "live_entry_scope_explicit",
            _is_true(env.get("EXECUTE")) and len(approved_live_flags) <= 1,
            "global EXECUTE=true and at most one entry agent enabled"
            if _is_true(env.get("EXECUTE")) and len(approved_live_flags) <= 1
            else f"EXECUTE={env.get('EXECUTE')} live_entry_flags={approved_live_flags}",
        ))
    results.append(CheckResult(
        "exit_manager_live",
        maintain_live,
        "EXECUTE_MAINTAIN=true" if maintain_live else "EXECUTE_MAINTAIN is not true",
    ))
    results.append(CheckResult(
        "supervisor_enforces_halt",
        supervisor_enforces,
        "TRADING_SUPERVISOR_ENFORCE_HALT=true"
        if supervisor_enforces else "supervisor only logs; it will not halt",
    ))
    return results


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _open_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    open_ph = ",".join("?" for _ in OPEN_STATUSES)
    terminal_ph = ",".join("?" for _ in TERMINAL_STATUSES)
    sql = f"""
        SELECT t.id, t.ts, t.market_id, t.token_id, t.status, t.size_usdc
        FROM trades t
        LEFT JOIN (
            SELECT token_id, MAX(id) AS terminal_id
            FROM trades
            WHERE status IN ({terminal_ph})
              AND token_id IS NOT NULL
              AND token_id != ''
            GROUP BY token_id
        ) x ON x.token_id = t.token_id
        WHERE t.status IN ({open_ph})
          AND t.token_id IS NOT NULL
          AND t.token_id != ''
          AND (t.error IS NULL OR t.error NOT LIKE 'SHADOW%')
          AND t.id > COALESCE(x.terminal_id, 0)
        ORDER BY t.id
    """
    return list(conn.execute(sql, (*TERMINAL_STATUSES, *OPEN_STATUSES)))


def check_open_positions(conn: sqlite3.Connection) -> list[CheckResult]:
    positions = _open_positions(conn)
    missing_marks: list[int] = []
    missing_decisions: list[int] = []
    for pos in positions:
        mark = conn.execute(
            "SELECT token_id FROM position_marks WHERE token_id = ?",
            (pos["token_id"],),
        ).fetchone()
        if not mark:
            missing_marks.append(int(pos["id"]))
        decision = conn.execute(
            """
            SELECT id FROM brain_decisions
            WHERE agent = 'position_manager'
              AND decision_type = 'exit'
              AND token_id = ?
              AND ts >= ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (pos["token_id"], pos["ts"]),
        ).fetchone()
        if not decision:
            missing_decisions.append(int(pos["id"]))

    return [
        CheckResult(
            "open_positions_accounted",
            not missing_marks and not missing_decisions,
            f"open={len(positions)}"
            if not missing_marks and not missing_decisions
            else f"open={len(positions)} missing_marks={missing_marks} "
                 f"missing_exit_decisions={missing_decisions}",
        )
    ]


def check_settlement(conn: sqlite3.Connection) -> list[CheckResult]:
    try:
        rows = list(conn.execute(
            """
            SELECT token_id, market_id, status, action, updated_ts,
                   recoverable_usdc, redeemable_usdc
            FROM settlement_reconciliation
            WHERE status IN ('active_unmanaged', 'redeemable', 'reconcile_error')
            ORDER BY updated_ts DESC
            """
        ))
    except sqlite3.OperationalError:
        return [CheckResult("settlement_reconciliation", False, "table missing")]
    return [
        CheckResult(
            "settlement_requires_no_action",
            not rows,
            "no critical settlement rows"
            if not rows else f"critical_rows={len(rows)} statuses="
            f"{sorted({row['status'] for row in rows})}",
        )
    ]


def check_halt_state(env: dict[str, str], root: Path, *, mode: str) -> list[CheckResult]:
    raw = env.get("KILL_SWITCH_FILE", "./data/HALT")
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    exists = path.exists()
    if mode == "freeze":
        return [
            CheckResult(
                "halt_file_present",
                exists,
                f"HALT present at {path}" if exists
                else f"HALT missing at {path}; freeze lacks physical brake",
            )
        ]
    return [
        CheckResult(
            "halt_file_absent",
            not exists,
            f"HALT absent at {path}" if not exists
            else f"HALT present at {path}",
        )
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".", help="repo root")
    parser.add_argument("--env", default=".env", help="env file relative to root")
    parser.add_argument("--db", default="data/trade_log.db", help="SQLite DB relative to root")
    parser.add_argument(
        "--mode",
        choices=("freeze", "live"),
        default="freeze",
        help="freeze expects HALT + no live entries; live expects explicit entry scope + no HALT",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    env = _parse_env(root / args.env)
    db_path = root / args.db

    results: list[CheckResult] = []
    results.extend(check_freeze_config(env, mode=args.mode))
    results.extend(check_halt_state(env, root, mode=args.mode))

    if not db_path.exists():
        results.append(CheckResult("trade_log_db_exists", False, f"missing {db_path}"))
    else:
        with _connect(db_path) as conn:
            results.append(CheckResult("trade_log_db_exists", True, str(db_path)))
            results.extend(check_open_positions(conn))
            results.extend(check_settlement(conn))

    ok = all(result.ok for result in results)
    payload = {
        "ts": _utc_now_iso(),
        "mode": args.mode,
        "status": "ok" if ok else "blocked",
        "checks": [result.__dict__ for result in results],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"trading_stability_preflight[{args.mode}]: {payload['status']}")
        for result in results:
            marker = "OK" if result.ok else "BLOCKED"
            print(f"- {marker} {result.name}: {result.detail}")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
