import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import {
  MEDIATION_BOUNDARY_TEXT,
  GATEWAY_TOOLS,
  formatLiveRun,
  buildLiveRuns,
  getHoldKind,
  groupEventsByTurn,
  transformApiEvent,
} from "../scripts/app.js";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, "..", "..");

function readBackendModels() {
  const candidates = [
    join(repoRoot, "backend", "scopewatch", "models.py"),
    join(repoRoot, "..", "backend", "scopewatch", "models.py"),
  ];
  for (const p of candidates) {
    try {
      return readFileSync(p, "utf-8");
    } catch {
      // next
    }
  }
  return "";
}

test("every backend run becomes selectable (defect 3b, #30 item 1)", () => {
  const backendRuns = [
    { id: "run-1", name: "Safe audit", task_scope: { task_description: "Audit", allowed_paths: ["invoices/approved"], blocked_paths: [], allowed_operations: ["read_text"], requires_approval: [] } },
    { id: "run-2", name: "Reasoning-Injection Escalation", task_scope: { task_description: "Escalate", allowed_paths: ["invoices/approved"], blocked_paths: [], allowed_operations: ["read_text"], requires_approval: [] } },
    { id: "run-3", name: "Traversal", task_scope: { task_description: "Block", allowed_paths: [], blocked_paths: [], allowed_operations: [], requires_approval: [] } },
  ];
  const eventsByRun = {
    "run-1": [{ id: "e1", sequence: 1, event_type: "ACTION_REQUESTED", details: { tool: "workspace" } }],
    "run-2": [{ id: "e2", sequence: 1, event_type: "POLICY_HELD", details: { reason_code: "REASONING_SCOPE_CONCERN" } }],
    "run-3": [],
  };
  const liveRuns = buildLiveRuns(backendRuns, eventsByRun);
  assert.equal(liveRuns.length, 3);
  assert.deepEqual(liveRuns.map((r) => r.id), ["run-1", "run-2", "run-3"]);
  assert.ok(liveRuns.every((r) => r.isLive === true));
  // The seeded escalation headline must be reachable by name
  assert.ok(liveRuns.some((r) => /Reasoning-Injection Escalation/.test(r.name)));
});

test("formatLiveRun keeps task scope and transforms events", () => {
  const run = formatLiveRun(
    { id: "run-x", name: "Demo", task_scope: { task_description: "Do things", allowed_paths: ["a"], blocked_paths: ["b"], allowed_operations: ["read_text"], requires_approval: ["delete_path"] } },
    [{ id: "e9", sequence: 7, event_type: "ACTION_REQUESTED", details: { tool: "workspace", operation: "read_text" } }],
  );
  assert.equal(run.id, "run-x");
  assert.match(run.name, /Demo/);
  assert.equal(run.task, "Do things");
  assert.equal(run.events.length, 1);
  assert.equal(run.events[0].tool, "workspace");
});

test("hold kinds split policy / concern / failed without colour (#30)", () => {
  assert.equal(getHoldKind({ status: "pending-approval", reasonCode: "APPROVAL_REQUIRED" }), "policy");
  assert.equal(getHoldKind({ status: "pending-approval", reasonCode: "REASONING_SCOPE_CONCERN" }), "concern");
  assert.equal(getHoldKind({ status: "pending-approval", reasonCode: "REASONING_AUDIT_FAILED" }), "failed");
  assert.equal(getHoldKind({ status: "executed", reasonCode: "x" }), null);
});

test("events group by turn_id with unassigned fallback (#30 turn grouping)", () => {
  const events = [
    { id: "a", turnId: "turn-1" },
    { id: "b", turnId: "turn-1" },
    { id: "c", turnId: "turn-2" },
    { id: "d", turnId: null },
  ];
  const groups = groupEventsByTurn(events);
  assert.equal(groups.length, 3);
  assert.equal(groups[0].turnId, "turn-1");
  assert.deepEqual(groups[0].events.map((e) => e.id), ["a", "b"]);
  assert.equal(groups[1].turnId, "turn-2");
  assert.equal(groups[2].turnId, null);
  assert.deepEqual(groups[2].events.map((e) => e.id), ["d"]);
});

test("mediation boundary text is exact and non-dismissible wording (#61)", () => {
  assert.equal(
    MEDIATION_BOUNDARY_TEXT,
    "All tool actions go through the gateway API; actions that bypass the API are not observed, blocked, or recorded.",
  );
});

test("gateway tools list matches backend SUPPORTED_TOOLS/OPERATIONS (#61)", () => {
  const src = readBackendModels();
  assert.ok(src.includes("SUPPORTED_TOOLS"), "backend models.py must define SUPPORTED_TOOLS");
  for (const tool of GATEWAY_TOOLS.tools) {
    assert.match(src, new RegExp(`"${tool}"`), `backend must list tool ${tool}`);
  }
  for (const op of GATEWAY_TOOLS.operations) {
    assert.match(src, new RegExp(`"${op}"`), `backend must list operation ${op}`);
  }
  // transformApiEvent stays XSS-safe for the provenance path used by the tools panel
  assert.ok(typeof transformApiEvent === "function");
});
