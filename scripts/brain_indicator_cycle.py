#!/usr/bin/env python3
"""Run one safe MetaBrain indicator cycle.

This script is the control-room loop between research signals and execution
agents.  It refreshes external indicators, updates shadow markouts/scorecards,
runs the scanner, and lets scanner_executor consume approved scanner decisions
in shadow mode unless live dispatch is explicitly enabled.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


ROOT = Path(__file__).resolve().parents[1]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class BrainIndicatorConfig:
    db_path: str = "./data/trade_log.db"
    data_dir: str = "./data"
    poll_seconds: int = 60
    command_timeout_sec: int = 240
    no_trade_guard: bool = True
    run_market_universe: bool = True
    run_alphainsider: bool = True
    run_markouts: bool = True
    run_provider_scorecard: bool = True
    run_strategy_scorecard: bool = True
    run_opportunity_factory: bool = True
    run_market_scanner: bool = True
    run_backup: bool = True
    dispatch_scanner_executor: bool = True
    allow_live_dispatch: bool = False
    alphainsider_limit: int = 25
    alphainsider_sort: str = "performance"
    markout_limit: int = 500
    markout_decisions: str = "SHADOW_ENTER,SHADOW_QUOTE,ENTER"
    markout_live_fallback: bool = False
    strategy_min_decisions: int = 20
    provider_min_matched: int = 1
    scanner_max_candidates: int = 12
    scanner_target_trade_decisions: int = 4
    tavily_daily_limit: int = 3
    enable_tavily: bool = False
    enable_llm: bool = False
    market_universe_interval_sec: int = 300
    alphainsider_interval_sec: int = 900
    markouts_interval_sec: int = 60
    provider_scorecard_interval_sec: int = 300
    strategy_scorecard_interval_sec: int = 300
    opportunity_factory_interval_sec: int = 60
    market_scanner_interval_sec: int = 60
    scanner_executor_interval_sec: int = 60
    backup_interval_sec: int = 14400  # 4h; preflight requires <30h freshness
    backup_dir: str = "./data/backups"
    backup_keep: int = 6
    run_calibration_refresh: bool = True
    calibration_refresh_interval_sec: int = 3600  # every hour
    calibration_lookback_days: int = 30
    calibration_max_age_hours: int = 48
    calibration_out_path: str = "./data/probability_calibration.json"
    state_path: str = "./data/brain_indicator_cycle_state.json"
    heartbeat_path: str = "./data/brain_indicator_cycle_heartbeat"
    report_path: str = "./data/brain_indicator_cycle_latest.json"

    @classmethod
    def from_env(cls) -> "BrainIndicatorConfig":
        data_dir = os.getenv("BRAIN_INDICATOR_DATA_DIR", "./data")
        return cls(
            db_path=os.getenv("BRAIN_INDICATOR_DB_PATH", "./data/trade_log.db"),
            data_dir=data_dir,
            poll_seconds=_env_int("BRAIN_INDICATOR_CYCLE_SECONDS", 60),
            command_timeout_sec=_env_int("BRAIN_INDICATOR_COMMAND_TIMEOUT_SEC", 240),
            no_trade_guard=_env_bool("BRAIN_INDICATOR_NO_TRADE_GUARD", True),
            run_market_universe=_env_bool("BRAIN_INDICATOR_RUN_MARKET_UNIVERSE", True),
            run_alphainsider=_env_bool("BRAIN_INDICATOR_RUN_ALPHAINSIDER", True),
            run_markouts=_env_bool("BRAIN_INDICATOR_RUN_MARKOUTS", True),
            run_provider_scorecard=_env_bool("BRAIN_INDICATOR_RUN_PROVIDER_SCORECARD", True),
            run_strategy_scorecard=_env_bool("BRAIN_INDICATOR_RUN_STRATEGY_SCORECARD", True),
            run_opportunity_factory=_env_bool("BRAIN_INDICATOR_RUN_OPPORTUNITY_FACTORY", True),
            run_market_scanner=_env_bool("BRAIN_INDICATOR_RUN_MARKET_SCANNER", True),
            run_backup=_env_bool("BRAIN_INDICATOR_RUN_BACKUP", True),
            dispatch_scanner_executor=_env_bool("BRAIN_INDICATOR_DISPATCH_SCANNER_EXECUTOR", True),
            allow_live_dispatch=_env_bool("BRAIN_INDICATOR_ALLOW_LIVE_DISPATCH", False),
            alphainsider_limit=_env_int("BRAIN_INDICATOR_ALPHAINSIDER_LIMIT", 25),
            alphainsider_sort=os.getenv("BRAIN_INDICATOR_ALPHAINSIDER_SORT", "performance"),
            markout_limit=_env_int("BRAIN_INDICATOR_MARKOUT_LIMIT", 500),
            markout_decisions=os.getenv(
                "BRAIN_INDICATOR_MARKOUT_DECISIONS",
                "SHADOW_ENTER,SHADOW_QUOTE,ENTER",
            ),
            markout_live_fallback=_env_bool("BRAIN_INDICATOR_MARKOUT_LIVE_FALLBACK", False),
            strategy_min_decisions=_env_int("BRAIN_INDICATOR_STRATEGY_MIN_DECISIONS", 20),
            provider_min_matched=_env_int("BRAIN_INDICATOR_PROVIDER_MIN_MATCHED", 1),
            scanner_max_candidates=_env_int("BRAIN_INDICATOR_SCANNER_MAX_CANDIDATES", 12),
            scanner_target_trade_decisions=_env_int("BRAIN_INDICATOR_SCANNER_TARGET_TRADE_DECISIONS", 4),
            tavily_daily_limit=_env_int("BRAIN_INDICATOR_TAVILY_DAILY_LIMIT", 3),
            enable_tavily=_env_bool("BRAIN_INDICATOR_ENABLE_TAVILY", False),
            enable_llm=_env_bool("BRAIN_INDICATOR_ENABLE_LLM", False),
            market_universe_interval_sec=_env_int("BRAIN_INDICATOR_MARKET_UNIVERSE_INTERVAL_SEC", 300),
            alphainsider_interval_sec=_env_int("BRAIN_INDICATOR_ALPHAINSIDER_INTERVAL_SEC", 900),
            markouts_interval_sec=_env_int("BRAIN_INDICATOR_MARKOUTS_INTERVAL_SEC", 60),
            provider_scorecard_interval_sec=_env_int("BRAIN_INDICATOR_PROVIDER_SCORECARD_INTERVAL_SEC", 300),
            strategy_scorecard_interval_sec=_env_int("BRAIN_INDICATOR_STRATEGY_SCORECARD_INTERVAL_SEC", 300),
            opportunity_factory_interval_sec=_env_int("BRAIN_INDICATOR_OPPORTUNITY_FACTORY_INTERVAL_SEC", 60),
            market_scanner_interval_sec=_env_int("BRAIN_INDICATOR_MARKET_SCANNER_INTERVAL_SEC", 60),
            scanner_executor_interval_sec=_env_int("BRAIN_INDICATOR_SCANNER_EXECUTOR_INTERVAL_SEC", 60),
            backup_interval_sec=_env_int("BRAIN_INDICATOR_BACKUP_INTERVAL_SEC", 14400),
            backup_dir=os.getenv("BRAIN_INDICATOR_BACKUP_DIR", f"{data_dir}/backups"),
            backup_keep=_env_int("BRAIN_INDICATOR_BACKUP_KEEP", 6),
            run_calibration_refresh=_env_bool(
                "BRAIN_INDICATOR_RUN_CALIBRATION_REFRESH", True
            ),
            calibration_refresh_interval_sec=_env_int(
                "BRAIN_INDICATOR_CALIBRATION_REFRESH_INTERVAL_SEC", 3600
            ),
            calibration_lookback_days=_env_int(
                "BRAIN_INDICATOR_CALIBRATION_LOOKBACK_DAYS", 30
            ),
            calibration_max_age_hours=_env_int(
                "BRAIN_INDICATOR_CALIBRATION_MAX_AGE_HOURS", 48
            ),
            calibration_out_path=os.getenv(
                "BRAIN_INDICATOR_CALIBRATION_OUT_PATH",
                f"{data_dir}/probability_calibration.json",
            ),
            state_path=os.getenv(
                "BRAIN_INDICATOR_STATE_PATH",
                f"{data_dir}/brain_indicator_cycle_state.json",
            ),
            heartbeat_path=os.getenv(
                "BRAIN_INDICATOR_HEARTBEAT_PATH",
                f"{data_dir}/brain_indicator_cycle_heartbeat",
            ),
            report_path=os.getenv(
                "BRAIN_INDICATOR_REPORT_PATH",
                f"{data_dir}/brain_indicator_cycle_latest.json",
            ),
        )


@dataclass
class StepResult:
    name: str
    ok: bool
    returncode: int
    duration_sec: float
    stdout_excerpt: str = ""
    stderr_excerpt: str = ""
    skipped: bool = False


Runner = Callable[[list[str], dict[str, str], int], subprocess.CompletedProcess]


def default_runner(cmd: list[str], env: dict[str, str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def build_steps(cfg: BrainIndicatorConfig) -> list[tuple[str, list[str], dict[str, str]]]:
    data = Path(cfg.data_dir)
    py = sys.executable
    steps: list[tuple[str, list[str], dict[str, str]]] = []
    if cfg.run_market_universe:
        steps.append((
            "market_universe",
            [py, "-m", "agents.application.market_universe", "--once", "--db", cfg.db_path],
            {
                "MARKET_UNIVERSE_OUTPUT_PATH": str(data / "market_universe.json"),
                "MARKET_UNIVERSE_HEARTBEAT_PATH": str(data / "market_universe_heartbeat"),
            },
        ))
    if cfg.run_alphainsider:
        steps.append((
            "alphainsider_rankings",
            [
                py,
                "scripts/alphainsider_strategy_rankings.py",
                "--timeframes",
                "month,year,five_year",
                "--sort",
                cfg.alphainsider_sort,
                "--limit",
                str(cfg.alphainsider_limit),
                "--out",
                str(data / "alphainsider_strategy_rankings_latest.json"),
            ],
            {},
        ))
    if cfg.run_markouts:
        markout_cmd = [
            py,
            "scripts/update_shadow_markouts.py",
            "--db",
            cfg.db_path,
            "--horizons",
            "1,3,5,15",
            "--limit",
            str(cfg.markout_limit),
            "--decisions",
            cfg.markout_decisions,
        ]
        if cfg.markout_live_fallback:
            markout_cmd.append("--live-fallback")
        steps.append(("shadow_markouts", markout_cmd, {}))
    if cfg.run_provider_scorecard:
        steps.append((
            "provider_scorecard",
            [
                py,
                "scripts/provider_scorecard.py",
                "--db",
                cfg.db_path,
                "--out",
                str(data / "provider_scorecard.json"),
                "--min-matched",
                str(cfg.provider_min_matched),
            ],
            {},
        ))
    if cfg.run_strategy_scorecard:
        steps.append((
            "strategy_scorecard",
            [
                py,
                "scripts/strategy_scorecard.py",
                "--db",
                cfg.db_path,
                "--out",
                str(data / "strategy_scorecard.json"),
                "--min-decisions",
                str(cfg.strategy_min_decisions),
            ],
            {},
        ))
    if cfg.run_opportunity_factory:
        steps.append((
            "opportunity_factory",
            [py, "-m", "agents.application.opportunity_factory", "--once", "--db", cfg.db_path],
            {
                "OPPORTUNITY_FACTORY_DATA_DIR": str(data),
                "OPPORTUNITY_FACTORY_ALPHAINSIDER_PATH": str(data / "alphainsider_strategy_rankings_latest.json"),
                "OPPORTUNITY_FACTORY_MARKET_UNIVERSE_PATH": str(data / "market_universe.json"),
                "OPPORTUNITY_FACTORY_REPORT_PATH": str(data / "opportunity_factory_latest.json"),
                "OPPORTUNITY_FACTORY_HEARTBEAT_PATH": str(data / "opportunity_factory_heartbeat"),
            },
        ))
    if cfg.run_market_scanner:
        steps.append((
            "market_scanner",
            [py, "-m", "agents.application.market_scanner", "--once", "--json", "--db", cfg.db_path],
            {
                "SCANNER_MAX_CANDIDATES": str(cfg.scanner_max_candidates),
                "SCANNER_TARGET_TRADE_DECISIONS": str(cfg.scanner_target_trade_decisions),
                "TAVILY_ENABLED": "true" if cfg.enable_tavily else "false",
                "TAVILY_DAILY_LIMIT": str(cfg.tavily_daily_limit),
                "META_BRAIN_STRADDLE_TAVILY_ENABLED": "true" if cfg.enable_tavily else "false",
                "META_BRAIN_STRADDLE_LLM_ENABLED": "true" if cfg.enable_llm else "false",
            },
        ))
    if cfg.dispatch_scanner_executor:
        env = {
            "EXECUTE_SCANNER_EXECUTOR": "false",
            "EXECUTE": "false",
            "RUNTIME_AGENT": "scanner_executor",
        }
        if cfg.allow_live_dispatch and not cfg.no_trade_guard:
            env = {"RUNTIME_AGENT": "scanner_executor"}
        steps.append((
            "scanner_executor_dispatch",
            [py, "-m", "agents.application.scanner_executor", "--once", "--db", cfg.db_path],
            env,
        ))
    if cfg.run_backup:
        steps.append((
            "trade_log_backup",
            [
                py,
                "scripts/python/backup_trade_log.py",
                "--db",
                cfg.db_path,
                "--out-dir",
                cfg.backup_dir,
                "--keep",
                str(cfg.backup_keep),
            ],
            {},
        ))
    if cfg.run_calibration_refresh:
        steps.append((
            "probability_calibration_refresh",
            [
                py,
                "scripts/refresh_probability_calibration.py",
                "--db",
                cfg.db_path,
                "--days",
                str(cfg.calibration_lookback_days),
                "--max-age-hours",
                str(cfg.calibration_max_age_hours),
                "--out",
                cfg.calibration_out_path,
            ],
            {},
        ))
    return steps


def step_intervals(cfg: BrainIndicatorConfig) -> dict[str, int]:
    return {
        "market_universe": cfg.market_universe_interval_sec,
        "alphainsider_rankings": cfg.alphainsider_interval_sec,
        "shadow_markouts": cfg.markouts_interval_sec,
        "provider_scorecard": cfg.provider_scorecard_interval_sec,
        "strategy_scorecard": cfg.strategy_scorecard_interval_sec,
        "opportunity_factory": cfg.opportunity_factory_interval_sec,
        "market_scanner": cfg.market_scanner_interval_sec,
        "scanner_executor_dispatch": cfg.scanner_executor_interval_sec,
        "trade_log_backup": cfg.backup_interval_sec,
        "probability_calibration_refresh": cfg.calibration_refresh_interval_sec,
    }


def run_once(
    cfg: BrainIndicatorConfig,
    *,
    runner: Optional[Runner] = None,
    dry_run: bool = False,
) -> dict:
    runner = runner or default_runner
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)
    report = {
        "generated_at": started.isoformat(),
        "config": asdict(cfg),
        "mode": "shadow_dispatch" if cfg.no_trade_guard or not cfg.allow_live_dispatch else "live_dispatch_allowed",
        "steps": [],
        "blockers": [],
    }
    state_path = Path(cfg.state_path)
    state = _read_json(state_path)
    last_run = state.get("last_run_ts") if isinstance(state.get("last_run_ts"), dict) else {}
    now_ts = time.time()
    intervals = step_intervals(cfg)
    if cfg.allow_live_dispatch and cfg.no_trade_guard:
        report["blockers"].append("allow_live_dispatch_ignored_by_no_trade_guard")
    for name, cmd, env_overrides in build_steps(cfg):
        interval = max(0, int(intervals.get(name, 0)))
        previous = float(last_run.get(name) or 0.0)
        elapsed = now_ts - previous if previous else None
        if interval and previous and elapsed is not None and elapsed < interval:
            report["steps"].append({
                "name": name,
                "ok": True,
                "returncode": 0,
                "duration_sec": 0.0,
                "skipped": True,
                "skip_reason": "cadence",
                "interval_sec": interval,
                "next_due_in_sec": round(interval - elapsed, 3),
            })
            continue
        if dry_run:
            report["steps"].append({
                "name": name,
                "ok": True,
                "returncode": 0,
                "duration_sec": 0.0,
                "command": cmd,
                "env_overrides": env_overrides,
                "skipped": True,
                "skip_reason": "dry_run",
            })
            continue
        env = os.environ.copy()
        env.update(env_overrides)
        if cfg.no_trade_guard:
            env.setdefault("EXECUTE", "false")
            env.setdefault("EXECUTE_SCANNER_EXECUTOR", "false")
        t0 = time.monotonic()
        try:
            proc = runner(cmd, env, cfg.command_timeout_sec)
            step = StepResult(
                name=name,
                ok=proc.returncode == 0,
                returncode=proc.returncode,
                duration_sec=round(time.monotonic() - t0, 3),
                stdout_excerpt=_excerpt(proc.stdout),
                stderr_excerpt=_excerpt(proc.stderr),
            )
        except subprocess.TimeoutExpired as exc:
            step = StepResult(
                name=name,
                ok=False,
                returncode=124,
                duration_sec=round(time.monotonic() - t0, 3),
                stdout_excerpt=_excerpt(exc.stdout),
                stderr_excerpt=f"timeout after {cfg.command_timeout_sec}s",
            )
        report["steps"].append(asdict(step))
        if not step.ok:
            report["blockers"].append(f"{name}_failed")
        else:
            last_run[name] = time.time()
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["ok"] = not report["blockers"]
    state["last_run_ts"] = last_run
    state["updated_at"] = report["completed_at"]
    _write_json(state_path, state)
    _write_json(Path(cfg.report_path), report)
    _touch(Path(cfg.heartbeat_path))
    return report


def run_daemon(cfg: BrainIndicatorConfig) -> None:
    while True:
        report = run_once(cfg)
        print(json.dumps({"ok": report["ok"], "blockers": report["blockers"]}, sort_keys=True), flush=True)
        time.sleep(max(10, cfg.poll_seconds))


def _excerpt(value, limit: int = 1600) -> str:
    if value is None:
        return ""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
    except Exception:
        return {}
    return {}


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run safe MetaBrain indicator cycle")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    cfg = BrainIndicatorConfig.from_env()
    if args.db:
        cfg = BrainIndicatorConfig(**{**asdict(cfg), "db_path": args.db})
    if args.daemon:
        run_daemon(cfg)
        return 0
    report = run_once(cfg, dry_run=args.dry_run)
    if args.json or args.once or args.dry_run:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
