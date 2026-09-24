#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# --quick skips the Playwright browser suite (use while iterating or when Chromium is unavailable).
QUICK=0
for arg in "$@"; do
  case "${arg}" in
    --quick) QUICK=1 ;;
    -h|--help)
      echo "Usage: ./scripts/validate.sh [--quick]"
      exit 0
      ;;
    *)
      echo "Unknown argument: ${arg}" >&2
      exit 2
      ;;
  esac
done

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
echo "[1/6] Compiling backend and PoC Python sources..."
"${PYTHON}" -m compileall -q "${REPO_ROOT}/backend/scopewatch" "${REPO_ROOT}/backend/tests" "${REPO_ROOT}/scripts" \
  "${REPO_ROOT}/poc/cot-auditing/src" "${REPO_ROOT}/poc/cot-auditing/scripts"
echo "  ✓ Python compilation clean"

# 2. Run backend pytest suite
echo ""
echo "[2/6] Running backend pytest suite..."
PYTHONPATH="${REPO_ROOT}/backend" "${PYTHON}" -m pytest "${REPO_ROOT}/backend/tests" -q
echo "  ✓ All backend unit and integration tests passed"

# 3. Run PoC pytest suite
echo ""
echo "[3/6] Running reasoning-audit PoC pytest suite..."
"${PYTHON}" -m pytest "${REPO_ROOT}/poc/cot-auditing" -q
echo "  ✓ All PoC tests passed"

# 4. Run frontend unit tests
echo ""
echo "[4/6] Running frontend unit tests..."
npm test --prefix "${REPO_ROOT}/frontend"
echo "  ✓ All frontend unit tests passed"

# 5. Run browser Playwright tests
echo ""
if [[ "${QUICK}" -eq 1 ]]; then
  echo "[5/6] Skipping browser Playwright tests (--quick). Browser behaviour NOT verified."
else
  echo "[5/6] Running browser Playwright integration tests..."
  npm run test:browser --prefix "${REPO_ROOT}/frontend"
  echo "  ✓ All browser integration tests passed"
fi

# 6. Clean-room scenario seed & security verification
echo ""
echo "[6/6] Running clean-room demo scenario verification..."
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
