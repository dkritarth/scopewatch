#!/usr/bin/env bash
# Reset hosted-demo data (issue #42).
#
# Full reset: drops the sqlite + workspace volumes and restarts, which
# reseeds the scripted demo from scratch (see deploy/docker-entrypoint.sh).
# Run on the VM from the deploy/ directory.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

read -r -p "This DELETES all demo runs, approvals, evidence, and workspace outputs. Continue? [y/N] " answer
if [[ "${answer}" != "y" && "${answer}" != "Y" ]]; then
  echo "Aborted."
  exit 0
fi

docker compose down
docker compose down -v --remove-orphans 2>/dev/null || docker compose down -v
docker compose up -d --build

echo "Reset requested. Waiting for health..."
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
    echo "Gate healthy. Demo reset complete."
    exit 0
  fi
  sleep 2
done
echo "WARNING: gate did not become healthy in 60s; run 'docker compose ps' and 'docker compose logs'." >&2
exit 1
