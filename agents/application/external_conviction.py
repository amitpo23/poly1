"""External conviction shadow agent.

Scans liquid, active Polymarket markets and asks an external analysis layer for
a short-horizon opinion. The result is a shadow-only trade plan written to
``data/external_convictions.jsonl`` and ``brain_decisions``.

This module never places orders. External tools are treated as research input;
poly1 remains responsible for execution, risk, and live approval.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import logging
import os
import signal
import threading
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from agents.application.trade_log import TradeLog
from agents.utils.notify import notify_trade, _safe_balance


logger = logging.getLogger(__name__)
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"


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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ExternalConvictionConfig:
    poll_sec: int = 10800
    market_limit: int = 200
    max_candidates: int = 12
    min_volume_usdc: float = 5000.0
    min_liquidity_usdc: float = 500.0
    min_price: float = 0.12
    max_price: float = 0.88
    min_confidence: float = 0.58
    take_profit_pct: float = 0.10
    stop_loss_pct: float = 0.07
    max_hold_minutes: int = 60
    position_size_usdc: float = 3.0
    max_live_trades_per_cycle: int = 1
    max_open_positions: int = 1
    output_path: str = "./data/external_convictions.jsonl"
    heartbeat_path: str = "/app/data/external_conviction_heartbeat"
    agent_name: str = "external_conviction"
    strategy_name: str = "event_probability_scalping"
    provider: str = "heuristic"
    api_url: str = ""
    api_key_env: str = "EXTERNAL_CONVICTION_API_KEY"
    polifly_bridge_url: str = ""
    polifly_api_key_env: str = "POLIFLY_BROWSER_BRIDGE_API_KEY"
    execute: bool = False

    @classmethod
    def from_env(cls) -> "ExternalConvictionConfig":
        return cls(
            poll_sec=_env_int("EXTERNAL_CONVICTION_POLL_SEC", 10800),
            market_limit=_env_int("EXTERNAL_CONVICTION_MARKET_LIMIT", 200),
            max_candidates=_env_int("EXTERNAL_CONVICTION_MAX_CANDIDATES", 12),
            min_volume_usdc=_env_float("EXTERNAL_CONVICTION_MIN_VOLUME_USDC", 5000.0),
            min_liquidity_usdc=_env_float("EXTERNAL_CONVICTION_MIN_LIQUIDITY_USDC", 500.0),
            min_price=_env_float("EXTERNAL_CONVICTION_MIN_PRICE", 0.12),
            max_price=_env_float("EXTERNAL_CONVICTION_MAX_PRICE", 0.88),
            min_confidence=_env_float("EXTERNAL_CONVICTION_MIN_CONFIDENCE", 0.58),
            take_profit_pct=_env_float("EXTERNAL_CONVICTION_TAKE_PROFIT_PCT", 0.10),
            stop_loss_pct=_env_float("EXTERNAL_CONVICTION_STOP_LOSS_PCT", 0.07),
            max_hold_minutes=_env_int("EXTERNAL_CONVICTION_MAX_HOLD_MINUTES", 60),
            position_size_usdc=_env_float("EXTERNAL_CONVICTION_POSITION_SIZE_USDC", 3.0),
            max_live_trades_per_cycle=_env_int(
                "EXTERNAL_CONVICTION_MAX_LIVE_TRADES_PER_CYCLE", 1
            ),
            max_open_positions=_env_int("EXTERNAL_CONVICTION_MAX_OPEN_POSITIONS", 1),
            output_path=os.getenv(
                "EXTERNAL_CONVICTION_OUTPUT_PATH", "./data/external_convictions.jsonl"
            ),
            heartbeat_path=os.getenv(
                "EXTERNAL_CONVICTION_HEARTBEAT_PATH",
                "/app/data/external_conviction_heartbeat",
            ),
            agent_name=os.getenv("EXTERNAL_CONVICTION_AGENT_NAME", "external_conviction"),
            strategy_name=os.getenv(
                "EXTERNAL_CONVICTION_STRATEGY_NAME",
                "event_probability_scalping",
            ),
            provider=os.getenv("EXTERNAL_CONVICTION_PROVIDER", "heuristic"),
            api_url=os.getenv("EXTERNAL_CONVICTION_API_URL", ""),
            api_key_env=os.getenv(
                "EXTERNAL_CONVICTION_API_KEY_ENV", "EXTERNAL_CONVICTION_API_KEY"
            ),
            polifly_bridge_url=os.getenv("POLIFLY_BROWSER_BRIDGE_URL", ""),
            polifly_api_key_env=os.getenv(
                "POLIFLY_BROWSER_BRIDGE_API_KEY_ENV",
                "POLIFLY_BROWSER_BRIDGE_API_KEY",
            ),
            execute=os.getenv(
                "EXECUTE_EXTERNAL_CONVICTION", "false"
            ).lower() in {"1", "true", "yes", "on"},
        )


@dataclass
class MarketSnapshot:
    market_id: str
    question: str
    slug: str
    yes_price: float
    no_price: float
    volume_usdc: float
    liquidity_usdc: float
    end_date: str
    outcomes: list[str]
    tokens: list[str]
    category: str
    raw: dict


@dataclass
class ExternalVerdict:
    direction: str
    confidence: float
    source: str
    reason: str
    evidence: dict


@dataclass
class ShadowTradePlan:
    ts: str
    market_id: str
    question: str
    action: str
    side: str
    token_id: Optional[str]
    entry_price: float
    take_profit: float
    stop_loss: float
    max_hold_minutes: int
    confidence: float
    source: str
    reason: str
    volume_usdc: float
    liquidity_usdc: float
    status: str


def _json_list(raw, fallback: Optional[list] = None) -> list:
    if fallback is None:
        fallback = []
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else fallback
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _float_field(market: dict, *names: str) -> float:
    for name in names:
        raw = market.get(name)
        if raw in (None, ""):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return 0.0


def _infer_category(question: str, market: dict) -> str:
    text = " ".join(
        str(x or "")
        for x in (
            question,
            market.get("slug"),
            market.get("category"),
            market.get("groupItemTitle"),
        )
    ).lower()
    if any(k in text for k in ("bitcoin", "btc", "ethereum", "eth", "solana", "crypto")):
        return "crypto"
    if any(k in text for k in ("nba", "nfl", "football", "soccer", "goal", "match")):
        return "sports"
    if any(k in text for k in ("fed", "inflation", "cpi", "jobs", "rate cut")):
        return "macro"
    if any(k in text for k in ("election", "trump", "biden", "senate", "president")):
        return "politics"
    return "general"


def market_from_gamma(market: dict) -> Optional[MarketSnapshot]:
    question = str(market.get("question") or "").strip()
    market_id = str(
        market.get("conditionId")
        or market.get("condition_id")
        or market.get("id")
        or ""
    ).strip()
    if not question or not market_id:
        return None
    prices = _json_list(market.get("outcomePrices"))
    outcomes = [str(x) for x in _json_list(market.get("outcomes"), ["Yes", "No"])]
    tokens = [str(x) for x in _json_list(market.get("clobTokenIds"))]
    if len(prices) < 2:
        return None
    try:
        yes_price = float(prices[0])
        no_price = float(prices[1])
    except (TypeError, ValueError):
        return None
    return MarketSnapshot(
        market_id=market_id,
        question=question,
        slug=str(market.get("slug") or ""),
        yes_price=yes_price,
        no_price=no_price,
        volume_usdc=_float_field(market, "volumeClob", "volume", "volumeNum"),
        liquidity_usdc=_float_field(market, "liquidityClob", "liquidity", "liquidityNum"),
        end_date=str(market.get("endDate") or market.get("end_date") or ""),
        outcomes=outcomes,
        tokens=tokens,
        category=_infer_category(question, market),
        raw=market,
    )


def filter_candidates(
    markets: Iterable[dict],
    cfg: ExternalConvictionConfig,
) -> list[MarketSnapshot]:
    candidates: list[MarketSnapshot] = []
    for raw in markets:
        if raw.get("active") is False or raw.get("closed") is True:
            continue
        snap = market_from_gamma(raw)
        if snap is None:
            continue
        if snap.volume_usdc < cfg.min_volume_usdc:
            continue
        if snap.liquidity_usdc < cfg.min_liquidity_usdc:
            continue
        if not (cfg.min_price <= snap.yes_price <= cfg.max_price):
            continue
        candidates.append(snap)
    candidates.sort(
        key=lambda s: (s.liquidity_usdc, s.volume_usdc, -abs(0.5 - s.yes_price)),
        reverse=True,
    )
    return candidates[: cfg.max_candidates]


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


class ExternalProvider:
    source = "base"

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        raise NotImplementedError

    def _skip(self, reason: str, evidence: Optional[dict] = None) -> ExternalVerdict:
        return ExternalVerdict(
            direction="skip",
            confidence=0.0,
            source=self.source,
            reason=reason,
            evidence=evidence or {},
        )


class HeuristicProvider(ExternalProvider):
    """Conservative local placeholder until paid/provider APIs are connected."""

    source = "heuristic"

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        q = market.question.lower()
        category_bonus = {
            "crypto": 0.06,
            "sports": 0.05,
            "macro": 0.04,
            "politics": 0.02,
            "general": 0.0,
        }.get(market.category, 0.0)
        event_words = (
            "today", "tonight", "tomorrow", "game", "match", "goal", "fed",
            "cpi", "bitcoin", "btc", "ethereum", "earnings", "debate",
        )
        hit_count = sum(1 for w in event_words if w in q)
        confidence = min(0.72, 0.48 + category_bonus + 0.025 * hit_count)
        if confidence < 0.52:
            direction = "skip"
        elif market.yes_price <= 0.45:
            direction = "yes"
        elif market.yes_price >= 0.55:
            direction = "no"
        else:
            direction = "skip"
            confidence = min(confidence, 0.54)
        return ExternalVerdict(
            direction=direction,
            confidence=round(confidence, 3),
            source=self.source,
            reason=(
                f"local shadow heuristic: category={market.category}, "
                f"event_words={hit_count}, yes_price={market.yes_price:.3f}"
            ),
            evidence={
                "category": market.category,
                "event_word_hits": hit_count,
                "provider_mode": "placeholder_until_external_api_key",
            },
        )


class HttpJsonProvider(ExternalProvider):
    """Generic API adapter.

    POSTs a market snapshot to ``EXTERNAL_CONVICTION_API_URL``. The endpoint can
    be a thin wrapper around Kaito, Santiment, Glassnode, a browser automation
    service, or any later tool. Expected response:

    ``{"direction":"yes|no|skip","confidence":0.62,"reason":"...","evidence":{}}``
    """

    source = "http_json"

    def __init__(self, url: str, api_key: str = ""):
        self.url = url
        self.api_key = api_key

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        if not self.url:
            return ExternalVerdict(
                direction="skip",
                confidence=0.0,
                source=self.source,
                reason="missing EXTERNAL_CONVICTION_API_URL",
                evidence={},
            )
        payload = json.dumps({"market": asdict(market)}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "poly1-external-conviction",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
        direction = str(body.get("direction", "skip")).lower()
        if direction not in ("yes", "no", "skip"):
            direction = "skip"
        try:
            confidence = float(body.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return ExternalVerdict(
            direction=direction,
            confidence=max(0.0, min(1.0, confidence)),
            source=str(body.get("source") or self.source),
            reason=str(body.get("reason") or "")[:1000],
            evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {},
        )


class PoliflyBrowserProvider(HttpJsonProvider):
    """Bridge adapter for Polifly's browser-only AI Analyzer.

    The Polifly website requires an interactive, logged-in browser session and
    currently gates Analyzer access behind Pro. This provider deliberately does
    not try to handle credentials or payment. Instead, it calls a local browser
    bridge once the operator has activated access and started that bridge.
    """

    source = "polifly_browser"

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        if not self.url:
            return ExternalVerdict(
                direction="skip",
                confidence=0.0,
                source=self.source,
                reason=(
                    "missing POLIFLY_BROWSER_BRIDGE_URL; Polifly Analyzer "
                    "requires Pro access and an operator-started browser bridge"
                ),
                evidence={"access_required": True, "shadow_only": True},
            )
        verdict = super().analyze(market)
        if verdict.source == HttpJsonProvider.source:
            verdict.source = self.source
        return verdict


class PublicNewsProvider(ExternalProvider):
    """No-key public news/RSS research provider.

    This is real external data, not mock data: it queries public news RSS for
    the market question and uses headline density as short-horizon attention
    evidence. It stays deliberately conservative because news attention is not
    the same thing as truth or positive expected value.
    """

    source = "public_news"
    rss_url = "https://news.google.com/rss/search"

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        query = f"{market.question} Polymarket odds latest"
        items = self._fetch_items(query)
        titles = [item["title"] for item in items]
        snippets = [item["description"] for item in items]
        joined = " ".join(titles + snippets).lower()
        attention_terms = (
            "breaking", "latest", "confirmed", "report", "forecast", "odds",
            "wins", "win", "lead", "leads", "surges", "rises", "approved",
            "fails", "loss", "loses", "injury", "delay", "cancel", "drops",
            "falls", "bitcoin", "crypto", "fed", "inflation", "earnings",
        )
        hits = sum(1 for term in attention_terms if term in joined)
        confidence = min(0.74, 0.42 + 0.045 * len(items) + 0.02 * hits)
        if len(items) < 2 or confidence < 0.58:
            direction = "skip"
        elif market.yes_price <= 0.45:
            direction = "yes"
        elif market.yes_price >= 0.55:
            direction = "no"
        else:
            direction = "skip"
            confidence = min(confidence, 0.57)
        return ExternalVerdict(
            direction=direction,
            confidence=round(confidence, 3),
            source=self.source,
            reason=(
                f"public news density: results={len(items)}, "
                f"attention_terms={hits}, yes_price={market.yes_price:.3f}"
            ),
            evidence={
                "query": query,
                "result_count": len(items),
                "attention_term_hits": hits,
                "titles": titles[:5],
                "links": [item["link"] for item in items[:5]],
            },
        )

    def _fetch_items(self, query: str) -> list[dict]:
        params = {
            "q": query,
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        }
        url = self.rss_url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "poly1-external-conviction-public-news"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read()
        root = ET.fromstring(body)
        out: list[dict] = []
        for item in root.findall("./channel/item")[:8]:
            title = html.unescape(item.findtext("title") or "").strip()
            description = html.unescape(item.findtext("description") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()
            if title:
                out.append({
                    "title": title,
                    "description": description,
                    "link": link,
                    "pubDate": pub_date,
                })
        return out


class TavilyProvider(ExternalProvider):
    """Tavily search-backed research provider.

    This is still shadow-only. It uses search result density as external
    evidence that a market has current narrative/news attention; it does not
    pretend to settle the market.
    """

    source = "tavily"
    url = "https://api.tavily.com/search"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        if not self.api_key:
            return ExternalVerdict(
                direction="skip",
                confidence=0.0,
                source=self.source,
                reason="missing TAVILY_API_KEY",
                evidence={},
            )
        query = f"{market.question} latest news odds prediction market"
        payload = json.dumps({
            "api_key": self.api_key,
            "query": query,
            "max_results": 5,
            "search_depth": "basic",
            "topic": "news",
        }).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "poly1-external-conviction-tavily",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
        results = body.get("results") if isinstance(body, dict) else []
        if not isinstance(results, list):
            results = []
        titles = [str(r.get("title") or "") for r in results if isinstance(r, dict)]
        snippets = [str(r.get("content") or "") for r in results if isinstance(r, dict)]
        joined = " ".join(titles + snippets).lower()
        event_terms = (
            "breaking", "latest", "confirmed", "injury", "goal", "wins",
            "launch", "approved", "poll", "forecast", "odds", "bitcoin",
            "crypto", "fed", "inflation", "earnings",
        )
        hits = sum(1 for term in event_terms if term in joined)
        confidence = min(0.76, 0.44 + 0.04 * len(results) + 0.025 * hits)
        if confidence < 0.58:
            direction = "skip"
        elif market.yes_price <= 0.45:
            direction = "yes"
        elif market.yes_price >= 0.55:
            direction = "no"
        else:
            direction = "skip"
            confidence = min(confidence, 0.57)
        return ExternalVerdict(
            direction=direction,
            confidence=round(confidence, 3),
            source=self.source,
            reason=(
                f"tavily search density: results={len(results)}, "
                f"event_terms={hits}, yes_price={market.yes_price:.3f}"
            ),
            evidence={
                "query": query,
                "result_count": len(results),
                "event_term_hits": hits,
                "titles": titles[:5],
            },
        )


class GdeltProvider(ExternalProvider):
    """Free GDELT DOC 2.0 API — news event volume and sentiment for a market question.

    No API key required. Returns a directional signal based on whether recent
    news tone (goldsteinscale, avgtone) is strongly positive or negative.
    Query is limited to the first 60 chars of the question to avoid URL length issues.

    GDELT endpoint: http://api.gdeltproject.org/api/v2/doc/doc
    Rate limit: unauthenticated — be conservative (short caching at caller level).
    """

    source = "gdelt"
    BASE_URL = "http://api.gdeltproject.org/api/v2/doc/doc"

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        question = (market.question or "").strip()
        if not question:
            return self._skip("gdelt: empty question")

        # Shorten query and encode it safely.
        short_q = question[:60]
        try:
            params = urllib.parse.urlencode({
                "query": short_q,
                "mode": "ArtList",
                "maxrecords": "10",
                "timespan": "3d",
                "format": "json",
            })
            url = f"{self.BASE_URL}?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "poly1-gdelt"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read())
        except Exception as exc:
            return self._skip(f"gdelt: fetch error: {exc}")

        articles = (body or {}).get("articles") if isinstance(body, dict) else []
        if not isinstance(articles, list) or len(articles) < 2:
            return self._skip(
                f"gdelt: only {len(articles) if articles else 0} articles found",
                {"articles_found": len(articles) if articles else 0},
            )

        tones: list[float] = []
        for art in articles:
            if not isinstance(art, dict):
                continue
            raw_tone = art.get("tone", None)
            if raw_tone is None:
                raw_tone = art.get("avgtone", None)
            try:
                tones.append(float(raw_tone))
            except (TypeError, ValueError):
                pass

        if not tones:
            return self._skip("gdelt: no tone data in articles",
                              {"articles_found": len(articles)})

        avg_tone = sum(tones) / len(tones)
        # GDELT tone: negative = negative sentiment (fear/anger), positive = calm/positive.
        # For a binary YES/NO market: strongly negative news → likely "yes" to bad events,
        # strongly positive → likely "yes" to good outcomes.
        # Map: |avg_tone| > 3 → directional signal.
        abs_tone = abs(avg_tone)
        if abs_tone < 2.0:
            return self._skip(
                f"gdelt: tone too neutral (avg={avg_tone:.2f})",
                {"avg_tone": round(avg_tone, 3), "articles": len(tones)},
            )

        confidence = min(0.65, 0.30 + abs_tone * 0.05)
        # Negative tone → bad events more likely → map to "yes" (e.g. "will X fail?")
        # This is heuristic — callers should cross-check with question semantics.
        direction = "no" if avg_tone > 0 else "yes"

        return ExternalVerdict(
            direction=direction,
            confidence=round(confidence, 3),
            source=self.source,
            reason=(
                f"gdelt: {len(tones)} articles, avg_tone={avg_tone:.2f} "
                f"({'positive' if avg_tone > 0 else 'negative'} sentiment)"
            ),
            evidence={
                "articles_found": len(articles),
                "tones_parsed": len(tones),
                "avg_tone": round(avg_tone, 3),
                "abs_tone": round(abs_tone, 3),
            },
        )


class CLOBWhaleProvider(ExternalProvider):
    """Fetch recent large trades from Polymarket data API and compute directional consensus."""

    source = "clob_whale"
    trades_url = "https://data-api.polymarket.com/trades"

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        condition_id = market.market_id
        params = urllib.parse.urlencode({
            "asset_id": market.tokens[0] if market.tokens else condition_id,
            "limit": "100",
        })
        url = self.trades_url + "?" + params
        req = urllib.request.Request(
            url, headers={"User-Agent": "poly1-clob-whale"}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                trades = json.loads(resp.read())
        except Exception as exc:
            return self._skip(f"clob_whale fetch error: {exc}")
        if not isinstance(trades, list):
            return self._skip("clob_whale: unexpected response format")
        big_trades = [
            t for t in trades
            if isinstance(t, dict) and _safe_float(t.get("size", 0)) >= 5000.0
        ]
        if len(big_trades) < 2:
            return self._skip(
                f"clob_whale: only {len(big_trades)} whale trades (need >=2)",
                {"whale_count": len(big_trades), "total_trades": len(trades)},
            )
        buy_vol = sum(
            _safe_float(t.get("size", 0))
            for t in big_trades
            if str(t.get("side", "")).upper() == "BUY"
        )
        sell_vol = sum(
            _safe_float(t.get("size", 0))
            for t in big_trades
            if str(t.get("side", "")).upper() == "SELL"
        )
        total = buy_vol + sell_vol
        if total < 1.0:
            return self._skip("clob_whale: zero whale volume")
        buy_ratio = buy_vol / total
        confidence = min(0.78, 0.45 + 0.3 * abs(buy_ratio - 0.5))
        if buy_ratio >= 0.6:
            direction = "yes"
        elif buy_ratio <= 0.4:
            direction = "no"
        else:
            direction = "skip"
        return ExternalVerdict(
            direction=direction,
            confidence=round(confidence, 3),
            source=self.source,
            reason=(
                f"whale consensus: {len(big_trades)} trades >$5K, "
                f"buy_ratio={buy_ratio:.2f}, buy=${buy_vol:.0f} sell=${sell_vol:.0f}"
            ),
            evidence={
                "whale_count": len(big_trades),
                "buy_vol": round(buy_vol, 2),
                "sell_vol": round(sell_vol, 2),
                "buy_ratio": round(buy_ratio, 3),
            },
        )


class ManifoldDivergenceProvider(ExternalProvider):
    """Compare Polymarket price with Manifold Markets probability."""

    source = "manifold_divergence"
    search_url = "https://api.manifold.markets/v0/search-markets"

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        query = market.question[:80]
        params = urllib.parse.urlencode({"term": query, "limit": "3"})
        url = self.search_url + "?" + params
        req = urllib.request.Request(
            url, headers={"User-Agent": "poly1-manifold-divergence"}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                results = json.loads(resp.read())
        except Exception as exc:
            return self._skip(f"manifold fetch error: {exc}")
        if not isinstance(results, list) or len(results) == 0:
            return self._skip("manifold: no matching markets")
        best = results[0]
        manifold_prob = _safe_float(best.get("probability", 0))
        if manifold_prob <= 0 or manifold_prob >= 1:
            return self._skip("manifold: invalid probability")
        poly_prob = market.yes_price
        divergence = manifold_prob - poly_prob
        abs_div = abs(divergence)
        if abs_div < 0.10:
            return self._skip(
                f"manifold: divergence {divergence:+.3f} below 10% threshold",
                {"manifold_prob": manifold_prob, "poly_prob": poly_prob},
            )
        confidence = min(0.80, 0.50 + abs_div)
        direction = "yes" if divergence > 0 else "no"
        return ExternalVerdict(
            direction=direction,
            confidence=round(confidence, 3),
            source=self.source,
            reason=(
                f"manifold divergence: manifold={manifold_prob:.3f} vs "
                f"poly={poly_prob:.3f} (diff={divergence:+.3f})"
            ),
            evidence={
                "manifold_prob": round(manifold_prob, 4),
                "poly_prob": round(poly_prob, 4),
                "divergence": round(divergence, 4),
                "manifold_slug": str(best.get("slug", "")),
            },
        )


class MetaculusDivergenceProvider(ExternalProvider):
    """Compare Polymarket price with Metaculus superforecaster median."""

    source = "metaculus_divergence"
    search_url = "https://www.metaculus.com/api/questions/"

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        query = market.question[:60]
        params = urllib.parse.urlencode({
            "search": query,
            "limit": 3,
            "type": "forecast",
            "status": "open",
        })
        url = self.search_url + "?" + params
        req = urllib.request.Request(
            url, headers={"User-Agent": "poly1-metaculus-divergence"}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read())
        except Exception as exc:
            return self._skip(f"metaculus fetch error: {exc}")
        results = body.get("results") if isinstance(body, dict) else body
        if not isinstance(results, list) or len(results) == 0:
            return self._skip("metaculus: no matching questions")
        best = results[0]
        community = best.get("community_prediction") or {}
        mc_prob = _safe_float(community.get("full", {}).get("q2", 0))
        if mc_prob <= 0 or mc_prob >= 1:
            return self._skip("metaculus: no valid community prediction")
        poly_prob = market.yes_price
        divergence = mc_prob - poly_prob
        abs_div = abs(divergence)
        if abs_div < 0.10:
            return self._skip(
                f"metaculus: divergence {divergence:+.3f} below 10% threshold",
                {"metaculus_prob": mc_prob, "poly_prob": poly_prob},
            )
        confidence = min(0.80, 0.50 + abs_div)
        direction = "yes" if divergence > 0 else "no"
        return ExternalVerdict(
            direction=direction,
            confidence=round(confidence, 3),
            source=self.source,
            reason=(
                f"metaculus divergence: metaculus={mc_prob:.3f} vs "
                f"poly={poly_prob:.3f} (diff={divergence:+.3f})"
            ),
            evidence={
                "metaculus_prob": round(mc_prob, 4),
                "poly_prob": round(poly_prob, 4),
                "divergence": round(divergence, 4),
                "metaculus_id": best.get("id"),
            },
        )


class CrossMarketProvider(ExternalProvider):
    """Find related Polymarket markets by keyword overlap and signal if moved strongly."""

    source = "cross_market"

    def __init__(self) -> None:
        self.all_markets: list[MarketSnapshot] = []

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1}

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        if not self.all_markets:
            return self._skip("cross_market: no all_markets injected")
        keywords = self._tokenize(market.question)
        stop_words = {
            "will", "the", "an", "in", "on", "at", "to", "be",
            "is", "of", "by", "for", "or", "and", "this", "that", "it",
        }
        keywords -= stop_words
        if len(keywords) < 2:
            return self._skip("cross_market: too few keywords")
        related: list[MarketSnapshot] = []
        for other in self.all_markets:
            if other.market_id == market.market_id:
                continue
            other_words = self._tokenize(other.question)
            overlap = keywords & other_words
            if len(overlap) >= 2:
                related.append(other)
        if not related:
            return self._skip("cross_market: no related markets found")
        movers = [
            m for m in related if abs(m.yes_price - 0.50) > 0.15
        ]
        if not movers:
            return self._skip(
                f"cross_market: {len(related)} related but none moved >15% from 0.50",
                {"related_count": len(related)},
            )
        avg_price = sum(m.yes_price for m in movers) / len(movers)
        confidence = min(0.75, 0.45 + 0.25 * len(movers))
        if avg_price >= 0.65:
            direction = "yes"
        elif avg_price <= 0.35:
            direction = "no"
        else:
            direction = "skip"
        return ExternalVerdict(
            direction=direction,
            confidence=round(confidence, 3),
            source=self.source,
            reason=(
                f"cross-market signal: {len(movers)}/{len(related)} related markets "
                f"moved >15%, avg_yes={avg_price:.3f}"
            ),
            evidence={
                "related_count": len(related),
                "mover_count": len(movers),
                "avg_mover_yes_price": round(avg_price, 4),
                "mover_slugs": [m.slug for m in movers[:5]],
            },
        )


class KalshiDivergenceProvider(ExternalProvider):
    """Compare Polymarket price with Kalshi real-money prices."""

    source = "kalshi_divergence"
    search_url = "https://api.elections.kalshi.com/trade-api/v2/markets"

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        query = market.question[:60]
        params = urllib.parse.urlencode({
            "limit": "3",
            "status": "open",
            "title": query,
        })
        url = self.search_url + "?" + params
        req = urllib.request.Request(
            url, headers={"User-Agent": "poly1-kalshi-divergence"}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read())
        except Exception as exc:
            return self._skip(f"kalshi fetch error: {exc}")
        markets_list = body.get("markets") if isinstance(body, dict) else []
        if not isinstance(markets_list, list):
            return self._skip("kalshi: unexpected response format")
        query_lower = query.lower()
        best = None
        best_score = 0
        for km in markets_list:
            title = str(km.get("title", "")).lower()
            overlap = sum(1 for w in query_lower.split() if w in title)
            if overlap > best_score:
                best_score = overlap
                best = km
        if best is None or best_score < 2:
            return self._skip("kalshi: no matching market found")
        kalshi_yes = _safe_float(best.get("yes_ask", 0)) / 100.0
        if kalshi_yes <= 0 or kalshi_yes >= 1:
            kalshi_yes = _safe_float(best.get("last_price", 0)) / 100.0
        if kalshi_yes <= 0 or kalshi_yes >= 1:
            return self._skip("kalshi: no valid price")
        poly_prob = market.yes_price
        divergence = kalshi_yes - poly_prob
        abs_div = abs(divergence)
        if abs_div < 0.10:
            return self._skip(
                f"kalshi: divergence {divergence:+.3f} below 10% threshold",
                {"kalshi_yes": kalshi_yes, "poly_prob": poly_prob},
            )
        confidence = min(0.82, 0.52 + abs_div)
        direction = "yes" if divergence > 0 else "no"
        return ExternalVerdict(
            direction=direction,
            confidence=round(confidence, 3),
            source=self.source,
            reason=(
                f"kalshi divergence: kalshi={kalshi_yes:.3f} vs "
                f"poly={poly_prob:.3f} (diff={divergence:+.3f})"
            ),
            evidence={
                "kalshi_yes": round(kalshi_yes, 4),
                "poly_prob": round(poly_prob, 4),
                "divergence": round(divergence, 4),
                "kalshi_ticker": str(best.get("ticker", "")),
            },
        )


class DataAPIWhaleConsensusProvider(ExternalProvider):
    """Poll top leaderboard wallets for position consensus on a market."""

    source = "whale_consensus"
    leaderboard_url = "https://data-api.polymarket.com/leaderboard"
    positions_url = "https://data-api.polymarket.com/positions"

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        try:
            req = urllib.request.Request(
                self.leaderboard_url + "?limit=20&window=30d",
                headers={"User-Agent": "poly1-whale-consensus"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                leaders = json.loads(resp.read())
        except Exception as exc:
            return self._skip(f"whale_consensus leaderboard error: {exc}")
        if not isinstance(leaders, list) or len(leaders) == 0:
            return self._skip("whale_consensus: empty leaderboard")
        yes_votes = 0
        no_votes = 0
        checked = 0
        for leader in leaders[:20]:
            addr = str(leader.get("address") or leader.get("proxyWallet") or "")
            if not addr:
                continue
            params = urllib.parse.urlencode({
                "user": addr,
                "market": market.market_id,
            })
            try:
                req = urllib.request.Request(
                    self.positions_url + "?" + params,
                    headers={"User-Agent": "poly1-whale-consensus"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    positions = json.loads(resp.read())
            except Exception:
                continue
            if not isinstance(positions, list):
                continue
            for pos in positions:
                size = _safe_float(pos.get("size", 0))
                if size <= 0:
                    continue
                outcome = str(pos.get("outcome", "")).lower()
                if outcome in ("yes", "0"):
                    yes_votes += 1
                elif outcome in ("no", "1"):
                    no_votes += 1
            checked += 1
        total_votes = yes_votes + no_votes
        if total_votes < 2:
            return self._skip(
                f"whale_consensus: only {total_votes} positions found (checked {checked} wallets)",
                {"yes_votes": yes_votes, "no_votes": no_votes, "checked": checked},
            )
        yes_ratio = yes_votes / total_votes
        confidence = min(0.78, 0.45 + 0.25 * abs(yes_ratio - 0.5) + 0.01 * total_votes)
        if yes_ratio >= 0.65:
            direction = "yes"
        elif yes_ratio <= 0.35:
            direction = "no"
        else:
            direction = "skip"
        return ExternalVerdict(
            direction=direction,
            confidence=round(confidence, 3),
            source=self.source,
            reason=(
                f"whale consensus: {yes_votes}Y/{no_votes}N from top-{checked} "
                f"leaderboard wallets, yes_ratio={yes_ratio:.2f}"
            ),
            evidence={
                "yes_votes": yes_votes,
                "no_votes": no_votes,
                "wallets_checked": checked,
                "yes_ratio": round(yes_ratio, 3),
            },
        )


class BullBearDebateProvider(ExternalProvider):
    """Three-call LLM debate: Bull -> Bear -> Judge. Uses raw OpenAI REST API."""

    source = "bull_bear_debate"

    def __init__(self, api_key: str = "", model: str = "gpt-4o-mini"):
        self.api_key = api_key
        self.model = model

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        if not self.api_key:
            return self._skip("bull_bear_debate: missing OPENAI_API_KEY")
        context = (
            f"Market: {market.question}\n"
            f"Current YES price: {market.yes_price:.3f}\n"
            f"Category: {market.category}\n"
            f"Volume: ${market.volume_usdc:,.0f}"
        )
        try:
            bull_arg = self._chat(
                f"You are a BULL analyst. Argue strongly why YES is the right bet "
                f"on this prediction market. Be specific and concise (3-4 sentences).\n\n{context}"
            )
            bear_arg = self._chat(
                f"You are a BEAR analyst. Argue strongly why NO is the right bet "
                f"on this prediction market. Respond to the bull case below. "
                f"Be specific and concise (3-4 sentences).\n\n{context}\n\n"
                f"Bull argument: {bull_arg}"
            )
            judge_raw = self._chat(
                f"You are an impartial judge evaluating a prediction market debate. "
                f"Based on the arguments below, respond with EXACTLY one JSON object: "
                f'{{"direction":"yes" or "no" or "skip","confidence":0.XX,"reason":"one sentence"}}\n\n'
                f"{context}\n\nBull: {bull_arg}\n\nBear: {bear_arg}"
            )
        except Exception as exc:
            return self._skip(f"bull_bear_debate LLM error: {exc}")
        try:
            start = judge_raw.index("{")
            end = judge_raw.rindex("}") + 1
            verdict_data = json.loads(judge_raw[start:end])
        except (ValueError, json.JSONDecodeError):
            return self._skip(
                f"bull_bear_debate: judge returned unparseable response",
                {"judge_raw": judge_raw[:500]},
            )
        direction = str(verdict_data.get("direction", "skip")).lower()
        if direction not in ("yes", "no", "skip"):
            direction = "skip"
        confidence = max(0.0, min(1.0, _safe_float(verdict_data.get("confidence", 0))))
        return ExternalVerdict(
            direction=direction,
            confidence=round(confidence, 3),
            source=self.source,
            reason=str(verdict_data.get("reason", ""))[:500],
            evidence={
                "bull_argument": bull_arg[:300],
                "bear_argument": bear_arg[:300],
                "judge_direction": direction,
                "model": self.model,
            },
        )

    def _chat(self, prompt: str) -> str:
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 300,
            "temperature": 0.7,
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "poly1-bull-bear-debate",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        return str(
            body.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        ).strip()


class NansenSmartMoneyProvider(ExternalProvider):
    """Nansen smart-money Polygon CTF flow signal. Requires NANSEN_API_KEY."""

    source = "nansen_smart_money"
    api_url = "https://api.nansen.ai/v1/smart-money/flows"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        if not self.api_key:
            return self._skip("nansen: missing NANSEN_API_KEY")
        token = market.tokens[0] if market.tokens else ""
        if not token:
            return self._skip("nansen: no token_id for market")
        params = urllib.parse.urlencode({
            "chain": "polygon",
            "token_address": token,
            "window": "24h",
        })
        url = self.api_url + "?" + params
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "poly1-nansen",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read())
        except Exception as exc:
            return self._skip(f"nansen fetch error: {exc}")
        if not isinstance(body, dict):
            return self._skip("nansen: unexpected response format")
        inflow = _safe_float(body.get("smart_money_inflow", 0))
        outflow = _safe_float(body.get("smart_money_outflow", 0))
        net = inflow - outflow
        total = inflow + outflow
        if total < 1000:
            return self._skip(
                f"nansen: low smart money volume (${total:.0f})",
                {"inflow": inflow, "outflow": outflow},
            )
        ratio = inflow / total if total > 0 else 0.5
        confidence = min(0.80, 0.48 + 0.3 * abs(ratio - 0.5))
        if ratio >= 0.6:
            direction = "yes"
        elif ratio <= 0.4:
            direction = "no"
        else:
            direction = "skip"
        return ExternalVerdict(
            direction=direction,
            confidence=round(confidence, 3),
            source=self.source,
            reason=(
                f"nansen smart money: inflow=${inflow:,.0f} outflow=${outflow:,.0f} "
                f"net=${net:+,.0f}, ratio={ratio:.2f}"
            ),
            evidence={
                "inflow": round(inflow, 2),
                "outflow": round(outflow, 2),
                "net_flow": round(net, 2),
                "flow_ratio": round(ratio, 3),
            },
        )


class WalletMasterProvider(ExternalProvider):
    """Wallet Master API: win-rate-weighted whale consensus. Requires WALLET_MASTER_API_KEY."""

    source = "wallet_master"
    api_url = "https://api.walletmaster.io/v1/market-consensus"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        if not self.api_key:
            return self._skip("wallet_master: missing WALLET_MASTER_API_KEY")
        payload = json.dumps({
            "market_id": market.market_id,
            "platform": "polymarket",
        }).encode("utf-8")
        req = urllib.request.Request(
            self.api_url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "poly1-wallet-master",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read())
        except Exception as exc:
            return self._skip(f"wallet_master fetch error: {exc}")
        if not isinstance(body, dict):
            return self._skip("wallet_master: unexpected response format")
        consensus = str(body.get("consensus", "skip")).lower()
        if consensus not in ("yes", "no", "skip"):
            consensus = "skip"
        weighted_score = _safe_float(body.get("weighted_score", 0))
        wallets_counted = int(_safe_float(body.get("wallets_counted", 0)))
        confidence = max(0.0, min(1.0, _safe_float(body.get("confidence", 0))))
        return ExternalVerdict(
            direction=consensus,
            confidence=round(confidence, 3),
            source=self.source,
            reason=(
                f"wallet_master: consensus={consensus}, "
                f"weighted_score={weighted_score:.3f}, wallets={wallets_counted}"
            ),
            evidence={
                "consensus": consensus,
                "weighted_score": round(weighted_score, 4),
                "wallets_counted": wallets_counted,
            },
        )


class PoliflyEnhancedProvider(ExternalProvider):
    """Extends PoliflyBrowserProvider with retry + public_news fallback."""

    source = "polifly_enhanced"

    def __init__(self, polifly_url: str = "", polifly_api_key: str = ""):
        self._polifly = PoliflyBrowserProvider(polifly_url, polifly_api_key)
        self._fallback = PublicNewsProvider()

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        for attempt in range(2):
            try:
                verdict = self._polifly.analyze(market)
                if verdict.direction != "skip" or not verdict.reason.startswith("missing"):
                    if verdict.source == "polifly_browser":
                        verdict.source = self.source
                    return verdict
                break  # got a skip with "missing" reason — fall through to fallback
            except Exception:
                continue
        fallback_verdict = self._fallback.analyze(market)
        return ExternalVerdict(
            direction=fallback_verdict.direction,
            confidence=round(fallback_verdict.confidence * 0.9, 3),
            source=self.source,
            reason=f"polifly_enhanced fallback: {fallback_verdict.reason}",
            evidence={
                **fallback_verdict.evidence,
                "fallback": True,
                "primary_provider": "polifly_browser",
            },
        )


class TechnicalSignalProvider(ExternalProvider):
    """EMA crossover + RSI + BB on CLOB probability history.

    Skips when hours_to_close < 24 or price extreme (<0.10 or >0.90)
    to avoid false signals from resolution convergence.
    """

    source = "technical_signal"

    def __init__(self):
        self.ema_short = _env_int("EXTERNAL_CONVICTION_VIBE_EMA_SHORT", 12)
        self.ema_long = _env_int("EXTERNAL_CONVICTION_VIBE_EMA_LONG", 26)
        self.rsi_period = _env_int("EXTERNAL_CONVICTION_VIBE_RSI_PERIOD", 14)
        self.rsi_oversold = _env_float("EXTERNAL_CONVICTION_VIBE_RSI_OVERSOLD", 30.0)
        self.rsi_overbought = _env_float("EXTERNAL_CONVICTION_VIBE_RSI_OVERBOUGHT", 70.0)
        self.bb_period = _env_int("EXTERNAL_CONVICTION_VIBE_BB_PERIOD", 20)
        self.bb_std = _env_float("EXTERNAL_CONVICTION_VIBE_BB_STD", 2.0)
        self.min_bars = _env_int("EXTERNAL_CONVICTION_VIBE_MIN_BARS", 30)

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        # Resolution skip
        if market.yes_price < 0.10 or market.yes_price > 0.90:
            return self._skip("extreme_price", {"yes_price": market.yes_price})
        hours = _hours_to_close_from_snapshot(market)
        if hours is not None and hours < 24:
            return self._skip("too_close_to_resolution", {"hours_to_close": hours})
        prices = _fetch_probability_history(market)
        if not prices or len(prices) < self.min_bars:
            return self._skip("insufficient_price_history", {"bars": len(prices) if prices else 0})
        from agents.application.vibe_analysis import probability_technical_composite
        composite = probability_technical_composite(
            prices,
            ema_short=self.ema_short,
            ema_long=self.ema_long,
            rsi_period=self.rsi_period,
            rsi_oversold=self.rsi_oversold,
            rsi_overbought=self.rsi_overbought,
            bb_period=self.bb_period,
            bb_std=self.bb_std,
            min_bars=self.min_bars,
        )
        if composite is None:
            return self._skip("composite_returned_none")
        direction_map = {"bullish": "yes", "bearish": "no", "skip": "skip"}
        direction = direction_map.get(composite["direction"], "skip")
        return ExternalVerdict(
            direction=direction,
            confidence=round(composite.get("confidence", 0.0), 3),
            source=self.source,
            reason=(
                f"technical: dir={composite['direction']} "
                f"agreement={composite.get('agreement', 0):.2f} "
                f"contributors={composite.get('contributing_count', 0)}"
            ),
            evidence={
                "indicators": composite.get("indicators", {}),
                "agreement": composite.get("agreement", 0),
                "contributing_count": composite.get("contributing_count", 0),
                "bars": len(prices),
            },
        )


class VolatilityRegimeProvider(ExternalProvider):
    """HV percentile in lookback window. Low vol → expansion expected,
    high vol → contraction expected."""

    source = "volatility_regime"

    def __init__(self):
        self.hv_window = _env_int("EXTERNAL_CONVICTION_VIBE_HV_WINDOW", 20)
        self.hv_lookback = _env_int("EXTERNAL_CONVICTION_VIBE_HV_LOOKBACK", 120)
        self.min_bars = _env_int("EXTERNAL_CONVICTION_VIBE_MIN_BARS", 30)

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        if market.yes_price < 0.10 or market.yes_price > 0.90:
            return self._skip("extreme_price", {"yes_price": market.yes_price})
        hours = _hours_to_close_from_snapshot(market)
        if hours is not None and hours < 24:
            return self._skip("too_close_to_resolution", {"hours_to_close": hours})
        prices = _fetch_probability_history(market)
        if not prices or len(prices) < self.min_bars:
            return self._skip("insufficient_price_history", {"bars": len(prices) if prices else 0})
        from agents.application.vibe_analysis import volatility_percentile as vp_func
        vp = vp_func(prices, hv_window=self.hv_window, lookback=self.hv_lookback)
        if vp < 0.20:
            direction = "yes" if market.yes_price < 0.5 else "no"
            confidence = 0.60 + (0.20 - vp) * 0.5
            regime = "low_vol_expansion_expected"
        elif vp > 0.80:
            direction = "skip"
            confidence = 0.0
            regime = "high_vol_contraction_expected"
        else:
            direction = "skip"
            confidence = 0.0
            regime = "mid_vol_neutral"
        return ExternalVerdict(
            direction=direction,
            confidence=round(min(0.75, confidence), 3),
            source=self.source,
            reason=f"vol_regime: pctile={vp:.2f} regime={regime}",
            evidence={
                "vol_percentile": round(vp, 4),
                "regime": regime,
                "bars": len(prices),
            },
        )


class CryptoDerivativesProvider(ExternalProvider):
    """Fetch OKX/Binance public funding rates for crypto markets.

    Overheated long → bearish, overheated short → bullish.
    Only activates for crypto-category markets (slug detection).
    """

    source = "crypto_derivatives"

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        if market.category != "crypto":
            return self._skip("non_crypto_market", {"category": market.category})
        rate = self._fetch_funding_rate()
        if rate is None:
            return self._skip("funding_rate_unavailable")
        from agents.application.vibe_analysis import funding_rate_regime
        regime = funding_rate_regime(rate)
        direction_map = {"bullish": "yes", "bearish": "no", "skip": "skip"}
        direction = direction_map.get(regime["signal"], "skip")
        confidence = 0.0
        if direction != "skip":
            confidence = min(0.70, 0.50 + abs(rate) * 200)
        return ExternalVerdict(
            direction=direction,
            confidence=round(confidence, 3),
            source=self.source,
            reason=f"funding: regime={regime['regime']} rate_8h={rate:.6f}",
            evidence=regime,
        )

    def _fetch_funding_rate(self) -> Optional[float]:
        """Try OKX first, fallback to Binance. Returns 8h funding rate."""
        # OKX
        try:
            url = "https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP"
            req = urllib.request.Request(url, headers={"User-Agent": "poly1-vibe/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = json.loads(resp.read())
            data = body.get("data", [])
            if data:
                return float(data[0].get("fundingRate", 0))
        except Exception:
            pass
        # Binance fallback
        try:
            url = "https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1"
            req = urllib.request.Request(url, headers={"User-Agent": "poly1-vibe/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = json.loads(resp.read())
            if body and isinstance(body, list):
                return float(body[0].get("fundingRate", 0))
        except Exception:
            pass
        return None


class MultiFactorRankProvider(ExternalProvider):
    """Meta-provider: z-score normalizes confidence×direction across the
    batch of all_markets. Needs all_markets injection like CrossMarketProvider."""

    source = "multi_factor_rank"

    def __init__(self):
        self.all_markets: list[MarketSnapshot] = []

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        if not self.all_markets or len(self.all_markets) < 3:
            return self._skip("no all_markets injected or too few markets")
        factors: dict[str, float] = {}
        for m in self.all_markets:
            deviation = abs(m.yes_price - 0.5)
            liquidity_score = min(1.0, m.liquidity_usdc / 50000.0)
            factors[m.market_id] = deviation * 0.6 + liquidity_score * 0.4
        from agents.application.vibe_analysis import multi_factor_zscore
        zscores = multi_factor_zscore(factors)
        my_z = zscores.get(market.market_id, 0.0)
        if my_z > 0.5:
            direction = "yes" if market.yes_price < 0.5 else "no"
            confidence = min(0.70, 0.50 + my_z * 0.10)
        elif my_z < -0.5:
            direction = "skip"
            confidence = 0.0
        else:
            direction = "skip"
            confidence = 0.0
        return ExternalVerdict(
            direction=direction,
            confidence=round(confidence, 3),
            source=self.source,
            reason=f"multi_factor_rank: z={my_z:.3f} of {len(self.all_markets)} markets",
            evidence={
                "z_score": round(my_z, 4),
                "market_count": len(self.all_markets),
                "raw_factor": round(factors.get(market.market_id, 0), 4),
            },
        )


def _hours_to_close_from_snapshot(market: MarketSnapshot) -> Optional[float]:
    """Compute hours to close from a MarketSnapshot's end_date."""
    raw = market.end_date
    if not raw:
        return None
    try:
        from datetime import datetime, timezone
        end_dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = (end_dt - now).total_seconds()
        return max(0.0, delta / 3600.0)
    except Exception:
        return None


