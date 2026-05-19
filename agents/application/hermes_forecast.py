"""Hermes Forecast Bridge.

Tiny HTTP service that turns the existing Claude/Anthropic access into a
structured MetaBrain signal. It never places orders. It only argues the trade
case and returns a bounded confidence/win-rate estimate.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _anthropic_model() -> str:
    return (
        os.getenv("HERMES_ANTHROPIC_MODEL", "").strip()
        or os.getenv("ANTHROPIC_MODEL", "").strip()
        or "claude-sonnet-4-5-20250929"
    )


def _coerce_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return {}
    return {}


def forecast(payload: dict) -> dict:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return {
            "confidence": 0.5,
            "winrate": 0.5,
            "score": 0.5,
            "reason": "anthropic_key_missing",
            "source": "hermes_forecast",
        }

    prompt = (
        "You are Hermes, a skeptical trading negotiation agent for a live "
        "Polymarket scalping system. You do not place trades. Debate the entry "
        "against the strategy: fast in/out, 3% stop loss, take profit quickly, "
        "avoid holding unless the forecast is strong. Return JSON only with "
        "keys: winrate (0-1), confidence (0-1), score (0-1), stance "
        "('approve'|'oppose'|'neutral'), reason (short), counterargument "
        "(short), exit_bias ('fast_exit'|'hold_only_if_strong'|'skip'). "
        "Trade candidate data: "
        + json.dumps(payload, sort_keys=True)
    )
    body = {
        "model": _anthropic_model(),
        "max_tokens": _env_int("HERMES_MAX_TOKENS", 300),
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "User-Agent": "poly1-hermes-forecast/1.0",
        },
    )
    started = time.time()
    with urllib.request.urlopen(req, timeout=_env_int("HERMES_ANTHROPIC_TIMEOUT_SEC", 12)) as resp:
        raw = json.loads(resp.read())
    text = ""
    for item in raw.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text += str(item.get("text") or "")
    parsed = _coerce_json(text)
    confidence = max(0.0, min(1.0, float(parsed.get("confidence", parsed.get("score", 0.5)))))
    winrate = max(0.0, min(1.0, float(parsed.get("winrate", confidence))))
    score = max(0.0, min(1.0, float(parsed.get("score", (confidence + winrate) / 2.0))))
    return {
        "confidence": round(confidence, 4),
        "winrate": round(winrate, 4),
        "score": round(score, 4),
        "stance": str(parsed.get("stance") or "neutral")[:40],
        "reason": str(parsed.get("reason") or "")[:300],
        "counterargument": str(parsed.get("counterargument") or "")[:300],
        "exit_bias": str(parsed.get("exit_bias") or "")[:80],
        "model": _anthropic_model(),
        "latency_ms": int((time.time() - started) * 1000),
        "source": "hermes_forecast",
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"ok": True, "service": "hermes_forecast"})
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/forecast":
            self._send(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            self._send(200, forecast(payload))
        except Exception as exc:
            logger.exception("hermes forecast failed")
            self._send(200, {
                "confidence": 0.5,
                "winrate": 0.5,
                "score": 0.5,
                "reason": f"hermes_error:{exc}",
                "source": "hermes_forecast",
            })

    def log_message(self, fmt: str, *args) -> None:
        logger.info("http %s", fmt % args)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    port = _env_int("HERMES_FORECAST_PORT", 8097)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    logger.info("HermesForecast: listening on port %s", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
