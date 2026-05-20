#!/usr/bin/env python3
"""scout.py — hourly scanner that surfaces market opportunities.

Per `docs/SESSION_2026-05-10_SCOUT_PLAN.md`: this is read-only,
opportunity-surfacing infrastructure. **It does NOT auto-activate
any agent**, modify .env, or change BOT_MODE. Workflow:

  scout (this script, hourly cron) →
    writes ranked candidates to `scout_opportunities` table →
  state_watcher picks up new rows and alerts user →
  human reviews, runs backtest_market_sweep on candidate →
  if backtest passes 55% WR with split-test stability →
  human flips BOT_MODE=live for that one strategy

Each cycle:
  1. Scan Gamma /markets ordered by volume desc.
  2. Filter into per-strategy buckets:
     - nothing_happens: "Will X happen by Y", NO ≤ 0.30, vol ≥ $10k
     - market_maker:    mid 25-75%, spread ≥ 2¢, liq ≥ $50k
     - mean_reversion:  daily up/down crypto, mid 30-70%, vol ≥ $10k
  3. For each candidate, pull Tavily news (slug as query).
  4. Score by replayable heuristics — *not* predicted WR.
  5. Insert into `scout_opportunities` (deduped by slug+strategy+date).

Heuristics, by strategy:
  - nothing_happens: cheaper-NO bonus (lower price = better R/R),
    days-to-resolution bonus (longer = more time for nothing to happen),
    volume bonus (higher vol = thinner spread effective), news bonus
    (Tavily mentions = actively discussed → market may be mispriced).
  - market_maker: spread bonus (wider = more capture), volume bonus,
    proximity-to-50% bonus.
  - mean_reversion: standard daily; mid in 30-70% band only.

Usage:
    docker exec poly1-position-manager python /app/scripts/python/scout.py

Cron via /loop:
    /loop 1h scout
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.application.research_committee import MarketContext, ResearchCommittee  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("scout")


SCHEMA = """
CREATE TABLE IF NOT EXISTS scout_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    market_id TEXT,
    strategy_match TEXT NOT NULL,
    score REAL NOT NULL,
    yes_price REAL,
    no_price REAL,
    spread_cents REAL,
    volume_24h REAL,
    liquidity REAL,
    end_date TEXT,
    days_to_end REAL,
    news_count INTEGER DEFAULT 0,
    top_news_headline TEXT,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_scout_ts ON scout_opportunities(ts);
CREATE INDEX IF NOT EXISTS ix_scout_strategy ON scout_opportunities(strategy_match, ts);
CREATE UNIQUE INDEX IF NOT EXISTS ux_scout_dedupe
    ON scout_opportunities(market_slug, strategy_match, date(ts));

-- Phase B (forward data collector): every scout cycle records the
-- current price for every candidate that passed filters, so after
-- N days we have our own time-series for resolved markets — bypassing
-- CLOB's missing price-history on speculative political markets.
CREATE TABLE IF NOT EXISTS price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    market_id TEXT,
    yes_price REAL,
    no_price REAL,
    spread_cents REAL,
    volume_24h REAL,
    liquidity REAL
);
CREATE INDEX IF NOT EXISTS ix_snap_slug_ts ON price_snapshots(market_slug, ts);
CREATE INDEX IF NOT EXISTS ix_snap_ts ON price_snapshots(ts);

-- TradingAgents-inspired read-only research layer. Reports are advisory:
-- approved_for_live is intentionally stored and expected to be 0 until a
-- separate backtest + human activation flow promotes a strategy.
CREATE TABLE IF NOT EXISTS research_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    market_id TEXT,
    strategy_match TEXT NOT NULL,
    final_action TEXT NOT NULL,
    final_score REAL NOT NULL,
    risk_score REAL NOT NULL,
    confidence REAL NOT NULL,
    approved_for_backtest INTEGER NOT NULL,
    approved_for_live INTEGER NOT NULL,
    features_json TEXT NOT NULL,
    assessments_json TEXT NOT NULL,
    conclusion TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_research_reports_ts ON research_reports(created_ts);
CREATE INDEX IF NOT EXISTS ix_research_reports_strategy ON research_reports(strategy_match, created_ts);
CREATE UNIQUE INDEX IF NOT EXISTS ux_research_reports_dedupe
    ON research_reports(market_slug, strategy_match, date(created_ts));
