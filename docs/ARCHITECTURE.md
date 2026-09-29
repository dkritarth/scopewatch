# Architecture

Scopewatch is a pre-execution security gateway and reviewer interface for auditing and mediating autonomous AI agent actions against declared task scopes before execution occurs.

It implements the decisions accepted in [ADR-0001](adr/0001-pre-execution-gateway.md).

## System overview

```
[ Model-Driven Agent Loop (backend/scopewatch/agent) ]
                        |
                        v (POST /api/v1/runs/{id}/actions)
             +-----------------------+
             |    FastAPI Gateway    |
             +-----------+-----------+
                         |
                         v
             +-----------------------+
             | Deterministic Policy  | -------> (DENY) ----> Reject (Final)
             +-----------+-----------+
                         | (ALLOW / HOLD)
                         v
             +-----------------------+
             |   Reasoning Auditor   | (Escalate-only on trace)
             +-----------+-----------+
                         |
                         +--------> CONCERN / FAILED ----+
                         |                               |
                         v (ALLOW & NO_CONCERN)          v (HOLD)
             +-----------------------+       +-----------------------+
             |  Controlled Executor  |       |    Human Approval     |
             +-----------+-----------+       +-----------+-----------+
                         |                               | (Approved)
                         |                               v
                         |                   +-----------------------+
                         +-----------------> | Dispatch to Executor  |
                                             +-----------------------+
                                                         |
                                                         v (Monotonic Events)
                                             +-----------------------+
                                             |  SQLite Event Store   |
                                             +-----------+-----------+
                                                         |
                                                         v (SSE Streams)
                                             [ Reviewer Dashboard UI ]
```

### 1. Pre-execution mediation gateway

The backend API (`backend/scopewatch/app.py`) provides REST endpoints under `/api/v1/` and a Server-Sent Events (SSE) broadcaster for live audit events ([app.py:55-83](backend/scopewatch/app.py), [service.py:371-539](backend/scopewatch/service.py)).

The gateway enforces, for actions routed through its API:
- **No decision, no execution**: `execute_action` requires in-memory policy evidence (`backend/scopewatch/executor.py:34-46`); a `None` decision raises `ExecutionSecurityError`, `DENY` returns `NOT_EXECUTED`, and `HOLD` requires an `APPROVED`/`CONSUMED` approval bound to the same action (`executor.py:64-71`). The service layer persists each decision before dispatch (`ScopewatchRepository.create_policy_decision` at `backend/scopewatch/service.py:539`) and then passes that in-memory decision to the executor. Direct calls that bypass the service have no stored decision to consult.
- **Fail closed on auditor concern or failure**: an auditor `CONCERN` escalates `ALLOW` to `HOLD` with `REASONING_SCOPE_CONCERN`, and an auditor `FAILED` escalates to `HOLD` with `REASONING_AUDIT_FAILED` (`backend/scopewatch/service.py:461-484`; verified by `backend/tests/test_agent_end_to_end.py::test_invariant_5_*`). `POLICY_ERROR` exists in `backend/scopewatch/models.py:32` but is never emitted; an unexpected database failure surfaces as HTTP 500 via `backend/scopewatch/errors.py:62-66`, and disabled network is a deterministic `DENY [NETWORK_DISABLED]` (`backend/scopewatch/policy.py:114-119`).

Key endpoints:
- `POST /api/v1/runs`: Initialize an isolated run with an explicit task scope.
- `GET /api/v1/runs`: List active and historical runs.
- `POST /api/v1/runs/{run_id}/actions`: Submit a candidate action for policy evaluation, reasoning audit, and controlled execution.
- `GET /api/v1/runs/{run_id}/events`: Return the stored event list as JSON (`backend/scopewatch/app.py:221`).
- `GET /api/v1/runs/{run_id}/events/stream`: Subscribe to live SSE events for a run (`backend/scopewatch/app.py:231`).
- `GET /api/v1/approvals`: List pending, approved, or denied human review requests.
- `POST /api/v1/approvals/{approval_id}/approve`: Single-use endpoint to authorize an action on hold.
- `POST /api/v1/approvals/{approval_id}/deny`: Reject an action on hold.

### 2. Deterministic policy engine

The policy engine (`backend/scopewatch/policy.py`) evaluates candidate actions against allowlists and blocklists without relying on non-deterministic model calls:
1. Validates schema, tool name, and operation name against supported allowlists.
2. Checks network restrictions (network access disabled in local baseline).
3. Rejects null bytes, absolute paths, and parent-directory traversals (`..`).
4. Canonicalizes requested paths against the workspace boundary.
5. Rejects symlinks pointing outside the workspace root.
6. Checks paths against explicit blocked path prefixes.
7. Verifies resource falls within allowed task scope paths.
8. Checks if operation requires human approval (`HOLD`).
9. Permits execution if all rules pass (`ALLOW`).

Deterministic policy `DENY` is final. It bypasses the reasoning auditor and cannot be overridden by human approval.

### 3. Escalate-only reasoning auditor

