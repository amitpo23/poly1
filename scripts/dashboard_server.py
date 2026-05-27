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


def _yahoo_chart(symbol: str, range_: str = "1d", interval: str = "5m") -> dict:
    """Fetch Yahoo Finance chart data."""
    sym = symbol.replace("^", "%5E")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={range_}&interval={interval}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        d = json.loads(resp.read().decode())
    r = d.get("chart", {}).get("result", [{}])[0]
    if not r:
        return {"symbol": symbol, "error": "no data"}
    meta = r.get("meta", {})
    closes = r.get("indicators", {}).get("quote", [{}])[0].get("close", []) or []
    opens = r.get("indicators", {}).get("quote", [{}])[0].get("open", []) or []
    highs = r.get("indicators", {}).get("quote", [{}])[0].get("high", []) or []
    lows = r.get("indicators", {}).get("quote", [{}])[0].get("low", []) or []
    ts = r.get("timestamp", []) or []
    # Drop None entries
    bars = []
    for i, t in enumerate(ts):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        bars.append({
            "t": t,
            "o": opens[i] if i < len(opens) else c,
            "h": highs[i] if i < len(highs) else c,
            "l": lows[i] if i < len(lows) else c,
            "c": c,
        })
    last = bars[-1]["c"] if bars else None
    prev = meta.get("chartPreviousClose")
    change_pct = None
    if last and prev:
        change_pct = round(100.0 * (last - prev) / prev, 3)
    return {
        "symbol": symbol,
        "name": meta.get("longName") or meta.get("shortName") or symbol,
        "price": last,
        "prev_close": prev,
        "change_pct": change_pct,
        "currency": meta.get("currency"),
        "bars": bars[-200:],  # last 200 bars for performance
    }


def fetch_macro() -> dict:
    """Fetch macro context: crypto + indices + rates."""
    symbols = {
        "BTC-USD": "1d",
        "ETH-USD": "1d",
        "SOL-USD": "1d",
        "^GSPC": "1d",
        "^NDX": "1d",
        "^VIX": "1d",
        "^TNX": "1d",
        "DX-Y.NYB": "1d",
        "GC=F": "1d",
    }
    out = {}
    for sym, rng in symbols.items():
        try:
            interval = "5m" if rng == "1d" else "1d"
            out[sym] = _yahoo_chart(sym, range_=rng, interval=interval)
        except Exception as exc:
            out[sym] = {"symbol": sym, "error": str(exc)[:60]}
    return out


def fetch_news(symbol: str = "BTC-USD", count: int = 8) -> list:
    """Fetch Yahoo Finance news for a symbol via search API."""
    sym = symbol.replace("^", "%5E")
    url = f"https://query1.finance.yahoo.com/v1/finance/search?q={sym}&newsCount={count}&quotesCount=0"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            d = json.loads(resp.read().decode())
    except Exception as exc:
        return [{"_error": str(exc)[:80]}]
    items = d.get("news", []) or []
    out = []
    for it in items[:count]:
        out.append({
            "title": it.get("title"),
            "publisher": it.get("publisher"),
            "link": it.get("link"),
            "pub_time": it.get("providerPublishTime"),
            "type": it.get("type"),
        })
    return out


def fetch_all_news() -> dict:
    """Aggregate news from BTC, ETH, general market."""
    return {
        "BTC-USD": CACHE.get_or_fetch("news:btc", 300.0, lambda: fetch_news("BTC-USD", 6)),
        "ETH-USD": CACHE.get_or_fetch("news:eth", 300.0, lambda: fetch_news("ETH-USD", 6)),
        "^GSPC": CACHE.get_or_fetch("news:spx", 300.0, lambda: fetch_news("%5EGSPC", 6)),
    }


