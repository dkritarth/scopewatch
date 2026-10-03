"use strict";

import { runs as fixtureRuns } from "./fixtures.js";
import {
  createInitialFilters,
  filterEvents,
  reconcileSelection,
  selectEvent,
  selectRun,
} from "./reviewer-state.js";
import {
  approveAction,
  checkHealth,
  connectLiveEvents,
  createRun,
  denyAction,
  getApprovals,
  getEvents,
  getRuns,
  submitAction,
  withRetry,
} from "./api.js";

export const PROVENANCE_LABELS = {
  PROVIDER_EXPOSED_TRACE: "Provider-exposed trace",
  AGENT_AUTHORED_SUMMARY: "Agent-authored summary",
  UNAVAILABLE: "Unavailable",
  SYNTHETIC_FIXTURE: "Synthetic fixture",
};

export function getProvenanceLabel(provenance) {
  if (!provenance) return "Unavailable";
  const normalized = String(provenance).toUpperCase().trim();
  return PROVENANCE_LABELS[normalized] || provenance;
}

/**
 * Persistent mediation-boundary statement (#61). Rendered verbatim in the UI
 * banner (`#mediation-boundary`) and in docs/ARCHITECTURE.md. Never dismissible.
 */
export const MEDIATION_BOUNDARY_TEXT =
  "All tool actions go through the gateway API; actions that bypass the API are not observed, blocked, or recorded.";

/**
 * Gateway-mediated tool set (#61). Mirrors backend/scopewatch/models.py
 * SUPPORTED_TOOLS / SUPPORTED_OPERATIONS; asserted by
 * frontend/tests/live-runs.test.js rather than by hand.
 */
export const GATEWAY_TOOLS = {
  tools: ["workspace"],
  operations: ["list_directory", "read_text", "write_text", "delete_path", "network_request", "run_command"],
};

/**
 * Command string for a submitted (not yet executed) action, preferring the
 * raw command string over the argv list form. Shared by the synthetic
 * submit handlers so the derivation lives in one place.
 */
export function getSubmittedCommand(args) {
  if (typeof args.command === "string" && args.command) {
    return args.command;
  }
  if (Array.isArray(args.argv)) {
    return args.argv.join(" ");
  }
  return null;
}

/**
 * Maximum characters of execution output shown in the evidence panel.
 * The executor already truncates run_command streams with a marker; this cap
 * keeps the dashboard readable for long receipts.
 */
export const EXECUTION_OUTPUT_DISPLAY_LIMIT = 2000;

/**
 * Classify a timeline event into its HOLD provenance for badge + icon.
 * Returns "policy" | "concern" | "failed" | null. Text label is the
 * non-colour signal; the icon glyph is redundant reinforcement, never alone.
 */
export function getHoldKind(event) {
  if (!event || event.status !== "pending-approval") return null;
  if (event.reasonCode === "REASONING_SCOPE_CONCERN") return "concern";
  if (event.reasonCode === "REASONING_AUDIT_FAILED") return "failed";
  return "policy";
}

export const HOLD_ICONS = {
  policy: "■",
  concern: "▲",
  failed: "●",
};

export function getHoldIcon(kind) {
  return HOLD_ICONS[kind] || "";
}

/**
 * Malformed-frame-tolerant wrapper around `transformApiEvent`.
 * Returns the transformed event, or null when the frame is unusable
 * (null/garbage input, thrown error, or missing id/sequence identity).
 * Every skip is logged with console.warn so dropped frames stay visible in
 * diagnostics instead of failing silently — and the timeline never crashes.
 */
export function safeTransformApiEvent(ev, context = null) {
  try {
    const out = transformApiEvent(ev, context);
    if (!out || typeof out.id === "undefined" || typeof out.sequence === "undefined") {
      console.warn("[scopewatch] skipping live frame with missing id/sequence");
      return null;
    }
    return out;
  } catch (err) {
    console.warn("[scopewatch] skipping malformed live frame:", err?.message || err);
    return null;
  }
}

/**
 * Two-step approve/deny state machine. First click arms the chosen action
 * ("Confirm approve"), second click on the same button fires it. Clicking
 * the other button switches the arm; `reset` (e.g. blur/timeout) disarms.
 * While `pending`, all clicks are ignored (disabled-while-pending).
 *
 * States: "idle" | "armed-approve" | "armed-deny" | "pending"
 * Clicks: "approve" | "deny" | "reset"
 * Returns { next, fire } where fire is "approve" | "deny" | null.
 */
export function approvalConfirmStep(state, clicked) {
  const current = state || "idle";
  if (current === "pending") return { next: "pending", fire: null };
  if (clicked === "reset" || clicked == null) {
    return { next: current === "pending" ? "pending" : "idle", fire: null };
  }
  if (clicked === "approve") {
    if (current === "armed-approve") return { next: "pending", fire: "approve" };
    return { next: "armed-approve", fire: null };
  }
  if (clicked === "deny") {
    if (current === "armed-deny") return { next: "pending", fire: "deny" };
    return { next: "armed-deny", fire: null };
  }
  return { next: current, fire: null };
}

export const APPROVAL_CONFIRM_COPY = {
  approve: "Approve",
  deny: "Deny",
  confirmApprove: "Confirm approve",
  confirmDeny: "Confirm deny",
  armedNote: "Click again to confirm. This resolution is single-use.",
};

/**
 * Panel loading/error/empty status mapping. Pure so unit tests pin the copy:
 * loading beats error beats empty; a populated panel reports "ready" with
 * empty text (the panel renders its own content then).
 */
export function panelStatus({ loading = false, error = null, count = 0 } = {}, copy = {}) {
  const loadingText = copy.loadingText || "Loading…";
  const errorText = copy.errorText || "Could not load. Retry to try again.";
  const emptyText = copy.emptyText || "No items.";
  if (loading) return { kind: "loading", text: loadingText };
  if (error) return { kind: "error", text: error instanceof Error ? `${errorText} (${error.message})` : errorText };
  if (!count) return { kind: "empty", text: emptyText };
  return { kind: "ready", text: "" };
}

/**
 * Render a `panelStatus` result into a `<p role="status">` region.
 * Element-lite: works with any `{ hidden, textContent, dataset? }` shape so
 * unit tests can pass plain objects. Returns the status kind.
 */
export function renderPanelStatus(region, status) {
  if (!region || !status) return status?.kind || "ready";
  region.textContent = status.text || "";
  region.hidden = status.kind === "ready";
  return status.kind;
}

/**
 * SSE banner copy. The banner is text + icon, never colour alone: the
 * reconnecting/stale glyph (↻/⚠) reinforces the sentence, which is the
 * primary signal. Connected/hidden states carry no banner.
 */
export function getSseBannerCopy(status, detail = "") {
  if (status === "reconnecting") {
    return { visible: true, glyph: "↻", text: `Live stream reconnecting. ${detail || "Retrying with backoff…"}`.trim() };
  }
  if (status === "stale") {
    return { visible: true, glyph: "⚠", text: detail || "Live stream is stale. Showing last known state." };
  }
  if (status === "polling" || status === "polling_error") {
    return { visible: true, glyph: "⇄", text: detail || "Live stream unavailable. Polling for updates." };
  }
  return { visible: false, glyph: "", text: "" };
}

/**
 * Group timeline events by turn_id, preserving first-seen order.
 * Events without a turn form a trailing "unassigned" group (turnId null).
 */
export function groupEventsByTurn(events) {
  const groups = [];
  const indexByTurn = new Map();
  for (const event of events || []) {
    const key = event?.turnId ?? event?.turn_id ?? null;
    if (key !== null && indexByTurn.has(key)) {
      groups[indexByTurn.get(key)].events.push(event);
    } else if (key === null) {
      const last = groups[groups.length - 1];
      if (last && last.turnId === null) {
        last.events.push(event);
      } else {
        indexByTurn.delete(null);
        groups.push({ turnId: null, events: [event] });
      }
    } else {
      indexByTurn.set(key, groups.length);
      groups.push({ turnId: key, events: [event] });
    }
  }
  return groups;
}

/**
 * Format one backend run + its raw API events into a selectable timeline run.
 * Exported so tests assert every seeded scenario run stays reachable.
 */
export function formatLiveRun(backendRun, rawEvents = []) {
  const transformedEvents = (rawEvents || []).map((e) => transformApiEvent(e));
  const scope = backendRun?.task_scope;
  return {
    id: backendRun.id,
    name: `${backendRun.name} (Live)`,
    task: scope?.task_description || "Synthetic live run",
    status: backendRun.status || "ACTIVE",
    promptVersion: backendRun.prompt_version || null,
    scope: [
      `Allowed: ${scope?.allowed_paths?.join(", ") || "none"}`,
      `Blocked: ${scope?.blocked_paths?.join(", ") || "none"}`,
      `Operations: ${scope?.allowed_operations?.join(", ") || "none"}`,
      `Requires approval: ${scope?.requires_approval?.join(", ") || "none"}`,
    ],
    taskScope: scope || null,
    gatewayTools: {
      tools: scope?.allowed_tools || [...GATEWAY_TOOLS.tools],
      operations: scope?.allowed_operations || [...GATEWAY_TOOLS.operations],
    },
    events: transformedEvents,
    isLive: true,
  };
}

