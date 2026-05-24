from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from agents.application.scanner_executor import ScannerExecutor, ScannerExecutorConfig
from agents.application.trade_log import FILLED, TradeLog


def _features(**overrides):
    data = {
        "question": "Will BTC be up in 5 minutes?",
        "condition_id": "0xabc",
        "gamma_market_id": "123",
        "slug": "btc-updown-5m-123",
        "outcomes": ["Up", "Down"],
        "outcome_prices": [0.50, 0.50],
        "clob_token_ids": ["tok_up", "tok_down"],
        "selected_side": "BUY",
        "selected_token_id": "tok_up",
        "selected_outcome": "Up",
        "selected_entry_price": 0.50,
        "estimated_win_probability": 0.58,
        "estimated_win_probability_calibrated": True,
        "estimated_win_probability_source": "test_calibrated_source",
        "scanner_raw_ev": 0.16,
        "meta_timing": "now",
    }
    data.update(overrides)
    return data


class ScannerExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "trade_log.db")
        self.log = TradeLog(db_path=self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _engine(
        self,
        *,
        execute=True,
        price=0.50,
        avg_price=None,
        fillable=1.0,
        book_quality=None,
        allow_wait_with_high_score=False,
        wait_override_min_score=0.79,
        min_score=0.55,
        min_proven_calibrated_score=0.54,
        min_net_ev=0.03,
        require_promotable_strategy=False,
        enforce_regime_router=False,
        learning_guard_enabled=False,
        learning_preferred_side="BUY",
        learning_min_entry_price=0.40,
        learning_max_entry_price=0.50,
        learning_allow_proven_side_override=False,
        learning_allow_proven_price_override=False,
        market_loss_cooldown_hours=1.0,
        prefer_maker_for_fast_markets=True,
        candidate_agents=("market_scanner",),
    ):
        pm = MagicMock()
        pm._fillable_market_buy.return_value = (
            price,
            fillable,
            price if avg_price is None else avg_price,
        )
        pm._fillable_market_buy_with_quality.return_value = (
            price,
            fillable,
            price if avg_price is None else avg_price,
            book_quality
            if book_quality is not None
            else {
                "book_quality_score": 0.95,
                "best_bid": price - 0.01,
                "best_ask": price,
                "spread_pct": 0.02,
                "bid_depth_usdc": 50.0,
                "ask_depth_usdc": 50.0,
                "fillable_usdc": fillable,
                "avg_entry_price": price if avg_price is None else avg_price,
            },
        )
        pm.execute_market_order.return_value = {
            "status": "matched",
            "order_avg_price_estimate": price,
            "amount_usdc": fillable,
        }
        pm.get_usdc_balance.return_value = 27.0
        risk = MagicMock()
        risk.reason.return_value = None
        cfg = ScannerExecutorConfig(
            poll_seconds=1,
            max_decision_age_seconds=180,
            position_size_usdc=1.0,
            min_score=min_score,
            min_proven_calibrated_score=min_proven_calibrated_score,
            min_raw_ev=0.04,
            min_net_ev=min_net_ev,
            round_trip_cost_pct=0.04,
            max_entry_drift_pct=0.04,
            prefer_maker_for_fast_markets=prefer_maker_for_fast_markets,
            require_timing_now=True,
            require_calibrated_probability=True,
            allow_wait_with_high_score=allow_wait_with_high_score,
            wait_override_min_score=wait_override_min_score,
            require_promotable_strategy=require_promotable_strategy,
            enforce_regime_router=enforce_regime_router,
            learning_guard_enabled=learning_guard_enabled,
            learning_preferred_side=learning_preferred_side,
            learning_min_entry_price=learning_min_entry_price,
            learning_max_entry_price=learning_max_entry_price,
            learning_allow_proven_side_override=learning_allow_proven_side_override,
            learning_allow_proven_price_override=learning_allow_proven_price_override,
            market_loss_cooldown_hours=market_loss_cooldown_hours,
            strategy_scorecard_path=str(Path(self.tmp.name) / "strategy_scorecard.json"),
            provider_scorecard_path=str(Path(self.tmp.name) / "provider_scorecard.json"),
            candidate_agents=candidate_agents,
        )
        return ScannerExecutor(
            cfg=cfg,
            trade_log=self.log,
            polymarket=pm,
            risk_gate=risk,
            execute=execute,
        ), pm

    def test_pre_sweep_risk_gate_block_aborts_cycle_before_any_per_market_work(self):
        """CLAUDE.md invariant #4: RiskGate.ok() is called twice per cycle —
        pre-sweep AND per-market. This verifies the pre-sweep short-circuit:
        when RiskGate.reason() returns non-None at start of run_once, the
        entire cycle aborts without touching candidates."""
        # Insert a real candidate that would otherwise be processed
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            asset=None,
            action="BUY",
            features={
                "selected_side": "BUY",
                "selected_token_id": "tok-up",
                "selected_entry_price": 0.45,
                "estimated_win_probability": 0.6,
                "estimated_win_probability_calibrated": True,
                "meta_timing": "now",
            },
            signal_source="market_scanner",
            token_id="tok-up",
        )
        engine, pm = self._engine(execute=True)
        # Make RiskGate block this cycle (e.g., HALT just appeared)
        engine.risk_gate.reason.return_value = "HALT file present"
        engine.risk_gate.is_freeze_only_block.return_value = False
        stats = engine.run_once()
        self.assertEqual(stats["cycle_blocked"], 1)
        self.assertEqual(stats["seen"], 0)
        self.assertEqual(stats["executed"], 0)
        # Critical: no order should have been placed even with a valid candidate
        pm.execute_market_order.assert_not_called()
        # And no per-market processing — the candidate was not even fetched.

    def test_consensus_relaxes_min_score_only_when_enabled_and_present(self):
        """When consensus is enabled AND another entry agent approved the
        same market in the window, the score floor drops from min_score
        to consensus_min_score. Without consensus, strict floor applies.
        This is the runtime hook for the 2-source-agreement architecture.
        """
        # Insert a low-score scanner approval that would normally fail
        # the strict 0.55 default but pass the consensus 0.50 floor.
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xconsensus",
            approved=True,
            reason="scanner_approved score=0.52",
            score=0.52,
            market_type="general_binary",
            features=_features(),
            action="BUY",
            signal_source="market_scanner",
            token_id="tok_up",
        )
        # Insert a SECOND agent approving the same market within window.
        self.log.insert_brain_decision(
            agent="opportunity_factory",
            strategy="opportunity",
            decision_type="entry",
            market_id="0xconsensus",
            approved=True,
            reason="opportunity",
            score=0.50,
            market_type="general_binary",
            features={"selected_entry_price": 0.45},
            action="BUY",
            signal_source="opportunity_factory",
            token_id="tok_up",
        )
        engine, _pm = self._engine(execute=False, min_score=0.55)
        # Without enabling consensus: low score should fail.
        stats = engine.run_once()
        # In some setups other gates fire first; we only care that the
        # `score_below_executor_min` rejection is present.
        recent = self.log.recent_brain_decisions(limit=4)
        reasons = [r["reason"] for r in recent if r["agent"] == "scanner_executor"]
        self.assertTrue(
            any("score_below_executor_min" in r for r in reasons),
            f"expected score rejection without consensus, got: {reasons}",
        )

        # Re-run with consensus enabled and threshold lowered.
        engine2, _pm2 = self._engine(
            execute=False,
            min_score=0.55,
        )
        engine2.cfg = engine2.cfg.__class__(
            **{**engine2.cfg.__dict__, "consensus_enabled": True,
               "consensus_min_score": 0.50}
        )
        # Reset the processed cache so the same decision is re-examined.
        engine2._processed = set()
        stats2 = engine2.run_once()
        recent2 = self.log.recent_brain_decisions(limit=4)
        reasons2 = [r["reason"] for r in recent2 if r["agent"] == "scanner_executor"]
        # With consensus, score should NOT be the blocker anymore.
        score_reject_count = sum(1 for r in reasons2 if "score_below_executor_min" in r)
        # The pre-consensus run already added one. The post-consensus
        # run should add 0 (i.e., reject reason should be different).
        self.assertLessEqual(score_reject_count, 1, f"reasons: {reasons2}")

    def test_freeze_only_block_routes_to_shadow_path(self):
        """When risk_gate.reason() returns freeze-only, the cycle continues
        and the per-market handler routes to the shadow path so
        decision_journal still records SHADOW_ENTER. This enables the
        Week-1 shadow measurement to accumulate data while the bot is
        frozen against live execution."""
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(),
            action="BUY",
        )
        engine, pm = self._engine(execute=True)
        engine.risk_gate.reason.return_value = (
            "runtime control mode=freeze blocks live entries"
        )
        engine.risk_gate.is_freeze_only_block.return_value = True
        stats = engine.run_once()
        # Cycle was NOT aborted; the per-market path ran.
        self.assertEqual(stats["cycle_blocked"], 0)
        self.assertEqual(stats["shadow"], 1)
        self.assertEqual(stats["executed"], 0)
        # No live order was placed.
        pm.execute_market_order.assert_not_called()
        # SHADOW_ENTER row was written.
        journal = self.log.recent_decision_journal(limit=1)[0]
        self.assertEqual(journal["decision"], "SHADOW_ENTER")

    def test_executes_fresh_scanner_approval_when_ev_and_risk_pass(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(),
            action="BUY",
        )
        engine, pm = self._engine(execute=True)

        stats = engine.run_once()

        self.assertEqual(stats["executed"], 1)
        pm.execute_market_order.assert_called_once()
        rows = self.log.filled_positions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["token_id"], "tok_up")
        decisions = self.log.recent_brain_decisions(limit=2)
        self.assertEqual(decisions[0]["agent"], "scanner_executor")
        self.assertEqual(decisions[0]["approved"], 1)

    def test_executes_non_scanner_agent_when_it_emits_canonical_proposal(self):
        self.log.insert_brain_decision(
            agent="external_conviction_api",
            strategy="public_news_event_probability_scalping",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="public_news_approved",
            score=0.86,
            market_type="general_binary",
            features=_features(signal_source="public_news"),
            action="BUY",
            signal_source="public_news",
        )
        engine, pm = self._engine(
            execute=True,
            candidate_agents=("market_scanner", "external_conviction_api"),
        )

        stats = engine.run_once()

        self.assertEqual(stats["executed"], 1)
        pm.execute_market_order.assert_called_once()
        row = self.log.recent_decision_journal(limit=1)[0]
        self.assertEqual(row["decision"], "ENTER")
        self.assertIn('"proposal_source_agent": "external_conviction_api"', row["features_json"])

    def test_executes_shadow_trade_plan_when_it_has_execution_metadata(self):
        self.log.insert_brain_decision(
            agent="external_conviction_divergence",
            strategy="manifold_divergence_event_probability_scalping",
            decision_type="shadow_trade_plan",
            market_id="0xabc",
            token_id="tok_no",
            approved=True,
            reason="manifold divergence",
            score=0.78,
            market_type="general_binary",
            features={
                "entry_price": 0.48,
                "condition_id": "0xabc",
                "clob_token_ids": '["tok_yes", "tok_no"]',
                "outcomes": '["Yes", "No"]',
                "estimated_win_probability_calibrated": True,
            },
            action="SHADOW_BUY_NO",
            signal_source="manifold_divergence",
        )
        engine, pm = self._engine(
            execute=True,
            candidate_agents=("external_conviction_divergence",),
        )

        stats = engine.run_once()

        self.assertEqual(stats["executed"], 1)
        pm.execute_market_order.assert_called_once()
        recommendation = pm.execute_market_order.call_args.args[1]
        self.assertEqual(recommendation.side, "SELL")
        self.assertEqual(self.log.recent(limit=1)[0]["token_id"], "tok_no")

    def test_rejects_non_scanner_agent_without_execution_metadata(self):
        self.log.insert_brain_decision(
            agent="external_conviction_api",
            strategy="public_news_event_probability_scalping",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="public_news_approved",
            score=0.86,
            market_type="general_binary",
            features={"question": "Will BTC be up?", "estimated_win_probability": 0.70},
            action="BUY",
            signal_source="public_news",
        )
        engine, pm = self._engine(
            execute=True,
            candidate_agents=("market_scanner", "external_conviction_api"),
        )

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        self.assertEqual(
            self.log.recent_brain_decisions(limit=1)[0]["reason"],
            "proposal_missing_execution_fields",
        )

    def test_rejects_rank_only_probability(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(
                estimated_win_probability=0.86,
                estimated_win_probability_calibrated=False,
                estimated_win_probability_source="rank_only",
            ),
            action="BUY",
        )
        engine, pm = self._engine(execute=True)

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        self.assertEqual(
            self.log.recent_brain_decisions(limit=1)[0]["reason"],
            "probability_not_calibrated",
        )

    def test_can_gate_live_entries_on_shadow_strategy_scorecard(self):
        Path(self.tmp.name, "strategy_scorecard.json").write_text(
            json.dumps(
                {
                    "strategies": [
                        {
                            "agent": "scanner_executor",
                            "strategy": "execute_scanner_trade_opportunity",
                            "decisions": 100,
                            "approvals": 10,
                            "markout_samples": 10,
                            "avg_markout_pct": -0.02,
                            "promotion_state": "shadow_only",
                            "blockers": ["non_positive_markout"],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(),
            action="BUY",
        )
        engine, pm = self._engine(execute=True, require_promotable_strategy=True)

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        row = self.log.recent_brain_decisions(limit=1)[0]
        self.assertEqual(row["reason"], "strategy_scorecard_not_promotable")
        self.assertIn('"proof_strategy_state": "shadow_only"', row["features_json"])
        self.assertIn('"proof_strategy_avg_markout_pct": -0.02', row["features_json"])

    def test_learning_guard_blocks_sell_after_buy_outperformance_day(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(
                selected_side="SELL",
                selected_token_id="tok_down",
                selected_outcome="Down",
            ),
            action="SELL",
        )
        engine, pm = self._engine(execute=True, learning_guard_enabled=True)

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        self.assertEqual(
            self.log.recent_brain_decisions(limit=1)[0]["reason"],
            "today_lesson_side_blocked",
        )

    def test_learning_guard_blocks_prices_outside_observed_good_band(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(selected_entry_price=0.55),
            action="BUY",
        )
        engine, pm = self._engine(
            execute=True,
            price=0.55,
            learning_guard_enabled=True,
        )

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        self.assertEqual(
            self.log.recent_brain_decisions(limit=1)[0]["reason"],
            "today_lesson_price_band_blocked",
        )

    def test_learning_guard_allows_buy_inside_observed_good_band(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(selected_entry_price=0.46),
            action="BUY",
        )
        engine, pm = self._engine(
            execute=True,
            price=0.46,
            learning_guard_enabled=True,
        )

        stats = engine.run_once()

        self.assertEqual(stats["executed"], 1)
        pm.execute_market_order.assert_called_once()

    def test_learning_guard_expires_after_ttl(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(
                selected_side="SELL",
                selected_token_id="tok_down",
                selected_outcome="Down",
            ),
            action="SELL",
        )
        engine, pm = self._engine(execute=True, learning_guard_enabled=True)
        engine._learning_guard_started_ts -= 25 * 3600

        stats = engine.run_once()

        self.assertEqual(stats["executed"], 1)
        pm.execute_market_order.assert_called_once()

    def test_repeat_reject_quarantines_same_market(self):
        engine, _ = self._engine(execute=True)
        row = {
            "id": 99,
            "market_id": "0xrepeat",
            "score": 0.5,
            "signal_source": "market_scanner",
            "action": "BUY",
            "features_json": json.dumps(_features(condition_id="0xrepeat")),
        }

        for _ in range(engine.cfg.repeat_reject_quarantine_threshold):
            engine._record_reject(row, "internal_edge_too_low", {"raw_ev": -0.04})

        self.assertGreater(engine._market_quarantine_remaining("0xrepeat"), 0)

    def test_rejects_missing_execution_metadata(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(selected_token_id=""),
            action="BUY",
        )
        engine, pm = self._engine(execute=True)

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        self.assertEqual(self.log.filled_positions(), [])
        self.assertEqual(self.log.recent_brain_decisions(limit=1)[0]["reason"], "missing_execution_metadata")

    def test_rejects_negative_live_ev_even_if_scanner_score_is_high(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.900",
            score=0.90,
            market_type="general_binary",
            features=_features(selected_entry_price=0.55, estimated_win_probability=0.54),
            action="BUY",
        )
        engine, pm = self._engine(execute=True, price=0.55)

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        self.assertEqual(self.log.recent_brain_decisions(limit=1)[0]["reason"], "raw_ev_below_council_min")

    def test_rejects_when_net_ev_does_not_cover_round_trip_cost(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.900",
            score=0.90,
            market_type="general_binary",
            features=_features(estimated_win_probability=0.53),
            action="BUY",
        )
        engine, pm = self._engine(execute=True, price=0.50)

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        row = self.log.recent_brain_decisions(limit=1)[0]
        self.assertEqual(row["reason"], "net_ev_below_council_min")
        self.assertIn('"net_ev": 0.02', row["features_json"])

    def test_decision_journal_records_rejects_and_live_enters(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.900",
            score=0.90,
            market_type="general_binary",
            features=_features(estimated_win_probability=0.53),
            action="BUY",
        )
        engine, _ = self._engine(execute=True, price=0.50)

        engine.run_once()

        journal = self.log.recent_decision_journal(limit=1)[0]
        self.assertEqual(journal["decision"], "REJECT")
        self.assertEqual(journal["reason"], "net_ev_below_council_min")
        self.assertAlmostEqual(journal["internal_probability"], 0.53)

        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xdef",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(
                condition_id="0xdef",
                selected_token_id="tok_up2",
                clob_token_ids=["tok_up2", "tok_down2"],
            ),
            action="BUY",
        )
        engine, _ = self._engine(execute=True)

        engine.run_once()

        journal = self.log.recent_decision_journal(limit=1)[0]
        self.assertEqual(journal["decision"], "ENTER")
        self.assertEqual(journal["reason"], "live_executed")
        self.assertGreater(journal["net_ev"], 0.0)

    def test_expert_solo_can_use_lower_net_ev_threshold(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.700",
            score=0.70,
            market_type="general_binary",
            features=_features(
                estimated_win_probability=0.535,
                evidence_route={
                    "mode": "solo",
                    "leader": "wallet:abc",
                    "reason": "expert_solo:wallet:abc",
                },
            ),
            action="BUY",
            signal_source="wallet:abc",
        )
        engine, pm = self._engine(execute=True, price=0.50)

        stats = engine.run_once()

        self.assertEqual(stats["executed"], 1)
        pm.execute_market_order.assert_called_once()
        journal = self.log.recent_decision_journal(limit=1)[0]
        self.assertEqual(journal["mode"], "solo")

    def test_rejects_when_live_entry_price_worsens_after_scan(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.900",
            score=0.90,
            market_type="general_binary",
            features=_features(selected_entry_price=0.50, estimated_win_probability=0.75),
            action="BUY",
        )
        engine, pm = self._engine(execute=True, price=0.52, avg_price=0.53)

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        self.assertEqual(self.log.recent_brain_decisions(limit=1)[0]["reason"], "entry_price_drift_too_high")

    def test_shadow_mode_does_not_call_exchange(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(),
            action="BUY",
        )
        wide_book = {
            "book_quality_score": 0.95,
            "best_bid": 0.47,
            "best_ask": 0.505,
            "spread_pct": 0.069,
            "bid_depth_usdc": 50.0,
            "ask_depth_usdc": 50.0,
            "fillable_usdc": 1.0,
            "avg_entry_price": 0.505,
        }
        engine, pm = self._engine(execute=False, price=0.505, book_quality=wide_book)

        stats = engine.run_once()

        self.assertEqual(stats["shadow"], 1)
        pm.execute_market_order.assert_not_called()
        rows = self.log.filled_positions_with_id()
        self.assertEqual(rows, [])
        self.assertEqual(self.log.filled_positions(), [])
        self.assertFalse(self.log.has_active_trade_for_market("0xabc", token_id="tok_up"))
        raw_rows = self.log.count_recent(FILLED, hours=1)
        self.assertEqual(raw_rows, 0)
        journal = self.log.recent_decision_journal(limit=1)[0]
        self.assertEqual(journal["decision"], "SHADOW_QUOTE")
        self.assertEqual(journal["reason"], "shadow_maker_quoted")

    def test_shadow_market_entry_does_not_create_real_filled_row(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(question="Will this non-crypto event happen?"),
            action="BUY",
        )
        engine, pm = self._engine(
            execute=False,
            price=0.50,
            prefer_maker_for_fast_markets=False,
        )

        stats = engine.run_once()

        self.assertEqual(stats["shadow"], 1)
        pm.execute_market_order.assert_not_called()
        self.assertEqual(self.log.count_recent(FILLED, hours=1), 0)
        self.assertEqual(self.log.recent(limit=1)[0]["status"], "shadow_filled")

    def test_shadow_mode_dedupes_recent_same_market_token(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(),
            action="BUY",
        )
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.861",
            score=0.861,
            market_type="general_binary",
            features=_features(),
            action="BUY",
        )
        wide_book = {
            "book_quality_score": 0.95,
            "best_bid": 0.47,
            "best_ask": 0.505,
            "spread_pct": 0.069,
            "bid_depth_usdc": 50.0,
            "ask_depth_usdc": 50.0,
            "fillable_usdc": 1.0,
            "avg_entry_price": 0.505,
        }
        engine, pm = self._engine(execute=False, price=0.505, book_quality=wide_book)

        stats = engine.run_once()

        self.assertEqual(stats["shadow"], 1)
        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        self.assertEqual(
            self.log.recent_brain_decisions(limit=1)[0]["reason"],
            "shadow_recent_entry_exists",
        )

    def test_blocks_same_market_reentry_after_recent_loss(self):
        self.log.insert_terminal(
            "close-loss",
            "0xabc",
            "closed_stop_loss",
            token_id="tok_up",
            side="SELL",
            price=0.47,
            size_usdc=0.9,
        )
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(
                selected_side="SELL",
                selected_token_id="tok_down",
                estimated_win_probability=0.60,
            ),
            action="SELL",
        )
        engine, pm = self._engine(execute=True)
        engine.cfg.reentry_cooldown_hours = -1

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        row = self.log.recent_brain_decisions(limit=1)[0]
        self.assertEqual(row["reason"], "recent_market_loss_cooldown")
        self.assertIn("do_not_reenter_same_market_immediately_after_loss", row["features_json"])

    def test_rejects_taker_entry_when_spread_alone_crosses_stop(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(
                selected_entry_price=0.27,
                estimated_win_probability=0.80,
            ),
            action="BUY",
        )
        engine, pm = self._engine(
            execute=True,
            price=0.27,
            book_quality={
                "book_quality_score": 0.95,
                "best_bid": 0.25,
                "best_ask": 0.27,
                "spread_pct": 0.074,
                "bid_depth_usdc": 500.0,
                "ask_depth_usdc": 500.0,
                "fillable_usdc": 1.0,
                "avg_entry_price": 0.27,
            },
        )

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        row = self.log.recent_brain_decisions(limit=1)[0]
        self.assertEqual(row["reason"], "taker_entry_below_stop_on_spread")
        self.assertIn('"immediate_exit_loss_pct": 0.0741', row["features_json"])

    def test_wait_timing_can_execute_as_controlled_probe_when_score_is_high(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.792",
            score=0.792,
            market_type="general_binary",
            features=_features(meta_timing="wait", estimated_win_probability=0.58),
            action="BUY",
        )
        engine, pm = self._engine(execute=True, allow_wait_with_high_score=True)

        stats = engine.run_once()

        self.assertEqual(stats["executed"], 1)
        pm.execute_market_order.assert_called_once()
        response = self.log.filled_positions_with_id()[0]["response_json"]
        self.assertIn('"timing_override": true', response)

    def test_executor_preserves_scanner_signal_source_for_feedback(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(signal_source="meta_brain,tavily"),
            action="BUY",
            signal_source="meta_brain,tavily",
        )
        engine, _ = self._engine(execute=True)

        stats = engine.run_once()

        self.assertEqual(stats["executed"], 1)
        row = self.log.recent_brain_decisions(limit=1)[0]
        self.assertEqual(row["signal_source"], "meta_brain,tavily")
        self.assertIn('"scanner_signal_source": "meta_brain,tavily"', row["features_json"])
        self.assertIn('"strategy_family": "news_sentiment_event_driven"', row["features_json"])
        self.assertIn('"regime": "unknown"', row["features_json"])

    def test_regime_router_can_hard_block_mismatched_family_when_enabled(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(
                strategy_family="mean_reversion",
                micro_regime="trending",
                micro_regime_confidence=0.72,
                estimated_win_probability=0.80,
            ),
            action="BUY",
        )
        engine, pm = self._engine(execute=True, enforce_regime_router=True)

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        row = self.log.recent_brain_decisions(limit=1)[0]
        self.assertEqual(row["reason"], "strategy_family_blocked_by_regime")
        self.assertIn('"regime_family_allowed": false', row["features_json"])

    def test_proven_calibrated_source_uses_dedicated_score_floor(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="opportunity_factory_alphainsider_tape prob=0.660",
            score=0.66,
            market_type="crypto_updown",
            features=_features(
                estimated_win_probability=0.66,
                estimated_win_probability_source="alphainsider_proven_family_plus_crypto_tape",
                signal_source="opportunity_factory,alphainsider_proven,crypto_tape",
            ),
            action="BUY",
            signal_source="opportunity_factory,alphainsider_proven,crypto_tape",
        )
        engine, pm = self._engine(
            execute=True,
            min_score=0.79,
            min_proven_calibrated_score=0.54,
            min_net_ev=0.005,
        )

        stats = engine.run_once()

        self.assertEqual(stats["executed"], 1)
        pm.execute_market_order.assert_called_once()

    def test_wait_timing_still_rejects_when_override_score_is_low(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.780",
            score=0.78,
            market_type="general_binary",
            features=_features(meta_timing="wait", estimated_win_probability=0.58),
            action="BUY",
        )
        engine, pm = self._engine(execute=True, allow_wait_with_high_score=True)

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        self.assertEqual(self.log.recent_brain_decisions(limit=1)[0]["reason"], "timing_not_now")

    def test_rejects_when_exit_book_is_too_thin(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(estimated_win_probability=0.70),
            action="BUY",
        )
        engine, pm = self._engine(
            execute=True,
            book_quality={
                "book_quality_score": 0.90,
                "best_bid": 0.49,
                "best_ask": 0.50,
                "spread_pct": 0.02,
                "bid_depth_usdc": 3.0,
                "ask_depth_usdc": 50.0,
                "fillable_usdc": 1.0,
            },
        )

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        row = self.log.recent_brain_decisions(limit=1)[0]
        self.assertEqual(row["reason"], "book_exit_depth_below_min")
        self.assertIn('"decision_council_bid_depth_usdc": 3.0', row["features_json"])

    def test_rejects_when_book_quality_score_is_low(self):
        self.log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id="0xabc",
            approved=True,
            reason="scanner_approved score=0.860",
            score=0.86,
            market_type="general_binary",
            features=_features(estimated_win_probability=0.70),
            action="BUY",
        )
        engine, pm = self._engine(
            execute=True,
            book_quality={
                "book_quality_score": 0.40,
                "best_bid": 0.48,
                "best_ask": 0.50,
                "spread_pct": 0.04,
                "bid_depth_usdc": 50.0,
                "ask_depth_usdc": 50.0,
                "fillable_usdc": 1.0,
            },
        )

        stats = engine.run_once()

        self.assertEqual(stats["skipped"], 1)
        pm.execute_market_order.assert_not_called()
        self.assertEqual(self.log.recent_brain_decisions(limit=1)[0]["reason"], "book_quality_below_min")


if __name__ == "__main__":
    unittest.main()
