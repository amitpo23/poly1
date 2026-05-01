#!/usr/bin/env bash
# Deploy a tagged version to the VPS.
# Usage:  ./deploy/deploy.sh <vps_host> <git_ref>
set -euo pipefail

VPS_HOST="${1:-}"
REF="${2:-main}"

if [[ -z "$VPS_HOST" ]]; then
  echo "usage: $0 <user@host> [git_ref]" >&2
  exit 1
fi

echo "Deploying $REF to $VPS_HOST"

ssh "$VPS_HOST" bash -s <<EOF
set -euo pipefail
cd /srv/poly1
git fetch --tags --all
git checkout "$REF"
docker compose build
docker compose up -d
docker compose ps
EOF

echo "Deploy complete. Watching logs (Ctrl-C to detach):"
ssh "$VPS_HOST" "docker logs poly1 --tail 50 -f"