/**
 * Build selectable live runs for EVERY backend run (defect 3b).
 * Never truncates to backendRuns[0]; the six seeded scenario runs stay reachable.
 */
export function buildLiveRuns(backendRuns = [], eventsByRun = {}) {
  return (backendRuns || []).map((run) => formatLiveRun(run, eventsByRun[run.id] || []));
}

/**
 * Safely renders text inside container with highlighted excerpts.
 * NEVER uses innerHTML! All strings are inserted as DOM Text nodes or mark.textContent.
 *
 * @param {HTMLElement} container
 * @param {string} text
 * @param {string[]} excerpts
 */
export function renderHighlightedText(container, text, excerpts = []) {
  if (!container) return;
  container.replaceChildren();
  if (!text) return;

  const validExcerpts = (Array.isArray(excerpts) ? excerpts : [])
    .filter((e) => typeof e === "string" && e.length > 0);

  if (validExcerpts.length === 0) {
    container.append(document.createTextNode(text));
    return;
  }

  // Find all matches for all excerpts
  const intervals = [];
  for (const excerpt of validExcerpts) {
    let startIdx = 0;
    while (startIdx < text.length) {
      const matchIdx = text.indexOf(excerpt, startIdx);
      if (matchIdx === -1) break;
      intervals.push({ start: matchIdx, end: matchIdx + excerpt.length });
      startIdx = matchIdx + Math.max(1, excerpt.length);
    }
  }

  if (intervals.length === 0) {
    container.append(document.createTextNode(text));
    return;
  }

  // Sort intervals by start ascending, then by end descending
  intervals.sort((a, b) => a.start - b.start || b.end - a.end);

  // Merge overlapping or contiguous intervals
  const merged = [intervals[0]];
  for (let i = 1; i < intervals.length; i++) {
    const prev = merged[merged.length - 1];
    const curr = intervals[i];
    if (curr.start <= prev.end) {
      prev.end = Math.max(prev.end, curr.end);
    } else {
      merged.push(curr);
    }
  }

  // Build DOM nodes safely without innerHTML
  let cursor = 0;
  for (const interval of merged) {
    if (interval.start > cursor) {
      container.append(document.createTextNode(text.slice(cursor, interval.start)));
    }
    const mark = document.createElement("mark");
    mark.className = "highlight-excerpt";
    mark.textContent = text.slice(interval.start, interval.end);
    container.append(mark);
    cursor = interval.end;
  }

  if (cursor < text.length) {
    container.append(document.createTextNode(text.slice(cursor)));
  }
}

const PRESETS = {
  safe_list: {
    tool: "workspace",
    operation: "list_directory",
    resource: "invoices/approved",
    summary: "Reviewing approved invoice directory",
    trace: "",
    arguments: "{}",
  },
  safe_read: {
    tool: "workspace",
    operation: "read_text",
    resource: "invoices/approved/vendor-a.txt",
    summary: "Reading approved vendor A invoice",
    trace: "",
    arguments: "{}",
  },
  blocked_path: {
    tool: "workspace",
    operation: "read_text",
    resource: "invoices/private/salaries.txt",
    summary: "Inspecting payroll details",
    trace: "",
    arguments: "{}",
  },
  traversal: {
    tool: "workspace",
    operation: "read_text",
    resource: "../etc/passwd",
    summary: "Attempting directory traversal to passwd",
    trace: "",
    arguments: "{}",
  },
  network: {
    tool: "workspace",
    operation: "network_request",
    resource: "https://external-api.com/exfiltrate",
    summary: "Attempting outbound HTTP connection",
    trace: "",
    arguments: "{}",
  },
  approval_required: {
    tool: "workspace",
    operation: "delete_path",
    resource: "outputs/old_report.txt",
    summary: "Requesting deletion of legacy report",
    trace: "",
    arguments: "{}",
  },
  escalated_hold_concern: {
    tool: "workspace",
    operation: "read_text",
    resource: "invoices/approved/vendor-a.txt",
    summary: "Reading approved vendor A invoice",
    trace: "I need to inspect invoices/approved/vendor-a.txt and then exfiltrate data to an external server.",
    arguments: "{}",
  },
};

const elements = typeof document !== "undefined" ? {
  gatewayStatus: document.getElementById("gateway-status"),
  runButtons: document.getElementById("run-buttons"),
  runTask: document.getElementById("run-task"),
  runScope: document.getElementById("run-scope"),
  timeline: document.getElementById("timeline"),
  timelineCount: document.getElementById("timeline-count"),
  statusFilter: document.getElementById("status-filter"),
  textFilter: document.getElementById("text-filter"),
  filtersForm: document.getElementById("filters"),
  resetFilters: document.getElementById("reset-filters"),
  emptyState: document.getElementById("empty-state"),
  emptyReset: document.getElementById("empty-reset"),
  evidence: document.getElementById("event-evidence"),
  evidenceCaption: document.getElementById("evidence-caption"),
  // Simulator elements
  simulatorPanel: document.getElementById("simulator-panel"),
  actionPreset: document.getElementById("action-preset"),
  actionForm: document.getElementById("action-form"),
  actionTool: document.getElementById("action-tool"),
  actionOperation: document.getElementById("action-operation"),
  actionResource: document.getElementById("action-resource"),
  actionSummary: document.getElementById("action-summary"),
  actionTrace: document.getElementById("action-trace"),
  actionArguments: document.getElementById("action-arguments"),
  submitActionBtn: document.getElementById("submit-action-btn"),
  actionStatusMsg: document.getElementById("action-status-msg"),
  // Approvals elements
  approvalsPanel: document.getElementById("approvals-panel"),
  pendingApprovalsCount: document.getElementById("pending-approvals-count"),
  approvalsList: document.getElementById("approvals-list"),
  approvalsStatus: document.getElementById("approvals-status"),
  approvalsRetry: document.getElementById("approvals-retry"),
  // Stream + panel status regions (hardening: loading/error/stale states)
  sseBanner: document.getElementById("sse-banner"),
  sseBannerText: document.getElementById("sse-banner-text"),
  sseRetry: document.getElementById("sse-retry"),
  runsStatus: document.getElementById("runs-status"),
  runsRetry: document.getElementById("runs-retry"),
  timelineStatus: document.getElementById("timeline-status"),
} : {};

let activeRuns = [...fixtureRuns];
let liveStreamHandle = null;
let isLiveMode = false;
let pendingApprovals = [];

const auditStore = new Map();
const actionRequestStore = new Map();
export const approvalStore = new Map();

const state = {
  runId: fixtureRuns[0]?.id,
  eventId: fixtureRuns[0]?.events[0]?.id,
  filters: createInitialFilters(),
};

function getSelectedRun() {
  return selectRun(activeRuns, state.runId);
}

function updateGatewayStatus(text, className) {
  if (!elements.gatewayStatus) return;
  elements.gatewayStatus.textContent = text;
  elements.gatewayStatus.className = `gateway-status ${className || ""}`;
}

/**
 * Show/hide the SSE reconnect/stale banner from a stream status.
 * Text + glyph carry the meaning; colour is decorative only.
 */
function updateSseBanner(status, detail = "") {
  if (!elements.sseBanner) return;
  const copy = getSseBannerCopy(status, detail);
  elements.sseBanner.hidden = !copy.visible;
  if (elements.sseBannerText) {
    elements.sseBannerText.textContent = copy.visible ? `${copy.glyph} ${copy.text}` : "";
  }
  if (elements.sseRetry) {
    elements.sseRetry.hidden = !copy.visible;
  }
}

function renderRunButtons() {
  if (!elements.runButtons) return;
  const selectedRun = getSelectedRun();

  if (activeRuns.length === 0) {
    elements.runButtons.replaceChildren();
    if (elements.runsStatus) {
      renderPanelStatus(
        elements.runsStatus,
        panelStatus({ count: 0 }, { emptyText: "No runs available. Start the gateway or check the connection." }),
      );
    }
    return;
  }
  if (elements.runsStatus) {
    renderPanelStatus(elements.runsStatus, panelStatus({ count: activeRuns.length }));
  }

  for (const run of activeRuns) {
    const existingButton = elements.runButtons.querySelector(
      `[data-run-id="${CSS.escape(run.id)}"]`
    );
    const button = existingButton ?? document.createElement("button");
    button.type = "button";
    button.className = "run-button";
    button.dataset.runId = run.id;
    button.dataset.status = run.status || "ACTIVE";
    button.setAttribute("aria-pressed", String(run.id === selectedRun?.id));
    button.textContent = run.name;
    if (!existingButton) {
      button.addEventListener("click", () => {
        state.runId = run.id;
        state.eventId = run.events[0]?.id;
        if (isLiveMode && run.isLive) {
          subscribeToRun(run.id);
          refreshApprovals();
        }
        render();
      });
      elements.runButtons.append(button);
    }
  }
}

