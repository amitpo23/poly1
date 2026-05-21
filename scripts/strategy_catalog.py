#!/usr/bin/env python3
"""Print the canonical poly1 strategy catalog."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.application.strategy_catalog import catalog_summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = catalog_summary()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"strategy_catalog: {payload['strategy_count']} strategies")
    print("families:")
    for family, count in sorted(payload["families"].items()):
        print(f"- {family}: {count}")
    print("maturity:")
    for maturity, count in sorted(payload["maturity"].items()):
        print(f"- {maturity}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
