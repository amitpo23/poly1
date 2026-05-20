"""MetaBrain — single synthesizing layer that aggregates all signal sources
into one unified MetaDecision with entry timing, win-rate context, and
probability velocity (fast-trend detection).

Architecture
-----------
MarketBrain (gate + score)
    └─ CrossMarketSignalFeed  (Kalshi / Metaculus / Manifold)
ConvictionJSONLReader         (reads external_conviction output files)
WinRateAdvisor                (queries brain_decisions + trades in SQLite)
ProbVelocityDetector          (tracks recent CLOB probability changes)
    └─ MetaBrain.synthesize() → MetaDecision

Callers: trade.py, market_scanner.py — call synthesize() instead of
evaluate_general_entry().  evaluate_general_entry() still works standalone;
MetaBrain wraps it and adds the extra layers.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as _ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from agents.application.execution_quality import ExecutionQualityAdvisor
from agents.application.sizing import binary_raw_ev
from agents.application.alpaca_market_data import (
    AlpacaMarketDataClient,
    AlpacaMarketSignal,
    alpaca_enabled_for_metabrain,
    question_aligned_direction,
)
from agents.application.crypto_exchange_tape import (
    CryptoExchangeSignal,
    CryptoExchangeTapeClient,
    crypto_tape_enabled_for_metabrain,
)

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_set(name: str, default: str) -> set[str]:
    raw = os.getenv(name, default)
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _parse_timestamp(value: object) -> Optional[float]:
    """Parse compact or ISO timestamps into epoch seconds."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.isdigit() and len(text) >= 14:
            return datetime.strptime(text[:14], "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            ).timestamp()
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _seconds_since(value: object) -> float:
    ts = _parse_timestamp(value)
    if ts is None:
        return float("inf")
    return max(0.0, time.time() - ts)


def _probability(value: object, default: float, *, clamp: bool = True) -> float:
    try:
        prob = float(value)
    except (TypeError, ValueError):
        return default
    if clamp:
        return max(0.0, min(1.0, prob))
    return prob


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------

@dataclass
class MetaDecision:
    """Unified decision produced by MetaBrain.synthesize()."""
    approved: bool
    reason: str
    score: float                           # 0–1 composite
    entry_timing: str                      # "now" | "wait" | "skip"
    # Historical win-rate from brain_decisions + trades SQLite
    winrate_estimate: Optional[float]      # fraction, e.g. 0.62
    winrate_sample_size: int               # how many resolved trades used
    # Signal provenance
    signal_sources: list                   # ["manifold","metaculus","clob_whale",…]
    cross_market_prob: Optional[float]     # consensus probability from cross-market
    cross_market_divergence: Optional[float]  # consensus − poly_prob
    # Velocity (fast trend)
    velocity_direction: Optional[str]      # "rising" | "falling" | "stable" | None
    velocity_pct_per_hour: Optional[float]
    # Convictions from JSONL (external_conviction agents)
    conviction_direction: Optional[str]    # "yes" | "no" | "skip" | None
    conviction_confidence: float
    conviction_sources: list
    # Full feature bag (for logging)
    features: dict = field(default_factory=dict)

    @property
    def summary(self) -> str:
        parts = [
            f"{'✅ APPROVED' if self.approved else '❌ BLOCKED'} ({self.reason})",
            f"score={self.score:.3f}",
            f"timing={self.entry_timing}",
        ]
        if self.winrate_estimate is not None:
            parts.append(f"winrate={self.winrate_estimate:.1%}[n={self.winrate_sample_size}]")
        if self.cross_market_divergence is not None:
            parts.append(f"cm_div={self.cross_market_divergence:+.2f}")
        if self.conviction_direction and self.conviction_direction != "skip":
            parts.append(
                f"conviction={self.conviction_direction}"
                f"@{self.conviction_confidence:.2f}"
                f"({','.join(self.conviction_sources[:3])})"
            )
        if self.velocity_direction:
            v = f"{self.velocity_pct_per_hour:+.1%}/h" if self.velocity_pct_per_hour else ""
            parts.append(f"velocity={self.velocity_direction}{v}")
        return " | ".join(parts)


# ---------------------------------------------------------------------------
# WinRateAdvisor
# ---------------------------------------------------------------------------

@dataclass
class WinRateStats:
    winrate: Optional[float]
    wins: int
    losses: int
    total_with_outcome: int
    avg_brain_score_wins: Optional[float]
    avg_brain_score_losses: Optional[float]
    source: str  # "brain_decisions" or "trades"


class WinRateAdvisor:
    """Computes historical win-rate from the SQLite trade ledger.

    Uses two sources (tried in order):
    1. brain_decisions.outcome_status — best because it's tied to brain scoring.
       A "win" = outcome_status IN ('closed_take_profit', 'resolved_yes',
       'resolved_no', 'resolved_skipped_no').
       A "loss" = outcome_status IN ('closed_stop_loss', 'closed_timeout',
       'resolved_loss').
    2. trades table — fallback, counts closed_take_profit vs closed_stop_loss.

    Results are cached for 5 minutes per (db_path, hours) combination.
    """

    WIN_OUTCOMES = frozenset({
        "closed_take_profit",
        "resolved_yes",           # held YES token, market resolved YES → payout=1 → profit
        "resolved_no",            # held NO token, market resolved NO → payout=1 → profit
        "resolved_skipped_no",    # legacy label; kept for backward-compat
    })
    LOSS_OUTCOMES = frozenset({
        "closed_stop_loss",
        "closed_timeout",
        "resolved_loss",          # held any side that lost → payout=0
    })

    def __init__(self, cache_ttl_sec: int = 300):
        self._cache: dict = {}
        self.cache_ttl_sec = cache_ttl_sec

    def compute(
        self,
        db_path: str,
        market_type: str = "general_binary",
        hours: int = 168,  # 7 days default
    ) -> WinRateStats:
        key = (db_path, market_type, hours)
        now = time.time()
        if key in self._cache:
            ts, stats = self._cache[key]
            if now - ts < self.cache_ttl_sec:
                return stats
        stats = self._compute(db_path, market_type, hours)
        self._cache[key] = (now, stats)
        return stats

    def _compute(self, db_path: str, market_type: str, hours: int) -> WinRateStats:
        if not db_path or not os.path.isfile(db_path):
            return WinRateStats(None, 0, 0, 0, None, None, "no_db")
        try:
            with sqlite3.connect(db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                stats = self._from_brain_decisions(conn, market_type, hours)
            return self._refine_with_daily_journal(db_path, stats, market_type)
        except Exception as exc:
            logger.debug("winrate_advisor: sqlite error: %s", exc)
            return WinRateStats(None, 0, 0, 0, None, None, "error")

    def _refine_with_daily_journal(
        self,
        db_path: str,
        stats: WinRateStats,
        market_type: str,
    ) -> WinRateStats:
        if os.getenv("META_BRAIN_DAILY_JOURNAL_ENABLED", "true").lower() not in {
            "1", "true", "yes", "on",
        }:
            return stats
        try:
            from agents.application.trade_log import TradeLog

            journal = TradeLog(db_path=db_path)
            day = journal.daily_trade_journal_stats(market_type=market_type)
        except Exception as exc:
            logger.debug("winrate_advisor: daily journal refine failed: %s", exc)
            return stats

        day_total = int(day.get("total_with_outcome") or 0)
        failures = int(day.get("failures") or 0)
        if day_total <= 0 and failures <= 0:
            return stats

        base = stats.winrate
        if base is None:
            base = _env_float("META_BRAIN_WINRATE_PRIOR", 0.50)
        day_wr = day.get("winrate")
        if day_wr is None:
            day_wr = 0.50

        # Intraday evidence should matter quickly, but tiny samples should not
        # whip the system around. This Bayesian-style blend gives the live day
        # up to 60% influence once there are roughly ten resolved outcomes.
        day_weight = min(0.60, max(0.10, day_total / 16.0))
        failure_penalty = min(0.18, failures * _env_float("META_BRAIN_FAILURE_PENALTY", 0.03))
        refined = ((1.0 - day_weight) * float(base)) + (day_weight * float(day_wr))
        refined = max(0.0, min(1.0, refined - failure_penalty))

        return WinRateStats(
            winrate=round(refined, 4),
            wins=stats.wins,
            losses=stats.losses,
            total_with_outcome=max(stats.total_with_outcome, day_total),
            avg_brain_score_wins=stats.avg_brain_score_wins,
            avg_brain_score_losses=stats.avg_brain_score_losses,
            source=f"{stats.source}+daily_journal",
        )

    def _from_brain_decisions(
        self, conn: sqlite3.Connection, market_type: str, hours: int
    ) -> WinRateStats:
        conn.row_factory = sqlite3.Row
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - hours * 3600)
        )
        # Only consider approved decisions with a known outcome.
        rows = conn.execute(
            """
            SELECT outcome_status, score
            FROM brain_decisions
            WHERE approved = 1
              AND outcome_status IS NOT NULL
              AND market_type = ?
              AND ts >= ?
            """,
            (market_type, cutoff),
        ).fetchall()

        if not rows:
            return self._from_trades_table(conn, hours)

        wins = sum(1 for r in rows if r["outcome_status"] in self.WIN_OUTCOMES)
        losses = sum(1 for r in rows if r["outcome_status"] in self.LOSS_OUTCOMES)
        total = wins + losses
        if total == 0:
            return WinRateStats(None, wins, losses, len(rows), None, None, "brain_decisions")

        win_scores = [r["score"] for r in rows if r["outcome_status"] in self.WIN_OUTCOMES and r["score"]]
        loss_scores = [r["score"] for r in rows if r["outcome_status"] in self.LOSS_OUTCOMES and r["score"]]

        return WinRateStats(
            winrate=wins / total,
            wins=wins,
            losses=losses,
            total_with_outcome=total,
            avg_brain_score_wins=sum(win_scores) / len(win_scores) if win_scores else None,
            avg_brain_score_losses=sum(loss_scores) / len(loss_scores) if loss_scores else None,
            source="brain_decisions",
        )

    def _from_trades_table(
        self, conn: sqlite3.Connection, hours: int
    ) -> WinRateStats:
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - hours * 3600)
        )
        rows = conn.execute(
            "SELECT status FROM trades WHERE status IN (?,?,?) AND ts >= ?",
            ("closed_take_profit", "closed_stop_loss", "closed_timeout", cutoff),
        ).fetchall()
        wins = sum(1 for r in rows if r[0] == "closed_take_profit")
        losses = sum(1 for r in rows if r[0] in ("closed_stop_loss", "closed_timeout"))
        total = wins + losses
        return WinRateStats(
            winrate=wins / total if total > 0 else None,
            wins=wins,
            losses=losses,
            total_with_outcome=total,
            avg_brain_score_wins=None,
            avg_brain_score_losses=None,
            source="trades",
        )


# ---------------------------------------------------------------------------
# Expert evidence routing
# ---------------------------------------------------------------------------

@dataclass
class ReliabilityStats:
    source_id: str
    winrate: Optional[float]
    wins: int
    losses: int
    sample_size: int
    wilson_lower: Optional[float]
    source: str


def _wilson_lower_bound(wins: int, total: int, z: float = 1.96) -> Optional[float]:
    if total <= 0:
        return None
    phat = wins / total
    denom = 1.0 + z * z / total
    centre = phat + z * z / (2.0 * total)
    margin = z * ((phat * (1.0 - phat) + z * z / (4.0 * total)) / total) ** 0.5
    return max(0.0, (centre - margin) / denom)


