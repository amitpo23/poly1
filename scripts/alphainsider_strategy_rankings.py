#!/usr/bin/env python3
"""Pull AlphaInsider strategy rankings into a local research scorecard.

Set ALPHAINSIDER_API_TOKEN in the environment. The token is never written to
the output file.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.application.alphainsider_strategy_rankings import (
    TIMEFRAMES,
    AlphaInsiderClient,
    summarize_rankings,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeframes", default="month,year,five_year")
    parser.add_argument("--sort", default="performance", choices=["performance", "top", "trending", "popular", "newest"])
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--type", default=None, help="Optional AlphaInsider type filter, e.g. stock or cryptocurrency")
    parser.add_argument("--max-drawdown", type=float, default=None)
    parser.add_argument("--price-max", type=float, default=None)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    requested = [x.strip() for x in args.timeframes.split(",") if x.strip()]
    bad = [x for x in requested if x not in TIMEFRAMES]
    if bad:
        raise SystemExit(f"unsupported timeframe(s): {', '.join(bad)}")

    client = AlphaInsiderClient()
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "alphainsider",
        "sort": args.sort,
        "limit": args.limit,
        "strategy_type": args.type,
        "timeframes": {},
    }
    for timeframe in requested:
        strategies = client.search_strategies(
            timeframe=timeframe,
            sort=args.sort,
            limit=args.limit,
            strategy_type=args.type,
            max_drawdown=args.max_drawdown,
            price_max=args.price_max,
        )
        result["timeframes"][timeframe] = summarize_rankings(strategies, timeframe)

    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
