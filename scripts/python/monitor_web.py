"""Web monitor for both Polymarket bots.

Serves a single auto-refreshing HTML page at http://127.0.0.1:7777/
that shows poly1 + swarm state side-by-side. Read-only. Stdlib only.

Usage:
    python ~/Desktop/poly/monitor_web.py                # localhost:7777
    python ~/Desktop/poly/monitor_web.py --port 8080
    python ~/Desktop/poly/monitor_web.py --bind 0.0.0.0 # LAN access (be careful)

Endpoints:
    GET /          — auto-refreshing HTML dashboard (every 10s)
    GET /data.json — JSON snapshot
    GET /healthz   — readiness probe (returns 200 if both DBs reachable)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Pull state extractors from the canonical CLI monitor.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from monitor import poly1_state, swarm_state  # type: ignore
except ImportError as e:
    print(f"could not import monitor.py — make sure it lives next to this file: {e}",
          file=sys.stderr)
    sys.exit(1)


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s):
    if isinstance(s, str):
        return _ANSI.sub("", s)
    return s


def _clean(state: dict) -> dict:
    """Remove ANSI codes that monitor.py uses for terminal colors."""
    out = {}
    for k, v in state.items():
        if isinstance(v, str):
            out[k] = _strip_ansi(v)
        elif isinstance(v, list):
            out[k] = [_clean(item) if isinstance(item, dict) else item for item in v]
        elif isinstance(v, dict):
            out[k] = _clean(v)
        else:
            out[k] = v
    return out


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Polymarket Bots Monitor</title>
<meta http-equiv="refresh" content="10">
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --border: #30363d;
    --text: #c9d1d9; --dim: #8b949e;
    --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #58a6ff;
    --accent: #f0883e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px; background: var(--bg); color: var(--text);
    font: 14px/1.4 ui-monospace, "SF Mono", Monaco, Menlo, Consolas, monospace;
  }
  h1 { font-size: 16px; margin: 0 0 12px; color: var(--accent); }
  .ts { color: var(--dim); font-size: 12px; }
  .grid {
    display: grid; gap: 12px;
    grid-template-columns: repeat(auto-fit, minmax(450px, 1fr));
  }
  .card {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 6px; padding: 12px;
  }
  .card h2 {
    margin: 0 0 8px; font-size: 14px; color: var(--blue);
    display: flex; justify-content: space-between; align-items: center;
  }
  .badge {
    font-size: 11px; padding: 2px 6px; border-radius: 3px;
    background: #30363d; color: var(--text);
  }
  .badge.green { background: #1a472a; color: #56d364; }
  .badge.red   { background: #4d1212; color: #ff7b72; }
  .badge.yellow{ background: #4d3a12; color: #f0c674; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { padding: 4px 6px; border-bottom: 1px solid var(--border); text-align: left; }
  th { color: var(--dim); font-weight: normal; }
  tr:last-child td { border-bottom: none; }
  .pnl-pos { color: var(--green); }
  .pnl-neg { color: var(--red); }
  .err { color: var(--red); }
  .dim { color: var(--dim); }
  .row { display: flex; gap: 12px; margin: 4px 0; flex-wrap: wrap; }
  .kv { display: flex; gap: 4px; }
  .kv .k { color: var(--dim); }
  .footer { margin-top: 16px; font-size: 11px; color: var(--dim); text-align: center; }
  .alert { background: #4d1212; color: #ff7b72; padding: 6px 8px; border-radius: 4px; margin: 6px 0; }
</style>
</head>
<body>
<h1>Polymarket Bots Monitor <span class="ts">{TS}</span></h1>

<div class="grid">

  <div class="card">
    <h2>poly1 <span class="badge {P1_BADGE}">{P1_HB}</span></h2>
    <div class="dim">~/coding/poly1</div>
    {P1_BODY}
  </div>

  <div class="card">
    <h2>swarm <span class="badge {SW_BADGE}">{SW_HB}</span></h2>
    <div class="dim">~/Desktop/poly/bot</div>
    {SW_BODY}
  </div>

</div>

<div class="footer">
  Auto-refreshes every 10s. Read-only. Source of truth: ~/Desktop/poly/OPERATIONS.md.<br>
  JSON: <a href="/data.json" style="color:var(--blue)">/data.json</a> &middot;
  Health: <a href="/healthz" style="color:var(--blue)">/healthz</a>
</div>
</body>
</html>
"""


