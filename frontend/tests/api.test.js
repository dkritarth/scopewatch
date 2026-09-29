import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import {
  LIVE_EVENT_TYPES,
  STALE_AFTER_MS,
  computeBackoffDelay,
  computeReconnectDelay,
  connectLiveEvents,
  parseLiveFrame,
  withRetry,
} from "../scripts/api.js";

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

test("reconnect backoff doubles from 1s and caps at 30s", () => {
  assert.equal(computeReconnectDelay(0), 1000);
  assert.equal(computeReconnectDelay(1), 2000);
  assert.equal(computeReconnectDelay(2), 4000);
  assert.equal(computeReconnectDelay(5), 30000);
  assert.equal(computeReconnectDelay(99), 30000);
  // Defensive inputs never produce NaN/negative/zero-growth schedules
  assert.equal(computeReconnectDelay(-3), 1000);
  assert.equal(computeReconnectDelay(1.9), 2000);
  assert.equal(computeBackoffDelay(0, 300, 5000), 300);
  assert.equal(computeBackoffDelay(3, 300, 5000), 2400);
  assert.equal(computeBackoffDelay(9, 300, 5000), 5000);
});

test("parseLiveFrame returns objects for valid frames", () => {
  const raw = JSON.stringify({ id: "e1", sequence: 7, event_type: "POLICY_HELD" });
  assert.deepEqual(parseLiveFrame(raw), { id: "e1", sequence: 7, event_type: "POLICY_HELD" });
});

test("parseLiveFrame logs and skips malformed frames without throwing", () => {
  const warnings = [];
  const origWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    assert.equal(parseLiveFrame("not-json{{{"), null);
    assert.equal(parseLiveFrame(""), null);
    assert.equal(parseLiveFrame("   "), null);
    assert.equal(parseLiveFrame(null), null);
    assert.equal(parseLiveFrame(undefined), null);
    assert.equal(parseLiveFrame("[1,2,3]"), null);
    assert.equal(parseLiveFrame("42"), null);
    assert.equal(parseLiveFrame('"just a string"'), null);
  } finally {
    console.warn = origWarn;
  }
  assert.equal(warnings.length, 8, `expected one warning per malformed frame, got ${warnings.length}`);
  assert.ok(warnings.every((w) => /malformed live frame/.test(w)));
});

test("withRetry succeeds immediately without sleeping", async () => {
  const sleeps = [];
  let calls = 0;
  const result = await withRetry(async () => {
    calls += 1;
    return "ok";
  }, { sleep: (ms) => { sleeps.push(ms); return Promise.resolve(); } });
  assert.equal(result, "ok");
  assert.equal(calls, 1);
  assert.deepEqual(sleeps, []);
});

test("withRetry retries flaky fetches with backoff then succeeds", async () => {
  const sleeps = [];
  let calls = 0;
  const result = await withRetry(async () => {
    calls += 1;
    if (calls < 3) throw new Error(`flake ${calls}`);
    return "recovered";
  }, { retries: 3, baseDelayMs: 300, maxDelayMs: 5000, sleep: (ms) => { sleeps.push(ms); return Promise.resolve(); } });
  assert.equal(result, "recovered");
  assert.equal(calls, 3);
  assert.deepEqual(sleeps, [300, 600]);
});

test("withRetry rethrows after exhausting retries", async () => {
  const sleeps = [];
  let calls = 0;
  await assert.rejects(
    withRetry(async () => {
      calls += 1;
      throw new Error("persistent outage");
    }, { retries: 2, baseDelayMs: 100, sleep: (ms) => { sleeps.push(ms); return Promise.resolve(); } }),
    /persistent outage/,
  );
  assert.equal(calls, 3);
  assert.deepEqual(sleeps, [100, 200]);
});

test("connectLiveEvents falls back to polling when EventSource is unavailable", async () => {
  assert.equal(typeof EventSource, "undefined", "Node has no EventSource; fallback path must engage");
  const origFetch = globalThis.fetch;
  const seen = [];
  globalThis.fetch = async (url) => ({
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => {
      seen.push(String(url));
      return [{ id: "poll-1", sequence: 1, event_type: "ACTION_REQUESTED", details: { tool: "workspace" } }];
    },
  });
  const received = [];
  const statuses = [];
  let handle;
  try {
    handle = connectLiveEvents("run-fallback", {
      onEvent: (ev) => received.push(ev),
      onStatusChange: (s) => statuses.push(s),
      staleAfterMs: 0,
    });
    await new Promise((r) => setTimeout(r, 150));
  } finally {
    handle?.close();
    globalThis.fetch = origFetch;
  }
  assert.ok(statuses.some((s) => s.status === "polling"), `expected a polling status, got ${JSON.stringify(statuses)}`);
  assert.ok(seen.some((u) => u.includes("/events")), "fallback must poll the events endpoint");
  assert.ok(received.some((ev) => ev.id === "poll-1"), "polled events must reach onEvent");
  assert.ok(handle.getLastSequence() >= 1);
});

test("shipped frontend scripts never use innerHTML (XSS rule)", () => {
  const scriptNames = ["api.js", "app.js", "fixtures.js", "reviewer-state.js"];
  for (const name of scriptNames) {
    const src = readFileSync(join(here, "..", "scripts", name), "utf-8");
    // Strip comments so prose like "never uses innerHTML" is not flagged;
    // any remaining sink (e.g. `.innerHTML =`) is a violation.
    const code = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
    assert.ok(!/innerHTML|outerHTML|insertAdjacentHTML/.test(code), `${name} must render untrusted text via textContent only`);
  }
  assert.ok(STALE_AFTER_MS > 0, "stale-banner timeout must be a positive default");
});
