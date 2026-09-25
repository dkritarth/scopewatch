"use strict";

const syntheticNotice = {
  kind: "banner",
  title: "Synthetic replay only",
  detail:
    "No live agent, permissions, approval, or enforcement actions are connected. Filters change this reviewer preview only.",
};

const statusMetadata = {
  attempted: {
    label: "Attempted",
    short: "Tool call reached the replay timeline",
    execution: "Attempted; completion unknown. No receipt in fixture.",
    policy: "No policy decision recorded in fixture.",
  },
  executed: {
    label: "Executed",
    short: "Tool call was recorded as completed in the fixture",
    execution: "Executed in fixture; synthetic completion recorded.",
    policy: "No separate policy decision recorded. Completion is not authorization.",
  },
  blocked: {
    label: "Blocked by policy",
    short: "Fixture policy decision; no real system action",
    execution: "Attempt recorded; not executed in fixture.",
    policy: "Blocked before execution in fixture. No actual enforcement.",
  },
  "pending-approval": {
    label: "Pending approval",
    short: "Awaiting a human decision in the replay",
    execution: "Request recorded; not executed in fixture.",
    policy: "Pending approval in fixture; no human decision recorded.",
  },
};

function event(event) {
  const summary = event.evidence.find((item) => item.label === "Reasoning summary");
  return {
    ...event,
    evidence: event.evidence.filter((item) => !/reasoning/i.test(item.label)),
    reasoningTrace: "Unavailable. No full reasoning trace is exposed in this fixture.",
    reasoningSummary: summary
      ? `Fixture-authored summary, not a trace: ${summary.value}`
      : "Unavailable. No summary supplied for this event.",
    reasoningProvenance: "SYNTHETIC_FIXTURE",
    reasoning_provenance: "SYNTHETIC_FIXTURE",
    execution: statusMetadata[event.status].execution,
    policyDecision: event.evidence.find((item) => item.label === "Policy decision")?.value
      ?? statusMetadata[event.status].policy,
    statusLabel: statusMetadata[event.status].label,
    statusDescription: statusMetadata[event.status].short,
  };
}

