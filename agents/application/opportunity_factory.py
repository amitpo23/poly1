"""Opportunity Factory — turn strong indicators into auditable trade candidates.

The factory is proactive, but deliberately not a money-moving component.  It
reads proven external signals (wallets, AlphaInsider rankings, market universe)
and writes either:

* executable scanner opportunities when a signal has a direction and calibrated
  probability; or
* attention decisions when a source is strong but does not yet imply a concrete
  side for a specific market.

scanner_executor remains the last mile: EV, orderbook, duplicate and RiskGate
checks still apply before a shadow/live entry can happen.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from agents.application.crypto_exchange_tape import CryptoExchangeTapeClient
from agents.application.trade_log import TradeLog


logger = logging.getLogger(__name__)
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


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
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class OpportunityFactoryConfig:
    db_path: str = "./data/trade_log.db"
    data_dir: str = "./data"
    heartbeat_path: str = "./data/opportunity_factory_heartbeat"
    report_path: str = "./data/opportunity_factory_latest.json"
    alphainsider_path: str = "./data/alphainsider_strategy_rankings_latest.json"
    market_universe_path: str = "./data/market_universe.json"
    max_wallet_age_minutes: int = 60
    min_wallet_profit_usdc: float = 1_000_000.0
    min_wallet_trades_30d: int = 10
    min_wallet_winrate: float = 0.70
    max_wallet_rank: int = 100
    max_wallet_candidates: int = 10
    min_alphainsider_return_pct: float = 0.10
    max_alphainsider_drawdown: float = 0.35
    max_alphainsider_rank: int = 25
    enable_alphainsider_directional: bool = True
    min_alphainsider_directional_probability: float = 0.54
    min_alphainsider_directional_confidence: float = 0.54
    alphainsider_tape_probability_calibrated: bool = False
    max_alphainsider_directional_candidates: int = 6
    max_attention_decisions: int = 8

    @classmethod
    def from_env(cls) -> "OpportunityFactoryConfig":
        data_dir = os.getenv("OPPORTUNITY_FACTORY_DATA_DIR", "./data")
        return cls(
            db_path=os.getenv("OPPORTUNITY_FACTORY_DB_PATH", "./data/trade_log.db"),
            data_dir=data_dir,
            heartbeat_path=os.getenv(
                "OPPORTUNITY_FACTORY_HEARTBEAT_PATH",
                f"{data_dir}/opportunity_factory_heartbeat",
            ),
            report_path=os.getenv(
                "OPPORTUNITY_FACTORY_REPORT_PATH",
                f"{data_dir}/opportunity_factory_latest.json",
            ),
            alphainsider_path=os.getenv(
                "OPPORTUNITY_FACTORY_ALPHAINSIDER_PATH",
                f"{data_dir}/alphainsider_strategy_rankings_latest.json",
            ),
            market_universe_path=os.getenv(
                "OPPORTUNITY_FACTORY_MARKET_UNIVERSE_PATH",
                f"{data_dir}/market_universe.json",
            ),
            max_wallet_age_minutes=_env_int("OPPORTUNITY_FACTORY_MAX_WALLET_AGE_MINUTES", 60),
            min_wallet_profit_usdc=_env_float(
                "OPPORTUNITY_FACTORY_MIN_WALLET_PROFIT_USDC",
                1_000_000.0,
            ),
            min_wallet_trades_30d=_env_int("OPPORTUNITY_FACTORY_MIN_WALLET_TRADES_30D", 10),
            min_wallet_winrate=_env_float("OPPORTUNITY_FACTORY_MIN_WALLET_WINRATE", 0.70),
            max_wallet_rank=_env_int("OPPORTUNITY_FACTORY_MAX_WALLET_RANK", 100),
            max_wallet_candidates=_env_int("OPPORTUNITY_FACTORY_MAX_WALLET_CANDIDATES", 10),
            min_alphainsider_return_pct=_env_float(
                "OPPORTUNITY_FACTORY_MIN_ALPHAINSIDER_RETURN_PCT",
                0.10,
            ),
            max_alphainsider_drawdown=_env_float(
                "OPPORTUNITY_FACTORY_MAX_ALPHAINSIDER_DRAWDOWN",
                0.35,
            ),
            max_alphainsider_rank=_env_int("OPPORTUNITY_FACTORY_MAX_ALPHAINSIDER_RANK", 25),
            enable_alphainsider_directional=_env_bool(
                "OPPORTUNITY_FACTORY_ENABLE_ALPHAINSIDER_DIRECTIONAL",
                True,
            ),
            min_alphainsider_directional_probability=_env_float(
                "OPPORTUNITY_FACTORY_MIN_ALPHAINSIDER_DIRECTIONAL_PROBABILITY",
                0.54,
            ),
            min_alphainsider_directional_confidence=_env_float(
                "OPPORTUNITY_FACTORY_MIN_ALPHAINSIDER_DIRECTIONAL_CONFIDENCE",
                0.54,
            ),
            alphainsider_tape_probability_calibrated=_env_bool(
                "OPPORTUNITY_FACTORY_ALPHAINSIDER_TAPE_PROBABILITY_CALIBRATED",
                False,
            ),
            max_alphainsider_directional_candidates=_env_int(
                "OPPORTUNITY_FACTORY_MAX_ALPHAINSIDER_DIRECTIONAL_CANDIDATES",
                6,
            ),
            max_attention_decisions=_env_int("OPPORTUNITY_FACTORY_MAX_ATTENTION_DECISIONS", 8),
        )


class OpportunityFactory:
    def __init__(
        self,
        *,
        cfg: Optional[OpportunityFactoryConfig] = None,
        trade_log: Optional[TradeLog] = None,
        crypto_tape: Optional[CryptoExchangeTapeClient] = None,
    ) -> None:
        self.cfg = cfg or OpportunityFactoryConfig.from_env()
        self.trade_log = trade_log or TradeLog(db_path=self.cfg.db_path)
        self.crypto_tape = crypto_tape or CryptoExchangeTapeClient()

    def run_once(self) -> dict:
        stats = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "wallet_candidates": 0,
            "alphainsider_directional_candidates": 0,
            "attention_decisions": 0,
            "wallet_skipped": {},
            "proven_alphainsider_families": [],
        }
        proven = self._proven_alphainsider()
        stats["proven_alphainsider_families"] = sorted(proven.keys())
        (
            stats["alphainsider_directional_candidates"],
            stats["attention_decisions"],
        ) = self._write_alphainsider_opportunities(proven)
        wallet_rows = self._fresh_wallet_signals()
        for row in wallet_rows:
            if stats["wallet_candidates"] >= self.cfg.max_wallet_candidates:
                break
            reason = self._wallet_blocker(row)
            if reason:
                stats["wallet_skipped"][reason] = stats["wallet_skipped"].get(reason, 0) + 1
                continue
            if self._write_wallet_candidate(row):
                stats["wallet_candidates"] += 1
        self._write_json(Path(self.cfg.report_path), stats)
        self._touch(Path(self.cfg.heartbeat_path))
        logger.info("opportunity_factory: %s", json.dumps(stats, sort_keys=True))
        return stats

    def _proven_alphainsider(self) -> dict[str, dict]:
        payload = _read_json(Path(self.cfg.alphainsider_path))
        proven: dict[str, dict] = {}
        for timeframe, block in (payload.get("timeframes") or {}).items():
            for row in block.get("top") or []:
                family = str(row.get("family") or "other")
                return_pct = _float(row.get("return_pct"))
                max_dd = _float(row.get("max_drawdown"), 1.0)
                rank = min(
                    _int(row.get("rank_performance"), 999999),
                    _int(row.get("rank_top"), 999999),
                )
                if (
                    return_pct >= self.cfg.min_alphainsider_return_pct
                    and max_dd <= self.cfg.max_alphainsider_drawdown
                    and rank <= self.cfg.max_alphainsider_rank
                ):
                    current = proven.get(family)
                    if current is None or return_pct > current.get("return_pct", -1.0):
                        proven[family] = {**row, "matched_timeframe": timeframe}
        return proven

    def _write_alphainsider_opportunities(self, proven: dict[str, dict]) -> tuple[int, int]:
        if not proven:
            return 0, 0
        universe = _read_json(Path(self.cfg.market_universe_path))
        candidates = universe.get("candidates") or []
        directional_count = 0
        attention_count = 0
        for cand in candidates:
            if (
                attention_count >= self.cfg.max_attention_decisions
                and directional_count >= self.cfg.max_alphainsider_directional_candidates
            ):
                break
            if not bool(cand.get("eligible")):
                continue
            family = _family_for_candidate(cand)
            source = proven.get(family)
            if not source:
                continue
            if (
                self.cfg.enable_alphainsider_directional
                and directional_count < self.cfg.max_alphainsider_directional_candidates
                and self._write_alphainsider_directional_candidate(cand, source, family)
            ):
                directional_count += 1
                continue
            if attention_count >= self.cfg.max_attention_decisions:
                continue
            features = {
                "question": cand.get("question"),
                "slug": cand.get("slug"),
                "market_id": cand.get("market_id"),
                "route_agent": cand.get("route_agent"),
                "asset": cand.get("asset"),
                "horizon": cand.get("horizon"),
                "liquidity_usdc": cand.get("liquidity_usdc"),
                "alphainsider_family": family,
                "alphainsider_strategy_id": source.get("strategy_id"),
                "alphainsider_strategy_name": source.get("name"),
                "alphainsider_return_pct": source.get("return_pct"),
                "alphainsider_max_drawdown": source.get("max_drawdown"),
                "alphainsider_rank_performance": source.get("rank_performance"),
                "alphainsider_rank_top": source.get("rank_top"),
                "alphainsider_matched_timeframes": 1,
                "attention_reason": "proven_strategy_family_no_direction",
            }
            self.trade_log.insert_brain_decision(
                agent="opportunity_factory",
                strategy="indicator_attention",
                decision_type="entry",
                market_id=str(cand.get("market_id") or cand.get("slug") or ""),
                token_id=str(cand.get("up_token") or ""),
                approved=False,
                reason="proven_indicator_without_market_direction",
                score=float(source.get("quality_score") or 0.0),
                market_type="indicator_attention",
                asset=str(cand.get("asset") or ""),
                features=features,
                action="WATCH",
                signal_source=f"alphainsider_strategy:{family}",
            )
            attention_count += 1
        return directional_count, attention_count

    def _write_alphainsider_directional_candidate(
        self,
        cand: dict,
        source: dict,
        family: str,
    ) -> bool:
        if family != "trend_momentum":
            return False
        asset = str(cand.get("asset") or "").lower()
        horizon = str(cand.get("horizon") or "").lower()
        if asset not in {"btc", "eth", "sol", "xrp", "doge", "bnb"}:
            return False
        if horizon not in {"5m", "15m"}:
            return False
        market_key = str(cand.get("market_id") or "")
        if market_key and self.trade_log.has_active_trade_for_market(market_key):
            return False
        signal = self.crypto_tape.analyze_question(str(cand.get("question") or cand.get("slug") or asset))
        if signal.direction not in {"bullish", "bearish"}:
            return False
        if signal.probability < self.cfg.min_alphainsider_directional_probability:
            return False
        if signal.confidence < self.cfg.min_alphainsider_directional_confidence:
            return False
        market = self._gamma_market(market_key)
        if not market:
            return False
        tokens = _json_list(market.get("clobTokenIds"))
        outcomes = _json_list(market.get("outcomes")) or ["Up", "Down"]
        prices = [_float(x, 0.5) for x in _json_list(market.get("outcomePrices"))[:2]]
        if len(tokens) < 2:
            return False
        selected_index = 0 if signal.direction == "bullish" else 1
        selected_price = prices[selected_index] if len(prices) > selected_index else 0.5
        if selected_price <= 0:
            return False
        prob = max(0.0, min(1.0, float(signal.probability)))
        raw_ev = (prob - selected_price) / selected_price
        if raw_ev <= 0:
            return False
        side = "BUY" if selected_index == 0 else "SELL"
        condition_id = str(market.get("conditionId") or cand.get("condition_id") or market_key)
        features = {
            "question": market.get("question") or cand.get("question"),
            "condition_id": condition_id,
            "gamma_market_id": str(market.get("id") or market_key),
            "slug": market.get("slug") or cand.get("slug"),
            "route_agent": cand.get("route_agent"),
            "asset": asset,
            "horizon": horizon,
            "outcomes": outcomes,
            "outcome_prices": [round(x, 4) for x in prices[:2]],
            "clob_token_ids": [str(x) for x in tokens[:2]],
            "selected_side": side,
            "selected_token_id": str(tokens[selected_index]),
            "selected_outcome": outcomes[selected_index] if len(outcomes) > selected_index else signal.direction,
            "selected_entry_price": round(selected_price, 4),
            "meta_timing": "now",
            "estimated_win_probability": round(prob, 4),
            "estimated_win_probability_calibrated": bool(
                self.cfg.alphainsider_tape_probability_calibrated
            ),
            "estimated_win_probability_source": "alphainsider_proven_family_plus_crypto_tape",
            "scanner_raw_ev": round(raw_ev, 4),
            "evidence_route": {
                "mode": "solo",
                "leader": f"alphainsider:{source.get('strategy_id') or family}",
                "provider": "alphainsider_plus_crypto_tape",
                "direction": "yes" if selected_index == 0 else "no",
                "probability": round(prob, 4),
                "reason": signal.reason,
            },
            "alphainsider_family": family,
            "alphainsider_strategy_id": source.get("strategy_id"),
            "alphainsider_strategy_name": source.get("name"),
            "alphainsider_return_pct": source.get("return_pct"),
            "alphainsider_max_drawdown": source.get("max_drawdown"),
            "alphainsider_rank_performance": source.get("rank_performance"),
            "alphainsider_rank_top": source.get("rank_top"),
            "external_direction": signal.direction,
            "external_probability": signal.probability,
            "external_confidence": signal.confidence,
            "external_reason": signal.reason,
            **{f"crypto_tape_{k}": v for k, v in (signal.features or {}).items()},
            "market_cluster": str(market.get("slug") or cand.get("slug") or market_key)[:64],
        }
        self.trade_log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id=condition_id,
            token_id=str(tokens[selected_index]),
            approved=True,
            reason=f"opportunity_factory_alphainsider_tape prob={prob:.3f} ev={raw_ev:.3f}",
            score=prob,
            market_type="crypto_updown",
            asset=asset,
            features=features,
            action=side,
            signal_source="opportunity_factory,alphainsider_proven,crypto_tape",
        )
        return True

    def _fresh_wallet_signals(self) -> list[dict]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=self.cfg.max_wallet_age_minutes)
        ).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            with self.trade_log._lock, self.trade_log._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM wallet_signals
                    WHERE status = 'fresh' AND ts >= ?
                    ORDER BY wallet_profit_usdc DESC, wallet_winrate_external DESC
                    """,
                    (cutoff,),
                ).fetchall()
            return [dict(row) for row in rows]
        except Exception as exc:
            logger.warning("opportunity_factory: wallet read failed: %s", exc)
            return []

    def _wallet_blocker(self, row: dict) -> str:
        profit = _float(row.get("wallet_profit_usdc"))
        trades = _int(row.get("wallet_trades_30d"))
        winrate = row.get("wallet_winrate_external")
        rank = row.get("wallet_rank")
        if profit < self.cfg.min_wallet_profit_usdc:
            return "wallet_profit_below_min"
        if trades < self.cfg.min_wallet_trades_30d:
            return "wallet_trades_below_min"
        if winrate is None or _float(winrate) < self.cfg.min_wallet_winrate:
            return "wallet_winrate_below_min"
        if rank is not None and _int(rank, 999999) > self.cfg.max_wallet_rank:
            return "wallet_rank_below_min"
        if str(row.get("direction") or "").lower() not in {"yes", "no", "up", "down", "buy", "sell"}:
            return "wallet_direction_missing"
        return ""

    def _write_wallet_candidate(self, row: dict) -> bool:
        market = self._gamma_market(str(row.get("market_id") or ""))
        if not market:
            return False
        tokens = _json_list(market.get("clobTokenIds"))
        outcomes = _json_list(market.get("outcomes")) or ["Yes", "No"]
        prices = [_float(x, 0.5) for x in _json_list(market.get("outcomePrices"))[:2]]
        if len(tokens) < 2:
            return False
        direction = str(row.get("direction") or "").lower()
        selected_index = 0 if direction in {"yes", "up", "buy"} else 1
        side = "BUY" if selected_index == 0 else "SELL"
        entry_price = prices[selected_index] if len(prices) > selected_index else _float(row.get("yes_price"), 0.5)
        prob = max(0.0, min(1.0, _float(row.get("wallet_winrate_external"), 0.5)))
        raw_ev = (prob - max(entry_price, 1e-9)) / max(entry_price, 1e-9)
        features = {
            "question": market.get("question") or row.get("market_question"),
            "condition_id": market.get("conditionId") or row.get("market_id"),
            "gamma_market_id": str(market.get("id") or row.get("market_id") or ""),
            "slug": market.get("slug"),
            "outcomes": outcomes,
            "outcome_prices": [round(x, 4) for x in prices[:2]],
            "clob_token_ids": [str(x) for x in tokens[:2]],
            "selected_side": side,
            "selected_token_id": str(tokens[selected_index]),
            "selected_outcome": outcomes[selected_index] if len(outcomes) > selected_index else direction,
            "selected_entry_price": round(entry_price, 4),
            "meta_timing": "now",
            "estimated_win_probability": round(prob, 4),
            "estimated_win_probability_calibrated": True,
            "estimated_win_probability_source": "wallet_external_winrate",
            "scanner_raw_ev": round(raw_ev, 4),
            "evidence_route": {
                "mode": "solo",
                "leader": f"wallet:{str(row.get('wallet_address') or '')[:12]}",
                "provider": "proven_wallet",
                "direction": "yes" if selected_index == 0 else "no",
                "probability": round(prob, 4),
                "reason": "wallet_external_winrate",
            },
            "wallet_address": row.get("wallet_address"),
            "wallet_profit_usdc": row.get("wallet_profit_usdc"),
            "wallet_trades_30d": row.get("wallet_trades_30d"),
            "wallet_winrate_external": row.get("wallet_winrate_external"),
            "wallet_total_trades_external": row.get("wallet_total_trades_external"),
            "wallet_rank": row.get("wallet_rank"),
            "market_cluster": str(market.get("slug") or row.get("market_id") or "")[:64],
        }
        self.trade_log.insert_brain_decision(
            agent="market_scanner",
            strategy="scanner_trade_opportunity",
            decision_type="entry",
            market_id=str(market.get("conditionId") or row.get("market_id") or ""),
            token_id=str(tokens[selected_index]),
            approved=True,
            reason=f"opportunity_factory_wallet prob={prob:.3f} ev={raw_ev:.3f}",
            score=prob,
            market_type="general_binary",
            asset=None,
            features=features,
            action=side,
            signal_source="opportunity_factory,proven_wallet",
        )
        return True

    def _gamma_market(self, market_id: str) -> Optional[dict]:
        if not market_id:
            return None
        params = urllib.parse.urlencode({"id": market_id})
        try:
            req = urllib.request.Request(
                f"{GAMMA_MARKETS_URL}?{params}",
                headers={"User-Agent": "poly1-opportunity-factory/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if isinstance(data, list) and data:
                return data[0]
        except Exception as exc:
            logger.debug("opportunity_factory: gamma fetch failed %s: %s", market_id, exc)
        return None

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _touch(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def _family_for_candidate(candidate: dict) -> str:
    horizon = str(candidate.get("horizon") or "").lower()
    route = str(candidate.get("route_agent") or "").lower()
    question = str(candidate.get("question") or "").lower()
    if "vwap" in question or "mean" in question:
        return "vwap_mean_reversion"
    if route == "scalper" or horizon in {"5m", "15m"}:
        return "trend_momentum"
    return "other"


def _read_json(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("opportunity_factory: failed reading %s: %s", path, exc)
    return {}


def _json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw or "[]")
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def main() -> int:
    parser = argparse.ArgumentParser(description="Build executable/attention opportunities from strong indicators")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    cfg = OpportunityFactoryConfig.from_env()
    if args.db:
        cfg = OpportunityFactoryConfig(**{**cfg.__dict__, "db_path": args.db})
    print(json.dumps(OpportunityFactory(cfg=cfg).run_once(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
