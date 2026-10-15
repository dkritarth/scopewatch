#!/usr/bin/env bash
# Clean-room demo scenario verification (extracted from validate.sh).
# Runs the demo seed against an isolated per-run workspace and asserts
# security boundaries. Exits non-zero on any failure.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -f "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${REPO_ROOT}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="python3"
else
  echo "Error: Python 3 not found." >&2
  exit 1
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/scopewatch-validate-XXXXXX")"
cleanup() { rm -rf "${TMP_DIR}"; }
trap cleanup EXIT

CLEAN_DB="${TMP_DIR}/clean.db"
CLEAN_WS="${TMP_DIR}/workspace"
CLEAN_RUNS="${CLEAN_WS}/.runs"
export SCOPEWATCH_RUN_WORKSPACES_DIR="${CLEAN_RUNS}"

"${PYTHON}" "${REPO_ROOT}/scripts/seed_demo.py" \
  --db-path "${CLEAN_DB}" \
  --workspace-root "${CLEAN_WS}" \
  --auto-approve

RUN_COUNT="$(find "${CLEAN_RUNS}" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
if [[ "${RUN_COUNT}" -lt 1 ]]; then
  echo "Validation failure: no per-run workspaces were created under '${CLEAN_RUNS}'." >&2
  exit 1
fi
echo "  ✓ Per-run workspaces created: ${RUN_COUNT}"

AUDIT_SUMMARIES="$(find "${CLEAN_RUNS}" -mindepth 2 -path '*/outputs/audit-summary.txt' -type f | wc -l | tr -d ' ')"
if [[ "${AUDIT_SUMMARIES}" -lt 1 ]]; then
  echo "Validation failure: expected safe output 'outputs/audit-summary.txt' was not created in any run workspace." >&2
  exit 1
fi

if [[ -f "${CLEAN_WS}/../../etc/passwd" || -f "${TMP_DIR}/etc/passwd" ]]; then
  echo "Security failure: path traversal escaped workspace boundary." >&2
  exit 1
fi

if ! grep -q "Executive Compensation Schedule FY2026" "${CLEAN_WS}/invoices/private/executive-salaries.txt"; then
  echo "Validation failure: private fixture file was corrupted or altered." >&2
  exit 1
fi

if [[ -f "${CLEAN_WS}/outputs/audit-summary.txt" ]]; then
  echo "Validation failure: run output leaked into the shared scenario fixture." >&2
  exit 1
fi

AUDIT_OWNERS="$(find "${CLEAN_RUNS}" -path '*/outputs/audit-summary.txt' -type f \
  -exec dirname {} \; | xargs -r -n1 dirname | sort -u | wc -l | tr -d ' ')"
if [[ "${AUDIT_OWNERS}" -ne 1 ]]; then
  echo "Validation failure: expected exactly one run workspace to hold outputs/audit-summary.txt, found ${AUDIT_OWNERS}." >&2
  exit 1
fi

echo "  ✓ Clean-room workspace isolation verified"
echo "  ✓ Run outputs confined to per-run workspaces; fixture tree untouched"
echo "  ✓ Security boundary verified (no host escapes, no unauthorized file mutations)"
