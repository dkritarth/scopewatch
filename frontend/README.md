# Scopewatch reviewer dashboard

A dependency-free vanilla HTML, CSS, and JavaScript dashboard for reviewing
synthetic agent runs. No build step, no framework: plain ES modules served over
HTTP. It renders three invented replay runs from `scripts/fixtures.js` and, in
live mode, every run on a local synthetic gateway plus its approvals.

All fixtures and demo content are synthetic. The dashboard mediates nothing
itself; enforcement and auditing happen in the gateway (`backend/scopewatch/`).

## Run locally (static replay)

Requires Node.js 20 or newer for tests and Python 3 for the static server. No
npm install or build is needed to serve the UI or run unit tests. Browser tests
have separate development dependencies.

```sh
cd frontend
npm test
npm run serve
```

Open `http://127.0.0.1:4173`. The equivalent server command is
`python3 -m http.server 4173 --bind 127.0.0.1`. Stop with Ctrl+C. Serve over HTTP rather
than opening `index.html` directly, because the application uses ES modules.

## Run against the local gateway (live mode)

Live mode connects the same dashboard to a real synthetic gateway on
`http://127.0.0.1:8000`: all-runs view, action simulator, approvals with
double-confirm approve/deny, and live Server-Sent Events with polling fallback.

```sh
# From the repository root: seed synthetic demo data and start the gateway.
python3 scripts/seed_demo.py
PYTHONPATH=backend python3 -m uvicorn scopewatch.app:create_app --factory \
  --host 127.0.0.1 --port 8000
# Or: ./scripts/run_demo.sh   (dashboard and API on http://127.0.0.1:8000)
```

Then open `http://127.0.0.1:8000/?live=1`. Without `?live=1` (and when not
served from port 8000) the dashboard stays in static replay mode. The header
status pill shows `Live gateway connected`, `Reconnecting live stream…`,
`Gateway polling fallback`, or `Live stream stale`; reconnecting/stale states
also raise a text banner (`#sse-banner`) with a `Retry live stream` button.
Approvals resolve exactly once: the first click arms (`Confirm approve` /
`Confirm deny`), the second fires, and both buttons lock while pending.

## SSE contract

- Stream: `GET /api/v1/runs/{run_id}/events/stream` (event names are the
  backend `EventType` values; `frontend/scripts/api.js` `LIVE_EVENT_TYPES`
  tracks `backend/scopewatch/models.py` 1:1 and unit tests enforce it).
- Fallback: `GET /api/v1/runs/{run_id}/events?after_sequence=N` polled every
  2.5s when the stream is unavailable.
- Frames: JSON objects with at least `id`, `sequence`, `event_type`, and
  `details`. Malformed frames are logged (`console.warn`) and skipped; the
  timeline never crashes on them.
- Reconnect schedule: exponential backoff 1s, 2s, 4s, … capped at 30s
  (`computeReconnectDelay`); no live frame for 45s (`STALE_AFTER_MS`) raises
  the stale banner while the last known state stays visible.
- Related endpoints: `GET /api/v1/health`, `GET /api/v1/runs`,
  `GET /api/v1/runs/{id}/events`, `POST /api/v1/runs/{id}/actions`,
  `GET /api/v1/approvals?status=PENDING&run_id=…`,
  `POST /api/v1/approvals/{id}/approve|deny`. Approvals fetches retry twice
  with backoff before surfacing an inline error with a retry button.

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
- HOLD cards distinguish policy holds (`■ HOLD (policy)`), reasoning-concern
  escalations (`▲ HOLD (reasoning concern)` with audit verdict, concern type, and
  highlighted grounded excerpts), and failed-audit holds (`● HOLD (audit failed)`).
  Icon plus text carry the meaning; colour is never the only signal.
- Events group under `Turn <id>` headers; ungrouped events trail under
  `No turn assigned`.
- All untrusted text renders via `textContent` (highlighted excerpts included);
  `innerHTML` never appears in shipped scripts (enforced by a unit test).
- Runs, timeline, and approvals panels each report loading, error (with retry),
  and empty states via `role="status"` regions instead of going blank.

## Validation

`npm test` uses `node:test` for filters, selection reconciliation, reset defaults,
run isolation, fixture provenance, SSE reconnect/backoff math, malformed-frame
tolerance, approve/deny double-confirm steps, panel status copy, and XSS-safe
rendering (44 tests). CSS includes narrow-screen layouts down to 360px, visible
focus indicators, reduced-motion support, and light/dark palettes. Controls use native
buttons, labels, semantic lists, a skip link, and polite result-count status regions.

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
320, 390, 768, and 1440px, plus escalated-hold approve/deny with double-confirm,
a 360px usability pass, SSE polling-fallback banner, malformed-frame tolerance, and
real-gateway hold approve/deny with disabled-while-pending (10 browser tests).
Screen-reader behavior, zoom, and non-Chromium browsers remain
unverified. Unit tests alone do not verify rendering.

## Test commands

```sh
cd frontend
npm test              # 44 unit tests (node --test)
npm run test:browser  # 10 Playwright tests (needs: npm ci; npx playwright install chromium)
```

From the repository root, `./scripts/validate.sh --quick` runs the full
non-browser validation (backend untouched by frontend-only changes, but the
script proves it).