def _fetch_probability_history(
    market: MarketSnapshot,
    interval: str = "1h",
    fidelity: int = 60,
) -> list[float]:
    """Fetch CLOB price history for a market's YES token.

    Returns list of floats (probability 0-1). Falls back to empty list on error.
    """
    token_id = market.tokens[0] if market.tokens else None
    if not token_id:
        return []
    try:
        params = urllib.parse.urlencode({
            "market": token_id,
            "interval": interval,
            "fidelity": fidelity,
        })
        url = f"https://clob.polymarket.com/prices-history?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "poly1-vibe/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
        if isinstance(body, dict) and "history" in body:
            history = body["history"]
        elif isinstance(body, list):
            history = body
        else:
            return []
        prices = []
        for point in history:
            if isinstance(point, dict):
                p = point.get("p") or point.get("price") or point.get("t")
                if p is not None:
                    prices.append(float(p))
            elif isinstance(point, (int, float)):
                prices.append(float(point))
        return prices
    except Exception as exc:
        logger.debug("_fetch_probability_history failed for %s: %s", market.market_id[:20], exc)
        return []


class AggregatorProvider(ExternalProvider):
    """Runs N sub-providers, computes weighted majority verdict."""

    source = "aggregator"

    def __init__(self, sub_providers: list[ExternalProvider], weights: Optional[dict] = None):
        self.sub_providers = sub_providers
        self.weights = weights or {}

    def analyze(self, market: MarketSnapshot) -> ExternalVerdict:
        if not self.sub_providers:
            return self._skip("aggregator: no sub-providers configured")
        verdicts: list[tuple[ExternalProvider, ExternalVerdict]] = []
        for prov in self.sub_providers:
            try:
                v = prov.analyze(market)
                verdicts.append((prov, v))
            except Exception as exc:
                logger.warning(
                    "aggregator: sub-provider %s failed: %s",
                    getattr(prov, "source", "unknown"),
                    exc,
                )
        if not verdicts:
            return self._skip("aggregator: all sub-providers failed")
        yes_weight = 0.0
        no_weight = 0.0
        skip_weight = 0.0
        total_weight = 0.0
        for prov, v in verdicts:
            w = self.weights.get(prov.source, 1.0)
            if v.direction == "yes":
                yes_weight += w * v.confidence
            elif v.direction == "no":
                no_weight += w * v.confidence
            else:
                skip_weight += w
            total_weight += w
        if total_weight <= 0:
            return self._skip("aggregator: zero total weight")
        if yes_weight > no_weight and yes_weight > skip_weight:
            direction = "yes"
            consensus_strength = yes_weight / (yes_weight + no_weight + 0.001)
        elif no_weight > yes_weight and no_weight > skip_weight:
            direction = "no"
            consensus_strength = no_weight / (yes_weight + no_weight + 0.001)
        else:
            direction = "skip"
            consensus_strength = 0.0
        active_verdicts = [v for _, v in verdicts if v.direction != "skip"]
        if active_verdicts:
            avg_conf = sum(v.confidence for v in active_verdicts) / len(active_verdicts)
        else:
            avg_conf = 0.0
        confidence = min(0.85, avg_conf * consensus_strength)
        return ExternalVerdict(
            direction=direction,
            confidence=round(confidence, 3),
            source=self.source,
            reason=(
                f"aggregator: {len(verdicts)} providers, "
                f"yes_w={yes_weight:.2f} no_w={no_weight:.2f} skip_w={skip_weight:.2f}"
            ),
            evidence={
                "provider_count": len(verdicts),
                "yes_weight": round(yes_weight, 3),
                "no_weight": round(no_weight, 3),
                "skip_weight": round(skip_weight, 3),
                "sub_verdicts": [
                    {
                        "source": prov.source,
                        "direction": v.direction,
                        "confidence": v.confidence,
                    }
                    for prov, v in verdicts
                ],
            },
        )


