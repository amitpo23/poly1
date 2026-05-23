import unittest

from agents.application.signal_contract import TradeProposal, infer_strategy_type


class SignalContractTests(unittest.TestCase):
    def test_trade_proposal_from_brain_decision_normalizes_execution_shape(self):
        proposal = TradeProposal.from_brain_decision(
            {
                "id": 7,
                "agent": "external_conviction_api",
                "strategy": "public_news_event_probability_scalping",
                "market_id": "0xabc",
                "action": "buy",
                "score": 0.82,
                "signal_source": "public_news",
            },
            {
                "selected_token_id": "tok_yes",
                "selected_entry_price": 0.47,
                "estimated_win_probability": 0.61,
                "question": "Will a major event happen?",
            },
        )

        self.assertEqual(proposal.side, "BUY")
        self.assertEqual(proposal.token_id, "tok_yes")
        self.assertEqual(proposal.strategy_type, "event_news")
        self.assertAlmostEqual(proposal.probability, 0.61)
        self.assertEqual(proposal.source_decision_id, 7)

    def test_trade_proposal_requires_token_and_valid_side(self):
        with self.assertRaises(ValueError):
            TradeProposal.from_brain_decision(
                {"id": 1, "agent": "market_scanner", "market_id": "0xabc", "action": "HOLD"},
                {"selected_entry_price": 0.5, "estimated_win_probability": 0.6},
            )

    def test_shadow_buy_no_plan_normalizes_to_buy_token_proposal(self):
        proposal = TradeProposal.from_brain_decision(
            {
                "id": 9,
                "agent": "external_conviction_divergence",
                "strategy": "manifold_divergence_event_probability_scalping",
                "market_id": "0xdef",
                "token_id": "tok_no",
                "action": "SHADOW_BUY_NO",
                "score": 0.775,
                "signal_source": "manifold_divergence",
            },
            {"entry_price": 0.715},
        )

        self.assertEqual(proposal.side, "SELL")
        self.assertEqual(proposal.token_id, "tok_no")
        self.assertAlmostEqual(proposal.probability, 0.775)
        self.assertEqual(proposal.strategy_type, "cross_market_divergence")

    def test_infers_crypto_momentum_strategy(self):
        self.assertEqual(
            infer_strategy_type(
                row={"agent": "btc_5min", "strategy": "btc_5min_consensus"},
                features={"question": "Will BTC be up?"},
            ),
            "crypto_momentum",
        )


if __name__ == "__main__":
    unittest.main()