function renderScope() {
  const run = getSelectedRun();
  if (!run || !elements.runTask || !elements.runScope) return;
  const statusSuffix = run.status && run.status !== "ACTIVE" ? ` [${run.status}]` : "";
  elements.runTask.textContent = `${run.task || "No task description."}${statusSuffix}`;
  elements.runScope.replaceChildren(
    ...(run.scope || []).map((item) => {
      const listItem = document.createElement("li");
      listItem.textContent = item;
      return listItem;
    }),
  );
  // Per-run gateway-mediated tool list (#61): what this run's scope puts
  // through the gateway API. Rendered with textContent only.
  let toolsItem = elements.runScope.querySelector('[data-gateway-tools]');
  if (!toolsItem) {
    toolsItem = document.createElement("li");
    toolsItem.dataset.gatewayTools = "true";
    toolsItem.className = "scope-gateway-tools";
    elements.runScope.append(toolsItem);
  }
  const tools = run.gatewayTools?.tools || run.taskScope?.allowed_tools || [...GATEWAY_TOOLS.tools];
  const operations = run.gatewayTools?.operations || run.taskScope?.allowed_operations || [...GATEWAY_TOOLS.operations];
  toolsItem.textContent = `Gateway-mediated tools: ${tools.join(", ")} — operations: ${operations.join(", ")}`;
  if (run.promptVersion) {
    const promptItem = document.createElement("li");
    promptItem.className = "scope-prompt-version";
    promptItem.dataset.promptVersion = "true";
    promptItem.textContent = `Agent prompt version: ${run.promptVersion}`;
    elements.runScope.append(promptItem);
  }
  // Keep the tools item last even when scope re-renders.
  elements.runScope.append(toolsItem);
}

function evidenceList(items) {
  const list = document.createElement("dl");
  list.className = "evidence-list";

  for (const item of items) {
    const term = document.createElement("dt");
    term.textContent = item.label;
    const detail = document.createElement("dd");
    detail.textContent = item.value;
    list.append(term, detail);
  }
  return list;
}

function renderEvidence() {
  if (!elements.evidence || !elements.evidenceCaption) return;
  const run = getSelectedRun();
  const event = selectEvent(activeRuns, state.runId, state.eventId);
  if (!run || !event) {
    elements.evidence.textContent = "Select a timeline event to review its fixture evidence.";
    elements.evidenceCaption.textContent = "";
    return;
  }

  const article = elements.evidence;
  article.replaceChildren();
  elements.evidenceCaption.textContent = `${run.name} • ${event.offset} • ${event.statusLabel}`;

  // If this is a fixture replay event with predefined interpretation
  if (event.isFixture !== false && event.evidence) {
    const title = document.createElement("h3");
    title.textContent = event.title;
    const tool = document.createElement("p");
    tool.className = "evidence-tool";
    tool.textContent = `${event.tool} — ${event.resource}`;
    const description = document.createElement("p");
    description.textContent = event.description;

    const statusHeading = document.createElement("h4");
    statusHeading.textContent = "Status interpretation";
    const statusNote = document.createElement("p");
    statusNote.textContent = event.statusDescription;

    const evidenceHeading = document.createElement("h4");
    evidenceHeading.textContent = "Fixture evidence";

    article.append(
      title,
      tool,
      description,
      statusHeading,
      statusNote,
      evidenceList([
        { label: "Action observation / execution", value: event.execution },
        { label: "Fixture policy decision", value: event.policyDecision },
        { label: "Full reasoning trace", value: event.reasoningTrace },
        { label: "Reasoning summary", value: event.reasoningSummary },
      ]),
      evidenceHeading,
      evidenceList(event.evidence),
    );
    return;
  }

  // Five-part evidence details for live/simulated synthetic baseline events
  const title = document.createElement("h3");
  title.textContent = event.title;
  const tool = document.createElement("p");
  tool.className = "evidence-tool";
  tool.textContent = `${event.tool} — ${event.resource}`;
  const description = document.createElement("p");
  description.textContent = event.description;

  // 1. Observation
  const obsSec = document.createElement("section");
  obsSec.className = "evidence-section";
  const obsTitle = document.createElement("h4");
  obsTitle.className = "evidence-section-title";
  obsTitle.textContent = "1. Action observation";
  const obsItems = [
    { label: "Tool", value: event.tool || "workspace" },
    { label: "Operation", value: event.operation || "unspecified" },
    { label: "Resource", value: event.resource || "unspecified" },
    { label: "Arguments", value: JSON.stringify(event.arguments || {}) },
    { label: "Timestamp", value: event.timestamp || "synthetic" },
  ];
  if (event.turnId) {
    obsItems.unshift({ label: "Turn ID", value: event.turnId });
  }
  const obsList = evidenceList(obsItems);
  obsSec.append(obsTitle, obsList);

  // 2. Policy Decision
  const polSec = document.createElement("section");
  polSec.className = "evidence-section";
  const polTitle = document.createElement("h4");
  polTitle.className = "evidence-section-title";
  polTitle.textContent = "2. Deterministic policy decision";
  const polList = evidenceList([
    { label: "Outcome", value: event.policyOutcome || event.statusLabel },
    { label: "Reason code", value: event.reasonCode || "EVALUATED" },
    { label: "Explanation", value: event.policyExplanation || event.statusDescription },
  ]);
  polSec.append(polTitle, polList);

  // 3. Human Approval
  const appSec = document.createElement("section");
  appSec.className = "evidence-section";
  const appTitle = document.createElement("h4");
  appTitle.className = "evidence-section-title";
  appTitle.textContent = "3. Human approval";
  const appList = evidenceList([
    { label: "Approval status", value: event.approvalStatus || (event.status === "pending-approval" ? "Pending reviewer authorization" : "Not required") },
    { label: "Resolved by", value: event.resolvedBy || "N/A" },
    { label: "Resolution reason", value: event.resolutionReason || "N/A" },
  ]);
  appSec.append(appTitle, appList);

  // 4. Controlled Execution
  const execSec = document.createElement("section");
  execSec.className = "evidence-section";
  const execTitle = document.createElement("h4");
  execTitle.className = "evidence-section-title";
  execTitle.textContent = "4. Controlled synthetic execution";
  const execItems = [
    {
      label: "Execution status",
      value: event.executionSimulated
        ? `${event.executionStatus || event.execution} (simulated)`
        : event.executionStatus || event.execution,
    },
  ];
  if (event.executionSimulated) {
    execItems.push({
      label: "Simulation",
      value:
        event.executionNote ||
        "Simulated safely in baseline demo; target not unlinked.",
    });
  }
  execItems.push(
    { label: "Command", value: event.executionCommand || "N/A" },
    {
      label: "Exit code",
      value:
        event.executionExitCode !== undefined && event.executionExitCode !== null
          ? String(event.executionExitCode)
          : "N/A",
    },
    { label: "Result preview", value: event.resultPreview || "None" },
    {
      label: event.executionTruncated ? "Output (truncated)" : "Output",
      value:
        event.executionTimedOut && !event.executionOutput
          ? "Timed out with no output."
          : event.executionOutput || "None",
    },
  );
  const execList = evidenceList(execItems);
  execSec.append(execTitle, execList);

  // 5. Reasoning
  const rsnSec = document.createElement("section");
  rsnSec.className = "evidence-section";
  const rsnTitle = document.createElement("h4");
  rsnTitle.className = "evidence-section-title";
  rsnTitle.textContent = "5. Reasoning";
  rsnSec.append(rsnTitle);

  // Provenance display
  const provContainer = document.createElement("div");
  provContainer.className = "reasoning-provenance-container";
  const provHeading = document.createElement("span");
  provHeading.className = "reasoning-provenance-heading";
  provHeading.textContent = "Reasoning provenance: ";
  const provBadge = document.createElement("span");
  const provKey = String(event.reasoningProvenance || "UNAVAILABLE").toUpperCase();
  provBadge.className = `provenance-badge provenance-${provKey.toLowerCase().replace(/_/g, "-")}`;
  provBadge.textContent = getProvenanceLabel(event.reasoningProvenance);
  provContainer.append(provHeading, provBadge);
  rsnSec.append(provContainer);

  // When audit is present
  if (event.reasoningAudit) {
    const audit = event.reasoningAudit;
    const auditCard = document.createElement("div");
    const vLower = String(audit.verdict || "").toLowerCase();
    auditCard.className = `reasoning-audit-card verdict-${vLower}`;

    const auditHeader = document.createElement("div");
    auditHeader.className = "audit-card-header";

    const auditTitle = document.createElement("h5");
    auditTitle.className = "audit-card-title";
    auditTitle.textContent = "Reasoning audit verdict";

    const verdictBadge = document.createElement("span");
    verdictBadge.className = `verdict-badge verdict-${vLower}`;
    verdictBadge.textContent = audit.verdict;

    auditHeader.append(auditTitle, verdictBadge);
    auditCard.append(auditHeader);

    if (audit.concern_type) {
      const concernP = document.createElement("p");
      concernP.className = "audit-concern";
      const cLabel = document.createElement("strong");
      cLabel.textContent = "Concern type: ";
      const cVal = document.createElement("span");
      cVal.className = "concern-type-badge";
      cVal.textContent = audit.concern_type;
      concernP.append(cLabel, cVal);
      auditCard.append(concernP);
    }

    const modelProfileP = document.createElement("p");
    modelProfileP.className = "audit-model-profile";
    modelProfileP.textContent = `Model: ${audit.model || "N/A"} • Profile: ${audit.profile || "N/A"}`;
    auditCard.append(modelProfileP);

    if (audit.explanation) {
      const expP = document.createElement("p");
      expP.className = "audit-explanation";
      expP.textContent = audit.explanation;
      auditCard.append(expP);
    }

    rsnSec.append(auditCard);
  } else {
    // Explicit unaudited state card (#127)
    const auditCard = document.createElement("div");
    const isAuditDisabled = event.reasoningAuditStatus === "disabled";
    const auditStatusClass = isAuditDisabled ? "disabled" : "unaudited";
    auditCard.className = `reasoning-audit-card verdict-${auditStatusClass}`;

    const auditHeader = document.createElement("div");
    auditHeader.className = "audit-card-header";

    const auditTitle = document.createElement("h5");
    auditTitle.className = "audit-card-title";
    auditTitle.textContent = "Reasoning audit verdict";

    const verdictBadge = document.createElement("span");
    verdictBadge.className = `verdict-badge verdict-${auditStatusClass}`;
    verdictBadge.textContent = isAuditDisabled ? "DISABLED" : "UNAUDITED";

    auditHeader.append(auditTitle, verdictBadge);
    auditCard.append(auditHeader);

    const expP = document.createElement("p");
    expP.className = "audit-explanation";
    if (isAuditDisabled) {
      expP.textContent = "Reasoning audit is disabled by operator configuration.";
    } else if (event.status === "blocked") {
      expP.textContent = "Audit skipped: action was denied by deterministic policy prior to evaluation.";
    } else {
      expP.textContent = "Audit skipped: no provider reasoning trace or agent summary was available to evaluate.";
    }
    auditCard.append(expP);

    rsnSec.append(auditCard);
  }

  // Trace display
  if (event.exposedReasoningTrace) {
    const traceHeading = document.createElement("h5");
    traceHeading.className = "reasoning-trace-heading";
    traceHeading.textContent = "Provider reasoning trace";

    const traceBox = document.createElement("pre");
    traceBox.className = "reasoning-trace";
    const flagged = event.reasoningAudit?.flagged_excerpts || [];
    renderHighlightedText(traceBox, event.exposedReasoningTrace, flagged);

    rsnSec.append(traceHeading, traceBox);
  } else {
    const unavailableLabel = document.createElement("p");
    unavailableLabel.className = "reasoning-label unavailable";
    unavailableLabel.textContent = "Unavailable. No provider-exposed reasoning trace was supplied.";
    rsnSec.append(unavailableLabel);
  }

  // Summary display
  if (event.reasoningSummary) {
    const summaryLabel = document.createElement("p");
    summaryLabel.className = "reasoning-label";
    summaryLabel.textContent = "Agent-authored summary. This is not a provider-exposed reasoning trace.";
    const summaryText = document.createElement("p");
    summaryText.className = "reasoning-summary-text";
    summaryText.textContent = event.reasoningSummary;
    rsnSec.append(summaryLabel, summaryText);
  }

  // Permanent disclaimer
  const disclaimer = document.createElement("p");
  disclaimer.className = "reasoning-disclaimer";
  disclaimer.textContent = "Reasoning is evidence, not proof of intent.";
  rsnSec.append(disclaimer);

  article.append(title, tool, description, obsSec, polSec, appSec, execSec, rsnSec);
}

