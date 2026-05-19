#!/usr/bin/env python3
"""HTTP bridge from poly1 market snapshots to Polifly's analyzer endpoint.

The bridge is intentionally thin:
- poly1 sends {"market": {...}} to /analyze.
- the bridge renders that market into an SVG "screenshot".
- Polifly's real /api/analyze-market endpoint analyzes the image.
- the response is normalized into poly1's ExternalVerdict shape.

Authentication is supplied by POLIFLY_COOKIE as a full Cookie header copied
from a logged-in Polifly browser session. Without it, the bridge returns a
real skip verdict instead of fabricating a prediction.
"""

from __future__ import annotations

import html
import json
import os
import textwrap
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import requests


HOST = os.getenv("POLIFLY_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.getenv("POLIFLY_BRIDGE_PORT", "8787"))
POLIFLY_BASE_URL = os.getenv("POLIFLY_BASE_URL", "https://polifly.bet").rstrip("/")
POLIFLY_COOKIE = os.getenv("POLIFLY_COOKIE", "").strip()
INBOUND_TOKEN = os.getenv("POLIFLY_BROWSER_BRIDGE_API_KEY", "").strip()
REQUEST_TIMEOUT = float(os.getenv("POLIFLY_BRIDGE_TIMEOUT_SEC", "60"))


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: dict[str, Any]) -> None:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _skip(reason: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "direction": "skip",
        "confidence": 0.0,
        "source": "polifly_browser",
        "reason": reason[:1000],
        "evidence": evidence or {},
    }


def _wrap(text: Any, width: int = 72) -> list[str]:
    value = "" if text is None else str(text)
    return textwrap.wrap(value, width=width) or [""]


def _price(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "n/a"


def _market_svg(market: dict[str, Any]) -> bytes:
    """Create a Polymarket-like static image for Polifly's vision analyzer."""
    question = market.get("question") or market.get("slug") or "Unknown market"
    lines = [
        "POLYMARKET MARKET SNAPSHOT",
        "",
        *(_wrap(question, 68)),
        "",
        f"Category: {market.get('category') or 'n/a'}",
        f"Slug: {market.get('slug') or 'n/a'}",
        f"End date: {market.get('end_date') or 'n/a'}",
        "",
        f"YES price: {_price(market.get('yes_price'))}",
        f"NO price: {_price(market.get('no_price'))}",
        f"Liquidity USDC: {_price(market.get('liquidity_usdc'))}",
        f"Volume USDC: {_price(market.get('volume_usdc'))}",
        "",
        "Task: analyze this prediction market screenshot.",
        "Return a short-horizon YES / NO / UNCERTAIN lean for a trade.",
    ]
    escaped = [html.escape(line) for line in lines]
    row_height = 28
    height = max(620, 80 + row_height * len(escaped))
    text_nodes = "\n".join(
        f'<text x="54" y="{64 + i * row_height}" class="line">{line}</text>'
        for i, line in enumerate(escaped)
    )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{height}" viewBox="0 0 1200 {height}">
  <rect width="1200" height="{height}" fill="#f8fafc"/>
  <rect x="28" y="28" width="1144" height="{height - 56}" rx="18" fill="#ffffff" stroke="#cbd5e1" stroke-width="2"/>
  <rect x="28" y="28" width="1144" height="86" rx="18" fill="#0f172a"/>
  <text x="54" y="82" font-family="Arial, Helvetica, sans-serif" font-size="34" font-weight="700" fill="#ffffff">Polymarket</text>
  <style>
    .line {{ font-family: Arial, Helvetica, sans-serif; font-size: 24px; fill: #0f172a; }}
  </style>
  <g transform="translate(0,90)">{text_nodes}</g>
</svg>"""
    return svg.encode("utf-8")


def _normalize_polifly(body: dict[str, Any]) -> dict[str, Any]:
    analysis = body.get("analysis") if isinstance(body.get("analysis"), dict) else body
    recommendation = str(
        analysis.get("recommendation")
        or analysis.get("direction")
        or analysis.get("verdict")
        or ""
    ).strip().lower()

    if recommendation.startswith("yes"):
        direction = "yes"
    elif recommendation.startswith("no"):
        direction = "no"
    else:
        direction = "skip"

    try:
        confidence = float(analysis.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence > 1.0:
        confidence = confidence / 100.0
    confidence = max(0.0, min(1.0, confidence))

    reason_parts = [
        analysis.get("rationale"),
        analysis.get("market_summary"),
        analysis.get("detected_prices"),
    ]
    reason = " | ".join(str(part).strip() for part in reason_parts if part)
    if not reason:
        reason = str(analysis.get("detailed_explanation") or "Polifly analysis returned")

    return {
        "direction": direction,
        "confidence": round(confidence, 3),
        "source": "polifly_browser",
        "reason": reason[:1000],
        "evidence": {
            "polifly": analysis,
            "fetched_at": int(time.time()),
        },
    }


def analyze_market(market: dict[str, Any]) -> dict[str, Any]:
    if not POLIFLY_COOKIE:
        return _skip(
            "missing POLIFLY_COOKIE; bridge is live but Polifly requires a logged-in session",
            {"access_required": True, "auth": "missing_cookie"},
        )

    headers = {
        "Accept": "application/json",
        "Cookie": POLIFLY_COOKIE,
        "Origin": POLIFLY_BASE_URL,
        "Referer": f"{POLIFLY_BASE_URL}/dashboard/analyzer",
        "User-Agent": "poly1-polifly-bridge",
    }
    files = {
        "image": ("poly1-market.svg", _market_svg(market), "image/svg+xml"),
    }
    try:
        response = requests.post(
            f"{POLIFLY_BASE_URL}/api/analyze-market",
            headers=headers,
            files=files,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        return _skip("Polifly request failed", {"error": str(exc)})

    try:
        body = response.json()
    except ValueError:
        return _skip(
            f"Polifly returned non-JSON HTTP {response.status_code}",
            {"status_code": response.status_code, "body": response.text[:500]},
        )

    if not response.ok:
        return _skip(
            str(body.get("error") or f"Polifly HTTP {response.status_code}"),
            {"status_code": response.status_code, "code": body.get("code"), "body": body},
        )

    return _normalize_polifly(body)


class Handler(BaseHTTPRequestHandler):
    server_version = "poly1-polifly-bridge/1.0"

    def _authorized(self) -> bool:
        if not INBOUND_TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {INBOUND_TOKEN}"

    def do_GET(self) -> None:  # noqa: N802
        if self.path not in ("/", "/health"):
            _json_response(self, 404, {"ok": False, "error": "not found"})
            return
        _json_response(
            self,
            200,
            {
                "ok": True,
                "service": "poly1-polifly-bridge",
                "polifly_cookie_configured": bool(POLIFLY_COOKIE),
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/analyze":
            _json_response(self, 404, {"direction": "skip", "confidence": 0, "reason": "not found"})
            return
        if not self._authorized():
            _json_response(self, 401, _skip("unauthorized bridge request"))
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            _json_response(self, 400, _skip("invalid JSON payload"))
            return
        market = payload.get("market") if isinstance(payload.get("market"), dict) else {}
        _json_response(self, 200, analyze_market(market))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)


def main() -> None:
    print(f"polifly bridge listening on {HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
