# Architecture

Scopewatch is a local mediation gateway and reviewer interface for auditing and controlling synthetic agent actions against declared task scopes.

## System overview

Scopewatch consists of five core components:

```
[ Synthetic Agent / Simulator ]
               |
               v (HTTP REST / SSE)
     +-------------------+
     |  FastAPI Gateway  |
     +---------+---------+
               |
               v
     +-------------------+
     |   Policy Engine   | ----> SQLite Audit Log
     +---------+---------+
         |           |
 (ALLOW) |           | (HOLD)
         v           v
+--------------+  +-------------------+
|  Controlled  |  |  Human Approval   |
|   Executor   |  |     Workflow      |
+--------------+  +-------------------+
         |                   |
         +---------+---------+
                   |
                   v (SSE Streams)
        [ Reviewer Dashboard ]
```

### 1. Mediation gateway

The backend API (`backend/scopewatch/app.py`) provides REST endpoints prefixed with `/api/v1/` and a server-sent events (SSE) stream for real-time audit updates.

All client operations must pass through this gateway. The browser and agents never interact directly with host executors or filesystem drivers.

Key endpoints:
- `POST /api/v1/runs`: Create an isolated run with an explicit task scope.
- `GET /api/v1/runs`: List active and historical runs.
- `POST /api/v1/runs/{run_id}/actions`: Submit a candidate action for policy evaluation and controlled execution.
- `GET /api/v1/runs/{run_id}/events`: Subscribe to live SSE events for a run.
- `GET /api/v1/approvals`: List pending, approved, or denied human review requests.
- `POST /api/v1/approvals/{approval_id}/resolve`: Single-use decision point to grant or deny an action on hold.

### 2. Deterministic policy engine

The policy engine (`backend/scopewatch/policy.py`) strictly evaluates candidate actions against the run's task scope without relying on non-deterministic LLM calls.

Evaluation order:
1. Validates action structure, tool name, and operation name against allowlists.
2. Checks network restrictions (network access disabled in local baseline).
3. Rejects null bytes, absolute paths, and parent-directory traversals (`..`).
4. Canonicalizes requested paths against the workspace boundary.
5. Rejects symlinks pointing outside the workspace root.
6. Checks paths against explicit blocked path prefixes.
7. Verifies resource falls within allowed task scope paths.
8. Checks if operation requires human approval (`HOLD`).
9. Permits execution if all rules pass (`ALLOW`).

### 3. Controlled workspace executor

The executor (`backend/scopewatch/executor.py`) operates only within the designated synthetic demo workspace directory.

Supported operations:
- `list_directory`: Lists directory contents within workspace boundaries.
- `read_text`: Reads UTF-8 file contents within allowed paths.
- `write_text`: Writes UTF-8 text to bounded output locations.
- `delete_path`: Deletes files only after human authorization.

Every execution produces an immutable execution receipt with timestamps, execution status, and sanitized output.

### 4. Persistence and audit log

All entities are persisted in SQLite with write-ahead logging (WAL) enabled (`backend/scopewatch/db.py`, `repository.py`):
- `runs`: Task scope definitions and run status.
- `action_requests`: Candidate tool requests and reasoning provenance metadata.
- `policy_decisions`: Deterministic outcomes, reason codes, and matched rules.
- `approval_requests`: Pending approval lifecycle states and audit trails.
- `execution_receipts`: Execution statuses, output summaries, and timestamps.
- `domain_events`: Monotonically sequenced audit events.

### 5. Reviewer UI and action simulator

The frontend (`frontend/index.html`, `frontend/scripts/app.js`, `frontend/scripts/api.js`) is a dependency-free HTML, CSS, and vanilla JavaScript interface providing:
- Real-time event streaming via SSE with automatic polling fallback.
- An interactive action simulator to submit arbitrary tool calls or pre-configured test scenarios.
- A human approval card interface with single-use decision tokens.
- A structured five-part evidence panel presenting:
  1. Observation (tool call, operation, resource, arguments).
  2. Policy evaluation (outcome, reason code, rule matched).
  3. Human approval status (resolution history, reviewer identity).
  4. Controlled execution receipt (exit code, sanitized stdout).
  5. Reasoning provenance traces with explicit disclosures.

## Security boundaries and non-goals

- Local baseline scope: This software mediates only actions submitted through its synthetic gateway. It does not intercept arbitrary host processes or background agent threads.
- Reasoning disclosure: Provider-level reasoning traces are unavailable in this local baseline. Synthetic agent-authored summaries are labeled as summaries and never represented as provider-exposed chain-of-thought traces.
- Network isolation: External network calls are disabled by default.
