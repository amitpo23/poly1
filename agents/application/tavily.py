"""Minimal Tavily search helper — pure stdlib, no extra dependencies.

Used by multiple agents to get fresh external news context before making
or confirming a trade decision.  Graceful on any network / key error.

Usage::

    from agents.application.tavily import tavily_headlines
    context = tavily_headlines(market_question)   # "" when key missing
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_TAVILY_URL = "https://api.tavily.com/search"
_CACHE_PATH = Path(os.getenv("TAVILY_CACHE_PATH", "./data/tavily_cache.json"))
_USAGE_PATH = Path(os.getenv("TAVILY_USAGE_PATH", "./data/tavily_usage.json"))


def _enabled() -> bool:
    return os.getenv("TAVILY_ENABLED", "true").lower() in {"1", "true", "yes", "on"}


def _cache_ttl_sec() -> int:
    try:
        return int(os.getenv("TAVILY_CACHE_TTL_SEC", "21600"))
    except ValueError:
        return 21600


def _daily_limit() -> int:
    try:
        return int(os.getenv("TAVILY_DAILY_LIMIT", "5"))
    except ValueError:
        return 5


def _max_results_cap() -> int:
    try:
        return max(1, int(os.getenv("TAVILY_MAX_RESULTS", "2")))
    except ValueError:
        return 2


def _min_interval_sec() -> int:
    try:
        return max(0, int(os.getenv("TAVILY_MIN_QUERY_INTERVAL_SEC", "900")))
    except ValueError:
        return 900


def _critical_only() -> bool:
    return os.getenv("TAVILY_CRITICAL_ONLY", "true").lower() in {
        "1", "true", "yes", "on",
    }


def _critical_keywords() -> list[str]:
    raw = os.getenv(
        "TAVILY_CRITICAL_KEYWORDS",
        ",".join([
            "breaking", "urgent", "confirmed", "attack", "missile", "war",
            "iran", "israel", "fed", "fomc", "rate", "cpi", "inflation",
            "oil", "crude", "gas", "hack", "exploit", "sec", "etf",
            "earnings", "merger", "lawsuit", "election", "bitcoin", "btc",
            "ethereum", "crypto",
        ]),
    )
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _query_is_critical(query: str) -> bool:
    if not _critical_only():
        return True
    words = set(re.findall(r"[a-z0-9]+", query.lower()))
    text = query.lower()
    for keyword in _critical_keywords():
        if " " in keyword:
            if keyword in text:
                return True
        elif keyword in words:
            return True
    return False


def _cache_key(query: str, max_results: int) -> str:
    return json.dumps(
        {"query": query.strip().lower(), "max_results": int(max_results)},
        sort_keys=True,
    )


def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("tavily: failed reading %s", path, exc_info=True)
    return {}


def _write_json(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    except Exception:
        logger.debug("tavily: failed writing %s", path, exc_info=True)


def _cached(query: str, max_results: int) -> Optional[str]:
    cache = _read_json(_CACHE_PATH)
    item = cache.get(_cache_key(query, max_results))
    if not isinstance(item, dict):
        return None
    ts = float(item.get("ts") or 0)
    if time.time() - ts > _cache_ttl_sec():
        return None
    value = item.get("value")
    return value if isinstance(value, str) else None


def _store_cache(query: str, max_results: int, value: str) -> None:
    cache = _read_json(_CACHE_PATH)
    cache[_cache_key(query, max_results)] = {"ts": time.time(), "value": value}
    _write_json(_CACHE_PATH, cache)


def _usage_allowed() -> bool:
    today = time.strftime("%Y-%m-%d", time.gmtime())
    usage = _read_json(_USAGE_PATH)
    if usage.get("date") != today:
        usage = {"date": today, "count": 0, "last_call_ts": 0.0}
    if int(usage.get("count") or 0) >= _daily_limit():
        logger.info("tavily: daily limit reached (%s)", _daily_limit())
        return False
    last_call_ts = float(usage.get("last_call_ts") or 0.0)
    elapsed = time.time() - last_call_ts
    min_interval = _min_interval_sec()
    if min_interval and last_call_ts and elapsed < min_interval:
        logger.info(
            "tavily: throttled by min interval (%ss remaining)",
            int(min_interval - elapsed),
        )
        return False
    usage["count"] = int(usage.get("count") or 0) + 1
    usage["last_call_ts"] = time.time()
    _write_json(_USAGE_PATH, usage)
    return True


def tavily_headlines(
    query: str,
    *,
    api_key: Optional[str] = None,
    max_results: int = 4,
    timeout: int = 10,
) -> str:
    """Return a newline-separated bullet list of Tavily headlines, or "" on any error.

    Never raises — any failure returns empty string so callers can treat
    missing Tavily context as "no enrichment available".
    """
    max_results = min(max(1, int(max_results)), _max_results_cap())
    key = api_key or os.getenv("TAVILY_API_KEY", "").strip()
    cached = _cached(query, max_results)
    if cached is not None:
        return cached
    if not _enabled():
        return ""
    if not key or not query:
        return ""
    if not _query_is_critical(query):
        logger.info("tavily: skipped non-critical query under critical-only mode")
        return ""
    if not _usage_allowed():
        return ""
    payload = json.dumps({
        "api_key": key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "topic": "news",
    }).encode("utf-8")
    req = urllib.request.Request(
        _TAVILY_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "poly1-tavily/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        results = body.get("results") or []
        lines = [
            f"- {(r.get('title') or '').strip()}"
            for r in results[:max_results]
            if (r.get("title") or "").strip()
        ]
        logger.debug("tavily_headlines: %d results for '%s'", len(lines), query[:60])
        value = "\n".join(lines)
        _store_cache(query, max_results, value)
        return value
    except Exception as exc:
        logger.debug("tavily_headlines failed for '%s': %s", query[:60], exc)
        return ""


def tavily_confidence(
    query: str,
    direction_keywords_yes: list[str],
    direction_keywords_no: list[str],
    *,
    api_key: Optional[str] = None,
    max_results: int = 5,
    timeout: int = 10,
) -> tuple[str, float]:
    """Classify Tavily search results as supporting YES, NO, or neutral.

    Returns (direction, confidence) where direction is "yes" | "no" | "neutral"
    and confidence is in [0, 1].  Returns ("neutral", 0.5) on any error.

    Scoring: each headline is scored +1 if it contains a YES keyword, -1 for
    NO keyword.  Net score is normalised to [0,1] and mapped to direction.
    """
    headlines_text = tavily_headlines(
        query,
        api_key=api_key,
        max_results=max_results,
        timeout=timeout,
    )
    if not headlines_text:
        return ("neutral", 0.5)

    yes_hits = sum(
        1
        for kw in direction_keywords_yes
        if kw.lower() in headlines_text.lower()
    )
    no_hits = sum(
        1
        for kw in direction_keywords_no
        if kw.lower() in headlines_text.lower()
    )
    total = yes_hits + no_hits
    if total == 0:
        return ("neutral", 0.5)

    ratio = yes_hits / total
    if ratio >= 0.65:
        direction = "yes"
        confidence = min(0.85, 0.55 + 0.30 * ratio)
    elif ratio <= 0.35:
        direction = "no"
        confidence = min(0.85, 0.55 + 0.30 * (1.0 - ratio))
    else:
        direction = "neutral"
        confidence = 0.50
    return (direction, round(confidence, 3))
