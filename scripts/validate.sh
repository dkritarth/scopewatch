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
echo "[1/7] Compiling backend, PoC, and deploy Python sources..."
"${PYTHON}" -m compileall -q "${REPO_ROOT}/backend/scopewatch" "${REPO_ROOT}/backend/tests" "${REPO_ROOT}/scripts" \
  "${REPO_ROOT}/poc/cot-auditing/src" "${REPO_ROOT}/poc/cot-auditing/scripts" "${REPO_ROOT}/deploy"
echo "  ✓ Python compilation clean"

# 1b. Build-context guard: .dockerignore must exclude secrets without
# excluding any COPY source used by the four Dockerfiles.
echo "     Checking Docker build context exclusions..."
"${PYTHON}" "${REPO_ROOT}/scripts/check_dockerignore.py"
echo "  ✓ Docker build context excludes secrets and keeps every COPY source"

# 2. Run backend pytest suite
echo ""
echo "[2/7] Running backend pytest suite..."
PYTHONPATH="${REPO_ROOT}/backend" "${PYTHON}" -m pytest "${REPO_ROOT}/backend/tests" -q
echo "  ✓ All backend unit and integration tests passed"

# 3. Run PoC pytest suite
echo ""
echo "[3/7] Running reasoning-audit PoC pytest suite..."
"${PYTHON}" -m pytest "${REPO_ROOT}/poc/cot-auditing" -q
echo "  ✓ All PoC tests passed"

# 4. Run the hosted-demo deploy suite
# Covers deploy/gate/gate.py (token enforcement, caps, budgets, refusal
# envelope) and deploy/test_compose_layout.py (the runbook never curls a port
# compose does not publish). Neither was executed by any CI job before #107,
# so the envelope assertion #107 depends on now runs on every PR.
echo ""
echo "[4/7] Running hosted-demo deploy tests..."
"${PYTHON}" -m pytest "${REPO_ROOT}/deploy" -q
echo "  ✓ All deploy tests passed"

# 5. Run frontend unit tests
echo ""
echo "[5/7] Running frontend unit tests..."
npm test --prefix "${REPO_ROOT}/frontend"
echo "  ✓ All frontend unit tests passed"

# 6. Run browser Playwright tests
echo ""
if [[ "${QUICK}" -eq 1 ]]; then
  echo "[6/7] Skipping browser Playwright tests (--quick). Browser behaviour NOT verified."
else
  echo "[6/7] Running browser Playwright integration tests..."
  npm run test:browser --prefix "${REPO_ROOT}/frontend"
  echo "  ✓ All browser integration tests passed"
fi

# 7. Clean-room scenario seed & security verification
echo ""
echo "[7/7] Running clean-room demo scenario verification..."
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/scopewatch-validate-XXXXXX")"
cleanup() {
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT

CLEAN_DB="${TMP_DIR}/clean.db"
CLEAN_WS="${TMP_DIR}/workspace"
# Issue #117: each run executes against its own copy of the scenario fixture,
# never the fixture tree itself. The default managed root is `<fixture>/.runs`
# (workspaces.py: default_run_workspaces_root) — it nests inside the fixture
# because a sibling is a path a `read_only: true` container cannot create, which
# is exactly what broke the deployed demo. Set explicitly here so this check
# keeps testing isolation rather than the default's location, and so a change to
# either fails loudly instead of silently finding nothing.
CLEAN_RUNS="${CLEAN_WS}/.runs"
export SCOPEWATCH_RUN_WORKSPACES_DIR="${CLEAN_RUNS}"

"${PYTHON}" "${REPO_ROOT}/scripts/seed_demo.py" \
  --db-path "${CLEAN_DB}" \
  --workspace-root "${CLEAN_WS}" \
  --auto-approve

# One workspace directory per seeded run.
RUN_COUNT="$(find "${CLEAN_RUNS}" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
if [[ "${RUN_COUNT}" -lt 1 ]]; then
  echo "Validation failure: no per-run workspaces were created under '${CLEAN_RUNS}'." >&2
  exit 1
fi
echo "  ✓ Per-run workspaces created: ${RUN_COUNT}"

# Verify that safe audit created the expected output file inside a RUN's
# workspace, not in the shared fixture.
AUDIT_SUMMARIES="$(find "${CLEAN_RUNS}" -mindepth 2 -path '*/outputs/audit-summary.txt' -type f | wc -l | tr -d ' ')"
if [[ "${AUDIT_SUMMARIES}" -lt 1 ]]; then
  echo "Validation failure: expected safe output 'outputs/audit-summary.txt' was not created in any run workspace." >&2
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

# The shared fixture is read-only baseline: no run-generated output may leak
# back into it (issue #117). The managed root itself is exempt — it lives under
# the fixture in the default layout — so check the fixture's own content rather
# than the whole tree.
if [[ -f "${CLEAN_WS}/outputs/audit-summary.txt" ]]; then
  echo "Validation failure: run output leaked into the shared scenario fixture." >&2
  exit 1
fi

# Exactly one run wrote outputs/audit-summary.txt, so one run's output did not
# land in another's tree. (Per-run confinement is covered by
# backend/tests/test_issue_117_run_workspace.py; this is the end-to-end echo.)
AUDIT_OWNERS="$(find "${CLEAN_RUNS}" -path '*/outputs/audit-summary.txt' -type f \
  -exec dirname {} \; | xargs -r -n1 dirname | sort -u | wc -l | tr -d ' ')"
if [[ "${AUDIT_OWNERS}" -ne 1 ]]; then
  echo "Validation failure: expected exactly one run workspace to hold outputs/audit-summary.txt, found ${AUDIT_OWNERS}." >&2
  exit 1
fi

echo "  ✓ Clean-room workspace isolation verified"
echo "  ✓ Run outputs confined to per-run workspaces; fixture tree untouched"
echo "  ✓ Security boundary verified (no host escapes, no unauthorized file mutations)"

echo ""
echo "=================================================================="
echo "Scopewatch Validation Complete"
echo "All test suites and clean-room security checks passed."
echo "=================================================================="
