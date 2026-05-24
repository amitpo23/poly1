"""Per-source win-rate calibrator using actual TRADE outcomes.

The bot has 21 entry agents producing approvals. The meta_brain's
WinRateAdvisor reads `brain_decisions.outcome_status` (now populated
per Day 1), but those rows are PER-CANDIDATE — a single market can
produce 100+ approvals before one trade actually fires. Using per-
candidate winrate biases toward markets with high approval volume,
not toward markets with positive edge.

This calibrator works at the TRADE level instead: walks the `trades`
table (status IN closed_*/resolved_*), joins each close to the
brain_decision that triggered the open (via market_id + token_id +
ts ordering), and aggregates:

- per signal_source
- per market_type
- per side (BUY/SELL)
- per price band (0.40-0.49, 0.50-0.55, etc.)

Returns calibrated P(win) with sample size + Wilson lower bound for
each segment. Caller (bayesian_aggregator in Day 3) blends these
into a single P(win | candidate).

This is the foundation of the operator's "internal vaccination on
success probability" — for any new opportunity, we know the
empirical track record of every signal/market/side/price feature
that defines it.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


WIN_STATUSES = frozenset({
    "closed_take_profit",
    "resolved_yes",
    "resolved_no",
    "resolved_skipped_no",
})
LOSS_STATUSES = frozenset({
    "closed_stop_loss",
    "closed_timeout",
    "resolved_loss",
})


@dataclass(frozen=True)
class CalibrationStat:
    """Win rate + magnitude for a single (key, segment) pair.

    Magnitude fields (avg_win_pct, avg_loss_pct) make this RR-aware:
    a 36% winrate with avg_win=+6% and avg_loss=-1% has E[return] =
    +1.5% per trade, which is what an expected-value gate should care
    about. The pure-winrate Wilson lower bound misses this.
    """

    key: str
    segment: str
    wins: int
    losses: int
    sum_win_pnl_usdc: float = 0.0
    sum_loss_pnl_usdc: float = 0.0

    @property
    def total(self) -> int:
        return self.wins + self.losses

    @property
    def winrate(self) -> Optional[float]:
        if self.total == 0:
            return None
        return self.wins / self.total

    @property
    def wilson_lower(self) -> Optional[float]:
        """Wilson score interval lower bound at 95% confidence.

        Conservative point-estimate for sources with few samples.
        """
        n = self.total
        if n == 0:
            return None
        z = 1.96
        p = self.wins / n
        denom = 1 + z * z / n
        center = (p + z * z / (2 * n)) / denom
        half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
        return max(0.0, center - half)

    @property
    def avg_win_usdc(self) -> Optional[float]:
        if self.wins == 0:
            return None
        return self.sum_win_pnl_usdc / self.wins

    @property
    def avg_loss_usdc(self) -> Optional[float]:
        if self.losses == 0:
            return None
        return self.sum_loss_pnl_usdc / self.losses

    def expected_value_per_trade(self, position_size_usdc: float = 1.0) -> Optional[float]:
        """E[return per trade] using calibrated p_win + avg magnitudes.

        Returns None when insufficient data (no wins OR no losses) — at
        n<5 of either side the magnitude estimate is too noisy.
        """
        if self.total == 0:
            return None
        if self.avg_win_usdc is None or self.avg_loss_usdc is None:
            return None
        p = self.winrate or 0.0
        # avg_loss_usdc is negative (losing trades); using it as-is.
        return p * self.avg_win_usdc + (1 - p) * self.avg_loss_usdc

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "segment": self.segment,
            "wins": self.wins,
            "losses": self.losses,
            "total": self.total,
            "winrate": round(self.winrate, 4) if self.winrate is not None else None,
            "wilson_lower": (
                round(self.wilson_lower, 4) if self.wilson_lower is not None else None
            ),
            "avg_win_usdc": (
                round(self.avg_win_usdc, 4) if self.avg_win_usdc is not None else None
            ),
            "avg_loss_usdc": (
                round(self.avg_loss_usdc, 4) if self.avg_loss_usdc is not None else None
            ),
            "ev_per_trade_usdc": (
                round(self.expected_value_per_trade(), 4)
                if self.expected_value_per_trade() is not None
                else None
            ),
            "sum_win_pnl_usdc": round(self.sum_win_pnl_usdc, 4),
            "sum_loss_pnl_usdc": round(self.sum_loss_pnl_usdc, 4),
        }


def _price_band(price: float) -> str:
    """Map an entry price to a discrete band — six buckets we care about
    based on the empirical edge zones observed during 2026-05-24:

    <0.40, 0.40-0.49 (the proven BUY band), 0.50-0.55 (where Round 4 fired),
    0.55-0.65, 0.65-0.75, 0.75+.
    """
    if price is None or price <= 0 or price >= 1:
        return "invalid"
    if price < 0.40:
        return "<0.40"
    if price < 0.50:
        return "0.40-0.49"
    if price < 0.55:
        return "0.50-0.54"
    if price < 0.65:
        return "0.55-0.64"
    if price < 0.75:
        return "0.65-0.74"
    return "0.75+"


def _collect_closes(conn: sqlite3.Connection, days: int) -> list[dict]:
    """One row per actually-fired close trade in the window."""
    placeholders = ",".join("?" for _ in WIN_STATUSES | LOSS_STATUSES)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
    cursor = conn.execute(
        f"""
        SELECT t.id AS close_id, t.ts AS close_ts, t.market_id, t.token_id,
               t.status, t.response_json
        FROM trades t
        WHERE t.status IN ({placeholders}) AND t.ts >= ?
        ORDER BY t.ts
        """,
        (*WIN_STATUSES, *LOSS_STATUSES, cutoff),
    )
    out = []
    for row in cursor.fetchall():
        try:
            resp = json.loads(row["response_json"] or "{}")
        except (TypeError, ValueError):
            resp = {}
        out.append(
            {
                "close_id": row["close_id"],
                "close_ts": row["close_ts"],
                "market_id": row["market_id"],
                "token_id": row["token_id"],
                "status": row["status"],
                "pnl_usdc_real": resp.get("pnl_usdc_real"),
                "is_win": row["status"] in WIN_STATUSES,
            }
        )
    return out


def _find_originating_decision(
    conn: sqlite3.Connection,
    *,
    market_id: str,
    token_id: Optional[str],
    close_ts: str,
    max_age_hours: int = 48,
) -> Optional[dict]:
    """Find the latest brain_decision row that triggered this close.

    Heuristic: most recent approved=1 row for the same market+token,
    within max_age_hours BEFORE the close. This is the same matching
    rule as backfill_brain_decisions_outcomes.
    """
    try:
        close_dt = datetime.fromisoformat(close_ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    floor_iso = (close_dt - timedelta(hours=int(max_age_hours))).isoformat()
    # IMPORTANT: filter to entry decisions only. Position manager logs
    # `decision_type='exit'` rows on close — those would otherwise match
    # the close itself and confuse the calibration.
    if token_id:
        row = conn.execute(
            """
            SELECT id, signal_source, action, market_type, features_json
            FROM brain_decisions
            WHERE market_id = ? AND token_id = ?
              AND approved = 1 AND decision_type = 'entry'
              AND action IN ('BUY', 'SELL')
              AND ts >= ? AND ts <= ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(market_id), str(token_id), floor_iso, close_ts),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT id, signal_source, action, market_type, features_json
            FROM brain_decisions
            WHERE market_id = ?
              AND approved = 1 AND decision_type = 'entry'
              AND action IN ('BUY', 'SELL')
              AND ts >= ? AND ts <= ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(market_id), floor_iso, close_ts),
        ).fetchone()
    if row is None:
        return None
    try:
        features = json.loads(row["features_json"] or "{}")
    except (TypeError, ValueError):
        features = {}
    entry_price = (
        features.get("selected_entry_price")
        or features.get("entry_price")
        or features.get("yes_price")
    )
    return {
        "decision_id": row["id"],
        "signal_source": row["signal_source"] or "unknown",
        "action": row["action"] or "?",
        "market_type": row["market_type"] or "?",
        "entry_price": float(entry_price) if entry_price else None,
    }


