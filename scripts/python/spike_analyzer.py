"""Spike frequency analyzer — input for Amit v3 threshold calibration.

Reads 7 days of orderbook_snapshots for BTC 5-min markets, groups by
5-min period, samples mid prices, counts how many "spikes" each
(threshold, window) combination produces.

Output guides choice of spike_threshold_pct + spike_window_sec.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

DB_PATH = sys.argv[1] if len(sys.argv) > 1 else "/srv/poly1/data/trade_log.db"
LOOKBACK_DAYS = 7

THRESHOLDS = [0.02, 0.03, 0.05, 0.08, 0.10]  # 2%..10%
WINDOWS = [10, 20, 30, 60]                   # seconds


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def period_floor(dt: datetime) -> int:
    return int(dt.timestamp() // 300) * 300


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()

    # Pull BTC-related snapshots: limit to markets whose token_id appears
    # in btc5min_timed_* cycles, OR just fetch enough samples by limiting
    # ts range. We sample mid prices grouped by period_ts.
    print(f"Loading snapshots from {cutoff[:19]} onwards...")
    rows = conn.execute(
        """
        SELECT ts, token_id, mid
        FROM orderbook_snapshots
        WHERE ts > ?
          AND mid IS NOT NULL
          AND mid > 0
          AND mid < 1
        ORDER BY token_id, ts
        """,
        (cutoff,),
    ).fetchall()
    print(f"Loaded {len(rows):,} snapshots.")

    # Group by (period_ts, token_id) → list of (offset_sec, mid)
    series: dict[tuple[int, str], list[tuple[float, float]]] = defaultdict(list)
    for r in rows:
        dt = parse_ts(r["ts"])
        period = period_floor(dt)
        offset = (dt - datetime.fromtimestamp(period, timezone.utc)).total_seconds()
        if 0 <= offset <= 270:
            series[(period, r["token_id"])].append((offset, float(r["mid"])))

    print(f"Unique (period × token): {len(series):,}")
    # Filter to series with at least 5 samples in cycle
    valid = {k: v for k, v in series.items() if len(v) >= 5}
    print(f"With >= 5 samples: {len(valid):,}")
    print()

    # For each (threshold, window) — count spikes per cycle and direction
    print("=== Spike frequency per 5-min cycle (avg across all markets) ===")
    print(f"{'thr%':>5s} {'win-s':>6s} {'spikes/cycle':>14s} "
          f"{'cycles>=1':>10s} {'up%':>6s} {'down%':>6s}")
    print("-" * 60)

    for thr in THRESHOLDS:
        for win in WINDOWS:
            total_spikes_up = 0
            total_spikes_down = 0
            cycles_with_spike = 0
            for samples in valid.values():
                # Sort just in case
                samples = sorted(samples)
                spike_up = 0
                spike_down = 0
                last_fire_offset = -999
                for i, (off, mid) in enumerate(samples):
                    # find earliest sample within [off-win, off]
                    earlier = [s for s in samples[:i] if s[0] >= off - win]
                    if not earlier:
                        continue
                    base = earlier[0][1]
                    if base <= 0:
                        continue
                    change = (mid - base) / base
                    # Cooldown so we don't double-count same spike
                    if off - last_fire_offset < win:
                        continue
                    if change >= thr:
                        spike_up += 1
                        last_fire_offset = off
                    elif change <= -thr:
                        spike_down += 1
                        last_fire_offset = off
                if spike_up + spike_down > 0:
                    cycles_with_spike += 1
                    total_spikes_up += spike_up
                    total_spikes_down += spike_down
            total = total_spikes_up + total_spikes_down
            n_cycles = len(valid)
            avg = total / n_cycles if n_cycles else 0
            pct_cycles = 100 * cycles_with_spike / n_cycles if n_cycles else 0
            up_pct = 100 * total_spikes_up / total if total else 0
            down_pct = 100 * total_spikes_down / total if total else 0
            print(f"{thr*100:>4.0f}% {win:>6d} {avg:>13.2f}  {pct_cycles:>9.0f}% "
                  f"{up_pct:>5.0f}% {down_pct:>5.0f}%")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
