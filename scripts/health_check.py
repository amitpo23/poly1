#!/usr/bin/env python3
"""Operational health check — surface conditions that need attention.

Currently checks for HASH_STALE marker files written by risk_gate when
a container detects its RUNTIME_CONFIG_HASH no longer matches the
runtime_control.json file. Stale-hash containers silently refuse every
trade. Without this check, the only way to notice was by tailing logs
and grepping for WARNINGs — exactly the failure mode that hid bug #1
during the entire Round 1-10 session on 2026-05-24.

Exit codes:
  0 — healthy
  1 — at least one stale-hash agent detected
  2 — script error

Usage:
  python3 scripts/health_check.py
  python3 scripts/health_check.py --data-dir /srv/poly1/data --json
  python3 scripts/health_check.py --clear  # remove markers after review
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def find_hash_stale_markers(data_dir: Path) -> list[dict]:
    """Read every *_HASH_STALE marker in data_dir.

    Returns a list of dicts: {agent, expected, actual, ts, age_seconds,
    marker_path}.
    """
    out: list[dict] = []
    if not data_dir.exists():
        return out
    for marker in sorted(data_dir.glob("*_HASH_STALE")):
        try:
            raw = marker.read_text()
            payload = json.loads(raw) if raw.strip() else {}
        except (OSError, ValueError):
            payload = {}
        agent = (
            payload.get("agent")
            or marker.name.replace("_HASH_STALE", "")
        )
        ts_str = payload.get("ts") or ""
        age_seconds: float | None = None
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age_seconds = (datetime.now(timezone.utc) - ts).total_seconds()
        except ValueError:
            pass
        out.append({
            "agent": agent,
            "expected_hash": payload.get("expected_hash", "<unknown>"),
            "actual_hash": payload.get("actual_hash", "<unknown>"),
            "ts": ts_str,
            "age_seconds": age_seconds,
            "marker_path": str(marker),
            "remediation": payload.get("remediation", ""),
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        default="./data",
        help="Directory containing risk_gate marker files",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit findings as JSON (default: human-readable)",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="Remove HASH_STALE markers after listing them (acknowledge)",
    )
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    try:
        markers = find_hash_stale_markers(data_dir)
    except Exception as exc:  # pragma: no cover — operator-facing
        print(f"health_check: error scanning {data_dir}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump({"hash_stale": markers, "count": len(markers)}, sys.stdout, indent=2)
        print()
    else:
        if not markers:
            print(f"OK — no stale-hash containers in {data_dir}")
        else:
            print(f"CRITICAL — {len(markers)} stale-hash container(s) detected:\n")
            for m in markers:
                age = (
                    f"{m['age_seconds']:.0f}s ago"
                    if m["age_seconds"] is not None else "unknown age"
                )
                print(f"  agent: {m['agent']}")
                print(f"    expected: {m['expected_hash']}")
                print(f"    actual:   {m['actual_hash']}")
                print(f"    detected: {m['ts']} ({age})")
                if m["remediation"]:
                    print(f"    fix:      {m['remediation']}")
                print()

    if args.clear and markers:
        for m in markers:
            try:
                Path(m["marker_path"]).unlink()
            except OSError as exc:
                print(
                    f"health_check: failed to clear {m['marker_path']}: {exc}",
                    file=sys.stderr,
                )

    return 0 if not markers else 1


if __name__ == "__main__":
    raise SystemExit(main())
