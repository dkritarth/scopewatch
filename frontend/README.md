# Synthetic replay reviewer

A dependency-free vanilla HTML, CSS, and JavaScript dashboard for reviewing three
invented agent runs. This is an interface prototype, not an auditor or enforcement system.
No live APIs, agents, credentials, private traces, external assets, or approval actions
are connected. Run selection and filters only change in-memory display state.

## Run locally

Requires Node.js 20 or newer for tests and Python 3 for the static server. No npm install
or build is needed.

```sh
cd frontend
npm test
npm run serve
```

Open `http://127.0.0.1:4173`. The equivalent server command is
`python3 -m http.server 4173 --bind 127.0.0.1`. Stop with Ctrl+C. Serve over HTTP rather
than opening `index.html` directly, because the application uses ES modules.

## Review behavior

- Select one of three run buttons to see its task scope and chronological timeline.
- Filter by replay status and case-insensitive text in titles, descriptions, tools,
  resources, or status labels. Both filters apply together and persist across runs.
- Select an event for evidence. If a filter hides the selection, the first visible
  event is selected. No matches clears the evidence. Reset restores all events in
  the current run and retains a selection when it is still visible.
- Attempted and executed describe synthetic action observations. Blocked and pending
  approval describe fixture policy outcomes. Completion alone never implies permission.
- Full reasoning traces are unavailable. Some events have explicitly fixture-authored
  summaries; those are not actual exposed traces or proof of intent.
- Pending approval has no approval or execute button. No human approval is fabricated.

## Validation

`npm test` uses `node:test` for filters, selection reconciliation, reset defaults,
run isolation, and fixture provenance. CSS includes narrow-screen layouts, visible
focus indicators, reduced-motion support, and light/dark palettes. Controls use native
buttons, labels, semantic lists, a skip link, and a polite result-count status region.

Browser validation is intentionally deferred to the parent task. Check keyboard focus
on run/event selection, empty-state recovery, screen-reader announcements, 320px and
desktop layouts, zoom, and both color schemes. Unit tests do not verify rendering.
