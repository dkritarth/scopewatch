#!/usr/bin/env bash
set -euo pipefail

# Directory discovery
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
DB_PATH="${REPO_ROOT}/runtime-data/scopewatch.db"
WORKSPACE_ROOT="${REPO_ROOT}/demo/workspace"
DB_PATH_SET=0
WORKSPACE_ROOT_SET=0
MODE="scripted"
PROFILE=""
AUTO_APPROVE=0
CODING=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      cat <<'EOF'
Usage: scripts/run_demo.sh [options]

Options:
  --mode scripted|agent   How scenarios run (default: scripted).
                          scripted: submit each scenario action directly.
                          agent: replay each scenario through the agent loop
                            with the mock provider (deterministic replay, not
                            model choice). For genuine model choice, pass
                            --profile <live-profile> with --mode agent.
  --agent                 Shorthand for --mode agent (mock replay).
  --profile NAME          Provider profile for --mode agent (default: mock).
                          Any non-mock profile makes live model calls.
  --coding                Run the coding sequence 10-13 (issue #38) with the
                          synthetic coding-workspace fixture instead of 01-06.
  --auto-approve          Auto-approve HOLD actions while seeding.
  --db-path PATH          SQLite database to seed and serve (default:
                          runtime-data/scopewatch.db inside the repository).
  --workspace-root PATH   Synthetic scenario fixture each run is copied from
                          (default: demo/workspace inside the repository).
                          The fixture itself is read-only baseline: runs never
                          write to it. Each run executes against its own copy
                          under a '-runs' sibling directory (override with
                          SCOPEWATCH_RUN_WORKSPACES_DIR).
  --port PORT --host HOST Gateway bind address (defaults: 127.0.0.1:8000).

Scenario files carry no per-scenario mode key; the global --mode above is
the sole control. See demo/SCENARIOS.md for scripted vs mock-agent
(replay) vs live.
EOF
      exit 0
      ;;
    --agent)
      MODE="agent"
      shift
      ;;
    --coding)
      CODING=1
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
    --db-path)
      DB_PATH="$2"
      DB_PATH_SET=1
      shift 2
      ;;
    --workspace-root)
      WORKSPACE_ROOT="$2"
      WORKSPACE_ROOT_SET=1
      shift 2
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

# The coding sequence (issue #38) runs against an isolated runtime workspace
# seeded from the synthetic demo/coding-workspace fixture, never the fixture
# directory itself, so the agent's fix cannot dirty the repository.
if [[ "${CODING}" -eq 1 ]]; then
  if [[ "${WORKSPACE_ROOT_SET}" -eq 0 && "${WORKSPACE_ROOT}" == "${REPO_ROOT}/demo/workspace" ]]; then
    WORKSPACE_ROOT="${REPO_ROOT}/runtime-data/coding-workspace"
  fi
  if [[ "${DB_PATH_SET}" -eq 0 ]]; then
    DB_PATH="${REPO_ROOT}/runtime-data/scopewatch-coding.db"
  fi
fi

# Per-run workspace copies (issue #117). Default under runtime-data/ so a demo
# run never dirties the tracked demo/ tree, and so the gateway finds them again
# after a restart. Runs never write to the scenario fixture itself.
export SCOPEWATCH_RUN_WORKSPACES_DIR="${SCOPEWATCH_RUN_WORKSPACES_DIR:-${REPO_ROOT}/runtime-data/run-workspaces}"

echo "=================================================================="
echo "Scopewatch Local Demonstration"
if [[ "${CODING}" -eq 1 ]]; then
  echo "Synthetic coding-scenario sequence 10-13 (mode: ${MODE})"
else
  echo "Synthetic mediation gateway baseline (mode: ${MODE})"
fi
if [[ "${MODE}" == "agent" && -z "${PROFILE}" ]]; then
  echo "Agent mode with the default mock provider is deterministic replay,"
  echo "not model choice (see demo/SCENARIOS.md)."
fi
echo "=================================================================="
echo "Safety statement:"
echo "This local baseline mediates only actions submitted through its"
echo "synthetic demo gateway. It does not intercept arbitrary host or"
echo "agent operations."
echo "=================================================================="
echo ""

# 1. Seed workspace fixtures and scenarios
echo "[1/2] Seeding synthetic workspace and demonstration scenarios..."
mkdir -p "$(dirname "${DB_PATH}")"

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
if [[ "${CODING}" -eq 1 ]]; then
  SEED_ARGS+=(--coding)
fi

"${PYTHON}" "${REPO_ROOT}/scripts/seed_demo.py" "${SEED_ARGS[@]}"

# 2. Start server
echo ""
echo "[2/2] Starting Scopewatch gateway server on http://${HOST}:${PORT}..."
echo "  Reviewer UI (Live):      http://${HOST}:${PORT}/?live=1"
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

echo "Run workspaces: ${SCOPEWATCH_RUN_WORKSPACES_DIR} (one directory per run)"
echo ""

exec "${PYTHON}" -m uvicorn scopewatch.app:app --host "${HOST}" --port "${PORT}"
