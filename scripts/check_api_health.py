#!/usr/bin/env python3
"""Live API health probe for poly1 integrations.

The script prints redacted PASS/WARN/FAIL results only. It never prints API
keys, tokens, wallet secrets, or raw provider responses.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Check:
    name: str
    status: str
    detail: str
    latency_ms: int = 0


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_runtime_env() -> None:
    _load_env_file(ROOT / ".env")
    _load_env_file(ROOT / "deploy/.env.runtime")


def _request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 20,
) -> tuple[int, Any, int]:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    start = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        latency_ms = int((time.time() - start) * 1000)
        if not body:
            return resp.status, None, latency_ms
        return resp.status, json.loads(body), latency_ms


def _http_error_detail(exc: Exception) -> str:
    def redact(text: str) -> str:
        text = re.sub(r"sk-[A-Za-z0-9_\\-*]+", "sk-[REDACTED]", text)
        text = re.sub(r"sk-ant-[A-Za-z0-9_\\-*]+", "sk-ant-[REDACTED]", text)
        return text[:220]

    if isinstance(exc, urllib.error.HTTPError):
        try:
            raw = exc.read().decode("utf-8", errors="ignore")
            payload = json.loads(raw)
            message = payload.get("error", {}).get("message") or payload.get("message")
            if message:
                return f"HTTP {exc.code}: {redact(str(message))}"
        except Exception:
            pass
        return f"HTTP {exc.code}"
    return redact(str(exc))


def check_env() -> list[Check]:
    checks: list[Check] = []
    tavily_enabled = os.getenv("TAVILY_ENABLED", "true").lower() in {
        "1", "true", "yes", "on",
    }
    for key, required in (
        ("OPENAI_API_KEY", True),
        ("ANTHROPIC_API_KEY", False),
        ("TAVILY_API_KEY", tavily_enabled),
        ("BUILDER_API_KEY", False),
        ("BUILDER_SECRET", False),
        ("BUILDER_PASS_PHRASE", False),
        ("POLYMARKET_CLOB_API_KEY", False),
        ("POLYMARKET_CLOB_API_SECRET", False),
        ("POLYMARKET_CLOB_API_PASSPHRASE", False),
        ("POLYGON_WALLET_PRIVATE_KEY", True),
        ("NEWSAPI_API_KEY", False),
        ("NANSEN_API_KEY", False),
        ("WALLET_MASTER_API_KEY", False),
        ("POLIFLY_BROWSER_BRIDGE_API_KEY", False),
    ):
        value = os.getenv(key, "").strip()
        if value:
            checks.append(Check(f"env:{key}", "PASS", "set"))
        elif required:
            checks.append(Check(f"env:{key}", "FAIL", "missing required key"))
        else:
            checks.append(Check(f"env:{key}", "WARN", "not configured"))
    checks.append(Check("env:OPENAI_MODEL", "PASS", os.getenv("OPENAI_MODEL", "gpt-4o")))
    checks.append(
        Check("env:ANTHROPIC_MODEL", "PASS", os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"))
    )
    return checks


def check_openai(model: str) -> Check:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return Check("openai_chat", "FAIL", "OPENAI_API_KEY missing")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Return exactly this JSON and nothing else: "
                    '{"status":"ok","provider":"openai"}'
                ),
            }
        ],
        "temperature": 0,
        "max_tokens": 30,
        "response_format": {"type": "json_object"},
    }
    try:
        status, body, latency_ms = _request_json(
            "https://api.openai.com/v1/chat/completions",
            method="POST",
            headers={"Authorization": f"Bearer {key}", "User-Agent": "poly1-api-health"},
            payload=payload,
            timeout=30,
        )
        content = (
            body.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            if isinstance(body, dict)
            else ""
        )
        ok = status == 200 and "ok" in content.lower()
        return Check("openai_chat", "PASS" if ok else "FAIL", f"model={model}", latency_ms)
    except Exception as exc:
        return Check("openai_chat", "FAIL", f"model={model}; {_http_error_detail(exc)}")


def check_anthropic(model: str) -> Check:
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return Check("anthropic_messages", "WARN", "ANTHROPIC_API_KEY missing")
    payload = {
        "model": model,
        "max_tokens": 30,
        "messages": [
            {"role": "user", "content": 'Return exactly: {"status":"ok","provider":"anthropic"}'}
        ],
    }
    try:
        status, body, latency_ms = _request_json(
            "https://api.anthropic.com/v1/messages",
            method="POST",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "User-Agent": "poly1-api-health",
            },
            payload=payload,
            timeout=30,
        )
        text = ""
        if isinstance(body, dict):
            for item in body.get("content", []):
                if item.get("type") == "text":
                    text += item.get("text", "")
        ok = status == 200 and "ok" in text.lower()
        return Check("anthropic_messages", "PASS" if ok else "FAIL", f"model={model}", latency_ms)
    except Exception as exc:
        return Check("anthropic_messages", "FAIL", f"model={model}; {_http_error_detail(exc)}")


def check_tavily() -> Check:
    if os.getenv("TAVILY_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        limit = os.getenv("TAVILY_DAILY_LIMIT", "5")
        ttl = os.getenv("TAVILY_CACHE_TTL_SEC", "21600")
        return Check("tavily_search", "WARN", f"disabled by TAVILY_ENABLED=false; limit={limit}; cache_ttl={ttl}s")
    key = os.getenv("TAVILY_API_KEY", "").strip()
    if not key:
        return Check("tavily_search", "FAIL", "TAVILY_API_KEY missing")
    if os.getenv("TAVILY_HEALTH_REAL_CALL", "false").lower() not in {"1", "true", "yes", "on"}:
        limit = os.getenv("TAVILY_DAILY_LIMIT", "5")
        interval = os.getenv("TAVILY_MIN_QUERY_INTERVAL_SEC", "900")
        return Check(
            "tavily_search",
            "WARN",
            f"key set; real health call skipped; limit={limit}; min_interval={interval}s",
        )
    payload = {
        "api_key": key,
        "query": "Polymarket Bitcoin market news",
        "max_results": 2,
        "search_depth": "basic",
        "topic": "news",
    }
    try:
        status, body, latency_ms = _request_json(
            "https://api.tavily.com/search",
            method="POST",
            headers={"User-Agent": "poly1-api-health"},
            payload=payload,
            timeout=30,
        )
        count = len(body.get("results") or []) if isinstance(body, dict) else 0
        return Check("tavily_search", "PASS" if status == 200 and count else "WARN", f"results={count}", latency_ms)
    except Exception as exc:
        return Check("tavily_search", "FAIL", _http_error_detail(exc))


def check_url(name: str, url: str, *, timeout: int = 20) -> Check:
    try:
        status, body, latency_ms = _request_json(
            url,
            headers={"User-Agent": "poly1-api-health"},
            timeout=timeout,
        )
        detail = f"HTTP {status}"
        if isinstance(body, list):
            detail += f"; items={len(body)}"
        elif isinstance(body, dict):
            detail += f"; keys={len(body.keys())}"
        return Check(name, "PASS" if 200 <= status < 300 else "FAIL", detail, latency_ms)
    except Exception as exc:
        return Check(name, "FAIL", _http_error_detail(exc))


def check_polifly_bridge() -> Check:
    url = os.getenv("POLIFLY_BROWSER_BRIDGE_URL", "").strip()
    if not url:
        return Check("polifly_bridge", "WARN", "not configured")
    health_url = urllib.parse.urljoin(url.rstrip("/") + "/", "healthz")
    return check_url("polifly_bridge", health_url, timeout=10)


def check_tradingview_options() -> Check:
    url = os.getenv(
        "TRADINGVIEW_OPTIONS_CHAIN_URL",
        "https://www.tradingview.com/options/chain/?symbol=CME_MINI%3AES1%21",
    )
    start = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "poly1-api-health"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = int(getattr(resp, "status", 0))
            resp.read(512)
        result = Check(
            "tradingview_options_chain",
            "PASS" if 200 <= status < 400 else "FAIL",
            f"HTTP {status}",
            int((time.time() - start) * 1000),
        )
    except Exception as exc:
        return Check("tradingview_options_chain", "FAIL", _http_error_detail(exc))
    snapshot = Path(os.getenv(
        "TRADINGVIEW_OPTIONS_SNAPSHOT_PATH",
        str(ROOT / "data" / "tradingview_options_es1_snapshot.json"),
    ))
    if result.status == "PASS" and not snapshot.exists():
        return Check(
            result.name,
            "WARN",
            (
                f"page reachable; snapshot missing at {snapshot}; "
                "write with scripts/write_tradingview_options_snapshot.py"
            ),
            result.latency_ms,
        )
    if result.status == "PASS":
        try:
            body = json.loads(snapshot.read_text())
            put_call = body.get("put_call_ratio")
            if put_call is None:
                call_volume = float(body.get("call_volume") or 0)
                put_volume = float(body.get("put_volume") or 0)
                put_call = put_volume / max(call_volume, 1.0) if put_volume or call_volume else 0
            put_call = float(put_call or 0)
            age = max(0.0, time.time() - snapshot.stat().st_mtime)
            max_age = int(os.getenv("TRADINGVIEW_OPTIONS_MAX_AGE_SEC", "900"))
            if put_call <= 0:
                return Check(result.name, "WARN", f"snapshot missing put/call signal at {snapshot}", result.latency_ms)
            if age > max_age:
                return Check(result.name, "WARN", f"snapshot stale {int(age)}s>{max_age}s; put_call={put_call:.3f}", result.latency_ms)
            return Check(result.name, "PASS", f"page reachable; snapshot fresh; put_call={put_call:.3f}", result.latency_ms)
        except Exception as exc:
            return Check(result.name, "WARN", f"snapshot parse error at {snapshot}: {type(exc).__name__}", result.latency_ms)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--openai-model", default=None)
    parser.add_argument("--anthropic-model", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    load_runtime_env()
    openai_model = args.openai_model or os.getenv("OPENAI_MODEL", "gpt-4o")
    anthropic_model = args.anthropic_model or os.getenv(
        "ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"
    )

    checks = []
    checks.extend(check_env())
    checks.append(check_tavily())
    checks.append(check_openai(openai_model))
    checks.append(check_anthropic(anthropic_model))
    checks.append(check_url("polymarket_gamma", "https://gamma-api.polymarket.com/markets?limit=1"))
    checks.append(check_url("polymarket_clob", "https://clob.polymarket.com/markets?next_cursor=MA=="))
    checks.append(check_polifly_bridge())
    checks.append(check_tradingview_options())

    if args.json:
        print(json.dumps([check.__dict__ for check in checks], indent=2))
    else:
        for check in checks:
            latency = f" ({check.latency_ms}ms)" if check.latency_ms else ""
            print(f"{check.status:4} {check.name}: {check.detail}{latency}")

    return 1 if any(check.status == "FAIL" for check in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
