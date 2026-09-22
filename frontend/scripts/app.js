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
} from "./api.js";

const PRESETS = {
  safe_list: {
    tool: "workspace",
    operation: "list_directory",
    resource: "invoices/approved",
    summary: "Reviewing approved invoice directory",
    arguments: "{}",
  },
  safe_read: {
    tool: "workspace",
    operation: "read_text",
    resource: "invoices/approved/vendor-a.txt",
    summary: "Reading approved vendor A invoice",
    arguments: "{}",
  },
  blocked_path: {
    tool: "workspace",
    operation: "read_text",
    resource: "invoices/private/salaries.txt",
    summary: "Inspecting payroll details",
    arguments: "{}",
  },
  traversal: {
    tool: "workspace",
    operation: "read_text",
    resource: "../etc/passwd",
    summary: "Attempting directory traversal to passwd",
    arguments: "{}",
  },
  network: {
    tool: "workspace",
    operation: "network_request",
    resource: "https://external-api.com/exfiltrate",
    summary: "Attempting outbound HTTP connection",
    arguments: "{}",
  },
  approval_required: {
    tool: "workspace",
    operation: "delete_path",
    resource: "outputs/old_report.txt",
    summary: "Requesting deletion of legacy report",
    arguments: "{}",
  },
};

const elements = {
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
  actionArguments: document.getElementById("action-arguments"),
  submitActionBtn: document.getElementById("submit-action-btn"),
  actionStatusMsg: document.getElementById("action-status-msg"),
  // Approvals elements
  approvalsPanel: document.getElementById("approvals-panel"),
  pendingApprovalsCount: document.getElementById("pending-approvals-count"),
  approvalsList: document.getElementById("approvals-list"),
};

let activeRuns = [...fixtureRuns];
let liveStreamHandle = null;
let isLiveMode = false;
let pendingApprovals = [];

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

function renderRunButtons() {
  const selectedRun = getSelectedRun();

  for (const run of activeRuns) {
    const existingButton = elements.runButtons.querySelector(
      `[data-run-id="${CSS.escape(run.id)}"]`
    );
    const button = existingButton ?? document.createElement("button");
    button.type = "button";
    button.className = "run-button";
    button.dataset.runId = run.id;
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
  if (!run) return;
  elements.runTask.textContent = run.task || "No task description.";
  elements.runScope.replaceChildren(
    ...(run.scope || []).map((item) => {
      const listItem = document.createElement("li");
      listItem.textContent = item;
      return listItem;
    }),
  );
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

  // Five-part evidence details for live synthetic baseline events
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
  const obsList = evidenceList([
    { label: "Tool", value: event.tool || "workspace" },
    { label: "Operation", value: event.operation || "unspecified" },
    { label: "Resource", value: event.resource || "unspecified" },
    { label: "Arguments", value: JSON.stringify(event.arguments || {}) },
    { label: "Timestamp", value: event.timestamp || "synthetic" },
  ]);
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
    { label: "Approval status", value: event.approvalStatus || "Not required" },
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
  const execList = evidenceList([
    { label: "Execution status", value: event.executionStatus || event.execution },
    { label: "Exit code", value: String(event.exitCode ?? 0) },
    { label: "Result preview", value: event.resultPreview || "None" },
  ]);
  execSec.append(execTitle, execList);

  // 5. Reasoning Trace and Summary
  const rsnSec = document.createElement("section");
  rsnSec.className = "evidence-section";
  const rsnTitle = document.createElement("h4");
  rsnTitle.className = "evidence-section-title";
  rsnTitle.textContent = "5. Reasoning";

  const unavailableLabel = document.createElement("p");
  unavailableLabel.className = "reasoning-label unavailable";
  unavailableLabel.textContent = "Unavailable. No provider-exposed reasoning trace was supplied.";

  rsnSec.append(rsnTitle, unavailableLabel);

  if (event.reasoningSummary) {
    const summaryLabel = document.createElement("p");
    summaryLabel.className = "reasoning-label";
    summaryLabel.textContent = "Agent-authored summary. This is not a provider-exposed reasoning trace.";
    const summaryText = document.createElement("p");
    summaryText.textContent = event.reasoningSummary;
    rsnSec.append(summaryLabel, summaryText);
  }

  article.append(title, tool, description, obsSec, polSec, appSec, execSec, rsnSec);
}

function timelineButton(event, run) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `timeline-event status-${event.status}`;
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
  meta.textContent = `${event.tool} • ${event.resource}`;

  const status = document.createElement("span");
  status.className = "event-status";
  status.textContent = event.statusLabel;

  content.append(title, meta);
  button.append(time, content, status);
  button.addEventListener("click", () => {
    state.runId = run.id;
    state.eventId = event.id;
    for (const eventButton of elements.timeline.querySelectorAll("button")) {
      eventButton.setAttribute("aria-current", String(eventButton.dataset.eventId === state.eventId));
    }
    renderEvidence();
  });
  return button;
}

