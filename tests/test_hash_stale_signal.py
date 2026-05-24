"""Tests for the hash-mismatch self-heal signal added 2026-05-24.

When risk_gate detects RUNTIME_CONFIG_HASH != control.config_hash:
1. Write a marker file ./data/<agent>_HASH_STALE with diagnostic JSON
2. Log CRITICAL (in addition to the existing WARNING from ok())

A supervisor script (scripts/health_check.py) lists these markers
and exits non-zero so cron / on-call tooling can surface them.

Before this commit, hash mismatches only surfaced as one of thousands
of WARNING lines — which is exactly how bug #1 hid for an entire
trading session on 2026-05-24.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class HashStaleSignalTests(unittest.TestCase):
    """The risk_gate side: writing the marker + logging CRITICAL."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name) / "data"
        self.data_dir.mkdir(parents=True)
        self.control_path = self.data_dir / "runtime_control.json"
        # Seed a control file with a known hash.
        self.control_path.write_text(json.dumps({
            "mode": "live",
            "allowed_live_agents": ["scanner_executor"],
            "config_hash": "EXPECTED_HASH",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }))

    def tearDown(self):
        self.tmpdir.cleanup()

    def _make_gate(self):
        from agents.application.risk_gate import RiskGate
        tl = MagicMock()
        pm = MagicMock()
        gate = RiskGate.__new__(RiskGate)
        gate.runtime_control_file = self.control_path
        gate.kill_switch_file = self.data_dir / "HALT"
        gate.trade_log = tl
        gate.polymarket = pm
        return gate

    def test_hash_mismatch_writes_marker(self):
        gate = self._make_gate()
        os.environ["RUNTIME_AGENT"] = "scanner_executor"
        os.environ["RUNTIME_CONFIG_HASH"] = "WRONG_HASH"
        try:
            reason = gate.runtime_control_reason()
        finally:
            del os.environ["RUNTIME_AGENT"]
            del os.environ["RUNTIME_CONFIG_HASH"]
        self.assertIsNotNone(reason)
        self.assertIn("hash mismatch", reason)
        marker = self.data_dir / "scanner_executor_HASH_STALE"
        self.assertTrue(marker.exists(), f"marker not found at {marker}")
        payload = json.loads(marker.read_text())
        self.assertEqual(payload["agent"], "scanner_executor")
        self.assertEqual(payload["expected_hash"], "EXPECTED_HASH")
        self.assertEqual(payload["actual_hash"], "WRONG_HASH")
        self.assertIn("force-recreate", payload["remediation"])

    def test_hash_match_writes_no_marker(self):
        gate = self._make_gate()
        os.environ["RUNTIME_AGENT"] = "scanner_executor"
        os.environ["RUNTIME_CONFIG_HASH"] = "EXPECTED_HASH"
        try:
            reason = gate.runtime_control_reason()
        finally:
            del os.environ["RUNTIME_AGENT"]
            del os.environ["RUNTIME_CONFIG_HASH"]
        self.assertIsNone(reason)
        self.assertFalse(
            list(self.data_dir.glob("*_HASH_STALE")),
            "marker should not exist when hashes match",
        )

    def test_unset_actual_hash_records_unset_token(self):
        # Allow btc_5min so we reach the hash check (otherwise the
        # agent-allowlist guard short-circuits first).
        self.control_path.write_text(json.dumps({
            "mode": "live",
            "allowed_live_agents": ["btc_5min"],
            "config_hash": "EXPECTED_HASH",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }))
        gate = self._make_gate()
        os.environ["RUNTIME_AGENT"] = "btc_5min"
        # No RUNTIME_CONFIG_HASH set.
        os.environ.pop("RUNTIME_CONFIG_HASH", None)
        try:
            gate.runtime_control_reason()
        finally:
            del os.environ["RUNTIME_AGENT"]
        marker = self.data_dir / "btc_5min_HASH_STALE"
        self.assertTrue(marker.exists())
        self.assertIn("<unset>", marker.read_text())

    def test_critical_log_emitted(self):
        gate = self._make_gate()
        os.environ["RUNTIME_AGENT"] = "scanner_executor"
        os.environ["RUNTIME_CONFIG_HASH"] = "WRONG"
        try:
            with self.assertLogs("agents.application.risk_gate", level="CRITICAL") as cm:
                gate.runtime_control_reason()
        finally:
            del os.environ["RUNTIME_AGENT"]
            del os.environ["RUNTIME_CONFIG_HASH"]
        self.assertTrue(
            any("HASH STALE" in m for m in cm.output),
            f"Expected HASH STALE in CRITICAL log; got {cm.output}",
        )


class HealthCheckScriptTests(unittest.TestCase):
    """The supervisor side: scripts/health_check.py."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_finds_no_markers_in_clean_dir(self):
        from scripts.health_check import find_hash_stale_markers
        result = find_hash_stale_markers(self.data_dir)
        self.assertEqual(result, [])

    def test_finds_marker_and_parses(self):
        from scripts.health_check import find_hash_stale_markers
        (self.data_dir / "scanner_executor_HASH_STALE").write_text(json.dumps({
            "ts": "2026-05-24T18:00:00+00:00",
            "agent": "scanner_executor",
            "expected_hash": "AAA",
            "actual_hash": "BBB",
            "remediation": "docker compose up -d --force-recreate scanner-executor",
        }))
        result = find_hash_stale_markers(self.data_dir)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["agent"], "scanner_executor")
        self.assertEqual(result[0]["expected_hash"], "AAA")
        self.assertEqual(result[0]["actual_hash"], "BBB")
        self.assertIsNotNone(result[0]["age_seconds"])

    def test_handles_malformed_marker_via_filename(self):
        """If the marker JSON is broken, derive agent from filename."""
        from scripts.health_check import find_hash_stale_markers
        (self.data_dir / "btc_5min_HASH_STALE").write_text("{not json")
        result = find_hash_stale_markers(self.data_dir)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["agent"], "btc_5min")

    def test_handles_missing_data_dir(self):
        from scripts.health_check import find_hash_stale_markers
        result = find_hash_stale_markers(self.data_dir / "does_not_exist")
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