def provider_from_config(cfg: ExternalConvictionConfig) -> ExternalProvider:
    provider = cfg.provider.lower().strip()
    if provider in ("polifly", "polifly_browser", "browser"):
        return PoliflyBrowserProvider(
            cfg.polifly_bridge_url,
            os.getenv(cfg.polifly_api_key_env, "").strip(),
        )
    if provider in ("http", "http_json", "api"):
        return HttpJsonProvider(cfg.api_url, os.getenv(cfg.api_key_env, "").strip())
    if provider in ("public_news", "news", "rss"):
        return PublicNewsProvider()
    if provider == "tavily":
        return TavilyProvider(os.getenv("TAVILY_API_KEY", "").strip())
    if provider in ("clob_whale", "whale"):
        return CLOBWhaleProvider()
    if provider in ("manifold_divergence", "manifold"):
        return ManifoldDivergenceProvider()
    if provider in ("metaculus_divergence", "metaculus"):
        return MetaculusDivergenceProvider()
    if provider == "cross_market":
        return CrossMarketProvider()
    if provider in ("kalshi_divergence", "kalshi"):
        return KalshiDivergenceProvider()
    if provider in ("whale_consensus", "data_api_whale"):
        return DataAPIWhaleConsensusProvider()
    if provider in ("bull_bear_debate", "debate"):
        return BullBearDebateProvider(
            api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            model=os.getenv("EXTERNAL_CONVICTION_DEBATE_MODEL", "gpt-4o-mini"),
        )
    if provider in ("nansen_smart_money", "nansen"):
        return NansenSmartMoneyProvider(
            api_key=os.getenv("NANSEN_API_KEY", "").strip(),
        )
    if provider in ("wallet_master",):
        return WalletMasterProvider(
            api_key=os.getenv("WALLET_MASTER_API_KEY", "").strip(),
        )
    if provider in ("polifly_enhanced",):
        return PoliflyEnhancedProvider(
            polifly_url=cfg.polifly_bridge_url,
            polifly_api_key=os.getenv(cfg.polifly_api_key_env, "").strip(),
        )
    if provider in ("technical_signal", "technical"):
        return TechnicalSignalProvider()
    if provider in ("volatility_regime", "vol_regime"):
        return VolatilityRegimeProvider()
    if provider in ("crypto_derivatives", "crypto_deriv", "funding"):
        return CryptoDerivativesProvider()
    if provider in ("multi_factor_rank", "multi_factor", "rank"):
        return MultiFactorRankProvider()
    if provider in ("gdelt", "gdelt_news"):
        return GdeltProvider()
    if provider == "aggregator":
        return _build_aggregator(cfg)
    if provider not in ("heuristic", "local"):
        logger.warning(
            "unknown EXTERNAL_CONVICTION_PROVIDER=%s; falling back to heuristic",
            cfg.provider,
        )
    return HeuristicProvider()


