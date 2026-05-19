#!/usr/bin/env python3
"""Start and verify the compose services required by the active live policy."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "deploy" / "runtime_policy.json"
CONTROL_PATH = ROOT / "data" / "runtime_control.json"


@dataclass
class ComposeService:
    service: str
    profile: str | None
    role: str


def _parse_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _parse_env_files(root: Path, spec: str) -> dict[str, str]:
    merged: dict[str, str] = {}
    for raw in spec.split(","):
        item = raw.strip()
        if not item:
            continue
        path = Path(item)
        if not path.is_absolute():
            path = root / path
        merged.update(_parse_env(path))
    return merged


def _is_true(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _compose_base(profiles: list[str]) -> list[str]:
    cmd = ["docker", "compose"]
    for profile in profiles:
        cmd.extend(["--profile", profile])
    return cmd


def _run(root: Path, cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _compose_ps(root: Path, profiles: list[str]) -> dict[str, dict[str, Any]]:
    proc = _run(root, [*_compose_base(profiles), "ps", "--format", "json"])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    raw = proc.stdout.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        rows = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        service = str(row.get("Service") or row.get("Name") or "").strip()
        if service:
            out[service] = row
    return out


def _service_running(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    state = str(row.get("State") or "").strip().lower()
    status = str(row.get("Status") or "").strip().lower()
    return state == "running" or status.startswith("up")


def _service_healthy(row: dict[str, Any] | None) -> bool:
    if not row:
        return False
    health = str(row.get("Health") or "").strip().lower()
    status = str(row.get("Status") or "").strip().lower()
    if health:
        return health == "healthy"
    if "unhealthy" in status:
        return False
    if "health: starting" in status:
        return False
    return True


def _required_services(
    policy: dict[str, Any],
    control: dict[str, Any],
    env: dict[str, str],
    *,
    include_support: bool,
) -> tuple[list[ComposeService], list[str]]:
    entry_agents = policy.get("entry_agents") or {}
    allowed = set(control.get("allowed_live_agents") or [])
    enabled = {
        name
        for name, meta in entry_agents.items()
        if name != "trader" and _is_true(env.get(str(meta.get("execute_flag") or "")))
    }
    problems: list[str] = []
    if not _is_true(env.get("EXECUTE")):
        problems.append("EXECUTE is not true")
    extra_enabled = sorted(enabled - allowed)
    missing_enabled = sorted(allowed - enabled)
    if extra_enabled:
        problems.append(f"enabled_not_allowed={extra_enabled}")
    if missing_enabled:
        problems.append(f"allowed_but_execute_false={missing_enabled}")

    services: list[ComposeService] = []
    for name in sorted(allowed):
        meta = entry_agents.get(name)
        if not meta:
            problems.append(f"allowed_agent_missing_from_policy={name}")
            continue
        service = str(meta.get("compose_service") or "").strip()
        if not service:
            problems.append(f"agent_missing_compose_service={name}")
            continue
        profile = meta.get("compose_profile")
        services.append(ComposeService(service=service, profile=profile, role=name))

    if include_support:
        for role, meta in (policy.get("live_support_services") or {}).items():
            service = str(meta.get("compose_service") or "").strip()
            if not service:
                problems.append(f"support_missing_compose_service={role}")
                continue
            services.append(
                ComposeService(
                    service=service,
                    profile=meta.get("compose_profile"),
                    role=str(role),
                )
            )
    return services, problems


def _profiles(services: list[ComposeService]) -> list[str]:
    return sorted({item.profile for item in services if item.profile})


def _unique_services(services: list[ComposeService]) -> list[ComposeService]:
    seen: set[str] = set()
    out: list[ComposeService] = []
    for item in services:
        if item.service in seen:
            continue
        seen.add(item.service)
        out.append(item)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(ROOT), help="repo root on this host")
    parser.add_argument(
        "--env",
        default=".env,deploy/.env.runtime",
        help="comma-separated env files, later files override earlier ones",
    )
    parser.add_argument("--policy", default=str(POLICY_PATH))
    parser.add_argument("--control", default=str(CONTROL_PATH))
    parser.add_argument("--ensure", action="store_true", help="docker compose up required profiles before checking")
    parser.add_argument("--no-support", action="store_true", help="check entry agents only")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    policy = _load_json(Path(args.policy) if Path(args.policy).is_absolute() else root / args.policy)
    control = _load_json(Path(args.control) if Path(args.control).is_absolute() else root / args.control)
    env = _parse_env_files(root, args.env)

    services, policy_problems = _required_services(
        policy,
        control,
        env,
        include_support=not args.no_support,
    )
    services = _unique_services(services)
    profiles = _profiles(services)
    if args.ensure and services:
        proc = _run(root, [*_compose_base(profiles), "up", "-d", *[s.service for s in services]])
        if proc.returncode != 0:
            policy_problems.append(proc.stderr.strip() or proc.stdout.strip())

    rows: dict[str, dict[str, Any]] = {}
    compose_error = ""
    try:
        rows = _compose_ps(root, profiles)
    except Exception as exc:
        compose_error = str(exc)

    checks = []
    for item in services:
        row = rows.get(item.service)
        running = _service_running(row)
        healthy = _service_healthy(row)
        checks.append(
            {
                "role": item.role,
                "service": item.service,
                "profile": item.profile,
                "running": running,
                "healthy": healthy,
                "status": row.get("Status") if row else None,
            }
        )

    missing = [c for c in checks if not c["running"] or not c["healthy"]]
    ok = not policy_problems and not compose_error and not missing
    payload = {
        "status": "ok" if ok else "blocked",
        "profiles": profiles,
        "policy_problems": policy_problems,
        "compose_error": compose_error,
        "checks": checks,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"live_compose_guard: {payload['status']}")
        if profiles:
            print(f"- profiles: {', '.join(profiles)}")
        for problem in policy_problems:
            print(f"- BLOCKED policy: {problem}")
        if compose_error:
            print(f"- BLOCKED compose: {compose_error}")
        for check in checks:
            marker = "OK" if check["running"] and check["healthy"] else "BLOCKED"
            print(
                f"- {marker} {check['role']}: service={check['service']} "
                f"profile={check['profile'] or '<always>'} status={check['status']}"
            )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
