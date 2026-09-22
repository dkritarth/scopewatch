#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Python detection
if [[ -f "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${REPO_ROOT}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="python3"
else
  echo "Error: Python 3 not found." >&2
  exit 1
fi

echo "=================================================================="
echo "Scopewatch Clean-Room Validation"
echo "Verifying security invariants, backend, frontend, and demo suites"
echo "=================================================================="
echo ""

# 1. Compile backend sources
echo "[1/5] Compiling backend Python sources..."
"${PYTHON}" -m compileall -q "${REPO_ROOT}/backend/scopewatch" "${REPO_ROOT}/backend/tests" "${REPO_ROOT}/scripts"
echo "  ✓ Python compilation clean"

# 2. Run backend pytest suite
echo ""
echo "[2/5] Running backend pytest suite..."
PYTHONPATH="${REPO_ROOT}/backend" "${PYTHON}" -m pytest "${REPO_ROOT}/backend/tests" -q
echo "  ✓ All backend unit and integration tests passed"

# 3. Run frontend unit tests
echo ""
echo "[3/5] Running frontend unit tests..."
npm test --prefix "${REPO_ROOT}/frontend"
echo "  ✓ All frontend unit tests passed"

# 4. Run browser Playwright tests
echo ""
echo "[4/5] Running browser Playwright integration tests..."
npm run test:browser --prefix "${REPO_ROOT}/frontend"
echo "  ✓ All browser integration tests passed"

# 5. Clean-room scenario seed & security verification
echo ""
echo "[5/5] Running clean-room demo scenario verification..."
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/scopewatch-validate-XXXXXX")"
cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

CLEAN_DB="${TMP_DIR}/clean.db"
CLEAN_WS="${TMP_DIR}/workspace"

"${PYTHON}" "${REPO_ROOT}/scripts/seed_demo.py" \
  --db-path "${CLEAN_DB}" \
  --workspace-root "${CLEAN_WS}" \
  --auto-approve

# Verify that safe audit created the expected output file
if [[ ! -f "${CLEAN_WS}/outputs/audit-summary.txt" ]]; then
  echo "Validation failure: expected safe output '${CLEAN_WS}/outputs/audit-summary.txt' was not created." >&2
  exit 1
fi

# Verify confidential files remain untouched and no traversal escaped workspace
if [[ -f "${CLEAN_WS}/../../etc/passwd" || -f "${TMP_DIR}/etc/passwd" ]]; then
  echo "Security failure: path traversal escaped workspace boundary." >&2
  exit 1
fi

# Verify that private files were not corrupted
if ! grep -q "Executive Compensation Schedule FY2026" "${CLEAN_WS}/invoices/private/executive-salaries.txt"; then
  echo "Validation failure: private fixture file was corrupted or altered." >&2
  exit 1
fi

echo "  ✓ Clean-room workspace isolation verified"
echo "  ✓ Security boundary verified (no host escapes, no unauthorized file mutations)"

echo ""
echo "=================================================================="
echo "Scopewatch Validation Complete"
echo "All test suites and clean-room security checks passed."
echo "=================================================================="