def _build_aggregator(cfg: ExternalConvictionConfig) -> AggregatorProvider:
    raw = os.getenv("EXTERNAL_CONVICTION_AGGREGATOR_PROVIDERS", "")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        # Default: all free providers — divergence signals have higher quality
        # than heuristic-only, so include Manifold/Metaculus/Kalshi when possible.
        names = [
            "manifold", "metaculus", "kalshi",
            "technical_signal", "clob_whale",
            "public_news", "gdelt", "heuristic",
        ]
    sub_providers: list[ExternalProvider] = []
    for name in names:
        sub_cfg = ExternalConvictionConfig(
            provider=name,
            api_url=cfg.api_url,
            api_key_env=cfg.api_key_env,
            polifly_bridge_url=cfg.polifly_bridge_url,
            polifly_api_key_env=cfg.polifly_api_key_env,
        )
        prov = provider_from_config(sub_cfg)
        if not isinstance(prov, AggregatorProvider):
            sub_providers.append(prov)
    return AggregatorProvider(sub_providers)


def build_shadow_plan(
    market: MarketSnapshot,
    verdict: ExternalVerdict,
    cfg: ExternalConvictionConfig,
) -> ShadowTradePlan:
    if verdict.direction == "yes":
        side = "YES"
        token_id = market.tokens[0] if len(market.tokens) > 0 else None
        entry = market.yes_price
    elif verdict.direction == "no":
        side = "NO"
        token_id = market.tokens[1] if len(market.tokens) > 1 else None
        entry = market.no_price
    else:
        side = "SKIP"
        token_id = None
        entry = market.yes_price

    allowed = (
        verdict.direction in ("yes", "no")
        and verdict.confidence >= cfg.min_confidence
        and token_id is not None
    )
    action = f"SHADOW_BUY_{side}" if allowed else "SKIP"
    take_profit = min(0.99, entry * (1.0 + cfg.take_profit_pct))
    stop_loss = max(0.01, entry * (1.0 - cfg.stop_loss_pct))
    return ShadowTradePlan(
        ts=_now(),
        market_id=market.market_id,
        question=market.question,
        action=action,
        side=side,
        token_id=token_id,
        entry_price=round(entry, 4),
        take_profit=round(take_profit, 4),
        stop_loss=round(stop_loss, 4),
        max_hold_minutes=cfg.max_hold_minutes,
        confidence=round(verdict.confidence, 3),
        source=verdict.source,
        reason=verdict.reason,
        volume_usdc=round(market.volume_usdc, 4),
        liquidity_usdc=round(market.liquidity_usdc, 4),
        status="shadow_candidate" if allowed else "shadow_skip",
    )


