import logging
from datetime import datetime, timezone
from typing import Optional

from agents.application.trade_log import TradeLog


logger = logging.getLogger(__name__)


class ScalperState:
    TRACKING = "tracking"
    LEG1_FILLED = "leg1_filled"
    BOTH_FILLED = "both_filled"
    EXPIRED = "expired"
    REDEEMED = "redeemed"
    EXITED = "exited"
    SHADOW = "shadow"
    RECONCILE_NEEDED = "reconcile_needed"


# Not in TERMINAL_STATES intentionally: RECONCILE_NEEDED pairs remain in list_open()
# until an operator verifies on-chain positions and manually clears the row.
TERMINAL_STATES = (
    ScalperState.EXPIRED,
    ScalperState.REDEEMED,
    ScalperState.EXITED,
    ScalperState.SHADOW,
)


def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


class ScalperPairsDAO:
    """Persistence for scalper pair state. Stored alongside trades.db."""

    def __init__(self, trade_log: TradeLog):
        self._tl = trade_log

    def create(
        self, slug: str, period_ts: int, up_token: str, down_token: str
    ) -> None:
        with self._tl._lock, self._tl._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO scalper_pairs "
                "(slug, period_ts, up_token, down_token, state, opened_ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (slug, period_ts, up_token, down_token,
                 ScalperState.TRACKING, _now_ts()),
            )

    def get_by_slug(self, slug: str) -> Optional[dict]:
        with self._tl._lock, self._tl._connect() as conn:
            row = conn.execute(
                "SELECT * FROM scalper_pairs WHERE slug = ?", (slug,)
            ).fetchone()
            return dict(row) if row else None

    def list_open(self) -> list:
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        sql = f"SELECT * FROM scalper_pairs WHERE state NOT IN ({placeholders})"
        with self._tl._lock, self._tl._connect() as conn:
            return [dict(r) for r in conn.execute(sql, TERMINAL_STATES).fetchall()]

    def record_fill(
        self, slug: str, side: str, qty: float, cost_usdc: float, fill_price: Optional[float] = None
    ) -> None:
        if side not in ("up", "down"):
            raise ValueError(f"side must be 'up' or 'down', got {side}")
        col_qty = f"qty_{side}"
        col_cost = f"cost_{side}"
        col_attempts = f"attempts_{side}"
        col_last = f"last_price_{side}"
        sql = (
            f"UPDATE scalper_pairs SET "
            f"  {col_qty} = {col_qty} + ?, "
            f"  {col_cost} = {col_cost} + ?, "
            f"  {col_attempts} = {col_attempts} + 1, "
            f"  {col_last} = COALESCE(?, {col_last}) "
            f"WHERE slug = ?"
        )
        with self._tl._lock, self._tl._connect() as conn:
            cur = conn.execute(sql, (qty, cost_usdc, fill_price, slug))
            if cur.rowcount == 0:
                raise ValueError(f"record_fill: slug '{slug}' not found")

    def set_state(self, slug: str, state: str, error: Optional[str] = None) -> None:
        closed_ts = _now_ts() if state in TERMINAL_STATES else None
        with self._tl._lock, self._tl._connect() as conn:
            cur = conn.execute(
                "UPDATE scalper_pairs SET state = ?, error = ?, "
                "closed_ts = COALESCE(?, closed_ts) WHERE slug = ?",
                (state, error, closed_ts, slug),
            )
            if cur.rowcount == 0:
                raise ValueError(f"set_state: slug '{slug}' not found")

    def list_recent(self, limit: int = 50) -> list:
        with self._tl._lock, self._tl._connect() as conn:
            return [
                dict(r) for r in conn.execute(
                    "SELECT * FROM scalper_pairs ORDER BY opened_ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]
