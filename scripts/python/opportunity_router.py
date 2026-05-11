#!/usr/bin/env python3
"""Print or persist opportunity routes from scout research reports."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.application.opportunity_router import OpportunityRouter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scout-db", default="data/scout.db")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--persist", action="store_true",
                        help="Write current route decisions into scout_db.opportunity_routes")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    router = OpportunityRouter()
    routes = router.latest_from_scout_db(args.scout_db, limit=args.limit)
    written = router.persist_latest_routes(args.scout_db, limit=args.limit) if args.persist else 0
    if args.json:
        print(json.dumps({
            "persisted": written,
            "routes": [asdict(route) for route in routes],
        }, indent=2))
    else:
        print("# opportunity routes")
        if args.persist:
            print(f"persisted={written}")
        for route in routes:
            ev = "n/a" if route.expected_value is None else f"{route.expected_value:+.3f}"
            prob = (
                "n/a"
                if route.estimated_true_probability is None
                else f"{route.estimated_true_probability:.3f}"
            )
            entry = "n/a" if route.entry_price is None else f"{route.entry_price:.3f}"
            print(
                f"{route.route:10s} score={route.score:.3f} risk={route.risk_score:.3f} "
                f"prob={prob:>5s} entry={entry:>5s} ev={ev:>7s} "
                f"{route.strategy:18s} {route.market_slug}"
            )
            print(f"  reasons={', '.join(route.reasons)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
