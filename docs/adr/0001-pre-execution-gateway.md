# 0001: Pre-execution gateway with an open-weight reasoning agent

**Status:** Accepted
**Date:** 2026-09-23 (amended 2026-09-24)
**Deciders:** @dkritarth (with review requested from @nawailkhan and @atshalahmedkhan)

## Context

Scopewatch began with two independently runnable prototypes: a synthetic replay reviewer and a chain-of-thought (CoT) auditing experiment. These demonstrated user-facing timeline concepts and semantic classification in isolation, but no connection existed between them. There was no mediation gateway, no deterministic authorization, no controlled executor, and no real agent runtime.

To deliver a working, submittable security system for the Nebius x NVIDIA Hackathon by October 30, 2026, the team needed to resolve architectural open questions into a concrete implementation plan. The constraints are:

1. **Safety boundary:** The system must actively prevent unauthorized or out-of-scope actions before execution. Post-hoc observation alone cannot prevent damage or claim security enforcement.
2. **Hackathon alignment:** The project requires Nebius AI Cloud inference or compute and an open-weight NVIDIA model (Nemotron).
3. **Delivery timebox:** Five weeks remained from the planning session. The architecture must minimize unnecessary moving parts and dual-language maintenance.
4. **Agent-driven parallel execution:** Work must be partitioned into self-contained issues that coding agents can execute with clear invariants and minimal supervision.

During the planning session on September 23, 2026 (documented in [planning session record](../ideas/claude-opus-5.5-2026-09-23-planning-session.md)), 13 fundamental decisions were agreed upon. This ADR formally records those decisions and adopts the design proposed in [proposed Scopewatch architecture](../architecture/proposed-scopewatch-architecture.md) with explicit amendments.

## Decision

We accept [proposed Scopewatch architecture](../architecture/proposed-scopewatch-architecture.md) as the baseline architecture for Scopewatch V1, governed by the following 13 decisions:

### 1. Product shape: Pre-execution gateway
Scopewatch V1 is an active pre-execution security gateway positioned between the AI agent and the execution environment (`Agent -> Gateway -> Controlled Executor`). Deterministic policy runs first on every incoming `ActionRequest` and yields `ALLOW`, `HOLD`, or `DENY`. The reasoning auditor is an evidence source feeding that gateway, not the product itself. Model failure cannot cause unauthorized execution.

### 2. Implementation stack
The core backend is implemented in Python 3.12, FastAPI, and SQLite (used as an append-only event store behind a clean repository interface). The existing dependency-free vanilla JavaScript reviewer UI (`frontend/`) is accepted as the dashboard. The previously proposed Next.js/TypeScript rewrite is dropped. This unifies all security-critical logic in Python, avoids multi-language build complexity, and builds directly on PRs #16–#19.

### 3. Agent runtime
Scopewatch runs its own minimal agent loop with a purpose-built system prompt and a tool surface it completely controls. Core mediation does not rely on third-party agent harnesses (such as Claude Code, Codex, or t3code). Owning the tool surface ensures there is no execution bypass path by construction. External agent runtimes will be supported later via external adapters (Agent Client Protocol / ACP first), never in core.

### 4. Swappable models via provider profiles
Neither the agent loop nor the reasoning auditor hardcodes specific model IDs in core code. All model interactions are mediated by configuration-driven provider profiles specifying `base_url`, `model`, and `api_key_env`. Three standard profiles are established:
- `mock`: In-process deterministic responses for unit and integration testing.
- `openrouter-dev`: Development and testing using free or stealth endpoints.
- `nebius-demo`: Production hackathon demonstration using NVIDIA Nemotron on Nebius Token Factory.

### 5. Agent model requirement: Open-weight reasoning model
The agent model must be an open-weight reasoning model whose provider returns raw reasoning tokens (NVIDIA Nemotron by default). Closed commercial models expose only summarized thought traces, severely limiting auditor scrutiny. Closed models remain permitted only in "summary-only" mode, where any evidence presented must be strictly labeled `AGENT_AUTHORED_SUMMARY` and never confused with raw traces.

### 6. Amendment to architecture doc: Raw chain-of-thought dependency
We explicitly reverse the line in `docs/architecture/proposed-scopewatch-architecture.md` that stated "avoid making raw chain-of-thought a dependency" for the agent role. Raw chain-of-thought is a required dependency for the agent monitoring pipeline. However, reasoning is treated strictly as evidence, never proof. Visible reasoning may be unfaithful, incomplete, or manipulated via prompt injection from untrusted files. Untrusted trace isolation in the auditor is mandatory.

### 7. Reasoning authority is escalate-only
Semantic reasoning analysis has strictly escalate-only authority:
- A flagged concern can escalate a deterministic `ALLOW` into a `HOLD`.
- It can **never** produce `ALLOW`.
- It can **never** produce `DENY`.
- It can **never** relax a deterministic `DENY` or `HOLD`.
- If the reasoning audit fails, times out, or returns malformed output, the gateway fails closed by escalating an `ALLOW` to `HOLD`.
- Absent reasoning does not escalate decisions and is presented on the dashboard as visibly missing.

