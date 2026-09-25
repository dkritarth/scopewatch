#!/usr/bin/env bash
set -euo pipefail

# Directory discovery
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
DB_PATH="${REPO_ROOT}/runtime-data/scopewatch.db"
WORKSPACE_ROOT="${REPO_ROOT}/demo/workspace"
MODE="scripted"
PROFILE=""
AUTO_APPROVE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent)
      MODE="agent"
      shift
      ;;
    --mode)
      MODE="$2"
      shift 2
      ;;
    --profile)
      PROFILE="$2"
      shift 2
      ;;
    --auto-approve)
      AUTO_APPROVE=1
      shift
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --host)
      HOST="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
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
echo "Scopewatch Local Demonstration"
echo "Synthetic mediation gateway baseline (mode: ${MODE})"
echo "=================================================================="
echo "Safety statement:"
echo "This local baseline mediates only actions submitted through its"
echo "synthetic demo gateway. It does not intercept arbitrary host or"
echo "agent operations."
echo "=================================================================="
echo ""

# 1. Seed workspace fixtures and scenarios
echo "[1/2] Seeding synthetic workspace and demonstration scenarios..."
mkdir -p "${REPO_ROOT}/runtime-data"

SEED_ARGS=(
  --db-path "${DB_PATH}"
  --workspace-root "${WORKSPACE_ROOT}"
  --mode "${MODE}"
)
if [[ -n "${PROFILE}" ]]; then
  SEED_ARGS+=(--profile "${PROFILE}")
fi
if [[ "${AUTO_APPROVE}" -eq 1 ]]; then
  SEED_ARGS+=(--auto-approve)
fi

"${PYTHON}" "${REPO_ROOT}/scripts/seed_demo.py" "${SEED_ARGS[@]}"

# 2. Start server
echo ""
echo "[2/2] Starting Scopewatch gateway server on http://${HOST}:${PORT}..."
echo "  Reviewer UI (Live):      http://${HOST}:${PORT}/"
echo "  Reviewer UI (Static):    http://${HOST}:${PORT}/?live=0"
echo "  API Health:              http://${HOST}:${PORT}/api/v1/health"
echo "  Active Runs:             http://${HOST}:${PORT}/api/v1/runs"
echo "  Pending Approvals:       http://${HOST}:${PORT}/api/v1/approvals?status=PENDING"
echo ""
echo "Press Ctrl+C to stop the demonstration."
echo "=================================================================="
echo ""

export SCOPEWATCH_DB_PATH="${DB_PATH}"
export SCOPEWATCH_WORKSPACE_ROOT="${WORKSPACE_ROOT}"
export PYTHONPATH="${REPO_ROOT}/backend:${PYTHONPATH:-}"

exec "${PYTHON}" -m uvicorn scopewatch.app:app --host "${HOST}" --port "${PORT}"