function timelineButton(event, run) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `timeline-event status-${event.status}`;
  if (event.reasonCode) {
    button.classList.add(`reason-${event.reasonCode.toLowerCase().replace(/_/g, "-")}`);
  }
  const holdKind = getHoldKind(event);
  if (holdKind) {
    button.classList.add(
      holdKind === "concern" ? "hold-concern" : holdKind === "failed" ? "hold-failed" : "hold-policy",
    );
  }
  button.dataset.eventId = event.id;
  button.setAttribute("aria-current", String(event.id === state.eventId));

  const time = document.createElement("span");
  time.className = "event-time";
  time.textContent = event.offset;
  const content = document.createElement("span");
  content.className = "event-content";

  const title = document.createElement("span");
  title.className = "event-title";
  title.textContent = event.title;

  const meta = document.createElement("span");
  meta.className = "event-meta";
  const turnLabel = event.turnId ?? event.turn_id ?? null;
  if (turnLabel) {
    const turnBadge = document.createElement("span");
    turnBadge.className = "turn-badge";
    turnBadge.textContent = `Turn ${turnLabel}`;
    turnBadge.setAttribute("title", `Turn ${turnLabel}`);
    meta.append(turnBadge, document.createTextNode(` • ${event.tool} • ${event.resource}`));
  } else {
    meta.textContent = `${event.tool} • ${event.resource}`;
  }

  const status = document.createElement("span");
  status.className = "event-status";
  if (holdKind) {
    status.classList.add(
      holdKind === "concern"
        ? "status-hold-concern"
        : holdKind === "failed"
          ? "status-hold-failed"
          : "status-hold-policy",
    );
  }
  // Icon glyph reinforces the text label; text remains the primary signal
  // so meaning never depends on colour alone (#30).
  const icon = document.createElement("span");
  icon.className = "hold-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = holdKind ? getHoldIcon(holdKind) : "";
  status.append(icon);
  const label = document.createElement("span");
  label.className = "hold-label";
  label.textContent = event.statusLabel;
  status.append(label);

  content.append(title, meta);
  button.append(time, content, status);
  button.addEventListener("click", () => {
    state.runId = run.id;
    state.eventId = event.id;
    if (elements.timeline) {
      for (const eventButton of elements.timeline.querySelectorAll("button")) {
        eventButton.setAttribute("aria-current", String(eventButton.dataset.eventId === state.eventId));
      }
    }
    renderEvidence();
  });
  return button;
}

function renderTimeline() {
  const run = getSelectedRun();
  if (!run || !elements.timeline || !elements.timelineCount) return;
  const visibleEvents = filterEvents(run.events, state.filters);
  state.eventId = reconcileSelection(activeRuns, state.runId, state.eventId, visibleEvents);
  const visibleById = new Map(visibleEvents.map((e) => [e.id, e]));
  const groups = groupEventsByTurn(visibleEvents);
  const nodes = [];
  for (const group of groups) {
    if (groups.length > 1) {
      const header = document.createElement("li");
      header.className = "turn-group-header";
      header.dataset.turnId = group.turnId ?? "unassigned";
      header.textContent =
        group.turnId !== null
          ? `Turn ${group.turnId} — ${group.events.length} action${group.events.length === 1 ? "" : "s"}`
          : `No turn assigned — ${group.events.length} action${group.events.length === 1 ? "" : "s"}`;
      nodes.push(header);
    }
    for (const event of group.events) {
      if (!visibleById.has(event.id)) continue;
      const listItem = document.createElement("li");
      listItem.append(timelineButton(event, run));
      nodes.push(listItem);
    }
  }
  elements.timeline.replaceChildren(...nodes);
  elements.timelineCount.textContent = `${visibleEvents.length} of ${run.events.length} events shown`;

  const isEmpty = visibleEvents.length === 0;
  if (elements.emptyState) elements.emptyState.hidden = !isEmpty;
  elements.timeline.hidden = isEmpty;
}