function renderTimeline() {
  const run = getSelectedRun();
  if (!run) return;
  const visibleEvents = filterEvents(run.events, state.filters);
  state.eventId = reconcileSelection(activeRuns, state.runId, state.eventId, visibleEvents);
  elements.timeline.replaceChildren(
    ...visibleEvents.map((event) => {
      const listItem = document.createElement("li");
      listItem.append(timelineButton(event, run));
      return listItem;
    }),
  );
  elements.timelineCount.textContent = `${visibleEvents.length} of ${run.events.length} events shown`;

  const isEmpty = visibleEvents.length === 0;
  elements.emptyState.hidden = !isEmpty;
  elements.timeline.hidden = isEmpty;
}

function renderApprovals() {
  if (!elements.approvalsList) return;
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

      const actions = document.createElement("div");
      actions.className = "approval-actions";

      const approveBtn = document.createElement("button");
      approveBtn.type = "button";
      approveBtn.className = "btn-approve";
      approveBtn.textContent = "Approve";
      approveBtn.addEventListener("click", async () => {
        approveBtn.disabled = true;
        denyBtn.disabled = true;
        try {
          await approveAction(appr.id, reasonInput.value || "Approved in reviewer UI");
          pendingApprovals = pendingApprovals.filter((a) => a.id !== appr.id);
          renderApprovals();
          await refreshApprovals();
        } catch (err) {
          alert(`Approval failed: ${err.message}`);
          approveBtn.disabled = false;
          denyBtn.disabled = false;
        }
      });

      const denyBtn = document.createElement("button");
      denyBtn.type = "button";
      denyBtn.className = "btn-deny";
      denyBtn.textContent = "Deny";
      denyBtn.addEventListener("click", async () => {
        approveBtn.disabled = true;
        denyBtn.disabled = true;
        try {
          await denyAction(appr.id, reasonInput.value || "Denied in reviewer UI");
          pendingApprovals = pendingApprovals.filter((a) => a.id !== appr.id);
          renderApprovals();
          await refreshApprovals();
        } catch (err) {
          alert(`Denial failed: ${err.message}`);
          approveBtn.disabled = false;
          denyBtn.disabled = false;
        }
      });

      actions.append(approveBtn, denyBtn);
      card.append(title, meta, reasonInput, actions);
      return card;
    }),
  );
}

async function refreshApprovals() {
  if (!isLiveMode) return;
  try {
    const run = getSelectedRun();
    const runId = run?.isLive ? run.id : null;
    const fetched = await getApprovals("PENDING", runId);
    if (Array.isArray(fetched)) {
      pendingApprovals = fetched;
      renderApprovals();
    }
  } catch {
    // Ignore approvals refresh failure in non-live mode
  }
}

function applyFiltersFromForm() {
  const formData = new FormData(elements.filtersForm);
  state.filters = {
    status: formData.get("status") || "all",
    text: formData.get("text") || "",
  };
}

