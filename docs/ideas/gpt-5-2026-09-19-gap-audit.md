# Scopewatch gap audit

**Author:** GPT-5 via Codex
**Date:** September 19, 2026 (Refreshed September 25, 2026)
**Status:** Audit findings and recommendations awaiting team review; refreshed against main (PRs #16–#19, #54, #55, ADR-0001, and M0–M3 backlog).

## Audit status and refresh summary (September 25, 2026)

This document originally recorded the state of the repository on September 19, 2026, when Scopewatch consisted of two disconnected prototypes: a static synthetic reviewer UI and an experimental CoT auditing PoC.

Between September 20 and September 25, 2026, the repository advanced substantially:
- **PRs #16–#19** implemented the core FastAPI pre-execution gateway, 15-step deterministic policy engine, SQLite repository, single-use approval workflow, SSE event stream, live vanilla JS reviewer integration with simulator, five invoice-domain scenarios, and the `scripts/validate.sh` clean-room test suite.
- **PR #54** added the hardened scope auditor evaluation suite, prompt isolation, offline rule backend, and 564-line synthetic case suite.
- **PR #55** structured the repository for parallel agent threads and worktrees.
- **ADR-0001** (accepted September 23, 2026) codified the pre-execution gateway architecture, dropped Next.js in favor of the vanilla JS dashboard, established escalate-only reasoning authority, and laid out Milestones M0–M3.

Below, every original finding is preserved with its updated status: **Resolved**, **Tracked in backlog**, or **Out of scope**.

---

## Executive summary

*Original finding:* Scopewatch currently contains two independently runnable prototypes: a static synthetic replay reviewer and a Python reasoning-auditing proof of concept. Both prototypes have useful defensive labeling and passing structural tests, but they are not connected. The repository does not yet implement a security gateway, live agent integration, deterministic authorization, controlled executor, evidence store, or real approval workflow.

*Refresh update:*
- **Resolved by PRs #16–#19:** An active pre-execution security gateway, deterministic authorization engine, in-process workspace executor, SQLite evidence store, and real single-use approval workflow are now fully implemented and connected to the live reviewer UI.
- **Tracked in backlog (#27, #28, #29):** The live agent loop (#27) and connecting the hardened reasoning auditor into the runtime decision pipeline (#28, #29) are active Milestone M1 issues.

---

## Audit scope and observed validation

*Observed baseline (September 19, 2026):* 50 Python tests, 10 frontend unit tests, 3/6 mock auditor matches.

*Observed status (September 25, 2026):*
- Python backend test suite: 40 tests passed (`pytest backend/tests`).
- Hardened CoT auditing suite: 96 tests and 79 subtests passed (`pytest poc/cot-auditing`).
- Frontend unit tests: 10 tests passed (`npm test --prefix frontend`).
- Playwright browser integration tests: 2 suites passed (`npm run test:browser --prefix frontend`).
- Clean-room demo scenario verification: 5/5 scenarios passed (`./scripts/validate.sh`).

---

## Critical implementation gaps

### No implemented enforcement boundary
- *Original finding:* The reviewer displays synthetic `blocked` and `pending approval` outcomes, but no gateway currently intercepts agent operations. There is no evidence-producing controlled executor and no guarantee that an agent cannot reach filesystem, shell, credential, or network capabilities outside Scopewatch.
- **Status: Part-Resolved / Tracked in backlog.**
  - **Resolved by PRs #16, #17, #19:** The gateway intercepts all `ActionRequest` payloads and routes them through deterministic policy before reaching the executor. Denials are intercepted prior to dispatch.
  - **Tracked in backlog (#35, #36):** Hard host-level isolation (Docker container per run with `--network none`, read-only root, workspace mount only) and allowlisted `run_command` parsing with `shlex` are scheduled for Milestone M2.

### Disconnected prototypes
- *Original finding:* The frontend imports static JavaScript fixtures. It does not receive events from the Python pipeline, an agent runtime, a policy service, or an event store. The Python auditor produces classifications in isolation and does not affect the reviewer or any executable action.
- **Status: Part-Resolved / Tracked in backlog.**
  - **Resolved by PRs #17, #18, #19:** The reviewer dashboard is now directly connected to the FastAPI backend via Server-Sent Events (`/api/v1/events/stream`), receives live execution receipts and approvals, and features an interactive action simulator.
  - **Tracked in backlog (#28, #29):** Wiring the hardened reasoning auditor into the live gateway decision loop as an escalate-only evidence source is tracked in issues #28 and #29.

### No deterministic authorization engine
- *Original finding:* `TaskScope` represents a task description and basic path and tool lists, but the mock auditor reads only trace text and uses keyword matching. No implemented component canonicalizes resources or evaluates allowed workspaces, blocked resources, tools, verbs, commands, destinations, developer authority, or applicable approvals.
- **Status: Resolved by PR #16.**
  - A 15-step deterministic policy engine is implemented in `backend/scopewatch/policy/engine.py`. It enforces workspace boundaries, canonical path resolution, traversal prevention, explicitly blocked paths, tool/verb allowlists, network disabled state, and single-use approval validation.

### No runtime adapter or canonical contracts
- *Original finding:* There is no selected coding-agent runtime and no implemented, versioned contract for action requests, decisions, approvals, execution receipts, or evidence events. The repository therefore cannot establish interception coverage or distinguish operations that bypass the proposed gateway.
- **Status: Part-Resolved / Tracked in backlog.**
  - **Resolved by PRs #16, #17:** Canonical Pydantic schemas for `ActionRequest`, `PolicyDecision`, `ApprovalRequest`, `ExecutionReceipt`, and `TimelineEvent` are implemented in `backend/scopewatch/domain/models.py`.
  - **Tracked in backlog (#27):** Scopewatch minimal agent loop with purpose-built system prompt and controlled tool surface.
  - **Tracked in backlog (#48, #49):** Agent Client Protocol (ACP) adapter for external coding agents (stretch).

### No backend or evidence store
- *Original finding:* The project has no API, persistent database, append-only event log, event integrity mechanism, retention policy, or replay provenance. There is no authoritative record connecting an attempted operation, policy decision, approval, executor dispatch, and observed result.
- **Status: Part-Resolved / Tracked in backlog.**
  - **Resolved by PRs #16, #17:** FastAPI application with SQLite repository (`backend/scopewatch/storage/repository.py`) storing runs, action requests, policy decisions, approvals, execution receipts, and timeline events in append-only tables.
  - **Tracked in backlog (#52):** Cryptographic SHA-256 tamper-evident hash chain for audit events.

### No real approval workflow
- *Original finding:* Pending approval is a fixture status only. Authentication, approver authority, exact request binding, expiry, single-use consumption, replay protection, race handling, and a non-overridable deterministic denial have not been implemented.
- **Status: Resolved by PRs #16, #17, #18.**
  - Real server-side approval lifecycle implemented: approvals are bound to exact run ID and action hash, expire after timeout, are consumed exactly once, and cannot override deterministic denials. Supported by `/api/v1/approvals/{id}/resolve` and interactive UI controls.

---

## Auditor reliability gaps

- *Original finding:* Offline mock auditor matched 3 of 6 cases; keyword matching failed on edge cases; prompt injection vulnerability; missing reasoning; fixed confidence values; unverified Nemotron.
- **Status: Part-Resolved / Tracked in backlog.**
  - **Resolved by PR #15:** Mock auditor made scope-aware.
  - **Resolved by PR #54:** Hardened prompt, untrusted-trace boundary isolation, offline rule-based fallback backend, 564-line synthetic case suite, and comprehensive evaluation runner (`evaluate_hardened_auditor.py`).
  - **Formalized in ADR-0001:** Missing reasoning produces `HOLD` when audited; reasoning is strictly escalate-only (`ALLOW -> HOLD`); absent reasoning displayed as visibly missing.
  - **Tracked in backlog (#25, #26):** Provider spike (#25) and provider profiles (#26) verify Nemotron on Nebius Token Factory and OpenRouter.
  - **Tracked in backlog (#28, #29):** Integration of hardened auditor into backend with escalate-only merge.
  - **Tracked in backlog (#30):** Held-out adversarial evaluation and calibrated threshold tuning.

---

## Security and operations gaps

- *Path canonicalization, traversal defense, symlink escapes:*
  **Resolved by PR #16.** `backend/scopewatch/security/path.py` canonicalizes paths, verifies workspace boundaries, and rejects traversal sequences (`..`).
- *Safe command representation and shell argument policy:*
  **Tracked in backlog issue #36.** `run_command` parsed strictly with `shlex` against an allowlist; shell metacharacters rejected.
- *Outbound network mediation:*
  **Resolved in policy by PR #16 (`NETWORK_DISABLED`); container network isolation (`--network none`) tracked in backlog issue #35.**
- *Authentication, authorization, tenancy model:*
  **Decided in ADR-0001 (Out of scope for V1).** Single-user local gateway for a developer running one agent. Enterprise multi-tenancy is deferred.
- *Tamper-evident evidence records:*
  **Tracked in backlog issue #52.** SHA-256 hash chaining of audit records.
- *Request idempotency, rate limiting, approval replay defense:*
  **Resolved by PR #16.** Run-level serialization and single-use token consumption prevent approval replays.
- *Adversarial system test suite:*
  **Resolved by PR #19 (`scripts/validate.sh`); container bypass tests tracked in backlog issue #38.**
- *Branch-protection configuration without required status checks:*
  **Resolved by Issue #22.** Required status checks (`backend`, `cot-auditing`, `reviewer-ui`) configured in `.github/branch-protection.json` and applied to `main`.
- *Python dependency lock file:*
  **Tracked in backlog issue #20 (Milestone M0).**

---

## Frontend gaps

- *Static fixtures only / No API or live data:*
  **Resolved by PR #18.** Connected to backend API and live SSE stream (`/api/v1/events/stream`), with active action simulator and live approval buttons.
- *No visible interception coverage statement:*
  **Tracked in backlog issue #31.** Adding explicit mediated vs unmediated capability coverage banner.
- *No approval actions backed by authority service:*
  **Resolved by PRs #17, #18.** Live UI invokes backend approval endpoint.
- *Next.js / TypeScript dashboard:*
  **Decided in ADR-0001 (Out of scope).** Next.js rewrite dropped; existing dependency-free vanilla JS dashboard accepted.
- *Pagination / large stream virtualization / team investigation workflow:*
  **Out of scope for V1 hackathon submission.**
- *Windows Python launcher command:*
  **Resolved in docs/governance.** Development commands standardized on `python3` and Linux shell scripts.

---

## CI, governance, and delivery gaps

- *CI workflows not required status checks:*
  **Resolved by Issue #22.** `backend`, `cot-auditing`, and `reviewer-ui` made required status checks on `main`.
- *End-to-end clean-room validation suite:*
  **Resolved by PR #19.** `./scripts/validate.sh` runs full lint, test, build, and demo scenario checks.
- *Parallel agent threads governance:*
  **Resolved by PR #55.** AGENTS.md, role definitions, and worktree automation.
- *Hackathon delivery (Nebius VM, NVIDIA Nemotron, video, submission):*
  **Tracked in Milestone M3 (#42, #43, #45, #46).**

---

## Recommended implementation order

1. *Select coding-agent runtime and minimal tool surface:*
   **Resolved in ADR-0001; Agent loop tracked in #27.**
2. *Define versioned schemas:*
   **Resolved by PRs #16, #17.**
3. *Implement deterministic workspace, path, tool, verb rules:*
   **Resolved by PR #16.**
4. *Place controlled executor behind policy gate:*
   **Resolved for in-process workspace by PR #16; Docker isolation tracked in #35.**
5. *Store decisions and receipts in append-only event store:*
   **Resolved by PR #16, #17.**
6. *Connect reviewer to real events and display coverage:*
   **Resolved by PR #18; coverage banner tracked in #31.**
7. *Implement exact, single-use approvals:*
   **Resolved by PRs #16, #17, #18.**
8. *Integrate selected NVIDIA model through Nebius / OpenRouter:*
   **Tracked in spike #25 and issue #26.**
9. *Held-out adversarial evaluations and thresholds:*
   **Hardened suite landed in PR #54; held-out evaluation tracked in #30.**
10. *Deploy complete demonstration proving unsafe requests never reach executor:*
    **In-process demo validated in PR #19; hosted Nebius VM deployment tracked in #42, #43.**

---

## Proposed minimum end-to-end acceptance criteria

| Criterion | Current Status | Validation |
| --- | --- | --- |
| Agent completes useful allowed operation | **Satisfied** | Approved invoice audit scenario in `scripts/validate.sh` |
| Attempted read outside workspace deterministically denied | **Satisfied** | Confidential directory & path traversal scenarios in `scripts/validate.sh` |
| Executor evidence confirms denied action was not executed | **Satisfied** | `ExecutionReceipt(dispatched=False, status=NOT_EXECUTED)` recorded in SQLite |
| Ambiguous action enters `HOLD` | **Satisfied** | File deletion scenario in `scripts/validate.sh` triggers `HOLD` |
| Developer approves once or denies | **Satisfied** | Server-side single-use approval resolution in API & UI |
| Approval cannot override deterministic denial | **Satisfied** | Deterministic denials reject approval overrides in `policy/engine.py` |
| Dashboard receives real events and distinguishes states | **Satisfied** | Live reviewer UI receives SSE stream and colors states |
| Missing semantic-model output fails safely | **Satisfied / Formalized** | Invariant established in ADR-0001; merge tracked in #29 |
| Fixtures and logs remain synthetic | **Satisfied** | Synthetic-only policy enforced across repository |
