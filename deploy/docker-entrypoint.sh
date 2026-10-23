#!/usr/bin/env bash
# Gateway container entrypoint (issue #42).
#
# Seeds the synthetic workspace + scripted demo scenarios exactly once per
# data volume (marker file in /data), then execs the gateway. Seeding uses
# --mode scripted with the mock provider, so it NEVER makes model calls and
# needs no API keys. A `docker compose down -v` wipes /data and reseeds
# from scratch (see RUNBOOK.md "Reset demo data").
set -euo pipefail

# Hosted images require demo mode. Compose rejects unset/empty values, but
# a whitespace-only token would disable the native gateway guards too.
if [[ "${DEMO_TOKEN:-}" =~ ^[[:space:]]*$ ]]; then
  echo "[entrypoint] DEMO_TOKEN must contain non-whitespace characters." >&2
  exit 1
fi

MARKER="/data/.seeded-scripted-v1"

if [[ ! -f "${MARKER}" ]]; then
  echo "[entrypoint] first start on this volume: seeding scripted demo..."
  python /app/scripts/seed_demo.py \
    --db-path "${SCOPEWATCH_DB_PATH:-/data/scopewatch.db}" \
    --workspace-root "${SCOPEWATCH_WORKSPACE_ROOT:-/workspace}" \
    --mode scripted \
    --auto-approve
  touch "${MARKER}"
  echo "[entrypoint] seed complete."
else
  echo "[entrypoint] volume already seeded (${MARKER}); skipping seed."
fi

exec "$@"