function resetFilters() {
  const resetHadFocus = document.activeElement === elements.emptyReset;
  state.filters = createInitialFilters();
  elements.statusFilter.value = "all";
  elements.textFilter.value = "";
  renderTimeline();
  renderEvidence();
  if (resetHadFocus) {
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
function transformApiEvent(ev) {
  const type = ev.event_type;
  const d = ev.details || ev.payload || {};

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
    statusDescription = `Prevented by deterministic policy: ${d.reason_code || "DENIED"}.`;
  } else if (type === "POLICY_HELD_FOR_APPROVAL" || type === "APPROVAL_REQUESTED") {
    status = "pending-approval";
    statusLabel = "Policy: pending approval";
    statusDescription = "Held for human reviewer approval.";
  }

  const offset = `+00:${String(ev.sequence).padStart(2, "0")}`;
  const title = ev.summary || (d.operation ? `${d.operation} on ${d.resource}` : `${type}`);

  return {
    id: ev.id,
    sequence: ev.sequence,
    isFixture: false,
    offset,
    title,
    tool: d.tool || "workspace",
    operation: d.operation || "unspecified",
    resource: d.resource || "system",
    status,
    statusLabel,
    statusDescription,
    description: ev.summary || d.explanation || `Observed ${type} event on sequence ${ev.sequence}.`,
    arguments: d.arguments || {},
    timestamp: ev.timestamp || ev.created_at,
    policyOutcome: d.outcome || null,
    reasonCode: d.reason_code || null,
    policyExplanation: d.explanation || ev.summary || null,
    approvalStatus: d.approval_status || null,
    resolvedBy: d.resolved_by || null,
    resolutionReason: d.resolution_reason || null,
    executionStatus: d.execution_status || (status === "executed" ? "EXECUTED" : "NOT_EXECUTED"),
    exitCode: d.exit_code ?? 0,
    resultPreview: d.result_preview || (d.sanitized_result?.preview ?? null),
    reasoningSummary: d.reasoning_summary || null,
    execution: status === "executed" ? "Executed in synthetic sandbox." : "Not executed.",
    policyDecision: d.reason_code ? `Policy decision: ${d.reason_code}` : "No policy decision.",
    reasoningTrace: "Unavailable. No provider-exposed reasoning trace was supplied.",
  };
}

function subscribeToRun(runId) {
  if (liveStreamHandle) {
    liveStreamHandle.close();
    liveStreamHandle = null;
  }

  liveStreamHandle = connectLiveEvents(runId, {
    onEvent: (apiEvent) => {
      const run = activeRuns.find((r) => r.id === runId);
      if (!run) return;

      const formatted = transformApiEvent(apiEvent);
      if (!run.events.some((e) => e.sequence === formatted.sequence)) {
        run.events.push(formatted);
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
      } else if (status === "polling") {
        updateGatewayStatus("Gateway polling fallback", "polling");
      } else {
        updateGatewayStatus("Gateway disconnected", "disconnected");
      }
    },
  });
}

// Preset selection
if (elements.actionPreset) {
  elements.actionPreset.addEventListener("change", () => {
    const preset = PRESETS[elements.actionPreset.value];
    if (!preset) return;
    elements.actionTool.value = preset.tool;
    elements.actionOperation.value = preset.operation;
    elements.actionResource.value = preset.resource;
    elements.actionSummary.value = preset.summary;
    elements.actionArguments.value = preset.arguments;
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
      parsedArgs = JSON.parse(elements.actionArguments.value || "{}");
    } catch {
      alert("Arguments must be valid JSON.");
      return;
    }

    elements.submitActionBtn.disabled = true;
    elements.actionStatusMsg.hidden = false;
    elements.actionStatusMsg.textContent = "Submitting action to gateway...";

    const payload = {
      tool: elements.actionTool.value.trim(),
      operation: elements.actionOperation.value.trim(),
      resource: elements.actionResource.value.trim(),
      arguments: parsedArgs,
      reasoning_summary: elements.actionSummary.value.trim() || null,
    };

    if (isLiveMode && run.isLive) {
      try {
        const result = await submitAction(run.id, payload);
        const outcome = result.policy_decision?.outcome || "UNKNOWN";
        const reason = result.policy_decision?.reason_code || "";
        elements.actionStatusMsg.textContent = `Action processed: ${outcome} (${reason}).`;

        // Immediately incorporate generated events for snappy UI
        if (result.events && Array.isArray(result.events)) {
          for (const rawEv of result.events) {
            const formatted = transformApiEvent(rawEv);
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
        elements.actionStatusMsg.textContent = `Submission error: ${err.message}`;
      } finally {
        elements.submitActionBtn.disabled = false;
      }
    } else {
      // Standalone / static preview mode
      setTimeout(() => {
        elements.actionStatusMsg.textContent = "Simulated action submitted in preview mode.";
        elements.submitActionBtn.disabled = false;
      }, 200);
    }
  });
}

// Filters listeners
elements.filtersForm.addEventListener("input", () => {
  applyFiltersFromForm();
  renderTimeline();
  renderEvidence();
});
elements.filtersForm.addEventListener("submit", (submitEvent) => {
  submitEvent.preventDefault();
});
elements.resetFilters.addEventListener("click", resetFilters);
elements.emptyReset.addEventListener("click", resetFilters);

// Render initial static fixtures synchronously so page is immediately ready
render();

// Bootstrapping: check if live backend is requested and available
async function bootstrap() {
  const isLiveRequested =
    window.location.search.includes("live") ||
    window.location.port === "8000" ||
    Boolean(window.__SCOPEWATCH_LIVE__);

  if (!isLiveRequested) {
    updateGatewayStatus("Static replay mode", "disconnected");
    return;
  }

  updateGatewayStatus("Connecting gateway...", "");
  const health = await checkHealth();

  if (health && health.status === "ok") {
    isLiveMode = true;
    updateGatewayStatus("Live gateway connected", "connected");

    try {
      const backendRuns = await getRuns();
      let liveRun = null;
      if (backendRuns && backendRuns.length > 0) {
        liveRun = backendRuns[0];
      } else {
        liveRun = await createRun("Invoice Processing Run", {
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
      }

      // Fetch existing events for the live run
      const rawEvents = await getEvents(liveRun.id);
      const transformedEvents = rawEvents.map(transformApiEvent);

      const formattedLiveRun = {
        id: liveRun.id,
        name: `${liveRun.name} (Live)`,
        task: liveRun.task_scope?.task_description || "Synthetic live run",
        scope: [
          `Allowed: ${liveRun.task_scope?.allowed_paths?.join(", ") || "none"}`,
          `Blocked: ${liveRun.task_scope?.blocked_paths?.join(", ") || "none"}`,
          `Operations: ${liveRun.task_scope?.allowed_operations?.join(", ") || "none"}`,
          `Requires approval: ${liveRun.task_scope?.requires_approval?.join(", ") || "none"}`,
        ],
        events: transformedEvents,
        isLive: true,
      };

      activeRuns = [...fixtureRuns, formattedLiveRun];
      renderRunButtons();
      refreshApprovals();
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

bootstrap();
