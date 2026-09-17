"use strict";

export const STATUS_FACETS = [
  "attempted",
  "executed",
  "blocked",
  "pending-approval",
];

export function createInitialFilters() {
  return {
    status: "all",
    text: "",
  };
}

export function normalizeText(value) {
  return String(value ?? "").trim().toLowerCase();
}

export function eventMatchesFilters(event, filters) {
  const normalizedStatus = String(event.status ?? "").toLowerCase();
  const statusAccepted =
    filters.status === "all" || normalizedStatus === filters.status;
  if (!statusAccepted) {
    return false;
  }

  const query = normalizeText(filters.text);
  if (!query) {
    return true;
  }

  return [
    event.title,
    event.description,
    event.tool,
    event.resource,
    event.statusLabel,
  ]
    .filter(Boolean)
    .some((value) => normalizeText(value).includes(query));
}

export function filterEvents(events, filters) {
  return events.filter((event) => eventMatchesFilters(event, filters));
}

export function selectRun(runs, runId) {
  return runs.find((run) => run.id === runId) ?? runs[0] ?? null;
}

export function selectEvent(runs, runId, eventId) {
  const run = selectRun(runs, runId);
  if (!run) {
    return null;
  }
  return run.events.find((event) => event.id === eventId) ?? null;
}

export function reconcileSelection(runs, runId, eventId, visibleEvents) {
  const selectedEvent = selectEvent(runs, runId, eventId);
  if (visibleEvents.some((event) => event.id === selectedEvent?.id)) {
    return selectedEvent?.id ?? null;
  }
  return visibleEvents[0]?.id ?? null;
}