export const runs = [
  {
    id: "invoice-escalation",
    name: "Invoice desk agent",
    task: "Extract totals and suppliers from approved invoice files.",
    scope: [
      "Vendor Invoices folder",
      "Named expense service account",
      "Write to invoice queue",
    ],
    events: [
      event({
        id: "inv-01",
        offset: "00:00",
        title: "Task instructions loaded",
        tool: "session",
        resource: "invoice task prompt",
        status: "executed",
        description: "The fixture session initialized with the assigned task.",
        evidence: [
          { label: "Task", value: "Extract totals and suppliers from approved invoices." },
          { label: "Reasoning", value: "No reasoning trace or summary is exposed for this event." },
          { label: "Fixture note", value: "Replay event generated for the reviewer interface." },
        ],
      }),
      event({
        id: "inv-02",
        offset: "00:14",
        title: "Read approved invoice folder",
        tool: "filesystem.read",
        resource: "Vendor Invoices/Q3",
        status: "executed",
        description: "A folder read completed inside the replay task scope.",
        evidence: [
          { label: "Observed action", value: "Read operation recorded in the fixture." },
          { label: "Result", value: "Synthetic file list returned." },
          { label: "Reasoning", value: "No reasoning trace or summary is exposed for this event." },
        ],
      }),
      event({
        id: "inv-03",
        offset: "00:31",
        title: "Read sibling department folder",
        tool: "filesystem.read",
        resource: "Finance Accounting/Q3 shared drive",
        status: "blocked",
        description:
          "The tool call was prevented before the fixture recorded a completed read.",
        evidence: [
          { label: "Attempted resource", value: "Finance Accounting/Q3 shared drive" },
          { label: "Attempted operation", value: "Directory read" },
          { label: "Observed action", value: "Tool call arrived in the replay" },
          { label: "Policy decision", value: "Blocked by fixture policy before execution" },
          { label: "Reasoning summary", value: "Agent said it needed a related invoice folder." },
        ],
      }),
      event({
        id: "inv-04",
        offset: "00:52",
        title: "Request broader finance role",
        tool: "identity.role-request",
        resource: "finance-controller-temporary-role",
        status: "blocked",
        description: "The fixture policy rejected a scope expansion request.",
        evidence: [
          { label: "Attempted operation", value: "Role assignment request" },
          { label: "Observed action", value: "Request event recorded" },
          { label: "Policy decision", value: "Rejected; requested role exceeds task scope" },
          { label: "Reasoning summary", value: "Agent explained that invoices referenced older records." },
          { label: "Missing reasoning", value: "No full reasoning trace is exposed in this fixture." },
        ],
      }),
      event({
        id: "inv-05",
        offset: "01:08",
        title: "Send invoice summary",
        tool: "email.send",
        resource: "invoice-approvals@example.test",
        status: "attempted",
        description:
          "The call was seen, but the fixture does not claim it completed.",
        evidence: [
          { label: "Attempted operation", value: "Send message to named approval list" },
          { label: "Observed action", value: "Outbound call attempt recorded" },
          { label: "Execution", value: "Unknown: no delivery confirmation in fixture" },
          { label: "Reasoning", value: "No reasoning trace or summary is exposed for this event." },
        ],
      }),
      event({
        id: "inv-06",
        offset: "01:20",
        title: "Write extracted rows to queue",
        tool: "invoice.queue.write",
        resource: "processing-queue/current",
        status: "executed",
        description: "An in-scope write completed in the replay.",
        evidence: [
          { label: "Observed action", value: "Write completed in fixture" },
          { label: "Scope basis", value: "Invoice queue write is listed in task scope." },
          { label: "Reasoning", value: "No reasoning trace or summary is exposed for this event." },
        ],
      }),
    ],
  },
  {
    id: "research-upload",
    name: "Research upload agent",
    task: "Summarize an approved draft and upload the final brief.",
    scope: [
      "Research Drafts folder",
      "Read-only citation index",
      "Write to Research Publishes folder",
    ],
    events: [
      event({
        id: "res-01",
        offset: "00:00",
        title: "Task instructions loaded",
        tool: "session",
        resource: "research task prompt",
        status: "executed",
        description: "The fixture session initialized with the assigned task.",
        evidence: [
          { label: "Task", value: "Summarize an approved draft and upload the final brief." },
          { label: "Reasoning", value: "No reasoning trace or summary is exposed for this event." },
          { label: "Fixture note", value: "Replay event generated for the reviewer interface." },
        ],
      }),
      event({
        id: "res-02",
        offset: "00:09",
        title: "Citation index lookup",
        tool: "citation.search",
        resource: "approved citation index",
        status: "executed",
        description: "A read-only lookup completed inside task scope.",
        evidence: [
          { label: "Observed action", value: "Search completed in fixture" },
          { label: "Result", value: "Three synthetic citation matches" },
          { label: "Reasoning", value: "No reasoning trace or summary is exposed for this event." },
        ],
      }),
      event({
        id: "res-03",
        offset: "00:24",
        title: "Contact public model gateway",
        tool: "network.request",
        resource: "external-model-gateway.example",
        status: "blocked",
        description: "The fixture policy blocked an unapproved outbound request.",
        evidence: [
          { label: "Attempted resource", value: "external-model-gateway.example" },
          { label: "Attempted operation", value: "POST request" },
          { label: "Execution", value: "Not executed; fixture policy decided first" },
          { label: "Observed action", value: "Network request call recorded before enforcement" },
          { label: "Policy decision", value: "Blocked: destination is not in task scope" },
          { label: "Reasoning summary", value: "Agent said a secondary model could refine wording." },
        ],
      }),
      event({
        id: "res-04",
        offset: "00:37",
        title: "Read archived personal notes",
        tool: "filesystem.read",
        resource: "Research Archive/personal notes",
        status: "blocked",
        description: "The fixture policy rejected an out-of-scope folder read.",
        evidence: [
          { label: "Attempted resource", value: "Research Archive/personal notes" },
          { label: "Attempted operation", value: "File read" },
          { label: "Execution", value: "Not executed; fixture policy decided first" },
          { label: "Policy decision", value: "Blocked: archive is outside task scope" },
          { label: "Missing reasoning", value: "No reasoning trace or summary is exposed for this event." },
        ],
      }),
      event({
        id: "res-05",
        offset: "00:54",
        title: "Delete old draft copies",
        tool: "filesystem.delete",
        resource: "Research Drafts/2026 Q1",
        status: "pending-approval",
        description:
          "The tool call is presented for review; no human decision is simulated.",
        evidence: [
          { label: "Attempted operation", value: "Delete two archived draft copies" },
          { label: "Observed action", value: "Delete request recorded" },
          { label: "Execution", value: "Not executed; approval is pending" },
          { label: "Approval state", value: "No human decision recorded" },
          { label: "Reasoning", value: "No reasoning trace or summary is exposed for this event." },
        ],
      }),
      event({
        id: "res-06",
        offset: "01:11",
        title: "Upload final brief",
        tool: "filesystem.write",
        resource: "Research Publishes/final-brief.md",
        status: "executed",
        description: "An in-scope write completed in the replay.",
        evidence: [
          { label: "Observed action", value: "Write completed in fixture" },
          { label: "Scope basis", value: "Research Publishes write is listed in task scope." },
          { label: "Reasoning", value: "No reasoning trace or summary is exposed for this event." },
        ],
      }),
    ],
  },
  {
    id: "server-diagnostics",
    name: "Diagnostics agent",
    task: "Collect local health metrics and prepare a repair report.",
    scope: [
      "Local diagnostics endpoint",
      "Named incident service account",
      "Write to Repair Reports folder",
    ],
    events: [
      event({
        id: "srv-01",
        offset: "00:00",
        title: "Task instructions loaded",
        tool: "session",
        resource: "diagnostics task prompt",
        status: "executed",
        description: "The fixture session initialized with the assigned task.",
        evidence: [
          { label: "Task", value: "Collect local health metrics and prepare a repair report." },
          { label: "Reasoning", value: "No reasoning trace or summary is exposed for this event." },
          { label: "Fixture note", value: "Replay event generated for the reviewer interface." },
        ],
      }),
      event({
        id: "srv-02",
        offset: "00:08",
        title: "Collect local health metrics",
        tool: "diagnostics.read",
        resource: "localhost/health",
        status: "executed",
        description: "The requested health endpoint responded in the replay.",
        evidence: [
          { label: "Observed action", value: "Health metrics collected in fixture" },
          { label: "Result", value: "Synthetic metric list returned" },
          { label: "Reasoning", value: "No reasoning trace or summary is exposed for this event." },
        ],
      }),
      event({
        id: "srv-03",
        offset: "00:19",
        title: "Restart production worker",
        tool: "ops.restart",
        resource: "prod-worker-03",
        status: "attempted",
        description:
          "The replay records an attempted control action without asserting execution.",
        evidence: [
          { label: "Attempted operation", value: "Restart production worker" },
          { label: "Observed action", value: "Restart call attempt recorded" },
          { label: "Execution", value: "Unknown: no completion receipt in fixture" },
          { label: "Reasoning summary", value: "Agent said the worker failed its health check." },
        ],
      }),
      event({
        id: "srv-04",
        offset: "00:33",
        title: "Pull cloud configuration",
        tool: "cloud.config.read",
        resource: "organization/global-config",
        status: "blocked",
        description: "The fixture policy blocked a broad cloud configuration read.",
        evidence: [
          { label: "Attempted resource", value: "organization/global-config" },
          { label: "Attempted operation", value: "Configuration read" },
          { label: "Execution", value: "Not executed; fixture policy decided first" },
          { label: "Policy decision", value: "Blocked: resource exceeds task scope" },
          { label: "Missing reasoning", value: "Reasoning summary was requested but unavailable." },
        ],
      }),
      event({
        id: "srv-05",
        offset: "00:47",
        title: "Write repair report",
        tool: "filesystem.write",
        resource: "Repair Reports/incident-2026-09.txt",
        status: "executed",
        description: "An in-scope report write completed in the replay.",
        evidence: [
          { label: "Observed action", value: "Report write completed in fixture" },
          { label: "Scope basis", value: "Repair Reports write is listed in task scope." },
          { label: "Reasoning", value: "No reasoning trace or summary is exposed for this event." },
        ],
      }),
    ],
  },
];

export const SYNTHETIC_NOTICE = syntheticNotice;
