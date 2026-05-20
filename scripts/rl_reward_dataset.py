#!/usr/bin/env python3
"""Export an offline RL reward dataset from decision_journal markouts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.application.rl_reward_lab import RewardConfig, write_reward_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description="Build RL reward JSONL from shadow decisions")
    parser.add_argument("--db", default="./data/trade_log.db")
    parser.add_argument("--out", default="./data/rl_reward_dataset.jsonl")
    parser.add_argument("--summary-out", default="./data/rl_reward_summary.json")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--preferred-horizon-minutes", type=int, default=5)
    parser.add_argument("--round-trip-cost-pct", type=float, default=0.04)
    parser.add_argument("--spread-free-pct", type=float, default=0.02)
    parser.add_argument("--thin-depth-usdc", type=float, default=20.0)
    parser.add_argument("--reentry-cooldown-hours", type=float, default=12.0)
    args = parser.parse_args()
    cfg = RewardConfig(
        preferred_horizon_minutes=args.preferred_horizon_minutes,
        round_trip_cost_pct=args.round_trip_cost_pct,
        spread_free_pct=args.spread_free_pct,
        thin_depth_usdc=args.thin_depth_usdc,
        reentry_cooldown_hours=args.reentry_cooldown_hours,
    )
    payload = write_reward_dataset(
        args.db,
        args.out,
        summary_path=args.summary_out,
        limit=args.limit,
        cfg=cfg,
    )
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
