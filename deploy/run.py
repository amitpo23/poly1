"""Container entrypoint: configure logging, validate env, run the daemon."""
import logging
import os
import sys

from agents.application.cron import TraderDaemon
from agents.application.trade import Trader
from agents.application.trading_policy import MARKET_SCAN_SECONDS
from agents.utils.logging_setup import configure_logging


REQUIRED_ENV = ["POLYGON_WALLET_PRIVATE_KEY", "OPENAI_API_KEY"]


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def main() -> int:
    configure_logging()
    logger = logging.getLogger(__name__)

    missing = [v for v in REQUIRED_ENV if not os.getenv(v)]
    if missing:
        logger.error("missing required env vars: %s", missing)
        return 2

    execute = os.getenv("EXECUTE", "false").lower() == "true"
    poll_seconds = _env_int("CYCLE_SECONDS", MARKET_SCAN_SECONDS)
    jitter = _env_int("CYCLE_JITTER_SECONDS", 30)
    top_n = _env_int("TOP_N", 3)
    max_trades_per_cycle = _env_int("MAX_TRADES_PER_CYCLE", 2)
    max_position_fraction = _env_float("MAX_POSITION_FRACTION", 0.05)
    min_confidence = _env_float("MIN_CONFIDENCE", 0.60)

    logger.info(
        "deploy/run.py: execute=%s top_n=%s poll=%ss min_confidence=%s "
        "max_position_fraction=%s",
        execute, top_n, poll_seconds, min_confidence, max_position_fraction,
    )

    trader = Trader(
        dry_run=not execute,
        top_n=top_n,
        max_trades_per_cycle=max_trades_per_cycle,
        max_position_fraction=max_position_fraction,
        min_confidence=min_confidence,
    )
    daemon = TraderDaemon(
        trader=trader,
        poll_seconds=poll_seconds,
        jitter_seconds=jitter,
    )
    daemon.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