def _hb_badge(age_s):
    if age_s is None:
        return "red", "no heartbeat"
    if age_s < 90:
        return "green", f"{int(age_s)}s ago"
    if age_s < 1800:
        return "yellow", _human_age(age_s)
    return "red", _human_age(age_s)


def _human_age(seconds):
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds/60)}m ago"
    if seconds < 86400:
        return f"{seconds/3600:.1f}h ago"
    return f"{seconds/86400:.1f}d ago"


def _pnl_html(value):
    cls = "pnl-pos" if value > 0 else ("pnl-neg" if value < 0 else "dim")
    sign = "+" if value > 0 else ("-" if value < 0 else "")
    return f'<span class="{cls}">{sign}${abs(value):.2f}</span>'


def _esc(s):
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _poly1_body(state: dict) -> str:
    if "error" in state:
        return f'<div class="err">{_esc(state["error"])}</div>'

    counts = state.get("counts_today", {}) or {}
    parts = []

    parts.append('<div class="row">')
    for k in ("submitted", "filled", "skipped_dry_run", "skipped_gate",
              "skipped_dedupe", "failed", "may_have_fired"):
        n = counts.get(k, 0)
        cls = "err" if (k == "may_have_fired" and n) else "dim"
        if n:
            cls = "pnl-pos" if k in ("submitted", "filled") else cls
        parts.append(
            f'<div class="kv"><span class="k">{k}:</span>'
            f'<span class="{cls}">{n}</span></div>'
        )
    parts.append("</div>")

    if counts.get("may_have_fired"):
        parts.append(
            f'<div class="alert">⚠ {counts["may_have_fired"]} MAY_HAVE_FIRED — '
            f'verify on-chain before re-trading those markets.</div>'
        )

    parts.append(
        f'<div class="row">'
        f'<div class="kv"><span class="k">active positions:</span>'
        f'<span>{state.get("active_count", 0)}</span></div>'
        f'<div class="kv"><span class="k">capital deployed:</span>'
        f'<span>${state.get("active_capital", 0):.2f}</span></div>'
        f'</div>'
    )

    recent = state.get("recent") or []
    if recent:
        parts.append("<table><thead><tr>"
                     "<th>time</th><th>status</th><th>side</th>"
                     "<th>price</th><th>size</th><th>conf</th><th>err</th>"
                     "</tr></thead><tbody>")
        for r in recent:
            ts = (r.get("ts") or "")[:19].replace("T", " ")
            size = f"${r['size_usdc']:.2f}" if r.get("size_usdc") is not None else "-"
            price = f"{r['price']:.3f}" if r.get("price") is not None else "-"
            conf = f"{r['confidence']:.2f}" if r.get("confidence") is not None else "-"
            parts.append(
                f"<tr><td>{_esc(ts)}</td><td>{_esc(r.get('status'))}</td>"
                f"<td>{_esc(r.get('side') or '-')}</td>"
                f"<td>{_esc(price)}</td><td>{_esc(size)}</td>"
                f"<td>{_esc(conf)}</td>"
                f"<td class='dim'>{_esc((r.get('error') or '')[:30])}</td></tr>"
            )
        parts.append("</tbody></table>")
    return "".join(parts)


