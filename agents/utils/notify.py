import logging
import os
import threading
from typing import Optional

import requests


logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0


def _post_telegram(token: str, chat_id: str, text: str, timeout: float) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "text": text[:4000]},
            timeout=timeout,
        )
        if not resp.ok:
            logger.warning(
                "telegram send failed: %s %s", resp.status_code, resp.text[:200]
            )
    except requests.RequestException as e:
        logger.warning("telegram request error: %s", e)


def notify_telegram(
    message: str, timeout: float = DEFAULT_TIMEOUT, blocking: bool = False
) -> bool:
    """Fire-and-forget Telegram notification. Runs on a daemon thread so the
    main loop is never blocked. Set `blocking=True` only in tests."""
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    if not token or not chat_id:
        return False

    if blocking:
        _post_telegram(token, chat_id, message, timeout)
        return True

    threading.Thread(
        target=_post_telegram,
        args=(token, chat_id, message, timeout),
        daemon=True,
    ).start()
    return True


def notify_trade(
    *,
    event: str,
    agent: str = "poly1",
    market_id: str = "",
    side: str = "",
    price: Optional[float] = None,
    size_usdc: Optional[float] = None,
    pnl_usdc: Optional[float] = None,
    reason: str = "",
    balance_usdc: Optional[float] = None,
) -> bool:
    """Send a structured trade notification to Telegram.

    Callers provide whatever fields they have; missing fields are omitted.
    Balance is shown when provided — callers with access to polymarket or
    risk_gate should read and pass it.
    """
    icon = {
        "fill": "\U0001f7e2",       # green circle
        "close_tp": "\U0001f4b0",   # money bag
        "close_sl": "\U0001f534",   # red circle
        "close_timeout": "\u23f0",  # alarm clock
        "close_dust": "\U0001fab6", # feather (dust)
        "error": "\u26a0\ufe0f",    # warning
        "cycle": "\U0001f504",      # arrows cycle
    }.get(event, "\u2139\ufe0f")    # info

    lines = [f"{icon} {agent}: {event}"]
    if market_id:
        label = market_id[:20] if len(market_id) > 20 else market_id
        lines.append(f"  Market: {label}")
    if side:
        lines.append(f"  Side: {side}")
    if price is not None:
        lines.append(f"  Price: ${price:.4f}")
    if size_usdc is not None:
        lines.append(f"  Size: ${size_usdc:.2f}")
    if pnl_usdc is not None:
        pnl_icon = "\u2705" if pnl_usdc >= 0 else "\u274c"
        lines.append(f"  PnL: {pnl_icon} ${pnl_usdc:+.4f}")
    if reason:
        lines.append(f"  Reason: {reason}")
    if balance_usdc is not None:
        lines.append(f"  \U0001f4b5 Balance: ${balance_usdc:.2f}")

    return notify_telegram("\n".join(lines))


def _safe_balance(polymarket) -> Optional[float]:
    """Try to read USDC balance; return None on any failure."""
    if polymarket is None:
        return None
    try:
        return polymarket.get_usdc_balance()
    except Exception:
        logger.debug("_safe_balance failed (non-fatal)")
        return None


def ping_healthcheck(url: Optional[str] = None, timeout: float = 5.0) -> bool:
    target = url or os.getenv("HEALTHCHECK_URL")
    if not target:
        return False
    try:
        resp = requests.get(target, timeout=timeout)
        return resp.ok
    except requests.RequestException as e:
        logger.warning("healthcheck ping error: %s", e)
        return False
