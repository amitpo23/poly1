"""Manual end-to-end CLOB v2 order test.

Picks a liquid market from /sampling-simplified-markets, prints the orderbook,
and (only if EXECUTE=true) places a $1 BUY market order to prove the v2 SDK
wiring is correct after the 2026-05-04 Polymarket migration.

Usage:
    docker compose run --rm trader python scripts/python/manual_order_test.py
    # then, if dry inspection looks right:
    EXECUTE=true docker compose run --rm trader python scripts/python/manual_order_test.py
"""

from __future__ import annotations

import os
import sys
import json
import logging

from agents.polymarket.polymarket import Polymarket

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("manual_order")


def find_liquid_market(p: Polymarket) -> dict | None:
    """Return one liquid binary market with mid-range price (0.20 .. 0.80)."""
    markets = p.client.get_sampling_simplified_markets()
    candidates = []
    for m in markets.get("data", []):
        tokens = m.get("tokens", [])
        if len(tokens) != 2:
            continue
        prices = [t.get("price") for t in tokens]
        if not all(prices) or not all(0.2 < float(pr) < 0.8 for pr in prices):
            continue
        candidates.append(m)
    if not candidates:
        return None
    # Sort by spread tightness via reward parameters when available; fall back to first.
    candidates.sort(key=lambda m: float(m.get("rewards", {}).get("min_size", 0)), reverse=True)
    return candidates[0]


def main() -> int:
    p = Polymarket(live=True)
    log.info("collateral_address = %s", p.collateral_address)
    log.info("exchange_address   = %s", p.exchange_address)
    log.info("funder             = %s", p.funder)
    bal = p.get_usdc_balance()
    log.info("balance (pUSD on funder) = %.4f", bal)

    market = find_liquid_market(p)
    if not market:
        log.error("No liquid mid-priced market found in sampling-simplified-markets")
        return 2

    cond = market.get("condition_id")
    tokens = market["tokens"]
    log.info("condition_id = %s", cond)
    for t in tokens:
        log.info("  token %s outcome=%s price=%s", t.get("token_id"), t.get("outcome"), t.get("price"))

    # Pick the YES-side token (or whichever is listed first) and read its book.
    token = tokens[0]
    token_id = token["token_id"]
    book = p.client.get_order_book(token_id)
    log.info("book bids (top 3): %s", json.dumps(book.bids[:3] if hasattr(book, "bids") else book.get("bids", [])[:3], default=str))
    log.info("book asks (top 3): %s", json.dumps(book.asks[:3] if hasattr(book, "asks") else book.get("asks", [])[:3], default=str))

    # Determine best ask to set FOK price slightly above it (to actually fill).
    asks = book.asks if hasattr(book, "asks") else book.get("asks", [])
    if not asks:
        log.error("No asks on token %s — pick another market.", token_id)
        return 3
    # asks ordered ascending? confirm
    sorted_asks = sorted(asks, key=lambda a: float(a.price if hasattr(a, "price") else a["price"]))
    best_ask = sorted_asks[0]
    best_ask_price = float(best_ask.price if hasattr(best_ask, "price") else best_ask["price"])
    log.info("best ask = %.4f", best_ask_price)

    if not os.getenv("EXECUTE", "").lower() in ("1", "true", "yes"):
        log.warning("EXECUTE not set — dry inspection complete; not placing order.")
        return 0

    # FOK $1 BUY at best ask.
    from py_clob_client_v2.clob_types import MarketOrderArgs, OrderType
    args = MarketOrderArgs(
        token_id=token_id,
        amount=1.0,            # $1 USDC budget
        side="BUY",
        price=best_ask_price,
        order_type=OrderType.FOK,
    )
    log.info("creating market order: token=%s amount=$1.00 side=BUY price=%.4f", token_id, best_ask_price)
    signed = p.client.create_market_order(args)
    log.info("signed order built; posting…")
    resp = p.client.post_order(signed, order_type=OrderType.FOK)
    log.info("post_order response = %s", resp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
