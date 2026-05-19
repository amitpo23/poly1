"""Short-horizon Polymarket universe discovery.

This daemon is intentionally read-only with respect to trading.  It discovers
liquid crypto up/down markets, scores them, and writes a single source of truth
that entry agents and dashboards can consume.
"""
from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from agents.application.scalper_pairs import ScalperPairsDAO
from agents.application.trade_log import TradeLog


logger = logging.getLogger(__name__)

ASSETS = ("btc", "eth", "sol", "xrp", "doge", "bnb")
HORIZON_SECONDS = {"5m": 300, "15m": 900}
ROUTE_BY_HORIZON = {"5m": "btc_5min", "15m": "scalper"}
BTC_5MIN_SUPPORTED_ASSETS = {"btc", "eth", "sol", "xrp", "doge"}
LIVE_ROUTE_AGENTS = {"btc_5min", "scalper"}
DEFAULT_OUTPUT_PATH = "/app/data/market_universe.json"


@dataclass(frozen=True)
class MarketCandidate:
    slug: str
    horizon: str
    asset: str
    period_ts: int
    market_id: str
    question: str
    liquidity_usdc: float
    volume_usdc: float
    yes_price: Optional[float]
    no_price: Optional[float]
    up_token: Optional[str]
    down_token: Optional[str]
    accepting_orders: bool
    route_agent: str
    score: float
    winrate_estimate: float
    eligible: bool
    top_rank: Optional[int]
    details_json: dict


@dataclass(frozen=True)
class UniverseConfig:
    assets: tuple[str, ...] = ASSETS
    horizons: tuple[str, ...] = ("5m", "15m")
    periods_ahead: int = 4
    min_liquidity_usdc: float = 1500.0
    min_winrate: float = 0.52
    top_n: int = 10
    poll_sec: float = 60.0
    trend_enabled: bool = True
    trend_every_sec: float = 180.0
    trend_limit: int = 100
    trend_min_liquidity_usdc: float = 5000.0
    trend_min_volume_24h_usdc: float = 1000.0
    trend_max_hours_to_close: float = 24.0
    trend_trade_enabled: bool = False
    daily_journal_weight: float = 0.35
    output_path: str = DEFAULT_OUTPUT_PATH
    write_scalper_pairs: bool = True
    heartbeat_path: str = "/app/data/market_universe_heartbeat"

    @classmethod
    def from_env(cls) -> "UniverseConfig":
        return cls(
            assets=_env_list("MARKET_UNIVERSE_ASSETS", ",".join(ASSETS)),
            horizons=_env_list("MARKET_UNIVERSE_HORIZONS", "5m,15m"),
            periods_ahead=_env_int("MARKET_UNIVERSE_PERIODS_AHEAD", 4),
            min_liquidity_usdc=_env_float("MARKET_UNIVERSE_MIN_LIQUIDITY_USDC", 1500.0),
            min_winrate=_env_float("MARKET_UNIVERSE_MIN_WINRATE", 0.52),
            top_n=_env_int("MARKET_UNIVERSE_TOP_N", 10),
            poll_sec=_env_float("MARKET_UNIVERSE_POLL_SEC", 60.0),
            trend_enabled=_env_bool("MARKET_UNIVERSE_TRENDS_ENABLED", True),
            trend_every_sec=_env_float("MARKET_UNIVERSE_TREND_EVERY_SEC", 180.0),
            trend_limit=_env_int("MARKET_UNIVERSE_TREND_LIMIT", 100),
            trend_min_liquidity_usdc=_env_float(
                "MARKET_UNIVERSE_TREND_MIN_LIQUIDITY_USDC", 5000.0
            ),
            trend_min_volume_24h_usdc=_env_float(
                "MARKET_UNIVERSE_TREND_MIN_VOLUME_24H_USDC", 1000.0
            ),
            trend_max_hours_to_close=_env_float(
                "MARKET_UNIVERSE_TREND_MAX_HOURS_TO_CLOSE", 24.0
            ),
            trend_trade_enabled=_env_bool("MARKET_UNIVERSE_TREND_TRADE_ENABLED", False),
            daily_journal_weight=_env_float("MARKET_UNIVERSE_DAILY_JOURNAL_WEIGHT", 0.35),
            output_path=os.getenv("MARKET_UNIVERSE_OUTPUT_PATH", DEFAULT_OUTPUT_PATH),
            write_scalper_pairs=_env_bool("MARKET_UNIVERSE_WRITE_SCALPER_PAIRS", True),
            heartbeat_path=os.getenv(
                "MARKET_UNIVERSE_HEARTBEAT_PATH", "/app/data/market_universe_heartbeat"
            ),
        )


