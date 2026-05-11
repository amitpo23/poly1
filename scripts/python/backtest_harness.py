#!/usr/bin/env python3
"""btc_daily backtest harness — v1.

Replays historical CLOB price-history of bitcoin-up-or-down-on-{date}
markets through `BtcDailyEngine.maybe_enter` and simulates exits at the
position_manager's MAINTAIN_TAKE_PROFIT_PCT / MAINTAIN_STOP_LOSS_PCT
thresholds. Outputs paper PnL across 7d / 14d / 30d windows.

One question, one answer: does the fade-the-move strategy have a
positive expected value worth scaling?

Scope (per advisor):
  - btc_daily ONLY in v1
  - 3 time windows (7d / 14d / 30d) — verify the answer is stable
  - read-only: never places orders, never writes to trade_log

Data source:
  - CLOB /prices-history with explicit start_ts/end_ts (works for
    resolved markets too)
  - Gamma /markets?slug=... for token_id lookup
  - BTC direction proxy: derive from binary YES mid changes (1-mid
    correlates with BTC outlook). The engine's `percent_change` only
    needs sign + magnitude over a 3-min window — the proxy preserves
    both.

Usage:
    docker exec poly1-position-manager python \\
      /app/scripts/python/backtest_harness.py --days 30 --json

Or to test a specific day:
    ... --start-date 2026-05-07 --end-date 2026-05-07
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.application.btc_daily import (  # noqa: E402
    BtcDailyConfig,
    BtcDailyEngine,
    format_btc_daily_slug,
)
from agents.application.trade_log import TradeLog  # noqa: E402

logging.basicConfig(
    level=logging.WARNING,  # quiet by default; --verbose flips to INFO
    format="%(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("backtest_harness")


# ---------------------------------------------------------------------
# Replay primitives
# ---------------------------------------------------------------------


@dataclass
class _PriceSample:
    ts: int       # unix seconds
    yes_mid: float  # 0..1


@dataclass
class _BacktestFeed:
    """Mock CoinbasePriceFeed driven by binary-mid changes.

    The engine asks `percent_change(window_sec)`. We answer with the
    DIRECTIONAL move in YES probability over that window, scaled to a
    plausible BTC % change. A 1¢ swing in YES probability roughly
    corresponds to a 0.05% BTC move on a typical 24h binary daily.
    The exact scaling doesn't matter — only that direction + magnitude
    rank-correlates with BTC.
    """
    samples: list[_PriceSample] = field(default_factory=list)
    cursor: int = 0   # index of "current" sample
    proxy_scale: float = 0.05  # 1¢ mid → 0.05% btc

    def update(self) -> Optional[float]:
        # Engine calls this each tick. Cursor advances elsewhere.
        if self.cursor >= len(self.samples):
            return None
        return self.samples[self.cursor].yes_mid

    def percent_change(self, window_sec: int) -> Optional[float]:
        if self.cursor < 1:
            return None
        cur = self.samples[self.cursor]
        target_ts = cur.ts - window_sec
        # find oldest sample at or before target_ts
        oldest = None
        for s in self.samples[: self.cursor + 1]:
            if s.ts <= target_ts:
                oldest = s
            else:
                break
        if oldest is None:
            # window not yet filled; use first sample if a least half full
            first = self.samples[0]
            if (cur.ts - first.ts) < window_sec / 2:
                return None
            oldest = first
        if oldest.yes_mid <= 0:
            return None
        # Sign: rising mid = market expects YES (BTC up) more → btc_move > 0
        delta_mid = cur.yes_mid - oldest.yes_mid
        return delta_mid * self.proxy_scale  # 1.0 swing → 5% BTC move


def _gamma_market_for_slug(slug: str) -> Optional[dict]:
    """Find market doc on Gamma by slug. Returns None if not found.

    Past markets only return when `closed=true` is set explicitly. Try
    open first, then closed.
    """
    for closed_flag in ("false", "true"):
        try:
            params = urllib.parse.urlencode({"slug": slug, "closed": closed_flag})
            url = f"https://gamma-api.polymarket.com/markets?{params}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 poly1-backtest"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if isinstance(data, list) and data:
                return data[0]
        except Exception as exc:
            logger.debug("gamma %s closed=%s failed: %s", slug, closed_flag, exc)
    return None


def _fetch_history(polymarket, token_id: str, start_ts: int, end_ts: int) -> list[_PriceSample]:
    """Pull full price-history for an active or resolved market."""
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
        logger.warning("prices-history failed for %s: %s", token_id[:18], exc)
        return []
    raw = resp.get("history", []) if isinstance(resp, dict) else []
    return [_PriceSample(ts=int(r["t"]), yes_mid=float(r["p"])) for r in raw]


# ---------------------------------------------------------------------
# Per-day backtest
# ---------------------------------------------------------------------


@dataclass
class _DayResult:
    date: str
    slug: str
    token_id: str
    yes_resolved: Optional[bool]   # True if YES won, False if NO, None if unknown
    samples: int
    entries: int
    paper_pnl_usdc: float = 0.0
    trades: list[dict] = field(default_factory=list)


def _simulate_day(
    polymarket,
    cfg: BtcDailyConfig,
    when: datetime,
    tp_pct: float,
    sl_pct: float,
    position_size: float,
) -> Optional[_DayResult]:
    """Replay one bitcoin-up-or-down market through BtcDailyEngine.

    Engine entries are taken at face value (live entry semantics). Exits
    are simulated at MAINTAIN-style thresholds: when subsequent ticks
    cross +tp_pct or -sl_pct relative to entry mid, that's the exit.
    No EOD; if neither threshold is hit by end of day, the position
    settles at terminal mid (1.0 if YES won, 0.0 if NO).
    """
    slug = format_btc_daily_slug(when)
    market = _gamma_market_for_slug(slug)
    if not market:
        return None
    import ast
    tok_ids = ast.literal_eval(market.get("clobTokenIds") or "[]")
    if not tok_ids:
        return None
    yes_tok = tok_ids[0]
    outcome_prices = market.get("outcomePrices") or "[]"
    try:
        prices = [float(p) for p in ast.literal_eval(outcome_prices)]
        yes_resolved = prices[0] >= 0.99 if market.get("closed") else None
    except Exception:
        yes_resolved = None

    start_ts = int(datetime(when.year, when.month, when.day, 0, 0, tzinfo=timezone.utc).timestamp())
    end_ts = start_ts + 86400
    samples = _fetch_history(polymarket, yes_tok, start_ts, end_ts)
    if len(samples) < 10:
        return None

    feed = _BacktestFeed(samples=samples)

    # Build engine with mocked Polymarket and trade_log. We run with
    # execute=True so `maybe_enter` returns a real OpenPosition (the
    # shadow path returns None which makes the harness blind to
    # entries). The Polymarket mock returns synthetic fill data; no
    # real CLOB call is made.
    pm = MagicMock()
    pm.client.get_midpoint = MagicMock(
        side_effect=lambda tok: {
            "mid": (
                feed.samples[feed.cursor].yes_mid
                if tok == yes_tok
                else 1.0 - feed.samples[feed.cursor].yes_mid
            )
        }
    )
    pm.client.get_balance_allowance = MagicMock(return_value={"balance": "0"})

    def _synth_execute(market_doc_tuple, recommendation):
        side = recommendation.side
        cur_mid = feed.samples[feed.cursor].yes_mid
        # In poly1 LLM convention: BUY → token_ids[0] at price; SELL → token_ids[1] at 1-price.
        held_yes = side.upper() == "BUY"
        fill_price = cur_mid if held_yes else (1.0 - cur_mid)
        return {
            "status": "matched",
            "order_avg_price_estimate": fill_price,
            "amount_usdc": recommendation.amount_usdc,
            "token_id": tok_ids[0] if held_yes else tok_ids[1],
            "outcome_traded": "Up" if held_yes else "Down",
        }
    pm.execute_market_order = MagicMock(side_effect=_synth_execute)

    log = MagicMock()
    log.has_filled_position_for_market.return_value = False
    log.insert_pending.return_value = 1
    log.new_cycle_id.return_value = "backtest_cycle"

    engine = BtcDailyEngine(
        polymarket=pm,
        trade_log=log,
        risk_gate=None,
        feed=feed,
        cfg=cfg,
        execute=True,  # so maybe_enter returns OpenPosition; pm is mocked
    )

    # Override _resolve_today_market — we pass the YES token directly.
    engine._market_cache[slug] = {
        "market_id": str(market.get("id", "")),
        "token_ids": tok_ids,
        "doc": MagicMock(),  # placeholder
    }
    # Patch _resolve_today_market to return our cached doc
    engine._resolve_today_market = lambda: engine._market_cache[slug]
    # Patch the candidate-mid pre-check to read directly from feed cursor
    # (the real one calls polymarket.client.get_midpoint which we mocked
    # but it asks for token_ids[0|1] — both legs. For YES side we hit
    # mid; for NO side we'd want 1-mid.)
    pm.client.get_midpoint.side_effect = lambda tok: (
        {"mid": feed.samples[feed.cursor].yes_mid}
        if tok == yes_tok
        else {"mid": 1.0 - feed.samples[feed.cursor].yes_mid}
    )

    entries: list[dict] = []
    open_pos = None
    open_entry_mid: Optional[float] = None

    for i in range(len(samples)):
        feed.cursor = i
        cur_mid = samples[i].yes_mid

        # First, simulate exit on any open paper position
        if open_pos is not None and open_entry_mid is not None:
            # The position holds either YES or NO depending on side
            # which the engine recorded in open_pos.outcome.
            # P&L per share if YES side: (cur_mid - entry_mid)
            # P&L per share if NO side: ((1-cur_mid) - (1-entry_mid)) = -(cur_mid - entry_mid)
            held_yes = open_pos.outcome.lower().startswith("y")
            held_price_now = cur_mid if held_yes else (1.0 - cur_mid)
            held_entry = open_entry_mid if held_yes else (1.0 - open_entry_mid)
            diff_pct = (held_price_now - held_entry) / max(held_entry, 0.001)
            exit_reason = None
            if diff_pct >= tp_pct:
                exit_reason = "take_profit"
            elif diff_pct <= -sl_pct:
                exit_reason = "stop_loss"
            if exit_reason:
                pnl = position_size * diff_pct
                entries[-1].update({
                    "exit_ts": samples[i].ts,
                    "exit_mid": cur_mid,
                    "exit_held_price": held_price_now,
                    "exit_reason": exit_reason,
                    "paper_pnl_usdc": round(pnl, 4),
                })
                open_pos = None
                open_entry_mid = None

        # Then, see if the engine would enter
        if open_pos is None:
            try:
                pos = engine.maybe_enter()
            except Exception as exc:
                logger.debug("entry check failed at i=%d: %s", i, exc)
                pos = None
            if pos is not None:
                held_yes = pos.outcome.lower().startswith("y")
                # The "entry mid" for P&L is the YES mid at entry
                open_entry_mid = cur_mid
                open_pos = pos
                entries.append({
                    "entry_ts": samples[i].ts,
                    "entry_mid": cur_mid,
                    "side": "YES" if held_yes else "NO",
                    "btc_move_at_entry": pos.btc_move_at_entry,
                })

    # If a position is still open at end-of-day, settle at terminal
    if open_pos is not None and open_entry_mid is not None and yes_resolved is not None:
        held_yes = open_pos.outcome.lower().startswith("y")
        terminal_held = (1.0 if yes_resolved else 0.0) if held_yes else (0.0 if yes_resolved else 1.0)
        held_entry = open_entry_mid if held_yes else (1.0 - open_entry_mid)
        diff_pct = (terminal_held - held_entry) / max(held_entry, 0.001)
        pnl = position_size * diff_pct
        entries[-1].update({
            "exit_ts": samples[-1].ts,
            "exit_mid": samples[-1].yes_mid,
            "exit_held_price": terminal_held,
            "exit_reason": "settled_yes_won" if yes_resolved else "settled_no_won",
            "paper_pnl_usdc": round(pnl, 4),
        })

    paper_pnl = sum(e.get("paper_pnl_usdc", 0.0) for e in entries)

    return _DayResult(
        date=when.strftime("%Y-%m-%d"),
        slug=slug,
        token_id=yes_tok,
        yes_resolved=yes_resolved,
        samples=len(samples),
        entries=len(entries),
        paper_pnl_usdc=round(paper_pnl, 4),
        trades=entries,
    )


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


def _summarize_window(days: list[_DayResult], label: str) -> dict:
    if not days:
        return {"label": label, "days": 0, "entries": 0, "paper_pnl_usdc": 0.0,
                "win_rate": None}
    entries = sum(d.entries for d in days)
    pnl = sum(d.paper_pnl_usdc for d in days)
    wins = sum(1 for d in days for t in d.trades if t.get("paper_pnl_usdc", 0) > 0)
    losses = sum(1 for d in days for t in d.trades if t.get("paper_pnl_usdc", 0) < 0)
    win_rate = (wins / (wins + losses)) if (wins + losses) else None
    return {
        "label": label,
        "days": len(days),
        "entries": entries,
        "paper_pnl_usdc": round(pnl, 4),
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
    }


def _print_text(results: list[_DayResult], summaries: list[dict], cfg_dict: dict) -> None:
    print("# btc_daily backtest harness — v1")
    print(f"config: {json.dumps(cfg_dict)}")
    print()
    print("## Per-day results")
    for d in sorted(results, key=lambda x: x.date, reverse=True):
        marker = "✓" if d.yes_resolved else ("✗" if d.yes_resolved is False else "?")
        print(
            f"  {d.date}  resolved={marker}  samples={d.samples:4d}  "
            f"entries={d.entries:2d}  pnl=${d.paper_pnl_usdc:+.4f}"
        )
    print()
    print("## Window summaries")
    for s in summaries:
        wr = f"{s['win_rate']*100:.1f}%" if s.get("win_rate") is not None else "n/a"
        print(
            f"  {s['label']:>4s}: days={s['days']:2d} entries={s['entries']:3d} "
            f"wins={s.get('wins', 0)}/losses={s.get('losses', 0)} "
            f"win_rate={wr}  paper_pnl=${s['paper_pnl_usdc']:+.4f}"
        )
    print()
    # Stability check
    pnls = [s["paper_pnl_usdc"] for s in summaries]
    if all(p > 0 for p in pnls):
        print("VERDICT: positive paper PnL across all windows — strategy worth keeping live.")
    elif all(p < 0 for p in pnls):
        print("VERDICT: negative paper PnL across all windows — strategy not worth scaling.")
    else:
        print("VERDICT: inconsistent across windows — no edge demonstrated, treat as luck.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30,
                        help="Number of past days to replay (default 30)")
    parser.add_argument("--start-date", default=None,
                        help="Only replay this single date (YYYY-MM-DD); overrides --days")
    parser.add_argument("--position-size", type=float, default=3.0,
                        help="Position size USDC for paper PnL math")
    parser.add_argument("--tp-pct", type=float, default=0.05,
                        help="Take profit threshold (default 0.05 = MAINTAIN_TAKE_PROFIT_PCT)")
    parser.add_argument("--sl-pct", type=float, default=0.07,
                        help="Stop loss threshold (default 0.07 = MAINTAIN_STOP_LOSS_PCT)")
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    from agents.polymarket.polymarket import Polymarket
    pm = Polymarket(live=True)
    cfg = BtcDailyConfig.from_env()

    results: list[_DayResult] = []
    if args.start_date:
        when = datetime.strptime(args.start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        r = _simulate_day(pm, cfg, when, args.tp_pct, args.sl_pct, args.position_size)
        if r:
            results.append(r)
    else:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        for delta in range(1, args.days + 1):
            when = today - timedelta(days=delta)
            r = _simulate_day(pm, cfg, when, args.tp_pct, args.sl_pct, args.position_size)
            if r:
                results.append(r)
                logger.info("day %s: entries=%d pnl=$%.4f",
                            r.date, r.entries, r.paper_pnl_usdc)
            else:
                logger.info("day %s: skipped (no data)", when.strftime("%Y-%m-%d"))

    # Sort newest first for windowing
    results.sort(key=lambda x: x.date, reverse=True)

    summaries = [
        _summarize_window(results[:7], "7d"),
        _summarize_window(results[:14], "14d"),
        _summarize_window(results[:30], "30d"),
    ]

    cfg_dict = {
        "trigger_pct": cfg.trigger_pct,
        "window_sec": cfg.window_sec,
        "tp_pct": args.tp_pct,
        "sl_pct": args.sl_pct,
        "position_size": args.position_size,
        "min_candidate_price": cfg.min_candidate_price,
        "trend_threshold_pct": cfg.trend_threshold_pct,
    }

    if args.json:
        print(json.dumps({
            "config": cfg_dict,
            "windows": summaries,
            "days": [{"date": d.date, "samples": d.samples,
                      "entries": d.entries, "pnl": d.paper_pnl_usdc,
                      "yes_resolved": d.yes_resolved}
                     for d in results],
        }, indent=2))
    else:
        _print_text(results, summaries, cfg_dict)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
