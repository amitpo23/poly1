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
            self.assertIn('SCANNER_EXECUTOR_WAIT_OVERRIDE_MIN_SCORE="0.79"', env_text)
            self.assertIn('SCANNER_EXECUTOR_MIN_SCORE="0.79"', env_text)
            self.assertIn('SCANNER_EXECUTOR_MAX_ENTRY_DRIFT_PCT="0.10"', env_text)
            self.assertIn('SCANNER_EXECUTOR_MAX_IMMEDIATE_EXIT_LOSS_PCT="0.10"', env_text)
            self.assertIn('SCANNER_EXECUTOR_MIN_RAW_EV="0.015"', env_text)
            self.assertIn('SCANNER_EXECUTOR_MIN_NET_EV="0.005"', env_text)
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
            self.assertFalse(halt.exists())

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


if __name__ == "__main__":
    unittest.main()
