#!/usr/bin/env python3
"""market sweep — test our strategies across DIFFERENT market types.

The earlier scalper sweep (`backtest_scalper_sweep.py`) tested only
crypto 15-min markets and found ALL strategies < 35% WR. The user's
hypothesis: maybe the issue is the MARKET, not the strategy. Test on
sports, politics, news, longer-horizon crypto.

Question: does ANY of our strategy concepts achieve ≥55% WR with
n≥30 settled trades on ANY market category?

Strategy concepts tested (replayable from price-history alone):

  1. CHEAP_HOLD    — enter cheap side ≤ X, hold to resolution.
                     CTF redemption → no slippage. Need WR > X to
                     break even (a 0.30 entry breaks even at 30% WR).

  2. NO_BIAS_HOLD  — always BUY NO at first sample, hold to resolution.
                     Tests systematic NO-bias on speculative markets
                     (config.py:182 says it exists; this measures it).

  3. FADE_LATE     — enter against direction of last 30% of market
                     lifecycle move. TP=+10%, SL=-7%, exit before
                     resolution. 2% FAK slippage on exit.

  4. MOMENTUM_LATE — opposite of FADE_LATE: enter WITH the move.

Categories sampled from Gamma:
  - crypto (excluding 15-min: weekly/monthly BTC/ETH price targets)
  - politics
  - sports
  - other (will-X-happen-by-Y speculative)

Per (category, strategy): aggregate WR, paper PnL. Flag any cell
with WR ≥ 55% AND PnL > 0 AND n ≥ 30 (statistically meaningful).

Usage:
    docker exec poly1-position-manager python \\
      /app/scripts/python/backtest_market_sweep.py --max-markets 300
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
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("market_sweep")


@dataclass
class _Market:
    slug: str
    category: str         # crypto / politics / sports / other
    yes_token: str
    no_token: str
    start_ts: int
    end_ts: int
    yes_won: bool         # ground truth from outcomePrices


@dataclass
class _Sample:
    ts: int
    yes_mid: float        # 0..1, NO mid = 1 - yes_mid


def _gamma_get(path: str, params: dict) -> list:
    qs = urllib.parse.urlencode(params)
    url = f"https://gamma-api.polymarket.com/{path}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 poly1-mkt-sweep"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        logger.warning("gamma %s failed: %s", path, exc)
        return []


def _categorize(m: dict) -> str:
    """Classify market by tag/slug heuristics."""
    slug = (m.get("slug") or "").lower()
    q = (m.get("question") or "").lower()
    text = f"{slug} {q}"
    if any(k in text for k in ["bitcoin", "ethereum", "solana", "btc", "eth", "sol", "crypto", "doge", "xrp"]):
        if any(k in slug for k in ["up-or-down", "updown", "-15m-"]):
            return "crypto-short"
        return "crypto"
    if any(k in text for k in ["election", "trump", "biden", "harris", "congress", "senate", "president", "vote", "primary"]):
        return "politics"
    if any(k in text for k in ["nba", "nfl", "atp", "wta", "tennis", "soccer", "football", "basketball", "rublev", "championship", "tournament"]):
        return "sports"
    if any(k in text for k in ["will ", "by-end-of", "by-december", "by-june", "happen", "announce"]):
        return "speculative"
    return "other"


def _fetch_resolved_markets(max_markets: int, *, max_age_days: int = 90, min_age_days: int = 0) -> list[_Market]:
    """Pull resolved markets ordered by volume desc — these have real
    trading activity and reliable price-history.

    `max_age_days` / `min_age_days` define the window: only include
    markets whose end_ts is in [now - max_age_days, now - min_age_days].
    Used by --split to run on disjoint 30-day windows.
    """
    markets: list[_Market] = []
    seen: set[str] = set()
    now_ts = int(time.time())
    offset = 0
    page_size = 100
    while len(markets) < max_markets and offset < 3000:
        batch = _gamma_get("markets", {
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
            slug = m.get("slug") or ""
            if slug in seen:
                continue
            seen.add(slug)
            try:
                tok_ids = ast.literal_eval(m.get("clobTokenIds") or "[]")
                if len(tok_ids) < 2:
                    continue
                op = ast.literal_eval(m.get("outcomePrices") or "[]")
                if len(op) < 2:
                    continue
                yes_won = float(op[0]) >= 0.99
                no_won = float(op[1]) >= 0.99
                if not (yes_won or no_won):
                    continue  # ambiguous
                # Time bounds
                from datetime import datetime
                end_iso = m.get("endDate") or m.get("end_date") or ""
                start_iso = m.get("startDate") or m.get("start_date") or ""
                if not end_iso:
                    continue
                end_ts = int(datetime.fromisoformat(end_iso.replace("Z", "+00:00")).timestamp())
                start_ts = (
                    int(datetime.fromisoformat(start_iso.replace("Z", "+00:00")).timestamp())
                    if start_iso else end_ts - 86400 * 7
                )
                if end_ts - start_ts < 86400:  # at least 1 day of activity
                    continue
                # Skip markets whose end_ts is in the future (administrative
                # early-close in Gamma — CLOB has no price-history for these).
                if end_ts > now_ts:
                    continue
                age_sec = now_ts - end_ts
                # Apply caller-specified age window (defaults: last 90 days).
                if age_sec > 86400 * max_age_days:
                    continue
                if age_sec < 86400 * min_age_days:
                    continue
                if end_ts - start_ts > 86400 * 365:
                    start_ts = end_ts - 86400 * 365  # cap to last 1y
                markets.append(_Market(
                    slug=slug,
                    category=_categorize(m),
                    yes_token=str(tok_ids[0]),
                    no_token=str(tok_ids[1]),
                    start_ts=start_ts,
                    end_ts=end_ts,
                    yes_won=yes_won,
                ))
            except Exception as exc:
                logger.debug("parse fail %s: %s", slug[:30], exc)
                continue
        offset += page_size
        if len(batch) < page_size:
            break
    return markets[:max_markets]


def _fetch_history(polymarket, token_id: str, start_ts: int, end_ts: int) -> list[_Sample]:
    """CLOB rejects intervals > ~30 days with 400. For long-running markets,
    take the last 30 days of the active period (where price-action concentrates
    anyway as resolution approaches)."""
    from py_clob_client_v2.clob_types import PricesHistoryParams
    MAX_RANGE = 30 * 86400
    if end_ts - start_ts > MAX_RANGE:
        start_ts = end_ts - MAX_RANGE
    try:
        resp = polymarket.client.get_prices_history(
            params=PricesHistoryParams(market=token_id, start_ts=start_ts, end_ts=end_ts, fidelity=60)
        )
    except Exception as exc:
        logger.debug("history fail %s: %s", token_id[:18], exc)
        return []
    raw = resp.get("history", []) if isinstance(resp, dict) else []
    return [_Sample(ts=int(r["t"]), yes_mid=float(r["p"])) for r in raw]


# ---------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------


def _strategy_cheap_hold(samples: list[_Sample], market: _Market, threshold: float) -> Optional[dict]:
    """Find first sample where cheaper side ≤ threshold. Hold to expiry."""
    for s in samples:
        no_mid = 1.0 - s.yes_mid
        cheaper_side = "YES" if s.yes_mid <= no_mid else "NO"
        cheaper_price = min(s.yes_mid, no_mid)
        if cheaper_price <= threshold and cheaper_price >= 0.05:
            won = (cheaper_side == "YES") == market.yes_won
            terminal = 1.0 if won else 0.0
            shares = 1.0 / max(cheaper_price, 0.001)
            pnl = shares * terminal - 1.0  # paper PnL on $1 position
            return {
                "side": cheaper_side,
                "entry_price": cheaper_price,
                "won": won,
                "pnl_per_dollar": pnl,
            }
    return None


def _strategy_no_bias_hold(samples: list[_Sample], market: _Market, ask_premium: float = 0.01) -> Optional[dict]:
    """Always BUY NO at first sample. Hold to expiry.

    `ask_premium` is added to mid to model bid-ask: in early-stage markets
    the spread is typically 2-3¢ so paying mid+1¢ is realistic-conservative.
    Default 0.01 = 1¢ premium.
    """
    if not samples:
        return None
    s = samples[0]
    no_mid = 1.0 - s.yes_mid
    if no_mid < 0.05 or no_mid > 0.95:
        return None
    no_ask = min(0.99, no_mid + ask_premium)  # cap to avoid >$1
    won = not market.yes_won
    terminal = 1.0 if won else 0.0
    shares = 1.0 / max(no_ask, 0.001)
    pnl = shares * terminal - 1.0
    return {
        "side": "NO",
        "entry_price": no_ask,
        "entry_mid": no_mid,
        "won": won,
        "pnl_per_dollar": pnl,
    }


def _strategy_fade_late(samples: list[_Sample], market: _Market, slippage: float = 0.02) -> Optional[dict]:
    """In last 30% of market lifecycle, enter against the direction of
    the move from market-start to entry-point. Exit at +10% / -7% or
    just-before-resolution. Apply 2% sell slippage on exit (FAK)."""
    if len(samples) < 10:
        return None
    cutoff_idx = int(len(samples) * 0.70)
    if cutoff_idx >= len(samples) - 2:
        return None
    start_mid = samples[0].yes_mid
    entry_sample = samples[cutoff_idx]
    move = entry_sample.yes_mid - start_mid
    if abs(move) < 0.05:
        return None  # skip flat markets
    side = "NO" if move > 0 else "YES"
    entry_price = entry_sample.yes_mid if side == "YES" else (1.0 - entry_sample.yes_mid)
    if entry_price < 0.10 or entry_price > 0.60:
        return None
    shares = 1.0 / max(entry_price, 0.001)
    # Walk forward looking for TP/SL
    for s in samples[cutoff_idx + 1:]:
        held_mid = s.yes_mid if side == "YES" else (1.0 - s.yes_mid)
        diff = (held_mid - entry_price) / entry_price
        if diff >= 0.10 or diff <= -0.07:
            effective = max(0.01, held_mid * (1.0 - slippage))
            pnl = shares * effective - 1.0
            return {
                "side": side,
                "entry_price": entry_price,
                "exit_price": effective,
                "won": pnl > 0,
                "pnl_per_dollar": pnl,
            }
    # No TP/SL → settle at resolution (CTF redemption, no slippage)
    won = (side == "YES") == market.yes_won
    pnl = shares * (1.0 if won else 0.0) - 1.0
    return {
        "side": side,
        "entry_price": entry_price,
        "exit_price": 1.0 if won else 0.0,
        "won": won,
        "pnl_per_dollar": pnl,
    }


def _strategy_momentum_late(samples: list[_Sample], market: _Market, slippage: float = 0.02) -> Optional[dict]:
    """Same lifecycle position as FADE_LATE but enter WITH the move."""
    if len(samples) < 10:
        return None
    cutoff_idx = int(len(samples) * 0.70)
    if cutoff_idx >= len(samples) - 2:
        return None
    start_mid = samples[0].yes_mid
    entry_sample = samples[cutoff_idx]
    move = entry_sample.yes_mid - start_mid
    if abs(move) < 0.05:
        return None
    side = "YES" if move > 0 else "NO"  # follow the move
    entry_price = entry_sample.yes_mid if side == "YES" else (1.0 - entry_sample.yes_mid)
    if entry_price < 0.10 or entry_price > 0.85:
        return None
    shares = 1.0 / max(entry_price, 0.001)
    for s in samples[cutoff_idx + 1:]:
        held_mid = s.yes_mid if side == "YES" else (1.0 - s.yes_mid)
        diff = (held_mid - entry_price) / entry_price
        if diff >= 0.10 or diff <= -0.07:
            effective = max(0.01, held_mid * (1.0 - slippage))
            pnl = shares * effective - 1.0
            return {
                "side": side,
                "entry_price": entry_price,
                "exit_price": effective,
                "won": pnl > 0,
                "pnl_per_dollar": pnl,
            }
    won = (side == "YES") == market.yes_won
    pnl = shares * (1.0 if won else 0.0) - 1.0
    return {
        "side": side,
        "entry_price": entry_price,
        "exit_price": 1.0 if won else 0.0,
        "won": won,
        "pnl_per_dollar": pnl,
    }


# ---------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------


@dataclass
class _CellResult:
    n: int = 0
    wins: int = 0
    losses: int = 0
    paper_pnl: float = 0.0


def _accumulate(cell: _CellResult, trade: dict) -> None:
    cell.n += 1
    if trade["won"]:
        cell.wins += 1
    else:
        cell.losses += 1
    cell.paper_pnl += trade["pnl_per_dollar"]


def _print_matrix(matrix: dict[str, dict[str, _CellResult]], min_n: int = 30) -> None:
    strategies = sorted({s for cat in matrix.values() for s in cat.keys()})
    categories = sorted(matrix.keys())
    print()
    header = f"{'category':<14}" + "".join(f"  {s[:18]:<18}" for s in strategies)
    print(header)
    print("-" * len(header))
    passes_total = 0
    for cat in categories:
        row = f"{cat:<14}"
        for strat in strategies:
            c = matrix[cat].get(strat) or _CellResult()
            decided = c.wins + c.losses
            wr = (c.wins / decided) if decided else None
            wr_str = f"{wr*100:5.1f}%" if wr is not None else "  n/a"
            passes = wr is not None and wr >= 0.55 and c.paper_pnl > 0 and decided >= min_n
            marker = "*" if passes else " "
            if passes:
                passes_total += 1
            row += f" {marker} {wr_str} n={decided:>3}      "
        print(row)
    print()
    print(f"  * = WR ≥ 55% + PnL > 0 + n ≥ {min_n}")
    print()
    if passes_total == 0:
        print("VERDICT: 0 (category × strategy) cells pass the gate. None of our "
              "strategies achieves a meaningful edge on any tested market type.")
    else:
        print(f"VERDICT: {passes_total} cell(s) pass — see * markers above.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-markets", type=int, default=300)
    parser.add_argument("--slippage", type=float, default=0.02)
    parser.add_argument("--ask-premium", type=float, default=0.01,
                        help="Entry premium over mid (cents). Default 0.01 = 1¢ buy at mid+1¢.")
    parser.add_argument("--max-age-days", type=int, default=90)
    parser.add_argument("--min-age-days", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    from agents.polymarket.polymarket import Polymarket
    pm = Polymarket(live=True)

    logger.info("fetching resolved markets [%d-%d days old]…", args.min_age_days, args.max_age_days)
    markets = _fetch_resolved_markets(args.max_markets,
                                      max_age_days=args.max_age_days,
                                      min_age_days=args.min_age_days)
    logger.info("loaded %d resolved markets", len(markets))

    cat_counts: dict[str, int] = {}
    for m in markets:
        cat_counts[m.category] = cat_counts.get(m.category, 0) + 1
    logger.info("categories: %s", cat_counts)

    matrix: dict[str, dict[str, _CellResult]] = {}

    for i, market in enumerate(markets):
        if args.verbose and i % 25 == 0:
            logger.info("progress: %d/%d (%s)", i, len(markets), market.category)
        samples = _fetch_history(pm, market.yes_token, market.start_ts, market.end_ts)
        if len(samples) < 5:
            continue
        cat_bucket = matrix.setdefault(market.category, {})

        # cheap_hold at thresholds 0.20/0.30/0.40
        for thresh in [0.20, 0.30, 0.40]:
            t = _strategy_cheap_hold(samples, market, thresh)
            if t is not None:
                _accumulate(cat_bucket.setdefault(f"cheap_hold_{thresh:.2f}", _CellResult()), t)

        # no_bias_hold
        t = _strategy_no_bias_hold(samples, market, ask_premium=args.ask_premium)
        if t is not None:
            _accumulate(cat_bucket.setdefault("no_bias_hold", _CellResult()), t)

        # fade_late
        t = _strategy_fade_late(samples, market, slippage=args.slippage)
        if t is not None:
            _accumulate(cat_bucket.setdefault("fade_late", _CellResult()), t)

        # momentum_late
        t = _strategy_momentum_late(samples, market, slippage=args.slippage)
        if t is not None:
            _accumulate(cat_bucket.setdefault("momentum_late", _CellResult()), t)

    if args.json:
        out = {
            cat: {
                strat: {
                    "n": c.n,
                    "wins": c.wins,
                    "losses": c.losses,
                    "paper_pnl_per_dollar": round(c.paper_pnl, 4),
                    "win_rate": round(c.wins / (c.wins + c.losses), 4) if (c.wins + c.losses) else None,
                }
                for strat, c in strats.items()
            }
            for cat, strats in matrix.items()
        }
        print(json.dumps({"markets_total": len(markets), "matrix": out}, indent=2))
    else:
        print(f"# market sweep — {len(markets)} resolved markets, slippage={args.slippage}")
        for cat, n in sorted(cat_counts.items(), key=lambda kv: -kv[1]):
            print(f"  category {cat}: {n} markets")
        _print_matrix(matrix)

    return 0


if __name__ == "__main__":
    sys.exit(main())
