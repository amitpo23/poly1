"""Order-book monitor for execution-quality decisions.

The monitor keeps a compact, queryable view of Polymarket CLOB microstructure:
best bid/ask, spread, depth, imbalance, and simulated buy slippage.  MetaBrain
uses this as a pre-trade execution gate so good forecasts are not ruined by bad
books.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from agents.application.trade_log import TradeLog


logger = logging.getLogger(__name__)


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


@dataclass
class OrderbookMonitorConfig:
    poll_sec: float = 1.0
    token_limit: int = 60
    prune_minutes: int = 180
    heartbeat_path: str = "/app/data/orderbook_monitor_heartbeat"
    clob_url: str = "https://clob.polymarket.com"
    stale_market_grace_sec: int = 300
    shadow_lookback_hours: int = 24
    brain_shadow_lookback_hours: int = 6
    watch_5min_crypto: bool = True

    @classmethod
    def from_env(cls) -> "OrderbookMonitorConfig":
        return cls(
            poll_sec=_env_float("ORDERBOOK_MONITOR_POLL_SEC", 1.0),
            token_limit=_env_int("ORDERBOOK_MONITOR_TOKEN_LIMIT", 60),
            prune_minutes=_env_int("ORDERBOOK_MONITOR_PRUNE_MINUTES", 180),
            heartbeat_path=os.getenv(
                "ORDERBOOK_MONITOR_HEARTBEAT_PATH",
                "/app/data/orderbook_monitor_heartbeat",
            ),
            clob_url=os.getenv("ORDERBOOK_MONITOR_CLOB_URL", "https://clob.polymarket.com"),
            stale_market_grace_sec=_env_int("ORDERBOOK_MONITOR_STALE_MARKET_GRACE_SEC", 300),
            shadow_lookback_hours=_env_int("ORDERBOOK_MONITOR_SHADOW_LOOKBACK_HOURS", 24),
            brain_shadow_lookback_hours=_env_int("ORDERBOOK_MONITOR_BRAIN_SHADOW_LOOKBACK_HOURS", 6),
            watch_5min_crypto=os.getenv(
                "ORDERBOOK_MONITOR_WATCH_5MIN_CRYPTO", "true"
            ).lower() in ("1", "true", "yes"),
        )


def normalize_book(book: object) -> dict:
    bids = _entries(book, "bids")
    asks = _entries(book, "asks")
    bids = sorted(bids, key=lambda x: x[0], reverse=True)
    asks = sorted(asks, key=lambda x: x[0])
    return {"bids": bids, "asks": asks}


def metrics_from_book(
    *,
    token_id: str,
    market_id: Optional[str],
    book: object,
    source: str,
) -> Optional[dict]:
    normalized = normalize_book(book)
    bids = normalized["bids"]
    asks = normalized["asks"]
    if not bids and not asks:
        return None
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    mid = (
        (best_bid + best_ask) / 2.0
        if best_bid is not None and best_ask is not None else None
    )
    spread_pct = (
        (best_ask - best_bid) / best_ask
        if best_bid is not None and best_ask and best_ask > 0 else None
    )
    bid_depth_usdc = sum(price * size for price, size in bids)
    ask_depth_usdc = sum(price * size for price, size in asks)
    denom = bid_depth_usdc + ask_depth_usdc
    imbalance = (bid_depth_usdc - ask_depth_usdc) / denom if denom > 0 else None
    avg_1 = _avg_buy_price(asks, 1.0)
    avg_3 = _avg_buy_price(asks, 3.0)
    avg_5 = _avg_buy_price(asks, 5.0)
    return {
        "token_id": str(token_id),
        "market_id": market_id,
        "source": source,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_pct": spread_pct,
        "bid_depth_usdc": bid_depth_usdc,
        "ask_depth_usdc": ask_depth_usdc,
        "bid_levels": len(bids),
        "ask_levels": len(asks),
        "imbalance": imbalance,
        "avg_buy_price_1": avg_1,
        "avg_buy_price_3": avg_3,
        "avg_buy_price_5": avg_5,
        "slippage_buy_1_pct": _slippage(avg_1, best_ask),
        "slippage_buy_3_pct": _slippage(avg_3, best_ask),
        "slippage_buy_5_pct": _slippage(avg_5, best_ask),
        "raw_json": {
            "bids_top": bids[:10],
            "asks_top": asks[:10],
        },
    }


class ClobBookFetcher:
    def __init__(self, clob_url: str = "https://clob.polymarket.com"):
        self.clob_url = clob_url.rstrip("/")
        self._client = None

    def fetch(self, token_id: str):
        try:
            return self._fetch_sdk(token_id)
        except Exception as exc:
            logger.debug("orderbook_monitor sdk fetch failed %s: %s", token_id[:18], exc)
        return self._fetch_http(token_id)

    def _fetch_sdk(self, token_id: str):
        if self._client is None:
            from py_clob_client_v2.client import ClobClient

            self._client = ClobClient(self.clob_url)
        return self._client.get_order_book(str(token_id))

    def _fetch_http(self, token_id: str) -> dict:
        query = urllib.parse.urlencode({"token_id": str(token_id)})
        req = urllib.request.Request(
            f"{self.clob_url}/book?{query}",
            headers={"User-Agent": "poly1-orderbook-monitor/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))


class OrderbookMonitorDaemon:
    def __init__(
        self,
        trade_log: Optional[TradeLog] = None,
        cfg: Optional[OrderbookMonitorConfig] = None,
        fetcher: Optional[ClobBookFetcher] = None,
    ):
        self.cfg = cfg or OrderbookMonitorConfig.from_env()
        self.trade_log = trade_log or TradeLog(
            db_path=os.getenv("TRADE_LOG_DB", "./data/trade_log.db")
        )
        self.fetcher = fetcher or ClobBookFetcher(self.cfg.clob_url)
        self._stop = False

    def run_once(self) -> dict:
        tokens = self._tokens()
        updated = 0
        errors = 0
        for item in tokens:
            token_id = str(item.get("token_id") or "")
            if not token_id:
                continue
            try:
                book = self.fetcher.fetch(token_id)
                row = metrics_from_book(
                    token_id=token_id,
                    market_id=item.get("market_id"),
                    book=book,
                    source="clob_rest",
                )
                if row is None:
                    continue
                self.trade_log.upsert_orderbook_snapshot(row)
                updated += 1
            except Exception as exc:
                errors += 1
                logger.debug("orderbook_monitor fetch failed %s: %s", token_id[:18], exc)
        pruned = self.trade_log.prune_orderbook_snapshots(self.cfg.prune_minutes)
        self._heartbeat()
        result = {"tokens": len(tokens), "updated": updated, "errors": errors, "pruned": pruned}
        logger.info("orderbook_monitor: %s", result)
        return result

    def run(self) -> None:
        logging.basicConfig(
            level=os.getenv("LOG_LEVEL", "INFO"),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        signal.signal(signal.SIGTERM, self._handle_stop)
        signal.signal(signal.SIGINT, self._handle_stop)
        logger.info(
            "orderbook_monitor: starting poll=%ss token_limit=%d",
            self.cfg.poll_sec,
            self.cfg.token_limit,
        )
        while not self._stop:
            try:
                self.run_once()
            except Exception:
                logger.exception("orderbook_monitor: cycle failed")
                self._heartbeat()
            time.sleep(max(0.5, self.cfg.poll_sec))

    def _tokens(self) -> list[dict]:
        seen: set[str] = set()
        result: list[dict] = []
        min_period_ts = int(time.time()) - int(self.cfg.stale_market_grace_sec)
        for item in self.trade_log.market_universe_tokens(
            limit=self.cfg.token_limit,
            min_period_ts=min_period_ts,
        ):
            token_id = str(item.get("token_id") or "")
            if token_id and token_id not in seen:
                seen.add(token_id)
                result.append(item)
        for item in self.trade_log.filled_positions_with_id():
            token_id = str(item.get("token_id") or "")
            if token_id and token_id not in seen:
                seen.add(token_id)
                result.append({
                    "market_id": item.get("market_id"),
                    "token_id": token_id,
                    "outcome": "open_position",
                })
        # Recent SHADOW_ENTER tokens — without these, shadow research
        # markets never accumulate orderbook snapshots, so synthetic
        # markouts can't be computed and Bayesian calibration stays
        # starved.
        try:
            shadow_items = self.trade_log.recent_shadow_decision_tokens(
                max_age_hours=int(self.cfg.shadow_lookback_hours)
            )
        except (AttributeError, sqlite3.OperationalError) as exc:
            logger.debug("orderbook_monitor: shadow tokens fetch failed: %s", exc)
            shadow_items = []
        for item in shadow_items:
            token_id = str(item.get("token_id") or "")
            if token_id and token_id not in seen:
                seen.add(token_id)
                result.append({
                    "market_id": item.get("market_id"),
                    "token_id": token_id,
                    "outcome": "shadow_research",
                })
        # Recent external_conviction SHADOW signals — operator 2026-05-25
        # discovered 4,639 SHADOW_BUY_* decisions had 0 orderbook coverage
        # because their tokens were never in the watchlist. Adding them
        # here unlocks P13 (shadow simulator) to measure edge per agent.
        try:
            brain_shadow_items = self.trade_log.recent_brain_shadow_tokens(
                max_age_hours=int(self.cfg.brain_shadow_lookback_hours),
            )
        except (AttributeError, sqlite3.OperationalError) as exc:
            logger.debug("orderbook_monitor: brain_shadow tokens fetch failed: %s", exc)
            brain_shadow_items = []
        for item in brain_shadow_items:
            token_id = str(item.get("token_id") or "")
            if token_id and token_id not in seen:
                seen.add(token_id)
                result.append({
                    "market_id": item.get("market_id"),
                    "token_id": token_id,
                    "outcome": f"brain_shadow:{item.get('agent', 'unknown')}",
                })
        # Current 5-min crypto markets — operator 2026-05-25 requested
        # validation of Polymarket DOWN price bias in the first minute,
        # which requires sub-minute snapshots on the active 5min market.
        # Without this, btc_5min markets accumulate ZERO snapshots
        # (they expire faster than market_universe can index them).
        if self.cfg.watch_5min_crypto:
            try:
                for item in self._current_5min_market_tokens():
                    token_id = str(item.get("token_id") or "")
                    if token_id and token_id not in seen:
                        seen.add(token_id)
                        result.append({
                            "market_id": item.get("market_id"),
                            "token_id": token_id,
                            "outcome": "btc_5min_window",
                        })
            except Exception as exc:
                logger.debug("orderbook_monitor: 5min token fetch failed: %s", exc)
        return result[: self.cfg.token_limit]

    def _current_5min_market_tokens(self) -> list[dict]:
        """Query Gamma for active 5-min crypto markets (BTC/ETH/SOL/DOGE/XRP).

        Returns both YES and NO token_ids so both sides can be tracked
        (BUY DOWN = SELL YES, so we need the NO-side prices too).
        """
        period_ts = int(time.time() // 300) * 300
        assets = ("btc", "eth", "sol", "doge", "xrp")
        out: list[dict] = []
        for asset in assets:
            slug = f"{asset}-updown-5m-{period_ts}"
            try:
                params = urllib.parse.urlencode({"slug": slug})
                url = f"https://gamma-api.polymarket.com/markets?{params}"
                req = urllib.request.Request(url, headers={
                    "User-Agent": "poly1-orderbook-monitor/1.0",
                })
                with urllib.request.urlopen(req, timeout=5) as r:
                    data = json.loads(r.read())
            except Exception:
                continue
            if not data:
                continue
            m = data[0]
            if not m.get("active", True) or m.get("closed", False):
                continue
            try:
                import ast
                tokens = ast.literal_eval(m.get("clobTokenIds") or "[]")
            except Exception:
                continue
            for tok in tokens:
                out.append({
                    "market_id": str(m.get("id") or ""),
                    "token_id": str(tok),
                    "asset": asset,
                    "slug": slug,
                })
        return out

    def _heartbeat(self) -> None:
        path = Path(self.cfg.heartbeat_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(time.time()))

    def _handle_stop(self, *_args) -> None:
        self._stop = True


def _entries(book: object, side: str) -> list[tuple[float, float]]:
    entries = getattr(book, side, None)
    if entries is None and isinstance(book, dict):
        entries = book.get(side, [])
    out: list[tuple[float, float]] = []
    for entry in entries or []:
        try:
            if hasattr(entry, "price"):
                price, size = float(entry.price), float(entry.size)
            else:
                price, size = float(entry["price"]), float(entry["size"])
            if price > 0 and size > 0:
                out.append((price, size))
        except Exception:
            continue
    return out


def _avg_buy_price(asks: Iterable[tuple[float, float]], amount_usdc: float) -> Optional[float]:
    remaining = float(amount_usdc)
    spend = 0.0
    tokens = 0.0
    for price, size in asks:
        if remaining <= 0:
            break
        level_cost = price * size
        take = min(remaining, level_cost)
        if take <= 0:
            continue
        spend += take
        tokens += take / price
        remaining -= take
    if spend <= 0 or tokens <= 0:
        return None
    return spend / tokens


def _slippage(avg_price: Optional[float], best_ask: Optional[float]) -> Optional[float]:
    if avg_price is None or best_ask is None or best_ask <= 0:
        return None
    return max(0.0, (avg_price - best_ask) / best_ask)


if __name__ == "__main__":
    OrderbookMonitorDaemon().run()