class ExternalConvictionAgent:
    def __init__(
        self,
        cfg: Optional[ExternalConvictionConfig] = None,
        gamma=None,
        provider: Optional[ExternalProvider] = None,
        trade_log: Optional[TradeLog] = None,
        polymarket=None,
        risk_gate=None,
        brain=None,
    ):
        self.cfg = cfg or ExternalConvictionConfig.from_env()
        self.gamma = gamma
        self.provider = provider or provider_from_config(self.cfg)
        self.trade_log = trade_log or TradeLog()
        self.polymarket = polymarket
        self.risk_gate = risk_gate
        self.brain = brain
        self.output_path = Path(self.cfg.output_path)
        self.heartbeat_path = Path(self.cfg.heartbeat_path)
        self._live_entries_this_cycle: set = set()

        if self.cfg.execute:
            if self.polymarket is None:
                from agents.polymarket.polymarket import Polymarket

                self.polymarket = Polymarket(live=True)
            if self.risk_gate is None:
                from agents.application.risk_gate import RiskGate

                self.risk_gate = RiskGate(
                    trade_log=self.trade_log,
                    polymarket=self.polymarket,
                    external_conviction_reserve_usdc=_env_float(
                        "EXTERNAL_CONVICTION_RESERVE_USDC", 0.0
                    ),
                )

    def collect_once(self) -> int:
        gamma = self.gamma
        if gamma is None:
            raw_markets = fetch_current_markets(limit=self.cfg.market_limit)
        else:
            raw_markets = gamma.get_current_markets(limit=self.cfg.market_limit)
        candidates = filter_candidates(raw_markets, self.cfg)
        if isinstance(self.provider, CrossMarketProvider):
            self.provider.all_markets = candidates
        elif isinstance(self.provider, MultiFactorRankProvider):
            self.provider.all_markets = candidates
        elif isinstance(self.provider, AggregatorProvider):
            for sub in self.provider.sub_providers:
                if isinstance(sub, CrossMarketProvider):
                    sub.all_markets = candidates
                elif isinstance(sub, MultiFactorRankProvider):
                    sub.all_markets = candidates
        logger.info(
            "external_conviction: markets=%d candidates=%d provider=%s",
            len(raw_markets) if hasattr(raw_markets, "__len__") else -1,
            len(candidates),
            getattr(self.provider, "source", self.cfg.provider),
        )
        written = 0
        live_trades = 0
        self._live_entries_this_cycle = set()
        for market in candidates:
            try:
                verdict = self.provider.analyze(market)
            except Exception as exc:
                logger.warning(
                    "external_conviction: provider failed market=%s err=%s",
                    market.market_id,
                    exc,
                )
                verdict = ExternalVerdict(
                    direction="skip",
                    confidence=0.0,
                    source=getattr(self.provider, "source", "provider_error"),
                    reason=f"provider_error:{type(exc).__name__}",
                    evidence={},
                )
            plan = build_shadow_plan(market, verdict, self.cfg)
            record = {
                "plan": asdict(plan),
                "verdict": asdict(verdict),
                "market": {
                    "slug": market.slug,
                    "yes_price": market.yes_price,
                    "no_price": market.no_price,
                    "end_date": market.end_date,
                    "category": market.category,
                    "outcomes": market.outcomes,
                },
            }
            self._append_jsonl(record)
            self.trade_log.insert_brain_decision(
                agent=self.cfg.agent_name,
                strategy=self.cfg.strategy_name,
                decision_type="shadow_trade_plan",
                market_id=plan.market_id,
                token_id=plan.token_id,
                approved=plan.status == "shadow_candidate",
                reason=plan.reason or plan.status,
                score=plan.confidence,
                market_type=market.category,
                asset=None,
                features={
                    "source": plan.source,
                    "action": plan.action,
                    "side": plan.side,
                    "entry_price": plan.entry_price,
                    "take_profit": plan.take_profit,
                    "stop_loss": plan.stop_loss,
                    "max_hold_minutes": plan.max_hold_minutes,
                    "volume_usdc": plan.volume_usdc,
                    "liquidity_usdc": plan.liquidity_usdc,
                    "shadow_only": True,
                    "external_evidence": verdict.evidence,
                },
                action=plan.action,
            )
            if plan.status == "shadow_candidate":
                traded = self._maybe_execute_live(plan, market)
                if traded:
                    live_trades += 1
                    if live_trades >= self.cfg.max_live_trades_per_cycle:
                        logger.info(
                            "external_conviction: hit max live trades per cycle=%d",
                            self.cfg.max_live_trades_per_cycle,
                        )
                        written += 1
                        break
            written += 1
        self._heartbeat()
        return written

    def _maybe_execute_live(
        self,
        plan: ShadowTradePlan,
        market: MarketSnapshot,
    ) -> bool:
        if not self.cfg.execute:
            return False
        if self.polymarket is None or self.risk_gate is None:
            logger.warning("external_conviction live blocked: missing runtime adapters")
            return False
        if not self.risk_gate.ok():
            logger.warning(
                "external_conviction live blocked by risk gate: %s",
                self.risk_gate.reason(),
            )
            return False
        if market.market_id in self._live_entries_this_cycle:
            logger.info(
                "external_conviction live skip %s: already entered this cycle",
                market.market_id,
            )
            return False
        open_count = len(self.trade_log.filled_positions())
        if open_count >= self.cfg.max_open_positions:
            logger.info(
                "external_conviction live skip: open_positions=%d >= max_open=%d",
                open_count,
                self.cfg.max_open_positions,
            )
            return False
        if self.trade_log.has_filled_position_for_market(
            market.market_id, token_id=plan.token_id,
        ):
            logger.info(
                "external_conviction live skip %s: already holds filled position",
                market.market_id,
            )
            return False
        if self.trade_log.has_active_trade_for_market(
            market.market_id, hours=6, token_id=plan.token_id,
        ):
            logger.info(
                "external_conviction live skip %s: recent active trade",
                market.market_id,
            )
            return False
        # Fix 1: Post-close re-entry cooldown — don't re-buy a market
        # that was just closed (timeout/SL/TP/dust).
        reentry_cooldown_hours = _env_int("REENTRY_COOLDOWN_HOURS", 12)
        if self.trade_log.has_recent_close_for_market(
            market.market_id, hours=reentry_cooldown_hours, token_id=plan.token_id,
        ):
            logger.info(
                "external_conviction live skip %s: re-entry cooldown (%dh)",
                market.market_id, reentry_cooldown_hours,
            )
            self.trade_log.insert_terminal(
                cycle_id=self.trade_log.new_cycle_id(),
                market_id=market.market_id,
                status="skipped_gate",
                token_id=plan.token_id,
                side="BUY" if plan.side == "YES" else "SELL",
                price=market.yes_price,
                size_usdc=0,
                error=f"re-entry cooldown ({reentry_cooldown_hours}h)",
            )
            return False
        # Fix 2: Per-market concentration limit.
        max_fills_24h = _env_int("MAX_FILLS_PER_MARKET_24H", 3)
        recent_fills = self.trade_log.count_recent_fills_for_market(
            market.market_id, hours=24, token_id=plan.token_id,
        )
        if recent_fills >= max_fills_24h:
            logger.info(
                "external_conviction live skip %s: concentration limit "
                "(%d fills in 24h >= max %d)",
                market.market_id, recent_fills, max_fills_24h,
            )
            self.trade_log.insert_terminal(
                cycle_id=self.trade_log.new_cycle_id(),
                market_id=market.market_id,
                status="skipped_gate",
                token_id=plan.token_id,
                side="BUY" if plan.side == "YES" else "SELL",
                price=market.yes_price,
                size_usdc=0,
                error=f"concentration limit ({recent_fills}/{max_fills_24h} in 24h)",
            )
            return False
        # Fix 4d: Brain gate for general binary markets.
        if self.brain is not None:
            try:
                decision = self.brain.evaluate_general_entry(
                    question=market.question or "",
                    spread_pct=(
                        abs(market.yes_price - (1 - market.no_price))
                        if market.no_price is not None else None
                    ),
                    hours_to_close=_hours_to_close_from_snapshot(market),
                )
                if not decision.approved:
                    logger.info(
                        "external_conviction brain rejected %s: %s",
                        market.market_id, decision.reason,
                    )
                    self.trade_log.insert_terminal(
                        cycle_id=self.trade_log.new_cycle_id(),
                        market_id=market.market_id,
                        status="skipped_gate",
                        token_id=plan.token_id,
                        side="BUY" if plan.side == "YES" else "SELL",
                        price=market.yes_price,
                        size_usdc=0,
                        error=f"brain rejected: {decision.reason}",
                    )
                    return False
            except Exception:
                logger.warning(
                    "external_conviction brain gate failed for %s (fail-open)",
                    market.market_id,
                )
        from agents.application.execution_safety import exitable_size_check

        entry_price = market.yes_price if plan.side == "YES" else market.no_price
        safety = exitable_size_check(
            amount_usdc=self.cfg.position_size_usdc,
            entry_price=entry_price,
        )
        if not safety.ok:
            self.trade_log.insert_terminal(
                cycle_id=self.trade_log.new_cycle_id(),
                market_id=market.market_id,
                status="skipped_gate",
                token_id=plan.token_id,
                side="BUY" if plan.side == "YES" else "SELL",
                price=market.yes_price,
                size_usdc=self.cfg.position_size_usdc,
                confidence=plan.confidence,
                error=f"external_conviction_{safety.reason}",
            )
            logger.info("external_conviction live skip: %s", safety.reason)
            return False

        from agents.utils.objects import TradeRecommendation

        recommendation = TradeRecommendation(
            price=market.yes_price,
            size_fraction=0.0,
            side="BUY" if plan.side == "YES" else "SELL",
            confidence=plan.confidence,
            amount_usdc=self.cfg.position_size_usdc,
            raw_response=plan.reason,
        )
        market_doc = _make_market_doc(market)
        cycle_id = self.trade_log.new_cycle_id()
        pending_id = self.trade_log.insert_pending(
            cycle_id=cycle_id,
            market_id=market.market_id,
            token_id=plan.token_id,
            side=recommendation.side,
            price=market.yes_price,
            size_usdc=self.cfg.position_size_usdc,
            confidence=plan.confidence,
        )
        try:
            response = self.polymarket.execute_market_order(
                (market_doc["doc"], 0.0),
                recommendation,
            )
        except Exception as exc:
            self.trade_log.mark(
                pending_id,
                "failed",
                error=f"external_conviction execute_market_order raised: {exc}",
            )
            logger.warning(
                "external_conviction live entry failed %s: %s",
                market.market_id,
                exc,
            )
            return False
        if not response or response.get("status") not in ("matched", "filled"):
            self.trade_log.mark(
                pending_id,
                "failed",
                response=response,
                error="external_conviction entry not matched",
            )
            return False
        response = {
            **response,
            "agent": self.cfg.agent_name,
            "strategy": self.cfg.strategy_name,
            "source": plan.source,
            "verdict_reason": plan.reason,
            "take_profit_pct": self.cfg.take_profit_pct,
            "stop_loss_pct": self.cfg.stop_loss_pct,
            "max_hold_minutes": self.cfg.max_hold_minutes,
        }
        fill_price = float(response.get("order_avg_price_estimate", entry_price))
        fill_size = float(response.get("amount_usdc", self.cfg.position_size_usdc))
        self.trade_log.mark(
            pending_id,
            "filled",
            response=response,
            price=fill_price,
            size_usdc=fill_size,
        )
        self._live_entries_this_cycle.add(market.market_id)
        logger.info(
            "external_conviction LIVE ENTRY: %s %s @ %.4f size=%.2f",
            recommendation.side,
            market.market_id,
            fill_price,
            fill_size,
        )
        notify_trade(
            event="fill",
            agent="ext_conviction",
            market_id=market.market_id,
            side=recommendation.side,
            price=fill_price,
            size_usdc=fill_size,
            reason=plan.reason[:60] if plan.reason else "",
            balance_usdc=_safe_balance(self.polymarket),
        )
        return True

    def _append_jsonl(self, record: dict) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")

    def _heartbeat(self) -> None:
        self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        self.heartbeat_path.write_text(_now() + "\n")

    def run(self) -> None:
        stop = threading.Event()

        def _handle_sigterm(*_) -> None:
            logger.info("external_conviction: SIGTERM received")
            stop.set()

        signal.signal(signal.SIGTERM, _handle_sigterm)
        logger.info(
            "external_conviction: daemon starting poll=%ds output=%s provider=%s",
            self.cfg.poll_sec,
            self.cfg.output_path,
            self.cfg.provider,
        )
        while not stop.is_set():
            try:
                self.collect_once()
            except Exception as exc:
                logger.exception("external_conviction: unhandled error: %s", exc)
                self._heartbeat()
            stop.wait(self.cfg.poll_sec)
        logger.info("external_conviction: stopped cleanly")


