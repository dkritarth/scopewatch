# Scopewatch backend

Synthetic baseline backend for Scopewatch, implementing deterministic policy evaluation, controlled synthetic execution, SQLite evidence persistence, and human-in-the-loop approvals.

## Architecture

- `scopewatch.models`: Core domain enums and constants.
- `scopewatch.schemas`: Versioned Pydantic v2 schemas for tasks, runs, actions, decisions, approvals, receipts, and events.
- `scopewatch.db`: SQLite database initialization, WAL mode, foreign keys, and transaction helpers.
- `scopewatch.repository`: Atomic CRUD, per-run monotonic event sequencing, and single-use approval consumption.
- `scopewatch.policy`: 15-step deterministic policy engine evaluating paths, tools, operations, and approval requirements without shell or external network access.
- `scopewatch.executor`: Controlled synthetic workspace executor supporting allowlisted operations (`list_directory`, `read_text`, `write_text`, and simulated `delete_path`). Direct execution without stored policy evidence fails.

## Running tests

```sh
python -m pytest backend/tests -v
python -m compileall -q backend/scopewatch backend/tests
```
