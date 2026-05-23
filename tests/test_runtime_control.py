import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


def _load_runtime_control():
    path = Path(__file__).resolve().parents[1] / "scripts" / "runtime_control.py"
    spec = importlib.util.spec_from_file_location("runtime_control_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class RuntimeControlTests(unittest.TestCase):
    def test_runtime_policy_exposes_external_strategy_family(self):
        policy_path = Path(__file__).resolve().parents[1] / "deploy" / "runtime_policy.json"
        policy = json.loads(policy_path.read_text())
        agents = policy["entry_agents"]
        expected = {
            "external_conviction",
            "external_conviction_api",
            "external_conviction_polifly",
            "external_conviction_whale",
            "external_conviction_divergence",
            "external_conviction_debate",
            "external_conviction_aggregator",
            "external_conviction_tradingview",
            "external_conviction_technical",
            "external_conviction_gdelt",
            "external_conviction_crypto_deriv",
        }
        self.assertTrue(expected.issubset(agents))
        for agent in expected:
            self.assertEqual(agents[agent]["execute_flag"], "EXECUTE_EXTERNAL_CONVICTION")
            self.assertEqual(agents[agent]["reserve_flag"], "EXTERNAL_CONVICTION_RESERVE_USDC")

    def test_runtime_policy_exposes_router_signal_services(self):
        policy_path = Path(__file__).resolve().parents[1] / "deploy" / "runtime_policy.json"
        policy = json.loads(policy_path.read_text())
        services = set(policy.get("live_signal_services") or {})
        expected = {
            "market_scanner",
            "brain_indicator_cycle",
            "market_universe",
            "orderbook_monitor",
            "news_signal",
            "wallet_watcher",
            "hermes_forecast",
        }
        self.assertTrue(expected.issubset(services))
        for service in expected:
            meta = policy["live_signal_services"][service]
            self.assertFalse(meta["writes_live_orders"], service)
            self.assertIsNone(meta["execute_flag"], service)
            self.assertTrue(meta["compose_service"], service)

    def test_live_hour_budget_sets_reserve_adjusted_floor(self):
        rc = _load_runtime_control()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deploy").mkdir()
            (root / "data").mkdir()
            policy = {
                "entry_agents": {
                    "near_resolution": {
                        "execute_flag": "EXECUTE_NEAR_RESOLUTION",
                        "reserve_flag": "NEAR_RESOLUTION_RESERVE_USDC",
                    },
                    "trader": {
                        "execute_flag": "EXECUTE",
                    },
                }
            }
            (root / "deploy" / "runtime_policy.json").write_text(json.dumps(policy))
            halt = root / "data" / "HALT"
            halt.write_text("halt\n")

            rc.ROOT = root
            rc.POLICY_PATH = root / "deploy" / "runtime_policy.json"
            rc.ENV_RUNTIME_PATH = root / "deploy" / ".env.runtime"
            rc.CONTROL_PATH = root / "data" / "runtime_control.json"
            rc.HALT_PATH = halt

            args = argparse.Namespace(
                agents="near_resolution",
                minutes=60,
                max_hold_minutes=45,
                budget=15.0,
                wallet_balance=34.2452,
                equity_balance=35.125,
                max_open=100,
                max_position_fraction="0.03",
                max_daily_token_usd="10.0",
                position_size_usdc="1.50",
                scanner_allow_wait=True,
                scanner_wait_min_score="0.79",
                aggressive_execution=True,
                note="test",
                arm=True,
            )
            rc.live_hour(args)

            env_text = rc.ENV_RUNTIME_PATH.read_text()
            control = json.loads(rc.CONTROL_PATH.read_text())
            self.assertIn('MIN_USDC_FLOOR="4.2452"', env_text)
            self.assertNotIn('MIN_USDC_FLOOR="19.2452"', env_text)
            self.assertIn('NEAR_RESOLUTION_POSITION_SIZE_USDC="1.50"', env_text)
            self.assertIn('NEWS_SHOCK_POSITION_SIZE_USDC="1.50"', env_text)
            self.assertIn('WALLET_FOLLOW_POSITION_SIZE_USDC="1.50"', env_text)
            self.assertIn('EXTERNAL_CONVICTION_POSITION_SIZE_USDC="1.50"', env_text)
            self.assertIn('BTC_DAILY_POSITION_SIZE_USDC="1.50"', env_text)
            self.assertIn('BTC_5MIN_POSITION_SIZE_USDC="1.50"', env_text)
            self.assertIn('SCANNER_EXECUTOR_ALLOW_WAIT_WITH_HIGH_SCORE="true"', env_text)
            self.assertIn('SCANNER_EXECUTOR_REQUIRE_CALIBRATED_PROBABILITY="true"', env_text)
            self.assertIn('SCANNER_EXECUTOR_LEARNING_GUARD_ENABLED="true"', env_text)
            self.assertIn('SCANNER_EXECUTOR_LEARNING_PREFERRED_SIDE="BUY"', env_text)
            self.assertIn('SCANNER_EXECUTOR_LEARNING_MAX_ENTRY_PRICE="0.49"', env_text)
            self.assertIn('SCANNER_EXECUTOR_MARKET_LOSS_COOLDOWN_HOURS="1.0"', env_text)
            self.assertIn('SCANNER_EXECUTOR_WAIT_OVERRIDE_MIN_SCORE="0.79"', env_text)
            self.assertIn('SCANNER_EXECUTOR_MIN_SCORE="0.79"', env_text)
            self.assertIn('SCANNER_EXECUTOR_MAX_ENTRY_DRIFT_PCT="0.10"', env_text)
            self.assertIn('SCANNER_EXECUTOR_MAX_IMMEDIATE_EXIT_LOSS_PCT="0.10"', env_text)
            self.assertIn('SCANNER_EXECUTOR_MIN_RAW_EV="0.015"', env_text)
            self.assertIn('SCANNER_EXECUTOR_MIN_NET_EV="0.005"', env_text)
            self.assertIn('SCANNER_EXECUTOR_BATCH_LIMIT="200"', env_text)
            self.assertIn('SCANNER_MAX_CANDIDATES="160"', env_text)
            self.assertIn('SCANNER_TARGET_TRADE_DECISIONS="12"', env_text)
            self.assertIn('MARKET_UNIVERSE_TREND_LIMIT="250"', env_text)
            self.assertIn('ORDERBOOK_MONITOR_TOKEN_LIMIT="180"', env_text)
            self.assertIn('EXTERNAL_CONVICTION_POLL_SEC="60"', env_text)
            self.assertIn('DECISION_COUNCIL_MIN_NET_EV="0.005"', env_text)
            self.assertIn('MAINTAIN_MIN_EXIT_NOTIONAL_USDC="0.50"', env_text)
            self.assertIn('MAINTAIN_MIN_TAKE_PROFIT_NET_PCT="0.015"', env_text)
            self.assertIn('MAINTAIN_MIN_TAKE_PROFIT_USDC="0.01"', env_text)
            self.assertIn('POLY1_MAX_HOLD_SECONDS="2700"', env_text)
            self.assertIn('MAINTAIN_MAX_HOLD_HOURS="0.7500"', env_text)
            self.assertEqual(control["budget_usdc"], 15.0)
            self.assertEqual(control["wallet_balance_at_start_usdc"], 34.2452)
            self.assertEqual(control["equity_at_start_usdc"], 35.125)
            self.assertEqual(control["max_hold_minutes"], 45)
            self.assertTrue(control["aggressive_execution"])
            self.assertIn("router_signal_services", control)
            self.assertFalse(halt.exists())

    def test_live_hour_all_expands_agents_and_reports_signal_services(self):
        rc = _load_runtime_control()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deploy").mkdir()
            (root / "data").mkdir()
            policy = {
                "entry_agents": {
                    "scanner_executor": {
                        "execute_flag": "EXECUTE_SCANNER_EXECUTOR",
                        "reserve_flag": "SCANNER_EXECUTOR_RESERVE_USDC",
                    },
                    "wallet_follow": {
                        "execute_flag": "EXECUTE_WALLET_FOLLOW",
                        "reserve_flag": "WALLET_FOLLOW_RESERVE_USDC",
                    },
                },
                "live_signal_services": {
                    "market_scanner": {
                        "compose_service": "market_scanner",
                        "compose_profile": "scanner",
                        "execute_flag": None,
                        "writes_live_orders": False,
                    }
                },
                "shadow_research_services": {
                    "crypto_5m_market_maker_shadow": {
                        "compose_service": "crypto-5m-market-maker-shadow",
                        "compose_profile": "research",
                        "execute_flag": None,
                        "writes_live_orders": False,
                    }
                },
            }
            (root / "deploy" / "runtime_policy.json").write_text(json.dumps(policy))
            halt = root / "data" / "HALT"
            halt.write_text("halt\n")

            rc.ROOT = root
            rc.POLICY_PATH = root / "deploy" / "runtime_policy.json"
            rc.ENV_RUNTIME_PATH = root / "deploy" / ".env.runtime"
            rc.CONTROL_PATH = root / "data" / "runtime_control.json"
            rc.HALT_PATH = halt

            args = argparse.Namespace(
                agents="all",
                minutes=15,
                max_hold_minutes=30,
                budget=4.0,
                wallet_balance=24.0,
                equity_balance=24.0,
                max_open=2,
                max_trades_per_hour=10,
                max_position_fraction="0.03",
                max_daily_token_usd="10.0",
                position_size_usdc="1.00",
                scanner_allow_wait=False,
                scanner_wait_min_score="0.79",
                scanner_learning_guard_enabled=True,
                scanner_learning_preferred_side="BUY",
                scanner_learning_allow_proven_side_override=False,
                scanner_learning_allow_proven_price_override=False,
                scanner_learning_min_entry_price="0.40",
                scanner_learning_max_entry_price="0.49",
                scanner_recent_close_skip_hours=12,
                scanner_executor_reentry_cooldown_hours=12,
                scanner_executor_market_loss_cooldown_hours=0.5,
                aggressive_execution=False,
                lab_mode=False,
                note="all agents",
                arm=False,
            )
            rc.live_hour(args)

            env_text = rc.ENV_RUNTIME_PATH.read_text()
            control = json.loads(rc.CONTROL_PATH.read_text())
            self.assertEqual(control["allowed_live_agents"], ["scanner_executor", "wallet_follow"])
            self.assertEqual(
                control["router_signal_services"],
                ["crypto_5m_market_maker_shadow", "market_scanner"],
            )
            self.assertIn('ROUTER_LIVE_ENTRY_AGENTS="scanner_executor,wallet_follow"', env_text)
            self.assertIn('SCANNER_EXECUTOR_CANDIDATE_AGENTS="market_scanner,scanner_executor,wallet_follow"', env_text)
            self.assertIn(
                'ROUTER_SIGNAL_SERVICES="crypto_5m_market_maker_shadow,market_scanner"',
                env_text,
            )
            self.assertIn('SCANNER_EXECUTOR_MARKET_LOSS_COOLDOWN_HOURS="0.5"', env_text)
            self.assertTrue(halt.exists())

    def test_shadow_probe_allows_riskgate_without_live_execute_flags(self):
        rc = _load_runtime_control()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deploy").mkdir()
            (root / "data").mkdir()
            policy = {
                "entry_agents": {
                    "scanner_executor": {
                        "execute_flag": "EXECUTE_SCANNER_EXECUTOR",
                        "reserve_flag": "SCANNER_EXECUTOR_RESERVE_USDC",
                    },
                }
            }
            (root / "deploy" / "runtime_policy.json").write_text(json.dumps(policy))
            halt = root / "data" / "HALT"
            halt.write_text("halt\n")

            rc.ROOT = root
            rc.POLICY_PATH = root / "deploy" / "runtime_policy.json"
            rc.ENV_RUNTIME_PATH = root / "deploy" / ".env.runtime"
            rc.CONTROL_PATH = root / "data" / "runtime_control.json"
            rc.HALT_PATH = halt

            args = argparse.Namespace(
                agent="scanner_executor",
                minutes=30,
                position_size_usdc="1.00",
                scanner_allow_wait=True,
                scanner_wait_min_score="0.79",
                note="shadow",
                arm=True,
            )
            rc.shadow_probe(args)

            env_text = rc.ENV_RUNTIME_PATH.read_text()
            control = json.loads(rc.CONTROL_PATH.read_text())
            self.assertIn('RUNTIME_MODE="paper"', env_text)
            self.assertIn('EXECUTE="false"', env_text)
            self.assertIn('EXECUTE_SCANNER_EXECUTOR="false"', env_text)
            self.assertIn('SCANNER_EXECUTOR_MIN_SCORE="0.79"', env_text)
            self.assertIn('SCANNER_EXECUTOR_REQUIRE_CALIBRATED_PROBABILITY="true"', env_text)
            self.assertEqual(control["mode"], "paper")
            self.assertEqual(control["allowed_live_agents"], ["scanner_executor"])
            self.assertTrue(control["shadow_only"])
            self.assertFalse(halt.exists())

    def test_shadow_probe_accepts_multiple_agents_without_live_execute_flags(self):
        rc = _load_runtime_control()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "deploy").mkdir()
            (root / "data").mkdir()
            policy = {
                "entry_agents": {
                    "scanner_executor": {
                        "execute_flag": "EXECUTE_SCANNER_EXECUTOR",
                        "reserve_flag": "SCANNER_EXECUTOR_RESERVE_USDC",
                    },
                    "btc_5min": {
                        "execute_flag": "EXECUTE_BTC_5MIN",
                        "reserve_flag": "BTC_5MIN_RESERVE_USDC",
                    },
                }
            }
            (root / "deploy" / "runtime_policy.json").write_text(json.dumps(policy))
            halt = root / "data" / "HALT"
            halt.write_text("halt\n")

            rc.ROOT = root
            rc.POLICY_PATH = root / "deploy" / "runtime_policy.json"
            rc.ENV_RUNTIME_PATH = root / "deploy" / ".env.runtime"
            rc.CONTROL_PATH = root / "data" / "runtime_control.json"
            rc.HALT_PATH = halt

            args = argparse.Namespace(
                agent="scanner_executor,btc_5min",
                minutes=30,
                position_size_usdc="1.00",
                scanner_allow_wait=True,
                scanner_wait_min_score="0.79",
                note="shadow suite",
                arm=True,
            )
            rc.shadow_probe(args)

            env_text = rc.ENV_RUNTIME_PATH.read_text()
            control = json.loads(rc.CONTROL_PATH.read_text())
            self.assertIn('EXECUTE_SCANNER_EXECUTOR="false"', env_text)
            self.assertIn('EXECUTE_BTC_5MIN="false"', env_text)
            self.assertEqual(control["mode"], "paper")
            self.assertEqual(control["allowed_live_agents"], ["scanner_executor", "btc_5min"])
            self.assertEqual(control["budget_usdc"], 0.0)
            self.assertTrue(control["shadow_only"])
            self.assertFalse(halt.exists())


