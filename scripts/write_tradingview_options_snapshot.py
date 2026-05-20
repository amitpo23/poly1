#!/usr/bin/env python3
"""Write a validated TradingView options-chain snapshot for MetaBrain.

TradingView's options chain is a browser UI, not a stable public JSON API.
This script gives the operator/browser bridge one controlled write path for
the data MetaBrain and ExternalConviction read.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_PATH = Path("data/tradingview_options_es1_snapshot.json")


def _positive_float(value: str, name: str) -> float:
    try:
        out = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be numeric") from exc
    if out < 0:
        raise argparse.ArgumentTypeError(f"{name} must be non-negative")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default=str(DEFAULT_PATH))
    parser.add_argument("--symbol", default="CME_MINI:ES1!")
    parser.add_argument("--source", default="manual_tradingview_options_chain")
    parser.add_argument("--put-call-ratio", type=lambda v: _positive_float(v, "put_call_ratio"))
    parser.add_argument("--put-volume", type=lambda v: _positive_float(v, "put_volume"))
    parser.add_argument("--call-volume", type=lambda v: _positive_float(v, "call_volume"))
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    if args.put_call_ratio is None:
        if args.put_volume is None or args.call_volume is None:
            raise SystemExit(
                "provide either --put-call-ratio or both --put-volume and --call-volume"
            )
        args.put_call_ratio = args.put_volume / max(args.call_volume, 1.0)

    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": args.symbol,
        "source": args.source,
        "put_call_ratio": round(float(args.put_call_ratio), 6),
        "put_volume": args.put_volume,
        "call_volume": args.call_volume,
        "notes": args.notes,
    }

    path = Path(args.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"wrote {path}")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
