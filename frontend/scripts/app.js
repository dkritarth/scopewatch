"use strict";

import { runs } from "./fixtures.js";
import {
  createInitialFilters,
  filterEvents,
  reconcileSelection,
  selectEvent,
  selectRun,
} from "./reviewer-state.js";

const elements = {
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
};

const state = {
  runId: runs[0]?.id,
  eventId: runs[0]?.events[0]?.id,
  filters: createInitialFilters(),
};

function getSelectedRun() {
  return selectRun(runs, state.runId);
}

function renderRunButtons() {
  const selectedRun = getSelectedRun();

  for (const run of runs) {
    const existingButton = elements.runButtons.querySelector(`[data-run-id="${CSS.escape(run.id)}"]`);
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
        render();
      });
      elements.runButtons.append(button);
    }
  }
}

function renderScope() {
  const run = getSelectedRun();
  if (!run) {
    return;
  }
  elements.runTask.textContent = run.task;
  elements.runScope.replaceChildren(
    ...run.scope.map((item) => {
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
  const event = selectEvent(runs, state.runId, state.eventId);
  if (!run || !event) {
    elements.evidence.textContent = "Select a timeline event to review its fixture evidence.";
    elements.evidenceCaption.textContent = "";
    return;
  }

  const article = elements.evidence;
  article.replaceChildren();
  elements.evidenceCaption.textContent = `${run.name} • ${event.offset} • ${event.statusLabel}`;

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
  if (!run) {
    return;
  }
  const visibleEvents = filterEvents(run.events, state.filters);
  state.eventId = reconcileSelection(runs, state.runId, state.eventId, visibleEvents);
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
}

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

render();
