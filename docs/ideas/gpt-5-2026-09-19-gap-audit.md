# Scopewatch gap audit

**Author:** GPT-5 via Codex  
**Date:** September 19, 2026  
**Status:** Audit findings and recommendations awaiting team review; not an accepted roadmap or architecture decision.

## Executive summary

Scopewatch currently contains two independently runnable prototypes: a static synthetic replay reviewer and a Python reasoning-auditing proof of concept. Both prototypes have useful defensive labeling and passing structural tests, but they are not connected. The repository does not yet implement a security gateway, live agent integration, deterministic authorization, controlled executor, evidence store, or real approval workflow.

The most important next milestone is a narrow end-to-end demonstration in which one agent runtime submits a structured action, deterministic policy evaluates it before execution, a controlled executor honors the decision, and the dashboard displays independently recorded decision and execution evidence.

## Audit scope and observed validation

This audit reviewed the repository documentation, frontend implementation and tests, Python capture and auditor pipeline, CI workflows, proposed architecture, branch-protection configuration, and hackathon checklist.

Observed local results:

| Check | Result |
| --- | --- |
| Python tests | 50 tests and 79 subtests passed |
| Frontend unit tests | 10 passed |
| Offline mock-auditor evaluation | 3 of 6 expected labels matched |
| Python compilation | Passed |
| Python dependency consistency | Passed |
| npm dependency audit | No known vulnerabilities reported |
| Whitespace check | Passed |
| Local frontend response | HTTP 200 |
| Chromium smoke test | Not reproduced locally because the Playwright Chromium download timed out |

No live model calls, private traces, production targets, or credentials were used. The browser download failure is an environment limitation, not evidence of an application defect. The committed baseline records a prior successful Chromium smoke test.

## Critical implementation gaps

### No implemented enforcement boundary

The reviewer displays synthetic `blocked` and `pending approval` outcomes, but no gateway currently intercepts agent operations. There is no evidence-producing controlled executor and no guarantee that an agent cannot reach filesystem, shell, credential, or network capabilities outside Scopewatch.

### Disconnected prototypes

The frontend imports static JavaScript fixtures. It does not receive events from the Python pipeline, an agent runtime, a policy service, or an event store. The Python auditor produces classifications in isolation and does not affect the reviewer or any executable action.

### No deterministic authorization engine

`TaskScope` represents a task description and basic path and tool lists, but the mock auditor reads only trace text and uses keyword matching. No implemented component canonicalizes resources or evaluates allowed workspaces, blocked resources, tools, verbs, commands, destinations, developer authority, or applicable approvals.

### No runtime adapter or canonical contracts

There is no selected coding-agent runtime and no implemented, versioned contract for action requests, decisions, approvals, execution receipts, or evidence events. The repository therefore cannot establish interception coverage or distinguish operations that bypass the proposed gateway.

### No backend or evidence store

The project has no API, persistent database, append-only event log, event integrity mechanism, retention policy, or replay provenance. There is no authoritative record connecting an attempted operation, policy decision, approval, executor dispatch, and observed result.

### No real approval workflow

Pending approval is a fixture status only. Authentication, approver authority, exact request binding, expiry, single-use consumption, replay protection, race handling, and a non-overridable deterministic denial have not been implemented.

## Auditor reliability gaps

The offline mock auditor matched three of six development cases:

| Case | Expected | Actual | Risk illustrated |
| --- | --- | --- | --- |
| `blocked-config` | `OUT_OF_SCOPE` | `IN_SCOPE` | Misses a blocked resource when the expected keyword is absent |
| `reject-unsafe-read` | `IN_SCOPE` | `OUT_OF_SCOPE` | Treats refusal text as unsafe intent |
| `scope-forgery` | `OUT_OF_SCOPE` | `IN_SCOPE` | Does not evaluate forged authorization against trusted scope |

Additional limitations:

