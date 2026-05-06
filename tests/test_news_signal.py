import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from agents.application.news_signal import (
    NewsItem,
    NewsSignalClassifier,
    _coerce_json,
    collect_once,
    extract_keywords,
    fetch_rss_items,
    match_news_to_markets,
)
from agents.application.trade_log import TradeLog


class TestNewsSignalLogic(unittest.TestCase):
    def test_extract_keywords_removes_stopwords(self):
        words = extract_keywords("Will OpenAI release GPT-5 before August 2026?")
        self.assertIn("openai", words)
        self.assertIn("gpt-5", words)
        self.assertNotIn("will", words)

    def test_match_news_to_markets_scores_keyword_overlap(self):
        markets = [
            {
                "id": "1",
                "question": "Will OpenAI release GPT-5 before August 2026?",
                "outcomePrices": "[\"0.40\", \"0.60\"]",
            },
            {
                "id": "2",
                "question": "Will Bitcoin hit 100k?",
                "outcomePrices": "[\"0.50\", \"0.50\"]",
            },
        ]
        matches = match_news_to_markets(
            "OpenAI reportedly expands GPT-5 partner testing",
            markets,
            min_relevance=0.1,
        )
        self.assertEqual(matches[0].market_id, "1")
        self.assertAlmostEqual(matches[0].yes_price, 0.40)

    def test_match_news_to_markets_requires_multiple_hits(self):
        markets = [{
            "id": "1",
            "question": "New Rihanna Album before GTA VI?",
            "outcomePrices": "[\"0.40\", \"0.60\"]",
        }]
        matches = match_news_to_markets(
            "Anthropic deepens push into Wall Street with new AI agents",
            markets,
            min_relevance=0.1,
        )
        self.assertEqual(matches, [])

    def test_coerce_json_handles_markdown_fence(self):
        payload = _coerce_json("""```json
        {"direction":"bullish","materiality":0.7,"reasoning":"x"}
        ```""")
        self.assertEqual(payload["direction"], "bullish")
        self.assertEqual(payload["materiality"], 0.7)

    @patch("agents.application.news_signal.urllib.request.urlopen")
    def test_fetch_rss_items_parses_titles(self, urlopen):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"""
                <rss><channel><title>Feed</title>
                  <item><title>OpenAI headline</title><link>https://x.test</link></item>
                </channel></rss>
                """

        urlopen.return_value = _Resp()
        items = fetch_rss_items(limit=1, feeds=["https://feed.test/rss"])
        self.assertEqual(items[0].headline, "OpenAI headline")
        self.assertEqual(items[0].source, "Feed")


class TestNewsSignalStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = TradeLog(db_path=self.tmp.name)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.tmp.name + suffix
            if os.path.exists(p):
                os.unlink(p)

    def test_news_signals_table_exists_and_inserts(self):
        row_id = self.log.insert_news_signal(
            headline="OpenAI tests GPT-5",
            source="test",
            market_id="m1",
            market_question="Will OpenAI release GPT-5?",
            direction="bullish",
            materiality=0.8,
            relevance_score=0.5,
            status="news_signal",
            reasoning="Relevant positive evidence.",
        )
        self.assertGreater(row_id, 0)
        rows = self.log.recent_news_signals()
        self.assertEqual(rows[0]["direction"], "bullish")
        self.assertEqual(rows[0]["status"], "news_signal")

    @patch("agents.application.news_signal.fetch_news_items")
    def test_collect_once_logs_without_executor(self, fetch_news_items):
        fetch_news_items.return_value = [
            NewsItem(headline="OpenAI expands GPT-5 partner testing", source="test")
        ]
        gamma = MagicMock()
        gamma.get_current_markets.return_value = [{
            "id": "m1",
            "question": "Will OpenAI release GPT-5 before August 2026?",
            "outcomePrices": "[\"0.40\", \"0.60\"]",
        }]
        classifier = MagicMock()
        classifier.classify.return_value.direction = "bullish"
        classifier.classify.return_value.materiality = 0.7
        classifier.classify.return_value.reasoning = "News supports YES."
        classifier.classify.return_value.latency_ms = 123
        classifier.classify.return_value.model = "test-model"

        inserted = collect_once(
            query="OpenAI",
            trade_log=self.log,
            gamma=gamma,
            classifier=classifier,
            min_relevance=0.1,
        )
        self.assertEqual(inserted, 1)
        self.assertEqual(self.log.recent_news_signals()[0]["materiality"], 0.7)


class TestNewsSignalClassifier(unittest.TestCase):
    @patch("langchain_openai.ChatOpenAI")
    def test_classifier_parses_llm_json(self, chat_openai):
        llm = MagicMock()
        response = MagicMock()
        response.content = '{"direction":"bearish","materiality":0.4,"reasoning":"x"}'
        llm.invoke.return_value = response
        chat_openai.return_value = llm

        classifier = NewsSignalClassifier(model="test")
        market = MagicMock()
        market.question = "Will Bitcoin hit 100k?"
        market.yes_price = 0.5
        result = classifier.classify(NewsItem("Bitcoin sells off"), market)
        self.assertEqual(result.direction, "bearish")
        self.assertEqual(result.materiality, 0.4)


if __name__ == "__main__":
    unittest.main()