"""


# ---------------------------------------------------------------------
# Gamma scan
# ---------------------------------------------------------------------


def _gamma_markets(limit: int = 500) -> list[dict]:
    """Fetch active open markets from Gamma, ordered by volume desc."""
    params = urllib.parse.urlencode({
        "closed": "false",
        "active": "true",
        "limit": limit,
        "order": "volume24hr",
        "ascending": "false",
    })
    url = f"https://gamma-api.polymarket.com/markets?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "poly1-scout"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        logger.warning("gamma fetch failed: %s", exc)
        return []
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------
# Per-strategy filters + scoring
# ---------------------------------------------------------------------


def _market_basics(m: dict) -> Optional[dict]:
    """Extract common fields with type coercion. Returns None if invalid."""
    try:
        op_raw = m.get("outcomePrices") or "[]"
        prices = json.loads(op_raw) if isinstance(op_raw, str) else op_raw
        if not prices or len(prices) < 2:
            return None
        yes_price = float(prices[0])
        no_price = float(prices[1])
        bb = m.get("bestBid")
        ba = m.get("bestAsk")
        spread = (float(ba) - float(bb)) * 100 if bb is not None and ba is not None else None
        end_iso = m.get("endDate") or m.get("end_date") or ""
        end_ts = None
        days_to_end = None
        if end_iso:
            try:
                end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                end_ts = int(end_dt.timestamp())
                days_to_end = (end_ts - int(time.time())) / 86400
            except Exception:
                pass
        return {
            "slug": m.get("slug") or "",
            "question": m.get("question") or "",
            "market_id": str(m.get("conditionId") or m.get("condition_id") or m.get("id") or ""),
            "yes_price": yes_price,
            "no_price": no_price,
            "spread": spread,
            "vol24": float(m.get("volume24hr") or 0),
            "liquidity": float(m.get("liquidity") or 0),
            "end_iso": end_iso,
            "days_to_end": days_to_end,
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _filter_nothing_happens(b: dict) -> Optional[float]:
    """Return score (0..1) if market fits nothing_happens, else None.

    Filter: "Will X happen by Y" speculative + cheap NO (≤30¢) +
    decent volume + at least 7 days to resolution.
    """
    slug = b["slug"].lower()
    if not (slug.startswith("will-") or "happen-by" in slug or "by-end-of" in slug or "by-december" in slug or "by-june" in slug or "by-march" in slug):
        return None
    if b["no_price"] > 0.30 or b["no_price"] < 0.05:
        return None
    if b["vol24"] < 10_000 or b["liquidity"] < 50_000:
        return None
    if b["days_to_end"] is None or b["days_to_end"] < 7:
        return None
    # Heuristic score (replayable): cheaper NO + more days + more vol = better
    cheap_score = (0.30 - b["no_price"]) / 0.25  # 0 at 0.30, 1 at 0.05
    time_score = min(1.0, b["days_to_end"] / 90)  # cap at 90 days
    vol_score = min(1.0, b["vol24"] / 100_000)
    return round(0.5 * cheap_score + 0.3 * time_score + 0.2 * vol_score, 3)


def _filter_market_maker(b: dict) -> Optional[float]:
    """Filter: mid 25-75%, spread ≥2¢, liq ≥$50k, vol ≥$10k."""
    if b["spread"] is None or b["spread"] < 2.0:
        return None
    if b["yes_price"] < 0.25 or b["yes_price"] > 0.75:
        return None
    if b["vol24"] < 10_000 or b["liquidity"] < 50_000:
        return None
    if b["days_to_end"] is None or b["days_to_end"] < 1:
        return None
    spread_score = min(1.0, (b["spread"] - 1.5) / 3.5)  # 0 at 1.5¢, 1 at 5.0¢
    proximity = 1.0 - 2 * abs(b["yes_price"] - 0.5)  # 1 at 50%, 0 at 0%/100%
    vol_score = min(1.0, b["vol24"] / 100_000)
    return round(0.4 * spread_score + 0.3 * proximity + 0.3 * vol_score, 3)


def _filter_mean_reversion(b: dict) -> Optional[float]:
    """Filter: daily up/down crypto, mid 30-70%."""
    slug = b["slug"].lower()
    if not any(k in slug for k in ["bitcoin-up-or-down", "ethereum-up-or-down", "solana-up-or-down"]):
        return None
    if b["yes_price"] < 0.30 or b["yes_price"] > 0.70:
        return None
    if b["vol24"] < 5_000:
        return None
    # Reject markets without parseable end date — downstream `reason` f-string
    # uses days_to_end with `:.1f` formatting which crashes on None.
    if b["days_to_end"] is None or b["days_to_end"] < 0:
        return None
    proximity = 1.0 - 2 * abs(b["yes_price"] - 0.5)
    vol_score = min(1.0, b["vol24"] / 50_000)
    return round(0.6 * proximity + 0.4 * vol_score, 3)


STRATEGY_FILTERS = {
    "nothing_happens": _filter_nothing_happens,
    "market_maker": _filter_market_maker,
    "mean_reversion": _filter_mean_reversion,
}


# ---------------------------------------------------------------------
# Tavily news enrichment
# ---------------------------------------------------------------------


def _tavily_news(query: str, max_results: int = 5) -> list[dict]:
    """Fetch budgeted Tavily headlines. Returns [] silently on error."""
    try:
        from agents.application.tavily import tavily_headlines

        headlines = tavily_headlines(query, max_results=max_results, timeout=15)
    except Exception as exc:
        logger.debug("tavily failed for %s: %s", query[:30], exc)
        return []
    return [
        {"title": line.lstrip("- ").strip()}
        for line in headlines.splitlines()
        if line.strip()
    ]


def _slug_to_query(slug: str) -> str:
    """Turn a market slug into a search query."""
    return slug.replace("-", " ")


# ---------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------


def _open_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _persist(conn: sqlite3.Connection, row: dict) -> bool:
    """Insert opportunity; True if a new row was actually inserted (not deduped).

    NOTE: ``conn.total_changes`` is cumulative since connection open — using
    it here would report True on every call after the first successful insert.
    Use ``cursor.rowcount`` instead, which is per-statement.
    """
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO scout_opportunities
                (ts, market_slug, market_id, strategy_match, score,
                 yes_price, no_price, spread_cents, volume_24h, liquidity,
                 end_date, days_to_end, news_count, top_news_headline, reason)
            VALUES (?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?)
            """,
            (
                row["ts"], row["slug"], row.get("market_id"),
                row["strategy"], row["score"],
                row.get("yes_price"), row.get("no_price"), row.get("spread"),
                row.get("vol24"), row.get("liquidity"),
                row.get("end_iso"), row.get("days_to_end"),
                row.get("news_count", 0), row.get("top_news_headline"),
                row.get("reason"),
            ),
        )
        return cur.rowcount > 0
    except sqlite3.Error as exc:
        logger.warning("scout persist failed for %s/%s: %s", row.get("slug"), row.get("strategy"), exc)
        return False


