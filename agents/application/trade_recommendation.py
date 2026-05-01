import json
import re

from agents.utils.objects import TradeRecommendation


def parse_trade_recommendation(best_trade: str) -> TradeRecommendation:
    parsed = _parse_trade_json(best_trade) or _parse_trade_fields(best_trade)
    if parsed is None:
        raise ValueError(f"Could not parse trade recommendation: {best_trade}")

    price = float(parsed["price"])
    size_fraction = float(parsed["size_fraction"])
    side = str(parsed["side"]).upper()
    confidence = parsed.get("confidence")
    confidence = float(confidence) if confidence is not None else None

    if not 0 <= price <= 1:
        raise ValueError(f"Trade price must be between 0 and 1. Got {price}.")
    if not 0 < size_fraction <= 1:
        raise ValueError(
            f"Trade size_fraction must be greater than 0 and at most 1. Got {size_fraction}."
        )
    if side not in {"BUY", "SELL"}:
        raise ValueError(f"Trade side must be BUY or SELL. Got {side}.")
    if confidence is not None and not 0 <= confidence <= 1:
        raise ValueError(f"Trade confidence must be between 0 and 1. Got {confidence}.")

    return TradeRecommendation(
        price=price,
        size_fraction=size_fraction,
        side=side,
        confidence=confidence,
        raw_response=best_trade,
    )


def _parse_trade_json(best_trade: str):
    json_match = re.search(r"\{.*\}", best_trade, re.DOTALL)
    if not json_match:
        return None

    try:
        data = json.loads(json_match.group(0))
    except json.JSONDecodeError:
        return None

    if "size" in data and "size_fraction" not in data:
        data["size_fraction"] = data["size"]

    required_keys = {"price", "size_fraction", "side"}
    if not required_keys.issubset(data):
        return None

    return data


def _parse_trade_fields(best_trade: str):
    def find_number(key: str):
        match = re.search(
            rf"{key}\s*[:=]\s*['\"]?([0-9]*\.?[0-9]+)",
            best_trade,
            flags=re.IGNORECASE,
        )
        return match.group(1) if match else None

    price = find_number("price")
    size_fraction = find_number("size_fraction") or find_number("size")
    confidence = find_number("confidence")
    side_match = re.search(
        r"side\s*[:=]\s*['\"]?(BUY|SELL)",
        best_trade,
        flags=re.IGNORECASE,
    )

    if not price or not size_fraction or not side_match:
        return None

    return {
        "price": price,
        "size_fraction": size_fraction,
        "side": side_match.group(1),
        "confidence": confidence,
    }
