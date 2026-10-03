import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import {
  LIVE_EVENT_TYPES,
  STALE_AFTER_MS,
  clearReviewerToken,
  computeBackoffDelay,
  computeReconnectDelay,
  connectLiveEvents,
  fetchJson,
  getReviewerToken,
  isMutatingRequest,
  parseApiErrorBody,
  parseLiveFrame,
  setReviewerToken,
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

test("isMutatingRequest mirrors the gateway demo-guard boundary (#115)", () => {
  // Same rule as backend/scopewatch/demo_guards.py is_mutating_api_call:
  // mutating method AND /api/ path. Everything else is a public read.
  assert.equal(isMutatingRequest("POST", "/api/v1/runs"), true);
  assert.equal(isMutatingRequest("POST", "/api/v1/runs/abc/actions"), true);
  assert.equal(isMutatingRequest("POST", "/api/v1/approvals/x/approve"), true);
  assert.equal(isMutatingRequest("DELETE", "/api/v1/runs/abc"), true);
  assert.equal(isMutatingRequest("PUT", "/api/v1/runs/abc"), true);
  assert.equal(isMutatingRequest("PATCH", "/api/v1/runs/abc"), true);
  assert.equal(isMutatingRequest("GET", "/api/v1/runs"), false);
  assert.equal(isMutatingRequest("GET", "/api/v1/health"), false);
  assert.equal(isMutatingRequest("HEAD", "/api/v1/runs"), false);
  assert.equal(isMutatingRequest("POST", "/"), false);
  assert.equal(isMutatingRequest("POST", "/index.html"), false);
  assert.equal(isMutatingRequest("POST", undefined), false);
  // Query strings do not change the verdict.
  assert.equal(isMutatingRequest("POST", "/api/v1/runs?x=1"), true);
});

test("reviewer token is memory-only: stored, trimmed, and cleared (#115)", () => {
  clearReviewerToken();
  assert.equal(getReviewerToken(), "");
  setReviewerToken("  judge-token-abc  ");
  assert.equal(getReviewerToken(), "judge-token-abc");
  clearReviewerToken();
  assert.equal(getReviewerToken(), "");
  // A non-string never becomes a token that could be sent as a header.
  setReviewerToken(undefined);
  assert.equal(getReviewerToken(), "");
  // Nothing is persisted: no storage API is touched by api.js at all
  // (comments stripped so the prose naming these APIs is not a false hit).
  const apiCode = readFileSync(join(here, "..", "scripts", "api.js"), "utf-8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\/\/.*$/gm, "");
  assert.ok(!/localStorage|sessionStorage|document\.cookie/.test(apiCode),
    "the reviewer token must never be written to browser storage");
});

test("fetchJson sends X-Demo-Token only on mutating API requests (#115)", async () => {
  const origFetch = globalThis.fetch;
  const seen = [];
  globalThis.fetch = async (url, options = {}) => {
    seen.push({ url: String(url), headers: { ...(options.headers || {}) } });
    return { ok: true, status: 200, statusText: "OK", json: async () => ({}) };
  };
  try {
    clearReviewerToken();
    await fetchJson("/api/v1/runs", { method: "POST", body: {} });
    assert.equal(seen.at(-1).headers["X-Demo-Token"], undefined, "no token held: none sent");

    setReviewerToken("judge-token-abc");
    await fetchJson("/api/v1/runs", { method: "POST", body: {} });
    assert.equal(seen.at(-1).headers["X-Demo-Token"], "judge-token-abc");

    // Reads stay credential-free even with a token held.
    await fetchJson("/api/v1/runs");
    assert.equal(seen.at(-1).headers["X-Demo-Token"], undefined, "reads must stay public");
    await fetchJson("/api/v1/runs/abc/events");
    assert.equal(seen.at(-1).headers["X-Demo-Token"], undefined, "event reads must stay public");

    // Static asset paths are not gateway API calls.
    await fetchJson("/", { method: "POST", body: {} });
    assert.equal(seen.at(-1).headers["X-Demo-Token"], undefined, "non-API paths carry no credential");

    // An explicit caller header wins; the client never overwrites it.
    await fetchJson("/api/v1/runs", {
      method: "POST",
      body: {},
      headers: { "X-Demo-Token": "explicit-token" },
    });
    assert.equal(seen.at(-1).headers["X-Demo-Token"], "explicit-token");
  } finally {
    clearReviewerToken();
    globalThis.fetch = origFetch;
  }
});

test("fetchJson error exposes code/status from both refusal envelopes (#107)", async () => {
  const origFetch = globalThis.fetch;
  const respond = (status, body) => {
    globalThis.fetch = async () => ({
      ok: false,
      status,
      statusText: "Error",
      json: async () => body,
    });
  };
  try {
    // Flat demo-guard / gate refusal: {"error": "<code>", "message": "..."}
    respond(401, { error: "demo_token_required", message: "Demo access token required." });
    const guardErr = await fetchJson("/api/v1/runs", { method: "POST", body: {} }).catch((e) => e);
    assert.equal(guardErr.status, 401);
    assert.equal(guardErr.code, "demo_token_required");
    assert.match(guardErr.message, /Demo access token required/);

    // Nested gateway envelope: {"error": {"code", "message", "details"}}
    respond(422, { error: { code: "SCHEMA_VALIDATION_ERROR", message: "name: bad", details: { field: "name" } } });
    const nestedErr = await fetchJson("/api/v1/runs", { method: "POST", body: {} }).catch((e) => e);
    assert.equal(nestedErr.code, "SCHEMA_VALIDATION_ERROR");
    assert.deepEqual(nestedErr.details, { field: "name" });

    // Legacy gate envelope still parses (a deployed older gate).
    respond(401, { error_code: "demo_token_required", detail: "Demo access token required." });
    const legacyErr = await fetchJson("/api/v1/runs", { method: "POST", body: {} }).catch((e) => e);
    assert.equal(legacyErr.code, "demo_token_required");
    assert.match(legacyErr.message, /Demo access token required/);
  } finally {
    globalThis.fetch = origFetch;
  }
});

test("parseApiErrorBody falls back to the status line for unknown bodies", () => {
  assert.deepEqual(parseApiErrorBody(null, 503, "Service Unavailable"), {
    code: "HTTP_ERROR",
    message: "HTTP error 503 (Service Unavailable)",
    details: null,
  });
  assert.deepEqual(parseApiErrorBody({ error: {} }, 500, "Server Error"), {
    code: "HTTP_ERROR",
    message: "HTTP error 500 (Server Error)",
    details: null,
  });
});
