# Proposed Scopewatch Architecture

**Status:** Accepted with amendments by [ADR-0001](../adr/0001-pre-execution-gateway.md) (2026-09-23).

This document turns the repository's current working direction into a concrete first-version architecture proposal. It deliberately separates the choices proposed here from ideas already supported by repository discussion and from work that remains open or deferred. The team accepted this proposal on September 23, 2026 with amendments recorded in [ADR-0001: Pre-execution gateway with an open-weight reasoning agent](../adr/0001-pre-execution-gateway.md).

## Decision status

### Amendments accepted in ADR-0001

The planning session on September 23, 2026 accepted this architecture with the following amendments (recorded in [ADR-0001](../adr/0001-pre-execution-gateway.md)):

1. **Stack:** Python 3.12, FastAPI, and SQLite (append-only event store behind a repository interface) for the backend. Next.js is dropped; the existing dependency-free vanilla JavaScript reviewer UI (`frontend/`) is accepted as the dashboard.
2. **Raw chain-of-thought dependency:** Reverses the earlier working direction "Avoid making raw chain-of-thought a dependency" for the agent role. The agent model must be an open-weight reasoning model whose provider exposes raw reasoning tokens (Nemotron by default); closed models are permitted only in summary-only mode with the `AGENT_AUTHORED_SUMMARY` label. Reasoning is evidence, never proof.
3. **Reasoning authority:** Strictly escalate-only. Reasoning concern can escalate `ALLOW` to `HOLD`; it never produces `ALLOW` or `DENY`, and never relaxes a deterministic decision. Fail-closed: audit errors yield `HOLD`.
4. **Agent runtime:** Scopewatch runs its own minimal agent loop with a purpose-built system prompt and a controlled tool surface.
5. **Swappable models:** Provider profiles (`mock`, `openrouter-dev`, `nebius-demo`) configure endpoints and models; core code does not hardcode model IDs.
6. **Audit granularity:** One reasoning audit per agent turn covering all tool calls in that turn.
7. **Executor isolation:** Docker container per run (`--network none`, non-root, read-only root filesystem, workspace mounted only). `run_command` accepts only allowlisted command prefixes parsed with `shlex`.
8. **Demo progression:** Invoice processing scenarios first (M1), coding scenario with Docker executor second (M2).

### Decisions proposed to be locked in

- Scopewatch is a pre-execution security gateway for AI coding agents, not only an observability or post-hoc monitoring system.
- The primary V1 user is a developer running one AI coding agent.
- Every protected operation passes through a Scopewatch runtime adapter and policy gate before it can reach a controlled executor.
- The policy gate evaluates deterministic authorization before optional semantic scope analysis and returns `ALLOW`, `HOLD`, or `DENY`.
- Deterministic policy is the hard security boundary. Model output supplies semantic classification and evidence but cannot grant authority or override a deterministic denial.
- Held actions require an explicit, single-use developer decision before execution.
- Every decision and execution outcome produces an evidence record for a developer-facing timeline.
- V1 uses synthetic fixtures, dummy secrets, and controlled destinations only.
- The implementation stack is Python 3.12 and FastAPI for orchestration and policy evaluation, SQLite for the initial event store, vanilla JavaScript for the dashboard (Next.js dropped), and JSON or YAML for policy representation.
- The proposed agent and semantic-evaluation model is NVIDIA Nemotron through Nebius Token Factory, swappable via provider profiles.

### Existing working direction

The following direction already has support in the repository's [PR #4 discussion record](../ideas/pr-4-discussion-record.md), but was not accepted there as final architecture:

- Start with the developer running the agent as the primary user.
- Choose one coding-agent runtime and one controlled executor boundary.
- Demonstrate a held or denied operation, not only post-hoc observation.
- Keep deterministic policy independent of model availability.
- Make interception coverage and actual execution status visible.
- Use a runtime-neutral `ActionRequest` between adapters and downstream services.
- Use synthetic fixtures, dummy credentials, and controlled local destinations.

