#!/usr/bin/env python3
"""Build a strategy scorecard from decision_journal."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.application.strategy_scorecard import write_strategy_scorecard


def main() -> int:
    parser = argparse.ArgumentParser(description="Build strategy scorecard JSON")
    parser.add_argument("--db", default="./data/trade_log.db")
    parser.add_argument("--out", default="./data/strategy_scorecard.json")
    parser.add_argument("--min-decisions", type=int, default=50)
    args = parser.parse_args()
    payload = write_strategy_scorecard(args.db, args.out, min_decisions=args.min_decisions)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
