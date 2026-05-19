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
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


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
       A "win" = outcome_status IN ('closed_take_profit', 'resolved_yes', 'resolved_skipped_no').
       A "loss" = outcome_status IN ('closed_stop_loss', 'resolved_loss', 'resolved_no').
    2. trades table — fallback, counts closed_take_profit vs closed_stop_loss.

    Results are cached for 5 minutes per (db_path, hours) combination.
    """

    WIN_OUTCOMES = frozenset({
        "closed_take_profit", "resolved_yes", "resolved_skipped_no",
    })
    LOSS_OUTCOMES = frozenset({
        "closed_stop_loss", "closed_timeout", "resolved_loss", "resolved_no",
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
                return self._from_brain_decisions(conn, market_type, hours)
        except Exception as exc:
            logger.debug("winrate_advisor: sqlite error: %s", exc)
            return WinRateStats(None, 0, 0, 0, None, None, "error")

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
              AND ts >= ?
            """,
            (cutoff,),
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
            try:
                import re
                ts_str = r.get("ts") or r.get("timestamp") or ""
                digits = re.sub(r"[^0-9]", "", ts_str[:19])
                return int(digits) if digits else 0
            except Exception:
                return 0

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
        latest_ts_int = _ts(best_records[0])
        now_int = int(time.strftime("%Y%m%d%H%M%S", time.gmtime()))
        age = max(0.0, float(now_int - latest_ts_int))

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

            import re
            def _parse_ts(s: str) -> float:
                try:
                    return float(re.sub(r"[^0-9]", "", s[:19])[:14])
                except Exception:
                    return 0.0

            dt_hours = max(
                (_parse_ts(last_ts) - _parse_ts(first_ts)) / 1e6, 1e-6
            )
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
        self.conviction_reader = ConvictionJSONLReader(conviction_paths=conviction_paths)
        self.velocity_detector = ProbVelocityDetector(window_minutes=velocity_window_min)

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

        # 5. Composite score: blend brain score with external conviction.
        score = brain_decision.score
        if conviction.direction in ("yes", "no") and conviction.confidence > 0:
            # Conviction from JSONL adds up to +0.10 if confident.
            score = min(1.0, score + min(0.10, conviction.confidence * 0.15))
        if velocity.direction == "rising" and (poly_prob or 0) < 0.85:
            score = min(1.0, score + 0.05)
        elif velocity.direction == "falling" and (poly_prob or 0) > 0.15:
            score = min(1.0, score + 0.05)  # falling = shorting opportunity

        score = round(score, 4)
        features["meta_score"] = score

        # 6. Entry timing: "now" if strong signals converge; "wait" if marginal.
        entry_timing = self._compute_timing(
            brain_approved=brain_decision.approved,
            brain_score=brain_decision.score,
            conviction=conviction,
            velocity=velocity,
            win_stats=win_stats,
            cross_market_divergence=features.get("cross_market_divergence"),
        )

        # 7. If brain gate rejected, propagate as skip.
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

        return MetaDecision(
            approved=True,
            reason=brain_decision.reason,
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
    ) -> str:
        if not brain_approved:
            return "skip"

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

        if strong_signals >= 2:
            return "now"
        if brain_score >= 0.55:
            return "wait"
        return "skip"