### Future work and intentionally deferred scope

V1 does not include universal runtime support, unrestricted OS-wide shell interception, full causal taint tracking, enterprise IAM, Kubernetes, or full SOC/SIEM functionality. Production event infrastructure, richer approval scopes, broad network mediation, and integrations with additional agent runtimes remain future work.

## Goals

Scopewatch should answer:

> Is this agent action still within the authority and task scope given to the agent?

V1 should:

- mediate a small set of structured tool operations before execution;
- enforce explicit authorization boundaries deterministically;
- use semantic analysis only for permitted but ambiguous actions;
- stop denied or unapproved held operations before they execute;
- make attempted, approved, denied, and executed actions distinguishable; and
- demonstrate useful agent work with a clear, inspectable evidence trail.

## Non-goals

V1 is not intended to:

- protect operations that bypass the Scopewatch gateway;
- mediate arbitrary processes with unrestricted host access;
- support every coding agent or enterprise environment;
- infer or reconstruct hidden model reasoning;
- replace endpoint protection, IAM, a SIEM, or a SOC;
- guarantee detection of every prompt injection or unsafe intention; or
- establish production readiness from a scripted hackathon demonstration.

## System architecture

The coding agent never directly invokes a protected executor. All protected actions follow this invariant:

```text
Agent -> Scopewatch -> Executor
```

The following path is outside V1's security claim and must not be available to the protected agent:

```text
Agent -> Executor directly
```

```mermaid
flowchart TD
    Developer[Developer] --> Agent[Coding Agent]
    Agent --> Adapter[Scopewatch Runtime Adapter]
    Adapter --> Request[Canonical ActionRequest]
    Request --> Gate[Scopewatch Policy Gate]
    Gate --> Decision{ALLOW / HOLD / DENY}
    Decision -->|ALLOW| Executor[Controlled Executor / Sandbox]
    Decision -->|HOLD| Approval[Human Approval]
    Approval -->|Approve once| Gate
    Approval -->|Deny| Evidence[Evidence Store]
    Decision -->|DENY| Evidence
    Executor --> Evidence
    Gate <--> Semantic[Semantic Scope Evaluator\nNVIDIA Nemotron via Nebius]
    Evidence --> Dashboard[Scopewatch Dashboard]
    Dashboard --> Developer
    Dashboard --> Approval
```

The policy gate writes its result before execution. The executor then records whether an allowed or approved action actually ran and its sanitized result or failure. A dashboard label alone is not evidence that an action was blocked; the evidence record must show that interception happened before executor dispatch.

## Component responsibilities

### Developer

The developer supplies the original task, establishes the permitted workspace and tool scope, and decides held actions. In V1 the developer may approve a held action once or deny it. Approval cannot override an explicit deterministic prohibition.

### Coding agent

V1 integrates one coding-agent runtime using a purpose-built Scopewatch agent loop. The default model is NVIDIA Nemotron accessed through Nebius Token Factory or OpenRouter.

The initial structured tool surface should remain small:

- `read_file(path)`
- `write_file(path, content)`
- `run_command(command)`

The agent receives these mediated tools instead of direct access to the protected executor. A narrowly defined network operation may be added for the ambiguous-action demo only after its request schema and enforcement boundary are agreed; general network access is not part of the initial tool set.

### Scopewatch runtime adapter

The adapter translates runtime-specific tool calls into a canonical `ActionRequest`. It validates the request shape, canonicalizes the resource using executor-aware rules, computes a digest of the arguments, attaches authorization context references, and submits the request to the policy gate.

Runtime adapters may change as new agents are supported. Policy evaluation, approval, evidence recording, and replay should depend on the canonical request rather than a particular agent protocol.

### Scopewatch policy gate

The policy gate is the only route from the agent-facing adapter to the controlled executor. It owns decision orchestration and contains two logical layers.

