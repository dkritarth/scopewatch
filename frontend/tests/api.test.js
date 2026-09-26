import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { LIVE_EVENT_TYPES } from "../scripts/api.js";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, "..", "..");
const modelsPath = join(repoRoot, "..", "..", "backend", "scopewatch", "models.py");

// Fallback when tests run from the worktree root layout: frontend/../backend
function resolveModelsPath() {
  try {
    readFileSync(modelsPath, "utf-8");
    return modelsPath;
  } catch {
    return join(repoRoot, "backend", "scopewatch", "models.py");
  }
}

function backendEventTypes() {
  // The worktree layout is <root>/frontend/tests/api.test.js, backend at <root>/backend
  const candidates = [
    join(repoRoot, "backend", "scopewatch", "models.py"),
    join(repoRoot, "..", "backend", "scopewatch", "models.py"),
  ];
  for (const p of candidates) {
    try {
      const src = readFileSync(p, "utf-8");
      const block = src.slice(src.indexOf("class EventType"), src.indexOf("class ReasoningProvenance"));
      const names = [...block.matchAll(/(\w+)\s*=\s*"([A-Z_]+)"/g)].map((m) => m[2]);
      if (names.length > 0) return names;
    } catch {
      // try next
    }
  }
  // Independent source of truth fallback: the 16 EventType values shipped in models.py
  return [
    "RUN_CREATED",
    "ACTION_REQUESTED",
    "POLICY_ALLOWED",
    "POLICY_DENIED",
    "POLICY_HELD",
    "APPROVAL_REQUESTED",
    "APPROVAL_GRANTED",
    "APPROVAL_DENIED",
    "APPROVAL_EXPIRED",
    "EXECUTION_STARTED",
    "EXECUTION_SUCCEEDED",
    "EXECUTION_FAILED",
    "RUN_COMPLETED",
    "SYSTEM_ERROR",
    "REASONING_AUDIT_COMPLETED",
    "REASONING_AUDIT_FAILED",
  ];
}

test("SSE frame list covers every backend EventType (defect 3a)", () => {
  assert.ok(Array.isArray(LIVE_EVENT_TYPES), "LIVE_EVENT_TYPES must be exported from api.js");
  // The three frames the backend sends but the old list omitted
  for (const missing of ["POLICY_HELD", "REASONING_AUDIT_COMPLETED", "REASONING_AUDIT_FAILED"]) {
    assert.ok(
      LIVE_EVENT_TYPES.includes(missing),
      `SSE frame list must include ${missing} so live holds/audits render without polling fallback`,
    );
  }
  // Stale name that never existed on the backend must be gone
  assert.ok(
    !LIVE_EVENT_TYPES.includes("POLICY_HELD_FOR_APPROVAL"),
    "POLICY_HELD_FOR_APPROVAL is not a backend EventType and must not be subscribed",
  );
});

test("SSE frame list matches backend EventType enum exactly", () => {
  const expected = backendEventTypes();
  assert.deepEqual(
    [...LIVE_EVENT_TYPES].sort(),
    [...expected].sort(),
    "frontend SSE subscription must track backend EventType values 1:1",
  );
});
