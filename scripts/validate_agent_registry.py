#!/usr/bin/env python3
"""Validate and summarize the poly1 agent registry."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.application.agent_registry import load_agent_registry


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate config/agent_registry.json")
    parser.add_argument("--path", default="config/agent_registry.json")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    registry = load_agent_registry(args.path)
    summary = registry.summary()
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"agent_registry ok: version={summary['version']} agents={summary['agent_count']}")
        print("by_mode:", ", ".join(f"{k}={v}" for k, v in sorted(summary["by_mode"].items())))
        print("live_capable:", ", ".join(summary["live_capable_agents"]) or "none")
        print("anchor_capable:", ", ".join(summary["anchor_capable_agents"]) or "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