def claude_analyze(prompt_text: str, dashboard_state: dict) -> dict:
    """Call Claude with a digest of the dashboard state for context analysis."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY not configured"}
    digest_lines = []
    rt = dashboard_state.get("runtime", {})
    digest_lines.append(f"runtime mode: {rt.get('mode')}, halt: {rt.get('halt')}, allowed agents: {rt.get('allowed_live_agents')}")
    tot = dashboard_state.get("totals", {})
    digest_lines.append(f"day PnL closed: ${tot.get('day_pnl_closed')}")
    digest_lines.append(f"agent stats: {json.dumps(dashboard_state.get('agents', {}))[:600]}")
    onchain = dashboard_state.get("onchain", []) or []
    if isinstance(onchain, list):
        tot_val = sum(p.get("value", 0) for p in onchain if isinstance(p, dict))
        tot_pnl = sum(p.get("cashPnl", 0) for p in onchain if isinstance(p, dict))
        digest_lines.append(f"on-chain: {len(onchain)} positions, total value ${tot_val:.2f}, total cash PnL ${tot_pnl:.2f}")
    digest_lines.append(f"open positions in journal: {len(dashboard_state.get('open_positions') or [])}")
    body = json.dumps({
        "model": "claude-opus-4-7",
        "max_tokens": 800,
        "messages": [{
            "role": "user",
            "content": (
                "You are an analyst reviewing a trading bot's live dashboard. "
                "Provide factual observations and risk awareness only. "
                "Do NOT give buy/sell/hold recommendations. "
                "Use neutral language like 'capital exposure is elevated', "
                "'win-rate trend is positive', 'spread risk on these tokens', etc.\n\n"
                f"User question: {prompt_text}\n\n"
                f"Dashboard snapshot:\n" + "\n".join(digest_lines)
            ),
        }],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            d = json.loads(resp.read().decode())
        text = ""
        for block in d.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        return {"text": text, "model": d.get("model"), "stop_reason": d.get("stop_reason")}
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}: {exc.read().decode()[:200]}"}
    except Exception as exc:
        return {"error": str(exc)[:200]}


_UUID_RE = __import__("re").compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _attribute(cycle_id: str) -> str:
    """Map cycle_id prefix to a readable agent label."""
    if not cycle_id:
        return "(none)"
    # Order matters: more-specific prefixes first.
    prefix_map = (
        ("btc5min_timed_v3:", "amit_v3"),
        ("btc5min_timed_v2:", "amit_v2"),
        ("btc5min_timed:", "amit_v1"),
        ("btc_5min:", "btc_5min"),
        ("btc_daily:", "btc_daily"),
        ("scanner_executor:", "scanner_executor"),
        ("trader:", "trader"),
        ("trading_supervisor:", "supervisor"),
        ("close:", "close"),
        ("scalper", "scalper"),
        ("near_resolution", "near_resolution"),
        ("news_shock", "news_shock"),
        ("external_conviction_api", "ec_api"),
        ("external_conviction_polifly", "ec_polifly"),
        ("external_conviction_whale", "ec_whale"),
        ("external_conviction_divergence", "ec_divergence"),
        ("external_conviction_debate", "ec_debate"),
        ("external_conviction_aggregator", "ec_aggregator"),
        ("external_conviction_tradingview", "ec_tradingview"),
        ("external_conviction_crypto_tape", "ec_crypto_tape"),
        ("external_conviction_alpaca", "ec_alpaca"),
        ("external_conviction_openbb", "ec_openbb"),
        ("external_conviction_technical", "ec_technical"),
        ("external_conviction_gdelt", "ec_gdelt"),
        ("external_conviction_crypto_deriv", "ec_crypto_deriv"),
        ("external_conviction:", "ec_main"),
        ("opportunity_factory", "opportunity_factory"),
        ("resolution_sync", "resolution_sync"),
        ("phantom_sweep", "phantom_sweep"),
        ("market_scanner", "market_scanner"),
        ("brain_indicator", "brain_indicator"),
    )
    for prefix, label in prefix_map:
        if cycle_id.startswith(prefix):
            return label
    # UUID-style cycle_ids are wallet_follow's signature.
    if _UUID_RE.match(cycle_id):
        return "wallet_follow"
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
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>poly1 — trading dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<style>
  :root {
    --bg: #0a0e13; --panel: #131822; --border: #1f2937; --border-strong: #2d3741;
    --text: #e4e7eb; --muted: #9ca3af; --dim: #6b7280;
    --pos: #34d399; --neg: #f87171; --warn: #fbbf24; --info: #60a5fa;
    --accent: #8b5cf6;
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 0; font-size: 14px; }
  .hero { padding: 14px 18px; background: linear-gradient(135deg, #0f1419 0%, #1a1f2e 100%); border-bottom: 1px solid var(--border-strong); display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
  .hero-title { display: flex; align-items: center; gap: 12px; }
  .hero-title h1 { margin: 0; font-size: 18px; font-weight: 700; letter-spacing: -0.3px; }
  .hero-badge { padding: 4px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
  .hero-mode-live { background: #065f46; color: #d1fae5; }
  .hero-mode-freeze { background: #78350f; color: #fef3c7; }
  .hero-halt { background: #991b1b; color: #fee2e2; }
  .hero-metrics { display: flex; gap: 18px; }
  .hero-metric { text-align: right; }
  .hero-metric .val { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .hero-metric .lbl { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
  .tabs { display: flex; background: var(--panel); border-bottom: 1px solid var(--border-strong); padding: 0 12px; overflow-x: auto; }
  .tab { padding: 12px 16px; cursor: pointer; color: var(--muted); border-bottom: 2px solid transparent; font-size: 13px; font-weight: 500; white-space: nowrap; transition: color 0.15s; }
  .tab:hover { color: var(--text); }
  .tab.active { color: var(--text); border-bottom-color: var(--info); }
  .content { padding: 16px; }
  .page { display: none; }
  .page.active { display: block; }
  h2 { font-size: 11px; margin: 0 0 8px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px; font-weight: 600; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
  .grid-4 { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
  .card-elev { background: linear-gradient(180deg, #161c28 0%, #131822 100%); }
  .full { grid-column: 1 / -1; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; font-variant-numeric: tabular-nums; }
  th, td { padding: 6px 8px; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px; font-size: 10px; }
  td.mono { font-family: ui-monospace, "SF Mono", Menlo, monospace; }
  .pos { color: var(--pos); } .neg { color: var(--neg); } .warn { color: var(--warn); } .info { color: var(--info); }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 10px; font-weight: 600; letter-spacing: 0.3px; }
  .b-tp { background: #065f46; color: #d1fae5; }
  .b-sl { background: #991b1b; color: #fee2e2; }
  .b-open { background: #1e40af; color: #dbeafe; }
  .b-failed { background: #374151; color: #d1d5db; }
  .b-deferred { background: #78350f; color: #fef3c7; }
  .b-resolved { background: #4c1d95; color: #ede9fe; }
  .metric-lg { font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .metric { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
  .stale { color: var(--neg); } .fresh { color: var(--pos); }
  #refresh-status { font-size: 10px; color: var(--muted); margin-left: 12px; }
  .live-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--pos); margin-right: 6px; vertical-align: middle; animation: pulse 1.4s infinite; box-shadow: 0 0 8px var(--pos); }
  .live-dot.stale { background: var(--neg); box-shadow: 0 0 8px var(--neg); }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
  .chart-box { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 8px; min-height: 280px; }
  .gauge-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
  .gauge-box { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 6px; }
  .books-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px; }
  .book { border: 1px solid var(--border-strong); border-radius: 6px; padding: 8px; background: #0f141d; font-family: ui-monospace, monospace; font-size: 11px; }
  .book-title { font-weight: 600; color: var(--muted); margin-bottom: 6px; font-size: 10px; word-break: break-all; }
  .book-side { display: flex; justify-content: space-between; padding: 2px 4px; }
  .book-bid { color: var(--pos); } .book-ask { color: var(--neg); }
  .book-spread { background: #1f2937; padding: 4px; margin: 4px 0; text-align: center; color: var(--warn); font-weight: 600; }
  .macro-card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
  .macro-card .sym { font-size: 11px; color: var(--muted); font-weight: 600; }
  .macro-card .name { font-size: 10px; color: var(--dim); margin-bottom: 4px; }
  .macro-card .price { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .macro-card .delta { font-size: 11px; font-weight: 600; }
  .news-item { padding: 10px 0; border-bottom: 1px solid var(--border); }
  .news-item a { color: var(--text); text-decoration: none; font-weight: 500; }
  .news-item a:hover { color: var(--info); }
  .news-item .meta { font-size: 10px; color: var(--dim); margin-top: 4px; }
  .news-tabs { display: flex; gap: 4px; margin-bottom: 8px; }
  .news-tab { padding: 6px 12px; background: var(--panel); border: 1px solid var(--border); border-radius: 4px; cursor: pointer; font-size: 11px; color: var(--muted); }
  .news-tab.active { color: var(--text); background: var(--border-strong); }
  textarea#ai-prompt { width: 100%; min-height: 80px; background: #0a0e13; color: var(--text); border: 1px solid var(--border-strong); border-radius: 6px; padding: 10px; font-family: inherit; font-size: 13px; resize: vertical; }
  button.btn { background: var(--info); color: white; border: none; padding: 8px 16px; border-radius: 6px; font-weight: 600; cursor: pointer; font-size: 13px; }
  button.btn:hover { background: #3b82f6; }
  button.btn:disabled { background: var(--dim); cursor: wait; }
  .ai-response { background: #0a0e13; border: 1px solid var(--border); border-radius: 6px; padding: 12px; margin-top: 12px; white-space: pre-wrap; font-size: 13px; line-height: 1.6; }
  .ai-quick { display: flex; gap: 6px; flex-wrap: wrap; margin: 8px 0; }
  .ai-quick button { background: var(--border); color: var(--text); border: 1px solid var(--border-strong); padding: 6px 10px; border-radius: 4px; cursor: pointer; font-size: 11px; }
  .ai-quick button:hover { background: var(--border-strong); }
  footer { padding: 18px 24px; border-top: 1px solid var(--border-strong); background: var(--panel); color: var(--dim); font-size: 10px; text-align: center; line-height: 1.6; margin-top: 24px; }
  .disclosure { max-width: 900px; margin: 0 auto; }
  .skeleton { background: linear-gradient(90deg, var(--border) 0%, var(--border-strong) 50%, var(--border) 100%); background-size: 200% 100%; animation: shimmer 1.4s infinite; height: 16px; border-radius: 4px; }
  @keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 10px; background: var(--border); color: var(--text); font-size: 11px; }
</style>
</head><body>

<div class="hero">
  <div class="hero-title">
    <h1>📈 poly1 trading terminal</h1>
    <span id="hero-mode" class="hero-badge hero-mode-freeze">—</span>
    <span id="hero-halt" class="hero-badge hero-halt" style="display:none">HALT</span>
    <span class="live-dot" id="live-dot"></span>
    <span id="refresh-status"></span>
  </div>
  <div class="hero-metrics">
    <div class="hero-metric"><div id="hero-pnl" class="val">—</div><div class="lbl">day PnL</div></div>
    <div class="hero-metric"><div id="hero-equity" class="val">—</div><div class="lbl">on-chain $</div></div>
    <div class="hero-metric"><div id="hero-positions" class="val">—</div><div class="lbl">positions</div></div>
    <div class="hero-metric"><div id="hero-btc" class="val">—</div><div class="lbl">BTC-USD</div></div>
  </div>
</div>

<div class="tabs">
  <div class="tab active" data-page="live">🟢 Live</div>
  <div class="tab" data-page="markets">📊 Markets</div>
  <div class="tab" data-page="agents">🤖 Agents</div>
  <div class="tab" data-page="portfolio">💼 Portfolio</div>
  <div class="tab" data-page="books">📖 Order Books</div>
  <div class="tab" data-page="macro">🌍 Macro</div>
  <div class="tab" data-page="news">📰 News</div>
  <div class="tab" data-page="ai">🧠 AI Analyst</div>
</div>

<div class="content">

<!-- ========== LIVE ========== -->
<div class="page active" id="page-live">
  <div class="grid">
    <div class="card card-elev">
      <h2>Runtime</h2>
      <div id="runtime"></div>
    </div>
    <div class="card card-elev">
      <h2>Day Totals</h2>
      <div id="totals"></div>
    </div>
    <div class="card full">
      <h2>Open Positions</h2>
      <table id="open-tbl"><thead><tr>
        <th>id</th><th>time</th><th>agent</th><th>side</th><th>entry</th><th>now bid</th><th>$ size</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <div class="card full">
      <h2>Recent Trades</h2>
      <table id="recent-tbl"><thead><tr>
        <th>id</th><th>time</th><th>agent</th><th>side</th><th>status</th><th>price</th><th>PnL</th>
      </tr></thead><tbody></tbody></table>
    </div>
    <div class="card">
      <h2>Heartbeats</h2>
      <div id="hb"></div>
    </div>
    <div class="card">
      <h2>Swarm (dryrun)</h2>
      <div id="swarm"></div>
    </div>
  </div>
</div>

<!-- ========== MARKETS ========== -->
<div class="page" id="page-markets">
  <div class="grid">
    <div class="chart-box full" id="chart-btc"></div>
    <div class="chart-box" id="chart-eth"></div>
    <div class="chart-box" id="chart-sol"></div>
    <div class="card full">
      <h2>Live Polymarket markets (top 20 by 24h volume)</h2>
      <table id="markets-tbl"><thead><tr>
        <th>Question</th><th>YES</th><th>NO</th><th>24h vol</th><th>liquidity</th><th>ends</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>
</div>

<!-- ========== AGENTS ========== -->
<div class="page" id="page-agents">
  <div class="card full">
    <h2>Today — neutral framing (Strategy strength = activity intensity + win rate, NOT a buy signal)</h2>
    <table id="agents-tbl"><thead><tr>
      <th>Agent</th><th>Entries</th><th>Wins</th><th>Losses</th><th>WR</th><th>Net PnL</th>
    </tr></thead><tbody></tbody></table>
  </div>
  <div class="gauge-grid" id="agent-gauges"></div>
</div>

<!-- ========== PORTFOLIO ========== -->
<div class="page" id="page-portfolio">
  <div class="grid">
    <div class="card">
      <h2>On-chain summary</h2>
      <div id="onchain-summary-card"></div>
    </div>
    <div class="chart-box" id="chart-allocation"></div>
    <div class="card full">
      <h2>🔗 On-chain CTF positions (live from Polymarket)</h2>
      <table id="onchain-tbl"><thead><tr>
        <th>Market</th><th>Outcome</th><th>Size</th><th>Avg</th><th>Now</th><th>Value</th><th>PnL $</th><th>PnL %</th><th>Redeem?</th>
      </tr></thead><tbody></tbody></table>
    </div>
  </div>
</div>

<!-- ========== BOOKS ========== -->
<div class="page" id="page-books">
  <h2>📖 Live order books for tokens we hold (refresh 15s)</h2>
  <div id="books" class="books-grid"></div>
</div>

<!-- ========== MACRO ========== -->
<div class="page" id="page-macro">
  <div class="grid-4" id="macro-cards"></div>
  <div class="grid" style="margin-top:16px">
    <div class="chart-box full" id="chart-vix"></div>
    <div class="chart-box" id="chart-tnx"></div>
    <div class="chart-box" id="chart-spx"></div>
  </div>
</div>

<!-- ========== NEWS ========== -->
<div class="page" id="page-news">
  <div class="news-tabs">
    <div class="news-tab active" data-news="BTC-USD">BTC</div>
    <div class="news-tab" data-news="ETH-USD">ETH</div>
    <div class="news-tab" data-news="^GSPC">S&amp;P 500</div>
  </div>
  <div class="card" id="news-feed"></div>
</div>

<!-- ========== AI ANALYST ========== -->
<div class="page" id="page-ai">
  <div class="card">
    <h2>🧠 Claude analyst (factual observations only — no buy/sell)</h2>
    <textarea id="ai-prompt" placeholder="Ask about the current dashboard state. Example: 'What is the current capital exposure and which agent is showing the strongest activity?'"></textarea>
    <div class="ai-quick">
      <button onclick="setAIPrompt('Summarize the current bot state in 3 bullet points.')">Summary</button>
      <button onclick="setAIPrompt('What risks do you see in the current on-chain position mix?')">Risk scan</button>
      <button onclick="setAIPrompt('Which agent shows the strongest win-rate vs entries activity today?')">Top agent</button>
      <button onclick="setAIPrompt('Are there any stuck positions that look unrecoverable?')">Stuck check</button>
    </div>
    <button class="btn" id="ai-go">Ask Claude</button>
    <div class="ai-response" id="ai-response" style="display:none"></div>
  </div>
</div>

</div>

<footer>
  <div class="disclosure">
    <strong>Educational and operational use only.</strong> This dashboard displays the state of an automated trading bot and is not financial advice, not a recommendation to buy or sell any security or prediction-market contract, and is not personalized to your situation. Gauge labels such as "Strategy strength" and "Risk score" are neutral descriptive metrics computed from activity and win-rate data only — they do not imply any forward-looking opinion. Verify all values on-chain before acting. Consult a licensed advisor before making investment decisions.
  </div>
</footer>

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
// ========== Plotly defaults ==========
const PLOTLY_LAYOUT = {
  paper_bgcolor: '#131822', plot_bgcolor: '#131822',
  font: { color: '#e4e7eb', family: '-apple-system, sans-serif', size: 11 },
  xaxis: { gridcolor: '#1f2937', linecolor: '#374151', tickfont: { size: 10 } },
  yaxis: { gridcolor: '#1f2937', linecolor: '#374151', tickfont: { size: 10 } },
  margin: { l: 40, r: 16, t: 30, b: 30 },
};
const PLOTLY_CONFIG = { displayModeBar: false, responsive: true };

// ========== Tab switching ==========
let currentTab = 'live';
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    document.querySelectorAll('.page').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    const page = t.dataset.page;
    document.getElementById('page-' + page).classList.add('active');
    currentTab = page;
    onTabActivate(page);
  });
});

function onTabActivate(page) {
  if (page === 'markets') refreshMarketsCharts();
  if (page === 'macro') refreshMacro();
  if (page === 'news') refreshNews(activeNewsTopic);
  if (page === 'portfolio') drawAllocationChart();
  if (page === 'agents') drawAgentGauges();
}

// ========== Plotly helpers ==========
function drawSparkline(divId, bars, color) {
  if (!bars || !bars.length) return;
  const x = bars.map(b => new Date(b.t * 1000));
  const y = bars.map(b => b.c);
  const data = [{
    x, y, type: 'scatter', mode: 'lines',
    line: { color: color || '#60a5fa', width: 1.5 }, fill: 'tozeroy',
    fillcolor: color ? color.replace(')', ', 0.15)').replace('rgb', 'rgba') : 'rgba(96,165,250,0.12)',
    hovertemplate: '%{x|%H:%M}<br>%{y:.2f}<extra></extra>',
  }];
  const layout = {
    ...PLOTLY_LAYOUT,
    margin: { l: 0, r: 0, t: 0, b: 0 },
    xaxis: { ...PLOTLY_LAYOUT.xaxis, showgrid: false, showticklabels: false },
    yaxis: { ...PLOTLY_LAYOUT.yaxis, showgrid: false, showticklabels: false },
    showlegend: false, height: 40,
  };
  Plotly.react(divId, data, layout, PLOTLY_CONFIG);
}

function drawPriceChart(divId, bars, title, color) {
  if (!bars || !bars.length) {
    document.getElementById(divId).innerHTML = '<div class="label" style="text-align:center;padding:40px">no data</div>';
    return;
  }
  const first = bars[0].c;
  const x = bars.map(b => new Date(b.t * 1000));
  const y = bars.map(b => b.c);
  const last = y[y.length - 1];
  const pctChg = ((last - first) / first * 100);
  const upColor = '#34d399', downColor = '#f87171';
  const lineColor = pctChg >= 0 ? upColor : downColor;
  const data = [{
    x, y, type: 'scatter', mode: 'lines',
    line: { color: lineColor, width: 1.8 }, fill: 'tozeroy',
    fillcolor: pctChg >= 0 ? 'rgba(52, 211, 153, 0.10)' : 'rgba(248, 113, 113, 0.10)',
    hovertemplate: '%{x|%H:%M}<br>$%{y:.2f}<extra></extra>',
  }];
  const layout = {
    ...PLOTLY_LAYOUT,
    height: 260,
    title: { text: title + ' · $' + last.toFixed(2) + ' · ' + (pctChg >= 0 ? '+' : '') + pctChg.toFixed(2) + '%', font: { size: 13 } },
    yaxis: { ...PLOTLY_LAYOUT.yaxis, rangemode: 'normal' },
  };
  Plotly.react(divId, data, layout, PLOTLY_CONFIG);
}

function drawGauge(divId, value, title, subtitle) {
  // value 0-100; red 0-30, yellow 30-60, green 60-100
  const data = [{
    type: 'indicator', mode: 'gauge+number',
    value: value,
    number: { font: { size: 22, color: '#e4e7eb' }, suffix: '' },
    gauge: {
      axis: { range: [0, 100], tickwidth: 1, tickcolor: '#374151', tickfont: { size: 9 } },
      bar: { color: '#60a5fa', thickness: 0.18 },
      bgcolor: '#0a0e13',
      borderwidth: 0,
      steps: [
        { range: [0, 30], color: '#7f1d1d' },
        { range: [30, 60], color: '#78350f' },
        { range: [60, 100], color: '#065f46' },
      ],
      threshold: { line: { color: '#fbbf24', width: 2 }, thickness: 0.85, value: value },
    },
  }];
  const layout = {
    ...PLOTLY_LAYOUT,
    height: 170,
    margin: { l: 20, r: 20, t: 40, b: 0 },
    title: { text: '<b>' + title + '</b><br><span style="font-size:10px;color:#9ca3af">' + (subtitle || '') + '</span>', font: { size: 12 } },
  };
  Plotly.react(divId, data, layout, PLOTLY_CONFIG);
}

// ========== Main status refresh ==========
let lastStatus = null;
let lastRefreshTs = Date.now();
async function refresh() {
  try {
    const r = await fetch('/api/status?t=' + Date.now());
    const d = await r.json();
    lastStatus = d;
    lastRefreshTs = Date.now();
    document.getElementById('live-dot').classList.remove('stale');
    document.getElementById('refresh-status').textContent = '· LIVE · ' + new Date(d.ts).toLocaleTimeString();

    // Hero
    const rt = d.runtime || {};
    const heroMode = document.getElementById('hero-mode');
    heroMode.textContent = (rt.mode || '—').toUpperCase();
    heroMode.className = 'hero-badge ' + (rt.mode === 'live' ? 'hero-mode-live' : 'hero-mode-freeze');
    document.getElementById('hero-halt').style.display = rt.halt ? 'inline-block' : 'none';
    const dp = d.totals && d.totals.day_pnl_closed || 0;
    const pnlEl = document.getElementById('hero-pnl');
    pnlEl.textContent = '$' + dp.toFixed(2);
    pnlEl.className = 'val ' + (dp >= 0 ? 'pos' : 'neg');
    const oc = Array.isArray(d.onchain) ? d.onchain : [];
    const ocVal = oc.reduce((a, p) => a + (p.value || 0), 0);
    document.getElementById('hero-equity').textContent = '$' + ocVal.toFixed(2);
    document.getElementById('hero-positions').textContent = (d.open_positions || []).length + ' / ' + oc.length;

    // Runtime card
    document.getElementById('runtime').innerHTML =
      '<div class="metric ' + (rt.mode === 'live' ? 'pos' : 'warn') + '">' + (rt.mode || '—').toUpperCase() + (rt.halt ? ' · HALT' : '') + '</div>' +
      '<div class="label">expires: ' + (rt.expires_at || '—') + '</div>' +
      '<div class="label">agents: ' + ((rt.allowed_live_agents || []).join(', ') || '—') + '</div>' +
      '<div class="label">budget: $' + (rt.budget_usdc || 0) + '</div>';

    // Totals card
    document.getElementById('totals').innerHTML =
      '<div class="metric-lg ' + (dp >= 0 ? 'pos' : 'neg') + '">$' + dp.toFixed(3) + '</div>' +
      '<div class="label">closed PnL today</div>' +
      '<div style="margin-top:8px"><span class="pill">' + (d.recent_trades || []).filter(t => t.status === 'closed_take_profit').length + ' TP today</span> ' +
      '<span class="pill">' + (d.recent_trades || []).filter(t => t.status === 'closed_stop_loss').length + ' SL today</span></div>';

    // Agents table — atomic update (build HTML, assign once → no flicker)
    let agentsHtml = '';
    Object.entries(d.agents || {}).sort((a, b) => (b[1].net_pnl||0) - (a[1].net_pnl||0))
      .forEach(([name, st]) => {
        const pnlCls = (st.net_pnl||0) >= 0 ? 'pos' : 'neg';
        agentsHtml +=
          '<tr><td><strong>' + name + '</strong></td>' +
          '<td class="mono">' + (st.entries_today || 0) + '</td>' +
          '<td class="mono pos">' + (st.wins || 0) + '</td>' +
          '<td class="mono neg">' + (st.losses || 0) + '</td>' +
          '<td class="mono">' + ((st.wr || 0) * 100).toFixed(0) + '%</td>' +
          '<td class="mono ' + pnlCls + '">$' + (st.net_pnl || 0).toFixed(3) + '</td></tr>';
      });
    if (!agentsHtml) agentsHtml = '<tr><td colspan="6" class="label" style="text-align:center;padding:16px">no agent activity today</td></tr>';
    document.getElementById('agents-tbl').querySelector('tbody').innerHTML = agentsHtml;

    // Open positions — atomic
    let openHtml = '';
    (d.open_positions || []).forEach(p => {
      const sideCls = p.side === 'BUY' ? 'pos' : 'neg';
      openHtml +=
        '<tr><td class="mono">' + p.id + '</td>' +
        '<td class="mono">' + p.ts.slice(11, 19) + '</td>' +
        '<td>' + p.agent + '</td>' +
        '<td class="mono ' + sideCls + '">' + p.side + '</td>' +
        '<td class="mono">' + p.entry + '</td>' +
        '<td class="mono">' + (p.current_bid !== null ? p.current_bid : '—') + '</td>' +
        '<td class="mono">$' + p.size_usdc + '</td></tr>';
    });
    if (!openHtml) openHtml = '<tr><td colspan="7" class="label" style="text-align:center;padding:16px">no open positions</td></tr>';
    document.getElementById('open-tbl').querySelector('tbody').innerHTML = openHtml;

    // Recent trades — atomic
    let recHtml = '';
    (d.recent_trades || []).forEach(t => {
      let badge = '';
      if (t.status === 'closed_take_profit') badge = '<span class="badge b-tp">TP</span>';
      else if (t.status === 'closed_stop_loss') badge = '<span class="badge b-sl">SL</span>';
      else if (t.status === 'failed') badge = '<span class="badge b-failed">FAIL</span>';
      else if (t.status === 'exit_deferred') badge = '<span class="badge b-deferred">DEF</span>';
      else if (t.status.endsWith('open')) badge = '<span class="badge b-open">OPEN</span>';
      else if (t.status.startsWith('resolved')) badge = '<span class="badge b-resolved">RES</span>';
      else badge = '<span class="badge b-failed">' + t.status + '</span>';
      let pnlHtml = '—';
      if (t.pnl_usdc !== null) {
        const cls2 = t.pnl_usdc >= 0 ? 'pos' : 'neg';
        pnlHtml = '<span class="mono ' + cls2 + '">$' + t.pnl_usdc.toFixed(3) + '</span>';
      }
      recHtml +=
        '<tr><td class="mono">' + t.id + '</td>' +
        '<td class="mono">' + t.ts.slice(11, 19) + '</td>' +
        '<td>' + t.agent + '</td>' +
        '<td class="mono">' + (t.side || '—') + '</td>' +
        '<td>' + badge + '</td>' +
        '<td class="mono">' + (t.price || '—') + '</td>' +
        '<td>' + pnlHtml + '</td></tr>';
    });
    if (!recHtml) recHtml = '<tr><td colspan="7" class="label" style="text-align:center;padding:16px">no trades yet</td></tr>';
    document.getElementById('recent-tbl').querySelector('tbody').innerHTML = recHtml;

    // Heartbeats — atomic
    let hbHtml = '';
    Object.entries(d.heartbeats || {}).sort().forEach(([name, age]) => {
      const cls = age < 60 ? 'fresh' : 'stale';
      hbHtml += '<div class="' + cls + '" style="padding:2px 0">' + name + ': <span class="mono">' + age.toFixed(0) + 's</span></div>';
    });
    document.getElementById('hb').innerHTML = hbHtml || '<div class="label">no heartbeats</div>';

    // Swarm
    const s = d.swarm || {};
    document.getElementById('swarm').innerHTML =
      '<div><span class="label">fills:</span> <span class="metric">' + (s.fills !== undefined ? s.fills : '—') + '</span></div>' +
      '<div><span class="label">pnl events:</span> ' + (s.pnl_events !== undefined ? s.pnl_events : '—') + '</div>' +
      '<div class="label" style="margin-top:6px">pending: ' + (s.pending_orders ? Object.entries(s.pending_orders).map(([k,v]) => k+':'+v).join(', ') : '—') + '</div>';

    // Live markets — atomic
    let mktHtml = '';
    const markets = Array.isArray(d.markets) ? d.markets : [];
    markets.forEach(m => {
      const yes = (m.prices && m.prices[0]) || '—';
      const no = (m.prices && m.prices[1]) || '—';
      const ends = m.end_date ? new Date(m.end_date).toLocaleString() : '—';
      mktHtml +=
        '<tr><td>' + (m.question || '—') + '</td>' +
        '<td class="mono pos">' + yes + '</td>' +
        '<td class="mono neg">' + no + '</td>' +
        '<td class="mono">$' + (m.volume_24h || 0).toLocaleString() + '</td>' +
        '<td class="mono">$' + (m.liquidity || 0).toLocaleString() + '</td>' +
        '<td class="mono" style="font-size:10px">' + ends + '</td></tr>';
    });
    if (!mktHtml) mktHtml = '<tr><td colspan="6" class="label" style="text-align:center;padding:16px">loading markets…</td></tr>';
    document.getElementById('markets-tbl').querySelector('tbody').innerHTML = mktHtml;

    // On-chain positions — atomic
    let ocHtml = '', ocTotalValue = 0, ocTotalPnl = 0, redeemableCt = 0;
    oc.forEach(p => {
      ocTotalValue += p.value || 0;
      ocTotalPnl += p.cashPnl || 0;
      if (p.redeemable) redeemableCt++;
      const pnlCls = (p.cashPnl || 0) >= 0 ? 'pos' : 'neg';
      ocHtml +=
        '<tr><td>' + (p.title || '—') + '</td>' +
        '<td>' + (p.outcome || '—') + '</td>' +
        '<td class="mono">' + (p.size || 0) + '</td>' +
        '<td class="mono">' + (p.avg_price || '—') + '</td>' +
        '<td class="mono">' + (p.cur_price || '—') + '</td>' +
        '<td class="mono">$' + (p.value || 0).toFixed(3) + '</td>' +
        '<td class="mono ' + pnlCls + '">$' + (p.cashPnl || 0).toFixed(3) + '</td>' +
        '<td class="mono ' + pnlCls + '">' + (p.percentPnl || 0).toFixed(1) + '%</td>' +
        '<td>' + (p.redeemable ? '✅' : '—') + '</td></tr>';
    });
    if (!ocHtml) ocHtml = '<tr><td colspan="9" class="label" style="text-align:center;padding:16px">no on-chain positions</td></tr>';
    document.getElementById('onchain-tbl').querySelector('tbody').innerHTML = ocHtml;
    document.getElementById('onchain-summary-card').innerHTML =
      '<div class="metric-lg">' + oc.length + '</div><div class="label">total positions</div>' +
      '<div style="margin-top:10px;display:flex;gap:14px">' +
      '<div><div class="metric">$' + ocTotalValue.toFixed(2) + '</div><div class="label">current value</div></div>' +
      '<div><div class="metric ' + (ocTotalPnl >= 0 ? 'pos' : 'neg') + '">$' + ocTotalPnl.toFixed(2) + '</div><div class="label">total cash PnL</div></div>' +
      '<div><div class="metric">' + redeemableCt + '</div><div class="label">redeemable</div></div>' +
      '</div>';

    // Order books — atomic per-book HTML, single container assign
    let booksHtml = '';
    const books = d.orderbooks || {};
    Object.entries(books).forEach(([tid, b]) => {
      let inner = '<div class="book-title">' + tid.slice(0, 20) + '…</div>';
      if (b._error) {
        inner += '<div class="neg">err: ' + b._error + '</div>';
      } else {
        (b.asks || []).slice().reverse().forEach(a => {
          inner += '<div class="book-side book-ask"><span>' + a.price + '</span><span>' + a.size + '</span></div>';
        });
        inner += '<div class="book-spread">spread ' + (b.spread !== null ? b.spread : '—') + '</div>';
        (b.bids || []).forEach(bd => {
          inner += '<div class="book-side book-bid"><span>' + bd.price + '</span><span>' + bd.size + '</span></div>';
        });
      }
      booksHtml += '<div class="book">' + inner + '</div>';
    });
    if (!booksHtml) booksHtml = '<div class="label" style="grid-column:1/-1;text-align:center;padding:20px">no open positions to show order books for</div>';
    document.getElementById('books').innerHTML = booksHtml;

    // If on a tab that uses lastStatus, redraw
    if (currentTab === 'portfolio') drawAllocationChart();
    if (currentTab === 'agents') drawAgentGauges();

  } catch (e) {
    document.getElementById('live-dot').classList.add('stale');
    document.getElementById('refresh-status').textContent = '· error: ' + e.message;
  }
}

// Stale detector — if no refresh in 12s, mark dot red
setInterval(() => {
  if (Date.now() - lastRefreshTs > 12000) {
    document.getElementById('live-dot').classList.add('stale');
  }
}, 2000);

// ========== Macro (yfinance) ==========
let macroData = null;
async function refreshMacro() {
  try {
    const r = await fetch('/api/macro?t=' + Date.now());
    const d = await r.json();
    macroData = d;
    // Cards — atomic (build HTML, assign once)
    const order = ['BTC-USD', 'ETH-USD', 'SOL-USD', '^GSPC', '^NDX', '^VIX', '^TNX', 'DX-Y.NYB', 'GC=F'];
    const sparkUpdates = [];
    let cardsHtml = '';
    order.forEach(sym => {
      const m = d[sym];
      if (!m || m.error) return;
      const sparkId = 'spark-' + sym.replace(/[\^=\.\-]/g, '_');
      const chg = m.change_pct || 0;
      const chgCls = chg >= 0 ? 'pos' : 'neg';
      cardsHtml +=
        '<div class="macro-card">' +
        '<div class="sym">' + sym + '</div>' +
        '<div class="name">' + (m.name || '').slice(0, 24) + '</div>' +
        '<div class="price">' + (m.price !== null ? m.price.toFixed(2) : '—') + '</div>' +
        '<div class="delta ' + chgCls + '">' + (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%</div>' +
        '<div id="' + sparkId + '" style="height:40px;margin-top:4px"></div>' +
        '</div>';
      sparkUpdates.push({ id: sparkId, bars: m.bars, color: chg >= 0 ? '#34d399' : '#f87171' });
    });
    document.getElementById('macro-cards').innerHTML = cardsHtml;
    sparkUpdates.forEach(s => drawSparkline(s.id, s.bars, s.color));
    // Charts
    if (d['^VIX']) drawPriceChart('chart-vix', d['^VIX'].bars, 'VIX (volatility)');
    if (d['^TNX']) drawPriceChart('chart-tnx', d['^TNX'].bars, '10Y yield');
    if (d['^GSPC']) drawPriceChart('chart-spx', d['^GSPC'].bars, 'S&P 500');

    // Update hero BTC
    if (d['BTC-USD'] && d['BTC-USD'].price) {
      const btc = d['BTC-USD'];
      const cls = (btc.change_pct || 0) >= 0 ? 'pos' : 'neg';
      document.getElementById('hero-btc').innerHTML = '$' + btc.price.toFixed(0) + ' <span class="' + cls + '" style="font-size:12px">' + ((btc.change_pct||0) >= 0 ? '+' : '') + (btc.change_pct||0).toFixed(1) + '%</span>';
    }
  } catch (e) {
    console.error('macro err', e);
  }
}

// Markets tab charts
function refreshMarketsCharts() {
  if (!macroData) refreshMacro();
  setTimeout(() => {
    if (macroData) {
      if (macroData['BTC-USD']) drawPriceChart('chart-btc', macroData['BTC-USD'].bars, 'BTC-USD');
      if (macroData['ETH-USD']) drawPriceChart('chart-eth', macroData['ETH-USD'].bars, 'ETH-USD');
      if (macroData['SOL-USD']) drawPriceChart('chart-sol', macroData['SOL-USD'].bars, 'SOL-USD');
    }
  }, 200);
}

// ========== Agents gauges ==========
function drawAgentGauges() {
  if (!lastStatus) return;
  const gaugesDiv = document.getElementById('agent-gauges');
  gaugesDiv.innerHTML = '';
  const agents = lastStatus.agents || {};
  Object.entries(agents).forEach(([name, st]) => {
    const box = document.createElement('div');
    box.className = 'gauge-box';
    const g1 = 'gauge-strength-' + name.replace(/[^a-z0-9]/gi, '_');
    const g2 = 'gauge-risk-' + name.replace(/[^a-z0-9]/gi, '_');
    box.innerHTML =
      '<div class="label" style="text-align:center;padding:4px 0">' + name + '</div>' +
      '<div id="' + g1 + '" style="height:170px"></div>' +
      '<div id="' + g2 + '" style="height:170px"></div>';
    gaugesDiv.appendChild(box);
    // Strategy strength = WR * activity factor (capped 100). Neutral metric.
    const wr = (st.wr || 0) * 100;
    const activity = Math.min(100, (st.entries_today || 0) * 5);
    const strength = Math.round((wr * 0.6 + activity * 0.4));
    // Risk: high if entries with no wins; low if balanced
    const trades = (st.wins || 0) + (st.losses || 0);
    const lossPenalty = trades > 0 ? Math.min(100, (st.losses / trades) * 100) : 0;
    const risk = Math.round(Math.max(0, 100 - lossPenalty));
    setTimeout(() => {
      drawGauge(g1, strength, 'Strategy strength', 'WR × activity');
      drawGauge(g2, risk, 'Risk profile', 'inverse loss rate');
    }, 0);
  });
}

// Allocation pie chart
function drawAllocationChart() {
  if (!lastStatus) return;
  const oc = Array.isArray(lastStatus.onchain) ? lastStatus.onchain : [];
  const top = oc.filter(p => (p.value || 0) > 0.05).slice(0, 10);
  if (!top.length) {
    document.getElementById('chart-allocation').innerHTML = '<div class="label" style="text-align:center;padding:40px">no allocations</div>';
    return;
  }
  const data = [{
    type: 'pie', hole: 0.5,
    labels: top.map(p => (p.title || '').slice(0, 24)),
    values: top.map(p => p.value),
    textinfo: 'percent',
    textfont: { size: 10, color: '#e4e7eb' },
    marker: { colors: ['#60a5fa', '#34d399', '#fbbf24', '#f87171', '#8b5cf6', '#ec4899', '#14b8a6', '#f59e0b', '#a78bfa', '#22d3ee'] },
    hovertemplate: '%{label}<br>$%{value:.3f}<br>%{percent}<extra></extra>',
  }];
  const layout = {
    ...PLOTLY_LAYOUT, height: 280,
    title: { text: 'Top positions by value', font: { size: 13 } },
    showlegend: false,
  };
  Plotly.react('chart-allocation', data, layout, PLOTLY_CONFIG);
}

// ========== News ==========
let activeNewsTopic = 'BTC-USD';
let newsCache = null;
async function refreshNews(topic) {
  activeNewsTopic = topic;
  document.querySelectorAll('.news-tab').forEach(t => t.classList.toggle('active', t.dataset.news === topic));
  const feed = document.getElementById('news-feed');
  if (!newsCache) {
    feed.innerHTML = '<div class="skeleton" style="margin:8px 0"></div>'.repeat(5);
    try {
      const r = await fetch('/api/news?t=' + Date.now());
      newsCache = await r.json();
    } catch (e) {
      feed.innerHTML = '<div class="neg">news error: ' + e.message + '</div>';
      return;
    }
  }
  const items = newsCache[topic] || [];
  feed.innerHTML = '';
  if (!items.length || items[0]._error) {
    feed.innerHTML = '<div class="label">no news available</div>';
    return;
  }
  items.forEach(it => {
    const ago = it.pub_time ? Math.round((Date.now() / 1000 - it.pub_time) / 60) : null;
    const agoStr = ago !== null ? (ago < 60 ? ago + 'm ago' : Math.round(ago / 60) + 'h ago') : '';
    const div = document.createElement('div');
    div.className = 'news-item';
    div.innerHTML =
      '<a href="' + (it.link || '#') + '" target="_blank" rel="noopener">' + (it.title || '') + '</a>' +
      '<div class="meta">' + (it.publisher || '—') + ' · ' + agoStr + '</div>';
    feed.appendChild(div);
  });
}
document.querySelectorAll('.news-tab').forEach(t => {
  t.addEventListener('click', () => refreshNews(t.dataset.news));
});

// ========== AI Analyst ==========
function setAIPrompt(text) {
  document.getElementById('ai-prompt').value = text;
}
document.getElementById('ai-go').addEventListener('click', async () => {
  const btn = document.getElementById('ai-go');
  const respDiv = document.getElementById('ai-response');
  const prompt = document.getElementById('ai-prompt').value.trim();
  if (!prompt) return;
  btn.disabled = true; btn.textContent = 'Thinking…';
  respDiv.style.display = 'block';
  respDiv.textContent = 'Claude is analyzing…';
  try {
    const r = await fetch('/api/claude', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prompt }),
    });
    const d = await r.json();
    if (d.error) respDiv.innerHTML = '<span class="neg">error: ' + d.error + '</span>';
    else respDiv.textContent = d.text || '(no response)';
  } catch (e) {
    respDiv.innerHTML = '<span class="neg">network error: ' + e.message + '</span>';
  } finally {
    btn.disabled = false; btn.textContent = 'Ask Claude';
  }
});

// ========== Boot ==========
refresh();
setInterval(refresh, 5000);
refreshMacro();
setInterval(refreshMacro, 60000);  // macro refreshes 60s
</script>
</body></html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # quiet

    def _send_json(self, status: int, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

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
                self._send_json(200, collect_state())
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if u.path == "/api/macro":
            try:
                data = CACHE.get_or_fetch("macro", 60.0, fetch_macro)
                self._send_json(200, data)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        if u.path == "/api/news":
            try:
                self._send_json(200, fetch_all_news())
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/claude":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body_raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                req = json.loads(body_raw or "{}")
                user_q = (req.get("prompt") or "").strip()
                if not user_q:
                    self._send_json(400, {"error": "missing prompt"})
                    return
                state = collect_state()
                result = claude_analyze(user_q, state)
                self._send_json(200, result)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
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