def fetch_current_markets(limit: int = 200) -> list[dict]:
    params = {
        "active": "true",
        "closed": "false",
        "archived": "false",
        "limit": str(limit),
        "enableOrderBook": "true",
    }
    url = GAMMA_MARKETS_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "poly1-external-conviction"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    return data if isinstance(data, list) else []


def _make_market_doc(market: MarketSnapshot) -> dict:
    class _Doc:
        pass

    doc = _Doc()
    doc.dict = lambda: {  # noqa: E731
        "metadata": {
            "id": market.market_id,
            "slug": market.slug,
            "question": market.question,
            "outcomes": str(market.outcomes),
            "clob_token_ids": str(market.tokens),
            "outcome_prices": json.dumps([
                str(market.yes_price),
                str(market.no_price),
            ]),
        }
    }
    return {"doc": doc}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument("--daemon", action="store_true", help="run forever")
    parser.add_argument("--limit", type=int, help="override market scan limit")
    parser.add_argument("--max-candidates", type=int, help="override candidate count")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = ExternalConvictionConfig.from_env()
    if args.limit is not None:
        cfg.market_limit = args.limit
    if args.max_candidates is not None:
        cfg.max_candidates = args.max_candidates
    agent = ExternalConvictionAgent(cfg=cfg)
    if args.daemon:
        agent.run()
        return 0
    inserted = agent.collect_once()
    print(f"external_conviction: wrote {inserted} shadow plans to {cfg.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
