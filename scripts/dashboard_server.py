#!/usr/bin/env python3
"""Real-time dashboard for poly1 + swarm + per-agent live activity.

Built 2026-05-25 evening per operator request. Serves a single-page
HTML dashboard at http://<host>:8090 that auto-refreshes every 5s
with live state of every active trading agent.

What it shows:
  - Per-agent: trades today, wins, losses, win-rate, net PnL
  - Recent trades (last 30 by id, with timestamps + agent attribution)
  - Open positions per agent with live MTM
  - Wallet balance (cash + estimated equity)
  - Daemon health: each container's heartbeat freshness
  - Runtime mode (live / freeze) + HALT marker
  - Last 10 cycle activities per major agent (skip reasons, decisions)

Usage:
  cd /srv/poly1
  python3 scripts/dashboard_server.py --port 8090

Or as a docker service (see docker-compose dashboard service).

Read-only: never writes to DB. SAFE to run alongside live trading.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DB_PATH = os.environ.get("DASHBOARD_DB", "/srv/poly1/data/trade_log.db")
SWARM_DB_PATH = os.environ.get("DASHBOARD_SWARM_DB", "/home/trader/swarm/data/swarm.db")
RUNTIME_CTRL = os.environ.get("DASHBOARD_RUNTIME", "/srv/poly1/data/runtime_control.json")
HALT_FILE = os.environ.get("DASHBOARD_HALT", "/srv/poly1/data/HALT")
HEARTBEAT_DIR = os.environ.get("DASHBOARD_HEARTBEATS", "/srv/poly1/data")
WALLET_ADDR = (
    os.environ.get("POLYMARKET_PROXY_ADDRESS")
    or os.environ.get("POLY1_PROXY_ADDRESS")
    or os.environ.get("POLY1_WALLET")
    or os.environ.get("POLYMARKET_DEPOSIT_WALLET")
    or ""
).lower()
GAMMA_URL = "https://gamma-api.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"


class TTLCache:
    """Simple thread-safe TTL cache with stale-on-error fallback."""
    def __init__(self):
        self._data: dict = {}
        self._lock = threading.Lock()

    def get_or_fetch(self, key: str, ttl: float, fetch_fn):
        now = time.time()
        with self._lock:
            entry = self._data.get(key)
            if entry and entry["expires"] > now:
                return entry["value"]
        try:
            value = fetch_fn()
            with self._lock:
                self._data[key] = {"value": value, "expires": now + ttl, "stale": False}
            return value
        except Exception as exc:
            with self._lock:
                if key in self._data:
                    # Serve stale data with a marker
                    stale = dict(self._data[key]["value"]) if isinstance(self._data[key]["value"], dict) else self._data[key]["value"]
                    if isinstance(stale, dict):
                        stale["_stale"] = True
                        stale["_error"] = str(exc)[:80]
                    return stale
            return {"_error": str(exc)[:80]}


CACHE = TTLCache()


def _http_get_json(url: str, timeout: float = 4.0) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "poly1-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return json.loads(body.decode("utf-8"))


def fetch_markets() -> list:
    """Fetch active markets from Gamma API, focused on crypto 5min / daily."""
    params = urlencode({
        "active": "true",
        "closed": "false",
        "limit": "20",
        "order": "volume24hr",
        "ascending": "false",
    })
    url = f"{GAMMA_URL}/markets?{params}"
    raw = _http_get_json(url)
    out = []
    for m in raw if isinstance(raw, list) else []:
        try:
            outcomes = json.loads(m.get("outcomes") or "[]")
            prices = json.loads(m.get("outcomePrices") or "[]")
            token_ids = json.loads(m.get("clobTokenIds") or "[]")
        except Exception:
            outcomes, prices, token_ids = [], [], []
        out.append({
            "question": (m.get("question") or "")[:80],
            "slug": m.get("slug"),
            "volume_24h": round(float(m.get("volume24hr") or 0), 2),
            "liquidity": round(float(m.get("liquidity") or 0), 2),
            "outcomes": outcomes,
            "prices": [round(float(p), 4) for p in prices] if prices else [],
            "token_ids": token_ids[:2] if token_ids else [],
            "end_date": m.get("endDate"),
        })
    return out


def fetch_onchain_positions() -> list:
    """Fetch on-chain CTF positions from Polymarket Data API."""
    if not WALLET_ADDR:
        return [{"_error": "no wallet address configured"}]
    params = urlencode({"user": WALLET_ADDR, "sizeThreshold": "0.01"})
    url = f"{DATA_API_URL}/positions?{params}"
    raw = _http_get_json(url, timeout=6.0)
    out = []
    for p in raw if isinstance(raw, list) else []:
        out.append({
            "title": (p.get("title") or "")[:60],
            "outcome": p.get("outcome"),
            "size": round(float(p.get("size") or 0), 3),
            "avg_price": round(float(p.get("avgPrice") or 0), 4),
            "cur_price": round(float(p.get("curPrice") or 0), 4),
            "value": round(float(p.get("currentValue") or 0), 3),
            "cashPnl": round(float(p.get("cashPnl") or 0), 3),
            "percentPnl": round(float(p.get("percentPnl") or 0), 2),
            "redeemable": p.get("redeemable", False),
            "asset": (p.get("asset") or "")[:20],
        })
    out.sort(key=lambda r: -abs(r.get("value", 0)))
    return out


def fetch_orderbook(token_id: str) -> dict:
    """Fetch the order book for a CLOB token."""
    url = f"{CLOB_URL}/book?token_id={token_id}"
    raw = _http_get_json(url, timeout=4.0)
    bids = raw.get("bids", []) if isinstance(raw, dict) else []
    asks = raw.get("asks", []) if isinstance(raw, dict) else []
    # Each level: {"price": "0.95", "size": "20"}
    pb = [{"price": float(b["price"]), "size": float(b["size"])} for b in bids[:5]]
    pa = [{"price": float(a["price"]), "size": float(a["size"])} for a in asks[:5]]
    pb.sort(key=lambda x: -x["price"])  # highest bid first
    pa.sort(key=lambda x: x["price"])   # lowest ask first
    best_bid = pb[0]["price"] if pb else None
    best_ask = pa[0]["price"] if pa else None
    return {
        "bids": pb,
        "asks": pa,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": round(best_ask - best_bid, 4) if (best_bid is not None and best_ask is not None) else None,
    }


def _attribute(cycle_id: str) -> str:
    """Map cycle_id prefix to a readable agent label."""
    if not cycle_id:
        return "(none)"
    if cycle_id.startswith("btc5min_timed_v2:"):
        return "amit_v2"
    if cycle_id.startswith("btc5min_timed:"):
        return "amit_v1"
    if cycle_id.startswith("btc_5min:"):
        return "btc_5min"
    if cycle_id.startswith("scanner_executor:"):
        return "scanner_executor"
    if cycle_id.startswith("close:"):
        return "close"
    if cycle_id.startswith("scalper"):
        return "scalper"
    if cycle_id.startswith("near_resolution"):
        return "near_resolution"
    return cycle_id.split(":")[0] if ":" in cycle_id else cycle_id


def collect_state() -> dict:
    """Build the full dashboard state in one DB read pass."""
    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agents": {},
        "recent_trades": [],
        "open_positions": [],
        "runtime": {},
        "heartbeats": {},
        "totals": {},
        "swarm": {},
    }

    # Runtime mode + HALT
    try:
        with open(RUNTIME_CTRL) as f:
            rc = json.load(f)
        out["runtime"] = {
            "mode": rc.get("mode"),
            "expires_at": rc.get("expires_at"),
            "allowed_live_agents": rc.get("allowed_live_agents") or [],
            "budget_usdc": rc.get("budget_usdc"),
            "halt": os.path.exists(HALT_FILE),
        }
    except Exception as exc:
        out["runtime"] = {"error": str(exc)}

    # Heartbeats
    hb_pat = ["btc5min_timed", "btc5min_timed_v2", "btc_5min",
              "scanner_executor", "position_manager", "orderbook_monitor",
              "brain_indicator_cycle", "market_universe", "market_scanner"]
    hb_dir = Path(HEARTBEAT_DIR)
    for name in hb_pat:
        for suffix in ("_heartbeat", "-heartbeat"):
            p = hb_dir / f"{name}{suffix}"
            if p.exists():
                age = time.time() - p.stat().st_mtime
                out["heartbeats"][name] = round(age, 1)
                break

    # Main DB queries
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            today_start = datetime.utcnow().strftime("%Y-%m-%dT00:00:00")

            # Recent 30 trades
            rows = conn.execute(
                "SELECT id, ts, cycle_id, side, status, price, "
                "size_usdc, response_json FROM trades "
                "ORDER BY id DESC LIMIT 30"
            ).fetchall()
            for r in rows:
                r = dict(r)
                pnl = None
                try:
                    resp = json.loads(r.get("response_json") or "{}")
                    pnl = resp.get("pnl_usdc_real")
                except Exception:
                    pass
                out["recent_trades"].append({
                    "id": r["id"],
                    "ts": r["ts"],
                    "agent": _attribute(r.get("cycle_id") or ""),
                    "side": r["side"],
                    "status": r["status"],
                    "price": round(float(r["price"] or 0), 4),
                    "size_usdc": round(float(r["size_usdc"] or 0), 2),
                    "pnl_usdc": round(float(pnl), 4) if pnl is not None else None,
                })

            # Per-agent stats today
            agent_stats = {}
            closes_today = conn.execute(
                "SELECT cycle_id, status, response_json FROM trades "
                "WHERE ts > ? AND status LIKE 'closed_%' ORDER BY id",
                (today_start,),
            ).fetchall()
            for c in closes_today:
                c = dict(c)
                # Closes have cycle_id="close:...". Walk back to find entry agent.
                # Approximate: use token_id matching would require another query.
                # For dashboard, we tag close rows as 'close' agent and rely on
                # the entries below for per-agent attribution.
                ag = _attribute(c.get("cycle_id") or "")
                pnl = 0
                try:
                    pnl = float(json.loads(c.get("response_json") or "{}").get("pnl_usdc_real") or 0)
                except Exception:
                    pass
                agent_stats.setdefault(ag, {"wins": 0, "losses": 0, "net_pnl": 0.0, "trades": 0})
                if c["status"] == "closed_take_profit":
                    agent_stats[ag]["wins"] += 1
                elif c["status"] in ("closed_stop_loss", "closed_timeout", "resolved_loss"):
                    agent_stats[ag]["losses"] += 1
                agent_stats[ag]["net_pnl"] += pnl
                agent_stats[ag]["trades"] += 1

            # Per-agent entry counts today
            entries = conn.execute(
                "SELECT cycle_id, COUNT(*) AS n FROM trades "
                "WHERE ts > ? AND status IN ('filled','btc_5min_open','btc5min_timed_open','btc5min_timed_v2_open') "
                "GROUP BY substr(cycle_id, 1, instr(cycle_id||':', ':')-1)",
                (today_start,),
            ).fetchall()
            for e in entries:
                ag = _attribute(e["cycle_id"] or "")
                if ag not in agent_stats:
                    agent_stats[ag] = {"wins": 0, "losses": 0, "net_pnl": 0.0, "trades": 0}
                agent_stats[ag]["entries_today"] = agent_stats[ag].get("entries_today", 0) + e["n"]

            for ag, st in agent_stats.items():
                trades = st["wins"] + st["losses"]
                st["wr"] = round(st["wins"] / trades, 3) if trades else 0
                st["net_pnl"] = round(st["net_pnl"], 3)
            out["agents"] = agent_stats

            # Open positions
            open_rows = conn.execute(
                "SELECT t1.id, t1.ts, t1.cycle_id, t1.side, t1.price, "
                "t1.size_usdc, t1.token_id "
                "FROM trades t1 WHERE t1.status IN "
                "('filled','btc_5min_open','btc5min_timed_open','btc5min_timed_v2_open') "
                "AND NOT EXISTS (SELECT 1 FROM trades t2 WHERE t2.token_id=t1.token_id "
                "AND t2.id>t1.id AND (t2.status LIKE 'closed_%' OR t2.status LIKE 'resolved_%')) "
                "ORDER BY t1.id DESC LIMIT 40"
            ).fetchall()
            for r in open_rows:
                r = dict(r)
                # Get latest bid for MTM
                mtm = None
                try:
                    snap = conn.execute(
                        "SELECT best_bid FROM orderbook_snapshots "
                        "WHERE token_id=? ORDER BY ts DESC LIMIT 1",
                        (r["token_id"],),
                    ).fetchone()
                    if snap:
                        mtm = float(snap["best_bid"] or 0)
                except Exception:
                    pass
                out["open_positions"].append({
                    "id": r["id"],
                    "ts": r["ts"],
                    "agent": _attribute(r.get("cycle_id") or ""),
                    "side": r["side"],
                    "entry": round(float(r["price"] or 0), 4),
                    "size_usdc": round(float(r["size_usdc"] or 0), 2),
                    "current_bid": round(mtm, 4) if mtm is not None else None,
                })

            # Day total
            tot = conn.execute(
                "SELECT ROUND(SUM(CAST(json_extract(response_json,'$.pnl_usdc_real') AS REAL)), 3) "
                "FROM trades WHERE ts > ? AND status LIKE 'closed_%'",
                (today_start,),
            ).fetchone()
            out["totals"]["day_pnl_closed"] = tot[0] if tot and tot[0] is not None else 0
    except Exception as exc:
        out["error_main"] = str(exc)

    # Swarm DB
    try:
        with sqlite3.connect(f"file:{SWARM_DB_PATH}?mode=ro", uri=True, timeout=3) as conn:
            conn.row_factory = sqlite3.Row
            fills = conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
            pnl_events = conn.execute("SELECT COUNT(*) FROM pnl_events").fetchone()[0]
            pending = conn.execute(
                "SELECT status, COUNT(*) FROM pending_orders GROUP BY status"
            ).fetchall()
            out["swarm"] = {
                "fills": fills,
                "pnl_events": pnl_events,
                "pending_orders": {r[0]: r[1] for r in pending},
            }
    except Exception as exc:
        out["swarm"] = {"error": str(exc)}

    # Level A: live Polymarket markets (cached 60s)
    out["markets"] = CACHE.get_or_fetch("markets", 60.0, fetch_markets)

    # Level B: on-chain CTF positions (cached 30s)
    out["onchain"] = CACHE.get_or_fetch("onchain", 30.0, fetch_onchain_positions)

    # Level C: order books for tokens we have open positions in (cached 15s each)
    token_ids = list({p["token_id"] for p in [
        # Need to capture token_id during open_positions loop above — re-query briefly
    ] if p.get("token_id")})
    # Simpler: re-derive token_ids from open_rows by hitting DB again with read mode
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=3) as conn:
            rows = conn.execute(
                "SELECT DISTINCT t1.token_id FROM trades t1 WHERE t1.status IN "
                "('filled','btc_5min_open','btc5min_timed_open','btc5min_timed_v2_open') "
                "AND NOT EXISTS (SELECT 1 FROM trades t2 WHERE t2.token_id=t1.token_id "
                "AND t2.id>t1.id AND (t2.status LIKE 'closed_%' OR t2.status LIKE 'resolved_%')) "
                "LIMIT 8"
            ).fetchall()
            token_ids = [r[0] for r in rows if r[0]]
    except Exception:
        token_ids = []

    books = {}
    for tid in token_ids:
        books[tid] = CACHE.get_or_fetch(f"book:{tid}", 15.0, lambda tid=tid: fetch_orderbook(tid))
    out["orderbooks"] = books

    return out


DASHBOARD_HTML = """<!doctype html>
<html lang="he"><head>
<meta charset="utf-8">
<title>poly1 live dashboard</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #0f1419; color: #e4e7eb; margin: 0; padding: 16px; }
  h1 { margin: 0 0 8px; font-size: 18px; }
  h2 { font-size: 14px; margin: 16px 0 8px; color: #9ca3af; text-transform: uppercase; letter-spacing: 0.5px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .card { background: #1a2027; border: 1px solid #2d3741; border-radius: 8px; padding: 12px; }
  .full { grid-column: 1 / -1; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; font-family: ui-monospace, monospace; }
  th, td { padding: 4px 6px; text-align: left; border-bottom: 1px solid #2a3038; }
  th { color: #6b7280; font-weight: 600; }
  .pos { color: #34d399; }
  .neg { color: #f87171; }
  .mode-live { color: #34d399; }
  .mode-freeze { color: #fbbf24; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .b-tp { background: #065f46; color: #d1fae5; }
  .b-sl { background: #991b1b; color: #fee2e2; }
  .b-open { background: #1e40af; color: #dbeafe; }
  .b-failed { background: #4b5563; color: #d1d5db; }
  .b-deferred { background: #92400e; color: #fef3c7; }
  .metric { font-size: 20px; font-weight: 600; }
  .label { font-size: 11px; color: #9ca3af; }
  .stale { color: #f87171; }
  .fresh { color: #34d399; }
  #refresh-status { font-size: 11px; color: #9ca3af; }
  .books-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
  .book { border: 1px solid #2d3741; border-radius: 6px; padding: 8px; background: #131820; font-family: ui-monospace, monospace; font-size: 11px; }
  .book-title { font-weight: 600; color: #9ca3af; margin-bottom: 6px; font-size: 10px; word-break: break-all; }
  .book-side { display: flex; justify-content: space-between; padding: 2px 4px; }
  .book-bid { color: #34d399; }
  .book-ask { color: #f87171; }
  .book-spread { background: #1f2937; padding: 4px; margin: 4px 0; text-align: center; color: #fbbf24; }
  .stale-banner { background: #92400e; color: #fef3c7; padding: 2px 6px; border-radius: 3px; font-size: 10px; display: inline-block; }
</style>
</head><body>
<h1>poly1 live dashboard <span id="refresh-status"></span></h1>

<div class="grid">
  <div class="card">
    <h2>Runtime</h2>
    <div id="runtime"></div>
  </div>
  <div class="card">
    <h2>Day Totals</h2>
    <div id="totals"></div>
  </div>

  <div class="card full">
    <h2>Agents Today</h2>
    <table id="agents-tbl"><thead><tr>
      <th>Agent</th><th>Entries</th><th>Wins</th><th>Losses</th><th>WR</th><th>Net PnL</th>
    </tr></thead><tbody></tbody></table>
  </div>

  <div class="card full">
    <h2>Open Positions</h2>
    <table id="open-tbl"><thead><tr>
      <th>id</th><th>time</th><th>agent</th><th>side</th><th>entry</th><th>now bid</th><th>$ size</th>
    </tr></thead><tbody></tbody></table>
  </div>

  <div class="card full">
    <h2>Recent Trades (last 30)</h2>
    <table id="recent-tbl"><thead><tr>
      <th>id</th><th>time</th><th>agent</th><th>side</th><th>status</th><th>price</th><th>PnL</th>
    </tr></thead><tbody></tbody></table>
  </div>

  <div class="card">
    <h2>Heartbeats (seconds since last)</h2>
    <div id="hb"></div>
  </div>
  <div class="card">
    <h2>Swarm (dryrun)</h2>
    <div id="swarm"></div>
  </div>

  <div class="card full">
    <h2>📊 Live Polymarket markets (top by 24h volume)</h2>
    <table id="markets-tbl"><thead><tr>
      <th>Question</th><th>YES</th><th>NO</th><th>24h vol</th><th>liquidity</th><th>ends</th>
    </tr></thead><tbody></tbody></table>
  </div>

  <div class="card full">
    <h2>🔗 On-chain CTF positions (live from Polymarket)</h2>
    <div class="label" id="onchain-summary"></div>
    <table id="onchain-tbl"><thead><tr>
      <th>Market</th><th>Outcome</th><th>Size</th><th>Avg</th><th>Now</th><th>Value</th><th>PnL $</th><th>PnL %</th><th>Redeem?</th>
    </tr></thead><tbody></tbody></table>
  </div>

  <div class="card full">
    <h2>📖 Live order books (open-position tokens)</h2>
    <div id="books" class="books-grid"></div>
  </div>
</div>

<script>
async function refresh() {
  try {
    const r = await fetch('/api/status?t=' + Date.now());
    const d = await r.json();
    document.getElementById('refresh-status').textContent = '· last refresh ' + new Date(d.ts).toLocaleTimeString();

    // Runtime
    const rt = d.runtime || {};
    const modeClass = rt.mode === 'live' ? 'mode-live' : 'mode-freeze';
    document.getElementById('runtime').innerHTML =
      '<div class="metric ' + modeClass + '">' + (rt.mode || '—').toUpperCase() + (rt.halt ? ' · HALT' : '') + '</div>' +
      '<div class="label">expires: ' + (rt.expires_at || '—') + '</div>' +
      '<div class="label">agents: ' + (rt.allowed_live_agents || []).join(', ') + '</div>';

    // Totals
    const t = d.totals || {};
    const pnl = t.day_pnl_closed || 0;
    const cls = pnl >= 0 ? 'pos' : 'neg';
    document.getElementById('totals').innerHTML =
      '<div class="metric ' + cls + '">$' + pnl.toFixed(3) + '</div>' +
      '<div class="label">day PnL (closed only)</div>';

    // Agents
    const agentsBody = document.getElementById('agents-tbl').querySelector('tbody');
    agentsBody.innerHTML = '';
    Object.entries(d.agents || {}).sort((a, b) => (b[1].net_pnl||0) - (a[1].net_pnl||0))
      .forEach(([name, st]) => {
        const row = document.createElement('tr');
        const pnlCls = (st.net_pnl||0) >= 0 ? 'pos' : 'neg';
        row.innerHTML =
          '<td>' + name + '</td>' +
          '<td>' + (st.entries_today || 0) + '</td>' +
          '<td class="pos">' + (st.wins || 0) + '</td>' +
          '<td class="neg">' + (st.losses || 0) + '</td>' +
          '<td>' + ((st.wr || 0) * 100).toFixed(0) + '%</td>' +
          '<td class="' + pnlCls + '">$' + (st.net_pnl || 0).toFixed(3) + '</td>';
        agentsBody.appendChild(row);
      });

    // Open positions
    const openBody = document.getElementById('open-tbl').querySelector('tbody');
    openBody.innerHTML = '';
    (d.open_positions || []).forEach(p => {
      const row = document.createElement('tr');
      const sideCls = p.side === 'BUY' ? 'pos' : 'neg';
      row.innerHTML =
        '<td>' + p.id + '</td>' +
        '<td>' + p.ts.slice(11, 19) + '</td>' +
        '<td>' + p.agent + '</td>' +
        '<td class="' + sideCls + '">' + p.side + '</td>' +
        '<td>' + p.entry + '</td>' +
        '<td>' + (p.current_bid !== null ? p.current_bid : '—') + '</td>' +
        '<td>$' + p.size_usdc + '</td>';
      openBody.appendChild(row);
    });

    // Recent trades
    const recBody = document.getElementById('recent-tbl').querySelector('tbody');
    recBody.innerHTML = '';
    (d.recent_trades || []).forEach(t => {
      const row = document.createElement('tr');
      let badge = '';
      if (t.status === 'closed_take_profit') badge = '<span class="badge b-tp">TP</span>';
      else if (t.status === 'closed_stop_loss') badge = '<span class="badge b-sl">SL</span>';
      else if (t.status === 'failed') badge = '<span class="badge b-failed">FAIL</span>';
      else if (t.status === 'exit_deferred') badge = '<span class="badge b-deferred">DEF</span>';
      else if (t.status.endsWith('open')) badge = '<span class="badge b-open">OPEN</span>';
      else badge = '<span class="badge b-failed">' + t.status + '</span>';

      let pnlHtml = '—';
      if (t.pnl_usdc !== null) {
        const cls2 = t.pnl_usdc >= 0 ? 'pos' : 'neg';
        pnlHtml = '<span class="' + cls2 + '">$' + t.pnl_usdc.toFixed(3) + '</span>';
      }
      row.innerHTML =
        '<td>' + t.id + '</td>' +
        '<td>' + t.ts.slice(11, 19) + '</td>' +
        '<td>' + t.agent + '</td>' +
        '<td>' + (t.side || '—') + '</td>' +
        '<td>' + badge + '</td>' +
        '<td>' + (t.price || '—') + '</td>' +
        '<td>' + pnlHtml + '</td>';
      recBody.appendChild(row);
    });

    // Heartbeats
    const hbDiv = document.getElementById('hb');
    hbDiv.innerHTML = '';
    Object.entries(d.heartbeats || {}).sort().forEach(([name, age]) => {
      const cls = age < 60 ? 'fresh' : 'stale';
      hbDiv.innerHTML += '<div class="' + cls + '">' + name + ': ' + age.toFixed(0) + 's</div>';
    });

    // Swarm
    const s = d.swarm || {};
    document.getElementById('swarm').innerHTML =
      '<div>fills: ' + (s.fills !== undefined ? s.fills : '—') + '</div>' +
      '<div>pnl_events: ' + (s.pnl_events !== undefined ? s.pnl_events : '—') + '</div>' +
      '<div>pending: ' + (s.pending_orders ? Object.entries(s.pending_orders).map(([k,v]) => k+':'+v).join(', ') : '—') + '</div>';

    // Live markets
    const mktBody = document.getElementById('markets-tbl').querySelector('tbody');
    mktBody.innerHTML = '';
    const markets = Array.isArray(d.markets) ? d.markets : [];
    markets.forEach(m => {
      const yes = (m.prices && m.prices[0]) || '—';
      const no = (m.prices && m.prices[1]) || '—';
      const ends = m.end_date ? new Date(m.end_date).toLocaleString() : '—';
      const row = document.createElement('tr');
      row.innerHTML =
        '<td>' + (m.question || '—') + '</td>' +
        '<td class="pos">' + yes + '</td>' +
        '<td class="neg">' + no + '</td>' +
        '<td>$' + (m.volume_24h || 0).toLocaleString() + '</td>' +
        '<td>$' + (m.liquidity || 0).toLocaleString() + '</td>' +
        '<td>' + ends + '</td>';
      mktBody.appendChild(row);
    });

    // On-chain positions
    const ocBody = document.getElementById('onchain-tbl').querySelector('tbody');
    ocBody.innerHTML = '';
    const oc = Array.isArray(d.onchain) ? d.onchain : [];
    let ocTotalValue = 0, ocTotalPnl = 0;
    oc.forEach(p => {
      ocTotalValue += p.value || 0;
      ocTotalPnl += p.cashPnl || 0;
      const row = document.createElement('tr');
      const pnlCls = (p.cashPnl || 0) >= 0 ? 'pos' : 'neg';
      row.innerHTML =
        '<td>' + (p.title || '—') + '</td>' +
        '<td>' + (p.outcome || '—') + '</td>' +
        '<td>' + (p.size || 0) + '</td>' +
        '<td>' + (p.avg_price || '—') + '</td>' +
        '<td>' + (p.cur_price || '—') + '</td>' +
        '<td>$' + (p.value || 0).toFixed(3) + '</td>' +
        '<td class="' + pnlCls + '">$' + (p.cashPnl || 0).toFixed(3) + '</td>' +
        '<td class="' + pnlCls + '">' + (p.percentPnl || 0).toFixed(1) + '%</td>' +
        '<td>' + (p.redeemable ? '✅' : '—') + '</td>';
      ocBody.appendChild(row);
    });
    document.getElementById('onchain-summary').textContent =
      'positions: ' + oc.length + ' · total value: $' + ocTotalValue.toFixed(3) +
      ' · total unrealized PnL: $' + ocTotalPnl.toFixed(3);

    // Order books
    const booksDiv = document.getElementById('books');
    booksDiv.innerHTML = '';
    const books = d.orderbooks || {};
    Object.entries(books).forEach(([tid, b]) => {
      const box = document.createElement('div');
      box.className = 'book';
      let html = '<div class="book-title">' + tid.slice(0, 16) + '…</div>';
      if (b._error) {
        html += '<div class="neg">err: ' + b._error + '</div>';
      } else {
        // asks descending (lowest at bottom near spread)
        (b.asks || []).slice().reverse().forEach(a => {
          html += '<div class="book-side book-ask"><span>' + a.price + '</span><span>' + a.size + '</span></div>';
        });
        html += '<div class="book-spread">spread ' + (b.spread !== null ? b.spread : '—') + '</div>';
        (b.bids || []).forEach(bd => {
          html += '<div class="book-side book-bid"><span>' + bd.price + '</span><span>' + bd.size + '</span></div>';
        });
      }
      box.innerHTML = html;
      booksDiv.appendChild(box);
    });

  } catch (e) {
    document.getElementById('refresh-status').textContent = '· error: ' + e.message;
  }
}
refresh();
setInterval(refresh, 5000);
</script>
</body></html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # quiet

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
            return
        if u.path == "/api/status":
            try:
                state = collect_state()
                body = json.dumps(state).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(exc)}).encode())
            return
        self.send_response(404)
        self.end_headers()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--host", default="0.0.0.0")
    args = p.parse_args()
    server = HTTPServer((args.host, args.port), DashboardHandler)
    print(f"dashboard listening at http://{args.host}:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
