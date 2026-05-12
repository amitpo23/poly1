"""Dry-run news classification signal.

This module never places orders. It classifies news against active markets and
persists observations to `news_signals` for dashboard/calibration review.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from agents.application.trade_log import TradeLog


logger = logging.getLogger(__name__)

NEWS_SIGNAL_STATUS = "news_signal"
CLASSIFIER_FAILED_STATUS = "classifier_failed"
HEURISTIC_SIGNAL_STATUS = "heuristic_signal"

DEFAULT_RSS_FEEDS = [
    "https://news.google.com/rss/search?q=OpenAI%20OR%20Anthropic%20OR%20Bitcoin%20OR%20Ethereum%20OR%20Fed%20OR%20Nvidia&hl=en-US&gl=US&ceid=US:en",
    "https://feeds.feedburner.com/TechCrunch",
    "https://www.theverge.com/rss/index.xml",
]

STOPWORDS = {
    "will", "the", "a", "an", "be", "by", "in", "on", "at", "to", "of",
    "for", "is", "it", "this", "that", "and", "or", "not", "before",
    "after", "end", "yes", "no", "any", "has", "have", "does", "do",
    "than", "more", "less", "over", "under", "above", "below", "through",
    "during", "between", "reach", "exceed", "what", "when", "with",
    "new", "before",
}

CLASSIFICATION_PROMPT = """You classify breaking news for prediction markets.

Market question:
{question}

Current YES price:
{yes_price}

News headline:
{headline}

Task:
Does this news make the market question more likely to resolve YES, more likely
to resolve NO, or is it not relevant?

