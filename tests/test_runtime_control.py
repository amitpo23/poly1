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
                budget=15.0,
                wallet_balance=34.2452,
                max_open=100,
                max_position_fraction="0.03",
                max_daily_token_usd="10.0",
                position_size_usdc="1.50",
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
            self.assertEqual(control["budget_usdc"], 15.0)
            self.assertFalse(halt.exists())


if __name__ == "__main__":
    unittest.main()
