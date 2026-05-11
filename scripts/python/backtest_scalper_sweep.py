#!/usr/bin/env python3
"""scalper sweep — try every variant we can replay.

Question: is there ANY scalper-style strategy on Polymarket 15-min
crypto markets that achieves ≥65% win rate after realistic execution
costs? Or is the platform itself unsuited?

Tests 4 strategy families on the same historical data, ranks results.
Each family explores its threshold space independently. Slippage is
modeled per family (TP/SL exits pay 2% on FAK SELL; hold-to-expiry
incurs no slippage — CTF redemption is exact $1/$0).

Strategy families:

  A. EDGE_SCORE_TPSL — current production scalper. Thresholds 0.20/
     0.30/0.40 on edge_score; exit at TP=10% / SL=7% / near-expiry.

  B. CHEAP_TPSL — enter any side priced ≤ X. Same TP/SL exit. Tests
     whether the brain's edge_score adds anything over a flat price
     filter.

  C. CHEAP_HOLD_TO_EXPIRY — enter any side priced ≤ X. NO mid-period
     exit. Settle at $1 if won, $0 if lost. Avoids the 2% slippage
     drag entirely (CTF pays exact). The math: 30¢ entry needs 30%
     WR to break even.

  D. MOMENTUM_TPSL — BUY the side that just rose (opposite of fade).
     Enter when held side rose ≥X% in last 60s. Exit at TP=10% / SL=7%.

Each strategy outputs entries / wins / losses / win_rate / paper_pnl
on the same 200-pair sample. Result table is ranked by WR; rows above
65% are highlighted. Negative VERDICT means no row passes the gate.

Usage:
    docker exec poly1-position-manager python \\
      /app/scripts/python/backtest_scalper_sweep.py --max-pairs 200
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("scalper_sweep")


# ---------------------------------------------------------------------
# Replay primitives
# ---------------------------------------------------------------------


@dataclass
class _Tick:
    ts: int
    up_price: float
    down_price: float


@dataclass
class _Pair:
    slug: str
    period_ts: int
    ticks: list[_Tick]
    up_resolved_yes: Optional[bool]


def _fetch_history(polymarket, token_id: str, start_ts: int, end_ts: int) -> dict[int, float]:
    from py_clob_client_v2.clob_types import PricesHistoryParams
    try:
        resp = polymarket.client.get_prices_history(
            params=PricesHistoryParams(market=token_id, start_ts=start_ts, end_ts=end_ts, fidelity=1)
        )
    except Exception as exc:
        logger.debug("history fetch failed for %s: %s", token_id[:18], exc)
        return {}
    raw = resp.get("history", []) if isinstance(resp, dict) else []
    return {int(r["t"]): float(r["p"]) for r in raw}


def _build_pair(polymarket, slug: str, period_ts: int, up_token: str, down_token: str) -> Optional[_Pair]:
    start_ts = period_ts - 900
    end_ts = period_ts + 60
    up_hist = _fetch_history(polymarket, up_token, start_ts, end_ts)
    down_hist = _fetch_history(polymarket, down_token, start_ts, end_ts)
    if not up_hist or not down_hist:
        return None
    all_ts = sorted(set(up_hist.keys()) | set(down_hist.keys()))
    if len(all_ts) < 5:
        return None
    ticks: list[_Tick] = []
    last_up = last_down = None
    for t in all_ts:
        if t in up_hist:
            last_up = up_hist[t]
        if t in down_hist:
            last_down = down_hist[t]
        if last_up is not None and last_down is not None:
            ticks.append(_Tick(ts=t, up_price=last_up, down_price=last_down))
    if len(ticks) < 5:
        return None
    terminal = ticks[-1]
    up_resolved_yes = None
    if terminal.up_price >= 0.95:
        up_resolved_yes = True
    elif terminal.up_price <= 0.05:
        up_resolved_yes = False
    return _Pair(slug=slug, period_ts=period_ts, ticks=ticks, up_resolved_yes=up_resolved_yes)


# ---------------------------------------------------------------------
# Strategy interface
# ---------------------------------------------------------------------


@dataclass
class _Strategy:
    name: str
    threshold: float
    """Returns (side, entry_price) if should enter this tick, else None.
    side ∈ {'up','down'}."""
    entry_rule: Callable[[_Pair, int, dict], Optional[tuple[str, float]]]
    """If True, exit at TP/SL/near-expiry with 2% slippage. If False,
    hold to settlement (no mid-period exit, no slippage)."""
    use_tpsl_exit: bool = True
    tp_pct: float = 0.10
    sl_pct: float = 0.07


def _entry_edge_score(threshold: float):
    """Family A: production scalper edge_score logic."""
    from agents.application.market_brain import BrainConfig, MarketBrain
    from dataclasses import replace as _replace
    cfg = BrainConfig.from_env()
    cfg = _replace(cfg, scalper_min_edge_score=threshold)
    brain = MarketBrain(cfg=cfg)

    def rule(pair: _Pair, i: int, state: dict) -> Optional[tuple[str, float]]:
        tick = pair.ticks[i]
        for side, candidate in [("up", tick.up_price), ("down", tick.down_price)]:
            decision = brain.evaluate_scalper_entry(
                slug=pair.slug,
                side=side,
                up_ask=tick.up_price,
                down_ask=tick.down_price,
                candidate_price=candidate,
                signal_reason="cheap" if candidate < 0.40 else "reversal",
                now_ms=tick.ts * 1000,
                period_ts=pair.period_ts,
            )
            if decision.approved:
                return (side, candidate)
        return None

    return rule


def _entry_cheap(threshold: float):
    """Families B & C: enter any side priced ≤ threshold (cheaper side wins ties)."""
    def rule(pair: _Pair, i: int, state: dict) -> Optional[tuple[str, float]]:
        tick = pair.ticks[i]
        # Pair-sum sanity: skip tightly-priced books (typical 1.04 max)
        if tick.up_price + tick.down_price > 1.06:
            return None
        cheaper_side, cheaper_price = (
            ("up", tick.up_price) if tick.up_price <= tick.down_price else ("down", tick.down_price)
        )
        if cheaper_price > threshold:
            return None
        return (cheaper_side, cheaper_price)
    return rule


def _entry_momentum(threshold: float, window_sec: int = 60):
    """Family D: BUY the side that just rose (chase, not fade)."""
    def rule(pair: _Pair, i: int, state: dict) -> Optional[tuple[str, float]]:
        if i < 2:
            return None
        tick = pair.ticks[i]
        # Find the tick that's >= window_sec ago
        target_ts = tick.ts - window_sec
        oldest = None
        for j in range(i, -1, -1):
            if pair.ticks[j].ts <= target_ts:
                oldest = pair.ticks[j]
                break
        if oldest is None:
            return None
        up_change = (tick.up_price - oldest.up_price) / max(oldest.up_price, 0.001)
        down_change = (tick.down_price - oldest.down_price) / max(oldest.down_price, 0.001)
        # Whichever rose more — buy that side (chase)
        if up_change >= threshold and up_change >= down_change:
            return ("up", tick.up_price)
        if down_change >= threshold and down_change > up_change:
            return ("down", tick.down_price)
        return None
    return rule


# ---------------------------------------------------------------------
# Per-pair simulator (strategy-agnostic)
# ---------------------------------------------------------------------


@dataclass
class _PairResult:
    entries: int = 0
    wins: int = 0
    losses: int = 0
    paper_pnl_usdc: float = 0.0


def _simulate(
    pair: _Pair,
    strategy: _Strategy,
    *,
    position_size: float = 2.5,
    cooldown_sec: int = 30,
    sell_slippage: float = 0.02,
) -> _PairResult:
    """Walk pair ticks, applying strategy's entry/exit rules.

    For TP/SL exits: apply 2% sell slippage (matches exit_executor live).
    For hold-to-expiry: settle at terminal price ($1/$0 from CTF, no slippage).
    """
    state: dict = {}
    open_pos: Optional[dict] = None
    entries: list[dict] = []
    last_entry_ts = 0
    res = _PairResult()

    for i, tick in enumerate(pair.ticks):
        secs_to_expiry = pair.period_ts - tick.ts
        if secs_to_expiry < 30 and not strategy.use_tpsl_exit:
            # near-expiry: stop checking new entries; settlement happens after loop
            pass
        elif secs_to_expiry < 30:
            break

        # Exit phase (TP/SL only — hold-to-expiry skips this)
        if open_pos is not None and strategy.use_tpsl_exit:
            held_yes = open_pos["side"] == "up"
            entry_price = open_pos["entry_price"]
            current_held_price = tick.up_price if held_yes else tick.down_price
            diff_pct = (current_held_price - entry_price) / max(entry_price, 0.001)
            exit_reason = None
            if diff_pct >= strategy.tp_pct:
                exit_reason = "tp"
            elif diff_pct <= -strategy.sl_pct:
                exit_reason = "sl"
            elif secs_to_expiry < 90:
                exit_reason = "near_expiry"
            if exit_reason:
                effective_exit = max(0.01, current_held_price * (1.0 - sell_slippage))
                shares = position_size / max(entry_price, 0.001)
                pnl = shares * effective_exit - position_size
                entries[-1]["paper_pnl_usdc"] = pnl
                if pnl > 0:
                    res.wins += 1
                elif pnl < 0:
                    res.losses += 1
                res.paper_pnl_usdc += pnl
                open_pos = None

        # Entry phase
        if open_pos is None and (tick.ts - last_entry_ts) >= cooldown_sec:
            choice = strategy.entry_rule(pair, i, state)
            if choice is not None:
                side, entry_price = choice
                open_pos = {"side": side, "entry_price": entry_price, "entry_ts": tick.ts}
                last_entry_ts = tick.ts
                entries.append({"side": side, "entry_price": entry_price})
                res.entries += 1

    # Settle any still-open position at terminal price
    if open_pos is not None and pair.up_resolved_yes is not None:
        held_yes = open_pos["side"] == "up"
        won = (held_yes and pair.up_resolved_yes) or (not held_yes and not pair.up_resolved_yes)
        terminal_price = 1.0 if won else 0.0
        shares = position_size / max(open_pos["entry_price"], 0.001)
        pnl = shares * terminal_price - position_size
        entries[-1]["paper_pnl_usdc"] = pnl
        if pnl > 0:
            res.wins += 1
        elif pnl < 0:
            res.losses += 1
        res.paper_pnl_usdc += pnl

    return res


# ---------------------------------------------------------------------
# Sweep runner
# ---------------------------------------------------------------------


def _build_strategies() -> list[_Strategy]:
    out: list[_Strategy] = []

    # Family A: edge_score (current production)
    for thresh in [0.20, 0.30, 0.40, 0.50]:
        out.append(_Strategy(
            name=f"A_edge_score_tpsl",
            threshold=thresh,
            entry_rule=_entry_edge_score(thresh),
            use_tpsl_exit=True,
            tp_pct=0.10, sl_pct=0.07,
        ))

    # Family B: cheap-entry + TP/SL
    for thresh in [0.20, 0.25, 0.30, 0.35, 0.40]:
        out.append(_Strategy(
            name=f"B_cheap_tpsl",
            threshold=thresh,
            entry_rule=_entry_cheap(thresh),
            use_tpsl_exit=True,
            tp_pct=0.10, sl_pct=0.07,
        ))

    # Family C: cheap-entry + hold-to-expiry (CTF redemption, no slippage)
    for thresh in [0.15, 0.20, 0.25, 0.30, 0.35, 0.40]:
        out.append(_Strategy(
            name=f"C_cheap_hold_to_expiry",
            threshold=thresh,
            entry_rule=_entry_cheap(thresh),
            use_tpsl_exit=False,
        ))

    # Family D: momentum (chase, not fade) + TP/SL
    for thresh in [0.01, 0.02, 0.05, 0.10]:
        out.append(_Strategy(
            name=f"D_momentum_tpsl",
            threshold=thresh,
            entry_rule=_entry_momentum(thresh, window_sec=60),
            use_tpsl_exit=True,
            tp_pct=0.10, sl_pct=0.07,
        ))

    return out


def _run_sweep(pairs: list[_Pair], strategies: list[_Strategy], *, position_size: float, slippage: float) -> list[dict]:
    rows: list[dict] = []
    for strat in strategies:
        agg = _PairResult()
        for pair in pairs:
            r = _simulate(pair, strat, position_size=position_size, sell_slippage=slippage)
            agg.entries += r.entries
            agg.wins += r.wins
            agg.losses += r.losses
            agg.paper_pnl_usdc += r.paper_pnl_usdc
        decided = agg.wins + agg.losses
        wr = (agg.wins / decided) if decided else None
        rows.append({
            "name": strat.name,
            "threshold": strat.threshold,
            "use_tpsl_exit": strat.use_tpsl_exit,
            "tp_pct": strat.tp_pct,
            "sl_pct": strat.sl_pct,
            "pairs": len(pairs),
            "entries": agg.entries,
            "wins": agg.wins,
            "losses": agg.losses,
            "win_rate": round(wr, 4) if wr is not None else None,
            "paper_pnl_usdc": round(agg.paper_pnl_usdc, 4),
        })
    return rows


def _print_table(rows: list[dict], min_n_for_pass: int = 20) -> None:
    rows_sorted = sorted(
        rows,
        key=lambda r: ((r["win_rate"] or 0), r["paper_pnl_usdc"]),
        reverse=True,
    )
    print()
    print(f"{'strategy':<25} {'thresh':>6} {'exit':>4} {'n':>4} {'wins':>4} {'loss':>4} {'WR':>7} {'paper_pnl':>10}")
    print("-" * 75)
    passes = 0
    for r in rows_sorted:
        wr_str = f"{(r['win_rate'] or 0)*100:5.1f}%" if r['win_rate'] is not None else "n/a"
        exit_str = "tpsl" if r["use_tpsl_exit"] else "hold"
        n_decided = (r["wins"] or 0) + (r["losses"] or 0)
        passes_real = (
            r["win_rate"] is not None
            and r["win_rate"] >= 0.65
            and r["paper_pnl_usdc"] > 0
            and n_decided >= min_n_for_pass
        )
        passes_noise = (
            r["win_rate"] is not None
            and r["win_rate"] >= 0.65
            and r["paper_pnl_usdc"] > 0
            and n_decided < min_n_for_pass
        )
        marker = "*" if passes_real else ("?" if passes_noise else " ")
        if passes_real:
            passes += 1
        print(f"{marker} {r['name']:<23} {r['threshold']:>6.2f} {exit_str:>4} "
              f"{r['entries']:>4} {r['wins']:>4} {r['losses']:>4} {wr_str:>7} ${r['paper_pnl_usdc']:>+8.4f}")
    print()
    print(f"  * = passes 65% WR + PnL>0 with n≥{min_n_for_pass} settled trades (statistically meaningful)")
    print(f"  ? = passes WR/PnL but n<{min_n_for_pass} (likely noise — ignore)")
    print()
    if passes == 0:
        print("VERDICT: 0 configurations pass with statistical significance. Scalper-style "
              "strategy does not work on Polymarket 15-min crypto markets at the current "
              "spread/fee regime.")
    else:
        print(f"VERDICT: {passes} configuration(s) pass the gate with statistical significance.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=48,
                        help="Replay pairs from last N hours (default 48)")
    parser.add_argument("--max-pairs", type=int, default=200)
    parser.add_argument("--position-size", type=float, default=2.5)
    parser.add_argument("--slippage", type=float, default=0.02)
    parser.add_argument("--db-path", default="/app/data/trade_log.db")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    from agents.polymarket.polymarket import Polymarket
    pm = Polymarket(live=True)

    # Load pairs from local DB
    cutoff_ts = int(time.time()) - args.hours * 3600
    con = sqlite3.connect(args.db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT slug, period_ts, up_token, down_token FROM scalper_pairs "
        "WHERE state = 'expired' AND period_ts >= ? ORDER BY period_ts DESC LIMIT ?",
        (cutoff_ts, args.max_pairs),
    ).fetchall()
    con.close()
    logger.info("loaded %d expired pairs", len(rows))

    # Pre-build pair tick sequences (shared across all strategy runs)
    pairs: list[_Pair] = []
    for i, row in enumerate(rows):
        if args.verbose and i % 20 == 0:
            logger.info("fetching ticks: %d/%d", i, len(rows))
        p = _build_pair(pm, row["slug"], row["period_ts"], row["up_token"], row["down_token"])
        if p:
            pairs.append(p)
    logger.info("built %d valid pair tick sequences", len(pairs))

    if not pairs:
        print("No pairs available — DB empty or all fetches failed.", file=sys.stderr)
        return 1

    strategies = _build_strategies()
    logger.info("running %d strategies × %d pairs", len(strategies), len(pairs))

    sweep = _run_sweep(pairs, strategies, position_size=args.position_size, slippage=args.slippage)

    if args.json:
        print(json.dumps({"pairs_used": len(pairs), "results": sweep}, indent=2))
    else:
        print(f"# scalper sweep — {len(pairs)} pairs × {len(strategies)} strategies "
              f"(slippage={args.slippage}, position=${args.position_size})")
        _print_table(sweep)

    return 0


if __name__ == "__main__":
    sys.exit(main())
