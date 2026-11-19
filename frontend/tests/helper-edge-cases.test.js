import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import {
  STATUS_GLYPHS,
  getHoldKind,
  getProvenanceLabel,
  getStatusGlyph,
  getSubmittedCommand,
} from "../scripts/app.js";
import {
  computeBackoffDelay,
  computeReconnectDelay,
  parseApiErrorBody,
  withRetry,
} from "../scripts/api.js";
import { reconcileSelection } from "../scripts/reviewer-state.js";

const here = dirname(fileURLToPath(import.meta.url));

// State labelling: the seven action states must stay distinct end to end.
// Pinned here so a refactor cannot fold two of them into one glyph.
test("attempted, allowed, held, denied, approved, executed and failed have distinct glyphs", () => {
  const states = ["attempted", "allowed", "held", "denied", "approved", "executed", "failed"];
  const glyphs = states.map((st) => getStatusGlyph(st));
  for (const glyph of glyphs) assert.ok(glyph, "every core state needs a non-empty glyph");
  assert.equal(new Set(glyphs).size, states.length);
});

test("getStatusGlyph returns the table glyph for known statuses and empty string for unknown", () => {
  for (const [status, glyph] of Object.entries(STATUS_GLYPHS)) {
    assert.equal(getStatusGlyph(status), glyph);
  }
  assert.equal(getStatusGlyph("not-a-real-status"), "");
  assert.equal(getStatusGlyph(undefined), "");
});

test("getStatusGlyph lets a hold kind override the status glyph", () => {
  assert.equal(getStatusGlyph("held", "concern"), "▲");
  assert.equal(getStatusGlyph("held", "failed"), "●");
  assert.equal(getStatusGlyph("held", "policy"), "■");
  // A hold kind on a non-held status still wins; callers pass null when not held.
  assert.equal(getStatusGlyph("allowed", null), STATUS_GLYPHS.allowed);
});

test("getHoldKind treats a plain held event with no reason code as a policy hold", () => {
  assert.equal(getHoldKind({ status: "held" }), "policy");
  assert.equal(getHoldKind({ status: "held", reasonCode: "APPROVAL_REQUIRED" }), "policy");
});

test("getHoldKind classifies audit reason codes on held events, not only pending-approval", () => {
  assert.equal(getHoldKind({ status: "held", reasonCode: "REASONING_SCOPE_CONCERN" }), "concern");
  assert.equal(getHoldKind({ status: "held", reasonCode: "REASONING_AUDIT_FAILED" }), "failed");
});

test("getHoldKind returns null for non-hold statuses even when audit reason codes are present", () => {
  for (const status of ["attempted", "allowed", "denied", "approved", "executed", "failed"]) {
    assert.equal(getHoldKind({ status, reasonCode: "REASONING_SCOPE_CONCERN" }), null, status);
  }
  assert.equal(getHoldKind(undefined), null);
});

test("getSubmittedCommand prefers a non-empty command string", () => {
  assert.equal(getSubmittedCommand({ command: "pytest -q", argv: ["ignored"] }), "pytest -q");
});

test("getSubmittedCommand joins argv when no command string is present", () => {
  assert.equal(getSubmittedCommand({ argv: ["python3", "-m", "pytest"] }), "python3 -m pytest");
  assert.equal(getSubmittedCommand({ command: "", argv: ["ls", "-la"] }), "ls -la");
});

test("getSubmittedCommand returns null when neither form is usable", () => {
  assert.equal(getSubmittedCommand({}), null);
  assert.equal(getSubmittedCommand({ command: "", argv: "ls" }), null);
  assert.equal(getSubmittedCommand({ command: 42 }), null);
});

test("getProvenanceLabel normalises case and surrounding whitespace before lookup", () => {
  assert.equal(getProvenanceLabel("  provider_exposed_trace "), "Provider-exposed trace");
  assert.equal(getProvenanceLabel("unavailable"), "Unavailable");
});

test("getProvenanceLabel passes unknown values through unchanged, case preserved", () => {
  assert.equal(getProvenanceLabel("Mystery Source"), "Mystery Source");
});

test("computeReconnectDelay doubles from 1s and caps at 30s", () => {
  assert.equal(computeReconnectDelay(0), 1000);
  assert.equal(computeReconnectDelay(1), 2000);
  assert.equal(computeReconnectDelay(2), 4000);
  assert.equal(computeReconnectDelay(4), 16000);
  assert.equal(computeReconnectDelay(5), 30000);
  assert.equal(computeReconnectDelay(50), 30000);
});

