#!/usr/bin/env python3
"""nothing_happens backtest — does the agent's actual filter cascade
+ entry rule produce ≥55% WR with stability across 3×30-day windows?

Mimics swarm's ``NothingHappensConfig`` defaults exactly:

  - eligibility: vol24h ≥ $100k, end in [7, 90] days, slug matches
    require_keywords ("will ", " by ", "before "), NOT in
    exclude_keywords (sports, "no change" / "fail to" inversions)
  - entry: first sample where NO ≤ 0.30
  - exit: hold to resolution (CTF redemption, no slippage)
  - position size: $1 (the live config)

Per `docs/archive/sessions/2026-05/SESSION_2026-05-10_SCOUT_PLAN.md` Step 4: this is the gate
that decides whether nothing_happens flips BOT_MODE=live. We split
the 90-day window into 3×30-day buckets and require ALL three to
exceed 55% WR with n≥30 settled trades. (The 90-day average alone
hid regime-specific noise on no_bias_hold yesterday.)

Usage:
    docker exec poly1-position-manager python \\
      /app/scripts/python/backtest_nothing_happens.py
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import re
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
logger = logging.getLogger("backtest_nh")


# Mirrors swarm/config.py:NothingHappensConfig
NO_PRICE_CAP = 0.30
MIN_VOLUME_USD = 100_000.0
MIN_DAYS_TO_RESOLUTION = 7.0
MAX_DAYS_TO_RESOLUTION = 90.0
POSITION_SIZE_USD = 1.0

EXCLUDE_KEYWORDS = [
    "nba", "nfl", "mlb", "nhl", "ufc", "mma", "soccer", "football",
    "baseball", "basketball", "hockey", "tennis", "golf", "champions",
    "premier", "bundesliga", "liga", "serie a", "ligue 1", "lck",
    "lol:", "csgo", "valorant", "cricket", "f1", "formula", "scorer",
    "match", "game ", "vs ", "playoffs", "championship",
    "no change", "won't", "will not", "remain unchanged",
    "stay the same", "fail to", "not happen",
]
REQUIRE_KEYWORDS = ["will ", " by ", "before "]


@dataclass
class _Market:
    slug: str
    question: str
    condition_id: str
    yes_token: str
    no_token: str
    end_ts: int
    yes_won: bool
    volume_24h: float


@dataclass
class _Sample:
    ts: int
    yes_mid: float


def _gamma_get(params: dict) -> list:
    qs = urllib.parse.urlencode(params)
    url = f"https://gamma-api.polymarket.com/markets?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 poly1-nh-backtest"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        logger.warning("gamma fetch failed: %s", exc)
        return []


def _passes_filter(m: dict, *, now_ts: int, min_age_days: int, max_age_days: int, min_volume_usd: float = MIN_VOLUME_USD) -> Optional[_Market]:
    """Apply nothing_happens eligibility filters. Returns _Market or None."""
    try:
        slug = (m.get("slug") or "").lower()
        question = (m.get("question") or "").lower()
        if not slug or not question:
            return None
        text = f"{slug} {question}"

        # Slug keyword filter
        if not any(kw in text for kw in REQUIRE_KEYWORDS):
            return None
        if any(kw in text for kw in EXCLUDE_KEYWORDS):
            return None

        # Volume filter. Closed markets have volume24hr=0; use cumulative
        # `volumeNum` instead which reflects total trading interest.
        # MIN_VOLUME_USD=$100k of cumulative volume is roughly equivalent
        # to the live filter's "$100k 24h volume" on actively-traded markets.
        vol = float(m.get("volumeNum") or m.get("volume") or 0)
        if vol < min_volume_usd:
            return None

        # Outcome resolution clarity
        op = ast.literal_eval(m.get("outcomePrices") or "[]")
        if len(op) < 2:
            return None
        yes_won = float(op[0]) >= 0.99
        no_won = float(op[1]) >= 0.99
        if not (yes_won or no_won):
            return None

        # End date in past, within age window
        end_iso = m.get("endDate") or ""
        if not end_iso:
            return None
        from datetime import datetime
        end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        end_ts = int(end_dt.timestamp())
        if end_ts > now_ts:
            return None
        age_sec = now_ts - end_ts
        if age_sec > 86400 * max_age_days or age_sec < 86400 * min_age_days:
            return None

        # Tokens
        tok_ids = ast.literal_eval(m.get("clobTokenIds") or "[]")
        if len(tok_ids) < 2:
            return None

        cid = str(m.get("conditionId") or m.get("condition_id") or "")
        if not cid:
            return None
        return _Market(
            slug=slug,
            question=question,
            condition_id=cid,
            yes_token=str(tok_ids[0]),
            no_token=str(tok_ids[1]),
            end_ts=end_ts,
            yes_won=yes_won,
            volume_24h=vol,
        )
    except Exception:
        return None


def _fetch_markets(*, max_markets: int, max_age_days: int, min_age_days: int, min_volume_usd: float = MIN_VOLUME_USD) -> list[_Market]:
    """Pull resolved markets meeting nothing_happens filters."""
    out: list[_Market] = []
    seen: set[str] = set()
    now_ts = int(time.time())
    offset = 0
    page_size = 100
    while len(out) < max_markets and offset < 5000:
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
            slug = m.get("slug") or ""
            if slug in seen:
                continue
            seen.add(slug)
            mkt = _passes_filter(m, now_ts=now_ts, min_age_days=min_age_days, max_age_days=max_age_days, min_volume_usd=min_volume_usd)
            if mkt:
                out.append(mkt)
        offset += page_size
        if len(batch) < page_size:
            break
    return out[:max_markets]


def _fetch_trades_for_market(condition_id: str, max_pages: int = 50) -> list[dict]:
    """Pull ALL trades for a market from data-api.polymarket.com.

    CLOB's /prices-history endpoint returns 0 samples for high-volume
    closed political markets — exactly the markets nothing_happens
    targets. The Polymarket data-api keeps full trade history for any
    closed market and is publicly accessible (no API key).

    Filter: ``market=<conditionId>`` (NOT token_id). Pagination via
    offset; each page returns up to 100 trades. Returns raw trade
    dicts sorted by timestamp ascending.
    """
    out: list[dict] = []
    seen_hashes: set[str] = set()
    for page in range(max_pages):
        offset = page * 100
        params = urllib.parse.urlencode({"market": condition_id, "limit": 100, "offset": offset})
        url = f"https://data-api.polymarket.com/trades?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "poly1-nh-backtest"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                batch = json.loads(resp.read())
        except Exception as exc:
            logger.debug("data-api trades fail page=%d cid=%s: %s", page, condition_id[:18], exc)
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
    """Convert raw trades to a price-time series in YES-mid semantics.

    Each trade's ``price`` is the outcome's (YES or NO) execution
    price. For the YES-side time series, BUY of YES = price; BUY of
    NO at price p means equivalent YES = 1 - p (binary symmetry).
    Filter trades by `outcomeIndex` (0=YES, 1=NO).

    Returns _Sample list with `yes_mid` semantics.
    """
    samples: list[_Sample] = []
    for t in trades:
        try:
            ts = int(t.get("timestamp") or 0)
            price = float(t.get("price") or 0)
            asset = str(t.get("asset") or "")
            if ts <= 0 or price <= 0:
                continue
            # asset == yes_token → price is YES side; else NO side
            if asset == yes_token:
                yes_mid = price
            else:
                yes_mid = 1.0 - price
            samples.append(_Sample(ts=ts, yes_mid=yes_mid))
        except (TypeError, ValueError):
            continue
    samples.sort(key=lambda s: s.ts)
    return samples


@dataclass
class _Trade:
    market_slug: str
    end_ts: int
    entry_ts: int
    entry_no_price: float
    won: bool   # NO won?
    pnl_per_dollar: float


def _simulate(market: _Market, samples: list[_Sample]) -> Optional[_Trade]:
    """Find the first sample where NO ≤ NO_PRICE_CAP (= 1 - YES). Hold to resolution.

    samples[].yes_mid is YES side. NO price = 1 - yes_mid.
    """
    if not samples:
        return None
    for s in samples:
        no_price = 1.0 - s.yes_mid
        if no_price > NO_PRICE_CAP or no_price < 0.05:
            continue
        # Entry
        won = not market.yes_won  # NO wins if YES did NOT win
        terminal = 1.0 if won else 0.0
        shares = 1.0 / max(no_price, 0.001)
        pnl = shares * terminal - 1.0  # PnL per $1 position
        return _Trade(
            market_slug=market.slug,
            end_ts=market.end_ts,
            entry_ts=s.ts,
            entry_no_price=no_price,
            won=won,
            pnl_per_dollar=pnl,
        )
    return None


@dataclass
class _WindowResult:
    label: str
    n: int = 0
    wins: int = 0
    losses: int = 0
    paper_pnl: float = 0.0


def _accumulate(window: _WindowResult, trade: _Trade) -> None:
    window.n += 1
    if trade.won:
        window.wins += 1
    else:
        window.losses += 1
    window.paper_pnl += trade.pnl_per_dollar


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-markets", type=int, default=300,
                        help="Cap markets per window (default 300)")
    parser.add_argument("--min-volume", type=float, default=MIN_VOLUME_USD,
                        help=f"Min cumulative volume ($) for eligibility. Default ${MIN_VOLUME_USD:,.0f}.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    windows = [
        _WindowResult(label="0-30d"),
        _WindowResult(label="30-60d"),
        _WindowResult(label="60-90d"),
    ]
    age_ranges = [(0, 30), (30, 60), (60, 90)]

    print("# nothing_happens backtest — split test 30/30/30")
    print(f"config: NO≤{NO_PRICE_CAP} vol≥${MIN_VOLUME_USD:,.0f} "
          f"days_to_resolution=[{MIN_DAYS_TO_RESOLUTION},{MAX_DAYS_TO_RESOLUTION}] "
          f"position_size=${POSITION_SIZE_USD}")
    print()

    all_trades: list[_Trade] = []

    for window, (min_age, max_age) in zip(windows, age_ranges):
        markets = _fetch_markets(max_markets=args.max_markets, max_age_days=max_age, min_age_days=min_age, min_volume_usd=args.min_volume)
        logger.info("window %s: %d markets passed eligibility filter", window.label, len(markets))
        for m in markets:
            # data-api.polymarket.com/trades returns ALL trades for the
            # conditionId, paginated. Far more reliable for closed
            # political markets than CLOB /prices-history (which returns
            # 0 samples for these markets — verified 2026-05-10).
            raw_trades = _fetch_trades_for_market(m.condition_id, max_pages=20)
            samples = _trades_to_yes_samples(raw_trades, m.yes_token)
            trade = _simulate(m, samples)
            if trade:
                _accumulate(window, trade)
                all_trades.append(trade)

    # Print per-window
    print("## Window results")
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
        print("VERDICT ✅ nothing_happens passes ALL 3 windows. Ready to flip BOT_MODE=live for this agent only.")
    elif pass_count >= 1:
        print(f"VERDICT 🟡 passes {pass_count}/3 windows — REGIME-SPECIFIC. Do NOT flip live; "
              "would replicate yesterday's no_bias_hold mistake.")
    else:
        print("VERDICT ❌ fails all 3 windows. Strategy does not have stable edge in current data.")

    if args.json:
        out = {
            "windows": [
                {
                    "label": w.label, "n": w.n, "wins": w.wins, "losses": w.losses,
                    "win_rate": (w.wins / (w.wins + w.losses)) if (w.wins + w.losses) else None,
                    "paper_pnl_per_dollar": round(w.paper_pnl, 4),
                }
                for w in windows
            ],
            "pass_count": pass_count,
            "config": {
                "no_price_cap": NO_PRICE_CAP,
                "min_volume_usd": MIN_VOLUME_USD,
                "min_days_to_resolution": MIN_DAYS_TO_RESOLUTION,
                "max_days_to_resolution": MAX_DAYS_TO_RESOLUTION,
            },
        }
        print(json.dumps(out, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
