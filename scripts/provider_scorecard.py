#!/usr/bin/env python3
"""Build a provider/source scorecard from recorded brain decisions.

The output is intentionally simple JSON so MetaBrain can consume it as
``PROVIDER_SCORECARD_PATH`` and operators can inspect it after a QA run.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path


WIN_OUTCOMES = {
    "closed_take_profit",
    "resolved_yes",
    "resolved_no",
    "resolved_skipped_no",
}
LOSS_OUTCOMES = {
    "closed_stop_loss",
    "closed_timeout",
    "resolved_loss",
}


def wilson_lower_bound(wins: int, total: int, z: float = 1.96):
    if total <= 0:
        return None
    phat = wins / total
    denom = 1.0 + z * z / total
    centre = phat + z * z / (2.0 * total)
    margin = z * ((phat * (1.0 - phat) + z * z / (4.0 * total)) / total) ** 0.5
    return max(0.0, (centre - margin) / denom)


def split_sources(raw: str | None) -> list[str]:
    if not raw:
        return ["unknown"]
    parts = []
    for chunk in str(raw).replace(";", ",").split(","):
        item = chunk.strip()
        if item:
            parts.append(item)
    return parts or ["unknown"]


def build_scorecard(db_path: str, *, min_matched: int = 1) -> dict:
    groups = defaultdict(lambda: {"wins": 0, "losses": 0, "scores": [], "rows": 0})
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='brain_decisions'"
        ).fetchone()
        if not exists:
            return {"providers": [], "provider_count": 0, "reason": "no_brain_decisions_table"}
        rows = conn.execute(
            """
            SELECT signal_source, outcome_status, score
            FROM brain_decisions
            WHERE approved = 1
              AND outcome_status IS NOT NULL
            """
        ).fetchall()

    for row in rows:
        outcome = row["outcome_status"]
        if outcome not in WIN_OUTCOMES and outcome not in LOSS_OUTCOMES:
            continue
        for source in split_sources(row["signal_source"]):
            groups[source]["rows"] += 1
            if outcome in WIN_OUTCOMES:
                groups[source]["wins"] += 1
            else:
                groups[source]["losses"] += 1
            try:
                groups[source]["scores"].append(float(row["score"]))
            except (TypeError, ValueError):
                pass

    providers = []
    for source, stats in groups.items():
        matched = int(stats["wins"] + stats["losses"])
        if matched < min_matched:
            continue
        winrate = stats["wins"] / matched if matched else None
        providers.append(
            {
                "source": source,
                "matched": matched,
                "wins": int(stats["wins"]),
                "losses": int(stats["losses"]),
                "winrate": None if winrate is None else round(winrate, 4),
                "wilson_lower": None if matched <= 0 else round(wilson_lower_bound(stats["wins"], matched), 4),
                "avg_score": (
                    None
                    if not stats["scores"]
                    else round(sum(stats["scores"]) / len(stats["scores"]), 4)
                ),
            }
        )

    providers.sort(
        key=lambda r: (
            r["wilson_lower"] if r["wilson_lower"] is not None else -1.0,
            r["winrate"] if r["winrate"] is not None else -1.0,
            r["matched"],
        ),
        reverse=True,
    )
    return {"providers": providers, "provider_count": len(providers)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build provider scorecard JSON")
    parser.add_argument("--db", default="./data/trade_log.db")
    parser.add_argument("--out", default="./data/provider_scorecard.json")
    parser.add_argument("--min-matched", type=int, default=1)
    args = parser.parse_args()

    payload = build_scorecard(args.db, min_matched=args.min_matched)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(out)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
