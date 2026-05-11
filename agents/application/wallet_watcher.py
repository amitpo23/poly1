"""Wallet-Watcher — poly1 smart-wallet signal producer.

Monitors Polymarket accounts that have demonstrated profitable trading.
For each trade detected from a watched wallet (bought YES or NO on an
active binary market), the watcher writes a row to the ``wallet_signals``
table. The ``wallet_follow`` agent reads that table and decides whether to
copy-enter the same position.

Two modes of operation
----------------------
1. **Static watch-list** — operator supplies ``WALLET_WATCH_ADDRESSES`` as a
   comma-separated list of proxy wallet addresses.
2. **Auto-scout** (opt-in via ``WALLET_SCOUT_ENABLE=true``) — periodically
   fetches the Polymarket public leaderboard and promotes wallets that meet
   the profit/trade thresholds into the watch-list. Discovered addresses are
   stored in-memory only; they do *not* persist across restarts, so each
   watcher boot re-scouts fresh.

Signal freshness
----------------
Only trades made within the last ``WALLET_WATCHER_MAX_AGE_HOURS`` hours are
considered.  Duplicate signals for the same (wallet, market) pair within the
same age window are suppressed by a DB uniqueness check.

Storage: ``wallet_signals`` table in the shared SQLite ledger.

Environment variables (all optional, see defaults below):
  WALLET_WATCH_ADDRESSES       — comma-separated hex addresses (default "")
  WALLET_SCOUT_ENABLE          — "true" to auto-discover top traders (default false)
  WALLET_SCOUT_LIMIT           — max wallets from leaderboard (default 20)
  WALLET_SCOUT_MIN_PROFIT_USDC — min 30d profit to qualify for scout (default 200)
  WALLET_SCOUT_MIN_TRADES      — min 30d trade count to qualify (default 15)
  WALLET_WATCHER_POLL_SEC      — loop cadence in seconds (default 120)
  WALLET_WATCHER_MAX_AGE_HOURS — max trade age to generate signals (default 4)
  WALLET_WATCHER_HEARTBEAT_PATH — file path for heartbeat (default /app/data/wallet_watcher_heartbeat)
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

from agents.application.trade_log import TradeLog

logger = logging.getLogger(__name__)

DATA_API_URL = "https://data-api.polymarket.com"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class WalletWatcherConfig:
    watch_addresses: list[str] = field(default_factory=list)
    scout_enable: bool = False
    scout_limit: int = 20
    scout_min_profit_usdc: float = 200.0
    scout_min_trades: int = 15
    poll_sec: int = 120
    max_age_hours: float = 4.0
    heartbeat_path: str = "/app/data/wallet_watcher_heartbeat"

    @classmethod
    def from_env(cls) -> "WalletWatcherConfig":
        raw_addrs = os.getenv("WALLET_WATCH_ADDRESSES", "")
        addresses = [a.strip() for a in raw_addrs.split(",") if a.strip()]
        return cls(
            watch_addresses=addresses,
            scout_enable=os.getenv("WALLET_SCOUT_ENABLE", "false").lower() == "true",
            scout_limit=_env_int("WALLET_SCOUT_LIMIT", 20),
            scout_min_profit_usdc=_env_float("WALLET_SCOUT_MIN_PROFIT_USDC", 200.0),
            scout_min_trades=_env_int("WALLET_SCOUT_MIN_TRADES", 15),
            poll_sec=_env_int("WALLET_WATCHER_POLL_SEC", 120),
            max_age_hours=_env_float("WALLET_WATCHER_MAX_AGE_HOURS", 4.0),
            heartbeat_path=os.getenv(
                "WALLET_WATCHER_HEARTBEAT_PATH",
                "/app/data/wallet_watcher_heartbeat",
            ),
        )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class WalletWatcherEngine:
    def __init__(self, trade_log: TradeLog, cfg: WalletWatcherConfig):
        self.trade_log = trade_log
        self.cfg = cfg
        # In-memory set of all addresses currently being watched
        # (static list + scouted addresses).
        self._watched: set[str] = set(cfg.watch_addresses)
        # Cache wallet performance stats from leaderboard to attach to signals.
        self._wallet_stats: dict[str, dict] = {}

    # -------------------------------------------------------- public API

    def run_once(self) -> int:
        """Refresh watch-list (optional scout) then poll each wallet for new
        trades. Returns the total number of new signals written."""
        if self.cfg.scout_enable:
            self._scout_leaderboard()

        if not self._watched:
            logger.debug("wallet_watcher: no addresses to watch")
            return 0

        total = 0
        for address in list(self._watched):
            try:
                total += self._poll_wallet(address)
            except Exception as exc:
                logger.warning("wallet_watcher: poll %s failed: %s", address, exc)
        return total

    # ------------------------------------------------------ leaderboard scout

    def _scout_leaderboard(self) -> None:
        """Fetch public leaderboard and add high-performers to watch list."""
        try:
            params = urllib.parse.urlencode({
                "window": "all",
                "limit": self.cfg.scout_limit,
            })
            url = f"{DATA_API_URL}/leaderboard?{params}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "poly1-wallet-watcher/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
        except Exception as exc:
            logger.warning("wallet_watcher: leaderboard fetch failed: %s", exc)
            return

        if not isinstance(data, list):
            return

        added = 0
        for entry in data:
            addr = str(entry.get("proxyWallet") or entry.get("address") or "").lower()
            if not addr or not addr.startswith("0x"):
                continue
            profit = float(entry.get("profit") or entry.get("profitLoss") or 0.0)
            trades = int(entry.get("tradesCount") or entry.get("numTrades") or 0)
            if profit < self.cfg.scout_min_profit_usdc:
                continue
            if trades < self.cfg.scout_min_trades:
                continue
            if addr not in self._watched:
                self._watched.add(addr)
                added += 1
            # Cache stats for later signal enrichment
            self._wallet_stats[addr] = {
                "profit_usdc": profit,
                "trades_30d": trades,
            }

        if added:
            logger.info("wallet_watcher: scouted %d new wallets (total=%d)", added, len(self._watched))

    # ------------------------------------------------------- activity polling

    def _poll_wallet(self, address: str) -> int:
        """Fetch recent activity for one wallet; write new wallet_signals.
        Returns count of signals written."""
        try:
            params = urllib.parse.urlencode({"user": address, "limit": 50})
            url = f"{DATA_API_URL}/activity?{params}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "poly1-wallet-watcher/1.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                trades = json.loads(resp.read())
        except Exception as exc:
            logger.warning("wallet_watcher: activity fetch %s failed: %s", address, exc)
            return 0

        if not isinstance(trades, list):
            return 0

        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=self.cfg.max_age_hours)
        written = 0

        for trade in trades:
            ts_epoch = int(trade.get("timestamp") or trade.get("ts") or 0)
            if ts_epoch == 0:
                continue
            trade_dt = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
            if trade_dt < cutoff_dt:
                # Activity API is ordered newest-first; once we hit old trades
                # we can stop processing.
                break

            market_id = str(trade.get("conditionId") or trade.get("marketId") or "")
            if not market_id:
                continue

            trade_type = str(trade.get("type") or "").upper()
            if trade_type not in ("BUY", "SELL"):
                continue

            # Determine direction: BUY = bought YES tokens (bullish),
            # SELL = sold YES tokens (bearish). Some API versions label the
            # outcome directly; fall back to token index if available.
            outcome = str(trade.get("outcome") or "").upper()
            if trade_type == "BUY":
                direction = "bullish" if outcome in ("YES", "") else "bearish"
            else:
                direction = "bearish" if outcome in ("YES", "") else "bullish"

            token_id = str(trade.get("asset") or trade.get("tokenId") or "")
            wallet_entry_price = float(trade.get("price") or 0.0)
            wallet_size_usdc = float(trade.get("size") or trade.get("usdcSize") or 0.0)
            market_question = str(trade.get("title") or trade.get("question") or "")

            ts_str = trade_dt.isoformat()
            stats = self._wallet_stats.get(address.lower(), {})
            wallet_profit = float(stats.get("profit_usdc") or 0.0)
            wallet_trades = int(stats.get("trades_30d") or 0)

            # Fetch current yes_price from Gamma for signal enrichment.
            yes_price = self._fetch_yes_price(market_id)

            if self._write_signal(
                ts=ts_str,
                wallet_address=address,
                wallet_profit_usdc=wallet_profit,
                wallet_trades_30d=wallet_trades,
                market_id=market_id,
                market_question=market_question,
                direction=direction,
                token_id=token_id,
                yes_price=yes_price,
                wallet_entry_price=wallet_entry_price,
                wallet_size_usdc=wallet_size_usdc,
            ):
                written += 1

        return written

    # ------------------------------------------------------- Gamma helper

    def _fetch_yes_price(self, market_id: str) -> Optional[float]:
        """Fetch current YES price from Gamma for a given market ID."""
        try:
            params = urllib.parse.urlencode({"id": market_id})
            url = f"{GAMMA_MARKETS_URL}?{params}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "poly1-wallet-watcher/1.0"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            mkt = data[0] if isinstance(data, list) and data else data
            if not isinstance(mkt, dict):
                return None
            prices = json.loads(mkt.get("outcomePrices", '["0.5","0.5"]'))
            return float(prices[0])
        except Exception:
            return None

    # ------------------------------------------------------- DB write

    def _write_signal(
        self,
        ts: str,
        wallet_address: str,
        wallet_profit_usdc: float,
        wallet_trades_30d: int,
        market_id: str,
        market_question: str,
        direction: str,
        token_id: str,
        yes_price: Optional[float],
        wallet_entry_price: float,
        wallet_size_usdc: float,
    ) -> bool:
        """Insert a wallet_signals row if no fresh duplicate exists.
        Returns True if a new row was written."""
        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=self.cfg.max_age_hours)
        cutoff_str = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S")

        with self.trade_log._lock, self.trade_log._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM wallet_signals "
                "WHERE wallet_address = ? AND market_id = ? AND ts >= ? LIMIT 1",
                (wallet_address, market_id, cutoff_str),
            ).fetchone()
            if existing:
                return False

            conn.execute(
                """
                INSERT INTO wallet_signals
                  (ts, wallet_address, wallet_profit_usdc, wallet_trades_30d,
                   market_id, market_question, direction, token_id,
                   yes_price, wallet_entry_price, wallet_size_usdc, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'fresh')
                """,
                (
                    ts,
                    wallet_address,
                    wallet_profit_usdc,
                    wallet_trades_30d,
                    market_id,
                    market_question,
                    direction,
                    token_id,
                    yes_price,
                    wallet_entry_price,
                    wallet_size_usdc,
                ),
            )
        logger.info(
            "wallet_watcher: new signal wallet=%s market=%s dir=%s entry_price=%.3f",
            wallet_address[:10],
            market_id[:16],
            direction,
            wallet_entry_price,
        )
        return True


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class WalletWatcherDaemon:
    """Long-running loop. SIGTERM-aware."""

    def __init__(self, db_path: Optional[str] = None):
        self.cfg = WalletWatcherConfig.from_env()
        self.trade_log = TradeLog(db_path=db_path)
        self.engine = WalletWatcherEngine(trade_log=self.trade_log, cfg=self.cfg)
        self._stop = threading.Event()

    def _handle_sigterm(self, *_) -> None:
        logger.info("wallet_watcher: SIGTERM received — stopping")
        self._stop.set()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
        logger.info(
            "wallet_watcher: starting — watch=%d scout=%s poll=%ds",
            len(self.cfg.watch_addresses),
            self.cfg.scout_enable,
            self.cfg.poll_sec,
        )

        while not self._stop.is_set():
            try:
                n = self.engine.run_once()
                if n:
                    logger.info("wallet_watcher: wrote %d new signals", n)
            except Exception as exc:
                logger.exception("wallet_watcher: unhandled error: %s", exc)

            # Heartbeat
            try:
                hb = self.cfg.heartbeat_path
                os.makedirs(os.path.dirname(hb) or ".", exist_ok=True)
                with open(hb, "w") as f:
                    f.write(datetime.now(timezone.utc).isoformat())
            except OSError:
                pass

            self._stop.wait(self.cfg.poll_sec)

        logger.info("wallet_watcher: stopped cleanly")


if __name__ == "__main__":
    WalletWatcherDaemon().run()
