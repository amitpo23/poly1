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
