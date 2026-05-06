#!/usr/bin/env python3
"""Collect dry-run news classification signals.

This script logs analytics only. It never places orders.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.application.news_signal import collect_once  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--query",
        default=os.getenv(
            "NEWS_SIGNAL_QUERY",
            "OpenAI,Anthropic,Bitcoin,Ethereum,Fed,Nvidia,Trump",
        ),
    )
    parser.add_argument("--limit-news", type=int, default=10)
    parser.add_argument("--limit-markets", type=int, default=100)
    parser.add_argument("--max-matches", type=int, default=3)
    parser.add_argument("--min-relevance", type=float, default=0.12)
    parser.add_argument(
        "--headline",
        action="append",
        default=None,
        help="Manual headline to classify instead of fetching news. Can be repeated.",
    )
    args = parser.parse_args()

    news_items = None
    if args.headline:
        from agents.application.news_signal import NewsItem

        news_items = [
            NewsItem(headline=headline, source="manual", url="")
            for headline in args.headline
        ]

    inserted = collect_once(
        query=args.query,
        limit_news=args.limit_news,
        limit_markets=args.limit_markets,
        max_matches_per_item=args.max_matches,
        min_relevance=args.min_relevance,
        news_items=news_items,
    )
    print(f"inserted_news_signals={inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
