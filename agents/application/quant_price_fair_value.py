"""Digital fair-value signal for price-threshold prediction markets.

This module intentionally implements the useful subset of the Heston-style
idea for the current bot: price threshold markets need a calibrated probability,
not another rank score.  It uses a lognormal digital approximation with a
Bayesian-style volatility shrinkage.  If the question, target, current price, or
time horizon is missing, it returns neutral and never blocks or trades.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Optional


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class QuantPriceFairValueSignal:
    direction: Optional[str]       # "yes" | "no" | None
    probability: float             # calibrated YES probability when known
    confidence: float
    asset: Optional[str]
    target_price: Optional[float]
    current_price: Optional[float]
    edge: float
    model: str
    reason: str
    features: dict = field(default_factory=dict)


class QuantPriceFairValueReader:
    """Compute a calibrated probability for price-threshold markets."""

    ASSET_ALIASES = {
        "BTC": ("bitcoin", "btc"),
        "ETH": ("ethereum", "eth"),
        "SOL": ("solana", "sol"),
        "XRP": ("xrp",),
        "DOGE": ("dogecoin", "doge"),
        "BNB": ("bnb",),
    }

    _ABOVE = (
        "above", "over", "greater", "higher", "exceed", "exceeds",
        "at least", "reach", "reaches", "cross", "crosses", "hit", "hits",
        "מעל", "יותר", "יחצה", "יעבור", "יגיע",
    )
    _BELOW = (
        "below", "under", "less", "lower", "beneath", "drop below",
        "falls below", "מתחת", "פחות", "ירד",
    )

    def query(
        self,
        *,
        question: str,
        hours_to_close: Optional[float],
        market_price: Optional[float],
        tape_features: Optional[dict] = None,
    ) -> QuantPriceFairValueSignal:
        if not _env_bool("META_BRAIN_QUANT_FV_ENABLED", True):
            return self._neutral("quant_fv: disabled")

        text = str(question or "")
        asset = self._infer_asset(text)
        if asset is None:
            return self._neutral("quant_fv: no supported asset")

        target = self._extract_target_price(text)
        if target is None or target <= 0:
            return self._neutral("quant_fv: no price target", asset=asset)

        current = self._extract_current_price(tape_features)
        if current is None or current <= 0:
            return self._neutral("quant_fv: no current price", asset=asset, target=target)

        try:
            hours = float(hours_to_close) if hours_to_close is not None else 0.0
        except (TypeError, ValueError):
            hours = 0.0
        min_hours = _env_float("QUANT_FV_MIN_HOURS_TO_CLOSE", 0.01)
        max_hours = _env_float("QUANT_FV_MAX_HOURS_TO_CLOSE", 168.0)
        if hours < min_hours or hours > max_hours:
            return self._neutral(
                f"quant_fv: horizon_out_of_range:{hours:.3f}h",
                asset=asset,
                target=target,
                current=current,
            )

        yes_is_above = self._yes_means_above(text)
        if yes_is_above is None:
            return self._neutral(
                "quant_fv: ambiguous price direction",
                asset=asset,
                target=target,
                current=current,
            )

        annual_vol = self._posterior_annual_vol(asset, tape_features)
        yes_probability = self.digital_probability(
            spot=current,
            strike=target,
            hours_to_expiry=hours,
            annual_vol=annual_vol,
            yes_is_above=yes_is_above,
        )
        try:
            mkt = float(market_price) if market_price is not None else 0.5
        except (TypeError, ValueError):
            mkt = 0.5
        mkt = max(0.0, min(1.0, mkt))
        edge = yes_probability - mkt
        min_abs_edge = _env_float("QUANT_FV_MIN_ABS_EDGE", 0.02)
        min_confidence = _env_float("QUANT_FV_MIN_CONFIDENCE", 0.55)
        confidence = min(0.90, 0.50 + abs(edge) * 1.8)
        direction = "yes" if edge >= min_abs_edge else "no" if edge <= -min_abs_edge else None
        if confidence < min_confidence:
            direction = None

        return QuantPriceFairValueSignal(
            direction=direction,
            probability=round(yes_probability, 4),
            confidence=round(confidence if direction else min(confidence, 0.52), 4),
            asset=asset,
            target_price=round(target, 6),
            current_price=round(current, 6),
            edge=round(edge, 6),
            model="lognormal_digital_bayesian_vol",
            reason=(
                f"quant_fv {asset}: spot={current:.4f}, strike={target:.4f}, "
                f"T={hours:.3f}h, vol={annual_vol:.3f}, yes_prob={yes_probability:.3f}, "
                f"edge={edge:+.3f}"
            ),
            features={
                "quant_fv_yes_is_above": yes_is_above,
                "quant_fv_hours_to_close": round(hours, 6),
                "quant_fv_annual_vol": round(annual_vol, 6),
                "quant_fv_market_price": round(mkt, 6),
                "quant_fv_min_abs_edge": min_abs_edge,
                "quant_fv_min_confidence": min_confidence,
            },
        )

    @classmethod
    def digital_probability(
        cls,
        *,
        spot: float,
        strike: float,
        hours_to_expiry: float,
        annual_vol: float,
        yes_is_above: bool,
    ) -> float:
        s = max(float(spot), 1e-9)
        k = max(float(strike), 1e-9)
        sigma = max(float(annual_vol), 1e-6)
        t = max(float(hours_to_expiry), 1e-9) / (365.0 * 24.0)
        d2 = (math.log(s / k) - 0.5 * sigma * sigma * t) / (sigma * math.sqrt(t))
        p_above = cls._normal_cdf(d2)
        return max(0.0, min(1.0, p_above if yes_is_above else 1.0 - p_above))

    @staticmethod
    def _normal_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(float(x) / math.sqrt(2.0)))

    def _posterior_annual_vol(self, asset: str, tape_features: Optional[dict]) -> float:
        prior = _env_float(f"QUANT_FV_DEFAULT_ANNUAL_VOL_{asset}", 0.60)
        prior = _env_float("QUANT_FV_DEFAULT_ANNUAL_VOL", prior)
        realized = None
        if isinstance(tape_features, dict):
            for key in (
                "realized_volatility_annual",
                "annualized_volatility",
                "annual_vol",
            ):
                value = tape_features.get(key)
                if value not in (None, ""):
                    try:
                        realized = float(value)
                        break
                    except (TypeError, ValueError):
                        pass
        if realized is None or realized <= 0:
            realized = prior
        weight = max(0.0, min(1.0, _env_float("QUANT_FV_VOL_PRIOR_WEIGHT", 0.35)))
        posterior_var = weight * prior * prior + (1.0 - weight) * realized * realized
        return max(0.05, min(3.0, math.sqrt(max(posterior_var, 1e-9))))

    def _infer_asset(self, question: str) -> Optional[str]:
        text = question.lower()
        for asset, aliases in self.ASSET_ALIASES.items():
            if any(alias in text for alias in aliases):
                return asset
        return None

    def _extract_target_price(self, question: str) -> Optional[float]:
        text = question.replace(",", "")
        matches = re.findall(r"(\$?)\b(\d+(?:\.\d+)?)([kKmM]?)\b", text)
        candidates: list[float] = []
        for dollar, raw, suffix in matches:
            try:
                value = float(raw)
            except ValueError:
                continue
            if suffix.lower() == "k":
                value *= 1_000.0
            elif suffix.lower() == "m":
                value *= 1_000_000.0
            # Tiny plain numbers are usually horizons or percentages.  Keep them
            # only when the question explicitly marks them as prices.
            if value >= 10.0 or dollar or suffix:
                candidates.append(value)
        return max(candidates) if candidates else None

    def _extract_current_price(self, tape_features: Optional[dict]) -> Optional[float]:
        if not isinstance(tape_features, dict):
            return None
        for key in ("last_price", "mid", "current_price"):
            value = tape_features.get(key)
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price
        return None

    def _yes_means_above(self, question: str) -> Optional[bool]:
        text = question.lower()
        above = any(term in text for term in self._ABOVE)
        below = any(term in text for term in self._BELOW)
        if above and not below:
            return True
        if below and not above:
            return False
        return None

    def _neutral(
        self,
        reason: str,
        *,
        asset: Optional[str] = None,
        target: Optional[float] = None,
        current: Optional[float] = None,
    ) -> QuantPriceFairValueSignal:
        return QuantPriceFairValueSignal(
            None,
            0.5,
            0.0,
            asset,
            target,
            current,
            0.0,
            "none",
            reason,
            {},
        )


def quant_fv_enabled_for_metabrain() -> bool:
    return _env_bool("META_BRAIN_QUANT_FV_ENABLED", True)
