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
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_TAVILY_URL = "https://api.tavily.com/search"


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
    key = api_key or os.getenv("TAVILY_API_KEY", "").strip()
    if not key or not query:
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
        return "\n".join(lines)
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
