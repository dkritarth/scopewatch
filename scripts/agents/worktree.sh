#!/usr/bin/env bash
# Create an isolated git worktree for one issue so parallel agent threads never share a checkout.
#
# Usage: scripts/agents/worktree.sh <issue-number> <short-slug> [branch-prefix]
#   branch-prefix defaults to "feature" (use fix, docs, chore, test, or spike as needed)
#
# Creates ../scopewatch-worktrees/<issue>-<slug> on branch <prefix>/<issue>-<slug>, based on origin/main.
# Remove it after merge with: git worktree remove ../scopewatch-worktrees/<issue>-<slug>
set -euo pipefail

if [[ $# -lt 2 ]]; then
  sed -n '2,8p' "$0"
  exit 1
fi

ISSUE="$1"
SLUG="$2"
PREFIX="${3:-feature}"
REPO_ROOT="$(git rev-parse --show-toplevel)"
NAME="${ISSUE}-${SLUG}"
BRANCH="${PREFIX}/${NAME}"
TARGET="$(dirname "${REPO_ROOT}")/scopewatch-worktrees/${NAME}"

git -C "${REPO_ROOT}" fetch --quiet origin main
if git -C "${REPO_ROOT}" show-ref --verify --quiet "refs/heads/${BRANCH}"; then
  git -C "${REPO_ROOT}" worktree add "${TARGET}" "${BRANCH}"
else
  git -C "${REPO_ROOT}" worktree add --no-track -b "${BRANCH}" "${TARGET}" origin/main
fi

echo "Worktree: ${TARGET}"
echo "Branch:   ${BRANCH}  (first push: git push -u origin ${BRANCH})"
