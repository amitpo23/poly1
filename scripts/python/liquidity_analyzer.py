"""Phase 1 — Backward liquidity analyzer.

Built 2026-05-27 per advisor's recommendation. Inputs: 7 days of closed
btc5min_timed (v1 + v2) trades joined to orderbook_snapshots at entry
time. Outputs: hour-of-day WR with CIs, depth-vs-outcome correlation,
single-feature gate simulator, slippage analysis.

NO writes to runtime config. Pure analysis script.

Usage (server-side):
  docker exec poly1-live-dashboard python3 /tmp/liquidity_analyzer.py
  # or
  scp this file then run inside any container that mounts /app/data
"""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DB_PATH = "/app/data/trade_log.db"
LOOKBACK_DAYS = 7
SNAPSHOT_TOLERANCE_SEC = 60  # how close in time the snapshot must be to entry


# ---------- helpers ----------------------------------------------------------

def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% CI for binomial proportion."""
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - spread), min(1.0, center + spread))


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def classify_outcome(close_row: dict | None) -> str:
    """Map close-row status to a single label."""
    if close_row is None:
        return "unknown"
    s = (close_row.get("status") or "").lower()
    if s.startswith("closed_take_profit"):
        return "TP"
    if s.startswith("closed_stop_loss"):
        return "SL"
    if s.startswith("closed_timeout"):
        return "TIMEOUT"
    if s.startswith("resolved_win") or s.startswith("closed_win"):
        return "TP"
    if s.startswith("resolved_loss"):
        return "SL"
    return "OTHER"


# ---------- main analyzer ----------------------------------------------------

class Analyzer:
    def __init__(self, db_path: str, lookback_days: int):
        self.db_path = db_path
        self.lookback_days = lookback_days
        # use plain connect (mode=ro via uri sometimes fails inside containers);
        # we never write so this is read-only by convention.
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row

    # entry rows we care about
    def fetch_entries(self) -> list[dict]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        ).isoformat()
        cur = self.conn.execute(
            """
            SELECT id, ts, cycle_id, token_id, market_id, side, price, size_usdc,
                   status, response_json
            FROM trades
            WHERE ts > ?
              AND cycle_id LIKE 'btc5min_timed%'
              AND status IN ('btc5min_timed_open', 'btc5min_timed_v2_open',
                             'closed_take_profit', 'closed_stop_loss',
                             'closed_timeout', 'closed_failed',
                             'failed', 'resolved_loss', 'resolved_win')
            ORDER BY id
            """,
            (cutoff,),
        )
        return [dict(r) for r in cur.fetchall()]

    def fetch_close_for(self, token_id: str, after_id: int) -> Optional[dict]:
        cur = self.conn.execute(
            """
            SELECT id, ts, status, price, response_json
            FROM trades
            WHERE token_id = ?
              AND id > ?
              AND (status LIKE 'closed_%' OR status LIKE 'resolved_%')
            ORDER BY id
            LIMIT 1
            """,
            (token_id, after_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def fetch_snapshot(self, token_id: str, ts: str) -> Optional[dict]:
        """Find the orderbook_snapshot closest to `ts` within tolerance."""
        target_ts = parse_ts(ts)
        before = (target_ts - timedelta(seconds=SNAPSHOT_TOLERANCE_SEC)).isoformat()
        after = (target_ts + timedelta(seconds=SNAPSHOT_TOLERANCE_SEC)).isoformat()
        cur = self.conn.execute(
            """
            SELECT ts, best_bid, best_ask, mid, spread_pct,
                   bid_depth_usdc, ask_depth_usdc, bid_levels, ask_levels, imbalance
            FROM orderbook_snapshots
            WHERE token_id = ?
              AND ts BETWEEN ? AND ?
            ORDER BY ABS(strftime('%s', ts) - strftime('%s', ?))
            LIMIT 1
            """,
            (token_id, before, after, ts),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def build_dataset(self) -> list[dict]:
        """One row per closed entry with all features + outcome."""
        rows = []
        entries = self.fetch_entries()
        # Filter to entry rows only (status indicates open or failed-at-entry)
        open_statuses = {
            "btc5min_timed_open", "btc5min_timed_v2_open", "failed",
        }
        for e in entries:
            if e["status"] not in open_statuses:
                continue
            close = self.fetch_close_for(e["token_id"], e["id"])
            snap = self.fetch_snapshot(e["token_id"], e["ts"])
            outcome = classify_outcome(close) if close else (
                "FAILED" if e["status"] == "failed" else "OPEN"
            )
            try:
                resp = json.loads(e.get("response_json") or "{}")
            except Exception:
                resp = {}
            phase = resp.get("phase") or (
                "phase1" if "phase1" in e["cycle_id"]
                else ("phase2" if "phase2" in e["cycle_id"] else "?")
            )
            agent = "v2" if "btc5min_timed_v2" in e["cycle_id"] else "v1"
            entry_ts = parse_ts(e["ts"])
            close_price = float(close["price"]) if close and close.get("price") else None
            entry_price = float(e["price"] or 0)
            slippage_pct = None
            if close and outcome == "SL" and entry_price > 0 and close_price:
                # SL exit price vs entry — negative number for loss
                if e["side"] == "BUY":
                    slippage_pct = (close_price - entry_price) / entry_price
                else:  # SELL — own NO/DOWN token; closing means selling the same
                    slippage_pct = (close_price - entry_price) / entry_price

            rows.append({
                "id": e["id"],
                "ts": e["ts"],
                "hour_utc": entry_ts.hour,
                "agent": agent,
                "phase": phase,
                "side": e["side"],
                "entry_price": entry_price,
                "size_usdc": float(e["size_usdc"] or 0),
                "outcome": outcome,
                "close_price": close_price,
                "sl_slippage_pct": slippage_pct,
                "snap_best_bid": snap.get("best_bid") if snap else None,
                "snap_best_ask": snap.get("best_ask") if snap else None,
                "snap_spread_pct": snap.get("spread_pct") if snap else None,
                "snap_bid_depth_usdc": snap.get("bid_depth_usdc") if snap else None,
                "snap_ask_depth_usdc": snap.get("ask_depth_usdc") if snap else None,
                "snap_imbalance": snap.get("imbalance") if snap else None,
                "snap_found": snap is not None,
            })
        return rows

    # ---------- aggregations ------------------------------------------------

    @staticmethod
    def wr_by_hour(rows: list[dict]) -> list[dict]:
        bucket = defaultdict(lambda: {"tp": 0, "sl": 0, "to": 0, "fail": 0, "other": 0})
        for r in rows:
            b = bucket[r["hour_utc"]]
            o = r["outcome"]
            if o == "TP":
                b["tp"] += 1
            elif o == "SL":
                b["sl"] += 1
            elif o == "TIMEOUT":
                b["to"] += 1
            elif o == "FAILED":
                b["fail"] += 1
            else:
                b["other"] += 1
        out = []
        for hour in sorted(bucket.keys()):
            b = bucket[hour]
            tp = b["tp"]
            n = tp + b["sl"] + b["to"]  # exclude failed/other from WR
            wr = tp / n if n else 0
            lo, hi = wilson_ci(tp, n)
            out.append({
                "hour_utc": hour, "n": n, "tp": tp, "sl": b["sl"], "to": b["to"],
                "fail": b["fail"], "wr": wr, "wr_lo95": lo, "wr_hi95": hi,
            })
        return out

    @staticmethod
    def wr_by_phase(rows: list[dict]) -> dict:
        by_phase: dict[str, dict] = defaultdict(
            lambda: {"tp": 0, "sl": 0, "to": 0, "fail": 0}
        )
        for r in rows:
            p = r["phase"]
            o = r["outcome"]
            if o == "TP":
                by_phase[p]["tp"] += 1
            elif o == "SL":
                by_phase[p]["sl"] += 1
            elif o == "TIMEOUT":
                by_phase[p]["to"] += 1
            elif o == "FAILED":
                by_phase[p]["fail"] += 1
        out = {}
        for phase, b in by_phase.items():
            n = b["tp"] + b["sl"] + b["to"]
            wr = b["tp"] / n if n else 0
            lo, hi = wilson_ci(b["tp"], n)
            out[phase] = {**b, "n": n, "wr": wr, "wr_lo95": lo, "wr_hi95": hi}
        return out

    @staticmethod
    def gate_simulator(rows: list[dict], feature_key: str,
                        thresholds: list[float]) -> list[dict]:
        """For each threshold, simulate 'skip if feature < threshold'.

        Reports: how many losers (SL+TIMEOUT) and winners (TP) would have
        been skipped, and the resulting WR if we'd traded only the kept set.
        """
        results = []
        # Only consider closed trades with a valid feature value
        valid = [r for r in rows if r["outcome"] in ("TP", "SL", "TIMEOUT")
                 and r.get(feature_key) is not None]
        if not valid:
            return results
        for thr in thresholds:
            kept = [r for r in valid if r[feature_key] >= thr]
            skipped = [r for r in valid if r[feature_key] < thr]
            kept_tp = sum(1 for r in kept if r["outcome"] == "TP")
            kept_sl = sum(1 for r in kept if r["outcome"] == "SL")
            kept_to = sum(1 for r in kept if r["outcome"] == "TIMEOUT")
            skip_tp = sum(1 for r in skipped if r["outcome"] == "TP")
            skip_sl = sum(1 for r in skipped if r["outcome"] == "SL")
            kept_n = kept_tp + kept_sl + kept_to
            kept_wr = kept_tp / kept_n if kept_n else 0
            lo, hi = wilson_ci(kept_tp, kept_n)
            results.append({
                "threshold": thr,
                "kept_n": kept_n,
                "kept_tp": kept_tp, "kept_sl": kept_sl, "kept_to": kept_to,
                "kept_wr": kept_wr, "kept_wr_lo95": lo, "kept_wr_hi95": hi,
                "skipped_n": len(skipped),
                "skipped_tp": skip_tp, "skipped_sl": skip_sl,
                "feature": feature_key,
            })
        return results

    @staticmethod
    def slippage_vs_depth(rows: list[dict]) -> dict:
        """Correlation: SL exit slippage vs bid_depth_usdc at entry."""
        sl_rows = [
            r for r in rows
            if r["outcome"] == "SL"
            and r.get("sl_slippage_pct") is not None
            and r.get("snap_bid_depth_usdc") is not None
        ]
        if len(sl_rows) < 5:
            return {"n": len(sl_rows), "note": "need at least 5 SL points"}
        # Bucket by depth
        buckets = {"≤1": [], "1-5": [], "5-20": [], ">20": []}
        for r in sl_rows:
            d = r["snap_bid_depth_usdc"]
            sl_pct = r["sl_slippage_pct"]
            if d <= 1:
                buckets["≤1"].append(sl_pct)
            elif d <= 5:
                buckets["1-5"].append(sl_pct)
            elif d <= 20:
                buckets["5-20"].append(sl_pct)
            else:
                buckets[">20"].append(sl_pct)
        return {
            "n": len(sl_rows),
            "buckets": {
                k: {
                    "n": len(v),
                    "avg_slip_pct": (sum(v) / len(v)) if v else None,
                    "median": (statistics.median(v) if v else None),
                }
                for k, v in buckets.items()
            },
        }


# ---------- output -----------------------------------------------------------

def main() -> int:
    db = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    a = Analyzer(db, LOOKBACK_DAYS)
    rows = a.build_dataset()
    print(f"=== Liquidity Analyzer — last {LOOKBACK_DAYS} days ===")
    print(f"DB: {db}")
    print(f"entry rows analyzed: {len(rows)}")
    n_with_snap = sum(1 for r in rows if r["snap_found"])
    print(f"  with orderbook snapshot: {n_with_snap}/{len(rows)} ({100*n_with_snap/max(1,len(rows)):.1f}%)")
    closed = [r for r in rows if r["outcome"] in ("TP", "SL", "TIMEOUT")]
    print(f"  closed (TP/SL/TIMEOUT): {len(closed)}")
    print()

    # WR by hour
    print("=== WR BY HOUR-OF-DAY (UTC) ===")
    print(f"{'hr':>3s} {'n':>4s} {'TP':>3s} {'SL':>3s} {'TO':>3s} {'WR':>6s} {'95% CI':>14s}")
    for h in a.wr_by_hour(rows):
        ci = f"[{h['wr_lo95']:.2f}, {h['wr_hi95']:.2f}]"
        print(f"{h['hour_utc']:>3d} {h['n']:>4d} {h['tp']:>3d} {h['sl']:>3d} "
              f"{h['to']:>3d} {h['wr']*100:>5.0f}% {ci:>14s}")
    print()

    # WR by phase
    print("=== WR BY PHASE ===")
    by_phase = a.wr_by_phase(rows)
    for phase in sorted(by_phase.keys()):
        p = by_phase[phase]
        ci = f"[{p['wr_lo95']:.2f}, {p['wr_hi95']:.2f}]"
        print(f"  {phase:8s} n={p['n']:3d} TP={p['tp']} SL={p['sl']} "
              f"TO={p['to']} WR={p['wr']*100:.0f}% CI={ci}")
    print()

    # Single-feature gate sim: bid_depth_usdc
    print("=== GATE SIMULATOR — bid_depth_usdc thresholds ===")
    print("(skip entries where pre-entry bid depth < threshold)")
    print(f"{'thr$':>6s} {'kept':>5s} {'TP':>3s} {'SL':>3s} {'TO':>3s} {'WR':>6s} "
          f"{'skipped':>8s} {'skip TP':>8s} {'skip SL':>8s}")
    g_results = a.gate_simulator(
        rows, "snap_bid_depth_usdc",
        thresholds=[0.0, 1.0, 5.0, 10.0, 25.0, 50.0, 100.0],
    )
    for g in g_results:
        print(f"{g['threshold']:>6.1f} {g['kept_n']:>5d} {g['kept_tp']:>3d} "
              f"{g['kept_sl']:>3d} {g['kept_to']:>3d} "
              f"{g['kept_wr']*100:>5.0f}% {g['skipped_n']:>8d} "
              f"{g['skipped_tp']:>8d} {g['skipped_sl']:>8d}")
    print()

    # ask_depth + spread + imbalance
    for feature, thresholds in [
        ("snap_ask_depth_usdc", [0.0, 1.0, 5.0, 25.0, 100.0]),
        ("snap_spread_pct", [1.0, 0.10, 0.05, 0.03, 0.02]),
    ]:
        print(f"=== GATE SIMULATOR — {feature} ===")
        if "spread" in feature:
            print("(skip entries where spread_pct >= threshold; lower=tighter)")
        else:
            print("(skip entries where feature < threshold)")
        print(f"{'thr':>6s} {'kept':>5s} {'TP':>3s} {'SL':>3s} {'WR':>6s} {'skip':>6s}")
        # For spread, invert the gate direction
        spread = "spread" in feature
        valid = [r for r in rows
                 if r["outcome"] in ("TP", "SL", "TIMEOUT")
                 and r.get(feature) is not None]
        for thr in thresholds:
            if spread:
                kept = [r for r in valid if r[feature] < thr]
            else:
                kept = [r for r in valid if r[feature] >= thr]
            kept_n = len(kept)
            tp = sum(1 for r in kept if r["outcome"] == "TP")
            sl = sum(1 for r in kept if r["outcome"] == "SL")
            wr = tp / kept_n if kept_n else 0
            print(f"{thr:>6.3f} {kept_n:>5d} {tp:>3d} {sl:>3d} {wr*100:>5.0f}% "
                  f"{len(valid)-kept_n:>6d}")
        print()

    # Slippage vs depth
    print("=== SL SLIPPAGE vs BID DEPTH (at entry) ===")
    sd = a.slippage_vs_depth(rows)
    if "note" in sd:
        print(f"  {sd['note']} (n={sd['n']})")
    else:
        print(f"  total SL trades with depth data: {sd['n']}")
        for bucket, stats in sd["buckets"].items():
            if stats["n"] == 0:
                continue
            avg = stats["avg_slip_pct"] or 0
            med = stats["median"] or 0
            print(f"  depth bucket ${bucket:6s}: n={stats['n']:3d}  "
                  f"avg slip = {avg*100:+6.1f}%  median = {med*100:+6.1f}%")
    print()

    # Side x phase x outcome
    print("=== SIDE x PHASE breakdown ===")
    triplet = defaultdict(lambda: {"TP": 0, "SL": 0, "TIMEOUT": 0, "FAILED": 0})
    for r in rows:
        k = f"{r['side']}/{r['phase']}"
        if r["outcome"] in triplet[k]:
            triplet[k][r["outcome"]] += 1
    for k in sorted(triplet.keys()):
        t = triplet[k]
        n = t["TP"] + t["SL"] + t["TIMEOUT"]
        wr = t["TP"] / n if n else 0
        print(f"  {k:18s} TP={t['TP']:3d} SL={t['SL']:3d} TO={t['TIMEOUT']:3d} "
              f"FAIL={t['FAILED']:3d}  WR={wr*100:.0f}%")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