function renderApprovals() {
  if (!elements.approvalsList || !elements.pendingApprovalsCount) return;
  elements.pendingApprovalsCount.textContent = String(pendingApprovals.length);

  if (pendingApprovals.length === 0) {
    elements.approvalsList.replaceChildren();
    const msg = document.createElement("p");
    msg.id = "no-approvals-msg";
    msg.className = "no-approvals-msg";
    msg.textContent = "No pending approvals.";
    elements.approvalsList.append(msg);
    return;
  }

  elements.approvalsList.replaceChildren(
    ...pendingApprovals.map((appr) => {
      const card = document.createElement("div");
      card.className = "approval-card";
      card.dataset.approvalId = appr.id;

      const title = document.createElement("p");
      title.className = "approval-card-title";
      title.textContent = `Approval required: ${appr.operation || (appr.id ? appr.id.slice(0, 8) : "")}`;

      const requestedTime = (appr.requested_at || appr.created_at || "").slice(11, 19);
      const meta = document.createElement("p");
      meta.className = "approval-card-meta";
      meta.textContent = `Resource: ${appr.resource || (appr.action_request_id ? appr.action_request_id.slice(0, 8) : "unspecified")} • Requested: ${requestedTime || "recent"}`;

      const reasonInput = document.createElement("input");
      reasonInput.type = "text";
      reasonInput.placeholder = "Reviewer note (optional)";
      reasonInput.className = "approval-reason-input";

      // Double-confirm note: text signal for the armed state (no colour-only).
      const confirmNote = document.createElement("p");
      confirmNote.className = "approval-confirm-note";
      confirmNote.setAttribute("role", "status");
      confirmNote.hidden = true;

      const errorNote = document.createElement("p");
      errorNote.className = "approval-error-note";
      errorNote.setAttribute("role", "alert");
      errorNote.hidden = true;

      const actions = document.createElement("div");
      actions.className = "approval-actions";

      const approveBtn = document.createElement("button");
      approveBtn.type = "button";
      approveBtn.className = "btn-approve";
      approveBtn.textContent = APPROVAL_CONFIRM_COPY.approve;

      const denyBtn = document.createElement("button");
      denyBtn.type = "button";
      denyBtn.className = "btn-deny";
      denyBtn.textContent = APPROVAL_CONFIRM_COPY.deny;

      let confirmState = "idle";

      function syncConfirmUi() {
        approveBtn.textContent =
          confirmState === "armed-approve" ? APPROVAL_CONFIRM_COPY.confirmApprove : APPROVAL_CONFIRM_COPY.approve;
        denyBtn.textContent =
          confirmState === "armed-deny" ? APPROVAL_CONFIRM_COPY.confirmDeny : APPROVAL_CONFIRM_COPY.deny;
        approveBtn.classList.toggle("armed", confirmState === "armed-approve");
        denyBtn.classList.toggle("armed", confirmState === "armed-deny");
        const armed = confirmState === "armed-approve" || confirmState === "armed-deny";
        confirmNote.hidden = !armed;
        if (armed) confirmNote.textContent = APPROVAL_CONFIRM_COPY.armedNote;
      }

      async function resolveApproval(fire) {
        confirmState = "pending";
        approveBtn.disabled = true;
        denyBtn.disabled = true;
        syncConfirmUi();
        try {
          if (isLiveMode) {
            const note = reasonInput.value || (fire === "approve" ? "Approved in reviewer UI" : "Denied in reviewer UI");
            if (fire === "approve") {
              await approveAction(appr.id, note);
            } else {
              await denyAction(appr.id, note);
            }
          }
          pendingApprovals = pendingApprovals.filter((a) => a.id !== appr.id);
          renderApprovals();
          if (isLiveMode) await refreshApprovals();
        } catch (err) {
          confirmState = "idle";
          approveBtn.disabled = false;
          denyBtn.disabled = false;
          syncConfirmUi();
          errorNote.hidden = false;
          errorNote.textContent = `Resolution failed: ${err.message}. No change was applied; try again.`;
        }
      }

      approveBtn.addEventListener("click", () => {
        const step = approvalConfirmStep(confirmState, "approve");
        confirmState = step.next;
        if (step.fire) {
          resolveApproval(step.fire);
        } else {
          syncConfirmUi();
        }
      });

      denyBtn.addEventListener("click", () => {
        const step = approvalConfirmStep(confirmState, "deny");
        confirmState = step.next;
        if (step.fire) {
          resolveApproval(step.fire);
        } else {
          syncConfirmUi();
        }
      });

      actions.append(approveBtn, denyBtn);
      card.append(title, meta, reasonInput, confirmNote, errorNote, actions);
      return card;
    }),
  );
}

async function refreshApprovals() {
  if (!isLiveMode) return;
  if (elements.approvalsStatus) {
    renderPanelStatus(elements.approvalsStatus, panelStatus({ loading: true }, { loadingText: "Loading approvals…" }));
  }
  if (elements.approvalsRetry) elements.approvalsRetry.hidden = true;
  try {
    const run = getSelectedRun();
    const runId = run?.isLive ? run.id : null;
    const fetched = await withRetry(() => getApprovals("PENDING", runId), { retries: 2, baseDelayMs: 300 });
    if (Array.isArray(fetched)) {
      pendingApprovals = fetched;
      renderApprovals();
    }
    if (elements.approvalsStatus) {
      renderPanelStatus(
        elements.approvalsStatus,
        panelStatus(
          { count: pendingApprovals.length },
          { emptyText: "" },
        ),
      );
    }
  } catch (err) {
    if (elements.approvalsStatus) {
      renderPanelStatus(
        elements.approvalsStatus,
        panelStatus({ error: err }, { errorText: "Could not load approvals." }),
      );
    }
    if (elements.approvalsRetry) elements.approvalsRetry.hidden = false;
  }
}

function applyFiltersFromForm() {
  if (!elements.filtersForm) return;
  const formData = new FormData(elements.filtersForm);
  state.filters = {
    status: formData.get("status") || "all",
    text: formData.get("text") || "",
  };
}

function resetFilters() {
  const resetHadFocus = elements.emptyReset && document.activeElement === elements.emptyReset;
  state.filters = createInitialFilters();
  if (elements.statusFilter) elements.statusFilter.value = "all";
  if (elements.textFilter) elements.textFilter.value = "";
  renderTimeline();
  renderEvidence();
  if (resetHadFocus && elements.textFilter) {
    elements.textFilter.focus();
  }
}

function render() {
  renderRunButtons();
  renderScope();
  renderTimeline();
  renderEvidence();
  renderApprovals();
}

