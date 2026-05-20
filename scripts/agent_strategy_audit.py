#!/usr/bin/env python3
"""Audit entry agents against the live trading policy.

This is a static "commander's inspection": it does not trade, touch the
exchange, or require secrets.  It checks whether every configured entry agent
has one clear policy owner, compose service, execution flag, code entrypoint,
brain/risk hooks, journal writes, and tests.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "deploy" / "runtime_policy.json"
COMPOSE = ROOT / "docker-compose.yml"

AGENT_CODE = {
    "trader": ["agents/application/trade.py"],
    "scalper": ["agents/application/scalper.py"],
    "btc_daily": ["agents/application/btc_daily.py"],
    "btc_5min": ["agents/application/btc_5min.py"],
    "near_resolution": ["agents/application/near_resolution.py"],
    "news_shock": ["agents/application/news_shock.py"],
    "wallet_follow": ["agents/application/wallet_follow.py"],
    "external_conviction_api": ["agents/application/external_conviction.py"],
    "scanner_executor": ["agents/application/scanner_executor.py"],
}


def audit() -> dict:
    policy = json.loads(POLICY.read_text())
    compose_text = COMPOSE.read_text()
    tests = {p.name: p.read_text(errors="ignore") for p in (ROOT / "tests").glob("test_*.py")}
    rows = []
    for agent, cfg in policy.get("entry_agents", {}).items():
        code_paths = [ROOT / p for p in AGENT_CODE.get(agent, [])]
        code_text = "\n".join(p.read_text(errors="ignore") for p in code_paths if p.exists())
        test_hits = sorted(name for name, text in tests.items() if agent.replace("_api", "") in text or agent in name)
        service = str(cfg.get("compose_service") or "")
        row = {
            "agent": agent,
            "execute_flag": cfg.get("execute_flag"),
            "reserve_flag": cfg.get("reserve_flag"),
            "compose_service": service,
            "compose_profile": cfg.get("compose_profile"),
            "policy_defined": True,
            "compose_service_found": f"\n  {service}:" in compose_text or compose_text.startswith(f"{service}:"),
            "code_files": [str(p.relative_to(ROOT)) for p in code_paths if p.exists()],
            "has_code": any(p.exists() for p in code_paths),
            "uses_risk_gate": "RiskGate" in code_text or "risk_gate" in code_text,
            "uses_brain": any(
                token in code_text
                for token in ("MetaBrain", "MarketBrain", "DecisionCouncil", "meta_brain", "market_brain", "decision_council")
            ),
            "writes_brain_decisions": "insert_brain_decision" in code_text,
            "writes_trade_log": "TradeLog" in code_text or "insert_pending" in code_text,
            "has_shadow_or_execute_gate": any(
                token in code_text
                for token in (cfg.get("execute_flag") or "", "execute=", "EXECUTE_", "shadow")
            ),
            "tests": test_hits,
            "issues": [],
        }
        if not row["compose_service_found"]:
            row["issues"].append("compose_service_missing")
        if not row["has_code"]:
            row["issues"].append("code_file_missing")
        if not row["uses_risk_gate"]:
            row["issues"].append("risk_gate_not_detected")
        if not row["uses_brain"] and agent not in {"scalper"}:
            row["issues"].append("brain_hook_not_detected")
        if not row["writes_brain_decisions"]:
            row["issues"].append("brain_decision_journal_missing")
        if not row["writes_trade_log"]:
            row["issues"].append("trade_log_missing")
        if not row["has_shadow_or_execute_gate"]:
            row["issues"].append("execute_shadow_gate_not_detected")
        if not row["tests"]:
            row["issues"].append("tests_not_detected")
        rows.append(row)
    shadow_rows = []
    for name, cfg in policy.get("shadow_research_services", {}).items():
        service = str(cfg.get("compose_service") or "")
        shadow_rows.append({
            "agent": name,
            "compose_service": service,
            "compose_profile": cfg.get("compose_profile"),
            "compose_service_found": f"\n  {service}:" in compose_text,
            "writes_live_orders": bool(cfg.get("writes_live_orders")),
        })
    return {
        "version": policy.get("version"),
        "entry_agent_count": len(rows),
        "shadow_research_count": len(shadow_rows),
        "issue_count": sum(len(row["issues"]) for row in rows),
        "rows": rows,
        "shadow_research": shadow_rows,
    }


def to_markdown(report: dict) -> str:
    lines = [
        "# Agent Strategy Audit",
        "",
        f"- Entry agents: {report['entry_agent_count']}",
        f"- Static issues: {report['issue_count']}",
        "",
        "| Agent | Compose | Brain | Risk | Journal | Shadow/Execute | Tests | Issues |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["rows"]:
        lines.append(
            "| {agent} | {compose} | {brain} | {risk} | {journal} | {gate} | {tests} | {issues} |".format(
                agent=row["agent"],
                compose="yes" if row["compose_service_found"] else "no",
                brain="yes" if row["uses_brain"] else "no",
                risk="yes" if row["uses_risk_gate"] else "no",
                journal="yes" if row["writes_brain_decisions"] else "no",
                gate="yes" if row["has_shadow_or_execute_gate"] else "no",
                tests=", ".join(row["tests"]) or "no",
                issues=", ".join(row["issues"]) or "none",
            )
        )
    lines.append("")
    if report.get("shadow_research"):
        lines.extend([
            "## Shadow Research Services",
            "",
            "| Agent | Compose | Profile | Live orders |",
            "| --- | --- | --- | --- |",
        ])
        for row in report["shadow_research"]:
            lines.append(
                f"| {row['agent']} | {'yes' if row['compose_service_found'] else 'no'} | "
                f"{row.get('compose_profile') or ''} | {'yes' if row['writes_live_orders'] else 'no'} |"
            )
        lines.append("")
    lines.append("This audit is static. A clean row means the wiring is visible in source; live readiness still requires runtime preflight.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit entry agents against runtime policy")
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    report = audit()
    text = json.dumps(report, indent=2, sort_keys=True) if args.format == "json" else to_markdown(report)
    if args.output:
        Path(args.output).write_text(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
