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
SEED_ONLY=0

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
                          Those scenarios submit run_command actions, which
                          only execute on the Docker executor: this selects
                          SCOPEWATCH_EXECUTOR=docker when a Docker daemon is
                          reachable, and says so loudly when it is not
                          (without it, every run_command is denied
                          deterministically before execution).
  --seed-only             Seed the scenarios and exit without starting the
                          gateway (to inspect the evidence database, and to
                          let tests exercise the executor preflight).
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
    --seed-only)
      SEED_ONLY=1
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

# The gateway package lives in backend/; both the seeding subprocess and the
# server below need it on the import path.
export PYTHONPATH="${REPO_ROOT}/backend:${PYTHONPATH:-}"

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

# Executor preflight for the coding sequence (issue #204).
#
# Scenarios 10 and 13 submit run_command actions. The deterministic policy
# engine refuses those for every backend except Docker
# (policy.py RULE_RUN_COMMAND_REQUIRES_DOCKER), so with the default local
# backend a judge sees `DENY [UNSUPPORTED_OPERATION]` twice and the coding
# track looks broken even though the gateway behaved correctly. Silent DENYs
# are the failure mode this preflight removes: either the Docker executor is
# selected, or the reason is stated before seeding starts.
#
# An explicit SCOPEWATCH_EXECUTOR always wins: the operator may be pointing at
# a remote runner on purpose.
if [[ "${CODING}" -eq 1 ]]; then
  if [[ -z "${SCOPEWATCH_EXECUTOR:-}" ]]; then
    if "${PYTHON}" -c 'import sys
from scopewatch.executor_docker import is_docker_available
sys.exit(0 if is_docker_available() else 1)'; then
      export SCOPEWATCH_EXECUTOR=docker
      echo ""
      echo "Executor: docker (Docker daemon detected; run_command scenarios execute)."
    else
      echo "" >&2
      echo "WARNING: --coding needs the Docker executor, and no Docker daemon was" >&2
      echo "detected (SCOPEWATCH_EXECUTOR is unset). Scenarios 10 and 13 submit" >&2
      echo "run_command actions, which the deterministic policy denies for every" >&2
      echo "non-Docker backend, so the dashboard will show:" >&2
      echo "  DENY [UNSUPPORTED_OPERATION] for each run_command" >&2
      echo "That is the policy failing closed, not a broken gateway. To see the" >&2
      echo "coding commands execute: install Docker (and start the daemon), or run" >&2
      echo "a remote executor-runner and set SCOPEWATCH_EXECUTOR=remote." >&2
      echo "" >&2
    fi
  else
    echo ""
    echo "Executor: ${SCOPEWATCH_EXECUTOR} (from the environment)."
  fi

  # Preflight for the Docker executor image (issue #302): the configured image
  # must exist locally and provide pytest, probed with --network none. We never
  # install over the network at run time.
  if [[ "${SCOPEWATCH_EXECUTOR:-}" == "docker" ]]; then
    export SCOPEWATCH_EXECUTOR_IMAGE="${SCOPEWATCH_EXECUTOR_IMAGE:-scopewatch-executor:local}"
    if ! "${PYTHON}" "${REPO_ROOT}/scripts/preflight_executor_image.py"; then
      echo "" >&2
      echo "ERROR: coding demo requires a Docker executor image with pytest, and" >&2
      echo "the configured image (${SCOPEWATCH_EXECUTOR_IMAGE}) is not ready." >&2
      echo "Build it locally (no network at run time) with:" >&2
      echo "  ${REPO_ROOT}/scripts/build_executor_image.sh" >&2
      exit 1
    fi
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

# Capture credential (#116). Held by integrations that call the provider
# client in-process (this shell's demo seeder, a live agent CLI started from
# it). Generated per run, never committed. Submissions without it are recorded
# as caller-asserted reasoning provenance.
if [[ -z "${SCOPEWATCH_CAPTURE_TOKEN:-}" ]]; then
  SCOPEWATCH_CAPTURE_TOKEN="$("${PYTHON}" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  export SCOPEWATCH_CAPTURE_TOKEN
fi

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

# --seed-only: stop here so the evidence database can be inspected (or the
# executor preflight exercised) without binding a port.
if [[ "${SEED_ONLY}" -eq 1 ]]; then
  echo ""
  echo "Seeding complete (--seed-only); gateway not started."
  echo "  Evidence DB:  ${DB_PATH}"
  echo "  Workspace:    ${WORKSPACE_ROOT}"
  echo "  Executor:     $("${PYTHON}" -c 'from scopewatch.executor import get_executor_backend; print(get_executor_backend())')"
  echo "  Run workspaces: ${SCOPEWATCH_RUN_WORKSPACES_DIR}"
  exit 0
fi

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

echo "Run workspaces: ${SCOPEWATCH_RUN_WORKSPACES_DIR} (one directory per run)"
echo ""

exec "${PYTHON}" -m uvicorn scopewatch.app:app --host "${HOST}" --port "${PORT}"