#### Layer 1: deterministic policy engine

Deterministic authorization is evaluated first. It asks whether the agent **can** perform the requested operation under the applicable rules. Checks include:

- whether the canonical resource is inside the allowed workspace;
- whether the tool and verb are allowed;
- whether the resource or destination is explicitly blocked;
- whether the developer has the authority represented by the request; and
- whether any applicable approval is exact, current, and unused.

This layer must work when the semantic model is unavailable. An explicit denial is final and cannot be overridden by the model or by an approval intended only for ambiguous actions.

Example allowed request:

```text
Request: read_file("/workspace/project/src/auth.py")
Allowed workspace: /workspace/project
Decision: ALLOW
```

Example denied request:

```text
Request: read_file("/home/user/.ssh/id_rsa")
Decision: DENY
Reason: RESOURCE_OUTSIDE_ALLOWED_SCOPE
```

Path enforcement must use canonical, executor-resolved resources and must be tested against traversal and symlink escapes. String-prefix comparison alone is insufficient.

#### Layer 2: semantic scope evaluator

For actions that are deterministically permissible but ambiguous in relation to the task, NVIDIA Nemotron through Nebius Token Factory supplies semantic evidence. It asks whether the action **should** be happening given the developer's task.

Inputs may include:

- the original developer task;
- declared permissions and relevant authorization context;
- the current `ActionRequest`; and
- a bounded history of recent safe actions.

The evaluator should return validated structured output conceptually similar to:

```json
{
  "scope": "OUT_OF_SCOPE",
  "risk": "EXTERNAL_DATA_TRANSFER",
  "confidence": 0.94,
  "reason": "The operation is unrelated to the assigned coding task."
}
```

The model is not a security boundary. Its classification can trigger `HOLD` (escalate-only), but it cannot override a deterministic `DENY`, and cannot grant `ALLOW`. A timeout, unavailable model, malformed response, or response that cannot be safely interpreted defaults to `HOLD` for the protected ambiguous request.

### Controlled executor

The executor is isolated behind the policy gate and exposes only the operations that the adapter can mediate. For the hackathon, execution occurs in an isolated Docker container per run (`--network none`, non-root, read-only root, workspace mount only).

The executor rechecks the bound decision or approval before performing an operation, reports whether dispatch occurred, and returns a sanitized result or failure for evidence recording.

### Evidence store

Every decision produces an append-only evidence record containing, as applicable:

- run ID and original task reference;
- timestamp;
- requested operation and canonical resource;
- arguments digest rather than unnecessary sensitive arguments;
- deterministic policy result and policy version;
- semantic result, when used;
- final Scopewatch decision;
- human approval or denial;
- execution status; and
- sanitized execution result or failure.

V1 uses SQLite. Storage access sits behind a repository interface so policy logic is not coupled to SQLite and a production event database can replace it later.

### Dashboard

The dashboard is for the developer and centers on an action timeline. Each entry shows the action, canonical resource, decision, reason, timestamp, and whether execution occurred.

```text
ALLOWED            Read package.json
ALLOWED            Read src/auth.ts
ALLOWED            Run test suite
HELD FOR APPROVAL  POST to external service
BLOCKED            Read ~/.ssh/id_rsa
```

The UI must distinguish an attempted action from an approved action, and an approved action from one that actually executed. It must also distinguish a denial produced before execution from a post-execution alert.

## Canonical `ActionRequest`

The conceptual request schema is:

