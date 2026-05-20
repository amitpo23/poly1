import json
import unittest
from pathlib import Path

from agents.application.trading_policy import (
    AGENT_MANIFEST,
    FAST_TAKE_PROFIT_PCT,
    PROFIT_TAKE_ALLOWED_PCT,
    MARKET_SCAN_SECONDS,
    MAX_AGENT_ALLOCATION_FRACTION,
    MAX_TRADES_PER_HOUR,
    PREFERRED_TAKE_PROFIT_HIGH_PCT,
    POSITION_POLL_SECONDS,
    REQUIRE_BRAIN_APPROVAL,
    SOFT_STOP_LOSS_PCT,
    STOP_LOSS_PCT,
    TAKE_PROFIT_CAP_PCT,
    TELEGRAM_REPORT_SECONDS,
    TradingPolicy,
)


ROOT = Path(__file__).resolve().parents[1]


class TestTradingPolicyContract(unittest.TestCase):
    def test_canonical_defaults_match_commander_policy(self):
        policy = TradingPolicy()
        self.assertEqual(policy.soft_stop_loss_pct, 0.03)
        self.assertEqual(policy.stop_loss_pct, 0.06)
        self.assertEqual(policy.profit_take_allowed_pct, 0.015)
        self.assertEqual(policy.fast_take_profit_pct, 0.04)
        self.assertEqual(policy.preferred_take_profit_high_pct, 0.08)
        self.assertEqual(policy.take_profit_cap_pct, 0.25)
        self.assertEqual(policy.max_hold_seconds, 21600)
        self.assertEqual(policy.position_poll_seconds, 60)
        self.assertEqual(policy.market_scan_seconds, 60)
        self.assertEqual(policy.telegram_report_seconds, 3600)
        self.assertEqual(policy.max_trades_per_hour, 100)
        self.assertEqual(policy.max_agent_allocation_fraction, 0.50)
        self.assertTrue(policy.require_brain_approval)

        self.assertEqual(SOFT_STOP_LOSS_PCT, 0.03)
        self.assertEqual(STOP_LOSS_PCT, 0.06)
        self.assertEqual(PROFIT_TAKE_ALLOWED_PCT, 0.015)
        self.assertEqual(FAST_TAKE_PROFIT_PCT, 0.04)
        self.assertEqual(PREFERRED_TAKE_PROFIT_HIGH_PCT, 0.08)
        self.assertEqual(TAKE_PROFIT_CAP_PCT, 0.25)
        self.assertEqual(POSITION_POLL_SECONDS, 60)
        self.assertEqual(MARKET_SCAN_SECONDS, 60)
        self.assertEqual(TELEGRAM_REPORT_SECONDS, 3600)
        self.assertEqual(MAX_TRADES_PER_HOUR, 100)
        self.assertEqual(MAX_AGENT_ALLOCATION_FRACTION, 0.50)
        self.assertTrue(REQUIRE_BRAIN_APPROVAL)

    def test_runtime_policy_covers_live_capable_entry_agents(self):
        runtime = json.loads((ROOT / "deploy/runtime_policy.json").read_text())
        entry_agents = set(runtime["entry_agents"])
        live_capable = {
            name
            for name, meta in AGENT_MANIFEST.items()
            if meta["places_orders"] == "yes"
        }
        # external_conviction_api is the runtime name for the executable
        # external-conviction service.
        live_capable.discard("external_conviction")
        live_capable.add("external_conviction_api")
        self.assertTrue(live_capable.issubset(entry_agents))

    def test_live_entry_agents_have_compose_guards(self):
        runtime = json.loads((ROOT / "deploy/runtime_policy.json").read_text())
        for name, meta in runtime["entry_agents"].items():
            if name == "trader":
                continue
            self.assertTrue(meta.get("execute_flag"), name)
            self.assertTrue(meta.get("compose_service"), name)
            self.assertIn("compose_profile", meta, name)
        support = runtime.get("live_support_services") or {}
        for required in (
            "trader",
            "position_manager",
            "trading_supervisor",
            "settlement_reconciler",
            "telegram_reporter",
        ):
            self.assertIn(required, support)
            self.assertTrue(support[required].get("compose_service"))

    def test_secret_free_examples_match_policy(self):
        env_text = (ROOT / ".env.example").read_text()
        self.assertIn('CYCLE_SECONDS="60"', env_text)
        self.assertIn('MARKET_BRAIN_EXIT_TAKE_PROFIT_PCT="0.25"', env_text)
        self.assertIn('MARKET_BRAIN_SMART_EXIT_MIN_PROFIT_PCT="0.015"', env_text)
        self.assertIn('MARKET_BRAIN_PREFERRED_TAKE_PROFIT_PCT="0.04"', env_text)
        self.assertIn('MARKET_BRAIN_EXIT_SOFT_STOP_LOSS_PCT="0.03"', env_text)
        self.assertIn('MARKET_BRAIN_EXIT_STOP_LOSS_PCT="0.06"', env_text)
        self.assertIn('MARKET_BRAIN_EXIT_MAX_HOLD_SECONDS="21600"', env_text)
        self.assertIn('MAINTAIN_TAKE_PROFIT_PCT="0.25"', env_text)
        self.assertIn('MAINTAIN_SOFT_STOP_LOSS_PCT="0.03"', env_text)
        self.assertIn('MAINTAIN_STOP_LOSS_PCT="0.06"', env_text)
        self.assertIn('MAINTAIN_PROFIT_TAKE_ALLOWED_PCT="0.015"', env_text)
        self.assertIn('MAINTAIN_PREFERRED_TAKE_PROFIT_PCT="0.04"', env_text)
        self.assertIn('MAINTAIN_PREFERRED_TAKE_PROFIT_HIGH_PCT="0.08"', env_text)
        self.assertIn('MAINTAIN_IMMEDIATE_REVIEW_MOVE_PCT="0.02"', env_text)
        self.assertIn('MAINTAIN_MAX_HOLD_HOURS="6"', env_text)
        self.assertIn('MAINTAIN_POLL_SEC="60"', env_text)
        self.assertIn('MAINTAIN_LLM_EXIT_INTERVAL_SEC="60"', env_text)
        self.assertIn('MAX_TRADES_PER_HOUR="100"', env_text)
        self.assertIn('MAX_AGENT_ALLOCATION_FRACTION="0.50"', env_text)
        self.assertIn('POLY1_REQUIRE_BRAIN_APPROVAL="true"', env_text)
        self.assertIn('KELLY_SIZING_ENABLED="true"', env_text)
        self.assertIn('KELLY_FRACTION_SCALE="0.25"', env_text)
        self.assertIn('META_BRAIN_MIN_RAW_EV="0.04"', env_text)
        self.assertIn('META_BRAIN_WINRATE_PRIOR="0.50"', env_text)
        self.assertIn('META_BRAIN_ANCHOR_THRESHOLD="0.70"', env_text)
        self.assertIn('META_BRAIN_MIN_WEIGHTED_SCORE_ANCHOR="0.40"', env_text)
        self.assertIn('META_BRAIN_WEIGHT_NEWS="0.10"', env_text)
        self.assertIn('META_BRAIN_EXECUTION_QUALITY_ENABLED="true"', env_text)
        self.assertIn('EXECUTION_QUALITY_REQUIRE_FRESH="true"', env_text)
        self.assertIn('EXECUTION_QUALITY_MAX_SPREAD_PCT="0.05"', env_text)
        self.assertIn('EXECUTION_QUALITY_FEE_BUFFER_PCT="0.01"', env_text)
        self.assertIn('EXECUTION_QUALITY_MIN_NET_EV="0.02"', env_text)
        self.assertIn('ORDERBOOK_MONITOR_POLL_SEC="1"', env_text)
        self.assertIn('ORDERBOOK_MONITOR_STALE_MARKET_GRACE_SEC="300"', env_text)
        self.assertIn('PREFLIGHT_MAX_DISK_USED_PCT="85"', env_text)
        self.assertIn('PREFLIGHT_REQUIRE_DB_BACKUP="true"', env_text)
        self.assertIn('EXTERNAL_CONVICTION_ALLOW_WEAK_PROVIDERS="false"', env_text)
        self.assertIn('PROVIDER_SCORECARD_PATH="/app/data/provider_scorecard.json"', env_text)
        self.assertIn('EXTERNAL_CONVICTION_AGGREGATOR_PROVIDERS="manifold,metaculus,kalshi,technical_signal,clob_whale"', env_text)
        self.assertNotIn('EXTERNAL_CONVICTION_AGGREGATOR_PROVIDERS="manifold,metaculus,kalshi,technical_signal,clob_whale,gdelt,public_news,heuristic"', env_text)
        self.assertIn('EXPERT_SOLO_MIN_WINRATE="0.65"', env_text)
        self.assertIn('EXPERT_SOLO_MIN_WILSON="0.58"', env_text)
        self.assertIn('EXPERT_SOLO_MIN_SAMPLES="30"', env_text)
        self.assertIn('EXPERT_WALLET_EXTERNAL_MIN_WINRATE="0.70"', env_text)
        self.assertIn('EXPERT_WALLET_EXTERNAL_MIN_TRADES="50"', env_text)
        self.assertIn('MAINTAIN_PARTIAL_TAKE_PROFIT_ENABLED="true"', env_text)
        self.assertIn('MAINTAIN_PARTIAL_TAKE_PROFIT_PCT="0.10"', env_text)
        self.assertIn('MAINTAIN_PARTIAL_TAKE_PROFIT_FRACTION="0.50"', env_text)
        self.assertIn('MAINTAIN_PARTIAL_TAKE_PROFIT_MIN_POSITION_USDC="500.0"', env_text)
        self.assertIn('TELEGRAM_REPORT_SECONDS="3600"', env_text)
        self.assertIn('TELEGRAM_REPORT_SEND_ON_START="false"', env_text)
        self.assertIn('TELEGRAM_DIRECT_NOTIFICATIONS="false"', env_text)
        self.assertIn('TELEGRAM_TRADE_ALERTS="true"', env_text)
        self.assertIn('SCANNER_POLL_SEC="60"', env_text)

    def test_compose_has_operator_dashboard(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertIn("telegram-reporter:", compose)
        self.assertIn("deploy/.env.runtime", compose)
        self.assertIn('RUNTIME_AGENT: telegram_reporter', compose)
        self.assertIn('command: ["python", "scripts/python/telegram_report.py", "--daemon"]', compose)
        self.assertIn('CYCLE_SECONDS: "60"', compose)
        self.assertIn('SCANNER_POLL_SEC: "60"', compose)
        self.assertIn('MAINTAIN_LLM_EXIT_INTERVAL_SEC: "60"', compose)
        self.assertIn("orderbook-monitor:", compose)
        self.assertIn('RUNTIME_AGENT: orderbook_monitor', compose)
        self.assertIn('command: ["python", "-m", "agents.application.orderbook_monitor"]', compose)

    def test_live_entry_brain_failures_are_fail_closed(self):
        for rel in (
            "agents/application/trade.py",
            "agents/application/scalper.py",
            "agents/application/btc_daily.py",
            "agents/application/btc_5min.py",
            "agents/application/news_shock.py",
            "agents/application/wallet_follow.py",
            "agents/application/external_conviction.py",
        ):
            text = (ROOT / rel).read_text()
            self.assertNotIn("brain gate failed (fail-open)", text, rel)
            self.assertNotIn("brain_entry_gate failed for %s (fail-open)", text, rel)
            self.assertRegex(text, r"block(?:ed|ing) (?:entry|— missing MarketBrain)", rel)


if __name__ == "__main__":
    unittest.main()