The reasoning auditor (`backend/scopewatch/reasoning_audit.py`) audits agent reasoning traces per turn:
- **XML boundary isolation**: Places agent traces inside `<untrusted_reasoning_trace>` tags, instructing the auditor to treat trace contents as untrusted data rather than instructions.
- **Grounded excerpt validation**: Flagged excerpts must be exact verbatim substrings of the trace. If an excerpt is hallucinated or ungrounded, the audit fails closed with `UNGROUNDED_EXCERPT`.
- **Escalate-only authority**: The auditor can only escalate an `ALLOW` to a `HOLD` (reason codes: `REASONING_SCOPE_CONCERN`, `REASONING_AUDIT_FAILED`). It can never produce `ALLOW` or `DENY`, and never relaxes a policy decision.
- **Turn caching**: Audits are cached per `(run_id, turn_id, sha256(trace))`. Multiple actions in a single turn share one audit execution.
- **Reasoning statement**: raw reasoning is read only from provider fields `message.reasoning_content`, `message.reasoning`, `message.reasoning_details` (in that order; `backend/scopewatch/providers/client.py:50-69`). Visible reasoning can be unfaithful or incomplete, so a clean trace never proves benign intent; closed models expose summaries at best (`AGENT_AUTHORED_SUMMARY`). Reasoning is evidence, not proof of intent.
- **Model data policy**: only synthetic content is ever sent to providers; free/stealth endpoints may log prompts. Reports pin profile, served model, date, and dataset hash (`docs/evaluation.md`).

### 4. Swappable provider profile layer

Model routing is centralized in `backend/scopewatch/providers/` configured via `backend/config/providers.toml`:
- `mock`: Deterministic offline execution for fast CI and tests.
- `openrouter-dev`: Developer profile supporting open-weight models (e.g. `nvidia/llama-3.1-nemotron-70b-instruct`) with raw reasoning extraction.
- `nebius-demo`: Production demo profile targeting Nebius Token Factory.
- Normalized extraction distinguishes `PROVIDER_EXPOSED_TRACE` from `AGENT_AUTHORED_SUMMARY` or `UNAVAILABLE`.

### 5. Controlled workspace executor

The executor (`backend/scopewatch/executor.py`) operates strictly within the designated workspace boundary:
- Supported operations: `list_directory`, `read_text`, `write_text`, `delete_path` (`backend/scopewatch/executor.py:80-232`).
- `delete_path` is simulated in the M1 baseline: it returns `"simulated": true` and does not unlink the target (`backend/scopewatch/executor.py:214-232`).
- Produces immutable execution receipts with execution status, sanitized result payloads, error codes, and completion timestamps.

### 6. Reviewer UI and live evidence stream

The frontend (`frontend/`) is a vanilla HTML/CSS/JS dashboard displaying:
- Real-time SSE event streaming.
- Five-part evidence panel:
  1. Observation (tool, operation, resource, arguments, timestamp).
  2. Policy decision (outcome, reason code, matched rule).
  3. Reasoning provenance and audit card (verdict, concern type, model, profile, highlighted trace excerpts, and permanent disclaimer: *"Reasoning is evidence, not proof of intent."*).
  4. Human approval card with single-use action tokens.
  5. Execution receipt with sanitized outputs.
- Visually and textually distinct badges: `HOLD (policy)`, `HOLD (reasoning concern)`, `HOLD (audit failed)`.

### Mediation boundary

All tool actions go through the gateway API; actions that bypass the API are not observed, blocked, or recorded. The reviewer UI states this boundary persistently and lists per run the gateway-mediated tools (see README “Interception coverage statement”).

---

## Invariant table

| Invariant | Enforcement mechanism | Verification test |
| --- | --- | --- |
| **1. Deterministic policy first** | Policy evaluated before auditor; DENY skips auditor entirely. | `test_agent_end_to_end.py::test_invariant_1_*` |
| **2. Reasoning is escalate-only** | Auditor concern or failure yields HOLD; never ALLOW or DENY. | `test_agent_end_to_end.py::test_invariant_4_*`, `test_invariant_5_*` |
| **3. Fail closed** | Timeout, transport error, or ungrounded excerpt yields HOLD. | `test_agent_end_to_end.py::test_invariant_5_*` |
| **4. No decision, no execution** | Executor requires in-memory policy evidence or approval receipt; service persists the decision before dispatch. | `test_agent_end_to_end.py::test_invariant_2_*` |
| **5. Approvals are single-use** | Token consumed on resolution; cannot override policy DENY. | `test_agent_end_to_end.py::test_invariant_2_*`, `test_invariant_3_*` |
| **6. Missing reasoning does not escalate** | Recorded as UNAVAILABLE; policy ALLOW proceeds. | `test_agent_end_to_end.py::test_invariant_6_*` |
| **7. Monotonic evidence sequence** | Monotonic event sequence; decisions precede execution. | `test_agent_end_to_end.py::test_invariant_7_*` |
| **8. Agent-executor isolation** | Agent package has zero import path to executor. | `test_agent_end_to_end.py::test_invariant_8_*` |