### 8. Audit granularity
The reasoning audit executes once per agent turn, evaluating all tool calls proposed in that turn together against the task scope. Individual per-action auditing was rejected because measured PoC latency was approximately 7.6 seconds per model call; per-turn batching maintains responsive interactive execution.

### 9. Controlled executor isolation
All actual execution occurs inside an isolated Docker container spawned per run:
- Flags: `--network none`, non-root user, read-only root filesystem, only the designated run workspace mounted, and no host home directory mounted.
- Command execution (`run_command`) accepts only allowlisted command prefixes parsed strictly with `shlex`, rejecting any shell metacharacters (`|`, `&`, `;`, `$`, `>`, `<`, etc.).

### 10. Demo domains and progression
Scopewatch implements two demonstration domains in sequence:
- **Milestone M1:** Invoice processing scenarios first, connecting the live agent loop to existing invoice fixtures. This delivers an early, submittable end-to-end baseline.
- **Milestone M2:** A software development coding scenario backed by the Docker container executor.
Track selection (e.g., Agents vs Developer Tools) is decided after M2 status is evaluated (target date: October 15, 2026).

### 11. Infrastructure and hosting
The hosted demonstration runs on a single Nebius AI Cloud VM hosting the FastAPI gateway, local executor containers, and the static dashboard UI. This satisfies the Nebius compute criteria alongside Nebius Token Factory inference.

### 12. Model data policy and reproducibility
Only synthetic fixtures, dummy credentials, and synthetic traces may be sent to any external model endpoint. Evaluation reports must pin the exact model ID and snapshot date, ensuring reproducibility even when third-party stealth endpoints change.

### 13. Agent autonomy and review governance
Autonomous coding agents may self-merge ordinary pull requests once all required CI checks pass. PRs addressing issues labeled `review: second-pass` (amended on September 24, 2026 from `review: human-required` for student team velocity) require an approving review from an independent agent thread or a human before merging. Direct pushes to `main` remain prohibited.

## Consequences

### What becomes easier
- A unified Python 3.12 codebase simplifies security auditing, dependency management, and CI testing.
- The escalate-only model eliminates the hazard of an untrusted LLM erroneously granting access or overriding deterministic safety rules.
- Owning the agent loop eliminates tool-call escape hatches and ensures every tool invocation traverses the gateway.
- Preserving the existing vanilla JS reviewer UI avoids a costly frontend rewrite while five weeks remain.
- Provider profiles allow fast local iteration and cheap synthetic development without incurring Nebius billing until deployment.

### What becomes harder
- Extracting raw reasoning from different providers requires handling diverging schemas (`reasoning_content` vs `<think>` tags).
- Running Docker containers from a containerized or VM gateway requires careful permission boundaries to prevent Docker socket exposure.
- Failing closed on reasoning audit errors means gateway availability depends on auditor latency and uptime.

### Follow-up work
- **M0:** Required CI checks on `main` (#22), gap audit refresh (#24), provider spike (#25).
- **M1:** Swappable provider profiles (#26), minimal agent loop (#27), hardened reasoning auditor backend (#28), escalate-only decision merge (#29), invoice demo integration (#32).
- **M2:** Docker container executor (#35), allowlisted `run_command` (#36), coding scenario (#37).
- **M3:** Nebius VM deployment (#42/#43), submission package and demo video (#45/#46).
- **Future:** Tamper-evident evidence log (#52), Agent Client Protocol adapter (#48/#49), severity grading (#50).

## Alternatives considered

1. **Post-hoc monitor centered on reasoning auditor:**
   *Rejected.* An observability-only system cannot stop unsafe actions before execution. If an agent deletes a database or leaks credentials, alerting after the fact fails the core security mission.

2. **Next.js and TypeScript frontend rewrite:**
   *Rejected.* The vanilla JS reviewer UI was already implemented, well-tested, accessible, and fast. Rewriting in Next.js would consume scarce hackathon time without adding security value.

3. **MCP server adapter for V1:**
   *Rejected.* Model Context Protocol mediation is client-voluntary; third-party agents retain native tool surfaces and escape hatches. Owning the agent loop guarantees comprehensive mediation by construction.

4. **Hardcoding NVIDIA Nemotron in core logic:**
   *Rejected.* Hardcoding model IDs prevents running unit tests offline with mocks and impedes development using low-cost OpenRouter endpoints.

5. **Allowing reasoning auditor to emit `DENY` or `ALLOW`:**
   *Rejected.* Giving an LLM the authority to permit actions undermines deterministic security boundaries. Giving it the authority to deny creates denial-of-service risks from uncalibrated hallucinations or adversarial prompt injection.

6. **Subprocess execution without container isolation:**
   *Rejected.* Executing agent commands directly on the host allows symlink traversal, process escape, and host credential theft even if parameters appear benign.