class SourceReliabilityAdvisor:
    """Computes empirical reliability for specific signal sources.

    Sources are read from brain_decisions.signal_source.  This is intentionally
    separate from the strategy-level WinRateAdvisor: a provider or wallet should
    earn trust from its own resolved outcomes, not from the global strategy.
    """

    WIN_OUTCOMES = WinRateAdvisor.WIN_OUTCOMES
    LOSS_OUTCOMES = WinRateAdvisor.LOSS_OUTCOMES

    def __init__(self, cache_ttl_sec: int = 300):
        self._cache: dict = {}
        self.cache_ttl_sec = cache_ttl_sec

    def best(
        self,
        db_path: str,
        source_ids: list[str],
        *,
        market_type: str = "general_binary",
        hours: int = 720,
    ) -> ReliabilityStats:
        candidates = [
            self.compute(db_path, source_id, market_type=market_type, hours=hours)
            for source_id in source_ids
            if source_id
        ]
        if not candidates:
            return ReliabilityStats("", None, 0, 0, 0, None, "no_source")
        return max(
            candidates,
            key=lambda s: (
                s.wilson_lower if s.wilson_lower is not None else -1.0,
                s.winrate if s.winrate is not None else -1.0,
                s.sample_size,
            ),
        )

    def compute(
        self,
        db_path: str,
        source_id: str,
        *,
        market_type: str = "general_binary",
        hours: int = 720,
    ) -> ReliabilityStats:
        if not source_id:
            return ReliabilityStats(source_id, None, 0, 0, 0, None, "no_source")
        if not db_path or not os.path.isfile(db_path):
            return ReliabilityStats(source_id, None, 0, 0, 0, None, "no_db")
        key = (db_path, source_id, market_type, hours)
        now = time.time()
        if key in self._cache:
            ts, stats = self._cache[key]
            if now - ts < self.cache_ttl_sec:
                return stats
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(now - hours * 3600)
        )
        try:
            with sqlite3.connect(db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT outcome_status
                    FROM brain_decisions
                    WHERE approved = 1
                      AND outcome_status IS NOT NULL
                      AND market_type = ?
                      AND ts >= ?
                      AND signal_source LIKE ?
                    """,
                    (market_type, cutoff, f"%{source_id}%"),
                ).fetchall()
        except Exception as exc:
            logger.debug("source_reliability: sqlite error for %s: %s", source_id, exc)
            return ReliabilityStats(source_id, None, 0, 0, 0, None, "error")
        wins = sum(1 for r in rows if r["outcome_status"] in self.WIN_OUTCOMES)
        losses = sum(1 for r in rows if r["outcome_status"] in self.LOSS_OUTCOMES)
        total = wins + losses
        stats = ReliabilityStats(
            source_id=source_id,
            winrate=(wins / total) if total else None,
            wins=wins,
            losses=losses,
            sample_size=total,
            wilson_lower=_wilson_lower_bound(wins, total),
            source="brain_decisions.signal_source",
        )
        if total == 0:
            scorecard_stats = self._from_provider_scorecard(source_id)
            if scorecard_stats.sample_size:
                stats = scorecard_stats
        self._cache[key] = (now, stats)
        return stats

    def _from_provider_scorecard(self, source_id: str) -> ReliabilityStats:
        path = os.getenv("PROVIDER_SCORECARD_PATH", "./data/provider_scorecard.json")
        if not path or not os.path.isfile(path):
            return ReliabilityStats(source_id, None, 0, 0, 0, None, "no_scorecard")
        min_matched = _env_int("PROVIDER_SCORECARD_MIN_MATCHED", 10)
        min_winrate = _env_float("PROVIDER_SCORECARD_MIN_WINRATE", 0.55)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except Exception as exc:
            logger.debug("provider_scorecard: failed to read %s: %s", path, exc)
            return ReliabilityStats(source_id, None, 0, 0, 0, None, "scorecard_error")
        providers = payload.get("providers") if isinstance(payload, dict) else []
        if not isinstance(providers, list):
            return ReliabilityStats(source_id, None, 0, 0, 0, None, "scorecard_empty")
        wanted = {source_id, source_id.replace("news:", ""), source_id.replace("wallet:", "")}
        for row in providers:
            if not isinstance(row, dict) or str(row.get("source")) not in wanted:
                continue
            matched = int(row.get("matched") or 0)
            winrate = row.get("winrate")
            if matched < min_matched or winrate is None or float(winrate) < min_winrate:
                return ReliabilityStats(source_id, None, int(row.get("wins") or 0), int(row.get("losses") or 0), matched, row.get("wilson_lower"), "provider_scorecard_rejected")
            wins = int(row.get("wins") or 0)
            losses = int(row.get("losses") or 0)
            return ReliabilityStats(
                source_id=source_id,
                winrate=float(winrate),
                wins=wins,
                losses=losses,
                sample_size=matched,
                wilson_lower=row.get("wilson_lower"),
                source="provider_scorecard",
            )
        return ReliabilityStats(source_id, None, 0, 0, 0, None, "scorecard_no_source")


@dataclass
class EvidenceClaim:
    source_id: str
    source_type: str
    direction: Optional[str]       # "yes" | "no" | None
    probability: float             # estimated probability for the claimed direction
    confidence: float
    reliability: Optional[ReliabilityStats] = None
    freshness_sec: Optional[float] = None
    raw: dict = field(default_factory=dict)


@dataclass
class EvidenceRoute:
    mode: str                       # "solo" | "consensus" | "blocked" | "none"
    direction: Optional[str]
    probability: Optional[float]
    leader: Optional[EvidenceClaim]
    reason: str
    claims: list[EvidenceClaim] = field(default_factory=list)
    conflicts: list[EvidenceClaim] = field(default_factory=list)


class EvidenceRouter:
    """Arbitrate evidence instead of averaging silence.

    A proven expert can lead alone.  Otherwise, the caller falls back to the
    existing informed-only consensus path.
    """

    def route(self, claims: list[EvidenceClaim]) -> EvidenceRoute:
        actionable = [c for c in claims if c.direction in {"yes", "no"} and c.probability > 0]
        if not actionable:
            return EvidenceRoute("none", None, None, None, "no_actionable_claims", claims)

        solo = [c for c in actionable if self._is_solo_eligible(c)]
        if solo:
            leader = max(
                solo,
                key=lambda c: (
                    c.reliability.wilson_lower if c.reliability and c.reliability.wilson_lower is not None else -1.0,
                    c.reliability.winrate if c.reliability and c.reliability.winrate is not None else -1.0,
                    c.probability,
                ),
            )
            conflicts = [c for c in actionable if c.direction != leader.direction and self._is_conflict(c)]
            if conflicts and any(self._conflict_beats_or_ties(c, leader) for c in conflicts):
                return EvidenceRoute(
                    "blocked",
                    None,
                    None,
                    leader,
                    f"expert_conflict:{leader.source_id}",
                    claims,
                    conflicts,
                )
            return EvidenceRoute(
                "solo",
                leader.direction,
                leader.probability,
                leader,
                f"expert_solo:{leader.source_id}",
                claims,
                conflicts,
            )

        return EvidenceRoute("consensus", None, None, None, "no_solo_expert", claims)

    def _is_solo_eligible(self, claim: EvidenceClaim) -> bool:
        min_prob = _env_float("EXPERT_SOLO_MIN_PROB", 0.65)
        if claim.source_type == "wallet" and self._has_external_wallet_proof(claim):
            return claim.probability >= min_prob
        if self._is_external_solo_eligible(claim, min_prob=min_prob):
            return True
        min_wr = _env_float("EXPERT_SOLO_MIN_WINRATE", 0.65)
        min_wilson = _env_float("EXPERT_SOLO_MIN_WILSON", 0.58)
        min_samples = _env_int("EXPERT_SOLO_MIN_SAMPLES", 30)
        max_age = _env_int("EXPERT_SOLO_MAX_AGE_SEC", 3600)
        if claim.probability < min_prob:
            return False
        if claim.freshness_sec is not None and claim.freshness_sec > max_age:
            return False
        rel = claim.reliability
        if rel is None or rel.sample_size < min_samples or rel.winrate is None:
            return False
        if rel.winrate < min_wr:
            return False
        if rel.wilson_lower is None or rel.wilson_lower < min_wilson:
            return False
        return True

    def _is_external_solo_eligible(self, claim: EvidenceClaim, *, min_prob: float) -> bool:
        """Allow strong, fresh external pricing/tape sources to lead alone.

        Local win-rate is ideal, but some sources are themselves market data:
        cross-venue prices, options fair value, Alpaca tape, and crypto tape.
        Treat them as calibrated only when they explicitly provide direction,
        probability, confidence, and freshness/liquidity conditions.
        """
        source_types = _env_set(
            "EXPERT_EXTERNAL_SOLO_SOURCE_TYPES",
            "cross_market,equity_fv,alpaca_market_data,crypto_exchange_tape",
        )
        if claim.source_type not in source_types:
            return False
        if claim.probability < min_prob:
            return False
        min_conf = _env_float("EXPERT_EXTERNAL_SOLO_MIN_CONFIDENCE", 0.60)
        if claim.confidence < min_conf:
            return False
        max_age = _env_int("EXPERT_EXTERNAL_SOLO_MAX_AGE_SEC", 300)
        if claim.freshness_sec is not None and claim.freshness_sec > max_age:
            return False
        raw = claim.raw or {}
        if claim.source_type == "equity_fv":
            return abs(float(raw.get("edge") or 0.0)) >= _env_float(
                "EXPERT_EXTERNAL_SOLO_MIN_EQUITY_EDGE",
                0.08,
            )
        if claim.source_type in {"alpaca_market_data", "crypto_exchange_tape"}:
            spread = raw.get("spread_pct")
            if spread is not None:
                try:
                    if float(spread) > _env_float("EXPERT_EXTERNAL_SOLO_MAX_SPREAD_PCT", 0.003):
                        return False
                except (TypeError, ValueError):
                    return False
        return True

    def _has_external_wallet_proof(self, claim: EvidenceClaim) -> bool:
        raw = claim.raw or {}
        wr = raw.get("wallet_winrate_external")
        trades = raw.get("wallet_total_trades_external")
        profit = raw.get("wallet_profit_usdc")
        try:
            wr = float(wr)
            if wr > 1.0:
                wr = wr / 100.0
        except (TypeError, ValueError):
            return False
        try:
            trades = int(trades or 0)
        except (TypeError, ValueError):
            trades = 0
        try:
            profit = float(profit or 0.0)
        except (TypeError, ValueError):
            profit = 0.0
        return (
            wr >= _env_float("EXPERT_WALLET_EXTERNAL_MIN_WINRATE", 0.70)
            and trades >= _env_int("EXPERT_WALLET_EXTERNAL_MIN_TRADES", 50)
            and profit >= _env_float("EXPERT_WALLET_EXTERNAL_MIN_PROFIT_USDC", 0.0)
        )

    def _is_conflict(self, claim: EvidenceClaim) -> bool:
        return (
            claim.probability >= _env_float("EXPERT_CONFLICT_MIN_PROB", 0.62)
            and claim.reliability is not None
            and claim.reliability.winrate is not None
            and claim.reliability.winrate >= _env_float("EXPERT_CONFLICT_MIN_WINRATE", 0.58)
            and claim.reliability.sample_size >= _env_int("EXPERT_CONFLICT_MIN_SAMPLES", 15)
        )

    def _conflict_beats_or_ties(self, conflict: EvidenceClaim, leader: EvidenceClaim) -> bool:
        c_rel = conflict.reliability
        l_rel = leader.reliability
        if c_rel is None or l_rel is None:
            return False
        c_lb = c_rel.wilson_lower or 0.0
        l_lb = l_rel.wilson_lower or 0.0
        return c_lb >= l_lb - _env_float("EXPERT_CONFLICT_WILSON_MARGIN", 0.03)


@dataclass
class EquityFairValueSignal:
    direction: Optional[str]
    probability: float
    edge: float
    selected_ticker: Optional[str]
    selected_outcome: Optional[str]
    age_seconds: float
    features: dict = field(default_factory=dict)


class EquityFairValueReader:
    """Reads shadow fair-value signals produced by equity_options_fair_value."""

    TICKER_ALIASES = {
        "NVDA": ("nvda", "nvidia"),
        "MSFT": ("msft", "microsoft"),
        "AAPL": ("aapl", "apple"),
        "GOOGL": ("googl", "goog", "google", "alphabet"),
        "AMZN": ("amzn", "amazon"),
        "META": ("meta", "facebook"),
        "TSLA": ("tsla", "tesla"),
    }

    def query(self, *, db_path: str, market_id: str, question: str) -> EquityFairValueSignal:
        if not db_path or not os.path.isfile(db_path):
            return EquityFairValueSignal(None, 0.5, 0.0, None, None, float("inf"))
        max_age = _env_int("EQUITY_FV_MAX_SIGNAL_AGE_SEC", 900)
        try:
            with sqlite3.connect(db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT ts, features_json
                    FROM brain_decisions
                    WHERE agent = 'equity_options_fair_value'
                      AND market_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (market_id,),
                ).fetchone()
        except Exception as exc:
            logger.debug("equity_fv_reader: sqlite error: %s", exc)
            return EquityFairValueSignal(None, 0.5, 0.0, None, None, float("inf"))
        if row is None:
            return EquityFairValueSignal(None, 0.5, 0.0, None, None, float("inf"))
        age = _seconds_since(row["ts"])
        if age > max_age:
            return EquityFairValueSignal(None, 0.5, 0.0, None, None, age)
        try:
            features = json.loads(row["features_json"] or "{}")
        except Exception:
            features = {}
        ticker = features.get("selected_ticker")
        fair_prob = _probability(features.get("fair_probability"), 0.5)
        edge = _probability(features.get("edge"), 0.0, clamp=False)
        selected_outcome = features.get("selected_outcome")
        if ticker and self._question_mentions_ticker(question, str(ticker)):
            return EquityFairValueSignal(
                "yes",
                fair_prob,
                edge,
                str(ticker),
                None if selected_outcome is None else str(selected_outcome),
                age,
                features,
            )
        return EquityFairValueSignal(None, 0.5, edge, str(ticker) if ticker else None, selected_outcome, age, features)

    def _question_mentions_ticker(self, question: str, ticker: str) -> bool:
        text = str(question or "").lower()
        for alias in self.TICKER_ALIASES.get(ticker.upper(), (ticker.lower(),)):
            if alias in text:
                return True
        return False


class AlpacaMarketDataReader:
    """Reads Alpaca bars as a direct MetaBrain signal with short caching."""

    def __init__(self, client: Optional[AlpacaMarketDataClient] = None) -> None:
        self.client = client or AlpacaMarketDataClient()

    def query(self, question: str) -> AlpacaMarketSignal:
        if not alpaca_enabled_for_metabrain():
            return AlpacaMarketSignal(
                None, 0.5, 0.0, None, None, "alpaca: disabled for MetaBrain"
            )
        try:
            return self.client.analyze_question(question)
        except Exception as exc:
            return AlpacaMarketSignal(
                None,
                0.5,
                0.0,
                None,
                None,
                f"alpaca: error:{type(exc).__name__}",
                {"error": str(exc)[:180]},
            )


class CryptoExchangeTapeReader:
    """Reads Binance/OKX public crypto tape for fast-market context."""

    def __init__(self, client: Optional[CryptoExchangeTapeClient] = None) -> None:
        self.client = client or CryptoExchangeTapeClient()

    def query(self, question: str) -> CryptoExchangeSignal:
        if not crypto_tape_enabled_for_metabrain():
            return CryptoExchangeSignal(
                None, 0.5, 0.0, None, None, "crypto_tape: disabled for MetaBrain"
            )
        try:
            return self.client.analyze_question(question)
        except Exception as exc:
            return CryptoExchangeSignal(
                None,
                0.5,
                0.0,
                None,
                None,
                f"crypto_tape: error:{type(exc).__name__}",
                {"error": str(exc)[:180]},
            )


# ---------------------------------------------------------------------------
# ConvictionJSONLReader
# ---------------------------------------------------------------------------

@dataclass
class ConvictionSummary:
    direction: Optional[str]   # "yes" | "no" | "skip" | None
    confidence: float
    sources: list              # provider names that contributed
    age_seconds: float         # age of the most recent matching record


class ConvictionJSONLReader:
    """Reads the most recent external_conviction JSONL output and returns a
    directional consensus for a given market_id or question keyword.

    Reads at most `max_lines_scan` lines from the tail of each JSONL file
    (fast — avoids loading multi-GB logs).  Results are cached per file for
    `cache_ttl_sec` seconds.

    Default JSONL paths come from env vars used by each conviction container.
    Override `conviction_paths` to inject test paths.
    """

    DEFAULT_ENV_VARS = [
        "EXTERNAL_CONVICTION_OUTPUT_PATH",           # aggregator / active instance
        "EXTERNAL_CONVICTION_AGGREGATOR_OUTPUT",     # explicit aggregator output
    ]
    DEFAULT_FALLBACK_PATHS = [
        "./data/external_convictions.jsonl",
        "./data/external_convictions_aggregator.jsonl",
        "./data/external_convictions_alpaca.jsonl",
        "./data/external_convictions_crypto_tape.jsonl",
        "./data/external_convictions_tradingview.jsonl",
        "./data/external_convictions_20test.jsonl",
    ]

    def __init__(
        self,
        conviction_paths: Optional[list] = None,
        max_lines_scan: int = 500,
        cache_ttl_sec: int = 120,
    ):
        self.max_lines_scan = max_lines_scan
        self.cache_ttl_sec = cache_ttl_sec
        self._conviction_paths = conviction_paths
        self._cache: dict = {}

    def _resolve_paths(self) -> list:
        if self._conviction_paths:
            return [p for p in self._conviction_paths if os.path.isfile(p)]
        paths = []
        for env_var in self.DEFAULT_ENV_VARS:
            val = os.getenv(env_var, "").strip()
            if val and os.path.isfile(val):
                paths.append(val)
        for p in self.DEFAULT_FALLBACK_PATHS:
            if os.path.isfile(p):
                paths.append(p)
        return list(dict.fromkeys(paths))  # dedup, preserve order

    def _tail_lines(self, path: str, n: int) -> list:
        """Efficiently read last n lines without loading the whole file."""
        chunk = 4096
        lines: list = []
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                pos = size
                buf = b""
                while pos > 0 and len(lines) < n:
                    read_size = min(chunk, pos)
                    pos -= read_size
                    f.seek(pos)
                    buf = f.read(read_size) + buf
                    lines = buf.split(b"\n")
            return [ln.decode("utf-8", errors="replace") for ln in lines[-n:] if ln.strip()]
        except Exception:
            return []

    def _load_file(self, path: str) -> list:
        now = time.time()
        key = path
        if key in self._cache:
            ts, records = self._cache[key]
            if now - ts < self.cache_ttl_sec:
                return records
        raw_lines = self._tail_lines(path, self.max_lines_scan)
        records = []
        for line in raw_lines:
            try:
                records.append(json.loads(line))
            except Exception:
                pass
        self._cache[key] = (now, records)
        return records

    def query(self, market_id: str, question: str = "") -> ConvictionSummary:
        """Find the most relevant conviction record for a market."""
        paths = self._resolve_paths()
        if not paths:
            return ConvictionSummary(None, 0.0, [], float("inf"))

        question_lower = (question or "").lower()[:60]
        market_id_lower = (market_id or "").lower()

        best_records: list = []
        for path in paths:
            for rec in self._load_file(path):
                mid = str(rec.get("market_id", "")).lower()
                mq = str(rec.get("question", "")).lower()
                # Match on market_id or keyword overlap in question.
                if mid == market_id_lower:
                    best_records.append(rec)
                elif question_lower and any(
                    w in mq for w in question_lower.split() if len(w) > 4
                ):
                    best_records.append(rec)

        if not best_records:
            return ConvictionSummary(None, 0.0, [], float("inf"))

        # Sort by recency; use ts field (ISO string) or fallback to position.
        def _ts(r):
            ts_val = _parse_timestamp(r.get("ts") or r.get("timestamp"))
            return ts_val if ts_val is not None else 0

        best_records.sort(key=_ts, reverse=True)
        recent = best_records[:5]

        # Aggregate direction and confidence.
        yes_conf = sum(
            float(r.get("confidence", 0))
            for r in recent
            if str(r.get("direction", "")).lower() == "yes"
        )
        no_conf = sum(
            float(r.get("confidence", 0))
            for r in recent
            if str(r.get("direction", "")).lower() == "no"
        )
        sources = list({
            str(r.get("source", r.get("provider", "")))
            for r in recent
            if r.get("direction") not in (None, "skip", "")
        })

        if yes_conf == 0 and no_conf == 0:
            direction, confidence = "skip", 0.0
        elif yes_conf >= no_conf:
            direction = "yes"
            confidence = yes_conf / len(recent)
        else:
            direction = "no"
            confidence = no_conf / len(recent)

        # Age of most recent record.
        latest_ts = _ts(best_records[0])
        age = max(0.0, time.time() - latest_ts) if latest_ts else float("inf")

        return ConvictionSummary(
            direction=direction,
            confidence=round(min(1.0, confidence), 3),
            sources=sources,
            age_seconds=age,
        )


# ---------------------------------------------------------------------------
# ProbVelocityDetector
# ---------------------------------------------------------------------------

@dataclass
class VelocitySignal:
    direction: Optional[str]         # "rising" | "falling" | "stable"
    pct_per_hour: Optional[float]    # probability change per hour (e.g. 0.05 = +5%/h)
    window_minutes: int
    data_points: int


class ProbVelocityDetector:
    """Detects fast-moving probability trends from the position_marks SQLite
    table (which is updated every ~60s by the position manager).

    Falls back to tracking the last N prices passed via record().
    This gives the MetaBrain a velocity signal: "this market's probability
    is moving +8%/h — enter before the crowd reprices."
    """

    def __init__(
        self,
        min_abs_velocity: float = 0.02,  # %/h threshold for "rising"/"falling"
        window_minutes: int = 30,
        cache_ttl_sec: int = 60,
    ):
        self.min_abs_velocity = min_abs_velocity
        self.window_minutes = window_minutes
        self.cache_ttl_sec = cache_ttl_sec
        self._in_memory: dict[str, list] = {}   # token_id → [(ts, price), ...]
        self._db_cache: dict = {}

    def record(self, token_id: str, prob: float) -> None:
        """Store a live probability sample for a token (called by live agents)."""
        if token_id not in self._in_memory:
            self._in_memory[token_id] = []
        self._in_memory[token_id].append((time.time(), prob))
        # Keep only window_minutes * 2 of history.
        cutoff = time.time() - self.window_minutes * 120
        self._in_memory[token_id] = [
            x for x in self._in_memory[token_id] if x[0] >= cutoff
        ]

    def detect(
        self,
        market_id: str,
        token_id: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> VelocitySignal:
        """Return velocity signal for a market using in-memory + DB samples."""
        # Try in-memory first.
        if token_id and token_id in self._in_memory:
            sig = self._from_samples(self._in_memory[token_id])
            if sig.data_points >= 3:
                return sig

        # Fall back to position_marks table.
        if db_path and os.path.isfile(db_path):
            sig = self._from_db(db_path, market_id, token_id)
            if sig.data_points >= 2:
                return sig

        return VelocitySignal(None, None, self.window_minutes, 0)

    def _from_samples(self, samples: list) -> VelocitySignal:
        if len(samples) < 2:
            return VelocitySignal(None, None, self.window_minutes, len(samples))
        cutoff = time.time() - self.window_minutes * 60
        recent = [(t, p) for t, p in samples if t >= cutoff]
        if len(recent) < 2:
            recent = samples[-5:]
        if len(recent) < 2:
            return VelocitySignal(None, None, self.window_minutes, len(recent))

        t0, p0 = recent[0]
        t1, p1 = recent[-1]
        dt_hours = (t1 - t0) / 3600.0
        if dt_hours < 1e-6:
            return VelocitySignal("stable", 0.0, self.window_minutes, len(recent))

        velocity = (p1 - p0) / dt_hours
        if abs(velocity) < self.min_abs_velocity:
            direction = "stable"
        elif velocity > 0:
            direction = "rising"
        else:
            direction = "falling"

        return VelocitySignal(direction, round(velocity, 4), self.window_minutes, len(recent))

    def _from_db(
        self, db_path: str, market_id: str, token_id: Optional[str]
    ) -> VelocitySignal:
        key = (db_path, market_id, token_id)
        now = time.time()
        if key in self._db_cache:
            ts, sig = self._db_cache[key]
            if now - ts < self.cache_ttl_sec:
                return sig
        try:
            with sqlite3.connect(db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                if token_id:
                    rows = conn.execute(
                        """SELECT entry_price, current_price, first_seen_ts, last_seen_ts
                           FROM position_marks WHERE token_id = ?""",
                        (token_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT entry_price, current_price, first_seen_ts, last_seen_ts
                           FROM position_marks WHERE market_id = ?
                           ORDER BY last_seen_ts DESC LIMIT 1""",
                        (market_id,),
                    ).fetchall()
            if not rows:
                return VelocitySignal(None, None, self.window_minutes, 0)
            row = rows[0]
            entry_price = float(row["entry_price"] or 0)
            current_price = float(row["current_price"] or 0)
            first_ts = row["first_seen_ts"] or ""
            last_ts = row["last_seen_ts"] or ""

            first_epoch = _parse_timestamp(first_ts)
            last_epoch = _parse_timestamp(last_ts)
            if first_epoch is None or last_epoch is None:
                return VelocitySignal(None, None, self.window_minutes, 0)
            dt_hours = max((last_epoch - first_epoch) / 3600.0, 1e-6)
            if entry_price <= 0:
                return VelocitySignal(None, None, self.window_minutes, 1)
            velocity = (current_price - entry_price) / entry_price / dt_hours
            direction = (
                "rising" if velocity > self.min_abs_velocity
                else "falling" if velocity < -self.min_abs_velocity
                else "stable"
            )
            sig = VelocitySignal(direction, round(velocity, 4), self.window_minutes, 2)
            self._db_cache[key] = (now, sig)
            return sig
        except Exception as exc:
            logger.debug("velocity_detector: db error: %s", exc)
            return VelocitySignal(None, None, self.window_minutes, 0)


