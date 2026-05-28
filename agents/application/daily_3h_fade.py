"""Daily BTC up/down fade-the-mispricing agent.

STRATEGY
--------
At ~3 hours before the daily BTC up/down market closes (16:00 UTC), if the
market is still uncertain (YES price strictly between 0.20 and 0.80), buy
DOWN (= SELL side per poly1 token mapping). No exit logic — the market
self-resolves at 16:00 UTC and `resolution_sync` records the payout.

BACKTEST CONTEXT (2026-04-28 → 2026-05-22, n=13)
  WR=69%, EV=+16.7%/trade, max drawdown=$1.03. Caveat: most wins in the
  last 11 days (BTC trended DOWN). Bootstrap 95% CI: EV/$1 in [-$0.026,
  +$0.356]. P(EV>0)=95.5%. Treat as suggestive, not proven.

USAGE
-----
    python -m agents.application.daily_3h_fade

Run via cron at 13:00 UTC (= 3h before 16:00 UTC market close). Idempotent:
re-runs of the same day skip if a position already exists.

CONFIG (env)
------------
  EXECUTE_DAILY_3H_FADE          true/false (default: false = shadow)
  DAILY_3H_FADE_POSITION_USDC    position size in USDC (default: 1.00)
  DAILY_3H_FADE_BAND_LOW         lower YES price bound (default: 0.20)
  DAILY_3H_FADE_BAND_HIGH        upper YES price bound (default: 0.80)
  DAILY_3H_FADE_LOSS_CAP_USDC    halt new entries when 14d cum PnL < -X
                                 (default: 4.00)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com/markets"


@dataclass
class Config:
    execute: bool = False
    position_usdc: float = 1.00
    band_low: float = 0.20
    band_high: float = 0.80
    loss_cap_usdc: float = 4.00
    target_hours_to_close_min: float = 2.5
    target_hours_to_close_max: float = 3.5

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            execute=os.getenv("EXECUTE_DAILY_3H_FADE", "false").lower() == "true",
            position_usdc=float(os.getenv("DAILY_3H_FADE_POSITION_USDC", "1.00")),
            band_low=float(os.getenv("DAILY_3H_FADE_BAND_LOW", "0.20")),
            band_high=float(os.getenv("DAILY_3H_FADE_BAND_HIGH", "0.80")),
            loss_cap_usdc=float(os.getenv("DAILY_3H_FADE_LOSS_CAP_USDC", "4.00")),
        )


def format_slug(when: datetime) -> str:
    month = when.strftime("%B").lower()
    return f"bitcoin-up-or-down-on-{month}-{when.day}-{when.year}"


def fetch_market(slug: str) -> dict | None:
    url = f"{GAMMA}?slug={slug}"
    req = urllib.request.Request(url, headers={"User-Agent": "poly1-daily-3h-fade/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode())
    return d[0] if d else None


def cum_pnl_last_14d(trade_log) -> float:
    """Sum of resolved PnL for daily_3h_fade trades in the last 14 days."""
    import sqlite3
    with sqlite3.connect(trade_log.db_path, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT response_json, size_usdc, status FROM trades
            WHERE status IN ('resolved_yes','resolved_no','resolved_loss')
              AND ts > datetime('now','-14 days')
              AND response_json LIKE '%"agent":"daily_3h_fade"%'
            """
        ).fetchall()
    total = 0.0
    for r in rows:
        try:
            payload = json.loads(r["response_json"] or "{}")
            pnl = payload.get("pnl_usdc_real")
            if pnl is not None:
                total += float(pnl)
        except Exception:
            pass
    return total


