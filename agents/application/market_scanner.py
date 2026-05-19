"""Market Scanner — proactive 1-minute opportunity finder.

Runs every SCANNER_POLL_SEC seconds (default 60 = 1 min).  Each cycle:

1. Fetches the most liquid active markets from Gamma (top SCANNER_MARKET_LIMIT).
2. Pre-filters with MarketBrain.evaluate_general_entry() (spread, horizon, price).
3. Searches Tavily for recent news on each surviving market.
4. Queries ManifoldDivergenceProvider for external probability divergence.
5. Scores and routes opportunities to the appropriate agent:

   ┌─────────────────────┬──────────────────────────────────────────────────────┐
   │  Agent              │  Routing criterion                                   │
   ├─────────────────────┼──────────────────────────────────────────────────────┤
   │  news_shock         │  Tavily found material news (materiality ≥ 0.4)      │
   │  near_resolution    │  hours_to_close ≤ 24 AND confidence ≥ 0.60           │
   │  trade (main LLM)   │  Overall score ≥ SCANNER_MIN_TRADE_SCORE             │
   └─────────────────────┴──────────────────────────────────────────────────────┘

Output to existing DB tables (no new schema):
  • brain_decisions  — conviction gate in trade.py reads these
  • news_signals     — news_shock agent reads these; scanner writes when
                       Tavily surfaced a material headline

Win-rate tracking: relies on existing brain_decisions outcome columns
(outcome_status / outcome_json) set by settlement_reconciler when a
market resolves. Run `scripts/python/cli.py brain-report` to see results.

AGENT_GOALS is the canonical definition of what each agent is trying to
achieve.  It is exported for use by the trading_supervisor status file.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from agents.application.market_brain import BrainConfig, MarketBrain
from agents.application.tavily import tavily_headlines, tavily_confidence
from agents.application.trade_log import TradeLog
from agents.application.trading_policy import MARKET_SCAN_SECONDS

logger = logging.getLogger(__name__)

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

# ---------------------------------------------------------------------------
# Agent goals registry
# ---------------------------------------------------------------------------

AGENT_GOALS: dict[str, dict] = {
    "trade": {
        "strategy": "psychological_bias_exploitation",
        "goal": (
            "Exploit crowd mispricing in liquid binary markets via LLM analysis. "
            "Enter when the LLM detects systematic anchoring or recency bias. "
            "Target: win-rate > 55%, avg hold < 6h, min liquidity $10k."
        ),
        "entry_criteria": (
            "Brain score >= 0.30, brain gate pass, conviction gate pass, "
            "LLM confidence >= min_confidence, exitable size check pass."
        ),
        "exit_criteria": "Exit as fast as profit/risk allows: brain re-check every minute, TP from +5%, hard cap +25%, stop-loss -3%, hard max 6h, hold only with strong forecast.",
        "poll_seconds": int(os.getenv("TRADER_POLL_SEC", str(MARKET_SCAN_SECONDS))),
    },
    "near_resolution": {
        "strategy": "resolution_bias_exploitation",
        "goal": (
            "Exploit last-mile momentum + crowd anchoring in markets closing within 24h. "
            "Crowds anchor to 50/50 even when the outcome is clear; fade the anchor. "
            "Target: win-rate > 60%, avg hold < 2h, fast entry on directional signal."
        ),
        "entry_criteria": (
            "Resolution in 0.5h–24h, Tavily/LLM direction confidence >= 0.65, "
            "liquidity >= $5k, entry price in [0.10, 0.65]."
        ),
        "exit_criteria": "Minute brain re-check, fast TP from +5%, hard cap +25%, stop-loss -3%, or natural resolution.",
        "poll_seconds": int(os.getenv("NEAR_RESOLUTION_POLL_SEC", "300")),
    },
    "news_shock": {
        "strategy": "news_driven_entry",
        "goal": (
            "Enter before the crowd re-prices after material news events. "
            "Speed is the edge: the window is 30 minutes after a market-moving headline. "
            "Target: entry < 30 min after news, win-rate > 50%."
        ),
        "entry_criteria": (
            "News signal materiality >= 0.5, signal age < 30 min, "
            "price drift since signal < 10%, liquidity >= min_liquidity."
        ),
        "exit_criteria": "Minute brain re-check, fast TP from +5%, hard cap +25%, stop-loss -3%, hard max 6h.",
        "poll_seconds": int(os.getenv("NEWS_SHOCK_POLL_SEC", "60")),
    },
    "wallet_follow": {
        "strategy": "whale_following",
        "goal": (
            "Mirror proven whale wallets in liquid markets. "
            "Whale signal = statistical edge from a proven track record. "
            "Target: follow whales with > 60% 30-day win-rate, min 5 trades."
        ),
        "entry_criteria": (
            "Wallet >= 5 trades in 30d, signal age < 1h, max drift 10%, "
            "min liquidity $3k."
        ),
        "exit_criteria": "Fast TP from +5%, hard cap +25%, stop-loss -3%.",
        "poll_seconds": int(os.getenv("WALLET_FOLLOW_POLL_SEC", "60")),
    },
    "btc_daily": {
        "strategy": "btc_mean_reversion",
        "goal": (
            "Fade BTC 24h crowd overreactions. Enter OPPOSITE the daily trend after "
            "a > 3% move; crowds over-extrapolate short-term momentum into the daily "
            "binary (UP/DOWN). Target: mean-revert within the day, win-rate > 55%."
        ),
        "entry_criteria": (
            "BTC 24h move > 3%, max entry price 0.65, "
            "no fundamental news (ETF/hack/regulation) detected by Tavily."
        ),
        "exit_criteria": "Fast TP from +5%, hard cap +25%, stop-loss -3%, timeout EOD.",
        "poll_seconds": int(os.getenv("BTC_DAILY_POLL_SEC", "900")),
    },
    "scalper": {
        "strategy": "crypto_15m_arb",
        "goal": (
            "Capture mathematical edge on crypto 15-min UP/DOWN markets when the "
            "pair ask sum < 1.04. This is pure arb — not directional. "
            "Target: > 55% win-rate, hold < 10 min."
        ),
        "entry_criteria": (
            "Pair ask sum < 1.04, entry price < 0.55, > 90s to expiry, "
            "brain edge score >= 0.35."
        ),
        "exit_criteria": "Take-profit +5%, trailing stop 2%, stop-loss -3%.",
        "poll_seconds": int(os.getenv("SCALPER_POLL_SEC", "30")),
    },
    "external_conviction": {
        "strategy": "multi_source_conviction",
        "goal": (
            "Aggregate 11+ external forecasting sources (Manifold, Metaculus, Kalshi, "
            "Polymarket CLOB whales, public news, Tavily, LLM) to find consensus edge. "
            "Confidence >= 0.58 with cross-source agreement = high-confidence entry. "
            "Target: divergence from Poly price > 5%, min volume $5k."
        ),
        "entry_criteria": (
            "Aggregate confidence >= 0.58, min_volume $5k, "
            "price in [0.12, 0.88]."
        ),
        "exit_criteria": "Timeout 60 min, fast TP +5%, stop-loss -3%.",
        "poll_seconds": int(os.getenv("EXTERNAL_CONVICTION_POLL_SEC", "10800")),
    },
    "market_scanner": {
        "strategy": "proactive_opportunity_discovery",
        "goal": (
            "Proactively scan the most liquid Polymarket markets every minute. "
            "Score each market with brain + Tavily + Manifold divergence. "
            "Route approved signals to the right agent via brain_decisions (conviction "
            "gate) and news_signals (news_shock). Never places orders directly."
        ),
        "entry_criteria": "N/A — discovery only.",
        "exit_criteria": "N/A — discovery only.",
        "poll_seconds": int(os.getenv("SCANNER_POLL_SEC", str(MARKET_SCAN_SECONDS))),
    },
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass
class ScannerConfig:
    poll_seconds: int = MARKET_SCAN_SECONDS
    market_limit: int = 120
    max_candidates: int = 30
    # Market quality gates (applied before any API calls).
    min_liquidity_usdc: float = 5_000.0
    min_volume_usdc: float = 3_000.0
    min_price: float = 0.10
    max_price: float = 0.90
    max_spread_pct: float = 0.14
    max_hours_to_close: float = 168.0
    min_hours_to_close: float = 0.5
    # Routing thresholds.
    min_trade_score: float = 0.55      # brain score to write trade brain_decision
    min_near_res_hours: float = 0.5
    max_near_res_hours: float = 24.0
    near_res_confidence: float = 0.60
    news_shock_materiality: float = 0.40  # Tavily confidence to write news_signal
    # Manifold divergence gate: only worth calling if spread exists.
    manifold_min_divergence: float = 0.07
    # Output.
    heartbeat_path: str = "/app/data/scanner_heartbeat"
    # Manifold: disable if you don't want external calls during tests.
    manifold_enabled: bool = True

    @classmethod
    def from_env(cls) -> "ScannerConfig":
        return cls(
            poll_seconds=_env_int("SCANNER_POLL_SEC", MARKET_SCAN_SECONDS),
            market_limit=_env_int("SCANNER_MARKET_LIMIT", 120),
            max_candidates=_env_int("SCANNER_MAX_CANDIDATES", 30),
            min_liquidity_usdc=_env_float("SCANNER_MIN_LIQUIDITY_USDC", 5_000.0),
            min_volume_usdc=_env_float("SCANNER_MIN_VOLUME_USDC", 3_000.0),
            min_price=_env_float("SCANNER_MIN_PRICE", 0.10),
            max_price=_env_float("SCANNER_MAX_PRICE", 0.90),
            max_spread_pct=_env_float("SCANNER_MAX_SPREAD_PCT", 0.14),
            max_hours_to_close=_env_float("SCANNER_MAX_HOURS_TO_CLOSE", 168.0),
            min_hours_to_close=_env_float("SCANNER_MIN_HOURS_TO_CLOSE", 0.5),
            min_trade_score=_env_float("SCANNER_MIN_TRADE_SCORE", 0.55),
            min_near_res_hours=_env_float("SCANNER_MIN_NEAR_RES_HOURS", 0.5),
            max_near_res_hours=_env_float("SCANNER_MAX_NEAR_RES_HOURS", 24.0),
            near_res_confidence=_env_float("SCANNER_NEAR_RES_CONFIDENCE", 0.60),
            news_shock_materiality=_env_float("SCANNER_NEWS_SHOCK_MATERIALITY", 0.40),
            manifold_min_divergence=_env_float("SCANNER_MANIFOLD_MIN_DIVERGENCE", 0.07),
            heartbeat_path=os.getenv(
                "SCANNER_HEARTBEAT_PATH", "/app/data/scanner_heartbeat"
            ),
            manifold_enabled=_env_bool("SCANNER_MANIFOLD_ENABLED", True),
        )


# ---------------------------------------------------------------------------
# Market scanner engine
# ---------------------------------------------------------------------------

class MarketScanner:
    """Proactive opportunity scanner.

    scan_once() is a pure orchestration loop — fetch → filter → score → route.
    All expensive network calls (Tavily, Manifold) gracefully return "" / None
    on error so a single bad API response never aborts the cycle.
    """

    def __init__(
        self,
        cfg: Optional[ScannerConfig] = None,
        trade_log: Optional[TradeLog] = None,
        brain: Optional[MarketBrain] = None,
        meta_brain=None,
    ):
        self.cfg = cfg or ScannerConfig.from_env()
        self.trade_log = trade_log or TradeLog()
        self.brain = brain or MarketBrain(BrainConfig.from_env())
        # MetaBrain: single synthesizing layer (wraps brain + win-rate + velocity + conviction).
        if meta_brain is None:
            from agents.application.meta_brain import MetaBrain
            meta_brain = MetaBrain(
                db_path=os.getenv("TRADE_LOG_PATH", "./data/poly1.db"),
                market_brain=self.brain,
            )
        self.meta_brain = meta_brain

    # ---------------------------------------------------------------- public

    def scan_once(self) -> dict:
        now = datetime.now(timezone.utc)
        result: dict = {
            "ts": now.isoformat(),
            "fetched": 0,
            "filtered": 0,
            "brain_approved": 0,
            "dispatched_trade": 0,
            "dispatched_near_resolution": 0,
            "dispatched_news_shock": 0,
        }

        # 1. Fetch liquid markets from Gamma.
        raw_markets = self._fetch_markets()
        result["fetched"] = len(raw_markets)
        if not raw_markets:
            logger.warning("scanner: Gamma fetch returned 0 markets")
            return result

        # 2. Coarse filter — quality gates before any API calls.
        candidates = self._filter_candidates(raw_markets, now)
        result["filtered"] = len(candidates)
        logger.info(
            "scanner: fetched=%d filtered=%d",
            result["fetched"], result["filtered"],
        )

        # 3. Score each candidate.
        for mkt in candidates[: self.cfg.max_candidates]:
            try:
                self._process_market(mkt, now, result)
            except Exception:
                logger.exception(
                    "scanner: unhandled error for market %s", mkt.get("id", "?")
                )

        self._heartbeat()
        logger.info("scanner: cycle done — %s", json.dumps(result))
        return result

    # -------------------------------------------------------------- private

    def _fetch_markets(self) -> list[dict]:
        try:
            params = urllib.parse.urlencode({
                "active": "true",
                "closed": "false",
                "limit": self.cfg.market_limit,
                "order": "volume24hr",
                "ascending": "false",
            })
            url = f"{GAMMA_MARKETS_URL}?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "poly1-scanner/1.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            logger.warning("scanner: Gamma fetch failed: %s", exc)
            return []

    def _filter_candidates(self, markets: list[dict], now: datetime) -> list[dict]:
        """Quick coarse filter — no API calls here."""
        out = []
        for m in markets:
            if m.get("active") is False or m.get("closed") is True:
                continue
            # Price range
            try:
                prices = json.loads(m.get("outcomePrices") or '["0.5","0.5"]')
                yes_p = float(prices[0])
            except Exception:
                continue
            if not (self.cfg.min_price <= yes_p <= self.cfg.max_price):
                continue
            # Liquidity
            liq = _safe_float(
                m.get("liquidityClob") or m.get("liquidity") or m.get("liquidityNum")
            )
            if liq < self.cfg.min_liquidity_usdc:
                continue
            # Volume
            vol = _safe_float(
                m.get("volume24hr") or m.get("volumeClob") or m.get("volume")
            )
            if vol < self.cfg.min_volume_usdc:
                continue
            # Time to close
            hours_to_close = _hours_to_close(m, now)
            if hours_to_close is not None:
                if (
                    hours_to_close < self.cfg.min_hours_to_close
                    or hours_to_close > self.cfg.max_hours_to_close
                ):
                    continue
            out.append(m)
        return out

    def _process_market(self, mkt: dict, now: datetime, result: dict) -> None:
        market_id = str(
            mkt.get("conditionId") or mkt.get("condition_id") or mkt.get("id") or ""
        )
        question = str(mkt.get("question") or "").strip()
        if not market_id or not question:
            return

        try:
            prices = json.loads(mkt.get("outcomePrices") or '["0.5","0.5"]')
            yes_price = float(prices[0])
        except Exception:
            yes_price = 0.5

        spread_pct = _spread_pct(mkt)
        hours_to_close = _hours_to_close(mkt, now)
        liq = _safe_float(
            mkt.get("liquidityClob") or mkt.get("liquidity") or mkt.get("liquidityNum")
        )

        # a. Vibe analysis — optional technical indicators for crypto markets.
        vibe_signals = None
        if _is_crypto_slug(question):
            try:
                vibe_signals = _compute_vibe_for_scanner(market_id, question)
            except Exception:
                logger.debug("scanner: vibe failed for %s", market_id[:20])

        # b. MetaBrain synthesis: gate + win-rate + cross-market + conviction + velocity.
        meta = self.meta_brain.synthesize(
            market_id=market_id,
            question=question,
            spread_pct=spread_pct,
            hours_to_close=hours_to_close,
            poly_prob=yes_price,
            external_context="",  # Tavily context not yet fetched at this stage
            vibe_signals=vibe_signals,
            token_id=None,
            liquidity_usdc=liq,
        )
        if not meta.approved:
            return
        result["brain_approved"] += 1
        logger.debug("scanner: meta_brain: %s", meta.summary)

        # c. Tavily news search — cheap, no LLM.
        tavily_ctx = ""
        tavily_direction, tavily_confidence_val = "", 0.0
        if question:
            tavily_ctx = tavily_headlines(question, max_results=3)
            if tavily_ctx:
                tavily_direction, tavily_confidence_val = tavily_confidence(
                    query=question,
                    direction_keywords_yes=["wins", "passes", "approved", "rises", "yes"],
                    direction_keywords_no=["loses", "fails", "rejected", "falls", "no"],
                )

        # d. Manifold divergence check (kept for near-resolution routing logic;
        #    MetaBrain already ran cross-market fetch — reuse if available).
        manifold_divergence = float(meta.cross_market_divergence or 0.0)
        manifold_source = ",".join(meta.signal_sources) if meta.signal_sources else ""
        if manifold_divergence == 0.0 and self.cfg.manifold_enabled:
            try:
                manifold_divergence, manifold_source = self._manifold_check(
                    question, yes_price, market_id
                )
            except Exception:
                pass

        # e. Opportunity score — use meta.score (already incorporates conviction + velocity).
        base_score = meta.score
        tavily_boost = 0.10 if tavily_ctx else 0.0
        opportunity_score = min(1.0, base_score + tavily_boost)

        features = {
            "yes_price": round(yes_price, 4),
            "spread_pct": round(spread_pct, 4) if spread_pct else None,
            "hours_to_close": round(hours_to_close, 2) if hours_to_close else None,
            "liquidity_usdc": round(liq, 0),
            "brain_score": round(meta.score, 4),
            "meta_timing": meta.entry_timing,
            "meta_winrate": meta.winrate_estimate,
            "meta_winrate_n": meta.winrate_sample_size,
            "meta_conviction": meta.conviction_direction,
            "meta_velocity": meta.velocity_direction,
            "meta_signal_sources": meta.signal_sources,
            "tavily_direction": tavily_direction,
            "tavily_confidence": round(tavily_confidence_val, 3),
            "tavily_preview": tavily_ctx[:120] if tavily_ctx else "",
            "manifold_divergence": round(manifold_divergence, 4),
            "opportunity_score": round(opportunity_score, 4),
        }
        if meta.features.get("weighted_components"):
            features["weighted_components"] = meta.features["weighted_components"]

        # f. Route to agents.

        # Route 1: trade — write brain_decision so conviction gate sees it.
        if opportunity_score >= self.cfg.min_trade_score:
            self.trade_log.insert_brain_decision(
                agent="market_scanner",
                strategy="scanner_trade_opportunity",
                decision_type="entry",
                market_id=market_id,
                approved=True,
                reason=f"scanner_approved score={opportunity_score:.3f}",
                score=opportunity_score,
                market_type="general_binary",
                features=features,
                action="BUY",
            )
            result["dispatched_trade"] += 1
            logger.info(
                "scanner → trade: market=%s score=%.3f %s",
                market_id[:20], opportunity_score, question[:60],
            )

        # Route 2: near_resolution — write news_signal so near_resolution agent sees it.
        if (
            hours_to_close is not None
            and self.cfg.min_near_res_hours <= hours_to_close <= self.cfg.max_near_res_hours
            and (tavily_confidence_val >= self.cfg.near_res_confidence
                 or manifold_divergence >= self.cfg.manifold_min_divergence)
        ):
            direction_for_nr = tavily_direction if tavily_direction in ("yes", "no") else (
                "yes" if manifold_divergence > 0 else "no"
            )
            self.trade_log.insert_news_signal(
                headline=f"[scanner] {question[:100]}",
                market_id=market_id,
                direction=direction_for_nr,
                materiality=round(max(tavily_confidence_val, abs(manifold_divergence)), 3),
                relevance_score=round(opportunity_score, 3),
                status="scanner_near_resolution",
                source="market_scanner",
                market_question=question,
                reasoning=(
                    f"hours_to_close={hours_to_close:.1f} "
                    f"tavily_confidence={tavily_confidence_val:.2f} "
                    f"manifold_divergence={manifold_divergence:+.3f}"
                ),
                yes_price=yes_price,
            )
            result["dispatched_near_resolution"] += 1
            logger.info(
                "scanner → near_resolution: market=%s h=%.1fh dir=%s",
                market_id[:20], hours_to_close, direction_for_nr,
            )

        # Route 3: news_shock — write news_signal when Tavily found material news.
        # Map scanner direction ('yes'/'no') to news_shock vocabulary ('bullish'/'bearish').
        if tavily_confidence_val >= self.cfg.news_shock_materiality and tavily_direction:
            direction_for_ns = "bullish" if tavily_direction == "yes" else "bearish"
            self.trade_log.insert_news_signal(
                headline=f"[scanner/tavily] {question[:100]}",
                market_id=market_id,
                direction=direction_for_ns,
                materiality=round(tavily_confidence_val, 3),
                relevance_score=round(opportunity_score, 3),
                status="scanner_news_shock",
                source="scanner_tavily",
                market_question=question,
                reasoning=f"Tavily context: {tavily_ctx[:200]}",
                yes_price=yes_price,
            )
            result["dispatched_news_shock"] += 1
            logger.info(
                "scanner → news_shock: market=%s dir=%s conf=%.2f",
                market_id[:20], tavily_direction, tavily_confidence_val,
            )

    def _manifold_check(
        self, question: str, poly_prob: float, market_id: str
    ) -> tuple[float, str]:
        """Returns (divergence, source_slug). Divergence > 0 means Manifold
        thinks YES is more likely than Polymarket does. Fails silently."""
        try:
            query = urllib.parse.quote(question[:80])
            url = f"https://api.manifold.markets/v0/search-markets?term={query}&limit=1"
            req = urllib.request.Request(
                url, headers={"User-Agent": "poly1-scanner/1.0"}
            )
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read())
            if not data:
                return 0.0, ""
            best = data[0]
            manifold_prob = float(best.get("probability", 0))
            if manifold_prob <= 0 or manifold_prob >= 1:
                return 0.0, ""
            divergence = manifold_prob - poly_prob
            slug = str(best.get("slug", ""))
            logger.debug(
                "scanner: manifold mkt=%s poly=%.3f manifold=%.3f div=%+.3f",
                market_id[:20], poly_prob, manifold_prob, divergence,
            )
            return divergence, slug
        except Exception as exc:
            logger.debug("scanner: manifold check failed for %s: %s", market_id[:20], exc)
            return 0.0, ""

    def _heartbeat(self) -> None:
        try:
            p = Path(self.cfg.heartbeat_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Vibe analysis (optional technical indicators on probability series)
# ---------------------------------------------------------------------------

_CRYPTO_KEYWORDS = frozenset({
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto",
    "xrp", "dogecoin", "doge",
})


def _is_crypto_slug(question: str) -> bool:
    """Return True if the market question mentions crypto assets."""
    q = (question or "").lower()
    return any(kw in q for kw in _CRYPTO_KEYWORDS)


def _compute_vibe_for_scanner(
    condition_id: str, question: str
) -> Optional[dict]:
    """Fetch CLOB probability history, run composite signal, return dict
    with {direction, confidence} or None on failure."""
    from agents.application.external_conviction import MarketSnapshot, _fetch_probability_history
    from agents.application.vibe_analysis import probability_technical_composite

    # Build a minimal MarketSnapshot for _fetch_probability_history
    # We need a token_id; for scanner markets we use condition_id as token proxy
    # (the CLOB endpoint accepts condition_id in some contexts).
    # In practice, the scanner doesn't have clobTokenIds easily,
    # so we try condition_id directly.
    dummy = MarketSnapshot(
        market_id=condition_id,
        question=question,
        slug="",
        yes_price=0.5,
        no_price=0.5,
        volume_usdc=0,
        liquidity_usdc=0,
        end_date="",
        outcomes=["Yes", "No"],
        tokens=[condition_id],  # use condition_id as token placeholder
        category="crypto",
        raw={},
    )
    prices = _fetch_probability_history(dummy)
    if not prices or len(prices) < 30:
        return None
    composite = probability_technical_composite(prices, min_bars=30)
    if composite is None:
        return None
    return {
        "direction": composite.get("direction", "skip"),
        "confidence": composite.get("confidence", 0.0),
        "agreement": composite.get("agreement", 0.0),
        "contributing_count": composite.get("contributing_count", 0),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _spread_pct(mkt: dict) -> Optional[float]:
    try:
        return float(mkt["spread"])
    except (KeyError, TypeError, ValueError):
        pass
    try:
        prices = json.loads(mkt.get("outcomePrices") or '["0.5","0.5"]')
        yes_p = float(prices[0])
        no_p = float(prices[1])
        # Spread as fraction of mid.
        mid = (yes_p + no_p) / 2.0
        return abs(yes_p - no_p) / max(mid, 0.001)
    except Exception:
        return None


def _hours_to_close(mkt: dict, now: datetime) -> Optional[float]:
    raw = mkt.get("endDate") or mkt.get("end_date") or mkt.get("end") or ""
    if not raw:
        return None
    try:
        end_dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        delta = (end_dt - now).total_seconds()
        return max(0.0, delta / 3600.0)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class ScannerDaemon:
    def __init__(self, db_path: Optional[str] = None):
        self.cfg = ScannerConfig.from_env()
        self.engine = MarketScanner(
            cfg=self.cfg,
            trade_log=TradeLog(db_path=db_path),
        )
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            signal.signal(signal.SIGTERM, lambda *_: self.stop())
            signal.signal(signal.SIGINT, lambda *_: self.stop())
        except (ValueError, OSError):
            pass
        logger.info(
            "ScannerDaemon: starting poll=%ss", self.cfg.poll_seconds
        )
        try:
            while not self._stop.is_set():
                try:
                    result = self.engine.scan_once()
                    logger.info("scanner cycle: %s", result)
                except Exception:
                    logger.exception("scanner cycle failed")
                self._stop.wait(self.cfg.poll_seconds)
        finally:
            logger.info("ScannerDaemon: exited")


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="poly1 market scanner")
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument("--json", action="store_true", help="print JSON result")
    parser.add_argument("--goals", action="store_true", help="print AGENT_GOALS and exit")
    parser.add_argument("--db", default=None, help="override DB path")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.goals:
        print(json.dumps(AGENT_GOALS, indent=2))
        return 0

    if args.once:
        scanner = MarketScanner(trade_log=TradeLog(db_path=args.db))
        result = scanner.scan_once()
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"scanner: {result}")
        return 0

    ScannerDaemon(db_path=args.db).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
