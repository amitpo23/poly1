#!/usr/bin/env python3
"""manual_entry.py — place a directional bet with auto-TP exit.

For user-driven directional convictions ("I think BTC will rise" /
"oil will drop in 90 days") that don't fit any backtested strategy.
Bypasses the algorithmic gates we apply to autonomous agents — the
user accepts the risk explicitly.

Workflow:
  1. Resolve <slug> via Gamma → token_ids + current outcome prices.
  2. Compute fill price for the chosen side (best_ask + small buffer).
  3. Place a FOK BUY via the existing Polymarket adapter.
  4. On success, write a `filled` row to trade_log with `tp_pct_override`
     and (optionally) `no_sl` in response_json.
  5. position_manager (already running) picks it up next cycle and
     auto-exits at +tp_pct OR (if !no_sl) -stop_loss_pct.

Usage:
    docker exec poly1-position-manager python /app/scripts/python/manual_entry.py \\
        --slug will-wti-reach-110-in-may-2026-116-472 \\
        --side NO \\
        --size-usdc 2.5 \\
        --tp-pct 0.20 \\
        --no-sl \\
        --execute

Without --execute: dryrun (shows what would happen without placing).
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("manual_entry")


def _gamma_market(slug: str) -> Optional[dict]:
    """Resolve slug via Gamma. Returns the first matching market or None."""
    for closed in ("false", "true"):
        params = urllib.parse.urlencode({"slug": slug, "closed": closed})
        url = f"https://gamma-api.polymarket.com/markets?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "poly1-manual"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if isinstance(data, list) and data:
                return data[0]
        except Exception as exc:
            logger.warning("gamma %s closed=%s failed: %s", slug, closed, exc)
    return None


def _get_ask(polymarket, token_id: str) -> Optional[float]:
    """Best-ask of a token from CLOB book. Falls back to midpoint if book unavailable."""
    try:
        book = polymarket.client.get_book(token_id)
        asks = book.get("asks", []) if isinstance(book, dict) else []
        if asks:
            # asks are typically sorted lowest-first
            return float(asks[0]["price"])
    except Exception as exc:
        logger.debug("book fetch failed: %s; falling back to midpoint", exc)
    try:
        mid_resp = polymarket.client.get_midpoint(token_id)
        mid = float(mid_resp.get("mid")) if isinstance(mid_resp, dict) else float(mid_resp)
        return mid
    except Exception as exc:
        logger.warning("midpoint fetch failed: %s", exc)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True, help="Polymarket slug")
    parser.add_argument("--side", required=True, choices=["YES", "NO"],
                        help="Buy YES (token_ids[0]) or NO (token_ids[1])")
    parser.add_argument("--size-usdc", type=float, required=True,
                        help="Position size in USDC (e.g. 2.5)")
    parser.add_argument("--tp-pct", type=float, default=0.20,
                        help="Take profit at +X (default 0.20 = 20%%)")
    parser.add_argument("--no-sl", action="store_true",
                        help="Disable stop loss; hold to resolution if TP not hit")
    parser.add_argument("--max-price", type=float, default=None,
                        help="Cap entry price (don't pay above this). "
                             "If best_ask exceeds this, abort.")
    parser.add_argument("--execute", action="store_true",
                        help="Actually place. Default is dryrun (compute + log).")
    args = parser.parse_args()

    if args.size_usdc <= 0:
        print("--size-usdc must be > 0", file=sys.stderr)
        return 2
    if not (0.0 < args.tp_pct < 5.0):
        print("--tp-pct out of range", file=sys.stderr)
        return 2

    market = _gamma_market(args.slug)
    if market is None:
        print(f"slug not found in Gamma: {args.slug}", file=sys.stderr)
        return 1

    if market.get("closed"):
        print(f"market is closed; cannot enter. slug={args.slug}", file=sys.stderr)
        return 1

    try:
        tok_ids = ast.literal_eval(market.get("clobTokenIds") or "[]")
    except Exception:
        tok_ids = []
    if len(tok_ids) < 2:
        print(f"market lacks clobTokenIds: {args.slug}", file=sys.stderr)
        return 1

    yes_token = str(tok_ids[0])
    no_token = str(tok_ids[1])
    target_token = yes_token if args.side == "YES" else no_token

    # Lazy import — keeps this script importable for dryrun even without
    # the live SDK in scope (e.g. CI smoke tests).
    from agents.polymarket.polymarket import Polymarket
    from agents.application.trade_log import TradeLog

    pm = Polymarket(live=True)
    ask = _get_ask(pm, target_token)
    if ask is None or ask <= 0 or ask >= 1:
        print(f"invalid ask for {args.side}: {ask}", file=sys.stderr)
        return 1

    if args.max_price is not None and ask > args.max_price:
        print(f"abort: best_ask={ask:.4f} > --max-price={args.max_price:.4f}",
              file=sys.stderr)
        return 1

    shares = args.size_usdc / ask
    cost_basis = args.size_usdc

    print(f"# manual_entry plan")
    print(f"  slug:         {args.slug}")
    print(f"  question:     {market.get('question', '')[:80]}")
    print(f"  side:         {args.side} (token={target_token[:20]}...)")
    print(f"  best_ask:     {ask:.4f}")
    print(f"  size:         ${cost_basis:.2f}")
    print(f"  shares:       {shares:.4f}")
    print(f"  tp_pct:       +{args.tp_pct*100:.1f}%  (exit if mid ≥ {ask*(1+args.tp_pct):.4f})")
    print(f"  sl:           {'DISABLED' if args.no_sl else 'global default (per .env)'}")
    print(f"  max_payout:   ${shares:.2f} if {args.side} resolves true")
    print(f"  net_to_tp:    ${shares * ask * args.tp_pct:.4f} (paper, before slippage)")

    if not args.execute:
        print()
        print("DRYRUN. Pass --execute to place the order.")
        return 0

    # Build TradeRecommendation in the same shape as the LLM path uses,
    # but with side semantics flipped per the user's intuitive YES/NO:
    #   - args.side==YES → BUY (buys token_ids[0]) at price=ask
    #   - args.side==NO  → SELL (buys token_ids[1] at 1-price); we set
    #     price=1-ask so the executor's `1.0 - price` gives back ask.
    from agents.utils.objects import TradeRecommendation

    if args.side == "YES":
        rec = TradeRecommendation(
            price=ask, size_fraction=0.0, side="BUY", confidence=1.0,
            amount_usdc=cost_basis,
        )
    else:
        rec = TradeRecommendation(
            price=1.0 - ask, size_fraction=0.0, side="SELL", confidence=1.0,
            amount_usdc=cost_basis,
        )

    # Build the (market_doc, ...) tuple shape execute_market_order expects.
    # The Polymarket adapter passes through to CLOB FOK by default.
    # market_doc must have a `.dict()["metadata"]` accessor — wrap raw dict.
    class _MarketWrapper:
        def __init__(self, raw):
            self._raw = raw
        def dict(self):
            return {"metadata": {
                "clob_token_ids": str(tok_ids),
                "outcomes": str(market.get("outcomes") or '["Yes","No"]'),
                "outcome_prices": market.get("outcomePrices") or "[0.5,0.5]",
            }}

    market_tuple = (_MarketWrapper(market),)
    try:
        result = pm.execute_market_order(market_tuple, rec)
    except Exception as exc:
        logger.exception("execute_market_order raised")
        print(f"ORDER FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print()
    print(f"# order result: {result}")

    status = (result or {}).get("status", "unknown")
    if status not in ("matched", "filled"):
        print(f"order not matched (status={status}); not writing filled row.",
              file=sys.stderr)
        return 1

    # Write filled row with overrides encoded in response_json
    log = TradeLog()
    cycle_id = f"manual:{args.slug[:24]}"
    response_payload = {
        **result,
        "tp_pct_override": args.tp_pct,
        "no_sl": bool(args.no_sl),
        "manual_entry": True,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    market_id = str(market.get("conditionId") or market.get("condition_id") or "")
    log.insert_pending(
        cycle_id=cycle_id,
        market_id=market_id,
        token_id=target_token,
        side=rec.side,
        price=rec.price,
        size_usdc=cost_basis,
        confidence=1.0,
        status="filled",
        response=response_payload,
    )
    print(f"OK — filled row written. position_manager will pick this up next "
          f"cycle and exit at +{args.tp_pct*100:.0f}% from {ask:.4f}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
