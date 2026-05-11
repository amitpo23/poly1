#!/usr/bin/env python3
"""scalper backtest harness — v2.

Replays historical 15-min crypto markets through `MarketBrain.evaluate_scalper_entry`
to measure paper PnL of the reversal/depth scoring formula.

Scope (per advisor):
  - scalper ONLY (no AI / news / market-maker — those need other inputs)
  - Reuse local `scalper_pairs` table (1000+ expired pairs over 2 days)
  - Window: 24h / 48h (limited by what's in the local DB)
  - read-only

Per-pair simulation:
  1. Pull price-history for both up_token and down_token over the 15-min
     active window [period_ts - 900, period_ts].
  2. For each tick: compute (up_ask, down_ask) and call
     `MarketBrain.evaluate_scalper_entry`.
  3. If approved + not in cooldown: record paper entry at candidate_price.
  4. Simulate exit at next tick where TP/SL/expiry trigger OR at period_ts
     (paid $1 if winning side won, $0 if losing).

Usage:
    docker exec poly1-position-manager python \\
      /app/scripts/python/backtest_scalper.py --hours 48 --json
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.application.market_brain import (  # noqa: E402
    BrainConfig,
    MarketBrain,
)

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backtest_scalper")


@dataclass
class _Tick:
    ts: int
    up_price: float
    down_price: float


@dataclass
class _PairResult:
    slug: str
    period_ts: int
    up_resolved_yes: Optional[bool]  # did UP win?
    ticks: int
    entries: int
    paper_pnl_usdc: float = 0.0
    trades: list[dict] = field(default_factory=list)


def _fetch_history(polymarket, token_id: str, start_ts: int, end_ts: int) -> dict[int, float]:
    """Pull price-history; return {ts → price} dict."""
    from py_clob_client_v2.clob_types import PricesHistoryParams
    try:
        resp = polymarket.client.get_prices_history(
            params=PricesHistoryParams(
                market=token_id,
                start_ts=start_ts,
                end_ts=end_ts,
                fidelity=1,
            )
        )
    except Exception as exc:
        logger.debug("history fetch failed for %s: %s", token_id[:18], exc)
        return {}
    raw = resp.get("history", []) if isinstance(resp, dict) else []
    return {int(r["t"]): float(r["p"]) for r in raw}


def _resolve_outcome(polymarket, up_token: str) -> Optional[bool]:
    """Did UP side win? Query CTF balance — 0 means resolved against
    that token. We can't directly tell winner from one side; check
    Gamma instead."""
    # Simpler: at terminal time, if up_price >= 0.99 → UP won; if up_price <= 0.01 → DOWN won
    # We can read the last sample's price.
    return None  # determined per-tick from terminal price


def _simulate_pair(
    polymarket,
    brain: MarketBrain,
    slug: str,
    period_ts: int,
    up_token: str,
    down_token: str,
    position_size: float,
    tp_pct: float,
    sl_pct: float,
    cooldown_sec: int = 30,
    sell_slippage: float = 0.02,
) -> Optional[_PairResult]:
    """Replay one 15-min market through the brain decision logic."""
    start_ts = period_ts - 900  # 15 min before resolution
    end_ts = period_ts + 60     # small buffer past resolution

    up_hist = _fetch_history(polymarket, up_token, start_ts, end_ts)
    down_hist = _fetch_history(polymarket, down_token, start_ts, end_ts)
    if not up_hist or not down_hist:
        return None

    # Merge timestamps from both sides (use union; missing values forward-fill)
    all_ts = sorted(set(up_hist.keys()) | set(down_hist.keys()))
    if len(all_ts) < 5:
        return None

    ticks: list[_Tick] = []
    last_up = None
    last_down = None
    for t in all_ts:
        if t in up_hist:
            last_up = up_hist[t]
        if t in down_hist:
            last_down = down_hist[t]
        if last_up is not None and last_down is not None:
            ticks.append(_Tick(ts=t, up_price=last_up, down_price=last_down))

    if len(ticks) < 5:
        return None

    # Determine winner from terminal sample (≥0.99 means that side won)
    terminal = ticks[-1]
    up_resolved_yes = None
    if terminal.up_price >= 0.95:
        up_resolved_yes = True
    elif terminal.up_price <= 0.05:
        up_resolved_yes = False

    entries: list[dict] = []
    open_pos: Optional[dict] = None
    last_entry_ts = 0

    for i, tick in enumerate(ticks):
        # Skip ticks too close to expiry — brain rejects these anyway
        secs_to_expiry = period_ts - tick.ts
        if secs_to_expiry < 30:
            break

        # Exit phase: simulate exit on open position
        if open_pos is not None:
            held_yes = open_pos["side"] == "up"
            entry_price = open_pos["entry_price"]
            current_held_price = tick.up_price if held_yes else tick.down_price
            diff_pct = (current_held_price - entry_price) / max(entry_price, 0.001)

            exit_reason = None
            if diff_pct >= tp_pct:
                exit_reason = "take_profit"
            elif diff_pct <= -sl_pct:
                exit_reason = "stop_loss"
            elif secs_to_expiry < 90:  # scalper exits before expiry per config
                exit_reason = "near_expiry"

            if exit_reason:
                # Apply FAK SELL slippage to match live exit_executor:
                # `limit_price = mid * (1 - sell_slippage)`. Without this,
                # backtest overestimates edge by ~2 percentage points
                # per round-trip on small-margin scalper trades.
                effective_exit_price = max(0.01, current_held_price * (1.0 - sell_slippage))
                shares = position_size / max(entry_price, 0.001)
                proceeds = shares * effective_exit_price
                pnl = proceeds - position_size
                entries[-1].update({
                    "exit_ts": tick.ts,
                    "exit_price": current_held_price,
                    "effective_exit_price": effective_exit_price,
                    "exit_reason": exit_reason,
                    "paper_pnl_usdc": round(pnl, 4),
                })
                open_pos = None

        # Entry phase: ask the brain
        if open_pos is None and (tick.ts - last_entry_ts) >= cooldown_sec:
            up_ask = tick.up_price
            down_ask = tick.down_price
            # Try BUY_UP if up_ask is cheaper than 0.50
            for side, candidate, signal in [
                ("up", up_ask, "cheap" if up_ask < 0.40 else "reversal"),
                ("down", down_ask, "cheap" if down_ask < 0.40 else "reversal"),
            ]:
                decision = brain.evaluate_scalper_entry(
                    slug=slug,
                    side=side,
                    up_ask=up_ask,
                    down_ask=down_ask,
                    candidate_price=candidate,
                    signal_reason=signal,
                    now_ms=tick.ts * 1000,
                    period_ts=period_ts,
                )
                if decision.approved:
                    entries.append({
                        "entry_ts": tick.ts,
                        "side": side,
                        "entry_price": candidate,
                        "score": decision.score,
                    })
                    open_pos = {
                        "side": side,
                        "entry_price": candidate,
                        "entry_ts": tick.ts,
                    }
                    last_entry_ts = tick.ts
                    break

    # Settle any still-open position at terminal
    if open_pos is not None and up_resolved_yes is not None:
        held_yes = open_pos["side"] == "up"
        won = (held_yes and up_resolved_yes) or (not held_yes and not up_resolved_yes)
        terminal_price = 1.0 if won else 0.0
        shares = position_size / max(open_pos["entry_price"], 0.001)
        proceeds = shares * terminal_price
        pnl = proceeds - position_size
        entries[-1].update({
            "exit_ts": terminal.ts,
            "exit_price": terminal_price,
            "exit_reason": "settled_yes" if won else "settled_loss",
            "paper_pnl_usdc": round(pnl, 4),
        })

    paper_pnl = sum(e.get("paper_pnl_usdc", 0.0) for e in entries)
    return _PairResult(
        slug=slug,
        period_ts=period_ts,
        up_resolved_yes=up_resolved_yes,
        ticks=len(ticks),
        entries=len(entries),
        paper_pnl_usdc=round(paper_pnl, 4),
        trades=entries,
    )


def _summarize(results: list[_PairResult], label: str) -> dict:
    if not results:
        return {"label": label, "pairs": 0, "entries": 0,
                "paper_pnl_usdc": 0.0, "win_rate": None}
    entries = sum(r.entries for r in results)
    pnl = sum(r.paper_pnl_usdc for r in results)
    wins = sum(1 for r in results for t in r.trades if t.get("paper_pnl_usdc", 0) > 0)
    losses = sum(1 for r in results for t in r.trades if t.get("paper_pnl_usdc", 0) < 0)
    win_rate = (wins / (wins + losses)) if (wins + losses) else None
    return {
        "label": label,
        "pairs": len(results),
        "entries": entries,
        "wins": wins,
        "losses": losses,
        "paper_pnl_usdc": round(pnl, 4),
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=48,
                        help="Replay pairs from last N hours (default 48)")
    parser.add_argument("--max-pairs", type=int, default=200,
                        help="Cap total pairs to run (default 200; spreads load)")
    parser.add_argument("--position-size", type=float, default=2.5,
                        help="Per-trade USDC (matches SCALP_LEG_USDC)")
    parser.add_argument("--tp-pct", type=float, default=0.10,
                        help="Take profit threshold (default 0.10)")
    parser.add_argument("--sl-pct", type=float, default=0.07,
                        help="Stop loss threshold (default 0.07 = SCALP_EXIT_STOP_LOSS_PCT)")
    parser.add_argument("--slippage", type=float, default=0.02,
                        help="FAK sell slippage applied to exit price (default 0.02 = 2%)")
    parser.add_argument("--min-edge-score", type=float, default=None,
                        help="Override brain min_edge_score (default: from env=0.35)")
    parser.add_argument("--db-path", default="/app/data/trade_log.db")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    from agents.polymarket.polymarket import Polymarket
    pm = Polymarket(live=True)

    # Build brain — match production config but allow override of min_edge_score
    brain_cfg = BrainConfig.from_env()
    if args.min_edge_score is not None:
        # BrainConfig is frozen; reconstruct with override.
        from dataclasses import replace as _replace
        brain_cfg = _replace(brain_cfg, scalper_min_edge_score=args.min_edge_score)
    brain = MarketBrain(cfg=brain_cfg)

    # Load expired pairs from local DB
    cutoff_ts = int(time.time()) - args.hours * 3600
    con = sqlite3.connect(args.db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT slug, period_ts, up_token, down_token
        FROM scalper_pairs
        WHERE state = 'expired' AND period_ts >= ?
        ORDER BY period_ts DESC
        LIMIT ?
        """,
        (cutoff_ts, args.max_pairs),
    ).fetchall()
    con.close()

    logger.info("loaded %d expired pairs from last %dh", len(rows), args.hours)

    results: list[_PairResult] = []
    for i, row in enumerate(rows):
        if args.verbose and i % 20 == 0:
            logger.info("progress: %d/%d", i, len(rows))
        r = _simulate_pair(
            pm, brain,
            slug=row["slug"],
            period_ts=row["period_ts"],
            up_token=row["up_token"],
            down_token=row["down_token"],
            position_size=args.position_size,
            tp_pct=args.tp_pct,
            sl_pct=args.sl_pct,
            sell_slippage=args.slippage,
        )
        if r:
            results.append(r)

    # Window summaries by hours buckets
    now_ts = int(time.time())
    last_24h = [r for r in results if r.period_ts >= now_ts - 86400]
    last_48h = [r for r in results if r.period_ts >= now_ts - 172800]
    summaries = [
        _summarize(last_24h, "24h"),
        _summarize(last_48h, "48h"),
    ]

    cfg_dict = {
        "min_edge_score": brain_cfg.scalper_min_edge_score,
        "max_pair_ask_sum": brain_cfg.scalper_max_pair_ask_sum,
        "max_entry_price": brain_cfg.scalper_max_entry_price,
        "min_seconds_to_expiry": brain_cfg.scalper_min_seconds_to_expiry,
        "tp_pct": args.tp_pct,
        "sl_pct": args.sl_pct,
        "position_size": args.position_size,
    }

    if args.json:
        print(json.dumps({
            "config": cfg_dict,
            "windows": summaries,
            "pairs_total": len(results),
        }, indent=2))
    else:
        print("# scalper backtest harness — v2")
        print(f"config: {json.dumps(cfg_dict)}")
        print()
        print(f"## {len(results)} pairs simulated (out of {len(rows)} loaded)")
        print()
        print("## Window summaries")
        for s in summaries:
            wr = f"{s['win_rate']*100:.1f}%" if s.get("win_rate") is not None else "n/a"
            print(
                f"  {s['label']:>4s}: pairs={s['pairs']:3d} entries={s['entries']:3d} "
                f"wins={s.get('wins', 0)}/losses={s.get('losses', 0)} "
                f"win_rate={wr}  paper_pnl=${s['paper_pnl_usdc']:+.4f}"
            )
        print()
        # Verdict
        pnls = [s["paper_pnl_usdc"] for s in summaries if s["entries"] > 0]
        if pnls and all(p > 0 for p in pnls):
            print("VERDICT: positive paper PnL across windows — strategy worth keeping live.")
        elif pnls and all(p < 0 for p in pnls):
            print("VERDICT: negative paper PnL across windows — strategy not worth scaling.")
        else:
            print("VERDICT: inconsistent across windows or insufficient entries — no edge demonstrated.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
