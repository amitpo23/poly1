"""Read-only AlphaInsider strategy ranking adapter.

AlphaInsider is used here as an external research/strategy-discovery source.
This module never places orders and never stores API tokens. It pulls public
strategy metadata available to an authenticated user and normalizes it into a
small scorecard the rest of the project can inspect.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable, Optional


TIMEFRAMES = ("day", "week", "month", "year", "five_year")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


@dataclass
class AlphaInsiderTimeframe:
    timeframe: str
    rank_performance: Optional[int] = None
    rank_top: Optional[int] = None
    rank_trending: Optional[int] = None
    rank_popular: Optional[int] = None
    max_drawdown: float = 0.0
    past_value: float = 1.0

    @classmethod
    def from_api(cls, raw: dict) -> "AlphaInsiderTimeframe":
        return cls(
            timeframe=str(raw.get("timeframe") or ""),
            rank_performance=_to_int(raw.get("rank_performance"), 0) or None,
            rank_top=_to_int(raw.get("rank_top"), 0) or None,
            rank_trending=_to_int(raw.get("rank_trending"), 0) or None,
            rank_popular=_to_int(raw.get("rank_popular"), 0) or None,
            max_drawdown=_to_float(raw.get("max_drawdown"), 0.0),
            past_value=_to_float(raw.get("past_value"), 1.0),
        )

    @property
    def return_pct(self) -> float:
        return self.past_value - 1.0


@dataclass
class AlphaInsiderStrategy:
    strategy_id: str
    name: str
    strategy_type: str
    user_id: str
    description: str = ""
    price: float = 0.0
    subscriber_count: int = 0
    categories: list[str] = field(default_factory=list)
    timeframes: dict[str, AlphaInsiderTimeframe] = field(default_factory=dict)

    @classmethod
    def from_api(cls, raw: dict) -> "AlphaInsiderStrategy":
        timeframes = {}
        for item in raw.get("timeframes") or []:
            tf = AlphaInsiderTimeframe.from_api(item)
            if tf.timeframe:
                timeframes[tf.timeframe] = tf
        return cls(
            strategy_id=str(raw.get("strategy_id") or ""),
            name=str(raw.get("name") or ""),
            strategy_type=str(raw.get("type") or ""),
            user_id=str(raw.get("user_id") or ""),
            description=str(raw.get("description") or ""),
            price=_to_float(raw.get("price"), 0.0),
            subscriber_count=_to_int(raw.get("subscriber_count"), 0),
            categories=[str(x) for x in raw.get("categories") or []],
            timeframes=timeframes,
        )

    def family(self) -> str:
        text = f"{self.name} {self.description} {' '.join(self.categories)}".lower()
        rules = [
            ("market_making", r"\bmarket maker|spread|grid\b"),
            ("vwap_mean_reversion", r"\bvwap|mean reversion|reversal|bollinger|z[- ]?score\b"),
            ("supply_demand", r"\bsupply|demand|support|resistance|order block\b"),
            ("trend_momentum", r"\btrend|momentum|breakout|moving average|ema|sma|macd|rsi\b"),
            ("volatility", r"\bvolatility|vix|atr|band\b"),
            ("machine_learning", r"\bmachine learning|ai|neural|xgboost|lstm|rl\b"),
            ("event_sentiment", r"\bnews|congress|insider|earnings|fed|sentiment\b"),
        ]
        for family, pattern in rules:
            if re.search(pattern, text):
                return family
        return "other"

    def score(self, timeframe: str) -> float:
        tf = self.timeframes.get(timeframe)
        if not tf:
            return -1e9
        perf_rank_bonus = 1.0 / max(tf.rank_performance or 9999, 1)
        top_rank_bonus = 0.5 / max(tf.rank_top or 9999, 1)
        subscriber_bonus = min(self.subscriber_count, 500) / 5000.0
        drawdown_penalty = max(tf.max_drawdown, 0.0) * 2.0
        return tf.return_pct + perf_rank_bonus + top_rank_bonus + subscriber_bonus - drawdown_penalty

    def as_dict(self, timeframe: str) -> dict:
        tf = self.timeframes.get(timeframe)
        return {
            "strategy_id": self.strategy_id,
            "name": self.name,
            "type": self.strategy_type,
            "family": self.family(),
            "user_id": self.user_id,
            "price": self.price,
            "subscriber_count": self.subscriber_count,
            "timeframe": timeframe,
            "rank_performance": tf.rank_performance if tf else None,
            "rank_top": tf.rank_top if tf else None,
            "rank_trending": tf.rank_trending if tf else None,
            "rank_popular": tf.rank_popular if tf else None,
            "past_value": tf.past_value if tf else None,
            "return_pct": tf.return_pct if tf else None,
            "max_drawdown": tf.max_drawdown if tf else None,
            "quality_score": self.score(timeframe),
            "categories": self.categories,
            "description_excerpt": self.description[:500],
        }


class AlphaInsiderClient:
    def __init__(
        self,
        *,
        api_token: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout_sec: Optional[int] = None,
    ) -> None:
        self.api_token = api_token or os.getenv("ALPHAINSIDER_API_TOKEN", "").strip()
        self.base_url = (base_url or os.getenv("ALPHAINSIDER_API_BASE_URL", "https://alphainsider.com/api")).rstrip("/")
        self.timeout_sec = timeout_sec if timeout_sec is not None else _env_int("ALPHAINSIDER_TIMEOUT_SEC", 12)
        self.use_auth_for_search = _env_bool("ALPHAINSIDER_USE_AUTH_FOR_SEARCH", False)

    def search_strategies(
        self,
        *,
        timeframe: str = "month",
        sort: str = "performance",
        limit: int = 50,
        offset_id: Optional[str] = None,
        strategy_type: Optional[str] = None,
        max_drawdown: Optional[float] = None,
        trade_count_min: Optional[int] = None,
        price_max: Optional[float] = None,
    ) -> list[AlphaInsiderStrategy]:
        body: dict = {"timeframe": timeframe, "sort": sort, "limit": limit}
        if offset_id:
            body["offset_id"] = offset_id
        if strategy_type:
            body["type"] = {"includes": [strategy_type], "excludes": []}
        if max_drawdown is not None:
            body["max_drawdown"] = max_drawdown
        if trade_count_min is not None:
            body["trade_count_min"] = trade_count_min
        if price_max is not None:
            body["price_max"] = price_max
        data = self._post_json("searchStrategies", body, use_auth=self.use_auth_for_search)
        response = data.get("response") if isinstance(data, dict) else None
        if not isinstance(response, list):
            return []
        return [AlphaInsiderStrategy.from_api(item) for item in response if isinstance(item, dict)]

    def _post_json(self, endpoint: str, body: dict, *, use_auth: bool = True) -> dict:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "poly1-alphainsider/1.0",
        }
        if use_auth and self.api_token:
            headers["Authorization"] = self.api_token
        req = urllib.request.Request(
            f"{self.base_url}/{endpoint}",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:250]
            raise RuntimeError(f"alphainsider {endpoint} failed status={exc.code}: {detail}") from exc


def summarize_rankings(strategies: Iterable[AlphaInsiderStrategy], timeframe: str) -> dict:
    rows = [s.as_dict(timeframe) for s in strategies]
    rows.sort(key=lambda row: row.get("quality_score", -1e9), reverse=True)
    by_family: dict[str, dict] = {}
    for row in rows:
        fam = row["family"]
        item = by_family.setdefault(
            fam,
            {"count": 0, "avg_return_pct": 0.0, "avg_drawdown": 0.0, "best": None},
        )
        item["count"] += 1
        item["avg_return_pct"] += float(row.get("return_pct") or 0.0)
        item["avg_drawdown"] += float(row.get("max_drawdown") or 0.0)
        if item["best"] is None or row["quality_score"] > item["best"]["quality_score"]:
            item["best"] = row
    for item in by_family.values():
        if item["count"]:
            item["avg_return_pct"] = item["avg_return_pct"] / item["count"]
            item["avg_drawdown"] = item["avg_drawdown"] / item["count"]
    return {"timeframe": timeframe, "count": len(rows), "top": rows, "by_family": by_family}
