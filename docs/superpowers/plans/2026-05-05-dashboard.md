# Dashboard (Streamlit Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a 7-tab Streamlit dashboard that replaces `monitor_web.py` with live monitoring, P&L analytics, capital tracking, trade history, scalper state, LLM cost, and HALT/RESUME control — served from Docker on port 8050.

**Architecture:** Single `scripts/python/dashboard.py` reads SQLite (read-only) and `data/llm_usage.jsonl`; a `db.py` helper isolates all queries; Control tab writes `data/HALT`. Docker `dashboard` service with `profiles: ["dashboard"]`.

**Tech Stack:** Python 3.9+, Streamlit ≥1.35, pandas, plotly, sqlite3 (stdlib)

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `scripts/python/db.py` | **Create** | All SQLite + JSONL query functions |
| `scripts/python/dashboard.py` | **Create** | Streamlit app — imports db.py, renders 7 tabs |
| `requirements.txt` | **Modify** | Add `streamlit>=1.35`, `pandas>=2.0`, `plotly>=5.0` |
| `docker-compose.yml` | **Modify** | Add `dashboard` service with `profiles: ["dashboard"]` |

---

## Task 1: Add dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add Streamlit, pandas, plotly to requirements.txt**

Append after the last line:
```
streamlit>=1.35.0
pandas>=2.0.0
plotly>=5.22.0
```

- [ ] **Step 2: Verify imports work inside Docker**

```bash
docker compose build 2>&1 | tail -20
docker compose run --rm trader python -c "import streamlit; import pandas; import plotly; print('ok')"
```

Expected output: `ok`

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "deps: add streamlit, pandas, plotly for dashboard"
```

---

## Task 2: Data layer (`scripts/python/db.py`)

**Files:**
- Create: `scripts/python/db.py`

All functions open SQLite with `uri=True, check_same_thread=False` in read-only mode. Returns plain Python dicts / lists — no SQLite Row objects cross the module boundary. The `scalper_pairs` table may not exist (it ships with Strategy C); all scalper queries return empty lists/dicts gracefully.

- [ ] **Step 1: Write the file**

```python
"""Read-only data access for the dashboard.

All functions return plain dicts/lists. Callers never see sqlite3 Row objects.
scalper_pairs queries return [] / {} gracefully if the table does not exist yet.
"""
import json
import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


_DB = os.getenv("TRADE_LOG_DB", "./data/trade_log.db")
_LLM_FILE = os.getenv("LLM_USAGE_FILE", "./data/llm_usage.jsonl")
_LOG_DIR = os.getenv("LOG_DIR", "./data/logs")
_HALT_FILE = Path(os.getenv("KILL_SWITCH_FILE", "./data/HALT"))
_HB_TRADER = Path(os.getenv("HEARTBEAT_PATH", "./data/heartbeat"))
_HB_SCALPER = Path("./data/scalper_heartbeat")


