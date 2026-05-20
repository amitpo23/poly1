"""Process-wide live order placement lock.

All live entry agents eventually call the Polymarket adapter.  A short file
lock here makes the "check liquidity → submit order → normalize response"
section single-file across containers that share `/app/data`.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from pathlib import Path


@contextlib.contextmanager
def live_order_lock(path: str | None = None):
    lock_path = Path(path or os.getenv("LIVE_ORDER_LOCK_PATH", "/app/data/live_order.lock"))
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Local unit tests may run without a writable /app.  Production
        # containers mount /app/data; fallback keeps non-container tests usable.
        lock_path = Path(tempfile.gettempdir()) / "poly1-live_order.lock"
    with lock_path.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
