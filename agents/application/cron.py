import logging
import os
import random
import signal
import time
from pathlib import Path
from typing import Optional

from agents.application.trade import Trader
from agents.application.trading_policy import MARKET_SCAN_SECONDS
from agents.utils.notify import notify_telegram, notify_trade, _safe_balance, ping_healthcheck


logger = logging.getLogger(__name__)


class TraderDaemon:
    def __init__(
        self,
        trader: Trader,
        poll_seconds: int = MARKET_SCAN_SECONDS,
        jitter_seconds: int = 30,
        heartbeat_path: Optional[str] = None,
        healthcheck_url: Optional[str] = None,
    ):
        if poll_seconds < 1:
            raise ValueError("poll_seconds must be >= 1")
        self.trader = trader
        self.poll_seconds = poll_seconds
        self.jitter_seconds = max(0, jitter_seconds)
        self.heartbeat_path = Path(
            heartbeat_path or os.getenv("HEARTBEAT_PATH", "./data/heartbeat")
        )
        self.healthcheck_url = healthcheck_url or os.getenv("HEALTHCHECK_URL")
        self._stopping = False

    def start(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)

        logger.info(
            "TraderDaemon: starting poll_seconds=%s jitter=%s dry_run=%s",
            self.poll_seconds,
            self.jitter_seconds,
            self.trader.dry_run,
        )
        self._heartbeat()
        try:
            bal = _safe_balance(self.trader.polymarket)
            bal_str = f" | balance=${bal:.2f}" if bal is not None else ""
            notify_telegram(
                f"poly1: daemon started (dry_run={self.trader.dry_run}){bal_str}"
            )
        except Exception:
            logger.exception("startup telegram notify failed")

        while not self._stopping:
            cycle_started = time.time()
            self._heartbeat()
            try:
                self.trader.one_best_trade_sweep()
            except Exception as e:
                logger.exception("daemon: cycle failed")
                try:
                    notify_telegram(f"poly1: cycle failed: {e}")
                except Exception:
                    logger.exception("cycle failure telegram notify failed")

            if self.healthcheck_url:
                try:
                    ping_healthcheck(self.healthcheck_url)
                except Exception:
                    logger.exception("healthcheck ping failed")

            sleep_for = self.poll_seconds + (
                random.randint(0, self.jitter_seconds) if self.jitter_seconds else 0
            )
            elapsed = time.time() - cycle_started
            logger.info(
                "daemon: cycle elapsed=%.1fs sleeping=%ss", elapsed, sleep_for
            )
            self._sleep_with_check(sleep_for)

        logger.info("TraderDaemon: stopped")
        try:
            notify_telegram("poly1: daemon stopped")
        except Exception:
            logger.exception("shutdown telegram notify failed")

    def _heartbeat(self) -> None:
        try:
            self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            self.heartbeat_path.touch()
        except OSError as e:
            logger.warning("heartbeat write failed: %s", e)

    def _handle_stop(self, signum, frame) -> None:
        logger.info("TraderDaemon: received signal %s, stopping after current cycle", signum)
        self._stopping = True

    def _sleep_with_check(self, total_seconds: int) -> None:
        end = time.time() + total_seconds
        while not self._stopping and time.time() < end:
            self._heartbeat()
            time.sleep(min(2, end - time.time()))