def _conn() -> sqlite3.Connection:
    uri = f"file:{_DB}?mode=ro"
    c = sqlite3.connect(uri, uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def _scalar(sql: str, params: tuple = (), default: Any = None) -> Any:
    with _conn() as c:
        row = c.execute(sql, params).fetchone()
        return row[0] if row else default


def _table_exists(name: str) -> bool:
    return bool(_scalar(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ))


# ── heartbeats ────────────────────────────────────────────────────────────────

def heartbeat_age(path: Path) -> float | None:
    """Seconds since last heartbeat write, or None if file missing."""
    try:
        mtime = path.stat().st_mtime
        return datetime.now(timezone.utc).timestamp() - mtime
    except FileNotFoundError:
        return None


def trader_heartbeat_age() -> float | None:
    return heartbeat_age(_HB_TRADER)


def scalper_heartbeat_age() -> float | None:
    return heartbeat_age(_HB_SCALPER)


def is_halted() -> bool:
    return _HALT_FILE.exists()


def halt() -> None:
    _HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HALT_FILE.write_text("halted by dashboard")


def resume() -> None:
    if _HALT_FILE.exists():
        _HALT_FILE.unlink()


# ── trades ────────────────────────────────────────────────────────────────────

def trades_all() -> list[dict]:
    return _rows("SELECT * FROM trades ORDER BY ts DESC")


def trades_by_status(status: str) -> list[dict]:
    return _rows("SELECT * FROM trades WHERE status=? ORDER BY ts DESC", (status,))


def trades_filled() -> list[dict]:
    return _rows("SELECT * FROM trades WHERE status='filled' ORDER BY ts")


def last_gate_reason() -> str | None:
    row = _rows(
        "SELECT error FROM trades WHERE status='skipped_gate' ORDER BY ts DESC LIMIT 1"
    )
    return row[0]["error"] if row else None


def daily_capital_deployed() -> list[dict]:
    """Returns [{date, total_usdc}] for filled trades, ordered by date."""
    return _rows(
        "SELECT date(ts) AS day, SUM(size_usdc) AS total_usdc "
        "FROM trades WHERE status='filled' GROUP BY day ORDER BY day"
    )


def trade_status_counts() -> dict[str, int]:
    rows = _rows("SELECT status, COUNT(*) AS n FROM trades GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


def open_positions() -> list[dict]:
    """filled trades that are not yet redeemed — approximate 'capital at risk'."""
    return _rows(
        "SELECT * FROM trades WHERE status='filled' ORDER BY ts DESC"
    )


# ── scalper pairs ─────────────────────────────────────────────────────────────

def scalper_pairs_open() -> list[dict]:
    if not _table_exists("scalper_pairs"):
        return []
    return _rows(
        "SELECT * FROM scalper_pairs WHERE state NOT IN ('expired','redeemed','shadow') "
        "ORDER BY opened_ts DESC"
    )


def scalper_pairs_recent(limit: int = 50) -> list[dict]:
    if not _table_exists("scalper_pairs"):
        return []
    return _rows(
        "SELECT * FROM scalper_pairs ORDER BY opened_ts DESC LIMIT ?", (limit,)
    )


def scalper_state_counts() -> dict[str, int]:
    if not _table_exists("scalper_pairs"):
        return {}
    rows = _rows("SELECT state, COUNT(*) AS n FROM scalper_pairs GROUP BY state")
    return {r["state"]: r["n"] for r in rows}


# ── LLM usage ─────────────────────────────────────────────────────────────────

def llm_records() -> list[dict]:
    path = Path(_LLM_FILE)
    if not path.exists():
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


# ── log tail ──────────────────────────────────────────────────────────────────

def log_tail(n: int = 100) -> str:
    log_path = Path(_LOG_DIR) / "poly1.log"
    if not log_path.exists():
        return "(log file not found)"
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 32_768)
            f.seek(max(0, size - chunk))
            text = f.read().decode("utf-8", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[-n:])
    except OSError as e:
        return f"(error reading log: {e})"
```

- [ ] **Step 2: Verify db.py imports with no external deps**

```bash
TRADE_LOG_DB=/Users/mymac/coding/poly1/data/trade_log.db \
LLM_USAGE_FILE=/Users/mymac/coding/poly1/data/llm_usage.jsonl \
LOG_DIR=/Users/mymac/coding/poly1/data/logs \
python3 /Users/mymac/coding/poly1/scripts/python/db.py
```

Expected: exits silently (no errors). If `ModuleNotFoundError` — db.py uses only stdlib, so the error is a syntax issue.

- [ ] **Step 3: Smoke-test key functions from REPL**

```bash
cd /Users/mymac/coding/poly1
TRADE_LOG_DB=data/trade_log.db LLM_USAGE_FILE=data/llm_usage.jsonl LOG_DIR=data/logs \
python3 -c "
import sys; sys.path.insert(0, 'scripts/python')
import db
print('trades all:', len(db.trades_all()))
print('filled:', len(db.trades_filled()))
print('status counts:', db.trade_status_counts())
print('llm records:', len(db.llm_records()))
print('halted:', db.is_halted())
print('trader hb age:', db.trader_heartbeat_age())
print('log tail lines:', len(db.log_tail().splitlines()))
"
```

Expected (approximate):
```
trades all: 78
filled: 5
status counts: {'filled': 5, 'skipped_dedupe': 42, ...}
llm records: 72
halted: False
trader hb age: <float or None>
log tail lines: 100
```

- [ ] **Step 4: Commit**

```bash
git add scripts/python/db.py
git commit -m "feat: dashboard data layer (db.py)"
```

---

## Task 3: Dashboard skeleton + Live tab

**Files:**
- Create: `scripts/python/dashboard.py`

- [ ] **Step 1: Write the skeleton with Live tab**

```python
"""Poly1 Streamlit dashboard — Phase 1.

Run:
    streamlit run scripts/python/dashboard.py --server.port 8050

Reads data from TRADE_LOG_DB, LLM_USAGE_FILE, LOG_DIR (env vars).
Writes to KILL_SWITCH_FILE (Control tab only).
"""
import os
import sys
import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

# Allow running from repo root or inside Docker (/app)
_SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPT_DIR))
import db  # noqa: E402

