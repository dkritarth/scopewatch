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
- **No decision, no execution**: one shared pre-dispatch gate, `authorize_dispatch` in `backend/scopewatch/dispatch_gate.py`, owns these rules: a decision is required (else `ExecutionSecurityError`), `DENY` returns `NOT_EXECUTED`, only `ALLOW`/`HOLD` authorize anything else (unknown outcomes fail closed), `HOLD` requires an `APPROVED` approval bound to the same action, and `network_request` never executes. A `CONSUMED` approval authorizes nothing, because single-use means a spent approval cannot authorize a replay. Every executor backend calls that one gate before touching Docker, a socket, or the filesystem — local (`executor.py`), Docker (`executor_docker.py`), remote (`executor_remote.py`) — and the `executor-runner` sidecar loads the same authored file (`deploy/executor-runner/runner.py::_load_dispatch_gate`, copied into its image by `deploy/executor-runner/Dockerfile`). The gate returns a *verdict*, not a receipt: each caller renders it in its own terms (gateway backends as `ExecutionSecurityError` or a `NOT_EXECUTED` receipt, the runner as an HTTP status), which is why the module is stdlib-only and accepts plain mappings as well as pydantic models. Issue #105 extracted this after #104 found four hand-written copies had drifted. Backend-specific checks stay in their backends and run only on `PROCEED`: staged-target and `run_command` revalidation (local, Docker, runner), stored-approval and run-lifecycle verification against SQLite (local, Docker), operation routing (local refuses `run_command` as Docker-only), and the runner's signed-protocol layers (signature, one-shot token with TTL, action digest, run/decision binding). Two test suites guard this: `backend/tests/test_dispatch_gate.py` unit-tests the rules at their source, and `backend/tests/test_executor_gate_consistency.py` asserts the four call sites agree end to end, including the per-backend protocol the gate does not own.
- **Dispatch authentication**: the remote gateway signs the complete dispatch with a key separate from its bearer token; the runner verifies that signature and checks that `HOLD` carries an approved request bound to the action, run, and decision. The service layer persists each decision before dispatch (`ScopewatchRepository.create_policy_decision` at `backend/scopewatch/service.py:539`), resolves approvals within an atomic transaction, and passes its active database store handle (`db_path=conn`) to `execute_action` (issue #108). This ensures the executor verifies stored approval status, confirms no receipt already exists, and validates the run lifecycle against persisted state in production as well as in direct executor calls.
- **Fail closed on auditor concern or failure**: an auditor `CONCERN` escalates `ALLOW` to `HOLD` with `REASONING_SCOPE_CONCERN`, and an auditor `FAILED` escalates to `HOLD` with `REASONING_AUDIT_FAILED` (`backend/scopewatch/service.py:461-484`; verified by `backend/tests/test_agent_end_to_end.py::test_invariant_5_*`). `POLICY_ERROR` exists in `backend/scopewatch/models.py:32` but is never emitted; an unexpected database failure surfaces as HTTP 500 via `backend/scopewatch/errors.py:62-66`, and disabled network is a deterministic `DENY [NETWORK_DISABLED]` (`backend/scopewatch/policy.py:114-119`).
- **Two-phase durable dispatch**: Action requests, policy decisions, and `EXECUTION_STARTED` events are committed to SQLite *before* filesystem side-effects are dispatched to the executor (issue #109). If a post-execution failure occurs during receipt persistence, the attempted action and decision remain durably preserved and identifiable, avoiding untracked workspace mutations.
- **Pre-dispatch lifecycle re-check**: Immediately prior to dispatch under SQLite lock, the service re-verifies that the run has not transitioned to a terminal status (`COMPLETED` or `FAILED`) during reasoning audit latency; stale actions are rejected with `RUN_NOT_ACTIVE` before execution (issue #110).
- **Action-aware turn audit caching**: Turn-level reasoning audit caching is bound not only to `(run_id, turn_id, trace_hash)`, but also to the target action identity (`tool`, `operation`, `resource`, `arguments`). Actions within the same model turn targeting different resources trigger distinct auditor reviews instead of reusing earlier verdicts (issue #111).
- **One Docker job builder**: The image pin, container hardening flags, in-container helper source, staged-workspace copy, and symlink-tolerant copy-back walk live in `backend/scopewatch/docker_job.py` and are imported by both the Docker backend and the `executor-runner` sidecar (`deploy/executor-runner/runner.py`), which previously re-declared them (issue #106). `prepare_docker_job` returns a frozen `DockerJob` carrying its arguments and their JSON serialization together, following the `prepare_dispatch` model from #104, so the dispatched form and the serialized form cannot disagree. The shared module is stdlib-only by contract, because the sidecar is; `backend/tests/test_docker_job_share.py` asserts byte-identical argv across operations and argument shapes, that the module imports nothing outside the standard library, and that neither caller re-declares the flag list.

Key endpoints:
- `POST /api/v1/runs`: Initialize an isolated run with an explicit task scope.
- `GET /api/v1/runs`: List active and historical runs.
- `POST /api/v1/runs/{run_id}/actions`: Submit a candidate action for policy evaluation, reasoning audit, and controlled execution.
- `GET /api/v1/runs/{run_id}/events`: Return the stored event list as JSON (`backend/scopewatch/app.py:221`).
- `GET /api/v1/runs/{run_id}/events/stream`: Subscribe to live SSE events for a run (`backend/scopewatch/app.py:231`). Accepts `Last-Event-ID` or `?after_sequence=` query parameter; subscribes to future broadcast queues before querying stored events to ensure gap-free continuous delivery (issues #112, #113).
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

Every decision records the policy identity that produced it, as `policy_version` on the `policy_decisions` row and in the `details` of its `POLICY_*` event (issue #119, [ADR-0003](adr/0003-policy-identity-in-evidence.md)). The value is `"<revision label>+<rule-set fingerprint>"` — for example `2026-10-03.1+3f2a1b9c8d0e` — and is independent of the `schema_version` ("1") that describes the stored data shape. `evaluate_policy` stamps it at the single public entry point of the engine, while `PolicyDecision.policy_version` defaults to `None` so a read of a pre-version row stays visibly `unknown (legacy)` rather than being assigned the revision running today. Nothing backfills old rows, and no environment variable can override the identity. `ApprovalRequest.policy_version` is derived from the bound decision on read rather than stored twice, so the two cannot disagree. When an approval is resolved after the policy has moved on, it is honoured on the decision the reviewer saw and the drift is recorded explicitly on the event as `policy_version_current` / `policy_version_changed`.

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
- `openrouter-dev`: Developer profile configured for `nvidia/nemotron-3.5-lightning`; live inference remains blocked by provider authentication.
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
  2. Policy decision (outcome, reason code, matched rule, and the evaluated policy version — shown as `unknown (legacy)` for records written before policy versioning).
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
| **4. No decision, no execution** | Shared gate `authorize_dispatch` requires in-memory policy evidence; service persists the decision before dispatch. | `test_dispatch_gate.py::test_missing_decision_refuses`, `test_agent_end_to_end.py::test_invariant_2_*` |
| **5. Approvals are single-use** | Shared gate accepts only `APPROVED` for a held action; token consumed on resolution; cannot override policy DENY. | `test_dispatch_gate.py::test_hold_refuses_*`, `test_agent_end_to_end.py::test_invariant_2_*`, `test_invariant_3_*` |
| **6. Missing reasoning does not escalate** | Recorded as UNAVAILABLE; policy ALLOW proceeds. | `test_agent_end_to_end.py::test_invariant_6_*` |
| **7. Monotonic evidence sequence** | Monotonic event sequence; decisions precede execution. | `test_agent_end_to_end.py::test_invariant_7_*` |
| **8. Agent-executor isolation** | Agent package has zero import path to executor. | `test_agent_end_to_end.py::test_invariant_8_*` |
| **9. Evidence names its policy** | Every decision stores the evaluated `policy_version`; pre-version rows read as `unknown (legacy)` and are never backfilled; approvals resolve against the identity of their stored decision. | `test_issue_119_policy_version.py::*` |
