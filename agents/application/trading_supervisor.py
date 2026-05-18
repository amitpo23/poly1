"""Trading supervisor: safety loop for live trading control.

This daemon is not a trader. It is a control-plane guard that verifies the
exit path is alive for every open journal position. If an open position stops
receiving position-manager evaluations, the supervisor can trip the HALT file
so entry agents stop allocating fresh capital until the exit path is fixed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agents.application.trade_log import TradeLog
from agents.application.tavily import tavily_headlines


logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


@dataclass
class TradingSupervisorConfig:
    poll_seconds: int = 60
    heartbeat_path: str = "/app/data/trading_supervisor_heartbeat"
    state_path: str = "/app/data/trading_supervisor_status.json"
    position_manager_heartbeat_path: str = "/app/data/position_manager_heartbeat"
    kill_switch_file: str = "./data/HALT"
    stale_heartbeat_seconds: int = 180
    evaluation_grace_seconds: int = 180
    min_position_age_seconds: int = 45
    close_failed_window_minutes: int = 15
    close_failed_threshold: int = 5
    settlement_max_age_minutes: int = 15
    settlement_critical_statuses: tuple[str, ...] = (
        "active_unmanaged",
        "redeemable",
        "reconcile_error",
    )
    enforce_halt: bool = True

    @classmethod
    def from_env(cls) -> "TradingSupervisorConfig":
        maintain_poll = _env_int("MAINTAIN_POLL_SEC", 60)
        return cls(
            poll_seconds=_env_int("TRADING_SUPERVISOR_POLL_SEC", 60),
            heartbeat_path=os.getenv(
                "TRADING_SUPERVISOR_HEARTBEAT_PATH",
                "/app/data/trading_supervisor_heartbeat",
            ),
            state_path=os.getenv(
                "TRADING_SUPERVISOR_STATE_PATH",
                "/app/data/trading_supervisor_status.json",
            ),
            position_manager_heartbeat_path=os.getenv(
                "TRADING_SUPERVISOR_POSITION_MANAGER_HEARTBEAT",
                os.getenv("MAINTAIN_HEARTBEAT_PATH", "/app/data/position_manager_heartbeat"),
            ),
            kill_switch_file=os.getenv("KILL_SWITCH_FILE", "./data/HALT"),
            stale_heartbeat_seconds=_env_int(
                "TRADING_SUPERVISOR_STALE_HEARTBEAT_SEC", max(180, maintain_poll * 6)
            ),
            evaluation_grace_seconds=_env_int(
                "TRADING_SUPERVISOR_EVAL_GRACE_SEC", max(180, maintain_poll * 6)
            ),
            min_position_age_seconds=_env_int(
                "TRADING_SUPERVISOR_MIN_POSITION_AGE_SEC", max(45, maintain_poll * 2)
            ),
            close_failed_window_minutes=_env_int(
                "TRADING_SUPERVISOR_CLOSE_FAILED_WINDOW_MIN", 15
            ),
            close_failed_threshold=_env_int(
                "TRADING_SUPERVISOR_CLOSE_FAILED_THRESHOLD", 5
            ),
            settlement_max_age_minutes=_env_int(
                "TRADING_SUPERVISOR_SETTLEMENT_MAX_AGE_MIN", 15
            ),
            enforce_halt=_env_bool("TRADING_SUPERVISOR_ENFORCE_HALT", True),
        )


class TradingSupervisor:
    def __init__(
        self,
        trade_log: TradeLog,
        cfg: Optional[TradingSupervisorConfig] = None,
    ):
        self.trade_log = trade_log
        self.cfg = cfg or TradingSupervisorConfig.from_env()

    def run_once(self) -> dict:
        now = _now()
        open_positions = self.trade_log.filled_positions_with_id()
        issues: list[dict] = []

        self._check_position_manager_heartbeat(now, open_positions, issues)
        self._check_open_positions(now, open_positions, issues)
        self._check_close_failure_storm(issues)
        self._check_settlement_reconciliation(issues)
        self._check_news_alerts(open_positions, issues)

        critical_issues = [i for i in issues if i.get("severity") == "critical"]
        halted = False
        if critical_issues and self.cfg.enforce_halt:
            halted = self._trip_halt(critical_issues)

        result = {
            "ts": now.isoformat(),
            "status": "critical" if critical_issues else ("warning" if issues else "ok"),
            "open_positions": len(open_positions),
            "issues": issues,
            "halted": halted,
            "enforce_halt": self.cfg.enforce_halt,
        }
        self._write_state(result)
        self._heartbeat()
        if critical_issues:
            logger.error("trading_supervisor critical: %s", result)
        elif issues:
            logger.warning("trading_supervisor warning: %s", result)
        else:
            logger.info("trading_supervisor ok: open_positions=%d", len(open_positions))
        return result

    def _check_position_manager_heartbeat(
        self,
        now: datetime,
        open_positions: list[dict],
        issues: list[dict],
    ) -> None:
        hb = Path(self.cfg.position_manager_heartbeat_path)
        if not hb.exists():
            severity = "critical" if open_positions else "warning"
            issues.append({
                "severity": severity,
                "code": "position_manager_heartbeat_missing",
                "message": f"position_manager heartbeat missing: {hb}",
            })
            return
        try:
            age = now.timestamp() - hb.stat().st_mtime
        except OSError as exc:
            issues.append({
                "severity": "critical" if open_positions else "warning",
                "code": "position_manager_heartbeat_unreadable",
                "message": str(exc),
            })
            return
        if age > self.cfg.stale_heartbeat_seconds:
            issues.append({
                "severity": "critical" if open_positions else "warning",
                "code": "position_manager_heartbeat_stale",
                "age_seconds": round(age, 3),
                "threshold_seconds": self.cfg.stale_heartbeat_seconds,
            })

    def _check_open_positions(
        self,
        now: datetime,
        open_positions: list[dict],
        issues: list[dict],
    ) -> None:
        for pos in open_positions:
            opened = _parse_ts(pos.get("ts"))
            token_id = str(pos.get("token_id") or "")
            if not opened or not token_id:
                issues.append({
                    "severity": "critical",
                    "code": "open_position_missing_ts_or_token",
                    "trade_id": pos.get("id"),
                    "market_id": pos.get("market_id"),
                })
                continue
            age = (now - opened).total_seconds()
            if age < self.cfg.min_position_age_seconds:
                continue

            mark = self._latest_position_mark(token_id)
            if mark is None:
                issues.append({
                    "severity": "critical",
                    "code": "open_position_without_position_mark",
                    "trade_id": pos.get("id"),
                    "market_id": pos.get("market_id"),
                    "token_id": token_id,
                    "age_seconds": round(age, 3),
                })
            else:
                last_seen = _parse_ts(mark.get("last_seen_ts"))
                stale = (
                    last_seen is None
                    or last_seen < opened
                    or (now - last_seen).total_seconds() > self.cfg.evaluation_grace_seconds
                )
                if stale:
                    issues.append({
                        "severity": "critical",
                        "code": "position_mark_stale",
                        "trade_id": pos.get("id"),
                        "market_id": pos.get("market_id"),
                        "token_id": token_id,
                        "last_seen_ts": mark.get("last_seen_ts"),
                        "threshold_seconds": self.cfg.evaluation_grace_seconds,
                    })

            decision = self._latest_exit_decision(token_id, opened)
            if decision is None:
                issues.append({
                    "severity": "critical",
                    "code": "open_position_without_exit_decision",
                    "trade_id": pos.get("id"),
                    "market_id": pos.get("market_id"),
                    "token_id": token_id,
                    "age_seconds": round(age, 3),
                })
            else:
                decision_ts = _parse_ts(decision.get("ts"))
                if (
                    decision_ts is None
                    or (now - decision_ts).total_seconds() > self.cfg.evaluation_grace_seconds
                ):
                    issues.append({
                        "severity": "critical",
                        "code": "exit_decision_stale",
                        "trade_id": pos.get("id"),
                        "market_id": pos.get("market_id"),
                        "token_id": token_id,
                        "last_decision_ts": decision.get("ts"),
                        "reason": decision.get("reason"),
                        "threshold_seconds": self.cfg.evaluation_grace_seconds,
                    })

    def _check_close_failure_storm(self, issues: list[dict]) -> None:
        recent_failures = self.trade_log.count_recent(
            "close_failed",
            hours=max(self.cfg.close_failed_window_minutes / 60.0, 0.01),
        )
        if recent_failures >= self.cfg.close_failed_threshold:
            issues.append({
                "severity": "critical",
                "code": "close_failed_storm",
                "recent_close_failed": recent_failures,
                "window_minutes": self.cfg.close_failed_window_minutes,
                "threshold": self.cfg.close_failed_threshold,
            })

    def _check_settlement_reconciliation(self, issues: list[dict]) -> None:
        rows = self.trade_log.latest_settlement_reconciliations(
            max_age_minutes=self.cfg.settlement_max_age_minutes,
        )
        critical = set(self.cfg.settlement_critical_statuses)
        for row in rows:
            if row.get("status") not in critical:
                continue
            issues.append({
                "severity": "critical",
                "code": "settlement_reconciliation_requires_action",
                "token_id": row.get("token_id"),
                "market_id": row.get("market_id"),
                "status": row.get("status"),
                "action": row.get("action"),
                "recoverable_usdc": row.get("recoverable_usdc"),
                "redeemable_usdc": row.get("redeemable_usdc"),
                "updated_ts": row.get("updated_ts"),
            })

    def _latest_position_mark(self, token_id: str) -> Optional[dict]:
        with self.trade_log._lock, self.trade_log._connect() as conn:
            row = conn.execute(
                "SELECT * FROM position_marks WHERE token_id = ?",
                (str(token_id),),
            ).fetchone()
            return dict(row) if row else None

    def _latest_exit_decision(
        self, token_id: str, opened: datetime
    ) -> Optional[dict]:
        with self.trade_log._lock, self.trade_log._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM brain_decisions
                WHERE agent = 'position_manager'
                  AND decision_type = 'exit'
                  AND token_id = ?
                  AND ts >= ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (str(token_id), opened.isoformat()),
            ).fetchone()
            return dict(row) if row else None

    def _check_news_alerts(
        self,
        open_positions: list[dict],
        issues: list[dict],
    ) -> None:
        """Scan Tavily for breaking news that contradicts open positions.

        This is a WARNING-only check — it never trips HALT. Its purpose is
        to surface adverse news in the supervisor state file and logs so the
        operator (and position_manager LLM) can act on it quickly.

        Runs silently (no issue added) if TAVILY_API_KEY is not set.
        """
        if not open_positions:
            return
        api_key = os.getenv("TAVILY_API_KEY", "").strip()
        if not api_key:
            return  # No key — skip quietly.
        for pos in open_positions:
            market_id = str(pos.get("market_id") or "")
            if not market_id:
                continue
            # Resolve market question: try news_signals, then wallet_signals.
            question = None
            try:
                with self.trade_log._lock, self.trade_log._connect() as conn:
                    row = conn.execute(
                        "SELECT market_question FROM news_signals "
                        "WHERE market_id = ? AND market_question IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1",
                        (market_id,),
                    ).fetchone()
                    if row and row[0]:
                        question = str(row[0])
                    else:
                        row = conn.execute(
                            "SELECT market_question FROM wallet_signals "
                            "WHERE market_id = ? AND market_question IS NOT NULL "
                            "ORDER BY id DESC LIMIT 1",
                            (market_id,),
                        ).fetchone()
                        if row and row[0]:
                            question = str(row[0])
            except Exception:
                continue
            if not question:
                continue
            try:
                ctx = tavily_headlines(question, max_results=2)
                if not ctx:
                    continue
                logger.info(
                    "trading_supervisor: news_alert market=%s news=%s",
                    market_id, ctx[:200],
                )
                issues.append({
                    "severity": "warning",
                    "code": "position_news_alert",
                    "market_id": market_id,
                    "question_preview": question[:80],
                    "news_preview": ctx[:200],
                })
            except Exception:
                logger.debug(
                    "trading_supervisor: Tavily lookup failed for %s (non-fatal)",
                    market_id,
                )

    def _trip_halt(self, critical_issues: list[dict]) -> bool:
        halt_path = Path(self.cfg.kill_switch_file)
        halt_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": _now().isoformat(),
            "source": "trading_supervisor",
            "reason": "critical_exit_path_guard",
            "issues": critical_issues,
        }
        halt_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        try:
            self.trade_log.insert_terminal(
                cycle_id="trading_supervisor",
                market_id="__supervisor__",
                status="supervisor_halt",
                response=payload,
                error="critical exit-path guard tripped",
            )
        except Exception:
            logger.exception("trading_supervisor failed to journal halt event")
        return True

    def _write_state(self, result: dict) -> None:
        path = Path(self.cfg.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2, sort_keys=True))

    def _heartbeat(self) -> None:
        path = Path(self.cfg.heartbeat_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


class TradingSupervisorDaemon:
    def __init__(self, db_path: Optional[str] = None):
        self.cfg = TradingSupervisorConfig.from_env()
        self.trade_log = TradeLog(db_path=db_path)
        self.engine = TradingSupervisor(self.trade_log, self.cfg)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            signal.signal(signal.SIGTERM, lambda *_: self.stop())
            signal.signal(signal.SIGINT, lambda *_: self.stop())
        except (ValueError, OSError):
            pass
        logger.info(
            "TradingSupervisorDaemon: starting poll=%ss enforce_halt=%s",
            self.cfg.poll_seconds,
            self.cfg.enforce_halt,
        )
        try:
            while not self._stop.is_set():
                try:
                    self.engine.run_once()
                except Exception:
                    logger.exception("trading_supervisor cycle failed")
                self._stop.wait(self.cfg.poll_seconds)
        finally:
            logger.info("TradingSupervisorDaemon: exited")


def main() -> int:
    parser = argparse.ArgumentParser(description="poly1 trading supervisor")
    parser.add_argument("--once", action="store_true", help="run one check and exit")
    parser.add_argument("--json", action="store_true", help="print JSON result in --once mode")
    parser.add_argument(
        "--no-enforce",
        action="store_true",
        help="do not write HALT even when critical issues are found",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    if args.once:
        cfg = TradingSupervisorConfig.from_env()
        if args.no_enforce:
            cfg.enforce_halt = False
        result = TradingSupervisor(TradeLog(), cfg).run_once()
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        return 2 if result["status"] == "critical" else 0

    TradingSupervisorDaemon().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