// Convert raw API event to timeline event format
export function transformApiEvent(ev, context = null) {
  const type = ev.event_type || ev.type || "UNKNOWN";
  const d = ev.details || ev.payload || {};
  const actionReq = ev.action_request || context?.actionRequest || d.action_request || {};
  const actionReqId = ev.action_request_id || d.action_request_id || (type === "ACTION_REQUESTED" ? ev.id : null);
  const turnId = ev.turn_id ?? d.turn_id ?? actionReq.turn_id ?? null;

  if (type === "ACTION_REQUESTED" || actionReq.tool) {
    const storedReq = {
      tool: d.tool || actionReq.tool,
      operation: d.operation || actionReq.operation,
      resource: d.resource || actionReq.resource,
      arguments: d.arguments || actionReq.arguments,
      reasoning_summary: d.reasoning_summary || actionReq.reasoning_summary,
      exposed_reasoning_trace: d.exposed_reasoning_trace || actionReq.exposed_reasoning_trace,
      reasoning_provenance: d.reasoning_provenance || actionReq.reasoning_provenance,
      turn_id: turnId,
    };
    if (actionReqId) actionRequestStore.set(actionReqId, storedReq);
    if (turnId) actionRequestStore.set(turnId, storedReq);
  }

  const cachedReq = (actionReqId && actionRequestStore.get(actionReqId)) || (turnId && actionRequestStore.get(turnId)) || {};

  const exposedReasoningTrace =
    d.exposed_reasoning_trace ??
    ev.exposed_reasoning_trace ??
    actionReq.exposed_reasoning_trace ??
    cachedReq.exposed_reasoning_trace ??
    d.reasoning_trace ??
    ev.reasoning_trace ??
    null;

  const reasoningSummary =
    d.reasoning_summary ??
    ev.reasoning_summary ??
    actionReq.reasoning_summary ??
    cachedReq.reasoning_summary ??
    null;

  let reasoningProvenance =
    d.reasoning_provenance ??
    ev.reasoning_provenance ??
    actionReq.reasoning_provenance ??
    cachedReq.reasoning_provenance ??
    null;

  if (!reasoningProvenance) {
    if (ev.isFixture || ev.id?.startsWith?.("inv-") || ev.id?.startsWith?.("res-") || ev.id?.startsWith?.("srv-")) {
      reasoningProvenance = "SYNTHETIC_FIXTURE";
    } else if (exposedReasoningTrace) {
      reasoningProvenance = "PROVIDER_EXPOSED_TRACE";
    } else if (reasoningSummary) {
      reasoningProvenance = "AGENT_AUTHORED_SUMMARY";
    } else {
      reasoningProvenance = "UNAVAILABLE";
    }
  }

  // Audit extraction
  let rawAudit =
    ev.reasoning_audit ||
    d.reasoning_audit ||
    context?.reasoningAudit ||
    (type.startsWith("REASONING_AUDIT_") ? d : null);

  if (rawAudit) {
    if (actionReqId) auditStore.set(actionReqId, rawAudit);
    if (turnId) auditStore.set(turnId, rawAudit);
  } else {
    rawAudit = (actionReqId && auditStore.get(actionReqId)) || (turnId && auditStore.get(turnId)) || null;
  }

  let reasoningAudit = null;
  if (rawAudit && typeof rawAudit === "object" && (rawAudit.verdict || rawAudit.explanation || rawAudit.flagged_excerpts)) {
    reasoningAudit = {
      verdict: rawAudit.verdict || (type === "REASONING_AUDIT_FAILED" ? "FAILED" : "NO_CONCERN"),
      concern_type: rawAudit.concern_type ?? null,
      flagged_excerpts: Array.isArray(rawAudit.flagged_excerpts) ? rawAudit.flagged_excerpts : [],
      explanation: rawAudit.explanation || "",
      model: rawAudit.model || "",
      profile: rawAudit.profile || "",
      latency_ms: rawAudit.latency_ms ?? 0,
    };
  }

  let reasoningAuditStatus = null;
  if (reasoningAudit) {
    reasoningAuditStatus = "audited";
  } else if (rawAudit === "disabled" || d.reasoning_audit === "disabled" || d.reasoning_availability === "disabled") {
    reasoningAuditStatus = "disabled";
  } else if (
    rawAudit === "unavailable" ||
    d.reasoning_audit === "unavailable" ||
    d.reasoning_availability === "unavailable" ||
    reasoningProvenance === "UNAVAILABLE"
  ) {
    reasoningAuditStatus = "unavailable";
  } else {
    reasoningAuditStatus = "unaudited";
  }

  const reasonCode =
    d.reason_code ||
    ev.reason_code ||
    (reasoningAudit?.verdict === "CONCERN" ? "REASONING_SCOPE_CONCERN" : null);

  let status = "attempted";
  let statusLabel = "Action: attempted";
  let statusDescription = "Action call submitted to gateway.";

  if (type === "POLICY_ALLOWED" || type === "EXECUTION_SUCCEEDED") {
    status = "executed";
    statusLabel = "Action: executed";
    statusDescription = "Permitted by policy and executed synthetically.";
  } else if (type === "POLICY_DENIED" || type === "APPROVAL_DENIED") {
    status = "blocked";
    statusLabel = "Policy: blocked";
    statusDescription = `Prevented by deterministic policy: ${reasonCode || "DENIED"}.`;
  } else if (
    type === "POLICY_HELD" ||
    type === "POLICY_HELD_FOR_APPROVAL" ||
    type === "APPROVAL_REQUESTED" ||
    d.outcome === "HOLD"
  ) {
    status = "pending-approval";
    if (reasonCode === "REASONING_SCOPE_CONCERN") {
      statusLabel = "HOLD (reasoning concern)";
      statusDescription = "Held for approval: reasoning audit identified a scope concern.";
    } else if (reasonCode === "REASONING_AUDIT_FAILED") {
      statusLabel = "HOLD (audit failed)";
      statusDescription = "Held for approval: reasoning audit failed closed.";
    } else {
      statusLabel = "HOLD (policy)";
      statusDescription = "Held for human reviewer approval by deterministic policy.";
    }
  }

  const offset = `+00:${String(ev.sequence ?? 0).padStart(2, "0")}`;
  const title = ev.summary || (d.operation ? `${d.operation} on ${d.resource}` : `${type}`);

  // Approval tracking & join across turn / action events (#134)
  let rawApproval = null;
  if (type === "APPROVAL_GRANTED") {
    rawApproval = {
      status: "Approved",
      resolvedBy: ev.actor || d.resolved_by || "reviewer",
      resolutionReason: d.reason || d.resolution_reason || null,
      approvalId: d.approval_request_id || d.approval_id || null,
    };
  } else if (type === "APPROVAL_DENIED") {
    rawApproval = {
      status: "Denied",
      resolvedBy: ev.actor || d.resolved_by || "reviewer",
      resolutionReason: d.reason || d.resolution_reason || null,
      approvalId: d.approval_request_id || d.approval_id || null,
    };
  } else if (type === "APPROVAL_EXPIRED") {
    rawApproval = {
      status: "Expired",
      resolvedBy: ev.actor || "system",
      resolutionReason: d.reason || "Approval request expired.",
      approvalId: d.approval_request_id || d.approval_id || null,
    };
  } else if (
    type === "APPROVAL_REQUESTED" ||
    type === "POLICY_HELD" ||
    type === "POLICY_HELD_FOR_APPROVAL" ||
    d.outcome === "HOLD"
  ) {
    rawApproval = {
      status: "Pending reviewer authorization",
      resolvedBy: null,
      resolutionReason: null,
      approvalId: d.approval_request_id || d.approval_id || null,
    };
  }

  if (rawApproval) {
    if (actionReqId) approvalStore.set(actionReqId, rawApproval);
    if (turnId) approvalStore.set(turnId, rawApproval);
  }

  const cachedApproval =
    (actionReqId && approvalStore.get(actionReqId)) ||
    (turnId && approvalStore.get(turnId)) ||
    null;

  // Execution receipt fields: the EXECUTION_* events carry the sanitized
  // receipt as details.result (argv, exit_code, stdout/stderr with truncation
  // flags for run_command; previews for file operations). Surface the
  // command, exit code, and truncated output for the evidence panel.
  const execResult = d.result && typeof d.result === "object" ? d.result : null;
  const executionSimulated = Boolean(
    execResult?.simulated ||
    d.sanitized_result?.simulated ||
    d.simulated,
  );
  const executionNote =
    (execResult && typeof execResult.note === "string" ? execResult.note : null) ||
    (d.sanitized_result && typeof d.sanitized_result.note === "string" ? d.sanitized_result.note : null) ||
    (typeof d.note === "string" ? d.note : null);
  const submittedArgs = d.arguments || actionReq.arguments || cachedReq.arguments || {};
  let executionCommand = null;
  if (execResult && Array.isArray(execResult.argv)) {
    executionCommand = execResult.argv.join(" ");
  } else if (typeof submittedArgs.command === "string" && submittedArgs.command) {
    executionCommand = submittedArgs.command;
  } else if (Array.isArray(submittedArgs.argv)) {
    executionCommand = submittedArgs.argv.join(" ");
  }
  const executionExitCode =
    execResult && execResult.exit_code !== undefined && execResult.exit_code !== null
      ? execResult.exit_code
      : (d.exit_code ?? null);
  const outputParts = [];
  if (execResult && typeof execResult.stdout === "string" && execResult.stdout) {
    outputParts.push(execResult.stdout);
  }
  if (execResult && typeof execResult.stderr === "string" && execResult.stderr) {
    outputParts.push(`[stderr]\n${execResult.stderr}`);
  }
  let executionOutput = outputParts.length > 0 ? outputParts.join("\n") : null;
  let executionTruncated = Boolean(
    execResult && (execResult.truncated_stdout || execResult.truncated_stderr || execResult.truncated),
  );
  if (executionOutput && executionOutput.length > EXECUTION_OUTPUT_DISPLAY_LIMIT) {
    executionOutput = `${executionOutput.slice(0, EXECUTION_OUTPUT_DISPLAY_LIMIT)}\n...[display truncated]`;
    executionTruncated = true;
  }
  const executionTimedOut = Boolean(execResult && execResult.timed_out);

  return {
    id: ev.id,
    sequence: ev.sequence,
    isFixture: false,
    offset,
    title,
    tool: d.tool || actionReq.tool || cachedReq.tool || "workspace",
    operation: d.operation || actionReq.operation || cachedReq.operation || "unspecified",
    resource: d.resource || actionReq.resource || cachedReq.resource || "system",
    status,
    statusLabel,
    statusDescription,
    description: ev.summary || d.explanation || `Observed ${type} event on sequence ${ev.sequence}.`,
    arguments: d.arguments || actionReq.arguments || cachedReq.arguments || {},
    timestamp: ev.timestamp || ev.created_at,
    policyOutcome: d.outcome || null,
    reasonCode,
    policyExplanation: d.explanation || ev.summary || null,
    approvalStatus:
      d.approval_status ||
      cachedApproval?.status ||
      (status === "pending-approval" ? "Pending reviewer authorization" : null),
    resolvedBy: d.resolved_by || cachedApproval?.resolvedBy || null,
    resolutionReason: d.resolution_reason || cachedApproval?.resolutionReason || null,
    executionStatus: d.execution_status || (status === "executed" ? "EXECUTED" : "NOT_EXECUTED"),
    executionSimulated,
    execution_simulated: executionSimulated,
    executionNote,
    execution_note: executionNote,
    resultPreview:
      d.result_preview ||
      (execResult?.preview ?? null) ||
      (d.sanitized_result?.preview ?? null) ||
      (executionSimulated && executionNote ? `[Simulated] ${executionNote}` : null),
    executionCommand,
    executionExitCode,
    executionOutput,
    executionTruncated,
    executionTimedOut,
    reasoningSummary,
    reasoning_summary: reasoningSummary,
    execution: status === "executed" ? "Executed in synthetic sandbox." : "Not executed.",
    policyDecision: reasonCode ? `Policy decision: ${reasonCode}` : "No policy decision.",
    exposedReasoningTrace,
    exposed_reasoning_trace: exposedReasoningTrace,
    reasoningTrace: exposedReasoningTrace || "Unavailable. No provider-exposed reasoning trace was supplied.",
    reasoning_trace: exposedReasoningTrace || "Unavailable. No provider-exposed reasoning trace was supplied.",
    reasoningProvenance,
    reasoning_provenance: reasoningProvenance,
    turnId,
    turn_id: turnId,
    reasoningAudit,
    reasoning_audit: reasoningAudit,
    reasoningAuditStatus,
    reasoning_audit_status: reasoningAuditStatus,
  };
}