class LearningGuardDefaultsTests(unittest.TestCase):
    """Verify the scanner_executor learning guard env vars persist across all
    runtime modes (regression for 2026-05-21 production drawdown where the
    guard was inert because env vars were missing from .env.runtime)."""

    def _setup_root(self, tmp: str, policy: dict) -> "object":
        rc = _load_runtime_control()
        root = Path(tmp)
        (root / "deploy").mkdir()
        (root / "data").mkdir()
        (root / "deploy" / "runtime_policy.json").write_text(json.dumps(policy))
        rc.ROOT = root
        rc.POLICY_PATH = root / "deploy" / "runtime_policy.json"
        rc.ENV_RUNTIME_PATH = root / "deploy" / ".env.runtime"
        rc.CONTROL_PATH = root / "data" / "runtime_control.json"
        rc.HALT_PATH = root / "data" / "HALT"
        return rc

    def _assert_learning_guard_defaults(self, env_text: str) -> None:
        self.assertIn('SCANNER_EXECUTOR_LEARNING_GUARD_ENABLED="true"', env_text)
        self.assertIn('SCANNER_EXECUTOR_LEARNING_PREFERRED_SIDE="BUY"', env_text)
        self.assertIn('SCANNER_EXECUTOR_LEARNING_MIN_ENTRY_PRICE="0.40"', env_text)
        self.assertIn('SCANNER_EXECUTOR_LEARNING_MAX_ENTRY_PRICE="0.49"', env_text)
        self.assertIn(
            'SCANNER_EXECUTOR_LEARNING_ALLOW_PROVEN_SIDE_OVERRIDE="false"', env_text
        )
        self.assertIn(
            'SCANNER_EXECUTOR_LEARNING_ALLOW_PROVEN_PRICE_OVERRIDE="false"', env_text
        )
        self.assertIn('SCANNER_EXECUTOR_LEARNING_GUARD_TTL_HOURS="24"', env_text)

    def test_freeze_persists_learning_guard_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = {
                "entry_agents": {},
                "live_signal_services": {},
                "shadow_research_services": {},
            }
            rc = self._setup_root(tmp, policy)
            rc.freeze(argparse.Namespace(note=""))
            self._assert_learning_guard_defaults(rc.ENV_RUNTIME_PATH.read_text())

    def test_live_probe_persists_learning_guard_defaults(self):
        # live_probe() raises SystemExit if args.agent is not in entry_agents,
        # so the policy fixture must include at least one matching agent.
        with tempfile.TemporaryDirectory() as tmp:
            policy = {
                "entry_agents": {
                    "trader": {"execute_flag": "EXECUTE"},
                },
                "live_signal_services": {},
                "shadow_research_services": {},
            }
            rc = self._setup_root(tmp, policy)
            args = argparse.Namespace(
                agent="trader",
                budget=5.0,
                note="",
                arm=False,
            )
            rc.live_probe(args)
            self._assert_learning_guard_defaults(rc.ENV_RUNTIME_PATH.read_text())


if __name__ == "__main__":
    unittest.main()
