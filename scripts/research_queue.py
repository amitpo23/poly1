from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_PATH = Path("config/research_queue.json")
REQUIRED_FIELDS = {
    "id",
    "source",
    "status",
    "owner_agent",
    "expected_value_hypothesis",
    "required_evidence",
    "live_policy",
}


def load_queue(path: str | Path = DEFAULT_PATH) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def validate_queue(queue: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if int(queue.get("version") or 0) <= 0:
        errors.append("version must be positive")
    seen: set[str] = set()
    for idx, item in enumerate(queue.get("items") or []):
        missing = sorted(REQUIRED_FIELDS - set(item))
        if missing:
            errors.append(f"items[{idx}] missing: {','.join(missing)}")
        item_id = str(item.get("id") or "")
        if not item_id:
            errors.append(f"items[{idx}] id is empty")
        elif item_id in seen:
            errors.append(f"duplicate item id: {item_id}")
        seen.add(item_id)
        if not isinstance(item.get("required_evidence"), list) or not item.get("required_evidence"):
            errors.append(f"{item_id}: required_evidence must be a non-empty list")
    if not queue.get("items"):
        errors.append("queue must contain at least one item")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or print the strategy research queue.")
    parser.add_argument("--path", default=str(DEFAULT_PATH))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    queue = load_queue(args.path)
    errors = validate_queue(queue)
    if errors:
        if args.json:
            print(json.dumps({"ok": False, "errors": errors}, indent=2, sort_keys=True))
        else:
            print("research_queue invalid:")
            for error in errors:
                print(f"- {error}")
        return 1

    summary = {
        "ok": True,
        "version": queue.get("version"),
        "item_count": len(queue.get("items") or []),
        "implemented": [item["id"] for item in queue.get("items", []) if str(item.get("status", "")).startswith("implemented")],
        "queued": [item["id"] for item in queue.get("items", []) if item.get("status") == "queued"],
        "deferred": [item["id"] for item in queue.get("items", []) if item.get("status") == "deferred"],
    }
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(
            "research_queue ok: "
            f"version={summary['version']} items={summary['item_count']} "
            f"implemented={len(summary['implemented'])} queued={len(summary['queued'])} deferred={len(summary['deferred'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