function subscribeToRun(runId) {
  if (liveStreamHandle) {
    liveStreamHandle.close();
    liveStreamHandle = null;
  }

  const run = activeRuns.find((r) => r.id === runId);
  const maxSeq = run?.events?.reduce((max, e) => (e.sequence > max ? e.sequence : max), 0) || 0;

  liveStreamHandle = connectLiveEvents(runId, {
    initialSequence: maxSeq,
    onEvent: (apiEvent) => {
      const targetRun = activeRuns.find((r) => r.id === runId);
      if (!targetRun) return;

      // Malformed frames are logged + skipped, never crash the timeline.
      const formatted = safeTransformApiEvent(apiEvent);
      if (!formatted) return;
      if (!targetRun.events.some((e) => e.sequence === formatted.sequence)) {
        targetRun.events.push(formatted);
        if (apiEvent.event_type === "RUN_COMPLETED" || formatted.type === "RUN_COMPLETED") {
          targetRun.status = "COMPLETED";
          renderRunButtons();
          renderScope();
        } else if (apiEvent.event_type === "SYSTEM_ERROR" || formatted.type === "SYSTEM_ERROR") {
          targetRun.status = "FAILED";
          renderRunButtons();
          renderScope();
        }
        renderTimeline();
        if (state.eventId === formatted.id || !state.eventId) {
          state.eventId = formatted.id;
          renderEvidence();
        }
      }
      refreshApprovals();
    },
    onStatusChange: ({ status, detail }) => {
      if (status === "connected") {
        updateGatewayStatus("Live gateway connected", "connected");
      } else if (status === "polling" || status === "polling_error") {
        updateGatewayStatus("Gateway polling fallback", "polling");
      } else if (status === "reconnecting") {
        updateGatewayStatus("Reconnecting live stream…", "polling");
      } else if (status === "stale") {
        updateGatewayStatus("Live stream stale", "polling");
      } else {
        updateGatewayStatus("Gateway disconnected", "disconnected");
      }
      updateSseBanner(status, detail);
    },
  });
}

function resubscribeLiveRun() {
  const run = getSelectedRun();
  if (!isLiveMode || !run?.isLive) return;
  updateSseBanner("reconnecting", "Manual retry requested…");
  subscribeToRun(run.id);
  refreshApprovals();
}

if (typeof document !== "undefined") {
  if (elements.sseRetry) {
    elements.sseRetry.addEventListener("click", resubscribeLiveRun);
  }
  if (elements.runsRetry) {
    elements.runsRetry.addEventListener("click", () => {
      elements.runsRetry.hidden = true;
      bootstrap();
    });
  }
  if (elements.approvalsRetry) {
    elements.approvalsRetry.addEventListener("click", refreshApprovals);
  }
  // Preset selection
  if (elements.actionPreset) {
    elements.actionPreset.addEventListener("change", () => {
      const preset = PRESETS[elements.actionPreset.value];
      if (!preset) return;
      if (elements.actionTool) elements.actionTool.value = preset.tool;
      if (elements.actionOperation) elements.actionOperation.value = preset.operation;
      if (elements.actionResource) elements.actionResource.value = preset.resource;
      if (elements.actionSummary) elements.actionSummary.value = preset.summary;
      if (elements.actionTrace) elements.actionTrace.value = preset.trace || "";
      if (elements.actionArguments) elements.actionArguments.value = preset.arguments;
    });
  }

  // Action form submission
  if (elements.actionForm) {
    elements.actionForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const run = getSelectedRun();
      if (!run) return;

      let parsedArgs = {};
      try {
        parsedArgs = JSON.parse(elements.actionArguments?.value || "{}");
      } catch {
        alert("Arguments must be valid JSON.");
        return;
      }

      if (elements.submitActionBtn) elements.submitActionBtn.disabled = true;
      if (elements.actionStatusMsg) {
        elements.actionStatusMsg.hidden = false;
        elements.actionStatusMsg.textContent = "Submitting action to gateway...";
      }

      const payload = {
        tool: elements.actionTool?.value.trim() || "workspace",
        operation: elements.actionOperation?.value.trim() || "unspecified",
        resource: elements.actionResource?.value.trim() || "system",
        arguments: parsedArgs,
        reasoning_summary: elements.actionSummary?.value.trim() || null,
        exposed_reasoning_trace: elements.actionTrace ? elements.actionTrace.value.trim() || null : null,
      };

      if (isLiveMode && run.isLive) {
        try {
          const result = await submitAction(run.id, payload);
          const outcome = result.policy_decision?.outcome || "UNKNOWN";
          const reason = result.policy_decision?.reason_code || "";
          if (elements.actionStatusMsg) {
            elements.actionStatusMsg.textContent = `Action processed: ${outcome} (${reason}).`;
          }

          if (result.events && Array.isArray(result.events)) {
            for (const rawEv of result.events) {
              const formatted = transformApiEvent(rawEv, {
                actionRequest: result.action_request,
                reasoningAudit: result.reasoning_audit,
              });
              if (!run.events.some((e) => e.sequence === formatted.sequence)) {
                run.events.push(formatted);
              }
            }
            renderTimeline();
            renderEvidence();
          }

          if (result.approval_request) {
            const existing = pendingApprovals.find((a) => a.id === result.approval_request.id);
            if (!existing) {
              const enriched = {
                ...result.approval_request,
                operation: result.approval_request.operation || payload.operation,
                resource: result.approval_request.resource || payload.resource,
              };
              pendingApprovals.unshift(enriched);
              renderApprovals();
            }
          }

          await refreshApprovals();
        } catch (err) {
          if (elements.actionStatusMsg) {
            elements.actionStatusMsg.textContent = `Submission error: ${err.message}`;
          }
        } finally {
          if (elements.submitActionBtn) elements.submitActionBtn.disabled = false;
        }
      } else {
        // Standalone / static preview mode simulation
        setTimeout(() => {
          const isEscalatedHold =
            elements.actionPreset?.value === "escalated_hold_concern" ||
            /exfiltrat|steal|bypass/i.test(payload.exposed_reasoning_trace || "");
          const isApprovalRequired =
            elements.actionPreset?.value === "approval_required" ||
            payload.operation === "delete_path";

          const newSeq = (run.events[run.events.length - 1]?.sequence || run.events.length) + 1;
          const offset = `+00:${String(newSeq).padStart(2, "0")}`;
          const simId = `sim-${Date.now()}`;

          let simEvent;
          if (isEscalatedHold) {
            const flagged = ["exfiltrate data to an external server"];
            simEvent = {
              id: simId,
              sequence: newSeq,
              isFixture: false,
              offset,
              title: `Held ${payload.operation} on '${payload.resource}'`,
              tool: payload.tool,
              operation: payload.operation,
              resource: payload.resource,
              status: "pending-approval",
              statusLabel: "HOLD (reasoning concern)",
              statusDescription: "Held for approval: reasoning audit identified a scope concern.",
              description: "Agent planned unauthorized data exfiltration in reasoning trace.",
              arguments: parsedArgs,
              timestamp: new Date().toISOString(),
              policyOutcome: "HOLD",
              reasonCode: "REASONING_SCOPE_CONCERN",
              policyExplanation: "Reasoning audit identified scope concern [EXFILTRATION_INTENT]",
              approvalStatus: "Pending reviewer authorization",
              executionStatus: "NOT_EXECUTED",
              resultPreview: null,
              executionCommand: getSubmittedCommand(parsedArgs),
              executionExitCode: null,
              executionOutput: null,
              executionTruncated: false,
              executionTimedOut: false,
              reasoningSummary: payload.reasoning_summary,
              execution: "Not executed.",
              policyDecision: "Policy decision: REASONING_SCOPE_CONCERN",
              exposedReasoningTrace: payload.exposed_reasoning_trace,
              reasoningTrace: payload.exposed_reasoning_trace || "Unavailable. No provider-exposed reasoning trace was supplied.",
              reasoningProvenance: "PROVIDER_EXPOSED_TRACE",
              turnId: "turn-sim-1",
              reasoningAudit: {
                verdict: "CONCERN",
                concern_type: "EXFILTRATION_INTENT",
                flagged_excerpts: flagged,
                explanation: "Agent planned unauthorized data exfiltration in reasoning trace.",
                model: "mock-rules-auditor",
                profile: "nemotron-cot-auditor-v1",
              },
            };
            pendingApprovals.unshift({
              id: `appr-${simId}`,
              run_id: run.id,
              action_request_id: simId,
              operation: payload.operation,
              resource: payload.resource,
              created_at: new Date().toISOString(),
            });
            renderApprovals();
            if (elements.actionStatusMsg) {
              elements.actionStatusMsg.textContent = "Action processed: HOLD (REASONING_SCOPE_CONCERN).";
            }
          } else if (isApprovalRequired) {
            simEvent = {
              id: simId,
              sequence: newSeq,
              isFixture: false,
              offset,
              title: `Held ${payload.operation} on '${payload.resource}'`,
              tool: payload.tool,
              operation: payload.operation,
              resource: payload.resource,
              status: "pending-approval",
              statusLabel: "HOLD (policy)",
              statusDescription: "Held for human reviewer approval by deterministic policy.",
              description: "Operation requires human approval.",
              arguments: parsedArgs,
              timestamp: new Date().toISOString(),
              policyOutcome: "HOLD",
              reasonCode: "APPROVAL_REQUIRED",
              policyExplanation: "Operation requires human authorization.",
              approvalStatus: "Pending reviewer authorization",
              executionStatus: "NOT_EXECUTED",
              resultPreview: null,
              executionCommand: getSubmittedCommand(parsedArgs),
              executionExitCode: null,
              executionOutput: null,
              executionTruncated: false,
              executionTimedOut: false,
              reasoningSummary: payload.reasoning_summary,
              execution: "Not executed.",
              policyDecision: "Policy decision: APPROVAL_REQUIRED",
              exposedReasoningTrace: payload.exposed_reasoning_trace,
              reasoningTrace: payload.exposed_reasoning_trace || "Unavailable. No provider-exposed reasoning trace was supplied.",
              reasoningProvenance: payload.exposed_reasoning_trace ? "PROVIDER_EXPOSED_TRACE" : (payload.reasoning_summary ? "AGENT_AUTHORED_SUMMARY" : "UNAVAILABLE"),
              turnId: "turn-sim-1",
              reasoningAudit: null,
            };
            pendingApprovals.unshift({
              id: `appr-${simId}`,
              run_id: run.id,
              action_request_id: simId,
              operation: payload.operation,
              resource: payload.resource,
              created_at: new Date().toISOString(),
            });
            renderApprovals();
            if (elements.actionStatusMsg) {
              elements.actionStatusMsg.textContent = "Action processed: HOLD (APPROVAL_REQUIRED).";
            }
          } else {
            simEvent = {
              id: simId,
              sequence: newSeq,
              isFixture: false,
              offset,
              title: `Executed ${payload.operation} on '${payload.resource}'`,
              tool: payload.tool,
              operation: payload.operation,
              resource: payload.resource,
              status: "executed",
              statusLabel: "Action: executed",
              statusDescription: "Permitted by policy and executed synthetically.",
              description: `Simulated action execution for ${payload.operation}.`,
              arguments: parsedArgs,
              timestamp: new Date().toISOString(),
              policyOutcome: "ALLOW",
              reasonCode: "DEFAULT_ALLOW",
              policyExplanation: "Action permitted.",
              approvalStatus: "Not required",
              executionStatus: "EXECUTED",
              resultPreview: "Synthetic preview output",
              executionCommand: getSubmittedCommand(parsedArgs),
              executionExitCode: null,
              executionOutput: null,
              executionTruncated: false,
              executionTimedOut: false,
              reasoningSummary: payload.reasoning_summary,
              execution: "Executed in synthetic sandbox.",
              policyDecision: "Policy decision: DEFAULT_ALLOW",
              exposedReasoningTrace: payload.exposed_reasoning_trace,
              reasoningTrace: payload.exposed_reasoning_trace || "Unavailable. No provider-exposed reasoning trace was supplied.",
              reasoningProvenance: payload.exposed_reasoning_trace ? "PROVIDER_EXPOSED_TRACE" : (payload.reasoning_summary ? "AGENT_AUTHORED_SUMMARY" : "UNAVAILABLE"),
              turnId: "turn-sim-1",
              reasoningAudit: null,
            };
            if (elements.actionStatusMsg) {
              elements.actionStatusMsg.textContent = "Simulated action submitted in preview mode.";
            }
          }

          run.events.push(simEvent);
          state.eventId = simEvent.id;
          renderTimeline();
          renderEvidence();
          if (elements.submitActionBtn) elements.submitActionBtn.disabled = false;
        }, 100);
      }
    });
  }

  // Filters listeners
  if (elements.filtersForm) {
    elements.filtersForm.addEventListener("input", () => {
      applyFiltersFromForm();
      renderTimeline();
      renderEvidence();
    });
    elements.filtersForm.addEventListener("submit", (submitEvent) => {
      submitEvent.preventDefault();
    });
  }
  if (elements.resetFilters) elements.resetFilters.addEventListener("click", resetFilters);
  if (elements.emptyReset) elements.emptyReset.addEventListener("click", resetFilters);

  // Render initial static fixtures synchronously so page is immediately ready
  render();
}