def _swarm_body(state: dict) -> str:
    if "error" in state:
        return f'<div class="err">{_esc(state["error"])}</div>'

    parts = []
    daily = float(state.get("daily_pnl", 0.0) or 0.0)
    parts.append(
        f'<div class="row"><div class="kv">'
        f'<span class="k">daily PnL:</span>{_pnl_html(daily)}</div></div>'
    )

    by_agent = state.get("pnl_today_by_agent") or {}
    fills = state.get("fills_today_by_agent") or {}
    if by_agent or fills:
        parts.append('<table><thead><tr><th>agent</th><th>PnL</th>'
                     '<th>events</th><th>fills</th><th>volume</th></tr></thead><tbody>')
        all_agents = set(by_agent) | set(fills)
        for a in sorted(all_agents):
            pnl = (by_agent.get(a) or {}).get("pnl", 0.0)
            ev = (by_agent.get(a) or {}).get("events", 0)
            f_n = (fills.get(a) or {}).get("fills", 0)
            f_sz = (fills.get(a) or {}).get("size", 0.0)
            parts.append(
                f'<tr><td>{_esc(a)}</td><td>{_pnl_html(pnl)}</td>'
                f'<td>{ev}</td><td>{f_n}</td><td>${f_sz:.2f}</td></tr>'
            )
        parts.append("</tbody></table>")

    nh = state.get("nh_open_positions") or []
    if nh:
        parts.append(f'<div style="margin-top:8px"><b>NothingHappens open ({len(nh)})</b></div>')
        parts.append('<table><thead><tr><th>slug</th><th>size</th>'
                     '<th>NO entry</th><th>filled</th><th>ends</th></tr></thead><tbody>')
        for p in nh:
            parts.append(
                f'<tr><td>{_esc((p.get("slug") or "")[:50])}</td>'
                f'<td>${p.get("size_usd"):.0f}</td>'
                f'<td>{p.get("no_entry"):.4f}</td>'
                f'<td>{"FILLED" if p.get("filled") else "pending"}</td>'
                f'<td>{_esc(p.get("end"))}</td></tr>'
            )
        parts.append("</tbody></table>")

    rf = state.get("recent_fills") or []
    if rf:
        parts.append('<div style="margin-top:8px"><b>recent fills</b></div>')
        parts.append('<table><thead><tr><th>time</th><th>agent</th>'
                     '<th>side</th><th>outcome</th><th>price</th><th>size</th>'
                     '<th>fee</th></tr></thead><tbody>')
        for r in rf:
            ts = datetime.fromtimestamp(
                r["ts_ms"] / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M:%S")
            parts.append(
                f'<tr><td>{_esc(ts)}</td><td>{_esc(r.get("agent"))}</td>'
                f'<td>{_esc(r.get("side"))}</td><td>{_esc(r.get("outcome"))}</td>'
                f'<td>{r.get("price"):.4f}</td><td>${r.get("size"):.2f}</td>'
                f'<td>${r.get("fee"):.4f}</td></tr>'
            )
        parts.append("</tbody></table>")

    return "".join(parts)


def render_html() -> str:
    p1 = _clean(poly1_state())
    sw = _clean(swarm_state())

    p1_age = p1.get("heartbeat_age_s")
    sw_age = sw.get("heartbeat_age_s")
    p1_badge_cls, p1_badge_text = _hb_badge(p1_age)
    sw_badge_cls, sw_badge_text = _hb_badge(sw_age)

    return (
        HTML_TEMPLATE
        .replace("{TS}", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
        .replace("{P1_HB}", p1_badge_text)
        .replace("{P1_BADGE}", p1_badge_cls)
        .replace("{SW_HB}", sw_badge_text)
        .replace("{SW_BADGE}", sw_badge_cls)
        .replace("{P1_BODY}", _poly1_body(p1))
        .replace("{SW_BODY}", _swarm_body(sw))
    )


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path in ("/", "/index.html"):
                body = render_html().encode("utf-8")
                return self._send(200, body, "text/html; charset=utf-8")
            if self.path in ("/data.json", "/api"):
                snapshot = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "poly1": _clean(poly1_state()),
                    "swarm": _clean(swarm_state()),
                }
                body = json.dumps(snapshot, default=str, indent=2).encode("utf-8")
                return self._send(200, body, "application/json")
            if self.path == "/healthz":
                p1 = _clean(poly1_state())
                sw = _clean(swarm_state())
                ok = ("error" not in p1) or ("error" not in sw)
                body = json.dumps({"ok": ok, "p1": "error" not in p1,
                                   "sw": "error" not in sw}).encode("utf-8")
                return self._send(200 if ok else 503, body, "application/json")
            return self._send(404, b"not found", "text/plain")
        except Exception as e:
            body = f"server error: {e}".encode("utf-8")
            self._send(500, body, "text/plain")

    def log_message(self, fmt: str, *args) -> None:
        # Quieter access log; only print errors.
        if "404" in fmt or "500" in fmt or "error" in fmt.lower():
            sys.stderr.write("%s — %s\n" % (self.address_string(), fmt % args))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=7777)
    p.add_argument("--bind", default="127.0.0.1",
                   help="bind address; '0.0.0.0' for LAN access (read-only, but be careful)")
    args = p.parse_args()

    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    url = f"http://{args.bind}:{args.port}/"
    print(f"Polymarket monitor running at {url}", flush=True)
    print(f"  /          dashboard (auto-refresh 10s)", flush=True)
    print(f"  /data.json snapshot JSON", flush=True)
    print(f"  /healthz   readiness", flush=True)
    print(f"Ctrl-C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
