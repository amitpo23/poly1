from __future__ import annotations

import unittest

from scripts.research_queue import load_queue, validate_queue


class ResearchQueueTests(unittest.TestCase):
    def test_repository_research_queue_is_valid(self):
        queue = load_queue("config/research_queue.json")
        errors = validate_queue(queue)

        self.assertEqual(errors, [])
        item_ids = {item["id"] for item in queue["items"]}
        self.assertIn("vwap_microstructure_signal", item_ids)
        self.assertIn("oddpool_style_cross_venue_arb", item_ids)
        self.assertIn("sports_cheap_hold_sweep", item_ids)
        self.assertIn("crypto_orderflow_footprint", item_ids)
        self.assertIn("volatility_relative_value", item_ids)

    def test_validation_rejects_missing_required_fields(self):
        queue = {"version": 1, "items": [{"id": "x"}]}

        errors = validate_queue(queue)

        self.assertTrue(any("missing" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
