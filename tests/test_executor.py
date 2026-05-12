import os
import sys
import types
import unittest
from unittest.mock import MagicMock

from agents.application.trade_recommendation import parse_trade_recommendation
from agents.application.anthropic_compat import to_anthropic_messages


class TestTradeRecommendationParsing(unittest.TestCase):
    def test_parse_json_trade_recommendation(self):
        recommendation = parse_trade_recommendation(
            '{"price": 0.42, "size_fraction": 0.08, "side": "BUY", "confidence": 0.7}'
        )

        self.assertEqual(recommendation.price, 0.42)
        self.assertEqual(recommendation.size_fraction, 0.08)
        self.assertEqual(recommendation.side, "BUY")
        self.assertEqual(recommendation.confidence, 0.7)

    def test_parse_legacy_field_trade_recommendation(self):
        recommendation = parse_trade_recommendation(
            """
            price:0.55,
            size:0.1,
            side:SELL,
            """
        )

        self.assertEqual(recommendation.price, 0.55)
        self.assertEqual(recommendation.size_fraction, 0.1)
        self.assertEqual(recommendation.side, "SELL")

    def test_rejects_out_of_range_size(self):
        with self.assertRaises(ValueError):
            parse_trade_recommendation(
                '{"price": 0.42, "size_fraction": 1.5, "side": "BUY"}'
            )

    def test_rejects_unknown_side(self):
        with self.assertRaises(ValueError):
            parse_trade_recommendation(
                '{"price": 0.42, "size_fraction": 0.1, "side": "HOLD"}'
            )


class TestAnthropicFallback(unittest.TestCase):
    """Regression tests for to_anthropic_messages() in anthropic_compat.py."""

    # Names must match what anthropic_compat.py checks (type(m).__name__)
    class HumanMessage:
        def __init__(self, content): self.content = content

    class SystemMessage:
        def __init__(self, content): self.content = content

    def test_string_prompt_becomes_single_user_message(self):
        """Plain string → exactly one user message, no assistant fragments."""
        system, msgs = to_anthropic_messages("Evaluate this market please.")
        self.assertIsNone(system)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertIn("Evaluate", msgs[0]["content"])

    def test_string_prompt_no_trailing_whitespace(self):
        """Regression: iterating string char-by-char produced single-space
        assistant messages that Anthropic rejected with 400."""
        system, msgs = to_anthropic_messages("  test prompt  ")
        for m in msgs:
            self.assertTrue(
                m["content"].strip(),
                f"Message has whitespace-only content: {m!r}",
            )

    def test_langchain_messages_converted_correctly(self):
        """HumanMessage + SystemMessage list maps to correct Anthropic format."""
        messages = [
            self.SystemMessage("You are a trader."),
            self.HumanMessage("Should I buy YES?"),
        ]
        system, msgs = to_anthropic_messages(messages)
        self.assertEqual(system, "You are a trader.")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertIn("buy YES", msgs[0]["content"])

    def test_empty_assistant_turns_skipped(self):
        """Empty assistant content is skipped to avoid Anthropic 400 errors."""
        class FakeAIMessage:
            content = "   "  # whitespace only
        messages = [
            self.HumanMessage("hello"),
            FakeAIMessage(),
        ]
        system, msgs = to_anthropic_messages(messages)
        roles = [m["role"] for m in msgs]
        self.assertNotIn("assistant", roles)


if __name__ == "__main__":
    unittest.main()
