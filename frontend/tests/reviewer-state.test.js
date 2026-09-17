import test from "node:test";
import assert from "node:assert/strict";
import { runs } from "../scripts/fixtures.js";
import {
  createInitialFilters, filterEvents, normalizeText, reconcileSelection,
  selectEvent, selectRun, STATUS_FACETS,
} from "../scripts/reviewer-state.js";

test("initial and reset filters are independent and show all events", () => {
  const edited = createInitialFilters();
  edited.status = "blocked";
  edited.text = "finance";
  const reset = createInitialFilters();
  assert.deepEqual(reset, { status: "all", text: "" });
  assert.deepEqual(filterEvents(runs[0].events, reset), runs[0].events);
});

test("search trims whitespace and ignores case", () => {
  assert.equal(normalizeText(null), "");
  assert.equal(normalizeText("  FINANCE  "), "finance");
  const events = filterEvents(runs[0].events, { status: "all", text: "  FINANCE  " });
  assert.deepEqual(events.map((event) => event.id), ["inv-03", "inv-04"]);
});

test("each status facet filters without changing source order", () => {
  for (const status of STATUS_FACETS) {
    const events = runs.flatMap((run) => run.events);
    const filtered = filterEvents(events, { status, text: "" });
    assert.ok(filtered.length > 0);
    assert.deepEqual(filtered, events.filter((event) => event.status === status));
  }
});

test("text searches titles, descriptions, tools, resources and status labels", () => {
  for (const field of ["title", "description", "tool", "resource", "statusLabel"]) {
    const event = { id: field, status: "attempted", [field]: "Unique needle" };
    assert.deepEqual(filterEvents([event], { status: "all", text: "needle" }), [event]);
  }
});

test("status and text constraints are intersected", () => {
  const filtered = filterEvents(runs[0].events, { status: "executed", text: "finance" });
  assert.deepEqual(filtered, []);
  assert.deepEqual(filterEvents(runs[0].events, { status: "blocked", text: "identity" })
    .map((event) => event.id), ["inv-04"]);
});

test("empty and unmatched inputs yield no events", () => {
  assert.deepEqual(filterEvents([], createInitialFilters()), []);
  assert.deepEqual(filterEvents(runs[0].events, { status: "all", text: "<script>" }), []);
});

test("run selection handles exact, unknown, and empty runs", () => {
  assert.equal(selectRun(runs, runs[1].id), runs[1]);
  assert.equal(selectRun(runs, "missing"), runs[0]);
  assert.equal(selectRun([], "missing"), null);
});

test("events cannot leak between selected runs", () => {
  assert.equal(selectEvent(runs, runs[0].id, "inv-03"), runs[0].events[2]);
  assert.equal(selectEvent(runs, runs[1].id, "inv-03"), null);
  assert.equal(selectEvent([], "missing", "inv-03"), null);
});

test("selection stays visible, clears on empty, and recovers after reset", () => {
  const run = runs[0];
  const visible = filterEvents(run.events, { status: "blocked", text: "" });
  assert.equal(reconcileSelection(runs, run.id, "inv-04", visible), "inv-04");
  assert.equal(reconcileSelection(runs, run.id, "inv-01", visible), "inv-03");
  assert.equal(reconcileSelection(runs, run.id, "inv-03", []), null);
  assert.equal(reconcileSelection(runs, run.id, null, run.events), "inv-01");
  assert.equal(reconcileSelection(runs, runs[1].id, "inv-01", runs[1].events), "res-01");
});

test("all three fixtures distinguish action, policy, and missing traces", () => {
  assert.equal(runs.length, 3);
  const ids = new Set();
  for (const run of runs) {
    assert.ok(run.scope.length > 0);
    for (const event of run.events) {
      assert.ok(!ids.has(event.id));
      ids.add(event.id);
      assert.ok(STATUS_FACETS.includes(event.status));
      assert.match(event.reasoningTrace, /Unavailable/);
      assert.match(event.reasoningSummary, /Unavailable|Fixture-authored summary, not a trace/);
      assert.ok(event.policyDecision);
      assert.ok(event.execution);
      if (["blocked", "pending-approval"].includes(event.status)) {
        assert.match(event.execution, /not executed/);
      }
      if (event.status === "executed") {
        assert.match(event.policyDecision, /Completion is not authorization/);
      }
    }
  }
});
