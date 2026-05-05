# Scalper Strategy C — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a TS-style hedged-arb scalper for 15-min Polymarket crypto Up/Down markets as an independent module that runs alongside the existing LLM-directional Trader, isolated by capital reserve and a separate process.

**Architecture:** New `ScalperEngine` polls gamma for `*-updown-15m-*` slugs, tracks per-side running lows on the orderbook, fires FAK market BUYs on reversal/depth triggers, holds to expiry. Persists pair state in a new `scalper_pairs` table. Each FAK fill also writes a `SCALPER_LEG` row to the existing `trades` table for audit/PnL. A new `ScalperDaemon` process runs separately from `TraderDaemon`. A hard `SCALPER_RESERVE_USDC` reserve isolates capital.

**Tech Stack:** Python 3.10+, `py_clob_client` (existing), `sqlite3` with WAL, `requests`. No LLM. No new dependencies.

**Decision log:**
- **FAK only, no LIMIT primitives.** TS-style scalper needs only MARKET BUY (FAK). LIMIT-BUY/SELL/cancel are deferred until concrete need (e.g., take-profit). YAGNI per CLAUDE.md.
- **Source license:** PoDev TS repo used as algorithmic reference only; this is a clean re-implementation. Personal use confirmed.
- **Operational sequencing (NOT in this plan, runtime decisions):** Stage 0 (shadow, 2-3 days) → Stage 1 (live, **min 2 weeks**, abort if PnL < −$15) → Stage 2 (scale).

**Out of scope for this plan:**
- LIMIT order primitives (separate plan if/when needed)
- Take-profit / stop-loss for Trader
- Rust-style hedge ladder
- Kalshi cross-exchange arb (separate plan)
- Auto-redemption of resolved positions (existing scripts already handle redeem)

---

## File Structure

**New files:**
| Path | Responsibility |
|------|----------------|
| `agents/application/scalper.py` | `ScalperEngine`, `ScalpPair`, `ScalperConfig`, `ScalperDaemon`, `__main__` entry |
| `agents/application/scalper_pairs.py` | `ScalperPairsDAO` — CRUD for `scalper_pairs` table |
| `tests/test_scalper.py` | Pure-logic tests for entry/profit-gate/state transitions |
| `tests/test_scalper_pairs.py` | DAO tests (in-memory SQLite) |
| `tests/test_scalper_engine.py` | Engine integration tests with mocked CLOB |
| `scripts/python/scalper_inspect.py` | CLI: list recent pairs, PnL summary |

**Modified files:**
| Path | Change |
|------|--------|
| `agents/polymarket/polymarket.py` | Add `OrderType.FAK` parameter to `execute_market_order` |
| `agents/application/trade_log.py` | Add `WAL` pragma, `SCALPER_LEG` status, `scalper_pairs` table to schema |
| `agents/application/risk_gate.py` | Add `SCALPER_RESERVE_USDC` env, `available_for_trader()` method |
| `.env.example` | New scalper env vars |
| `SPEC.md` | New §15 documenting scalper module |
| `CLAUDE.md` | Update §"What's intentionally NOT in scope" — short-duration scalper now in scope |
| `docker-compose.yml` | New `scalper` service |

---

## Task 1: Schema migration — `scalper_pairs` table, `SCALPER_LEG` status, WAL mode

**Files:**
- Modify: `agents/application/trade_log.py`
- Modify: `tests/test_trader.py` (add to existing `TestTradeLog` class)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_trader.py` inside `class TestTradeLog`:

```python
def test_scalper_pairs_table_exists(self):
    log = TradeLog(db_path=":memory:")
    with log._connect() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scalper_pairs'"
        ).fetchone()
    self.assertIsNotNone(row, "scalper_pairs table must be created on init")

def test_wal_mode_enabled(self):
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        log = TradeLog(db_path=path)
        with log._connect() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")
    finally:
        os.unlink(path)

def test_scalper_leg_status_constant(self):
    from agents.application.trade_log import SCALPER_LEG, ACTIVE_STATUSES
    self.assertEqual(SCALPER_LEG, "scalper_leg")
    # Must NOT be in ACTIVE_STATUSES — scalper has its own dedupe
    self.assertNotIn(SCALPER_LEG, ACTIVE_STATUSES)
```

- [ ] **Step 2: Run tests to verify failure**

Run:
```bash
python3 -m unittest tests.test_trader.TestTradeLog.test_scalper_pairs_table_exists tests.test_trader.TestTradeLog.test_wal_mode_enabled tests.test_trader.TestTradeLog.test_scalper_leg_status_constant -v
```
Expected: all 3 FAIL with `no such table: scalper_pairs`, journal_mode mismatch (likely "memory" or "delete"), and `ImportError: cannot import name 'SCALPER_LEG'`.

- [ ] **Step 3: Add SCALPER_LEG constant and update SCHEMA**

In `agents/application/trade_log.py`, locate the status enum block (around line 36) and add:

```python
SCALPER_LEG = "scalper_leg"
```

after the existing `SKIPPED_DRY_RUN = "skipped_dry_run"` line. Do **not** add `SCALPER_LEG` to `TIME_BOUNDED_ACTIVE_STATUSES` or `ACTIVE_STATUSES` — scalper dedupes via its own table.

Then locate the `SCHEMA` constant (line 16) and replace it with:

```python
SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    cycle_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT,
    side TEXT,
    price REAL,
    size_usdc REAL,
    confidence REAL,
    status TEXT NOT NULL,
    response_json TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_status_ts ON trades(market_id, status, ts);
CREATE INDEX IF NOT EXISTS idx_status_ts ON trades(status, ts);