def _research_context(row: dict) -> MarketContext:
    return MarketContext(
        market_slug=row["slug"],
        market_id=row.get("market_id"),
        strategy=row["strategy"],
        score=float(row.get("score") or 0.0),
        yes_price=row.get("yes_price"),
        no_price=row.get("no_price"),
        spread_cents=row.get("spread"),
        volume_24h=row.get("vol24"),
        liquidity=row.get("liquidity"),
        days_to_end=row.get("days_to_end"),
        news_count=int(row.get("news_count") or 0),
        top_news_headline=row.get("top_news_headline"),
        reason=row.get("reason"),
    )


def _persist_research_report(conn: sqlite3.Connection, report) -> bool:
    data = report.to_dict()
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO research_reports
                (created_ts, market_slug, market_id, strategy_match, final_action,
                 final_score, risk_score, confidence, approved_for_backtest,
                 approved_for_live, features_json, assessments_json, conclusion)
            VALUES (?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?, ?)
            """,
            (
                report.created_ts,
                report.market_slug,
                report.market_id,
                report.strategy,
                report.final_action,
                report.final_score,
                report.risk_score,
                report.confidence,
                1 if report.approved_for_backtest else 0,
                1 if report.approved_for_live else 0,
                json.dumps(data["features"], sort_keys=True),
                json.dumps(data["assessments"], sort_keys=True),
                report.conclusion,
            ),
        )
        return cur.rowcount > 0
    except sqlite3.Error as exc:
        logger.warning(
            "research report persist failed for %s/%s: %s",
            report.market_slug,
            report.strategy,
            exc,
        )
        return False


# ---------------------------------------------------------------------
# Main scout loop
# ---------------------------------------------------------------------


def run_once(db_path: str, *, max_markets: int = 500, news_top_n: int = 10) -> dict:
    """One pass: scan Gamma, filter, score top N, write opportunities."""
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    raw = _gamma_markets(limit=max_markets)
    logger.info("scout: fetched %d markets from Gamma", len(raw))
    committee = ResearchCommittee()

    candidates: list[dict] = []
    for m in raw:
        b = _market_basics(m)
        if b is None or not b["slug"]:
            continue
        for strategy, filt in STRATEGY_FILTERS.items():
            score = filt(b)
            if score is None or score < 0.10:
                continue
            candidates.append({**b, "strategy": strategy, "score": score, "ts": now_iso})

    candidates.sort(key=lambda c: c["score"], reverse=True)
    logger.info("scout: %d candidates passed filters", len(candidates))

    conn = _open_db(db_path)
    inserted = 0
    research_written = 0
    snapshots = 0
    enriched = 0
    by_strategy: dict[str, int] = {}

    # Phase B: snapshot the price for every candidate that passed filters,
    # deduped by slug (candidates can match multiple strategies).
    seen_slugs: set[str] = set()
    for cand in candidates:
        if cand["slug"] in seen_slugs:
            continue
        seen_slugs.add(cand["slug"])
        try:
            conn.execute(
                """
                INSERT INTO price_snapshots
                    (ts, market_slug, market_id, yes_price, no_price,
                     spread_cents, volume_24h, liquidity)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (cand["ts"], cand["slug"], cand.get("market_id"),
                 cand.get("yes_price"), cand.get("no_price"), cand.get("spread"),
                 cand.get("vol24"), cand.get("liquidity")),
            )
            snapshots += 1
        except sqlite3.Error as exc:
            logger.warning("snapshot insert failed for %s: %s", cand.get("slug"), exc)

    for cand in candidates:
        # Tavily news enrichment for top N candidates
        news_count = 0
        top_headline = None
        if enriched < news_top_n:
            news = _tavily_news(_slug_to_query(cand["slug"]), max_results=3)
            if news:
                news_count = len(news)
                top_headline = (news[0].get("title") or "")[:200]
            cand["news_count"] = news_count
            cand["top_news_headline"] = top_headline
            enriched += 1
            # Bump score by news count (more discussion → potentially more mispriced)
            cand["score"] = round(cand["score"] + 0.05 * min(news_count, 3), 3)

        # Defensive formatting — every numeric could be None if Gamma returned
        # malformed data. `.get(key, 0)` doesn't help when the key IS present
        # with value None; explicit `or 0` does.
        days_str = f"{(cand.get('days_to_end') or 0):.1f}"
        cand["reason"] = (
            f"strategy={cand['strategy']} no={(cand.get('no_price') or 0):.3f} "
            f"yes={(cand.get('yes_price') or 0):.3f} vol24=${(cand.get('vol24') or 0):,.0f} "
            f"liq=${(cand.get('liquidity') or 0):,.0f} days_to_end={days_str}"
        )
        if _persist(conn, cand):
            inserted += 1
            by_strategy[cand["strategy"]] = by_strategy.get(cand["strategy"], 0) + 1
        if committee.cfg.enabled:
            report = committee.review(_research_context(cand))
            if _persist_research_report(conn, report):
                research_written += 1

    conn.close()
    return {
        "ts": now_iso,
        "markets_scanned": len(raw),
        "candidates_passed_filters": len(candidates),
        "enriched_with_news": enriched,
        "inserted_new": inserted,
        "research_reports_written": research_written,
        "price_snapshots_written": snapshots,
        "by_strategy": by_strategy,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="/app/data/scout.db",
                        help="SQLite DB path. Default: separate scout.db (avoids WAL contention with trade_log.db).")
    parser.add_argument("--max-markets", type=int, default=500,
                        help="Max Gamma markets to scan (default 500, ordered by vol24 desc)")
    parser.add_argument("--news-top-n", type=int, default=10,
                        help="Enrich top N candidates with Tavily news (rate-limit budget)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    summary = run_once(
        args.db_path,
        max_markets=args.max_markets,
        news_top_n=args.news_top_n,
    )
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"scout @ {summary['ts']}")
        print(f"  scanned: {summary['markets_scanned']} markets")
        print(f"  filtered: {summary['candidates_passed_filters']} candidates")
        print(f"  news-enriched: {summary['enriched_with_news']}")
        print(f"  inserted: {summary['inserted_new']} new opportunities")
        print(f"  research reports: {summary['research_reports_written']}")
        if summary["by_strategy"]:
            print(f"  by strategy: {summary['by_strategy']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
