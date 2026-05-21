from __future__ import annotations

import unittest

from agents.application.research_harness import HarnessConfig, build_run_plans, summarize_harness
from scripts.research_harness import load_harness
from scripts.research_queue import load_queue


class ResearchHarnessTests(unittest.TestCase):
    def test_repository_harness_builds_run_plans_from_queue(self):
        cfg = load_harness("config/research_harness.json")
        queue = load_queue("config/research_queue.json")

        plans = build_run_plans(queue, cfg)
        summary = summarize_harness(cfg, plans)

        self.assertEqual(summary["skill_count"], 9)
        self.assertEqual(summary["plan_count"], 9)
        self.assertIn("vwap_microstructure_signal", summary["ready_plan_ids"])
        self.assertIn("sports_cheap_hold_sweep", summary["ready_plan_ids"])
        self.assertIn("latent_regime_chaos_score", summary["blocked_plan_ids"])

    def test_harness_rejects_unknown_guardrail(self):
        payload = {
            "version": 1,
            "updated_at": "2026-05-21",
            "max_parallel_tasks": 2,
            "default_mode": "standard",
            "skills": [
                {
                    "skill_id": "x",
                    "purpose": "test",
                    "inputs": ["a"],
                    "outputs": ["b"],
                    "guardrails": ["made_up_guardrail"],
                }
            ],
        }

        with self.assertRaises(ValueError):
            HarnessConfig.from_dict(payload)

    def test_harness_limits_parallelism_to_three(self):
        payload = {
            "version": 1,
            "updated_at": "2026-05-21",
            "max_parallel_tasks": 4,
            "default_mode": "standard",
            "skills": [
                {
                    "skill_id": "x",
                    "purpose": "test",
                    "inputs": ["a"],
                    "outputs": ["b"],
                }
            ],
        }

        with self.assertRaises(ValueError):
            HarnessConfig.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
