from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.application.research_harness import HarnessConfig, build_run_plans, summarize_harness
from scripts.research_queue import load_queue


DEFAULT_HARNESS_PATH = Path("config/research_harness.json")
DEFAULT_QUEUE_PATH = Path("config/research_queue.json")


def load_harness(path: str | Path = DEFAULT_HARNESS_PATH) -> HarnessConfig:
    payload = json.loads(Path(path).read_text())
    return HarnessConfig.from_dict(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the research harness and build run plans from the research queue.")
    parser.add_argument("--harness", default=str(DEFAULT_HARNESS_PATH))
    parser.add_argument("--queue", default=str(DEFAULT_QUEUE_PATH))
    parser.add_argument("--plans", action="store_true", help="Print full run plans instead of summary only.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        cfg = load_harness(args.harness)
        queue = load_queue(args.queue)
        plans = build_run_plans(queue, cfg)
        summary = summarize_harness(cfg, plans)
    except Exception as exc:
        payload = {"ok": False, "error": str(exc)}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"research_harness invalid: {exc}")
        return 1

    payload = {"ok": True, **summary}
    if args.plans:
        payload["plans"] = [asdict(plan) for plan in plans]

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "research_harness ok: "
            f"version={summary['version']} skills={summary['skill_count']} "
            f"plans={summary['plan_count']} blocked={summary['blocked_count']} "
            f"max_parallel={summary['max_parallel_tasks']}"
        )
        if args.plans:
            for plan in plans:
                state = "blocked" if plan.blocked else "ready"
                print(f"- {plan.item_id}: {state} mode={plan.mode} skills={','.join(plan.skill_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
