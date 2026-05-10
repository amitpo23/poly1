#!/usr/bin/env python3
"""BTC daily momentum backtest — chase the move, opposite of btc_daily.

btc_daily FADES: BTC up → buy NO. mean_reversion thesis.
This harness tests the OPPOSITE: BTC up → buy YES (follow the trend).

Question: does momentum on BTC up-or-down dailies pass ≥55% WR with
stability across 3×30-day windows? If yes → build a momentum agent.
If no → save the verdict, don't build.

Strategy logic:
  - Walk daily BTC up-or-down trades from data-api (more reliable
    than CLOB /prices-history for older markets).
  - For each market, find the FIRST trade where YES has moved by
    ≥`trigger_pct` from market open price.
  - If YES moved UP by ≥trigger → BUY YES at current ask.
  - If YES moved DOWN by ≥trigger → BUY NO at current ask.
  - Hold to TP=+10% / SL=-7% / EOD resolution.
  - 2% slippage on TP/SL exits; CTF redemption (no slippage) at EOD.

Compare directly to btc_daily backtest (same markets, opposite side).

Usage:
    docker exec poly1-position-manager python \\
      /app/scripts/python/backtest_btc_momentum.py --days 30
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("backtest_momentum")


SLIPPAGE = 0.02
TRIGGER_PCT_DEFAULT = 0.005   # 0.5% YES-mid move from open (proxy for BTC move)
TP_PCT = 0.10
SL_PCT = 0.07


@dataclass
class _Market:
    slug: str
    condition_id: str
    yes_token: str
    end_ts: int
    start_ts: int
    yes_won: bool


@dataclass
class _Sample:
    ts: int
    yes_mid: float


def _gamma_get(params: dict) -> list:
    qs = urllib.parse.urlencode(params)
    url = f"https://gamma-api.polymarket.com/markets?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "poly1-bt-mom"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        logger.warning("gamma fetch failed: %s", exc)
        return []


def _format_btc_slug(when: datetime) -> str:
    return f"bitcoin-up-or-down-on-{when.strftime('%B').lower()}-{when.day}-{when.year}"


def _resolve_btc_market(slug: str) -> Optional[_Market]:
    """Fetch one BTC daily market by slug; return _Market or None."""
    for closed in ("true", "false"):
        for m in _gamma_get({"slug": slug, "closed": closed}):
            if not isinstance(m, dict):
                continue
            try:
                tok_ids = ast.literal_eval(m.get("clobTokenIds") or "[]")
                op = ast.literal_eval(m.get("outcomePrices") or "[]")
                if len(tok_ids) < 2 or len(op) < 2:
                    return None
                yes_won = float(op[0]) >= 0.99
                no_won = float(op[1]) >= 0.99
                if not (yes_won or no_won):
                    return None
                end_iso = m.get("endDate") or ""
                start_iso = m.get("startDate") or ""
                if not end_iso:
                    return None
                end_ts = int(datetime.fromisoformat(end_iso.replace("Z", "+00:00")).timestamp())
                start_ts = int(datetime.fromisoformat(start_iso.replace("Z", "+00:00")).timestamp()) if start_iso else end_ts - 86400
                cid = str(m.get("conditionId") or m.get("condition_id") or "")
                if not cid:
                    return None
                return _Market(
                    slug=slug, condition_id=cid,
                    yes_token=str(tok_ids[0]),
                    end_ts=end_ts, start_ts=start_ts, yes_won=yes_won,
                )
            except Exception:
                continue
    return None


def _fetch_trades(condition_id: str, max_pages: int = 30) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for page in range(max_pages):
        params = urllib.parse.urlencode({
            "market": condition_id, "limit": 100, "offset": page * 100,
        })
        url = f"https://data-api.polymarket.com/trades?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "poly1-bt-mom"})
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
            if h in seen:
                continue
            seen.add(h)
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


@dataclass
class _Trade:
    market_slug: str
    entry_ts: int
    entry_price: float
    side: str
    exit_reason: str
    pnl_per_dollar: float
    won: bool


def _simulate_momentum(market: _Market, samples: list[_Sample], *, trigger_pct: float) -> Optional[_Trade]:
    """Find first sample where YES-mid moved ≥ trigger_pct from the
    earliest sample. CHASE direction: if YES rose → buy YES; if YES
    dropped → buy NO. Hold to TP=+10% / SL=-7% / EOD resolution.
    Apply 2% slippage on TP/SL exits; resolution is exact ($1/$0).
    """
    if len(samples) < 5:
        return None
    open_yes = samples[0].yes_mid
    if open_yes <= 0 or open_yes >= 1:
        return None

    # Find trigger
    entry_idx: Optional[int] = None
    for i, s in enumerate(samples[1:], start=1):
        delta = (s.yes_mid - open_yes) / max(open_yes, 1e-9)
        if abs(delta) >= trigger_pct:
            entry_idx = i
            break
    if entry_idx is None:
        return None

    s_entry = samples[entry_idx]
    move_up = s_entry.yes_mid > open_yes
    if move_up:
        side = "YES"
        entry_price = s_entry.yes_mid
    else:
        side = "NO"
        entry_price = 1.0 - s_entry.yes_mid

    if entry_price < 0.05 or entry_price > 0.95:
        return None  # no upside left / unreasonably extreme

    shares = 1.0 / max(entry_price, 0.001)

    # Walk forward looking for TP/SL
    for s in samples[entry_idx + 1:]:
        held_mid = s.yes_mid if side == "YES" else (1.0 - s.yes_mid)
        diff = (held_mid - entry_price) / max(entry_price, 1e-9)
        if diff >= TP_PCT:
            effective = max(0.01, held_mid * (1.0 - SLIPPAGE))
            pnl = shares * effective - 1.0
            return _Trade(
                market_slug=market.slug, entry_ts=s_entry.ts,
                entry_price=entry_price, side=side,
                exit_reason="take_profit", pnl_per_dollar=pnl,
                won=pnl > 0,
            )
        if diff <= -SL_PCT:
            effective = max(0.01, held_mid * (1.0 - SLIPPAGE))
            pnl = shares * effective - 1.0
            return _Trade(
                market_slug=market.slug, entry_ts=s_entry.ts,
                entry_price=entry_price, side=side,
                exit_reason="stop_loss", pnl_per_dollar=pnl,
                won=pnl > 0,
            )

    # No mid-period exit → settle at resolution
    won = (side == "YES") == market.yes_won
    pnl = shares * (1.0 if won else 0.0) - 1.0
    return _Trade(
        market_slug=market.slug, entry_ts=s_entry.ts,
        entry_price=entry_price, side=side,
        exit_reason="settled", pnl_per_dollar=pnl, won=won,
    )


@dataclass
class _Window:
    label: str
    n: int = 0
    wins: int = 0
    losses: int = 0
    paper_pnl: float = 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="Total replay span (will be split 3 ways)")
    parser.add_argument("--trigger-pct", type=float, default=TRIGGER_PCT_DEFAULT)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    span_total = args.days * 3
    bucket_size = args.days
    windows = [_Window(label=f"0-{bucket_size}d"),
               _Window(label=f"{bucket_size}-{bucket_size*2}d"),
               _Window(label=f"{bucket_size*2}-{span_total}d")]

    print(f"# BTC daily momentum backtest")
    print(f"config: trigger_pct={args.trigger_pct} TP={TP_PCT} SL={SL_PCT} slippage={SLIPPAGE}")
    print()

    skipped = 0
    for delta in range(1, span_total + 1):
        when = today - timedelta(days=delta)
        slug = _format_btc_slug(when)
        market = _resolve_btc_market(slug)
        if market is None:
            skipped += 1
            continue
        raw_trades = _fetch_trades(market.condition_id, max_pages=20)
        samples = _trades_to_yes_samples(raw_trades, market.yes_token)
        trade = _simulate_momentum(market, samples, trigger_pct=args.trigger_pct)
        if trade is None:
            continue
        # Bucket by age
        bucket = (delta - 1) // bucket_size
        if 0 <= bucket < 3:
            w = windows[bucket]
            w.n += 1
            if trade.won:
                w.wins += 1
            else:
                w.losses += 1
            w.paper_pnl += trade.pnl_per_dollar

    print("## Window results")
    print(f"  {'window':<10} {'n':>4} {'wins':>4} {'loss':>4} {'WR':>7} {'paper_pnl/$':>12}")
    print("  " + "-" * 50)
    pass_count = 0
    for w in windows:
        decided = w.wins + w.losses
        wr = (w.wins / decided) if decided else None
        wr_str = f"{wr*100:5.1f}%" if wr is not None else "  n/a"
        passes = wr is not None and wr >= 0.55 and w.paper_pnl > 0 and decided >= 15
        marker = "*" if passes else " "
        if passes:
            pass_count += 1
        print(f"  {marker} {w.label:<10} {w.n:>4} {w.wins:>4} {w.losses:>4} {wr_str:>7} ${w.paper_pnl:>+10.4f}")
    print()
    print(f"  * = WR ≥ 55% + PnL > 0 + n ≥ 15 (statistically meaningful pass)")
    print()
    if pass_count == 3:
        print("VERDICT ✅ momentum on BTC daily passes ALL 3 windows. Worth building an agent.")
    elif pass_count >= 1:
        print(f"VERDICT 🟡 passes {pass_count}/3 — REGIME-SPECIFIC. Do NOT build agent.")
    else:
        print(f"VERDICT ❌ fails all 3 windows. Momentum on BTC daily not viable as formulated.")

    if skipped:
        print(f"  (skipped {skipped} days where Gamma had no market or no resolution)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