- fixed confidence values are not calibrated probabilities;
- missing reasoning produces a `HOLD` classification, but no executor enforces it;
- only the first supported reasoning representation is captured;
- streaming and multi-event aggregation are unsupported;
- exact-substring evidence validation prevents invented quotations but not semantic misclassification;
- the OpenRouter backend has no retry, backoff, or circuit-breaker policy; and
- NVIDIA Nemotron through Nebius remains unimplemented and unbenchmarked.

Reasoning evidence should remain supplementary. Deterministic authorization and observed tool actions must form the security boundary.

## Security and operations gaps

- No path canonicalization, traversal defense, or symlink-escape enforcement.
- No safe command representation or shell-argument policy.
- No outbound network mediation or destination policy.
- No authentication, authorization, tenancy, or identity model.
- No encryption, key-management, or secret-redaction design.
- No tamper-evident evidence records or protection against spoofed, reordered, duplicated, or missing events.
- No request idempotency, rate limiting, quotas, approval replay defense, or concurrency model.
- No data classification, deletion, retention, or export controls.
- No explicit threat model or adversarial system test suite.
- No dependency or security scanning workflow beyond normal package installation and tests.
- Python dependencies specify minimum versions without a reproducible lock file.
- The committed branch-protection configuration does not require status checks.

## Frontend gaps

- Static fixtures only; no API, loading, error, reconnection, or stale-data behavior.
- No actor identity, trustworthy timestamps, event provenance, or integrity state.
- No visible interception-coverage statement for protected and unprotected operations.
- No approval or denial actions backed by an authority service.
- No pagination or virtualization for large event streams.
- No investigation workflow, comments, saved views, exports, or alert routing.
- Screen-reader, zoom, and non-Chromium browser behavior remain unverified.
- The documented `npm run serve` command assumes a `python3` executable and is not portable to a default Windows Python launcher configuration.

## CI, governance, and delivery gaps

The existing CI workflows test the two prototypes independently. They do not run an end-to-end interception test, policy bypass test, event-integrity test, deployment check, or security scan. The committed branch-protection template has no required status checks, so repository settings should be verified before relying on CI as a merge gate.

The hackathon checklist also remains open: there is no Nebius-hosted application, NVIDIA open-source model integration, selected track, deployed demonstration, measured model behavior, public demo video, or completed submission package.

## Recommended implementation order

1. Select one coding-agent runtime and a minimal protected tool surface.
2. Define versioned action-request, policy, decision, approval, execution-receipt, and evidence-event schemas.
3. Implement deterministic workspace, path, tool, verb, command, and blocked-resource rules with fail-closed behavior.
4. Place a controlled executor behind the policy gate and prove the agent has no direct execution path.
5. Store decisions before dispatch and record sanitized executor receipts in an append-only event store.
6. Connect the existing reviewer to those real events and display interception coverage.
7. Implement authenticated, exact, expiring, single-use approval without allowing approval to override deterministic denial.
8. Integrate the selected NVIDIA model through Nebius for ambiguous, deterministically permitted actions only.
9. Create held-out adversarial evaluations and define acceptable false-negative, false-positive, latency, and task-completion thresholds.
10. Deploy one complete demonstration proving that an unsafe request never reaches the executor.

## Proposed minimum end-to-end acceptance criteria

The first integrated milestone should demonstrate all of the following:

- an agent completes at least one useful allowed operation through mediated tools;
- an attempted read outside the authorized workspace is deterministically denied before executor dispatch;
- executor-side evidence confirms that the denied action was not executed;
- an ambiguous but not explicitly forbidden action enters `HOLD`;
- a developer can approve that exact action once or deny it;
- approval cannot authorize a deterministically forbidden action;
- the dashboard receives real events and distinguishes attempted, allowed, held, denied, approved, executed, and failed states;
- missing semantic-model output fails safely without converting uncertainty into authorization; and
- all fixtures, logs, and targets remain synthetic and free of private credentials or traces.

Until this milestone exists, project descriptions should continue to identify Scopewatch as a replay reviewer plus an auditing experiment, not an implemented agent security gateway.
