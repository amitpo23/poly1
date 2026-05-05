"""Poly1 Streamlit dashboard — Phase 1.

Run:
    streamlit run scripts/python/dashboard.py --server.port 8050

Reads data from TRADE_LOG_DB, LLM_USAGE_FILE, LOG_DIR (env vars).
Writes to KILL_SWITCH_FILE (Control tab only).
"""
from __future__ import annotations

import os
import sys
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
        st.metric("Gate blocks", counts.get("skipped_gate", 0))
    with col6:
        st.metric("Deduped", counts.get("skipped_dedupe", 0))
    with col7:
        st.metric("Failed", counts.get("failed", 0))

    if gate_reason:
        st.info(f"Last gate block: {gate_reason}")

    st.divider()
    st.subheader("Log tail (last 80 lines)")
    st.code(db.log_tail(80), language=None)

    st.caption("Page refreshes automatically every 30 s — or click Refresh now above.")

# ── Placeholder stubs for other tabs (filled in subsequent tasks) ─────────────

with TAB_PNL:
    st.info("P&L tab — coming soon (Task 4)")

with TAB_CAPITAL:
    st.info("Capital tab — coming soon (Task 5)")

with TAB_TRADES:
    st.info("Trades tab — coming soon (Task 6)")

with TAB_SCALPER:
    st.info("Scalper tab — coming soon (Task 7)")

with TAB_LLM:
    st.info("LLM Cost tab — coming soon (Task 8)")

with TAB_CTRL:
    st.info("Control tab — coming soon (Task 9)")
