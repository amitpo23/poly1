#!/usr/bin/env python3
"""Backtest strategies #5 (Resolution Drift) and #9 (Range-Bound) from
the 10-strategies design doc (~/Desktop/poly/05_עשר_אסטרטגיות_בוט.md).

Same gate as the rest of our work: ≥55% WR with stability across
3×30-day windows + n≥30 settled trades per window. Uses Polymarket's
data-api `/trades` endpoint (the path that worked for nothing_happens
where CLOB /prices-history failed).

## Strategy #5 — Resolution Drift

Premise: as a market approaches resolution, prices accelerate toward
0 or 1 (decay-of-uncertainty). Entering on the FAVORITE side late in
the cycle should win at the priced-in rate, but with very short hold
time = high capital efficiency.

Logic:
  - In the last 10% of the market's trade-history timeline (proxy for
    "approaching resolution"), find the dominant side (price > 0.50).
  - Enter at that side's current price.
  - Hold to resolution. Win at $1 if that side won, $0 if not.

This is "follow the late-stage favorite" — the OPPOSITE of fade.

## Strategy #9 — Range-Bound

Premise: some markets stay near 50/50 for most of their life (binary
events with genuine uncertainty). Mean-reversion within the band:
buy at 0.45, sell at 0.55, repeat.

Logic:
  - Walk price-time series.
  - When mid drops to ≤ 0.45 → enter BUY (whichever side is at 0.45).
  - Exit when mid recovers to ≥ 0.50 (TP) or drops to ≤ 0.40 (SL).
  - 2% slippage on exit (FAK SELL convention).
  - Each market can produce multiple trades (vs. once-per-market for
    #5/nothing_happens).

Usage:
    docker exec poly1-position-manager python \\
      /app/scripts/python/backtest_strategy_5_9.py --strategy 5
    docker exec poly1-position-manager python \\
      /app/scripts/python/backtest_strategy_5_9.py --strategy 9
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("backtest_5_9")


SLIPPAGE = 0.02


@dataclass
class _Market:
    slug: str
    condition_id: str
    yes_token: str
    end_ts: int
    yes_won: bool
    volume_total: float


@dataclass
class _Sample:
    ts: int
    yes_mid: float


def _gamma_get(params: dict) -> list:
    qs = urllib.parse.urlencode(params)
    url = f"https://gamma-api.polymarket.com/markets?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "poly1-bt"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        logger.warning("gamma fetch failed: %s", exc)
        return []


def _fetch_resolved_markets(*, max_markets: int, max_age_days: int, min_age_days: int,
                             min_volume: float = 5_000.0) -> list[_Market]:
    """Pull resolved markets via Gamma, ordered by volume desc."""
    markets: list[_Market] = []
    seen: set[str] = set()
    now_ts = int(time.time())
    offset = 0
    page_size = 100
    while len(markets) < max_markets and offset < 5000:
        batch = _gamma_get({
            "closed": "true",
            "order": "volumeNum",
            "ascending": "false",
            "limit": page_size,
            "offset": offset,
        })
        if not batch:
            break
        for m in batch:
            if not isinstance(m, dict):
                continue
            try:
                slug = m.get("slug") or ""
                if not slug or slug in seen:
                    continue
                seen.add(slug)
                op = ast.literal_eval(m.get("outcomePrices") or "[]")
                if len(op) < 2:
                    continue
                yes_won = float(op[0]) >= 0.99
                no_won = float(op[1]) >= 0.99
                if not (yes_won or no_won):
                    continue
                end_iso = m.get("endDate") or ""
                if not end_iso:
                    continue
                end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                end_ts = int(end_dt.timestamp())
                if end_ts > now_ts:
                    continue
                age_sec = now_ts - end_ts
                if age_sec > 86400 * max_age_days or age_sec < 86400 * min_age_days:
                    continue
                vol = float(m.get("volumeNum") or m.get("volume") or 0)
                if vol < min_volume:
                    continue
                tok_ids = ast.literal_eval(m.get("clobTokenIds") or "[]")
                if len(tok_ids) < 2:
                    continue
                cid = str(m.get("conditionId") or m.get("condition_id") or "")
                if not cid:
                    continue
                markets.append(_Market(
                    slug=slug, condition_id=cid,
                    yes_token=str(tok_ids[0]),
                    end_ts=end_ts, yes_won=yes_won,
                    volume_total=vol,
                ))
            except Exception:
                continue
        offset += page_size
        if len(batch) < page_size:
            break
    return markets[:max_markets]


def _fetch_trades(condition_id: str, max_pages: int = 20) -> list[dict]:
    out: list[dict] = []
    seen_hashes: set[str] = set()
    for page in range(max_pages):
        params = urllib.parse.urlencode({
            "market": condition_id, "limit": 100, "offset": page * 100,
        })
        url = f"https://data-api.polymarket.com/trades?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "poly1-bt"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                batch = json.loads(resp.read())
        except Exception as exc:
            logger.debug("data-api fail page=%d: %s", page, exc)
            break
        if not batch:
            break
        new_count = 0
        for t in batch:
            h = t.get("transactionHash") or f"{t.get('timestamp')}:{t.get('proxyWallet')}"
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            out.append(t)
            new_count += 1
        if new_count == 0 or len(batch) < 100:
            break
    out.sort(key=lambda t: t.get("timestamp", 0))
    return out


def _trades_to_yes_samples(trades: list[dict], yes_token: str) -> list[_Sample]:
    samples: list[_Sample] = []
    for t in trades:
        try:
            ts = int(t.get("timestamp") or 0)
            price = float(t.get("price") or 0)
            asset = str(t.get("asset") or "")
            if ts <= 0 or price <= 0 or price >= 1:
                continue
            yes_mid = price if asset == yes_token else (1.0 - price)
            samples.append(_Sample(ts=ts, yes_mid=yes_mid))
        except (TypeError, ValueError):
            continue
    samples.sort(key=lambda s: s.ts)
    return samples


# ---------------------------------------------------------------------
# Strategy #5 — Resolution Drift
# ---------------------------------------------------------------------


@dataclass
class _Trade:
    market_slug: str
    entry_ts: int
    entry_price: float
    side: str  # "YES" or "NO"
    won: bool
    pnl_per_dollar: float


def _strategy_5_resolution_drift(market: _Market, samples: list[_Sample]) -> Optional[_Trade]:
    """In last 10% of trade-history timeline, enter on the favorite (price > 0.50)
    and hold to resolution. CTF redemption — no slippage."""
    if len(samples) < 10:
        return None
    cutoff_idx = int(len(samples) * 0.90)
    if cutoff_idx >= len(samples) - 1:
        return None
    s = samples[cutoff_idx]
    yes_p = s.yes_mid
    no_p = 1.0 - yes_p
    if max(yes_p, no_p) < 0.55:  # not decisive enough
        return None
    if max(yes_p, no_p) > 0.95:  # already at extreme — too late
        return None
    if yes_p > no_p:
        side = "YES"; entry_price = yes_p; won = market.yes_won
    else:
        side = "NO"; entry_price = no_p; won = not market.yes_won
    shares = 1.0 / max(entry_price, 0.001)
    terminal = 1.0 if won else 0.0
    pnl = shares * terminal - 1.0
    return _Trade(
        market_slug=market.slug, entry_ts=s.ts, entry_price=entry_price,
        side=side, won=won, pnl_per_dollar=pnl,
    )


# ---------------------------------------------------------------------
# Strategy #9 — Range-Bound
# ---------------------------------------------------------------------


def _strategy_9_range_bound(market: _Market, samples: list[_Sample]) -> list[_Trade]:
    """When yes_mid drops to ≤0.45, BUY YES. Exit at TP=0.50 or SL=0.40.
    2% FAK slippage on exit. Multiple trades per market possible.

    Also runs the symmetric leg: when yes_mid rises to ≥0.55 (i.e., NO ≤ 0.45),
    BUY NO. Exit at NO_mid≥0.50 (yes_mid≤0.50) or NO_mid≤0.40 (yes_mid≥0.60).
    """
    out: list[_Trade] = []
    if len(samples) < 10:
        return out
    open_pos: Optional[dict] = None
    for s in samples:
        if open_pos is None:
            # YES leg
            if s.yes_mid <= 0.45 and s.yes_mid >= 0.20:
                open_pos = {"side": "YES", "entry": s.yes_mid, "entry_ts": s.ts}
            # NO leg (symmetric)
            elif s.yes_mid >= 0.55 and s.yes_mid <= 0.80:
                open_pos = {"side": "NO", "entry": 1.0 - s.yes_mid, "entry_ts": s.ts}
            continue
        # Already open — check exit
        if open_pos["side"] == "YES":
            held_mid = s.yes_mid
            tp_hit = held_mid >= 0.50
            sl_hit = held_mid <= 0.40
        else:  # NO
            no_mid = 1.0 - s.yes_mid
            held_mid = no_mid
            tp_hit = no_mid >= 0.50
            sl_hit = no_mid <= 0.40
        if not (tp_hit or sl_hit):
            continue
        # Apply slippage on exit
        effective_exit = max(0.01, held_mid * (1.0 - SLIPPAGE))
        shares = 1.0 / max(open_pos["entry"], 0.001)
        pnl = shares * effective_exit - 1.0
        out.append(_Trade(
            market_slug=market.slug, entry_ts=open_pos["entry_ts"],
            entry_price=open_pos["entry"], side=open_pos["side"],
            won=pnl > 0, pnl_per_dollar=pnl,
        ))
        open_pos = None
    # If still open at end, settle at terminal ($1 if held_side won)
    if open_pos is not None:
        if open_pos["side"] == "YES":
            won = market.yes_won
        else:
            won = not market.yes_won
        shares = 1.0 / max(open_pos["entry"], 0.001)
        pnl = shares * (1.0 if won else 0.0) - 1.0
        out.append(_Trade(
            market_slug=market.slug, entry_ts=open_pos["entry_ts"],
            entry_price=open_pos["entry"], side=open_pos["side"],
            won=won, pnl_per_dollar=pnl,
        ))
    return out


# ---------------------------------------------------------------------
# Window aggregation + CLI
# ---------------------------------------------------------------------


@dataclass
class _Window:
    label: str
    n: int = 0
    wins: int = 0
    losses: int = 0
    paper_pnl: float = 0.0


def _accumulate(window: _Window, trade: _Trade) -> None:
    window.n += 1
    if trade.won:
        window.wins += 1
    else:
        window.losses += 1
    window.paper_pnl += trade.pnl_per_dollar


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", type=int, choices=[5, 9], required=True,
                        help="5 = Resolution Drift, 9 = Range-Bound")
    parser.add_argument("--max-markets", type=int, default=150,
                        help="Cap markets per window (default 150)")
    parser.add_argument("--min-volume", type=float, default=5_000.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    windows = [
        _Window(label="0-30d"),
        _Window(label="30-60d"),
        _Window(label="60-90d"),
    ]
    age_ranges = [(0, 30), (30, 60), (60, 90)]

    strategy_name = {5: "Resolution Drift", 9: "Range-Bound"}[args.strategy]
    print(f"# strategy #{args.strategy} — {strategy_name}")
    print(f"config: min_vol=${args.min_volume:,.0f} slippage={SLIPPAGE*100:.0f}%")
    print()

    for window, (min_age, max_age) in zip(windows, age_ranges):
        markets = _fetch_resolved_markets(
            max_markets=args.max_markets, max_age_days=max_age,
            min_age_days=min_age, min_volume=args.min_volume,
        )
        logger.info("window %s: %d markets", window.label, len(markets))
        for m in markets:
            trades_raw = _fetch_trades(m.condition_id, max_pages=10)
            samples = _trades_to_yes_samples(trades_raw, m.yes_token)
            if not samples:
                continue
            if args.strategy == 5:
                t = _strategy_5_resolution_drift(m, samples)
                if t:
                    _accumulate(window, t)
            else:
                trades = _strategy_9_range_bound(m, samples)
                for t in trades:
                    _accumulate(window, t)

    print(f"## Window results")
    print(f"  {'window':<8} {'n':>4} {'wins':>4} {'loss':>4} {'WR':>7} {'paper_pnl/$':>12}")
    print("  " + "-" * 50)
    pass_count = 0
    for w in windows:
        decided = w.wins + w.losses
        wr = (w.wins / decided) if decided else None
        wr_str = f"{wr*100:5.1f}%" if wr is not None else "  n/a"
        passes = wr is not None and wr >= 0.55 and w.paper_pnl > 0 and decided >= 30
        marker = "*" if passes else " "
        if passes:
            pass_count += 1
        print(f"  {marker} {w.label:<8} {w.n:>4} {w.wins:>4} {w.losses:>4} {wr_str:>7} ${w.paper_pnl:>+10.4f}")
    print()
    print("  * = WR ≥ 55%, PnL > 0, n ≥ 30 (statistically meaningful pass)")
    print()
    if pass_count == 3:
        print(f"VERDICT ✅ strategy #{args.strategy} passes ALL 3 windows. Worth building an agent for.")
    elif pass_count >= 1:
        print(f"VERDICT 🟡 passes {pass_count}/3 windows — REGIME-SPECIFIC. Do NOT build live agent.")
    else:
        print(f"VERDICT ❌ fails all 3 windows. Strategy not viable as currently formulated.")

    if args.json:
        out = {
            "strategy": args.strategy,
            "strategy_name": strategy_name,
            "windows": [
                {"label": w.label, "n": w.n, "wins": w.wins, "losses": w.losses,
                 "win_rate": (w.wins / (w.wins + w.losses)) if (w.wins + w.losses) else None,
                 "paper_pnl_per_dollar": round(w.paper_pnl, 4)}
                for w in windows
            ],
            "pass_count": pass_count,
        }
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
