"""Equity/options fair-value shadow agent for prediction markets.

The first target is the class of markets shown in the research reel:
"largest company by market cap by date".  Polymarket can lag liquid equities
and options markets; this agent turns those external prices into a calibrated
probability and records a shadow signal for MetaBrain/DecisionCouncil.

It is intentionally shadow-only.  Execution must still go through
scanner_executor + DecisionCouncil.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

from agents.application.trade_log import TradeLog

logger = logging.getLogger(__name__)


DEFAULT_SHARES_OUTSTANDING = {
    "NVDA": 24.30e9,
    "MSFT": 7.43e9,
    "AAPL": 14.85e9,
    "GOOGL": 12.20e9,
    "GOOG": 12.20e9,
    "AMZN": 10.65e9,
    "META": 2.55e9,
    "TSLA": 3.22e9,
}

DEFAULT_ANNUAL_VOL = {
    "NVDA": 0.48,
    "MSFT": 0.24,
    "AAPL": 0.25,
    "GOOGL": 0.28,
    "GOOG": 0.28,
    "AMZN": 0.32,
    "META": 0.36,
    "TSLA": 0.58,
}

OUTCOME_TICKERS = {
    "nvidia": "NVDA",
    "nvda": "NVDA",
    "microsoft": "MSFT",
    "msft": "MSFT",
    "apple": "AAPL",
    "aapl": "AAPL",
    "alphabet": "GOOGL",
    "google": "GOOGL",
    "googl": "GOOGL",
    "goog": "GOOGL",
    "amazon": "AMZN",
    "amzn": "AMZN",
    "meta": "META",
    "facebook": "META",
    "tesla": "TSLA",
    "tsla": "TSLA",
}


@dataclass(frozen=True)
class FairValueResult:
    selected_ticker: str
    selected_outcome: str
    fair_probability: float
    market_probability: float
    edge: float
    probabilities: dict
    features: dict


def largest_market_cap_probabilities(
    *,
    prices: dict[str, float],
    shares_outstanding: Optional[dict[str, float]] = None,
    annual_vols: Optional[dict[str, float]] = None,
    days_to_expiry: float = 30.0,
    correlation: float = 0.55,
    simulations: int = 20_000,
    seed: int = 7,
) -> dict[str, float]:
    """Monte-Carlo lognormal estimate of who has the largest market cap."""
    shares_outstanding = shares_outstanding or DEFAULT_SHARES_OUTSTANDING
    annual_vols = annual_vols or DEFAULT_ANNUAL_VOL
    tickers = [
        t
        for t in prices
        if t in shares_outstanding and t in annual_vols and prices.get(t, 0) > 0
    ]
    if len(tickers) < 2:
        return {}
    t_years = max(float(days_to_expiry), 0.25) / 365.0
    rho = max(0.0, min(0.95, float(correlation)))
    common_weight = math.sqrt(rho)
    idio_weight = math.sqrt(1.0 - rho)
    rng = random.Random(seed)
    counts = {ticker: 0 for ticker in tickers}
    total = max(1_000, int(simulations))
    for _ in range(total):
        common = rng.gauss(0.0, 1.0)
        best_ticker = None
        best_cap = -1.0
        for ticker in tickers:
            vol = float(annual_vols[ticker])
            z = common_weight * common + idio_weight * rng.gauss(0.0, 1.0)
            drift = -0.5 * vol * vol * t_years
            terminal_price = float(prices[ticker]) * math.exp(drift + vol * math.sqrt(t_years) * z)
            cap = terminal_price * float(shares_outstanding[ticker])
            if cap > best_cap:
                best_cap = cap
                best_ticker = ticker
        counts[str(best_ticker)] += 1
    return {ticker: round(counts[ticker] / total, 6) for ticker in tickers}


def evaluate_largest_company_market(
    *,
    market: dict,
    prices: dict[str, float],
    now: Optional[datetime] = None,
    simulations: int = 20_000,
) -> Optional[FairValueResult]:
    question = str(market.get("question") or market.get("title") or "")
    if "largest" not in question.lower() and "market cap" not in question.lower():
        return None
    outcomes = _as_list(market.get("outcomes"))
    outcome_prices = [float(x) for x in _as_list(market.get("outcomePrices") or market.get("outcome_prices"))]
    if len(outcomes) < 2 or len(outcomes) != len(outcome_prices):
        return None
    outcome_to_ticker = {outcome: _ticker_for_outcome(outcome) for outcome in outcomes}
    if not any(outcome_to_ticker.values()):
        return None
    days = _days_to_expiry(market, now=now)
    probabilities = largest_market_cap_probabilities(
        prices={ticker: prices[ticker] for ticker in set(outcome_to_ticker.values()) if ticker and ticker in prices},
        days_to_expiry=days,
        simulations=simulations,
    )
    if not probabilities:
        return None
    rows = []
    for outcome, market_prob in zip(outcomes, outcome_prices):
        ticker = outcome_to_ticker.get(outcome)
        if not ticker or ticker not in probabilities:
            continue
        fair_prob = probabilities[ticker]
        rows.append((fair_prob - float(market_prob), ticker, outcome, fair_prob, float(market_prob)))
    if not rows:
        return None
    edge, ticker, outcome, fair_prob, market_prob = max(rows, key=lambda item: item[0])
    return FairValueResult(
        selected_ticker=ticker,
        selected_outcome=str(outcome),
        fair_probability=round(float(fair_prob), 6),
        market_probability=round(float(market_prob), 6),
        edge=round(float(edge), 6),
        probabilities=probabilities,
        features={
            "model": "bivariate_lognormal_market_cap_mc",
            "days_to_expiry": round(days, 3),
            "simulations": simulations,
            "outcome_to_ticker": outcome_to_ticker,
            "prices": {k: prices[k] for k in probabilities if k in prices},
        },
    )


class EquityOptionsFairValueAgent:
    def __init__(self, *, trade_log: Optional[TradeLog] = None, db_path: Optional[str] = None):
        self.trade_log = trade_log or TradeLog(db_path=db_path)

    def run_once(self, *, limit: int = 200) -> dict:
        prices = _load_prices()
        if not prices:
            return {"seen": 0, "recorded": 0, "skipped": 0, "reason": "missing_equity_prices"}
        markets = _fetch_gamma_markets(limit=limit)
        stats = {"seen": 0, "recorded": 0, "skipped": 0}
        for market in markets:
            stats["seen"] += 1
            result = evaluate_largest_company_market(market=market, prices=prices)
            if result is None:
                stats["skipped"] += 1
                continue
            self._record(market, result)
            stats["recorded"] += 1
        return stats

    def _record(self, market: dict, result: FairValueResult) -> None:
        market_id = str(market.get("conditionId") or market.get("condition_id") or market.get("id") or "")
        features = {
            "question": market.get("question") or market.get("title"),
            "selected_outcome": result.selected_outcome,
            "selected_ticker": result.selected_ticker,
            "fair_probability": result.fair_probability,
            "market_probability": result.market_probability,
            "edge": result.edge,
            "probabilities": result.probabilities,
            **result.features,
        }
        self.trade_log.insert_brain_decision(
            agent="equity_options_fair_value",
            strategy="largest_market_cap_fair_value_shadow",
            decision_type="entry_signal",
            market_id=market_id,
            token_id=None,
            approved=result.edge > _env_float("EQUITY_FV_MIN_EDGE", 0.04),
            reason="fair_value_edge" if result.edge > _env_float("EQUITY_FV_MIN_EDGE", 0.04) else "edge_below_min",
            score=max(0.0, min(1.0, 0.5 + result.edge)),
            market_type="equity_options_fair_value",
            features=features,
            action=result.selected_outcome,
            signal_source="equity_options_fair_value",
        )


def _fetch_gamma_markets(*, limit: int) -> list[dict]:
    url = os.getenv("GAMMA_MARKETS_URL", "https://gamma-api.polymarket.com/markets")
    params = {"active": "true", "closed": "false", "limit": str(limit)}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else data.get("markets", [])


def _load_prices() -> dict[str, float]:
    raw = os.getenv("EQUITY_FV_PRICES_JSON")
    if raw:
        try:
            data = json.loads(raw)
            return {str(k).upper(): float(v) for k, v in data.items()}
        except Exception:
            logger.warning("invalid EQUITY_FV_PRICES_JSON", exc_info=True)
    # Shadow-safe fallback for tests/local development only.  Live deployment
    # should set EQUITY_FV_PRICES_JSON from a market-data job/API.
    if os.getenv("EQUITY_FV_ALLOW_STATIC_PRICES", "").lower() in {"1", "true", "yes"}:
        return {
            "NVDA": 215.0,
            "MSFT": 415.0,
            "AAPL": 292.0,
            "GOOGL": 399.0,
            "AMZN": 272.0,
            "TSLA": 429.0,
        }
    return {}


def _ticker_for_outcome(outcome: str) -> Optional[str]:
    text = str(outcome or "").lower()
    for key, ticker in OUTCOME_TICKERS.items():
        if key in text:
            return ticker
    return None


def _days_to_expiry(market: dict, *, now: Optional[datetime]) -> float:
    now = now or datetime.now(timezone.utc)
    raw = market.get("endDate") or market.get("end_date") or market.get("endDateIso")
    if not raw:
        return _env_float("EQUITY_FV_DEFAULT_DAYS_TO_EXPIRY", 30.0)
    try:
        end = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return max(0.25, (end - now).total_seconds() / 86400.0)
    except Exception:
        return _env_float("EQUITY_FV_DEFAULT_DAYS_TO_EXPIRY", 30.0)


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def main() -> int:
    parser = argparse.ArgumentParser(description="Equity/options fair-value shadow agent")
    parser.add_argument("--once", action="store_true", help="run one scan and exit")
    parser.add_argument("--db", default=None, help="override DB path")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    agent = EquityOptionsFairValueAgent(db_path=args.db)
    print(json.dumps(agent.run_once(limit=args.limit), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