test("computeBackoffDelay treats garbage attempts as attempt 0", () => {
  assert.equal(computeBackoffDelay(-3, 300, 5000), 300);
  assert.equal(computeBackoffDelay(Number.NaN, 300, 5000), 300);
  assert.equal(computeBackoffDelay("nope", 300, 5000), 300);
  assert.equal(computeBackoffDelay(2.9, 300, 5000), 1200);
});

test("computeBackoffDelay with zero or invalid base never schedules a delay", () => {
  assert.equal(computeBackoffDelay(3, 0, 5000), 0);
  assert.equal(computeBackoffDelay(3, -100, 5000), 0);
  assert.equal(computeBackoffDelay(3, "abc", 5000), 0);
});

test("parseApiErrorBody reads the nested gateway envelope with details", () => {
  const body = { error: { code: "POLICY_DENIED", message: "blocked by policy", details: { rule: "r1" } } };
  assert.deepEqual(parseApiErrorBody(body, 403, "Forbidden"), {
    code: "POLICY_DENIED",
    message: "blocked by policy",
    details: { rule: "r1" },
  });
});

test("parseApiErrorBody reads the flat demo-guard envelope", () => {
  const body = { error: "UNAUTHORIZED", message: "Reviewer token required." };
  assert.deepEqual(parseApiErrorBody(body, 401, "Unauthorized"), {
    code: "UNAUTHORIZED",
    message: "Reviewer token required.",
    details: null,
  });
});

test("parseApiErrorBody accepts FastAPI-style detail and top-level error_code", () => {
  assert.deepEqual(parseApiErrorBody({ detail: "Not found" }, 404, "Not Found"), {
    code: "HTTP_ERROR",
    message: "Not found",
    details: null,
  });
  assert.deepEqual(parseApiErrorBody({ error_code: "RATE_LIMITED" }, 429, "Too Many Requests"), {
    code: "RATE_LIMITED",
    message: "HTTP error 429 (Too Many Requests)",
    details: null,
  });
});

test("parseApiErrorBody never lets a non-string message leak through", () => {
  const out = parseApiErrorBody({ message: { nested: "<img src=x onerror=alert(1)>" } }, 500, "Server Error");
  assert.equal(out.message, "HTTP error 500 (Server Error)");
});

test("withRetry with retries 0 throws the first failure without sleeping", async () => {
  const sleeps = [];
  let calls = 0;
  await assert.rejects(
    withRetry(async () => {
      calls += 1;
      throw new Error("first and only");
    }, { retries: 0, sleep: (ms) => { sleeps.push(ms); return Promise.resolve(); } }),
    /first and only/,
  );
  assert.equal(calls, 1);
  assert.deepEqual(sleeps, []);
});

test("withRetry clamps negative retries to zero", async () => {
  let calls = 0;
  await assert.rejects(
    withRetry(async () => {
      calls += 1;
      throw new Error("boom");
    }, { retries: -5, sleep: () => Promise.resolve() }),
    /boom/,
  );
  assert.equal(calls, 1);
});

test("withRetry backoff schedule respects maxDelayMs", async () => {
  const sleeps = [];
  await assert.rejects(
    withRetry(async () => { throw new Error("down"); }, {
      retries: 4,
      baseDelayMs: 1000,
      maxDelayMs: 2500,
      sleep: (ms) => { sleeps.push(ms); return Promise.resolve(); },
    }),
    /down/,
  );
  assert.deepEqual(sleeps, [1000, 2000, 2500, 2500]);
});

test("reconcileSelection falls back to the first visible event when the selected run is unknown", () => {
  const runs = [{ id: "r1", events: [{ id: "e1" }, { id: "e2" }] }];
  const visible = [{ id: "e2" }];
  assert.equal(reconcileSelection(runs, "missing-run", "e1", visible), "e2");
});

test("reconcileSelection returns null when nothing is visible", () => {
  const runs = [{ id: "r1", events: [{ id: "e1" }] }];
  assert.equal(reconcileSelection(runs, "r1", "e1", []), null);
});

// Untrusted-text rule: extends the existing innerHTML guard to every DOM
// sink and dynamic-code path the shipped scripts could reach.
test("shipped scripts avoid every HTML-parsing and dynamic-code sink", () => {
  const scriptsDir = join(here, "..", "scripts");
  const banned = /innerHTML|outerHTML|insertAdjacentHTML|document\.write|\.srcdoc\b|new Function\s*\(|\beval\s*\(/;
  for (const name of readdirSync(scriptsDir).filter((f) => f.endsWith(".js"))) {
    const src = readFileSync(join(scriptsDir, name), "utf-8");
    const code = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
    assert.ok(!banned.test(code), `${name} must not use an HTML-parsing or dynamic-code sink`);
  }
});
