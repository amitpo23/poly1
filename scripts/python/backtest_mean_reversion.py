#!/usr/bin/env python3
"""mean_reversion backtest harness — v1.

Replays bitcoin-up-or-down-on-{date} markets through swarm's
``MeanReversionAgent`` decision logic, modeled in pure Python without
booting the swarm. Models bid-ask spread explicitly (the lesson from
scalper: ``backtest_scalper.py`` originally exit-at-mid → claimed 65%
WR → live lost money. Re-running with --slippage 0.02 showed every
threshold negative).

One question: does the fade-the-move strategy at MR's parameters
(0.3% trigger / 180s / TP=5¢ / SL=3¢ / 25min hold) have ≥65% win rate
with realistic spread costs?

Scope:
  - mean_reversion ONLY
  - 7d / 14d / 30d windows
  - read-only: never places orders, no DB writes
  - spread-based slippage (entry pays ask, exit receives bid)

Data:
  - CLOB /prices-history (mid only — same constraint as scalper harness)
  - Gamma /markets?slug=... for token_ids and resolution
  - BTC-direction proxy: yes-mid changes (same as btc_daily harness)

Usage:
    docker exec poly1-position-manager python \\
      /app/scripts/python/backtest_mean_reversion.py --days 30 --spread-cents 2.0
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
import sys
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
logger = logging.getLogger("backtest_mr")


# ---------------------------------------------------------------------
# MR config (mirrors swarm/config.py:MeanReversionConfig defaults)
# ---------------------------------------------------------------------


@dataclass
class _MRConfig:
    btc_move_pct_trigger: float = 0.003   # 0.3% in window
    btc_move_window_seconds: int = 180    # 3 min
    take_profit_cents: float = 5.0
    stop_loss_cents: float = 3.0
    max_hold_minutes: int = 25
    cooldown_seconds: int = 180
    skip_if_strong_trend: bool = True
    trend_window_minutes: int = 30
    trend_threshold_pct: float = 0.03
    position_size_usd: float = 1.0


# ---------------------------------------------------------------------
# Replay primitives
# ---------------------------------------------------------------------


@dataclass
class _PriceSample:
    ts: int
    yes_mid: float  # 0..1


def _gamma_market_for_slug(slug: str) -> Optional[dict]:
    for closed_flag in ("false", "true"):
        try:
            params = urllib.parse.urlencode({"slug": slug, "closed": closed_flag})
            url = f"https://gamma-api.polymarket.com/markets?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 poly1-mr-backtest"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if isinstance(data, list) and data:
                return data[0]
        except Exception as exc:
            logger.debug("gamma %s closed=%s failed: %s", slug, closed_flag, exc)
    return None


def _fetch_history(polymarket, token_id: str, start_ts: int, end_ts: int) -> list[_PriceSample]:
    from py_clob_client_v2.clob_types import PricesHistoryParams
    try:
        resp = polymarket.client.get_prices_history(
            params=PricesHistoryParams(market=token_id, start_ts=start_ts, end_ts=end_ts, fidelity=1)
        )
    except Exception as exc:
        logger.warning("prices-history failed for %s: %s", token_id[:18], exc)
        return []
    raw = resp.get("history", []) if isinstance(resp, dict) else []
    return [_PriceSample(ts=int(r["t"]), yes_mid=float(r["p"])) for r in raw]


def _percent_change(samples: list[_PriceSample], cur_idx: int, window_sec: int) -> Optional[float]:
    """MR proxy for BTC % change over window. Same trick as btc_daily harness:
    yes-mid swings rank-correlate with BTC direction. 1¢ swing ≈ 0.05% BTC."""
    if cur_idx < 1:
        return None
    cur = samples[cur_idx]
    target_ts = cur.ts - window_sec
    oldest = None
    for s in samples[: cur_idx + 1]:
        if s.ts <= target_ts:
            oldest = s
        else:
            break
    if oldest is None:
        first = samples[0]
        if (cur.ts - first.ts) < window_sec / 2:
            return None
        oldest = first
    if oldest.yes_mid <= 0:
        return None
    delta_mid = cur.yes_mid - oldest.yes_mid
    return delta_mid * 0.05


def _format_slug(when: datetime) -> str:
    return f"bitcoin-up-or-down-on-{when.strftime('%B').lower()}-{when.day}-{when.year}"


# ---------------------------------------------------------------------
# Per-day MR simulation
# ---------------------------------------------------------------------


@dataclass
class _DayResult:
    date: str
    slug: str
    yes_resolved: Optional[bool]
    samples: int
    entries: int
    paper_pnl_usdc: float = 0.0
    trades: list[dict] = field(default_factory=list)


def _simulate_day(
    polymarket,
    cfg: _MRConfig,
    when: datetime,
    spread_cents: float,
) -> Optional[_DayResult]:
    """Replay one day's BTC-up-or-down market through MR logic.

    Slippage model: spread is symmetric around mid.
      - Entry pays  mid_held_side + spread/2  (= ask of the side we buy)
      - Exit receives mid_held_side - spread/2  (= bid of the side we sell)
    Settlement at terminal $1 / $0 has no slippage (CTF redemption).
    """
    slug = _format_slug(when)
    market = _gamma_market_for_slug(slug)
    if not market:
        return None
    tok_ids = ast.literal_eval(market.get("clobTokenIds") or "[]")
    if len(tok_ids) < 2:
        return None
    yes_tok = tok_ids[0]
    op = market.get("outcomePrices") or "[]"
    try:
        prices = [float(p) for p in ast.literal_eval(op)]
        yes_resolved = prices[0] >= 0.99 if market.get("closed") else None
    except Exception:
        yes_resolved = None

    start_ts = int(datetime(when.year, when.month, when.day, tzinfo=timezone.utc).timestamp())
    end_ts = start_ts + 86400
    samples = _fetch_history(polymarket, yes_tok, start_ts, end_ts)
    if len(samples) < 10:
        return None

    half_spread = spread_cents / 200.0  # cents → fraction (1¢ = 0.01)

    entries: list[dict] = []
    open_trade: Optional[dict] = None
    last_trade_ts = 0

    for i, s in enumerate(samples):
        cur_mid = s.yes_mid

        # ---- exit check on open trade ----
        if open_trade is not None:
            held_yes = open_trade["side"] == "YES"
            held_mid = cur_mid if held_yes else (1.0 - cur_mid)
            # Sell at bid (mid - half_spread)
            current_sell_price = max(0.0, held_mid - half_spread)
            entry_price_cents = open_trade["entry_price_cents"]
            current_price_cents = current_sell_price * 100

            elapsed_sec = s.ts - open_trade["entry_ts"]
            tp_hit = current_price_cents >= entry_price_cents + cfg.take_profit_cents
            sl_hit = current_price_cents <= entry_price_cents - cfg.stop_loss_cents
            timeout = elapsed_sec >= cfg.max_hold_minutes * 60

            if tp_hit or sl_hit or timeout:
                reason = "take_profit" if tp_hit else ("stop_loss" if sl_hit else "timeout")
                shares = open_trade["shares"]
                proceeds = shares * (current_price_cents / 100.0)
                cost = open_trade["cost_usdc"]
                pnl = proceeds - cost
                open_trade.update({
                    "exit_ts": s.ts,
                    "exit_price_cents": round(current_price_cents, 2),
                    "exit_reason": reason,
                    "paper_pnl_usdc": round(pnl, 4),
                })
                entries.append(open_trade)
                last_trade_ts = s.ts
                open_trade = None

        # ---- entry check ----
        if open_trade is None:
            if last_trade_ts and (s.ts - last_trade_ts) < cfg.cooldown_seconds:
                continue

            btc_move = _percent_change(samples, i, cfg.btc_move_window_seconds)
            if btc_move is None or abs(btc_move) < cfg.btc_move_pct_trigger:
                continue

            if cfg.skip_if_strong_trend:
                trend = _percent_change(samples, i, cfg.trend_window_minutes * 60)
                if trend is not None and abs(trend) > cfg.trend_threshold_pct:
                    if (btc_move > 0) == (trend > 0):
                        continue  # don't fight long trend

            # Fade: BTC up → buy NO; BTC down → buy YES
            side = "NO" if btc_move > 0 else "YES"
            held_mid = cur_mid if side == "YES" else (1.0 - cur_mid)
            # Pay ask = mid + half_spread
            entry_price = min(1.0, held_mid + half_spread)
            entry_price_cents = entry_price * 100

            # Skip if entry side too cheap to fade meaningfully (mirrors btc_daily floor)
            # MR doesn't have this filter explicitly — keeping it commented
            # if entry_price_cents < 30: continue

            cost_usdc = cfg.position_size_usd
            shares = cost_usdc / max(entry_price, 0.001)

            open_trade = {
                "entry_ts": s.ts,
                "side": side,
                "entry_mid": cur_mid,
                "entry_price_cents": round(entry_price_cents, 2),
                "shares": round(shares, 4),
                "cost_usdc": cost_usdc,
                "btc_move_at_entry": btc_move,
            }

    # End-of-day settlement for any still-open trade
    if open_trade is not None and yes_resolved is not None:
        held_yes = open_trade["side"] == "YES"
        terminal = (1.0 if yes_resolved else 0.0) if held_yes else (0.0 if yes_resolved else 1.0)
        # CTF redemption — no slippage; you get $1 per winning share, $0 per loser
        proceeds = open_trade["shares"] * terminal
        cost = open_trade["cost_usdc"]
        pnl = proceeds - cost
        open_trade.update({
            "exit_ts": samples[-1].ts,
            "exit_price_cents": round(terminal * 100, 2),
            "exit_reason": "settled_yes_won" if yes_resolved else "settled_no_won",
            "paper_pnl_usdc": round(pnl, 4),
        })
        entries.append(open_trade)
        open_trade = None

    paper_pnl = sum(e.get("paper_pnl_usdc", 0.0) for e in entries)

    return _DayResult(
        date=when.strftime("%Y-%m-%d"),
        slug=slug,
        yes_resolved=yes_resolved,
        samples=len(samples),
        entries=len(entries),
        paper_pnl_usdc=round(paper_pnl, 4),
        trades=entries,
    )


# ---------------------------------------------------------------------
# Window summaries + CLI
# ---------------------------------------------------------------------


def _summarize(days: list[_DayResult], label: str) -> dict:
    if not days:
        return {"label": label, "days": 0, "entries": 0, "paper_pnl_usdc": 0.0, "win_rate": None}
    entries = sum(d.entries for d in days)
    pnl = sum(d.paper_pnl_usdc for d in days)
    wins = sum(1 for d in days for t in d.trades if t.get("paper_pnl_usdc", 0) > 0)
    losses = sum(1 for d in days for t in d.trades if t.get("paper_pnl_usdc", 0) < 0)
    wr = (wins / (wins + losses)) if (wins + losses) else None
    return {
        "label": label,
        "days": len(days),
        "entries": entries,
        "paper_pnl_usdc": round(pnl, 4),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wr, 4) if wr is not None else None,
    }


def _print_text(results: list[_DayResult], summaries: list[dict], cfg_dict: dict) -> None:
    print("# mean_reversion backtest harness — v1")
    print(f"config: {json.dumps(cfg_dict)}")
    print()
    print("## Per-day results")
    for d in sorted(results, key=lambda x: x.date, reverse=True):
        marker = "✓" if d.yes_resolved else ("✗" if d.yes_resolved is False else "?")
        print(f"  {d.date}  resolved={marker}  samples={d.samples:4d}  entries={d.entries:2d}  pnl=${d.paper_pnl_usdc:+.4f}")
    print()
    print("## Window summaries")
    for s in summaries:
        wr = f"{s['win_rate']*100:.1f}%" if s.get("win_rate") is not None else "n/a"
        print(f"  {s['label']:>4s}: days={s['days']:2d} entries={s['entries']:3d} "
              f"wins={s.get('wins', 0)}/losses={s.get('losses', 0)} "
              f"win_rate={wr}  paper_pnl=${s['paper_pnl_usdc']:+.4f}")
    print()
    pnls = [s["paper_pnl_usdc"] for s in summaries]
    wrs = [s.get("win_rate") for s in summaries if s.get("win_rate") is not None]
    if all(p > 0 for p in pnls) and all(w >= 0.65 for w in wrs):
        print("VERDICT: ≥65% win rate AND positive PnL across all windows — passes user gate.")
    elif all(p > 0 for p in pnls):
        print("VERDICT: positive PnL but win rate < 65% — does not meet user gate.")
    elif all(p < 0 for p in pnls):
        print("VERDICT: negative paper PnL across all windows — strategy not worth scaling.")
    else:
        print("VERDICT: inconsistent across windows — no edge demonstrated.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--spread-cents", type=float, default=2.0,
                        help="Bid-ask spread in cents (default 2.0). Entry pays mid + spread/2; exit receives mid - spread/2.")
    parser.add_argument("--position-size", type=float, default=1.0)
    parser.add_argument("--trigger-pct", type=float, default=0.003)
    parser.add_argument("--tp-cents", type=float, default=5.0)
    parser.add_argument("--sl-cents", type=float, default=3.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    from agents.polymarket.polymarket import Polymarket
    pm = Polymarket(live=True)

    cfg = _MRConfig(
        btc_move_pct_trigger=args.trigger_pct,
        take_profit_cents=args.tp_cents,
        stop_loss_cents=args.sl_cents,
        position_size_usd=args.position_size,
    )

    results: list[_DayResult] = []
    if args.start_date:
        when = datetime.strptime(args.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        r = _simulate_day(pm, cfg, when, args.spread_cents)
        if r:
            results.append(r)
    else:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        for delta in range(1, args.days + 1):
            when = today - timedelta(days=delta)
            r = _simulate_day(pm, cfg, when, args.spread_cents)
            if r:
                results.append(r)
                logger.info("day %s: entries=%d pnl=$%.4f", r.date, r.entries, r.paper_pnl_usdc)

    results.sort(key=lambda x: x.date, reverse=True)

    summaries = [
        _summarize(results[:7], "7d"),
        _summarize(results[:14], "14d"),
        _summarize(results[:30], "30d"),
    ]

    cfg_dict = {
        "trigger_pct": cfg.btc_move_pct_trigger,
        "window_sec": cfg.btc_move_window_seconds,
        "tp_cents": cfg.take_profit_cents,
        "sl_cents": cfg.stop_loss_cents,
        "max_hold_min": cfg.max_hold_minutes,
        "spread_cents": args.spread_cents,
        "position_size": cfg.position_size_usd,
    }

    if args.json:
        print(json.dumps({
            "config": cfg_dict,
            "windows": summaries,
            "days": [{"date": d.date, "samples": d.samples, "entries": d.entries,
                      "pnl": d.paper_pnl_usdc, "yes_resolved": d.yes_resolved}
                     for d in results],
        }, indent=2))
    else:
        _print_text(results, summaries, cfg_dict)

    return 0


if __name__ == "__main__":
    sys.exit(main())
