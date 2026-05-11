"""Shared position exit executor.

This module is deliberately small: it turns an exit decision into a FAK SELL
attempt and reports whether the position is actually flat. It never marks a
position closed unless the CLOB response says the sell matched/filled.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)


try:
    from py_clob_client_v2.clob_types import OrderType as _ClobOrderType
    _FAK_TYPE = _ClobOrderType.FAK
except Exception:
    _FAK_TYPE = "FAK"


MATCHED_STATUSES = {"matched", "filled"}


@dataclass(frozen=True)
class ExitOrderResult:
    closed: bool
    status: str
    response: Optional[dict]
    error: Optional[str] = None


class ExitExecutor:
    """Execute exits with FAK SELL orders."""

    def __init__(self, polymarket, sell_slippage: float = 0.02):
        self.polymarket = polymarket
        self.sell_slippage = sell_slippage

    def limit_price_from_mid(self, mid: float) -> float:
        return max(0.01, float(mid) * (1.0 - self.sell_slippage))

    def sell_fak(
        self,
        *,
        token_id: str,
        shares: float,
        mid: float,
    ) -> ExitOrderResult:
        if shares <= 0:
            return ExitOrderResult(
                closed=False,
                status="invalid",
                response=None,
                error="shares must be > 0",
            )
        limit_price = self.limit_price_from_mid(mid)
        try:
            response = self.polymarket.sell_shares(
                token_id=token_id,
                shares=shares,
                limit_price=limit_price,
                order_type=_FAK_TYPE,
            )
        except Exception as exc:
            logger.exception("exit sell_fak raised for token=%s", str(token_id)[:18])
            return ExitOrderResult(
                closed=False,
                status="exception",
                response=None,
                error=f"{type(exc).__name__}: {exc}",
            )

        status = "unknown"
        if isinstance(response, dict):
            status = str(response.get("status") or response.get("orderStatus") or "unknown").lower()
        closed = status in MATCHED_STATUSES
        return ExitOrderResult(
            closed=closed,
            status=status,
            response=response if isinstance(response, dict) else {"raw": response},
        )