def already_open_today(trade_log, market_id: str) -> bool:
    """True if a daily_3h_fade position already exists for this market_id."""
    import sqlite3
    with sqlite3.connect(trade_log.db_path, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT id FROM trades WHERE market_id=? AND status=? LIMIT 1""",
            (market_id, "daily_3h_fade_open"),
        ).fetchone()
    return row is not None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = Config.from_env()
    logger.info(
        "daily_3h_fade starting | execute=%s position=$%.2f band=[%.2f,%.2f] loss_cap=$%.2f",
        cfg.execute, cfg.position_usdc, cfg.band_low, cfg.band_high, cfg.loss_cap_usdc,
    )

    # Find today's market
    today_utc = datetime.now(timezone.utc)
    slug = format_slug(today_utc)
    try:
        market = fetch_market(slug)
    except Exception as exc:
        logger.error("daily_3h_fade: failed to fetch market %s: %s", slug, exc)
        return 1
    if market is None:
        logger.warning("daily_3h_fade: no market found for slug=%s", slug)
        return 0
    if market.get("closed"):
        logger.info("daily_3h_fade: market already closed (slug=%s)", slug)
        return 0

    # Hours to close
    end_dt = datetime.fromisoformat(market["endDate"].replace("Z", "+00:00"))
    hours_to_close = (end_dt - today_utc).total_seconds() / 3600
    logger.info("daily_3h_fade: %s, hours_to_close=%.2f", slug, hours_to_close)
    if not (cfg.target_hours_to_close_min <= hours_to_close <= cfg.target_hours_to_close_max):
        logger.info(
            "daily_3h_fade: outside entry window [%.1f,%.1f] — skipping",
            cfg.target_hours_to_close_min, cfg.target_hours_to_close_max,
        )
        return 0

    # Band check
    prices = json.loads(market.get("outcomePrices", "[]"))
    if len(prices) < 2:
        logger.warning("daily_3h_fade: missing prices on %s", slug)
        return 0
    yes_price = float(prices[0])
    if not (cfg.band_low < yes_price < cfg.band_high):
        logger.info(
            "daily_3h_fade: YES=%.3f outside band [%.2f,%.2f] — skipping (market decided)",
            yes_price, cfg.band_low, cfg.band_high,
        )
        return 0

    market_id = market.get("id") or market.get("conditionId")
    token_ids = json.loads(market.get("clobTokenIds", "[]"))
    if not market_id or len(token_ids) < 2:
        logger.warning("daily_3h_fade: missing market_id or token_ids on %s", slug)
        return 0
    no_token_id = token_ids[1]  # DOWN (NO) token

    # Lazy imports of heavy modules (avoid Polymarket() during shadow / tests)
    from agents.application.trade_log import TradeLog, DAILY_3H_FADE_OPEN
    tl = TradeLog()

    # Idempotency
    if already_open_today(tl, market_id):
        logger.info("daily_3h_fade: position already exists for %s, skipping", market_id)
        return 0

    # Kill switch
    cum = cum_pnl_last_14d(tl)
    if cum < -cfg.loss_cap_usdc:
        logger.warning(
            "daily_3h_fade: 14d cum PnL=$%.2f < -$%.2f kill switch tripped, skipping",
            cum, cfg.loss_cap_usdc,
        )
        return 0

    logger.info(
        "daily_3h_fade: GATES PASSED — YES=%.3f DOWN=%.3f token=%s 14d_pnl=$%+.2f",
        yes_price, 1 - yes_price, no_token_id[:24], cum,
    )

    cycle_id = tl.new_cycle_id()
    pending_id = tl.insert_pending(
        cycle_id=cycle_id,
        market_id=str(market_id),
        token_id=no_token_id,
        side="SELL",   # SELL ≡ buy NO/DOWN per poly1 convention
        price=yes_price,
        size_usdc=cfg.position_usdc,
        confidence=0.6,
    )

    if not cfg.execute:
        tl.mark(
            pending_id, DAILY_3H_FADE_OPEN,
            response={
                "agent": "daily_3h_fade", "shadow": True, "side": "SELL",
                "yes_price_at_entry": yes_price, "down_price_at_entry": 1 - yes_price,
                "hours_to_close": hours_to_close, "slug": slug,
            },
            error=f"SHADOW: would have bought DOWN at {1 - yes_price:.3f}",
        )
        logger.info(
            "daily_3h_fade SHADOW: would buy DOWN @ %.3f on %s ($%.2f)",
            1 - yes_price, slug, cfg.position_usdc,
        )
        return 0

    # Live execution — model after btc_daily.py
    from agents.polymarket.polymarket import Polymarket
    from agents.application.risk_gate import RiskGate
    from agents.utils.objects import TradeRecommendation

    pm = Polymarket(live=True)
    risk_gate = RiskGate(trade_log=tl, polymarket=pm)
    if not risk_gate.ok():
        reason = risk_gate.reason()
        logger.warning("daily_3h_fade: risk_gate blocks entry: %s", reason)
        tl.mark(pending_id, "skipped_gate", error=f"risk_gate: {reason}")
        return 0

    recommendation = TradeRecommendation(
        price=yes_price, size_fraction=0.0, side="SELL",
        confidence=0.6, amount_usdc=cfg.position_usdc,
    )
    try:
        response = pm.execute_market_order(
            ({"id": str(market_id), "token_ids": token_ids, "outcomes": ["Up", "Down"]}, 0.0),
            recommendation,
        )
    except Exception as exc:
        logger.warning("daily_3h_fade: execute_market_order raised: %s", exc)
        tl.mark(pending_id, "failed", error=f"execute_market_order raised: {exc}")
        return 1

    if not response or response.get("status") not in ("matched", "filled"):
        logger.warning("daily_3h_fade: entry not matched: %s", response)
        tl.mark(pending_id, "failed", response=response, error="entry not matched")
        return 1

    entry_price = float(response.get("order_avg_price_estimate", 1 - yes_price))
    entry_size = float(response.get("amount_usdc", cfg.position_usdc))
    response = {
        **response, "agent": "daily_3h_fade", "actual_entry_price": entry_price,
        "yes_price_at_entry": yes_price, "slug": slug,
        "hours_to_close_at_entry": hours_to_close,
    }
    tl.mark(
        pending_id, DAILY_3H_FADE_OPEN,
        response=response, price=entry_price, size_usdc=entry_size,
    )
    logger.info(
        "daily_3h_fade LIVE: bought DOWN @ %.3f size=$%.2f on %s",
        entry_price, entry_size, slug,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
