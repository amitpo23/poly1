import logging
import os
import threading
import time
from typing import Optional

import requests


logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


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
    message: str,
    timeout: float = DEFAULT_TIMEOUT,
    blocking: bool = False,
    force: bool = False,
) -> bool:
    """Fire-and-forget Telegram notification. Runs on a daemon thread so the
    main loop is never blocked. Set `blocking=True` only in tests."""
    if not force and not _env_bool("TELEGRAM_DIRECT_NOTIFICATIONS", False):
        logger.info("telegram direct notification suppressed by policy")
        return False
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    if not token or not chat_id:
        logger.warning("telegram credentials missing: TG_BOT_TOKEN/TG_CHAT_ID")
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
    if not _trade_alert_allowed(event):
        logger.info("telegram trade notification suppressed event=%s agent=%s", event, agent)
        return False

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

    return notify_telegram("\n".join(lines), force=True)


def _trade_alert_allowed(event: str) -> bool:
    """Return True for immediate Telegram alerts allowed outside the hourly report.

    Default policy is intentionally quiet: hourly dashboard only, plus
    rate-limited critical/error alerts. Operators can opt into fill/close spam
    with TELEGRAM_TRADE_ALERTS=true.
    """
    event = (event or "").strip().lower()
    if _env_bool("TELEGRAM_TRADE_ALERTS", False):
        return True
    if event not in {"error", "critical", "halt"}:
        return False
    min_interval = _env_int("TELEGRAM_CRITICAL_MIN_INTERVAL_SEC", 900)
    state_path = os.getenv(
        "TELEGRAM_NOTIFY_STATE_PATH",
        "/app/data/telegram_notify_state.json",
    )
    try:
        import json
        from pathlib import Path

        path = Path(state_path)
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text() or "{}")
            except Exception:
                data = {}
        now = time.time()
        key = f"last_{event}"
        last = float(data.get(key) or 0)
        if now - last < min_interval:
            return False
        data[key] = now
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, sort_keys=True))
    except Exception:
        logger.debug("telegram critical rate-limit state failed", exc_info=True)
    return True


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