# ---------------------------------------------------------------------------
# WhaleSentimentReader
# ---------------------------------------------------------------------------

@dataclass
class WhaleSentimentSignal:
    direction: Optional[str]   # "bullish" | "bearish" | None
    confidence: float          # 0-1, dampened consensus (50/50 → 0, unanimous → 1)
    n_whales: int              # unique profitable wallets contributing
    total_size_usdc: float     # total USDC size of their recorded positions
    avg_profit_usdc: float     # average historical profit per contributing wallet
    wallets: list = field(default_factory=list)  # contributing wallet addresses
    best_wallet_winrate_external: Optional[float] = None
    best_wallet_trades_external: int = 0
    best_wallet_rank: Optional[int] = None


class WhaleSentimentReader:
    """Reads the wallet_signals table written by WalletFollowEngine and weights
    each signal by the wallet's historical profit (wallet_profit_usdc).

    Only considers:
    - Signals recorded within the last ``max_age_hours`` (default 24 h).
    - Wallets with wallet_profit_usdc >= min_profit_usdc (default 50 USDC).
    - One entry per wallet per market — most recent signal wins.

    Confidence is dampened so that a 50/50 split → 0.0 and unanimous → 1.0:
        confidence = (raw_fraction - 0.5) * 2.0

    Results are cached for ``cache_ttl_sec`` seconds (default 120 s).
    Fails silently — any DB error returns a neutral signal.
    """

    def __init__(
        self,
        max_age_hours: int = 24,
        min_profit_usdc: float = 50.0,
        cache_ttl_sec: int = 120,
    ):
        self.max_age_hours = max_age_hours
        self.min_profit_usdc = min_profit_usdc
        self.cache_ttl_sec = cache_ttl_sec
        self._cache: dict = {}

    def query(self, market_id: str, db_path: str) -> WhaleSentimentSignal:
        """Return whale sentiment for one market from wallet_signals."""
        key = (db_path, market_id)
        now = time.time()
        if key in self._cache:
            ts, sig = self._cache[key]
            if now - ts < self.cache_ttl_sec:
                return sig
        sig = self._compute(market_id, db_path)
        self._cache[key] = (now, sig)
        return sig

    def _compute(self, market_id: str, db_path: str) -> WhaleSentimentSignal:
        if not db_path or not os.path.isfile(db_path):
            return WhaleSentimentSignal(None, 0.0, 0, 0.0, 0.0)
        rows = []
        try:
            cutoff = time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.gmtime(time.time() - self.max_age_hours * 3600),
            )
            with sqlite3.connect(db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT direction, wallet_profit_usdc, wallet_size_usdc,
                           wallet_address, wallet_winrate_external,
                           wallet_total_trades_external, wallet_rank
                    FROM wallet_signals
                    WHERE market_id = ?
                      AND ts >= ?
                      AND wallet_profit_usdc >= ?
                    ORDER BY ts DESC
                    LIMIT 100
                    """,
                    (market_id, cutoff, self.min_profit_usdc),
                ).fetchall()
        except Exception as exc:
            logger.debug("whale_sentiment: db error: %s", exc)
            return WhaleSentimentSignal(None, 0.0, 0, 0.0, 0.0)

        if not rows:
            return WhaleSentimentSignal(None, 0.0, 0, 0.0, 0.0)

        # One signal per wallet — most recent already first (ORDER BY ts DESC).
        seen: set = set()
        bull_weight = 0.0
        bear_weight = 0.0
        total_size = 0.0
        profits: list = []
        wallets: list = []
        best_external_winrate = None
        best_external_trades = 0
        best_rank = None
        for row in rows:
            wallet = row["wallet_address"]
            if wallet in seen:
                continue
            seen.add(wallet)
            wallets.append(str(wallet))
            profit = max(0.0, float(row["wallet_profit_usdc"] or 0))
            size = float(row["wallet_size_usdc"] or 1.0)
            weight = max(1.0, profit)  # weight by historical profit in USDC
            if row["direction"] == "bullish":
                bull_weight += weight
            elif row["direction"] == "bearish":
                bear_weight += weight
            total_size += size
            profits.append(profit)
            try:
                ext_wr = row["wallet_winrate_external"]
            except (KeyError, IndexError):
                ext_wr = None
            if ext_wr not in (None, ""):
                try:
                    ext_wr = float(ext_wr)
                    if ext_wr > 1.0:
                        ext_wr = ext_wr / 100.0
                    trades = int(row["wallet_total_trades_external"] or 0)
                    if best_external_winrate is None or (
                        ext_wr, trades
                    ) > (best_external_winrate, best_external_trades):
                        best_external_winrate = ext_wr
                        best_external_trades = trades
                        best_rank = row["wallet_rank"]
                except (TypeError, ValueError):
                    pass

        total_weight = bull_weight + bear_weight
        if total_weight == 0:
            return WhaleSentimentSignal(None, 0.0, len(seen), total_size, 0.0)

        if bull_weight >= bear_weight:
            direction = "bullish"
            raw_conf = bull_weight / total_weight
        else:
            direction = "bearish"
            raw_conf = bear_weight / total_weight

        # Dampen: map [0.5, 1.0] → [0.0, 1.0] so 50/50 → 0 and unanimous → 1.
        confidence = round(min(1.0, max(0.0, (raw_conf - 0.5) * 2.0)), 3)
        avg_profit = sum(profits) / len(profits) if profits else 0.0
        return WhaleSentimentSignal(
            direction=direction,
            confidence=confidence,
            n_whales=len(seen),
            total_size_usdc=round(total_size, 2),
            avg_profit_usdc=round(avg_profit, 2),
            wallets=wallets,
            best_wallet_winrate_external=best_external_winrate,
            best_wallet_trades_external=best_external_trades,
            best_wallet_rank=best_rank,
        )


# ---------------------------------------------------------------------------
# BreakingNewsReader
# ---------------------------------------------------------------------------

@dataclass
class NewsSignal:
    direction: Optional[str]  # "bullish" | "bearish" | None
    confidence: float          # 0–1 (fraction of sentiment-bearing items on winning side)
    n_items: int               # total relevant items found across all sources
    sources: list              # which feed names contributed
    headlines: list            # up to 5 matching headlines (for logging/audit)


class BreakingNewsReader:
    """Fetches breaking news from configurable RSS feeds and optionally from
    the Twitter/X API v2 search endpoint, then scores relevance + sentiment
    relative to the market question.

    Sources (all fail-silent — a network error = neutral signal):
    • Ynet RSS      — BREAKING_NEWS_YNET_URL  (default: ynet.co.il main feed)
    • Trump Truth   — BREAKING_NEWS_TRUMP_URL (default: @realDonaldTrump RSS)
    • Extra feeds   — BREAKING_NEWS_EXTRA_URLS (comma-separated URLs)
    • Twitter/X     — only if TWITTER_BEARER_TOKEN is set in env

    Relevance filter: a news item counts only if at least one content word
    from the market question (len > 3) appears in the item text.

    Sentiment: simple keyword matching — counts explicit positive / negative
    words.  confidence = (winner_fraction − 0.5) × 2.0, clamped to [0, 1].

    Results are cached per question for BREAKING_NEWS_CACHE_SEC (default 300s).
    All network calls respect BREAKING_NEWS_TIMEOUT_SEC (default 4s).
    """

    _DEFAULT_YNET = "https://www.ynet.co.il/Integration/StoryRss2.xml"
    _DEFAULT_TRUMP = "https://truthsocial.com/@realDonaldTrump.rss"

    _BULLISH: frozenset = frozenset({
        "win", "wins", "won", "victory", "approve", "approves", "approved",
        "pass", "passes", "passed", "confirms", "confirmed", "deal",
        "agreement", "rises", "surge", "surges", "rally", "gains", "higher",
        "increase", "elected", "leads", "leading", "yes", "support",
    })
    _BEARISH: frozenset = frozenset({
        "lose", "loses", "lost", "losing", "defeat", "defeated", "fail",
        "fails", "failed", "reject", "rejects", "rejected", "crisis",
        "crash", "falls", "drops", "lower", "decrease", "war", "attack",
        "ban", "bans", "banned", "no", "against", "resign", "resigns",
        "scandal", "indicted", "guilty",
    })

    def __init__(self, cache_ttl_sec: int = 300) -> None:
        self.cache_ttl_sec = cache_ttl_sec
        self._cache: dict = {}

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def query(self, question: str) -> NewsSignal:
        """Return a cached-or-fresh NewsSignal for *question*."""
        key = question[:100].lower()
        now = time.time()
        if key in self._cache:
            ts, sig = self._cache[key]
            if now - ts < self.cache_ttl_sec:
                return sig
        try:
            sig = self._compute(question)
        except Exception as exc:
            logger.debug("news_reader: unexpected error: %s", exc)
            sig = NewsSignal(None, 0.0, 0, [], [])
        self._cache[key] = (now, sig)
        return sig

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compute(self, question: str) -> NewsSignal:
        timeout = _env_int("BREAKING_NEWS_TIMEOUT_SEC", 4)
        # Keywords: content words longer than 3 chars from the question.
        keywords = {w.lower() for w in question.split() if len(w) > 3}

        # Build feed map: name → url
        feeds: dict[str, str] = {}
        ynet_url = os.getenv("BREAKING_NEWS_YNET_URL", self._DEFAULT_YNET).strip()
        if ynet_url:
            feeds["ynet"] = ynet_url
        trump_url = os.getenv("BREAKING_NEWS_TRUMP_URL", self._DEFAULT_TRUMP).strip()
        if trump_url:
            feeds["trump_truth"] = trump_url
        extra_raw = os.getenv("BREAKING_NEWS_EXTRA_URLS", "").strip()
        for i, url in enumerate(extra_raw.split(",") if extra_raw else []):
            url = url.strip()
            if url:
                feeds[f"extra_{i}"] = url

        items: list[tuple[str, str]] = []  # (title_text, source_name)

        for source, url in feeds.items():
            try:
                items += self._fetch_rss(url, source, keywords, timeout)
            except Exception as exc:
                logger.debug("news_reader: rss %s failed: %s", source, exc)

        bearer = os.getenv("TWITTER_BEARER_TOKEN", "").strip()
        if bearer and keywords:
            try:
                items += self._fetch_twitter(bearer, keywords, timeout)
            except Exception as exc:
                logger.debug("news_reader: twitter/x failed: %s", exc)

        if not items:
            return NewsSignal(None, 0.0, 0, [], [])

        bull = sum(1 for t, _ in items if self._sentiment(t) == "bullish")
        bear = sum(1 for t, _ in items if self._sentiment(t) == "bearish")
        total_sentiment = bull + bear
        sources = sorted({src for _, src in items})
        headlines = [t for t, _ in items[:5]]

        if total_sentiment == 0:
            return NewsSignal(None, 0.0, len(items), sources, headlines)

        if bull >= bear:
            raw_frac = bull / total_sentiment
            direction = "bullish"
        else:
            raw_frac = bear / total_sentiment
            direction = "bearish"

        confidence = round(min(1.0, max(0.0, (raw_frac - 0.5) * 2.0)), 3)
        return NewsSignal(direction, confidence, len(items), sources, headlines)

    def _fetch_rss(
        self, url: str, source: str, keywords: set, timeout: int
    ) -> list[tuple[str, str]]:
        req = urllib.request.Request(
            url, headers={"User-Agent": "poly1-newsreader/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        root = _ET.fromstring(data)
        results: list[tuple[str, str]] = []
        for item in root.iter("item"):
            title_el = item.find("title")
            title = (title_el.text or "").strip() if title_el is not None else ""
            desc_el = item.find("description")
            desc = (desc_el.text or "").strip() if desc_el is not None else ""
            combined = (title + " " + desc).lower()
            # Include only items relevant to the market question.
            if keywords and not any(kw in combined for kw in keywords):
                continue
            results.append((title, source))
            if len(results) >= 10:
                break
        return results

    def _fetch_twitter(
        self, bearer: str, keywords: set, timeout: int
    ) -> list[tuple[str, str]]:
        query_str = " OR ".join(list(keywords)[:6])
        url = (
            "https://api.twitter.com/2/tweets/search/recent"
            f"?query={urllib.parse.quote(query_str)}&max_results=10"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {bearer}",
                "User-Agent": "poly1-newsreader/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        return [
            (tweet.get("text", "")[:150], "twitter_x")
            for tweet in (body.get("data") or [])
        ]

    def _sentiment(self, text: str) -> Optional[str]:
        t = text.lower()
        bull = sum(1 for w in self._BULLISH if w in t)
        bear = sum(1 for w in self._BEARISH if w in t)
        if bull > bear:
            return "bullish"
        if bear > bull:
            return "bearish"
        return None


# ---------------------------------------------------------------------------
# MetaBrain
# ---------------------------------------------------------------------------

class MetaBrain:
    """Single synthesizing layer.

    Usage
    -----
    brain = MetaBrain(db_path="./data/poly1.db")
    decision = brain.synthesize(
        market_id="0xabc…",
        question="Will X happen?",
        spread_pct=0.03,
        hours_to_close=12.0,
        poly_prob=0.55,
        external_context="Tavily context here",
        vibe_signals={"direction": "yes", "confidence": 0.7},
    )
    if decision.approved:
        # proceed with LLM pipeline
    logger.info("meta_brain: %s", decision.summary)
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        conviction_paths: Optional[list] = None,
        winrate_hours: int = 168,
        velocity_window_min: int = 30,
        market_brain=None,
    ):
        # Lazy import to avoid circular dependency.
        if market_brain is None:
            from agents.application.market_brain import MarketBrain
            market_brain = MarketBrain()
        self.market_brain = market_brain

        self.db_path = db_path or os.getenv("TRADE_LOG_PATH", "./data/poly1.db")
        self.winrate_hours = winrate_hours

        self.winrate_advisor = WinRateAdvisor()
        self.source_reliability = SourceReliabilityAdvisor()
        self.evidence_router = EvidenceRouter()
        self.conviction_reader = ConvictionJSONLReader(conviction_paths=conviction_paths)
        self.velocity_detector = ProbVelocityDetector(window_minutes=velocity_window_min)
        self.whale_reader = WhaleSentimentReader()
        self.news_reader = BreakingNewsReader()
        self.equity_fv_reader = EquityFairValueReader()
        self.alpaca_reader = AlpacaMarketDataReader()
        self.crypto_tape_reader = CryptoExchangeTapeReader()
        self.execution_quality = ExecutionQualityAdvisor(db_path=self.db_path)

    def synthesize_crypto_straddle(
        self,
        *,
        slug: str,
        question: str,
        asset: str,
        up_price: float,
        down_price: float,
        pair_ask_sum: float,
        seconds_to_expiry: int,
        token_id: Optional[str] = None,
        liquidity_usdc: Optional[float] = None,
    ) -> MetaDecision:
        """Pair-aware synthesis for very short crypto up/down straddles.

        This path keeps MarketBrain as the hard gate, then blends the existing
        signal layer: intraday win-rate journal, external conviction JSONL,
        probability velocity, Tavily news context, TradingView macro options
        snapshot, and an optional Hermes/HTTP/LLM forecast hook.
        """
        brain_decision = self.market_brain.evaluate_crypto_straddle_entry(
            slug=slug,
            up_price=up_price,
            down_price=down_price,
            pair_ask_sum=pair_ask_sum,
            seconds_to_expiry=seconds_to_expiry,
        )
        features: dict = dict(brain_decision.features or {})
        features["brain_reason"] = brain_decision.reason
        features["brain_score"] = brain_decision.score
        features["asset"] = asset
        features["question_preview"] = (question or slug)[:120]

        signal_sources: list = []

        win_stats = WinRateStats(None, 0, 0, 0, None, None, "not_computed")
        try:
            win_stats = self.winrate_advisor.compute(
                self.db_path,
                market_type="crypto_5min_straddle",
                hours=self.winrate_hours,
            )
        except Exception as exc:
            logger.debug("meta_brain straddle: winrate failed: %s", exc)
        features["winrate"] = win_stats.winrate
        features["winrate_n"] = win_stats.total_with_outcome
        features["winrate_source"] = win_stats.source

        conviction = ConvictionSummary(None, 0.0, [], float("inf"))
        try:
            conviction = self.conviction_reader.query(slug, question)
            if conviction.direction and conviction.direction != "skip":
                signal_sources += conviction.sources
        except Exception as exc:
            logger.debug("meta_brain straddle: conviction failed: %s", exc)
        features["conviction_direction"] = conviction.direction
        features["conviction_confidence"] = conviction.confidence
        features["conviction_sources"] = conviction.sources
        features["conviction_age_sec"] = conviction.age_seconds

        velocity = VelocitySignal(None, None, 30, 0)
        try:
            if token_id:
                self.velocity_detector.record(token_id, up_price)
            velocity = self.velocity_detector.detect(slug, token_id, self.db_path)
        except Exception as exc:
            logger.debug("meta_brain straddle: velocity failed: %s", exc)
        features["velocity_direction"] = velocity.direction
        features["velocity_pct_per_hour"] = velocity.pct_per_hour

        tavily_component, tavily_features = self._crypto_tavily_component(asset)
        tradingview_component, tradingview_features = self._tradingview_macro_component()
        hermes_component, hermes_features = self._hermes_or_llm_component(
            slug=slug,
            question=question,
            asset=asset,
            up_price=up_price,
            down_price=down_price,
            pair_ask_sum=pair_ask_sum,
            seconds_to_expiry=seconds_to_expiry,
        )
        features.update(tavily_features)
        features.update(tradingview_features)
        features.update(hermes_features)

        winrate_prior = _env_float("META_BRAIN_WINRATE_PRIOR", 0.50)
        min_winrate_samples = _env_int("META_BRAIN_MIN_WINRATE_SAMPLES", 5)
        winrate_component = (
            float(win_stats.winrate)
            if win_stats.winrate is not None
            and win_stats.total_with_outcome >= min_winrate_samples
            else winrate_prior
        )
        conviction_component = (
            float(conviction.confidence)
            if conviction.direction in {"yes", "no"}
            else 0.50
        )
        velocity_component = 0.5
        if velocity.direction in ("rising", "falling"):
            velocity_component = 0.70
        elif velocity.direction == "stable":
            velocity_component = 0.55
        liquidity_component = 0.5
        if liquidity_usdc is not None:
            liquidity_component = min(1.0, max(0.0, float(liquidity_usdc) / 50_000.0))

        weights = {
            "brain": _env_float("META_BRAIN_STRADDLE_WEIGHT_BRAIN", 0.30),
            "winrate": _env_float("META_BRAIN_STRADDLE_WEIGHT_WINRATE", 0.25),
            "tavily": _env_float("META_BRAIN_STRADDLE_WEIGHT_TAVILY", 0.10),
            "tradingview": _env_float("META_BRAIN_STRADDLE_WEIGHT_TRADINGVIEW", 0.10),
            "hermes": _env_float("META_BRAIN_STRADDLE_WEIGHT_HERMES", 0.15),
            "conviction": _env_float("META_BRAIN_STRADDLE_WEIGHT_CONVICTION", 0.05),
            "velocity": _env_float("META_BRAIN_STRADDLE_WEIGHT_VELOCITY", 0.03),
            "liquidity": _env_float("META_BRAIN_STRADDLE_WEIGHT_LIQUIDITY", 0.02),
        }
        components = {
            "brain": float(brain_decision.score),
            "winrate": winrate_component,
            "tavily": tavily_component,
            "tradingview": tradingview_component,
            "hermes": hermes_component,
            "conviction": conviction_component,
            "velocity": velocity_component,
            "liquidity": liquidity_component,
        }
        weight_sum = sum(max(0.0, v) for v in weights.values()) or 1.0
        score = sum(max(0.0, weights[k]) * components[k] for k in weights) / weight_sum
        score = round(max(0.0, min(1.0, score)), 4)
        features["meta_score"] = score
        features["weighted_components"] = {
            **{k: round(v, 4) for k, v in components.items()},
            "winrate_sample_size": win_stats.total_with_outcome,
            "weights": weights,
        }

        if not brain_decision.approved:
            approved = False
            reason = brain_decision.reason
            entry_timing = "skip"
        else:
            min_score = _env_float("META_BRAIN_CRYPTO_STRADDLE_MIN_SCORE", 0.52)
            approved = score >= min_score
            reason = (
                brain_decision.reason
                if approved
                else f"weighted_score_too_low:{score:.3f}<{min_score:.3f}"
            )
            entry_timing = "now" if approved else "skip"

        if approved and _env_bool("META_BRAIN_EXECUTION_QUALITY_ENABLED", True):
            try:
                execution_quality = self.execution_quality.evaluate(
                    token_id=token_id,
                    intended_usdc=_env_float("META_BRAIN_EXECUTION_QUALITY_USDC", 3.0),
                    internal_probability=score,
                    entry_price=up_price,
                )
                features.update(execution_quality.features)
                if not execution_quality.ok:
                    approved = False
                    reason = execution_quality.reason
                    entry_timing = "skip"
            except Exception as exc:
                logger.debug("meta_brain straddle: execution quality failed: %s", exc)
                if _env_bool("META_BRAIN_EXECUTION_QUALITY_FAIL_CLOSED", False):
                    features["execution_quality_error"] = f"{type(exc).__name__}: {exc}"
                    approved = False
                    reason = "execution_quality_error"
                    entry_timing = "skip"

        return MetaDecision(
            approved=approved,
            reason=reason,
            score=score,
            entry_timing=entry_timing,
            winrate_estimate=win_stats.winrate,
            winrate_sample_size=win_stats.total_with_outcome,
            signal_sources=list(dict.fromkeys(signal_sources)),
            cross_market_prob=None,
            cross_market_divergence=None,
            velocity_direction=velocity.direction,
            velocity_pct_per_hour=velocity.pct_per_hour,
            conviction_direction=conviction.direction,
            conviction_confidence=conviction.confidence,
            conviction_sources=conviction.sources,
            features=features,
        )

    def _crypto_tavily_component(self, asset: str) -> tuple[float, dict]:
        if os.getenv("META_BRAIN_STRADDLE_TAVILY_ENABLED", "true").lower() not in {
            "1", "true", "yes", "on",
        }:
            return 0.50, {"tavily_status": "disabled"}
        try:
            from agents.application.tavily import tavily_headlines

            query = f"{asset.upper()} crypto breaking news volatility liquidation ETF"
            headlines = tavily_headlines(query, max_results=3, timeout=4)
        except Exception as exc:
            return 0.50, {"tavily_status": f"error:{exc}"}
        if not headlines:
            return 0.50, {"tavily_status": "empty"}
        lower = headlines.lower()
        volatility_terms = (
            "volatility", "liquidation", "rally", "crash", "surge",
            "plunge", "etf", "fed", "hack", "exploit",
        )
        hits = sum(1 for term in volatility_terms if term in lower)
        component = min(0.75, 0.55 + hits * 0.04)
        return component, {
            "tavily_status": "ok",
            "tavily_component": round(component, 4),
            "tavily_hits": hits,
            "tavily_headlines": headlines[:500],
        }

    def _tradingview_macro_component(self) -> tuple[float, dict]:
        path = os.getenv(
            "TRADINGVIEW_OPTIONS_SNAPSHOT_PATH",
            "./data/tradingview_options_es1_snapshot.json",
        )
        try:
            if not os.path.isfile(path):
                return 0.50, {"tradingview_status": "missing_snapshot", "tradingview_path": path}
            age_limit = _env_int("TRADINGVIEW_OPTIONS_MAX_AGE_SEC", 900)
            age = max(0.0, time.time() - os.path.getmtime(path))
            with open(path, "r", encoding="utf-8") as f:
                snapshot = json.load(f)
            put_call = snapshot.get("put_call_ratio")
            if put_call is None:
                call_volume = float(snapshot.get("call_volume") or 0)
                put_volume = float(snapshot.get("put_volume") or 0)
                put_call = put_volume / max(call_volume, 1.0) if put_volume or call_volume else 0
            put_call = float(put_call or 0)
            if put_call <= 0:
                return 0.50, {"tradingview_status": "missing_put_call", "tradingview_age_sec": age}
            distance = abs(put_call - 1.0)
            component = 0.50 if distance < 0.15 else min(0.72, 0.56 + distance * 0.18)
            if age > age_limit:
                component = min(component, 0.52)
            return component, {
                "tradingview_status": "ok",
                "tradingview_put_call_ratio": round(put_call, 4),
                "tradingview_age_sec": round(age, 1),
                "tradingview_component": round(component, 4),
            }
        except Exception as exc:
            return 0.50, {"tradingview_status": f"error:{exc}"}

    def _hermes_or_llm_component(
        self,
        *,
        slug: str,
        question: str,
        asset: str,
        up_price: float,
        down_price: float,
        pair_ask_sum: float,
        seconds_to_expiry: int,
    ) -> tuple[float, dict]:
        payload = {
            "slug": slug,
            "question": question,
            "asset": asset,
            "strategy": "crypto_5min_straddle",
            "up_price": up_price,
            "down_price": down_price,
            "pair_ask_sum": pair_ask_sum,
            "seconds_to_expiry": seconds_to_expiry,
        }
        url = (
            os.getenv("HERMES_FORECAST_URL", "").strip()
            or os.getenv("HERMES_API_URL", "").strip()
        )
        if url:
            component, features = self._http_forecast_component(url, payload)
            features["hermes_url_configured"] = True
            return component, features
        if os.getenv("META_BRAIN_STRADDLE_LLM_ENABLED", "false").lower() in {
            "1", "true", "yes", "on",
        }:
            return self._llm_forecast_component(payload)
        return 0.50, {"hermes_status": "not_configured", "llm_status": "disabled"}

    def _http_forecast_component(self, url: str, payload: dict) -> tuple[float, dict]:
        try:
            data = json.dumps(payload).encode("utf-8")
            headers = {"Content-Type": "application/json", "User-Agent": "poly1-metabrain/1.0"}
            key = os.getenv("HERMES_API_KEY", "").strip()
            if key:
                headers["Authorization"] = f"Bearer {key}"
            req = urllib.request.Request(url, data=data, headers=headers)
            timeout = _env_int("HERMES_TIMEOUT_SEC", 4)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read())
            confidence = float(
                body.get("winrate")
                or body.get("confidence")
                or body.get("score")
                or 0.5
            )
            component = max(0.0, min(1.0, confidence))
            return component, {
                "hermes_status": "ok",
                "hermes_component": round(component, 4),
                "hermes_reason": str(body.get("reason") or body.get("reasoning") or "")[:300],
            }
        except Exception as exc:
            return 0.50, {"hermes_status": f"error:{exc}"}

    def _llm_forecast_component(self, payload: dict) -> tuple[float, dict]:
        prompt = (
            "Return JSON only: {\"winrate\":0-1,\"reason\":\"short\"}. "
            "Estimate whether this crypto 5m Polymarket straddle has enough "
            "short-term volatility edge after spread. Data: "
            + json.dumps(payload, sort_keys=True)
        )
        try:
            from langchain_core.messages import HumanMessage
            from langchain_openai import ChatOpenAI
            from agents.application.llm_config import openai_model

            model = os.getenv("META_BRAIN_STRADDLE_LLM_MODEL", "").strip() or openai_model()
            llm = ChatOpenAI(model=model, temperature=0, timeout=8)
            response = llm.invoke([HumanMessage(content=prompt)])
            text = getattr(response, "content", str(response))
            start = text.find("{")
            end = text.rfind("}")
            body = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
            component = max(0.0, min(1.0, float(body.get("winrate", 0.5))))
            return component, {
                "llm_status": "ok",
                "llm_model": model,
                "llm_component": round(component, 4),
                "llm_reason": str(body.get("reason", ""))[:300],
            }
        except Exception as exc:
            if os.getenv("ANTHROPIC_API_KEY", "").strip():
                try:
                    import anthropic
                    from agents.application.llm_config import anthropic_model

                    model = os.getenv(
                        "META_BRAIN_STRADDLE_ANTHROPIC_MODEL", ""
                    ).strip() or anthropic_model()
                    client = anthropic.Anthropic(
                        api_key=os.getenv("ANTHROPIC_API_KEY", "").strip()
                    )
                    response = client.messages.create(
                        model=model,
                        max_tokens=256,
                        messages=[{"role": "user", "content": prompt}],
                    )
                    text = response.content[0].text if response.content else ""
                    start = text.find("{")
                    end = text.rfind("}")
                    body = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
                    component = max(0.0, min(1.0, float(body.get("winrate", 0.5))))
                    return component, {
                        "llm_status": "ok_anthropic_fallback",
                        "llm_model": model,
                        "llm_openai_error": str(exc)[:180],
                        "llm_component": round(component, 4),
                        "llm_reason": str(body.get("reason", ""))[:300],
                    }
                except Exception as anth_exc:
                    return 0.50, {
                        "llm_status": f"error:{exc}",
                        "llm_anthropic_status": f"error:{anth_exc}",
                    }
            return 0.50, {"llm_status": f"error:{exc}"}

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def synthesize(
        self,
        *,
        market_id: str,
        question: str,
        spread_pct: Optional[float] = None,
        hours_to_close: Optional[float] = None,
        poly_prob: Optional[float] = None,
        external_context: str = "",
        vibe_signals: Optional[dict] = None,
        token_id: Optional[str] = None,
        liquidity_usdc: Optional[float] = None,
    ) -> MetaDecision:
        """Full synthesis: gate → win-rate → conviction → velocity → timing."""

        # 1. MarketBrain gate (includes CrossMarketSignalFeed internally).
        brain_decision = self.market_brain.evaluate_general_entry(
            question=question,
            spread_pct=spread_pct,
            hours_to_close=hours_to_close,
            external_context=external_context,
            vibe_signals=vibe_signals,
            poly_prob=poly_prob,
        )

        features: dict = dict(brain_decision.features or {})
        features["brain_reason"] = brain_decision.reason
        features["brain_score"] = brain_decision.score

        signal_sources: list = list(features.get("cross_market_sources") or [])

        # 2. Historical win-rate (non-blocking, best-effort).
        win_stats = WinRateStats(None, 0, 0, 0, None, None, "not_computed")
        try:
            win_stats = self.winrate_advisor.compute(
                self.db_path,
                market_type="general_binary",
                hours=self.winrate_hours,
            )
            features["winrate"] = win_stats.winrate
            features["winrate_n"] = win_stats.total_with_outcome
            features["winrate_source"] = win_stats.source
        except Exception as exc:
            logger.debug("meta_brain: winrate advisor failed: %s", exc)

        # 3. External conviction from JSONL files.
        conviction = ConvictionSummary(None, 0.0, [], float("inf"))
        try:
            conviction = self.conviction_reader.query(market_id, question)
            if conviction.direction and conviction.direction != "skip":
                signal_sources += [s for s in conviction.sources if s not in signal_sources]
            features["conviction_direction"] = conviction.direction
            features["conviction_confidence"] = conviction.confidence
            features["conviction_sources"] = conviction.sources
            features["conviction_age_sec"] = conviction.age_seconds
        except Exception as exc:
            logger.debug("meta_brain: conviction reader failed: %s", exc)

        # 4. Probability velocity (market momentum).
        velocity = VelocitySignal(None, None, 30, 0)
        try:
            velocity = self.velocity_detector.detect(market_id, token_id, self.db_path)
            features["velocity_direction"] = velocity.direction
            features["velocity_pct_per_hour"] = velocity.pct_per_hour
        except Exception as exc:
            logger.debug("meta_brain: velocity detector failed: %s", exc)

        # 5. Whale wallet sentiment — profitable on-chain wallets that recently
        #    traded this market (from wallet_signals table written by WalletFollow).
        whale = WhaleSentimentSignal(None, 0.0, 0, 0.0, 0.0)
        try:
            whale = self.whale_reader.query(market_id, self.db_path)
            features["whale_direction"] = whale.direction
            features["whale_confidence"] = whale.confidence
            features["whale_n"] = whale.n_whales
            features["whale_size_usdc"] = whale.total_size_usdc
            if whale.direction:
                signal_sources.append(f"whale_wallet:{whale.direction}")
        except Exception as exc:
            logger.debug("meta_brain: whale reader failed: %s", exc)

        # 6. Breaking news — RSS (Ynet, Truth Social) + Twitter/X if Bearer token.
        #    Fail-silent: a timeout or missing token produces a neutral signal.
        news = NewsSignal(None, 0.0, 0, [], [])
        try:
            news = self.news_reader.query(question)
            features["news_direction"] = news.direction
            features["news_confidence"] = news.confidence
            features["news_n_items"] = news.n_items
            features["news_sources"] = news.sources
            features["news_headlines"] = news.headlines
            if news.direction:
                signal_sources.append(f"news:{news.direction}")
        except Exception as exc:
            logger.debug("meta_brain: news reader failed: %s", exc)

        # 7. Equity/options fair value — liquid external markets can price
        #    stock/index outcomes faster than Polymarket.  Neutral unless the
        #    binary question clearly references the same selected ticker.
        equity_fv = EquityFairValueSignal(None, 0.5, 0.0, None, None, float("inf"))
        try:
            equity_fv = self.equity_fv_reader.query(
                db_path=self.db_path,
                market_id=market_id,
                question=question,
            )
            features["equity_fv_direction"] = equity_fv.direction
            features["equity_fv_probability"] = equity_fv.probability
            features["equity_fv_edge"] = equity_fv.edge
            features["equity_fv_selected_ticker"] = equity_fv.selected_ticker
            features["equity_fv_selected_outcome"] = equity_fv.selected_outcome
            features["equity_fv_age_sec"] = equity_fv.age_seconds
            if equity_fv.direction:
                signal_sources.append(f"equity_fv:{equity_fv.selected_ticker}")
        except Exception as exc:
            logger.debug("meta_brain: equity fair-value reader failed: %s", exc)

        # 8. Alpaca market data — live external tape for supported crypto and
        #    equity symbols.  This is a signal only; it never bypasses EV,
        #    execution-quality, or risk gates.
        alpaca = AlpacaMarketSignal(None, 0.5, 0.0, None, None, "not_computed")
        alpaca_direction = "skip"
        try:
            alpaca = self.alpaca_reader.query(question)
            features["alpaca_direction"] = alpaca.direction
            features["alpaca_probability"] = alpaca.probability
            features["alpaca_confidence"] = alpaca.confidence
            features["alpaca_symbol"] = alpaca.symbol
            features["alpaca_asset_class"] = alpaca.asset_class
            features["alpaca_reason"] = alpaca.reason
            features["alpaca_features"] = alpaca.features
            if alpaca.direction:
                signal_sources.append(f"alpaca:{alpaca.symbol}")
                alpaca_direction = question_aligned_direction(question, alpaca.direction)
        except Exception as exc:
            logger.debug("meta_brain: alpaca reader failed: %s", exc)

        # 9. Fast crypto exchange tape — Binance spot bars/book plus OKX
        #    funding.  This is the fastest public external signal for BTC/ETH
        #    style Up/Down markets and remains read-only.
        crypto_tape = CryptoExchangeSignal(None, 0.5, 0.0, None, None, "not_computed")
        crypto_tape_direction = "skip"
        try:
            crypto_tape = self.crypto_tape_reader.query(question)
            features["crypto_tape_direction"] = crypto_tape.direction
            features["crypto_tape_probability"] = crypto_tape.probability
            features["crypto_tape_confidence"] = crypto_tape.confidence
            features["crypto_tape_asset"] = crypto_tape.asset
            features["crypto_tape_symbol"] = crypto_tape.symbol
            features["crypto_tape_reason"] = crypto_tape.reason
            features["crypto_tape_features"] = crypto_tape.features
            if crypto_tape.direction:
                signal_sources.append(f"crypto_tape:{crypto_tape.symbol}")
                crypto_tape_direction = question_aligned_direction(
                    question, crypto_tape.direction
                )
        except Exception as exc:
            logger.debug("meta_brain: crypto tape reader failed: %s", exc)

        # 8. Weighted score. Missing win-rate data gets a neutral prior, so new
        # strategies can trade, but proven history improves the final score.
        winrate_prior = _env_float("META_BRAIN_WINRATE_PRIOR", 0.50)
        min_winrate_samples = _env_int("META_BRAIN_MIN_WINRATE_SAMPLES", 5)
        winrate_component = (
            float(win_stats.winrate)
            if win_stats.winrate is not None
            and win_stats.total_with_outcome >= min_winrate_samples
            else winrate_prior
        )
        # Neutral when no external conviction data — unknown is not negative.
        conviction_component = (
            float(conviction.confidence)
            if conviction.direction in ("yes", "no")
            else 0.5
        )
        velocity_component = 0.5
        if velocity.direction in ("rising", "falling"):
            velocity_component = 0.72
        elif velocity.direction == "stable":
            velocity_component = 0.55
        divergence = features.get("cross_market_divergence")
        cross_market_component = 0.5
        if divergence is not None:
            cross_market_component = min(1.0, 0.5 + abs(float(divergence)))
        liquidity_component = 0.5
        if liquidity_usdc is not None:
            liquidity_component = min(1.0, max(0.0, float(liquidity_usdc) / 50_000.0))
        # Whale component: bullish → [0.5, 1.0]; bearish → [0.0, 0.5]; no data → 0.5.
        whale_component = 0.5
        if whale.direction == "bullish":
            whale_component = 0.5 + whale.confidence * 0.5
        elif whale.direction == "bearish":
            whale_component = 0.5 - whale.confidence * 0.5
        # News component (Ynet RSS, Truth Social, Twitter/X): same mapping as whale.
        # 0.5 = no relevant news found (neutral, excluded from informed-only score).
        news_component = 0.5
        if news.direction == "bullish":
            news_component = 0.5 + news.confidence * 0.5
        elif news.direction == "bearish":
            news_component = 0.5 - news.confidence * 0.5
        equity_fv_component = 0.5
        if equity_fv.direction == "yes":
            equity_fv_component = equity_fv.probability
        elif equity_fv.direction == "no":
            equity_fv_component = 1.0 - equity_fv.probability
        alpaca_component = 0.5
        if alpaca_direction == "yes":
            alpaca_component = alpaca.probability
        elif alpaca_direction == "no":
            alpaca_component = 1.0 - alpaca.probability
        crypto_tape_component = 0.5
        if crypto_tape_direction == "yes":
            crypto_tape_component = crypto_tape.probability
        elif crypto_tape_direction == "no":
            crypto_tape_component = 1.0 - crypto_tape.probability

        weights = {
            "brain": _env_float("META_BRAIN_WEIGHT_BRAIN", 0.25),
            "winrate": _env_float("META_BRAIN_WEIGHT_WINRATE", 0.15),
            "conviction": _env_float("META_BRAIN_WEIGHT_CONVICTION", 0.15),
            "velocity": _env_float("META_BRAIN_WEIGHT_VELOCITY", 0.10),
            "cross_market": _env_float("META_BRAIN_WEIGHT_CROSS_MARKET", 0.10),
            "equity_fv": _env_float("META_BRAIN_WEIGHT_EQUITY_FV", 0.12),
            "alpaca": _env_float("META_BRAIN_WEIGHT_ALPACA", 0.08),
            "crypto_tape": _env_float("META_BRAIN_WEIGHT_CRYPTO_TAPE", 0.12),
            "whale": _env_float("META_BRAIN_WEIGHT_WHALE", 0.10),
            "news": _env_float("META_BRAIN_WEIGHT_NEWS", 0.10),
            "liquidity": _env_float("META_BRAIN_WEIGHT_LIQUIDITY", 0.05),
        }
        components = {
            "brain": float(brain_decision.score),
            "winrate": winrate_component,
            "conviction": conviction_component,
            "velocity": velocity_component,
            "cross_market": cross_market_component,
            "equity_fv": equity_fv_component,
            "alpaca": alpaca_component,
            "crypto_tape": crypto_tape_component,
            "whale": whale_component,
            "news": news_component,
            "liquidity": liquidity_component,
        }

        reliability_hours = _env_int("EXPERT_RELIABILITY_HOURS", 720)
        brain_reliability = (
            win_stats
            if win_stats.winrate is not None
            else None
        )
        conviction_reliability = self.source_reliability.best(
            self.db_path,
            [str(s) for s in conviction.sources],
            hours=reliability_hours,
        )
        wallet_source_ids = [f"wallet:{w}" for w in getattr(whale, "wallets", [])]
        whale_reliability = self.source_reliability.best(
            self.db_path,
            wallet_source_ids,
            hours=reliability_hours,
        )
        news_reliability = self.source_reliability.best(
            self.db_path,
            [f"news:{s}" for s in getattr(news, "sources", [])],
            hours=reliability_hours,
        )
        brain_rel = None
        if brain_reliability is not None:
            brain_rel = ReliabilityStats(
                source_id="brain",
                winrate=brain_reliability.winrate,
                wins=brain_reliability.wins,
                losses=brain_reliability.losses,
                sample_size=brain_reliability.total_with_outcome,
                wilson_lower=_wilson_lower_bound(
                    brain_reliability.wins,
                    brain_reliability.total_with_outcome,
                ),
                source=brain_reliability.source,
            )
        claims = [
            EvidenceClaim(
                source_id="brain",
                source_type="llm_brain",
                direction="yes",
                probability=float(brain_decision.score),
                confidence=float(brain_decision.score),
                reliability=brain_rel,
                raw={"component": components["brain"]},
            )
        ]
        cross_prob = features.get("cross_market_prob")
        if cross_prob is not None:
            try:
                cp = max(0.0, min(1.0, float(cross_prob)))
                claims.append(
                    EvidenceClaim(
                        source_id="cross_market",
                        source_type="cross_market",
                        direction="yes" if cp >= 0.5 else "no",
                        probability=cp if cp >= 0.5 else 1.0 - cp,
                        confidence=abs(cp - 0.5) * 2.0,
                        raw={
                            "cross_market_prob": cp,
                            "divergence": features.get("cross_market_divergence"),
                        },
                    )
                )
            except (TypeError, ValueError):
                pass
        if conviction.direction in ("yes", "no"):
            claims.append(
                EvidenceClaim(
                    source_id=conviction_reliability.source_id or ",".join(conviction.sources) or "conviction",
                    source_type="conviction",
                    direction=conviction.direction,
                    probability=float(conviction.confidence),
                    confidence=float(conviction.confidence),
                    reliability=conviction_reliability if conviction_reliability.sample_size else None,
                    freshness_sec=conviction.age_seconds,
                    raw={"sources": conviction.sources},
                )
            )
        if whale.direction in ("bullish", "bearish"):
            wallet_source = whale_reliability.source_id or "whale_wallet"
            claims.append(
                EvidenceClaim(
                    source_id=wallet_source,
                    source_type="wallet",
                    direction="yes" if whale.direction == "bullish" else "no",
                    probability=whale_component if whale.direction == "bullish" else 1.0 - whale_component,
                    confidence=float(whale.confidence),
                    reliability=whale_reliability if whale_reliability.sample_size else None,
                    raw={
                        "wallets": getattr(whale, "wallets", []),
                        "wallet_winrate_external": whale.best_wallet_winrate_external,
                        "wallet_total_trades_external": whale.best_wallet_trades_external,
                        "wallet_rank": whale.best_wallet_rank,
                        "wallet_profit_usdc": whale.avg_profit_usdc,
                        "avg_profit_usdc": whale.avg_profit_usdc,
                        "n_whales": whale.n_whales,
                    },
                )
            )
        if news.direction in ("bullish", "bearish"):
            claims.append(
                EvidenceClaim(
                    source_id=news_reliability.source_id or "news",
                    source_type="news",
                    direction="yes" if news.direction == "bullish" else "no",
                    probability=news_component if news.direction == "bullish" else 1.0 - news_component,
                    confidence=float(news.confidence),
                    reliability=news_reliability if news_reliability.sample_size else None,
                    raw={"sources": news.sources},
                )
            )
        if equity_fv.direction in ("yes", "no"):
            equity_rel = self.source_reliability.best(
                self.db_path,
                ["equity_options_fair_value"],
                hours=reliability_hours,
            )
            claims.append(
                EvidenceClaim(
                    source_id="equity_options_fair_value",
                    source_type="equity_fv",
                    direction=equity_fv.direction,
                    probability=equity_fv.probability,
                    confidence=max(0.0, min(1.0, abs(equity_fv.edge) * 4.0)),
                    reliability=equity_rel if equity_rel.sample_size else None,
                    freshness_sec=equity_fv.age_seconds,
                    raw={
                        "selected_ticker": equity_fv.selected_ticker,
                        "selected_outcome": equity_fv.selected_outcome,
                        "edge": equity_fv.edge,
                    },
                )
            )
        if alpaca_direction in ("yes", "no"):
            alpaca_rel = self.source_reliability.best(
                self.db_path,
                ["alpaca_market_data", f"alpaca:{alpaca.symbol}"],
                hours=reliability_hours,
            )
            claims.append(
                EvidenceClaim(
                    source_id=f"alpaca:{alpaca.symbol}",
                    source_type="alpaca_market_data",
                    direction=alpaca_direction,
                    probability=(
                        alpaca.probability
                        if alpaca_direction == "yes"
                        else 1.0 - alpaca.probability
                    ),
                    confidence=float(alpaca.confidence),
                    reliability=alpaca_rel if alpaca_rel.sample_size else None,
                    raw={
                        "symbol": alpaca.symbol,
                        "asset_class": alpaca.asset_class,
                        "market_direction": alpaca.direction,
                        **alpaca.features,
                    },
                )
            )
        if crypto_tape_direction in ("yes", "no"):
            crypto_tape_rel = self.source_reliability.best(
                self.db_path,
                ["crypto_exchange_tape", f"crypto_tape:{crypto_tape.symbol}"],
                hours=reliability_hours,
            )
            claims.append(
                EvidenceClaim(
                    source_id=f"crypto_tape:{crypto_tape.symbol}",
                    source_type="crypto_exchange_tape",
                    direction=crypto_tape_direction,
                    probability=(
                        crypto_tape.probability
                        if crypto_tape_direction == "yes"
                        else 1.0 - crypto_tape.probability
                    ),
                    confidence=float(crypto_tape.confidence),
                    reliability=(
                        crypto_tape_rel if crypto_tape_rel.sample_size else None
                    ),
                    raw={
                        "asset": crypto_tape.asset,
                        "symbol": crypto_tape.symbol,
                        "market_direction": crypto_tape.direction,
                        **crypto_tape.features,
                    },
                )
            )
        route = self.evidence_router.route(claims)
        features["evidence_route"] = {
            "mode": route.mode,
            "direction": route.direction,
            "probability": None if route.probability is None else round(route.probability, 4),
            "leader": None if route.leader is None else route.leader.source_id,
            "reason": route.reason,
            "claims": [
                {
                    "source_id": c.source_id,
                    "source_type": c.source_type,
                    "direction": c.direction,
                    "probability": round(c.probability, 4),
                    "confidence": round(c.confidence, 4),
                    "reliability_winrate": (
                        None if c.reliability is None or c.reliability.winrate is None
                        else round(c.reliability.winrate, 4)
                    ),
                    "reliability_n": 0 if c.reliability is None else c.reliability.sample_size,
                    "wilson_lower": (
                        None if c.reliability is None or c.reliability.wilson_lower is None
                        else round(c.reliability.wilson_lower, 4)
                    ),
                }
                for c in claims
            ],
            "conflicts": [c.source_id for c in route.conflicts],
        }
        # Informed-only weighting: neutral signals (exactly 0.5) carry no weight
        # so they cannot dilute a single strong signal.  A component at 0.5 means
        # "no data" — it should be invisible, not drag the score toward the median.
        NEUTRAL = 0.5
        active_weight_sum = sum(
            max(0.0, weights[k])
            for k, v in components.items()
            if v != NEUTRAL
        )
        if active_weight_sum > 0:
            score = sum(
                max(0.0, weights[k]) * v
                for k, v in components.items()
                if v != NEUTRAL
            ) / active_weight_sum
        else:
            # All signals neutral — fall back to full-weight blend (will ≈ 0.5).
            active_weight_sum = sum(max(0.0, v) for v in weights.values()) or 1.0
            score = sum(max(0.0, weights[k]) * v for k, v in components.items()) / active_weight_sum
        score = round(max(0.0, min(1.0, score)), 4)

        # Identify the strongest individual signal for audit.  "Anchor" status
        # is reserved for EvidenceRouter solo experts with measured reliability;
        # a high unproven score is not enough to bypass consensus.
        anchor_threshold = _env_float("META_BRAIN_ANCHOR_THRESHOLD", 0.70)
        best_signal_key = max(components, key=lambda k: components[k])
        best_signal_val = components[best_signal_key]
        legacy_anchor_candidate = best_signal_val >= anchor_threshold
        has_anchor = route.mode == "solo"
        if route.mode == "solo":
            score = round(max(score, float(route.probability or 0.0)), 4)
        features["best_signal"] = best_signal_key
        features["best_signal_value"] = round(best_signal_val, 4)
        features["legacy_anchor_candidate"] = legacy_anchor_candidate
        features["has_anchor"] = has_anchor
        features["meta_score"] = score
        features["weighted_components"] = {
            **{k: round(v, 4) for k, v in components.items()},
            "winrate_sample_size": win_stats.total_with_outcome,
            "weights": weights,
            "active_weight_sum": round(active_weight_sum, 4),
        }

        # 7. Entry timing: "now" if strong signals converge; "wait" if marginal.
        entry_timing = self._compute_timing(
            brain_approved=brain_decision.approved,
            brain_score=brain_decision.score,
            conviction=conviction,
            velocity=velocity,
            win_stats=win_stats,
            cross_market_divergence=features.get("cross_market_divergence"),
            whale=whale,
            news=news,
            has_anchor=has_anchor,
        )

        if route.mode == "blocked":
            return MetaDecision(
                approved=False,
                reason=route.reason,
                score=score,
                entry_timing="skip",
                winrate_estimate=win_stats.winrate,
                winrate_sample_size=win_stats.total_with_outcome,
                signal_sources=signal_sources,
                cross_market_prob=features.get("cross_market_prob"),
                cross_market_divergence=features.get("cross_market_divergence"),
                velocity_direction=velocity.direction,
                velocity_pct_per_hour=velocity.pct_per_hour,
                conviction_direction=conviction.direction,
                conviction_confidence=conviction.confidence,
                conviction_sources=conviction.sources,
                features=features,
            )

        # 8. If brain gate rejected, propagate as skip.
        if not brain_decision.approved:
            return MetaDecision(
                approved=False,
                reason=brain_decision.reason,
                score=score,
                entry_timing="skip",
                winrate_estimate=win_stats.winrate,
                winrate_sample_size=win_stats.total_with_outcome,
                signal_sources=signal_sources,
                cross_market_prob=features.get("cross_market_prob"),
                cross_market_divergence=features.get("cross_market_divergence"),
                velocity_direction=velocity.direction,
                velocity_pct_per_hour=velocity.pct_per_hour,
                conviction_direction=conviction.direction,
                conviction_confidence=conviction.confidence,
                conviction_sources=conviction.sources,
                features=features,
            )

        # Score threshold: if a single strong "anchor" signal exists (≥ anchor_threshold),
        # the blended score requirement is relaxed — one strong signal should not be
        # vetoed by neutral ones.  Without an anchor, the full threshold applies.
        min_weighted_score = _env_float("META_BRAIN_MIN_WEIGHTED_SCORE", 0.50)
        min_weighted_score_with_anchor = _env_float("META_BRAIN_MIN_WEIGHTED_SCORE_ANCHOR", 0.40)
        effective_min = min_weighted_score_with_anchor if has_anchor else min_weighted_score
        if score < effective_min:
            return MetaDecision(
                approved=False,
                reason=(
                    f"weighted_score_too_low:{score:.3f}<{effective_min:.3f}"
                    + (f"[anchor:{best_signal_key}={best_signal_val:.2f}]" if has_anchor else "")
                ),
                score=score,
                entry_timing="skip",
                winrate_estimate=win_stats.winrate,
                winrate_sample_size=win_stats.total_with_outcome,
                signal_sources=signal_sources,
                cross_market_prob=features.get("cross_market_prob"),
                cross_market_divergence=features.get("cross_market_divergence"),
                velocity_direction=velocity.direction,
                velocity_pct_per_hour=velocity.pct_per_hour,
                conviction_direction=conviction.direction,
                conviction_confidence=conviction.confidence,
                conviction_sources=conviction.sources,
                features=features,
            )

        if poly_prob is not None:
            min_edge = _env_float("META_BRAIN_MIN_EDGE_PCT", 0.02)
            min_raw_ev = _env_float("META_BRAIN_MIN_RAW_EV", 0.04)
            market_price = max(0.0, min(1.0, float(poly_prob)))
            # Use cross_market_prob (Kalshi/Metaculus/Manifold consensus) as the
            # best estimate of true probability. It is a calibrated external
            # probability, not a composite quality score.  Fall back to the
            # composite score only when no cross-market data is available.
            cross_prob = features.get("cross_market_prob")
            probability_calibrated = False
            if route.mode == "solo" and route.probability is not None:
                internal_probability = max(0.0, min(1.0, float(route.probability)))
                internal_prob_source = route.reason
                probability_calibrated = True
            elif cross_prob is not None:
                internal_probability = max(0.0, min(1.0, float(cross_prob)))
                internal_prob_source = "cross_market"
                probability_calibrated = True
            else:
                internal_probability = score
                internal_prob_source = "meta_score_rank_only"
            edge = internal_probability - market_price
            raw_ev = binary_raw_ev(internal_probability, market_price)
            features["internal_probability"] = round(internal_probability, 4)
            features["internal_prob_source"] = internal_prob_source
            features["internal_probability_calibrated"] = probability_calibrated
            features["market_entry_price"] = round(market_price, 4)
            features["edge"] = round(edge, 4)
            features["raw_ev"] = round(raw_ev, 4)
            features["min_edge_pct"] = min_edge
            features["min_raw_ev"] = min_raw_ev
            if not probability_calibrated:
                return MetaDecision(
                    approved=False,
                    reason="rank_only_no_calibrated_probability",
                    score=score,
                    entry_timing="skip",
                    winrate_estimate=win_stats.winrate,
                    winrate_sample_size=win_stats.total_with_outcome,
                    signal_sources=signal_sources,
                    cross_market_prob=features.get("cross_market_prob"),
                    cross_market_divergence=features.get("cross_market_divergence"),
                    velocity_direction=velocity.direction,
                    velocity_pct_per_hour=velocity.pct_per_hour,
                    conviction_direction=conviction.direction,
                    conviction_confidence=conviction.confidence,
                    conviction_sources=conviction.sources,
                    features=features,
                )
            if edge < min_edge:
                return MetaDecision(
                    approved=False,
                    reason=f"internal_edge_too_low:{edge:.3f}<{min_edge:.3f}",
                    score=score,
                    entry_timing="skip",
                    winrate_estimate=win_stats.winrate,
                    winrate_sample_size=win_stats.total_with_outcome,
                    signal_sources=signal_sources,
                    cross_market_prob=features.get("cross_market_prob"),
                    cross_market_divergence=features.get("cross_market_divergence"),
                    velocity_direction=velocity.direction,
                    velocity_pct_per_hour=velocity.pct_per_hour,
                    conviction_direction=conviction.direction,
                    conviction_confidence=conviction.confidence,
                    conviction_sources=conviction.sources,
                    features=features,
                )
            if raw_ev < min_raw_ev:
                return MetaDecision(
                    approved=False,
                    reason=f"raw_ev_too_low:{raw_ev:.3f}<{min_raw_ev:.3f}",
                    score=score,
                    entry_timing="skip",
                    winrate_estimate=win_stats.winrate,
                    winrate_sample_size=win_stats.total_with_outcome,
                    signal_sources=signal_sources,
                    cross_market_prob=features.get("cross_market_prob"),
                    cross_market_divergence=features.get("cross_market_divergence"),
                    velocity_direction=velocity.direction,
                    velocity_pct_per_hour=velocity.pct_per_hour,
                    conviction_direction=conviction.direction,
                    conviction_confidence=conviction.confidence,
                    conviction_sources=conviction.sources,
                    features=features,
                )

        if _env_bool("META_BRAIN_EXECUTION_QUALITY_ENABLED", True):
            try:
                execution_quality = self.execution_quality.evaluate(
                    token_id=token_id,
                    intended_usdc=_env_float("META_BRAIN_EXECUTION_QUALITY_USDC", 3.0),
                    internal_probability=features.get("internal_probability"),
                    entry_price=features.get("market_entry_price") or poly_prob,
                )
                features.update(execution_quality.features)
                if not execution_quality.ok:
                    return MetaDecision(
                        approved=False,
                        reason=execution_quality.reason,
                        score=score,
                        entry_timing="skip",
                        winrate_estimate=win_stats.winrate,
                        winrate_sample_size=win_stats.total_with_outcome,
                        signal_sources=signal_sources,
                        cross_market_prob=features.get("cross_market_prob"),
                        cross_market_divergence=features.get("cross_market_divergence"),
                        velocity_direction=velocity.direction,
                        velocity_pct_per_hour=velocity.pct_per_hour,
                        conviction_direction=conviction.direction,
                        conviction_confidence=conviction.confidence,
                        conviction_sources=conviction.sources,
                        features=features,
                    )
            except Exception as exc:
                logger.debug("meta_brain: execution quality failed: %s", exc)
                if _env_bool("META_BRAIN_EXECUTION_QUALITY_FAIL_CLOSED", False):
                    features["execution_quality_error"] = f"{type(exc).__name__}: {exc}"
                    return MetaDecision(
                        approved=False,
                        reason="execution_quality_error",
                        score=score,
                        entry_timing="skip",
                        winrate_estimate=win_stats.winrate,
                        winrate_sample_size=win_stats.total_with_outcome,
                        signal_sources=signal_sources,
                        cross_market_prob=features.get("cross_market_prob"),
                        cross_market_divergence=features.get("cross_market_divergence"),
                        velocity_direction=velocity.direction,
                        velocity_pct_per_hour=velocity.pct_per_hour,
                        conviction_direction=conviction.direction,
                        conviction_confidence=conviction.confidence,
                        conviction_sources=conviction.sources,
                        features=features,
                    )

        return MetaDecision(
            approved=True,
            reason=route.reason if route.mode == "solo" else brain_decision.reason,
            score=score,
            entry_timing=entry_timing,
            winrate_estimate=win_stats.winrate,
            winrate_sample_size=win_stats.total_with_outcome,
            signal_sources=signal_sources,
            cross_market_prob=features.get("cross_market_prob"),
            cross_market_divergence=features.get("cross_market_divergence"),
            velocity_direction=velocity.direction,
            velocity_pct_per_hour=velocity.pct_per_hour,
            conviction_direction=conviction.direction,
            conviction_confidence=conviction.confidence,
            conviction_sources=conviction.sources,
            features=features,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _compute_timing(
        self,
        *,
        brain_approved: bool,
        brain_score: float,
        conviction: ConvictionSummary,
        velocity: VelocitySignal,
        win_stats: WinRateStats,
        cross_market_divergence: Optional[float],
        whale: Optional[WhaleSentimentSignal] = None,
        news: Optional[NewsSignal] = None,
        has_anchor: bool = False,
    ) -> str:
        if not brain_approved:
            return "skip"

        # A single trusted expert from EvidenceRouter is strong enough on its
        # own to trigger immediate entry.  Unproven high scores remain in the
        # normal consensus path.
        if has_anchor:
            return "now"

        strong_signals = 0

        # Cross-market divergence >= 10% is a strong pricing edge.
        if cross_market_divergence is not None and abs(cross_market_divergence) >= 0.10:
            strong_signals += 1

        # Multiple conviction sources with decent confidence.
        if (
            conviction.direction in ("yes", "no")
            and conviction.confidence >= 0.55
            and len(conviction.sources) >= 2
        ):
            strong_signals += 1

        # Recent positive win-rate from history.
        if win_stats.winrate is not None and win_stats.winrate >= 0.55 and win_stats.total_with_outcome >= 5:
            strong_signals += 1

        # Velocity momentum (market is moving in a tradeable direction).
        if velocity.direction in ("rising", "falling"):
            strong_signals += 1

        # High brain score on its own (>= 0.72 → single strong signal).
        if brain_score >= 0.72:
            strong_signals += 1

        # Strong whale consensus (>= 60% confident, at least 1 profitable whale).
        if (
            whale is not None
            and whale.direction is not None
            and whale.confidence >= 0.60
            and whale.n_whales >= 1
        ):
            strong_signals += 1

        # Breaking news with clear directional consensus (>= 60% confidence).
        if (
            news is not None
            and news.direction is not None
            and news.confidence >= 0.60
            and news.n_items >= 2
        ):
            strong_signals += 1

        if strong_signals >= 2:
            return "now"
        if brain_score >= 0.55:
            return "wait"
        return "skip"
