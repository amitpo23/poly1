#!/usr/bin/env python3
"""Refresh probability_calibration.json — runs daily.

scanner_executor reads this file at decision time (when bayesian gate
is enabled) and uses it to compute calibrated P(win) for each candidate.
This script is intended to run from brain_indicator_cycle (or a cron)
once per day; it's cheap (~1 sec on 30-day window) and the calibration
only needs to be fresh, not real-time.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.application.multi_pipeline_calibrator import multi_pipeline_calibrate
from agents.application.probability_calibrator import calibrate as _scanner_only_calibrate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/trade_log.db")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--max-age-hours", type=int, default=48)
    parser.add_argument(
        "--out",
        default="data/probability_calibration.json",
        help="Output path for the calibration JSON",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"db not found: {db_path}", file=sys.stderr)
        return 2

    # Full calibration covers all 3 pipelines: scanner_executor (via
    # decision_journal), direct-execution agents (btc_5min, scalper),
    # and shadow-research visibility.
    result = multi_pipeline_calibrate(
        str(db_path), days=args.days, max_age_hours=args.max_age_hours,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, indent=2, sort_keys=True)
    # Atomic write: write to .tmp then rename, so scanner_executor never
    # reads a half-written JSON.
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(text + "\n")
    tmp_path.replace(out_path)
    print(
        json.dumps(
            {
                "refreshed_at": datetime.now(timezone.utc).isoformat(),
                "out": str(out_path),
                "total_closes": result["total_closes"],
                "matched": result["matched"],
                "unmatched": result["unmatched"],
                "n_signal_sources": len(result["per_signal_source"]),
                "n_source_bands": len(result["per_source_band"]),
                "n_direct_execution_agents": len(result.get("per_direct_execution_agent", [])),
                "n_shadow_research_agents": len(result.get("shadow_research_visibility", [])),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
