from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.brain_indicator_cycle import BrainIndicatorConfig, build_steps, run_once


class BrainIndicatorCycleTests(unittest.TestCase):
    def test_build_steps_includes_shadow_dispatch_guard(self):
        cfg = BrainIndicatorConfig(
            run_market_universe=False,
            run_alphainsider=False,
            run_markouts=False,
            run_provider_scorecard=False,
            run_strategy_scorecard=False,
            run_opportunity_factory=False,
            run_market_scanner=False,
            dispatch_scanner_executor=True,
            run_backup=False,
            no_trade_guard=True,
            allow_live_dispatch=False,
        )

        steps = build_steps(cfg)

        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0][0], "scanner_executor_dispatch")
        self.assertEqual(steps[0][2]["EXECUTE_SCANNER_EXECUTOR"], "false")
        self.assertEqual(steps[0][2]["EXECUTE"], "false")
        self.assertEqual(steps[0][2]["RUNTIME_AGENT"], "scanner_executor")

    def test_live_dispatch_uses_scanner_executor_runtime_agent(self):
        cfg = BrainIndicatorConfig(
            run_market_universe=False,
            run_alphainsider=False,
            run_markouts=False,
            run_provider_scorecard=False,
            run_strategy_scorecard=False,
            run_opportunity_factory=False,
            run_market_scanner=False,
            dispatch_scanner_executor=True,
            run_backup=False,
            no_trade_guard=False,
            allow_live_dispatch=True,
        )

        steps = build_steps(cfg)

        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0][0], "scanner_executor_dispatch")
        self.assertEqual(steps[0][2], {"RUNTIME_AGENT": "scanner_executor"})

    def test_market_universe_paths_follow_cycle_data_dir(self):
        cfg = BrainIndicatorConfig(
            data_dir="/tmp/poly1-cycle",
            run_market_universe=True,
            run_alphainsider=False,
            run_markouts=False,
            run_provider_scorecard=False,
            run_strategy_scorecard=False,
            run_market_scanner=False,
            dispatch_scanner_executor=False,
            run_backup=False,
        )

        steps = build_steps(cfg)

        self.assertEqual(steps[0][0], "market_universe")
        self.assertEqual(
            steps[0][2]["MARKET_UNIVERSE_OUTPUT_PATH"],
            "/tmp/poly1-cycle/market_universe.json",
        )
        self.assertEqual(
            steps[0][2]["MARKET_UNIVERSE_HEARTBEAT_PATH"],
            "/tmp/poly1-cycle/market_universe_heartbeat",
        )

    def test_run_once_applies_no_trade_env_to_every_step(self):
        seen_envs = []

        def runner(cmd, env, timeout):
            seen_envs.append((cmd, env, timeout))
            return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": true}', stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            cfg = BrainIndicatorConfig(
                db_path=str(Path(tmp) / "trade_log.db"),
                data_dir=tmp,
                state_path=str(Path(tmp) / "state.json"),
                report_path=str(Path(tmp) / "report.json"),
                heartbeat_path=str(Path(tmp) / "heartbeat"),
                run_market_universe=False,
                run_alphainsider=False,
                run_markouts=False,
                run_provider_scorecard=False,
                run_strategy_scorecard=False,
                run_opportunity_factory=False,
                run_market_scanner=True,
                dispatch_scanner_executor=True,
                run_backup=False,
                no_trade_guard=True,
            )
            report = run_once(cfg, runner=runner)

            self.assertTrue(report["ok"])
            self.assertTrue(Path(cfg.report_path).exists())
            self.assertTrue(Path(cfg.heartbeat_path).exists())
            self.assertEqual(len(seen_envs), 2)
            for _, env, _ in seen_envs:
                self.assertEqual(env["EXECUTE"], "false")
                self.assertEqual(env["EXECUTE_SCANNER_EXECUTOR"], "false")

    def test_market_scanner_disables_expensive_providers_by_default(self):
        cfg = BrainIndicatorConfig(
            run_market_universe=False,
            run_alphainsider=False,
            run_markouts=False,
            run_provider_scorecard=False,
            run_strategy_scorecard=False,
            run_opportunity_factory=False,
            run_market_scanner=True,
            dispatch_scanner_executor=False,
            run_backup=False,
            enable_tavily=False,
            enable_llm=False,
            tavily_daily_limit=1,
        )

        steps = build_steps(cfg)

        self.assertEqual(steps[0][0], "market_scanner")
        env = steps[0][2]
        self.assertEqual(env["TAVILY_ENABLED"], "false")
        self.assertEqual(env["TAVILY_DAILY_LIMIT"], "1")
        self.assertEqual(env["META_BRAIN_STRADDLE_TAVILY_ENABLED"], "false")
        self.assertEqual(env["META_BRAIN_STRADDLE_LLM_ENABLED"], "false")

    def test_run_once_skips_steps_until_their_cadence_is_due(self):
        calls = []

        def runner(cmd, env, timeout):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            cfg = BrainIndicatorConfig(
                data_dir=tmp,
                state_path=str(Path(tmp) / "state.json"),
                report_path=str(Path(tmp) / "report.json"),
                heartbeat_path=str(Path(tmp) / "heartbeat"),
                run_market_universe=False,
                run_alphainsider=True,
                run_markouts=False,
                run_provider_scorecard=False,
                run_strategy_scorecard=False,
                run_opportunity_factory=False,
                run_market_scanner=True,
                dispatch_scanner_executor=False,
                run_backup=False,
                alphainsider_interval_sec=900,
                market_scanner_interval_sec=60,
            )

            first = run_once(cfg, runner=runner)
            second = run_once(cfg, runner=runner)

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(step.get("skipped") for step in second["steps"]))
        self.assertTrue(all(step.get("skip_reason") == "cadence" for step in second["steps"]))

    def test_run_backup_step_added_with_correct_args(self):
        """Backup step must invoke scripts/python/backup_trade_log.py with
        the configured db_path and backup_dir. Addresses preflight
        trade_log_backup BLOCKED (50h vs 30h threshold)."""
        cfg = BrainIndicatorConfig(
            db_path="/tmp/poly1/trade_log.db",
            run_market_universe=False,
            run_alphainsider=False,
            run_markouts=False,
            run_provider_scorecard=False,
            run_strategy_scorecard=False,
            run_opportunity_factory=False,
            run_market_scanner=False,
            dispatch_scanner_executor=False,
            run_backup=True,
            backup_dir="/tmp/poly1/backups",
        )
        steps = build_steps(cfg)
        self.assertEqual(len(steps), 1)
        name, cmd, _env = steps[0]
        self.assertEqual(name, "trade_log_backup")
        self.assertIn("scripts/python/backup_trade_log.py", cmd)
        self.assertIn("/tmp/poly1/trade_log.db", cmd)
        self.assertIn("/tmp/poly1/backups", cmd)

    def test_allow_live_is_blocked_when_no_trade_guard_is_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = BrainIndicatorConfig(
                data_dir=tmp,
                report_path=str(Path(tmp) / "report.json"),
                heartbeat_path=str(Path(tmp) / "heartbeat"),
                run_market_universe=False,
                run_alphainsider=False,
                run_markouts=False,
                run_provider_scorecard=False,
                run_strategy_scorecard=False,
                run_opportunity_factory=False,
                run_market_scanner=False,
                dispatch_scanner_executor=False,
                run_backup=False,
                no_trade_guard=True,
                allow_live_dispatch=True,
            )

            report = run_once(cfg, runner=lambda cmd, env, timeout: subprocess.CompletedProcess(cmd, 0))

        self.assertFalse(report["ok"])
        self.assertIn("allow_live_dispatch_ignored_by_no_trade_guard", report["blockers"])


if __name__ == "__main__":
    unittest.main()
