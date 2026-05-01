"""Deterministic retro: $100 starting balance, today's live markets, LLM stubbed.

The stub forecaster predicts probability = market_price + sin-noise (so half
the time it sees +edge over the market and half the time -edge). This
produces a realistic mix of BUY/SELL/skip decisions. Confidence is set
high (0.65-0.85) so trades pass min_confidence=0.60.

Output: a structured trading journal showing every decision today.
"""
import json
import logging
import math
import os
import random
import sys
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

# --- Set up env BEFORE imports ---
tmpdir = tempfile.mkdtemp(prefix="poly1_retro_")
os.environ.update({
    "TRADE_LOG_DB": os.path.join(tmpdir, "trade_log.db"),
    "LLM_USAGE_FILE": os.path.join(tmpdir, "llm_usage.jsonl"),
    "KILL_SWITCH_FILE": os.path.join(tmpdir, "HALT"),
    "LOG_DIR": tmpdir,
    "LOG_LEVEL": "WARNING",
    "STARTING_BALANCE_USDC": "100.0",
    "MIN_USDC_FLOOR": "0.0",
    "MAX_DAILY_LOSS_PCT": "0.50",
    "MAX_TRADES_PER_HOUR": "99",
    "MAX_DAILY_TOKEN_USD": "999",
    "OPENAI_API_KEY": "stub",
    "GAMMA_EVENT_LIMIT": "100",
})

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, "/Users/mymac/coding/poly1")
os.chdir("/Users/mymac/coding/poly1")

from agents.application.trade import Trader
from agents.application.trade_log import TradeLog, ACTIVE_STATUSES
from agents.polymarket.polymarket import Polymarket
from agents.utils.objects import TradeRecommendation


# --- Stub the LLM-driven helpers in Executor ---
random.seed(42)  # deterministic

def _stub_filter_events_with_rag(self, events):
    """Skip Chroma; just take the first 8 tradeable events with markets attached."""
    chosen = []
    for ev in events[:30]:
        if not (ev.markets or ""):
            continue
        # Build a Document-like tuple expected downstream:
        # (Document with .json() and .dict() metadata containing 'markets')
        from langchain_core.documents import Document
        doc = Document(
            page_content=ev.title,
            metadata={"markets": ev.markets, "id": str(ev.id), "title": ev.title},
        )
        chosen.append((doc, 0.0))
        if len(chosen) >= 8:
            break
    return chosen


