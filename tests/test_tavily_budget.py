import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.application import tavily


class TavilyBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cache = tavily._CACHE_PATH
        self.old_usage = tavily._USAGE_PATH
        tavily._CACHE_PATH = Path(self.tmp.name) / "cache.json"
        tavily._USAGE_PATH = Path(self.tmp.name) / "usage.json"
        self.old_env = os.environ.copy()
        os.environ.update({
            "TAVILY_ENABLED": "true",
            "TAVILY_API_KEY": "test-key",
            "TAVILY_DAILY_LIMIT": "1",
            "TAVILY_CACHE_TTL_SEC": "21600",
            "TAVILY_MIN_QUERY_INTERVAL_SEC": "900",
            "TAVILY_MAX_RESULTS": "2",
            "TAVILY_CRITICAL_ONLY": "true",
        })

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        tavily._CACHE_PATH = self.old_cache
        tavily._USAGE_PATH = self.old_usage
        self.tmp.cleanup()

    def test_non_critical_query_does_not_spend_budget(self):
        with patch("urllib.request.urlopen") as urlopen:
            result = tavily.tavily_headlines("Will a random TV show win an award?")

        self.assertEqual(result, "")
        urlopen.assert_not_called()
        self.assertFalse(tavily._USAGE_PATH.exists())

    def test_daily_limit_blocks_network_call(self):
        tavily._USAGE_PATH.write_text(json.dumps({
            "date": "2099-01-01",
            "count": 99,
            "last_call_ts": 0,
        }))

        with patch("time.strftime", return_value="2099-01-01"):
            with patch("urllib.request.urlopen") as urlopen:
                result = tavily.tavily_headlines("Breaking Iran oil attack")

        self.assertEqual(result, "")
        urlopen.assert_not_called()

    def test_cached_query_does_not_spend_budget(self):
        key = tavily._cache_key("Breaking Iran oil attack", 2)
        tavily._CACHE_PATH.write_text(json.dumps({
            key: {"ts": 4102444800.0, "value": "- cached"}
        }))

        with patch("time.time", return_value=4102444801.0):
            with patch("urllib.request.urlopen") as urlopen:
                result = tavily.tavily_headlines("Breaking Iran oil attack", max_results=5)

        self.assertEqual(result, "- cached")
        urlopen.assert_not_called()
        self.assertFalse(tavily._USAGE_PATH.exists())


if __name__ == "__main__":
    unittest.main()
