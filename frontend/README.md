# Synthetic replay reviewer

A dependency-free vanilla HTML, CSS, and JavaScript dashboard for reviewing three
invented agent runs. This is an interface prototype, not an auditor or enforcement system.
No live APIs, agents, credentials, private traces, external assets, or approval actions
are connected. Run selection and filters only change in-memory display state.

## Run locally

Requires Node.js 20 or newer for tests and Python 3 for the static server. No npm install
or build is needed to serve the UI or run unit tests. Browser tests have separate development dependencies.

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

Run the same isolated Chromium smoke test used in CI:

```sh
npm ci
npx playwright install chromium
npm run test:browser
```

On Linux, browser system libraries may require `npx playwright install --with-deps chromium`.
Installation downloads development tools; the test itself serves only committed synthetic
fixtures on an ephemeral loopback port and blocks non-local page requests. It closes the
server and browser on success or failure. Tests cover every fixture event, keyboard focus,
intersecting filters, empty evidence, reset, run isolation, and light/dark layouts at
320, 390, 768, and 1440px. Screen-reader behavior, zoom, and non-Chromium browsers remain
unverified. Unit tests alone do not verify rendering.