def _stub_filter_markets(self, markets):
    """Convert SimpleMarket objects to Document-tuples Chroma normally returns."""
    from langchain_core.documents import Document
    out = []
    for m in markets:
        meta = m.dict() if hasattr(m, "dict") else m
        try:
            outcome_prices = json.loads(meta.get("outcome_prices") or "[]")
            outcomes = json.loads(meta.get("outcomes") or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if len(outcomes) != 2 or len(outcome_prices) != 2:
            continue
        try:
            p0 = float(outcome_prices[0])
        except (TypeError, ValueError):
            continue
        if not (0.10 < p0 < 0.90):
            continue
        # Stash full market data as metadata. trade.py reads clob_token_ids/outcomes.
        meta_str = {
            "id": str(meta.get("id", "")),
            "question": meta.get("question", ""),
            "spread": str(meta.get("spread", 0.0) or 0.0),
            "outcomes": meta.get("outcomes"),
            "outcome_prices": meta.get("outcome_prices"),
            "clob_token_ids": meta.get("clob_token_ids", "[]"),
        }
        doc = Document(
            page_content=meta.get("description", "") or meta.get("question", ""),
            metadata=meta_str,
        )
        # Score: closer to 0.5 = more interesting (smaller distance).
        score = abs(p0 - 0.5)
        out.append((doc, score))
    out.sort(key=lambda x: x[1])
    return out[:6]


# Predetermined edge per question hash so output is stable & realistic
def _llm_forecast(question: str, market_p0: float) -> tuple[float, float]:
    """Return (forecast_p0, confidence)."""
    h = sum(ord(c) for c in question) % 100
    # Six clusters: -10%, -5%, 0%, +5%, +10%, +15% deltas
    deltas = [-0.10, -0.05, 0.0, 0.05, 0.10, 0.15]
    delta = deltas[h % len(deltas)]
    forecast = max(0.02, min(0.98, market_p0 + delta))
    # Confidence: tighter forecasts get higher confidence
    confidence = 0.55 + (abs(delta) * 2.0)  # 0.55 → 0.85
    return round(forecast, 3), round(min(0.95, confidence), 2)


def _stub_source_best_trade(self, market_object):
    """Replace 2-LLM-call pipeline with a deterministic predictor."""
    meta = market_object[0].dict()["metadata"]
    outcomes = json.loads(meta.get("outcomes") or "[]")
    prices = json.loads(meta.get("outcome_prices") or "[]")
    p0 = float(prices[0])
    forecast_p0, confidence = _llm_forecast(meta.get("question", ""), p0)

    # Decide side: BUY if forecast > market (we think outcomes[0] more likely),
    # SELL if forecast < market.
    if forecast_p0 > p0:
        side = "BUY"
    elif forecast_p0 < p0:
        side = "SELL"
    else:
        side = "BUY"  # tie — no edge, but still BUY

    # Skip-no-edge: if |delta| < 3%, generate low confidence (will be filtered)
    if abs(forecast_p0 - p0) < 0.03:
        confidence = 0.50

    # Trade size: 0.05 of balance for high confidence, less otherwise
    size_fraction = 0.05 if confidence >= 0.65 else 0.03

    return json.dumps({
        "price": p0,
        "size_fraction": size_fraction,
        "side": side,
        "confidence": confidence,
        "_forecast_yes": forecast_p0,  # for retro display
    })


# We need clob_token_ids in the market metadata. The gamma /markets endpoint
# returns them, so we ensure map_filtered_events_to_markets fetches them. Patch
# that to use the markets we already have from the events.
def _stub_map_filtered_events_to_markets(self, filtered_events):
    """Fetch each market via gamma /markets/{id}."""
    import requests
    from agents.utils.objects import SimpleMarket

    markets = []
    seen = set()
    for e in filtered_events:
        market_ids = e[0].metadata.get("markets", "").split(",")
        for mid in market_ids:
            mid = mid.strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            try:
                r = requests.get(
                    f"https://gamma-api.polymarket.com/markets/{mid}",
                    timeout=10,
                )
                if not r.ok:
                    continue
                data = r.json()
            except requests.RequestException:
                continue

            outcomes = data.get("outcomes")
            prices = data.get("outcomePrices")
            tokens = data.get("clobTokenIds")
            # Strings -> JSON strings; data may already be lists
            outcomes_str = outcomes if isinstance(outcomes, str) else json.dumps(outcomes or [])
            prices_str = prices if isinstance(prices, str) else json.dumps(prices or [])
            tokens_str = tokens if isinstance(tokens, str) else json.dumps(tokens or [])

            try:
                m = SimpleMarket(
                    id=int(data.get("id")),
                    question=data.get("question", ""),
                    end=data.get("endDate") or "",
                    description=data.get("description") or "",
                    active=bool(data.get("active", True)),
                    funded=bool(data.get("funded", True)),
                    rewardsMinSize=float(data.get("rewardsMinSize") or 0),
                    rewardsMaxSpread=float(data.get("rewardsMaxSpread") or 0),
                    spread=float(data.get("spread") or 0),
                    outcomes=outcomes_str,
                    outcome_prices=prices_str,
                    clob_token_ids=tokens_str,
                )
                markets.append(m)
            except Exception:
                continue
            if len(markets) >= 12:
                return markets
    return markets


# Also bypass _record_usage to avoid file IO noise
def _stub_record_usage(self, result, tag):
    pass


# Skip pre_trade_logic clear_local_dbs (we won't use Chroma anyway)
def _stub_pre_trade_logic(self):
    pass


# --- Run ---
print("=" * 75)
print(f"POLY1 RETROSPECTIVE — $100 starting, dry-run, today's live markets")
print(f"Seed=42 (deterministic). LLM stubbed with hash-based forecasts.")
print(f"Run at: {datetime.now(timezone.utc).isoformat()}")
print("=" * 75)

with patch.object(Polymarket, "get_usdc_balance", lambda self: 100.0):
    from agents.application.executor import Executor
    Executor.filter_events_with_rag = _stub_filter_events_with_rag
    Executor.filter_markets = _stub_filter_markets
    Executor.source_best_trade = _stub_source_best_trade
    Executor.map_filtered_events_to_markets = _stub_map_filtered_events_to_markets
    Executor._record_usage = _stub_record_usage
    Trader.pre_trade_logic = _stub_pre_trade_logic

    trader = Trader(
        dry_run=True,
        top_n=5,
        max_trades_per_cycle=4,
        max_position_fraction=0.05,
        min_confidence=0.60,
    )
    trader.one_best_trade_sweep()

print()
print("=" * 75)
print("RETRO TRADE JOURNAL")
print("=" * 75)
tl = TradeLog(os.environ["TRADE_LOG_DB"])
rows = tl.recent(limit=50)
if not rows:
    print("(no rows)")
else:
    # Pretty print
    print(f"\n{'#':<3} {'status':<18} {'side':<5} {'price':<6} {'size':<6} {'conf':<5} question")
    print("-" * 90)
    total_committed = 0.0
    n_actionable = 0
    for i, r in enumerate(reversed(rows), 1):
        status = r["status"]
        side = r["side"] or "-"
        price = f"{r['price']:.3f}" if r["price"] is not None else "-"
        size = f"${r['size_usdc']:.2f}" if r["size_usdc"] is not None else "-"
        conf = f"{r['confidence']:.2f}" if r["confidence"] is not None else "-"
        market_id = r["market_id"]
        if status in ("skipped_dry_run", "submitted", "filled"):
            n_actionable += 1
            if r["size_usdc"]:
                total_committed += float(r["size_usdc"])
        # truncated market id since we don't have the question text in the row
        print(f"{i:<3} {status:<18} {side:<5} {price:<6} {size:<6} {conf:<5} market_id={market_id}")

    print()
    print("Summary:")
    print(f"  total decisions:        {len(rows)}")
    print(f"  would-have-traded:      {n_actionable}")
    print(f"  total $ committed:      ${total_committed:.2f} of $100 (= {total_committed:.1f}%)")
    print(f"  remaining cash:         ${100.0 - total_committed:.2f}")
    skipped = [r for r in rows if r["status"].startswith("skipped")]
    by_reason = {}
    for r in skipped:
        reason = r["error"] or "no edge"
        by_reason[reason] = by_reason.get(reason, 0) + 1
    if by_reason:
        print(f"\n  skips by reason:")
        for k, v in sorted(by_reason.items(), key=lambda x: -x[1]):
            print(f"    {v}× {k[:80]}")