def calibrate(db_path: str, *, days: int = 30, max_age_hours: int = 48) -> dict:
    """Walk every close, find its originating decision, accumulate stats."""
    with sqlite3.connect(db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        closes = _collect_closes(conn, days=days)
        # Group counters: key=segment_type:value, [wins, losses, sum_win_pnl, sum_loss_pnl]
        per_source: dict[str, list[float]] = {}
        per_market_type: dict[str, list[float]] = {}
        per_action: dict[str, list[float]] = {}
        per_band: dict[str, list[float]] = {}
        per_source_band: dict[str, list[float]] = {}
        matched = 0
        unmatched = 0
        for close in closes:
            decision = _find_originating_decision(
                conn,
                market_id=close["market_id"],
                token_id=close["token_id"],
                close_ts=close["close_ts"],
                max_age_hours=max_age_hours,
            )
            if decision is None:
                unmatched += 1
                continue
            matched += 1
            is_win = close["is_win"]
            source = decision["signal_source"]
            mtype = decision["market_type"]
            action = decision["action"]
            band = _price_band(decision["entry_price"])
            # Magnitude (PnL in USDC) — used for RR-aware EV calc.
            pnl = close.get("pnl_usdc_real")
            try:
                pnl_f = float(pnl) if pnl is not None else 0.0
            except (TypeError, ValueError):
                pnl_f = 0.0
            for bucket, key in (
                (per_source, source),
                (per_market_type, mtype),
                (per_action, action),
                (per_band, band),
                (per_source_band, f"{source}|{band}"),
            ):
                # [wins, losses, sum_win_pnl, sum_loss_pnl]
                bucket.setdefault(key, [0, 0, 0.0, 0.0])
                if is_win:
                    bucket[key][0] += 1
                    bucket[key][2] += pnl_f
                else:
                    bucket[key][1] += 1
                    bucket[key][3] += pnl_f  # pnl_f is negative for losses

        def _build(stats: dict, segment: str) -> list[dict]:
            out = []
            for key, (wins, losses, sum_win, sum_loss) in stats.items():
                stat = CalibrationStat(
                    key=key, segment=segment,
                    wins=int(wins), losses=int(losses),
                    sum_win_pnl_usdc=float(sum_win),
                    sum_loss_pnl_usdc=float(sum_loss),
                )
                out.append(stat.as_dict())
            return sorted(out, key=lambda x: -x["total"])

    return {
        "days": days,
        "max_age_hours": max_age_hours,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_closes": len(closes),
        "matched": matched,
        "unmatched": unmatched,
        "per_signal_source": _build(per_source, "signal_source"),
        "per_market_type": _build(per_market_type, "market_type"),
        "per_action": _build(per_action, "action"),
        "per_price_band": _build(per_band, "price_band"),
        "per_source_band": _build(per_source_band, "source|band"),
    }


def lookup_winrate(
    calibration: dict,
    *,
    signal_source: Optional[str] = None,
    market_type: Optional[str] = None,
    action: Optional[str] = None,
    price_band: Optional[str] = None,
    min_samples: int = 5,
) -> Optional[CalibrationStat]:
    """Best-fit winrate lookup for a candidate.

    Tries the most specific combination first (source|band), then falls
    back to broader segments. Returns the FIRST segment with at least
    `min_samples` total observations.
    """
    def _find(segment_list: list[dict], key: str) -> Optional[CalibrationStat]:
        for entry in segment_list:
            if entry["key"] == key and entry["total"] >= min_samples:
                return CalibrationStat(
                    key=entry["key"],
                    segment=entry["segment"],
                    wins=entry["wins"],
                    losses=entry["losses"],
                    sum_win_pnl_usdc=entry.get("sum_win_pnl_usdc", 0.0) or 0.0,
                    sum_loss_pnl_usdc=entry.get("sum_loss_pnl_usdc", 0.0) or 0.0,
                )
        return None

    if signal_source and price_band:
        s = _find(calibration["per_source_band"], f"{signal_source}|{price_band}")
        if s:
            return s
    if signal_source:
        s = _find(calibration["per_signal_source"], signal_source)
        if s:
            return s
    if price_band:
        s = _find(calibration["per_price_band"], price_band)
        if s:
            return s
    if action:
        s = _find(calibration["per_action"], action)
        if s:
            return s
    if market_type:
        s = _find(calibration["per_market_type"], market_type)
        if s:
            return s
    return None


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/trade_log.db")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--max-age-hours", type=int, default=48)
    parser.add_argument("--out", default=None, help="Write JSON to file (default: stdout)")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"db not found: {args.db}", file=__import__("sys").stderr)
        return 2

    result = calibrate(args.db, days=args.days, max_age_hours=args.max_age_hours)
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n")
        print(f"wrote {args.out}", file=__import__("sys").stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
