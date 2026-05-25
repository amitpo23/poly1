#!/usr/bin/env python3
"""Backtest the time-based DOWN/UP no-SL strategy on BTC 5-min markets.

Strategy proposed 2026-05-25 (operator):
  Phase 1 (t=0:01 → t=3:00):  BUY DOWN @ ~$0.50, TP at +15%, no SL
  Phase 2 (t=3:00 → t=4:50):  BUY UP   @ ~$0.50, TP at +10%, no SL
  If TP not hit by t=4:55 → market resolves binary → 100% win or 100% loss.

No SL means an adverse position can fall to $0 by resolution.

Data: Coinbase BTC-USD 1-minute candles (free, public, ~300 days history).

Polymarket price model:
  For a 5-min binary "DOWN" market, the implied probability that BTC
  ends lower than start is approximately the probability that current
  trajectory continues. We use a simple empirical mapping:

    DOWN_price(t) ≈ 0.50 - SENSITIVITY * (BTC(t) - BTC(0)) / BTC(0) / sqrt((5-t)/5)

  Where SENSITIVITY ≈ 30 (calibrated from observed Polymarket sensitivity:
  a 0.05% BTC move at t=2:30 moves DOWN by ~$0.075).

  TP hit if DOWN_price reaches start_price * 1.15 (15% gain).
  Resolution: DOWN wins if BTC(5:00) < BTC(0:00).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional


COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
SENSITIVITY = 30.0  # how much $0.01 of Polymarket price per 0.01% BTC move
PHASE1_TP = 0.15  # +15% from entry
PHASE2_TP = 0.10  # +10% from entry
PHASE1_ENTRY_SEC = 1   # 1 second after period start
PHASE2_ENTRY_SEC = 180  # 3:00 into the period
PHASE2_DEADLINE_SEC = 290  # 4:50 (last chance to enter, exits by resolution)


def fetch_btc_1min_candles(start_sec: int, end_sec: int) -> list[tuple[int, float]]:
    """Fetch BTC-USD 1-min close prices via Coinbase public API.
    Returns [(ts_sec, close), ...] sorted ascending."""
    out = []
    # Coinbase limits to 300 candles per request → 300 minutes = 5 hours per call.
    chunk = 300 * 60
    cur = start_sec
    while cur < end_sec:
        chunk_end = min(cur + chunk, end_sec)
        params = urllib.parse.urlencode({
            "start": datetime.fromtimestamp(cur, tz=timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(chunk_end, tz=timezone.utc).isoformat(),
            "granularity": 60,
        })
        url = f"{COINBASE_CANDLES}?{params}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "poly1-bt/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                rows = json.loads(resp.read())
            # Coinbase format: [time, low, high, open, close, volume] DESC
            for r in rows:
                if len(r) >= 5:
                    out.append((int(r[0]), float(r[4])))
        except Exception as exc:
            print(f"WARN: chunk fetch failed {cur}: {exc}", file=sys.stderr)
        cur = chunk_end
        time.sleep(0.35)  # Coinbase rate limit
    out.sort()
    return out


def implied_down_price(btc_change_pct: float, time_remaining_sec: float) -> float:
    """Estimate Polymarket DOWN price given BTC change & time left."""
    # As time runs out, sensitivity to BTC change AMPLIFIES (less time
    # for reversal, more certainty about resolution).
    time_factor = math.sqrt(max(5.0, time_remaining_sec / 60.0) / 5.0)
    delta = -SENSITIVITY * btc_change_pct / time_factor
    return max(0.01, min(0.99, 0.50 + delta))


def simulate_window(candles_window: list[tuple[int, float]]) -> dict:
    """Simulate strategy on one 5-min window. candles_window has 5 candles
    (one per minute, t=0..4)."""
    if len(candles_window) < 5:
        return {"valid": False}

    # Per-minute closes for the 5-min window
    p0 = candles_window[0][1]  # price at t=0
    p3 = candles_window[3][1]  # at t=3:00
    p_end = candles_window[4][1]  # at t=4:00 (closest we have to 5:00)

    # Resolution: DOWN wins if BTC ends lower than start
    down_wins = p_end < p0
    up_wins = p_end > p0

    # ----- Phase 1: BUY DOWN at t=0:01, TP at +15% -----
    # We can only observe per-minute closes, so we approximate the
    # intra-window minimum BTC price as min of the 5 closes.
    # DOWN_price hits +15% when implied_down_price reaches 0.575.
    # That means BTC needs to be down enough at some time t∈[0:01, 3:00].
    p1_entry = 0.50
    p1_target = 0.575
    p1_hit_tp = False
    # check at each minute from t=1 to t=3 (closest observation points)
    for i in range(1, 4):  # minutes 1, 2, 3
        if i >= len(candles_window):
            break
        btc_t = candles_window[i][1]
        time_remaining = (5 - i) * 60
        btc_change = (btc_t - p0) / p0
        down_p = implied_down_price(btc_change, time_remaining)
        if down_p >= p1_target:
            p1_hit_tp = True
            break

    if p1_hit_tp:
        p1_pnl = (p1_target - p1_entry) / p1_entry  # +15%
        p1_outcome = "tp_hit"
    elif down_wins:
        # Market resolved DOWN — we collect $1.00 from $0.50 entry = +100%
        p1_pnl = (1.0 - p1_entry) / p1_entry
        p1_outcome = "resolved_win"
    else:
        # Market resolved UP — DOWN side worth $0 → -100%
        p1_pnl = -1.0
        p1_outcome = "resolved_loss"

    # ----- Phase 2: BUY UP at t=3:00, TP at +10% -----
    p2_entry = 0.50
    p2_target = 0.55
    p2_hit_tp = False
    p3_price = candles_window[3][1] if len(candles_window) > 3 else p0
    for i in [4]:  # only t=4 available (approximation of 4:50)
        if i >= len(candles_window):
            break
        btc_t = candles_window[i][1]
        time_remaining = (5 - i) * 60
        # UP price moves opposite of DOWN
        btc_change_from_3 = (btc_t - p3_price) / p3_price
        # implied_up_price = 0.50 - implied_DOWN_excursion
        up_p = 1.0 - implied_down_price(btc_change_from_3, time_remaining)
        if up_p >= p2_target:
            p2_hit_tp = True
            break

    if p2_hit_tp:
        p2_pnl = (p2_target - p2_entry) / p2_entry  # +10%
        p2_outcome = "tp_hit"
    elif up_wins:
        p2_pnl = (1.0 - p2_entry) / p2_entry
        p2_outcome = "resolved_win"
    else:
        p2_pnl = -1.0
        p2_outcome = "resolved_loss"

    return {
        "valid": True,
        "t0_btc": p0,
        "t5_btc": p_end,
        "btc_change_pct": (p_end - p0) / p0 * 100,
        "phase1_outcome": p1_outcome,
        "phase1_pnl_pct": p1_pnl * 100,
        "phase2_outcome": p2_outcome,
        "phase2_pnl_pct": p2_pnl * 100,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7,
                        help="History window in days (default 7)")
    parser.add_argument("--position-usdc", type=float, default=1.0,
                        help="Position size per leg per window (default $1)")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args()

    now = int(time.time())
    start_sec = now - args.days * 86400
    # Align to 5-min boundary
    start_sec = (start_sec // 300) * 300
    # Stop ~10 min in past to make sure all candles are settled
    end_sec = ((now - 600) // 300) * 300

    print(f"Fetching BTC 1-min candles {datetime.fromtimestamp(start_sec, tz=timezone.utc).isoformat()} → "
          f"{datetime.fromtimestamp(end_sec, tz=timezone.utc).isoformat()}", file=sys.stderr)
    candles = fetch_btc_1min_candles(start_sec, end_sec)
    print(f"Got {len(candles)} candles", file=sys.stderr)
    if not candles:
        print("ERROR: no candles fetched", file=sys.stderr)
        return 2

    # Index by minute timestamp
    candle_by_ts = dict(candles)

    # Iterate 5-min windows
    results = []
    cur = start_sec
    while cur + 300 <= end_sec:
        window_candles = []
        for i in range(5):
            ts = cur + i * 60
            if ts in candle_by_ts:
                window_candles.append((ts, candle_by_ts[ts]))
        if len(window_candles) == 5:
            r = simulate_window(window_candles)
            r["window_start"] = datetime.fromtimestamp(cur, tz=timezone.utc).isoformat()
            results.append(r)
        cur += 300

    valid = [r for r in results if r["valid"]]
    if not valid:
        print("ERROR: no valid windows", file=sys.stderr)
        return 2

    # Aggregate
    p1_pnls = [r["phase1_pnl_pct"] / 100 * args.position_usdc for r in valid]
    p2_pnls = [r["phase2_pnl_pct"] / 100 * args.position_usdc for r in valid]
    p1_tps = sum(1 for r in valid if r["phase1_outcome"] == "tp_hit")
    p1_wins = sum(1 for r in valid if r["phase1_outcome"] == "resolved_win")
    p1_losses = sum(1 for r in valid if r["phase1_outcome"] == "resolved_loss")
    p2_tps = sum(1 for r in valid if r["phase2_outcome"] == "tp_hit")
    p2_wins = sum(1 for r in valid if r["phase2_outcome"] == "resolved_win")
    p2_losses = sum(1 for r in valid if r["phase2_outcome"] == "resolved_loss")

    summary = {
        "days": args.days,
        "windows": len(valid),
        "position_usdc": args.position_usdc,
        "phase1": {
            "tp_hit_rate": p1_tps / len(valid),
            "resolved_win_rate": p1_wins / len(valid),
            "resolved_loss_rate": p1_losses / len(valid),
            "total_pnl_usdc": round(sum(p1_pnls), 2),
            "avg_pnl_usdc": round(sum(p1_pnls) / len(valid), 4),
            "sharpe_proxy": (
                round(sum(p1_pnls) / len(valid) / (
                    (sum((p - sum(p1_pnls)/len(valid))**2 for p in p1_pnls) / len(valid)) ** 0.5 or 1e-9
                ), 3) if len(p1_pnls) > 1 else 0
            ),
        },
        "phase2": {
            "tp_hit_rate": p2_tps / len(valid),
            "resolved_win_rate": p2_wins / len(valid),
            "resolved_loss_rate": p2_losses / len(valid),
            "total_pnl_usdc": round(sum(p2_pnls), 2),
            "avg_pnl_usdc": round(sum(p2_pnls) / len(valid), 4),
        },
        "combined": {
            "total_pnl_usdc": round(sum(p1_pnls) + sum(p2_pnls), 2),
            "avg_pnl_per_window_usdc": round((sum(p1_pnls) + sum(p2_pnls)) / len(valid), 4),
        },
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print()
        print(f"=== Backtest: {args.days}d, {len(valid)} 5-min windows ===")
        print(f"Position per leg: ${args.position_usdc:.2f}")
        print()
        print("Phase 1 (DOWN @ t=0:01, TP+15%):")
        print(f"  TP hit rate:    {summary['phase1']['tp_hit_rate']:.1%}")
        print(f"  Resolved win:   {summary['phase1']['resolved_win_rate']:.1%}")
        print(f"  Resolved loss:  {summary['phase1']['resolved_loss_rate']:.1%}")
        print(f"  Total PnL:      ${summary['phase1']['total_pnl_usdc']:+.2f}")
        print(f"  Avg per trade:  ${summary['phase1']['avg_pnl_usdc']:+.4f}")
        print()
        print("Phase 2 (UP @ t=3:00, TP+10%):")
        print(f"  TP hit rate:    {summary['phase2']['tp_hit_rate']:.1%}")
        print(f"  Resolved win:   {summary['phase2']['resolved_win_rate']:.1%}")
        print(f"  Resolved loss:  {summary['phase2']['resolved_loss_rate']:.1%}")
        print(f"  Total PnL:      ${summary['phase2']['total_pnl_usdc']:+.2f}")
        print(f"  Avg per trade:  ${summary['phase2']['avg_pnl_usdc']:+.4f}")
        print()
        print(f"COMBINED: ${summary['combined']['total_pnl_usdc']:+.2f} over {len(valid)} windows")
        print(f"          ${summary['combined']['avg_pnl_per_window_usdc']:+.4f}/window (both legs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