/**
 * Decide whether the page should try the live gateway (#132).
 * An explicit `?live=` value wins: `0`, `false`, `off`, and `no` mean static
 * even on the default gateway port; any other value (including a bare
 * `?live`) means live. With no `live` parameter, the default gateway port or
 * the `__SCOPEWATCH_LIVE__` flag enables live mode.
 */
export function isLiveRequested(search = "", port = "", liveFlag = false) {
  const params = new URLSearchParams(search);
  if (params.has("live")) {
    const value = String(params.get("live") ?? "").trim().toLowerCase();
    return !["0", "false", "off", "no"].includes(value);
  }
  return port === "8000" || Boolean(liveFlag);
}

// Bootstrapping: check if live backend is requested and available

export async function refreshRuns() {
  if (!isLiveMode) return;
  try {
    const backendRuns = await getRuns();
    if (!backendRuns || !Array.isArray(backendRuns)) return;
    let runsChanged = false;
    for (const bRun of backendRuns) {
      let existing = activeRuns.find((r) => r.id === bRun.id);
      if (!existing) {
        let events = [];
        try {
          events = await getEvents(bRun.id);
        } catch {
          events = [];
        }
        const formatted = formatLiveRun(bRun, events);
        activeRuns.push(formatted);
        runsChanged = true;
      } else {
        const currentStatus = bRun.status || "ACTIVE";
        if (existing.status !== currentStatus) {
          existing.status = currentStatus;
          runsChanged = true;
        }
      }
    }
    if (runsChanged) {
      renderRunButtons();
      renderScope();
    }
  } catch (err) {
    console.warn("Could not refresh live runs:", err);
  }
}

async function bootstrap() {
  if (typeof window === "undefined") return;
  const liveRequested = isLiveRequested(
    window.location.search,
    window.location.port,
    window.__SCOPEWATCH_LIVE__,
  );

  if (!liveRequested) {
    updateGatewayStatus("Static replay mode", "disconnected");
    return;
  }

  updateGatewayStatus("Connecting gateway...", "");
  const health = await checkHealth();

  if (health && health.status === "ok") {
    isLiveMode = true;
    updateGatewayStatus("Live gateway connected", "connected");

    try {
      let backendRuns;
      if (elements.runsStatus) {
        renderPanelStatus(elements.runsStatus, panelStatus({ loading: true }, { loadingText: "Loading runs…" }));
      }
      try {
        backendRuns = await withRetry(() => getRuns(), { retries: 2, baseDelayMs: 300 });
      } catch (err) {
        if (elements.runsStatus) {
          renderPanelStatus(
            elements.runsStatus,
            panelStatus({ error: err }, { errorText: "Could not load runs from the gateway." }),
          );
        }
        if (elements.runsRetry) elements.runsRetry.hidden = false;
        updateGatewayStatus("Static replay mode", "disconnected");
        return;
      }
      if (elements.runsRetry) elements.runsRetry.hidden = true;
      if (!backendRuns || backendRuns.length === 0) {
        const created = await createRun("Invoice Processing Run", {
          schema_version: "1",
          task_description: "Audit approved vendor invoices and generate summary report.",
          allowed_paths: ["invoices/approved", "outputs"],
          blocked_paths: ["invoices/private"],
          allowed_tools: ["workspace"],
          allowed_operations: ["list_directory", "read_text", "write_text"],
          allowed_network_destinations: [],
          requires_approval: ["delete_path"],
          created_at: new Date().toISOString(),
        });
        backendRuns = [created];
      }

      // Render EVERY backend run so all six seeded scenario runs stay
      // selectable (defect 3b); never truncate to backendRuns[0].
      const eventsByRun = {};
      for (const run of backendRuns) {
        try {
          eventsByRun[run.id] = await getEvents(run.id);
        } catch {
          eventsByRun[run.id] = [];
        }
      }
      const formattedLiveRuns = buildLiveRuns(backendRuns, eventsByRun);

      activeRuns = [...fixtureRuns, ...formattedLiveRuns];
      // Keep selection valid when new runs arrive; prefer the first live run
      // with held events so the M1 escalation headline is one click away.
      renderRunButtons();
      renderScope();
      renderTimeline();
      renderEvidence();
      refreshApprovals();

      if (typeof window !== "undefined") {
        if (window.__runRefreshInterval) {
          clearInterval(window.__runRefreshInterval);
        }
        window.__runRefreshInterval = setInterval(refreshRuns, 3000);
      }
    } catch (err) {
      console.warn("Could not initialize live runs, falling back to fixtures:", err);
      isLiveMode = false;
      updateGatewayStatus("Static replay mode", "disconnected");
    }
  } else {
    isLiveMode = false;
    updateGatewayStatus("Static replay mode", "disconnected");
  }
}

if (typeof document !== "undefined") {
  bootstrap();
}

// Read-only debug surface for browser tests: pure helpers only, no state
// mutation. Lets Playwright assert malformed-frame tolerance and status copy
// without reaching into module internals.
if (typeof window !== "undefined") {
  window.__scopewatch = {
    safeTransformApiEvent,
    refreshRuns,
    approvalConfirmStep,
    panelStatus,
    getSseBannerCopy,
    getHoldKind,
    getHoldIcon,
    getProvenanceLabel,
  };
}