Return ONLY valid JSON:
{{
  "direction": "bullish" | "bearish" | "neutral",
  "materiality": <number 0.0 to 1.0>,
  "reasoning": "<one short sentence>"
}}"""


@dataclass
class NewsItem:
    headline: str
    source: str = "unknown"
    url: str = ""
    published_at: Optional[str] = None


@dataclass
class MarketCandidate:
    market_id: str
    question: str
    yes_price: Optional[float]
    relevance_score: float


@dataclass
class ClassificationResult:
    direction: str
    materiality: float
    reasoning: str
    latency_ms: int
    model: str
    status: str = NEWS_SIGNAL_STATUS


def extract_keywords(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9'-]{2,}", text.lower())
    return [w for w in words if w not in STOPWORDS]


def relevance_score(headline: str, question: str) -> float:
    keywords = extract_keywords(question)
    if not keywords:
        return 0.0
    haystack = headline.lower()
    hits = sum(1 for kw in keywords if kw in haystack)
    return hits / len(keywords)


def relevance_hits(headline: str, question: str) -> int:
    haystack = headline.lower()
    return sum(1 for kw in extract_keywords(question) if kw in haystack)


def _parse_yes_price(market: dict) -> Optional[float]:
    raw = market.get("outcomePrices")
    if not raw:
        return None
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        if prices:
            return float(prices[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def match_news_to_markets(
    headline: str,
    markets: Iterable[dict],
    min_relevance: float = 0.12,
    max_matches: int = 5,
    min_hits: int = 2,
) -> list[MarketCandidate]:
    scored: list[MarketCandidate] = []
    for market in markets:
        question = market.get("question") or ""
        market_id = str(
            market.get("conditionId")
            or market.get("condition_id")
            or market.get("id")
            or ""
        )
        if not question or not market_id:
            continue
        hits = relevance_hits(headline, question)
        if hits < min_hits:
            continue
        score = relevance_score(headline, question)
        if score < min_relevance:
            continue
        scored.append(MarketCandidate(
            market_id=market_id,
            question=question,
            yes_price=_parse_yes_price(market),
            relevance_score=score,
        ))
    scored.sort(key=lambda c: c.relevance_score, reverse=True)
    return scored[:max_matches]


class NewsSignalClassifier:
    def __init__(self, model: Optional[str] = None):
        from langchain_openai import ChatOpenAI

        self.model = model or os.getenv("NEWS_CLASSIFICATION_MODEL") or os.getenv(
            "OPENAI_MODEL", "gpt-4o-mini"
        )
        self.llm = ChatOpenAI(model=self.model, temperature=0)
        self.quota_cooldown_sec = _env_int_ns("NEWS_SIGNAL_QUOTA_COOLDOWN_SEC", 3600)
        self._quota_blocked_until = 0.0

    def classify(self, item: NewsItem, market: MarketCandidate) -> ClassificationResult:
        from langchain_core.messages import HumanMessage

        start = time.time()
        now = time.time()
        if now < self._quota_blocked_until:
            return ClassificationResult(
                direction="neutral",
                materiality=0.0,
                reasoning="classification_error:insufficient_quota_cooldown",
                latency_ms=0,
                model=self.model,
                status=CLASSIFIER_FAILED_STATUS,
            )
        prompt = CLASSIFICATION_PROMPT.format(
            question=market.question,
            yes_price=(
                f"{market.yes_price:.3f}" if market.yes_price is not None else "unknown"
            ),
            headline=item.headline,
        )
        try:
            response = self.llm.invoke([HumanMessage(content=prompt)])
            payload = _coerce_json(response.content)
            direction = payload.get("direction", "neutral")
            if direction not in ("bullish", "bearish", "neutral"):
                direction = "neutral"
            materiality = max(0.0, min(1.0, float(payload.get("materiality", 0.0))))
            reasoning = str(payload.get("reasoning", ""))[:500]
            status = NEWS_SIGNAL_STATUS
        except Exception as exc:
            logger.warning("news_signal classification failed: %s", exc)
            if _is_insufficient_quota_error(exc):
                self._quota_blocked_until = time.time() + self.quota_cooldown_sec
            fallback = heuristic_classify(item, market)
            direction = fallback.direction
            materiality = fallback.materiality
            reasoning = f"classification_error:{type(exc).__name__}; {fallback.reasoning}"
            status = (
                HEURISTIC_SIGNAL_STATUS
                if fallback.materiality > 0
                else CLASSIFIER_FAILED_STATUS
            )

        return ClassificationResult(
            direction=direction,
            materiality=materiality,
            reasoning=reasoning,
            latency_ms=int((time.time() - start) * 1000),
            model=self.model,
            status=status,
        )


def heuristic_classify(item: NewsItem, market: MarketCandidate) -> ClassificationResult:
    """Deterministic fallback when the LLM is unavailable.

    It is intentionally conservative and writes `heuristic_signal`, not
    `news_signal`, so allocation can inspect it without treating it as a
    live-trade approval.
    """
    headline = item.headline.lower()
    positive_terms = {
        "approved", "wins", "won", "passes", "passed", "launches", "launched",
        "confirms", "confirmed", "settles", "settled", "returns", "returned",
        "beats", "beat", "above", "yes",
    }
    negative_terms = {
        "denies", "denied", "fails", "failed", "rejects", "rejected", "cancels",
        "cancelled", "delays", "delayed", "below", "misses", "missed", "no",
    }
    hits = relevance_hits(item.headline, market.question)
    if hits <= 0:
        return ClassificationResult(
            direction="neutral",
            materiality=0.0,
            reasoning="heuristic:no_keyword_overlap",
            latency_ms=0,
            model="heuristic",
            status=CLASSIFIER_FAILED_STATUS,
        )
    pos = sum(1 for term in positive_terms if term in headline)
    neg = sum(1 for term in negative_terms if term in headline)
    if pos == neg:
        direction = "neutral"
        materiality = min(0.35, 0.08 * hits)
    elif pos > neg:
        direction = "bullish"
        materiality = min(0.70, 0.18 + 0.08 * hits + 0.05 * (pos - neg))
    else:
        direction = "bearish"
        materiality = min(0.70, 0.18 + 0.08 * hits + 0.05 * (neg - pos))
    if market.yes_price is not None and (market.yes_price < 0.08 or market.yes_price > 0.92):
        materiality = min(materiality, 0.35)
    return ClassificationResult(
        direction=direction,
        materiality=round(materiality, 3),
        reasoning=f"heuristic:hits={hits} pos={pos} neg={neg}",
        latency_ms=0,
        model="heuristic",
        status=HEURISTIC_SIGNAL_STATUS,
    )


def _coerce_json(text: str) -> dict:
    text = text.strip()
    if "```" in text:
        parts = text.split("```")
        text = parts[1] if len(parts) > 1 else text
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start:end + 1]
    return json.loads(text)


def _is_insufficient_quota_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "insufficient_quota" in text or "exceeded your current quota" in text


def fetch_news_items(query: str, limit: int = 10) -> list[NewsItem]:
    """Fetch news from all configured sources, deduped by headline.

    Source preference order: Tavily > NewsAPI > RSS. Tavily search is
    keyword-targeted (better for prediction-market relevance) so it
    runs first when ``TAVILY_API_KEY`` is set; remaining slots fall
    back to NewsAPI then RSS to fill ``limit``.
    """
    out: list[NewsItem] = []
    seen: set[str] = set()

    def _add(items: Iterable[NewsItem]) -> None:
        for it in items:
            key = it.headline.lower().strip()[:120]
            if key in seen or not key:
                continue
            seen.add(key)
            out.append(it)
            if len(out) >= limit:
                break

    if os.getenv("TAVILY_API_KEY", "").strip():
        try:
            _add(fetch_tavily_items(query=query, limit=limit))
        except Exception as exc:
            logger.warning("news_signal tavily fetch failed: %s", exc)

    if len(out) < limit and os.getenv("NEWSAPI_API_KEY", "").strip():
        try:
            from agents.connectors.news import News
            news = News()
            articles = news.get_articles_for_cli_keywords(query)
            newsapi_items: list[NewsItem] = []
            for article in articles[: limit]:
                source = getattr(article, "source", None)
                if isinstance(source, dict):
                    source_name = source.get("name") or "newsapi"
                else:
                    source_name = str(source or "newsapi")
                headline = getattr(article, "title", None) or getattr(article, "description", "")
                if not headline:
                    continue
                newsapi_items.append(NewsItem(
                    headline=headline,
                    source=source_name,
                    url=getattr(article, "url", "") or "",
                    published_at=str(getattr(article, "publishedAt", "") or ""),
                ))
            _add(newsapi_items)
        except Exception as exc:
            logger.warning("news_signal newsapi fetch failed: %s", exc)

    if len(out) < limit:
        _add(fetch_rss_items(limit=limit - len(out)))

    return out


TAVILY_SEARCH_URL = "https://api.tavily.com/search"


def fetch_tavily_items(query: str, limit: int = 10) -> list[NewsItem]:
    """Fetch search results from Tavily as ``NewsItem``s.

    Tavily POST /search returns ``{"results": [{title, url, content,
    published_date, ...}]}``. Requires ``TAVILY_API_KEY``. Returns
    [] silently on missing key (caller checks first) or on any
    network/parse failure with a warning logged.
    """
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        return []
    payload = json.dumps({
        "api_key": api_key,
        "query": query,
        "max_results": min(max(1, limit), 20),
        "search_depth": "basic",
        "topic": "news",
    }).encode("utf-8")
    req = urllib.request.Request(
        TAVILY_SEARCH_URL,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "poly1-news-signal"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
    except Exception as exc:
        logger.warning("tavily fetch failed: %s", exc)
        return []
    raw_results = body.get("results") or []
    out: list[NewsItem] = []
    for r in raw_results[:limit]:
        title = (r.get("title") or "").strip()
        if not title:
            continue
        out.append(NewsItem(
            headline=title,
            source="tavily",
            url=(r.get("url") or "").strip(),
            published_at=(r.get("published_date") or "").strip(),
        ))
    return out


def fetch_rss_items(limit: int = 10, feeds: Optional[list[str]] = None) -> list[NewsItem]:
    out: list[NewsItem] = []
    seen: set[str] = set()
    for feed in feeds or DEFAULT_RSS_FEEDS:
        if len(out) >= limit:
            break
        try:
            with urllib.request.urlopen(feed, timeout=10) as resp:
                data = resp.read()
            root = ET.fromstring(data)
        except Exception as exc:
            logger.warning("news_signal rss fetch failed feed=%s err=%s", feed, exc)
            continue
        channel_title = root.findtext("./channel/title") or "rss"
        for item in root.findall("./channel/item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            key = title.lower()[:120]
            if key in seen:
                continue
            seen.add(key)
            out.append(NewsItem(
                headline=title,
                source=channel_title,
                url=(item.findtext("link") or "").strip(),
                published_at=(item.findtext("pubDate") or "").strip(),
            ))
            if len(out) >= limit:
                break
    return out


def collect_once(
    query: str,
    limit_news: int = 10,
    limit_markets: int = 100,
    max_matches_per_item: int = 3,
    min_relevance: float = 0.12,
    trade_log: Optional[TradeLog] = None,
    gamma=None,
    classifier: Optional[NewsSignalClassifier] = None,
    news_items: Optional[list[NewsItem]] = None,
) -> int:
    """Fetch, classify, and log dry-run news signals. Returns rows inserted."""
    log = trade_log or TradeLog()
    if gamma is None:
        from agents.polymarket.gamma import GammaMarketClient

        gamma_client = GammaMarketClient()
    else:
        gamma_client = gamma
    classifier = classifier or NewsSignalClassifier()
    markets = gamma_client.get_current_markets(limit=limit_markets)
    items = news_items if news_items is not None else fetch_news_items(
        query=query, limit=limit_news
    )
    logger.info(
        "news_signal: fetched %d news items, %d markets for matching",
        len(items), len(markets) if hasattr(markets, "__len__") else "?",
    )
    inserted = 0

    for item in items:
        matches = match_news_to_markets(
            item.headline, markets, min_relevance=min_relevance,
            max_matches=max_matches_per_item,
            min_hits=1,
        )
        for market in matches:
            result = classifier.classify(item, market)
            log.insert_news_signal(
                headline=item.headline,
                source=item.source,
                url=item.url,
                market_id=market.market_id,
                market_question=market.question,
                direction=result.direction,
                materiality=result.materiality,
                relevance_score=market.relevance_score,
                latency_ms=result.latency_ms,
                model=result.model,
                status=getattr(result, "status", NEWS_SIGNAL_STATUS),
                reasoning=result.reasoning,
            )
            inserted += 1
    logger.info(
        "news_signal: inserted=%d query=%s at=%s",
        inserted, query, datetime.now(timezone.utc).isoformat(),
    )
    return inserted


# ---------------------------------------------------------------------------
# Daemon entry-point
# ---------------------------------------------------------------------------


def _env_int_ns(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


if __name__ == "__main__":
    import signal
    import threading

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    _poll_sec = _env_int_ns("NEWS_SIGNAL_POLL_SEC", 900)   # 15 minutes default
    _query = os.getenv("NEWS_SIGNAL_QUERY", "crypto OR bitcoin OR ethereum OR AI OR fed OR nvidia")
    _heartbeat_path = os.getenv("NEWS_SIGNAL_HEARTBEAT_PATH", "/app/data/news_signal_heartbeat")

    _stop = threading.Event()

    def _handle_sigterm(*_) -> None:
        logger.info("news_signal: SIGTERM received — stopping")
        _stop.set()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    logger.info("news_signal: daemon starting — poll=%ds query=%r", _poll_sec, _query)

    while not _stop.is_set():
        try:
            collect_once(query=_query)
        except Exception as exc:
            logger.exception("news_signal: unhandled error: %s", exc)

        # Heartbeat
        try:
            os.makedirs(os.path.dirname(_heartbeat_path) or ".", exist_ok=True)
            with open(_heartbeat_path, "w") as _hb:
                _hb.write(datetime.now(timezone.utc).isoformat())
        except OSError:
            pass

        _stop.wait(_poll_sec)

    logger.info("news_signal: stopped cleanly")
