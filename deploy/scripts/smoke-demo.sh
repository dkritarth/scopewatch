#!/usr/bin/env bash
# Acceptance smoke test for the hosted demo (issue #42).
#
# Checks, against a running stack (local compose or the VM):
#   1. Health + static dashboard load WITHOUT any token.
#   2. Run creation WITHOUT the token returns 401.
#   3. Run creation WITH the token succeeds (and cleans up after itself).
#   4. (Optional, destructive to budgets) --exhaust-budget also verifies the
#      daily-budget refusal path by pointing at a throwaway stack with
#      DEMO_MAX_RUNS_PER_DAY=1. See RUNBOOK.md "Verify the demo".
#
# Usage:
#   ./scripts/smoke-demo.sh [base-url] [demo-token]
#   ./scripts/smoke-demo.sh --exhaust-budget [base-url] [demo-token]
set -euo pipefail

EXHAUST=0
if [[ "${1:-}" == "--exhaust-budget" ]]; then
  EXHAUST=1
  shift
fi
BASE="${1:-http://127.0.0.1:8080}"
TOKEN="${2:-${DEMO_TOKEN:-}}"

pass() { echo "  PASS: $1"; }
fail() { echo "  FAIL: $1" >&2; exit 1; }

echo "Smoke test against ${BASE}"

# 1. Health without token (through the gate).
CODE="$(curl -sS -o /tmp/smoke-health.json -w '%{http_code}' "${BASE}/api/v1/health")"
[[ "${CODE}" == "200" ]] || fail "health returned ${CODE}"
grep -q '"status"[[:space:]]*:[[:space:]]*"ok"' /tmp/smoke-health.json || fail "health body not ok"
pass "health loads without token (200)"

# 1b. Dashboard without token.
CODE="$(curl -sS -o /tmp/smoke-index.html -w '%{http_code}' "${BASE}/")"
[[ "${CODE}" == "200" ]] || fail "dashboard returned ${CODE}"
grep -qi "scopewatch" /tmp/smoke-index.html || fail "dashboard body unexpected"
pass "dashboard loads without token (200)"

# 2. Run creation without token -> 401.
CODE="$(curl -sS -o /tmp/smoke-401.json -w '%{http_code}' -X POST "${BASE}/api/v1/runs" \
  -H 'Content-Type: application/json' \
  -d '{"name":"smoke","task_scope":{"task_description":"smoke","created_at":"2026-01-01T00:00:00Z"}}')"
[[ "${CODE}" == "401" ]] || fail "run creation without token returned ${CODE}, want 401"
grep -q "demo_token_required" /tmp/smoke-401.json || fail "401 body missing error code"
pass "run creation without token returns 401"

# 3. Run creation with token -> 201, then fail the run to keep the demo tidy.
[[ -n "${TOKEN}" ]] || fail "no token: pass one as \$2 or DEMO_TOKEN for the authed checks"
CODE="$(curl -sS -o /tmp/smoke-run.json -w '%{http_code}' -X POST "${BASE}/api/v1/runs" \
  -H 'Content-Type: application/json' -H "X-Demo-Token: ${TOKEN}" \
  -d '{"name":"smoke-test-run","task_scope":{"task_description":"smoke","created_at":"2026-01-01T00:00:00Z"}}')"
[[ "${CODE}" == "201" ]] || fail "run creation with token returned ${CODE}: $(cat /tmp/smoke-run.json)"
RUN_ID="$(python3 -c "import json;print(json.load(open('/tmp/smoke-run.json'))['id'])")"
pass "run creation with token returns 201 (run ${RUN_ID})"

curl -sS -o /dev/null -w '%{http_code}' -X POST "${BASE}/api/v1/runs/${RUN_ID}/fail?reason=smoke-test-cleanup" \
  -H "X-Demo-Token: ${TOKEN}" | grep -q "200" || fail "cleanup fail-run failed"
pass "cleanup: smoke run marked failed"

# 4. Budget exhaustion (only with --exhaust-budget, against a 1-run budget).
if [[ "${EXHAUST}" == "1" ]]; then
  CODE="$(curl -sS -o /tmp/smoke-budget.json -w '%{http_code}' -X POST "${BASE}/api/v1/runs" \
    -H 'Content-Type: application/json' -H "X-Demo-Token: ${TOKEN}" \
    -d '{"name":"smoke-over-budget","task_scope":{"task_description":"smoke","created_at":"2026-01-01T00:00:00Z"}}')"
  [[ "${CODE}" == "429" ]] || fail "over-budget run returned ${CODE}, want 429"
  grep -q "budget_exhausted" /tmp/smoke-budget.json || fail "429 body missing error code"
  grep -qi "budget exhausted" /tmp/smoke-budget.json || fail "429 message unclear"
  pass "over-budget run creation returns 429 budget_exhausted with clear message"
fi

echo "All smoke checks passed."
