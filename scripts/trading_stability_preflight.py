#!/usr/bin/env python3
"""Stability checks for the poly1 trading stack.

This script is intentionally dependency-light: it uses stdlib only so it can
run before the application virtualenv is fully installed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


OPEN_STATUSES = (
    "filled",
    "btc_daily_open",
    "near_resolution_open",
    "news_shock_open",
    "wallet_follow_open",
    "btc_5min_open",
)

TERMINAL_STATUSES = (
    "closed_take_profit",
    "closed_stop_loss",
    "closed_timeout",
    "resolved_yes",
    "resolved_no",
    "resolved_loss",
)

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

ENTRY_RESERVE_FLAGS = (
    "SWARM_RESERVE_USDC",
    "SCALPER_RESERVE_USDC",
    "BTC_DAILY_RESERVE_USDC",
    "BTC_5MIN_RESERVE_USDC",
    "NEAR_RESOLUTION_RESERVE_USDC",
    "NEWS_SHOCK_RESERVE_USDC",
    "WALLET_FOLLOW_RESERVE_USDC",
    "EXTERNAL_CONVICTION_RESERVE_USDC",
    "SCANNER_EXECUTOR_RESERVE_USDC",
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


def _parse_env_files(root: Path, spec: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            continue
        path = Path(item)
        if not path.is_absolute():
            path = root / path
        merged.update(_parse_env(path))
    return merged


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
        approved_live_flags = [key for key in live_flags if key != "EXECUTE"]
        max_allowed = 100 if mode == "live" else 1
        results.append(CheckResult(
            "live_entry_scope_explicit",
            _is_true(env.get("EXECUTE")) and 1 <= len(approved_live_flags) <= max_allowed,
            f"global EXECUTE=true and {len(approved_live_flags)} entry agent(s) enabled"
            if _is_true(env.get("EXECUTE")) and 1 <= len(approved_live_flags) <= max_allowed
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
    brain_required = _is_true(env.get("POLY1_REQUIRE_BRAIN_APPROVAL"))
    brain_enabled = _is_true(env.get("MARKET_BRAIN_ENABLED"))
    results.append(CheckResult(
        "brain_gate_required",
        brain_required and brain_enabled,
        "POLY1_REQUIRE_BRAIN_APPROVAL=true and MARKET_BRAIN_ENABLED=true"
        if brain_required and brain_enabled
        else f"POLY1_REQUIRE_BRAIN_APPROVAL={env.get('POLY1_REQUIRE_BRAIN_APPROVAL')} "
             f"MARKET_BRAIN_ENABLED={env.get('MARKET_BRAIN_ENABLED')}",
    ))
    max_trades = _as_float(env.get("MAX_TRADES_PER_HOUR"))
    results.append(CheckResult(
        "max_trades_per_hour_policy",
        max_trades <= 100 and max_trades > 0,
        f"MAX_TRADES_PER_HOUR={max_trades:g} (<=100)"
        if max_trades <= 100 and max_trades > 0
        else f"MAX_TRADES_PER_HOUR={env.get('MAX_TRADES_PER_HOUR')}",
    ))
    alloc_fraction = _as_float(env.get("MAX_AGENT_ALLOCATION_FRACTION"))
    results.append(CheckResult(
        "agent_allocation_cap_policy",
        0 < alloc_fraction <= 0.50,
        f"MAX_AGENT_ALLOCATION_FRACTION={alloc_fraction:.2f} (<=0.50)"
        if 0 < alloc_fraction <= 0.50
        else f"MAX_AGENT_ALLOCATION_FRACTION={env.get('MAX_AGENT_ALLOCATION_FRACTION')}",
    ))
    exit_interval = _as_float(env.get("MAINTAIN_LLM_EXIT_INTERVAL_SEC"))
    poll_sec = _as_float(env.get("MAINTAIN_POLL_SEC"))
    results.append(CheckResult(
        "minute_exit_revalidation",
        0 < poll_sec <= 60 and 0 < exit_interval <= 60,
        f"MAINTAIN_POLL_SEC={poll_sec:g}, MAINTAIN_LLM_EXIT_INTERVAL_SEC={exit_interval:g}"
        if 0 < poll_sec <= 60 and 0 < exit_interval <= 60
        else f"MAINTAIN_POLL_SEC={env.get('MAINTAIN_POLL_SEC')} "
             f"MAINTAIN_LLM_EXIT_INTERVAL_SEC={env.get('MAINTAIN_LLM_EXIT_INTERVAL_SEC')}",
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


def check_disk_space(root: Path) -> list[CheckResult]:
    threshold = _as_float(os.getenv("PREFLIGHT_MAX_DISK_USED_PCT", "85"))
    try:
        usage = shutil.disk_usage(root)
    except Exception as exc:
        return [CheckResult("disk_space", False, f"disk usage check failed: {exc}")]
    used_pct = (usage.used / usage.total) * 100 if usage.total else 100.0
    return [
        CheckResult(
            "disk_space",
            used_pct < threshold,
            f"used={used_pct:.1f}% threshold={threshold:.1f}% free_gb={usage.free / (1024**3):.2f}",
        )
    ]


def check_trade_log_backup(root: Path, db_path: Path) -> list[CheckResult]:
    if os.getenv("PREFLIGHT_REQUIRE_DB_BACKUP", "true").lower() not in {"1", "true", "yes", "on"}:
        return [CheckResult("trade_log_backup", True, "backup requirement disabled")]
    max_age_hours = _as_float(os.getenv("PREFLIGHT_MAX_BACKUP_AGE_HOURS", "30"))
    backup_dir = root / "data" / "backups"
    candidates = sorted(backup_dir.glob("trade_log-*.db")) if backup_dir.exists() else []
    if not candidates:
        return [CheckResult("trade_log_backup", False, f"no backups in {backup_dir}")]
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    age_hours = (datetime.now(timezone.utc).timestamp() - newest.stat().st_mtime) / 3600.0
    return [
        CheckResult(
            "trade_log_backup",
            age_hours <= max_age_hours,
            f"newest={newest.name} age_hours={age_hours:.2f} max={max_age_hours:.1f}",
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


def check_runtime_control(env: dict[str, str], root: Path, *, mode: str) -> list[CheckResult]:
    raw = env.get("RUNTIME_CONTROL_PATH", "./data/runtime_control.json")
    path = Path(raw)
    if str(path).startswith("/app/"):
        path = root / str(path).removeprefix("/app/")
    elif not path.is_absolute():
        path = root / path

    if not path.exists():
        return [CheckResult("runtime_control_present", False, f"missing {path}")]
    try:
        control = json.loads(path.read_text())
    except Exception as exc:
        return [CheckResult("runtime_control_parseable", False, str(exc))]

    expected_hash = str(control.get("config_hash") or "").strip()
    actual_hash = env.get("RUNTIME_CONFIG_HASH", "").strip()
    control_mode = str(control.get("mode") or "").strip()
    expected_modes = {"freeze"} if mode == "freeze" else {"live_probe", "live"}
    allowed_live_agents = control.get("allowed_live_agents") or []
    results = [
        CheckResult(
            "runtime_control_mode",
            control_mode in expected_modes,
            f"mode={control_mode or '<unset>'} expected={sorted(expected_modes)}",
        ),
        CheckResult(
            "runtime_config_hash_matches",
            bool(expected_hash) and expected_hash == actual_hash,
            f"env={actual_hash or '<unset>'} control={expected_hash or '<unset>'}",
        ),
    ]
    if mode != "freeze":
        expires_at = str(control.get("expires_at") or "").strip()
        if expires_at:
            try:
                expires_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                expires_ok = expires_dt > datetime.now(timezone.utc)
                expires_detail = f"expires_at={expires_at}"
            except ValueError:
                expires_ok = False
                expires_detail = f"invalid expires_at={expires_at!r}"
            results.append(CheckResult(
                "runtime_expiry_future",
                expires_ok,
                expires_detail,
            ))
        results.append(CheckResult(
            "runtime_live_agent_scope",
            1 <= len(allowed_live_agents) <= 100,
            f"allowed_live_agents={allowed_live_agents}",
        ))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".", help="repo root")
    parser.add_argument(
        "--env",
        default=".env,deploy/.env.runtime",
        help="comma-separated env files, later files override earlier ones",
    )
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
    env = _parse_env_files(root, args.env)
    # In Docker, deploy/.env.runtime is applied through compose `env_file`.
    # The file baked into /app can be stale after runtime_control.py updates,
    # so the live process environment must win over parsed files.
    env.update(os.environ)
    db_path = root / args.db

    results: list[CheckResult] = []
    results.extend(check_freeze_config(env, mode=args.mode))
    results.extend(check_runtime_control(env, root, mode=args.mode))
    results.extend(check_halt_state(env, root, mode=args.mode))
    results.extend(check_disk_space(root))

    if not db_path.exists():
        results.append(CheckResult("trade_log_db_exists", False, f"missing {db_path}"))
    else:
        with _connect(db_path) as conn:
            results.append(CheckResult("trade_log_db_exists", True, str(db_path)))
            results.extend(check_trade_log_backup(root, db_path))
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