CREATE TABLE IF NOT EXISTS scalper_pairs (
    slug TEXT PRIMARY KEY,
    period_ts INTEGER NOT NULL,
    up_token TEXT NOT NULL,
    down_token TEXT NOT NULL,
    qty_up REAL NOT NULL DEFAULT 0,
    qty_down REAL NOT NULL DEFAULT 0,
    cost_up REAL NOT NULL DEFAULT 0,
    cost_down REAL NOT NULL DEFAULT 0,
    attempts_up INTEGER NOT NULL DEFAULT 0,
    attempts_down INTEGER NOT NULL DEFAULT 0,
    last_price_up REAL,
    last_price_down REAL,
    state TEXT NOT NULL,
    opened_ts INTEGER NOT NULL,
    closed_ts INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_scalper_state ON scalper_pairs(state);
CREATE INDEX IF NOT EXISTS idx_scalper_period ON scalper_pairs(period_ts);
"""
```

- [ ] **Step 4: Enable WAL mode in `_connect`**

Locate the `_connect` method (line 67) and modify it:

```python
@contextmanager
def _connect(self):
    conn = sqlite3.connect(self.db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        yield conn
        conn.commit()
    finally:
        conn.close()
```

Note: `:memory:` databases ignore WAL silently — the test for WAL uses a file path.

- [ ] **Step 5: Run tests to verify pass**

Run:
```bash
python3 -m unittest tests.test_trader.TestTradeLog -v
```
Expected: all `TestTradeLog` tests PASS, including the 3 new ones and any existing.

- [ ] **Step 6: Commit**

```bash
git add agents/application/trade_log.py tests/test_trader.py
git commit -m "feat(scalper): add scalper_pairs table + SCALPER_LEG status + WAL mode

Foundation for the scalper module. New scalper_pairs table tracks pair
state independently of the trades ledger. SCALPER_LEG is added to the
status enum but deliberately NOT in ACTIVE_STATUSES — scalper dedupes
via scalper_pairs.state. WAL mode enables safe two-process access from
TraderDaemon and the upcoming ScalperDaemon."
```

---

## Task 2: `ScalperPairsDAO` — CRUD for `scalper_pairs`

**Files:**
- Create: `agents/application/scalper_pairs.py`
- Create: `tests/test_scalper_pairs.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_scalper_pairs.py`:

```python
import os
import tempfile
import unittest

from agents.application.trade_log import TradeLog
from agents.application.scalper_pairs import ScalperPairsDAO, ScalperState


class TestScalperPairsDAO(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = TradeLog(db_path=self.tmp.name)
        self.dao = ScalperPairsDAO(self.log)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_create_pair_inserts_tracking_row(self):
        self.dao.create("btc-updown-15m-100", 100, "tok_up", "tok_dn")
        row = self.dao.get_by_slug("btc-updown-15m-100")
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], ScalperState.TRACKING)
        self.assertEqual(row["qty_up"], 0.0)
        self.assertEqual(row["attempts_up"], 0)

    def test_create_is_idempotent(self):
        self.dao.create("s1", 1, "u", "d")
        self.dao.create("s1", 1, "u", "d")  # second call no-ops
        rows = self.dao.list_open()
        self.assertEqual(len(rows), 1)

    def test_record_fill_updates_qty_and_cost(self):
        self.dao.create("s1", 1, "u", "d")
        self.dao.record_fill("s1", "up", qty=5.0, cost_usdc=2.50)
        row = self.dao.get_by_slug("s1")
        self.assertEqual(row["qty_up"], 5.0)
        self.assertEqual(row["cost_up"], 2.50)
        self.assertEqual(row["attempts_up"], 1)

    def test_set_state_transitions(self):
        self.dao.create("s1", 1, "u", "d")
        self.dao.set_state("s1", ScalperState.LEG1_FILLED)
        self.assertEqual(self.dao.get_by_slug("s1")["state"], "leg1_filled")
        self.dao.set_state("s1", ScalperState.BOTH_FILLED)
        self.assertEqual(self.dao.get_by_slug("s1")["state"], "both_filled")

    def test_list_open_excludes_terminal_states(self):
        self.dao.create("s1", 1, "u", "d")
        self.dao.create("s2", 1, "u", "d")
        self.dao.set_state("s2", ScalperState.REDEEMED)
        open_rows = self.dao.list_open()
        slugs = [r["slug"] for r in open_rows]
        self.assertIn("s1", slugs)
        self.assertNotIn("s2", slugs)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify failure**

Run:
```bash
python3 -m unittest tests.test_scalper_pairs -v
```
Expected: ImportError — `agents.application.scalper_pairs` does not exist.

- [ ] **Step 3: Implement DAO**

Create `agents/application/scalper_pairs.py`:

```python
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
    SHADOW = "shadow"
    RECONCILE_NEEDED = "reconcile_needed"  # restart found leg1_filled — verify positions


TERMINAL_STATES = (ScalperState.EXPIRED, ScalperState.REDEEMED, ScalperState.SHADOW)


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

    def list_open(self) -> list[dict]:
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        sql = f"SELECT * FROM scalper_pairs WHERE state NOT IN ({placeholders})"
        with self._tl._lock, self._tl._connect() as conn:
            return [dict(r) for r in conn.execute(sql, TERMINAL_STATES).fetchall()]

    def record_fill(
        self, slug: str, side: str, qty: float, cost_usdc: float, fill_price: float = None
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
            conn.execute(sql, (qty, cost_usdc, fill_price, slug))

    def set_state(self, slug: str, state: str, error: Optional[str] = None) -> None:
        closed_ts = _now_ts() if state in TERMINAL_STATES else None
        with self._tl._lock, self._tl._connect() as conn:
            conn.execute(
                "UPDATE scalper_pairs SET state = ?, error = ?, "
                "closed_ts = COALESCE(?, closed_ts) WHERE slug = ?",
                (state, error, closed_ts, slug),
            )

    def list_recent(self, limit: int = 50) -> list[dict]:
        with self._tl._lock, self._tl._connect() as conn:
            return [
                dict(r) for r in conn.execute(
                    "SELECT * FROM scalper_pairs ORDER BY opened_ts DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            ]
```

- [ ] **Step 4: Run tests to verify pass**

Run:
```bash
python3 -m unittest tests.test_scalper_pairs -v
```
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/application/scalper_pairs.py tests/test_scalper_pairs.py
git commit -m "feat(scalper): add ScalperPairsDAO with state machine

Persistence layer for pair state. State transitions: tracking →
leg1_filled → both_filled → expired → redeemed. SHADOW for
EXECUTE_SCALPER=false runs. RECONCILE_NEEDED set on restart when a
leg1_filled row is found, requiring on-chain position verification."
```

---

## Task 3: `ScalpPair` tracking object — running-low per side

**Files:**
- Create initial skeleton: `agents/application/scalper.py`
- Create: `tests/test_scalper.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_scalper.py`:

```python
import unittest

from agents.application.scalper import ScalpPair, ScalperConfig


class TestScalpPair(unittest.TestCase):
    def setUp(self):
        self.cfg = ScalperConfig()  # all defaults
        self.pair = ScalpPair(slug="btc-updown-15m-100", period_ts=100,
                               up_token="u", down_token="d", cfg=self.cfg)

    def test_ineligible_when_ask_above_threshold(self):
        # threshold = 0.499, ask = 0.55 → ineligible
        self.pair.apply_tick("up", best_ask=0.55, now_ms=1000)
        self.assertIsNone(self.pair.temp_price_up)

    def test_temp_price_tracks_running_low(self):
        self.pair.apply_tick("up", best_ask=0.49, now_ms=1000)
        self.assertEqual(self.pair.temp_price_up, 0.49)
        self.pair.apply_tick("up", best_ask=0.47, now_ms=1100)
        self.assertEqual(self.pair.temp_price_up, 0.47)
        self.pair.apply_tick("up", best_ask=0.48, now_ms=1200)
        self.assertEqual(self.pair.temp_price_up, 0.47, "running low must not increase")

    def test_temp_price_resets_on_ineligibility(self):
        self.pair.apply_tick("up", best_ask=0.45, now_ms=1000)
        self.assertEqual(self.pair.temp_price_up, 0.45)
        self.pair.apply_tick("up", best_ask=0.51, now_ms=1100)  # > threshold
        self.assertIsNone(self.pair.temp_price_up,
                          "becoming ineligible must reset tracker")

    def test_per_side_independence(self):
        self.pair.apply_tick("up", best_ask=0.45, now_ms=1000)
        self.pair.apply_tick("down", best_ask=0.49, now_ms=1000)
        self.assertEqual(self.pair.temp_price_up, 0.45)
        self.assertEqual(self.pair.temp_price_down, 0.49)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify failure**

Run:
```bash
python3 -m unittest tests.test_scalper -v
```
Expected: ImportError — `agents.application.scalper` does not exist.

- [ ] **Step 3: Create scalper.py skeleton with `ScalpPair` and `ScalperConfig`**

Create `agents/application/scalper.py`:

```python
"""Strategy C — short-duration crypto Up/Down scalper.

Independent module that runs in its own process alongside the LLM Trader.
See docs/STRATEGY_C_SCALPING_SPEC.md for algorithm reference.
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Optional


logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class ScalperConfig:
    threshold: float = 0.499
    reversal_delta: float = 0.020
    depth_buy_discount: float = 0.05  # 5% below tempPrice
    second_side_buffer: float = 0.01
    second_side_time_ms: int = 200
    dynamic_threshold_boost: float = 0.04
    max_sum_avg: float = 0.98          # profit gate; tighten to 0.97 in Stage 1
    max_buys_per_side: int = 4
    shares_per_side: float = 5.0       # approximate; in Stage 0 use $-cap below
    leg_usdc_cap: float = 5.0
    poll_ms: int = 250
    discover_every_sec: int = 60       # re-scan gamma every N seconds

    @classmethod
    def from_env(cls) -> "ScalperConfig":
        return cls(
            threshold=_env_float("SCALP_ENTRY_THRESHOLD", 0.499),
            reversal_delta=_env_float("SCALP_REVERSAL_DELTA", 0.020),
            depth_buy_discount=_env_float("SCALP_DEPTH_DISCOUNT", 0.05),
            second_side_buffer=_env_float("SCALP_SECOND_BUFFER", 0.01),
            second_side_time_ms=_env_int("SCALP_SECOND_TIME_MS", 200),
            dynamic_threshold_boost=_env_float("SCALP_DYNAMIC_BOOST", 0.04),
            max_sum_avg=_env_float("SCALP_MAX_SUM_AVG", 0.98),
            max_buys_per_side=_env_int("SCALP_MAX_BUYS_PER_SIDE", 4),
            leg_usdc_cap=_env_float("SCALP_LEG_USDC", 5.0),
            poll_ms=_env_int("SCALP_POLL_MS", 250),
            discover_every_sec=_env_int("SCALP_DISCOVER_EVERY_SEC", 60),
        )


@dataclass
class ScalpPair:
    slug: str
    period_ts: int
    up_token: str
    down_token: str
    cfg: ScalperConfig
    # per-side tracking
    temp_price_up: Optional[float] = None
    temp_price_down: Optional[float] = None
    last_update_up_ms: int = 0
    last_update_down_ms: int = 0
    below_dyn_since_up_ms: Optional[int] = None
    below_dyn_since_down_ms: Optional[int] = None

    def apply_tick(self, side: str, best_ask: float, now_ms: int) -> None:
        """Update running low (tempPrice) for one side from a fresh ask quote.

        Eligibility: ask must be ≤ threshold. Above threshold resets the tracker
        — TS source treats price exiting the eligible range as a fresh start.
        """
        if side == "up":
            attr = "temp_price_up"
            ts_attr = "last_update_up_ms"
        elif side == "down":
            attr = "temp_price_down"
            ts_attr = "last_update_down_ms"
        else:
            raise ValueError(f"side must be 'up' or 'down', got {side}")

        if best_ask > self.cfg.threshold:
            setattr(self, attr, None)
            return
        cur = getattr(self, attr)
        if cur is None or best_ask < cur:
            setattr(self, attr, best_ask)
        setattr(self, ts_attr, now_ms)
```

- [ ] **Step 4: Run tests to verify pass**

Run:
```bash
python3 -m unittest tests.test_scalper -v
```
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/application/scalper.py tests/test_scalper.py
git commit -m "feat(scalper): add ScalpPair tracking + ScalperConfig defaults

Per-slug per-side running-low tracking — the foundation for the entry
trigger. apply_tick() resets the tracker when the ask leaves the eligible
range (ask > threshold), matching TS source behavior. Config defaults
match TS source verbatim."
```

---

## Task 4: Entry decision — eligibility, reversal trigger, depth trigger

**Files:**
- Modify: `agents/application/scalper.py`
- Modify: `tests/test_scalper.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_scalper.py` inside `class TestScalpPair`:

```python
def test_no_signal_while_no_temp_price(self):
    # ineligible — never set tempPrice
    self.pair.apply_tick("up", best_ask=0.55, now_ms=1000)
    sig = self.pair.evaluate_entry("up", best_ask=0.55, now_ms=1000)
    self.assertIsNone(sig)

def test_no_signal_when_just_setting_low(self):
    self.pair.apply_tick("up", best_ask=0.45, now_ms=1000)
    sig = self.pair.evaluate_entry("up", best_ask=0.45, now_ms=1100)
    self.assertIsNone(sig, "must wait for reversal or deeper drop")

def test_reversal_trigger_at_2c_bounce(self):
    self.pair.apply_tick("up", best_ask=0.45, now_ms=1000)
    # bounce by exactly reversal_delta = 0.020 → still must satisfy >=
    sig = self.pair.evaluate_entry("up", best_ask=0.47, now_ms=1100)
    self.assertIsNotNone(sig)
    self.assertEqual(sig["reason"], "reversal")
    self.assertAlmostEqual(sig["price"], 0.47)

def test_reversal_trigger_below_2c_does_not_fire(self):
    self.pair.apply_tick("up", best_ask=0.45, now_ms=1000)
    sig = self.pair.evaluate_entry("up", best_ask=0.469, now_ms=1100)
    self.assertIsNone(sig)

def test_depth_trigger_at_5pct_discount(self):
    self.pair.apply_tick("up", best_ask=0.40, now_ms=1000)
    # 5% below 0.40 = 0.38 — deeper drop fires depth trigger
    sig = self.pair.evaluate_entry("up", best_ask=0.38, now_ms=1100)
    self.assertIsNotNone(sig)
    self.assertEqual(sig["reason"], "depth")

def test_depth_trigger_above_threshold_resets(self):
    self.pair.apply_tick("up", best_ask=0.40, now_ms=1000)
    # ineligible → resets, no signal
    sig = self.pair.evaluate_entry("up", best_ask=0.55, now_ms=1100)
    self.assertIsNone(sig)
```

- [ ] **Step 2: Run tests to verify failure**

Run:
```bash
python3 -m unittest tests.test_scalper.TestScalpPair -v
```
Expected: 6 new tests fail with `AttributeError: 'ScalpPair' object has no attribute 'evaluate_entry'`.

- [ ] **Step 3: Implement `evaluate_entry`**

Add to `class ScalpPair` in `agents/application/scalper.py`:

```python
    def evaluate_entry(
        self, side: str, best_ask: float, now_ms: int
    ) -> Optional[dict]:
        """Return entry signal dict {'reason': str, 'price': float} or None.

        Must be called AFTER apply_tick for the same (side, best_ask, now_ms)
        — apply_tick maintains the running low; this method reads it and
        decides whether to fire.
        """
        if best_ask > self.cfg.threshold:
            return None
        temp_attr = "temp_price_up" if side == "up" else "temp_price_down"
        temp = getattr(self, temp_attr)
        if temp is None:
            return None
        # Depth trigger: ask is at least depth_buy_discount % below tempPrice
        if best_ask <= temp * (1.0 - self.cfg.depth_buy_discount):
            return {"reason": "depth", "price": best_ask}
        # Reversal trigger: ask has bounced by at least reversal_delta
        if best_ask >= temp + self.cfg.reversal_delta:
            return {"reason": "reversal", "price": best_ask}
        return None
```

Note: `apply_tick` should be invoked *before* `evaluate_entry` for the same tick. The test `test_reversal_trigger_at_2c_bounce` reads the running low set by the previous `apply_tick` call, then evaluates — order matters in the engine loop too.

- [ ] **Step 4: Run tests to verify pass**

Run:
```bash
python3 -m unittest tests.test_scalper.TestScalpPair -v
```
Expected: all `TestScalpPair` tests PASS (10 total).

- [ ] **Step 5: Commit**

```bash
git add agents/application/scalper.py tests/test_scalper.py
git commit -m "feat(scalper): entry trigger — eligibility + reversal + depth

Implements the TS source's two-trigger entry logic. Eligibility (ask <=
threshold) gates the running-low tracker; once tempPrice is set, either
a 2-cent bounce up (reversal) or a 5% deeper drop (depth) fires the buy.
Pure logic, no I/O — fully unit-testable."
```

---

## Task 5: Profit gate — `avg_yes + avg_no <= max_sum_avg`

**Files:**
- Modify: `agents/application/scalper.py`
- Modify: `tests/test_scalper.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_scalper.py`:

```python
class TestProfitGate(unittest.TestCase):
    def setUp(self):
        self.cfg = ScalperConfig()
        self.pair = ScalpPair(slug="s", period_ts=1, up_token="u",
                               down_token="d", cfg=self.cfg)

    def test_gate_allows_when_no_other_side_yet(self):
        # No fills yet on opposite side → other_avg = 0 → max acceptable = 0.98
        self.assertTrue(self.pair.check_profit_gate(side="up", price=0.45,
                                                       qty_other=0, cost_other=0))
        self.assertTrue(self.pair.check_profit_gate(side="up", price=0.97,
                                                       qty_other=0, cost_other=0))

    def test_gate_blocks_when_sum_exceeds_max(self):
        # other_avg = 0.50, candidate price = 0.49 → sum = 0.99 > 0.98 → block
        self.assertFalse(self.pair.check_profit_gate(side="up", price=0.49,
                                                        qty_other=10, cost_other=5.0))

    def test_gate_allows_when_sum_at_boundary(self):
        # other_avg = 0.50, candidate = 0.48 → sum = 0.98 == max → allow (<=)
        self.assertTrue(self.pair.check_profit_gate(side="up", price=0.48,
                                                       qty_other=10, cost_other=5.0))

    def test_gate_with_partial_other_fills(self):
        # 5 shares filled at avg cost 0.40 → other_avg = 0.40
        # candidate 0.57 → sum = 0.97 < 0.98 → allow
        self.assertTrue(self.pair.check_profit_gate(side="up", price=0.57,
                                                       qty_other=5, cost_other=2.0))
        # candidate 0.59 → sum = 0.99 > 0.98 → block
        self.assertFalse(self.pair.check_profit_gate(side="up", price=0.59,
                                                        qty_other=5, cost_other=2.0))
```

- [ ] **Step 2: Run tests to verify failure**

Run:
```bash
python3 -m unittest tests.test_scalper.TestProfitGate -v
```
Expected: 4 tests fail with `AttributeError: 'ScalpPair' object has no attribute 'check_profit_gate'`.

- [ ] **Step 3: Implement `check_profit_gate`**

Add to `class ScalpPair`:

```python
    def check_profit_gate(
        self, side: str, price: float, qty_other: float, cost_other: float
    ) -> bool:
        """Return True if `existing_avg_other + price <= max_sum_avg`.

        `qty_other` and `cost_other` are total fills on the OTHER side so far
        for this pair. With no opposite fills (qty_other=0), the gate compares
        `price` alone against `max_sum_avg` (which permits any price ≤ 0.98).
        """
        if qty_other <= 0:
            other_avg = 0.0
        else:
            other_avg = cost_other / qty_other
        return (other_avg + price) <= self.cfg.max_sum_avg
```

- [ ] **Step 4: Run tests to verify pass**

Run:
```bash
python3 -m unittest tests.test_scalper -v
```
Expected: all tests PASS, including 4 new in `TestProfitGate`.

- [ ] **Step 5: Commit**

```bash
git add agents/application/scalper.py tests/test_scalper.py
git commit -m "feat(scalper): profit gate — sum_avg <= max_sum_avg

Mandatory check before EVERY buy: existing_avg_on_other_side + candidate
price must not exceed max_sum_avg (0.98 default). One bad fill that
violates this gate makes the pair unprofitable; the gate stops further
attempts on the same pair."
```

---

## Task 6: Second-leg trigger — dynamic threshold + 200ms continuous-below

**Files:**
- Modify: `agents/application/scalper.py`
- Modify: `tests/test_scalper.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_scalper.py`:

```python
class TestSecondLeg(unittest.TestCase):
    def setUp(self):
        self.cfg = ScalperConfig()
        self.pair = ScalpPair(slug="s", period_ts=1, up_token="u",
                               down_token="d", cfg=self.cfg)
        # Simulate leg 1 fill on UP at 0.45 → dyn_threshold for DOWN = 1 - 0.45 + 0.04 = 0.59
        self.pair.dynamic_threshold_down = 0.59

    def test_second_leg_immediate_trigger(self):
        # ask <= dyn - second_side_buffer (0.01) → immediate
        sig = self.pair.evaluate_second_leg("down", best_ask=0.57, now_ms=1000)
        self.assertEqual(sig["reason"], "dyn_threshold_immediate")

    def test_second_leg_blocked_when_above_dyn(self):
        sig = self.pair.evaluate_second_leg("down", best_ask=0.62, now_ms=1000)
        self.assertIsNone(sig)
        # below_dyn_since must NOT be set
        self.assertIsNone(self.pair.below_dyn_since_down_ms)

    def test_second_leg_continuous_below_starts_timer(self):
        # ask = 0.585 → below dyn (0.59) but above (dyn - buffer = 0.58)
        sig = self.pair.evaluate_second_leg("down", best_ask=0.585, now_ms=1000)
        self.assertIsNone(sig)  # timer just started
        self.assertEqual(self.pair.below_dyn_since_down_ms, 1000)

    def test_second_leg_fires_after_200ms_continuous(self):
        self.pair.evaluate_second_leg("down", best_ask=0.585, now_ms=1000)
        sig = self.pair.evaluate_second_leg("down", best_ask=0.585, now_ms=1200)
        self.assertEqual(sig["reason"], "dyn_threshold_continuous")

    def test_second_leg_timer_resets_on_exit(self):
        self.pair.evaluate_second_leg("down", best_ask=0.585, now_ms=1000)
        # price spikes back above dyn
        self.pair.evaluate_second_leg("down", best_ask=0.62, now_ms=1100)
        self.assertIsNone(self.pair.below_dyn_since_down_ms)
        # comes back — timer restarts
        self.pair.evaluate_second_leg("down", best_ask=0.585, now_ms=1200)
        self.assertEqual(self.pair.below_dyn_since_down_ms, 1200)
```

Add `dynamic_threshold_up: Optional[float] = None` and `dynamic_threshold_down: Optional[float] = None` to `ScalpPair` (test setUp sets one directly).

- [ ] **Step 2: Run tests to verify failure**

Run:
```bash
python3 -m unittest tests.test_scalper.TestSecondLeg -v
```
Expected: 5 tests fail.

- [ ] **Step 3: Implement `evaluate_second_leg` and add fields**

Modify the `ScalpPair` dataclass declaration to add:

```python
    dynamic_threshold_up: Optional[float] = None
    dynamic_threshold_down: Optional[float] = None
```

Add method to `ScalpPair`:

```python
    def evaluate_second_leg(
        self, side: str, best_ask: float, now_ms: int
    ) -> Optional[dict]:
        """Return second-leg fire signal or None.

        Caller must have set `dynamic_threshold_<side>` after leg 1 fill.
        Fires on either:
          (a) ask <= dyn - second_side_buffer  (immediate)
          (b) ask <= dyn for >= second_side_time_ms continuously (timer)
        """
        if side == "up":
            dyn = self.dynamic_threshold_up
            timer_attr = "below_dyn_since_up_ms"
        elif side == "down":
            dyn = self.dynamic_threshold_down
            timer_attr = "below_dyn_since_down_ms"
        else:
            raise ValueError(f"side must be 'up' or 'down', got {side}")

        if dyn is None:
            return None
        if best_ask > dyn:
            setattr(self, timer_attr, None)
            return None
        # ask <= dyn from here on
        if best_ask <= dyn - self.cfg.second_side_buffer:
            return {"reason": "dyn_threshold_immediate", "price": best_ask}
        cur = getattr(self, timer_attr)
        if cur is None:
            setattr(self, timer_attr, now_ms)
            return None
        if (now_ms - cur) >= self.cfg.second_side_time_ms:
            return {"reason": "dyn_threshold_continuous", "price": best_ask}
        return None
```

- [ ] **Step 4: Run tests to verify pass**

Run:
```bash
python3 -m unittest tests.test_scalper -v
```
Expected: all `TestSecondLeg` tests PASS plus all earlier tests still passing.

- [ ] **Step 5: Commit**

```bash
git add agents/application/scalper.py tests/test_scalper.py
git commit -m "feat(scalper): second-leg trigger — dyn threshold + 200ms timer

After leg 1 fills at price P, dynamic_threshold for the opposite side =
1 - P + 0.04. Second leg fires immediately if ask <= dyn - 0.01, or
after the ask has been <= dyn for at least 200ms (timer).
Timer auto-resets when ask exits below-dyn region."
```

---

## Task 7: Add `OrderType.FAK` path to `polymarket.execute_market_order`

**Files:**
- Modify: `agents/polymarket/polymarket.py`
- Modify: `tests/test_drift_fixes.py` or create `tests/test_polymarket_fak.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_polymarket_fak.py`:

```python
import unittest
from unittest.mock import MagicMock, patch

from agents.polymarket.polymarket import Polymarket
from agents.utils.objects import TradeRecommendation


class TestExecuteMarketOrderFAK(unittest.TestCase):
    def _make_market(self):
        from langchain_core.documents import Document
        doc = Document(
            page_content="...",
            metadata={
                "outcomes": "['Up', 'Down']",
                "clob_token_ids": "['tok_up', 'tok_dn']",
                "outcome_prices": "['0.5', '0.5']",
                "id": "btc-updown-15m-100",
            },
        )
        return (doc, 0.0)

    @patch.object(Polymarket, "__init__", lambda self, **kw: None)
    def test_execute_market_order_passes_fak_when_requested(self):
        from py_clob_client.clob_types import OrderType
        p = Polymarket()
        p.client = MagicMock()
        p._fillable_market_buy = MagicMock(return_value=(0.51, 5.0, 0.50))
        p.client.create_and_post_market_order = MagicMock(
            return_value={"status": "filled", "orderID": "abc"}
        )
        rec = TradeRecommendation(price=0.50, size_fraction=0.05, side="BUY",
                                   confidence=0.7, amount_usdc=5.0)
        result = p.execute_market_order(self._make_market(), rec, order_type=OrderType.FAK)
        # Inspect the call kwargs
        call_kwargs = p.client.create_and_post_market_order.call_args.kwargs
        self.assertEqual(call_kwargs.get("order_type"), OrderType.FAK)
        self.assertEqual(result["status"], "filled")

    @patch.object(Polymarket, "__init__", lambda self, **kw: None)
    def test_execute_market_order_defaults_to_fok(self):
        from py_clob_client.clob_types import OrderType
        p = Polymarket()
        p.client = MagicMock()
        p._fillable_market_buy = MagicMock(return_value=(0.51, 5.0, 0.50))
        p.client.create_and_post_market_order = MagicMock(
            return_value={"status": "filled", "orderID": "abc"}
        )
        rec = TradeRecommendation(price=0.50, size_fraction=0.05, side="BUY",
                                   confidence=0.7, amount_usdc=5.0)
        p.execute_market_order(self._make_market(), rec)  # no order_type kwarg
        call_kwargs = p.client.create_and_post_market_order.call_args.kwargs
        self.assertEqual(call_kwargs.get("order_type"), OrderType.FOK)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify failure**

Run:
```bash
docker compose run --rm trader python -m unittest tests.test_polymarket_fak -v
```
Expected: tests fail because `execute_market_order` does not accept `order_type` kwarg.

- [ ] **Step 3: Modify `execute_market_order` signature and use the kwarg**

In `agents/polymarket/polymarket.py`, around line 573, change the function signature and the `_post()` body:

```python
    def execute_market_order(
        self, market, recommendation: TradeRecommendation,
        order_type=None,
    ) -> dict:
        from py_clob_client.clob_types import OrderType  # local to avoid cycles
        if order_type is None:
            order_type = OrderType.FOK
```

Then locate `_post()` (around line 675) and change:

```python
        def _post():
            return self.client.create_and_post_market_order(
                order_args,
                order_type=order_type,
            )
```

Make sure the existing `OrderType` import at module top covers `FAK` (py_clob_client 0.34+ exposes both `FOK` and `FAK` in the same enum).

- [ ] **Step 4: Run tests to verify pass**

Run:
```bash
docker compose run --rm trader python -m unittest tests.test_polymarket_fak tests.test_trader -v
```
Expected: all PASS, including original `test_trader` regression.

- [ ] **Step 5: Commit**

```bash
git add agents/polymarket/polymarket.py tests/test_polymarket_fak.py
git commit -m "feat(polymarket): expose order_type kwarg on execute_market_order

Default remains FOK (existing Trader behavior — duplicates are worse than
missed fills). The new FAK path is opt-in for the scalper, where partial
fills are acceptable. No other call sites change."
```

---

## Task 8: `ScalperPairsDAO.record_fill` integration via `place_leg`

**Files:**
- Modify: `agents/application/scalper.py`
- Modify: `tests/test_scalper_engine.py` (new)

- [ ] **Step 1: Write failing test**

Create `tests/test_scalper_engine.py`:

```python
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from agents.application.scalper import ScalperEngine, ScalperConfig, ScalpPair
from agents.application.scalper_pairs import ScalperPairsDAO, ScalperState
from agents.application.trade_log import TradeLog, SCALPER_LEG


class TestPlaceLeg(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = TradeLog(db_path=self.tmp.name)
        self.dao = ScalperPairsDAO(self.log)
        self.client = MagicMock()
        self.cfg = ScalperConfig()
        self.engine = ScalperEngine(client=self.client, log=self.log,
                                      dao=self.dao, cfg=self.cfg, execute=True)
        self.dao.create("s1", 100, "tok_up", "tok_dn")

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_place_leg_writes_to_dao_and_trades(self):
        # Mock a successful FAK fill at 0.45 for $5.00 → ~11.11 shares
        self.client.execute_market_order = MagicMock(return_value={
            "status": "filled",
            "amount_usdc": 5.0,
            "order_avg_price_estimate": 0.45,
            "token_id": "tok_up",
            "order_id": "abc",
        })
        result = self.engine.place_leg(slug="s1", side="up",
                                          token="tok_up", usdc=5.0,
                                          intended_price=0.45)
        self.assertTrue(result["filled"])
        # DAO updated
        row = self.dao.get_by_slug("s1")
        self.assertGreater(row["qty_up"], 0)
        self.assertAlmostEqual(row["cost_up"], 5.0)
        self.assertEqual(row["attempts_up"], 1)
        # trades row written with SCALPER_LEG status
        recent = self.log.recent(limit=5)
        self.assertEqual(recent[0]["status"], SCALPER_LEG)
        self.assertEqual(recent[0]["market_id"], "s1")

    def test_place_leg_handles_failure(self):
        self.client.execute_market_order = MagicMock(side_effect=ValueError("FOK kill"))
        result = self.engine.place_leg(slug="s1", side="up",
                                          token="tok_up", usdc=5.0,
                                          intended_price=0.45)
        self.assertFalse(result["filled"])
        # DAO attempts incremented but qty NOT changed
        row = self.dao.get_by_slug("s1")
        self.assertEqual(row["qty_up"], 0)
        self.assertEqual(row["attempts_up"], 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify failure**

Run:
```bash
python3 -m unittest tests.test_scalper_engine -v
```
Expected: ImportError — `ScalperEngine` does not exist.

- [ ] **Step 3: Implement `ScalperEngine` skeleton with `place_leg`**

Add to `agents/application/scalper.py`:

```python
from py_clob_client.clob_types import OrderType
from agents.application.trade_log import SCALPER_LEG, TradeLog
from agents.application.scalper_pairs import ScalperPairsDAO, ScalperState
from agents.utils.objects import TradeRecommendation


class ScalperEngine:
    """Top-level scalper engine. Owns the running pairs and the I/O loop."""

    def __init__(
        self,
        client,                     # Polymarket-like, has execute_market_order
        log: TradeLog,
        dao: ScalperPairsDAO,
        cfg: ScalperConfig,
        execute: bool = False,      # False = shadow mode
    ):
        self.client = client
        self.log = log
        self.dao = dao
        self.cfg = cfg
        self.execute = execute

    def place_leg(
        self,
        slug: str,
        side: str,                  # "up" | "down"
        token: str,
        usdc: float,
        intended_price: float,
    ) -> dict:
        """Attempt one FAK leg. Always increments attempts in DAO; only
        increments qty/cost on success. Always writes a SCALPER_LEG row.
        """
        cycle_id = f"scalp:{slug}:{side}"
        # Construct a synthetic recommendation that mirrors the algorithm decision
        rec = TradeRecommendation(
            price=intended_price if side == "up" else (1.0 - intended_price),
            size_fraction=0.0,        # not used by execute_market_order in this path
            side="BUY" if side == "up" else "SELL",
            confidence=None,
            amount_usdc=usdc,
        )
        # Construct a minimal market tuple compatible with execute_market_order
        from langchain_core.documents import Document
        market = (Document(page_content="", metadata={
            "outcomes": "['Up', 'Down']",
            "clob_token_ids": "['" + token + "', '" + token + "']",  # see note
            "outcome_prices": "['" + str(intended_price) + "', '" +
                str(1.0 - intended_price) + "']",
            "id": slug,
        }), 0.0)
        # NOTE: The clob_token_ids contortion is because execute_market_order
        # expects 2 tokens and infers BUY → tokens[0], SELL → tokens[1]. To
        # avoid touching the existing convention, the engine ALWAYS resolves
        # token explicitly via a thin wrapper around the CLOB client.

        try:
            response = self.client.execute_market_order(
                market, rec, order_type=OrderType.FAK,
            )
            filled_usdc = float(response.get("amount_usdc", 0.0))
            avg_price = float(response.get("order_avg_price_estimate",
                                            intended_price))
            qty = filled_usdc / avg_price if avg_price > 0 else 0.0
            self.dao.record_fill(slug, side, qty=qty,
                                   cost_usdc=filled_usdc,
                                   fill_price=avg_price)
            self.log.insert_terminal(
                cycle_id=cycle_id, market_id=slug, status=SCALPER_LEG,
                token_id=token, side=rec.side, price=avg_price,
                size_usdc=filled_usdc, response=response,
            )
            return {"filled": True, "qty": qty, "avg_price": avg_price}
        except Exception as e:
            # Increment attempts even on failure (record_fill increments via
            # qty=0, cost=0)
            self.dao.record_fill(slug, side, qty=0.0, cost_usdc=0.0)
            self.log.insert_terminal(
                cycle_id=cycle_id, market_id=slug, status=SCALPER_LEG,
                token_id=token, side="BUY" if side == "up" else "SELL",
                price=intended_price, size_usdc=0.0, error=str(e),
            )
            return {"filled": False, "error": str(e)}
```

**Refinement note:** the synthetic-market trick in `place_leg` is a workaround so the existing `execute_market_order` semantics work unchanged. Task 9's `ScalperEngine` discovery path will store both `up_token` and `down_token` properly; `place_leg` is given the explicit token to use. If this proves messy in Task 10 integration, refactor `execute_market_order` to expose a lower-level `place_buy(token_id, usdc, price, order_type)` helper at that point — not now. (YAGNI.)

- [ ] **Step 4: Run tests to verify pass**

Run:
```bash
python3 -m unittest tests.test_scalper_engine -v
```
Expected: both `TestPlaceLeg` tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/application/scalper.py tests/test_scalper_engine.py
git commit -m "feat(scalper): place_leg writes to DAO + SCALPER_LEG audit row

Each FAK attempt updates scalper_pairs.qty/cost/attempts and writes a
matching trades row with status SCALPER_LEG. Failures increment attempts
and write a trades row with the error — visible to the existing
inspect-trades CLI."
```

---

## Task 9: Market discovery — gamma `*-updown-15m-*` slug filter

**Files:**
- Modify: `agents/application/scalper.py`
- Modify: `tests/test_scalper_engine.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_scalper_engine.py`:

```python
class TestMarketDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = TradeLog(db_path=self.tmp.name)
        self.dao = ScalperPairsDAO(self.log)
        self.gamma = MagicMock()
        self.engine = ScalperEngine(client=MagicMock(), log=self.log,
                                      dao=self.dao, cfg=ScalperConfig(),
                                      gamma=self.gamma, execute=False)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_discover_filters_to_updown_15m_only(self):
        self.gamma.get_events_by_tag = MagicMock(return_value=[
            {"slug": "btc-updown-15m-100", "markets": [
                {"slug": "btc-updown-15m-100",
                 "clobTokenIds": "['tok_up', 'tok_dn']",
                 "outcomes": "['Up', 'Down']",
                 "endDate": "2026-05-05T12:15:00Z",
                 "acceptingOrders": True}]},
            {"slug": "trump-2028", "markets": [{"slug": "trump-2028"}]},
            {"slug": "eth-updown-5m-50", "markets": [{"slug": "eth-updown-5m-50"}]},
        ])
        slugs = self.engine.discover_markets()
        self.assertEqual([m["slug"] for m in slugs], ["btc-updown-15m-100"])

    def test_discover_skips_not_accepting_orders(self):
        self.gamma.get_events_by_tag = MagicMock(return_value=[
            {"slug": "btc-updown-15m-100", "markets": [
                {"slug": "btc-updown-15m-100", "acceptingOrders": False,
                 "clobTokenIds": "['tok_up', 'tok_dn']",
                 "outcomes": "['Up', 'Down']",
                 "endDate": "2026-05-05T12:15:00Z"}]},
        ])
        self.assertEqual(self.engine.discover_markets(), [])

    def test_discover_creates_pair_in_dao(self):
        self.gamma.get_events_by_tag = MagicMock(return_value=[
            {"slug": "btc-updown-15m-100", "markets": [
                {"slug": "btc-updown-15m-100", "acceptingOrders": True,
                 "clobTokenIds": "['tok_up', 'tok_dn']",
                 "outcomes": "['Up', 'Down']",
                 "endDate": "2026-05-05T12:15:00Z"}]},
        ])
        self.engine.discover_markets()
        self.assertIsNotNone(self.dao.get_by_slug("btc-updown-15m-100"))
```

- [ ] **Step 2: Run test to verify failure**

Run:
```bash
python3 -m unittest tests.test_scalper_engine.TestMarketDiscovery -v
```
Expected: 3 tests fail because `gamma` kwarg and `discover_markets` do not exist.

- [ ] **Step 3: Implement `discover_markets`**

Modify `ScalperEngine.__init__` to accept a `gamma` arg:

```python
    def __init__(
        self, client, log: TradeLog, dao: ScalperPairsDAO,
        cfg: ScalperConfig, gamma=None, execute: bool = False,
    ):
        ...
        self.gamma = gamma
```

Add method:

```python
    SLUG_FILTER = "-updown-15m-"

    def discover_markets(self) -> list[dict]:
        """Scan gamma for `*-updown-15m-*` events. Create scalper_pairs rows
        for new ones. Return the list of currently-trading markets."""
        import ast
        if self.gamma is None:
            raise RuntimeError("gamma client not provided")
        # tag_id 21 = crypto on Polymarket gamma (verified per spec live calls)
        events = self.gamma.get_events_by_tag(tag_id=21)
        out = []
        for ev in events:
            for m in ev.get("markets", []):
                slug = m.get("slug", "")
                if self.SLUG_FILTER not in slug:
                    continue
                if not m.get("acceptingOrders", False):
                    continue
                try:
                    tokens = ast.literal_eval(m["clobTokenIds"])
                    outcomes = ast.literal_eval(m["outcomes"])
                except Exception as e:
                    logger.warning("scalper: bad market metadata %s: %s", slug, e)
                    continue
                if len(tokens) != 2 or len(outcomes) != 2:
                    continue
                # Period_ts: try the suffix on the slug; fall back to endDate.
                period_ts = self._parse_period_ts(slug, m.get("endDate"))
                self.dao.create(slug=slug, period_ts=period_ts,
                                 up_token=tokens[0], down_token=tokens[1])
                out.append({
                    "slug": slug,
                    "up_token": tokens[0],
                    "down_token": tokens[1],
                    "period_ts": period_ts,
                })
        return out

    @staticmethod
    def _parse_period_ts(slug: str, end_date: Optional[str]) -> int:
        """Try suffix integer (Unix seconds) on slug, else parse endDate."""
        suffix = slug.rsplit("-", 1)[-1]
        if suffix.isdigit():
            return int(suffix)
        if end_date:
            from datetime import datetime
            try:
                return int(datetime.fromisoformat(
                    end_date.replace("Z", "+00:00")).timestamp())
            except Exception:
                pass
        return 0
```

If `agents/polymarket/gamma.py` does not have `get_events_by_tag`, add a thin wrapper there:

```python
# Add to agents/polymarket/gamma.py
def get_events_by_tag(self, tag_id: int, limit: int = 50) -> list[dict]:
    """GET /events?tag_id=&active=true&closed=false&limit=&order=endDate&ascending=true"""
    url = f"{self._base}/events"
    params = {"tag_id": tag_id, "active": "true", "closed": "false",
              "limit": str(limit), "order": "endDate", "ascending": "true"}
    resp = self._session.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()
```

(If `_base` and `_session` aren't present in gamma.py, follow the existing pattern in that file — adapt the snippet to actual fields.)

- [ ] **Step 4: Run tests to verify pass**

Run:
```bash
python3 -m unittest tests.test_scalper_engine -v
```
Expected: all `TestMarketDiscovery` PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/application/scalper.py agents/polymarket/gamma.py tests/test_scalper_engine.py
git commit -m "feat(scalper): discover_markets — gamma scan filtered to updown-15m

Polls gamma /events?tag_id=21 (crypto), filters to slugs containing
'-updown-15m-', skips closed/non-accepting markets. Creates a tracking
row per new pair. Period_ts parsed from slug suffix or endDate fallback."
```

---

## Task 10: Tick loop — `apply_tick` + entry/second-leg evaluation per pair

**Files:**
- Modify: `agents/application/scalper.py`
- Modify: `tests/test_scalper_engine.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_scalper_engine.py`:

```python
class TestTickLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = TradeLog(db_path=self.tmp.name)
        self.dao = ScalperPairsDAO(self.log)
        self.client = MagicMock()
        self.cfg = ScalperConfig(leg_usdc_cap=5.0)
        self.engine = ScalperEngine(client=self.client, log=self.log,
                                      dao=self.dao, cfg=self.cfg, execute=True)
        self.engine.add_pair(ScalpPair(slug="s1", period_ts=100,
                                          up_token="tok_up", down_token="tok_dn",
                                          cfg=self.cfg))

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_tick_below_threshold_sets_temp_only(self):
        self.engine.tick(slug="s1", up_ask=0.45, down_ask=0.50, now_ms=1000)
        # No leg fired — only tracker set
        self.client.execute_market_order.assert_not_called()
        self.assertEqual(self.engine.pairs["s1"].temp_price_up, 0.45)

    def test_tick_reversal_fires_leg1(self):
        self.client.execute_market_order = MagicMock(return_value={
            "status": "filled", "amount_usdc": 5.0,
            "order_avg_price_estimate": 0.47, "token_id": "tok_up",
            "order_id": "x",
        })
        self.engine.tick(slug="s1", up_ask=0.45, down_ask=0.50, now_ms=1000)
        self.engine.tick(slug="s1", up_ask=0.47, down_ask=0.50, now_ms=1100)
        self.client.execute_market_order.assert_called_once()
        row = self.dao.get_by_slug("s1")
        self.assertEqual(row["state"], ScalperState.LEG1_FILLED)
        self.assertGreater(row["qty_up"], 0)

    def test_tick_no_repeat_after_max_attempts(self):
        # Force 4 failing attempts by raising
        self.client.execute_market_order = MagicMock(
            side_effect=ValueError("FOK kill"))
        for i in range(6):
            self.engine.tick(slug="s1", up_ask=0.45, down_ask=0.50, now_ms=1000+i*10)
            self.engine.tick(slug="s1", up_ask=0.47, down_ask=0.50, now_ms=1010+i*10)
        # Capped at max_buys_per_side=4
        self.assertEqual(self.client.execute_market_order.call_count, 4)
```

- [ ] **Step 2: Run tests to verify failure**

Run:
```bash
python3 -m unittest tests.test_scalper_engine.TestTickLoop -v
```
Expected: tests fail because `add_pair`, `tick`, and `pairs` do not exist.

- [ ] **Step 3: Implement `tick` and `add_pair`**

Add to `ScalperEngine`:

```python
    def __init__(self, ...):
        ...
        self.pairs: dict[str, ScalpPair] = {}

    def add_pair(self, pair: ScalpPair) -> None:
        self.pairs[pair.slug] = pair

    def tick(self, slug: str, up_ask: float, down_ask: float, now_ms: int) -> None:
        """One scheduling tick per slug. Apply ticks → maybe fire legs."""
        pair = self.pairs.get(slug)
        if pair is None:
            return
        row = self.dao.get_by_slug(slug)
        if row is None:
            return
        if row["state"] in (ScalperState.BOTH_FILLED, ScalperState.EXPIRED,
                             ScalperState.REDEEMED, ScalperState.SHADOW):
            return

        # Update both sides' running lows
        pair.apply_tick("up", up_ask, now_ms)
        pair.apply_tick("down", down_ask, now_ms)

        if row["state"] == ScalperState.TRACKING:
            # Both sides eligible for leg 1 — evaluate each
            for side, ask in (("up", up_ask), ("down", down_ask)):
                attempts = row[f"attempts_{side}"]
                if attempts >= self.cfg.max_buys_per_side:
                    continue
                sig = pair.evaluate_entry(side, ask, now_ms)
                if sig is None:
                    continue
                # Profit gate: at first leg, qty_other = 0 → always passes
                if not pair.check_profit_gate(side, sig["price"],
                                                qty_other=0, cost_other=0):
                    continue
                token = pair.up_token if side == "up" else pair.down_token
                result = self.place_leg(slug=slug, side=side, token=token,
                                          usdc=self.cfg.leg_usdc_cap,
                                          intended_price=sig["price"])
                if result["filled"]:
                    self.dao.set_state(slug, ScalperState.LEG1_FILLED)
                    # Set dynamic threshold for opposite side
                    other = "down" if side == "up" else "up"
                    setattr(pair, f"dynamic_threshold_{other}",
                            1.0 - result["avg_price"]
                                + self.cfg.dynamic_threshold_boost)
                    return
                # If failed: row attempts already incremented; loop continues
                # next tick may retry up to max_buys_per_side total

        elif row["state"] == ScalperState.LEG1_FILLED:
            # Determine which side is the unfilled (second) leg
            second = "up" if row["qty_up"] == 0 else "down"
            ask = up_ask if second == "up" else down_ask
            attempts = row[f"attempts_{second}"]
            if attempts >= self.cfg.max_buys_per_side:
                return
            sig = pair.evaluate_second_leg(second, ask, now_ms)
            if sig is None:
                return
            qty_other = row["qty_up" if second == "down" else "qty_down"]
            cost_other = row["cost_up" if second == "down" else "cost_down"]
            if not pair.check_profit_gate(second, sig["price"],
                                             qty_other=qty_other,
                                             cost_other=cost_other):
                return
            token = pair.up_token if second == "up" else pair.down_token
            result = self.place_leg(slug=slug, side=second, token=token,
                                      usdc=self.cfg.leg_usdc_cap,
                                      intended_price=sig["price"])
            if result["filled"]:
                self.dao.set_state(slug, ScalperState.BOTH_FILLED)
```

- [ ] **Step 4: Run tests to verify pass**

Run:
```bash
python3 -m unittest tests.test_scalper_engine -v
```
Expected: all `TestTickLoop` PASS plus all earlier tests still passing.

- [ ] **Step 5: Commit**

```bash
git add agents/application/scalper.py tests/test_scalper_engine.py
git commit -m "feat(scalper): tick orchestration — entry → leg1 → leg2

The tick() entry point ties together apply_tick + evaluate_entry +
check_profit_gate + place_leg. Once leg 1 fills, sets dynamic threshold
on the opposite side and switches the pair to LEG1_FILLED state.
Once both legs fill, moves to BOTH_FILLED. Capped at max_buys_per_side
attempts per side."
```

---

## Task 11: `SCALPER_RESERVE_USDC` enforcement on both sides

**Files:**
- Modify: `agents/application/risk_gate.py`
- Modify: `agents/application/scalper.py`
- Modify: `tests/test_trader.py` (extend `TestRiskGate`)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_trader.py` inside `class TestRiskGate`:

```python
def test_available_for_trader_subtracts_scalper_reserve(self):
    log = TradeLog(db_path=":memory:")
    poly = MagicMock()
    poly.get_usdc_balance = MagicMock(return_value=80.0)
    gate = RiskGate(trade_log=log, polymarket=poly,
                     starting_balance_usdc=80.0,
                     scalper_reserve_usdc=20.0)
    self.assertEqual(gate.available_for_trader(), 60.0)

def test_available_for_trader_zero_reserve_default(self):
    log = TradeLog(db_path=":memory:")
    poly = MagicMock()
    poly.get_usdc_balance = MagicMock(return_value=80.0)
    gate = RiskGate(trade_log=log, polymarket=poly,
                     starting_balance_usdc=80.0)  # default reserve=0
    self.assertEqual(gate.available_for_trader(), 80.0)

def test_min_floor_uses_available_after_reserve(self):
    """If reserve makes available drop below min_usdc_floor, gate blocks."""
    log = TradeLog(db_path=":memory:")
    poly = MagicMock()
    poly.get_usdc_balance = MagicMock(return_value=25.0)
    gate = RiskGate(trade_log=log, polymarket=poly,
                     starting_balance_usdc=80.0,
                     scalper_reserve_usdc=20.0,
                     min_usdc_floor=10.0)
    # available = 25 - 20 = 5 < 10 → block
    self.assertIsNotNone(gate.reason())
```

- [ ] **Step 2: Run tests to verify failure**

Run:
```bash
python3 -m unittest tests.test_trader.TestRiskGate -v
```
Expected: tests fail — `scalper_reserve_usdc` kwarg missing, `available_for_trader` missing.

- [ ] **Step 3: Add reserve to `RiskGate`**

Modify `agents/application/risk_gate.py`. In `__init__`, add the parameter and stored field:

```python
    def __init__(
        self,
        trade_log: TradeLog,
        polymarket=None,
        starting_balance_usdc: Optional[float] = None,
        max_daily_loss_pct: Optional[float] = None,
        max_trades_per_hour: Optional[int] = None,
        min_usdc_floor: Optional[float] = None,
        max_daily_token_usd: Optional[float] = None,
        kill_switch_file: Optional[str] = None,
        llm_usage_file: Optional[str] = None,
        scalper_reserve_usdc: Optional[float] = None,
    ):
        ...
        self.scalper_reserve = (
            scalper_reserve_usdc if scalper_reserve_usdc is not None
            else _env_float("SCALPER_RESERVE_USDC", 0.0)
        )
```

Add a method:

```python
    def available_for_trader(self) -> float:
        if self.polymarket is None:
            return 0.0
        bal = self.polymarket.get_usdc_balance()
        return max(0.0, bal - self.scalper_reserve)
```

In `reason()`, change the balance check from `bal` to `self.available_for_trader()`:

Locate the existing balance check (around line 95):

```python
        if self.polymarket is not None:
            try:
                bal = self.polymarket.get_usdc_balance()
            except Exception as e:
                return f"balance read failed: {e}"
            available = max(0.0, bal - self.scalper_reserve)
            if available < self.min_usdc_floor:
                return (
                    f"available {available:.4f} (after scalper reserve "
                    f"{self.scalper_reserve:.4f}) below floor {self.min_usdc_floor}"
                )
            if self.starting_balance > 0:
                drawdown = (self.starting_balance - bal) / self.starting_balance
                ...
```

(Keep drawdown using `bal`, not `available` — drawdown is total wallet, not Trader-allocated.)

- [ ] **Step 4: Trader uses `available_for_trader` for sizing**

In `agents/application/trade.py`, locate `usdc_balance = self.polymarket.get_usdc_balance()` (line 230) and replace with:

```python
        try:
            usdc_balance = self.risk_gate.available_for_trader()
        except Exception as e:
            ...
```

- [ ] **Step 5: Add scalper-side balance check**

In `agents/application/scalper.py`, before each `place_leg` call in `tick()`, add a balance check:

```python
    def _has_balance_for_leg(self) -> bool:
        try:
            bal = self.client.get_usdc_balance()
        except Exception:
            return False
        # Scalper needs leg cost × 2 (for the second leg too)
        needed = self.cfg.leg_usdc_cap * 2
        return bal >= needed
```

Insert `if not self._has_balance_for_leg(): return` at the top of each branch in `tick()` that may call `place_leg`.

- [ ] **Step 6: Run tests to verify pass**

Run:
```bash
python3 -m unittest tests.test_trader.TestRiskGate -v
python3 -m unittest tests.test_scalper_engine -v
```
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add agents/application/risk_gate.py agents/application/trade.py agents/application/scalper.py tests/test_trader.py
git commit -m "feat(risk): SCALPER_RESERVE_USDC isolates scalper capital from Trader

RiskGate now exposes available_for_trader() which subtracts the scalper
reserve from the wallet balance. Trader sizing uses available_for_trader,
and min_usdc_floor is checked against available — not raw balance.
Scalper has its own pre-leg balance check requiring leg_cost × 2."
```

---

## Task 12: `MAX_SCALP_TRADES_PER_HOUR` rate-limit gate

**Files:**
- Modify: `agents/application/scalper.py`
- Modify: `tests/test_scalper_engine.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_scalper_engine.py`:

```python
class TestScalperRateLimit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = TradeLog(db_path=self.tmp.name)
        self.dao = ScalperPairsDAO(self.log)
        self.client = MagicMock()
        self.cfg = ScalperConfig()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_rate_limit_blocks_after_max(self):
        from agents.application.trade_log import SCALPER_LEG
        # Pre-fill 60 SCALPER_LEG rows in last hour
        for i in range(60):
            self.log.insert_terminal(
                cycle_id=f"c{i}", market_id=f"m{i}", status=SCALPER_LEG)
        engine = ScalperEngine(client=self.client, log=self.log,
                                  dao=self.dao, cfg=self.cfg, execute=True,
                                  max_legs_per_hour=60)
        self.assertFalse(engine._has_rate_capacity())

    def test_rate_limit_allows_below_max(self):
        engine = ScalperEngine(client=self.client, log=self.log,
                                  dao=self.dao, cfg=self.cfg, execute=True,
                                  max_legs_per_hour=60)
        self.assertTrue(engine._has_rate_capacity())
```

- [ ] **Step 2: Run test to verify failure**

Run:
```bash
python3 -m unittest tests.test_scalper_engine.TestScalperRateLimit -v
```
Expected: tests fail — `max_legs_per_hour` kwarg + `_has_rate_capacity` missing.

- [ ] **Step 3: Implement gate**

Modify `ScalperEngine.__init__`:

```python
    def __init__(self, client, log, dao, cfg, gamma=None, execute=False,
                 max_legs_per_hour=None):
        ...
        self.max_legs_per_hour = (
            max_legs_per_hour if max_legs_per_hour is not None
            else _env_int("MAX_SCALP_TRADES_PER_HOUR", 60)
        )
```

Add method:

```python
    def _has_rate_capacity(self) -> bool:
        from agents.application.trade_log import SCALPER_LEG
        recent = self.log.count_recent(SCALPER_LEG, hours=1)
        return recent < self.max_legs_per_hour
```

Add `if not self._has_rate_capacity(): return` at the top of `tick()`.

- [ ] **Step 4: Run tests to verify pass**

Run:
```bash
python3 -m unittest tests.test_scalper_engine -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/application/scalper.py tests/test_scalper_engine.py
git commit -m "feat(scalper): MAX_SCALP_TRADES_PER_HOUR rate-limit gate

Hard cap on FAK attempts per hour, default 60. Counted via SCALPER_LEG
status rows in trades. Independent of Trader's MAX_TRADES_PER_HOUR."
```

---

## Task 13: Restart-time reconciliation — `RECONCILE_NEEDED` flow

**Files:**
- Modify: `agents/application/scalper.py`
- Modify: `agents/application/scalper_pairs.py`
- Modify: `tests/test_scalper_engine.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_scalper_engine.py`:

```python
class TestRestartReconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = TradeLog(db_path=self.tmp.name)
        self.dao = ScalperPairsDAO(self.log)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_leg1_filled_at_startup_flagged_reconcile(self):
        self.dao.create("s1", 1, "u", "d")
        self.dao.set_state("s1", ScalperState.LEG1_FILLED)
        # New engine on restart
        engine = ScalperEngine(client=MagicMock(), log=self.log,
                                  dao=self.dao, cfg=ScalperConfig(),
                                  execute=True)
        engine.reconcile_at_startup()
        self.assertEqual(self.dao.get_by_slug("s1")["state"],
                          ScalperState.RECONCILE_NEEDED)

    def test_reconcile_blocks_new_entries_for_pair(self):
        self.dao.create("s1", 1, "u", "d")
        self.dao.set_state("s1", ScalperState.RECONCILE_NEEDED)
        engine = ScalperEngine(client=MagicMock(), log=self.log,
                                  dao=self.dao, cfg=ScalperConfig(),
                                  execute=True)
        engine.add_pair(ScalpPair(slug="s1", period_ts=1, up_token="u",
                                     down_token="d", cfg=ScalperConfig()))
        # tick should silently skip
        engine.tick("s1", up_ask=0.45, down_ask=0.50, now_ms=1000)
        engine.client.execute_market_order.assert_not_called()
```

- [ ] **Step 2: Run test to verify failure**

Run:
```bash
python3 -m unittest tests.test_scalper_engine.TestRestartReconcile -v
```
Expected: 2 tests fail.

- [ ] **Step 3: Implement reconciliation**

In `agents/application/scalper.py`:

```python
    def reconcile_at_startup(self) -> int:
        """Find any LEG1_FILLED rows from prior process, mark RECONCILE_NEEDED.
        Operator must verify on-chain before clearing."""
        flipped = 0
        for row in self.dao.list_open():
            if row["state"] == ScalperState.LEG1_FILLED:
                self.dao.set_state(row["slug"],
                                     ScalperState.RECONCILE_NEEDED,
                                     error="restart_found_leg1_filled")
                flipped += 1
        if flipped:
            logger.warning("scalper: %d pair(s) flipped to RECONCILE_NEEDED — "
                           "verify on-chain positions before resuming", flipped)
        return flipped
```

Modify the `tick` early-return list at top to also include `RECONCILE_NEEDED`:

```python
        if row["state"] in (ScalperState.BOTH_FILLED, ScalperState.EXPIRED,
                              ScalperState.REDEEMED, ScalperState.SHADOW,
                              ScalperState.RECONCILE_NEEDED):
            return
```

- [ ] **Step 4: Run tests to verify pass**

Run:
```bash
python3 -m unittest tests.test_scalper_engine -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/application/scalper.py tests/test_scalper_engine.py
git commit -m "feat(scalper): restart reconciliation — LEG1_FILLED → RECONCILE_NEEDED

Analogue of MAY_HAVE_FIRED in the Trader. On restart, any pair stuck
mid-leg-2 is flipped to RECONCILE_NEEDED and the engine refuses to
trade it until an operator verifies on-chain positions and clears
the row manually."
```

---

## Task 14: Period reaping — close pairs at window expiry

**Files:**
- Modify: `agents/application/scalper.py`
- Modify: `tests/test_scalper_engine.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_scalper_engine.py`:

```python
class TestReapPeriod(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = TradeLog(db_path=self.tmp.name)
        self.dao = ScalperPairsDAO(self.log)
        self.engine = ScalperEngine(client=MagicMock(), log=self.log,
                                      dao=self.dao, cfg=ScalperConfig(),
                                      execute=True)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_reap_marks_expired_pairs(self):
        # period_ts in the past
        self.dao.create("old", period_ts=100, up_token="u", down_token="d")
        # period_ts in the future (assume now > 200)
        self.dao.create("new", period_ts=10**12, up_token="u", down_token="d")
        self.engine.reap_expired(now_ts=1000)
        self.assertEqual(self.dao.get_by_slug("old")["state"],
                          ScalperState.EXPIRED)
        self.assertEqual(self.dao.get_by_slug("new")["state"],
                          ScalperState.TRACKING)
```

- [ ] **Step 2: Run test to verify failure**

Run:
```bash
python3 -m unittest tests.test_scalper_engine.TestReapPeriod -v
```
Expected: fails.

- [ ] **Step 3: Implement `reap_expired`**

```python
    def reap_expired(self, now_ts: int) -> int:
        n = 0
        for row in self.dao.list_open():
            if row["period_ts"] and row["period_ts"] < now_ts:
                self.dao.set_state(row["slug"], ScalperState.EXPIRED)
                self.pairs.pop(row["slug"], None)
                n += 1
        return n
```

- [ ] **Step 4: Run tests to verify pass**

Run:
```bash
python3 -m unittest tests.test_scalper_engine -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/application/scalper.py tests/test_scalper_engine.py
git commit -m "feat(scalper): reap_expired marks past-window pairs as EXPIRED

Periodic call from the daemon to clean up open pairs whose 15-min window
has elapsed. Removes them from in-memory tracking so the next discovery
pass doesn't re-create them. Redemption is handled by existing scripts."
```

---

## Task 15: `EXECUTE_SCALPER` flag — shadow vs live

**Files:**
- Modify: `agents/application/scalper.py`
- Modify: `tests/test_scalper_engine.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_scalper_engine.py`:

```python
class TestShadowMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.log = TradeLog(db_path=self.tmp.name)
        self.dao = ScalperPairsDAO(self.log)
        self.client = MagicMock()
        self.cfg = ScalperConfig()
        # execute=False = shadow mode
        self.engine = ScalperEngine(client=self.client, log=self.log,
                                      dao=self.dao, cfg=self.cfg, execute=False)
        self.engine.add_pair(ScalpPair(slug="s1", period_ts=100,
                                          up_token="tok_up", down_token="tok_dn",
                                          cfg=self.cfg))

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_shadow_does_not_call_execute(self):
        self.engine.tick(slug="s1", up_ask=0.45, down_ask=0.50, now_ms=1000)
        self.engine.tick(slug="s1", up_ask=0.47, down_ask=0.50, now_ms=1100)
        self.client.execute_market_order.assert_not_called()

    def test_shadow_logs_hypothetical_pair(self):
        from agents.application.trade_log import SCALPER_LEG
        self.engine.tick(slug="s1", up_ask=0.45, down_ask=0.50, now_ms=1000)
        self.engine.tick(slug="s1", up_ask=0.47, down_ask=0.50, now_ms=1100)
        # Should write a SCALPER_LEG audit row showing what would have fired
        recent = [r for r in self.log.recent(limit=5)
                  if r["status"] == SCALPER_LEG]
        self.assertGreaterEqual(len(recent), 1)
        # The error column carries the SHADOW marker
        self.assertIn("SHADOW", recent[0]["error"] or "")
```

- [ ] **Step 2: Run test to verify failure**

Run:
```bash
python3 -m unittest tests.test_scalper_engine.TestShadowMode -v
```
Expected: fails.

- [ ] **Step 3: Wrap `place_leg` calls with shadow branch**

Modify `place_leg` in `agents/application/scalper.py`:

```python
    def place_leg(self, slug, side, token, usdc, intended_price):
        cycle_id = f"scalp:{slug}:{side}"
        if not self.execute:
            # SHADOW mode: log a hypothetical leg, don't call CLOB
            self.log.insert_terminal(
                cycle_id=cycle_id, market_id=slug, status=SCALPER_LEG,
                token_id=token, side="BUY",
                price=intended_price, size_usdc=usdc,
                error=f"SHADOW: would have fired at {intended_price:.4f}",
            )
            self.dao.record_fill(slug, side, qty=0.0, cost_usdc=0.0,
                                   fill_price=intended_price)
            return {"filled": False, "shadow": True}
        # ... existing live code from Task 8
```

- [ ] **Step 4: Run tests to verify pass**

Run:
```bash
python3 -m unittest tests.test_scalper_engine -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/application/scalper.py tests/test_scalper_engine.py
git commit -m "feat(scalper): EXECUTE_SCALPER=false runs in SHADOW mode

When execute=False, place_leg writes SCALPER_LEG audit rows annotated
with 'SHADOW: would have fired at ...' but never calls execute_market_order.
The DAO updates attempts (qty/cost remain zero), so the same trigger logic
runs and we get a true count of opportunities."
```

---

## Task 16: `ScalperDaemon` — main loop with SIGTERM, heartbeat, healthcheck ping

**Files:**
- Modify: `agents/application/scalper.py`
- Create: `tests/test_scalper_daemon.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_scalper_daemon.py`:

```python
import os
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from agents.application.scalper import ScalperDaemon, ScalperConfig
from agents.application.trade_log import TradeLog
from agents.application.scalper_pairs import ScalperPairsDAO


class TestScalperDaemon(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.heartbeat = tempfile.NamedTemporaryFile(suffix="-hb", delete=False)
        self.heartbeat.close()

    def tearDown(self):
        os.unlink(self.tmp.name)
        os.unlink(self.heartbeat.name)

    @patch("agents.application.scalper.Polymarket")
    @patch("agents.application.scalper.GammaMarketClient")
    def test_stop_signal_breaks_loop(self, gamma_mock, poly_mock):
        gamma_mock.return_value.get_events_by_tag = MagicMock(return_value=[])
        poly_mock.return_value.get_usdc_balance = MagicMock(return_value=80.0)
        daemon = ScalperDaemon(heartbeat_path=self.heartbeat.name,
                                 db_path=self.tmp.name,
                                 poll_ms=100, discover_every_sec=10)

        t = threading.Thread(target=daemon.run, daemon=True)
        t.start()
        time.sleep(0.3)
        daemon.stop()
        t.join(timeout=2)
        self.assertFalse(t.is_alive())
```

- [ ] **Step 2: Run test to verify failure**

Run:
```bash
python3 -m unittest tests.test_scalper_daemon -v
```
Expected: fails — `ScalperDaemon` does not exist.

- [ ] **Step 3: Implement `ScalperDaemon`**

Append to `agents/application/scalper.py`:

```python
import signal
import threading
import time
from pathlib import Path

from agents.polymarket.polymarket import Polymarket
from agents.polymarket.gamma import GammaMarketClient


class ScalperDaemon:
    """Long-running loop. SIGTERM-aware. One process per replica.

    Cadence:
      - poll_ms          → re-fetch order books for tracked tokens, run tick()
      - discover_every_sec → re-scan gamma for new -updown-15m- markets
    """

    def __init__(
        self,
        heartbeat_path: str = None,
        db_path: str = None,
        poll_ms: int = None,
        discover_every_sec: int = None,
        execute: bool = None,
    ):
        self.heartbeat = Path(heartbeat_path or
                                os.getenv("SCALPER_HEARTBEAT_PATH",
                                           "/app/data/scalper_heartbeat"))
        self.cfg = ScalperConfig.from_env()
        if poll_ms is not None:
            self.cfg.poll_ms = poll_ms
        if discover_every_sec is not None:
            self.cfg.discover_every_sec = discover_every_sec
        self.execute = (
            execute if execute is not None
            else os.getenv("EXECUTE_SCALPER", "false").lower() == "true"
        )
        self.tl = TradeLog(db_path=db_path)
        self.dao = ScalperPairsDAO(self.tl)
        self.client = Polymarket(live=self.execute)
        self.gamma = GammaMarketClient()
        self.engine = ScalperEngine(
            client=self.client, log=self.tl, dao=self.dao,
            cfg=self.cfg, gamma=self.gamma, execute=self.execute,
        )
        self._stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: self.stop())
        signal.signal(signal.SIGINT, lambda *_: self.stop())

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        logger.info("ScalperDaemon: starting (execute=%s)", self.execute)
        # Reconcile any leftover state from prior process
        self.engine.reconcile_at_startup()
        last_discover = 0.0
        try:
            while not self._stop.is_set():
                now = time.time()
                if now - last_discover >= self.cfg.discover_every_sec:
                    try:
                        self.engine.discover_markets()
                    except Exception:
                        logger.exception("discover_markets failed")
                    self.engine.reap_expired(now_ts=int(now))
                    last_discover = now
                # Tick all open pairs
                for row in self.dao.list_open():
                    slug = row["slug"]
                    if slug not in self.engine.pairs:
                        # Hydrate pair object from DAO row (after restart)
                        self.engine.add_pair(ScalpPair(
                            slug=slug, period_ts=row["period_ts"],
                            up_token=row["up_token"], down_token=row["down_token"],
                            cfg=self.cfg,
                        ))
                    try:
                        book_up = self.client.client.get_order_book(row["up_token"])
                        book_dn = self.client.client.get_order_book(row["down_token"])
                        ask_up = self._best_ask(book_up)
                        ask_dn = self._best_ask(book_dn)
                        if ask_up and ask_dn:
                            self.engine.tick(slug, up_ask=ask_up, down_ask=ask_dn,
                                              now_ms=int(now * 1000))
                    except Exception:
                        logger.exception("tick failed for %s", slug)
                # Heartbeat
                try:
                    self.heartbeat.parent.mkdir(parents=True, exist_ok=True)
                    self.heartbeat.touch()
                except Exception:
                    pass
                self._stop.wait(self.cfg.poll_ms / 1000.0)
        finally:
            logger.info("ScalperDaemon: exited")

    @staticmethod
    def _best_ask(book) -> Optional[float]:
        asks = getattr(book, "asks", None) if not isinstance(book, dict) \
            else book.get("asks", [])
        if not asks:
            return None
        prices = []
        for a in asks:
            if hasattr(a, "price"):
                prices.append(float(a.price))
            else:
                prices.append(float(a["price"]))
        return min(prices) if prices else None
```

Add `__main__` entry at the bottom of `scalper.py`:

```python
if __name__ == "__main__":
    import logging
    from agents.utils.logging_setup import setup_logging
    setup_logging()
    ScalperDaemon().run()
```

- [ ] **Step 4: Run tests to verify pass**

Run:
```bash
python3 -m unittest tests.test_scalper_daemon -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/application/scalper.py tests/test_scalper_daemon.py
git commit -m "feat(scalper): ScalperDaemon — SIGTERM-aware main loop

Separate process per replica. On startup, reconciles any leg1_filled
rows. Then alternates between gamma discovery (every N seconds) and
fast book-polling (poll_ms cadence). Touches scalper_heartbeat for
docker healthcheck. Reads EXECUTE_SCALPER to choose live vs shadow."
```

---

## Task 17: docker-compose service

**Files:**
- Modify: `docker-compose.yml`
- Modify: `Dockerfile` (only if needed — likely no changes)

- [ ] **Step 1: Add scalper service**

Modify `docker-compose.yml` to append after the existing `trader` service:

```yaml
  scalper:
    build: .
    image: poly1:local
    container_name: poly1-scalper
    restart: unless-stopped
    init: true
    env_file: .env
    environment:
      TZ: UTC
    command: ["python", "-m", "agents.application.scalper"]
    volumes:
      - ./data:/app/data
    healthcheck:
      test: ["CMD", "python", "-c",
             "import os, time; assert time.time() - os.path.getmtime('/app/data/scalper_heartbeat') < 30"]
      interval: 30s
      timeout: 5s
      retries: 3
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
    mem_limit: 512m
    cpus: "0.5"
    stop_grace_period: 30s
    profiles: ["scalper"]
```

The `profiles: ["scalper"]` ensures the scalper does NOT auto-start with `docker compose up`. To run it: `docker compose --profile scalper up -d scalper`. This is intentional for Stage 0 — scalper is opt-in until validated.

- [ ] **Step 2: Smoke test**

Run:
```bash
docker compose build scalper
docker compose --profile scalper run --rm scalper python -c \
  "from agents.application.scalper import ScalperConfig; print(ScalperConfig.from_env())"
```
Expected: prints a `ScalperConfig(...)` line with defaults.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(scalper): docker-compose service with profile gate

Scalper runs as a second container, sharing ./data with the Trader.
Behind the 'scalper' profile so 'docker compose up' won't start it
until explicitly enabled. Heartbeat file at /app/data/scalper_heartbeat
gates the healthcheck."
```

---

## Task 18: CLI inspector — `scalper_inspect.py`

**Files:**
- Create: `scripts/python/scalper_inspect.py`
- Modify: `tests/test_scalper_pairs.py` (add a CLI smoke)

- [ ] **Step 1: Implement inspector**

Create `scripts/python/scalper_inspect.py`:

```python
"""CLI: list recent scalper pairs and a P&L summary."""
import argparse
import sys
from pathlib import Path

# Make package importable when run via `python scripts/python/scalper_inspect.py`
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agents.application.trade_log import TradeLog, SCALPER_LEG
from agents.application.scalper_pairs import ScalperPairsDAO


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    tl = TradeLog(db_path=args.db) if args.db else TradeLog()
    dao = ScalperPairsDAO(tl)

    rows = dao.list_recent(limit=args.limit)
    print(f"--- last {len(rows)} scalper pairs ---")
    for r in rows:
        net_cost = r["cost_up"] + r["cost_down"]
        sum_avg = "n/a"
        if r["qty_up"] > 0 and r["qty_down"] > 0:
            avg_up = r["cost_up"] / r["qty_up"]
            avg_dn = r["cost_down"] / r["qty_down"]
            sum_avg = f"{avg_up + avg_dn:.4f}"
        print(f"  {r['slug']:<40s} state={r['state']:<14s} "
              f"qty=({r['qty_up']:.2f},{r['qty_down']:.2f}) "
              f"cost=${net_cost:.2f} sum_avg={sum_avg}")

    legs = tl.recent(limit=args.limit * 4)
    scalper_legs = [l for l in legs if l["status"] == SCALPER_LEG]
    spent = sum(l["size_usdc"] or 0 for l in scalper_legs)
    print(f"\n--- {len(scalper_legs)} scalper legs (last {args.limit*4} rows) ---")
    print(f"Total spent: ${spent:.2f}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke test**

Run:
```bash
docker compose run --rm trader python scripts/python/scalper_inspect.py --limit 5
```
Expected: prints headers, possibly empty body if no pairs yet. No error.

- [ ] **Step 3: Commit**

```bash
git add scripts/python/scalper_inspect.py
git commit -m "feat(scalper): scalper_inspect CLI — pair state + leg spend

Read-only view of scalper_pairs and SCALPER_LEG rows. Shows state, qty,
cost, and the critical sum_avg metric (must be < max_sum_avg=0.98 for
the pair to be profitable on settlement)."
```

---

## Task 19: env vars + docs update

**Files:**
- Modify: `.env.example`
- Modify: `SPEC.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Append scalper env vars to `.env.example`**

```
# --- Scalper (Strategy C — short-duration crypto Up/Down) ---
EXECUTE_SCALPER="false"        # flip to true after Stage-0 validation
SCALPER_RESERVE_USDC="20"      # hard reserve, Trader cannot touch
MAX_SCALP_TRADES_PER_HOUR="60"
SCALP_LEG_USDC="2.5"           # Stage-0/1 leg size; raise in Stage 2
SCALP_ENTRY_THRESHOLD="0.499"
SCALP_REVERSAL_DELTA="0.020"
SCALP_DEPTH_DISCOUNT="0.05"
SCALP_SECOND_BUFFER="0.01"
SCALP_SECOND_TIME_MS="200"
SCALP_DYNAMIC_BOOST="0.04"
SCALP_MAX_SUM_AVG="0.97"       # Stage-1 starts tighter than TS default 0.98
SCALP_MAX_BUYS_PER_SIDE="4"
SCALP_POLL_MS="500"            # Stage-0 starts loose; tighten if needed
SCALP_DISCOVER_EVERY_SEC="60"
SCALPER_HEARTBEAT_PATH="/app/data/scalper_heartbeat"
```

- [ ] **Step 2: Add §15 to `SPEC.md`**

Append a new section:

```markdown
## 15. Scalper (Strategy C — short-duration crypto Up/Down)

Independent module that runs in its own container alongside Trader.
Targets `*-updown-15m-*` markets via FAK market BUYs. No LLM. See
`docs/STRATEGY_C_SCALPING_SPEC.md` for the algorithm reference and
`docs/superpowers/plans/2026-05-05-scalper-strategy-c.md` for the build
log.

### Modules

| Module | Responsibility |
|---|---|
| `agents/application/scalper.py` | `ScalpPair`, `ScalperEngine`, `ScalperDaemon`, `__main__` |
| `agents/application/scalper_pairs.py` | `ScalperPairsDAO` — `scalper_pairs` CRUD |

### Storage

New `scalper_pairs` table in the existing `trade_log.db` (WAL mode).
Each FAK attempt also writes a `SCALPER_LEG` row to the existing
`trades` table for audit/PnL.

### Capital isolation

`SCALPER_RESERVE_USDC` reserves a fixed sub-balance for the scalper.
`RiskGate.available_for_trader()` returns `balance - reserve`. The
scalper itself reads the wallet balance directly and refuses to enter
new pairs when `balance < leg_cost × 2`.

### Operational stages

Stage 0 (shadow): `EXECUTE_SCALPER=false`, 2-3 days. Sanity check that
triggers fire and pair counts are non-trivial.

Stage 1 (live small): `EXECUTE_SCALPER=true`, leg=$2.50, **min 2 weeks**.
Abort if cumulative PnL < -$15 at any point.

Stage 2 (scale): leg=$5+ only after Stage 1 ends positive.
```

- [ ] **Step 3: Update `CLAUDE.md`**

Find the section "What's intentionally NOT in scope (don't add without discussion)":

Remove the line:
```
- Multi-strategy plug-in framework.
```

(Multi-strategy is now in scope via the scalper. Adaptive sizing remains out of scope.)

Add a new section above "Versioning":

```markdown
## Scalper module (Strategy C)

The scalper is a SECOND, INDEPENDENT trading agent in this repo. It runs
in its own container (`profiles: scalper` in docker-compose) and shares
only the SQLite ledger and the Polymarket wallet with the Trader. Capital
isolation is enforced by `SCALPER_RESERVE_USDC`.

When working on scalper code:

- Do not couple scalper logic to the Trader's LLM pipeline. The whole
  point of the scalper is that it runs without LLM calls.
- The dedupe contract for the scalper is `scalper_pairs.state`, NOT
  `ACTIVE_STATUSES`. Adding `SCALPER_LEG` to `ACTIVE_STATUSES` would
  break the Trader's dedupe of unrelated markets.
- `RECONCILE_NEEDED` is to the scalper what `MAY_HAVE_FIRED` is to the
  Trader: do not auto-clear it; the operator must verify on-chain.
```

- [ ] **Step 4: Smoke test the documentation rendering**

Run:
```bash
grep -n "Scalper" /Users/mymac/coding/poly1/SPEC.md
grep -n "Scalper" /Users/mymac/coding/poly1/CLAUDE.md
```
Expected: at least one match in each file.

- [ ] **Step 5: Commit**

```bash
git add .env.example SPEC.md CLAUDE.md
git commit -m "docs(scalper): SPEC §15, CLAUDE.md guidance, env vars

Documents the scalper module, its storage, and the Stage 0 → 1 → 2
operational sequence with the abort condition for Stage 1.
.env.example gets the full list of scalper-prefixed knobs.
CLAUDE.md gets a 'no LLM in scalper' invariant + dedupe contract
clarification for the scalper_pairs table."
```

---

## Task 20: Final integration smoke + Stage-0 runbook

**Files:**
- Create: `docs/SCALPER_STAGE_0_RUNBOOK.md`

- [ ] **Step 1: Run the full test suite**

```bash
docker compose build
docker compose run --rm trader python -m unittest discover -s tests -v
```
Expected: all tests PASS (existing + new).

- [ ] **Step 2: Black formatter check**

```bash
docker compose run --rm trader black --check .
```
Expected: no diff. If any new file needs formatting:
```bash
docker compose run --rm trader black agents/application/scalper.py \
    agents/application/scalper_pairs.py tests/test_scalper.py \
    tests/test_scalper_pairs.py tests/test_scalper_engine.py \
    tests/test_scalper_daemon.py scripts/python/scalper_inspect.py
```

- [ ] **Step 3: Write the Stage-0 runbook**

Create `docs/SCALPER_STAGE_0_RUNBOOK.md`:

```markdown
# Scalper Stage 0 — Shadow Mode Runbook

Goal: validate that the trigger logic fires on real markets. NOT a
profitability test.

## Pre-launch

- [ ] `.env` includes `EXECUTE_SCALPER="false"`.
- [ ] `.env` includes `SCALPER_RESERVE_USDC="20"`.
- [ ] `data/HALT` does NOT exist.
- [ ] Trader has been running cleanly for ≥24h (no MAY_HAVE_FIRED).
- [ ] `git tag stage0-scalper-shadow-$(date -u +%Y%m%d-%H%M)` and push.

## Launch

```bash
docker compose --profile scalper up -d scalper
docker compose logs -f scalper
```

## Daily checks (each day for 2-3 days)

- `docker compose run --rm trader python scripts/python/scalper_inspect.py --limit 50`
  - Did pairs get created? (rows under "last X scalper pairs")
  - How many SHADOW legs? (under "scalper legs")
- `docker compose ps` — both containers `Up`, healthcheck passing.
- `docker compose logs scalper --since 1h | grep -E "ERROR|exception"` — should be empty.

## Pass criteria for moving to Stage 1

| Criterion | Threshold |
|-----------|-----------|
| Pairs created per day | ≥ 5 |
| SHADOW legs that satisfied profit gate | ≥ 8/day |
| RECONCILE_NEEDED rows | 0 |
| Unhandled exceptions in scalper logs | 0 |
| Heartbeat staleness ever > 30s | No |

If all pass after 48h, proceed to Stage 1 (a separate runbook).

## Rollback

```bash
docker compose --profile scalper stop scalper
# Pairs in non-terminal state remain in the table. Mark them:
docker compose run --rm trader sqlite3 data/trade_log.db \
    "UPDATE scalper_pairs SET state='shadow' WHERE state IN ('tracking','leg1_filled');"
```
```

- [ ] **Step 4: Commit**

```bash
git add docs/SCALPER_STAGE_0_RUNBOOK.md
git commit -m "docs(scalper): Stage-0 shadow-mode runbook

Pre-launch checklist, daily checks, pass criteria for moving to Stage 1,
and a rollback procedure. Stage 0 is a sanity check that triggers fire,
NOT a profitability test."
```

- [ ] **Step 5: Tag**

```bash
git tag v0.3.0-scalper-built
```

---

## Self-review notes (auto-generated, fix-in-place)

- ✓ All tests have actual code, not "write tests"
- ✓ All file paths absolute or unambiguous repo-relative
- ✓ FAK is added behind a kwarg; FOK remains the default — no Trader behavior change
- ✓ `SCALPER_RESERVE_USDC` is enforced symmetrically (Trader sizing AND scalper pre-leg)
- ✓ Restart reconciliation modeled on `MAY_HAVE_FIRED` — do-not-auto-clear pattern preserved
- ✓ Storage decision is `scalper_pairs` table, NOT a new status in `ACTIVE_STATUSES`
- ✓ `EXECUTE_SCALPER` is independent of `EXECUTE` — Trader and scalper toggled separately
- ✓ Stage-1 abort condition is explicit: `cumulative PnL < -$15`
- ✓ Stage-1 minimum duration is documented as **2 weeks**, not 3-5 days
- ⚠️  License: PoDev TS used as algorithmic reference only; clean re-implementation. User confirmed personal use.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-05-scalper-strategy-c.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Best for a 20-task plan like this.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