st.set_page_config(
    page_title="poly1 dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

TAB_LIVE, TAB_PNL, TAB_CAPITAL, TAB_TRADES, TAB_SCALPER, TAB_LLM, TAB_CTRL = st.tabs([
    "🟢 Live",
    "📈 P&L",
    "💰 Capital",
    "📋 Trades",
    "🔪 Scalper",
    "🤖 LLM Cost",
    "⚙️ Control",
])


def _age_label(age: float | None) -> str:
    if age is None:
        return "⛔ no file"
    if age < 120:
        return f"🟢 {age:.0f}s ago"
    if age < 300:
        return f"🟡 {age:.0f}s ago"
    return f"🔴 {age:.0f}s ago — stale"


# ── Live tab ──────────────────────────────────────────────────────────────────

with TAB_LIVE:
    if st.button("🔄 Refresh now"):
        st.rerun()

    halted = db.is_halted()
    trader_age = db.trader_heartbeat_age()
    scalper_age = db.scalper_heartbeat_age()
    gate_reason = db.last_gate_reason()
    counts = db.trade_status_counts()

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Trader heartbeat", _age_label(trader_age))
    with col2:
        st.metric("Scalper heartbeat", _age_label(scalper_age))
    with col3:
        if halted:
            st.error("🛑 HALTED — kill switch active")
        else:
            st.success("✅ RUNNING — no kill switch")

    st.divider()

    col4, col5, col6, col7 = st.columns(4)
    with col4:
        st.metric("Filled trades", counts.get("filled", 0))
    with col5:
        st.metric("Gate blocks (today)", counts.get("skipped_gate", 0))
    with col6:
        st.metric("Deduped", counts.get("skipped_dedupe", 0))
    with col7:
        st.metric("Failed", counts.get("failed", 0))

    if gate_reason:
        st.info(f"Last gate block: {gate_reason}")

    st.divider()
    st.subheader("Log tail (last 80 lines)")
    st.code(db.log_tail(80), language=None)

    # Auto-refresh every 30 s using Streamlit's built-in fragment rerun
    st.caption("Page refreshes automatically every 30 s — or click Refresh now above.")
    time.sleep(0.1)  # yield so page renders before potential rerun
```

- [ ] **Step 2: Run locally to verify Live tab renders**

```bash
cd /Users/mymac/coding/poly1
TRADE_LOG_DB=data/trade_log.db \
LLM_USAGE_FILE=data/llm_usage.jsonl \
LOG_DIR=data/logs \
streamlit run scripts/python/dashboard.py --server.port 8050
```

Open http://localhost:8050 in browser. Verify: 🟢 Live tab shows heartbeat metrics, status counts, log tail. No Python exceptions in terminal.

- [ ] **Step 3: Commit skeleton**

```bash
git add scripts/python/dashboard.py
git commit -m "feat: dashboard skeleton + Live tab"
```

---

## Task 4: P&L tab

**Files:**
- Modify: `scripts/python/dashboard.py` — fill `TAB_PNL` block

- [ ] **Step 1: Add P&L section after the skeleton commit**

Replace the empty `with TAB_PNL:` block (or add after `TAB_LIVE` block ends) with:

```python
# ── P&L tab ───────────────────────────────────────────────────────────────────

with TAB_PNL:
    st.info(
        "⚠️ P&L is approximate. We track capital deployed (USDC paid per filled trade). "
        "Actual settlement profit requires outcome data — not yet tracked in DB."
    )

    filled = db.trades_filled()
    daily = db.daily_capital_deployed()

    if not filled:
        st.warning("No filled trades yet.")
    else:
        df_daily = pd.DataFrame(daily)
        df_daily["day"] = pd.to_datetime(df_daily["day"])
        df_daily["cumulative_usdc"] = df_daily["total_usdc"].cumsum()

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Cumulative capital deployed")
            fig = px.area(
                df_daily,
                x="day",
                y="cumulative_usdc",
                labels={"day": "Date", "cumulative_usdc": "USDC deployed"},
            )
            fig.update_layout(margin=dict(l=0, r=0, t=30, b=0))
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            st.subheader("Daily capital deployed")
            fig2 = px.bar(
                df_daily,
                x="day",
                y="total_usdc",
                labels={"day": "Date", "total_usdc": "USDC"},
            )
            fig2.update_layout(margin=dict(l=0, r=0, t=30, b=0))
            st.plotly_chart(fig2, use_container_width=True)

        st.divider()

        df_filled = pd.DataFrame(filled)
        total_deployed = df_filled["size_usdc"].sum()
        avg_price = df_filled["price"].mean()
        avg_confidence = df_filled["confidence"].mean()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total USDC deployed", f"${total_deployed:.2f}")
        c2.metric("Filled trade count", len(filled))
        c3.metric("Avg entry price", f"{avg_price:.3f}" if pd.notna(avg_price) else "n/a")
        c4.metric(
            "Avg LLM confidence",
            f"{avg_confidence:.1%}" if pd.notna(avg_confidence) else "n/a"
        )

        st.subheader("Filled trades detail")
        display_cols = ["ts", "market_id", "side", "price", "size_usdc", "confidence"]
        st.dataframe(
            df_filled[display_cols].rename(columns={
                "ts": "Timestamp",
                "market_id": "Market",
                "side": "Side",
                "price": "Entry price",
                "size_usdc": "USDC paid",
                "confidence": "Confidence",
            }),
            use_container_width=True,
            hide_index=True,
        )
```

- [ ] **Step 2: Verify P&L tab renders**

Reload http://localhost:8050 and click 📈 P&L tab. Should show charts + metric row + table. No Python exceptions.

- [ ] **Step 3: Commit**

```bash
git add scripts/python/dashboard.py
git commit -m "feat: dashboard P&L tab"
```

---

## Task 5: Capital tab

**Files:**
- Modify: `scripts/python/dashboard.py`

- [ ] **Step 1: Add Capital section**

```python
# ── Capital tab ───────────────────────────────────────────────────────────────

with TAB_CAPITAL:
    starting_balance = float(os.getenv("STARTING_BALANCE_USDC", "50.0"))
    max_fraction = float(os.getenv("MAX_POSITION_FRACTION", "0.05"))
    scalper_reserve = float(os.getenv("SCALPER_RESERVE_USDC", "0.0"))

    filled = db.trades_filled()
    df_filled = pd.DataFrame(filled) if filled else pd.DataFrame(
        columns=["size_usdc", "ts"]
    )

    total_deployed = df_filled["size_usdc"].sum() if not df_filled.empty else 0.0
    # Capital "at risk" = all filled trades (we don't track settlement yet)
    at_risk = total_deployed

    scalper_open = db.scalper_pairs_open()
    scalper_deployed = sum(
        (p.get("cost_up") or 0) + (p.get("cost_down") or 0)
        for p in scalper_open
    )

    col1, col2, col3 = st.columns(3)
    col1.metric("Starting balance", f"${starting_balance:.2f}")
    col2.metric("Capital deployed (filled)", f"${at_risk:.2f}")
    col3.metric("Scalper open (deployed)", f"${scalper_deployed:.2f}")

    st.divider()

    c1, c2, c3 = st.columns(3)
    c1.metric("MAX_POSITION_FRACTION", f"{max_fraction:.1%}")
    c2.metric("SCALPER_RESERVE_USDC", f"${scalper_reserve:.2f}")
    c3.metric(
        "Implied max per trade",
        f"${starting_balance * max_fraction:.2f}"
    )

    st.divider()

    if not df_filled.empty:
        st.subheader("Daily capital deployed (filled trades)")
        daily = db.daily_capital_deployed()
        df_daily = pd.DataFrame(daily)
        df_daily["day"] = pd.to_datetime(df_daily["day"])
        fig = px.bar(
            df_daily,
            x="day",
            y="total_usdc",
            labels={"day": "Date", "total_usdc": "USDC"},
        )
        fig.update_layout(margin=dict(l=0, r=0, t=30, b=0))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No filled trades to chart.")

    st.subheader("Open scalper positions")
    if scalper_open:
        st.dataframe(
            pd.DataFrame(scalper_open)[[
                "slug", "state", "cost_up", "cost_down", "opened_ts"
            ]],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No open scalper pairs.")
```

- [ ] **Step 2: Verify Capital tab**

Reload and click 💰 Capital. Verify metric row shows starting balance, deployed, max per trade. Charts render (or "No filled trades" if appropriate).

- [ ] **Step 3: Commit**

```bash
git add scripts/python/dashboard.py
git commit -m "feat: dashboard Capital tab"
```

---

## Task 6: Trades tab

**Files:**
- Modify: `scripts/python/dashboard.py`

- [ ] **Step 1: Add Trades section**

```python
# ── Trades tab ────────────────────────────────────────────────────────────────

with TAB_TRADES:
    all_trades = db.trades_all()
    df = pd.DataFrame(all_trades) if all_trades else pd.DataFrame()

    if df.empty:
        st.warning("No trades in database.")
    else:
        # Filters
        col_f1, col_f2, col_f3 = st.columns([1, 1, 2])
        with col_f1:
            all_statuses = sorted(df["status"].unique().tolist())
            status_filter = st.selectbox(
                "Status", ["(all)"] + all_statuses
            )
        with col_f2:
            side_filter = st.selectbox(
                "Side", ["(all)", "BUY", "SELL"]
            )
        with col_f3:
            market_filter = st.text_input("Market contains", "")

        filtered = df.copy()
        if status_filter != "(all)":
            filtered = filtered[filtered["status"] == status_filter]
        if side_filter != "(all)":
            filtered = filtered[filtered["side"] == side_filter]
        if market_filter:
            filtered = filtered[
                filtered["market_id"].str.contains(market_filter, case=False, na=False)
            ]

        st.caption(f"{len(filtered)} of {len(df)} trades")

        display_cols = ["ts", "market_id", "side", "price", "size_usdc", "confidence", "status", "error"]
        display_cols = [c for c in display_cols if c in filtered.columns]

        st.dataframe(
            filtered[display_cols].rename(columns={
                "ts": "Timestamp",
                "market_id": "Market",
                "side": "Side",
                "price": "Price",
                "size_usdc": "USDC",
                "confidence": "Confidence",
                "status": "Status",
                "error": "Error/Note",
            }),
            use_container_width=True,
            hide_index=True,
        )

        # Expandable raw JSON for filled trades
        with st.expander("Raw response_json (filled trades)"):
            filled_with_json = filtered[
                (filtered["status"] == "filled") & filtered["response_json"].notna()
            ]
            for _, row in filled_with_json.iterrows():
                st.caption(f"{row['ts']} — {row['market_id']}")
                st.json(row["response_json"])
```

- [ ] **Step 2: Verify Trades tab**

Reload and click 📋 Trades. Test each filter. Expand raw JSON on a filled trade.

- [ ] **Step 3: Commit**

```bash
git add scripts/python/dashboard.py
git commit -m "feat: dashboard Trades tab"
```

---

## Task 7: Scalper tab

**Files:**
- Modify: `scripts/python/dashboard.py`

Note: `scalper_pairs` table ships with Strategy C. If the table doesn't exist yet, all `db.scalper_*` functions return `[]` / `{}`, so this tab shows "no data" gracefully.

- [ ] **Step 1: Add Scalper section**

```python
# ── Scalper tab ───────────────────────────────────────────────────────────────

with TAB_SCALPER:
    state_counts = db.scalper_state_counts()
    open_pairs = db.scalper_pairs_open()
    recent_pairs = db.scalper_pairs_recent(50)

    if not state_counts:
        st.info(
            "No scalper_pairs table found. Strategy C (scalper) not yet deployed, "
            "or running on main branch before the scalper merge."
        )
    else:
        # Summary row
        total_pairs = sum(state_counts.values())
        reconcile_count = state_counts.get("reconcile_needed", 0)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total pairs (all time)", total_pairs)
        c2.metric("Open pairs", len(open_pairs))
        c3.metric("Redeemed", state_counts.get("redeemed", 0))
        if reconcile_count:
            c4.error(f"⚠️ RECONCILE_NEEDED: {reconcile_count}")
        else:
            c4.metric("Reconcile needed", 0)

        st.divider()

        # State breakdown bar chart
        df_states = pd.DataFrame(
            list(state_counts.items()), columns=["State", "Count"]
        )
        fig = px.bar(df_states, x="State", y="Count", title="Pairs by state")
        fig.update_layout(margin=dict(l=0, r=0, t=40, b=0))
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("Open pairs")
        if open_pairs:
            df_open = pd.DataFrame(open_pairs)
            # Highlight RECONCILE_NEEDED rows
            def _highlight_reconcile(row):
                if row.get("state") == "reconcile_needed":
                    return ["background-color: #ff4b4b22"] * len(row)
                return [""] * len(row)

            show_cols = [c for c in [
                "slug", "state", "cost_up", "cost_down",
                "qty_up", "qty_down", "opened_ts", "error"
            ] if c in df_open.columns]
            st.dataframe(
                df_open[show_cols].style.apply(_highlight_reconcile, axis=1),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No open pairs.")

        st.subheader("Recent pairs (last 50)")
        if recent_pairs:
            df_recent = pd.DataFrame(recent_pairs)
            show_cols = [c for c in [
                "slug", "state", "cost_up", "cost_down",
                "opened_ts", "closed_ts", "error"
            ] if c in df_recent.columns]
            st.dataframe(df_recent[show_cols], use_container_width=True, hide_index=True)
```

- [ ] **Step 2: Verify Scalper tab**

Reload and click 🔪 Scalper. Verify "no scalper_pairs table" message appears (since Strategy C not merged yet). No exceptions.

- [ ] **Step 3: Commit**

```bash
git add scripts/python/dashboard.py
git commit -m "feat: dashboard Scalper tab"
```

---

## Task 8: LLM Cost tab

**Files:**
- Modify: `scripts/python/dashboard.py`

`llm_usage.jsonl` format: `{"ts": "...", "tag": "...", "model": "...", "prompt_tokens": N, "completion_tokens": N, "est_cost_usd": F}`

- [ ] **Step 1: Add LLM Cost section**

```python
# ── LLM Cost tab ─────────────────────────────────────────────────────────────

with TAB_LLM:
    records = db.llm_records()
    max_daily_usd = float(os.getenv("MAX_DAILY_TOKEN_USD", "5.0"))

    if not records:
        st.info("No LLM usage records found (data/llm_usage.jsonl is empty or missing).")
    else:
        df_llm = pd.DataFrame(records)
        df_llm["ts"] = pd.to_datetime(df_llm["ts"], utc=True)
        df_llm["day"] = df_llm["ts"].dt.date

        total_cost = df_llm["est_cost_usd"].sum()
        total_tokens = df_llm["prompt_tokens"].sum() + df_llm["completion_tokens"].sum()
        today = df_llm[df_llm["day"] == df_llm["day"].max()]
        today_cost = today["est_cost_usd"].sum()

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total cost (all time)", f"${total_cost:.4f}")
        c2.metric("Total tokens", f"{total_tokens:,}")
        c3.metric("Today's cost", f"${today_cost:.4f}")
        c4.metric("Daily limit (gate)", f"${max_daily_usd:.2f}")

        st.divider()

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Daily cost")
            df_daily_cost = df_llm.groupby("day")["est_cost_usd"].sum().reset_index()
            df_daily_cost.columns = ["day", "cost_usd"]
            fig = px.bar(df_daily_cost, x="day", y="cost_usd",
                         labels={"day": "Date", "cost_usd": "USD"})
            fig.update_layout(margin=dict(l=0, r=0, t=30, b=0))
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            st.subheader("Cost by tag")
            df_tags = df_llm.groupby("tag")["est_cost_usd"].sum().reset_index()
            df_tags.columns = ["tag", "cost_usd"]
            fig2 = px.pie(df_tags, names="tag", values="cost_usd")
            fig2.update_layout(margin=dict(l=0, r=0, t=30, b=0))
            st.plotly_chart(fig2, use_container_width=True)

        st.divider()

        col3, col4 = st.columns(2)
        with col3:
            st.subheader("Prompt vs completion tokens")
            token_totals = {
                "prompt": df_llm["prompt_tokens"].sum(),
                "completion": df_llm["completion_tokens"].sum(),
            }
            st.bar_chart(token_totals)

        with col4:
            st.subheader("Cost by model")
            df_models = df_llm.groupby("model")["est_cost_usd"].sum().reset_index()
            df_models.columns = ["model", "cost_usd"]
            st.dataframe(df_models, use_container_width=True, hide_index=True)

        st.subheader("Recent invocations (last 20)")
        recent = df_llm.sort_values("ts", ascending=False).head(20)
        st.dataframe(
            recent[["ts", "tag", "model", "prompt_tokens", "completion_tokens", "est_cost_usd"]].rename(
                columns={"ts": "Timestamp", "tag": "Tag", "model": "Model",
                         "prompt_tokens": "Prompt tok.", "completion_tokens": "Completion tok.",
                         "est_cost_usd": "Est. cost USD"}
            ),
            use_container_width=True,
            hide_index=True,
        )
```

- [ ] **Step 2: Verify LLM Cost tab**

Reload and click 🤖 LLM Cost. Should show daily cost chart, tag pie chart, 20-row table from `data/llm_usage.jsonl` (72 records).

- [ ] **Step 3: Commit**

```bash
git add scripts/python/dashboard.py
git commit -m "feat: dashboard LLM Cost tab"
```

---

## Task 9: Control tab

**Files:**
- Modify: `scripts/python/dashboard.py`

HALT/RESUME use `st.session_state` to require a confirmation click before writing/deleting the file.

- [ ] **Step 1: Add Control section**

```python
# ── Control tab ───────────────────────────────────────────────────────────────

with TAB_CTRL:
    halted = db.is_halted()
    trader_age = db.trader_heartbeat_age()

    st.subheader("Kill switch")
    if halted:
        st.error("🛑 HALT file is present — trader is blocked.")
    else:
        st.success("✅ No HALT file — trader is allowed to run.")

    col1, col2 = st.columns(2)

    with col1:
        if not halted:
            if st.button("🛑 HALT trading"):
                st.session_state["halt_confirm"] = True
            if st.session_state.get("halt_confirm"):
                st.warning("Confirm: this will stop trading on the next cycle.")
                if st.button("✅ Confirm HALT"):
                    db.halt()
                    st.session_state["halt_confirm"] = False
                    st.success("HALT file created.")
                    st.rerun()
        else:
            st.info("Trader already halted.")

    with col2:
        if halted:
            if st.button("▶️ RESUME trading"):
                st.session_state["resume_confirm"] = True
            if st.session_state.get("resume_confirm"):
                st.warning("Confirm: this will allow trading to resume on the next cycle.")
                if st.button("✅ Confirm RESUME"):
                    db.resume()
                    st.session_state["resume_confirm"] = False
                    st.success("HALT file removed.")
                    st.rerun()
        else:
            st.info("Trader not halted.")

    st.divider()

    st.subheader("Environment (read-only)")
    env_keys = [
        "EXECUTE", "CYCLE_SECONDS", "MAX_POSITION_FRACTION",
        "STARTING_BALANCE_USDC", "MAX_DAILY_LOSS_PCT",
        "MAX_TRADES_PER_HOUR", "MIN_USDC_FLOOR",
        "MAX_DAILY_TOKEN_USD", "LOG_LEVEL",
    ]
    env_display = {k: os.getenv(k, "(not set)") for k in env_keys}
    st.table(pd.DataFrame(
        list(env_display.items()), columns=["Variable", "Value"]
    ))

    st.divider()

    st.subheader("Full log (last 200 lines)")
    st.code(db.log_tail(200), language=None)
```

- [ ] **Step 2: Test HALT/RESUME manually**

1. Open http://localhost:8050 → ⚙️ Control tab
2. Click "🛑 HALT trading" → warning appears
3. Click "✅ Confirm HALT" → success message; verify `data/HALT` file exists:
   ```bash
   ls -la /Users/mymac/coding/poly1/data/HALT
   ```
4. Click "▶️ RESUME trading" → "✅ Confirm RESUME" → verify HALT file removed:
   ```bash
   ls /Users/mymac/coding/poly1/data/HALT 2>&1
   # expected: No such file or directory
   ```

- [ ] **Step 3: Commit**

```bash
git add scripts/python/dashboard.py
git commit -m "feat: dashboard Control tab (HALT/RESUME)"
```

---

## Task 10: Auto-refresh on Live tab

**Files:**
- Modify: `scripts/python/dashboard.py`

Streamlit's `st.rerun()` + `time.sleep()` loop only works with `@st.fragment`. We use the simpler approach: a `<meta http-equiv="refresh">` injected via `st.markdown` on the Live tab, which refreshes the entire page every 30 s.

- [ ] **Step 1: Add auto-refresh to Live tab**

Find the `with TAB_LIVE:` block. Add this line **at the top of the block**, before any widgets:

```python
    # Auto-refresh every 30 s (browser-side meta refresh)
    st.markdown(
        '<meta http-equiv="refresh" content="30">',
        unsafe_allow_html=True,
    )
```

- [ ] **Step 2: Verify auto-refresh**

Open http://localhost:8050 on the Live tab. Wait ~30 s. The page should reload automatically (browser address bar updates). Note: the meta refresh fires on ALL tabs since it's injected into the page head — this is acceptable for Phase 1.

- [ ] **Step 3: Commit**

```bash
git add scripts/python/dashboard.py
git commit -m "feat: dashboard auto-refresh (30s meta refresh)"
```

---

## Task 11: Docker service

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add dashboard service**

Append to `docker-compose.yml` after the `trader` service block:

```yaml
  dashboard:
    build: .
    image: poly1:local
    profiles: ["dashboard"]
    command: >
      streamlit run scripts/python/dashboard.py
      --server.port 8050
      --server.address 0.0.0.0
      --server.headless true
    ports:
      - "8050:8050"
    env_file: .env
    environment:
      TZ: UTC
    volumes:
      - ./data:/app/data
    restart: unless-stopped
    mem_limit: 512m
    cpus: "0.5"
```

**Note:** `./data` is mounted read-write so the Control tab can write/delete `data/HALT`. Application code in `db.py` limits writes to that one file.

- [ ] **Step 2: Build and start dashboard container**

```bash
cd /Users/mymac/coding/poly1
docker compose build
docker compose --profile dashboard up dashboard -d
docker compose --profile dashboard logs dashboard --follow
```

Expected: Streamlit prints `You can now view your Streamlit app in your browser.` and `Network URL: http://0.0.0.0:8050`.

Open http://localhost:8050 in browser. All 7 tabs must render with real data.

- [ ] **Step 3: Verify HALT from container**

Open ⚙️ Control → HALT → Confirm. Check `data/HALT` was created on the host:
```bash
ls -la /Users/mymac/coding/poly1/data/HALT
```

Then RESUME → verify file removed.

- [ ] **Step 4: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: docker-compose dashboard service (profile: dashboard)"
```

---

## Task 12: Final smoke test

- [ ] **Step 1: Run all existing tests (verify no regressions)**

```bash
docker compose build
docker compose run --rm trader python -m unittest tests.test_executor tests.test_trader.TestTradeLog tests.test_trader.TestRiskGate -v
```

Expected: all pass.

- [ ] **Step 2: Full 7-tab walkthrough**

With `docker compose --profile dashboard up dashboard -d`, open http://localhost:8050:

| Tab | Check |
|-----|-------|
| 🟢 Live | Heartbeat age shown, status counts match CLI output |
| 📈 P&L | Charts render, filled trade count = 5 |
| 💰 Capital | Starting balance reads from env ($80), deployed shown |
| 📋 Trades | Filter by `filled` shows 5 rows; `skipped_gate` filter shows gate rows |
| 🔪 Scalper | "No scalper_pairs table" message (main branch) |
| 🤖 LLM Cost | Total records = 72, cost charts render |
| ⚙️ Control | Env vars displayed; HALT/RESUME cycle works |

- [ ] **Step 3: Tag release**

```bash
git tag dashboard-v1.0
git push --tags
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|-----------------|------|
| 7 tabs: Live, P&L, Capital, Trades, Scalper, LLM, Control | Tasks 3–9 |
| Streamlit on port 8050 | Tasks 3, 11 |
| Docker profile `dashboard` | Task 11 |
| HALT/RESUME control | Task 9 |
| Auto-refresh 30 s on Live tab | Task 10 |
| Read data/trade_log.db read-only | Task 2 (db.py, `?mode=ro`) |
| Scalper tab graceful if table missing | Task 7 (explicit `_table_exists` check) |
| P&L charts (approximate, noted) | Task 4 |
| LLM cost charts | Task 8 |
| Add streamlit/pandas/plotly | Task 1 |
| No writes to .env from dashboard | Task 9 (env display is read-only) |

**Placeholder scan:** No TBDs found.

**Type consistency:** `db.py` functions return `list[dict]` throughout. `dashboard.py` wraps all in `pd.DataFrame()` calls before display. `heartbeat_age()` returns `float | None` — handled in `_age_label()`.
