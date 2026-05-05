"""Print selected .env key presence without revealing values."""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv


DEFAULT_KEYS = [
    "POLYGON_WALLET_PRIVATE_KEY",
    "OPENAI_API_KEY",
    "POLYMARKET_FUNDER",
    "POLYMARKET_DEPOSIT_WALLET",
    "POLYMARKET_SIGNATURE_TYPE",
    "POLYMARKET_BUILDER_CODE",
    "POLYMARKET_BUILDER_ADDRESS",
    "BUILDER_API_KEY",
    "BUILDER_SECRET",
    "BUILDER_PASS_PHRASE",
    "POLYMARKET_RELAYER_URL",
    "EXECUTE",
]


def main() -> int:
    load_dotenv()
    keys = sys.argv[1:] or DEFAULT_KEYS
    for key in keys:
        value = os.getenv(key)
        state = "<set>" if value else "<empty>"
        print(f"{key}={state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
