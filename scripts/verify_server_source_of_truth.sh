#!/usr/bin/env bash
set -euo pipefail

SERVER="${POLY1_SERVER:-trader@83.229.82.193}"
REMOTE="${POLY1_REMOTE:-/srv/poly1}"

cd "$(dirname "$0")/.."

echo "== local runtime check =="
if docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^poly1($|-)' >/dev/null; then
  echo "FAIL: local poly1 containers are running"
  docker ps --format '{{.Names}} {{.Status}}' | grep -E '^poly1($|-)'
  exit 1
fi
echo "OK: no local poly1 containers"

echo
echo "== server runtime check =="
ssh "$SERVER" "cd '$REMOTE' && test -f data/runtime_control.json && test -f data/HALT && test -f data/trade_log.db && python3 - <<'PY'
import json
from pathlib import Path

runtime = json.loads(Path('data/runtime_control.json').read_text())
print('mode=' + str(runtime.get('mode')))
print('allowed_live_agents=' + ','.join(runtime.get('allowed_live_agents') or []))
print('config_hash=' + str(runtime.get('config_hash')))
print('halt=present')
print('trade_log=present')
PY"

echo
echo "== code/config drift check =="
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

{
  rsync -azcn --itemize-changes --delete \
    --exclude='__pycache__/' --exclude='*.pyc' \
    agents/ "$SERVER:$REMOTE/agents/"
  rsync -azcn --itemize-changes --delete \
    --exclude='__pycache__/' --exclude='*.pyc' \
    scripts/ "$SERVER:$REMOTE/scripts/"
  rsync -azcn --itemize-changes --delete \
    --exclude='__pycache__/' --exclude='*.pyc' \
    tests/ "$SERVER:$REMOTE/tests/"
  rsync -azcn --itemize-changes --delete \
    docs/ "$SERVER:$REMOTE/docs/"
  rsync -azcn --itemize-changes --delete \
    --exclude='.env.runtime' --exclude='.env.runtime.*' --exclude='*.bak*' \
    deploy/ "$SERVER:$REMOTE/deploy/"
  rsync -azcn --itemize-changes \
    Dockerfile docker-compose.yml requirements.txt .env.example .dockerignore \
    "$SERVER:$REMOTE/"
} >"$tmp"

if awk '$1 ~ /[<>].c/ || $1 ~ /^\*deleting/ { print }' "$tmp" | grep .; then
  echo "FAIL: content drift exists between local code/config and server"
  exit 1
fi

echo "OK: no content drift in managed runtime code/config"

echo
echo "== server git hygiene check =="
ssh "$SERVER" "cd '$REMOTE' && python3 - <<'PY'
import subprocess
import sys

allowed_tracked = {'deploy/.env.runtime'}
allowed_untracked_prefixes = set()

out = subprocess.check_output(['git', 'status', '--porcelain'], text=True)
bad = []
for raw in out.splitlines():
    if not raw:
        continue
    status = raw[:2]
    path = raw[3:]
    if status == '??':
        if not any(path.startswith(prefix) for prefix in allowed_untracked_prefixes):
            bad.append(raw)
        continue
    if path not in allowed_tracked:
        bad.append(raw)

if bad:
    print('FAIL: server worktree has unapproved drift/untracked files')
    for row in bad:
        print(row)
    sys.exit(1)
print('OK: server worktree drift limited to approved runtime files')
PY"

echo
echo "== server docker image parity check =="
ssh "$SERVER" "cd '$REMOTE' && python3 - <<'PY'
import subprocess
import sys

expected = subprocess.check_output(
    ['docker', 'image', 'inspect', 'poly1:local', '--format', '{{.Id}}'],
    text=True,
).strip()
rows = subprocess.check_output(
    ['docker', 'ps', '--format', '{{.Names}}|{{.Image}}|{{.ID}}'],
    text=True,
).splitlines()

external = {
    'poly1-grafana',
    'poly1-polifly-bridge',
    'codeloom-upstream',
}
bad = []
for row in rows:
    name, image, cid = row.split('|', 2)
    if name in external:
        continue
    if not (name == 'poly1' or name.startswith('poly1-')):
        continue
    actual = subprocess.check_output(
        ['docker', 'inspect', cid, '--format', '{{.Image}}'],
        text=True,
    ).strip()
    if actual != expected:
        bad.append((name, image, actual[:19], expected[:19]))

if bad:
    print('FAIL: running poly1 containers are not all on current poly1:local image')
    for name, image, actual, wanted in bad:
        print(f'{name}: image={image} actual={actual} expected={wanted}')
    sys.exit(1)
print('OK: all running managed poly1 containers use current poly1:local image')
PY"

echo "OK: server remains the source of truth for data, wallet env, runtime env, and Telegram"
