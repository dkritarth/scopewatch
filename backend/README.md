# Scopewatch backend

Synthetic baseline backend for Scopewatch, implementing deterministic policy evaluation, controlled synthetic execution, SQLite evidence persistence, human-in-the-loop approvals, and REST/SSE APIs.

## Architecture

- `scopewatch.models`: Core domain enums and constants.
- `scopewatch.schemas`: Versioned Pydantic v2 schemas for tasks, runs, actions, decisions, approvals, receipts, and events.
- `scopewatch.db`: SQLite database initialization, WAL mode, foreign keys, and transaction helpers.
- `scopewatch.repository`: Atomic CRUD, per-run monotonic event sequencing, and single-use approval consumption.
- `scopewatch.policy`: 15-step deterministic policy engine evaluating paths, tools, operations, and approval requirements without shell or external network access.
- `scopewatch.executor`: Controlled synthetic workspace executor supporting allowlisted operations (`list_directory`, `read_text`, `write_text`, and simulated `delete_path`). Direct execution without stored policy evidence fails.
- `scopewatch.service`: Orchestrates transactions, policy evaluations, executor calls, event appending, and live event broadcasts.
- `scopewatch.events`: Pub/sub broadcaster and Server-Sent Events (SSE) formatter.
- `scopewatch.errors`: Sanitized error responses with structured JSON envelopes (`error.code`, `error.message`, `error.request_id`) preventing stack trace disclosure.
- `scopewatch.app`: FastAPI application serving `/api/v1` routes and frontend static files.

## API endpoints

- `GET /api/v1/health`: System and database health check.
- `POST /api/v1/demo/reset`: Reset demo data and database tables safely.
- `GET /api/v1/runs`: List runs.
- `POST /api/v1/runs`: Create a run with a task scope.
- `GET /api/v1/runs/{run_id}`: Retrieve a run.
- `POST /api/v1/runs/{run_id}/complete`: Mark a run as completed.
- `POST /api/v1/runs/{run_id}/actions`: Submit an action for policy evaluation and controlled execution.
- `GET /api/v1/runs/{run_id}/actions/{action_id}`: Retrieve action state and evidence.
- `GET /api/v1/approvals`: List pending or resolved approval requests.
- `POST /api/v1/approvals/{approval_id}/approve`: Approve an action and trigger single-use execution.
- `POST /api/v1/approvals/{approval_id}/deny`: Deny an action.
- `GET /api/v1/runs/{run_id}/events`: Retrieve append-only evidence events with optional sequence pagination.
- `GET /api/v1/runs/{run_id}/events/stream`: Live Server-Sent Events stream for real-time timeline updates.

## Running tests

```sh
python -m pytest backend/tests -v
python -m compileall -q backend/scopewatch backend/tests
```
