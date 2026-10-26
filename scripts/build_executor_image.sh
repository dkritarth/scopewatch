#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE="${SCOPEWATCH_EXECUTOR_IMAGE:-scopewatch-executor:local}"

docker build \
  -f "${REPO_ROOT}/deploy/executor-image/Dockerfile" \
  -t "${IMAGE}" \
  "${REPO_ROOT}"

echo "Built ${IMAGE}. Select it with SCOPEWATCH_EXECUTOR_IMAGE=${IMAGE}."