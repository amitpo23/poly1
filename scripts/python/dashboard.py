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
    # Auto-refresh every 30 s (browser-side meta refresh)
    st.markdown(
        '<meta http-equiv="refresh" content="30">',
        unsafe_allow_html=True,
    )
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

    st.caption("Auto-refreshes every 30 s via meta refresh (applies to all tabs).")

# ── Placeholder stubs for other tabs (filled in subsequent tasks) ─────────────

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

    scalper_open = db.scalper_pairs_open()
    scalper_deployed = sum(
        (p.get("cost_up") or 0) + (p.get("cost_down") or 0)
        for p in scalper_open
    )

    col1, col2, col3 = st.columns(3)
    col1.metric("Starting balance", f"${starting_balance:.2f}")
    col2.metric("Capital deployed (filled)", f"${total_deployed:.2f}")
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

# ── Trades tab ────────────────────────────────────────────────────────────────

with TAB_TRADES:
    all_trades = db.trades_all()
    df = pd.DataFrame(all_trades) if all_trades else pd.DataFrame()

    if df.empty:
        st.warning("No trades in database.")
    else:
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

        with st.expander("Raw response_json (filled trades)"):
            filled_with_json = filtered[
                (filtered["status"] == "filled") & filtered["response_json"].notna()
            ]
            for _, row in filled_with_json.iterrows():
                st.caption(f"{row['ts']} — {row['market_id']}")
                st.json(row["response_json"])

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

        df_states = pd.DataFrame(
            list(state_counts.items()), columns=["State", "Count"]
        )
        fig = px.bar(df_states, x="State", y="Count", title="Pairs by state")
        fig.update_layout(margin=dict(l=0, r=0, t=40, b=0))
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("Open pairs")
        if open_pairs:
            df_open = pd.DataFrame(open_pairs)

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
                "prompt": int(df_llm["prompt_tokens"].sum()),
                "completion": int(df_llm["completion_tokens"].sum()),
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

# ── Control tab ───────────────────────────────────────────────────────────────

with TAB_CTRL:
    halted = db.is_halted()

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