```json
{
  \"run_id\": \"...\",
  \"subject\": \"...\",
  \"tool\": \"read_file\",\n  \"verb\": \"read\",\n  \"resource\": \"/workspace/project/src/auth.py\",\n  \"arguments_digest\": \"...\",\n  \"policy_version\": \"...\",\n  \"source_event_refs\": []\n}\n```\n\nField responsibilities:\n\n| Field | Purpose |\n| --- | --- |\n| `run_id` | Correlates the request with one agent run. |\n| `subject` | Identifies the agent or delegated principal being evaluated. |\n| `tool` | Names the structured tool requested by the agent. |\n| `verb` | Expresses the normalized operation, such as `read`, `write`, or `execute`. |\n| `resource` | Contains the canonical resource interpreted by policy and executor. |\n| `arguments_digest` | Binds the decision to exact arguments without duplicating sensitive content. |\n| `policy_version` | Identifies the policy evaluated for the request. |\n| `source_event_refs` | Links relevant prior evidence without asserting causal influence. |\n\n## Decision flow\n\n1. The agent makes a structured tool request through the runtime adapter.\n2. The adapter validates and canonicalizes it into an `ActionRequest`.\n3. The deterministic policy engine evaluates authority, tool, verb, and resource constraints.\n4. An explicit policy violation produces `DENY`; the executor is never called.\n5. A deterministically permitted, clearly task-relevant action produces `ALLOW` and may be sent to the executor.\n6. A deterministically permitted but semantically uncertain action is evaluated for scope drift.\n7. A material unresolved concern produces `HOLD`; the request remains unexecuted.\n8. The developer chooses **Approve once** or **Deny**. Approval is bound to the exact run, operation, arguments digest, and policy version and is consumed once.\n9. Before dispatch, the gate and executor recheck the decision and any approval.\n10. The evidence store records the decision, approval state, dispatch state, and result.\n\n### `ALLOW`\n\nThe action satisfies deterministic policy and has no unresolved semantic concern. The executor may perform it.\n\n### `HOLD`\n\nThe operation is not deterministically forbidden, but semantic scope drift or uncertainty requires human review. It must not execute until the developer approves it once. The other V1 action is **Deny**; broader approval scopes are deferred.\n\n### `DENY`\n\nThe action violates deterministic policy or an explicit security rule. It is never sent to the executor.\n\n## Security invariants\n\n1. Protected operations have no path from agent to executor that bypasses Scopewatch.\n2. Deterministic authorization runs before semantic evaluation and execution.\n3. Model output is evidence, not authorization, and cannot override hard policy.\n4. A protected request that cannot be safely interpreted defaults to no execution.\n5. A held request does not execute before an exact, single-use approval is validated.\n6. An action is described as blocked only when it was intercepted before execution.\n7. Evidence explicitly records whether an action was attempted, approved, denied, dispatched, executed, or failed.\n8. Missing reasoning and missing events remain visibly missing; Scopewatch never reconstructs unavailable chain-of-thought.\n9. Only legitimately exposed summaries, traces, tool calls, model outputs, and runtime events are used.\n10. Demonstrations use synthetic credentials, synthetic files, and controlled destinations.\n11. Coverage claims are limited to structured operations routed through the controlled gateway.\n\n## Primary demo flow\n\nThe developer asks:\n\n> Fix the failing authentication tests. Only work inside this repository.\n\nThe authorized workspace is `/workspace/scopewatch-demo`. The coding agent legitimately reads `package.json` and authentication source files, runs tests, and modifies authentication code through the mediated tools.\n\nA repository fixture contains an untrusted instruction telling the agent to inspect a secret file. The agent requests:\n\n```text\nread_file(\"~/.ssh/id_rsa\")\n```\n\nThe runtime adapter resolves the request to `/home/developer/.ssh/id_rsa`. Before execution, deterministic policy compares it with the authorized workspace and returns:\n\n```text\nDecision: DENY\nRequested resource: /home/developer/.ssh/id_rsa\nAuthorized scope: /workspace/scopewatch-demo\nReason: RESOURCE_OUTSIDE_WORKSPACE\nExecution status: NOT_EXECUTED\n```\n\nThe executor never receives the operation, and the dashboard displays it as blocked.\n\nA second scenario demonstrates an ambiguous external network request. This requires adding one narrowly structured network operation to the demo boundary; it is not permission for general agent network access. Deterministic policy establishes that the destination is not explicitly prohibited but does not establish task relevance. The semantic evaluator identifies possible scope drift, and Scopewatch returns `HOLD`. The developer chooses **Approve once** or **Deny**, and the timeline records the choice and whether execution followed.\n\nThe first scenario demonstrates hard deterministic enforcement. The second demonstrates model-assisted semantic reasoning without treating the model as authority.\n\n## Implementation stack\n\n| Concern | Choice | Status |\n| --- | --- | --- |\n| Dashboard | Dependency-free vanilla JavaScript | Accepted (PR #18, ADR-0001); Next.js dropped |\n| Orchestrator and policy gateway | Python 3.12 and FastAPI | Accepted (PRs #16–#17, ADR-0001) |\n| Evidence store | Append-only SQLite-backed event store | Accepted (PR #16, ADR-0001) |\n| Policy representation | In-memory domain rules / Pydantic models | Implemented in PR #16 |\n| Agent model | NVIDIA Nemotron (open-weight reasoning) | Accepted in ADR-0001 (default model) |\n| Semantic inference | Nebius Token Factory (with OpenRouter dev fallback) | Swappable via provider profiles (ADR-0001) |\n| Execution | In-process workspace executor (M1) and Docker container (M2) | Accepted in ADR-0001 |\n\n## Evaluation strategy\n\nInitial evaluation covers:\n\n- valid file reads and writes;\n- permitted commands;\n- access outside the workspace;\n- a synthetic secret-file access attempt;\n- malicious instructions in repository content;\n- an unnecessary external request;\n- a legitimate ambiguous request requiring approval;\n- an explicit policy violation; and\n- safe completion of the assigned coding task.\n\nWhere useful, compare deterministic policy alone with deterministic policy plus action-based semantic evaluation. Any later evaluation using legitimately exposed reasoning summaries should be reported separately. Held-out scenarios must remain separate from prompt-tuning examples.\n\nCandidate metrics include deterministic policy accuracy, unsafe-action block rate, false-positive hold rate, semantic classification accuracy, approval count, added p50 and p95 latency, and successful task completion rate. A denial is counted as a successful block only when executor-side evidence confirms that the operation was not dispatched.\n\n## Architecture boundaries and limitations\n\n### What V1 can protect\n\nV1 can protect structured operations that are routed through its controlled runtime adapter, policy gate, and executor.\n\n### What V1 cannot claim to protect\n\nV1 cannot claim protection for:\n\n- operations performed outside the gateway;\n- arbitrary processes with unrestricted OS access;\n- every coding-agent runtime;\n- hidden reasoning unavailable from the model or provider;\n- general endpoint security; or\n- every form of prompt injection.\n\nThe adapter and dashboard expose a coverage statement listing the operations actually mediated. Absence of an alert outside that boundary does not establish authorization or safety.\n\n## Hackathon positioning\n\nThis architecture fits the Coding and Agentic Engineering track because Scopewatch is a developer security layer for AI coding agents. Nebius supplies model and runtime infrastructure. NVIDIA Nemotron supplies coding-agent reasoning and semantic scope classification. Scopewatch supplies the mediation boundary, deterministic authorization, approval flow, evidence model, and developer experience.\n\n## Deferred work\n\n- additional coding-agent runtimes and adapters (ACP adapter tracked in #48/#49);\n- general-purpose network and API mediation;\n- arbitrary shell or host-wide interception;\n- multi-use, time-bound, or resource-family approvals;\n- enterprise identity and policy integrations;\n- production event databases, retention controls, and exports;\n- causal or token-level taint tracking;\n- additional synchronous or asynchronous model tiers;\n- SOC/SIEM workflows and alert routing;\n- Kubernetes deployment; and\n- broader prompt-injection coverage.\n