def _env_list(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    values = []
    for item in raw.split(","):
        value = item.strip().lower()
        if value:
            values.append(value)
    return tuple(dict.fromkeys(values))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _period_floor(now_ts: Optional[int], horizon: str) -> int:
    step = HORIZON_SECONDS[horizon]
    now = int(time.time()) if now_ts is None else int(now_ts)
    return now - (now % step)


def _periods(now_ts: Optional[int], horizon: str, periods_ahead: int) -> Iterable[int]:
    start = _period_floor(now_ts, horizon)
    step = HORIZON_SECONDS[horizon]
    for i in range(max(1, periods_ahead)):
        yield start + (i * step)


def format_slug(asset: str, horizon: str, period_ts: int) -> str:
    return f"{asset.lower()}-updown-{horizon}-{int(period_ts)}"


def route_agent_for(asset: str, horizon: str) -> str:
    asset = asset.lower()
    if horizon == "5m" and asset not in BTC_5MIN_SUPPORTED_ASSETS:
        return "unassigned_5min"
    return ROUTE_BY_HORIZON.get(horizon, "market_scanner")


def _parse_list(value, default: Optional[list] = None) -> list:
    if value in (None, ""):
        return [] if default is None else default
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else ([] if default is None else default)
    except Exception:
        pass
    try:
        parsed = ast.literal_eval(value)
        return parsed if isinstance(parsed, list) else ([] if default is None else default)
    except Exception:
        return [] if default is None else default


def _num(value, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fetch_gamma_market(slug: str, timeout: float = 10.0) -> Optional[dict]:
    query = urllib.parse.urlencode({"slug": slug})
    url = f"https://gamma-api.polymarket.com/markets?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "poly1-market-universe/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload:
        return None
    return payload[0]


def _fetch_gamma_markets(params: dict, timeout: float = 10.0) -> list[dict]:
    query = urllib.parse.urlencode(params)
    url = f"https://gamma-api.polymarket.com/markets?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "poly1-market-universe/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload if isinstance(payload, list) else []


def candidate_from_market(
    market: dict,
    *,
    asset: str,
    horizon: str,
    period_ts: int,
    min_liquidity_usdc: float,
) -> Optional[MarketCandidate]:
    slug = str(market.get("slug") or format_slug(asset, horizon, period_ts))
    active = bool(market.get("active", True))
    closed = bool(market.get("closed", False))
    accepting = bool(market.get("acceptingOrders", False))
    liquidity = _num(market.get("liquidity") or market.get("liquidityNum"))
    volume = _num(market.get("volume") or market.get("volumeNum"))
    if not active or closed or not accepting or liquidity < min_liquidity_usdc:
        return None

    tokens = _parse_list(market.get("clobTokenIds"))
    outcomes = [str(x).lower() for x in _parse_list(market.get("outcomes"))]
    prices = [_num(x, 0.5) for x in _parse_list(market.get("outcomePrices"), [0.5, 0.5])]
    if len(tokens) != 2:
        return None
    up_idx = 0
    down_idx = 1
    if len(outcomes) == 2:
        for i, outcome in enumerate(outcomes):
            if outcome in {"up", "higher", "yes"}:
                up_idx = i
            elif outcome in {"down", "lower", "no"}:
                down_idx = i
    yes_price = prices[up_idx] if len(prices) > up_idx else None
    no_price = prices[down_idx] if len(prices) > down_idx else None
    route_agent = route_agent_for(asset, horizon)
    score = score_candidate(
        liquidity_usdc=liquidity,
        volume_usdc=volume,
        yes_price=yes_price,
        no_price=no_price,
        horizon=horizon,
    )
    winrate_estimate = estimate_winrate_from_score(score)
    return MarketCandidate(
        slug=slug,
        horizon=horizon,
        asset=asset,
        period_ts=period_ts,
        market_id=str(market.get("id") or slug),
        question=str(market.get("question") or ""),
        liquidity_usdc=liquidity,
        volume_usdc=volume,
        yes_price=yes_price,
        no_price=no_price,
        up_token=str(tokens[up_idx]),
        down_token=str(tokens[down_idx]),
        accepting_orders=accepting,
        route_agent=route_agent,
        score=score,
        winrate_estimate=winrate_estimate,
        eligible=False,
        top_rank=None,
        details_json={
            "event_slug": market.get("eventSlug"),
            "end_date": market.get("endDate"),
            "outcomes": outcomes,
        },
    )


def score_candidate(
    *,
    liquidity_usdc: float,
    volume_usdc: float,
    yes_price: Optional[float],
    no_price: Optional[float],
    horizon: str,
) -> float:
    liquidity_score = min(1.0, max(0.0, liquidity_usdc / 10000.0))
    volume_score = min(1.0, max(0.0, volume_usdc / 5000.0))
    prices = [p for p in (yes_price, no_price) if p is not None]
    balance_score = 0.5
    if prices:
        closest_mid_gap = min(abs(float(p) - 0.5) for p in prices)
        balance_score = max(0.0, 1.0 - (closest_mid_gap * 2.0))
    horizon_score = 1.0 if horizon == "5m" else 0.85
    return round(
        (0.40 * liquidity_score)
        + (0.25 * volume_score)
        + (0.25 * balance_score)
        + (0.10 * horizon_score),
        4,
    )


def trend_score_candidate(
    *,
    liquidity_usdc: float,
    volume_24h_usdc: float,
    yes_price: Optional[float],
    no_price: Optional[float],
    hours_to_close: Optional[float],
) -> float:
    liquidity_score = min(1.0, max(0.0, liquidity_usdc / 100000.0))
    volume_score = min(1.0, max(0.0, volume_24h_usdc / 25000.0))
    prices = [p for p in (yes_price, no_price) if p is not None]
    balance_score = 0.5
    if prices:
        closest_mid_gap = min(abs(float(p) - 0.5) for p in prices)
        balance_score = max(0.0, 1.0 - (closest_mid_gap * 2.0))
    time_score = 0.0
    if hours_to_close is not None:
        time_score = max(0.0, min(1.0, (24.0 - hours_to_close) / 24.0))
    return round(
        (0.30 * liquidity_score)
        + (0.35 * volume_score)
        + (0.20 * balance_score)
        + (0.15 * time_score),
        4,
    )


def estimate_winrate_from_score(score: float) -> float:
    return round(max(0.0, min(1.0, score)), 4)


def refine_winrate_with_daily_journal(
    candidate: MarketCandidate,
    trade_log: TradeLog,
    *,
    weight: float = 0.35,
) -> MarketCandidate:
    """Fold today's real trading behavior into a candidate win-rate estimate."""
    try:
        stats = trade_log.daily_trade_journal_stats(agent=candidate.route_agent)
    except Exception as exc:
        logger.debug("market_universe: daily journal refine failed for %s: %s", candidate.slug, exc)
        return candidate
    total = int(stats.get("total_with_outcome") or 0)
    failures = int(stats.get("failures") or 0)
    if total <= 0 and failures <= 0:
        return candidate
    day_wr = stats.get("winrate")
    if day_wr is None:
        day_wr = 0.50
    w = min(0.60, max(0.05, float(weight) * min(1.0, max(1, total) / 10.0)))
    failure_penalty = min(0.18, failures * _env_float("MARKET_UNIVERSE_FAILURE_PENALTY", 0.03))
    refined = ((1.0 - w) * float(candidate.winrate_estimate)) + (w * float(day_wr))
    refined = max(0.0, min(1.0, refined - failure_penalty))
    details = dict(candidate.details_json or {})
    details["daily_journal"] = {
        "agent": candidate.route_agent,
        "wins": stats.get("wins"),
        "losses": stats.get("losses"),
        "failures": failures,
        "winrate": day_wr,
        "weight": round(w, 4),
        "failure_penalty": round(failure_penalty, 4),
    }
    return MarketCandidate(
        **{
            **asdict(candidate),
            "winrate_estimate": round(refined, 4),
            "details_json": details,
        }
    )


def _hours_to_close(end_date: object, now_ts: Optional[int] = None) -> Optional[float]:
    if not end_date:
        return None
    try:
        end = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        now = datetime.fromtimestamp(now_ts or time.time(), tz=timezone.utc)
        return (end - now).total_seconds() / 3600.0
    except Exception:
        return None


def _end_ts(end_date: object) -> int:
    if not end_date:
        return 0
    try:
        return int(datetime.fromisoformat(str(end_date).replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def candidate_from_trend_market(
    market: dict,
    *,
    min_liquidity_usdc: float,
    min_volume_24h_usdc: float,
    max_hours_to_close: float,
    trade_enabled: bool = False,
    now_ts: Optional[int] = None,
) -> Optional[MarketCandidate]:
    slug = str(market.get("slug") or "")
    active = bool(market.get("active", True))
    closed = bool(market.get("closed", False))
    accepting = bool(market.get("acceptingOrders", False))
    liquidity = _num(market.get("liquidity") or market.get("liquidityNum"))
    volume = _num(market.get("volume") or market.get("volumeNum"))
    volume_24h = _num(market.get("volume24hr") or market.get("volume24hrNum"))
    hours_to_close = _hours_to_close(market.get("endDate"), now_ts=now_ts)
    if not slug or not active or closed or not accepting:
        return None
    if liquidity < min_liquidity_usdc or volume_24h < min_volume_24h_usdc:
        return None
    if hours_to_close is None or hours_to_close < 0 or hours_to_close > max_hours_to_close:
        return None

    tokens = _parse_list(market.get("clobTokenIds"))
    prices = [_num(x, 0.5) for x in _parse_list(market.get("outcomePrices"), [0.5, 0.5])]
    outcomes = [str(x).lower() for x in _parse_list(market.get("outcomes"))]
    if len(tokens) != 2:
        return None
    yes_price = prices[0] if prices else None
    no_price = prices[1] if len(prices) > 1 else None
    score = trend_score_candidate(
        liquidity_usdc=liquidity,
        volume_24h_usdc=volume_24h,
        yes_price=yes_price,
        no_price=no_price,
        hours_to_close=hours_to_close,
    )
    return MarketCandidate(
        slug=slug,
        horizon="trend",
        asset="trend",
        period_ts=_end_ts(market.get("endDate")),
        market_id=str(market.get("id") or slug),
        question=str(market.get("question") or ""),
        liquidity_usdc=liquidity,
        volume_usdc=max(volume, volume_24h),
        yes_price=yes_price,
        no_price=no_price,
        up_token=str(tokens[0]),
        down_token=str(tokens[1]),
        accepting_orders=accepting,
        route_agent="scalper" if trade_enabled else "trend_watch",
        score=score,
        winrate_estimate=estimate_winrate_from_score(score),
        eligible=False,
        top_rank=None,
        details_json={
            "source": "gamma_trending",
            "end_date": market.get("endDate"),
            "hours_to_close": round(hours_to_close, 3),
            "volume_24h_usdc": volume_24h,
            "outcomes": outcomes,
        },
    )


def apply_focus_policy(
    candidates: list[MarketCandidate],
    *,
    min_winrate: float,
    top_n: int,
) -> list[MarketCandidate]:
    ranked = sorted(candidates, key=lambda c: (c.winrate_estimate, c.liquidity_usdc), reverse=True)
    eligible_slugs = {
        c.slug: rank
        for rank, c in enumerate(
            [
                c
                for c in ranked
                if c.route_agent in LIVE_ROUTE_AGENTS
                and c.accepting_orders
                and c.winrate_estimate >= min_winrate
            ][: max(1, top_n)],
            start=1,
        )
    }
    out: list[MarketCandidate] = []
    for candidate in candidates:
        rank = eligible_slugs.get(candidate.slug)
        out.append(
            MarketCandidate(
                **{
                    **asdict(candidate),
                    "eligible": rank is not None,
                    "top_rank": rank,
                }
            )
        )
    return sorted(out, key=lambda c: (c.top_rank is None, c.top_rank or 9999, -c.score))


def discover_trends(config: UniverseConfig, now_ts: Optional[int] = None) -> list[MarketCandidate]:
    if not config.trend_enabled:
        return []
    seen: set[str] = set()
    candidates: list[MarketCandidate] = []
    param_sets = [
        {"order": "volume24hr", "ascending": "false"},
        {"order": "volume", "ascending": "false"},
        {"order": "liquidity", "ascending": "false"},
    ]
    for extra in param_sets:
        params = {
            "active": "true",
            "closed": "false",
            "archived": "false",
            "limit": str(config.trend_limit),
            **extra,
        }
        try:
            markets = _fetch_gamma_markets(params)
        except Exception as exc:
            logger.debug("market_universe: trend fetch failed %s: %s", extra, exc)
            continue
        for market in markets:
            slug = str(market.get("slug") or "")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            candidate = candidate_from_trend_market(
                market,
                min_liquidity_usdc=config.trend_min_liquidity_usdc,
                min_volume_24h_usdc=config.trend_min_volume_24h_usdc,
                max_hours_to_close=config.trend_max_hours_to_close,
                trade_enabled=config.trend_trade_enabled,
                now_ts=now_ts,
            )
            if candidate:
                candidates.append(candidate)
    return sorted(candidates, key=lambda c: (c.score, c.liquidity_usdc), reverse=True)


def discover(
    config: UniverseConfig,
    now_ts: Optional[int] = None,
    *,
    include_trends: bool = True,
) -> list[MarketCandidate]:
    candidates: list[MarketCandidate] = []
    for horizon in config.horizons:
        if horizon not in HORIZON_SECONDS:
            logger.warning("market_universe: unknown horizon %s", horizon)
            continue
        for asset in config.assets:
            for period_ts in _periods(now_ts, horizon, config.periods_ahead):
                slug = format_slug(asset, horizon, period_ts)
                try:
                    market = _fetch_gamma_market(slug)
                except Exception as exc:
                    logger.debug("market_universe: gamma fetch failed %s: %s", slug, exc)
                    continue
                if not market:
                    continue
                candidate = candidate_from_market(
                    market,
                    asset=asset,
                    horizon=horizon,
                    period_ts=period_ts,
                    min_liquidity_usdc=config.min_liquidity_usdc,
                )
                if candidate:
                    candidates.append(candidate)
    if include_trends:
        candidates.extend(discover_trends(config, now_ts=now_ts))
    candidates = sorted(candidates, key=lambda c: (c.score, c.liquidity_usdc), reverse=True)
    return apply_focus_policy(
        candidates,
        min_winrate=config.min_winrate,
        top_n=config.top_n,
    )


def persist(
    trade_log: TradeLog,
    candidates: list[MarketCandidate],
    *,
    output_path: str,
    write_scalper_pairs: bool,
) -> None:
    dao = ScalperPairsDAO(trade_log) if write_scalper_pairs else None
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "count": len(candidates),
        "candidates": [asdict(c) for c in candidates],
    }
    for candidate in candidates:
        row = asdict(candidate)
        row["ts"] = payload["ts"]
        trade_log.upsert_market_universe(row)
        if (
            dao
            and candidate.eligible
            and candidate.route_agent == "scalper"
            and candidate.up_token
            and candidate.down_token
            and candidate.period_ts
        ):
            dao.create(candidate.slug, candidate.period_ts, candidate.up_token, candidate.down_token)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def run_once(
    config: UniverseConfig,
    db_path: Optional[str] = None,
    *,
    include_trends: bool = True,
) -> list[MarketCandidate]:
    trade_log = TradeLog(db_path=db_path)
    candidates = discover(config, include_trends=include_trends)
    candidates = [
        refine_winrate_with_daily_journal(
            candidate,
            trade_log,
            weight=config.daily_journal_weight,
        )
        for candidate in candidates
    ]
    candidates = apply_focus_policy(
        candidates,
        min_winrate=config.min_winrate,
        top_n=config.top_n,
    )
    persist(
        trade_log,
        candidates,
        output_path=config.output_path,
        write_scalper_pairs=config.write_scalper_pairs,
    )
    Path(config.heartbeat_path).parent.mkdir(parents=True, exist_ok=True)
    Path(config.heartbeat_path).touch()
    logger.info("market_universe: persisted %d candidates", len(candidates))
    return candidates


def run_daemon(config: UniverseConfig, db_path: Optional[str] = None) -> None:
    logger.info(
        "market_universe: starting assets=%s horizons=%s min_liquidity=%.2f",
        ",".join(config.assets),
        ",".join(config.horizons),
        config.min_liquidity_usdc,
    )
    last_trend_scan = 0.0
    while True:
        try:
            now = time.time()
            include_trends = (
                config.trend_enabled
                and now - last_trend_scan >= max(30.0, config.trend_every_sec)
            )
            run_once(config, db_path=db_path, include_trends=include_trends)
            if include_trends:
                last_trend_scan = now
        except Exception:
            logger.exception("market_universe: scan failed")
        time.sleep(max(5.0, config.poll_sec))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--print", action="store_true", dest="print_json")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    config = UniverseConfig.from_env()
    if args.once or args.print_json:
        candidates = run_once(config, db_path=args.db)
        if args.print_json:
            print(json.dumps([asdict(c) for c in candidates], indent=2, sort_keys=True))
        return
    run_daemon(config, db_path=args.db)


if __name__ == "__main__":
    main()
